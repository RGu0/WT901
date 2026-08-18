"""按需读取测试。全部离线：用 MemoryTransport 喂寄存器回帧。"""

from __future__ import annotations

import asyncio
import struct

import pytest

from wt901.device import WT901Device
from wt901.errors import TransportTimeoutError
from wt901.protocol.frames import FRAME_LENGTH, HEADER, FrameFlag
from wt901.protocol.registers import Register
from wt901.telemetry import PollerConfig, TelemetryPoller
from wt901.transport.memory import MemoryTransport


def register_frame(start: int, values: tuple[int, ...]) -> bytes:
    body = (
        bytes([HEADER, FrameFlag.REGISTER])
        + struct.pack("<H", start)
        + struct.pack("<4h", *values)
    )
    return body.ljust(FRAME_LENGTH, b"\x00")


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    device.registers.read_timeout = 0.05
    await device.open()
    return device, transport


class Responder:
    """一台会应答的假设备：盯着已发出的读指令，按地址表回帧。

    做成**持续运行的后台任务**而不是跑固定轮数：`read_device_info` 里故意不作答
    的那一项会真实消耗 0.15 秒（3 次重试 × 50 ms 超时），固定轮数的应答器早就
    停了，导致后续读取也拿不到答复——那测出来的失败是测试替身的，不是被测代码的。

    地址表里没有的寄存器一律不作答，用来构造超时场景。
    """

    def __init__(self, transport: MemoryTransport, table: dict[int, tuple[int, ...]]):
        self._transport = transport
        self._table = table
        self._answered = 0

    async def serve(self) -> None:
        while True:
            for command in self._transport.writes[self._answered :]:
                self._answered += 1
                if len(command) == 5 and command[2] == Register.READADDR:
                    values = self._table.get(command[3])
                    if values is not None:
                        self._transport.feed(register_frame(command[3], values))
            await asyncio.sleep(0.001)


async def _run(
    device: WT901Device,
    transport: MemoryTransport,
    table: dict[int, tuple[int, ...]],
    coro: object,
) -> object:
    """并发跑一个读取协程与一个持续应答的假设备。"""
    loop = asyncio.get_running_loop()
    serving = loop.create_task(Responder(transport, table).serve())
    try:
        return await loop.create_task(coro)  # type: ignore[arg-type]
    finally:
        serving.cancel()
        try:
            await serving
        except asyncio.CancelledError:
            pass


# ----- 磁场 ----------------------------------------------------------------


async def test_magnetic_field_reads_type_first_then_converts() -> None:
    """磁场换算系数不是常量，必须先取 0x72 的量纲类型。"""
    device, transport = await _opened()
    table = {
        Register.MAGTYPE: (2, 0, 0, 0),  # type 2 → ×0.15
        Register.HX: (100, 200, 300, 0),
    }
    field = await _run(device, transport, table, device.telemetry.read_magnetic_field())

    assert field.mag_type == 2  # type: ignore[attr-defined]
    assert field.is_calibrated_unit  # type: ignore[attr-defined]
    assert field.value.x == pytest.approx(15.0)  # type: ignore[attr-defined]
    assert field.value.y == pytest.approx(30.0)  # type: ignore[attr-defined]
    assert field.raw == (100, 200, 300)  # type: ignore[attr-defined]
    await device.close()


async def test_magnetic_field_type_is_cached() -> None:
    """量纲类型描述硬件配置，不会在运行中变化，没必要每次都读。"""
    device, transport = await _opened()
    table = {Register.MAGTYPE: (6, 0, 0, 0), Register.HX: (150, 0, 0, 0)}
    await _run(device, transport, table, device.telemetry.read_magnetic_field())
    transport.writes.clear()
    await _run(device, transport, table, device.telemetry.read_magnetic_field())

    magtype_reads = [
        w for w in transport.writes if len(w) == 5 and w[3] == Register.MAGTYPE
    ]
    assert magtype_reads == []
    assert device.telemetry.magnetic_field_type == 6
    await device.close()


async def test_unknown_magnetic_type_yields_no_unit() -> None:
    """未知量纲不猜系数：返回值明确标注单位不可用，只有 raw 能看。"""
    device, transport = await _opened()
    table = {Register.MAGTYPE: (99, 0, 0, 0), Register.HX: (100, 200, 300, 0)}
    field = await _run(device, transport, table, device.telemetry.read_magnetic_field())

    assert field.value is None  # type: ignore[attr-defined]
    assert not field.is_calibrated_unit  # type: ignore[attr-defined]
    assert field.raw == (100, 200, 300)  # type: ignore[attr-defined]
    await device.close()


