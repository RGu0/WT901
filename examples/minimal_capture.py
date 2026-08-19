"""最小采集：扫描 → 连接 → 设 100 Hz → 打印带时间戳的样本。

    python examples/minimal_capture.py [秒数]

设备出厂默认只有 10 Hz，所以「设速率」这一步不能省。
macOS 未授权蓝牙时进程可能零输出、以退出码 134 终止，见 README「平台差异」。
"""

import asyncio
import sys

from wt901 import ReturnRate, WT901Device, scan


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    found = await scan()
    if not found:
        raise SystemExit("没有发现 WT 设备：请确认设备已通电，且本终端有蓝牙权限")

    async with await WT901Device.connect(found[0]) as device:
        await device.registers.set_output_rate(ReturnRate.HZ_100)
        deadline = asyncio.get_running_loop().time() + seconds
        async for s in device.samples():
            print(f"{s.t_host:12.4f}  {s.accel}  {s.euler}")
            if asyncio.get_running_loop().time() >= deadline:
                break


asyncio.run(main())
