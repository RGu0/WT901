"""连接期链路信号强度（RAY-310）。

本库此前只有扫描期的 RSSI。连上之后这个量就没了，而它是唯一一个**原因侧**的
链路指标——`resync_count`、`dropped_samples` 都要等问题发生之后才动。

这些测试同时钉住两件从 bleak 0.22.3 源码读出来的事实，它们决定了实现的形状：

1. `get_rssi()` **只在 CoreBluetooth 后端的实现类上**，不在 `BleakClient` 的公开
   门面上，`BaseBleakClient` 也没声明它。所以要穿 `client._backend`，而且穿不到
   就得退化成 `None`。
2. bleak 的 `read_rssi()` 每个外设只存一个 future，**并发两次会让第一次永久挂起**
   （与 RAY-177 同一类失败），而且它自己没有超时。所以本库必须自带锁与超时。
"""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeClient
from wt901.device import WT901Device
from wt901.protocol.frames import HEADER, FrameFlag
from wt901.telemetry import PollerConfig, TelemetryPoller
from wt901.transport.ble import BleTransport
from wt901.transport.memory import MemoryTransport

MOTIONLESS = (0, 0, 2048, 0, 0, 0, 0, 0, 0)


def data_frame() -> bytes:
    return bytes([HEADER, FrameFlag.DATA]) + struct.pack("<9h", *MOTIONLESS)


class _Backend:
    """bleak 的后端客户端。只有 CoreBluetooth 那个有 `get_rssi`。"""

    def __init__(self, get_rssi: Any = None) -> None:
        self.calls = 0
        if get_rssi is not None:
            self.get_rssi = get_rssi  # type: ignore[method-assign]


class _BackendClient(FakeClient):
    """带 `_backend` 私有属性的假客户端，形状照 bleak 的 `BleakClient`。"""

    def __init__(self, *args: Any, backend: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if backend is not None:
            self._backend = backend


def _transport(backend: Any = None, **kwargs: Any) -> BleTransport:
    def factory(address: str, timeout: float, on_disconnect: Any) -> FakeClient:
        return _BackendClient(address, timeout, on_disconnect, backend=backend)

    return BleTransport("AA:BB:CC:DD:EE:FF", client_factory=factory, **kwargs)


# ----- 传输层的默认约定 -----------------------------------------------------


async def test_transports_without_a_radio_report_none() -> None:
    """基类默认 `None`：内存传输与回放没有链路可测，不该被迫各写一遍。"""
    assert await MemoryTransport("mem").read_rssi() is None


async def test_none_means_unknown_not_zero() -> None:
    """0 dBm 是极强信号。把「读不到」写成 0 会让链路看起来比实际好得多。"""
    value = await MemoryTransport("mem").read_rssi()
    assert value is None
    assert value != 0


# ----- BleTransport：拿得到的情况 -------------------------------------------


async def test_reads_rssi_through_the_corebluetooth_backend() -> None:
    async def get_rssi() -> int:
        return -57

    transport = _transport(backend=_Backend(get_rssi))
    await transport.connect()
    assert await transport.read_rssi() == -57
    await transport.disconnect()


# ----- BleTransport：拿不到的四种情况，全部落成 None ------------------------


async def test_none_when_backend_has_no_get_rssi() -> None:
    """BlueZ / WinRT / p4android 三个后端都没有这个方法。"""
    transport = _transport(backend=_Backend())
    await transport.connect()
    assert await transport.read_rssi() is None
    await transport.disconnect()


async def test_none_when_there_is_no_backend_attribute() -> None:
    """`_backend` 是 bleak 的私有属性。它哪天没了，本方法退化而不是抛。"""
    transport = _transport(backend=None)
    await transport.connect()
    assert await transport.read_rssi() is None
    await transport.disconnect()


async def test_none_when_not_connected() -> None:
    async def get_rssi() -> int:  # pragma: no cover - 不该被调用
        raise AssertionError("未连接时不该去读")

    transport = _transport(backend=_Backend(get_rssi))
    assert await transport.read_rssi() is None


async def test_none_when_the_backend_raises() -> None:
    async def get_rssi() -> int:
        raise RuntimeError("CoreBluetooth 说不行")

    transport = _transport(backend=_Backend(get_rssi))
    await transport.connect()
    assert await transport.read_rssi() is None
    await transport.disconnect()


async def test_none_on_timeout_and_it_actually_returns() -> None:
    """bleak 那个 await 没有超时；系统不回调它就一直等。"""

    async def get_rssi() -> int:
        await asyncio.sleep(3600)
        raise AssertionError("不可达")

    transport = _transport(backend=_Backend(get_rssi), rssi_timeout=0.01)
    await transport.connect()
    result = await asyncio.wait_for(transport.read_rssi(), 1.0)
    assert result is None
    await transport.disconnect()


# ----- 并发：本库自己加的锁 -------------------------------------------------


async def test_concurrent_reads_are_serialized() -> None:
    """bleak 的 `_read_rssi_futures` 每个外设只有一个槽。

    并发调两次，第二次覆盖第一次的 future，第一次那个 `await` 永远等不到结果
    ——与 RAY-177 同一类失败，只是发生在 bleak 里。这里用一个「重入就报错」的
    假后端把那个前提直接钉住：本库必须保证它不会被重入。
    """
    in_flight = 0
    peak = 0

    async def get_rssi() -> int:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)
            return -60
        finally:
            in_flight -= 1

    transport = _transport(backend=_Backend(get_rssi))
    await transport.connect()
    results = await asyncio.gather(*(transport.read_rssi() for _ in range(5)))

    assert results == [-60] * 5
    assert peak == 1, "同一台设备上的 RSSI 读必须串行，bleak 那边不能重入"
    await transport.disconnect()


