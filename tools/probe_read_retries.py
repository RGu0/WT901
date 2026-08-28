"""真机取证：连接初期的寄存器读是不是真的更不可靠（RAY-313）。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑。**

    ./dev run python tools/probe_read_retries.py [每相每寄存器的读取次数] [--address <地址>]

默认 30，即每次连接 3 相 × 2 寄存器 × 30 = 180 次读。

## 要回答的问题

官方 SDK 的读数据线程里有两处不寻常的写法：连上后先 ``Thread.sleep(5000)`` 才
开始读任何寄存器；``0x72`` 在还没读到时同一条读指令**紧挨着发两次**（其余寄存器
都只发一次）。两处都指向「刚连上的一段时间里寄存器读更容易失败」。

本库的 ``DEFAULT_READ_RETRIES = 2`` 可能一直在悄悄吸收这件事，所以从没被咬到，
也就从没被当成一个已知的设备行为记录下来。

## 判据（已在 RAY-313 预注册，本脚本只是执行它）

**第 0 条，不可省：** ``read_retries = 0``。本库默认重试 2 次会把首次失败整个
吞掉，测出来的会是「重试之后的成功率」——那个数一直是 100%，什么也说明不了。
脚本会把这个设置打出来。

### 三相，不是两相

=========  ==================  ==========================================
相         起始                内容
=========  ==================  ==========================================
early      ``open()`` 一返回   交替读 ``0x40`` / ``0x72``，各 N 次
late       t ≥ 5.0 s           同上
control    t ≥ 30.0 s          同上
=========  ==================  ==========================================

**为什么要第三相**：只有 early 与 late 时，晚相永远在早相之后。若失败是**累计
活动量**导致的而不是**连接时长**导致的，两者会被混为一谈。control 相与 late 相
基本一致，才说明变量确实是「距连接建立多久」。

### 为什么读两个寄存器

SDK 的两个迹象指向的不是同一件事：5 秒说的是**时间窗口**（所有寄存器都受影响），
``0x72`` 双发说的可能只是**这一个寄存器难读**。只测一个，两种成因产生的数据长得
一样。

===================  ===================  ==================================
``0x40`` 早期更差？  ``0x72`` 早期更差？  结论
===================  ===================  ==================================
是                   是                   时间窗口效应，SDK 那 5 秒有依据
否                   是                   只是 ``0x72`` 难读，与连接时长无关
是                   否                   反常，需重测
否                   否                   两个迹象都是历史遗留
===================  ===================  ==================================

### 判定门槛（预注册，不得事后调整）

1. **显著**：``early 失败率 − late 失败率 ≥ 15 个百分点``。N = 30 时这大致是这个
   样本量能可靠分辨的下限。**比这更小的差别判为「测不出」，不判为「不存在」**
   ——这两句话不一样。
2. **耗时**：``early 中位数 ≥ late 中位数 × 1.5`` 视为显著。中位数与 p90 都记。
3. **失败模式**：超时与「回帧内容异常」（如全零，见 RAY-305）分开计。差异若主要
   来自内容异常，那不是「读不可靠」而是「早期回帧内容不可信」，处置完全不同
   （前者加重试，后者加校验）。
4. **作废条件**：late 相任一寄存器失败率 > 5% → 本次连接作废（链路本身就烂，
   不能拿它谈早期）。
5. **样本量**：2 台设备 × 每台 3 次独立连接 = 6 次运行，方向一致 ≥ 4 次才算数。
   本脚本跑一次连接，重复由人执行。

三相之间**不做任何寄存器写**——写事务有自己的时序影响，那是另一个变量。
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

from wt901.device import WT901Device
from wt901.discovery import DiscoveredDevice, scan
from wt901.errors import TransportError, TransportTimeoutError, WT901Error
from wt901.protocol.registers import Register

DEFAULT_READS = 30
LATE_AT = 5.0
"""秒。官方 SDK 睡的就是这个数量级。"""
CONTROL_AT = 30.0
"""秒。用来分开「连接时长」与「累计活动量」。"""

SIGNIFICANT_FAILURE_GAP = 0.15
"""失败率差 >= 15 个百分点算显著。低于它判为「测不出」，不是「不存在」。"""
SIGNIFICANT_LATENCY_RATIO = 1.5
"""early 中位耗时 >= late 的这个倍数算显著。"""
BAD_LINK_FAILURE_RATE = 0.05
"""late 相失败率 > 5% → 本次连接作废。"""

PROBED = (Register.TEMPERATURE, Register.MAGTYPE)


def summarize(results: list[dict[str, object]]) -> dict[str, object]:
    """把一组读取结果归纳成失败率与耗时分位。"""
    total = len(results)
    timeouts = sum(1 for item in results if item["outcome"] == "timeout")
    anomalies = sum(1 for item in results if item["outcome"] == "anomalous")
    errors = sum(1 for item in results if item["outcome"] == "error")
    successes = [
        float(item["elapsed"]) for item in results if item["outcome"] == "ok"
    ]
    failures = timeouts + anomalies
    return {
        "reads": total,
        "timeouts": timeouts,
        "anomalous": anomalies,
        "errors": errors,
        "failure_rate": failures / total if total else 0.0,
        "median_s": statistics.median(successes) if successes else None,
        "p90_s": (
            sorted(successes)[min(len(successes) - 1, int(len(successes) * 0.9))]
            if successes
            else None
        ),
    }


def judge(
    early: dict[str, object], late: dict[str, object], control: dict[str, object]
) -> str:
    """按预注册门槛判读**一个寄存器在一次连接内**的三相对比。"""
    errors = sum(int(phase["errors"]) for phase in (early, late, control))
    if errors:
        return (
            f"作废：出现 {errors} 次非超时错误（链路断开一类）。那不是本实验要量的"
            "东西，掺进来会污染「早期读不可靠」的证据。重跑。"
        )

    late_rate = float(late["failure_rate"])
    if late_rate > BAD_LINK_FAILURE_RATE:
        return (
            f"作废：late 相自己就失败 {late_rate:.0%} > {BAD_LINK_FAILURE_RATE:.0%}，"
            "这次连接的链路条件不允许谈早期效应。重跑。"
        )

    gap = float(early["failure_rate"]) - late_rate
    control_gap = abs(float(control["failure_rate"]) - late_rate)
    notes: list[str] = []

    if control_gap >= SIGNIFICANT_FAILURE_GAP:
        notes.append(
            f"⚠ control 相与 late 相相差 {control_gap:.0%}，"
            "说明变量可能不是「距连接建立多久」而是累计活动量——本实验的前提要重估"
        )

    early_median = early["median_s"]
    late_median = late["median_s"]
    if (
        isinstance(early_median, float)
        and isinstance(late_median, float)
        and late_median > 0
        and early_median / late_median >= SIGNIFICANT_LATENCY_RATIO
    ):
        notes.append(
            f"耗时显著：early 中位 {early_median * 1000:.0f} ms 是 late 的 "
            f"{early_median / late_median:.2f} 倍"
        )

    if gap >= SIGNIFICANT_FAILURE_GAP:
        mode = (
            "内容异常为主 —— 这不是「读不可靠」，是「早期回帧内容不可信」，"
            "处置是加校验而不是加重试"
            if int(early["anomalous"]) > int(early["timeouts"])
            else "超时为主 —— 重试是对症的处置"
        )
        verdict = f"早期更不可靠：失败率高出 {gap:.0%}（≥ 15 个百分点）。{mode}"
    else:
        # 没有「早期反而显著更好」这一支：上面的作废闸已经把 late 失败率卡在 5%
        # 以内，所以 gap 最低只能到 −5%，永远够不到 −15% 的显著门槛。写一支
        # 够不到的分支只会让读的人以为它会发生。
        verdict = (
            f"测不出：失败率差 {gap:+.0%}，未达 15 个百分点。"
            "**这是「测不出」，不是「不存在」**——N=30 分辨不了更小的差别。"
        )
    return verdict + ("\n    " + "\n    ".join(notes) if notes else "")


async def _one_read(device: WT901Device, register: int) -> dict[str, object]:
    started = time.monotonic()
    try:
        response = await device.registers.read(register)
    except TransportTimeoutError:
        return {
            "register": register,
            "outcome": "timeout",
            "elapsed": time.monotonic() - started,
        }
    except WT901Error as exc:
        # 非超时的错误（链路断了、写失败）**不是**本实验要量的东西。把它记成
        # timeout 会往「早期读不可靠」的证据里掺入完全不同的成因。单列一态，
        # 并让判读见到它就作废这次连接。
        return {
            "register": register,
            "outcome": "error",
            "elapsed": time.monotonic() - started,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    elapsed = time.monotonic() - started
    # 回帧内容异常与超时成因不同，必须分开计（RAY-305 处理过全零那一类）。
    outcome = "anomalous" if all(value == 0 for value in response.values) else "ok"
    return {"register": register, "outcome": outcome, "elapsed": elapsed}


async def _phase(
    device: WT901Device, reads: int
) -> dict[int, list[dict[str, object]]]:
    """交替读两个寄存器，各 ``reads`` 次。"""
    collected: dict[int, list[dict[str, object]]] = {reg: [] for reg in PROBED}
    for _ in range(reads):
        for register in PROBED:
            collected[register].append(await _one_read(device, register))
    return collected


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


async def connect_probe(address: str | None) -> WT901Device | None:
    """连接，失败时重新扫描再试一次。

    macOS 上扫描得到的句柄是 CoreBluetooth 的会话内标识，可能在两次扫描之间失效；
    重新扫描拿一个新句柄比拿旧的重试有意义。
    """
    for attempt in (1, 2):
        target = await pick_device(address)
        if target is None:
            return None
        try:
            return await WT901Device.connect(target)
        except TransportError as exc:
            print(f"\n第 {attempt} 次连接失败：{exc}")
            if attempt == 2:
                print(_CONNECT_HELP)
                return None
            print("重新扫描后再试一次（macOS 上句柄可能已失效）…\n")
    return None


async def main(reads: int, address: str | None) -> int:
    connected = await connect_probe(address)
    if connected is None:
        return 1

    async with connected as device:
        # 原点必须取在**建链完成之后**。BLE 连接本身要花一秒上下，把它算进去会让
        # 「t ≥ 5 s」从一个错误的原点起算——那正是本实验要量的那段窗口。
        connected_at = time.monotonic()

        # 第 0 条：关掉重试。不关的话测出来的是「重试之后的成功率」，那个数一直
        # 是 100%，什么也说明不了。
        device.registers.read_retries = 0
        device_id = device.device_id
        print(f"设备        {device_id}")
        print(f"read_retries {device.registers.read_retries}  ← 必须是 0")
        print(f"read_timeout {device.registers.read_timeout} s")
        print(f"每相每寄存器 {reads} 次\n")

        phases: dict[str, dict[int, list[dict[str, object]]]] = {}

        print("early 相：现在开始（三相之间不做任何寄存器写）")
        phases["early"] = await _phase(device, reads)

        remaining = LATE_AT - (time.monotonic() - connected_at)
        if remaining > 0:
            print(f"等到 t = {LATE_AT} s（还剩 {remaining:.1f} s）…")
            await asyncio.sleep(remaining)
        print("late 相")
        phases["late"] = await _phase(device, reads)

        remaining = CONTROL_AT - (time.monotonic() - connected_at)
        if remaining > 0:
            print(f"等到 t = {CONTROL_AT} s（还剩 {remaining:.1f} s）…")
            await asyncio.sleep(remaining)
        print("control 相")
        phases["control"] = await _phase(device, reads)

    summaries: dict[str, dict[str, dict[str, object]]] = {}
    print("\n--- 归纳 ---")
    for register in PROBED:
        name = f"0x{register:02X}"
        summaries[name] = {
            phase: summarize(data[register]) for phase, data in phases.items()
        }
        print(f"\n寄存器 {name}")
        print(f"  {'相':<9} {'失败率':>7} {'超时':>5} {'异常':>5} {'中位':>9} {'p90':>9}")
        for phase in ("early", "late", "control"):
            s = summaries[name][phase]
            median = f"{float(s['median_s']) * 1000:.0f} ms" if s["median_s"] else "-"
            p90 = f"{float(s['p90_s']) * 1000:.0f} ms" if s["p90_s"] else "-"
            print(
                f"  {phase:<9} {float(s['failure_rate']):>6.0%} "
                f"{int(s['timeouts']):>5} {int(s['anomalous']):>5} "
                f"{median:>9} {p90:>9}"
            )
        print(
            "  判读："
            + judge(
                summaries[name]["early"],
                summaries[name]["late"],
                summaries[name]["control"],
            )
        )

    out = Path("read_retry_probe.json")
    out.write_text(
        json.dumps(
            {
                "issue": "RAY-313",
                "device": device_id,
                "read_retries": 0,
                "reads_per_phase_per_register": reads,
                "summaries": summaries,
                "raw": {
                    phase: {f"0x{reg:02X}": data[reg] for reg in PROBED}
                    for phase, data in phases.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n原始数据已写入 {out.resolve()}（连同判读一起进 evidence）")
    print(
        "\n这只是**一次连接**。判据要求 2 台设备 × 每台 3 次独立连接，"
        "方向一致 ≥ 4/6 次才算数。"
    )
    return 0


if __name__ == "__main__":
    chosen_address, rest = take_address(sys.argv[1:])
    raise SystemExit(
        asyncio.run(
            main(int(rest[0]) if rest else DEFAULT_READS, chosen_address)
        )
    )
