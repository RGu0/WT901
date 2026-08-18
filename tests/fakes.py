"""假的 bleak 客户端与扫描器。

BLE 里最需要测的分支——服务缺失、订阅失败、写入失败、连接超时、对端断连——
恰恰是真机上最难复现的。把 ``BleakClient`` 用到的那一小片接口显式建模出来，
这些路径就都能在 CI 里跑。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from wt901.transport.ble import (
    NOTIFY_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    WRITE_CHARACTERISTIC_UUID,
)

__all__ = [
    "FakeAdvertisement",
    "FakeCharacteristic",
    "FakeClient",
    "FakeDevice",
    "FakeService",
    "full_services",
]


@dataclass(frozen=True)
class FakeCharacteristic:
    uuid: str


@dataclass(frozen=True)
class FakeService:
    uuid: str
    characteristics: list[FakeCharacteristic]


def full_services() -> list[FakeService]:
    """一台特征齐全的正常设备。"""
    return [
        FakeService(
            uuid=SERVICE_UUID,
            characteristics=[
                FakeCharacteristic(NOTIFY_CHARACTERISTIC_UUID),
                FakeCharacteristic(WRITE_CHARACTERISTIC_UUID),
            ],
        )
    ]


class FakeClient:
    """按 ``BleakClientLike`` 建模的假客户端。"""

    def __init__(
        self,
        address: str,
        timeout: float,
        disconnected_callback: Callable[[Any], None],
        *,
        services: list[FakeService] | None = None,
        connect_error: Exception | None = None,
        notify_error: Exception | None = None,
        write_error: Exception | None = None,
        stop_notify_error: Exception | None = None,
        disconnect_error: Exception | None = None,
    ) -> None:
        self.address = address
        self.timeout = timeout
        self.disconnected_callback = disconnected_callback
        self.services: list[FakeService] = (
            full_services() if services is None else services
        )
        self._connect_error = connect_error
        self._notify_error = notify_error
        self._write_error = write_error
        self._stop_notify_error = stop_notify_error
        self._disconnect_error = disconnect_error

        self.is_connected = False
        self.calls: list[str] = []
        """按顺序记录生命周期调用，用来断言清理确实发生过。"""
        self.writes: list[bytes] = []
        self.notify_callback: Callable[[Any, bytearray], None] | None = None

    async def connect(self) -> None:
        self.calls.append("connect")
        if self._connect_error is not None:
            raise self._connect_error
        self.is_connected = True

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.is_connected = False
        if self._disconnect_error is not None:
            raise self._disconnect_error

    async def start_notify(
        self, characteristic: Any, callback: Callable[[Any, bytearray], None]
    ) -> None:
        self.calls.append("start_notify")
        if self._notify_error is not None:
            raise self._notify_error
        self.notify_callback = callback

    async def stop_notify(self, characteristic: Any) -> None:
        self.calls.append("stop_notify")
        if self._stop_notify_error is not None:
            raise self._stop_notify_error

    async def write_gatt_char(self, characteristic: Any, data: bytes) -> None:
        if self._write_error is not None:
            raise self._write_error
        self.writes.append(bytes(data))

    def push(self, data: bytes) -> None:
        """模拟设备发来一条通知。"""
        if self.notify_callback is None:
            raise AssertionError("还没订阅通知")
        self.notify_callback(object(), bytearray(data))

    def drop(self) -> None:
        """模拟对端断连。"""
        self.is_connected = False
        self.disconnected_callback(self)


@dataclass(frozen=True)
class FakeDevice:
    address: str
    name: str | None


@dataclass(frozen=True)
class FakeAdvertisement:
    rssi: int | None = None


@dataclass
class FakeScanner:
    """可注入的扫描函数。"""

    results: list[tuple[FakeDevice, FakeAdvertisement]] = field(default_factory=list)
    error: Exception | None = None
    timeouts: list[float] = field(default_factory=list)

    async def __call__(
        self, timeout: float
    ) -> list[tuple[FakeDevice, FakeAdvertisement]]:
        self.timeouts.append(timeout)
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.results
