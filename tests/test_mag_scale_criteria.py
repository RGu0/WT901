"""磁场量纲判据的分析部分（RAY-312 scope 1）。

取证要真机，**但判读不要**。球面拟合、覆盖度、以及那套 15% / 30% 的判定规则全是
纯函数，可以在没有设备的情况下逐条钉死。

这件事本身是这个 scope 的重点：RAY-298 的教训是「测完才发现证据不支持结论」。
把判读逻辑先写死并测过，就不会在拿到真机数据之后再去调门槛——那等于没有预注册。
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 这个 import 必须在上面那行 sys.path 之后，tools/ 不是包，靠 PEP 420 命名空间包解析。
from tools.probe_mag_scale import (
    ACCEPT_TOLERANCE,
    DOC_COEFFICIENT,
    MEAN_DIRECTION_LIMIT,
    MIN_OCTANTS,
    PYTHON_COEFFICIENT,
    REJECT_TOLERANCE,
    RESIDUAL_LIMIT,
    _separability,
    coverage,
    fit_sphere,
    judge,
)


def _sphere_points(
    center: tuple[float, float, float],
    radius: float,
    count: int,
    *,
    hemisphere: bool = False,
    noise: float = 0.0,
    seed: int = 7,
) -> list[tuple[float, float, float]]:
    """在球面上均匀取点。``hemisphere`` 只取上半球，用来构造覆盖不足的点云。"""
    rng = random.Random(seed)
    points: list[tuple[float, float, float]] = []
    for _ in range(count):
        # Marsaglia：先在单位球面上均匀取方向。
        while True:
            u = rng.uniform(-1, 1)
            v = rng.uniform(-1, 1)
            if u * u + v * v < 1:
                break
        factor = 2 * math.sqrt(1 - u * u - v * v)
        x, y, z = u * factor, v * factor, 1 - 2 * (u * u + v * v)
        if hemisphere:
            z = abs(z)
        points.append(
            (
                center[0] + radius * x + rng.gauss(0, noise),
                center[1] + radius * y + rng.gauss(0, noise),
                center[2] + radius * z + rng.gauss(0, noise),
            )
        )
    return points


# ----- 球面拟合 -------------------------------------------------------------


def test_fit_recovers_center_and_radius() -> None:
    """球心就是硬磁偏置，半径就是真实场强对应的计数值。"""
    points = _sphere_points((120.0, -60.0, 35.0), 350.0, 400)
    fitted = fit_sphere(points)
    assert fitted is not None
    center, radius = fitted

    assert center[0] == pytest.approx(120.0, abs=1.0)
    assert center[1] == pytest.approx(-60.0, abs=1.0)
    assert center[2] == pytest.approx(35.0, abs=1.0)
    assert radius == pytest.approx(350.0, abs=1.0)


def test_fit_is_not_fooled_by_a_large_hard_iron_offset() -> None:
    """偏置比半径还大时，静置单点读数会彻底失效；拟合不会。

    这正是判据里用转动拟合、而不是「静置读一次模长」的全部理由。
    """
    center = (900.0, -700.0, 500.0)
    points = _sphere_points(center, 350.0, 500, noise=3.0)
    fitted = fit_sphere(points)
    assert fitted is not None
    _, radius = fitted
    assert radius == pytest.approx(350.0, rel=0.02)

    # 对照：把偏置当成零去算模长，会得到一个偏出去十几倍的「场强」。
    naive = sum(math.sqrt(x * x + y * y + z * z) for x, y, z in points) / len(points)
    assert naive > radius * 3


def test_fit_refuses_a_degenerate_cloud() -> None:
    """根本没转动时不该交出一个数值上收敛的假半径。"""
    assert fit_sphere([(1.0, 2.0, 3.0)] * 50) is None


# ----- 覆盖度 ---------------------------------------------------------------


def test_full_sphere_coverage_passes() -> None:
    center = (10.0, 10.0, 10.0)
    points = _sphere_points(center, 300.0, 400)
    occupied, mean_direction = coverage(points, center)
    assert occupied == 8
    assert mean_direction <= MEAN_DIRECTION_LIMIT


def test_planar_rotation_is_caught() -> None:
    """最现实的失败模式：把设备平放在桌上原地转一圈。

    点云共面时法方程接近奇异但**不精确奇异**——浮点噪声让它照样解得出一个数，
    `fit_sphere` 挡不住。挡住它的只有覆盖度。这条比只扫半球那条更重要，因为
    「平放着转一圈」正是不看说明的人最可能做的动作。
    """
    rng = random.Random(11)
    center = (50.0, -30.0, 200.0)
    points = [
        (
            center[0] + 300.0 * math.cos(angle) + rng.gauss(0, 1.0),
            center[1] + 300.0 * math.sin(angle) + rng.gauss(0, 1.0),
            center[2] + rng.gauss(0, 1.0),
        )
        for angle in [rng.uniform(0, 2 * math.pi) for _ in range(400)]
    ]
    fitted = fit_sphere(points)
    # 拟合本身多半仍会给出一个数——这正是问题所在。
    if fitted is not None:
        occupied, _ = coverage(points, fitted[0])
        assert occupied < MIN_OCTANTS, "共面点云必须被卦限占用度挡下"


def test_hemisphere_sweep_is_caught() -> None:
    """只扫半个球面时，拟合仍会收敛——覆盖度是唯一能挡住它的那道闸。"""
    center = (10.0, 10.0, 10.0)
    points = _sphere_points(center, 300.0, 400, hemisphere=True)
    occupied, mean_direction = coverage(points, center)
    assert mean_direction > MEAN_DIRECTION_LIMIT
    assert occupied <= 4


# ----- 可分性表：判据的前置条件 ---------------------------------------------


@pytest.mark.parametrize(
    ("mag_type", "expect"),
    [
        (3, "能分开"),
        (6, "能分开"),
        (7, "能分开"),
        (2, "勉强"),
        (4, "勉强"),
        (5, "分不开"),
    ],
)
def test_separability_table(mag_type: int, expect: str) -> None:
    """能不能用磁场量级判，完全取决于 0x72 —— Issue 正文把它当成了备注。

    尤其 type 5：SDK 的 ×0.098 与协议文档的 ×0.1 只差 2%，本方法注定分不开。
    脚本必须在采集**之前**说出这件事，别让人白转五分钟。
    """
    assert expect in _separability(mag_type)


def test_unknown_mag_type_is_reported_not_guessed() -> None:
    assert "不在本库已知分档内" in _separability(99)


# ----- 判读门槛 -------------------------------------------------------------


def test_verdict_when_one_candidate_wins() -> None:
    """type 3：SDK 与协议文档差 7.7 倍，实测落在 SDK 上时应当判定成立。"""
    verdict, scored = judge(0.013, mag_type=3)
    assert "判定成立" in verdict
    assert "SDK type 3" in verdict
    # 其余两个都必须被排除，而不只是「没被选中」。
    others = [item for item in scored if "SDK" not in item[0]]
    assert all(deviation > REJECT_TOLERANCE for _, _, deviation in others)


def test_verdict_when_the_method_cannot_separate() -> None:
    """type 5：SDK ×0.098 与协议文档 ×0.1 差 2%，实测无论落在哪都分不开。

    这是**预注册的结果之一，不是失败**——它必须被明确说出来，否则下一个人会
    以为「随便挑一个差不多的」就算定论了。
    """
    verdict, _ = judge(0.099, mag_type=5)
    assert "分不开" in verdict
    assert "层次一" in verdict


def test_verdict_when_all_three_are_wrong() -> None:
    """三份资料全不对是最有价值的结果，不能被判成「不确定」。"""
    verdict, scored = judge(0.5, mag_type=2)
    assert "全不对" in verdict
    assert all(deviation > REJECT_TOLERANCE for _, _, deviation in scored)


def test_verdict_refuses_the_grey_zone() -> None:
    """落在 15%–30% 之间时判据不判读——那是「重采」，不是「大概是它」。"""
    # 0.122 相对协议文档 0.1 偏 22%，落在灰区；相对 SDK type 2 的 0.15 偏 19%，也在灰区。
    verdict, _ = judge(0.122, mag_type=2)
    assert "不确定" in verdict
    assert "重采" in verdict


def test_thresholds_are_the_pre_registered_ones() -> None:
    """门槛值本身钉住。事后调门槛等于没有预注册（RAY-298 的教训）。

    **五个门槛一个都不能漏。** 只钉判定门槛而放过拟合质量门槛，等于留了一条后门：
    数据不干净时把 RMS 上限从 8% 松到 15%，判定门槛再严也没用。
    """
    # 判定
    assert ACCEPT_TOLERANCE == 0.15
    assert REJECT_TOLERANCE == 0.30
    # 拟合质量
    assert RESIDUAL_LIMIT == 0.08
    assert MIN_OCTANTS == 6
    assert MEAN_DIRECTION_LIMIT == 0.35
    # 候选系数
    assert DOC_COEFFICIENT == 0.1
    assert PYTHON_COEFFICIENT == pytest.approx(1 / 120)


def test_candidates_always_include_all_three_sources() -> None:
    """三份资料都要被评分。少列一份，判读就退回本 Issue 要修的那个状态。"""
    _, scored = judge(0.15, mag_type=2)
    names = " ".join(name for name, _, _ in scored)
    assert "协议文档" in names
    assert "SDK" in names
    assert "Python" in names


# ----- 文档交叉引用 ---------------------------------------------------------


def test_docstring_names_the_issue_and_the_type5_caveat() -> None:
    """「要单独立项取证」而不说是哪条 Issue，等于读的人找不到判据在哪。

    同时钉住 type 5 那条限制：不写它，下一个人会照着「读一次模长看合不合理」去
    做一次注定分不出结果的实验——这正是 RAY-298 那次的失败方式。
    """
    from wt901.protocol.units import magnetic_field_to_ut

    doc = magnetic_field_to_ut.__doc__
    assert doc is not None
    assert "RAY-312" in doc
    assert "probe_mag_scale" in doc
    assert "0x72" in doc
    # 不得把判据说成「读一次模长看合不合理」那么简单。
    assert "注定分不开" in doc


def test_protocol_doc_records_the_tightened_criteria() -> None:
    doc = Path(__file__).resolve().parent.parent / "docs" / "protocol.md"
    text = doc.read_text(encoding="utf-8")
    assert "RAY-312" in text
    assert "probe_mag_scale.py" in text
    # 那句「÷120 几乎可以直接排除」的适用范围必须被限定住。
    flattened = text.replace("\n> ", "").replace("\n", "")
    assert "只在type 2/4/5 上成立" in flattened
