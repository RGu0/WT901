"""双设备并发采集，并把两路合成一条按时间排序的流。

    python examples/two_devices.py [秒数]

两台 WT901 的广播名可能完全相同，靠地址区分——``sample.device_id`` 取的就是地址。

macOS 上若本终端未获蓝牙授权，进程可能**零输出、以退出码 134 终止**——
CoreBluetooth 直接 abort，Python 来不及抛任何异常。见 README「平台差异」。
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

    # 连接循环必须在 try 里面，逐台连上就登记。放在外面的话，第一台连上、第二台
    # 超时，第一台就永远不会被关闭——而 BLE 连接不会因为进程里抛了异常就自己关掉，
    # 泄漏的连接会让**下一次** connect 直接失败。这正是 Transport.__aexit__ 的注释
    # 警告过的事。
    devices: list[WT901Device] = []
    try:
        for target in found[:2]:
            print(f"正在连接 {target.name} ({target.address}) ……")
            devices.append(await WT901Device.connect(target))
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
