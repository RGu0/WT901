"""真机取证：磁场原始计数值到 µT 的换算系数，三份上游资料哪一份对（RAY-312）。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑。**

    ./dev run python tools/probe_mag_scale.py <当地 IGRF 总强度 µT> [采样数]

采样数默认 300。IGRF 总强度从 NOAA 的 IGRF 计算器取（输入经纬度与日期，取
**总强度 F**），例如上海约 49.6 µT、北京约 54.3 µT——**别用这两个数代替查询**，
它们只是让你知道量级。

## 三个候选

同一个量，三份官方资料给三个答案：

======================  ==================================  ==============
资料                    它说的换算                          折算
======================  ==================================  ==============
本型号协议文档          原始值单位就是 mG，与 ``0x72`` 无关   × 0.1
C# / Android SDK        按 ``0x72`` 分档（本库当前实现）      视 type 而定
官方 Python 示例        写死 ``raw / 120``                    ≈ × 0.00833
======================  ==================================  ==============

## 判据（已在 Issue 里预注册，本脚本只是执行它）

**第 0 步先读 ``0x72``。** 能不能用磁场量级分开候选项，完全取决于它：

=======  ==========  ====================  ==============================
``0x72`` SDK 系数    SDK ÷ 协议文档(0.1)   地磁量级能否分开这两者
=======  ==========  ====================  ==============================
2        0.15        1.50                  勉强
3        0.013       0.13                  能（差 7.7 倍）
4        0.058       0.58                  勉强（差 1.72 倍）
5        0.098       0.98                  **不能**（差 1.02 倍）
6        1/150       0.067                 能（差 15 倍）
7        0.020       0.20                  能（差 5 倍）
=======  ==========  ====================  ==============================

``0x72`` 读到 5 的话，本方法注定分不开，只能走层次一（Windows + 维特上位机）。
脚本会在采集之前就告诉你这件事，别白转五分钟。

**采集方式：转动拟合球面，不是静置读一次。** 静置单点读数里，硬磁偏置与真实场
强不可分，而偏置量级常与地磁相当——那会把 1.5 倍的差别彻底淹没。把设备绕各个
方向缓慢转一圈，对点云拟合球面：球心是硬磁偏置（丢弃），**球半径就是真实场强
对应的计数值**。

    实测系数 = IGRF 总强度(µT) / 球半径(counts)

**判读门槛（预注册，不得事后调整）：**

1. 拟合残差 RMS ≤ 半径的 8%；姿态覆盖度过关（见下）。不过关就不判读，重采。
2. 实测系数与三个候选各算相对偏差：

   * 唯一一个 ≤ 15% 且其余 > 30% → 判定该候选成立。
   * 两个都 ≤ 15% → 判定「本方法分不开」，转层次一。**这是预注册的结果之一，
     不是失败。**
   * 三个都 > 30% → 「三份资料全不对」。这是最有价值的结果，原样记录，按方针 3
     （实测是唯一标准）处理。
3. 至少 2 台设备、每台 2 次独立采集（重新摆放、重新转动）。四次的实测系数极差
   > 20% 则整轮作废——那说明测量本身不稳。

## 姿态覆盖度：两个都要过

拟合一个球面而只覆盖它的一小块，会得到一个数值上收敛、物理上没意义的半径。

* **八卦限占用**：以拟合出的球心为原点，八个卦限里至少 6 个各有 ≥ 5 个点。
* **单位向量均值模长 ≤ 0.35**：点云均匀覆盖球面时这个值趋近 0；只扫了半个球面
  时约 0.5。它比卦限计数更能抓住「转了很多但只在一个平面里转」。

## 必须一并记进 evidence

设备的 ``0x72`` 读数（**结论只对这一档成立，不得外推**）、采集地经纬度与日期、
IGRF 参考值、完整点云、拟合的球心/半径/残差、以及设备离最近铁磁物体多远。
脚本会把点云写成 JSON，直接进 evidence 目录。
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path

from wt901.device import WT901Device
from wt901.discovery import DiscoveredDevice, scan
from wt901.errors import TransportError
from wt901.protocol import units

DEFAULT_SAMPLES = 300
DOC_COEFFICIENT = 0.1
"""本型号协议文档：原始值单位就是 mG，即 µT = raw × 0.1。"""
PYTHON_COEFFICIENT = 1 / 120
"""官方 Python 示例写死的 raw / 120。"""

RESIDUAL_LIMIT = 0.08
"""拟合残差 RMS 相对半径的上限。"""
MEAN_DIRECTION_LIMIT = 0.35
"""单位向量均值的模长上限。均匀覆盖趋近 0，半球覆盖约 0.5。"""
MIN_OCTANTS = 6
"""八个卦限里至少要有这么多个各含 >= 5 个点。"""
ACCEPT_TOLERANCE = 0.15
"""相对偏差 <= 这个值算「对上了」。"""
REJECT_TOLERANCE = 0.30
"""相对偏差 > 这个值算「排除」。介于两者之间的是「不确定」。"""



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

    连接失败时最要紧的信息是「扫到了什么、信号多强」。此前这里直接取
    ``devices[0]`` 且连接前什么都不打印，超时的回溯里看不出设备到底在不在范围内。
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


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """带部分主元的高斯消元。本仓库不依赖 numpy，4×4 手写即可。"""
    size = len(rhs)
    augmented = [row[:] + [rhs[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        for row in range(column + 1, size):
            factor = augmented[row][column] / augmented[column][column]
            for k in range(column, size + 1):
                augmented[row][k] -= factor * augmented[column][k]
    result = [0.0] * size
    for row in reversed(range(size)):
        total = augmented[row][size] - sum(
            augmented[row][k] * result[k] for k in range(row + 1, size)
        )
        result[row] = total / augmented[row][row]
    return result


def fit_sphere(
    points: list[tuple[float, float, float]],
) -> tuple[tuple[float, float, float], float] | None:
    """最小二乘拟合球面，返回 ``(球心, 半径)``。

    用线性化形式：``x²+y²+z² = 2ax + 2by + 2cz + d``，其中
    ``d = r² − a² − b² − c²``。这样是一个 4 参数线性最小二乘，不需要迭代，也就
    不会有「初值给坏了不收敛」这种在真机现场最难查的问题。
    """
    if len(points) < 4:
        return None
    normal = [[0.0] * 4 for _ in range(4)]
    rhs = [0.0] * 4
    for x, y, z in points:
        row = [2 * x, 2 * y, 2 * z, 1.0]
        target = x * x + y * y + z * z
        for i in range(4):
            for j in range(4):
                normal[i][j] += row[i] * row[j]
            rhs[i] += row[i] * target
    solution = _solve(normal, rhs)
    if solution is None:
        return None
    a, b, c, d = solution
    squared = d + a * a + b * b + c * c
    if squared <= 0:
        return None
    return (a, b, c), math.sqrt(squared)


def coverage(
    points: list[tuple[float, float, float]], center: tuple[float, float, float]
) -> tuple[int, float]:
    """返回 ``(占用的卦限数, 单位向量均值的模长)``。"""
    octants: dict[tuple[bool, bool, bool], int] = {}
    sums = [0.0, 0.0, 0.0]
    counted = 0
    for x, y, z in points:
        dx, dy, dz = x - center[0], y - center[1], z - center[2]
        norm = math.sqrt(dx * dx + dy * dy + dz * dz)
        if norm == 0:
            continue
        key = (dx >= 0, dy >= 0, dz >= 0)
        octants[key] = octants.get(key, 0) + 1
        sums[0] += dx / norm
        sums[1] += dy / norm
        sums[2] += dz / norm
        counted += 1
    occupied = sum(1 for count in octants.values() if count >= 5)
    if counted == 0:
        return occupied, 1.0
    mean = math.sqrt(sum(value * value for value in sums)) / counted
    return occupied, mean


def _sdk_coefficient(mag_type: int) -> float | None:
    """本库当前实现对该 type 用的系数（对 raw=1 求值即得）。"""
    return units.magnetic_field_to_ut(mag_type, 1)


def _separability(mag_type: int) -> str:
    sdk = _sdk_coefficient(mag_type)
    if sdk is None:
        return "该 type 不在本库已知分档内，SDK 那份对它没有说法"
    ratio = max(sdk / DOC_COEFFICIENT, DOC_COEFFICIENT / sdk)
    if ratio >= 2.0:
        return f"能分开 SDK 与协议文档（差 {ratio:.1f} 倍）"
    if ratio >= 1.4:
        return f"勉强能分开（差 {ratio:.2f} 倍），拟合质量必须过关"
    return (
        f"⚠ 分不开（SDK 与协议文档只差 {ratio:.2f} 倍）。"
        "本方法对这台设备无效，请走层次一（Windows + 维特上位机）"
    )


def judge(measured: float, mag_type: int) -> tuple[str, list[tuple[str, float, float]]]:
    """按预注册门槛判读。返回 ``(结论, [(候选名, 系数, 相对偏差)])``。"""
    candidates: list[tuple[str, float]] = [("协议文档 ×0.1", DOC_COEFFICIENT)]
    sdk = _sdk_coefficient(mag_type)
    if sdk is not None:
        candidates.append((f"SDK type {mag_type} ×{sdk:.4g}", sdk))
    candidates.append(("Python 示例 /120", PYTHON_COEFFICIENT))

    scored = [
        (name, value, abs(measured - value) / value) for name, value in candidates
    ]
    accepted = [item for item in scored if item[2] <= ACCEPT_TOLERANCE]
    rejected = [item for item in scored if item[2] > REJECT_TOLERANCE]

    if len(accepted) == 1 and len(rejected) == len(scored) - 1:
        return f"判定成立：{accepted[0][0]}", scored
    if len(accepted) >= 2:
        return (
            "本方法分不开："
            + "、".join(item[0] for item in accepted)
            + " 都落在 15% 以内。转层次一。",
            scored,
        )
    if not accepted and len(rejected) == len(scored):
        return "三份资料全不对——最有价值的结果，按方针 3 处理，原样记录。", scored
    return "不确定：有候选落在 15%–30% 之间，判据不判读这种情形。重采。", scored


async def main(igrf_ut: float, samples: int, address: str | None) -> int:
    connected = await connect_probe(address)
    if connected is None:
        return 1

    async with connected as device:
        device_id = device.device_id
        first = await device.telemetry.read_magnetic_field()
        mag_type = first.mag_type
        sdk = _sdk_coefficient(mag_type)

        print(f"设备          {device.device_id}")
        print(f"0x72 (MAGTYPE) {mag_type}")
        print(f"SDK 系数       {sdk if sdk is not None else '未知分档'}")
        print(f"协议文档系数   {DOC_COEFFICIENT}")
        print(f"Python 示例    {PYTHON_COEFFICIENT:.5f}")
        print(f"IGRF 参考      {igrf_ut} µT")
        print(f"\n第 0 步判读：{_separability(mag_type)}\n")

        print(
            f"现在开始采集 {samples} 组。**把设备绕各个方向缓慢转动**，尽量覆盖\n"
            "所有姿态（想象用它在空中画一个球）。远离笔记本、桌腿、手机。\n"
        )
        input("准备好后按回车开始…")

        points: list[tuple[float, float, float]] = []
        for index in range(samples):
            reading = await device.telemetry.read_magnetic_field()
            x, y, z = reading.raw[0], reading.raw[1], reading.raw[2]
            points.append((float(x), float(y), float(z)))
            if (index + 1) % 25 == 0:
                print(f"  {index + 1}/{samples}   最新 ({x}, {y}, {z})")

    fitted = fit_sphere(points)
    if fitted is None:
        print("球面拟合失败——点云退化（可能根本没转动）。")
        return 1
    center, radius = fitted

    residuals = [
        math.sqrt(
            (x - center[0]) ** 2 + (y - center[1]) ** 2 + (z - center[2]) ** 2
        )
        - radius
        for x, y, z in points
    ]
    rms = math.sqrt(sum(value * value for value in residuals) / len(residuals))
    occupied, mean_direction = coverage(points, center)

    print("\n--- 拟合 ---")
    print(f"球心（硬磁偏置） ({center[0]:.1f}, {center[1]:.1f}, {center[2]:.1f}) counts")
    print(f"半径             {radius:.1f} counts")
    print(f"残差 RMS         {rms:.1f} counts = 半径的 {rms / radius * 100:.1f}%")
    print(f"占用卦限         {occupied}/8（要求 ≥ {MIN_OCTANTS}）")
    print(f"单位向量均值模长 {mean_direction:.3f}（要求 ≤ {MEAN_DIRECTION_LIMIT}）")

    quality_ok = (
        rms / radius <= RESIDUAL_LIMIT
        and occupied >= MIN_OCTANTS
        and mean_direction <= MEAN_DIRECTION_LIMIT
    )

    payload = {
        "issue": "RAY-312",
        "device": device_id,
        "mag_type": mag_type,
        "igrf_total_ut": igrf_ut,
        "samples": len(points),
        "center": list(center),
        "radius_counts": radius,
        "residual_rms_counts": rms,
        "octants_occupied": occupied,
        "mean_direction_norm": mean_direction,
        "quality_ok": quality_ok,
        "points": points,
    }
    out = Path("mag_scale_points.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n点云已写入 {out.resolve()}（请连同判读一起进 evidence）")

    if not quality_ok:
        print(
            "\n⚠ 拟合质量未过关。按预注册判据，**不判读**，请重新采集：转动要覆盖\n"
            "  所有姿态，且全程远离铁磁物体。"
        )
        return 2

    measured = igrf_ut / radius
    verdict, scored = judge(measured, mag_type)
    print("\n--- 判读 ---")
    print(f"实测系数 = {igrf_ut} / {radius:.1f} = {measured:.5f} µT/count\n")
    for name, value, deviation in scored:
        mark = "✅" if deviation <= ACCEPT_TOLERANCE else (
            "❌" if deviation > REJECT_TOLERANCE else "…"
        )
        print(f"  {mark} {name:<24} {value:.5f}   相对偏差 {deviation * 100:5.1f}%")
    print(f"\n{verdict}")
    print(
        "\n提醒：这个结论**只对 0x72 = "
        f"{mag_type} 这一档成立**，不得外推到其它 type。"
        "\n还需要：至少 2 台设备 × 每台 2 次独立采集，四次实测系数极差 > 20% 则整轮作废。"
    )
    return 0


if __name__ == "__main__":
    chosen_address, rest = take_address(sys.argv[1:])
    if not rest:
        print(__doc__)
        print(
            "用法：./dev run python tools/probe_mag_scale.py "
            "<IGRF 总强度 µT> [采样数] [--address <地址>]"
        )
        raise SystemExit(2)
    raise SystemExit(
        asyncio.run(
            main(
                float(rest[0]),
                int(rest[1]) if len(rest) > 1 else DEFAULT_SAMPLES,
                chosen_address,
            )
        )
    )
