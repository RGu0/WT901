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
    assert "未实测" in doc
    assert "RAY-313" in doc
    assert "probe_read_retries" in doc


def test_default_read_retries_value_is_unchanged() -> None:
    """本 scope 不改这个值——改它同样需要实测支撑，而实测还没做。"""
    assert DEFAULT_READ_RETRIES == 2
    assert DEFAULT_READ_TIMEOUT == 0.5
