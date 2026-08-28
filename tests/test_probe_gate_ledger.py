"""``tools/probe_bandwidth.py`` 的门槛台账与真机回归。

取证脚本此前一行测试都没有，而 RAY-304 那次失败的直接成因恰恰是**一道预注册的门槛
从来没被评估过**——那种漏检不会在任何一次运行里报错，只会安静地放行。所以这里测的
不是「算得对不对」，而是**台账有没有漏掉门槛**，以及**已经付过代价的那两轮真机数据
会不会再被误判一次**。

脚本不进 wheel，所以这些测试也不碰公开契约；它们守的是判据本身。
"""

from __future__ import annotations

import importlib.util
import math
import sys
from dataclasses import fields
from pathlib import Path

import pytest

# ``tools/`` 不是包，也不进 wheel——按路径加载，而不是往 sys.path 里塞一个目录。
_PROBE = Path(__file__).resolve().parents[1] / "tools" / "probe_bandwidth.py"
_spec = importlib.util.spec_from_file_location("probe_bandwidth", _PROBE)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
# 必须先登记再 exec：``dataclass`` 会回头去 ``sys.modules`` 里找定义它的模块。
sys.modules[_spec.name] = probe
_spec.loader.exec_module(probe)


# ---------------------------------------------------------------------------
# RAY-304 两轮真机实测。数值逐条抄自
# ``ray-304/bandwidth-cutoff-probe/acceptance/run-{1,2}-console.txt``。
# 两轮都是 22 段、链路 0 缺陷。
# ---------------------------------------------------------------------------

_HARDWARE = {
    "run-1": (
        # (编码, 平台离散, 平台绝对水平, 阻带地板)
        (0x06, 1.67, 1.009, 0.04166),
        (0x05, 1.28, 0.892, 0.16875),
        (0x04, 1.37, 0.932, 0.04783),
        (0x03, 1.16, 0.926, 0.16233),
        (0x02, 1.29, 0.842, 0.72389),
        (0x01, 1.18, 0.946, 1.10438),
    ),
    "run-2": (
        (0x06, 1.09, 0.973, 0.18493),
        (0x05, 1.16, 0.945, 0.24907),
        (0x04, 1.13, 0.975, 0.18777),
        (0x03, 1.22, 1.033, 0.23543),
        (0x02, 1.18, 0.957, 0.62390),
        (0x01, 1.16, 0.950, 1.07059),
    ),
}


def _outcomes(run: str) -> list[probe._Outcome]:
    return [
        probe._Outcome(
            code=code,
            cutoff=None,
            gyro_cutoff=None,
            segments=22,
            fs=198.0,
            clean=True,
            link_defects=0,
            spread=spread,
            plateau_level=level,
            floor=floor,
            band_power_ratio=float("nan"),
        )
        for code, spread, level, floor in _HARDWARE[run]
    ]


# ---------------------------------------------------------------------------
# 台账不许有漏网的门槛
# ---------------------------------------------------------------------------


def test_ledger_covers_every_gate_constant() -> None:
    """模块里每一个 ``MAX_*`` / ``MIN_*`` 门槛常量都必须在台账上。

    这是 RAY-315 那条通则的结构性执行：``MIN_DYNAMIC_RANGE_DB`` 当初就是**预注册了
    但标定从未评估**，而那种疏漏没有任何一次运行会报出来。新增一道门槛却忘了登记，
    这条测试会挂。
    """
    declared = {
        name
        for name in vars(probe)
        if name.startswith(("MAX_", "MIN_")) and not name.startswith("_")
    }
    assert declared == {gate.constant for gate in probe.GATE_LEDGER}


def test_ledger_entries_are_well_formed() -> None:
    """每条条目自洽：读数字段两边都存在，评估不了的必须写明理由。"""
    case_fields = {f.name for f in fields(probe._CaseResult)}
    outcome_fields = {f.name for f in fields(probe._Outcome)} | {"dynamic_range_db"}
    for gate in probe.GATE_LEDGER:
        assert gate.trips_when in ("above", "below"), gate.constant
        assert getattr(probe, gate.constant) == pytest.approx(gate.limit), gate.constant
        # 同一份台账要能在合成用例与真机结果上跑同一段代码，字段名必须两边都有。
        assert gate.reading in case_fields, gate.constant
        assert gate.reading in outcome_fields, gate.constant
        if gate.case is None:
            # 静默跳过正是这条通则要禁的事。
            assert gate.unevaluable, gate.constant


def test_gate_trips_reads_both_directions() -> None:
    above = next(g for g in probe.GATE_LEDGER if g.trips_when == "above")
    below = next(g for g in probe.GATE_LEDGER if g.trips_when == "below")
    assert probe._gate_trips(above, above.limit + 1)
    assert not probe._gate_trips(above, above.limit)
    assert probe._gate_trips(below, below.limit - 1)
    assert not probe._gate_trips(below, below.limit)
    # 「测不出」不是「不合格」：非有限读数一律不触发。
    assert not probe._gate_trips(above, math.nan)
    assert not probe._gate_trips(below, math.inf)


