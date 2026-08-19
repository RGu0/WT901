"""BLE 5.0 传输（bleak）。

GATT 接入点取自官方 SDK 源码（Android ``BleUUID.java``、Python ``device_model.py``
一致）：

===========  =========================================
用途         UUID
===========  =========================================
Service      ``0000ffe5-0000-1000-8000-00805f9a34fb``
Notify       ``0000ffe4-0000-1000-8000-00805f9a34fb``
Write        ``0000ffe9-0000-1000-8000-00805f9a34fb``
===========  =========================================
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from wt901.discovery import DiscoveredDevice
from wt901.errors import (
    ConnectionLostError,
    DeviceNotFoundError,
    TransportError,
    TransportTimeoutError,
)
from wt901.transport.base import Transport

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_WRITE_TIMEOUT",
    "NOTIFY_CHARACTERISTIC_UUID",
    "SERVICE_UUID",
    "WRITE_CHARACTERISTIC_UUID",
    "BleTransport",
]

SERVICE_UUID = "0000ffe5-0000-1000-8000-00805f9a34fb"
NOTIFY_CHARACTERISTIC_UUID = "0000ffe4-0000-1000-8000-00805f9a34fb"
WRITE_CHARACTERISTIC_UUID = "0000ffe9-0000-1000-8000-00805f9a34fb"

DEFAULT_CONNECT_TIMEOUT = 15.0
"""秒。与官方 Python 示例一致。"""

DEFAULT_WRITE_TIMEOUT = 5.0
"""秒。单条 5 字节指令的 GATT 写正常在几十毫秒内完成，5 秒是宽松上限。

存在的意义不是催促慢链路，而是把「永远不返回」变成「失败」——真机上并发写同一
特征时，bleak 的 CoreBluetooth 后端会让其中一条永久挂起，且不在任何超时之内。
"""

_LOGGER = logging.getLogger(__name__)


class _Characteristic(Protocol):
    @property
    def uuid(self) -> str: ...


class _Service(Protocol):
    @property
    def uuid(self) -> str: ...
    @property
    def characteristics(self) -> Iterable[_Characteristic]: ...


NotifyCallback = Callable[[Any, bytearray], Awaitable[None] | None]
"""bleak 允许通知回调是同步或异步的；这里保持同样的宽度，否则 ``BleakClient``
无法满足下面的协议。本传输自己注册的是同步回调——通知处理必须尽快返回。"""


class BleakClientLike(Protocol):
    """本传输实际用到的 ``BleakClient`` 子集。

    显式写出这个协议，测试才能注入一个假客户端，把「服务缺失」「写入失败」
    「断连」这些路径在没有硬件时跑通——那些正是最需要测、又最难在真机上复现
    的分支。
    """

    @property
    def is_connected(self) -> bool: ...
    @property
    def services(self) -> Iterable[_Service]: ...
    async def connect(self) -> Any: ...
    async def disconnect(self) -> Any: ...
    # 参数声明为 positional-only：bleak 把第一个参数叫 char_specifier，协议里
    # 叫别的名字就会被判为不兼容。我们只按位置调用，名字不该成为约束。
    async def start_notify(
        self, characteristic: Any, callback: NotifyCallback, /
    ) -> None: ...
    async def stop_notify(self, characteristic: Any, /) -> None: ...
    async def write_gatt_char(self, characteristic: Any, data: bytes, /) -> None: ...


ClientFactory = Callable[[Any, float, Callable[[Any], None]], BleakClientLike]
"""``(target, timeout, disconnected_callback) -> client``。

