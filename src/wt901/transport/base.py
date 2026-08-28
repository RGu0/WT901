"""传输层抽象。

传输层只负责「字节进、字节出」，不认识协议语义：它不知道 ``0x55``，不知道
帧长，也不知道寄存器。这样协议层可以在没有硬件的情况下被完整测试，而传输层
可以在不解析任何内容的情况下被替换（真实 BLE、内存、录制回放）。

接收方向用**回调**而不是 async 迭代器，是为了不在这里引入队列。BLE 通知到达
时必须尽快返回，缓冲与背压策略属于设备层的决策（见 RAY-170）；传输层自己排队
会造成两层缓冲，谁在丢数据将无从判断。
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from types import TracebackType
from typing import Self

__all__ = ["DataCallback", "DisconnectCallback", "Transport"]

DataCallback = Callable[[bytes], None]
"""收到字节时调用。必须尽快返回，不要在里面做耗时工作。"""

DisconnectCallback = Callable[[], None]
"""连接断开时调用。设备层据此触发重连（RAY-170）。"""


class Transport(abc.ABC):
    """一条到设备的双向字节通道。"""

    __slots__ = ("_on_data", "_on_disconnect")

    def __init__(self) -> None:
        self._on_data: DataCallback | None = None
        self._on_disconnect: DisconnectCallback | None = None

    @property
    @abc.abstractmethod
    def device_id(self) -> str:
        """稳定标识这条通道的对端。

        跨平台不可移植：macOS 上是 CoreBluetooth 分配的 UUID，Linux/Windows
        上是 MAC 地址。上层只应把它当作不透明字符串。
        """

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @abc.abstractmethod
    async def connect(self) -> None:
        """建立连接并准备好收发。失败时抛 :class:`~wt901.errors.TransportError`。"""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """断开并释放资源。已断开时调用应当无副作用。"""

    @abc.abstractmethod
    async def write(self, data: bytes) -> None:
        """把字节发给设备。"""

    async def read_rssi(self) -> int | None:
        """连接期的链路信号强度，单位 dBm；这条通道给不出时为 ``None``。

        **不是抽象方法，默认就是 ``None``。** 信号强度是物理链路的属性，只有真
        实无线传输才可能有；内存传输与录制回放没有链路可测，让它们各写一遍
        ``return None`` 只是噪音。第三方传输不实现它也不会因此坏掉。

        ``None`` 的意思是「这个量拿不到」，**不是「信号为零」**——与
        :attr:`~wt901.models.Battery.percent`、
        :attr:`~wt901.models.Quaternion.is_plausible` 同一条规矩：拿不准的量不
        给一个看着正常的数。0 dBm 是极强信号，把「读不到」写成 0 会让链路看起来
        比实际好得多。

        实现者须知：这个方法**不得抛异常**。超时、后端不支持、链路已断——全部
        落成 ``None``。调用它的是周期轮询，一次读失败不该让轮询任务终止。
        """
        return None

    def on_data(self, callback: DataCallback | None) -> None:
        """注册接收回调。传 ``None`` 注销。"""
        self._on_data = callback

    def on_disconnect(self, callback: DisconnectCallback | None) -> None:
        """注册断连回调。传 ``None`` 注销。"""
        self._on_disconnect = callback

    def _emit_data(self, data: bytes) -> None:
        """子类收到字节时调用。"""
        if self._on_data is not None:
            self._on_data(data)

    def _emit_disconnect(self) -> None:
        """子类检测到断连时调用。"""
        if self._on_disconnect is not None:
            self._on_disconnect()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # 异常路径同样要释放：BLE 连接不会因为进程里抛了个异常就自己关掉，
        # 泄漏的连接会让下一次 connect 直接失败。
        await self.disconnect()
