"""回放传输：用录制文件驱动完整的设备层，不需要任何硬件。

这是 CI 里唯一能端到端验证「字节 → 帧 → 样本」整条链路的手段。单元测试可以喂
构造好的帧，但构造出来的帧是按我们**以为**的格式写的；录制文件里的字节是设备
真的发出来的。

**回放不回答下行指令。** 寄存器读写走的是请求/应答，而录制文件里只有接收方向的
字节，没有「针对这次请求的回帧」这个概念。回放时调用 ``registers.read()`` 会等到
超时——这是正确行为，不是缺陷：回放能验证的是数据流，不是事务。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Self

from wt901.errors import ConnectionLostError
from wt901.recording import Recording, read_recording
from wt901.transport.base import Transport

__all__ = ["ReplayTransport"]


class ReplayTransport(Transport):
    """按录制的时序（或全速）把字节喂回去。"""

    __slots__ = (
        "_connected",
        "_device_id",
        "_disconnect_at_end",
        "_exhausted",
        "_recording",
        "_speed",
        "_task",
        "writes",
    )

    def __init__(
        self,
        recording: Recording,
        *,
        speed: float | None = 1.0,
        device_id: str | None = None,
        disconnect_at_end: bool = False,
    ) -> None:
        """
        ``speed`` 为 ``1.0`` 时按录制的原时序回放，``2.0`` 是两倍速；传 ``None``
        表示不等待，尽可能快地喂完——**CI 应当用** ``None``：按原时序回放一段 10
        秒的录制就要花 10 秒，而回放测试要验证的是内容，不是墙上时钟。
        """
        super().__init__()
        if speed is not None and speed <= 0:
            raise ValueError("speed 必须为正数；不想等待请传 None")
        self._recording = recording
        self._speed = speed
        self._device_id = device_id if device_id is not None else recording.device_id
        self._disconnect_at_end = disconnect_at_end
        self._connected = False
        self._task: asyncio.Task[None] | None = None
        self._exhausted = asyncio.Event()
        self.writes: list[bytes] = []
        """回放期间收到的下行字节。没有人会回答它们，但记下来便于断言。"""

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        speed: float | None = 1.0,
        device_id: str | None = None,
        disconnect_at_end: bool = False,
    ) -> ReplayTransport:
        return cls(
            read_recording(path),
            speed=speed,
            device_id=device_id,
            disconnect_at_end=disconnect_at_end,
        )

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def recording(self) -> Recording:
        return self._recording

    @property
    def exhausted(self) -> bool:
        """录制是否已经喂完。"""
        return self._exhausted.is_set()

    async def connect(self) -> None:
        if self._connected:
            return
        self._connected = True
        self._exhausted.clear()
        self._task = asyncio.ensure_future(self._feed())

    async def disconnect(self) -> None:
        self._connected = False
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def write(self, data: bytes) -> None:
        if not self._connected:
            raise ConnectionLostError("回放传输未连接，写入被拒绝")
        self.writes.append(bytes(data))

    async def wait_exhausted(self) -> None:
        """等到最后一段字节喂完。"""
        await self._exhausted.wait()

    async def _feed(self) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        for chunk in self._recording.chunks:
            if self._speed is not None:
                delay = start + chunk.t / self._speed - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
            else:
                # 全速也要让出控制权：否则整段录制会在一个事件循环轮次里喂完，
                # 消费者一次都轮不到，队列满了就开始丢——丢出来的是回放的假象。
                await asyncio.sleep(0)
            self._emit_data(chunk.data)
        self._exhausted.set()
        if self._disconnect_at_end:
            self._connected = False
            self._emit_disconnect()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self
