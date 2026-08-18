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

LONG_RUN_RATE = ReturnRate.HZ_100
"""长跑用 100 Hz。链路压力是 50 Hz 的两倍，低速率不丢包说明不了高速率也不丢。"""


async def measure_rate(device: WT901Device, seconds: float) -> tuple[float, int]:
    """排空积压后测速率，返回 ``(Hz, 排空的积压样本数)``。

    **测速前必须先排空。** 切换速率的写事务本身要 0.2 s（两个延时），加上稳定
    等待和之前的寄存器读，这段时间里设备一直在以旧速率推数据而没人消费。不排
    空的话这批积压会在测量窗口一开始被瞬间计入，把速率读高一个固定偏移——短
    窗口下这个偏移足以把 10 Hz 测成 12.8 Hz。
    """
    backlog = device.pending_samples
    remaining = backlog
    count = 0
    start = time.monotonic()
    deadline = start + seconds
    async for _ in device.samples():
        if remaining > 0:
            # 先把积压吃掉；计时窗口从最后一个积压样本之后才开始。
            remaining -= 1
            start = time.monotonic()
            deadline = start + seconds
            continue
        count += 1
        if time.monotonic() >= deadline:
            break
    return count / (time.monotonic() - start), backlog


async def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0

    found = await scan(6.0)
    if not found:
        print("没扫到 WT 设备，需要一台通电的 WT9011DCL-BT50。")
        return 2
    sensor = found[0]
    print(f"连接 {sensor.name} ({sensor.address}) rssi={sensor.rssi}\n")

    async with await WT901Device.connect(sensor.address) as device:
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
            measured, backlog = await measure_rate(device, MEASURE_SECONDS)
            after = device.stats
            expected = 10.0 if rate is ReturnRate.HZ_10 else 50.0
            deviation = abs(measured - expected) / expected * 100
            verdict = "OK" if deviation <= 10 else "偏差超过 10%"
            print(
                f"设为 {rate.name:>6}: 实测 {measured:6.2f} Hz "
                f"(期望 {expected:.0f}, 偏差 {deviation:4.1f}% → {verdict})  "
                f"排空积压 {backlog:3d}  "
                f"resync+{after.resync_count - before.resync_count} "
                f"dropped+{after.dropped_samples - before.dropped_samples}"
            )

        await device.registers.set_bandwidth(Bandwidth.HZ_20)
        bandwidth_readback = await device.registers.read_bandwidth()
        print(f"\n带宽设为 20 Hz，读回 = 0x{bandwidth_readback:02X}")
        if bandwidth_code == Bandwidth.HZ_20:
            print("  （起始就已是 20 Hz，这一项不构成写入证明）")

        # --- 长时间稳定性（RAY-170） ---
        #
        # 跑在 100 Hz 而不是 50 Hz：链路压力翻倍，正是 dropped_samples 与
        # resync_count 最该暴露问题的地方。低速率下不丢包说明不了高速率也不丢。
        if minutes > 0:
            await device.registers.set_output_rate(LONG_RUN_RATE)
            await asyncio.sleep(0.5)
            print(
                f"\n开始长时间采集 {minutes} 分钟 @ "
                f"{LONG_RUN_RATE.name}（0x{LONG_RUN_RATE.value:02X}）..."
            )
            baseline = device.stats
            # 同样要先排空：切速率期间积压的样本不属于这段观测。
            remaining = device.pending_samples
            count = 0
            start = time.monotonic()
            async for _ in device.samples():
                if remaining > 0:
                    remaining -= 1
                    start = time.monotonic()
                    continue
                count += 1
                if time.monotonic() - start >= minutes * 60:
                    break
            elapsed = time.monotonic() - start
            final = device.stats
            expected = 100.0 if LONG_RUN_RATE is ReturnRate.HZ_100 else 50.0
            print(
                f"采集 {elapsed:.0f}s / {count} 样本 "
                f"（{count / elapsed:.2f} Hz，期望 {expected:.0f}）\n"
                f"  resync_count   +{final.resync_count - baseline.resync_count}\n"
                f"  dropped_bytes  +{final.dropped_bytes - baseline.dropped_bytes}\n"
                f"  dropped_samples+{final.dropped_samples - baseline.dropped_samples}\n"
                f"  reconnects     +{final.reconnects - baseline.reconnects}"
            )

        # --- 持久化验证：放最后，且必须写一个与起始值不同的值 ---
        #
        # 上一轮这条测了个寂寞：起始 RRATE 就已经是 0x08，再写 0x08 读回 0x08，
        # 什么也证明不了。要有结论，就得让写入前后的值不同。
        persist_target = (
            ReturnRate.HZ_10 if rate_code == ReturnRate.HZ_50 else ReturnRate.HZ_50
        )
        await device.registers.set_output_rate(persist_target)
        readback = await device.registers.read_output_rate()
        print(
            f"\n持久化验证：起始 RRATE 为 0x{rate_code:02X}，已改写为 "
            f"0x{persist_target.value:02X}（当场读回 0x{readback:02X}）。\n"
            f"  → 现在给传感器**断电再上电**，重跑本脚本。\n"
            f"  → 开头的「当前 RRATE」若为 0x{persist_target.value:02X}，save 生效；\n"
            f"  → 若变回 0x{rate_code:02X} 或出厂值 0x06，说明没保存住。"
        )

    print("\n连接已释放。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
