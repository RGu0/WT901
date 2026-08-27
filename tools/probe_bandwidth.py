"""真机取证：带宽编码 ``0x1F = 0x03`` 的抗混叠截止频率到底是多少。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑，设备要静置。**

    ./dev run python tools/probe_bandwidth.py [每档采集秒数]
    ./dev run python tools/probe_bandwidth.py --self-test   # 不需要设备

``ReturnRate`` 的核实办法是「写进去、量出来」，观测量与被测量是同一个东西。带宽不
一样：``0x1F`` 改的是**传感器内部抗混叠滤波器的截止频率**，它不改变样本速率，只
改变样本内容的频率成分。读寄存器只能读回自己刚写的编码，证明不了任何事。

## 方法（RAY-298 验收标准第 1 条要求开工前写定，已记进 Issue）

设备静置，回传速率固定在 ``0x0B``（已实测 198.43 Hz）。分别在带宽 ``0x00``（256 Hz）、
``0x03``（待判定）、``0x04``（20 Hz，已核实）下各采一段，对每段做 Welch periodogram，
然后看**比值**：

    R(f)  = P_code(f) / P_0x00(f)
    R̂(f) = R(f) / median(R(f), f ∈ [1, 8] Hz)

取 ``R̂`` 降到 0.5（−3 dB）的频率作为截止频率估计。在通带内 ``|H_0x00| ≈ 1``，所以
``R̂ = 0.5`` 就是 ``|H_code|² = 0.5``，即滤波器 −3 dB 带宽的标准定义——**与滤波器阶数
无关**，这正是选这个估计量的原因。

**归一化那一步不能省。** ``0x00`` 标称 256 Hz，而采样率只有 200 Hz，100 Hz 以上的
噪声必然折回到基带里，把参考谱整体抬高。合成数据上实测这个抬升约 3.7 倍
（通带里 ``R ≈ 0.27`` 而不是 1）。不归一化，所有估计都会被系统性推低。

**用加速度而不是角速度。** 滤波发生在量化之前，量化噪声不被它衰减——量化噪底决定
了这个实验的动态范围。加计 ±16 g（0.488 mg/LSB，噪声密度约 400 µg/√Hz）的传感器噪声
对量化噪声功率比约 800:1；角速度 ±2000 dps（0.061 dps/LSB，约 0.005 dps/√Hz）只有约
9:1。``--self-test`` 里按角速度量级合成的数据**根本找不到拐点**，所以角速度只打印、
不参与判读；它显示「无拐点」是预期结果，不是故障。

## 判据（由 ``--self-test`` 的合成标定确定，取证前写定，事后不得调整）

判据的区间不是拍脑袋定的。``--self-test`` 用已知截止频率的合成数据跑同一套分析流程，
量出这个估计量本身的偏差，区间由标定结果加余量得到。1–3 阶滤波器 ×3 组随机实现的
标定结果：

======  ==============  ====================================
真值    估计范围        说明
======  ==============  ====================================
10 Hz   11.0–13.8 Hz
20 Hz   17.0–25.3 Hz    ``0x04``，已核实档，用作方法自校验
42 Hz   41.8–66.4 Hz    偏高，因为参考档的混叠抬升随频率变化
98 Hz   无拐点          超过奈奎斯特，观测频段内看不到
188 Hz  无拐点
======  ==============  ====================================

合成数据的随机种子是固定的，所以这张表可以逐字复现——它不是「跑一次看到的样子」，
而是判据的出处。改动分析流程后 ``--self-test`` 会自己判断判据还成不成立。

1. **方法自校验**：``R̂_0x04`` 的 −3 dB 点落在 :data:`SELF_CHECK_BAND` → 方法有效。
   区间下沿同时把真值 10 Hz 排除在外，所以这一条不只是「数量级对」。
2. **主判据**：``R̂_0x03`` 落在 :data:`VERDICT_BAND` → 判定为 42 Hz，登记 ``HZ_42``。
   标定表显示 10 / 20 / 98 / 188 Hz 四个相邻档位全部落在这个区间之外，所以落进去
   只可能是 42 那一档。
3. 落在区间外或无拐点 → 本固件不按维特通用表映射这个编码，**不登记**（同 ``0x0A``）。
4. 第 1 条不过 → 方法无效，退回 Issue 描述里的方向 3。

**附加门槛两条**，任一不过就重采，不做「凑合判读」：

- 三档采集全程 ``dropped_samples`` 与 ``resync_count`` 必须为 0。丢样在时基上留洞。
- 通带平台必须平坦（``max/min ≤`` :data:`MAX_PLATEAU_SPREAD`）。**这一条是防环境振动的。**
  三个滤波器都原样放行的低频成分（脚步、桌面晃动、风扇）会同时出现在分子分母里，
  让平台里那一段的 ``R`` 冲向 1，而白噪声那部分只有 0.27——平台就被拉高，整条曲线
  被压低，拐点估计随之左移。合成数据上，干净时平台离散 ≤1.68，掺入低频污染后达 3.2。

## 写入策略

**全程 persist=False**，不写 flash。脚本开头读回设备原有的速率与带宽，结束时原样写
回（同样不保存），所以跑完之后设备的运行时状态与跑之前一致。

绕过 ``set_bandwidth()`` 的档位校验，直接调 ``registers.write()``——那个校验正是为了
防止**误**写未知编码，而这里是**有意**写，且写的正是本次要核实的那一个。
"""

