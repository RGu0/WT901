"""6 轴模式下磁场读数可能任意陈旧，调用方要能看出来（RAY-344 scope 1）。

成因已由真机取证确认（两台 WT901BLE67：9 轴下 8/8 不同、6 轴下 1/8 全同，见该
Issue 2026-09-03 的评论），**本 scope 离线可测**——这里测的是「调用方拿到的对象
能不能表达这件事」，不是设备行为本身。
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import register_frame, registers
from wt901.device import WT901Device
from wt901.models import MagneticField
from wt901.protocol.registers import AlgorithmMode, Register
from wt901.telemetry import TelemetryPoller
from wt901.transport.memory import MemoryTransport

NINE = int(AlgorithmMode.NINE_AXIS)
SIX = int(AlgorithmMode.SIX_AXIS)


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    device.registers.save_delay = 0.0
    device.registers.read_timeout = 0.02
    device.registers.read_retries = 0
    await device.open()
    return device, transport


class _Responder:
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


async def _run(transport: MemoryTransport, table: dict[int, tuple[int, ...]], coro):  # type: ignore[no-untyped-def]
    loop = asyncio.get_running_loop()
    serving = loop.create_task(_Responder(transport, table).serve())
    try:
        return await loop.create_task(coro)
    finally:
        serving.cancel()
        try:
            await serving
        except asyncio.CancelledError:
            pass


def _reads_of(transport: MemoryTransport, register: int) -> int:
    return sum(
        1
        for command in transport.writes
        if len(command) == 5 and command[2] == Register.READADDR and command[3] == register
    )


# ----- 判据本身 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("algorithm_mode", "stale"),
    [(NINE, False), (SIX, True), (None, True)],
)
def test_may_be_stale_only_clears_on_nine_axis(
    algorithm_mode: int | None, stale: bool
) -> None:
    """只有确认在 9 轴时才敢说读数是新的。

    ``None``（没取到 ``0x24``）**也算可能陈旧**——本库无法排除，按一贯的规矩
    「拿不准就不声称它是新鲜的」。要区分「确知陈旧」与「不知道」看
    ``algorithm_mode`` 本身。
    """
    field = MagneticField(
        value=None, mag_type=6, raw=(1, 2, 3), algorithm_mode=algorithm_mode
    )
    assert field.may_be_stale is stale


def test_calibrated_unit_and_freshness_are_independent() -> None:
    """**换算得出来不代表值是新的。** 两个信号互相独立，必须分开看。

    这是本 scope 最容易被误用的一点：调用方习惯了用 ``value is not None`` 当作
    「这个读数能用」，而在 6 轴下那个判断完全成立、数值却可能是几小时前的。
    """
    from wt901.models import Vec3

    stale_but_converted = MagneticField(
        value=Vec3(1.0, 2.0, 3.0), mag_type=6, raw=(150, 300, 450), algorithm_mode=SIX
    )
    assert stale_but_converted.is_calibrated_unit is True
    assert stale_but_converted.may_be_stale is True

    fresh_but_unconvertible = MagneticField(
        value=None, mag_type=99, raw=(1, 2, 3), algorithm_mode=NINE
    )
    assert fresh_but_unconvertible.is_calibrated_unit is False
    assert fresh_but_unconvertible.may_be_stale is False


def test_default_algorithm_mode_is_unknown_not_fresh() -> None:
    """不带 ``algorithm_mode`` 构造出来的读数默认是「可能陈旧」，不是「新鲜」。

    这个默认值的方向很要紧：加字段时若默认成 9 轴，所有旧构造点会**悄悄**变成
    「保证新鲜」，而它们其实什么都没保证。
    """
    field = MagneticField(value=None, mag_type=6, raw=(1, 2, 3))
    assert field.algorithm_mode is None
    assert field.may_be_stale is True


# ----- 读取路径 -------------------------------------------------------------


async def test_read_magnetic_field_records_algorithm_mode() -> None:
    device, transport = await _opened()
    table = {
        Register.MAGTYPE: registers(2, 0, 0, 0),
        Register.ALGORITHM: registers(SIX, 0, 0, 0),
        Register.HX: registers(100, 200, 300, 0),
    }
    field = await _run(transport, table, device.telemetry.read_magnetic_field())
    assert field.algorithm_mode == SIX
    assert field.may_be_stale is True
    assert field.is_calibrated_unit is True  # 单位没问题，是数值不新
    await device.close()


async def test_algorithm_register_is_probed_only_once() -> None:
    """``0x24`` 不能每次读磁场都读一遍——那会与 ``0x61`` 实时流抢链路。"""
    device, transport = await _opened()
    table = {
        Register.MAGTYPE: registers(6, 0, 0, 0),
        Register.ALGORITHM: registers(NINE, 0, 0, 0),
        Register.HX: registers(1, 2, 3, 0),
    }
    for _ in range(3):
        await _run(transport, table, device.telemetry.read_magnetic_field())
    assert _reads_of(transport, Register.ALGORITHM) == 1
    assert _reads_of(transport, Register.MAGTYPE) == 1
    assert device.telemetry.algorithm_mode == NINE
    await device.close()


async def test_written_algorithm_mode_wins_over_the_cached_read() -> None:
    """**算法模式是配置，运行中会变——缓存必须让位于写入。**

    ``mag_type`` 是硬件属性、读一次就够；``0x24`` 不是。先读到 6 轴、随后调用方
    切到 9 轴，若仍用缓存，磁场读数会被永久标成「可能陈旧」，而它其实已经新鲜了
    ——一个安静的错误答案。
    """
    device, transport = await _opened()
    table = {
        Register.MAGTYPE: registers(6, 0, 0, 0),
        Register.ALGORITHM: registers(SIX, 0, 0, 0),
        Register.HX: registers(1, 2, 3, 0),
    }
    first = await _run(transport, table, device.telemetry.read_magnetic_field())
    assert first.may_be_stale is True

    await device.registers.set_algorithm(AlgorithmMode.NINE_AXIS)

    second = await _run(transport, table, device.telemetry.read_magnetic_field())
    assert second.algorithm_mode == NINE
    assert second.may_be_stale is False
    # 公开属性必须与判定取同一个值——只读缓存的属性会在这里给出矛盾的答案
    assert device.telemetry.algorithm_mode == NINE
    await device.close()


async def test_unreadable_algorithm_register_does_not_fail_the_read() -> None:
    """``0x24`` 读不到时，磁场读数照常返回，只是新鲜度按未知处理。

    ``0x24`` 不在官方寄存器地址表里（RAY-309），某些固件上可能不应答。为了取不到
    一个标志而让整次磁场读取失败没有道理——何况「未知」正是安全的方向。
    """
    device, transport = await _opened()
    table = {
        Register.MAGTYPE: registers(2, 0, 0, 0),
        Register.HX: registers(100, 200, 300, 0),
    }  # 表里没有 ALGORITHM：不作答 → 超时
    field = await _run(transport, table, device.telemetry.read_magnetic_field())
    assert field.algorithm_mode is None
    assert field.may_be_stale is True
    assert field.raw == (100, 200, 300)
    assert field.is_calibrated_unit is True
    await device.close()


async def test_failed_probe_is_not_retried_every_read() -> None:
    """探测失败也只探一次——否则每次读磁场都要白等一整轮读超时。"""
    device, transport = await _opened()
    table = {
        Register.MAGTYPE: registers(2, 0, 0, 0),
        Register.HX: registers(1, 2, 3, 0),
    }
    for _ in range(3):
        await _run(transport, table, device.telemetry.read_magnetic_field())
    assert _reads_of(transport, Register.ALGORITHM) == 1
    await device.close()


# ----- 导航性文字的守卫 ------------------------------------------------------


def test_poller_docstring_mentions_both_magnetic_judgements() -> None:
    """``TelemetryPoller`` 的类 docstring 必须提到磁场有**两个**独立判据。

    RAY-310 与 RAY-311 都栽在同一件事上：加了字段，导航性的那段没跟着改，于是
    文档里留着一句已经不成立的话。这里钉住的正是那段。
    """
    doc = TelemetryPoller.__doc__
    assert doc is not None
    assert "may_be_stale" in doc
    assert "两个且互相独立" in doc
    assert "换算得出来不代表值是新的" in doc


def test_poller_magnetic_field_attribute_is_documented() -> None:
    """轮询器的 ``magnetic_field`` 属性要自带「先看 may_be_stale」的提示。

    轮询器每个周期都会写进一个**新对象**，很容易被读成「这是新数据」——而在 6 轴
    下对象是新的、里面的数值不是。
    """
    import inspect

    source = inspect.getsource(TelemetryPoller.__init__)
    marker = source.index("self.magnetic_field")
    following = source[marker : marker + 400]
    assert "may_be_stale" in following
    assert "任意陈旧" in following
