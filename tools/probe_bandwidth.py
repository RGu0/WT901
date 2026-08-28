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
10 Hz   10.3–13.5 Hz
20 Hz   16.8–21.5 Hz    ``0x04`` 的标称值，用作方法自校验
42 Hz   37.8–46.7 Hz
98 Hz   78.9–96.9 Hz    **9 组里 3 组测出了拐点，其中 78.9 Hz 落进主判据区间**
188 Hz  无拐点          超过奈奎斯特，观测频段内看不到
======  ==============  ====================================

**这张表是 RAY-315 用红噪声源重标定后的结果**，与此前白噪声版本不同。最要紧的差别
在 98 Hz 那一行：白噪声下它永远测不出拐点，红噪声下它会，而且有一组落进了
:data:`VERDICT_BAND`——**主判据在正确的噪声模型下分不开 42 与 98 Hz**。见下面「已知
判据缺陷」。

合成数据的随机种子是固定的，所以这张表可以逐字复现——它不是「跑一次看到的样子」，
而是判据的出处。改动分析流程后 ``--self-test`` 会自己判断判据还成不成立。

1. **方法自校验**：``R̂_0x04`` 的 −3 dB 点落在 :data:`SELF_CHECK_BAND` → 方法有效。
   区间下沿同时把真值 10 Hz 排除在外，所以这一条不只是「数量级对」。
2. **主判据**：``R̂_0x03`` 落在 :data:`VERDICT_BAND` → 判定为 42 Hz，登记 ``HZ_42``。
   白噪声标定下 10 / 20 / 98 / 188 Hz 四个相邻档位全部落在这个区间之外；**红噪声
   重标定之后 98 Hz 不再全在区间外**，这一条因此已不成立，见「已知判据缺陷」。
3. 落在区间外或无拐点 → 本固件不按维特通用表映射这个编码，**不登记**（同 ``0x0A``）。
4. 第 1 条不过 → 方法无效，退回 Issue 描述里的方向 3。

**附加门槛四道**，任一不过就重采，不做「凑合判读」。全部登记在 :data:`GATE_LEDGER`：

- 采集全程 ``dropped_samples`` + ``resync_count`` 为 0（:data:`MAX_LINK_DEFECTS`）。
- 每档段数 ≥ :data:`MIN_SEGMENTS`。段数决定谱估计的方差。
- 通带平台必须平坦（``max/min ≤`` :data:`MAX_PLATEAU_SPREAD`）。防环境振动。
- 可用动态范围 ≥ :data:`MIN_DYNAMIC_RANGE_DB`。

**通则（RAY-315）：每一道预注册的门槛，标定都必须评估它**，且分不开时要拿出「该
失败模式无害」的实测证据。来源是 RAY-304 审阅期的发现——动态范围那道门槛预注册了、
标定也建了，却从来没拿标定数据跑过自己；补跑后发现连干净合成数据都从未接近 40 dB，
「本方法过不了自己的门槛」在上真机之前就是可判定的，代价是用户白跑了两轮。
``--self-test`` 现在逐条走这张台账，评估不了的必须写明理由，不得静默跳过。

## 已知判据缺陷（RAY-315 重标定暴露，**未修**）

1. **通带平台绝对水平那道门槛已退役。** 它的标定用的是白噪声，真机是红噪声，两轮
   真机数据被它全部误判为「归一化基准失效、请重采」。换成红噪声之后它既分不开干净
   与污染，要防的失败模式也不再存在。详见 :data:`PLATEAU_LEVEL_CLEAN`。
2. **主判据在红噪声下分不开 42 与 98 Hz。** 见上面标定表。**本轮没有动
   :data:`VERDICT_BAND`**——改判据区间是又一次判据修改，要单独确认并升需求修订。
   它不影响任何已有结论：动态范围那道门槛在干净合成数据上就不过（14.5 dB，要求
   ≥40 dB），本方法已经退役，主判据根本轮不到生效。

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

MAX_LINK_DEFECTS = 0
"""采集期间允许的丢样 + 重同步总数。丢样在时基上留洞，而时基就是频率轴的刻度。

写成常量不是为了将来放宽它——是为了让 :data:`GATE_LEDGER` 能指着它，
好让「每一道门槛标定都必须评估」这条通则对它也生效（对它的评估结论是
「本合成评估不了」，那也必须显式写出来，不能静默跳过）。
"""

MAX_PLATEAU_SPREAD = 2.5
"""通带平台的 ``max/min`` 上限。**这一条是防环境振动的。**

门槛值 2.5 是 RAY-304 取证前预注册的，来自当时的白噪声标定（干净 ≤1.86，掺低频污染
3.2）。RAY-315 把标定的源噪声换成红噪声之后，**这道门槛的处境变了**：

============================  ==========
条件（红噪声 α=0.99）         平台离散
============================  ==========
干净，真值 10–188 Hz          ≤1.72
掺共同低频污染                1.16
============================  ==========

污染用例**不再触发它**——因为红噪声下这种共同污染根本不再扰动 R̂（平台本就在 1 附近，
归一化基准无从被抬高）。按 :data:`GATE_LEDGER` 的通则，分不开就必须拿出「该失败模式
无害」的实测证据：同一组种子掺污染前后 −3 dB 估计 44.4 → 42.9 Hz，相对变化 3.4%，
在 :data:`POLLUTION_BIAS_TOLERANCE` 之内。豁免成立，门槛值**未改**。

门槛留着不是因为它现在还挡得下什么，而是因为它挡的失败模式比这一个污染模型更宽
（不是所有环境振动都是共模的），而干净数据上它有余量，留着不误报。
"""

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