# ----- 设备层与遥测层的透传 -------------------------------------------------


async def test_device_and_telemetry_pass_through() -> None:
    async def get_rssi() -> int:
        return -42

    transport = _transport(backend=_Backend(get_rssi))
    device = WT901Device(transport)
    await device.open()

    assert await device.read_rssi() == -42
    assert await device.telemetry.read_rssi() == -42
    await device.close()


async def test_memory_backed_device_reports_none() -> None:
    device = WT901Device(MemoryTransport("mem"))
    await device.open()
    assert await device.read_rssi() is None
    await device.close()


# ----- TelemetryPoller ------------------------------------------------------


class _RssiTransport(MemoryTransport):
    """按脚本返回 RSSI 的内存传输。"""

    def __init__(self, script: list[int | None]) -> None:
        super().__init__("scripted")
        self.script = script
        self.reads = 0

    async def read_rssi(self) -> int | None:
        value = self.script[min(self.reads, len(self.script) - 1)]
        self.reads += 1
        return value


async def _poller(transport: MemoryTransport, **config: Any) -> tuple[
    TelemetryPoller, WT901Device
]:
    device = WT901Device(transport)
    await device.open()
    poller = TelemetryPoller(
        device.telemetry,
        PollerConfig(
            magnetic_field=None,
            quaternion=None,
            temperature=None,
            battery=None,
            **config,
        ),
    )
    return poller, device


async def test_poller_writes_rssi() -> None:
    transport = _RssiTransport([-55])
    poller, device = await _poller(transport, rssi=0.01)
    poller.start()
    await asyncio.sleep(0.05)

    assert poller.rssi == -55
    await poller.stop()
    await device.close()


async def test_poller_does_not_keep_a_stale_value() -> None:
    """陈旧的 RSSI 比没有 RSSI 更危险——它正是用来判断此刻链路好不好的。"""
    transport = _RssiTransport([-55, None])
    poller, device = await _poller(transport, rssi=0.01)
    poller.start()
    await asyncio.sleep(0.08)

    assert transport.reads >= 2
    assert poller.rssi is None
    await poller.stop()
    await device.close()


async def test_poller_starts_with_none() -> None:
    transport = _RssiTransport([-55])
    poller, device = await _poller(transport, rssi=0.01)
    assert poller.rssi is None
    await device.close()


async def test_rssi_can_be_turned_off() -> None:
    transport = _RssiTransport([-55])
    poller, device = await _poller(transport, rssi=None)
    poller.start()
    await asyncio.sleep(0.05)

    assert not poller.is_running
    assert transport.reads == 0
    await poller.stop()
    await device.close()


async def test_rssi_polling_does_not_disturb_the_sample_stream() -> None:
    """读 RSSI 走链路层，不占 0x61/0x71 通道——采样流不该少一个样本。"""
    transport = _RssiTransport([-55])
    poller, device = await _poller(transport, rssi=0.001)
    poller.start()

    for _ in range(20):
        transport.feed(data_frame())
        await asyncio.sleep(0)

    await asyncio.sleep(0.02)
    assert device.stats.samples == 20
    assert device.stats.dropped_samples == 0
    assert transport.reads > 0
    await poller.stop()
    await device.close()


# ----- 平台限制必须被写下来 -------------------------------------------------


def test_docstring_records_the_platform_limit() -> None:
    """「只有 macOS 有」这件事不写下来，下一个人会以为是 bug。

    钉的是否定式说法，不是具体句子——照 `test_bandwidth_full_range` 的先例。
    """
    doc = BleTransport.read_rssi.__doc__
    assert doc is not None
    assert "CoreBluetooth" in doc
    assert "_backend" in doc
    # 不得声称跨平台一致：必须点名拿不到的那几个后端。
    assert "BlueZ" in doc and "WinRT" in doc


def test_docstring_records_the_concurrency_hazard() -> None:
    """并发会永久挂起这件事只有读 bleak 源码才知道，必须留在代码里。"""
    doc = BleTransport.read_rssi.__doc__
    assert doc is not None
    assert "RAY-177" in doc
    assert "future" in doc


def test_poller_docstring_covers_the_fifth_item() -> None:
    """类 docstring 曾写「四项」且「读取失败时属性保持不变」——对 rssi 两句都不成立。

    钉住这两句不回退：漏更新它，读的人会以为 `rssi` 也保持上一次的值，而那正是
    这一项刻意不做的事。
    """
    doc = TelemetryPoller.__doc__
    assert doc is not None
    assert "四项里每一项" not in doc
    assert "rssi" in doc
    # 必须写明 rssi 的失败处置与其余四项不同。
    assert "被写成" in doc


