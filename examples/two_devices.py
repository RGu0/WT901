"""双设备并发采集，并把两路合成一条按时间排序的流。

    python examples/two_devices.py [秒数]

两台 WT901 的广播名可能完全相同，靠地址区分——``sample.device_id`` 取的就是地址。
"""

import asyncio
import sys
from collections import Counter

from wt901 import ReturnRate, WT901Device, merge, scan


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0

    # 单轮扫描未必能同时撞见两台：WT901 的广播占空比不高，拉长单轮比多轮拼装可靠
    # ——设备句柄只在本次扫描会话内有效，跨会话拼出来的那对连不上。
    found = await scan(15.0)
    if len(found) < 2:
        raise SystemExit(f"只发现 {len(found)} 台设备，本示例需要两台")

    devices = [await WT901Device.connect(target) for target in found[:2]]
    try:
        for device in devices:
            await device.registers.set_output_rate(ReturnRate.HZ_100)

        counts: Counter[str] = Counter()
        stream = merge(devices)
        deadline = asyncio.get_running_loop().time() + seconds
        async for sample in stream.samples():
            counts[sample.device_id] += 1
            if asyncio.get_running_loop().time() >= deadline:
                break

        for device_id, n in counts.items():
            print(f"{device_id}  {n} 个样本  {n / seconds:.1f} Hz")
        # out_of_order 非零说明有界延迟归并的超时路径放走了顺序，值得注意。
        print(stream.stats)
    finally:
        for device in devices:
            await device.close()


asyncio.run(main())
