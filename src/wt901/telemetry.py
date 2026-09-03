"""按需读取磁场、四元数、温度、电量、芯片时间、MAC、序列号、版本号。

这些量**不在** ``0x61`` 实时数据流里，必须主动读寄存器取回。它们与实时数据流
共用同一条 BLE 链路，所以读得越勤，留给采样的带宽越少——这就是本模块的周期
轮询默认关闭的原因。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from wt901.errors import UnexpectedRegisterResponse, WT901Error
from wt901.models import DeviceInfo, MagneticField, Quaternion, Vec3
from wt901.protocol import units
from wt901.protocol.registers import (
    MAC_WORDS,
    SERIAL_NUMBER_WORDS,
    Register,
)

if TYPE_CHECKING:
    from wt901.device import WT901Device

__all__ = [
    "Battery",
    "ChipTime",
    "PollerConfig",
    "SerialNumber",
    "Telemetry",
    "TelemetryPoller",
]

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ChipTime:
    """芯片自身维护的时间。

    与主机时钟无关，也不保证被校准过——它的用处是判断设备是否重新上电，
    不是给样本打时间戳。样本时间用 :attr:`~wt901.models.ImuSample.t_host`。
    """

    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    millisecond: int

    @property
    def is_plausible(self) -> bool:
        """这几个字段是否构成一个可能存在的日期。

        寄存器整块回读全零时得到 ``month = 0``、``day = 0``——日历上没有第 0 月、
        第 0 日，所以那**不是一个时间戳**。真机上全零回读是有据可查的现象，见
        ``docs/protocol.md`` §10。

        **这里刻意不抛异常**，与 :meth:`~wt901.telemetry.Telemetry.read_mac` 的处理
        不同。理由是本方法要回答的问题恰好是「设备是不是刚上电」：一台时钟从未设过
        的设备**可能本来就报全零**，而本库没有核实过它上电时报什么。抛异常会把「时钟
        未设」和「这次没读出内容」一起抹掉，而前者正是调用方问的那件事。给出字段并
        附这个标志，两种情况都还看得见。

        判据只针对**全零回读**这一种情形，不做日历校验：``month = 0`` / ``day = 0``
        是那次读取的签名，而「2 月 31 日」这类非零的错日期没有任何证据说明会发生，
        为它写规则就是解决一个不存在的问题。年、时、分、秒的全零本身都是可能的真实
        取值（2000 年、零点整），所以不看它们。
        """
        return 1 <= self.month <= 12 and 1 <= self.day <= 31

    def __str__(self) -> str:
        if not self.is_plausible:
            # 不能打成 "2000-00-00 00:00:00.000"——那看着像个时间戳，而它不是。
            # 数据层挡住了、显示层再把歧义造一遍，是 RAY-293 踩过的坑。
            return f"<非法芯片时间 {self.year:04d}-{self.month:02d}-{self.day:02d}>"
        return (
            f"{self.year:04d}-{self.month:02d}-{self.day:02d} "
            f"{self.hour:02d}:{self.minute:02d}:{self.second:02d}"
            f".{self.millisecond:03d}"
        )


@dataclass(frozen=True, slots=True)
class Battery:
    """电量。``percent`` 来自官方阶梯查表，不是线性插值。

    ``percent`` 为 ``None`` 表示 ``raw`` 不可能是一次真实测量（电压 ×100 不会是
    非正数），此时只有 ``raw`` 可用。形状与 :class:`~wt901.models.MagneticField`
    一致：拿不准的量纲/数值不硬给一个看着正常的结果。
    """

    raw: int
    percent: int | None

    @property
    def is_plausible(self) -> bool:
        """这次读数是否可能是真实测量。"""
        return self.percent is not None


@dataclass(frozen=True, slots=True)
class SerialNumber:
    """序列号。``value`` 为 ``None`` 表示这次读取没读出内容。

    形状与 :class:`Battery` 一致，理由也一样：读到了一个**不可能是真实内容**的
    回读时，不硬给一个看着正常的结果。这里的「不可能」很具体——12 个字节全是 0
    不是一个空序列号，是一次读不出内容的读取，而本器件真机上就是这样
    （RAY-172、RAY-293）。

    ``raw`` 始终给出，因为判定成因需要它：全零、部分可读、还是掺了非 ASCII 字节，
    指向的方向完全不同。

    **刻意不提供** ``__str__``。给它一个「全零时返回空串」的实现，等于把本类要消除的
    那个歧义原样搬进显示层——``f"SN: {serial}"`` 又会打出与「字段为空」一模一样的东西。
    :class:`Battery` 同样没有 ``__str__``。要显示就显式取 ``value``，拿不准先看
    ``is_plausible``。
    """

    raw: bytes
    value: str | None

    @property
    def is_plausible(self) -> bool:
        """这次读数是否可能是真实内容。"""
        return self.value is not None


@dataclass
class PollerConfig:
    """周期补充读取的节奏。单位秒；``None`` 表示不读该项。

    默认值刻意保守：磁场与四元数 1 秒一次已足够多数场景，而温度电量这类几乎
    不变的量没必要频繁读。官方 SDK 无条件开线程每轮都读磁场与四元数，那在
    100 Hz 采集下会明显挤占带宽。
    """

    magnetic_field: float | None = 1.0
    quaternion: float | None = 1.0
    temperature: float | None = 30.0
    battery: float | None = 30.0
    rssi: float | None = 5.0
    """链路信号强度的读取周期。

    比其它几项密，因为它测的是**正在变化的东西**：人走动、身体遮挡、设备转向，
    秒级就能让链路质量变一截。官方 SDK 的读数据线程每 5 轮读一次，同一个量级。

    它也比其它几项便宜——走链路层往返，不占 ``0x61``/``0x71`` 通道的带宽（见
    :meth:`~wt901.transport.ble.BleTransport.read_rssi`）。在拿不到 RSSI 的平台
    上这一项等于每 5 秒做一次 ``getattr`` 判断，代价可以忽略。
    """


def _u16(value: int) -> int:
    """把 int16 还原成无符号 16 位。寄存器里存的是位模式，不是有符号数。"""
    return value & 0xFFFF


async def _attempt(factory: Callable[[], Awaitable[_T]], label: str) -> _T | None:
    """执行一次读取，失败则返回 ``None`` 并记 debug 日志。"""
    try:
        return await factory()
    except WT901Error as exc:
        _LOGGER.debug("读取%s失败，已跳过：%s", label, exc)
        return None


class Telemetry:
    """一台设备的按需读取通道，通过 ``device.telemetry`` 访问。"""

    def __init__(self, device: WT901Device) -> None:
        self._device = device
        self._mag_type: int | None = None
        self._probed_algorithm_mode: int | None = None
        self._algorithm_probed = False

    @property
    def magnetic_field_type(self) -> int | None:
        """已缓存的磁场量纲类型（寄存器 ``0x72``），未读过时为 ``None``。"""
        return self._mag_type

    @property
    def algorithm_mode(self) -> int | None:
        """当前已知的姿态解算算法（寄存器 ``0x24``），不知道时为 ``None``。

        **这与 **:attr:`magnetic_field_type` **不是同一种缓存。** 量纲类型是硬件
        配置、运行中不会变，读一次就够；算法模式是**配置**，
        :meth:`~wt901.config.RegisterAccess.set_algorithm` 随时能改它。

        所以这里**写入优先**：本库在这条连接上写过 ``0x24`` 的话就以写入值为准
        （``applied_writes`` 已经是重连后配置重放的依据，与设备状态同步），只在
        从没写过时才用读回来的那个值。**本属性与 **
        :attr:`~wt901.models.MagneticField.may_be_stale` **取的是同一个值**——
        一个只读缓存的属性会在调用方切过模式之后给出与判定相矛盾的答案。

        读回来的那个值**只探一次**，理由见 :meth:`_current_algorithm_mode`。
        """
        for entry in self._device.registers.applied_writes:
            if entry.register == Register.ALGORITHM:
                return _u16(entry.value)
        return self._probed_algorithm_mode

    async def _current_algorithm_mode(self) -> int | None:
        """取当前的 ``0x24``；取不到时返回 ``None`` 而不是抛异常。

        **已知就直接用，否则只探一次。** ``0x24`` 不在官方寄存器地址表里
        （RAY-309 的溯源表），某些固件上可能根本不应答；每次调用都重试一遍会让
        每次磁场读取白等一整轮读超时（默认 ``0.5 s × 3``），而这条链路是与
        ``0x61`` 实时流共用的。

        探测失败后新鲜度按**未知**处理——那是安全的方向
        （:attr:`~wt901.models.MagneticField.may_be_stale` 为 ``True``），且一次
        :meth:`~wt901.config.RegisterAccess.set_algorithm` 就能让它重新变为已知，
        因为写入优先于探测结果。

        ⚠ **本库之外改的算法模式看不见**（例如维特上位机改了它）。那种情况下这里
        给出的是过时的值，而它决定 ``may_be_stale``。已知局限，记在
        ``docs/protocol.md`` §5.7.1。
        """
        known = self.algorithm_mode
        if known is not None:
            return known
        if not self._algorithm_probed:
            try:
                self._probed_algorithm_mode = _u16(
                    await self._device.registers.read_value(Register.ALGORITHM)
                )
            except WT901Error as exc:
                _LOGGER.debug("读 0x24 失败，磁场读数的新鲜度按未知处理：%s", exc)
            # 只有走完 try/except 才算探过——被取消打断不该让新鲜度永久变成未知。
            self._algorithm_probed = True
        return self.algorithm_mode

    async def read_magnetic_field(self) -> MagneticField:
        """读磁场三轴。

        必须先取寄存器 ``0x72`` 的量纲类型——磁场换算系数不是常量，而是按该
        类型分档。类型只读一次并缓存：它描述的是硬件配置，不会在运行中变化。

        量纲类型不在已知分档内时 :attr:`MagneticField.value` 为 ``None``，只有
        ``raw`` 可用。官方 Android SDK 在这种情况下原样返回未换算的计数值，
        那会让调用方拿到一个单位不明却看着正常的数。

        **同时取寄存器 ``0x24``（姿态解算算法），因为它决定这次读数可不可信。**
        6 轴模式下设备不采样磁力计，``0x3A``–``0x3C`` 停在一个可能任意陈旧的值上
        （RAY-344，两台设备实测）。结果记进
        :attr:`MagneticField.algorithm_mode`，判据是
        :attr:`MagneticField.may_be_stale`。

        **那个判据与 ``value`` 是否为 ``None`` 互相独立**：换算得出来不代表值是新的。

        ``0x24`` 与量纲类型一样只读一次并缓存，**不会给每次调用增加一次寄存器读**
        （那会与 ``0x61`` 实时流抢链路）。但它是配置而非硬件属性，所以取值时
        写入优先，细节见 :meth:`_current_algorithm_mode`。
        """
        if self._mag_type is None:
            self._mag_type = _u16(await self._device.registers.read_value(Register.MAGTYPE))
        algorithm_mode = await self._current_algorithm_mode()

        response = await self._device.registers.read(Register.HX)
        raw = response.values[:3]
        converted = [units.magnetic_field_to_ut(self._mag_type, value) for value in raw]

        value: Vec3 | None = None
        if all(component is not None for component in converted):
            value = Vec3(*(component for component in converted if component is not None))
        return MagneticField(
            value=value,
            mag_type=self._mag_type,
            raw=tuple(raw),
            algorithm_mode=algorithm_mode,
        )

    async def read_quaternion(self) -> Quaternion:
        """读姿态四元数。Q0–Q3 是回帧的前 4 个寄存器。

        **归一化之前先看** :attr:`~wt901.models.Quaternion.is_plausible`：寄存器整块
        回读全零时这里会交出 ``(0, 0, 0, 0)``，它的模是 0，不表示任何朝向，归一化会
        得到 NaN 并一路飘进姿态解算。它为何附标志而不抛异常，写在该属性的文档里。
        """
        response = await self._device.registers.read(Register.Q0)
        raw = response.values[:4]
        w, x, y, z = (units.quaternion_component(value) for value in raw)
        return Quaternion(w=w, x=x, y=y, z=z, raw=tuple(raw))

    async def read_temperature(self) -> float:
        """读温度，单位 °C。"""
        return units.temperature_to_celsius(
            await self._device.registers.read_value(Register.TEMPERATURE)
        )

    async def read_battery(self) -> Battery:
        """读电量。同时给出原始值与百分比。

        百分比走官方的 12 档阶梯查表。原始值也返回，因为阶梯很粗（比如 350–367
        全都报 10%），需要更细的判断时只能看原始值。
        """
        raw = await self._device.registers.read_value(Register.POWER)
        return Battery(raw=raw, percent=units.battery_percent(raw))

    async def read_chip_time(self) -> ChipTime:
        """读芯片时间。

        四个寄存器里前三个各自的高低字节承载两个字段，所以不能按「一个寄存器
        一个字段」来读。

        **用之前先看** :attr:`ChipTime.is_plausible`：寄存器整块回读全零时这里会
        交出 ``month = 0``、``day = 0``，那不是一个时间戳。它为何附标志而不抛异常，
        写在该属性的文档里。
        """
        response = await self._device.registers.read(Register.CHIP_TIME_YEAR_MONTH)
        year_month, day_hour, minute_second, millisecond = (
            _u16(value) for value in response.values[:4]
        )
        return ChipTime(
            year=2000 + (year_month & 0xFF),
            month=(year_month >> 8) & 0xFF,
            day=day_hour & 0xFF,
            hour=(day_hour >> 8) & 0xFF,
            minute=minute_second & 0xFF,
            second=(minute_second >> 8) & 0xFF,
            millisecond=millisecond,
        )

    async def read_mac(self) -> str:
        """读设备自报的蓝牙地址，返回 ``XX:XX:XX:XX:XX:XX``（大写冒号分隔）。

        **这是本库唯一可跨主机持久化的设备身份。**
        :attr:`~wt901.discovery.DiscoveredDevice.address` 在 macOS 上是
        CoreBluetooth UUID，换台主机就变；广播名同批次重复；序列号在真机上读到过
        全零（见 :meth:`read_serial_number`）。要把设备绑到「左脚/右脚」这类角色
        并持久化，只能用这个。

        **字节序**：``0x66``–``0x68`` 三个寄存器按小端取出的 6 个字节是地址的空口
        顺序（低位在前），所以整体倒过来才是显示顺序。2026-08-27 两台 WT901BLE67
        实测（``tools/probe_mac.py``，证据 ``ray-279/device-mac/acceptance/``）：

        ====================  =========================  =========================
        寄存器 0x66/0x67/0x68  取出的 6 字节               本方法返回
        ====================  =========================  =========================
        1147 901A FD08        ``47 11 1A 90 08 FD``      ``FD:08:90:1A:11:47``
        C931 4F46 F9B3        ``31 C9 46 4F B3 F9``      ``F9:B3:4F:46:C9:31``
        ====================  =========================  =========================

        **排布是推出来的，不是拿标签比出来的**——设备标签上没印 MAC，macOS 也不给。
        但四种可能的排布里只有这一种让**两台**设备都得到合法的蓝牙地址：倒过来后
        两个首字节 ``0xFD``/``0xF9`` 的高 2 位都是 ``11``，即 BLE 随机静态地址；另
        三种排布至少有一台落在「组播位为 1 的公有地址」（不存在）或「私有地址」
        （会自己轮换，不可能固定写在寄存器里）。空口低位在前也正是 BLE 传地址的
        方式。**要推翻它，只需在 Windows/Linux/Android 主机上看一眼同一台设备的
        MAC**——对不上就是这个方法错了，改一行加换 fixture 即可。

        全零的应答会抛 :class:`~wt901.errors.UnexpectedRegisterResponse` 而不是
        返回 ``00:00:00:00:00:00``：本器件的序列号寄存器就读到过逐字节全零
        （RAY-172），MAC 若也如此，一个「所有设备都相同的稳定绑定键」会让两台设备
        安静地互相冒充。只挡全零，不发明别的过滤规则——没实测到的不算数。
        """
        response = await self._device.registers.read(Register.MAC)
        words = [_u16(value) for value in response.values[:MAC_WORDS]]
        payload = b"".join(word.to_bytes(2, "little") for word in words)
        if not any(payload):
            raise UnexpectedRegisterResponse(
                "寄存器 0x66 回读全零，这不可能是一个真实的蓝牙地址"
            )
        return ":".join(f"{byte:02X}" for byte in reversed(payload))

    async def read_serial_number(self) -> SerialNumber:
        """读 12 字节 ASCII 序列号。

        ⚠ **这个寄存器（``0x7F``）在协议文档与官方 SDK 里都查不到**，2026-08-28 回头
        逐项核对时才发现（``ray-309``）。它可能根本不存在于本型号——那样的话本方法
        承诺的就是一个本器件没有的东西，而「读未定义地址返回零」正好能解释下面那条
        全零现象。定论要等 ``ray-309`` scope 2 的实测（读官方标为保留/未定义的地址
        与 ``0x7F`` 比对）。**本方法的行为不因此改变**：它如实报告读到了什么，而不是
        替调用方断言那是什么。

        序列号占 6 个寄存器，一次读取回 8 个，所以**一趟就够**。RAY-292 之前这里读了
        两次（``0x7F`` 与 ``0x82``），因为
        :data:`~wt901.protocol.registers.REGISTERS_PER_RESPONSE` 当时错写成 4；第二趟
        是白跑的，而寄存器读与 ``0x61`` 实时流抢同一条链路。

        非 ASCII 字节以 ``\\uFFFD`` 替换而非抛异常——序列号读花了是个诊断线索，不该
        让整个设备信息读取失败。

        **全零的回读不是一个空序列号，是一次读不出内容的读取。**
        :attr:`SerialNumber.value` 此时为 ``None``，只有 ``raw`` 可用。这不是假想
        防御：本器件真机上就读到过逐字节全零（RAY-172，`ray-279` 的两台设备再次
        全零），此前它会变成空字符串 ``""``，在 :class:`~wt901.models.DeviceInfo`
        里与「没读到」长得一模一样，而下游拿 ``""`` 当绑定键会让每台设备的键都相同。

        与 :meth:`read_mac` 的形状不同是**有意的**：MAC 只有一个用途——身份，一个
        不可信的 MAC 毫无用处，所以那里直接抛异常。序列号是设备信息，「它读出来是
        全零」这件事本身就是诊断线索，值得原样交出来，与 :class:`Battery` 同理。
        """
        response = await self._device.registers.read(Register.SERIAL_NUMBER)
        words = response.values[:SERIAL_NUMBER_WORDS]

        payload = b"".join(_u16(word).to_bytes(2, "little") for word in words)
        value: str | None = None
        if any(payload):
            value = payload.decode("ascii", errors="replace").rstrip("\x00")
        return SerialNumber(raw=payload, value=value)

    async def read_version(self) -> str:
        """读固件版本号。

        两个寄存器拼成 uint32。最高位为 1 时是「新版本号」编码，按 17/6/8 位
        拆成 ``主.次.修订``；否则退回直接显示低位寄存器的无符号值。这个分支来自
        官方 C# 实现，两种编码在真实设备上都存在。

        两个寄存器都为零时抛 :class:`~wt901.errors.UnexpectedRegisterResponse`，
        而不是走遗留分支返回 ``"0"``——那是一个看着像版本号的假值。这里选择抛异常
        而不是像 :class:`ChipTime` 那样附标志：版本号只有「显示与比对」一个用途，
        一个不可信的版本号毫无用处，与 :meth:`read_mac` 同理。经
        :func:`read_device_info` 的逐项容错后，该字段自然落成 ``None``。

        真机记录过的版本是 ``10080.1.22``（``ray-172/acceptance/``）。
        """
        response = await self._device.registers.read(Register.VERSION_LOW)
        low, high = _u16(response.values[0]), _u16(response.values[1])
        packed = low | (high << 16)
        if packed == 0:
            raise UnexpectedRegisterResponse(
                "寄存器 0x2E/0x2F 回读全零，这不可能是一个真实的版本号"
            )
        bits = f"{packed:032b}"
        if bits[0] == "1":
            major = int(bits[1:18], 2)
            minor = int(bits[18:24], 2)
            patch = int(bits[24:], 2)
            return f"{major}.{minor}.{patch}"
        return str(low)

    async def read_rssi(self) -> int | None:
        """连接期的链路信号强度（dBm），取不到时 ``None``。

        **这一项不走寄存器。** 其余每个 ``read_*`` 都是一次 ``0x71`` 往返，这个
        是链路层的量，直接问传输层要。放在这里只有一个理由：它和电量、温度一样
        是「周期补充读取的一个量」，而周期读取的调度在
        :class:`TelemetryPoller` 上——把它放到别处，调用方就得同时盯两个轮询器。

        它与其它几项的差别值得记住：**这是原因侧的量。** 电量、温度描述设备的
        状态，RSSI 描述**链路**的状态，是唯一一个能在丢包发生之前给出预警的。

        取不到时是 ``None``，永远不是 0。成因与平台限制见
        :meth:`WT901Device.read_rssi`。本方法不抛异常。
        """
        return await self._device.read_rssi()

    async def read_device_info(self) -> DeviceInfo:
        """一次性读齐设备身份信息。

        逐项容错：任何一项读失败只让该字段为 ``None``，不影响其余项。设备信息
        是诊断用的，「序列号读不到」不该连温度也一起拿不到。

        电量只读一次：``percent`` 与 ``raw`` 来自同一次读取，否则两个字段可能
        取自不同时刻的两次 BLE 往返，白白多花一次链路时间。序列号同理。
        """
        battery = await _attempt(self.read_battery, "电量")
        serial = await _attempt(self.read_serial_number, "序列号")
        return DeviceInfo(
            serial_number=serial.value if serial is not None else None,
            serial_number_raw=serial.raw if serial is not None else None,
            mac=await _attempt(self.read_mac, "MAC"),
            version=await _attempt(self.read_version, "版本号"),
            temperature_c=await _attempt(self.read_temperature, "温度"),
            battery_percent=battery.percent if battery is not None else None,
            battery_raw=battery.raw if battery is not None else None,
        )


class TelemetryPoller:
    """周期性补充读取。**默认不启动。**

    寄存器那四项（磁场、四元数、温度、电量）与 ``0x61`` 实时数据流抢同一条 BLE
    链路，所以做成显式开启，且每一项的周期都能单独关掉。代价的实测值见
    ``docs/protocol.md`` §8：本库默认配置在 100 Hz 采集下的影响是 0.1%–1.4%，
    **不大**；默认关闭的理由是「不用的人不该付费」以及代价随周期缩短线性增长。

    :attr:`rssi` 是第五项，也是**唯一不走寄存器**的一项——它是链路层的量，不占
    ``0x61``/``0x71`` 通道的带宽（见
    :meth:`~wt901.transport.ble.BleTransport.read_rssi`）。

    **不可信的读数照常写进属性，可信与否随值一起走。** 寄存器那四项每一项的类型
    都自带判据——:attr:`Battery.is_plausible`、
    :attr:`~wt901.models.Quaternion.is_plausible`，而磁场**有两个且互相独立**：
    :attr:`~wt901.models.MagneticField.value` 为 ``None``（量纲类型未知，换算不
    出来）与 :attr:`~wt901.models.MagneticField.may_be_stale`（6 轴模式下磁力计
    没在采样，数值可能任意陈旧）。**换算得出来不代表值是新的**，两个都要看。
    所以这里不需要额外的过滤或计数。

    轮询器不做「保持上一次的有效值」这类事：那会让一个陈旧的值冒充当前状态，比
    给出一个自带「不可信」标志的新值更难查。**但要注意本层做不到的那一半**：
    :attr:`magnetic_field` 在 6 轴下每个周期都会被写进一个**新的对象**，而那个
    对象里的 ``raw`` 可能自上电起就没变过。轮询器只保证「这是刚读回来的」，
    保证不了「这是刚测出来的」——后者只有 ``may_be_stale`` 说了算。

    **读取失败时的处置分两种，别记混：**

    * 寄存器那四项：失败（超时、链路错）时属性**保持不变**并记 debug 日志，见
      :meth:`_loop`。那与「读到了但值不可信」是两回事，后者会写进来。
    * :attr:`rssi`：失败在传输层就被吞成 ``None``，所以属性会**被写成**
      ``None``。这是有意的——一个陈旧的信号强度比没有信号强度更危险，它正是用来
      判断此刻链路好不好的。
    """

    def __init__(
        self,
        telemetry: Telemetry,
        config: PollerConfig | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._config = config or PollerConfig()
        self._tasks: list[asyncio.Task[None]] = []

        self.magnetic_field: MagneticField | None = None
        """最近一次读到的磁场。**用之前先看
        :attr:`~wt901.models.MagneticField.may_be_stale`**——6 轴模式下这里会
        每个周期都刷新成一个新对象，而里面的数值可能任意陈旧（RAY-344）。"""
        self.quaternion: Quaternion | None = None
        """最近一次读到的四元数。**用之前先看
        :attr:`~wt901.models.Quaternion.is_plausible`**——全零回读会写进这里。"""
        self.temperature_c: float | None = None
        self.battery: Battery | None = None
        self.rssi: int | None = None
        """最近一次读到的链路信号强度，单位 dBm。

        ``None`` 有两种成因，**这里区分不了**：从没读到过，或者这个平台根本给
        不出（只有 macOS 给得出）。两种都意味着同一件事——不要拿它当链路质量的
        判据。区分方法见 :meth:`WT901Device.read_rssi`。

        与其它三项一样，读失败时保持不变而不是清空。但 RSSI 的「读失败」已经在
        传输层被吞成 ``None`` 了，所以这里实际上会写进 ``None``——**这是有意的**：
        一个陈旧的 RSSI 比没有 RSSI 更危险，它正是用来判断此刻链路好不好的。
        """

    @property
    def is_running(self) -> bool:
        return bool(self._tasks)

    def start(self) -> None:
        """启动轮询。重复调用无副作用。"""
        if self._tasks:
            return
        loop = asyncio.get_running_loop()
        for interval, reader in (
            (self._config.magnetic_field, self._poll_magnetic_field),
            (self._config.quaternion, self._poll_quaternion),
            (self._config.temperature, self._poll_temperature),
            (self._config.battery, self._poll_battery),
            (self._config.rssi, self._poll_rssi),
        ):
            if interval is not None and interval > 0:
                self._tasks.append(loop.create_task(self._loop(interval, reader)))

    async def stop(self) -> None:
        """停止轮询并等待任务退出。"""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(
        self, interval: float, read: Callable[[], Awaitable[None]]
    ) -> None:
        while True:
            try:
                await read()
            except WT901Error as exc:
                # 单次读失败不该终止轮询：链路瞬时拥塞很常见，下个周期再试即可。
                _LOGGER.debug("周期读取失败，将在下个周期重试：%s", exc)
            await asyncio.sleep(interval)

    async def _poll_magnetic_field(self) -> None:
        self.magnetic_field = await self._telemetry.read_magnetic_field()

    async def _poll_quaternion(self) -> None:
        self.quaternion = await self._telemetry.read_quaternion()

    async def _poll_rssi(self) -> None:
        # 不套 _attempt：传输层承诺不抛，失败已经是 None。这一项是唯一一个
        # 「读不到就写 None」的——陈旧的信号强度比没有更危险。
        self.rssi = await self._telemetry.read_rssi()

    async def _poll_temperature(self) -> None:
        self.temperature_c = await self._telemetry.read_temperature()

    async def _poll_battery(self) -> None:
        self.battery = await self._telemetry.read_battery()
