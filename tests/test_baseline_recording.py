"""对着一份真机录制跑回归。

单元测试喂的是我们**构造**的帧——按我们以为的格式拼出来的。这份文件里的字节
是设备真的发出来的：100 Hz 下每次 BLE 通知打包 4 帧，帧与通知的边界不重合。
解码器在这种输入上会不会错位，构造出来的帧问不出来。

录制来历见文件头部的 ``note``。它不进 CI 之外的任何流程，只在这里被读。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from wt901.device import WT901Device
from wt901.models import ImuSample
from wt901.recording import read_recording
from wt901.transport.replay import ReplayTransport

BASELINE = Path(__file__).parent / "data" / "recordings" / "wt901-100hz.jsonl"

FRAME_LENGTH = 20
GRAVITY_LOW, GRAVITY_HIGH = 8.0, 12.0
"""``|accel|`` 均值的可信区间，m/s²。

设备录制时被拿在手上动过，单个样本会偏离重力；但三秒的均值必然由重力主导。
这条断言守的是 SI 换算的量级——若有人把 ``×9.80665`` 弄丢，均值会掉到 1 附近。
"""


async def _replay(*, speed: float | None = None) -> tuple[list[ImuSample], WT901Device]:
    transport = ReplayTransport.from_file(BASELINE, speed=speed)
    device = WT901Device(transport)
    await device.open()
    collected: list[ImuSample] = []
    async for sample in device.samples():
        collected.append(sample)
        if transport.exhausted and device.pending_samples == 0:
            break
    await device.close()
    return collected, device


def test_baseline_is_present_and_parses() -> None:
    recording = read_recording(BASELINE)
    assert recording.chunks
    assert recording.device_id
    assert "100 Hz" in recording.note


def test_every_notification_carries_packed_frames() -> None:
    """100 Hz 是打包传输：一次通知多帧。基线必须真的覆盖这个情形。

    若哪天基线被换成一份 10 Hz 的录制，这条会失败——那样的基线验不到打包路径，
    而打包正是解码器最容易出错的地方。
    """
    recording = read_recording(BASELINE)
    assert all(len(chunk.data) > FRAME_LENGTH for chunk in recording.chunks)


async def test_replay_decodes_every_byte_without_resyncing() -> None:
    """帧数必须恰好等于总字节数除以帧长，且一次错位都没有。"""
    recording = read_recording(BASELINE)
    samples, device = await _replay()

    assert len(samples) == recording.total_bytes // FRAME_LENGTH
    stats = device.stats
    assert stats.resync_count == 0
    assert stats.dropped_bytes == 0
    assert stats.dropped_samples == 0


async def test_acceleration_magnitude_is_dominated_by_gravity() -> None:
    samples, _ = await _replay()

    magnitudes = [
        math.sqrt(s.accel.x**2 + s.accel.y**2 + s.accel.z**2) for s in samples
    ]
    mean = sum(magnitudes) / len(magnitudes)
    assert GRAVITY_LOW < mean < GRAVITY_HIGH


async def test_replay_is_deterministic() -> None:
    """同一份录制回放两次必须逐样本相同，否则它当不了回归基线。"""
    first, _ = await _replay()
    second, _ = await _replay()

    assert [s.raw for s in first] == [s.raw for s in second]
    assert [s.seq for s in first] == [s.seq for s in second]


async def test_device_id_comes_from_the_recording() -> None:
    recording = read_recording(BASELINE)
    samples, _ = await _replay()

    assert {s.device_id for s in samples} == {recording.device_id}


@pytest.mark.parametrize("speed", [None, 50.0])
async def test_speed_does_not_change_the_decoded_content(speed: float | None) -> None:
    """时序只影响样本何时到达，不该影响解出什么。"""
    baseline, _ = await _replay()
    other, _ = await _replay(speed=speed)

    assert [s.raw for s in other] == [s.raw for s in baseline]