PLATEAU_LEVEL_CLEAN = (0.75, 1.15)
"""通带平台**绝对水平**在干净合成数据上的取值范围（红噪声 α=0.99，窄档）。

**这是诊断量，不是门槛。** 它曾经是一道前置门槛（``MAX_PLATEAU_LEVEL ≤ 0.40``），
RAY-315 把它退了下来。三条理由全部是合成标定实测出来的，不是推断：

**一、原门槛的标定用错了噪声颜色。** 干净基线 0.200–0.343 来自**白噪声**合成。参考
档 ``0x00`` 标称 256 Hz 而采样率只有 200 Hz，白噪声里 100–256 Hz 那一段必然折回带内，
把参考谱的低频抬高几倍，比值因此偏低。真实传感器噪声是红噪声（1/f + 环境），高频
没那么多东西可折，参考档低频不被抬高——**平台接近 1 才是干净数据的正常值**。
2026-08-28 两台设备各一轮，窄档实测 0.892–1.033，被这道门槛全部拦下并建议「挪到不
振动的地方重采」。那个建议是错的，白让用户跑了两轮。

**二、换成红噪声之后，这个统计量分不开干净与污染。** 同一套合成、只改源噪声颜色
（注入点在**器件滤波器之前**，见 :func:`_red_source`），窄档取值范围：

====================  =============  =============  =============
源噪声                干净           掺污染 ×1      掺污染 ×10
====================  =============  =============  =============
白（α=0）             0.222–0.309    0.400–0.475    0.586–0.628
红（α=0.99）          0.880–1.009    0.894–1.014    0.903–0.997
红（α=0.999）         0.818–1.218    0.874–1.196    0.927–1.090
====================  =============  =============  =============

（``--self-test`` 的「源噪声颜色对照」一节逐次复现这张表，种子固定。真机窄档实测
0.892–1.033，落在红噪声那两行里。）

共同污染把比值推向 1。白噪声下平台原本在 0.27，那是 2–3 倍的抬升，分得开；红噪声下
平台本来就在 1 附近，**没有可抬升的余量**。污染幅度加到标定用量的 10 倍仍然如此。
所以不是「门槛取值没找对」——是这个统计量在真实噪声模型下没有分辨力，任何取值都分
不开。

**三、它要防的那个失败模式在红噪声下不再存在。** 原门槛的理由是「共同低频污染会把
拐点估计带偏」。红噪声下实测不带偏：同一组种子，掺污染前后的 −3 dB 估计相差在
:data:`POLLUTION_BIAS_TOLERANCE` 之内。原因同上——平台本来就在 1 附近，归一化基准
无从被抬高。（白噪声下则确实被带偏：同样的污染把真值 42 Hz 读成 8.1–17.7 Hz。）

保留计算与打印，是因为这个数**仍然有信息**：远大于 1 说明候选档在通带内比参考档还
有劲，那在物理上讲不通，是分析或数据出了别的问题的信号。但它不再拦人。

.. note::

   退这道门槛属**判据修改**，走的是需求修订（RAY-315 R2），不是就地改数。判据先用
   合成标定定出来，**再**拿 RAY-304 的两轮真机数据回归；顺序没有反过来。
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
"""判据 1。区间是 RAY-304 取证前按白噪声标定预注册的（当时真值 20 Hz 读作 17.0–25.3，
10 Hz 读作 11.0–13.8），取在两者中间、两边都留余量。这一条只用来判断方法有没有效，
所以宁可宽一点：真正需要精度的是判据 2。

**RAY-315 的红噪声重标定没有推翻它**：20 Hz 读作 16.8–21.5（仍全在区间内），10 Hz
读作 10.3–13.5（仍全在区间外）。门槛值未改，也不需要改。"""

VERDICT_BAND = (35.0, 80.0)
"""判据 2。RAY-304 取证前按白噪声标定预注册：真值 42 Hz 读作 41.8–66.4，相邻的
10 / 20 / 98 / 188 Hz 全在区间外。

.. warning::

   **RAY-315 的红噪声重标定推翻了「相邻档位全在区间外」这半句。** 42 Hz 仍然落在
   区间内（37.8–46.7），但 98 Hz 在红噪声下测得出拐点了（9 组里 3 组，读作
   78.9–96.9 Hz），其中 78.9 Hz **落在区间内**——主判据分不开 42 与 98。

   **本轮没有动这个区间。** 改判据区间是判据修改，要单独确认并升需求修订；RAY-315
   确认的范围只到「退掉平台水平那道门槛」。

   它不影响任何已有结论：:data:`MIN_DYNAMIC_RANGE_DB` 在干净合成数据上就不过
   （14.5 dB，要求 ≥40 dB），本方法已经退役，主判据轮不到生效。要重新启用本方法，
   这一条必须先解决。
