"""BLE 传输测试。全部离线，靠假客户端驱动。"""

from __future__ import annotations

import functools
from typing import Any

import pytest

from fakes import FakeCharacteristic, FakeClient, FakeService
from wt901.errors import (
    ConnectionLostError,
    DeviceNotFoundError,
    TransportError,
    TransportTimeoutError,
)
from wt901.transport.ble import (
    NOTIFY_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    WRITE_CHARACTERISTIC_UUID,
    BleTransport,
)


def _transport(**client_kwargs: Any) -> tuple[BleTransport, list[FakeClient]]:
    """建一个传输，同时把它造出来的假客户端暴露给断言。"""
    created: list[FakeClient] = []

    def factory(address: str, timeout: float, on_disconnect: Any) -> FakeClient:
        client = FakeClient(address, timeout, on_disconnect, **client_kwargs)
        created.append(client)
        return client

    return BleTransport("AA:BB:CC:DD:EE:FF", client_factory=factory), created


async def test_connect_subscribes_notifications() -> None:
    transport, clients = _transport()
    await transport.connect()
    assert transport.is_connected
    assert clients[0].calls == ["connect", "start_notify"]
    await transport.disconnect()


async def test_received_bytes_reach_the_callback() -> None:
    transport, clients = _transport()
    received: list[bytes] = []
    transport.on_data(received.append)
    await transport.connect()

    clients[0].push(b"\x55\x61\x01\x02")
    assert received == [b"\x55\x61\x01\x02"]
    assert isinstance(received[0], bytes), "回调拿到的必须是不可变 bytes"
    await transport.disconnect()


async def test_write_reaches_the_characteristic() -> None:
    transport, clients = _transport()
    await transport.connect()
    await transport.write(bytes.fromhex("ffaa6988b5"))
    assert clients[0].writes == [bytes.fromhex("ffaa6988b5")]
    await transport.disconnect()


async def test_write_before_connect_is_rejected() -> None:
    transport, _ = _transport()
    with pytest.raises(ConnectionLostError):
        await transport.write(b"\x00")


async def test_missing_service_names_the_uuid() -> None:
    """型号不对时，异常必须说清楚缺的是哪个 UUID。"""
    transport, clients = _transport(services=[])
    with pytest.raises(DeviceNotFoundError, match=SERVICE_UUID):
        await transport.connect()
    # 服务不对也要把连接关掉，否则下一次 connect 会因为一个看似无关的原因失败。
    assert "disconnect" in clients[0].calls
    assert not transport.is_connected


async def test_missing_write_characteristic_names_the_uuid() -> None:
    services = [
        FakeService(
            uuid=SERVICE_UUID,
            characteristics=[FakeCharacteristic(NOTIFY_CHARACTERISTIC_UUID)],
        )
    ]
    transport, clients = _transport(services=services)
    with pytest.raises(DeviceNotFoundError, match=WRITE_CHARACTERISTIC_UUID):
        await transport.connect()
    assert "disconnect" in clients[0].calls


async def test_missing_notify_characteristic_names_the_uuid() -> None:
    services = [
        FakeService(
            uuid=SERVICE_UUID,
            characteristics=[FakeCharacteristic(WRITE_CHARACTERISTIC_UUID)],
        )
    ]
    transport, _ = _transport(services=services)
    with pytest.raises(DeviceNotFoundError, match=NOTIFY_CHARACTERISTIC_UUID):
        await transport.connect()


async def test_service_uuid_matching_is_case_insensitive() -> None:
    services = [
        FakeService(
            uuid=SERVICE_UUID.upper(),
            characteristics=[
                FakeCharacteristic(NOTIFY_CHARACTERISTIC_UUID.upper()),
                FakeCharacteristic(WRITE_CHARACTERISTIC_UUID.upper()),
            ],
        )
    ]
    transport, _ = _transport(services=services)
    await transport.connect()
    assert transport.is_connected
    await transport.disconnect()


async def test_connect_timeout_maps_to_transport_timeout() -> None:
    transport, _ = _transport(connect_error=TimeoutError())
    with pytest.raises(TransportTimeoutError):
        await transport.connect()


async def test_connect_failure_maps_to_transport_error() -> None:
    transport, _ = _transport(connect_error=RuntimeError("adapter off"))
    with pytest.raises(TransportError, match="adapter off"):
        await transport.connect()


async def test_start_notify_failure_releases_the_connection() -> None:
    transport, clients = _transport(notify_error=RuntimeError("busy"))
    with pytest.raises(TransportError, match="busy"):
        await transport.connect()
    assert "disconnect" in clients[0].calls
    assert not transport.is_connected


async def test_write_failure_maps_to_transport_error() -> None:
    transport, _ = _transport(write_error=RuntimeError("gatt busy"))
    await transport.connect()
    with pytest.raises(TransportError, match="gatt busy"):
        await transport.write(b"\x00")
    await transport.disconnect()


async def test_disconnect_releases_both_notify_and_connection() -> None:
    transport, clients = _transport()
    await transport.connect()
    await transport.disconnect()
    assert clients[0].calls == ["connect", "start_notify", "stop_notify", "disconnect"]
    assert not transport.is_connected


async def test_failing_stop_notify_still_disconnects() -> None:
    """清理的两步互不阻塞：一次失败的 stop_notify 不能永久留下一条连接。"""
    transport, clients = _transport(stop_notify_error=RuntimeError("already stopped"))
    await transport.connect()
    await transport.disconnect()
    assert "disconnect" in clients[0].calls
    assert not transport.is_connected


async def test_disconnect_is_idempotent() -> None:
    transport, clients = _transport()
    await transport.connect()
    await transport.disconnect()
    await transport.disconnect()
    assert clients[0].calls.count("disconnect") == 1


async def test_context_manager_disconnects_on_exception() -> None:
    """异常路径也要释放：BLE 连接不会因为进程里抛了个异常就自己关掉。"""
    transport, clients = _transport()
    with pytest.raises(ValueError):
        async with transport:
            raise ValueError("boom")
    assert "disconnect" in clients[0].calls
    assert not transport.is_connected


async def test_peer_disconnect_fires_callback() -> None:
    """断连回调是设备层实现重连的依据（RAY-170）。"""
    transport, clients = _transport()
    events: list[str] = []
    transport.on_disconnect(functools.partial(events.append, "dropped"))
    await transport.connect()

    clients[0].drop()
    assert events == ["dropped"]
    assert not transport.is_connected


async def test_connect_is_idempotent_while_connected() -> None:
    transport, clients = _transport()
    await transport.connect()
    await transport.connect()
    assert len(clients) == 1
    await transport.disconnect()


async def test_device_id_is_the_address() -> None:
    transport, _ = _transport()
    assert transport.device_id == "AA:BB:CC:DD:EE:FF"