# ---------------------------------------------------------------------------
# 真机回归：RAY-304 那两轮不许再被误判
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run", sorted(_HARDWARE))
def test_hardware_runs_trip_only_dynamic_range(run: str) -> None:
    """两轮真机数据里，唯一该触发的门槛是动态范围。

    此前触发的是「通带平台绝对水平 ≤0.40」，两轮六档全中，脚本据此建议「挪到不振动的
    地方重采」——那个建议是错的，白让用户跑了两轮。RAY-315 把那道门槛退成了诊断量。

    动态范围仍然触发，而且**应该**触发：最好一档 13.8 dB，判据要求 ≥40 dB。那是真阳性，
    也是 RAY-304 的结论所依赖的那一条。
    """
    tripped = {
        gate.constant
        for gate in probe.GATE_LEDGER
        for outcome in _outcomes(run)
        if probe._gate_trips(gate, probe._gate_reading(gate, outcome))
    }
    assert tripped == {"MIN_DYNAMIC_RANGE_DB"}


@pytest.mark.parametrize("run", sorted(_HARDWARE))
def test_retired_level_gate_would_still_have_misfired(run: str) -> None:
    """篡改核对：把退役的那道门槛按原值放回去，两轮真机数据仍然全部触发。

    没有这一条，上面那个回归测试证明不了任何事——门槛退役之后「没有门槛触发」是平凡
    成立的。这里重建 ``MAX_PLATEAU_LEVEL ≤ 0.40``，确认它**确实**会误判，回归测试才
    有内容。
    """
    retired = probe._Gate(
        constant="MAX_PLATEAU_LEVEL",
        limit=0.40,
        trips_when="above",
        reading="plateau_level",
        guards="（已退役）归一化基准失效",
        case="pollution",
    )
    readings = [probe._gate_reading(retired, o) for o in _outcomes(run)]
    assert all(probe._gate_trips(retired, value) for value in readings)


def test_plateau_level_gate_is_gone() -> None:
    """门槛常量必须真的不在了，而不是留着没人用——留着就会有人再拿它去判。"""
    assert not hasattr(probe, "MAX_PLATEAU_LEVEL")
    assert not hasattr(probe, "PLATEAU_LEVEL_MAX_NOMINAL")


def test_clean_calibration_range_covers_hardware() -> None:
    """验收标准第 3 条：重标定后的干净区间必须容得下两轮真机实测的窄档水平。

    窄档实测 0.892–1.033。容不下就说明重标定还是没对上真机，退门槛的理由也就不成立。
    """
    low, high = probe.PLATEAU_LEVEL_CLEAN
    narrow = [
        level
        for run in _HARDWARE
        for code, _, level, _ in _HARDWARE[run]
        if probe.NOMINAL_HZ[code] <= 42.0
    ]
    assert low <= min(narrow)
    assert max(narrow) <= high


# ---------------------------------------------------------------------------
# 噪声颜色必须注入在器件滤波器**之前**
# ---------------------------------------------------------------------------


def _redden(series: list[float], alpha: float) -> list[float]:
    """一阶红化。测试自己实现，好把「加在之后」这条错误路径也跑一遍。"""
    out: list[float] = []
    previous = 0.0
    for value in series:
        previous = alpha * previous + value
        out.append(previous)
    return out


def test_reddening_before_the_filter_moves_the_plateau() -> None:
    """源噪声换成红的，通带平台水平必须明显变化——这是重标定的全部依据。

    白噪声下参考档 ``0x00`` 把 100–256 Hz 折回带内、抬高参考谱，比值因此偏低；红噪声
    高频没那么多东西可折，比值接近 1。差别不出现，就说明红化没有真的作用到单路上。
    """
    count = 2000
    white = probe._run_case(count, 2, 20.0, 50_000, alpha=0.0)
    red = probe._run_case(count, 2, 20.0, 50_000, alpha=probe._SELF_TEST_RED_ALPHA)
    assert white.plateau_level < 0.5
    assert red.plateau_level > 0.8


def test_reddening_after_the_filter_changes_nothing() -> None:
    """红化加在**滤波之后**是一个空测试——把那条错误路径钉住。

    RAY-304 审阅期第一次就踩了这个坑：同一个线性滤波器同时作用于候选与参考，相除时
    精确抵消，三位小数完全不动，于是「测过了」变成了什么都没测。验证任何关于 R̂ 的
    假设，扰动必须只作用于单路。

    这条测试断言的正是那个「没有差别」，所以它同时是上一条测试的对照：只有当红化真的
    在滤波之前时，上一条才可能通过。
    """
    count = 2000
    order, base = 2, 60_000
    reference = [probe._synthesize(256.0, count, order, base + i, 0.0) for i in range(3)]
    candidate = [
        probe._synthesize(20.0, count, order, base + 100 + i, 0.0) for i in range(3)
    ]

    freqs, ref_power, _ = probe._average_axes(reference, probe._SELF_TEST_FS)
    _, power, _ = probe._average_axes(candidate, probe._SELF_TEST_FS)
    _, _, plain, _ = probe._ratio_curve(freqs, power, ref_power)

    alpha = probe._SELF_TEST_RED_ALPHA
    freqs, ref_power, _ = probe._average_axes(
        [_redden(axis, alpha) for axis in reference], probe._SELF_TEST_FS
    )
    _, power, _ = probe._average_axes(
        [_redden(axis, alpha) for axis in candidate], probe._SELF_TEST_FS
    )
    _, _, after, _ = probe._ratio_curve(freqs, power, ref_power)

    # 不是逐位相等：``_redden`` 从零起步，头上带一段瞬态，而 Welch 分段不是平移不变的。
    # 残差约 0.4%，对照之下「红化在滤波之前」把同一个量从 0.27 抬到 0.97（约 250%）。
    # 两者差着两个数量级——这正是「加在之后等于什么都没做」的量化表述。
    assert abs(after - plain) / plain < 0.02
