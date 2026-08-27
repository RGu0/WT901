"""并发寄存器事务的回归测试（RAY-177）。

这两类缺陷是**真机教会的**，离线测试原本完全漏掉：

1. 并发读没有串行化 → 多条 GATT 写同时打到同一特征 → bleak 的 CoreBluetooth
   后端让其中一条永久挂起。
2. 写入不在任何超时之内 → 挂起是静默且永久的。

原来的 `MemoryTransport.write` 立即返回，不存在「写入未完成」这个状态，所以
无论怎么写测试都碰不到。修复的同时把这个状态也建模出来。
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import register_frame, registers
from wt901.device import WT901Device
from wt901.errors import TransportTimeoutError
from wt901.protocol.registers import Bandwidth, Register, ReturnRate
from wt901.transport.ble import BleTransport
from wt901.transport.memory import MemoryTransport

POLLED_REGISTERS = (
    Register.HX,
    Register.Q0,
    Register.TEMPERATURE,
    Register.POWER,
)
"""TelemetryPoller 启动瞬间会同时读的四个寄存器——正是触发真机挂起的组合。"""


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    device.registers.save_delay = 0.0
    device.registers.read_timeout = 0.05
    await device.open()
    return device, transport


# ----- 串行化 --------------------------------------------------------------


async def test_concurrent_reads_issue_one_command_at_a_time() -> None:
    """**核心回归**：四个并发读在任一响应到达前只能发出一条指令。

    修复前这里会是 4 条——四条 GATT 写同时打到同一个特征上，而 bleak 的
    CoreBluetooth 后端对同一特征只维护一个待完成写入的 future。
    """
    device, transport = await _opened()
    tasks = [
        asyncio.get_running_loop().create_task(device.registers.read(register))
        for register in POLLED_REGISTERS
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(transport.writes) == 1, (
        f"并发读没有串行化：响应到达前已发出 {len(transport.writes)} 条指令"
    )

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await device.close()


async def test_concurrent_reads_all_resolve_without_crosstalk() -> None:
    """串行化之后每个读仍要拿到**自己那个地址**的响应。"""
    device, transport = await _opened()
    device.registers.read_timeout = 1.0
    answers = {
        Register.HX: registers(11, 12, 13, 14),
        Register.Q0: registers(21, 22, 23, 24),
        Register.TEMPERATURE: registers(31, 32, 33, 34),
        Register.POWER: registers(41, 42, 43, 44),
    }

    async def serve() -> None:
        answered = 0
        while True:
            for command in transport.writes[answered:]:
                answered += 1
                if len(command) == 5 and command[2] == Register.READADDR:
                    values = answers.get(command[3])
                    if values is not None:
                        transport.feed(register_frame(command[3], values))
            await asyncio.sleep(0.001)

    serving = asyncio.get_running_loop().create_task(serve())
    try:
        results = await asyncio.gather(
            *(device.registers.read(register) for register in POLLED_REGISTERS)
        )
    finally:
        serving.cancel()

    for register, response in zip(POLLED_REGISTERS, results, strict=True):
        assert response.start_register == register
        assert response.values == answers[register]
    await device.close()


async def test_read_and_write_are_mutually_exclusive() -> None:
    """读事务进行中时写要等待，反之亦然——它们共用同一条链路。"""
    device, transport = await _opened()
    device.registers.write_delay = 0.0
    device.registers.save_delay = 0.02

    read_task = asyncio.get_running_loop().create_task(
        device.registers.read(Register.HX)
    )
    await asyncio.sleep(0)
    write_task = asyncio.get_running_loop().create_task(
        device.registers.set_output_rate(ReturnRate.HZ_50)
    )
    await asyncio.sleep(0)

    # 读还没结束，写的第一条（解锁）不该已经发出去。
    assert transport.writes == [bytes.fromhex("ffaa273a00")]

    await asyncio.gather(read_task, write_task, return_exceptions=True)
    await device.close()


async def test_concurrent_writes_still_do_not_interleave() -> None:
    """把锁扩到读之后，写的原有保证不能退化。"""
    device, transport = await _opened()
    await asyncio.gather(
        device.registers.set_output_rate(ReturnRate.HZ_50),
        device.registers.set_bandwidth(Bandwidth.HZ_20),
    )
    assert len(transport.writes) == 6
    for chunk in (transport.writes[:3], transport.writes[3:]):
        assert chunk[0] == bytes.fromhex("ffaa6988b5")
        assert chunk[2] == bytes.fromhex("ffaa000000")
    await device.close()


# ----- 写入超时 ------------------------------------------------------------


class _StallingClient:
    """一个 GATT 写永不完成的假客户端。

    这正是真机上并发写同一特征时发生的事，而修复前它不在任何超时之内。
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.is_connected = False
        self.services = _full_services()

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def start_notify(self, characteristic: object, callback: object) -> None:
        return None

    async def stop_notify(self, characteristic: object) -> None:
        return None

    async def write_gatt_char(self, characteristic: object, data: bytes) -> None:
        await asyncio.Event().wait()  # 永远不返回


def _full_services() -> list[object]:
    from wt901.transport.ble import (
        NOTIFY_CHARACTERISTIC_UUID,
        SERVICE_UUID,
        WRITE_CHARACTERISTIC_UUID,
    )

    class Characteristic:
        def __init__(self, uuid: str) -> None:
            self.uuid = uuid

    class Service:
        def __init__(self) -> None:
            self.uuid = SERVICE_UUID
            self.characteristics = [
                Characteristic(NOTIFY_CHARACTERISTIC_UUID),
                Characteristic(WRITE_CHARACTERISTIC_UUID),
            ]

    return [Service()]


async def test_stalled_gatt_write_times_out_instead_of_hanging() -> None:
    """永不完成的 GATT 写必须变成异常，而不是静默挂死。

    这是第二道防线：串行化已经消除了已知的触发条件，但传输层对上层的承诺是
    「写入要么完成要么抛异常」，一个永远不返回的写违背了这个承诺。
    """
    transport = BleTransport(
        "addr",
        client_factory=lambda *args: _StallingClient(),  # type: ignore[arg-type,return-value]
        write_timeout=0.05,
    )
    await transport.connect()
    with pytest.raises(TransportTimeoutError, match="GATT 写入超时"):
        await transport.write(b"\xff\xaa\x27\x3a\x00")
    await transport.disconnect()


async def test_write_timeout_is_configurable() -> None:
    transport = BleTransport("addr", write_timeout=1.25)
    assert transport.write_timeout == 1.25