from __future__ import annotations

import asyncio
import cmath
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass

from wt901.device import WT901Device
from wt901.discovery import scan
from wt901.protocol.registers import Bandwidth, Register, ReturnRate

PROBE_RATE = ReturnRate.HZ_200
"""0x0B。实测 198.43 Hz，是已核实档位里最高的一档——采样率越高，能看到的频段越宽。"""

REFERENCE_CODE = Bandwidth.HZ_256
"""作为比值的分母：它是三档里滤得最松的，最接近「不滤」。"""

CANDIDATE_CODE = 0x03
"""本次要核实的编码。"""

SELF_CHECK_CODE = Bandwidth.HZ_20
"""已核实的 20 Hz 档。先跑它，方法自校验就先有结论。"""

DEFAULT_SECONDS = 30.0
SETTLE = 0.8
"""秒。写完带宽后等滤波器状态稳定再开始采。"""

SEGMENT = 512
"""Welch 分段长度。200 Hz 下频率分辨率约 0.39 Hz，足以定位 20 Hz 与 42 Hz 的拐点。"""

PLATEAU_BAND = (1.0, 8.0)
"""Hz。归一化用的通带平台。下限避开静置时的低频漂移，上限远低于最低的候选截止频率。"""

SMOOTH_BINS = 9
"""比值曲线的滑动平均宽度（约 3.5 Hz）。22 段平均后单点比值的相对标准差约 30%，
不平滑会让 −3 dB 交点被单个噪声点带偏。"""

MIN_SEGMENTS = 12
"""少于这么多段就不判读：段数决定谱估计的方差，方差大了拐点就无从谈起。"""

MAX_PLATEAU_SPREAD = 2.5
"""通带平台的 ``max/min`` 上限。合成标定：干净 ≤1.86，掺低频污染 3.2。取 2.5 让两边都有余量。"""

REPORT_FREQUENCIES = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90)
"""打印用的频点。判读由人做，所以曲线本身要看得见，不能只给一个结论。"""

SELF_CHECK_BAND = (15.0, 32.0)
"""判据 1。标定里真值 20 Hz 读作 17.0–25.3，真值 10 Hz 读作 11.0–13.8——区间取在两者
中间，两边都留出余量。这一条只用来判断方法有没有效，所以宁可宽一点：真正需要精度的
是判据 2，而它的区间由同一次标定独立给出。"""

VERDICT_BAND = (35.0, 80.0)
"""判据 2。标定里真值 42 Hz 读作 41.8–66.4；相邻的 10 / 20 / 98 / 188 Hz 全在区间外。"""


# ---------------------------------------------------------------------------
# 谱估计。本项目不依赖 numpy（``pyproject.toml`` 只有 bleak），取证脚本更不该为
# 一次性用途改锁文件，所以 FFT 在这里手写。512 点、几十段，纯 Python 足够快。
# ---------------------------------------------------------------------------


def _fft(values: list[complex]) -> list[complex]:
    """迭代式 radix-2 FFT。``len(values)`` 必须是 2 的幂。"""
    n = len(values)
    if n & (n - 1):
        raise ValueError("FFT 长度必须是 2 的幂")

    data = list(values)
    # 位反转置换
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            data[i], data[j] = data[j], data[i]

    length = 2
    while length <= n:
        step = cmath.exp(-2j * math.pi / length)
        half = length >> 1
        for start in range(0, n, length):
            factor = 1 + 0j
            for offset in range(start, start + half):
                even = data[offset]
                odd = data[offset + half] * factor
                data[offset] = even + odd
                data[offset + half] = even - odd
                factor *= step
        length <<= 1
    return data


