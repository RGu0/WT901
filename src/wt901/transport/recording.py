"""录制传输：包住另一条传输，把经过的字节抄一份到文件。

做成装饰器而不是给 :class:`~wt901.transport.ble.BleTransport` 加一个 ``record=``
参数，是因为录制与「怎么连上设备」完全无关。同一个装饰器可以包 BLE，也可以包
:class:`~wt901.transport.memory.MemoryTransport`——后者正是本模块自己的测试手段。

只录**接收方向**。下行指令是调用方自己发出的，回放时由调用方原样再发一遍即可；
把它们混进同一个文件反而会让「设备说了什么」和「我们说了什么」难以分辨。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Self

from wt901.recording import RecordingWriter
from wt901.transport.base import Transport

__all__ = ["RecordingTransport"]


class RecordingTransport(Transport):
    """把 ``inner`` 收到的每一段字节写进录制文件，然后原样向上传递。"""

    __slots__ = ("_inner", "_writer")

    def __init__(self, inner: Transport, writer: RecordingWriter) -> None:
        super().__init__()
        self._inner = inner
        self._writer = writer

    @classmethod
    def to_file(
        cls,
        inner: Transport,
        path: Path,
        *,
        note: str = "",
        clock: Callable[[], float] | None = None,
    ) -> RecordingTransport:
        """把录制写到 ``path``。文件在这里就打开，由 :meth:`disconnect` 关闭。"""
        handle = path.open("w", encoding="utf-8")
        writer = RecordingWriter(
            handle,
            device_id=inner.device_id,
            note=note,
            clock=clock or time.monotonic,
        )
        return cls(inner, writer)

    @property
    def device_id(self) -> str:
        return self._inner.device_id

    @property
    def is_connected(self) -> bool:
        return self._inner.is_connected

    @property
    def chunks_written(self) -> int:
        return self._writer.chunks_written

    async def connect(self) -> None:
        # 接线放在 connect 里而不是 __init__ 里：inner 可能在构造之后、连接之前
        # 被别人接管回调，那样录制会静悄悄地录不到东西。
        self._inner.on_data(self._record)
        self._inner.on_disconnect(self._emit_disconnect)
        await self._inner.connect()

    async def disconnect(self) -> None:
        try:
            await self._inner.disconnect()
        finally:
            # 无论断连是否抛异常，录到的部分都要落盘：一次异常断连的录制往往
            # 正是最值得看的那份。
            self._writer.close()
            self._inner.on_data(None)
            self._inner.on_disconnect(None)

    async def write(self, data: bytes) -> None:
        await self._inner.write(data)

    def _record(self, data: bytes) -> None:
        self._writer.write(data)
        self._emit_data(data)

    async def __aenter__(self) -> Self:
        await self.connect()
        return self
