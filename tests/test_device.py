"""设备门面测试。全部离线：用 MemoryTransport 喂字节，端到端跑通。

这正是 protocol 层零 I/O 与 transport 抽象换来的东西——生命周期、背压、重连
这三块最容易出错的逻辑，不需要硬件就能验。
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from conftest import register_frame
from wt901.device import (
    ConnectionEvent,
    ConnectionState,
    OutputMode,
    ReconnectPolicy,
    WT901Device,
)
from wt901.errors import ConfigurationError, ConnectionLostError
from wt901.protocol.frames import HEADER, FrameFlag
from wt901.protocol.units import STANDARD_GRAVITY
from wt901.transport.memory import MemoryTransport

MOTIONLESS = (0, 0, 2048, 0, 0, 0, 0, 0, 0)
"""Z 轴 1 g，其余为零。"""


def data_frame(counts: tuple[int, ...] = MOTIONLESS) -> bytes:
    return bytes([HEADER, FrameFlag.DATA]) + struct.pack("<9h", *counts)


async def _opened(**kwargs: object) -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("left-shank")
    device = WT901Device(transport, **kwargs)  # type: ignore[arg-type]
    await device.open()
    return device, transport


async def _take(device: WT901Device, count: int) -> list[object]:
    collected: list[object] = []
    async for sample in device.samples():
        collected.append(sample)
        if len(collected) == count:
            break
    return collected


# ----- 端到端 --------------------------------------------------------------


async def test_bytes_become_samples() -> None:
    device, transport = await _opened()
    transport.feed(data_frame())
    (sample,) = await _take(device, 1)

    assert sample.device_id == "left-shank"  # type: ignore[attr-defined]
    assert sample.accel.z == pytest.approx(STANDARD_GRAVITY)  # type: ignore[attr-defined]
    assert sample.seq == 0  # type: ignore[attr-defined]
    await device.close()


async def test_seq_increments_and_t_host_is_monotonic() -> None:
    device, transport = await _opened()
    transport.feed(data_frame() * 3)
    samples = await _take(device, 3)

    assert [s.seq for s in samples] == [0, 1, 2]  # type: ignore[attr-defined]
    times = [s.t_host for s in samples]  # type: ignore[attr-defined]
    assert times == sorted(times)
    await device.close()


async def test_split_bytes_across_notifications() -> None:
    """一帧被拆到两条通知里——BLE 上完全正常。"""
    device, transport = await _opened()
    raw = data_frame()
    transport.feed(raw[:7])
    transport.feed(raw[7:])
    (sample,) = await _take(device, 1)
    assert sample.accel.z == pytest.approx(STANDARD_GRAVITY)  # type: ignore[attr-defined]
    await device.close()


async def test_stats_track_frames_and_resync() -> None:
    device, transport = await _opened()
    transport.feed(b"\x00\x01" + data_frame())
    await _take(device, 1)

    stats = device.stats
    assert stats.frames == 1
    assert stats.samples == 1
    assert stats.resync_count == 1
    assert stats.dropped_bytes == 2
    await device.close()


# ----- 背压 ----------------------------------------------------------------


async def test_slow_consumer_drops_oldest_and_counts_it() -> None:
    """采集不能因为消费者慢而阻塞，丢弃必须可观测。"""
    device, transport = await _opened(queue_size=4)
    transport.feed(data_frame() * 10)

    assert device.stats.dropped_samples == 6
    assert device.stats.samples == 10

    kept = await _take(device, 4)
    # 丢的是最旧的：留下的是最后 4 个 seq。
    assert [s.seq for s in kept] == [6, 7, 8, 9]  # type: ignore[attr-defined]
    await device.close()


async def test_pending_samples_reports_backlog() -> None:
    """配置变更后队列里积的就是「陈数据」，测量前得知道有多少。"""
    device, transport = await _opened(queue_size=16)
    assert device.pending_samples == 0
    transport.feed(data_frame() * 5)
    assert device.pending_samples == 5
    await _take(device, 5)
    assert device.pending_samples == 0
    await device.close()


async def test_queue_does_not_grow_without_bound() -> None:
    device, transport = await _opened(queue_size=8)
    for _ in range(200):
        transport.feed(data_frame())
    assert device.stats.dropped_samples == 192
    kept = await _take(device, 8)
    assert len(kept) == 8
    await device.close()


# ----- 寄存器帧 ------------------------------------------------------------


async def test_register_frames_reach_the_listener_and_are_counted() -> None:
    """0x71 与 0x61 混在同一条链路上，两者都不能被吞掉。"""
    device, transport = await _opened()
    seen: list[object] = []
    device.on_register_response(seen.append)

    values = (10, 20, 30, 40, 50, 60, 70, 80)
    transport.feed(data_frame() + register_frame(0x3A, values))
    await _take(device, 1)

    assert len(seen) == 1
    assert seen[0].start_register == 0x3A  # type: ignore[attr-defined]
    assert seen[0].values == values  # type: ignore[attr-defined]
    assert device.stats.register_frames == 1
    await device.close()


async def test_register_frames_without_listener_are_not_an_error() -> None:
    device, transport = await _opened()
    transport.feed(register_frame(0x51, (1, 2, 3, 4, 5, 6, 7, 8)))
    assert device.stats.register_frames == 1
    await device.close()


# ----- 生命周期 ------------------------------------------------------------


async def test_context_manager_closes_on_exception() -> None:
    device, transport = await _opened()
    with pytest.raises(ValueError):
        async with device:
            raise ValueError("boom")
    assert transport.disconnect_calls == 1
    assert not device.is_connected


async def test_close_is_idempotent() -> None:
    device, transport = await _opened()
    await device.close()
    await device.close()
    assert transport.disconnect_calls == 1


async def test_samples_iterator_ends_on_close() -> None:
    """没有哨兵的话，async for 会在 close 之后永远挂着。"""
    device, transport = await _opened()
    transport.feed(data_frame())

    collected = []

    async def consume() -> None:
        async for sample in device.samples():
            collected.append(sample)

    task = asyncio.get_running_loop().create_task(consume())
    await asyncio.sleep(0)
    await device.close()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(collected) == 1


async def test_bytes_after_close_are_ignored() -> None:
    device, transport = await _opened()
    await device.close()
    transport.feed(data_frame())
    assert device.stats.frames == 0


# ----- 重连 ----------------------------------------------------------------


async def test_reconnect_resets_seq_and_emits_events() -> None:
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(initial_delay=0.01, max_delay=0.01),
    )
    transport.feed(data_frame() * 2)
    assert device.stats.samples == 2

    transport.drop()
    await asyncio.sleep(0.05)

    assert device.is_connected
    assert device.stats.reconnects == 1

    transport.feed(data_frame())
    samples = await _take(device, 3)
    # 前两个来自旧连接，第三个来自新连接且 seq 已归零。
    assert [s.seq for s in samples] == [0, 1, 0]  # type: ignore[attr-defined]

    events: list[ConnectionEvent] = []
    async for event in device.events():
        events.append(event)
        if event.state is ConnectionState.CONNECTED and event.attempt > 0:
            break
    assert [e.state for e in events] == [
        ConnectionState.CONNECTED,
        ConnectionState.DISCONNECTED,
        ConnectionState.RECONNECTING,
        ConnectionState.CONNECTED,
    ]
    await device.close()


async def test_reconnect_replays_configuration() -> None:
    """BLE 断连不保证模块保留运行时状态，重连后必须重新下发配置。"""
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(initial_delay=0.01, max_delay=0.01),
    )
    replayed = 0

    async def hook() -> None:
        nonlocal replayed
        replayed += 1

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0.05)

    assert replayed == 1
    await device.close()


async def test_reconnect_clears_stale_partial_frame() -> None:
    """旧连接的半帧拼上新连接的字节会凑出长度合法但内容错位的帧。"""
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(initial_delay=0.01, max_delay=0.01),
    )
    transport.feed(data_frame()[:9])  # 半帧
    transport.drop()
    await asyncio.sleep(0.05)

    transport.feed(data_frame())
    (sample,) = await _take(device, 1)
    assert sample.accel.z == pytest.approx(STANDARD_GRAVITY)  # type: ignore[attr-defined]
    assert device.stats.frames == 1
    await device.close()


async def test_reconnect_disabled_by_default() -> None:
    device, transport = await _opened()
    transport.drop()
    await asyncio.sleep(0.03)
    assert not device.is_connected
    assert device.stats.reconnects == 0
    await device.close()


async def test_reconnect_gives_up_after_max_attempts() -> None:
    class RefusingTransport(MemoryTransport):
        async def connect(self) -> None:
            if self.connect_calls >= 1:
                self.connect_calls += 1
                # 真实传输把各后端异常收敛成 TransportError，测试替身照做——
                # 否则测的就不是设备层实际会遇到的情形。
                raise ConnectionLostError("device out of range")
            await super().connect()

    transport = RefusingTransport("far-away")
    device = WT901Device(
        transport,
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(
            initial_delay=0.01, max_delay=0.01, max_attempts=2
        ),
    )
    await device.open()
    transport.drop()
    await asyncio.sleep(0.1)

    assert not device.is_connected
    states = [
        event.state
        for event in [device._events.get_nowait() for _ in range(device._events.qsize())]
        if event is not None
    ]
    assert ConnectionState.RECONNECT_FAILED in states
    await device.close()


async def test_close_cancels_pending_reconnect() -> None:
    """否则重连任务可能在 disconnect 之后又把连接建起来。"""
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(initial_delay=5.0, max_delay=5.0),
    )
    transport.drop()
    await asyncio.sleep(0)
    await device.close()
    await asyncio.sleep(0.02)
    assert not device.is_connected


# ----- 位移模式 ------------------------------------------------------------


async def test_displacement_mode_refuses_to_guess() -> None:
    """位移帧与实时数据帧共用 0x61 且布局无法区分，不能按运动语义解析。"""
    device, _ = await _opened(output_mode=OutputMode.DISPLACEMENT)
    with pytest.raises(ConfigurationError, match="0x96"):
        await anext(aiter(device.samples()))
    await device.close()


async def test_displacement_mode_does_not_emit_motion_samples() -> None:
    device, transport = await _opened(output_mode=OutputMode.DISPLACEMENT)
    transport.feed(data_frame())
    assert device.stats.frames == 1
    assert device.stats.samples == 0
    await device.close()


async def test_write_passes_through_to_transport() -> None:
    device, transport = await _opened()
    await device.write(bytes.fromhex("ffaa6988b5"))
    assert transport.writes == [bytes.fromhex("ffaa6988b5")]
    await device.close()
