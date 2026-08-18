"""设备发现测试。扫描函数可注入，所以全部离线。"""

from __future__ import annotations

import pytest

from fakes import FakeAdvertisement, FakeDevice, FakeScanner
from wt901.discovery import scan
from wt901.errors import TransportError, TransportTimeoutError


def _result(
    address: str, name: str | None, rssi: int | None
) -> tuple[FakeDevice, FakeAdvertisement]:
    return FakeDevice(address=address, name=name), FakeAdvertisement(rssi=rssi)


async def test_filters_to_wit_devices_by_default() -> None:
    scanner = FakeScanner(
        results=[
            _result("addr-1", "WT901BLE68", -50),
            _result("addr-2", "AirPods", -40),
            _result("addr-3", None, -30),
        ]
    )
    devices = await scan(discover=scanner)
    assert [device.address for device in devices] == ["addr-1"]


async def test_name_match_is_substring_and_case_insensitive() -> None:
    """官方示例用的是子串匹配；有些批次名字前面还带别的字符。"""
    scanner = FakeScanner(
        results=[
            _result("addr-1", "MyWT901", -50),
            _result("addr-2", "wt9011dcl", -60),
        ]
    )
    devices = await scan(discover=scanner)
    assert {device.address for device in devices} == {"addr-1", "addr-2"}


async def test_unfiltered_scan_returns_everything() -> None:
    """设备改过名时，得能分清「没扫到」和「被过滤掉了」。"""
    scanner = FakeScanner(
        results=[_result("addr-1", "WT901", -50), _result("addr-2", "Mouse", -60)]
    )
    devices = await scan(name_substring=None, discover=scanner)
    assert len(devices) == 2


async def test_sorted_by_signal_strength() -> None:
    scanner = FakeScanner(
        results=[
            _result("far", "WT-far", -80),
            _result("near", "WT-near", -40),
            _result("mid", "WT-mid", -60),
        ]
    )
    devices = await scan(discover=scanner)
    assert [device.address for device in devices] == ["near", "mid", "far"]


async def test_devices_without_rssi_sort_last() -> None:
    """拿不到信号强度不代表信号最差，但也不该插到前面。"""
    scanner = FakeScanner(
        results=[_result("unknown", "WT-a", None), _result("weak", "WT-b", -95)]
    )
    devices = await scan(discover=scanner)
    assert [device.address for device in devices] == ["weak", "unknown"]


async def test_empty_scan_returns_empty_list() -> None:
    assert await scan(discover=FakeScanner()) == []


async def test_timeout_is_passed_through() -> None:
    scanner = FakeScanner()
    await scan(2.5, discover=scanner)
    assert scanner.timeouts == [2.5]


async def test_scan_timeout_maps_to_transport_timeout() -> None:
    scanner = FakeScanner(error=TimeoutError())
    with pytest.raises(TransportTimeoutError):
        await scan(discover=scanner)


async def test_scan_failure_maps_to_transport_error() -> None:
    scanner = FakeScanner(error=RuntimeError("bluetooth off"))
    with pytest.raises(TransportError, match="bluetooth off"):
        await scan(discover=scanner)


async def test_discovered_device_is_immutable() -> None:
    scanner = FakeScanner(results=[_result("addr-1", "WT901", -50)])
    (device,) = await scan(discover=scanner)
    with pytest.raises(AttributeError):
        device.address = "other"  # type: ignore[misc]
