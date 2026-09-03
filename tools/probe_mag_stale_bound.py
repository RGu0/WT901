"""真机取证：6 轴下那个陈旧的磁场值能不能跨断电存活（RAY-344 scope 2）。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑。**

    ./dev run python tools/probe_mag_stale_bound.py before --address <地址>
    （用户给设备断电，再上电）
    ./dev run python tools/probe_mag_stale_bound.py after  --address <地址>

断电会中断进程，所以做成两相两次调用；``before`` 相把读数写进一份 JSON，
``after`` 相读回来比对。

## 要回答的问题

6 轴模式（``0x24 = 1``）下磁场寄存器停在一个固定值上不再更新（RAY-344 验收标准
第 1 条，两台设备实测）。已知那个值能跨越十几个小时、一整段 9 轴运行和多次断连
重连。**断电是最后一道**：

* 连断电都保留 → 它在**非易失存储**里，「陈旧」真的没有上界。
* 不保留 → 上界是「本次上电以来」。

## 判据（已在 RAY-344 预注册，本脚本只是执行它）

**全程不改任何配置。** 改算法模式会污染这次测量。

### 作废条件（先于一切判读）

1. 任一组 5 次读数**内部不一致** → 该次作废。前提是设备处于锁住状态，读数会变
   说明它没锁住，这次测的不是要测的东西。
2. 任一时刻 ``0x24 != 1`` → 该次作废。脚本会中止而不是去改它。

### 判定

=========================  ==========================================
``V_after`` 与 ``V_before``  判定
=========================  ==========================================
逐字节相同                 **跨断电保留**——值在非易失存储里，陈旧无上界
不同，且非全零             **不保留**——上界是「本次上电以来」；开机时
                           采样一次后锁住
全零 ``(0, 0, 0)``         **不保留，复位为全零**——上界同上，形态不同
=========================  ==========================================

**样本量：2 台设备各 1 轮，两台必须落在同一格。** 不一致则各重测一轮；仍不一致
判为「测不出」，如实记录，**不取其一**。

### 一个对照

``after`` 相要求**把设备摆成明显不同的朝向**。若设备在开机时采样一次然后锁住，
``V_after`` 会反映开机时的朝向而非断电前的——那与「保留旧值」区分得开。

## 三种结果都是完整交付

* 保留 → ``docs/protocol.md`` §5.7.1 措辞从「没有已知上界」收紧成具体事实。
* 不保留 → §5.7.1 写明上界是「本次上电以来」，比现在**更弱但更准**。
* 测不出 → §5.7.1 现有措辞（「可能任意陈旧」）**一个字不改**，它在三种结果下都
  成立。

**本 scope 不改 ``may_be_stale`` 的语义或实现**——它不依赖这次的结果。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from wt901.device import WT901Device
from wt901.discovery import DiscoveredDevice, scan
from wt901.errors import TransportError
from wt901.protocol.registers import AlgorithmMode, Register

READS = 5
"""每相读几次。锁住时它们必须全同——这是判读的前提，不是判据本身。"""

INTERVAL = 1.0
SCAN_TIMEOUT = 15.0

_CONNECT_HELP = """
连不上时按这个顺序查：

  1. 设备是不是已经被别的东西连着？BLE 外设同一时刻只接受一个中心设备。
  2. 上面列出的 rssi 低于约 -85 dBm 时连接常常超时。
  3. 低电时广播还在、连接会失败。
  4. 都不是的话跑 tools/smoke_ble.py。
