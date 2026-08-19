"""真机冒烟：双设备并发采集长跑 + 合流。

手动执行，不进 CI（CI 没有硬件，也没有蓝牙权限）：

    ./dev run python tools/smoke_multi_device.py [分钟数] [速率Hz]

默认 10 分钟、100 Hz —— 对应 RAY-174 的验收标准「双设备真机并发采集稳定运行
≥ 10 分钟，两路 dropped_samples 与 resync_count 记入 evidence」。

**中途可以关掉其中一台。** 每 30 秒打印一次分路计数，某一路停止增长而另一路
继续，就是「单台设备断连不影响另一台」的真机证据。脚本不会因此退出。

退出码：0 双路都有数据；1 有路没数据；2 没扫到两台设备。
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter

from wt901.device import WT901Device
from wt901.discovery import scan
from wt901.multi import merge
from wt901.protocol.registers import ReturnRate

REPORT_EVERY = 30.0


def _report(counts: Counter[str], devices: list[WT901Device], elapsed: float) -> None:
    parts = []
    for device in devices:
        stats = device.stats
        parts.append(
            f"{device.device_id[:8]}… n={counts[device.device_id]:6d} "
            f"({counts[device.device_id] / elapsed:5.1f} Hz) "
            f"dropped={stats.dropped_samples} resync={stats.resync_count} "
            f"reconnects={stats.reconnects}"
        )
    print(f"  [{elapsed / 60:5.2f} 分] " + " | ".join(parts))


async def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    rate_hz = int(sys.argv[2]) if len(sys.argv) > 2 else 100

    found = await scan(8.0, name_substring="WT")
    print(f"扫描到 {len(found)} 台 WT 设备")
    for device in found:
        print(f"  {device.address}  rssi={device.rssi}  name={device.name!r}")
    if len(found) < 2:
        print("\n需要两台通电的 WT9011DCL-BT50；本条验收无法在单台设备上完成。")
        return 2

    devices: list[WT901Device] = []
    for target in found[:2]:
        print(f"连接 {target.name} ({target.address}) …")
        devices.append(await WT901Device.connect(target))

    try:
        for device in devices:
            await device.registers.set_output_rate(ReturnRate[f"HZ_{rate_hz}"])
        # 配置生效前设备仍按旧速率推送，先排空再计数，否则前几秒的速率是虚高的。
        for device in devices:
            while device.pending_samples:
                await asyncio.sleep(0.05)

        print(f"\n合流采集 {minutes} 分钟 @ {rate_hz} Hz（可中途关掉一台）")
        counts: Counter[str] = Counter()
        order_violations = 0
        deadline = time.monotonic() + minutes * 60
        next_report = time.monotonic() + REPORT_EVERY
        start = time.monotonic()
        previous_t = None

        stream = merge(devices)
        async for sample in stream.samples():
            counts[sample.device_id] += 1
            if previous_t is not None and sample.t_host < previous_t:
                order_violations += 1
            previous_t = sample.t_host
            now = time.monotonic()
            if now >= next_report:
                _report(counts, devices, now - start)
                next_report += REPORT_EVERY
            if now >= deadline:
                break

        elapsed = time.monotonic() - start
        print(f"\n结束，实际运行 {elapsed / 60:.2f} 分钟")
        _report(counts, devices, elapsed)
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
