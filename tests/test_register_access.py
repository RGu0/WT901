"""寄存器读写事务测试。全部离线。

读是请求/响应，写是有时序的三步操作——两者的失败模式都在真机上极难复现：
响应乱序、迟到、丢失，以及两个写交错。这些在这里全部被显式构造出来。
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from wt901.device import WT901Device
from wt901.errors import TransportTimeoutError, UnsupportedRegisterError
from wt901.protocol.frames import FRAME_LENGTH, HEADER, FrameFlag
from wt901.protocol.registers import Bandwidth, Register, ReturnRate
from wt901.transport.memory import MemoryTransport

UNLOCK = bytes.fromhex("ffaa6988b5")
SAVE = bytes.fromhex("ffaa000000")


def register_frame(start: int, values: tuple[int, ...]) -> bytes:
    body = (
        bytes([HEADER, FrameFlag.REGISTER])
        + struct.pack("<H", start)
        + struct.pack("<4h", *values)
    )
    return body.ljust(FRAME_LENGTH, b"\x00")


def data_frame() -> bytes:
    return bytes([HEADER, FrameFlag.DATA]) + struct.pack("<9h", *([0] * 9))


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    """开一台设备，并把寄存器通道的时序调快到适合测试。

    时序参数是公开可写的属性——运行时调超时本就是合理需求（链路差时要放宽），
    测试用同一个入口，不去戳私有状态。
    """
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    device.registers.read_timeout = 0.05
    await device.open()
    return device, transport


async def _answer(transport: MemoryTransport, start: int, values: tuple[int, ...]) -> None:
    """让设备在下一轮事件循环里回一帧。"""
    await asyncio.sleep(0)
    transport.feed(register_frame(start, values))


# ----- 读事务 --------------------------------------------------------------


async def test_read_returns_four_consecutive_registers() -> None:
    """一次回帧固定携带 4 个寄存器，这是协议决定的。"""
    device, transport = await _opened()
    task = asyncio.get_running_loop().create_task(device.registers.read(Register.HX))
    await _answer(transport, Register.HX, (11, 22, 33, 44))
    response = await task

    assert response.start_register == Register.HX
    assert response.values == (11, 22, 33, 44)
    assert transport.writes[0] == bytes.fromhex("ffaa273a00")
    await device.close()


async def test_read_value_picks_the_requested_register() -> None:
    device, transport = await _opened()
    task = asyncio.get_running_loop().create_task(
        device.registers.read_value(Register.HZ)
    )
    # 请求 0x3C，设备回的是以 0x3C 起始的四个。
    await _answer(transport, Register.HZ, (7, 8, 9, 10))
    assert await task == 7
    await device.close()


async def test_unrelated_register_frames_do_not_resolve_the_request() -> None:
    """0x71 与 0x61 混在同一条链路上，别的地址的回帧不能误当成答复。"""
    device, transport = await _opened()
    task = asyncio.get_running_loop().create_task(device.registers.read(Register.Q0))

    await asyncio.sleep(0)
    transport.feed(register_frame(Register.HX, (1, 2, 3, 4)))  # 无关地址
    transport.feed(data_frame())  # 实时数据帧
    await asyncio.sleep(0)
    assert not task.done()

    transport.feed(register_frame(Register.Q0, (100, 200, 300, 400)))
    response = await task
    assert response.values == (100, 200, 300, 400)
    await device.close()


async def test_read_times_out_and_retries() -> None:
    device, transport = await _opened()
    with pytest.raises(TransportTimeoutError, match="0x3A"):
        await device.registers.read(Register.HX)
    # 默认重试 2 次 → 一共发出 3 条读指令，不是发一次就永远挂着。
    assert transport.writes.count(bytes.fromhex("ffaa273a00")) == 3
    await device.close()


async def test_late_response_after_timeout_is_ignored() -> None:
    """超时后迟到的响应不能让下一次读拿到过期数据。"""
    device, transport = await _opened()
    with pytest.raises(TransportTimeoutError):
        await device.registers.read(Register.HX)

    transport.feed(register_frame(Register.HX, (1, 2, 3, 4)))  # 迟到
    await asyncio.sleep(0)

    task = asyncio.get_running_loop().create_task(device.registers.read(Register.HX))
    await _answer(transport, Register.HX, (9, 9, 9, 9))
    assert (await task).values == (9, 9, 9, 9)
    await device.close()


async def test_concurrent_reads_of_same_register_both_resolve() -> None:
    device, transport = await _opened()
    loop = asyncio.get_running_loop()
    first = loop.create_task(device.registers.read(Register.Q0))
    second = loop.create_task(device.registers.read(Register.Q0))
    await _answer(transport, Register.Q0, (1, 2, 3, 4))
    assert (await first).values == (await second).values == (1, 2, 3, 4)
    await device.close()


async def test_response_for_nobody_is_not_an_error() -> None:
    device, transport = await _opened()
    transport.feed(register_frame(Register.TEMPERATURE, (2350, 0, 0, 0)))
    await asyncio.sleep(0)
    assert device.stats.register_frames == 1
    await device.close()


# ----- 写事务 --------------------------------------------------------------


async def test_write_emits_the_official_sequence() -> None:
    """解锁 → 写 → 保存，逐字节与官方 SDK 一致。"""
    device, transport = await _opened()
    await device.registers.write(Register.RRATE, ReturnRate.HZ_50)
    assert transport.writes == [UNLOCK, bytes.fromhex("ffaa030800"), SAVE]
    await device.close()


async def test_write_without_persist_skips_save() -> None:
    device, transport = await _opened()
    await device.registers.write(Register.RRATE, ReturnRate.HZ_10, persist=False)
    assert transport.writes == [UNLOCK, bytes.fromhex("ffaa030600")]
    await device.close()


async def test_concurrent_writes_do_not_interleave() -> None:
    """交错的两个写会变成 解锁/解锁/写/写/保存/保存 —— 那不是两次写。"""
    device, transport = await _opened()
    await asyncio.gather(
        device.registers.write(Register.RRATE, ReturnRate.HZ_50),
        device.registers.write(Register.BANDWIDTH, Bandwidth.HZ_20),
    )
    assert len(transport.writes) == 6
    # 每三条构成一个完整事务，顺序不被打断。
    for chunk in (transport.writes[:3], transport.writes[3:]):
        assert chunk[0] == UNLOCK
        assert chunk[2] == SAVE
    await device.close()


async def test_set_output_rate_and_bandwidth() -> None:
    device, transport = await _opened()
    assert await device.registers.set_output_rate(ReturnRate.HZ_50) is ReturnRate.HZ_50
    assert await device.registers.set_bandwidth(Bandwidth.HZ_256) is Bandwidth.HZ_256
    assert transport.writes == [
        UNLOCK,
        bytes.fromhex("ffaa030800"),
        SAVE,
        UNLOCK,
        bytes.fromhex("ffaa1f0000"),
        SAVE,
    ]
    await device.close()


@pytest.mark.parametrize("code", [0x00, 0x09, 0x0B, 0xFF])
async def test_unverified_output_rate_is_refused(code: int) -> None:
    """未核实的档位必须拒绝，而不是静默写入一个未知编码。"""
    device, transport = await _opened()
    with pytest.raises(UnsupportedRegisterError, match="未在真机上核实"):
        await device.registers.set_output_rate(code)
    assert transport.writes == []
    await device.close()


@pytest.mark.parametrize("code", [0x01, 0x02, 0x06, 0xFF])
async def test_unverified_bandwidth_is_refused(code: int) -> None:
    device, transport = await _opened()
    with pytest.raises(UnsupportedRegisterError):
        await device.registers.set_bandwidth(code)
    assert transport.writes == []
    await device.close()


async def test_read_back_returns_raw_code() -> None:
    """设备上可能存着本库尚未核实的档位，读回时不该抛异常。"""
    device, transport = await _opened()
    task = asyncio.get_running_loop().create_task(device.registers.read_output_rate())
    await _answer(transport, Register.RRATE, (0x0B, 0, 0, 0))  # 未核实的档位
    assert await task == 0x0B
    await device.close()


# ----- 配置事务与重连重放 --------------------------------------------------


async def test_settings_applies_on_exit() -> None:
    device, transport = await _opened()
    async with device.registers.settings() as settings:
        settings.output_rate = ReturnRate.HZ_50
        settings.bandwidth = Bandwidth.HZ_20
        assert transport.writes == []  # 尚未下发
    assert len(transport.writes) == 6
    await device.close()


async def test_settings_leaves_untouched_fields_alone() -> None:
    device, transport = await _opened()
    async with device.registers.settings() as settings:
        settings.output_rate = ReturnRate.HZ_10
    assert transport.writes == [UNLOCK, bytes.fromhex("ffaa030600"), SAVE]
    await device.close()


async def test_applied_writes_are_remembered_once_per_register() -> None:
    device, _ = await _opened()
    await device.registers.set_output_rate(ReturnRate.HZ_10)
    await device.registers.set_output_rate(ReturnRate.HZ_50)
    applied = device.registers.applied_writes
    assert len(applied) == 1
    assert applied[0].register == Register.RRATE
    assert applied[0].value == ReturnRate.HZ_50
    await device.close()


async def test_reconnect_replays_configuration_without_saving() -> None:
    """重放是为了恢复运行时状态，不该顺带再写一次 flash。"""
    from wt901.device import ReconnectPolicy

    transport = MemoryTransport("dev")
    device = WT901Device(
        transport,
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(initial_delay=0.01, max_delay=0.01),
    )
    device.registers.write_delay = 0.0
    await device.open()

    await device.registers.set_output_rate(ReturnRate.HZ_50)
    transport.writes.clear()

    transport.drop()
    await asyncio.sleep(0.08)

    assert device.stats.reconnects == 1
    assert transport.writes == [UNLOCK, bytes.fromhex("ffaa030800")]
    assert SAVE not in transport.writes
    await device.close()
