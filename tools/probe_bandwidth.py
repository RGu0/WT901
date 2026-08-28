"""真机取证：带宽编码 ``0x1F = 0x03`` 的抗混叠截止频率到底是多少。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑，设备要静置。**

    ./dev run python tools/probe_bandwidth.py [每档采集秒数]
    ./dev run python tools/probe_bandwidth.py --self-test   # 不需要设备

``ReturnRate`` 的核实办法是「写进去、量出来」，观测量与被测量是同一个东西。带宽不
一样：``0x1F`` 改的是**传感器内部抗混叠滤波器的截止频率**，它不改变样本速率，只
改变样本内容的频率成分。读寄存器只能读回自己刚写的编码，证明不了任何事。

## 方法（RAY-298 验收标准第 1 条要求开工前写定，已记进 Issue）

设备静置，回传速率固定在 ``0x0B``（已实测 198.43 Hz）。分别在带宽 ``0x00``（256 Hz）、
``0x03``（待判定）、``0x04``（标称 20 Hz）下各采一段，对每段做 Welch periodogram，
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
20 Hz   17.0–25.3 Hz    ``0x04`` 的标称值，用作方法自校验
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
import itertools
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
"""0x0B。实测 198.43 Hz，是已核实档位里最高的一档（速率是逐档实测过的，与带宽不同）。
采样率越高，能看到的频段越宽。"""

REFERENCE_CODE = Bandwidth.HZ_256
"""作为比值的分母：它是三档里滤得最松的，最接近「不滤」。"""

CANDIDATE_CODE = 0x03
"""本次要核实的编码。"""

SELF_CHECK_CODE = Bandwidth.HZ_20
"""标称 20 Hz 的那一档。先跑它，方法自校验就先有结论。

**这个标称值本身也没被实测过**（RAY-298 的取证反过来查实了这一点），所以自校验
失败时有两种成因分不开：方法无效，或者这一档根本不是 20 Hz。见 RAY-304。"""

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

FLOOR_BAND = (80.0, 95.0)
"""阻带地板的取值区间，Hz。

200 Hz 采样下奈奎斯特约 99 Hz，所以 80–95 Hz 是「所有候选档都已经进入阻带、而又
还没贴到奈奎斯特」的一段。地板取这一段归一化后 R̂ 的中位数。
"""

MIN_DYNAMIC_RANGE_DB = 40.0
"""可用动态范围下限，dB。**不到就不要开始测。**

RAY-298 那一轮失败的根因就是这个量不够：曲线压到 −13 dB 就平了，可用范围约 20:1
（≈26 dB），−3 dB 拐点的位置本就落在噪声里。而那一轮是**测完才发现**的。

40 dB 意味着阻带比通带低两个数量级，−3 dB 这个浅得多的门限才谈得上被可靠定位。
低于它时唯一诚实的结论是「噪声底法测不出绝对赫兹数」——那也是一个完整交付。
"""

MAX_PLATEAU_LEVEL = 0.40
"""通带平台**绝对水平**的上限（归一化之前）。**只对窄档有效**，见下。

干净数据下这个值很低：候选档与参考档在通带内都没被滤，但候选档的等效噪声带宽更
窄，功率比因此明显小于 1。**接近 1 意味着归一化基准已经失效**——通常是一个共同的
低频污染（桌面振动、手碰、缓慢漂移）同时主导了两路谱并在相除时抵消掉了，此时 R̂
的形状不再由滤波器决定，算出来的拐点没有意义。

这个量此前**一直被算出来却没有被返回**（`_ratio_curve` 里的 `scale`），所以从未被
打印过，也就从未被用作判据。

0.40 这个门槛来自合成标定（30 s/档，1–3 阶各 3 组）：

======================  ==============
条件                    平台绝对水平
======================  ==============
干净，真值 10/20/42 Hz  0.200–0.343
掺 5 Hz 共同污染        0.439–0.560
======================  ==============

两侧各留约 17% 余量。**同一组污染数据的平台离散是 2.19–2.43，全部通过 ≤2.5 那道
门槛**——这正是必须新增本判据的理由，也复现了 RAY-298 真机实测 1.59 / 1.20「离散
合格但数据不可判读」的那一幕。
"""

PLATEAU_LEVEL_MAX_NOMINAL = 42.0
"""平台水平判据只施加于标称值不高于此的档位，Hz。

