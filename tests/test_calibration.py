"""校准测试。全部离线：逐字节断言发出的指令序列。"""

from __future__ import annotations

import pytest

from wt901.device import WT901Device
from wt901.transport.memory import MemoryTransport

UNLOCK = bytes.fromhex("ffaa6988b5")
SAVE = bytes.fromhex("ffaa000000")
ACCEL_CAL = bytes.fromhex("ffaa010100")
FIELD_START = bytes.fromhex("ffaa010700")
FIELD_END = bytes.fromhex("ffaa010000")


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    await device.open()
    return device, transport


# ----- 指令字节 ------------------------------------------------------------


async def test_acceleration_calibration_bytes() -> None:
    """与官方 SDK 逐字节一致，含前置解锁。"""
    device, transport = await _opened()
    await device.calibration.calibrate_acceleration()
    assert transport.writes == [UNLOCK, ACCEL_CAL, SAVE]
    await device.close()


async def test_field_calibration_start_and_end_bytes() -> None:
    device, transport = await _opened()
    await device.calibration.start_field_calibration()
    await device.calibration.end_field_calibration()
    assert transport.writes == [UNLOCK, FIELD_START, SAVE, UNLOCK, FIELD_END, SAVE]
    await device.close()


# ----- 状态标志 ------------------------------------------------------------


async def test_state_flag_tracks_field_calibration() -> None:
    """校准态下的角度输出不可用于测量，上层需要能把该时段标为不可信。"""
    device, _ = await _opened()
    assert not device.calibration.is_field_calibrating

    await device.calibration.start_field_calibration()
    assert device.calibration.is_field_calibrating

    await device.calibration.end_field_calibration()
    assert not device.calibration.is_field_calibrating
    await device.close()


async def test_acceleration_calibration_does_not_set_field_flag() -> None:
    """加计校准是一次性动作，不进入任何持续状态。"""
    device, _ = await _opened()
    await device.calibration.calibrate_acceleration()
    assert not device.calibration.is_field_calibrating
    await device.close()


# ----- 上下文管理器 --------------------------------------------------------


async def test_context_manager_brackets_the_calibration() -> None:
    device, transport = await _opened()
    async with device.calibration.field_calibration():
        assert device.calibration.is_field_calibrating
        assert transport.writes == [UNLOCK, FIELD_START, SAVE]
    assert not device.calibration.is_field_calibrating
    assert transport.writes == [UNLOCK, FIELD_START, SAVE, UNLOCK, FIELD_END, SAVE]
    await device.close()


async def test_context_manager_ends_calibration_on_exception() -> None:
    """这是本 scope 最重要的一条。

    磁场校准没有超时也没有自动退出：一次未捕获的异常就能让设备无限期停在校准
    态，而唯一的症状是姿态数据一直不对——不报错、不断连、看不出来。
    """
    device, transport = await _opened()
    with pytest.raises(ValueError):
        async with device.calibration.field_calibration():
            raise ValueError("操作者中途放弃")

    assert FIELD_END in transport.writes
    assert not device.calibration.is_field_calibrating
    await device.close()


async def test_context_manager_yields_the_calibration_object() -> None:
    device, _ = await _opened()
    async with device.calibration.field_calibration() as calibration:
        assert calibration is device.calibration
    await device.close()


async def test_guided_calibration_brackets_the_wait() -> None:
    device, transport = await _opened()
    await device.calibration.guided_field_calibration(rotation_seconds=0.01)
    assert transport.writes == [UNLOCK, FIELD_START, SAVE, UNLOCK, FIELD_END, SAVE]
    assert not device.calibration.is_field_calibrating
    await device.close()


# ----- 与写事务的协作 ------------------------------------------------------


async def test_calibration_goes_through_the_write_transaction() -> None:
    """校准指令必须走完整的 解锁→写→保存 时序，不能裸发。"""
    device, transport = await _opened()
    await device.calibration.calibrate_acceleration()
    assert transport.writes[0] == UNLOCK
    assert transport.writes[-1] == SAVE
    await device.close()


async def test_calibration_is_recorded_for_reconnect_replay() -> None:
    """校准写入同样进入配置记忆——重连后 CALSW 会被重放为最后一次的值。

    对磁场校准来说这意味着：若在校准**进行中**掉线重连，设备会被重新置入校准
    态而不是悄悄回到正常输出。上层据 `is_field_calibrating` 判断即可，行为一致。
    """
    device, _ = await _opened()
    await device.calibration.start_field_calibration()
    await device.calibration.end_field_calibration()

    applied = device.registers.applied_writes
    calsw = [entry for entry in applied if entry.register == 0x01]
    assert len(calsw) == 1
    assert calsw[0].value == 0x0000  # 最后一次是「结束校准」
    await device.close()
