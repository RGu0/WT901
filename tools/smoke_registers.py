"""真机冒烟：寄存器读写 + 速率实测 + 长时间稳定性。

手动执行，不进 CI。**必须在 Claude 内置终端或 Terminal.app 里跑**——代理的
Bash 工具执行时 CoreBluetooth 会直接 abort（退出码 134，无输出）。

    ./dev run python tools/smoke_registers.py [长跑分钟数]

不带参数只做速率验证；带参数则在其后追加一段长时间采集，用于 RAY-170 的
「连续采集若干分钟并记录 resync_count / dropped_samples」验收。

覆盖的验收点：
  * 读事务能在实时数据流中拿到寄存器回帧（RAY-171）
  * 设为 10 Hz / 50 Hz 后实测速率与设定相符（RAY-171）
  * 写入后断电重连配置仍在 —— 需人工断电，脚本只打印读回值供比对（RAY-171）
  * 长时间采集的 resync_count 与 dropped_samples（RAY-170）
"""

from __future__ import annotations

import asyncio
import sys
import time

from wt901.device import WT901Device
from wt901.discovery import scan
from wt901.protocol.registers import Bandwidth, Register, ReturnRate

MEASURE_SECONDS = 10.0


async def measure_rate(device: WT901Device, seconds: float) -> float:
    """数指定时长内产出的样本数，换算成 Hz。"""
    count = 0
    deadline = time.monotonic() + seconds
    async for _ in device.samples():
        count += 1
        if time.monotonic() >= deadline:
            break
    return count / seconds


async def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0

    found = await scan(6.0)
    if not found:
        print("没扫到 WT 设备，需要一台通电的 WT9011DCL-BT50。")
        return 2
    target = found[0]
    print(f"连接 {target.name} ({target.address}) rssi={target.rssi}\n")

    async with await WT901Device.connect(target.address) as device:
        # --- 读事务：在实时数据流中拿到寄存器回帧 ---
        rate_code = await device.registers.read_output_rate()
        bandwidth_code = await device.registers.read_bandwidth()
        print(f"当前 RRATE     = 0x{rate_code:02X}")
        print(f"当前 BANDWIDTH = 0x{bandwidth_code:02X}")

        temperature = await device.registers.read_value(Register.TEMPERATURE)
        print(f"温度 = {temperature / 100:.2f} °C\n")

        # --- 写事务 + 速率实测 ---
        for rate in (ReturnRate.HZ_10, ReturnRate.HZ_50):
            await device.registers.set_output_rate(rate)
            await asyncio.sleep(0.5)  # 让设备切换稳定
            before = device.stats
            measured = await measure_rate(device, MEASURE_SECONDS)
            after = device.stats
            expected = 10.0 if rate is ReturnRate.HZ_10 else 50.0
            deviation = abs(measured - expected) / expected * 100
            verdict = "OK" if deviation <= 10 else "偏差超过 10%"
            print(
                f"设为 {rate.name:>6}: 实测 {measured:6.2f} Hz "
                f"(期望 {expected:.0f}, 偏差 {deviation:4.1f}% → {verdict})  "
                f"resync+{after.resync_count - before.resync_count} "
                f"dropped+{after.dropped_samples - before.dropped_samples}"
            )

        await device.registers.set_bandwidth(Bandwidth.HZ_20)
        print(f"\n带宽已设为 20 Hz，读回 = 0x{await device.registers.read_bandwidth():02X}")

        print(
            "\n断电重连验证（人工）：现在给传感器断电再上电，重跑本脚本，"
            f"若 RRATE 读回仍为 0x{ReturnRate.HZ_50.value:02X} 则 save 生效。"
        )

        # --- 长时间稳定性（RAY-170） ---
        if minutes > 0:
            print(f"\n开始长时间采集 {minutes} 分钟 ...")
            baseline = device.stats
            start = time.monotonic()
            count = 0
            async for _ in device.samples():
                count += 1
                if time.monotonic() - start >= minutes * 60:
                    break
            elapsed = time.monotonic() - start
            final = device.stats
            print(
                f"采集 {elapsed:.0f}s / {count} 样本 "
                f"（{count / elapsed:.2f} Hz）\n"
                f"  resync_count   +{final.resync_count - baseline.resync_count}\n"
                f"  dropped_bytes  +{final.dropped_bytes - baseline.dropped_bytes}\n"
                f"  dropped_samples+{final.dropped_samples - baseline.dropped_samples}\n"
                f"  reconnects     +{final.reconnects - baseline.reconnects}"
            )

    print("\n连接已释放。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
