"""真机探测：逐个试 ``RRATE`` 编码，实测其对应的实际速率。

本库只开放已核实的档位（见 `wt901.protocol.registers.ReturnRate`），而「核实」
的唯一手段就是在真机上写进去、量出来。这个脚本就是那个手段。

    ./dev run python tools/probe_rates.py [每档测量秒数]

**全程 persist=False**：探测不写 flash，断电即恢复上一次保存的配置。结束时会
把速率恢复到一个已核实的档位。

绕过了 `set_output_rate()` 的档位校验，直接调 `registers.write()`——那个校验正
是为了防止**误**写未知编码，而这里是**有意**写，且已限定在维特通用编码的连续
区间内。测到的结果若与预期一致，才有资格进入枚举。
"""

from __future__ import annotations

import asyncio
import sys
import time

from wt901.device import WT901Device
from wt901.discovery import scan
from wt901.protocol.registers import Register, ReturnRate

# 维特通用编码：0x01 起按 0.2/0.5/1/2/5/10/20/50/100/125/200 Hz 递增。
# 这里把「预期」写出来只是为了对账，不是断言——量出来是多少就是多少。
CANDIDATES: tuple[tuple[int, float | None], ...] = (
    (0x06, 10.0),  # 已核实，用作对照
    (0x07, 20.0),
    (0x08, 50.0),  # 已核实，用作对照
    (0x09, 100.0),
    (0x0A, 125.0),
    (0x0B, 200.0),
)

SETTLE = 0.6


async def measure(device: WT901Device, seconds: float) -> float:
    """排空积压后测速率。积压不排会把短窗口的读数抬高一个固定偏移。"""
    remaining = device.pending_samples
    count = 0
    start = time.monotonic()
    deadline = start + seconds
    async for _ in device.samples():
        if remaining > 0:
            remaining -= 1
            start = time.monotonic()
            deadline = start + seconds
            continue
        count += 1
        if time.monotonic() >= deadline:
            break
    return count / (time.monotonic() - start)


async def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0

    found = await scan(6.0)
    if not found:
        print("没扫到 WT 设备。")
        return 2
    sensor = found[0]
    print(f"连接 {sensor.name} ({sensor.address})\n")
    print(f"每档测量 {seconds:.0f} 秒，全程 persist=False（不写 flash）\n")
    print(f"{'编码':>6}  {'实测 Hz':>9}  {'预期':>7}  {'偏差':>7}  结论")
    print("-" * 52)

    verified: list[tuple[int, float]] = []

    async with await WT901Device.connect(sensor) as device:
        for code, expected in CANDIDATES:
            await device.registers.write(Register.RRATE, code, persist=False)
            await asyncio.sleep(SETTLE)
            measured = await measure(device, seconds)

            if expected is None:
                print(f"  0x{code:02X}  {measured:9.2f}  {'?':>7}  {'-':>7}  未知")
                continue
            deviation = abs(measured - expected) / expected * 100
            ok = deviation <= 10
            print(
                f"  0x{code:02X}  {measured:9.2f}  {expected:7.0f}  "
                f"{deviation:6.1f}%  {'✅ 相符' if ok else '❌ 不符'}"
            )
            if ok:
                verified.append((code, expected))

        # 恢复到一个已核实的档位，并保存。
        await device.registers.set_output_rate(ReturnRate.HZ_50)
        print(f"\n已恢复为 HZ_50 (0x{ReturnRate.HZ_50.value:02X}) 并保存。")

    print("\n可进入枚举的档位（实测相符）：")
    for code, expected in verified:
        print(f"  HZ_{int(expected)} = 0x{code:02X}")
    print(
        "\n注意：高速率档位受 BLE 链路带宽限制，实测值低于标称不一定是编码错误，"
        "\n也可能是链路跟不上。若某档实测明显低于标称，请对照 dropped_samples "
        "\n与 resync_count 再判断。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
