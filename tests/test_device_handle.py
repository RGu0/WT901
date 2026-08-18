"""连接目标透传测试（RAY-178）。

真机症状：扫描能看到设备（rssi -34），连接却报「设备未找到」。原因是我们把
扫描返回的平台句柄丢掉了，只用地址字符串构造 BleakClient——bleak 于是必须自己
再扫一遍做地址解析，而 macOS 的地址只是 CoreBluetooth 分配的会话内标识。

官方 SDK 传的是对象。这些测试钉住「传对象」这件事。
"""

from __future__ import annotations

from typing import Any

from fakes import FakeAdvertisement, FakeDevice, FakeScanner, full_services
from wt901.discovery import DiscoveredDevice, scan
from wt901.transport.ble import BleTransport


class _RecordingClient:
    """记下自己是被什么东西构造出来的。"""

    def __init__(self, target: Any) -> None:
        self.target = target
        self.is_connected = False
        self.services = full_services()

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def start_notify(self, characteristic: Any, callback: Any) -> None:
        return None

    async def stop_notify(self, characteristic: Any) -> None:
        return None

    async def write_gatt_char(self, characteristic: Any, data: bytes) -> None:
        return None


def _transport(target: Any) -> tuple[BleTransport, list[_RecordingClient]]:
    created: list[_RecordingClient] = []

    def factory(resolved: Any, _timeout: float, _on_disconnect: Any) -> _RecordingClient:
        client = _RecordingClient(resolved)
        created.append(client)
        return client

    return BleTransport(target, client_factory=factory), created  # type: ignore[arg-type]


# ----- 扫描保留句柄 --------------------------------------------------------


async def test_scan_keeps_the_underlying_device_object() -> None:
    device = FakeDevice(address="addr-1", name="WT901BLE67")
    scanner = FakeScanner(results=[(device, FakeAdvertisement(rssi=-34))])

    (found,) = await scan(discover=scanner)
    assert found.handle is device, "扫描必须保留底层设备对象，否则连接只能靠地址解析"
    assert found.address == "addr-1"


# ----- 连接透传句柄 --------------------------------------------------------


async def test_discovered_device_passes_the_handle_not_the_address() -> None:
    """**核心回归**：交给客户端的必须是句柄，不是地址字符串。"""
    handle = FakeDevice(address="addr-1", name="WT901BLE67")
    discovered = DiscoveredDevice(
        address="addr-1", name="WT901BLE67", rssi=-34, handle=handle
    )
    transport, clients = _transport(discovered)
    await transport.connect()

    assert clients[0].target is handle, (
        f"交给客户端的是 {clients[0].target!r}，应当是扫描得到的句柄"
    )
    await transport.disconnect()


async def test_device_id_is_still_the_address_string() -> None:
    """句柄只在本次扫描会话内有效，不能当标识；device_id 仍取地址。"""
    handle = FakeDevice(address="addr-1", name="WT901BLE67")
    discovered = DiscoveredDevice(
        address="addr-1", name="WT901BLE67", rssi=-34, handle=handle
    )
    transport, _ = _transport(discovered)
    assert transport.device_id == "addr-1"


async def test_plain_address_string_still_works() -> None:
    """向后兼容：已经知道地址、不想先扫描的场景仍可用。"""
    transport, clients = _transport("AA:BB:CC:DD:EE:FF")
    await transport.connect()

    assert clients[0].target == "AA:BB:CC:DD:EE:FF"
    assert transport.device_id == "AA:BB:CC:DD:EE:FF"
    await transport.disconnect()


async def test_discovered_device_without_handle_falls_back_to_address() -> None:
    """句柄缺失时退回地址——不要因为少个可选字段就连不上。"""
    discovered = DiscoveredDevice(address="addr-1", name="WT", rssi=-40)
    transport, clients = _transport(discovered)
    await transport.connect()

    assert clients[0].target == "addr-1"
    assert transport.device_id == "addr-1"
    await transport.disconnect()
