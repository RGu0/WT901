"""连接初期读可靠性：判读逻辑与 docstring（RAY-313 scope 1）。

取证必须真机（量的正是 BLE 连接建立后的瞬态行为，`MemoryTransport` 复现不了），
**但判读不用**。归纳与判定规则是纯函数，可以在这里逐条钉死。

先写死再取证，这是方针 2 的要求，也是 RAY-298 的教训：测完才发现证据不支持结论。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from wt901.config import DEFAULT_READ_RETRIES, DEFAULT_READ_TIMEOUT

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 这个 import 必须在上面那行 sys.path 之后，tools/ 不是包，靠 PEP 420 命名空间包解析。
from tools.probe_read_retries import (
    BAD_LINK_FAILURE_RATE,
    CONTROL_AT,
    LATE_AT,
    PROBED,
    SIGNIFICANT_FAILURE_GAP,
    SIGNIFICANT_LATENCY_RATIO,
    judge,
    summarize,
)


def _results(
    *,
    ok: int = 0,
    timeout: int = 0,
    anomalous: int = 0,
    error: int = 0,
    elapsed: float = 0.05,
) -> list[dict[str, object]]:
    return (
        [{"outcome": "ok", "elapsed": elapsed} for _ in range(ok)]
        + [{"outcome": "timeout", "elapsed": 0.5} for _ in range(timeout)]
        + [{"outcome": "anomalous", "elapsed": elapsed} for _ in range(anomalous)]
        + [{"outcome": "error", "elapsed": elapsed} for _ in range(error)]
    )


# ----- 归纳 -----------------------------------------------------------------


def test_summarize_counts_timeouts_and_anomalies_separately() -> None:
    """超时与「回帧内容异常」成因不同，合并计数就分不出该加重试还是该加校验。"""
    summary = summarize(_results(ok=24, timeout=4, anomalous=2))
    assert summary["reads"] == 30
    assert summary["timeouts"] == 4
    assert summary["anomalous"] == 2
    assert summary["failure_rate"] == pytest.approx(6 / 30)


def test_summarize_latency_uses_successes_only() -> None:
    """超时那几次的耗时等于 read_timeout，混进去会把中位数整个拉偏。"""
    summary = summarize(_results(ok=10, timeout=10, elapsed=0.05))
    assert summary["median_s"] == pytest.approx(0.05)


def test_summarize_handles_a_phase_with_no_successes() -> None:
    summary = summarize(_results(timeout=30))
    assert summary["failure_rate"] == 1.0
    assert summary["median_s"] is None
    assert summary["p90_s"] is None


# ----- 判定门槛 -------------------------------------------------------------


def test_early_unreliability_is_reported_with_the_failure_mode() -> None:
    """超时为主 → 重试是对症的处置。"""
    early = summarize(_results(ok=20, timeout=10))
    late = summarize(_results(ok=30))
    control = summarize(_results(ok=30))
    verdict = judge(early, late, control)
    assert "早期更不可靠" in verdict
    assert "超时为主" in verdict


def test_anomalous_dominated_failures_get_a_different_prescription() -> None:
    """内容异常为主时，加重试是错的处置——那是「内容不可信」，要加校验。

    这一条是判读里最容易被糊过去的：两种失败都体现为「失败率高」，但一个该加
    重试，另一个加了重试只会更快地拿到同样不可信的内容。
    """
    early = summarize(_results(ok=20, anomalous=10))
    late = summarize(_results(ok=30))
    control = summarize(_results(ok=30))
    verdict = judge(early, late, control)
    assert "早期更不可靠" in verdict
    assert "加校验" in verdict
    assert "超时为主" not in verdict


def test_small_gap_is_not_measurable_rather_than_absent() -> None:
    """差 10 个百分点：判为「测不出」，**不判为「不存在」**。

    这两句话不一样。写成「不存在」会让下一个人以为这件事已经有定论了。
    """
    early = summarize(_results(ok=27, timeout=3))
    late = summarize(_results(ok=30))
    control = summarize(_results(ok=30))
    verdict = judge(early, late, control)
    assert "测不出" in verdict
    assert "不是「不存在」" in verdict


def test_bad_link_run_is_discarded_before_anything_else() -> None:
    """late 相自己就烂时，不能拿这次连接谈早期效应——先作废，不判读。"""
    early = summarize(_results(ok=10, timeout=20))
    late = summarize(_results(ok=25, timeout=5))
    control = summarize(_results(ok=30))
    verdict = judge(early, late, control)
    assert "作废" in verdict
    # 作废优先于一切：不得同时给出「早期更不可靠」的结论。
    assert "早期更不可靠" not in verdict


def test_control_phase_disagreement_is_flagged() -> None:
    """control 与 late 差得多 → 变量可能是累计活动量而不是连接时长。

    这是加第三相的全部理由。没有它，「先早后晚」的顺序会把两种成因混为一谈。
    """
    early = summarize(_results(ok=18, timeout=12))
    late = summarize(_results(ok=30))
    control = summarize(_results(ok=20, timeout=10))
    verdict = judge(early, late, control)
    assert "累计活动量" in verdict


def test_latency_difference_is_reported() -> None:
    early = summarize(_results(ok=30, elapsed=0.20))
    late = summarize(_results(ok=30, elapsed=0.05))
    control = summarize(_results(ok=30, elapsed=0.05))
    verdict = judge(early, late, control)
    assert "耗时显著" in verdict


def test_reversed_direction_is_called_out_not_silently_accepted() -> None:
    early = summarize(_results(ok=30))
    late = summarize(_results(ok=20, timeout=10))
    control = summarize(_results(ok=20, timeout=10))
    verdict = judge(early, late, control)
    # late 相 33% 失败率先触发作废，这正是对的顺序。
    assert "作废" in verdict


def test_no_reachable_reversed_verdict() -> None:
    """「早期反而显著更好」够不到，所以判读里不该有那一支。

    作废闸把 late 失败率卡在 5% 以内，于是 gap = early − late 最低只能到 −5%，
    永远够不到 −15% 的显著门槛。穷举 late 相在闸内的所有取值验证这一点：写一支
    够不到的分支，只会让读的人以为它会发生。
    """
    control = summarize(_results(ok=30))
    for late_failures in range(2):  # 0/30 与 1/30 都 <= 5%
        late = summarize(_results(ok=30 - late_failures, timeout=late_failures))
        early = summarize(_results(ok=30))  # 早期完美，最有利于「反常」出现
        verdict = judge(early, late, control)
        assert "反常" not in verdict


def test_non_timeout_errors_void_the_run() -> None:
    """链路断开一类的错误不是本实验要量的东西，掺进来会污染证据。

    此前所有 WT901Error 都被记成 "timeout"——那会把「链路断了」算进「早期读不
    可靠」的证据里。
    """
    early = summarize(_results(ok=28, error=2))
    late = summarize(_results(ok=30))
    control = summarize(_results(ok=30))
    verdict = judge(early, late, control)
    assert "作废" in verdict
    assert "非超时错误" in verdict
    assert "早期更不可靠" not in verdict


def test_errors_are_counted_separately_from_timeouts() -> None:
    summary = summarize(_results(ok=25, timeout=3, error=2))
    assert summary["timeouts"] == 3
    assert summary["errors"] == 2
    # 非超时错误不计入失败率——它们让整次连接作废，而不是拉高一个比率。
    assert summary["failure_rate"] == pytest.approx(3 / 30)


# ----- 预注册的门槛与实验形状 -----------------------------------------------


def test_thresholds_are_the_pre_registered_ones() -> None:
    """门槛值钉住。事后调门槛等于没有预注册（RAY-298 的教训）。"""
    assert SIGNIFICANT_FAILURE_GAP == 0.15
    assert SIGNIFICANT_LATENCY_RATIO == 1.5
    assert BAD_LINK_FAILURE_RATE == 0.05
    assert LATE_AT == 5.0
    assert CONTROL_AT == 30.0


def test_two_registers_are_probed_not_one() -> None:
    """只测一个寄存器，分不开「早期不可靠」与「0x72 这个寄存器难读」。

    SDK 的两个迹象指向的不是同一件事：5 秒是时间窗口（所有寄存器），0x72 双发
    可能只是那一个寄存器。
    """
    assert len(PROBED) == 2
    assert 0x40 in PROBED  # 温度：SDK 只发一次
    assert 0x72 in PROBED  # MAGTYPE：SDK 唯一双发的那个


# ----- 验收标准第 4 条 ------------------------------------------------------


def test_default_read_retries_now_has_a_documented_basis() -> None:
    """这个公开可调参数此前一个字的说明都没有。

    钉住三件事：它承认自己未实测、它指向 RAY-313、它写明了代价。少任何一件，
    读的人都会以为这个 2 有依据。
    """
    import ast
    import inspect

    import wt901.config as module

    tree = ast.parse(inspect.getsource(module))
    doc: str | None = None
    body = tree.body
    for index, node in enumerate(body):
        is_target = (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "DEFAULT_READ_RETRIES"
        )
        if is_target and index + 1 < len(body):
            following = body[index + 1]
            if isinstance(following, ast.Expr) and isinstance(
                following.value, ast.Constant
            ):
                value = following.value.value
                assert isinstance(value, str)
                doc = value
    assert doc is not None, "DEFAULT_READ_RETRIES 必须有字面量 docstring"
    assert "RAY-313" in doc
    assert "probe_read_retries" in doc

    # scope 1 时这里钉的是「未实测」。scope 2 取证之后那句话不再成立，**但这条守卫
    # 不能就此删掉**：它守的从来不是那三个字，而是「这个公开可调参数必须如实交代
    # 自己的依据」。取证之后要交代的东西反而更多，所以钉得更紧。
    assert "已实测" in doc
    assert "测不出" in doc

    # 最要紧的一句。零失败很容易被下一个改这段的人写成「实测支持 2」或「实测表明
    # 不需要重试」——两句话这批数据都支持不了。
    assert "不是「不存在」" in doc


def test_default_read_retries_docstring_does_not_overclaim() -> None:
    """docstring 不得声称实测**支持**这个取值，或**否证**了早期不可靠。

    实测结论是「测不出」：既没给出改动它的依据，也没证明官方 SDK 那 5 秒是多余的。
    这条钉住的是措辞的方向——一个只差几个字的改写就能把「约束了效应大小」变成
    「否证了效应存在」。
    """
    from wt901 import config

    assert config.__file__ is not None
    source = Path(config.__file__).read_text(encoding="utf-8")
    start = source.index("DEFAULT_READ_RETRIES = 2")
    end = source.index("DEFAULT_WRITE_DELAY", start)
    block = source[start:end]

    for forbidden in ("实测支持", "实测表明不需要", "不存在这个效应", "可以去掉重试"):
        assert forbidden not in block, forbidden
    assert "约束不等于否证" in block


def test_default_read_retries_value_is_unchanged() -> None:
    """实测没有给出改动它的依据——「测不出」不是「不需要」。

    这条在 scope 1 与 scope 2 里都成立，但理由变了：scope 1 是「还没测」，
    scope 2 是「测了，这个方法在这个量级上分辨不出来」。
    """
    assert DEFAULT_READ_RETRIES == 2
    assert DEFAULT_READ_TIMEOUT == 0.5


# ----- 取证脚本的设备选择 ---------------------------------------------------


class _Found:
    def __init__(self, address: str, rssi: int | None) -> None:
        self.address = address
        self.rssi = rssi
        self.name = "WT901BLE68"


async def test_probe_picks_the_strongest_device_not_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """台架上那台几乎总是最近的一台，而扫描结果的顺序不保证任何东西。

    此前直接取 `devices[0]`：两台设备在场时选中哪台全凭运气，而连接超时的回溯里
    完全看不出扫到了什么。
    """
    import tools.probe_read_retries as probe

    async def fake_scan(timeout: float) -> list[_Found]:
        assert timeout == probe.SCAN_TIMEOUT
        return [_Found("far", -90), _Found("near", -47)]

    monkeypatch.setattr(probe, "scan", fake_scan)
    chosen = await probe.pick_device(None)
    assert chosen is not None
    assert chosen.address == "near"


async def test_probe_returns_none_when_nothing_is_in_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.probe_read_retries as probe

    async def fake_scan(timeout: float) -> list[_Found]:
        return []

    monkeypatch.setattr(probe, "scan", fake_scan)
    assert await probe.pick_device(None) is None


def test_probe_scan_timeout_matches_the_other_probes() -> None:
    """默认 5 秒在设备刚上电或信号偏弱时常扫不到；tools/ 里其它探测脚本用 15。"""
    import tools.probe_read_retries as probe

    assert probe.SCAN_TIMEOUT == 15.0


def test_probe_take_address() -> None:
    from tools.probe_read_retries import take_address

    assert take_address(["30", "--address", "ABC"]) == ("ABC", ["30"])
    assert take_address(["30"]) == (None, ["30"])


# ---------------------------------------------------------------------------
# RAY-313 scope 2 `read-retry-verdict` 的真机取证（2026-09-02）。
#
# 六次连接（2 台 × 各 3 次，正好是预注册的样本量）的归纳值，逐条抄自
# ``ray-313/read-retry-verdict/acceptance/read_retry_probe.*.json``。
# 钉住它的理由与 RAY-304 / RAY-310 相同：**已经付过代价的真机数据不该被改动后的
# 判读再误判一次**。判据预注册于取证之前，这些测试守的是判据。
#
# 每相记 (failure_rate, timeouts, anomalous, errors, median_s)。
# ---------------------------------------------------------------------------

_HARDWARE_2026_09_02: dict[tuple[str, str, str], tuple[tuple[object, ...], ...]] = {
    ("run1", "26F34505", "0x40"): ((0.0, 0, 0, 0, 0.060026), (0.0, 0, 0, 0, 0.060073), (0.0, 0, 0, 0, 0.060344)),
    ("run1", "26F34505", "0x72"): ((0.0, 0, 0, 0, 0.060418), (0.0, 0, 0, 0, 0.060382), (0.0, 0, 0, 0, 0.060214)),
    ("run2", "DDCD154C", "0x40"): ((0.0, 0, 0, 0, 0.060534), (0.0, 0, 0, 0, 0.059971), (0.0, 0, 0, 0, 0.061075)),
    ("run2", "DDCD154C", "0x72"): ((0.0, 0, 0, 0, 0.059823), (0.0, 0, 0, 0, 0.060526), (0.0, 0, 0, 0, 0.060058)),
    ("run3", "26F34505", "0x40"): ((0.0, 0, 0, 0, 0.060304), (0.0, 0, 0, 0, 0.059944), (0.0, 0, 0, 0, 0.059898)),
    ("run3", "26F34505", "0x72"): ((0.0, 0, 0, 0, 0.060764), (0.0, 0, 0, 0, 0.06031), (0.0, 0, 0, 0, 0.060055)),
    ("run4", "DDCD154C", "0x40"): ((0.0, 0, 0, 0, 0.059722), (0.0, 0, 0, 0, 0.059746), (0.0, 0, 0, 0, 0.060884)),
    ("run4", "DDCD154C", "0x72"): ((0.0, 0, 0, 0, 0.060824), (0.0, 0, 0, 0, 0.060533), (0.0, 0, 0, 0, 0.060863)),
    ("run5", "26F34505", "0x40"): ((0.0, 0, 0, 0, 0.060522), (0.0, 0, 0, 0, 0.060119), (0.0, 0, 0, 0, 0.060205)),
    ("run5", "26F34505", "0x72"): ((0.0, 0, 0, 0, 0.059977), (0.0, 0, 0, 0, 0.059991), (0.0, 0, 0, 0, 0.059891)),
    ("run6", "DDCD154C", "0x40"): ((0.0, 0, 0, 0, 0.059926), (0.0, 0, 0, 0, 0.059732), (0.0, 0, 0, 0, 0.060434)),
    ("run6", "DDCD154C", "0x72"): ((0.0, 0, 0, 0, 0.060161), (0.0, 0, 0, 0, 0.059969), (0.0, 0, 0, 0, 0.060065)),
}


def _phase(values: tuple[object, ...]) -> dict[str, object]:
    failure_rate, timeouts, anomalous, errors, median = values
    return {
        "failure_rate": failure_rate,
        "timeouts": timeouts,
        "anomalous": anomalous,
        "errors": errors,
        "median_s": median,
        "p90_s": median,
        "reads": 30,
    }


def test_hardware_sample_size_matches_the_preregistration() -> None:
    """预注册要求 2 台设备 × 每台 3 次独立连接。"""
    runs = {key[0] for key in _HARDWARE_2026_09_02}
    devices: dict[str, set[str]] = {}
    for run, device, _register in _HARDWARE_2026_09_02:
        devices.setdefault(device, set()).add(run)
    assert len(runs) == 6
    assert len(devices) == 2
    assert all(len(seen) == 3 for seen in devices.values())


@pytest.mark.parametrize("key", sorted(_HARDWARE_2026_09_02))
def test_every_hardware_run_reads_as_not_measurable(
    key: tuple[str, str, str],
) -> None:
    """六次运行两个寄存器全部判为「测不出」，且没有一次触发作废闸。

    这一条同时钉住**措辞**：判读必须说「测不出」，不能说「不存在」。差别在于
    前者允许效应存在而本方法看不见，后者是一个这批数据支持不了的断言。
    """
    early, late, control = (
        _phase(values) for values in _HARDWARE_2026_09_02[key]
    )
    verdict = judge(early, late, control)
    assert verdict.startswith("测不出"), verdict
    assert "不是「不存在」" in verdict
    assert "作废" not in verdict


def test_hardware_runs_had_zero_failures_of_any_kind() -> None:
    """1080 次读零失败——零超时、零内容异常、零非超时错误。

    零错误是「本次连接有效」的前提；零内容异常决定了处置是重试而非加校验的那条
    分支根本没被触发。
    """
    total = 0
    for phases in _HARDWARE_2026_09_02.values():
        for failure_rate, timeouts, anomalous, errors, _median in phases:
            assert failure_rate == 0.0
            assert timeouts == 0 and anomalous == 0 and errors == 0
            total += 30
    assert total == 1080


def test_hardware_latency_ratios_stayed_below_the_threshold() -> None:
    """early ÷ late 的中位比全部远低于预注册的 1.5。

    耗时被 BLE 连接间隔量化成 ~60 / ~90 ms 两档，1.5 倍恰好是「多跨一个间隔」；
    实测比值落在 1.00 附近，即一个间隔都没多跨。
    """
    for phases in _HARDWARE_2026_09_02.values():
        early_median = phases[0][4]
        late_median = phases[1][4]
        assert isinstance(early_median, float) and isinstance(late_median, float)
        ratio = early_median / late_median
        assert ratio < SIGNIFICANT_LATENCY_RATIO
        assert 0.98 <= ratio <= 1.02, ratio
