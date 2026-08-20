"""原始计数值到 SI 单位的换算。

对外 API 一律使用 SI 单位，不提供「度」版本的并行接口。器件的原始语义是度，
换算只在本文件一处发生；需要原始计数值的场景走各数据对象的 ``raw`` 属性。

| 物理量   | 单位  |
| -------- | ----- |
| 加速度   | m/s²  |
| 角速度   | rad/s |
| 角度     | rad   |
| 四元数   | 无量纲 |
| 磁场     | µT    |
| 温度     | °C    |
| 位移     | m     |
"""

from __future__ import annotations

import math

__all__ = [
    "ACCEL_FULL_SCALE_G",
    "ANGLE_FULL_SCALE_DEG",
    "BATTERY_PERCENT_STEPS",
    "GYRO_FULL_SCALE_DPS",
    "INT16_FULL_SCALE",
    "STANDARD_GRAVITY",
    "accel_to_m_s2",
    "angle_to_rad",
    "angular_velocity_to_rad_s",
    "battery_percent",
    "displacement_to_m",
    "magnetic_field_to_ut",
    "quaternion_component",
    "temperature_to_celsius",
]

INT16_FULL_SCALE = 32768.0
"""int16 的满量程分母。协议里所有比例换算都以它为基准。"""

STANDARD_GRAVITY = 9.80665
"""标准重力加速度，m/s²。CGPM 定义值，不用 9.8 近似。"""

ACCEL_FULL_SCALE_G = 16.0
GYRO_FULL_SCALE_DPS = 2000.0
ANGLE_FULL_SCALE_DEG = 180.0


def accel_to_m_s2(raw: int) -> float:
    """加速度原始计数值 → m/s²。"""
    return raw / INT16_FULL_SCALE * ACCEL_FULL_SCALE_G * STANDARD_GRAVITY


def angular_velocity_to_rad_s(raw: int) -> float:
    """角速度原始计数值 → rad/s。

    器件语义是 ±2000 °/s，这里一次性转成 SI。
    """
    return math.radians(raw / INT16_FULL_SCALE * GYRO_FULL_SCALE_DPS)


def angle_to_rad(raw: int) -> float:
    """角度原始计数值 → rad。

    器件语义是 ±180°，所以满量程恰好对应 ±π。
    """
    return raw / INT16_FULL_SCALE * math.pi


def quaternion_component(raw: int) -> float:
    """四元数分量原始计数值 → 无量纲实数。"""
    return raw / INT16_FULL_SCALE


def temperature_to_celsius(raw: int) -> float:
    """温度原始计数值 → °C。"""
    return raw / 100.0


def displacement_to_m(raw: int) -> float:
    """位移原始计数值 → m。

    器件以 mm 为单位输出；统一到 m 使长度量纲与加速度一致。
    """
    return raw / 1000.0


def magnetic_field_to_ut(mag_type: int, raw: int) -> float | None:
    """磁场原始计数值 → µT，系数由寄存器 ``0x72`` 的量纲类型决定。

    返回 ``None`` 表示该量纲类型未知，本库拒绝猜测系数——官方 Android SDK 在
    这种情况下原样返回未换算的计数值，那会让调用方拿到一个单位不明的数。
    调用方应据此把该次读数标记为不可用，或先把 ``0x72`` 读上来。

    官方 Python 示例写死的 ``raw / 120`` 与下面任何一档都不一致，属该示例自身
    的陈旧实现，本库以 C#/Android 的分档为准。
    """
    if mag_type == 2:
        return raw * 0.15
    if mag_type == 3:
        return raw * 13 / 1000.0
    if mag_type == 4:
        return raw * 0.058
    if mag_type == 5:
        return raw * 0.098
    if mag_type == 6:
        return raw / 150.0
    if mag_type == 7:
        return raw * 20 / 1000.0
    return None


BATTERY_PERCENT_STEPS: tuple[tuple[int, int], ...] = (
    (396, 100),
    (393, 90),
    (387, 75),
    (382, 60),
    (379, 50),
    (377, 40),
    (373, 30),
    (370, 20),
    (368, 15),
    (350, 10),
    (340, 5),
)
"""``(原始值下界, 百分比)``，按下界降序。取自官方 C# 实现。"""


def battery_percent(raw: int) -> int | None:
    """电量原始值 → 百分比。``None`` 表示原始值不可能是一次真实测量。

    这是一张查表而非线性插值：官方实现用的就是不等距阶梯，改成插值会得到与
    上位机软件不一致的读数。

    ``raw`` 是电压 ×100。**非正数不是「电量很低」，而是「这次读数无效」**：
    一台刚刚回答完寄存器读的设备不可能是 0 V。阶梯表的最低一档把 ``<340`` 映射
    到 0%，若原样套用，一个无效读数会变成一个看着正常的「没电了」——调用方据此
    去换电池，而真正的问题在别处。这与 :func:`magnetic_field_to_ut` 在量纲类型
    未知时返回 ``None`` 是同一条原则：**不把不知道的东西说成知道的样子。**

    阈值以下的**合法**低电量读数不受影响：339 仍然是 0%，340 仍然是 5%。
    """
    if raw <= 0:
        return None
    for threshold, percent in BATTERY_PERCENT_STEPS:
        if raw >= threshold:
            return percent
    return 0
