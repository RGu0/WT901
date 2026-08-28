"""真机取证：连接期到底能不能读到 RSSI，以及它随距离怎么变（RAY-310）。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑**——代理的 Bash 工具下
CoreBluetooth 不授权，脚本会在扫描那一步就失败。

    ./dev run python tools/probe_rssi.py [秒数] [--address <地址>]

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
from wt901.discovery import DiscoveredDevice, scan
from wt901.errors import TransportError
from wt901.protocol.registers import ReturnRate

DEFAULT_SECONDS = 60

SCAN_TIMEOUT = 15.0
"""秒。与 ``tools/`` 里其它探测脚本一致。默认的 5 秒在设备刚上电或信号偏弱时
常常扫不到，而扫不到与连不上的表现完全不同，不该被一个太短的超时混在一起。"""

_CONNECT_HELP = """
连不上时按这个顺序查：

  1. 设备是不是已经被别的东西连着？维特上位机、另一个还在跑的脚本、或者上一次
     没退干净的进程都会占着它。BLE 外设同一时刻只接受一个中心设备。
  2. 设备离主机多远？上面列出的 rssi 低于约 -85 dBm 时连接常常超时。
  3. 设备电量是不是快没了？低电时广播还在、连接会失败。
  4. 都不是的话跑 tools/smoke_ble.py —— 它不按名字过滤，能分开「设备不在范围」
     与「蓝牙本身没工作」。
"""


def take_address(argv: list[str]) -> tuple[str | None, list[str]]:
    """从参数里取出 ``--address <地址>``，返回 ``(地址, 其余参数)``。"""
    if "--address" not in argv:
        return None, argv
    index = argv.index("--address")
    if index + 1 >= len(argv):
        raise SystemExit("--address 后面要跟一个地址")
    return argv[index + 1], argv[:index] + argv[index + 2 :]


async def pick_device(address: str | None) -> DiscoveredDevice | None:
    """扫描并选一台设备，**把扫到的都打印出来**。

    连接失败时最要紧的信息是「扫到了什么、信号多强」。
    """
    devices = await scan(SCAN_TIMEOUT)
    if not devices:
        print(f"扫描 {SCAN_TIMEOUT:.0f} s 没有发现 WT 设备。")
        print(_CONNECT_HELP)
        return None

    print(f"扫描到 {len(devices)} 台 WT 设备：")
    for item in devices:
        print(f"  {item.address}  rssi={item.rssi}  name={item.name!r}")

    if address is not None:
        for item in devices:
            if item.address == address:
                print(f"\n按 --address 选中 {address}\n")
                return item
        print(f"\n扫到的设备里没有 {address}。")
        return None

    # 取信号最强的那台，不是列表里的第一台：台架上那台几乎总是最近的一台，
    # 而扫描结果的顺序不保证任何东西。
    chosen = max(devices, key=lambda d: d.rssi if d.rssi is not None else -999)
    print(f"\n选中信号最强的：{chosen.address}（rssi={chosen.rssi}）")
    if len(devices) > 1:
        print("要指定别的设备：--address <地址>")
    print()
    return chosen


async def connect_probe(address: str | None) -> tuple[WT901Device, int | None] | None:
    """连接，失败时重新扫描再试一次。返回 ``(设备, 扫描期 rssi)``。

    macOS 上扫描得到的句柄是 CoreBluetooth 的会话内标识，可能在两次扫描之间失效；
    重新扫描拿一个新句柄比拿旧的重试有意义。
    """
    for attempt in (1, 2):
        target = await pick_device(address)
        if target is None:
            return None
        try:
            return await WT901Device.connect(target), target.rssi
        except TransportError as exc:
            print(f"\n第 {attempt} 次连接失败：{exc}")
            if attempt == 2:
                print(_CONNECT_HELP)
                return None
            print("重新扫描后再试一次（macOS 上句柄可能已失效）…\n")
    return None


async def main(seconds: int, address: str | None) -> int:
    connected = await connect_probe(address)
    if connected is None:
        return 1
    device_handle, scan_rssi = connected

    print(f"扫描期 RSSI={scan_rssi} dBm")
    print("（扫描期那个值来自广播包，与下面连接期的读取不是同一条路径。）\n")

    async with device_handle as device:
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
    chosen_address, rest = take_address(sys.argv[1:])
    seconds = int(rest[0]) if rest else DEFAULT_SECONDS
    raise SystemExit(asyncio.run(main(seconds, chosen_address)))
