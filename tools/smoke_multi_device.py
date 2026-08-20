"""真机冒烟：双设备并发采集长跑 + 合流。

手动执行，不进 CI（CI 没有硬件，也没有蓝牙权限）：

    ./dev run python tools/smoke_multi_device.py [分钟数] [速率Hz] [单轮扫描秒数]

默认 10 分钟、100 Hz —— 对应 RAY-174 的验收标准「双设备真机并发采集稳定运行
≥ 10 分钟，两路 dropped_samples 与 resync_count 记入 evidence」。

**中途可以关掉其中一台。** 每 30 秒打印一次分路的**区间**速率，某一路停止增长而
另一路以原速率继续，才是「单台设备断连不影响另一台」的真机证据。脚本不会因此退出。

只看「另一路还在动」是不够的：RAY-190 里存活那路确实还在动，但速率塌到十分之一、
其余样本被静默丢弃。所以这里同时打 ``dropped`` 的区间增量——它不涨才算数。

退出码：0 双路都有数据；1 有路没数据；2 没扫到两台设备。
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

from wt901.device import WT901Device
from wt901.discovery import DiscoveredDevice, scan
from wt901.multi import merge
from wt901.protocol.registers import ReturnRate

REPORT_EVERY = 30.0
MIN_WINDOW = 1.0
"""短于此的区间不单独成行——它算出来的速率没有意义。"""
SETTLE_SECONDS = 0.5
SCAN_SECONDS = 15.0
SCAN_ROUNDS = 3


async def _scan_for_two(seconds: float) -> list[DiscoveredDevice]:
    """反复整轮扫描，直到**一次**扫描里同时出现两台设备。

    不把多轮结果并起来是有原因的：``DiscoveredDevice.handle`` 是 bleak 的
    ``BLEDevice``，只在本次扫描会话内有效。把上一轮的句柄和这一轮的凑成一对去
    连接，失效的那个会报「设备未找到」，哪怕它就在眼前——RAY-178 记的正是这件事。
    所以宁可整轮重来，也不跨会话拼装。

    WT901 的广播占空比不高，单轮扫得太短就容易只撞见其中一台。
    """
    found: list[DiscoveredDevice] = []
    for attempt in range(1, SCAN_ROUNDS + 1):
        everything = await scan(seconds, name_substring=None)
        found = [
            device
            for device in everything
            if device.name is not None and "wt" in device.name.lower()
        ]
        print(
            f"第 {attempt}/{SCAN_ROUNDS} 轮（{seconds:.0f} 秒）："
            f"BLE 设备 {len(everything)} 台，其中 WT {len(found)} 台"
        )
        for device in found:
            print(f"    {device.address}  rssi={device.rssi}  name={device.name!r}")
        if len({device.name for device in found}) == 1 and len(found) > 1:
            print("    ↑ 两台广播名相同，靠地址区分（device_id 也取自地址）")
        if len(found) >= 2:
            return found
    return found


async def _settle(device: WT901Device, seconds: float) -> int:
    """丢掉速率切换前后产生的样本，返回丢了几个。

    **不能写成** ``while device.pending_samples: await asyncio.sleep(...)``：设备
    在以新速率持续推送，而那个循环里没有任何消费者，队列永远不会归零——它是个
    死循环。要真的把样本消费掉，而且只能按时间窗丢：「队列清空」这个状态在
    100 Hz 下根本不会出现。
    """
    discarded = 0
    iterator = device.samples()
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            try:
                await asyncio.wait_for(anext(iterator), timeout=0.2)
            except (TimeoutError, StopAsyncIteration):
                break
            discarded += 1
    finally:
        await iterator.aclose()
    return discarded


@dataclass(slots=True)
class _Snapshot:
    """上一次报告时的计数，用来算区间速率。"""

    elapsed: float = 0.0
    counts: Counter[str] = field(default_factory=Counter)
    dropped: dict[str, int] = field(default_factory=dict)


def _snapshot(
    counts: Counter[str], devices: list[WT901Device], elapsed: float
) -> _Snapshot:
    return _Snapshot(
        elapsed=elapsed,
        counts=Counter(counts),
        dropped={device.device_id: device.stats.dropped_samples for device in devices},
    )


def _report(
    counts: Counter[str],
    devices: list[WT901Device],
    elapsed: float,
    previous: _Snapshot,
    baseline: _Snapshot,
    *,
    label: str | None = None,
) -> _Snapshot:
    """打印**区间**速率，不是全程平均。

    这个区别是 RAY-190 换来的。当时这里打的是 ``n / 全程耗时``，于是存活设备从
    198.6 Hz 塌到 19.5 Hz 这件事，在报告里表现为四行缓慢下滑的 188 → 179 → 171
    → 163——看着像轻微退化，实则是十倍塌陷。全程平均会把任何突变摊平成缓坡，
    而故障恰恰是突变。同理 ``dropped`` 也带上区间增量：总数在涨还是停了，
    是两件完全不同的事。

    ``dropped`` 以采集起点为基线。设备一连上就开始推送，而第二台还在扫描/连接/
    配置——先连的那台没有消费者，队列（1024）会被填满并开始丢弃。那些丢弃发生在
    采集开始之前，混进采集期的计数里只会让人误判。它们在开跑前单独交代一次。
    """
    window = max(elapsed - previous.elapsed, 1e-9)
    parts = []
    for device in devices:
        stats = device.stats
        device_id = device.device_id
        rate = (counts[device_id] - previous.counts[device_id]) / window
        gained = stats.dropped_samples - previous.dropped.get(device_id, 0)
        total = stats.dropped_samples - baseline.dropped.get(device_id, 0)
        parts.append(
            f"{device_id[:8]}… n={counts[device_id]:6d} ({rate:5.1f} Hz) "
            f"dropped={total}(+{gained}) "
            f"resync={stats.resync_count} reconnects={stats.reconnects}"
        )
    head = label if label is not None else f"[{elapsed / 60:5.2f} 分]"
    print(f"  {head} " + " | ".join(parts))
    return _snapshot(counts, devices, elapsed)


async def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    rate_hz = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    scan_seconds = float(sys.argv[3]) if len(sys.argv) > 3 else SCAN_SECONDS

    found = await _scan_for_two(scan_seconds)
    if len(found) < 2:
        print(
            "\n需要两台通电的 WT9011DCL-BT50 同时出现在**同一次**扫描里；"
            "本条验收无法在单台设备上完成。"
        )
        print(
            "  若确信两台都通着电，试试拉长单轮扫描："
            f"./dev run python {sys.argv[0]} {minutes} {rate_hz} 30"
        )
        return 2

    devices: list[WT901Device] = []
    for target in found[:2]:
        print(f"正在连接 {target.name} ({target.address}) ……")
        devices.append(await WT901Device.connect(target))
        print(f"  已连接 {target.name}")

    try:
        for device in devices:
            print(f"  设置 {rate_hz} Hz：{device.device_id[:8]}… ")
            await device.registers.set_output_rate(ReturnRate[f"HZ_{rate_hz}"])
        for device in devices:
            discarded = await _settle(device, SETTLE_SECONDS)
            print(f"  丢弃切换期样本 {discarded} 个：{device.device_id[:8]}…")

        setup_drops = {
            device.device_id: device.stats.dropped_samples for device in devices
        }
        if any(setup_drops.values()):
            summary = "，".join(
                f"{device_id[:8]}… {dropped}"
                for device_id, dropped in setup_drops.items()
            )
            print(
                f"  连接与配置期间丢弃：{summary}"
                "（先连上的那台在等另一台时无人消费，队列填满即丢；"
                "不计入下面的采集期计数）"
            )

        print(f"\n合流采集 {minutes} 分钟 @ {rate_hz} Hz（可中途关掉一台）")
        counts: Counter[str] = Counter()
        order_violations = 0
        deadline = time.monotonic() + minutes * 60
        next_report = time.monotonic() + REPORT_EVERY
        start = time.monotonic()
        previous_t = None

        stream = merge(devices)
        opening = _snapshot(counts, devices, 0.0)
        window = opening
        async for sample in stream.samples():
            counts[sample.device_id] += 1
            if previous_t is not None and sample.t_host < previous_t:
                order_violations += 1
            previous_t = sample.t_host
            now = time.monotonic()
            if now >= next_report:
                window = _report(counts, devices, now - start, window, opening)
                next_report += REPORT_EVERY
            if now >= deadline:
                break

        elapsed = time.monotonic() - start
        print(f"\n结束，实际运行 {elapsed / 60:.2f} 分钟")
        # 收尾区间若短得没有意义就不打：一个 0.0 Hz 的尾行与「设备已死」同形，
        # 而这正是本工具要用来判读的信号。
        if elapsed - window.elapsed >= MIN_WINDOW:
            _report(counts, devices, elapsed, window, opening)
        _report(counts, devices, elapsed, opening, opening, label="[全程平均]")
        print(f"\n合流统计：{stream.stats}")
        print(f"  逐样本核对的乱序数：{order_violations}")
        print(
            "  合流的 out_of_order 与这里的核对数应当一致；不一致说明统计写错了。"
        )
        return 0 if all(counts[d.device_id] for d in devices) else 1
    finally:
        for device in devices:
            await device.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