"""


# ---------------------------------------------------------------------------
# 门槛台账。RAY-315 立的通则：每一道预注册的门槛，标定都必须评估它。
# ---------------------------------------------------------------------------


POLLUTION_BIAS_TOLERANCE = 0.10
"""掺污染前后 −3 dB 估计允许的相对变化。

这个数是 :data:`MAX_PLATEAU_SPREAD` 那道门槛的**豁免条件**，不是一道门槛：污染用例
若不触发它，:data:`GATE_LEDGER` 就要求拿出「这个失败模式无害」的实测证据，而证据就是
同一组种子下拐点估计没有被带偏。带偏超过这个比例，豁免不成立，标定判失败。

取 0.10 的依据：红噪声下实测的偏移远小于此——污染加到 10 倍，真值 42 Hz 仍读作
38.8–44.4 Hz，与干净的 38.4–44.4 重合；白噪声下同样的污染把它读成 8.1–17.7 Hz，偏移
60% 以上。两种情形差着一个数量级，门限落在中间哪儿都不改变结论。
"""


@dataclass(frozen=True, slots=True)
class _Gate:
    """一道预注册的前置门槛，外加「标定该拿什么用例去评估它」。

    **通则（RAY-315 R2）**：每一道预注册的门槛，标定都必须评估它——报出干净数据上的
    实测值（会不会误报），以及它声称要挡下的那个失败模式上的实测值（挡不挡得下）。
    分不开时必须附上「该失败模式无害」的**实测**证据，否则标定判失败。合成评估不了
    的门槛必须写明理由，**不得静默跳过**。

    通则的来源是 RAY-304 审阅期的发现：``MIN_DYNAMIC_RANGE_DB`` 预注册了、合成标定也
    建了，但标定从来没拿这道门槛跑过自己的数据。补跑之后才看清——连干净白噪声合成都
    从未接近 40 dB，也就是说「本方法过不了自己预注册的门槛」在上真机之前就是可判定的。
    标定手上有全部所需信息，只是没去查，代价是用户在真机上白跑了两轮。

    加「必须证明能分开」这一条，是因为光「评估过」不够：``MAX_PLATEAU_LEVEL`` 正是
    评估过也没用的例子——它在白噪声下分得开，换个噪声模型就分不开了，而它真正要挡的
    那个失败模式同时也消失了。见 :data:`PLATEAU_LEVEL_CLEAN`。
    """

    constant: str
    """门槛常量在本模块里的名字。``tests/test_probe_gate_ledger.py`` 靠它核对台账没有
    漏掉任何一道门槛——漏掉正是 RAY-304 那次的失败方式。"""

    limit: float
    trips_when: str
    """``"above"`` = 读数超过 ``limit`` 即触发；``"below"`` = 低于即触发。"""

    reading: str
    """从测量结果里读哪个字段。合成用例（``_CaseResult``）与真机结果（``_Outcome``）
    用的是同一批字段名，所以同一份台账两边都能跑。"""

    guards: str
    """它声称要挡下的失败模式。"""

    case: str | None
    """标定里演示 ``guards`` 那个失败模式的用例键；``None`` = 本合成评估不了。"""

    unevaluable: str = ""
    """``case`` 为 ``None`` 时必须写明为什么评估不了。"""

    harmless: str = ""
    """用例演示不出分离时，指明「该失败模式无害」由哪一项实测来证。留空则标定判失败——
    这是本通则的牙齿：没有证据就不许有例外。"""


GATE_LEDGER: tuple[_Gate, ...] = (
    _Gate(
        constant="MAX_LINK_DEFECTS",
        limit=float(MAX_LINK_DEFECTS),
        trips_when="above",
        reading="link_defects",
        guards="丢样或重同步——时基上留洞，谱不可信",
        case=None,
        unevaluable=(
            "合成数据没有 BLE 链路，丢样与重同步无从发生。这道门槛只能在真机上评估，"
            "两轮真机取证里它都为 0。"
        ),
    ),
    _Gate(
        constant="MIN_SEGMENTS",
        limit=float(MIN_SEGMENTS),
        trips_when="below",
        reading="segments",
        guards="段数不足——谱估计方差过大，拐点无从谈起",
        case="short",
    ),
    _Gate(
        constant="MAX_PLATEAU_SPREAD",
        limit=MAX_PLATEAU_SPREAD,
        trips_when="above",
        reading="spread",
        guards="共同低频污染（环境振动）把通带平台压歪",
        case="pollution",
        harmless=(
            "红噪声下污染用例不触发它，豁免依据是同一组种子下 −3 dB 估计没有被带偏"
            f"（≤{POLLUTION_BIAS_TOLERANCE:.0%}）。白噪声下则确实被带偏，那时它挡得下。"
        ),
    ),
    _Gate(
        constant="MIN_DYNAMIC_RANGE_DB",
        limit=MIN_DYNAMIC_RANGE_DB,
        trips_when="below",
        reading="dynamic_range_db",
        guards="阻带压不下去——−3 dB 这个浅门限落在噪声里",
        case="clean",
    ),
)
"""本脚本全部的预注册前置门槛。**加门槛就要加这里的条目**，否则守卫测试挂。

