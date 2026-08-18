"""对外数据模型。

全部为 frozen dataclass：样本一旦产生就不该被下游改写，尤其在多设备并发采集
时。每个模型都带 ``raw`` 通道，暴露未换算的 int16 计数值——那是逃生舱，不是
并行的单位制。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from wt901.protocol import units
from wt901.protocol.frames import Frame, decode_data_frame

__all__ = [
    "DeviceInfo",
    "Euler",
    "ImuSample",
    "MagneticField",
    "Quaternion",
    "RawImuCounts",
    "Vec3",
]


@dataclass(frozen=True, slots=True)
class Vec3:
    """三轴矢量。单位由持有它的字段决定。"""

    x: float
    y: float
    z: float

    @property
    def magnitude(self) -> float:
        """矢量模。用 :func:`math.hypot` 而非手写平方和，避免中间量溢出/下溢。"""
        return math.hypot(self.x, self.y, self.z)


@dataclass(frozen=True, slots=True)
class Euler:
    """欧拉角，单位 rad。

    对应器件的 Roll(X) / Pitch(Y) / Yaw(Z)。量程 Roll ±π、Pitch ±π/2、Yaw ±π。
    """

    roll: float
    pitch: float
    yaw: float


@dataclass(frozen=True, slots=True)
class RawImuCounts:
    """0x61 帧的 9 个 int16 原始计数值，按帧内顺序排列。"""

    values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ImuSample:
    """一次实时数据帧对应的样本。

    ``t_host`` 是**主机收到 BLE 通知的时刻**，不是采样时刻：器件不提供时间戳。
    它含蓝牙栈抖动，本库不做插值也不伪造均匀时基。多设备之间的时间对齐是上层
    的职责，本库只保证时间戳来源一致且单调。
    """

    device_id: str
    t_host: float
    seq: int
    accel: Vec3
    """加速度，m/s²。"""
    gyro: Vec3
    """角速度，rad/s。"""
    euler: Euler
    """姿态角，rad。"""
    raw: RawImuCounts

    @classmethod
    def from_frame(
        cls, frame: Frame, *, device_id: str, t_host: float, seq: int
    ) -> ImuSample:
        """从一个 ``0x61`` 帧构造样本。

        ``t_host`` 由调用方传入而不是在这里取时钟，以保持协议层无副作用、可测试。
        """
        counts = decode_data_frame(frame)
        return cls(
            device_id=device_id,
            t_host=t_host,
            seq=seq,
            accel=Vec3(*(units.accel_to_m_s2(value) for value in counts[0:3])),
            gyro=Vec3(
                *(units.angular_velocity_to_rad_s(value) for value in counts[3:6])
            ),
            euler=Euler(*(units.angle_to_rad(value) for value in counts[6:9])),
            raw=RawImuCounts(values=counts),
        )


@dataclass(frozen=True, slots=True)
class Quaternion:
    """姿态四元数，无量纲。``w`` 对应器件的 Q0。"""

    w: float
    x: float
    y: float
    z: float
    raw: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MagneticField:
    """磁场三轴。

    ``value`` 为 ``None`` 表示寄存器 ``0x72`` 报告的量纲类型不在已知分档内，
    本库拒绝猜测系数。此时只有 ``raw`` 可用，且单位未知。
    """

    value: Vec3 | None
    """单位 µT；量纲类型未知时为 ``None``。"""
    mag_type: int
    raw: tuple[int, ...]

    @property
    def is_calibrated_unit(self) -> bool:
        """读数是否带确定单位。"""
        return self.value is not None


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """设备身份信息。字段为 ``None`` 表示尚未读取或读取失败。"""

    serial_number: str | None = None
    version: str | None = None
    temperature_c: float | None = None
    battery_percent: int | None = None
    battery_raw: int | None = None
