"""传输抽象与内存实现的测试。

内存实现是后续 scope（RAY-170 设备门面、RAY-171 寄存器事务）唯一的离线验证
手段，所以它自己的行为也得有测试兜住。
"""

from __future__ import annotations

import pytest

from wt901.errors import ConnectionLostError
from wt901.transport.memory import MemoryTransport


async def test_lifecycle_is_recorded() -> None:
    transport = MemoryTransport()
    assert not transport.is_connected
    await transport.connect()
    assert transport.is_connected
    await transport.disconnect()
    assert not transport.is_connected
    assert transport.connect_calls == 1
    assert transport.disconnect_calls == 1


async def test_writes_are_recorded_in_order() -> None:
    """后续 scope 要靠它逐字节断言「解锁 → 写 → 保存」的完整序列。"""
    transport = MemoryTransport()
    await transport.connect()
    await transport.write(bytes.fromhex("ffaa6988b5"))
    await transport.write(bytes.fromhex("ffaa030800"))
    await transport.write(bytes.fromhex("ffaa000000"))
    assert transport.writes == [
        bytes.fromhex("ffaa6988b5"),
        bytes.fromhex("ffaa030800"),
        bytes.fromhex("ffaa000000"),
    ]
    assert transport.written == bytes.fromhex("ffaa6988b5ffaa030800ffaa000000")


async def test_write_while_disconnected_is_rejected() -> None:
    transport = MemoryTransport()
    with pytest.raises(ConnectionLostError):
        await transport.write(b"\x00")


async def test_feed_delivers_to_callback() -> None:
    transport = MemoryTransport()
    received: list[bytes] = []
    transport.on_data(received.append)
    transport.feed(b"\x55\x61")
    assert received == [b"\x55\x61"]


async def test_callback_can_be_detached() -> None:
    transport = MemoryTransport()
    received: list[bytes] = []
    transport.on_data(received.append)
    transport.on_data(None)
    transport.feed(b"\x55")
    assert received == []


async def test_feed_without_callback_is_not_an_error() -> None:
    """连接建立与回调注册之间有窗口，此时到达的字节不该让程序崩掉。"""
    MemoryTransport().feed(b"\x55")


async def test_drop_fires_disconnect_callback() -> None:
    transport = MemoryTransport()
    events: list[str] = []
    transport.on_disconnect(lambda: events.append("dropped"))
    await transport.connect()
    transport.drop()
    assert events == ["dropped"]
    assert not transport.is_connected


async def test_context_manager_disconnects_on_exception() -> None:
    transport = MemoryTransport()
    with pytest.raises(ValueError):
        async with transport:
            raise ValueError("boom")
    assert transport.disconnect_calls == 1
    assert not transport.is_connected


async def test_device_id_is_stable() -> None:
    transport = MemoryTransport("left-shank")
    assert transport.device_id == "left-shank"