``MAX_PLATEAU_LEVEL`` 曾经在这张表上，RAY-315 把它退成了诊断量，见
:data:`PLATEAU_LEVEL_CLEAN`。退出台账不等于删掉证据：它为什么退，记在那条常量的
docstring 与 ``docs/protocol.md`` §6.2 里。
"""


def _gate_reading(gate: _Gate, source: object) -> float:
    """从一份测量结果里读出某道门槛关心的那个量。"""
    return float(getattr(source, gate.reading))


def _gate_trips(gate: _Gate, value: float) -> bool:
    """这个读数会不会触发该门槛。非有限值一律不触发——那是「测不出」，不是「不合格」。"""
    if not math.isfinite(value):
        return False
    return value > gate.limit if gate.trips_when == "above" else value < gate.limit


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
    R̂ 的形状已经不由滤波器决定。**但这个推理只在白噪声源下成立**，红噪声下平台
    本来就接近 1；它因此已从门槛退为诊断量，见 :data:`PLATEAU_LEVEL_CLEAN`。

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

_SELF_TEST_RED_ALPHA = 0.99
"""源噪声的一阶红化系数（RAY-315 R2 定）。

**真实传感器噪声不是白的。** 标定此前用白噪声，得出的干净基线在真机上系统性偏低
（见 :data:`PLATEAU_LEVEL_CLEAN`）。α=0.99 与 α=0.999 都能复现真机实测的窄档平台
水平 0.892–1.033；取 0.99 作准，0.999 作为对照记在文档里。

**注入点必须在器件滤波器之前**（:func:`_synthesize` 里就是这么接的）。RAY-304 审阅
期第一次把红化加在合成**之后**，三位小数完全没动——同一个线性滤波器同时作用于候选
与参考，相除时精确抵消，那个测试是空的。验证任何关于 R̂ 的假设，扰动必须只作用于
单路。这个坑容易重犯，所以记在这里。
"""

_SELF_TEST_WHITE_ALPHA = 0.0
"""白噪声，即旧标定用的源。留着是为了在同一套代码里跑出对照，说明旧门槛为什么失效——
不是为了给它留一条退路。"""


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


def _red_source(count: int, alpha: float, rng: random.Random) -> list[float]:
    """一阶 AR(1) 红噪声，事后归一化到 :data:`_SELF_TEST_NOISE_RMS`。

    ``x[n] = alpha * x[n-1] + w[n]``。α=0 退化成白噪声。

    **归一化不能省。** AR(1) 的方差按 ``1/(1-alpha**2)`` 涨，α=0.999 时是白噪声的
    五百倍。不归一化，红噪声那几组的量化噪声相对量级会小到可以忽略，测出来的差异就
    分不清是噪声颜色还是量化——而量化底正是这个实验动态范围的来源。

    预热按时间常数取（``1/(1-alpha)`` 个样本的 20 倍），不写死：α=0.999 的时间常数是
    α=0.99 的十倍，同一个预热长度对前者不够、对后者是白等。
    """
    if alpha <= 0.0:
        return [rng.gauss(0.0, _SELF_TEST_NOISE_RMS) for _ in range(count)]

    burn = min(20_000, max(1_000, int(20.0 / (1.0 - alpha))))
    series: list[float] = []
    previous = 0.0
    for _ in range(burn + count):
        previous = alpha * previous + rng.gauss(0.0, 1.0)
        series.append(previous)
    series = series[burn:]
    rms = math.sqrt(sum(v * v for v in series) / len(series))
    if rms <= 0:
        return series
    gain = _SELF_TEST_NOISE_RMS / rms
    return [v * gain for v in series]


def _synthesize(
    cutoff: float,
    count: int,
    order: int,
    seed: int,
    alpha: float = _SELF_TEST_RED_ALPHA,
) -> list[float]:
    """合成一路「源噪声 →（红化）→ 滤波 → 抽取 → 量化」的加速度原始计数。

    红化在**滤波之前**，见 :data:`_SELF_TEST_RED_ALPHA`。
    """
    rng = random.Random(seed)
    warmup = 4000
    inner_fs = _SELF_TEST_FS * _SELF_TEST_OVERSAMPLE
    total = count * _SELF_TEST_OVERSAMPLE + warmup
    source = _red_source(total, alpha, rng)
    filtered = _lowpass(source, cutoff, inner_fs, order)
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
    cutoff: float,
    count: int,
    order: int,
    seed: int,
    alpha: float = _SELF_TEST_RED_ALPHA,
    rms: float = _POLLUTION_RMS,
) -> list[float]:
    """与 :func:`_synthesize` 相同，但在**抽取之后**叠加一路共同的低频污染。"""
    clean = _synthesize(cutoff, count, order, seed, alpha)
    phase = (seed % 97) / 97.0 * 2 * math.pi
    step = 2 * math.pi * _POLLUTION_HZ / _SELF_TEST_FS
    return [
        value + rms * math.sqrt(2) * math.sin(phase + step * i)
        for i, value in enumerate(clean)
    ]


_POLLUTION_STRESS = 10.0
"""污染幅度的加码倍数。

标定幅度（:data:`_POLLUTION_RMS`）是按白噪声模型挑的「旧门槛挡不下、新门槛挡得下」
那一档。红噪声下这个统计量分不开，必须排除「只是幅度不够」这一种解释，所以再按 10 倍
跑一遍。10 倍仍然分不开，才说得上是统计量本身没有分辨力。
"""

_SHORT_CASE_SECONDS = 8.0
"""``MIN_SEGMENTS`` 的演示用例：短于判读所需的一段采集。