def _hann(size: int) -> list[float]:
    return [0.5 - 0.5 * math.cos(2 * math.pi * i / size) for i in range(size)]


def _welch(series: list[float], fs: float) -> tuple[list[float], list[float], int]:
    """单路时序的 Welch periodogram。返回 ``(频率, 功率谱, 段数)``。

    50% 重叠、Hann 窗、逐段去均值。去均值是必须的：静置时加计的直流分量是重力，
    量级比噪声大几个数量级，不去掉会让泄漏淹没整条曲线。
    """
    hop = SEGMENT // 2
    window = _hann(SEGMENT)
    window_power = sum(w * w for w in window)

    accumulator = [0.0] * (SEGMENT // 2 + 1)
    segments = 0
    for start in range(0, len(series) - SEGMENT + 1, hop):
        chunk = series[start : start + SEGMENT]
        mean = sum(chunk) / SEGMENT
        spectrum = _fft(
            [complex((value - mean) * w, 0.0) for value, w in zip(chunk, window)]
        )
        for k in range(len(accumulator)):
            power = abs(spectrum[k]) ** 2 / (fs * window_power)
            if 0 < k < SEGMENT // 2:
                power *= 2
            accumulator[k] += power
        segments += 1

    if segments == 0:
        return [], [], 0
    freqs = [k * fs / SEGMENT for k in range(len(accumulator))]
    return freqs, [value / segments for value in accumulator], segments


def _average_axes(
    axes: list[list[float]], fs: float
) -> tuple[list[float], list[float], int]:
    """三个轴各自算谱后平均。三轴的噪声互不相关，平均等于把段数翻三倍。"""
    freqs: list[float] = []
    total: list[float] = []
    segments = 0
    for series in axes:
        axis_freqs, power, count = _welch(series, fs)
        if count == 0:
            return [], [], 0
        segments = count
        if not total:
            freqs, total = axis_freqs, list(power)
        else:
            total = [a + b for a, b in zip(total, power)]
    return freqs, [value / len(axes) for value in total], segments


def _smooth(values: list[float]) -> list[float]:
    """滑动平均。边界处窗口自动收窄，不补零——补零会在两端造出假的下降沿。"""
    half = SMOOTH_BINS // 2
    out: list[float] = []
    for i in range(len(values)):
        low = max(0, i - half)
        high = min(len(values), i + half + 1)
        window = values[low:high]
        out.append(sum(window) / len(window))
    return out


def _ratio_curve(
    freqs: list[float], power: list[float], reference: list[float]
) -> tuple[list[float], float]:
    """算 ``R̂(f)``，并返回通带平台的离散度 ``max/min``。

    平台统计量取**中位数**而不是均值：污染来自个别频点冲向 1，中位数对那种尖峰不
    敏感。离散度一并返回，由调用方当门槛用——归一化本身没法判断自己是否被污染了。
    """
    ratio = _smooth([(p / r if r > 0 else 0.0) for p, r in zip(power, reference)])

    low, high = PLATEAU_BAND
    plateau = [value for freq, value in zip(freqs, ratio) if low <= freq <= high]
    if not plateau or min(plateau) <= 0:
        return ratio, math.inf
    spread = max(plateau) / min(plateau)
    scale = statistics.median(plateau)
    if scale <= 0:
        return ratio, spread
    return [value / scale for value in ratio], spread


def _cutoff(freqs: list[float], ratio: list[float]) -> float | None:
    """找 ``R̂`` 首次降到 0.5 并**持续**低于 0.5 的频率，线性插值。

    「持续」是指其后 10 Hz 内不再回到 0.5 以上。只看首次下穿会被一个噪声凹陷骗到；
    要求持续，就把凹陷排除了。找不到这样的点返回 ``None``——那意味着在观测频段内
    根本没有拐点，本身就是一个结论（标定里真值 98 与 188 Hz 都是这个结果）。
    """
    _, high = PLATEAU_BAND
    for i in range(1, len(freqs)):
        if freqs[i] <= high:
            continue
        if ratio[i] > 0.5 or ratio[i - 1] <= 0.5:
            continue
        sustained = all(
            value <= 0.5
            for freq, value in zip(freqs[i:], ratio[i:])
            if freq <= freqs[i] + 10.0
        )
        if not sustained:
            continue
        span = ratio[i - 1] - ratio[i]
        if span <= 0:
            return freqs[i]
        fraction = (ratio[i - 1] - 0.5) / span
        return freqs[i - 1] + fraction * (freqs[i] - freqs[i - 1])
    return None


def _sample_at(freqs: list[float], values: list[float], target: float) -> float:
    """取最接近 ``target`` 的那个频点的值。"""
    best = min(range(len(freqs)), key=lambda i: abs(freqs[i] - target))
    return values[best]


def _format_cutoff(value: float | None) -> str:
    return f"{value:.1f} Hz" if value is not None else "观测频段内无拐点"


# ---------------------------------------------------------------------------
# 合成标定。判据区间的出处，跑 ``--self-test`` 可复现。
# ---------------------------------------------------------------------------

_SELF_TEST_OVERSAMPLE = 8
"""器件内部的采样率高于回传速率，抽取时的混叠是真实存在的，合成数据必须复现它——
参考档的混叠抬升正是这个估计量偏高的原因，不复现就标定不出那个偏差。"""

_SELF_TEST_FS = 198.4
_SELF_TEST_NOISE_RMS = 8.2
"""LSB。加计量级：400 µg/√Hz × √100 Hz ÷ 0.488 mg/LSB。"""

_SELF_TEST_TRUTHS = (10.0, 20.0, 42.0, 98.0, 188.0)
"""维特通用编码表里 ``0x03`` 附近的档位。判据要能把 42 与它的邻居分开。"""


def _lowpass(series: list[float], cutoff: float, fs: float, order: int) -> list[float]:
    """``order`` 级一阶 RC 级联，整体 −3 dB 点校正到 ``cutoff``。

    器件滤波器的实际阶数未知，所以标定要跨阶数做——估计量对阶数不敏感才可用。
    """
    single = cutoff / math.sqrt(2 ** (1 / order) - 1)
    alpha = math.exp(-2 * math.pi * single / fs)
    out = list(series)
    for _ in range(order):
        previous = 0.0
        filtered = []
        for value in out:
            previous = alpha * previous + (1 - alpha) * value
            filtered.append(previous)
        out = filtered
    return out


def _synthesize(cutoff: float, count: int, order: int, seed: int) -> list[float]:
    """合成一路「白噪声 → 滤波 → 抽取 → 量化」的加速度原始计数。"""
    rng = random.Random(seed)
    warmup = 4000
    inner_fs = _SELF_TEST_FS * _SELF_TEST_OVERSAMPLE
    total = count * _SELF_TEST_OVERSAMPLE + warmup
    white = [rng.gauss(0, _SELF_TEST_NOISE_RMS) for _ in range(total)]
    filtered = _lowpass(white, cutoff, inner_fs, order)
    decimated = filtered[warmup :: _SELF_TEST_OVERSAMPLE][:count]
    return [float(round(value)) for value in decimated]


def _self_test(seconds: float) -> int:
    """用已知截止频率的合成数据跑同一套分析流程，看它能不能还原已知答案。"""
    count = int(_SELF_TEST_FS * seconds)
    print(f"合成标定：每档 {seconds:.0f} 秒 @ {_SELF_TEST_FS:.1f} Hz，")
    print(f"内部 {_SELF_TEST_OVERSAMPLE} 倍过采样后抽取（复现参考档的混叠），含量化。\n")
    print(f"判据区间：自校验 {SELF_CHECK_BAND[0]:.0f}–{SELF_CHECK_BAND[1]:.0f} Hz，"
          f"主判据 {VERDICT_BAND[0]:.0f}–{VERDICT_BAND[1]:.0f} Hz\n")

    header = "阶 组 " + " ".join(f"{truth:>10.0f}Hz" for truth in _SELF_TEST_TRUTHS)
    print(header + "   平台离散")
    print("-" * len(header) + "-" * 12)

    observed: dict[float, list[float]] = {truth: [] for truth in _SELF_TEST_TRUTHS}
    worst_spread = 0.0

    for order in (1, 2, 3):
        for trial in range(3):
            base = 10_000 * order + 1_000 * trial
            reference = [_synthesize(256.0, count, order, base + i) for i in range(3)]
            freqs, ref_power, segments = _average_axes(reference, _SELF_TEST_FS)

            cells = []
            row_spread = 0.0
            for index, truth in enumerate(_SELF_TEST_TRUTHS):
                axes = [
                    _synthesize(truth, count, order, base + 100 + 10 * index + i)
                    for i in range(3)
                ]
                _, power, _ = _average_axes(axes, _SELF_TEST_FS)
                ratio, spread = _ratio_curve(freqs, power, ref_power)
                row_spread = max(row_spread, spread)
                worst_spread = max(worst_spread, spread)
                estimate = _cutoff(freqs, ratio)
                if estimate is not None:
                    observed[truth].append(estimate)
                cells.append(f"{estimate:12.1f}" if estimate else f"{'无拐点':>10}")
            print(f"{order:>1} {trial:>2} " + " ".join(cells) + f"   ≤{row_spread:.2f}")

    print(f"\n（每档 {segments} 段）\n")
    print("还原结果：")
    for truth in _SELF_TEST_TRUTHS:
        values = observed[truth]
        if not values:
            print(f"  真值 {truth:>5.0f} Hz → 全部无拐点")
            continue
        print(f"  真值 {truth:>5.0f} Hz → {min(values):.1f}–{max(values):.1f} Hz")

    ok = True
    twenty = observed[20.0]
    low, high = SELF_CHECK_BAND
    if not twenty or not all(low <= v <= high for v in twenty):
        print(f"\n❌ 真值 20 Hz 没有全部落在自校验区间 {low:.0f}–{high:.0f} Hz 内。")
        ok = False
    forty_two = observed[42.0]
    low, high = VERDICT_BAND
    if not forty_two or not all(low <= v <= high for v in forty_two):
        print(f"\n❌ 真值 42 Hz 没有全部落在主判据区间 {low:.0f}–{high:.0f} Hz 内。")
        ok = False
    for truth in (10.0, 98.0, 188.0):
        if any(low <= v <= high for v in observed[truth]):
            print(f"\n❌ 真值 {truth:.0f} Hz 落进了主判据区间——判据无法把它与 42 Hz 分开。")
            ok = False
    if worst_spread > MAX_PLATEAU_SPREAD:
        print(f"\n❌ 干净数据的平台离散 {worst_spread:.2f} 已超过门槛 "
              f"{MAX_PLATEAU_SPREAD}，门槛定得太紧。")
        ok = False

    print(
        "\n✅ 标定自洽：判据区间容得下已知真值，且把相邻档位排除在外。"
        if ok
        else "\n❌ 标定不自洽。**先修判据，再去占用真机时间。**"
    )
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


async def _collect(
    device: WT901Device, seconds: float
) -> tuple[list[list[float]], list[list[float]], float, bool]:
    """采一段，返回 ``(加速度三轴, 角速度三轴, 实测采样率, 链路是否干净)``。

    先排空积压：写完寄存器到开始采之间队列里已经堆了若干样本，它们的 ``t_host``
    不反映真实到达节奏，会把实测速率算高——而实测速率就是频率轴的刻度。
    """
    remaining = device.pending_samples
    before = device.stats

    accel: list[list[float]] = [[], [], []]
    gyro: list[list[float]] = [[], [], []]
    start = time.monotonic()
    deadline = start + seconds

    async for sample in device.samples():
        if remaining > 0:
            remaining -= 1
            start = time.monotonic()
            deadline = start + seconds
            continue
        raw = sample.raw.values
        for axis in range(3):
            accel[axis].append(float(raw[axis]))
            gyro[axis].append(float(raw[3 + axis]))
        if time.monotonic() >= deadline:
            break

    elapsed = time.monotonic() - start
    after = device.stats
    clean = (
        after.dropped_samples == before.dropped_samples
        and after.resync_count == before.resync_count
    )
    return accel, gyro, len(accel[0]) / elapsed, clean


async def _measure(
    device: WT901Device, code: int, seconds: float
) -> tuple[list[float], list[float], list[float], int, float, bool]:
    """设一档带宽并采一段，返回 ``(频率, 加速度谱, 角速度谱, 段数, 采样率, 链路干净)``。"""
    await device.registers.write(Register.BANDWIDTH, code, persist=False)
    await asyncio.sleep(SETTLE)
    accel, gyro, fs, clean = await _collect(device, seconds)

    freqs, accel_power, segments = _average_axes(accel, fs)
    _, gyro_power, _ = _average_axes(gyro, fs)
    return freqs, accel_power, gyro_power, segments, fs, clean


def _print_curve(label: str, freqs: list[float], ratio: list[float]) -> None:
    print(f"\n   R̂ 曲线（dB）— {label}")
    cells = []
    for target in REPORT_FREQUENCIES:
        value = _sample_at(freqs, ratio, float(target))
        db = 10 * math.log10(value) if value > 0 else float("-inf")
        cells.append(f"{target:>3}Hz {db:>7.1f}")
    for i in range(0, len(cells), 5):
        print("     " + "   ".join(cells[i : i + 5]))


@dataclass(frozen=True, slots=True)
class _Outcome:
    """一档候选的测量结果。判读只看这些字段，原始谱留给人看打印输出。"""

    code: int
    cutoff: float | None
    gyro_cutoff: float | None
    segments: int
    fs: float
    clean: bool
    spread: float


async def _run_probe(seconds: float) -> int:
    found = await scan(6.0)
    if not found:
        print("没扫到 WT 设备，需要一台通电的 WT9011DCL-BT50。")
        return 2

    sensor = found[0]
    print(f"连接 {sensor.name} ({sensor.address})")
    print(f"每档采集 {seconds:.0f} 秒，全程 persist=False（不写 flash）。")
    print("**设备必须静置在一个不振动的地方**：本方法量的是噪声底，")
    print("手一碰、桌子一晃，低频成分就会把通带平台污染掉（脚本会检出并要求重采）。\n")

    outcomes: list[_Outcome] = []
    reference_clean = False
    reference_segments = 0

    async with await WT901Device.connect(sensor) as device:
        original_rate = await device.registers.read_output_rate()
        original_bandwidth = await device.registers.read_bandwidth()
        print(
            f"设备原有配置：RRATE=0x{original_rate:02X}  "
            f"BANDWIDTH=0x{original_bandwidth:02X}"
        )

        try:
            await device.registers.write(Register.RRATE, PROBE_RATE, persist=False)
            await asyncio.sleep(SETTLE)

            print(f"\n── 参考档 0x{int(REFERENCE_CODE):02X}（HZ_256）")
            ref = await _measure(device, REFERENCE_CODE, seconds)
            _, ref_accel, ref_gyro, reference_segments, ref_fs, reference_clean = ref
            print(
                f"   实测采样率 {ref_fs:.2f} Hz，{reference_segments} 段，"
                f"链路 {'干净 ✅' if reference_clean else '有丢样/重同步 ❌'}"
            )

            for code in (SELF_CHECK_CODE, CANDIDATE_CODE):
                label = "已核实的 20 Hz，用作方法自校验" if code == SELF_CHECK_CODE else "待判定"
                print(f"\n── 0x{int(code):02X}（{label}）")
                freqs, accel, gyro, segments, fs, clean = await _measure(
                    device, code, seconds
                )
                print(
                    f"   实测采样率 {fs:.2f} Hz，{segments} 段，"
                    f"链路 {'干净 ✅' if clean else '有丢样/重同步 ❌'}"
                )

                accel_ratio, spread = _ratio_curve(freqs, accel, ref_accel)
                gyro_ratio, _ = _ratio_curve(freqs, gyro, ref_gyro)
                _print_curve("加速度（主判据）", freqs, accel_ratio)
                _print_curve("角速度（旁证，量化底高，无拐点属预期）", freqs, gyro_ratio)

                accel_cut = _cutoff(freqs, accel_ratio)
                gyro_cut = _cutoff(freqs, gyro_ratio)
                print(
                    f"\n   通带平台离散 max/min = {spread:.2f}"
                    f"（门槛 ≤{MAX_PLATEAU_SPREAD}）"
                )
                print(
                    f"   −3 dB 截止估计：加速度 {_format_cutoff(accel_cut)}"
                    f"   角速度 {_format_cutoff(gyro_cut)}"
                )
                outcomes.append(
                    _Outcome(
                        code=int(code),
                        cutoff=accel_cut,
                        gyro_cutoff=gyro_cut,
                        segments=segments,
                        fs=fs,
                        clean=clean,
                        spread=spread,
                    )
                )
        finally:
            await device.registers.write(
                Register.BANDWIDTH, original_bandwidth, persist=False
            )
            await device.registers.write(Register.RRATE, original_rate, persist=False)
            print(
                f"\n已把 BANDWIDTH 与 RRATE 写回原值 "
                f"(0x{original_bandwidth:02X} / 0x{original_rate:02X})，未保存。"
            )

    _report(outcomes, reference_segments, reference_clean)
    return 0


def _report(outcomes: list[_Outcome], ref_segments: int, ref_clean: bool) -> None:
    print(f"\n{'=' * 70}")
    print("按 RAY-298 开工前写定的判据判读")
    print(f"{'=' * 70}")

    if len(outcomes) < 2:
        print("\n❌ 采集未完成，无法判读。")
        return

    if not (ref_clean and all(o.clean for o in outcomes)):
        print(
            "\n❌ 附加门槛未过：某一档采集期间有丢样或重同步。时基有洞，谱不可信。\n"
            "   **不要据此判读**。离主机近一点、关掉其他 BLE 设备后重采。"
        )
        return
    if ref_segments < MIN_SEGMENTS or any(o.segments < MIN_SEGMENTS for o in outcomes):
        print(f"\n❌ 段数不足（要求每档 ≥ {MIN_SEGMENTS}）。加大采集秒数后重跑。")
        return
    worst = max(o.spread for o in outcomes)
    if worst > MAX_PLATEAU_SPREAD:
        print(
            f"\n❌ 通带平台离散 {worst:.2f} 超过门槛 {MAX_PLATEAU_SPREAD}：\n"
            "   通带里混进了三档都原样放行的低频成分（环境振动最常见）。\n"
            "   归一化的基准因此被抬高，拐点估计会系统性偏低——**这种数据不能判读**。\n"
            "   把设备挪到不振动的地方（地面、厚垫子上，远离风扇和走动）后重采。"
        )
        return
    print("\n✅ 附加门槛通过：无丢样、无重同步，段数充足，通带平台平坦。")

    self_check = next(o for o in outcomes if o.code == int(SELF_CHECK_CODE))
    low, high = SELF_CHECK_BAND
    if self_check.cutoff is None or not low <= self_check.cutoff <= high:
        print(
            f"\n❌ 判据 1（方法自校验）不过：已核实的 0x04（20 Hz）估计为 "
            f"{_format_cutoff(self_check.cutoff)}，不在 {low:.0f}–{high:.0f} Hz 内。\n"
            "   方法无法从一个已知答案回推出已知答案，它对 0x03 的读数也就不算数。\n"
            "   → 按判据 4，退回 Issue 描述里的方向 3：只核实到「0x03 被接受且介于两档\n"
            "     之间」，登记时 docstring 明写证据强度与其余两档不同。"
        )
        return
    print(
        f"\n✅ 判据 1（方法自校验）通过：0x04 估计为 {self_check.cutoff:.1f} Hz，"
        f"落在 {low:.0f}–{high:.0f} Hz 内。方法有效。"
    )

    candidate = next(o for o in outcomes if o.code == CANDIDATE_CODE)
    low, high = VERDICT_BAND
    if candidate.cutoff is not None and low <= candidate.cutoff <= high:
        print(
            f"\n✅ 判据 2（主判据）通过：0x03 估计为 {candidate.cutoff:.1f} Hz，"
            f"落在 {low:.0f}–{high:.0f} Hz 内。\n"
            "   标定表里 10 / 20 / 98 / 188 Hz 都落在这个区间之外，所以这只可能是 42 那一档。\n"
            "   → 判定为 42 Hz。Bandwidth 增 HZ_42 = 0x03。"
        )
        return
    print(
        f"\n❌ 0x03 估计为 {_format_cutoff(candidate.cutoff)}，不在 "
        f"{low:.0f}–{high:.0f} Hz 内。\n"
        "   → 按判据 3，本固件不按维特通用表映射这个编码（同 ReturnRate 的 0x0A）。\n"
        "     不登记，把这个发现写进 docs/protocol.md 与适配文档的反馈。"
    )


async def main() -> int:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--self-test":
        seconds = float(arguments[1]) if len(arguments) > 1 else DEFAULT_SECONDS
        return _self_test(seconds)
    seconds = float(arguments[0]) if arguments else DEFAULT_SECONDS
    return await _run_probe(seconds)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
