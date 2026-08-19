"""多设备并发采集：把若干台设备的样本流合成一条。

并发本身不需要这个模块——每台 :class:`~wt901.device.WT901Device` 各自持有传输、
解码器与队列，在同一个 event loop 上并行采集互不阻塞，样本自带 ``device_id``。
本模块解决的是**合流**：把 N 条流按 ``t_host`` 归并成一条。

## 为什么合流不能是严格的 k 路归并

严格归并要求「每条流都拿到下一个样本」才能确定谁最小。一台设备掉线或只是安静
了一会儿，整条合流就停住——而另一台仍在正常采集。这正是验收标准「单台设备断连
不影响另一台继续采集」要排除的行为。

所以这里用**有界延迟归并**：

* 每条活跃流都有待发样本时，取 ``t_host`` 最小的发出——与严格归并等价；
* 有流还没交出样本时，最多再等 ``max_latency``；超时就从已有的样本里发最小的。

代价是超时路径上可能发生乱序：某条慢流稍后交出一个更早的样本，而它已经错过了。
**这种乱序被记进** :attr:`MergeStats.out_of_order`，不做隐藏。一个悄悄乱序的合流
比一个会卡住的合流更难查——上层拿到的是看着正常、时序却错的数据。

``max_latency=None`` 恢复严格归并。它适用于回放这类**有限且不会中断**的流，那里
确定性比活性重要；对着真实设备用它，等于把「一台掉线」变成「整条流挂起」。

## 不做时钟同步补偿

``t_host`` 是主机收到 BLE 通知的时刻，含蓝牙栈抖动。两台设备之间的真实采样时刻
差无法从这个量恢复出来。本库只保证时间戳来源一致且单调，补偿是上层的职责——
这条边界在 RAY-167 的范围外清单里已经写明。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from wt901.device import WT901Device
from wt901.models import ImuSample

__all__ = ["DEFAULT_MAX_LATENCY", "MergeStats", "MergedStream", "merge"]

DEFAULT_MAX_LATENCY = 0.05
"""默认的等待预算，秒。

