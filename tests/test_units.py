"""单位换算测试。

对外单位是 SI，器件原始语义是度与 g，换算错了不会崩溃，只会让下游拿到看似合
理的错数——所以边界值逐个钉死。
"""

from __future__ import annotations

import math

import pytest

from wt901.protocol.units import (
    STANDARD_GRAVITY,
    accel_to_m_s2,
    angle_to_rad,
    angular_velocity_to_rad_s,
    battery_percent,
    displacement_to_m,
    magnetic_field_to_ut,
    quaternion_component,
    temperature_to_celsius,
)


def test_accel_full_scale_negative() -> None:
    """-32768 恰好是 -16 g，这是唯一能精确表示的满量程端点。"""
    assert accel_to_m_s2(-32768) == pytest.approx(-16 * STANDARD_GRAVITY)


def test_accel_full_scale_positive() -> None:
    assert accel_to_m_s2(32767) == pytest.approx(
        32767 / 32768 * 16 * STANDARD_GRAVITY
    )


def test_accel_zero_and_one_g() -> None:
    assert accel_to_m_s2(0) == 0.0
    # 1 g 对应 32768/16 = 2048 counts。
    assert accel_to_m_s2(2048) == pytest.approx(STANDARD_GRAVITY)


def test_angular_velocity_is_radians_per_second() -> None:
    assert angular_velocity_to_rad_s(-32768) == pytest.approx(math.radians(-2000))
    assert angular_velocity_to_rad_s(32767) == pytest.approx(
        math.radians(32767 / 32768 * 2000)
    )
    assert angular_velocity_to_rad_s(0) == 0.0


def test_angle_full_scale_is_pi() -> None:
    """器件量程 ±180°，SI 下满量程恰好是 ±π。"""
    assert angle_to_rad(-32768) == pytest.approx(-math.pi)
    assert angle_to_rad(32767) == pytest.approx(math.pi, rel=1e-4)
    assert angle_to_rad(16384) == pytest.approx(math.pi / 2)
    assert angle_to_rad(0) == 0.0


def test_quaternion_component_is_unitless() -> None:
    assert quaternion_component(32768 // 2) == pytest.approx(0.5)
    assert quaternion_component(-32768) == -1.0
    assert quaternion_component(0) == 0.0


def test_temperature() -> None:
    assert temperature_to_celsius(2350) == pytest.approx(23.5)
    assert temperature_to_celsius(-1250) == pytest.approx(-12.5)
    assert temperature_to_celsius(0) == 0.0


def test_displacement_is_metres() -> None:
    """器件以 mm 输出，对外统一到 m。"""
    assert displacement_to_m(1234) == pytest.approx(1.234)
    assert displacement_to_m(-1000) == pytest.approx(-1.0)


@pytest.mark.parametrize(
    ("mag_type", "expected"),
    [
        (2, 1000 * 0.15),
        (3, 1000 * 13 / 1000.0),
        (4, 1000 * 0.058),
        (5, 1000 * 0.098),
        (6, 1000 / 150.0),
        (7, 1000 * 20 / 1000.0),
    ],
)
def test_magnetic_field_known_types(mag_type: int, expected: float) -> None:
    """逐档比对官方 Android DipSensorMagHelper。"""
    assert magnetic_field_to_ut(mag_type, 1000) == pytest.approx(expected)


@pytest.mark.parametrize("mag_type", [0, 1, 8, 99, -1])
def test_magnetic_field_unknown_type_returns_none(mag_type: int) -> None:
    """未知量纲不猜系数：返回 None，让调用方知道这个数没有单位。"""
    assert magnetic_field_to_ut(mag_type, 1000) is None


def test_magnetic_field_does_not_use_official_python_divide_by_120() -> None:
    """官方 Python 示例写死的 /120 与任何一档都不一致，不得复现。"""
    for mag_type in range(2, 8):
        assert magnetic_field_to_ut(mag_type, 1200) != pytest.approx(10.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (500, 100),
        (396, 100),
        (395, 90),
        (393, 90),
        (392, 75),
        (387, 75),
        (386, 60),
        (382, 60),
        (381, 50),
        (379, 50),
        (378, 40),
        (377, 40),
        (376, 30),
        (373, 30),
        (372, 20),
        (370, 20),
        (369, 15),
        (368, 15),
        (367, 10),
        (350, 10),
        (349, 5),
        (340, 5),
        (339, 0),
        (1, 0),
    ],
)
def test_battery_percent_steps(raw: int, expected: int) -> None:
    """12 个阶梯的每个边界与边界下一档都要对。

    官方实现是查表不是插值；改成插值会得到与上位机软件不一致的读数。
    """
    assert battery_percent(raw) == expected


@pytest.mark.parametrize("raw", [0, -1, -391, -32768])
def test_battery_percent_refuses_impossible_raw(raw: int) -> None:
    """非正数不是「电量很低」，是「这次读数无效」。

    ``raw`` 是电压 ×100。一台刚刚回答完寄存器读的设备不可能是 0 V。真机上确实
    读到过 0（见 RAY-182），而阶梯表的最低一档会把它映射成一个看着正常的 0%
    ——调用方据此去换电池，真正的问题却在别处。

    寄存器值按 int16 解码，所以负数同样要拦：一个 >32767 的值会以负数出现。
    """
    assert battery_percent(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [(340, 5), (339, 0), (1, 0)])
def test_legitimate_low_battery_still_reports_a_percentage(
    raw: int, expected: int
) -> None:
    """阈值以下的**合法**低电量读数不受影响。

    这条是上一条的反向保护：若把「不可能」的判据放宽到 ``raw < 340``，真正快
    没电的设备就报不出 0% 了，而那正是这个字段最该说话的时候。
    """
    assert battery_percent(raw) == expected
