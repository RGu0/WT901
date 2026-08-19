"""设备门面：把协议层与传输层黏合成对外的主 API。

这是使用者绝大部分时间面对的类。它做四件事：管理连接生命周期、把字节流变成
带时间戳的样本、在消费者跟不上时以可观测的方式丢弃、以及在断线后重连。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from types import TracebackType
from typing import Self, TypeVar

from wt901.calibration import Calibration
from wt901.config import RegisterAccess
from wt901.discovery import DiscoveredDevice
from wt901.errors import ConfigurationError, TransportError
from wt901.models import ImuSample
from wt901.protocol.frames import (
    FrameDecoder,
    FrameFlag,
    RegisterResponse,
    decode_register_response,
)
from wt901.telemetry import Telemetry
from wt901.transport.base import Transport
from wt901.transport.ble import DEFAULT_CONNECT_TIMEOUT, BleTransport

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "ConnectionEvent",
    "ConnectionState",
    "DeviceStats",
    "OutputMode",
    "ReconnectPolicy",
    "WT901Device",
]

_LOGGER = logging.getLogger(__name__)

# asyncio.Queue 是不变的（invariant），所以 _offer 必须泛型化——用 Queue[object]
# 会让 Queue[ImuSample | None] 传不进去。
_T = TypeVar("_T")

DEFAULT_QUEUE_SIZE = 1024
"""样本队列容量。20 字节/帧、200 Hz 下约等于 5 秒的缓冲。"""

_EVENT_QUEUE_SIZE = 64


class OutputMode(Enum):
    """设备当前的 ``0x61`` 帧语义。

    寄存器 ``0x96`` 置 1 后，位移帧复用同一个标志位且**字节布局无法区分**。
    解析方必须自带这个上下文——没有任何办法从帧本身看出来。
    """

    MOTION = "motion"
    """默认：加速度 / 角速度 / 角度。"""

    DISPLACEMENT = "displacement"
    """``0x96 = 1``：位移 / 位移速度 / 角度。v0.1 未实现解码。"""


class ConnectionState(Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    RECONNECT_FAILED = "reconnect_failed"


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """连接状态变化。

    上层**必须**据此判断样本序列的连续性：``seq`` 在每次重连后归零，把跨连接
    的样本当成一条连续序列会得到错误的时间/序号推断。
    """

    state: ConnectionState
    device_id: str
    attempt: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """指数退避。``max_attempts=None`` 表示一直重试。"""

    initial_delay: float = 0.5
    max_delay: float = 30.0
    factor: float = 2.0
    max_attempts: int | None = None

    def delay_for(self, attempt: int) -> float:
        return min(self.initial_delay * self.factor ** (attempt - 1), self.max_delay)


@dataclass(frozen=True, slots=True)
class DeviceStats:
    """链路与采集质量。全部单调递增，跨重连不清零。

    ``dropped_samples`` 与 ``resync_count`` 是判断「数据可不可信」的两个入口：
    前者说明消费者跟不上，后者说明链路上出现过字节错位。
    """

    frames: int = 0
    samples: int = 0
    dropped_samples: int = 0
    register_frames: int = 0
    resync_count: int = 0
    dropped_bytes: int = 0
    reconnects: int = 0


class WT901Device:
    """一台 WT9011DCL-BT50。

    典型用法::

        async with await WT901Device.connect(address) as device:
            async for sample in device.samples():
                print(sample.t_host, sample.accel, sample.euler)

    ``samples()`` 是**单消费者**接口：多处并发迭代会各自拿到样本的一个子集。
    需要分发给多个消费者时，由上层扇出。
    """

    def __init__(
        self,
        transport: Transport,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        auto_reconnect: bool = False,
        reconnect_policy: ReconnectPolicy | None = None,
        output_mode: OutputMode = OutputMode.MOTION,
    ) -> None:
        self._transport = transport
        self._output_mode = output_mode
        self._auto_reconnect = auto_reconnect
        self._policy = reconnect_policy or ReconnectPolicy()

        self._decoder = FrameDecoder()
        self._samples: asyncio.Queue[ImuSample | None] = asyncio.Queue(queue_size)
        self._events: asyncio.Queue[ConnectionEvent | None] = asyncio.Queue(
            _EVENT_QUEUE_SIZE
        )
        self._seq = 0
        self._closing = False
        self._reconnect_task: asyncio.Task[None] | None = None

        self._frames = 0
        self._samples_emitted = 0
        self._dropped_samples = 0
        self._register_frames = 0
        self._reconnects = 0

        self._register_listener: Callable[[RegisterResponse], None] | None = None
        self._reconnect_hook: Callable[[], Awaitable[None]] | None = None

        # 寄存器通道自带默认接线：0x71 回帧交给它，重连后由它重放配置。
        # 调用方仍可用 on_register_response() / on_reconnect() 覆盖。
        self._registers = RegisterAccess(self)
        self._register_listener = self._registers.dispatch
        self._reconnect_hook = self._registers.replay
        self._telemetry = Telemetry(self)
        self._calibration = Calibration(self)

    # ----- 构造与生命周期 -------------------------------------------------

    @classmethod
    async def connect(
        cls,
        target: str | DiscoveredDevice,
        *,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        auto_reconnect: bool = False,
        reconnect_policy: ReconnectPolicy | None = None,
        output_mode: OutputMode = OutputMode.MOTION,
    ) -> WT901Device:
        """通过 BLE 连接一台设备。

        **优先把 :func:`wt901.discovery.scan` 返回的整个
        :class:`~wt901.discovery.DiscoveredDevice` 传进来**，不要只传地址::

            devices = await scan()
            async with await WT901Device.connect(devices[0]) as device:
                ...

        只给地址字符串时，bleak 需要自己再扫一遍做地址→平台句柄的解析。macOS 上
        的地址只是 CoreBluetooth 分配的会话内标识，跨扫描会话解析并不可靠——失败
        时报的是「设备未找到」，哪怕设备就在眼前、信号很强。
        """
        device = cls(
            BleTransport(target, timeout=timeout),
            queue_size=queue_size,
            auto_reconnect=auto_reconnect,
            reconnect_policy=reconnect_policy,
            output_mode=output_mode,
        )
        await device.open()
        return device

    async def open(self) -> None:
        """建立连接并开始接收。"""
        self._closing = False
        self._transport.on_data(self._handle_bytes)
        self._transport.on_disconnect(self._handle_disconnect)
        await self._transport.connect()
        self._emit_event(ConnectionEvent(ConnectionState.CONNECTED, self.device_id))

    async def close(self) -> None:
        """停止接收并释放连接。已关闭时调用无副作用。"""
        if self._closing:
            return
        self._closing = True

        # 先取消重连任务：否则它可能在 disconnect 之后又把连接建起来。
        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._transport.on_data(None)
        self._transport.on_disconnect(None)
        await self._transport.disconnect()

        # 哨兵唤醒正在等待的迭代器，否则 async for 会永远挂着。
        self._offer(self._samples, None)
        self._offer(self._events, None)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    # ----- 对外读取 -------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._transport.device_id

    @property
    def is_connected(self) -> bool:
        return self._transport.is_connected

    @property
    def output_mode(self) -> OutputMode:
        return self._output_mode

    @property
    def pending_samples(self) -> int:
        """队列中尚未被取走的样本数。

        配置变更之后这个数就是「陈数据」的量：写事务本身要花几百毫秒，其间设备
        仍在以旧配置推数据。想测量新配置的效果，得先把它们取干净。
        """
        return self._samples.qsize()

    @property
    def telemetry(self) -> Telemetry:
        """按需读取磁场、四元数、温度、电量、芯片时间、序列号、版本号。

        这些量不在 ``0x61`` 实时数据流里，需要主动读寄存器取回。
        """
        return self._telemetry

    @property
    def calibration(self) -> Calibration:
        """加计校准与磁场校准。

        磁场校准优先用 ``device.calibration.field_calibration()`` 上下文管理器：
        它保证退出校准态，包括 with 体内抛异常的情况。
        """
        return self._calibration

    @property
    def registers(self) -> RegisterAccess:
        """寄存器读写通道。

        ``await device.registers.set_output_rate(ReturnRate.HZ_50)``、
        ``await device.registers.read(Register.HX)`` 等。
        """
        return self._registers

    @property
    def stats(self) -> DeviceStats:
        return DeviceStats(
            frames=self._frames,
            samples=self._samples_emitted,
            dropped_samples=self._dropped_samples,
            register_frames=self._register_frames,
            resync_count=self._decoder.resync_count,
            dropped_bytes=self._decoder.dropped_bytes,
            reconnects=self._reconnects,
        )

    async def samples(self) -> AsyncIterator[ImuSample]:
        """按到达顺序产出样本，直到 :meth:`close`。"""
        if self._output_mode is not OutputMode.MOTION:
            raise ConfigurationError(
                "设备被声明为位移输出模式（寄存器 0x96 = 1），v0.1 未实现该模式的"
                "解码。位移帧与实时数据帧共用标志位 0x61 且布局无法区分，因此本库"
                "拒绝按运动语义解析。"
            )
        while True:
            sample = await self._samples.get()
            if sample is None:
                return
            yield sample

    async def events(self) -> AsyncIterator[ConnectionEvent]:
        """产出连接状态变化，直到 :meth:`close`。"""
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def on_register_response(
        self, callback: Callable[[RegisterResponse], None] | None
    ) -> None:
        """注册 ``0x71`` 回读帧的接收回调。

        寄存器回读与实时数据流共用同一条链路，帧会混在一起到达。这个钩子让
        寄存器事务层（RAY-171）能取到它们，而不必重新解析字节流。
        """
        self._register_listener = callback

    def on_reconnect(self, hook: Callable[[], Awaitable[None]] | None) -> None:
        """注册重连后的配置重放钩子。

        BLE 断连不保证模块保留运行时状态，重连后必须重新下发已知配置。具体配置
        能力属寄存器事务层（RAY-171）；本层只保证在连接恢复后、样本恢复流动前
        调用这个钩子。
        """
        self._reconnect_hook = hook

    async def write(self, data: bytes) -> None:
        """直接向设备写字节。供上层的指令构造使用。"""
        await self._transport.write(data)

    # ----- 内部 -----------------------------------------------------------

    def _handle_bytes(self, data: bytes) -> None:
        """传输层回调。**必须尽快返回**，不做任何 await。"""
        if self._closing:
            return
        for frame in self._decoder.feed(data):
            self._frames += 1
            if frame.flag is FrameFlag.DATA:
                if self._output_mode is not OutputMode.MOTION:
                    # 位移模式：不猜测语义，只计数。
                    continue
                sample = ImuSample.from_frame(
                    frame,
                    device_id=self.device_id,
                    t_host=time.monotonic(),
                    seq=self._seq,
                )
                self._seq += 1
                self._samples_emitted += 1
                if not self._offer(self._samples, sample):
                    self._dropped_samples += 1
            else:
                self._register_frames += 1
                if self._register_listener is not None:
                    self._register_listener(decode_register_response(frame))

    @staticmethod
    def _offer(queue: asyncio.Queue[_T], item: _T) -> bool:
        """入队；队满时丢弃最旧的一个再入队，返回是否发生丢弃。

        采集绝不能因为消费者慢而阻塞——阻塞 BLE 回调会让丢失发生在协议栈里，
        那里既看不见也数不着。丢最旧的而不是最新的：实时姿态数据里，新样本
        永远比旧样本有价值。
        """
        try:
            queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - 单线程下不可达
                pass
            queue.put_nowait(item)
            return False

    def _emit_event(self, event: ConnectionEvent) -> None:
        self._offer(self._events, event)

    def _handle_disconnect(self) -> None:
        """传输层报告对端断开。"""
        if self._closing:
            return
        self._emit_event(ConnectionEvent(ConnectionState.DISCONNECTED, self.device_id))
        if self._auto_reconnect and self._reconnect_task is None:
            self._reconnect_task = asyncio.get_running_loop().create_task(
                self._reconnect_loop()
            )

    async def _reconnect_loop(self) -> None:
        attempt = 0
        try:
            while not self._closing:
                attempt += 1
                if (
                    self._policy.max_attempts is not None
                    and attempt > self._policy.max_attempts
                ):
                    self._emit_event(
                        ConnectionEvent(
                            ConnectionState.RECONNECT_FAILED,
                            self.device_id,
                            attempt=attempt - 1,
                            error="超过最大重试次数",
                        )
                    )
                    return
                self._emit_event(
                    ConnectionEvent(
                        ConnectionState.RECONNECTING, self.device_id, attempt=attempt
                    )
                )
                await asyncio.sleep(self._policy.delay_for(attempt))
                if self._closing:
                    return
                try:
                    await self._transport.connect()
                except TransportError as exc:
                    # 只重试传输层错误。Transport 抽象承诺把各后端异常收敛到
                    # TransportError；漏网的异常说明传输实现违约，那不该被当成
                    # 「设备暂时不在范围内」而无声重试掉。
                    _LOGGER.debug("重连第 %d 次失败：%s", attempt, exc)
                    continue

                # 旧连接残留的半帧与新连接的字节拼起来会凑出一个长度合法、内容
                # 错位的帧——那比丢弃更糟，因为它看不出错。
                self._decoder.reset()
                # seq 归零，配合 ConnectionEvent 让上层不会把跨连接样本当成
                # 一条连续序列。
                self._seq = 0
                self._reconnects += 1

                if self._reconnect_hook is not None:
                    await self._reconnect_hook()

                self._emit_event(
                    ConnectionEvent(
                        ConnectionState.CONNECTED, self.device_id, attempt=attempt
                    )
                )
                return
        finally:
            self._reconnect_task = None

    def __repr__(self) -> str:
        return (
            f"<WT901Device {self.device_id} "
            f"connected={self.is_connected} mode={self._output_mode.value}>"
        )
