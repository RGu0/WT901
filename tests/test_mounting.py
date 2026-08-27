"""安装方向（寄存器 0x23）的具名配置。全部离线。

这一层存在的理由与 RAY-241 的 `0x24` 同形：`write(0x23, 0)` 本来就能写，问题是把
「0 是水平、1 是垂直」这条设备知识留在每个调用方手里。装反了设备不报错，只会让姿态
解算的重力轴对不上——数据一直偏，而链路、速率、丢包这些可观测量全部正常。

还有一层：适配文档给的是**七步**序列，其中安装方向那一步写的可能就是设备当前已有的
值（0 是不是出厂默认，本库没核实过）。它的价值不在于改变什么，而在于让「一份配置快照
完全决定设备状态」成立，所以 `test_writing_horizontal_is_still_a_real_write` 把「哪怕
可能是幂等的也真的发出去」钉住——省掉它是最容易被后来者当成优化做掉的事。
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from wt901.device import WT901Device
from wt901.errors import UnsupportedRegisterError
from wt901.protocol.frames import FRAME_LENGTH, HEADER, FrameFlag
from wt901.protocol.registers import (
    AlgorithmMode,
    Bandwidth,
    Mounting,
    Register,
    ReturnRate,
)
from wt901.transport.memory import MemoryTransport

UNLOCK = bytes.fromhex("ffaa6988b5")
SAVE = bytes.fromhex("ffaa000000")
HORIZONTAL = bytes.fromhex("ffaa230000")
VERTICAL = bytes.fromhex("ffaa230100")


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
    await device.open()
    return device, transport


async def _answer(
    transport: MemoryTransport, start: int, values: tuple[int, ...]
) -> None:
    for _ in range(200):
        await asyncio.sleep(0)
        if transport.writes:
            break
    transport.feed(register_frame(start, values))


# ----- 验收标准 1、2：寄存器地址与具名枚举 ----------------------------------


def test_register_has_the_mounting_address() -> None:
    assert Register.MOUNTING == 0x23


def test_enum_maps_the_documented_encoding() -> None:
    assert Mounting.HORIZONTAL == 0
    assert Mounting.VERTICAL == 1


# ----- 验收标准 3：与 set_output_rate / set_bandwidth / set_algorithm 同构 ----


async def test_set_mounting_emits_the_documented_bytes() -> None:
    """适配文档 §3.1 第 ⑥ 步就是 `FF AA 23 00 00`。"""
    device, transport = await _opened()
    assert (
        await device.registers.set_mounting(Mounting.HORIZONTAL) is Mounting.HORIZONTAL
    )
    assert transport.writes == [UNLOCK, HORIZONTAL, SAVE]
    await device.close()


async def test_set_mounting_vertical() -> None:
    device, transport = await _opened()
    await device.registers.set_mounting(Mounting.VERTICAL)
    assert transport.writes == [UNLOCK, VERTICAL, SAVE]
    await device.close()


async def test_writing_horizontal_is_still_a_real_write() -> None:
    """这次写入可能是幂等的，但**必须真的发出去**。

    0（水平）是不是出厂默认，本库没有核实过；而无论是不是，省掉它都等于依赖设备
    残留的配置——模块会被别处用过，配置又固化在 flash。这是最容易被后来者当成冗余
    优化掉的一步，所以专门钉一条。
    """
    device, transport = await _opened()
    await device.registers.set_mounting(Mounting.HORIZONTAL)

    assert HORIZONTAL in transport.writes
    assert transport.writes.count(HORIZONTAL) == 1
    await device.close()


async def test_bare_int_still_works_like_the_others() -> None:
    device, transport = await _opened()
    assert await device.registers.set_mounting(1) is Mounting.VERTICAL
    assert transport.writes == [UNLOCK, VERTICAL, SAVE]
    await device.close()


@pytest.mark.parametrize("code", [0x02, 0x07, 0xFF])
async def test_undocumented_value_is_refused(code: int) -> None:
    """手册只定义 0 与 1。写别的值设备不报错，只会静默进入未知状态。"""
    device, transport = await _opened()
    with pytest.raises(UnsupportedRegisterError, match="安装方向"):
        await device.registers.set_mounting(code)
    assert transport.writes == []
    await device.close()


async def test_read_mounting_returns_raw_code() -> None:
    """读回原始 int：设备上可能存着上位机软件设过的、本库未登记的值。"""
    device, transport = await _opened()
    task = asyncio.get_running_loop().create_task(device.registers.read_mounting())
    await _answer(transport, Register.MOUNTING, (1, 0, 0, 0))
    assert await task == 1
    await device.close()


# ----- 验收标准 3、5：Settings 字段与下发顺序 --------------------------------


async def test_settings_carries_mounting() -> None:
    device, transport = await _opened()
    async with device.registers.settings() as settings:
        settings.mounting = Mounting.HORIZONTAL
        assert transport.writes == []
    assert transport.writes == [UNLOCK, HORIZONTAL, SAVE]
    await device.close()


async def test_settings_order_matches_the_adaptation_document() -> None:
    """《BS-BT91 硬件适配与时间同步方案 v0.1》§3.1 的七步序列。

    本库覆盖其中的 ②③④⑥（① 解锁与 ⑦ 保存由每次写事务自带，⑤ 是 `0x96`，走通用
    具名写入）。顺序写在 settings() 里，这条测试防止后来的人调换它而不自知。
    """
    device, transport = await _opened()
    async with device.registers.settings() as settings:
        settings.output_rate = ReturnRate.HZ_200
        settings.bandwidth = Bandwidth.HZ_20
        settings.algorithm = AlgorithmMode.SIX_AXIS
        settings.mounting = Mounting.HORIZONTAL
    assert transport.writes == [
        UNLOCK,
        bytes.fromhex("ffaa030b00"),
        SAVE,
        UNLOCK,
        bytes.fromhex("ffaa1f0400"),
        SAVE,
        UNLOCK,
        bytes.fromhex("ffaa240100"),
        SAVE,
        UNLOCK,
        HORIZONTAL,
        SAVE,
    ]
    await device.close()


async def test_settings_leaves_mounting_alone_when_unset() -> None:
    device, transport = await _opened()
    async with device.registers.settings() as settings:
        settings.output_rate = ReturnRate.HZ_50
    assert HORIZONTAL not in transport.writes
    assert VERTICAL not in transport.writes
    await device.close()


# ----- 验收标准 4：按配置型处理，参与重连重放 --------------------------------


async def test_mounting_is_remembered_for_replay() -> None:
    """安装方向是配置不是动作，必须进 applied_writes。

    重放在这里是必需的：设备重连后若退回残留配置，重力轴的解算基准就变了，而
    调用方毫不知情——与算法模式（RAY-241）同一类风险。
    """
    device, _ = await _opened()
    await device.registers.set_mounting(Mounting.HORIZONTAL)

    applied = [e for e in device.registers.applied_writes if e.register == 0x23]
    assert len(applied) == 1
    assert applied[0].value == Mounting.HORIZONTAL
    await device.close()


async def test_replay_reissues_the_mounting() -> None:
    device, transport = await _opened()
    await device.registers.set_mounting(Mounting.VERTICAL)
    transport.writes.clear()

    await device.registers.replay()

    assert VERTICAL in transport.writes
    await device.close()


async def test_replay_reissues_only_the_last_mounting() -> None:
    """同一寄存器只保留最后一次——重放不该把中间态再走一遍。"""
    device, transport = await _opened()
    await device.registers.set_mounting(Mounting.VERTICAL)
    await device.registers.set_mounting(Mounting.HORIZONTAL)
    transport.writes.clear()

    await device.registers.replay()

    assert HORIZONTAL in transport.writes
    assert VERTICAL not in transport.writes
    await device.close()


# ----- 公开契约 --------------------------------------------------------------


def test_mounting_is_exported_from_the_package_root() -> None:
    import wt901

    assert wt901.Mounting is Mounting
    assert "Mounting" in wt901.__all__


def test_mounting_is_exported_from_the_public_protocol_namespace() -> None:
    """公开协议命名空间应与同类枚举保持一致，供下游作类型标注。"""
    from wt901.protocol import Mounting as public_mounting

    assert public_mounting is Mounting
