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

    CONFIG_REPLAY_FAILED = "config_replay_failed"
    """链路连上了，但重连后的配置重放失败，本层已把这条连接断开。

    与 :attr:`RECONNECT_FAILED` 的差别是**调用方的处置**：那条说「还没连上，
    等着」，这条说「连是连上了，可设备停在它自己的上电状态上——别用这批数据」。
    它不是终态：本层随后按既有退避继续重试，重试耗尽时照常发
    :attr:`RECONNECT_FAILED`。
    """


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """连接状态变化。

    上层**必须**据此判断样本序列的连续性：``seq`` 在每次重连后归零，把跨连接
    的样本当成一条连续序列会得到错误的时间/序号推断。

    本层保证的是：**任何样本都不会在它所属那条连接的** :attr:`~ConnectionState.CONNECTED`
    **发出之前被造出来**。未就绪窗口内到达的数据帧被丢弃并计入
    :attr:`DeviceStats.dropped_before_ready`。

    保证到此为止。``samples()`` 与 ``events()`` 是两条独立队列、由两个任务各自
    消费，**跨队列的先后不归本层管**：分别迭代两者的调用方仍可能先取到样本、
    后取到那条事件。要严格对齐，就在收到 ``CONNECTED`` 之后再开始取样本。
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

    判断「数据可不可信」有三个入口，各指向不同的成因，不要互相替代：

    * ``dropped_samples`` —— 消费者跟不上，队列压力。
    * ``resync_count`` —— 链路上出现过字节错位。
    * ``dropped_before_ready`` —— 一段数据产生在连接就绪之前，本层没有交付它。

    三者都不为零时先看最后一条：它说明那段时间设备停在自己的上电配置上，与
    消费速度和链路质量都无关。
    """

    frames: int = 0
    samples: int = 0
    dropped_samples: int = 0
    dropped_before_ready: int = 0
    """连接就绪之前到达、因此被丢弃的实时数据帧数。

    「就绪」指本层已发出该条连接的 :attr:`ConnectionState.CONNECTED`。在那之前
    链路已经在收字节了：``open()`` 里订阅完成到事件发出之间有一小段，重连路径上
    则隔着一整次配置重放（每条寄存器约 0.2 s）。这段时间设备停在它的上电配置上
    （出厂 10 Hz），产出的样本与其余部分不是同一个采样率。

    这些帧被丢弃而不是缓冲后补发——补发只是把两种采样率换个时间混进同一条序列。

    **不要和** ``dropped_samples`` **混着看**：那条说「消费者跟不上」，是队列压力；
    这条说「流还没开始」，与消费速度无关。两者都不为零时，先看这条。
    """
    register_frames: int = 0
    resync_count: int = 0
    dropped_bytes: int = 0
    reconnects: int = 0
    """**完整恢复**的重连次数：连上且配置重放成功才计入。

    重放失败的那次不算——它以断开收场（见
    :attr:`ConnectionState.CONFIG_REPLAY_FAILED`）。把它算成一次成功重连，
    统计就会和事件流互相矛盾，而这两样常常是排查现场唯一剩下的东西。
    """


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
        self._delivering = False
        self._intentional_disconnect = False
        self._reconnect_task: asyncio.Task[None] | None = None

        self._frames = 0
        self._samples_emitted = 0
        self._dropped_samples = 0
        self._dropped_before_ready = 0
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
        """建立连接并开始接收。

        ``connect()`` 一返回通知就已订阅，字节随时会到，而 ``CONNECTED`` 事件还
        没发出。这段窗口里到达的实时数据帧被丢弃并计入
        :attr:`DeviceStats.dropped_before_ready`；重连路径上是同一条不变式，
        只是那边的窗口大得多（隔着一整次配置重放）。
        """
        self._closing = False
        self._delivering = False
        self._transport.on_data(self._handle_bytes)
        self._transport.on_disconnect(self._handle_disconnect)
        await self._transport.connect()
        self._emit_event(ConnectionEvent(ConnectionState.CONNECTED, self.device_id))
        self._delivering = True

    async def close(self) -> None:
        """停止接收并释放连接。已关闭时调用无副作用。"""
        if self._closing:
            return
        self._closing = True
        self._delivering = False

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
            dropped_before_ready=self._dropped_before_ready,
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

        钩子抛出的异常**不会**逃出重连循环：本层记一条日志、发
        :attr:`ConnectionState.CONFIG_REPLAY_FAILED`、断开链路，并把这次重连
        整体算作失败重来。所以钩子失败时只管抛，不必自己重试——重试用的是与
        连接同一套退避策略。
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
                if not self._delivering:
                    # 这条连接的 CONNECTED 还没发出：链路在收字节，但设备停在它
                    # 的上电配置上，这批样本与其余部分不是同一个采样率。丢掉并
                    # 单独计数——补发只会把两种采样率混进同一条序列。
                    #
                    # seq 不推进：CONNECTED 之后第一个交出去的样本仍是 seq 0，
                    # 于是 seq 的缺口只表示背压丢弃，不掺「流还没开始」。
                    self._dropped_before_ready += 1
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
        # 无条件关门：这条连接已经没了，之后到达的任何字节都属于下一条连接的
        # 未就绪窗口。三条进入路径（正常断连、close()、重放失败后的主动断开）
        # 都必须收敛到同一个状态，否则「就绪」会随断开的原因而不同。
        self._delivering = False
        if self._closing or self._intentional_disconnect:
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
                # 连接动作之前先关门。真实后端会在断连时回调 on_disconnect
                # 把它关上，但不能把不变式的成立寄托在后端一定回调上。
                self._delivering = False
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

                if self._reconnect_hook is not None:
                    try:
                        await self._reconnect_hook()
                    except Exception as exc:
                        # 钩子失败最容易发生在链路刚恢复、还没稳住的那一刻。
                        # 就这么连着比断开更危险：数据照流、没有一处报错，而
                        # 设备停在出厂默认值上（10 Hz），应用以为是它配的那个
                        # 速率。断开让这次重连整体算失败，交回同一套退避重试。
                        # 捕获 Exception 而不是 TransportError：钩子是调用方
                        # 能替换的，它抛什么都不该只剩 asyncio 在 GC 时打的那
                        # 行日志。
                        _LOGGER.warning(
                            "重连第 %d 次：配置重放失败，断开链路重来：%s",
                            attempt,
                            exc,
                            exc_info=True,
                        )
                        self._emit_event(
                            ConnectionEvent(
                                ConnectionState.CONFIG_REPLAY_FAILED,
                                self.device_id,
                                attempt=attempt,
                                error=str(exc),
                            )
                        )
                        await self._drop_connection()
                        continue

                # 连上**且**配置已恢复，这次重连才算数。
                self._reconnects += 1
                self._emit_event(
                    ConnectionEvent(
                        ConnectionState.CONNECTED, self.device_id, attempt=attempt
                    )
                )
                self._delivering = True
                return
        finally:
            self._reconnect_task = None

    async def _drop_connection(self) -> None:
        """配置重放失败后主动断开，且不把它记成一次新的断连。

        调用方从没收到过这条连接的 ``CONNECTED``——它在重放失败时就被判废了，
        再发一条 ``DISCONNECTED`` 只会让「一直是断开的」这件事看起来发生了两次。
        真实 BLE 后端会在这里回调 ``on_disconnect``，内存传输不会；压住它顺带
        让事件序列不随传输实现而变。
        """
        self._intentional_disconnect = True
        try:
            await self._transport.disconnect()
        except Exception as exc:  # 已经在失败路径上，断不掉也要接着重试
            _LOGGER.warning(
                "配置重放失败后断开链路也失败了：%s", exc, exc_info=True
            )
        finally:
            self._intentional_disconnect = False

    def __repr__(self) -> str:
        return (
            f"<WT901Device {self.device_id} "
            f"connected={self.is_connected} mode={self._output_mode.value}>"
        )
