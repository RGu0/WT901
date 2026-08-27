"""寄存器整块回读全零时，读取不得交出看着正常的假值。全部离线。

**这不是假想防御。** 全零回读是这个器件有据可查的现象，两次独立证据：
`ray-172/acceptance/`（两台不同主机）与 `ray-279/device-mac/acceptance/`（同一主机
两台设备，`0x7F` 负载 18 字节全为 0），`docs/protocol.md` §10 已收录。

在此之前，本库的防守是「谁踩了谁补」——电量（RAY-182）、MAC（RAY-279）、序列号
（RAY-293）各补过一次，而芯片时间、四元数、版本号仍在交出假值。本文件覆盖剩下这三个。

**形状不统一是有意的**，逐条的理由写在各自的 docstring 里：

* `read_version` **抛异常** —— 版本号只有「显示与比对」一个用途，不可信就毫无用处。
* `ChipTime` / `Quaternion` **附标志** —— 前者要回答的正是「设备是不是刚上电」，抛异常
  会把「时钟未设」这个答案一起抹掉；后者的原始值是判定成因所需。

`read_temperature` **不在本文件范围内**：全零给出 0 °C，而 0 °C 是可能的真实测量。
本库只挡不可能的值，挡它就是发明规则。
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import register_frame, registers
from wt901.device import WT901Device
from wt901.errors import UnexpectedRegisterResponse
from wt901.models import Quaternion
from wt901.protocol.registers import Register
from wt901.telemetry import ChipTime
from wt901.transport.memory import MemoryTransport

ZERO = registers()
"""整帧读作 0——这正是本文件要覆盖的那个真机现象。"""


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.read_timeout = 0.05
    await device.open()
    return device, transport


class Responder:
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


# ----- 芯片时间 --------------------------------------------------------------


async def test_all_zero_chip_time_is_marked_implausible() -> None:
    """月 0 日 0 在日历上不存在，那不是一个时间戳。"""
    device, transport = await _opened()
    chip = await _run(
        device,
        transport,
        {Register.CHIP_TIME_YEAR_MONTH: ZERO},
        device.telemetry.read_chip_time(),
    )

    assert not chip.is_plausible  # type: ignore[attr-defined]
    assert chip.month == 0 and chip.day == 0  # type: ignore[attr-defined]
    await device.close()


async def test_implausible_chip_time_does_not_print_like_a_timestamp() -> None:
    """数据层挡住了、显示层再把歧义造一遍——RAY-293 的 ``__str__`` 正是这么翻的车。"""
    unreadable = ChipTime(
        year=2000, month=0, day=0, hour=0, minute=0, second=0, millisecond=0
    )

    assert "2000-00-00 00:00:00" not in str(unreadable)
    assert "非法" in str(unreadable)


async def test_a_real_chip_time_is_plausible_and_prints_normally() -> None:
    device, transport = await _opened()
    table = {
        Register.CHIP_TIME_YEAR_MONTH: registers(
            26 | (8 << 8), 18 | (13 << 8), 45 | (7 << 8), 123
        )
    }
    chip = await _run(device, transport, table, device.telemetry.read_chip_time())

    assert chip.is_plausible  # type: ignore[attr-defined]
    assert str(chip) == "2026-08-18 13:45:07.123"
    await device.close()


def test_chip_time_only_rejects_values_that_cannot_exist() -> None:
    """年/时/分/秒全零都是可能的真实取值（2000 年、零点整），不该被挡。

    只有月和日有绝对不可能的取值。挡多了就是发明规则——与 ``read_temperature``
    不进这个 Issue 是同一条线。
    """
    midnight_2000 = ChipTime(
        year=2000, month=1, day=1, hour=0, minute=0, second=0, millisecond=0
    )

    assert midnight_2000.is_plausible
    assert str(midnight_2000) == "2000-01-01 00:00:00.000"


# ----- 四元数 ----------------------------------------------------------------


async def test_all_zero_quaternion_is_marked_implausible() -> None:
    """模为 0 不表示任何朝向——单位四元数的模恒为 1。"""
    device, transport = await _opened()
    quat = await _run(
        device, transport, {Register.Q0: ZERO}, device.telemetry.read_quaternion()
    )

    assert not quat.is_plausible  # type: ignore[attr-defined]
    assert quat.raw == (0, 0, 0, 0)  # type: ignore[attr-defined]
    await device.close()


async def test_a_real_quaternion_is_plausible() -> None:
    device, transport = await _opened()
    quat = await _run(
        device,
        transport,
        {Register.Q0: registers(32767)},
        device.telemetry.read_quaternion(),
    )

    assert quat.is_plausible  # type: ignore[attr-defined]
    assert quat.w == pytest.approx(1.0, rel=1e-4)  # type: ignore[attr-defined]
    await device.close()


def test_quaternion_judges_by_raw_not_by_a_norm_tolerance() -> None:
    """判据只看四个原始值是否全零，不检查模是否接近 1。

    器件给的是定点数，正常读数的模也只是「接近」1；划一条容差线就是发明一个没
    实测过的规则。一个模明显不为 1 但非零的读数仍算「读到了」——它可能是真实的
    异常数据，那是调用方要看的线索，不是本库该吞掉的东西。
    """
    odd_but_read = Quaternion(w=0.5, x=0.0, y=0.0, z=0.0, raw=(16384, 0, 0, 0))

    assert odd_but_read.is_plausible


# ----- 版本号 ----------------------------------------------------------------


async def test_all_zero_version_is_refused_not_reported_as_zero() -> None:
    """此前走遗留分支返回 ``"0"``——一个看着像版本号的假值。"""
    device, transport = await _opened()
    with pytest.raises(UnexpectedRegisterResponse, match="版本号"):
        await _run(
            device,
            transport,
            {Register.VERSION_LOW: ZERO},
            device.telemetry.read_version(),
        )
    await device.close()


async def test_device_info_version_is_none_when_registers_read_zero() -> None:
    """抛异常经 ``_attempt`` 落成 ``None``——这正是选择抛异常的好处之一。"""
    device, transport = await _opened()
    info = await _run(
        device,
        transport,
        {Register.TEMPERATURE: registers(3184)},
        device.telemetry.read_device_info(),
    )

    assert info.version is None  # type: ignore[attr-defined]
    assert info.temperature_c == pytest.approx(31.84)  # type: ignore[attr-defined]
    await device.close()


async def test_a_real_version_still_reads() -> None:
    """真机记录过的版本号 10080.1.22（ray-172/acceptance/）。"""
    device, transport = await _opened()
    version = await _run(
        device,
        transport,
        {Register.VERSION_LOW: registers(0x0116, -0x7628)},  # 0x89D8 作 int16
        device.telemetry.read_version(),
    )

    assert version == "10080.1.22"
    await device.close()


# ----- 轮询路径 --------------------------------------------------------------


async def test_poller_writes_the_value_with_its_plausibility_flag() -> None:
    """四元数是**默认被轮询**的那一项，所以这条路径必须一起管住。

    轮询器照常写入，可信与否随值一起走——与 ``poller.battery`` 今天的行为一致。
    不做「保持上一次的有效值」：那会让一个陈旧的值冒充当前状态，比给出一个自带
    「不可信」标志的新值更难查。
    """
    from wt901.telemetry import PollerConfig, TelemetryPoller

    device, transport = await _opened()
    poller = TelemetryPoller(
        device.telemetry,
        PollerConfig(
            magnetic_field=None, quaternion=0.01, temperature=None, battery=None
        ),
    )
    poller.start()
    await _run(device, transport, {Register.Q0: ZERO}, asyncio.sleep(0.15))
    await poller.stop()

    assert poller.quaternion is not None
    assert not poller.quaternion.is_plausible
    await device.close()
