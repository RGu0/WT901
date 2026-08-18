"""数据模型测试。"""

from __future__ import annotations

import dataclasses
import math

import pytest

from conftest import data_frame
from wt901.models import ImuSample
from wt901.protocol.frames import FrameDecoder
from wt901.protocol.units import STANDARD_GRAVITY


def _sample(counts: tuple[int, ...]) -> ImuSample:
    (frame,) = FrameDecoder().feed(data_frame(counts))
    return ImuSample.from_frame(frame, device_id="dev-a", t_host=1.5, seq=7)


def test_sample_fields_are_si_units() -> None:
    # 加速度 Z = 1 g，角速度 X 满量程一半，Yaw 半量程。
    sample = _sample((0, 0, 2048, 16384, 0, 0, 0, 0, 16384))
    assert sample.accel.z == pytest.approx(STANDARD_GRAVITY)
    assert sample.gyro.x == pytest.approx(math.radians(1000))
    assert sample.euler.yaw == pytest.approx(math.pi / 2)
    assert sample.device_id == "dev-a"
    assert sample.t_host == 1.5
    assert sample.seq == 7


def test_each_gyro_axis_reads_its_own_bytes() -> None:
    """官方协议文档把 WY、WZ 的公式都误写成了 WX，这里钉死三轴互不串扰。"""
    sample = _sample((0, 0, 0, 100, 200, 300, 0, 0, 0))
    assert sample.gyro.x == pytest.approx(math.radians(100 / 32768 * 2000))
    assert sample.gyro.y == pytest.approx(math.radians(200 / 32768 * 2000))
    assert sample.gyro.z == pytest.approx(math.radians(300 / 32768 * 2000))
    assert sample.gyro.x != sample.gyro.y != sample.gyro.z


def test_each_accel_axis_reads_its_own_bytes() -> None:
    sample = _sample((100, 200, 300, 0, 0, 0, 0, 0, 0))
    assert sample.accel.x < sample.accel.y < sample.accel.z


def test_euler_axes_map_to_roll_pitch_yaw_in_order() -> None:
    sample = _sample((0, 0, 0, 0, 0, 0, 100, 200, 300))
    assert sample.euler.roll < sample.euler.pitch < sample.euler.yaw


def test_raw_counts_are_preserved() -> None:
    """`.raw` 是逃生舱：需要复现器件原始语义时不必反算。"""
    counts = (1, -2, 3, -4, 5, -6, 7, -8, 9)
    sample = _sample(counts)
    assert sample.raw.values == counts


def test_sample_is_immutable() -> None:
    """多设备并发采集时，样本被下游改写会是极难定位的 bug。"""
    sample = _sample((0,) * 9)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sample.seq = 99  # type: ignore[misc]


def test_vec3_magnitude() -> None:
    sample = _sample((2048, 0, 0, 0, 0, 0, 0, 0, 0))
    assert sample.accel.magnitude == pytest.approx(STANDARD_GRAVITY)