**宽档天然接近 1，不是污染。** 标定实测：真值 188 Hz 的干净数据平台水平高达 0.889，
98 Hz 为 0.303–0.516——它们与 256 Hz 参考档在通带内本来就差不多，比值当然接近 1。
对它们施加同一道门槛只会把合格数据判死。

代价说清楚：**这道判据保护不了 98 / 188 / 256 三档**。那三档的拐点本就在 200 Hz
采样的观测频段之外（奈奎斯特约 99 Hz），本方法对它们只能得出「测不到」，所以这个
局限不额外损失什么——但不能假装它不存在。
"""

NOMINAL_HZ = {
    0x00: 256.0, 0x01: 188.0, 0x02: 98.0, 0x03: 42.0,
    0x04: 20.0, 0x05: 10.0, 0x06: 5.0,
}
"""本型号《蓝牙5.0通讯协议》「设置带宽」一节所载的七档标称值。

**这些数本库一档都没实测过**，本脚本存在的目的正是去核实它们。出处是本型号自己的
协议文档，不是「维特通用编码表」——那个说法在 RAY-308 里已被更正。
"""

SWEEP_CODES = (0x06, 0x05, 0x04, 0x03, 0x02, 0x01)
"""逐档扫描的顺序：从最窄扫到最宽。参考档 `0x00` 单独先测，不在此列。

从窄到宽是有意的：`0x06`/`0x05` 是**锚点**（拐点远低于出问题的区间，即使动态范围
有限也测得准），先跑它们，锚点不成立就不必浪费时间往上测。
"""

ANCHOR_CODES = (0x06, 0x05)
ANCHOR_TOLERANCE = 0.30
"""锚点档允许的相对偏差。

`ReturnRate` 的实测偏差都在 1% 以内，但那是直接计数样本；带宽是从噪声谱估拐点，
四分之一以内的偏差已足以支撑「标称值基本对得上」这个结论。再松就区分不了相邻档位
——5 Hz 与 10 Hz 只差一倍。
"""

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
) -> tuple[list[float], float, float, float]:
    """算 ``R̂(f)``，返回 ``(曲线, 平台离散度, 平台绝对水平, 阻带地板)``。

    平台统计量取**中位数**而不是均值：污染来自个别频点冲向 1，中位数对那种尖峰不
    敏感。三个标量都返回，由调用方当门槛用——归一化本身没法判断自己是否被污染了。

    **平台绝对水平此前被算出来就丢掉了**（下面的 ``scale``，只拿去做除法）。它是
    判断「归一化基准还成不成立」的唯一入口：接近 1 说明两路谱在通带内几乎相同，
    R̂ 的形状已经不由滤波器决定。见 :data:`MAX_PLATEAU_LEVEL`。

    阻带地板取归一化**之后**的 R̂ 在 :data:`FLOOR_BAND` 内的中位数，它与平台
    （归一化后恒为 1）的比值就是可用动态范围。见 :data:`MIN_DYNAMIC_RANGE_DB`。
    """
    ratio = _smooth([(p / r if r > 0 else 0.0) for p, r in zip(power, reference)])

    low, high = PLATEAU_BAND
    plateau = [value for freq, value in zip(freqs, ratio) if low <= freq <= high]
    if not plateau or min(plateau) <= 0:
        return ratio, math.inf, math.nan, math.nan
    spread = max(plateau) / min(plateau)
    scale = statistics.median(plateau)
    if scale <= 0:
        return ratio, spread, scale, math.nan
    normalised = [value / scale for value in ratio]
    return normalised, spread, scale, _floor_level(freqs, normalised)


def _floor_level(freqs: list[float], ratio: list[float]) -> float:
    """归一化后 R̂ 在 :data:`FLOOR_BAND` 内的中位数，即阻带地板。

    取中位数而不是最小值：最小值会被单个噪声凹陷拉低，让动态范围看起来比实际好，
    而这个量是用来**决定要不要开始测**的——高估它等于放行一次注定测不准的取证。
    """
    low, high = FLOOR_BAND
    band = [value for freq, value in zip(freqs, ratio) if low <= freq <= high]
    if not band:
        return math.nan
    return statistics.median(band)


def _dynamic_range_db(floor: float) -> float:
    """可用动态范围，dB。归一化后通带平台恒为 1，所以它就是地板的倒数。"""
    if not math.isfinite(floor) or floor <= 0:
        return math.inf
    return -10 * math.log10(floor)


def _band_power_ratio(
    freqs: list[float], power: list[float], reference: list[float]
) -> float:
    """通带内候选档与参考档的总功率比（带内方差比）。

    通带内两档都不该被滤，所以这个比值反映的是**等效噪声带宽之比**，而不是滤波器
    形状。它与平台绝对水平互为旁证：两者一起接近 1，基本可以断定通带被共同污染
    主导了；只有其中一个异常，则更可能是分析出了问题。
    """
    low, high = PLATEAU_BAND
    num = sum(p for freq, p in zip(freqs, power) if low <= freq <= high)
    den = sum(r for freq, r in zip(freqs, reference) if low <= freq <= high)
    if den <= 0:
        return math.nan
    return num / den


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


_POLLUTION_HZ = 5.0
_POLLUTION_RMS = 30.0
"""掺进合成数据的低频污染：幅度远大于噪声底的一个慢振荡。

