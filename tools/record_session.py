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
from wt901.recording import read_recording
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
    print(f"连接 {target.name} ({target.address}) rssi={target.rssi}")

    path.parent.mkdir(parents=True, exist_ok=True)
    transport = RecordingTransport.to_file(
        BleTransport(target),
        path,
        note=f"{rate_hz} Hz，{seconds} 秒，{target.name}",
    )
    device = WT901Device(transport)
    await device.open()
    try:
        await device.registers.set_output_rate(ReturnRate[f"HZ_{rate_hz}"])
        # 配置生效前设备仍在按旧速率推数据，先排空，否则基线开头混着旧配置的帧。
        while device.pending_samples:
            await asyncio.sleep(0.05)
        print(f"录制中 …… {seconds} 秒")
        await asyncio.sleep(seconds)
    finally:
        await device.close()

    recording = read_recording(path)
    print(
        f"写入 {path}："
        f"{len(recording.chunks)} 段 / {recording.total_bytes} 字节 / "
        f"{recording.duration:.2f} 秒"
    )
    print(f"设备统计：{device.stats}")
    if not recording.chunks:
        print("录制为空——设备没有推送任何数据。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