# ----- 取证脚本的设备选择 ---------------------------------------------------


class _Found:
    def __init__(self, address: str, rssi: int | None) -> None:
        self.address = address
        self.rssi = rssi
        self.name = "WT901BLE68"


async def test_probe_picks_the_strongest_device_not_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """台架上那台几乎总是最近的一台，而扫描结果的顺序不保证任何东西。

    此前直接取 `devices[0]`——两台设备在场时选中哪台全凭运气，而真机上第一次跑
    就撞上了连接超时，回溯里完全看不出扫到了什么。
    """
    import tools.probe_rssi as probe

    async def fake_scan(timeout: float) -> list[_Found]:
        assert timeout == probe.SCAN_TIMEOUT
        return [_Found("far", -90), _Found("near", -47)]

    monkeypatch.setattr(probe, "scan", fake_scan)
    chosen = await probe.pick_device(None)
    assert chosen is not None
    assert chosen.address == "near"


async def test_probe_returns_none_when_nothing_is_in_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.probe_rssi as probe

    async def fake_scan(timeout: float) -> list[_Found]:
        return []

    monkeypatch.setattr(probe, "scan", fake_scan)
    assert await probe.pick_device(None) is None


def test_probe_scan_timeout_matches_the_other_probes() -> None:
    """默认 5 秒在设备刚上电或信号偏弱时常扫不到；tools/ 里其它探测脚本用 15。"""
    import tools.probe_rssi as probe

    assert probe.SCAN_TIMEOUT == 15.0


def test_probe_take_address() -> None:
    from tools.probe_rssi import take_address

    assert take_address(["60", "--address", "ABC"]) == ("ABC", ["60"])
    assert take_address(["60"]) == (None, ["60"])


# ---------------------------------------------------------------------------
# RAY-310 scope 2 `rssi-platform-verdict` 的真机取证（2026-09-01）。
#
# 数值逐条抄自
# ``ray-310/rssi-platform-verdict/acceptance/probe_rssi-console-2026-09-01.txt``。
# 钉住它的理由与 RAY-304 那次相同：**已经付过代价的真机数据不该被改动后的判读再误
# 判一次**。判据本身预注册于取证之前（脚本模块 docstring 与 RAY-310 的 Linear
# 评论），这些测试守的是判据，不是实现。
# ---------------------------------------------------------------------------

_HARDWARE_2026_09_01 = (
    -47, -47, -47, -47, -46, -45, -45, -45, -45, -45,
    -45, -45, -46, -45, -45, -45, -45, -45, -45, -46,
    -46, -45, -46, -45, -45, -48, -52, -54, -54, -60,
    -57, -54, -57, -59, -63, -66, -66, -67, -62, -65,
    -70, -71, -62, -63, -63, -61, -58, -62, -63, -67,
    -67, -69, -65, -61, -56, -56, -50, -43, -40, -33,
)


def test_probe_criterion_1_thresholds_match_the_preregistration() -> None:
    """预注册原文：「60 次里 ≥ 55 次成功、值落在 −100..0 dBm」。

    此前次数门槛写的是 ``int(N * 0.9)``，N=60 时得 **54** —— 比预注册松一次。差一次
    只在边界上出现，但一出现就是打印的判读与预注册判据相反。
    """
    import tools.probe_rssi as probe

    assert probe.threshold(60) == 55
    assert probe.CRITERION_1_RANGE == (-100, 0)


def test_probe_judges_the_2026_09_01_hardware_run_as_criterion_1() -> None:
    """那一轮 60 次全部读到、全距 −71..−33 dBm，两个条件都满足。"""
    import tools.probe_rssi as probe

    readings = list(_HARDWARE_2026_09_01)
    assert len(readings) == 60
    assert len([v for v in readings if v is not None]) == 60
    assert min(readings) == -71 and max(readings) == -33
    assert probe.judge(readings).startswith("判据 1")


def test_probe_does_not_call_54_of_60_criterion_1() -> None:
    """54/60 恰好是旧门槛放行、预注册判据不放行的那一点。"""
    import tools.probe_rssi as probe

    readings: list[int | None] = [-50] * 54 + [None] * 6
    assert len([v for v in readings if v is not None]) == 54
    assert probe.judge(readings).startswith("判据 3")
    assert probe.judge([-50] * 55 + [None] * 5).startswith("判据 1")


def test_probe_out_of_range_value_blocks_criterion_1() -> None:
    """判据 1 的两个条件缺一不可。

    此前判定只看次数，第二个条件只被单独打印一行 —— 读到越界值时仍会打印
    「判据 1 成立」。越界不属于判据 3：判据 3 说的是「读不到」，不是「读到了但不
    可信」。
    """
    import tools.probe_rssi as probe

    verdict = probe.judge([-50] * 59 + [7])
    assert verdict.startswith("不判读")
    assert not verdict.startswith("判据")


def test_probe_all_none_is_criterion_2() -> None:
    import tools.probe_rssi as probe

    assert probe.judge([None] * 60).startswith("判据 2")