``target`` 是扫描得到的平台句柄，或退而求其次的地址字符串——bleak 两者都接受，
但只有句柄是可靠的。
"""


def _resolve_target(target: str | DiscoveredDevice) -> tuple[str, Any]:
    """把连接目标拆成 ``(用于展示的地址, 交给 bleak 的对象)``。

    ``device_id`` 始终取地址字符串：它稳定、可打印、可写进日志与证据；句柄只在
    本次扫描会话内有效，不适合当标识。
    """
    if isinstance(target, DiscoveredDevice):
        return target.address, (target.handle if target.handle is not None else target.address)
    return target, target


def _default_client_factory(
    target: Any, timeout: float, disconnected_callback: Callable[[Any], None]
) -> BleakClientLike:
    # 延迟 import：bleak 会在导入时初始化平台后端，把它推迟到真正要连接的时
    # 候，模块本身就能在任何环境下被导入（例如只跑协议层测试的 CI）。
    from bleak import BleakClient

    client: BleakClientLike = BleakClient(
        target, timeout=timeout, disconnected_callback=disconnected_callback
    )
    return client


class BleTransport(Transport):
    """通过 BLE 5.0 与 WT9011DCL-BT50 收发字节。"""

    __slots__ = (
        "_address",
        "_client",
        "_client_factory",
        "_handle",
        "_notify_characteristic",
        "_timeout",
        "_write_characteristic",
        "write_timeout",
    )

    def __init__(
        self,
        target: str | DiscoveredDevice,
        *,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
        client_factory: ClientFactory = _default_client_factory,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
    ) -> None:
        """``target`` 可以是 :class:`~wt901.discovery.DiscoveredDevice` 或地址字符串。

        **优先传 `DiscoveredDevice`。** 它携带扫描得到的平台句柄，可以直接交给
        bleak；只给地址字符串时 bleak 需要自己再扫一遍做地址→句柄解析，而 macOS
        上的地址只是 CoreBluetooth 分配的会话内标识，跨扫描会话解析并不可靠——
        失败时报的是「设备未找到」，哪怕设备就在眼前、信号很强。

        地址字符串路径保留给「已经知道地址、不想先扫描」的场景，但要接受它可能
        连不上并需要退回先扫描。
        """
        super().__init__()
        self._address, self._handle = _resolve_target(target)
        self._timeout = timeout
        self.write_timeout = write_timeout
        self._client_factory = client_factory
        self._client: BleakClientLike | None = None
        self._notify_characteristic: Any = None
        self._write_characteristic: Any = None

    @property
    def device_id(self) -> str:
        """连接时给定的地址。

        macOS 上 bleak 给出的是 CoreBluetooth UUID，Linux/Windows 上是 MAC。
        同一台设备在不同主机上的 ``device_id`` 不同，不要跨主机持久化它。
        """
        return self._address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        if self.is_connected:
            return
        # 交给客户端的是句柄（若有），不是地址字符串——这正是本类接受
        # DiscoveredDevice 的全部意义。
        client = self._client_factory(
            self._handle, self._timeout, self._handle_disconnected
        )
        try:
            await client.connect()
        except TimeoutError as exc:
            raise TransportTimeoutError(
                f"连接 {self._address} 超时（{self._timeout}s）"
            ) from exc
        except Exception as exc:  # bleak 的异常类型随后端而异
            raise TransportError(f"连接 {self._address} 失败：{exc}") from exc

        try:
            notify, write = _resolve_characteristics(client.services)
        except DeviceNotFoundError:
            # 服务不对就不要把连接留在那里：泄漏的连接会让下一次 connect 失败，
            # 而那次失败的原因看起来跟真正的问题毫无关系。
            await _suppress(client.disconnect(), "disconnect")
            raise

        self._client = client
        self._notify_characteristic = notify
        self._write_characteristic = write

        try:
            await client.start_notify(notify, self._handle_notification)
        except Exception as exc:
            await self.disconnect()
            raise TransportError(f"订阅通知失败：{exc}") from exc

    async def disconnect(self) -> None:
        client = self._client
        if client is None:
            return
        # 两步都用 best-effort：任何一步失败都不能阻止另一步，否则一次失败的
        # stop_notify 会永久留下一条打开的连接。
        if self._notify_characteristic is not None:
            await _suppress(
                client.stop_notify(self._notify_characteristic), "stop_notify"
            )
        await _suppress(client.disconnect(), "disconnect")
        self._client = None
        self._notify_characteristic = None
        self._write_characteristic = None

    async def write(self, data: bytes) -> None:
        """把字节发给设备。

        写入受 :attr:`write_timeout` 保护。``Transport`` 抽象承诺「写入要么完成
        要么抛异常」，而一个永不返回的 GATT 写违背了这个承诺——真机上确实会发生：
        并发写同一特征时，bleak 的 CoreBluetooth 后端只维护一个待完成写入的
        future，其中一个会永远得不到回调。上层已改为串行化寄存器事务（RAY-177），
        这里的超时是第二道防线：即使将来又出现并发写，也只会失败，不会静默挂死。
        """
        client = self._client
        if client is None or self._write_characteristic is None:
            raise ConnectionLostError("传输未连接，写入被拒绝")
        try:
            await asyncio.wait_for(
                client.write_gatt_char(self._write_characteristic, bytes(data)),
                self.write_timeout,
            )
        except TimeoutError as exc:
            raise TransportTimeoutError(
                f"GATT 写入超时（{self.write_timeout}s）：{data.hex(' ')}"
            ) from exc
        except Exception as exc:
            raise TransportError(f"写入失败：{exc}") from exc

    def _handle_notification(self, _sender: Any, data: bytearray) -> None:
        self._emit_data(bytes(data))

    def _handle_disconnected(self, _client: Any) -> None:
        self._emit_disconnect()


def _resolve_characteristics(services: Iterable[_Service]) -> tuple[Any, Any]:
    """在服务表里定位读写特征。

    缺什么就在异常里说清楚缺的是哪个 UUID——设备型号不对或固件不同的时候，
    「找不到设备」和「找到了但少一个特征」是完全不同的两件事。
    """
    for service in services:
        if service.uuid.lower() != SERVICE_UUID:
            continue
        notify: Any = None
        write: Any = None
        for characteristic in service.characteristics:
            uuid = characteristic.uuid.lower()
            if uuid == NOTIFY_CHARACTERISTIC_UUID:
                notify = characteristic
            elif uuid == WRITE_CHARACTERISTIC_UUID:
                write = characteristic
        missing = [
            uuid
            for uuid, found in (
                (NOTIFY_CHARACTERISTIC_UUID, notify),
                (WRITE_CHARACTERISTIC_UUID, write),
            )
            if found is None
        ]
        if missing:
            raise DeviceNotFoundError(
                f"服务 {SERVICE_UUID} 存在，但缺少特征：{', '.join(missing)}"
            )
        return notify, write
    raise DeviceNotFoundError(f"设备上没有服务 {SERVICE_UUID}")


async def _suppress(awaitable: Any, action: str) -> None:
    """尽力而为地 await：清理路径上的失败不应掩盖真正的错误。

    失败降级为 debug 日志而不是彻底丢弃——「断开时到底发生了什么」在排查连接
    泄漏时是关键线索，但它本身不该让调用方的 ``disconnect()`` 抛异常。
    """
    try:
        await awaitable
    except Exception:
        _LOGGER.debug("清理阶段 %s 失败，已忽略", action, exc_info=True)