"""


def _state_path(address: str) -> Path:
    return Path(f"mag_stale_bound_{address[:8]}.json")


async def _pick(address: str | None) -> DiscoveredDevice | None:
    devices = await scan(SCAN_TIMEOUT)
    if not devices:
        print(f"扫描 {SCAN_TIMEOUT:.0f} s 没有发现 WT 设备。")
        print(_CONNECT_HELP)
        return None
    for item in devices:
        print(f"  {item.address}  rssi={item.rssi}  name={item.name!r}")
    if address is not None:
        for item in devices:
            if item.address == address:
                print(f"\n按 --address 选中 {address}\n")
                return item
        print(f"\n扫到的设备里没有 {address}。")
        return None
    chosen = max(devices, key=lambda d: d.rssi if d.rssi is not None else -999)
    print(f"\n选中信号最强的：{chosen.address}（rssi={chosen.rssi}）\n")
    return chosen


async def _connect(address: str | None) -> tuple[WT901Device, str] | None:
    for attempt in (1, 2):
        target = await _pick(address)
        if target is None:
            return None
        try:
            return await WT901Device.connect(target), target.address
        except TransportError as exc:
            print(f"\n第 {attempt} 次连接失败：{exc}")
            if attempt == 2:
                print(_CONNECT_HELP)
                return None
            print("重新扫描后再试一次（macOS 上句柄可能已失效）…\n")
    return None


async def _sample(device: WT901Device) -> list[tuple[int, ...]]:
    print(f"读 {READS} 次，每次间隔 {INTERVAL:.0f} 秒。**全程转动设备。**")
    seen: list[tuple[int, ...]] = []
    for index in range(READS):
        reading = await device.telemetry.read_magnetic_field()
        raw = tuple(reading.raw)
        seen.append(raw)
        print(f"  {index + 1}  {raw}")
        if index < READS - 1:
            await asyncio.sleep(INTERVAL)
    return seen


async def main(phase: str, address: str | None) -> int:
    connected = await _connect(address)
    if connected is None:
        return 1
    device_handle, resolved = connected

    async with device_handle as device:
        algorithm = await device.registers.read_value(Register.ALGORITHM)
        print(f"0x24 ALGORITHM = {algorithm}")
        if algorithm != int(AlgorithmMode.SIX_AXIS):
            print(
                f"\n⚠ 作废条件 2：本判据要求设备处于 6 轴（0x24 = "
                f"{int(AlgorithmMode.SIX_AXIS)}），实际是 {algorithm}。\n"
                "  **本脚本不去改它**——改配置会污染这次测量。请先确认设备状态。"
            )
            return 2
        readings = await _sample(device)

    unique = {tuple(item) for item in readings}
    if len(unique) != 1:
        print(
            f"\n⚠ 作废条件 1：{READS} 次读数不一致（{len(unique)} 个不同值）。\n"
            "  本判据的前提是设备处于锁住状态；读数会变说明它没锁住，这次测的不是\n"
            "  要测的东西。本次作废，不判读。"
        )
        return 2
    value = readings[0]
    print(f"\n{phase} 相读数：{value}（{READS} 次全同）")

    path = _state_path(resolved)
    if phase == "before":
        path.write_text(
            json.dumps(
                {"issue": "RAY-344", "device": resolved, "before": list(value)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"已写入 {path.resolve()}")
        print(
            "\n现在**给设备断电，再上电**，然后跑：\n"
            f"  ./dev run python tools/probe_mag_stale_bound.py after --address {resolved}\n"
            "⚠ 上电后**把设备摆成一个明显不同的朝向**再读——那是个对照。"
        )
        return 0

    if not path.exists():
        print(f"\n找不到 {path}：请先跑 before 相。")
        return 1
    saved = json.loads(path.read_text(encoding="utf-8"))
    before = tuple(saved["before"])
    print(f"before 相读数：{before}")

    print("\n--- 判读 ---")
    if value == before:
        print(
            "**跨断电保留** —— 逐字节相同。那个值在非易失存储里，「陈旧」没有上界。\n"
            "  → §5.7.1 的措辞可从「没有已知上界」收紧成具体事实。"
        )
    elif value == (0, 0, 0):
        print(
            "**不保留，复位为全零** —— 陈旧的上界是「本次上电以来」。\n"
            "  → §5.7.1 写明该上界。"
        )
    else:
        print(
            "**不保留** —— 断电后变成了另一个值，陈旧的上界是「本次上电以来」；\n"
            "  开机时采样一次后锁住。\n"
            "  → §5.7.1 写明该上界。"
        )
    print(
        "\n这只是**一台设备**。判据要求 2 台各 1 轮且落在同一格；不一致则各重测一轮，\n"
        "仍不一致判为「测不出」，不取其一。"
    )
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] not in {"before", "after"}:
        print("用法：probe_mag_stale_bound.py {before|after} [--address <地址>]")
        raise SystemExit(2)
    chosen = None
    if "--address" in argv:
        index = argv.index("--address")
        if index + 1 >= len(argv):
            raise SystemExit("--address 后面要跟一个地址")
        chosen = argv[index + 1]
    raise SystemExit(asyncio.run(main(argv[0], chosen)))
