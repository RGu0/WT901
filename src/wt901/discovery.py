"""BLE 设备发现。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from wt901.errors import TransportError, TransportTimeoutError

__all__ = [
    "DEFAULT_NAME_SUBSTRING",
    "DEFAULT_SCAN_TIMEOUT",
    "DiscoveredDevice",
    "scan",
]

DEFAULT_SCAN_TIMEOUT = 5.0
DEFAULT_NAME_SUBSTRING = "WT"
"""维特设备的广播名里都带 WT（``WT901BLE68`` 等）。官方 Python 示例用的也是
子串匹配而非前缀匹配，这里保持一致——有些批次的名字前面还带别的字符。"""


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """一次扫描发现的设备。"""

    address: str
    """连接用的地址。**跨平台不可移植**：macOS 上是 CoreBluetooth UUID，
    Linux/Windows 上是 MAC 地址。不要跨主机持久化。"""
    name: str | None
    rssi: int | None


DiscoverFunc = Callable[[float], Awaitable[Iterable[tuple[Any, Any]]]]
"""``(timeout) -> [(device, advertisement), ...]``，可注入以便离线测试。"""


async def _bleak_discover(timeout: float) -> list[tuple[Any, Any]]:
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return list(found.values())


async def scan(
    timeout: float = DEFAULT_SCAN_TIMEOUT,
    *,
    name_substring: str | None = DEFAULT_NAME_SUBSTRING,
    discover: DiscoverFunc = _bleak_discover,
) -> list[DiscoveredDevice]:
    """扫描附近的 BLE 设备。

    默认只返回名字里带 ``WT`` 的设备。传 ``name_substring=None`` 返回全部——
    设备改过名、或者要确认「到底是没扫到还是被过滤掉了」时需要它。

    结果按信号强度降序，最近的设备排在前面。
    """
    try:
        found = await discover(timeout)
    except TimeoutError as exc:
        raise TransportTimeoutError(f"扫描超时（{timeout}s）") from exc
    except Exception as exc:  # bleak 的异常类型随后端而异
        raise TransportError(f"扫描失败：{exc}") from exc

    devices = [
        DiscoveredDevice(
            address=device.address,
            name=device.name,
            rssi=getattr(advertisement, "rssi", None),
        )
        for device, advertisement in found
    ]
    if name_substring is not None:
        needle = name_substring.lower()
        devices = [
            device
            for device in devices
            if device.name is not None and needle in device.name.lower()
        ]
    # rssi 为 None 的排在最后：拿不到信号强度不代表信号最差，但也不该插到前面。
    devices.sort(key=lambda device: (device.rssi is None, -(device.rssi or 0)))
    return devices
