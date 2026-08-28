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

## 等待预算属于样本，不属于流

一条流超过预算之后就被标记为**停滞**并移出等待集，直到它自己交出样本为止；这段
时间里合流按存活流全速推进。

这一条不是优化，是正确性。预算若按「每发一个样本就重新等一遍那条不说话的流」来
付，存活流的产出上限就变成绝对值 ``1 / max_latency``——默认配置下 20 Hz，与设备
实际速率无关。真机上关掉两台中的一台，存活那台从 198.6 Hz 塌到 19.5 Hz，其余
九成样本在设备队列里被丢掉（RAY-190）。它满足「另一台没有停」的字面要求，却把
一条十抽一的流交给上层，而且不报错。

停滞流恢复时可能造成乱序——那正是有界延迟归并本来就接受、且已被
:attr:`MergeStats.out_of_order` 计入的代价。

停滞**不是**结束：掉电设备的流并没有结束，重连策略仍在重试，所以它不计入
:attr:`MergeStats.sources_finished`。一条流停滞期间发出的样本计入
:attr:`MergeStats.emitted_while_stalled`——那段输出并没有真正在归并。

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
    """``t_host`` 早于**此前已发出的最大 ``t_host``** 的样本数。非零说明超时路径
    被走到了。

    基准是一条高水位线，不是「上一个已发样本」：乱序样本不推进这条线（机制见
    :meth:`MergedStream._take`），所以一串连续迟到的样本里的**每一个**都计一次，
    而不是整串算一次。两个定义能差好几倍——真实的抖动形状是「一条流卡住若干个
    采样周期，再把攒下的一次吐出」，按相邻逆序只算 1 处，按高水位则等于攒下的
    样本个数。

    选高水位是因为它数的是**调用方需要重排的样本总数**：要做时间对齐的下游，
    每一个越过水位线的样本都得单独处理一遍，相邻逆序数会低估这份工作量。代价
    是这个数不能读作「链路抖了几次」——那是 :attr:`latency_flushes`。
    """
    latency_flushes: int = 0
    """等待预算超时的次数。

    一条持续静默的流只贡献**一次**——超时后它就被移出等待集，不再每个样本付一次。
    所以这个数反映的是抖动频次，不会被一台掉线设备刷爆。
    """
    sources_finished: int = 0
    emitted_while_stalled: int = 0
    """至少有一条流处于停滞状态时发出的样本数。

    这段输出是单边的：它没有等齐所有流，顺序只在存活流之间成立。合流不会因此变慢
    或报错，所以**不看这个字段就发现不了**——它与 ``emitted`` 的比值就是「这次采集
    有多大一部分并没有真正在归并」。
    """


@dataclass(slots=True)
class _Source:
    """一条流以及它「已取出但尚未发出」的那个样本。"""

    iterator: AsyncIterator[ImuSample]
    task: asyncio.Task[ImuSample | None] | None = None
    sample: ImuSample | None = None
    arrived: float = 0.0
    finished: bool = False
    stalled: bool = False
    """已超出等待预算，暂不参与等待。它的取样任务仍在后台挂着。"""


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
        # ``0`` 是合法的，含义是「一个样本都不等」：每次都直接从手头已有的样本里
        # 发最小的，实际上等于不归并。它不被拒绝是因为它自曝——
        # ``emitted_while_stalled`` 会逼近 ``emitted``，看统计就能发现。想要的若是
        # 「等齐再发」，那是 ``None``（严格归并），不是 ``0``。
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
                # 先收下已经完成的取样。停滞流不进等待集，只能靠这一步回到归并——
                # 少了它，一条恢复了的流会被一直晾着。
                self._collect(sources, loop)

                live = [source for source in sources if not source.finished]
                if not live:
                    return

                for source in live:
                    if source.sample is None and source.task is None:
                        source.task = asyncio.ensure_future(
                            _next_sample(source.iterator)
                        )

                pending = [source for source in live if source.sample is None]
                ready = [source for source in live if source.sample is not None]

                if pending and not ready:
                    # 没有可发的样本，等下去不会让任何数据变旧，所以不设期限，
                    # 而且**停滞流也要等**：否则它恢复了都没人来收。
                    await self._wait(pending, None)
                    continue

                holding = [source for source in pending if not source.stalled]
                if holding:
                    await self._wait(holding, self._budget(ready, loop))
                    if self._collect(holding, loop):
                        # 有流交出了样本或结束了，重新评估；此时可能已经凑齐。
                        continue
                    for source in holding:
                        source.stalled = True
                    self._stats.latency_flushes += 1

                candidate = self._smallest(sources)
                if candidate is None:
                    continue
                source, sample = candidate
                stalled = any(
                    other.stalled and not other.finished for other in sources
                )
                yield self._take(source, sample, stalled=stalled)
        finally:
            await self._release(sources)

    @staticmethod
    async def _wait(
        sources: list[_Source], timeout: float | None
    ) -> None:
        """等这些流里任意一条交出结果，或等到超时。

        只负责等；收割交给 :meth:`_collect`。分开是因为这两件事的作用域不同：
        等待只看被挑出来的那几条流，收割必须覆盖全部——包括没在等待集里的停滞流。
        """
        tasks = [source.task for source in sources if source.task is not None]
        if not tasks:
            return
        await asyncio.wait(
            tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )

    def _budget(
        self, ready: list[_Source], loop: asyncio.AbstractEventLoop
    ) -> float | None:
        """最早的那个待发样本还能等多久。``max_latency=None`` 即严格归并，不设限。

        预算锚在样本的到达时刻上，所以它衡量的是「这个样本最多延迟多久」。调用方
        保证 ``ready`` 非空——没有待发样本时根本不需要预算，那条路径在主循环里
        单独处理，不在这里兜底：一个永远不会走到的分支也永远不会被验证。
        """
        if self._max_latency is None:
            return None
        deadline = min(source.arrived for source in ready) + self._max_latency
        return max(0.0, deadline - loop.time())

    def _collect(
        self, sources: list[_Source], loop: asyncio.AbstractEventLoop
    ) -> bool:
        """收下所有已完成的取样任务。返回是否有任何进展。

        判据是 ``task.done()`` 而不是「刚才那次 wait 返回的集合」：停滞流不进等待
        集，它的任务永远不会出现在那个集合里，只能靠这里发现它已经完成。
        """
        progressed = False
        for source in sources:
            task = source.task
            if task is None or not task.done():
                continue
            source.task = None
            sample = task.result()
            if sample is None:
                source.finished = True
                self._stats.sources_finished += 1
            else:
                source.sample = sample
                source.arrived = loop.time()
            source.stalled = False
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

    def _take(
        self, source: _Source, sample: ImuSample, *, stalled: bool = False
    ) -> ImuSample:
        source.sample = None
        # 只有不乱序的样本才推进 _last_t，于是它是一条高水位线，而不是「上一个
        # 已发样本」。这不是遗漏，是 out_of_order 的语义所在：推进了的话，一串
        # 连续迟到的样本只有第一个会被计到——后面几个相对彼此是升序的，而下游
        # 要重排的恰恰是它们全部。
        if self._last_t is not None and sample.t_host < self._last_t:
            self._stats.out_of_order += 1
        else:
            self._last_t = sample.t_host
        self._stats.emitted += 1
        if stalled:
            self._stats.emitted_while_stalled += 1
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
