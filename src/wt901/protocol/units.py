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

    ⚠ **三份上游资料对磁场量纲的说法互相矛盾，本库此前只记了其中两份。**

    ============================  ==========================================
    资料                          它说的换算
    ============================  ==========================================
    本型号官方协议文档            原始值单位**就是 mG**（``Hx=((HxH<<8)|HxL)``，
                                  并注明「和电脑上位机显示的单位不一样」），
                                  即 µT = raw × 0.1，**与 ``0x72`` 无关**
    官方 C# / Android SDK         按 ``0x72`` 分档，type 2 → ×0.15（本函数）
    官方 Python 示例              写死 ``raw / 120``（≈ ×0.00833）
    ============================  ==========================================

    三者两两都对不上，而且**第一份是本型号自己的协议文档**——它比 SDK 的出处更硬，
    却说磁场换算根本不需要读 ``0x72``。这不是「Python 示例陈旧」一句能解释掉的
    （此前这里就是这么写的，只提了后两份），是一处真正的三方矛盾。

    **本库暂时仍按 SDK 分档**，理由是它能解释 ``0x72`` 这个寄存器为什么存在——若
    原始值恒为 mG，那个寄存器就没有用处，而 C#、Android 两份独立实现都在读它。
    但这只是**权衡下的暂定选择，不是结论**：本库没有实测过任何一档，无法判定谁对。

    **按本项目方针「官方资料互相矛盾处先立 Issue、以实测定论」，这条要单独立项取证，
    不在实现里选边。** 判定它只需一次实测：把设备放进已知场强（例如地磁，约
    25–65 µT）里读原始值，看哪一份换算能算出合理数量级。在那之前，拿本函数的返回值
    做绝对场强判断的调用方要知道**它可能整体差 1.5 倍或 18 倍**；只用它做相对变化
    或方向的调用方不受影响。
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
