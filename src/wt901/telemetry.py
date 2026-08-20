"""按需读取磁场、四元数、温度、电量、芯片时间、序列号、版本号。

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

from wt901.errors import WT901Error
from wt901.models import DeviceInfo, MagneticField, Quaternion, Vec3
from wt901.protocol import units
from wt901.protocol.registers import (
    SERIAL_NUMBER_WORDS,
    Register,
)

if TYPE_CHECKING:
    from wt901.device import WT901Device

__all__ = [
    "Battery",
    "ChipTime",
    "PollerConfig",
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

    def __str__(self) -> str:
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

    @property
    def magnetic_field_type(self) -> int | None:
        """已缓存的磁场量纲类型（寄存器 ``0x72``），未读过时为 ``None``。"""
        return self._mag_type

    async def read_magnetic_field(self) -> MagneticField:
        """读磁场三轴。

        必须先取寄存器 ``0x72`` 的量纲类型——磁场换算系数不是常量，而是按该
        类型分档。类型只读一次并缓存：它描述的是硬件配置，不会在运行中变化。

        量纲类型不在已知分档内时 :attr:`MagneticField.value` 为 ``None``，只有
        ``raw`` 可用。官方 Android SDK 在这种情况下原样返回未换算的计数值，
        那会让调用方拿到一个单位不明却看着正常的数。
        """
        if self._mag_type is None:
            self._mag_type = _u16(await self._device.registers.read_value(Register.MAGTYPE))

        response = await self._device.registers.read(Register.HX)
        raw = response.values[:3]
        converted = [units.magnetic_field_to_ut(self._mag_type, value) for value in raw]

        value: Vec3 | None = None
        if all(component is not None for component in converted):
            value = Vec3(*(component for component in converted if component is not None))
        return MagneticField(value=value, mag_type=self._mag_type, raw=tuple(raw))

    async def read_quaternion(self) -> Quaternion:
        """读姿态四元数。一次读取正好覆盖 Q0–Q3 四个寄存器。"""
        response = await self._device.registers.read(Register.Q0)
        raw = response.values
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
        """
        response = await self._device.registers.read(Register.CHIP_TIME_YEAR_MONTH)
        year_month, day_hour, minute_second, millisecond = (
            _u16(value) for value in response.values
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

    async def read_serial_number(self) -> str:
        """读 12 字节 ASCII 序列号。

        序列号占 6 个寄存器，而一次读取只回 4 个，所以要读两次：``0x7F`` 与
        ``0x82``。非 ASCII 字节以 ``\\uFFFD`` 替换而非抛异常——序列号读花了是
        个诊断线索，不该让整个设备信息读取失败。
        """
        first = await self._device.registers.read(Register.SERIAL_NUMBER)
        second = await self._device.registers.read(Register.SERIAL_NUMBER + 3)
        words = list(first.values[:3]) + list(second.values[: SERIAL_NUMBER_WORDS - 3])

        payload = b"".join(_u16(word).to_bytes(2, "little") for word in words)
        return payload.decode("ascii", errors="replace").rstrip("\x00")

    async def read_version(self) -> str:
        """读固件版本号。

        两个寄存器拼成 uint32。最高位为 1 时是「新版本号」编码，按 17/6/8 位
        拆成 ``主.次.修订``；否则退回直接显示低位寄存器的无符号值。这个分支来自
        官方 C# 实现，两种编码在真实设备上都存在。
        """
        response = await self._device.registers.read(Register.VERSION_LOW)
        low, high = _u16(response.values[0]), _u16(response.values[1])
        packed = low | (high << 16)
        bits = f"{packed:032b}"
        if bits[0] == "1":
            major = int(bits[1:18], 2)
            minor = int(bits[18:24], 2)
            patch = int(bits[24:], 2)
            return f"{major}.{minor}.{patch}"
        return str(low)

    async def read_device_info(self) -> DeviceInfo:
        """一次性读齐设备身份信息。

        逐项容错：任何一项读失败只让该字段为 ``None``，不影响其余项。设备信息
        是诊断用的，「序列号读不到」不该连温度也一起拿不到。

        电量只读一次：``percent`` 与 ``raw`` 来自同一次读取，否则两个字段可能
        取自不同时刻的两次 BLE 往返，白白多花一次链路时间。
        """
        battery = await _attempt(self.read_battery, "电量")
        return DeviceInfo(
            serial_number=await _attempt(self.read_serial_number, "序列号"),
            version=await _attempt(self.read_version, "版本号"),
            temperature_c=await _attempt(self.read_temperature, "温度"),
            battery_percent=battery.percent if battery is not None else None,
            battery_raw=battery.raw if battery is not None else None,
        )


class TelemetryPoller:
    """周期性补充读取。**默认不启动。**

    这些读取与 ``0x61`` 实时数据流抢同一条 BLE 链路。官方 SDK 无条件开一个线程
    每轮都读磁场与四元数，在 100 Hz 采集下会明显挤占带宽。所以这里做成显式开启，
    并且每一项的周期都可以单独关掉。
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
        self.quaternion: Quaternion | None = None
        self.temperature_c: float | None = None
        self.battery: Battery | None = None

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

    async def _poll_temperature(self) -> None:
        self.temperature_c = await self._telemetry.read_temperature()

    async def _poll_battery(self) -> None:
        self.battery = await self._telemetry.read_battery()