100 Hz 下样本间隔 10 ms，50 ms 相当于容忍五个样本周期的抖动。再大对上层没有
好处（延迟直接加在数据上），再小会让正常的 BLE 抖动被当成超时。
"""


@dataclass(slots=True)
class MergeStats:
    """合流质量。判断输出可不可信的入口。"""

    emitted: int = 0
    out_of_order: int = 0
    """``t_host`` 早于上一个已发样本的样本数。非零说明超时路径被走到了。"""
    latency_flushes: int = 0
    """等待预算超时的次数。"""
    sources_finished: int = 0


@dataclass(slots=True)
class _Source:
    """一条流以及它「已取出但尚未发出」的那个样本。"""

    iterator: AsyncIterator[ImuSample]
    task: asyncio.Task[ImuSample | None] | None = None
    sample: ImuSample | None = None
    arrived: float = 0.0
    finished: bool = False


async def _next_sample(iterator: AsyncIterator[ImuSample]) -> ImuSample | None:
    """取下一个样本；流结束时返回 ``None``。

    用返回值而不是让 ``StopAsyncIteration`` 穿过 Task 边界：那个异常在 Task 里
    是「任务失败」，与真正的失败混在一起，调用方得靠异常类型去区分正常结束。
    """
    try:
        return await anext(iterator)
    except StopAsyncIteration:
        return None


class MergedStream:
    """若干台设备的样本合成的单一流。"""

    __slots__ = ("_devices", "_last_t", "_max_latency", "_stats")

    def __init__(
        self,
        devices: Sequence[WT901Device],
        *,
        max_latency: float | None = DEFAULT_MAX_LATENCY,
    ) -> None:
        if max_latency is not None and max_latency < 0:
            raise ValueError("max_latency 不能为负；严格归并请传 None")
        self._devices = tuple(devices)
        self._max_latency = max_latency
        self._stats = MergeStats()
        self._last_t: float | None = None

    @property
    def stats(self) -> MergeStats:
        return self._stats

    async def samples(self) -> AsyncIterator[ImuSample]:
        """按 ``t_host`` 归并产出，直到所有设备的流都结束。"""
        loop = asyncio.get_running_loop()
        sources = [_Source(iterator=device.samples()) for device in self._devices]
        try:
            while True:
                live = [source for source in sources if not source.finished]
                if not live:
                    return

                for source in live:
                    if source.sample is None and source.task is None:
                        source.task = asyncio.ensure_future(
                            _next_sample(source.iterator)
                        )

                waiting = [source for source in live if source.sample is None]
                ready = [source for source in live if source.sample is not None]

                if waiting:
                    timeout = self._budget(ready, loop)
                    done, _ = await asyncio.wait(
                        [source.task for source in waiting if source.task is not None],
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    progressed = self._harvest(waiting, done, loop)
                    if progressed or not ready:
                        # 有流交出了样本或结束了，重新评估；此时可能已经凑齐。
                        continue
                    self._stats.latency_flushes += 1

                candidate = self._smallest(sources)
                if candidate is None:
                    continue
                source, sample = candidate
                yield self._take(source, sample)
        finally:
            await self._release(sources)

    def _budget(self, ready: list[_Source], loop: asyncio.AbstractEventLoop) -> float | None:
        """还能等多久。没有待发样本时无需设限——等下去不会让任何数据变旧。"""
        if not ready or self._max_latency is None:
            return None
        deadline = min(source.arrived for source in ready) + self._max_latency
        return max(0.0, deadline - loop.time())

    def _harvest(
        self,
        waiting: list[_Source],
        done: set[asyncio.Task[ImuSample | None]],
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        """收割已完成的取样任务。返回是否有任何进展。"""
        progressed = False
        for source in waiting:
            task = source.task
            if task is None or task not in done:
                continue
            source.task = None
            sample = task.result()
            if sample is None:
                source.finished = True
                self._stats.sources_finished += 1
            else:
                source.sample = sample
                source.arrived = loop.time()
            progressed = True
        return progressed

    @staticmethod
    def _smallest(sources: list[_Source]) -> tuple[_Source, ImuSample] | None:
        """待发样本中 ``t_host`` 最小的那个，连同它所属的流。

        返回 ``(流, 样本)`` 而不是只返回流，是为了让「这条流确实有样本」由类型
        承载。只返回流的话，取样本那一步就得再断言一次非空——一个永远不会触发、
        因而也永远不会被验证的断言。
        """
        candidates = [
            (source, source.sample)
            for source in sources
            if not source.finished and source.sample is not None
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda pair: pair[1].t_host)

    def _take(self, source: _Source, sample: ImuSample) -> ImuSample:
        source.sample = None
        if self._last_t is not None and sample.t_host < self._last_t:
            self._stats.out_of_order += 1
        else:
            self._last_t = sample.t_host
        self._stats.emitted += 1
        return sample

    @staticmethod
    async def _release(sources: list[_Source]) -> None:
        """取消未完成的取样任务并关闭迭代器。

        不做这一步的话，提前 break 出合流会留下一批还挂在设备队列上的任务，
        它们各自持有一个已经取出的样本——那些样本既没被发出，也不会回到队列。
        """
        for source in sources:
            task = source.task
            source.task = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # aclose 属于异步生成器而非 AsyncIterator 协议；如实探测，
            # 不假定调用方传进来的一定是生成器。
            close = getattr(source.iterator, "aclose", None)
            if close is not None:
                await close()


def merge(
    devices: Sequence[WT901Device],
    *,
    max_latency: float | None = DEFAULT_MAX_LATENCY,
) -> MergedStream:
    """把多台设备的样本流合成一条::

        stream = merge([left, right])
        async for sample in stream.samples():
            print(sample.device_id, sample.t_host)
        print(stream.stats)
    """
    return MergedStream(devices, max_latency=max_latency)
