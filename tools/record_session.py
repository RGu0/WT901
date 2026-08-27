"""录一段真机字节流，作为 CI 的回归基线。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑。**

    ./dev run python tools/record_session.py [输出路径] [秒数] [速率Hz]

默认写 tests/data/recordings/wt901-100hz.jsonl，录 3 秒，100 Hz。

**3 秒是刻意的。** 基线要进 git，而 100 Hz 下每秒约 100 帧、每帧一行 JSON，
10 秒就是一千行。基线的作用是「让回放能端到端跑一遍真实字节」，不是「样本越多
越好」——真正的长跑验证由 smoke_multi_device.py 负责，那份数据不进仓库。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from wt901.device import WT901Device
from wt901.discovery import scan
from wt901.protocol.registers import ReturnRate
from wt901.recording import open_recording
from wt901.transport.ble import BleTransport
from wt901.transport.recording import RecordingTransport

DEFAULT_PATH = Path("tests/data/recordings/wt901-100hz.jsonl")


async def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    rate_hz = int(sys.argv[3]) if len(sys.argv) > 3 else 100

    found = await scan(6.0, name_substring="WT")
    if not found:
        print("没有发现 WT 设备：需要一台通电的 WT9011DCL-BT50。")
        return 2
    target = found[0]
    print(f"正在连接 {target.name} ({target.address}) rssi={target.rssi} ……")

    path.parent.mkdir(parents=True, exist_ok=True)
    transport = RecordingTransport.to_file(
        BleTransport(target),
        path,
        note=f"{rate_hz} Hz，{seconds} 秒（含起始的速率切换），{target.name}",
    )
    device = WT901Device(transport)
    await device.open()
    print("已连接，开始录制")
    try:
        # 录制发生在传输层，从 connect 那一刻就在写文件。所以基线开头会包含
        # 出厂默认 10 Hz 的若干帧和这次速率切换——**这是刻意保留的**：一份跨越
        # 速率切换的基线同时覆盖了单帧与打包传输两种情形，比一段匀速数据更值钱。
        print(f"  设置 {rate_hz} Hz ……")
        await device.registers.set_output_rate(ReturnRate[f"HZ_{rate_hz}"])
        print(f"  录制中 …… {seconds} 秒")
        await asyncio.sleep(seconds)
    finally:
        await device.close()

    # 走流式入口而不是 read_recording：这里只要三个统计量，单趟扫描就够，
    # 没有理由把整份文件读进内存。3 秒的基线无所谓，但同一段代码也会被用来
    # 检查 smoke 跑出来的长录制（RAY-200 的压测文件单台约 14 MB）。
    chunks = 0
    total_bytes = 0
    duration = 0.0
    with open_recording(path) as reader:
        for chunk in reader:
            chunks += 1
            total_bytes += len(chunk.data)
            duration = chunk.t

    print(f"写入 {path}：{chunks} 段 / {total_bytes} 字节 / {duration:.2f} 秒")
    print(f"设备统计：{device.stats}")
    if not chunks:
        print("录制为空——设备没有推送任何数据。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
