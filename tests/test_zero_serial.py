"""序列号回读全零：「读到了但是空」必须与「没读到」分得开。全部离线。

**这不是假想防御。** 真机上读到过逐字节全零的序列号：RAY-172 记过一次（两台不同
主机），RAY-279 的取证里两台设备再次全零（`ray-279/device-mac/acceptance/` 的
`0x7F` 负载 18 字节全为 0）。此前 `read_serial_number` 会把它变成空字符串 `""`，
在 `DeviceInfo` 里与「这一项没读到」长得一模一样——而下游拿 `""` 当绑定键会让**每台
设备的键都相同**，两台设备安静地互相冒充。

形状对齐 `Battery`（RAY-182 的 `raw = 0`）而不是 `read_mac`（RAY-279 的全零抛异常），
理由写在 `SerialNumber` 与 `read_serial_number` 的文档里：MAC 只有身份一个用途，
不可信的 MAC 毫无用处；序列号是设备信息，「它读出来是全零」本身就是诊断线索。
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from wt901.device import WT901Device
from wt901.protocol.frames import FRAME_LENGTH, HEADER, FrameFlag
from wt901.protocol.registers import Register
from wt901.telemetry import SerialNumber
from wt901.transport.memory import MemoryTransport

ZERO_WORDS = (0, 0, 0, 0)
"""真机回读：`0x7F` 与 `0x82` 两次都是全零。"""


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
    device.registers.save_delay = 0.0
    device.registers.read_timeout = 0.05
    await device.open()
    return device, transport


class Responder:
    """持续应答的假设备。地址表里没有的一律不作答，用来构造超时场景。"""

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


def _text_table(text: bytes) -> dict[int, tuple[int, ...]]:
    words = [int.from_bytes(text[i : i + 2], "little") for i in range(0, 12, 2)]
    signed = [w - 0x10000 if w > 0x7FFF else w for w in words]
    return {
        Register.SERIAL_NUMBER: (signed[0], signed[1], signed[2], 0),
        Register.SERIAL_NUMBER + 3: (signed[3], signed[4], signed[5], 0),
    }


# ----- 验收标准 1：全零不再变成空字符串 --------------------------------------


async def test_all_zero_yields_no_value_but_keeps_raw() -> None:
    """12 个字节全是 0 不是一个空序列号，是一次读不出内容的读取。"""
    device, transport = await _opened()
    table = {Register.SERIAL_NUMBER: ZERO_WORDS, Register.SERIAL_NUMBER + 3: ZERO_WORDS}
    serial = await _run(device, transport, table, device.telemetry.read_serial_number())

    assert serial.value is None  # type: ignore[attr-defined]
    assert serial.raw == b"\x00" * 12  # type: ignore[attr-defined]
    assert not serial.is_plausible  # type: ignore[attr-defined]
    await device.close()


async def test_real_serial_is_plausible() -> None:
    device, transport = await _opened()
    serial = await _run(
        device,
        transport,
        _text_table(b"WT9011DCL123"),
        device.telemetry.read_serial_number(),
    )

    assert serial.value == "WT9011DCL123"  # type: ignore[attr-defined]
    assert serial.is_plausible  # type: ignore[attr-defined]
    await device.close()


async def test_a_single_non_zero_byte_is_enough_to_count_as_content() -> None:
    """判据是「有没有非零字节」，不是「像不像一个序列号」。

    本库不发明「什么才算像样的序列号」这种规则——没实测到的不算数。部分写入的
    序列号是真实存在的可能，把它判成「没读出内容」会丢掉一条真线索。
    """
    device, transport = await _opened()
    serial = await _run(
        device,
        transport,
        _text_table(b"A" + b"\x00" * 11),
        device.telemetry.read_serial_number(),
    )

    assert serial.value == "A"  # type: ignore[attr-defined]
    assert serial.is_plausible  # type: ignore[attr-defined]
    await device.close()


# ----- 验收标准 2：非 ASCII 的替换行为不变 ------------------------------------


async def test_non_ascii_is_still_replaced_not_refused() -> None:
    """掺乱码与全零是两回事：前者读到了东西，后者根本没读出内容。"""
    device, transport = await _opened()
    table = {
        Register.SERIAL_NUMBER: (-1, -1, -1, 0),
        Register.SERIAL_NUMBER + 3: (-1, -1, -1, 0),
    }
    serial = await _run(device, transport, table, device.telemetry.read_serial_number())

    assert serial.value is not None  # type: ignore[attr-defined]
    assert "�" in serial.value  # type: ignore[attr-defined]
    assert serial.is_plausible  # type: ignore[attr-defined]
    await device.close()


# ----- 验收标准 1：DeviceInfo 里三态可分 --------------------------------------


async def test_device_info_distinguishes_unread_from_read_but_empty() -> None:
    """这是本 Issue 的全部目的：两种 ``None`` 靠 ``serial_number_raw`` 分开。

    与 ``battery_percent`` / ``battery_raw`` 的关系完全一致（RAY-182）。
    """
    device, transport = await _opened()
    table = {Register.SERIAL_NUMBER: ZERO_WORDS, Register.SERIAL_NUMBER + 3: ZERO_WORDS}
    info = await _run(device, transport, table, device.telemetry.read_device_info())

    assert info.serial_number is None  # type: ignore[attr-defined]
    assert info.serial_number_raw == b"\x00" * 12  # type: ignore[attr-defined]
    await device.close()


async def test_device_info_unread_serial_leaves_both_fields_none() -> None:
    """设备不答时两个字段都是 ``None``——与「读到了但是空」区分开。"""
    device, transport = await _opened()
    info = await _run(
        device,
        transport,
        {Register.TEMPERATURE: (3184, 0, 0, 0)},
        device.telemetry.read_device_info(),
    )

    assert info.serial_number is None  # type: ignore[attr-defined]
    assert info.serial_number_raw is None  # type: ignore[attr-defined]
    assert info.temperature_c == pytest.approx(31.84)  # type: ignore[attr-defined]
    await device.close()


async def test_device_info_carries_a_real_serial() -> None:
    device, transport = await _opened()
    info = await _run(
        device,
        transport,
        _text_table(b"WT9011DCL123"),
        device.telemetry.read_device_info(),
    )

    assert info.serial_number == "WT9011DCL123"  # type: ignore[attr-defined]
    assert info.serial_number_raw == b"WT9011DCL123"  # type: ignore[attr-defined]
    await device.close()


async def test_device_info_reads_the_serial_only_once() -> None:
    """``value`` 与 ``raw`` 必须来自同一次读取，否则白花一次 BLE 往返。

    与电量同一条理由——那条在 ``read_device_info`` 的文档里写着。
    """
    device, transport = await _opened()
    await _run(
        device,
        transport,
        _text_table(b"WT9011DCL123"),
        device.telemetry.read_device_info(),
    )

    reads = [
        w[3]
        for w in transport.writes
        if len(w) == 5 and w[2] == Register.READADDR
    ]
    assert reads.count(Register.SERIAL_NUMBER) == 1
    assert reads.count(Register.SERIAL_NUMBER + 3) == 1
    await device.close()


# ----- 公开契约 --------------------------------------------------------------


def test_serial_number_is_exported_from_the_package_root() -> None:
    import wt901

    assert wt901.SerialNumber is SerialNumber
    assert "SerialNumber" in wt901.__all__


def test_no_str_shortcut_that_would_reintroduce_the_ambiguity() -> None:
    """**刻意不提供** ``__str__``。

    一个「全零时返回空串」的 ``__str__`` 会把本 Issue 要消除的歧义原样搬进显示层：
    ``f"SN: {serial}"`` 又会打出与「字段为空」一模一样的东西。``Battery`` 同样没有。
    默认的 dataclass repr 是诚实的——它把 ``value=None`` 明摆着写出来。
    """
    unreadable = SerialNumber(raw=b"\x00" * 12, value=None)

    assert "__str__" not in SerialNumber.__dict__
    assert str(unreadable) != ""
    assert "value=None" in repr(unreadable)
