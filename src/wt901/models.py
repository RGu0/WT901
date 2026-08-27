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

    @property
    def is_plausible(self) -> bool:
        """这次读数是否可能是一个真实姿态。

        ``(0, 0, 0, 0)`` 的模是 0，**不表示任何朝向**——单位四元数的模恒为 1。
        它只会来自一次没读出内容的读取：真机上寄存器整块回读全零是有据可查的现象
        （见 :meth:`~wt901.telemetry.Telemetry.read_serial_number` 与
        ``docs/protocol.md`` §10）。

        判据只看**四个原始值是否全为零**，不检查模是否接近 1：器件给的是定点数，
        正常读数的模也只是「接近」1，划一条容差线就是发明一个没实测过的规则。与
        :class:`~wt901.telemetry.SerialNumber` 只看「有没有非零字节」是同一条线。

        字段照常给出——``False`` 时它们全是 0，而 ``raw`` 是判定成因需要的东西。
        **归一化之前先看这个属性**：对模为 0 的四元数归一化会得到 NaN，那个 NaN
        会一路飘进姿态解算，等有人发现时已经离现场很远。
        """
        return any(self.raw)


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
    """设备身份信息。字段为 ``None`` 表示尚未读取或读取失败。

    ``battery_percent`` 多一层含义，靠 ``battery_raw`` 区分：

    * ``battery_raw is None`` —— 这一项没读到（未读或读失败）
    * ``battery_raw`` 有值而 ``battery_percent`` 为 ``None`` —— 读到了，但原始值
      不可能是一次真实测量，本库拒绝把它映射成一个看着正常的百分比

    ``serial_number`` 与 ``serial_number_raw`` 是同一组关系：

    * ``serial_number_raw is None`` —— 这一项没读到（未读或读失败）
    * ``serial_number_raw`` 有值而 ``serial_number`` 为 ``None`` —— 读到了，但内容
      是逐字节全零，那不是一个空序列号而是一次读不出内容的读取（RAY-293）

    要做持久化的设备身份用 ``mac``，别用 ``serial_number``：真机上读到过逐字节
    全零的序列号（RAY-172），而在此之前那种失败是安静的——它会变成空字符串。
    """

    serial_number: str | None = None
    version: str | None = None
    temperature_c: float | None = None
    battery_percent: int | None = None
    battery_raw: int | None = None
    mac: str | None = None
    """设备自报的蓝牙地址 ``XX:XX:XX:XX:XX:XX``，见
    :meth:`~wt901.telemetry.Telemetry.read_mac`。**唯一可跨主机持久化的身份。**

    位置靠后只是因为**新字段一律追加在末尾**，不挪动既有字段的位置——它其实是这里
    最该被拿来当身份的那个。
    """
    serial_number_raw: bytes | None = None
    """序列号的原始 12 字节，见 :class:`~wt901.telemetry.SerialNumber`。

    与 ``battery_raw`` 的作用相同：区分「没读到」与「读到了但内容不可能」，并在
    后者发生时保留判定成因所需的字节。
    """
