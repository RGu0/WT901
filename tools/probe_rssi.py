"""真机取证：连接期到底能不能读到 RSSI，以及它随距离怎么变（RAY-310）。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑**——代理的 Bash 工具下
CoreBluetooth 不授权，脚本会在扫描那一步就失败。

    ./dev run python tools/probe_rssi.py [秒数]

默认量 60 秒，每秒一次。

## 判据（取证前预注册，按方针 2）

这个脚本要回答的是**一个是非题**：macOS/CoreBluetooth 对**已连接**外设调
``readRSSI()`` 能不能返回值。三种结果都是完整交付：

1. **能读到** —— 判定标准：60 次里成功 ≥ 55 次，且值落在 −100..0 dBm。
   记录中位数与全距。本库的实现按预期工作，把这条写进 `docs/protocol.md` §8.1。
2. **完全读不到** —— 60 次全部 ``None``。那么本库如实报 `None` 就是正确行为，
   把「macOS 上连接期 RSSI 读不到」写进 §8.1 与 README，并把那段依赖 bleak
   私有属性的代码标注为「已知在本平台无效」。
3. **时好时坏** —— 成功率落在中间。**这一条最有价值也最容易被误读**：它意味着
   调用方拿到的 ``None`` 有时是「这次没读到」而不是「这个平台没有」，那需要在
   docstring 里写明，否则下游会把偶发的 ``None`` 当成链路断了。

**同时记录的对照量**：`stats.resync_count` 与 `stats.dropped_samples`。RSSI 的
全部立项理由是它属**原因侧**——若整段采集里 RSSI 平稳而 resync 猛涨，那说明问题
不在链路距离上，这个对照本身就是结论。

## 想顺带验证「原因侧」这个说法

跑的时候拿着设备走远再走回来。若 RSSI 先降后升、而 `resync_count` 在 RSSI 最低
的那段开始涨，那就是这个量存在的意义的直接证据。这一步是可选的，不影响上面
三条判据。
"""

from __future__ import annotations

import asyncio
import statistics
import sys

from wt901.device import WT901Device
from wt901.discovery import scan
from wt901.protocol.registers import ReturnRate

DEFAULT_SECONDS = 60


async def main(seconds: int) -> int:
    devices = await scan()
    if not devices:
        print("没有扫描到设备。检查设备是否上电、是否已被别的程序连着。")
        return 1

    target = devices[0]
    print(f"目标：{target.address}  扫描期 RSSI={target.rssi} dBm")
    print("（扫描期那个值来自广播包，与下面连接期的读取不是同一条路径。）\n")

    async with await WT901Device.connect(target) as device:
        await device.registers.set_output_rate(ReturnRate.HZ_100)

        stop = asyncio.Event()

        async def drain() -> None:
            async for _ in device.samples():
                if stop.is_set():
                    return

        drainer = asyncio.create_task(drain())

        readings: list[int | None] = []
        print(f"{'秒':>4}  {'RSSI':>6}  {'resync':>7}  {'dropped':>8}")
        for second in range(seconds):
            value = await device.read_rssi()
            readings.append(value)
            stats = device.stats
            shown = f"{value}" if value is not None else "None"
            print(
                f"{second + 1:>4}  {shown:>6}  "
                f"{stats.resync_count:>7}  {stats.dropped_samples:>8}"
            )
            await asyncio.sleep(1.0)

        stop.set()
        drainer.cancel()

    got = [value for value in readings if value is not None]
    print("\n--- 判读 ---")
    print(f"读取次数        {len(readings)}")
    print(f"拿到值的次数    {len(got)}")
    if got:
        print(f"中位数          {statistics.median(got)} dBm")
        print(f"全距            {min(got)} .. {max(got)} dBm")
        in_range = all(-100 <= value <= 0 for value in got)
        print(f"全部落在 -100..0 dBm：{in_range}")

    if len(got) == 0:
        print("\n判据 2：完全读不到。本库报 None 是正确行为，按判据 2 落文档。")
    elif len(got) >= int(len(readings) * 0.9):
        print("\n判据 1：能稳定读到。按判据 1 落文档。")
    else:
        print(
            "\n判据 3：时好时坏。这一条要写进 docstring —— 调用方拿到的 None "
            "有时只是「这次没读到」，不能当成链路断了。"
        )
    return 0


if __name__ == "__main__":
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SECONDS
    raise SystemExit(asyncio.run(main(seconds)))
