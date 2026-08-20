"""``save`` 之后的 flash 等待（RAY-182）。

这条缺陷同样是**真机教会的**：设备在执行 `FF AA 00 00 00`（保存到 flash）期间，
对部分寄存器回读出中间状态。实测 `save` 之后立刻读 `0x64` 电量寄存器，4/4 读到
0——而 0 是电压 ×100，物理上不可能；等 0.5 秒后 4/4 正常。

写事务的文档说它是原子的。在设备仍处于 flash 写入状态时就返回，这个承诺就不成立。
"""

from __future__ import annotations

import asyncio
import struct

from wt901.device import WT901Device
from wt901.protocol.commands import save
from wt901.protocol.frames import FRAME_LENGTH, HEADER, FrameFlag
from wt901.protocol.registers import Register, ReturnRate
from wt901.transport.memory import MemoryTransport

SETTLE = 0.05
"""测试里用的 flash 等待，取一个能测但不拖慢套件的值。"""


def register_frame(start: int, values: tuple[int, ...]) -> bytes:
    body = (
        bytes([HEADER, FrameFlag.REGISTER])
        + struct.pack("<H", start)
        + struct.pack("<4h", *values)
    )
    return body.ljust(FRAME_LENGTH, b"\x00")


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    device.registers.save_delay = SETTLE
    device.registers.read_timeout = 0.05
    await device.open()
    return device, transport


async def test_persisted_write_waits_for_the_flash_write() -> None:
    device, _ = await _opened()
    loop = asyncio.get_running_loop()

    start = loop.time()
    await device.registers.set_output_rate(ReturnRate.HZ_100)
    elapsed = loop.time() - start

    assert elapsed >= SETTLE
    await device.close()


async def test_write_without_persist_does_not_wait() -> None:
    """不保存就不写 flash，也就没有那个窗口——重连后的配置重放走的正是这条路径。

    若把等待加在 `persist` 之外，每次自动重连都会白白多花半秒，而它一次 flash
    都没写。
    """
    device, _ = await _opened()
    loop = asyncio.get_running_loop()

    start = loop.time()
    await device.registers.write(Register.RRATE, ReturnRate.HZ_100, persist=False)
    elapsed = loop.time() - start

    assert elapsed < SETTLE
    await device.close()


async def test_the_settle_is_inside_the_transaction_lock() -> None:
    """**核心回归**：并发的读不能挤进 flash 写入窗口。

    等待若放在事务锁外面，一个并发读就能在设备仍在写 flash 时发出指令，拿到一个
    看着正常却是中间态的值——真机上那正是电量读成 0 的样子。所以这里断言的不是
    「等了」，而是「等的期间锁还握着」。
    """
    device, transport = await _opened()
    loop = asyncio.get_running_loop()

    async def responder() -> None:
        # 读指令一旦发出就作答，这样读的耗时里不含等待响应的时间。
        while True:
            if any(w.startswith(b"\xff\xaa\x27") for w in transport.writes):
                transport.feed(register_frame(Register.POWER, (400, 0, 0, 0)))
                return
            await asyncio.sleep(0)

    serving = asyncio.ensure_future(responder())
    writing = asyncio.ensure_future(
        device.registers.set_output_rate(ReturnRate.HZ_100)
    )
    await asyncio.sleep(0)  # 让写事务先拿到锁

    start = loop.time()
    await device.registers.read(Register.POWER)
    read_elapsed = loop.time() - start

    await writing
    await serving

    # 读被写事务挡住，因此它自己也至少等了一个 flash 窗口。
    assert read_elapsed >= SETTLE
    # 而且读指令确实排在 save 之后发出。
    commands = [w for w in transport.writes if w.startswith(b"\xff\xaa")]
    assert save() in commands
    assert commands.index(save()) < next(
        i for i, w in enumerate(commands) if w.startswith(b"\xff\xaa\x27")
    )
    await device.close()
