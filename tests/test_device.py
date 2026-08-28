"""设备门面测试。全部离线：用 MemoryTransport 喂字节，端到端跑通。

这正是 protocol 层零 I/O 与 transport 抽象换来的东西——生命周期、背压、重连
这三块最容易出错的逻辑，不需要硬件就能验。
"""

from __future__ import annotations

import asyncio
import logging
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


# ----- 重连：配置重放失败 --------------------------------------------------


def _drained_events(device: WT901Device) -> list[ConnectionEvent]:
    """把已入队的事件全部取出来。

    ``events()`` 要等到下一个事件才会返回，断言「到此为止的完整序列」用它会挂住。
    """
    events: list[ConnectionEvent] = []
    while not device._events.empty():
        event = device._events.get_nowait()
        if event is not None:
            events.append(event)
    return events


async def test_config_replay_failure_disconnects_and_retries() -> None:
    """连着但配置没恢复比断开更危险：数据照流、没有一处报错。"""
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(initial_delay=0.01, max_delay=0.01),
    )
    calls = 0

    async def hook() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionLostError("重连后链路又掉了")

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0.08)

    assert calls == 2
    assert transport.connect_calls == 3  # open + 两次重连尝试
    assert transport.disconnect_calls == 1  # 重放失败的那条连接被主动断掉
    assert device.is_connected
    # 失败那次不计入：统计说成功而事件说没成功，正是这个 bug 的样子。
    assert device.stats.reconnects == 1

    events = _drained_events(device)
    assert [e.state for e in events] == [
        ConnectionState.CONNECTED,
        ConnectionState.DISCONNECTED,
        ConnectionState.RECONNECTING,
        ConnectionState.CONFIG_REPLAY_FAILED,
        ConnectionState.RECONNECTING,
        ConnectionState.CONNECTED,
    ]
    failure = events[3]
    assert failure.attempt == 1
    assert failure.error == "重连后链路又掉了"
    await device.close()


async def test_config_replay_failure_does_not_escape_the_task() -> None:
    """此前异常直接冲出 _reconnect_loop，只剩 asyncio 在 GC 时打的一行日志。"""
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(
            initial_delay=0.01, max_delay=0.01, max_attempts=2
        ),
    )

    async def hook() -> None:
        raise ConnectionLostError("重连后链路又掉了")

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0)  # 让重连任务建起来
    task = device._reconnect_task
    assert task is not None

    await asyncio.sleep(0.1)
    assert task.done()
    assert task.exception() is None
    assert not device.is_connected  # 没留下一条配置未恢复的连接
    assert device.stats.reconnects == 0

    states = [event.state for event in _drained_events(device)]
    assert states.count(ConnectionState.CONFIG_REPLAY_FAILED) == 2
    assert states[-1] is ConnectionState.RECONNECT_FAILED
    await device.close()


async def test_config_replay_failure_is_logged_by_this_library(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """asyncio 那条 GC 期日志不算痕迹：调用方没配 logging 就完全看不到。"""
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(
            initial_delay=0.01, max_delay=0.01, max_attempts=1
        ),
    )

    async def hook() -> None:
        raise ConnectionLostError("重连后链路又掉了")

    device.on_reconnect(hook)
    with caplog.at_level(logging.WARNING, logger="wt901.device"):
        transport.drop()
        await asyncio.sleep(0.08)

    messages = [record.getMessage() for record in caplog.records]
    assert any("配置重放失败" in message for message in messages)
    assert any("重连后链路又掉了" in message for message in messages)
    await device.close()


# ----- 连接就绪前的样本门控 ------------------------------------------------


async def test_frames_during_config_replay_are_dropped_and_counted() -> None:
    """重放期间设备还按上电状态（出厂 10 Hz）在推，那段数据不该混进序列。

    重放每条寄存器约 0.2 s，200 Hz 下就是约 40 个样本——窗口随配置条数增长，
    与链路好坏无关。
    """
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(initial_delay=0.01, max_delay=0.01),
    )
    transport.feed(data_frame() * 2)  # 旧连接的两个样本

    async def hook() -> None:
        # 重放进行中：链路已连上，帧照常到达。
        transport.feed(data_frame() * 3)
        await asyncio.sleep(0.01)
        transport.feed(data_frame() * 2)

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0.08)

    assert device.stats.dropped_before_connected == 5
    assert device.stats.samples == 2  # 只有旧连接那两个进了队
    assert device.stats.frames == 7  # 帧确实都到了，照常计数
    assert device.stats.dropped_samples == 0  # 与「消费者跟不上」不是一回事

    transport.feed(data_frame())
    samples = await _take(device, 3)
    # 被丢的 5 个没有推进 seq：新连接的第一个交付样本仍是 0。
    assert [s.seq for s in samples] == [0, 1, 0]  # type: ignore[attr-defined]
    await device.close()


async def test_frames_arriving_during_open_are_dropped_and_counted() -> None:
    """真机上 start_notify 在 connect() 里，通知可能在 open() 返回前就到。

    首连与重连用同一个标志、同一条不变式——「重连管、首连不管」这种不对称迟早
    被误读成「首连的样本可以早于事件」。
    """

    class ChattyOnConnect(MemoryTransport):
        """connect() 内部就开始推数据的传输。"""

        async def connect(self) -> None:
            await super().connect()
            self.feed(data_frame() * 2)

    transport = ChattyOnConnect("eager")
    device = WT901Device(transport)
    await device.open()

    assert device.stats.dropped_before_connected == 2
    assert device.stats.samples == 0
    assert device.stats.frames == 2

    transport.feed(data_frame())
    (sample,) = await _take(device, 1)
    assert sample.seq == 0  # type: ignore[attr-defined]
    await device.close()


async def test_register_responses_still_dispatch_while_gated() -> None:
    """门控只挡实时数据帧。

    重放本身就跑在这条链路上：把 `0x71` 回帧一并挡住，会把这个修复变成新的死锁。
    """
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(initial_delay=0.01, max_delay=0.01),
    )
    seen: list[int] = []
    device.on_register_response(lambda response: seen.append(response.start_register))

    async def hook() -> None:
        transport.feed(register_frame(0x03, (0x0008,) * 8))
        await asyncio.sleep(0.01)

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0.08)

    assert seen == [0x03]
    assert device.stats.register_frames == 1
    assert device.stats.dropped_before_connected == 0
    await device.close()


async def test_gate_stays_shut_when_config_replay_fails() -> None:
    """RAY-306 的失败路径：链路被主动断开，闸门不得漏开。"""
    device, transport = await _opened(
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(
            initial_delay=0.01, max_delay=0.01, max_attempts=1
        ),
    )

    async def hook() -> None:
        raise ConnectionLostError("重连后链路又掉了")

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0.08)

    states = [event.state for event in _drained_events(device)]
    assert states[-1] is ConnectionState.RECONNECT_FAILED

    transport.feed(data_frame())
    assert device.stats.dropped_before_connected == 1
    assert device.stats.samples == 0
    await device.close()


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