# ----- 四元数 / 温度 / 电量 ------------------------------------------------


async def test_quaternion() -> None:
    device, transport = await _opened()
    table = {Register.Q0: (32767, -32768, 16384, 0)}
    quat = await _run(device, transport, table, device.telemetry.read_quaternion())

    assert quat.w == pytest.approx(1.0, rel=1e-4)  # type: ignore[attr-defined]
    assert quat.x == -1.0  # type: ignore[attr-defined]
    assert quat.y == pytest.approx(0.5)  # type: ignore[attr-defined]
    await device.close()


async def test_temperature() -> None:
    device, transport = await _opened()
    table = {Register.TEMPERATURE: (3184, 0, 0, 0)}
    value = await _run(device, transport, table, device.telemetry.read_temperature())
    assert value == pytest.approx(31.84)
    await device.close()


@pytest.mark.parametrize(("raw", "percent"), [(400, 100), (380, 50), (339, 0)])
async def test_battery_returns_raw_and_percent(raw: int, percent: int) -> None:
    """阶梯很粗（350–367 全报 10%），需要更细判断时只能看原始值。"""
    device, transport = await _opened()
    table = {Register.POWER: (raw, 0, 0, 0)}
    battery = await _run(device, transport, table, device.telemetry.read_battery())
    assert battery.raw == raw  # type: ignore[attr-defined]
    assert battery.percent == percent  # type: ignore[attr-defined]
    await device.close()


# ----- 芯片时间 ------------------------------------------------------------


async def test_chip_time_splits_high_and_low_bytes() -> None:
    """前三个寄存器各自的高低字节承载两个字段，不能按一寄存器一字段读。"""
    device, transport = await _opened()
    # 2026-08-18 13:45:07.123
    year_month = 26 | (8 << 8)
    day_hour = 18 | (13 << 8)
    minute_second = 45 | (7 << 8)
    table = {
        Register.CHIP_TIME_YEAR_MONTH: (year_month, day_hour, minute_second, 123)
    }
    chip = await _run(device, transport, table, device.telemetry.read_chip_time())

    assert (chip.year, chip.month, chip.day) == (2026, 8, 18)  # type: ignore[attr-defined]
    assert (chip.hour, chip.minute, chip.second) == (13, 45, 7)  # type: ignore[attr-defined]
    assert chip.millisecond == 123  # type: ignore[attr-defined]
    assert str(chip) == "2026-08-18 13:45:07.123"
    await device.close()


async def test_chip_time_handles_high_bit_bytes() -> None:
    """月份等字段落在高字节时，寄存器作为 int16 会是负数——不能按有符号解。"""
    device, transport = await _opened()
    year_month = 30 | (12 << 8)
    day_hour = 31 | (23 << 8)
    minute_second = 59 | (59 << 8)
    table = {
        Register.CHIP_TIME_YEAR_MONTH: (
            year_month - 0x10000 if year_month > 0x7FFF else year_month,
            day_hour,
            minute_second,
            999,
        )
    }
    chip = await _run(device, transport, table, device.telemetry.read_chip_time())
    assert (chip.year, chip.month, chip.day) == (2030, 12, 31)  # type: ignore[attr-defined]
    assert (chip.hour, chip.minute, chip.second) == (23, 59, 59)  # type: ignore[attr-defined]
    await device.close()


# ----- 序列号 / 版本号 -----------------------------------------------------


async def test_serial_number_spans_two_reads() -> None:
    """序列号占 6 个寄存器，而一次读只回 4 个，所以必须读两次。"""
    device, transport = await _opened()
    text = b"WT9011DCL123"
    words = [int.from_bytes(text[i : i + 2], "little") for i in range(0, 12, 2)]
    signed = [w - 0x10000 if w > 0x7FFF else w for w in words]
    table = {
        Register.SERIAL_NUMBER: (signed[0], signed[1], signed[2], 0),
        Register.SERIAL_NUMBER + 3: (signed[3], signed[4], signed[5], 0),
    }
    serial = await _run(device, transport, table, device.telemetry.read_serial_number())
    assert serial == "WT9011DCL123"
    await device.close()


