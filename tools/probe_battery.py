"""真机对照实验：什么条件下 ``0x64`` 读到不可能的原始值 0。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑。**

    ./dev run python tools/probe_battery.py [每档读取次数]

默认每档读 12 次。四个条件在**同一台设备、同一次连接**内依次进行，这样条件之间
的差异只有采集负载，不掺入连接状态、设备温度、电量变化等变量：

1. 刚连接、未配置速率（出厂 10 Hz），**不消费样本**
2. 10 Hz，消费样本
3. 100 Hz，消费样本
4. 200 Hz，消费样本

第 1 档最接近首次读到 391（正常值）的那次运行——RAY-172 的 smoke_telemetry 是在
切到 100 Hz 之前读的设备信息；而读到 0 的那次是在设完 100 Hz 之后读的。这是本次
实验要证实或推翻的那个差异。

**打印完整的 4 寄存器回帧**（``0x64``–``0x67``），不只是取出来的那一个：若问题
出在回帧本身，只看一个值会丢掉最关键的线索——比如「整帧全零」和「只有 0x64 是
零」指向完全不同的方向。
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

from wt901.device import WT901Device
from wt901.discovery import scan
from wt901.errors import WT901Error
from wt901.protocol.registers import Register, ReturnRate

DEFAULT_READS = 12


async def _drain(device: WT901Device, stop: asyncio.Event) -> int:
    """持续消费样本，直到被要求停止。返回消费掉的样本数。"""
    consumed = 0
    async for _ in device.samples():
        consumed += 1
        if stop.is_set():
            break
    return consumed


async def _probe(device: WT901Device, reads: int) -> list[tuple[int, ...] | None]:
    """连读 ``reads`` 次 0x64，每次记下完整回帧；失败记 ``None``。"""
    results: list[tuple[int, ...] | None] = []
    for _ in range(reads):
        try:
            response = await device.registers.read(Register.POWER)
        except WT901Error:
            results.append(None)
        else:
            results.append(tuple(response.values))
        await asyncio.sleep(0.05)
    return results


def _report(label: str, results: list[tuple[int, ...] | None]) -> int:
    """打印一档的结果，返回其中 0x64 为 0 的次数。"""
    ok = [r for r in results if r is not None]
    failed = len(results) - len(ok)
    first = Counter(r[0] for r in ok)
    zeros = first.get(0, 0)

    print(f"\n── {label}")
    print(f"   读取 {len(results)} 次：成功 {len(ok)}，失败 {failed}")
    print(f"   0x64 取值分布：{dict(sorted(first.items()))}")
    print(f"   0x64 == 0 的次数：{zeros}")
    if ok:
        print("   完整回帧（0x64–0x67）前若干次：")
        for values in ok[:5]:
            print(f"     {[f'0x{v & 0xFFFF:04X}' for v in values]}  → 0x64 = {values[0]}")
    return zeros


async def _condition(
    device: WT901Device,
    label: str,
    reads: int,
    *,
    rate: ReturnRate | None,
    consume: bool,
) -> int:
    if rate is not None:
        await device.registers.set_output_rate(rate)

    if not consume:
        return _report(label, await _probe(device, reads))

    stop = asyncio.Event()
    drain = asyncio.ensure_future(_drain(device, stop))
    try:
        results = await _probe(device, reads)
    finally:
        stop.set()
        # 消费者阻塞在 queue.get() 上，设一个 stop 还不够——它要等下一个样本
        # 到达才会看到。设备在推数据，所以这个等待是有界的，但仍要防它挂住。
        try:
            consumed = await asyncio.wait_for(drain, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            drain.cancel()
            consumed = -1
    zeros = _report(label, results)
    print(f"   期间消费样本：{consumed if consumed >= 0 else '（消费者未按时结束）'}")
    return zeros


async def main() -> int:
    reads = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_READS

    found = await scan(15.0)
    if not found:
        print("没有发现 WT 设备：请确认设备已通电，且本终端有蓝牙权限。")
        return 2
    target = found[0]
    print(f"设备 {target.name} ({target.address}) rssi={target.rssi}")
    print(f"每档读取 {reads} 次，四档在同一次连接内依次进行。")

    device = await WT901Device.connect(target)
    zeros: dict[str, int] = {}
    try:
        zeros["出厂速率 / 不消费"] = await _condition(
            device, "① 刚连接、未配置速率（出厂 10 Hz）、不消费样本",
            reads, rate=None, consume=False,
        )
        zeros["10 Hz / 消费"] = await _condition(
            device, "② 10 Hz，消费样本", reads, rate=ReturnRate.HZ_10, consume=True,
        )
        zeros["100 Hz / 消费"] = await _condition(
            device, "③ 100 Hz，消费样本", reads, rate=ReturnRate.HZ_100, consume=True,
        )
        zeros["200 Hz / 消费"] = await _condition(
            device, "④ 200 Hz，消费样本", reads, rate=ReturnRate.HZ_200, consume=True,
        )
    finally:
        await device.close()

    print("\n═══ 汇总：0x64 读到 0 的次数 ═══")
    for label, count in zeros.items():
        print(f"  {label:<22} {count} / {reads}")
    total = sum(zeros.values())
    if total == 0:
        print("\n本次未复现。不能据此断定问题不存在——只能记录为未复现。")
        return 1
    print(f"\n共复现 {total} 次。对照上表判断它是否与采集负载相关。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