这道门槛此前从未被标定评估过——和 ``MIN_DYNAMIC_RANGE_DB`` 一样，预注册了就没再管。
台账通则把它捞了出来。
"""


@dataclass(frozen=True, slots=True)
class _CaseResult:
    """一个合成用例上，:data:`GATE_LEDGER` 各门槛关心的那几个量。

    字段名与 :class:`_Outcome`（真机结果）刻意保持一致，好让同一份台账两边都能跑——
    真机回归核对靠的就是这一点。
    """

    plateau_level: float
    spread: float
    dynamic_range_db: float
    segments: int
    cutoff: float | None
    link_defects: int = 0
    """合成数据没有链路，恒为 0。留这个字段是为了字段名对齐，不是为了假装评估过它。"""


def _run_case(
    count: int,
    order: int,
    truth: float,
    base: int,
    *,
    alpha: float = _SELF_TEST_RED_ALPHA,
    pollution_rms: float = 0.0,
) -> _CaseResult:
    """跑一个合成用例：三轴参考档 + 三轴候选档，走同一套分析流程。

    ``base`` 决定种子，所以「同一组种子、只加污染」这种对照是可做的——拐点有没有被
    带偏，只有在种子对齐时问才有意义。
    """

    def axes(cutoff: float, offset: int) -> list[list[float]]:
        if pollution_rms > 0:
            return [
                _synthesize_polluted(
                    cutoff, count, order, base + offset + i, alpha, pollution_rms
                )
                for i in range(3)
            ]
        return [
            _synthesize(cutoff, count, order, base + offset + i, alpha)
            for i in range(3)
        ]

    freqs, ref_power, segments = _average_axes(axes(256.0, 0), _SELF_TEST_FS)
    _, power, _ = _average_axes(axes(truth, 100), _SELF_TEST_FS)
    ratio, spread, level, floor = _ratio_curve(freqs, power, ref_power)
    return _CaseResult(
        plateau_level=level,
        spread=spread,
        dynamic_range_db=_dynamic_range_db(floor),
        segments=segments,
        cutoff=_cutoff(freqs, ratio),
    )


def _pollution_case(
    count: int, rms: float = _POLLUTION_RMS
) -> tuple[_CaseResult, _CaseResult]:
    """同一组种子跑两遍：不掺污染、掺污染。返回 ``(干净, 污染)``。

    **种子必须对齐。** 「污染有没有把拐点带偏」这个问题，只有在其它一切都相同的情况下
    问才有意义；拿两组不同的随机实现去比，比的是随机涨落。
    """
    order = 2
    base = 70_000
    return (
        _run_case(count, order, 42.0, base),
        _run_case(count, order, 42.0, base, pollution_rms=rms),
    )


def _short_case() -> _CaseResult:
    """一段短于判读所需的采集，用来演示 :data:`MIN_SEGMENTS` 挡得下什么。"""
    return _run_case(int(_SELF_TEST_FS * _SHORT_CASE_SECONDS), 2, 42.0, 80_000)


def _cutoff_bias(clean: _CaseResult, polluted: _CaseResult) -> float:
    """污染把 −3 dB 估计带偏了多少（相对值）。测不出拐点算作 ``inf``——那是被带偏
    到没有了，不是「没有影响」。"""
    if clean.cutoff is None or polluted.cutoff is None or clean.cutoff <= 0:
        return math.inf
    return abs(polluted.cutoff - clean.cutoff) / clean.cutoff


def _colour_contrast(count: int) -> None:
    """打印「源噪声颜色 × 污染幅度」的对照表。

    这张表是 RAY-315 退掉平台水平门槛的直接依据，所以它由脚本自己跑出来，不靠转述。
    为了控制耗时只跑 2 阶、三个窄档真值——门槛当初也只施加于窄档。
    """
    print(f"\n{'─' * 66}")
    print("源噪声颜色对照（RAY-315）：平台绝对水平还分不分得开干净与污染")
    print(f"{'─' * 66}")
    print(f"{'源噪声':<14}{'干净':>18}{'掺污染 ×1':>18}{'掺污染 ×' + f'{_POLLUTION_STRESS:.0f}':>18}")

    for label, alpha in (
        ("白 α=0", _SELF_TEST_WHITE_ALPHA),
        (f"红 α={_SELF_TEST_RED_ALPHA}", _SELF_TEST_RED_ALPHA),
        ("红 α=0.999", 0.999),
    ):
        cells = []
        for rms in (0.0, _POLLUTION_RMS, _POLLUTION_RMS * _POLLUTION_STRESS):
            levels = [
                _run_case(
                    count, 2, truth, 90_000 + 500 * index, alpha=alpha, pollution_rms=rms
                ).plateau_level
                for index, truth in enumerate((10.0, 20.0, 42.0))
            ]
            usable = [v for v in levels if math.isfinite(v)]
            cells.append(
                f"{min(usable):.3f}–{max(usable):.3f}" if usable else "无有效平台"
            )
        print(f"{label:<14}" + "".join(f"{cell:>18}" for cell in cells))

    print(
        "\n共同污染把比值推向 1。白噪声下平台原本在 0.2–0.3，那是 2–3 倍的抬升，分得开；\n"
        "红噪声下平台本来就在 1 附近，**没有可抬升的余量**——幅度加到 10 倍也一样。\n"
        "所以不是门槛取值没找对，是这个统计量在真实噪声模型下没有分辨力。"
    )


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
    # 窄档的平台绝对水平。只统计、不判定——它已从门槛退为诊断量。
    narrow_levels: list[float] = []
    best_floor = math.inf     # 地板越小动态范围越大，故取最小者代表最好情况

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
                ratio, spread, level, floor = _ratio_curve(freqs, power, ref_power)
                if math.isfinite(floor) and floor > 0:
                    best_floor = min(best_floor, floor)
                row_spread = max(row_spread, spread)
                worst_spread = max(worst_spread, spread)
                # 只统计窄档：宽档（98/188 Hz）与 256 Hz 参考档在通带内本来就
                # 差不多，水平天然接近 1，混进来会让这个区间失去意义。
                if math.isfinite(level) and truth <= 42.0:
                    narrow_levels.append(level)
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
    leaked = [
        truth
        for truth in (10.0, 98.0, 188.0)
        if any(low <= v <= high for v in observed[truth])
    ]
    for truth in leaked:
        inside = [v for v in observed[truth] if low <= v <= high]
        print(
            f"\n❌ 真值 {truth:.0f} Hz 落进了主判据区间 {low:.0f}–{high:.0f} Hz"
            f"（{len(inside)}/{len(observed[truth]) or 0} 组，最低 {min(inside):.1f} Hz）"
            "——判据无法把它与 42 Hz 分开。"
        )
        ok = False
    if 98.0 in leaked:
        print(
            "   这一条是 RAY-315 换用红噪声源之后才暴露的：白噪声下 98 Hz 永远测不出\n"
            "   拐点（能量全在低频，奈奎斯特附近没东西可看），红噪声下它测得出。\n"
            "   **本轮没有动 VERDICT_BAND**——改判据区间是判据修改，要单独确认并升\n"
            "   需求修订。它不改变任何已有结论，因为动态范围那道门槛在干净合成数据上\n"
            "   就已经不过，本方法已经退役，主判据轮不到生效。"
        )
    print(
        f"\n干净窄档的平台绝对水平：{min(narrow_levels):.3f}–{max(narrow_levels):.3f}"
        f"（诊断量，不是门槛；常量 PLATEAU_LEVEL_CLEAN 记的就是这个区间）"
    )
    if PLATEAU_LEVEL_CLEAN != (0.0, 0.0):
        low, high = PLATEAU_LEVEL_CLEAN
        if not (low <= min(narrow_levels) and max(narrow_levels) <= high):
            print(
                f"⚠️ 与 PLATEAU_LEVEL_CLEAN 记的 {low:.2f}–{high:.2f} 对不上。"
                "常量该跟着标定走，去更新它。"
            )

    _colour_contrast(count)

    # ---- 门槛台账。RAY-315 立的通则：一道都不许跳过 ----
    print(f"\n{'─' * 66}")
    print("门槛台账：每一道预注册的门槛，标定都必须评估它（RAY-315）")
    print(f"{'─' * 66}")

    best_db = _dynamic_range_db(best_floor)
    clean_case = _CaseResult(
        plateau_level=max(narrow_levels),
        spread=worst_spread,
        dynamic_range_db=best_db,
        segments=segments,
        cutoff=None,
    )
    pollution_clean, pollution_dirty = _pollution_case(count)
    bias = _cutoff_bias(pollution_clean, pollution_dirty)
    # 只放「失败模式」用例：``case == "clean"`` 的门槛由下面那个分支单独处理，
    # 因为对它们而言「干净数据触发」本身就是结论，没有第二个用例可比。
    cases = {"pollution": pollution_dirty, "short": _short_case()}
    # 分不开时可用的豁免依据：必须是**实测**出来的，不能只是台账里那句话。
    harmless_checks = {
        "MAX_PLATEAU_SPREAD": (
            bias <= POLLUTION_BIAS_TOLERANCE,
            (
                f"同一组种子，掺污染前后 −3 dB 估计 "
                f"{_format_cutoff(pollution_clean.cutoff)} → "
                f"{_format_cutoff(pollution_dirty.cutoff)}，"
                f"相对变化 {bias:.1%}（容差 ≤{POLLUTION_BIAS_TOLERANCE:.0%}）"
            ),
        ),
    }

    for gate in GATE_LEDGER:
        sign = "≤" if gate.trips_when == "above" else "≥"
        print(f"\n· {gate.constant} {sign}{gate.limit:g} —— 挡的是：{gate.guards}")

        clean_value = _gate_reading(gate, clean_case) if gate.reading else math.nan
        if gate.case is None:
            if not gate.unevaluable:
                print("  ❌ 台账没写为什么评估不了。静默跳过正是本通则要禁的事。")
                ok = False
            else:
                print(f"  ⊘ 本合成评估不了：{gate.unevaluable}")
            continue

        if gate.case == "clean":
            print(f"  干净数据：{clean_value:g}")
            if _gate_trips(gate, clean_value):
                print("  ⚠️ **干净合成数据本身就触发了这道门槛——本方法达不到自己的要求。**")
            else:
                print("  ✅ 干净数据不误报。")
            continue

        case_value = _gate_reading(gate, cases[gate.case])
        print(f"  干净数据：{clean_value:g}    {gate.case} 用例：{case_value:g}")
        if _gate_trips(gate, clean_value):
            print("  ❌ 干净数据就触发了它——会误报，门槛定得太紧。")
            ok = False
            continue
        if _gate_trips(gate, case_value):
            print("  ✅ 分得开：干净不触发，失败模式触发。")
            continue

        print("  ⚠️ 分不开：失败模式也没触发它。按通则要拿出「该失败模式无害」的实测证据。")
        checked = harmless_checks.get(gate.constant)
        if not gate.harmless or checked is None:
            print("  ❌ 没有登记豁免依据。**分不开又证明不了无害的门槛不许留着。**")
            ok = False
            continue
        passed, detail = checked
        print(f"  依据：{detail}")
        if passed:
            print("  ✅ 无害成立，豁免通过。")
        else:
            print("  ❌ 无害不成立——它挡不下，而那个失败模式确实有害。")
            ok = False

    if best_db < MIN_DYNAMIC_RANGE_DB:
        print(
            "\n⚠️ **本方法在干净合成数据上就达不到自己预注册的动态范围门槛。**\n"
            "   这不是标定坏了，是方法不适用：噪声底在器件滤波器之后又被一层共同的\n"
            "   地板（ADC 量化 / 数字化 / 链路）压住，比值压不下去，−3 dB 拐点无从谈起。\n"
            "   真机已于 2026-08-28 跨两台设备实测确认（6.0–13.8 dB），RAY-304 据此\n"
            "   定论「噪声底法测不出绝对赫兹数」。**不要再拿它去跑真机**——要 40 dB\n"
            "   得改用主动激励（已知频率、幅度远高于地板的振动输入，扫频测传递函数）。"
        )

    if ok:
        print(
            "\n✅ 标定自洽：判据区间容得下已知真值、把相邻档位排除在外，"
            "且每一道门槛都被评估过。"
        )
    elif best_db < MIN_DYNAMIC_RANGE_DB:
        print(
            "\n❌ 标定不自洽（见上面标 ❌ 的行）。\n"
            "   注意这**不是**「把判据修好就能去跑真机」：动态范围那道门槛在干净合成\n"
            "   数据上就已经不过，本方法已经退役。修判据的意义只在于把结论记准确。"
        )
    else:
        print("\n❌ 标定不自洽。**先修判据，再去占用真机时间。**")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


async def _collect(
    device: WT901Device, seconds: float
) -> tuple[list[list[float]], list[list[float]], float, int]:
    """采一段，返回 ``(加速度三轴, 角速度三轴, 实测采样率, 链路缺陷数)``。

    缺陷数 = 丢样 + 重同步。返回计数而不是布尔，是为了让 :data:`GATE_LEDGER` 能像
    读别的门槛一样读它，而不是在判读里另写一条 if。

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
    defects = (after.dropped_samples - before.dropped_samples) + (
        after.resync_count - before.resync_count
    )
    return accel, gyro, len(accel[0]) / elapsed, defects


