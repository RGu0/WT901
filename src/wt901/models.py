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
from wt901.protocol.registers import AlgorithmMode

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

    **有两处独立的不可信来源，别混为一谈**——一个关于单位，一个关于新鲜度：

    ===========================  ==========================================
    信号                         含义
    ===========================  ==========================================
    ``value is None``            寄存器 ``0x72`` 报告的量纲类型不在已知分档
                                 内，换算系数无从取得。只有 ``raw`` 可用，
                                 且单位未知。
    :attr:`may_be_stale`         设备当时不在 9 轴模式，磁力计**没有在采样**，
                                 ``raw`` 可能是一个任意陈旧的值。单位没问题，
                                 数值本身不可信。
    ===========================  ==========================================

    两者可以同时成立，也可以都不成立。`value` 不是 ``None`` **不代表**数值新鲜。
    """

    value: Vec3 | None
    """单位 µT；量纲类型未知时为 ``None``。

    **它只表示"换算得出来"，不表示"值是新的"。** 新鲜度看 :attr:`may_be_stale`。
    """
    mag_type: int
    raw: tuple[int, ...]
    algorithm_mode: int | None = None
    """读数产生时设备的姿态解算算法（寄存器 ``0x24``），未取到时为 ``None``。

    ``0`` 是 9 轴、``1`` 是 6 轴（编码与直觉相反，见
    :class:`~wt901.protocol.registers.AlgorithmMode`）。记进读数里而不是只放在
    别处，是因为**它决定这次读数可不可信**，而调用方拿到的是这个对象。
    """

    @property
    def is_calibrated_unit(self) -> bool:
        """读数是否带确定单位。**与新鲜度无关**，见 :attr:`may_be_stale`。"""
        return self.value is not None

    @property
    def may_be_stale(self) -> bool:
        """这个读数是否**可能任意陈旧**。

        真机实测（RAY-344，两台 WT901BLE67）：``0x24 = 1``（6 轴）时设备不采样
        磁力计，``0x3A``–``0x3C`` 停在一个固定值上不再更新——转动设备、开着实时
        流、跨连接会话都不变。切到 9 轴后恢复更新，切回 6 轴又停住。

        **"任意"两个字是实测出来的，不是保守措辞。** 那个值**跨断电存活**：两台
        设备断电重启、换到明显不同的朝向后读回来的，与断电前**逐字节相同**
        （RAY-344 scope 2，判据取证前预注册）。它还跨越过十几个小时、一整段 9 轴
        运行和多次断连重连。

        所以它**不是"磁力计停工前的最后一个样本"**——那种值断电就没了。它存在
        某种非易失存储里，**陈旧的时间尺度可以长到设备上一次处于 9 轴是什么时候**。
        别按"大概几秒前"来用，也别指望重启能把它清掉。

        :attr:`algorithm_mode` 为 ``None``（没取到）时**也返回 ``True``**：那种
        情况下本库无法排除陈旧，按本库一贯的规矩，拿不准就不声称它是新鲜的。
        要区分"确知陈旧"与"不知道"，看 :attr:`algorithm_mode` 本身。
        """
        return self.algorithm_mode != AlgorithmMode.NINE_AXIS


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