async def test_serial_number_tolerates_garbage() -> None:
    """读花了是诊断线索，不该让整个设备信息读取失败。"""
    device, transport = await _opened()
    table = {
        Register.SERIAL_NUMBER: (-1, -1, -1, 0),
        Register.SERIAL_NUMBER + 3: (-1, -1, -1, 0),
    }
    serial = await _run(device, transport, table, device.telemetry.read_serial_number())
    assert isinstance(serial, str)
    await device.close()


async def test_version_new_encoding() -> None:
    """最高位为 1 时按 17/6/8 位拆成 主.次.修订。"""
    device, transport = await _opened()
    packed = (1 << 31) | (5 << 14) | (3 << 8) | 7
    low, high = packed & 0xFFFF, packed >> 16
    signed = [v - 0x10000 if v > 0x7FFF else v for v in (low, high)]
    table = {Register.VERSION_LOW: (signed[0], signed[1], 0, 0)}
    version = await _run(device, transport, table, device.telemetry.read_version())
    assert version == "5.3.7"
    await device.close()


async def test_version_old_encoding_falls_back_to_raw() -> None:
    """最高位为 0 时退回显示低位寄存器的无符号值。两种编码在真实设备上都存在。"""
    device, transport = await _opened()
    table = {Register.VERSION_LOW: (1234, 0, 0, 0)}
    version = await _run(device, transport, table, device.telemetry.read_version())
    assert version == "1234"
    await device.close()


# ----- 设备信息聚合 --------------------------------------------------------


async def test_device_info_is_resilient_per_field() -> None:
    """「序列号读不到」不该连温度也一起拿不到。"""
    device, transport = await _opened()
    table = {
        Register.TEMPERATURE: (3000, 0, 0, 0),
        Register.POWER: (396, 0, 0, 0),
        Register.VERSION_LOW: (1234, 0, 0, 0),
        # 序列号故意不作答 → 读超时
    }
    info = await _run(device, transport, table, device.telemetry.read_device_info())

    assert info.temperature_c == pytest.approx(30.0)  # type: ignore[attr-defined]
    assert info.battery_percent == 100  # type: ignore[attr-defined]
    assert info.battery_raw == 396  # type: ignore[attr-defined]
    assert info.version == "1234"  # type: ignore[attr-defined]
    assert info.serial_number is None  # type: ignore[attr-defined]
    await device.close()


async def test_device_info_reads_battery_only_once() -> None:
    """percent 与 raw 来自同一次读取，否则白白多花一次 BLE 往返。"""
    device, transport = await _opened()
    table = {
        Register.TEMPERATURE: (3000, 0, 0, 0),
        Register.POWER: (396, 0, 0, 0),
        Register.VERSION_LOW: (1, 0, 0, 0),
    }
    await _run(device, transport, table, device.telemetry.read_device_info())
    power_reads = [
        w for w in transport.writes if len(w) == 5 and w[3] == Register.POWER
    ]
    assert len(power_reads) == 1
    await device.close()


# ----- 周期轮询 ------------------------------------------------------------


async def test_poller_is_not_started_by_default() -> None:
    """周期读取与实时数据流抢同一条链路，必须显式开启。"""
    device, _ = await _opened()
    poller = TelemetryPoller(device.telemetry)
    assert not poller.is_running
    await device.close()


async def test_poller_can_disable_individual_reads() -> None:
    device, transport = await _opened()
    poller = TelemetryPoller(
        device.telemetry,
        PollerConfig(
            magnetic_field=None, quaternion=0.01, temperature=None, battery=None
        ),
    )
    poller.start()
    assert poller.is_running

    serving = asyncio.get_running_loop().create_task(
        Responder(transport, {Register.Q0: (16384, 0, 0, 0)}).serve()
    )
    await asyncio.sleep(0.1)
    serving.cancel()

    assert poller.quaternion is not None
    assert poller.magnetic_field is None
    await poller.stop()
    assert not poller.is_running
    await device.close()


async def test_poller_survives_read_failures() -> None:
    """链路瞬时拥塞很常见，一次读失败不该终止轮询。"""
    device, _ = await _opened()
    poller = TelemetryPoller(
        device.telemetry,
        PollerConfig(
            magnetic_field=None, quaternion=0.01, temperature=None, battery=None
        ),
    )
    poller.start()
    await asyncio.sleep(0.2)  # 无人应答 → 每轮都超时
    assert poller.is_running
    await poller.stop()
    await device.close()


async def test_read_timeout_propagates_when_device_silent() -> None:
    device, _ = await _opened()
    with pytest.raises(TransportTimeoutError):
        await device.telemetry.read_temperature()
    await device.close()
