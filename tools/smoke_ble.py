"""真机冒烟：扫描 → 连接 → 收原始字节。

手动执行，不进 CI（CI 没有硬件，也没有蓝牙权限）：

    ./dev run python tools/smoke_ble.py [连接秒数]

**macOS 前置条件**：宿主终端/应用需要在「系统设置 → 隐私与安全性 → 蓝牙」中
获得授权。没有授权时 CoreBluetooth 会直接 ``abort()``，进程以信号 6（退出码
134）终止且不打印任何原因——看起来像程序莫名其妙没了。

退出码：0 收到数据；1 连上但没数据；2 没扫到 WT 设备。
"""

from __future__ import annotations

import asyncio
import sys

from wt901.discovery import scan
from wt901.transport.ble import BleTransport


async def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0

    # 先不过滤：这样「没扫到 WT 设备」和「蓝牙根本没工作」能区分开。
    everything = await scan(6.0, name_substring=None)
    print(f"扫描到 {len(everything)} 台 BLE 设备")
    for device in everything[:15]:
        print(f"  {device.address}  rssi={device.rssi}  name={device.name!r}")

    wit = [
        device
        for device in everything
        if device.name is not None and "wt" in device.name.lower()
    ]
    if not wit:
        print("\n没有发现 WT 设备：真机验收项未通过，需要一台通电的 WT9011DCL-BT50。")
        return 2

    target = wit[0]
    print(f"\n连接 {target.name} ({target.address}) ...")
    chunks: list[bytes] = []
    transport = BleTransport(target.address)
    transport.on_data(chunks.append)
    async with transport:
        print("已连接，收数据中 ...")
        await asyncio.sleep(seconds)

    total = sum(len(chunk) for chunk in chunks)
    print(f"收到 {len(chunks)} 条通知 / {total} 字节，约 {total / seconds:.0f} B/s")
    for chunk in chunks[:5]:
        print("  ", chunk.hex(" "))
    print(f"连接已释放：is_connected={transport.is_connected}")
    return 0 if chunks else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
