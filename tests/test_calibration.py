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
    device.registers.save_delay = 0.0
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


async def test_calibration_is_not_recorded_for_reconnect_replay() -> None:
    """校准写入**不进**配置记忆，因此不会被重连重放。

    重放的语义是「把设备恢复成调用方配置过的样子」。这对配置成立，对动作不成立
    ——重放一次加计校准，就是在重连那一刻的姿态下重做零位标定。
    """
    device, _ = await _opened()
    await device.calibration.calibrate_acceleration()
    await device.calibration.start_field_calibration()
    await device.calibration.end_field_calibration()

    calsw = [
        entry for entry in device.registers.applied_writes if entry.register == 0x01
    ]
    assert calsw == []
    await device.close()


async def test_replay_does_not_redo_acceleration_calibration() -> None:
    """回归：重连重放绝不能重发加计校准。

    这是本 scope 评审时用探针查出来的真实缺陷。加计校准把当前读数当零位基准；
    自动重连可能发生在任何时刻、任何姿态下，重放它等于把一个倾斜姿态固化成
    「水平」——不报错，只是从此所有角度都偏。
    """
    device, transport = await _opened()
    await device.calibration.calibrate_acceleration()
    transport.writes.clear()

    await device.registers.replay()

    assert ACCEL_CAL not in transport.writes
    await device.close()


async def test_replay_does_not_reenter_field_calibration() -> None:
    """回归：重连重放绝不能把设备重新置入磁场校准态。

    掉线发生在 with 体内时，配对的结束调用会随那次异常一起走完；此后再被重放
    进校准态，就没有任何人会发结束指令了——正是上下文管理器要防的那件事。
    """
    device, transport = await _opened()
    await device.calibration.start_field_calibration()
    transport.writes.clear()

    await device.registers.replay()

    assert FIELD_START not in transport.writes
    await device.close()


async def test_ordinary_configuration_is_still_replayed() -> None:
    """反向保护：排除的只有校准，普通配置的重放不能被误伤。"""
    device, transport = await _opened()
    await device.registers.set_output_rate(0x09)
    transport.writes.clear()

    await device.registers.replay()

    assert bytes.fromhex("ffaa030900") in transport.writes
    await device.close()
