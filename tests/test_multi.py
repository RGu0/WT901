"""多设备并发采集与合流测试。全部离线。

``t_host`` 在设备层取自 ``time.monotonic()``，合流的顺序完全由它决定，所以这里
用一个可控时钟替换 ``wt901.device`` 模块里的 ``time`` 名字——**不是**全局给
``time.monotonic`` 打补丁：asyncio 的事件循环也用它算超时，全局替换会让
``asyncio.wait`` 的等待预算跟着一起失真，测出来的合流行为将不是真实行为。
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest

import wt901.device
from wt901.device import WT901Device
from wt901.models import ImuSample
from wt901.multi import merge
from wt901.protocol.frames import HEADER, FrameFlag
from wt901.transport.memory import MemoryTransport

MOTIONLESS = (0, 0, 2048, 0, 0, 0, 0, 0, 0)


def data_frame(counts: tuple[int, ...] = MOTIONLESS) -> bytes:
    return bytes([HEADER, FrameFlag.DATA]) + struct.pack("<9h", *counts)


class Clock:
    """替代 ``wt901.device.time``，只需要提供 ``monotonic``。"""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(wt901.device, "time", fake)
    return fake


async def _pair() -> tuple[
    tuple[WT901Device, MemoryTransport], tuple[WT901Device, MemoryTransport]
]:
    left_transport = MemoryTransport("left-shank")
    right_transport = MemoryTransport("right-shank")
    left = WT901Device(left_transport)
    right = WT901Device(right_transport)
    await left.open()
    await right.open()
    return (left, left_transport), (right, right_transport)


async def _drain(stream: Any, limit: int) -> list[ImuSample]:
    collected: list[ImuSample] = []
    async for sample in stream.samples():
        collected.append(sample)
        if len(collected) == limit:
            break
    return collected


# ----- 并发采集 ------------------------------------------------------------


async def test_two_devices_capture_independently(clock: Clock) -> None:
    (left, left_t), (right, right_t) = await _pair()

    left_t.feed(data_frame())
    right_t.feed(data_frame())

    assert left.pending_samples == 1
    assert right.pending_samples == 1
    await left.close()
    await right.close()


async def test_each_sample_carries_its_own_source_device_id(clock: Clock) -> None:
    (left, left_t), (right, right_t) = await _pair()
    left_t.feed(data_frame())
    right_t.feed(data_frame())
    await left.close()
    await right.close()

    stream = merge([left, right], max_latency=None)
    samples = [sample async for sample in stream.samples()]

    assert {sample.device_id for sample in samples} == {"left-shank", "right-shank"}


async def test_one_device_closing_does_not_stop_the_other(clock: Clock) -> None:
    """验收标准：单台设备断连不影响另一台继续采集。"""
    (left, left_t), (right, right_t) = await _pair()

    left_t.drop()          # 左腿掉线
    await left.close()

    right_t.feed(data_frame())
    right_t.feed(data_frame())

    assert right.is_connected
    assert right.pending_samples == 2
    await right.close()


# ----- 合流顺序 ------------------------------------------------------------


async def test_merge_orders_by_t_host_not_by_arrival(clock: Clock) -> None:
    (left, left_t), (right, right_t) = await _pair()

    # 故意让到达顺序与 t_host 顺序相反。
    clock.now = 1.0
    left_t.feed(data_frame())
    clock.now = 0.5
    right_t.feed(data_frame())
    clock.now = 1.5
    left_t.feed(data_frame())
    clock.now = 2.0
    right_t.feed(data_frame())
    await left.close()
    await right.close()

    stream = merge([left, right], max_latency=None)
    samples = [sample async for sample in stream.samples()]

    assert [sample.t_host for sample in samples] == [0.5, 1.0, 1.5, 2.0]
    assert [sample.device_id for sample in samples] == [
        "right-shank",
        "left-shank",
        "left-shank",
        "right-shank",
    ]
    assert stream.stats.emitted == 4
    assert stream.stats.out_of_order == 0
    assert stream.stats.sources_finished == 2


async def test_merge_ends_when_every_source_ends(clock: Clock) -> None:
    (left, _), (right, _) = await _pair()
    await left.close()
    await right.close()

    stream = merge([left, right], max_latency=None)
    assert [sample async for sample in stream.samples()] == []
    assert stream.stats.sources_finished == 2


# ----- 有界延迟：这才是活性与顺序的取舍所在 --------------------------------


async def test_a_silent_source_does_not_stall_the_merge(clock: Clock) -> None:
    """一台设备安静下来时，另一台的样本仍要流出去——最多晚 max_latency。"""
    (left, _left_t), (right, right_t) = await _pair()

    clock.now = 1.0
    right_t.feed(data_frame())
    clock.now = 1.01
    right_t.feed(data_frame())

    stream = merge([left, right], max_latency=0.01)
    samples = await asyncio.wait_for(_drain(stream, 2), timeout=2.0)

    assert [sample.device_id for sample in samples] == ["right-shank"] * 2
    assert stream.stats.latency_flushes >= 1
    await left.close()
    await right.close()


async def test_strict_merge_does_stall_on_a_silent_source(clock: Clock) -> None:
    """把 max_latency=None 的代价钉住：它换来确定性，代价是活性。

    这条测试存在的意义是让「严格归并不适合真实设备」成为可执行的事实，而不是
    文档里的一句提醒。
    """
    (left, _left_t), (right, right_t) = await _pair()
    clock.now = 1.0
    right_t.feed(data_frame())

    stream = merge([left, right], max_latency=None)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_drain(stream, 1), timeout=0.2)

    await left.close()
    await right.close()


async def test_out_of_order_is_counted_not_hidden(clock: Clock) -> None:
    """超时路径会放走顺序，但必须留下痕迹。

    一个悄悄乱序的合流比一个会卡住的合流更难查：上层拿到的是看着正常、时序却
    错的数据。
    """
    (left, left_t), (right, right_t) = await _pair()

    clock.now = 5.0
    right_t.feed(data_frame())

    stream = merge([left, right], max_latency=0.01)
    iterator = stream.samples()

    first = await asyncio.wait_for(anext(iterator), timeout=2.0)
    assert first.t_host == 5.0          # 左腿没说话，超时后先发了它

    clock.now = 1.0                     # 左腿迟到，且时刻更早
    left_t.feed(data_frame())
    second = await asyncio.wait_for(anext(iterator), timeout=2.0)

    assert second.t_host == 1.0
    assert stream.stats.out_of_order == 1
    await iterator.aclose()
    await left.close()
    await right.close()


async def test_negative_latency_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="max_latency 不能为负"):
        merge([], max_latency=-1.0)


async def test_leaving_the_merge_early_releases_the_sources(clock: Clock) -> None:
    """提前 break 不能留下挂在设备队列上的取样任务。"""
    (left, left_t), (right, right_t) = await _pair()
    clock.now = 1.0
    left_t.feed(data_frame())
    right_t.feed(data_frame())

    stream = merge([left, right], max_latency=0.01)
    async for _ in stream.samples():
        break

    await left.close()
    await right.close()
    assert stream.stats.emitted == 1
