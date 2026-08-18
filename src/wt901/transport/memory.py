"""内存传输：给后续 scope 做离线测试用。

设备层的生命周期、背压、重连逻辑（RAY-170）以及寄存器读写事务（RAY-171）都
需要在没有硬件的情况下验证。它们需要的不是「一个能跑的假对象」，而是一个能
**断言调用顺序**的对象——比如「异常退出时到底有没有 disconnect」这种问题，
只有把调用记下来才能回答。
"""

from __future__ import annotations

from wt901.errors import ConnectionLostError
from wt901.transport.base import Transport

__all__ = ["MemoryTransport"]


class MemoryTransport(Transport):
    """把写入记录下来、允许测试注入接收字节的传输实现。"""

    __slots__ = (
        "_connected",
        "_device_id",
        "connect_calls",
        "disconnect_calls",
        "writes",
    )

    def __init__(self, device_id: str = "memory-device") -> None:
        super().__init__()
        self._device_id = device_id
        self._connected = False
        self.writes: list[bytes] = []
        """按顺序记录每一次 :meth:`write`，供逐字节断言指令序列。"""
        self.connect_calls = 0
        self.disconnect_calls = 0

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    async def write(self, data: bytes) -> None:
        if not self._connected:
            raise ConnectionLostError("传输未连接，写入被拒绝")
        self.writes.append(bytes(data))

    def feed(self, data: bytes) -> None:
        """模拟设备发来一段字节，同步触发接收回调。"""
        self._emit_data(bytes(data))

    def drop(self) -> None:
        """模拟对端断连，触发断连回调。"""
        self._connected = False
        self._emit_disconnect()

    @property
    def written(self) -> bytes:
        """所有写入拼成一整段，便于与预期指令序列整体比对。"""
        return b"".join(self.writes)