async def _measure(
    device: WT901Device, code: int, seconds: float
) -> tuple[list[float], list[float], list[float], int, float, int]:
    """设一档带宽并采一段，返回 ``(频率, 加速度谱, 角速度谱, 段数, 采样率, 链路缺陷数)``。"""
    await device.registers.write(Register.BANDWIDTH, code, persist=False)
    await asyncio.sleep(SETTLE)
    accel, gyro, fs, defects = await _collect(device, seconds)

    freqs, accel_power, segments = _average_axes(accel, fs)
    _, gyro_power, _ = _average_axes(gyro, fs)
    return freqs, accel_power, gyro_power, segments, fs, defects


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
    link_defects: int
    """采集期间的丢样 + 重同步总数。台账（:data:`GATE_LEDGER`）读的是这个数而不是
    ``clean``，好让同一份台账在合成用例与真机结果上跑的是同一段代码。"""
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
    reference_defects = 0
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
            _, ref_accel, ref_gyro, reference_segments, ref_fs, reference_defects = ref
            print(
                f"   实测采样率 {ref_fs:.2f} Hz，{reference_segments} 段，"
                f"链路 {'干净 ✅' if reference_defects <= MAX_LINK_DEFECTS else '有丢样/重同步 ❌'}"
            )

            for code in SWEEP_CODES:
                role = "锚点" if code in ANCHOR_CODES else "待判定"
                print(
                    f"\n── 0x{int(code):02X}（标称 {NOMINAL_HZ[code]:.0f} Hz，{role}）"
                )
                freqs, accel, gyro, segments, fs, defects = await _measure(
                    device, code, seconds
                )
                print(
                    f"   实测采样率 {fs:.2f} Hz，{segments} 段，"
                    f"链路 {'干净 ✅' if defects <= MAX_LINK_DEFECTS else '有丢样/重同步 ❌'}"
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
                    f"（诊断量，不是门槛；干净合成标定 "
                    f"{PLATEAU_LEVEL_CLEAN[0]:.2f}–{PLATEAU_LEVEL_CLEAN[1]:.2f}）"
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
                        clean=defects <= MAX_LINK_DEFECTS,
                        link_defects=defects,
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

    _report(outcomes, reference_segments, reference_defects)
    return 0


def _report(outcomes: list[_Outcome], ref_segments: int, ref_defects: int) -> None:
    print(f"\n{'=' * 70}")
    print("按 RAY-304 scope 2 开工前预注册的判据判读")
    print("（其中「通带平台绝对水平 ≤0.40」已由 RAY-315 退为诊断量）")
    print(f"{'=' * 70}")

    if not outcomes:
        print("\n❌ 采集未完成，无法判读。")
        return

    # ---- 诊断量：通带平台绝对水平。**不是门槛**，见 PLATEAU_LEVEL_CLEAN ----
    levels = [o.plateau_level for o in outcomes if math.isfinite(o.plateau_level)]
    if levels:
        print(
            f"\n通带平台绝对水平 {min(levels):.3f}–{max(levels):.3f}"
            f"（诊断量；干净合成标定 "
            f"{PLATEAU_LEVEL_CLEAN[0]:.2f}–{PLATEAU_LEVEL_CLEAN[1]:.2f}）"
        )

    # ---- 前置门槛：逐条按 GATE_LEDGER 走，一道都不跳过 ----
    reference = _CaseResult(
        plateau_level=math.nan,
        spread=math.nan,
        dynamic_range_db=math.inf,
        segments=ref_segments,
        cutoff=None,
        link_defects=ref_defects,
    )
    for gate in GATE_LEDGER:
        readings = [_gate_reading(gate, o) for o in outcomes]
        readings.append(_gate_reading(gate, reference))
        tripped = [value for value in readings if _gate_trips(gate, value)]
        if not tripped:
            continue
        if gate.constant == "MIN_DYNAMIC_RANGE_DB":
            # 这一道不在这里报：它不过意味着「本方法测不出来」，是一种完整交付，
            # 不是「重采」。专门的说明在下面。用 continue 而不是 break，这样
            # 将来在它后面加门槛也不会被悄悄跳过。
            continue
        worst = max(tripped) if gate.trips_when == "above" else min(tripped)
        print(
            f"\n❌ 前置门槛 {gate.constant} 不过：实测最差 {worst:g}，"
            f"门槛 {'≤' if gate.trips_when == 'above' else '≥'}{gate.limit:g}。\n"
            f"   它挡的是：{gate.guards}。\n"
            "   **这种数据不能判读**，重采。"
        )
        return

    # ---- 动态范围不过：这是预注册判据列明的完整交付之一，不是「重采」 ----
    worst_range = min(o.dynamic_range_db for o in outcomes)
    if worst_range < MIN_DYNAMIC_RANGE_DB:
        print(
            f"\n⚠️ 前置门槛 MIN_DYNAMIC_RANGE_DB 不过：可用动态范围最低仅 "
            f"{worst_range:.1f} dB，低于 {MIN_DYNAMIC_RANGE_DB:.0f} dB。\n"
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
        f"\n✅ 前置门槛全部通过（{len(GATE_LEDGER)} 道）："
        f"离散 ≤{max(o.spread for o in outcomes):.2f}、"
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


USAGE = f"""\
用法：
  probe_bandwidth.py [每档秒数]           真机取证（需已授权蓝牙的终端）
  probe_bandwidth.py --self-test [秒数]   合成标定，不连设备
  probe_bandwidth.py --help

每档秒数默认 {DEFAULT_SECONDS:.0f}。七档全测：参考档 0x00(256 Hz) 先单独采一次，
其余 {len(SWEEP_CODES)} 档作为候选逐个扫，**共 {len(SWEEP_CODES) + 1} 次采集**，
总时长约为每档秒数的 {len(SWEEP_CODES) + 1} 倍，再加上每档写寄存器与稳定的时间。

取证前请确认：设备静置在不振动的地方（本方法量的是噪声底，任何振动都是污染），
采集速率固定 200 Hz，且先记下 0x1F 的读回值——否则无法区分测的是哪一档。
"""


def _parse_seconds(raw: str) -> float:
    """把秒数解析成正的浮点数；给不出就退出并说清楚，不抛栈。"""
    try:
        seconds = float(raw)
    except ValueError:
        raise SystemExit(f"秒数要是一个数，收到的是 {raw!r}\n\n{USAGE}") from None
    if seconds <= 0:
        raise SystemExit(f"秒数要是正数，收到的是 {seconds}\n\n{USAGE}")
    return seconds


async def main() -> int:
    arguments = sys.argv[1:]
    # 手写解析而不是 argparse：这个工具是交给人在终端里手敲的，参数只有两个，
    # 但打错时必须给出能照做的提示——上一版 `--help` 直接抛 traceback。
    if arguments and arguments[0] in {"-h", "--help"}:
        print(USAGE, end="")
        return 0
    if arguments and arguments[0] == "--self-test":
        seconds = _parse_seconds(arguments[1]) if len(arguments) > 1 else DEFAULT_SECONDS
        return _self_test(seconds)
    if arguments and arguments[0].startswith("-"):
        raise SystemExit(f"认不得的选项 {arguments[0]!r}\n\n{USAGE}")
    seconds = _parse_seconds(arguments[0]) if arguments else DEFAULT_SECONDS
    return await _run_probe(seconds)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
