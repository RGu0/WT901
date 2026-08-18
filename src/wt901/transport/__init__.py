"""传输层：与设备之间的字节通道，不认识协议语义。"""

from wt901.transport.base import DataCallback, DisconnectCallback, Transport
from wt901.transport.ble import (
    DEFAULT_CONNECT_TIMEOUT,
    NOTIFY_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    WRITE_CHARACTERISTIC_UUID,
    BleTransport,
)
from wt901.transport.memory import MemoryTransport

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "NOTIFY_CHARACTERISTIC_UUID",
    "SERVICE_UUID",
    "WRITE_CHARACTERISTIC_UUID",
    "BleTransport",
    "DataCallback",
    "DisconnectCallback",
    "MemoryTransport",
    "Transport",
]
