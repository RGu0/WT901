"""算法模式（寄存器 0x24）的具名配置。全部离线。

这一层存在的理由不是「能写 0x24」——`RegisterAccess.write(0x24, 1)` 本来就能写。
理由是 0/1 的含义与直觉相反（值大的 1 对应轴少的那个），把这条知识留在每个调用方
手里，就是把「写反了不会报错、只会让航向悄悄劣化」的机会复制给每个下游。
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import register_frame, registers
from wt901.device import WT901Device
from wt901.errors import UnsupportedRegisterError
from wt901.protocol.registers import AlgorithmMode, Bandwidth, Register, ReturnRate
from wt901.transport.memory import MemoryTransport

UNLOCK = bytes.fromhex("ffaa6988b5")
SAVE = bytes.fromhex("ffaa000000")
SIX_AXIS = bytes.fromhex("ffaa240100")
NINE_AXIS = bytes.fromhex("ffaa240000")


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


def test_register_has_the_algorithm_address() -> None:
    assert Register.ALGORITHM == 0x24


def test_enum_maps_the_counterintuitive_encoding() -> None:
    """值大的 1 是「轴少」的那个。这一条写反了不会报错，所以必须有测试钉住。"""
    assert AlgorithmMode.SIX_AXIS == 1
    assert AlgorithmMode.NINE_AXIS == 0


# ----- 验收标准 3：与 set_output_rate / set_bandwidth 同构 -------------------


async def test_set_algorithm_emits_the_documented_bytes() -> None:
    device, transport = await _opened()
    assert (
        await device.registers.set_algorithm(AlgorithmMode.SIX_AXIS)
        is AlgorithmMode.SIX_AXIS
    )
    assert transport.writes == [UNLOCK, SIX_AXIS, SAVE]
    await device.close()


async def test_set_algorithm_nine_axis() -> None:
    device, transport = await _opened()
    await device.registers.set_algorithm(AlgorithmMode.NINE_AXIS)
    assert transport.writes == [UNLOCK, NINE_AXIS, SAVE]
    await device.close()


async def test_bare_int_still_works_like_the_other_two() -> None:
    """裸 int 不是唯一入口，但也不被禁止——与 set_output_rate / set_bandwidth 一致。"""
    device, transport = await _opened()
    assert await device.registers.set_algorithm(1) is AlgorithmMode.SIX_AXIS
    assert transport.writes == [UNLOCK, SIX_AXIS, SAVE]
    await device.close()


@pytest.mark.parametrize("code", [0x02, 0x07, 0xFF])
async def test_undocumented_value_is_refused(code: int) -> None:
    """手册只定义 0 与 1。写别的值设备不报错，只会静默进入未知状态。"""
    device, transport = await _opened()
    with pytest.raises(UnsupportedRegisterError, match="算法模式"):
        await device.registers.set_algorithm(code)
    assert transport.writes == []
    await device.close()


async def test_read_algorithm_returns_raw_code() -> None:
    """读回原始 int：设备上可能存着上位机软件设过的、本库未登记的值。"""
    device, transport = await _opened()
    task = asyncio.get_running_loop().create_task(device.registers.read_algorithm())
    await _answer(transport, Register.ALGORITHM, registers(1))
    assert await task == 1
    await device.close()


# ----- 验收标准 3：Settings 字段 --------------------------------------------


async def test_settings_carries_algorithm() -> None:
    device, transport = await _opened()
    async with device.registers.settings() as settings:
        settings.algorithm = AlgorithmMode.SIX_AXIS
        assert transport.writes == []
    assert transport.writes == [UNLOCK, SIX_AXIS, SAVE]
    await device.close()


async def test_settings_order_matches_the_downstream_prd() -> None:
    """gait-IMU 的 PRD §6.1 规定下发序列为 速率 → 带宽 → 6 轴。

    顺序本身写在 settings() 里，这条测试防止后来的人调换它而不自知。
    """
    device, transport = await _opened()
    async with device.registers.settings() as settings:
        settings.output_rate = ReturnRate.HZ_200
        settings.bandwidth = Bandwidth.HZ_20
        settings.algorithm = AlgorithmMode.SIX_AXIS
    assert transport.writes == [
        UNLOCK,
        bytes.fromhex("ffaa030b00"),
        SAVE,
        UNLOCK,
        bytes.fromhex("ffaa1f0400"),
        SAVE,
        UNLOCK,
        SIX_AXIS,
        SAVE,
    ]
    await device.close()


async def test_settings_leaves_algorithm_alone_when_unset() -> None:
    device, transport = await _opened()
    async with device.registers.settings() as settings:
        settings.output_rate = ReturnRate.HZ_50
    assert SIX_AXIS not in transport.writes
    assert NINE_AXIS not in transport.writes
    await device.close()


# ----- 验收标准 4：按配置型处理，参与重连重放 --------------------------------


async def test_algorithm_is_remembered_for_replay() -> None:
    """算法模式是配置不是动作，必须进 applied_writes。

    与校准写入正好相反（RAY-173：校准是一次性动作，重放会重做零位标定）。
    这里重放是**必需**的：重连后设备若退回 9 轴，航向语义会在调用方毫不知情的
    情况下改变。
    """
    device, _ = await _opened()
    await device.registers.set_algorithm(AlgorithmMode.SIX_AXIS)

    applied = [e for e in device.registers.applied_writes if e.register == 0x24]
    assert len(applied) == 1
    assert applied[0].value == AlgorithmMode.SIX_AXIS
    await device.close()


async def test_replay_reissues_the_algorithm() -> None:
    device, transport = await _opened()
    await device.registers.set_algorithm(AlgorithmMode.SIX_AXIS)
    transport.writes.clear()

    await device.registers.replay()

    assert SIX_AXIS in transport.writes
    await device.close()


async def test_replay_reissues_only_the_last_algorithm() -> None:
    """同一寄存器只保留最后一次——重放不该把中间态再走一遍。"""
    device, transport = await _opened()
    await device.registers.set_algorithm(AlgorithmMode.NINE_AXIS)
    await device.registers.set_algorithm(AlgorithmMode.SIX_AXIS)
    transport.writes.clear()

    await device.registers.replay()

    assert SIX_AXIS in transport.writes
    assert NINE_AXIS not in transport.writes
    await device.close()


# ----- 公开契约 --------------------------------------------------------------


def test_algorithm_mode_is_exported_from_the_package_root() -> None:
    import wt901

    assert wt901.AlgorithmMode is AlgorithmMode
    assert "AlgorithmMode" in wt901.__all__


def test_algorithm_mode_is_exported_from_the_public_protocol_namespace() -> None:
    """公开协议命名空间应与同类枚举保持一致，供下游作类型标注。"""
    from wt901.protocol import AlgorithmMode as public_algorithm_mode

    assert public_algorithm_mode is AlgorithmMode