**频率必须落在** :data:`PLATEAU_BAND` **之内**。第一版写的 0.7 Hz 落在区间外，效果
恰好相反：污染只抬高了 1 Hz 以下的两路谱，平台区间（1–8 Hz）内反而因为归一化基准
被抬高而**降到 0.260**，比干净数据还低——用例非但没能证明新判据有效，还差点被当成
「门槛太松」去改门槛。这是标定该做的事：它先否掉了我自己的判据。

它模拟的是桌面振动、手碰、缓慢漂移这类**器件滤波器之后**才叠加上去的东西，所以
在合成里也加在滤波之后——加在之前会被滤掉，就复现不出真机上那个失败模式了。

关键在于**候选档与参考档掺的是同一个污染**：两路谱在通带内都被它主导，相除时抵消，
R̂ 的平台被整体抬高，而离散度看起来照样很小。RAY-298 真机实测 1.59 / 1.20 通过了
≤2.5 那道门槛，就是这么来的。

幅度 30 是标定里**旧门槛挡不下、新门槛挡得下**的那一档（水平 0.44、离散 2.4）。取更
大的幅度反而会让离散一起超标，用例就证明不了新判据不可替代了。
"""


def _synthesize_polluted(
    cutoff: float, count: int, order: int, seed: int
) -> list[float]:
    """与 :func:`_synthesize` 相同，但在**抽取之后**叠加一路共同的低频污染。"""
    clean = _synthesize(cutoff, count, order, seed)
    phase = (seed % 97) / 97.0 * 2 * math.pi
    step = 2 * math.pi * _POLLUTION_HZ / _SELF_TEST_FS
    return [
        value + _POLLUTION_RMS * math.sqrt(2) * math.sin(phase + step * i)
        for i, value in enumerate(clean)
    ]


def _pollution_case(count: int) -> tuple[float, float]:
    """跑一次「掺共同低频污染」的用例，返回 ``(平台绝对水平, 平台离散度)``。

    这个用例存在的唯一目的是**证明新判据能挡下旧判据挡不下的东西**。它不检验拐点
    还原得准不准——污染之下那个数本来就没有意义。
    """
    order = 2
    reference = [_synthesize_polluted(256.0, count, order, 70_000 + i) for i in range(3)]
    freqs, ref_power, _ = _average_axes(reference, _SELF_TEST_FS)
    axes = [_synthesize_polluted(42.0, count, order, 70_100 + i) for i in range(3)]
    _, power, _ = _average_axes(axes, _SELF_TEST_FS)
    _, spread, plateau, _ = _ratio_curve(freqs, power, ref_power)
    return plateau, spread


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
    worst_level = 0.0

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
                ratio, spread, level, _ = _ratio_curve(freqs, power, ref_power)
                row_spread = max(row_spread, spread)
                worst_spread = max(worst_spread, spread)
                # 只有窄档参与门槛标定：宽档（98/188 Hz）与 256 Hz 参考档在通带
                # 内本来就差不多，水平天然接近 1，见 PLATEAU_LEVEL_MAX_NOMINAL。
                if math.isfinite(level) and truth <= PLATEAU_LEVEL_MAX_NOMINAL:
                    worst_level = max(worst_level, level)
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
    if worst_level > MAX_PLATEAU_LEVEL:
        print(f"\n❌ 干净窄档的平台绝对水平 {worst_level:.3f} 已超过门槛 "
              f"{MAX_PLATEAU_LEVEL}，门槛定得太紧。")
        ok = False
    else:
        print(f"\n干净数据：窄档（≤{PLATEAU_LEVEL_MAX_NOMINAL:.0f} Hz）平台绝对水平最高 "
              f"{worst_level:.3f}（门槛 ≤{MAX_PLATEAU_LEVEL}），离散最高 "
              f"{worst_spread:.2f}。")

    # ---- 污染用例：证明新判据挡得下旧判据挡不下的东西 ----
    polluted_level, polluted_spread = _pollution_case(count)
    print(
        f"\n掺共同低频污染（{_POLLUTION_HZ} Hz，落在平台区间内，滤波之后叠加）："
        f"平台绝对水平 {polluted_level:.3f}，离散 {polluted_spread:.2f}"
    )
    if polluted_level <= MAX_PLATEAU_LEVEL:
        print(
            f"❌ 污染用例的平台绝对水平 {polluted_level:.3f} 没有超过门槛 "
            f"{MAX_PLATEAU_LEVEL}——**新判据挡不下它**，门槛太松或污染模型不对。"
        )
        ok = False
    else:
        print(f"✅ 新判据挡下了它（{polluted_level:.3f} > {MAX_PLATEAU_LEVEL}）。")
    if polluted_spread > MAX_PLATEAU_SPREAD:
        print(
            f"⚠️ 污染用例的离散 {polluted_spread:.2f} 也超了门槛——本用例没能复现\n"
            "   「离散度挡不下」那个失败模式（真机是 1.59 / 1.20，都在门槛内）。\n"
            "   新判据仍然有效，但这条用例证明不了它**不可替代**。"
        )
    else:
        print(
            f"✅ 而离散度 {polluted_spread:.2f} 仍在门槛 {MAX_PLATEAU_SPREAD} 内——\n"
            "   **这正是必须新增绝对水平判据的理由**：RAY-298 真机实测 1.59 / 1.20\n"
            "   通过了离散度门槛，数据却已经不可判读。"
        )

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
    plateau_level: float
    """通带平台的绝对水平（归一化前）。接近 1 即归一化基准失效。"""
    floor: float
    """归一化后 R̂ 的阻带地板。"""
    band_power_ratio: float
    """通带内候选档与参考档的总功率比。"""

    @property
    def dynamic_range_db(self) -> float:
        return _dynamic_range_db(self.floor)

    @property
    def nominal(self) -> float | None:
        return NOMINAL_HZ.get(self.code)

    @property
    def deviation(self) -> float | None:
        """拐点相对标称值的偏差。测不出拐点或无标称值时为 ``None``。"""
        if self.cutoff is None or not self.nominal:
            return None
        return (self.cutoff - self.nominal) / self.nominal


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

            for code in SWEEP_CODES:
                role = "锚点" if code in ANCHOR_CODES else "待判定"
                print(
                    f"\n── 0x{int(code):02X}（标称 {NOMINAL_HZ[code]:.0f} Hz，{role}）"
                )
                freqs, accel, gyro, segments, fs, clean = await _measure(
                    device, code, seconds
                )
                print(
                    f"   实测采样率 {fs:.2f} Hz，{segments} 段，"
                    f"链路 {'干净 ✅' if clean else '有丢样/重同步 ❌'}"
                )

                accel_ratio, spread, plateau, floor = _ratio_curve(
                    freqs, accel, ref_accel
                )
                gyro_ratio, _, _, _ = _ratio_curve(freqs, gyro, ref_gyro)
                band_ratio = _band_power_ratio(freqs, accel, ref_accel)
                _print_curve("加速度（主判据）", freqs, accel_ratio)
                _print_curve("角速度（旁证，量化底高，无拐点属预期）", freqs, gyro_ratio)

                accel_cut = _cutoff(freqs, accel_ratio)
                gyro_cut = _cutoff(freqs, gyro_ratio)
                print(
                    f"\n   通带平台离散 max/min = {spread:.2f}"
                    f"（门槛 ≤{MAX_PLATEAU_SPREAD}）"
                )
                print(
                    f"   通带平台绝对水平 = {plateau:.3f}"
                    + (
                        f"（门槛 ≤{MAX_PLATEAU_LEVEL}，干净数据 0.200–0.343）"
                        if NOMINAL_HZ[code] <= PLATEAU_LEVEL_MAX_NOMINAL
                        else "（宽档，不设门槛，天然接近 1）"
                    )
                )
                print(
                    f"   阻带地板 = {floor:.5f}  →  可用动态范围 "
                    f"{_dynamic_range_db(floor):.1f} dB"
                    f"（门槛 ≥{MIN_DYNAMIC_RANGE_DB:.0f} dB）"
                )
                print(f"   带内功率比 = {band_ratio:.3f}")
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
                        plateau_level=plateau,
                        floor=floor,
                        band_power_ratio=band_ratio,
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
    print("按 RAY-304 scope 2 开工前预注册的判据判读")
    print(f"{'=' * 70}")

    if not outcomes:
        print("\n❌ 采集未完成，无法判读。")
        return

    # ---- 采集质量（与判据无关，先排除「数据本身不能用」） ----
    if not (ref_clean and all(o.clean for o in outcomes)):
        print(
            "\n❌ 采集质量不过：某一档期间有丢样或重同步。时基有洞，谱不可信。\n"
            "   **不要据此判读**。离主机近一点、关掉其他 BLE 设备后重采。"
        )
        return
    if ref_segments < MIN_SEGMENTS or any(o.segments < MIN_SEGMENTS for o in outcomes):
        print(f"\n❌ 段数不足（要求每档 ≥ {MIN_SEGMENTS}）。加大采集秒数后重跑。")
        return

    # ---- 前置门槛一：归一化基准是否还成立 ----
    worst_spread = max(o.spread for o in outcomes)
    # 只看窄档：宽档与参考档在通带内本来就差不多，水平天然接近 1（标定实测干净
    # 数据下 188 Hz 可到 0.889），拿同一道门槛去卡只会把合格数据判死。
    narrow = [
        o for o in outcomes
        if math.isfinite(o.plateau_level) and o.nominal <= PLATEAU_LEVEL_MAX_NOMINAL
    ]
    worst_level = max((o.plateau_level for o in narrow), default=math.nan)
    if math.isfinite(worst_level) and worst_level > MAX_PLATEAU_LEVEL:
        offenders = ", ".join(
            f"0x{o.code:02X}({o.nominal:.0f} Hz)"
            for o in narrow if o.plateau_level > MAX_PLATEAU_LEVEL
        )
        print(
            f"\n❌ 前置门槛一不过：窄档 {offenders} 的通带平台绝对水平最高 "
            f"{worst_level:.3f}，\n"
            f"   超过 {MAX_PLATEAU_LEVEL}（标定实测干净窄档 0.200–0.343）。\n"
            "   两路谱在通带内几乎相同，说明一个**共同的低频成分**同时主导了候选档与\n"
            "   参考档，相除时抵消掉了——归一化的基准已经失效，R̂ 的形状不再由滤波器\n"
            "   决定，后面算出来的拐点没有意义。\n"
            f"   （平台离散 {worst_spread:.2f} 此时可能仍然「合格」：RAY-298 真机实测\n"
            "     1.59 / 1.20 就通过了 ≤2.5 这道门槛。**离散度挡不下这种污染。**）\n"
            "   把设备挪到不振动的地方（地面、厚垫子，远离风扇和走动）后重采。"
        )
        return
    if not narrow:
        print(
            f"\n⚠️ 没有任何标称值 ≤{PLATEAU_LEVEL_MAX_NOMINAL:.0f} Hz 的档位测出有效平台，\n"
            "   前置门槛一**这次没有生效**——下面的判读少了一道保护。"
        )
    if worst_spread > MAX_PLATEAU_SPREAD:
        print(
            f"\n❌ 通带平台离散 {worst_spread:.2f} 超过门槛 {MAX_PLATEAU_SPREAD}：\n"
            "   通带里混进了各档都原样放行的低频成分。**这种数据不能判读**，重采。"
        )
        return

    # ---- 前置门槛二：动态范围够不够开始 ----
    worst_range = min(o.dynamic_range_db for o in outcomes)
    if worst_range < MIN_DYNAMIC_RANGE_DB:
        print(
            f"\n⚠️ 前置门槛二不过：可用动态范围最低仅 {worst_range:.1f} dB，"
            f"低于 {MIN_DYNAMIC_RANGE_DB:.0f} dB。\n"
            "   阻带没有比通带低两个数量级，−3 dB 这个浅门限的位置落在噪声里，\n"
            "   拐点估计不可信。\n\n"
            "   → **结论：噪声底法测不出本器件带宽的绝对赫兹数。**\n"
            "     这是预注册判据列明的三种完整交付之一，不是失败：\n"
            "     · docs/protocol.md 记录该方法的局限与本次实测的动态范围；\n"
            "     · Bandwidth 七档的 docstring 维持「标称值来自本型号协议文档，\n"
            "       本库未实测」不变；\n"
            "     · 要坐实那七个数，需要换一种能主动激励的方法（已知频率振动台），\n"
            "       那是另一个 Issue。\n"
            "   RAY-298 那一轮约 26 dB，按本门槛当时就该停——而它是测完才发现的。"
        )
        return
    print(
        f"\n✅ 前置门槛通过：平台绝对水平 ≤{worst_level:.3f}、离散 ≤{worst_spread:.2f}、"
        f"可用动态范围 ≥{worst_range:.1f} dB。"
    )

    # ---- 主判据：锚点档 ----
    anchors = [o for o in outcomes if o.code in ANCHOR_CODES]
    if len(anchors) != len(ANCHOR_CODES):
        print("\n❌ 锚点档未全部采到，无法判读。")
        return
    failed = [
        o for o in anchors
        if o.deviation is None or abs(o.deviation) > ANCHOR_TOLERANCE
    ]
    for o in anchors:
        shown = f"{o.deviation:+.0%}" if o.deviation is not None else "无拐点"
        print(
            f"   锚点 0x{o.code:02X}  标称 {o.nominal:>5.1f} Hz  "
            f"实测 {_format_cutoff(o.cutoff):>12}  偏差 {shown}"
        )
    if failed:
        print(
            f"\n❌ 主判据不过：锚点档偏差超过 ±{ANCHOR_TOLERANCE:.0%}。\n"
            "   锚点的拐点远低于出问题的区间，其上方有充足频谱确立阻带——**它都还原\n"
            "   不出来，说明方法本身无效**，与那七个标称值对不对无关。\n"
            "   → 按预注册判据：**不得根据本次数据对任何一档下结论。**"
        )
        return
    print(
        f"\n✅ 主判据通过：锚点档偏差都在 ±{ANCHOR_TOLERANCE:.0%} 内，方法有效。\n"
        "   此时高档若测不准，成因定位在动态范围，而不是标签错。"
    )

    # ---- 交叉检验：单调性 ----
    ordered = sorted(outcomes, key=lambda o: o.nominal or 0.0)
    measured = [(o, o.cutoff) for o in ordered if o.cutoff is not None]
    inversions = [
        (a[0].code, b[0].code)
        for a, b in itertools.pairwise(measured)
        if b[1] <= a[1]
    ]
    if inversions:
        pairs = "、".join(f"0x{x:02X}→0x{y:02X}" for x, y in inversions)
        print(
            f"\n❌ 单调性交叉检验不过：{pairs} 出现倒序。\n"
            "   标称值单调递增的七档，实测拐点必须同样单调。任何一处倒序都意味着测量\n"
            "   不可信——**即使锚点通过了主判据，也不能下结论。**"
        )
        return
    print("\n✅ 单调性交叉检验通过：实测拐点随标称值单调递增。")

    # ---- 逐档结论 ----
    print(f"\n{'-' * 70}")
    print("逐档结果（标称值来自本型号协议文档，本库此前一档未实测）")
    print(f"{'-' * 70}")
    for o in ordered:
        shown = f"{o.deviation:+.0%}" if o.deviation is not None else "—"
        print(
            f"  0x{o.code:02X}  标称 {o.nominal:>5.1f} Hz  "
            f"实测 {_format_cutoff(o.cutoff):>12}  偏差 {shown}"
        )
    print(
        "\n判读要点：\n"
        "  · 偏差在 ±30% 内的档，本次实测**支持**其标称值。\n"
        "  · 「无拐点」不等于标称值错——高档的拐点可能已越过观测频段上限\n"
        "    （200 Hz 采样，奈奎斯特约 99 Hz，98/188/256 Hz 三档本就测不到）。\n"
        "    这三档的结论只能是「本方法测不到」，不能是「证伪」。\n"
        "  · 与标称值明显不符且拐点落在观测频段内的档，按 ReturnRate 排除 0x0A 的\n"
        "    先例处理：改正或移除，并记入 docs/protocol.md §9。"
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
