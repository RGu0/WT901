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


@pytest.mark.parametrize("mag_type", [2, 3, 4, 7])
def test_separability_says_decidable(mag_type: int) -> None:
    """这些档位上三个候选两两都够得开，判定规则能给出结果。"""
    verdict = _separability(mag_type)
    assert verdict.startswith("本方法能给出定论"), verdict
    assert "❌" not in verdict


@pytest.mark.parametrize(
    ("mag_type", "blocked_pair"),
    [
        (5, "SDK vs 协议文档"),   # ×0.098 与 ×0.1 只差 1.02 倍
        (6, "SDK vs Python 示例"),  # 1/150 与 /120 只差 1.25 倍
    ],
)
def test_separability_flags_undecidable_pairs(mag_type: int, blocked_pair: str) -> None:
    """判定规则在数学上分不开的档位，必须在**采集之前**说出来。

    **type 6 是 RAY-312 scope 2 栽的那一跤。** 此前这里只比 SDK 与协议文档：type 6 差
    15 倍，判「能分开」，方法据此被认为可行。而 SDK 与 Python 只差 1.25 倍——低于判定
    规则所需的 1 + REJECT_TOLERANCE = 1.30 倍。整轮真机取证做完才发现方法从一开始就
    不可能给出定论。

    注意这条不能只断言输出里有「能分开」三个字：修正后的 type 6 输出里，
    「SDK vs 协议文档……能分开」那一行仍然在，用子串判断会假通过。
    """
    verdict = _separability(mag_type)
    assert verdict.startswith("⚠ **本方法给不出定论**"), verdict
    assert blocked_pair in verdict
    assert "❌" in verdict
    assert "层次一" in verdict


def test_decidable_ratio_follows_the_reject_threshold() -> None:
    """两个候选要能被判定规则分开，最少要差 1 + REJECT_TOLERANCE 倍。

    实测精确命中候选 A 时，候选 B 的相对偏差是 |A−B|/B；要越过排除线就得
    |A−B|/B > REJECT_TOLERANCE，即两者相差超过 1 + REJECT_TOLERANCE 倍。

    这条把预检的门槛与判定门槛**绑在一起**——改了 REJECT_TOLERANCE 而忘了预检，
    正是上一次留下缺陷的方式。
    """
    from tools.probe_mag_scale import _decidable_ratio

    assert _decidable_ratio() == pytest.approx(1.0 + REJECT_TOLERANCE)
    assert PYTHON_COEFFICIENT / (1 / 150) < _decidable_ratio()  # type 6 那一对
    assert DOC_COEFFICIENT / PYTHON_COEFFICIENT > _decidable_ratio()


def test_verdict_windows_for_close_candidates_exclude_their_own_values() -> None:
    """判定窗口落在候选值外侧——测得越准越判不出来。

    这是 RAY-312 scope 2 最有价值的产出，也是上面那条预检要防的东西。对 type 6 的
    SDK 与 Python：能触发「判定成立」的实测系数区间**不包含候选自己的数值**，
    所以一次准确的测量只会得到「不确定」。

    钉住它是为了让下一个改 ACCEPT/REJECT 门槛的人立刻看到这个后果。
    """
    sdk, python = 1 / 150, PYTHON_COEFFICIENT

    def can_decide(measured: float, winner: float, loser: float) -> bool:
        return (
            abs(measured - winner) / winner <= ACCEPT_TOLERANCE
            and abs(measured - loser) / loser > REJECT_TOLERANCE
        )

    # 精确命中任一候选，都判不出来
    assert not can_decide(sdk, sdk, python)
    assert not can_decide(python, python, sdk)
    # 只有偏到候选值外侧才可能判出来
    assert can_decide(sdk * 0.87, sdk, python)
    assert can_decide(python * 1.10, python, sdk)


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


# ----- 设备选择：连不上时最要紧的是「扫到了什么」 ---------------------------


class _Found:
    def __init__(self, address: str, rssi: int | None) -> None:
        self.address = address
        self.rssi = rssi
        self.name = "WT901BLE68"


async def test_pick_device_takes_the_strongest_not_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """台架上那台几乎总是最近的一台，而扫描结果的顺序不保证任何东西。

    此前直接取 `devices[0]`——两台设备在场时，选中哪台全凭运气。
    """
    import tools.probe_mag_scale as probe

    async def fake_scan(timeout: float) -> list[_Found]:
        assert timeout == probe.SCAN_TIMEOUT
        return [_Found("far", -90), _Found("near", -47), _Found("mid", -70)]

    monkeypatch.setattr(probe, "scan", fake_scan)
    chosen = await probe.pick_device(None)
    assert chosen is not None
    assert chosen.address == "near"


async def test_pick_device_honours_an_explicit_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.probe_mag_scale as probe

    async def fake_scan(timeout: float) -> list[_Found]:
        return [_Found("far", -90), _Found("near", -47)]

    monkeypatch.setattr(probe, "scan", fake_scan)
    chosen = await probe.pick_device("far")
    assert chosen is not None
    assert chosen.address == "far"


async def test_pick_device_reports_when_nothing_was_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """扫不到与连不上表现完全不同，不该被一个太短的扫描超时混在一起。"""
    import tools.probe_mag_scale as probe

    async def fake_scan(timeout: float) -> list[_Found]:
        return []

    monkeypatch.setattr(probe, "scan", fake_scan)
    assert await probe.pick_device(None) is None


async def test_pick_device_refuses_an_address_that_is_not_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.probe_mag_scale as probe

    async def fake_scan(timeout: float) -> list[_Found]:
        return [_Found("near", -47)]

    monkeypatch.setattr(probe, "scan", fake_scan)
    assert await probe.pick_device("somewhere-else") is None


def test_scan_timeout_matches_the_other_probes() -> None:
    """默认的 5 秒在设备刚上电或信号偏弱时常扫不到；tools/ 里其它探测脚本用 15。"""
    from tools.probe_mag_scale import SCAN_TIMEOUT

    assert SCAN_TIMEOUT == 15.0


def test_take_address_extracts_the_flag() -> None:
    from tools.probe_mag_scale import take_address

    assert take_address(["49.6", "--address", "ABC", "300"]) == ("ABC", ["49.6", "300"])
    assert take_address(["49.6"]) == (None, ["49.6"])


# ---------------------------------------------------------------------------
# RAY-312 scope 2 `mag-scale-verdict` 的真机取证（2026-09-02）。
#
# 13 次采集逐条抄自 ``ray-312/mag-scale-verdict/acceptance/mag_scale_points.*.json``。
# 钉住它的理由与 RAY-304 / RAY-310 / RAY-313 相同：**已经付过代价的真机数据不该被改动
# 后的判读再误判一次**。判据预注册于取证之前，这些测试守的是判据。
#
# (时间戳, 设备, 是否远离笔记本≥2m, 球半径, 残差比, 覆盖, 卦限, 过闸)
# ---------------------------------------------------------------------------

IGRF_BEIJING_2026_09_02 = 54.9717
"""µT。NOAA WMMHR-2025，39.9°N/116.4°E，2026.66849 的 ``totalintensity``。

**本轮前 8 次用了错值**（49.6 是示例里的上海量级、50.818 来自非计算器的网上查询）。
球半径与 F 无关，故错值可事后重算——但那是运气，不是设计。
"""

_HARDWARE_2026_09_02 = (
    ("20260902T015134", "DDCD154C", False, 6470.9, 0.0967, 0.104, 8, False),
    ("20260902T015237", "DDCD154C", False, 6119.0, 0.0778, 0.212, 8, True),
    ("20260902T020219", "DDCD154C", False, 6279.6, 0.1059, 0.276, 8, False),
    ("20260902T020605", "26F34505", False, 7955.6, 0.0268, 0.436, 8, False),
    ("20260902T214902", "26F34505", False, 7265.6, 0.0338, 0.16, 8, True),
    ("20260902T215004", "26F34505", False, 7333.9, 0.0303, 0.208, 8, True),
    ("20260902T215241", "DDCD154C", False, 6340.5, 0.1225, 0.287, 8, False),
    ("20260902T215520", "DDCD154C", False, 6419.2, 0.0934, 0.353, 7, False),
    ("20260902T222615", "26F34505", True, 8390.6, 0.029, 0.068, 8, True),
    ("20260902T222715", "26F34505", True, 8419.7, 0.0281, 0.419, 7, False),
    ("20260902T223204", "26F34505", True, 8649.4, 0.0285, 0.22, 8, True),
    ("20260902T223552", "DDCD154C", True, 6961.0, 0.1175, 0.179, 8, False),
    ("20260902T223650", "DDCD154C", True, 6764.5, 0.0868, 0.361, 8, False),
)


def _measured(radius: float) -> float:
    return IGRF_BEIJING_2026_09_02 / radius


def test_hardware_protocol_doc_coefficient_is_excluded_everywhere() -> None:
    """协议文档的 ×0.1 在 13/13 次采集里都被排除——本轮最硬的结论。

    这条不挑过闸与否、不挑设备、不挑磁环境：偏差全部远超 30% 的排除线。
    """
    assert len(_HARDWARE_2026_09_02) == 13
    for _ts, _dev, _far, radius, *_rest in _HARDWARE_2026_09_02:
        deviation = abs(_measured(radius) - DOC_COEFFICIENT) / DOC_COEFFICIENT
        assert deviation > REJECT_TOLERANCE
        assert deviation > 0.90


def test_hardware_clean_valid_acquisitions_are_one_device_only() -> None:
    """预注册要求 2 台 × 每台 2 次合格；干净条件下只有一台达标。

    ``DDCD154C`` 不是"没采够"，是**在结构上产不出合格数据**——它的点云不是球。
    """
    clean_valid = [
        (dev, radius)
        for _ts, dev, far, radius, _res, _mn, _oc, ok in _HARDWARE_2026_09_02
        if far and ok
    ]
    assert len(clean_valid) == 2
    devices = {dev for dev, _radius in clean_valid}
    assert devices == {"26F34505"}


def test_hardware_clean_measurements_land_in_the_criteria_blind_spot() -> None:
    """两次干净合格采集都判「不确定」，而不是「判定成立」。

    实测系数与 SDK 的 1/150 只差 1.7% / 4.7%，**数据指向 SDK 很清楚**；但 Python 的
    偏差落在 21–24%，够不到 30% 的排除线，所以判定规则给不出结果。这正是判据缺陷的
    实测印证——**不得因为「明显是 SDK」就把它记成判定成立**。
    """
    sdk = 1 / 150
    checked = 0
    for _ts, _dev, far, radius, _res, _mn, _oc, ok in _HARDWARE_2026_09_02:
        if not (far and ok):
            continue
        measured = _measured(radius)
        sdk_dev = abs(measured - sdk) / sdk
        py_dev = abs(measured - PYTHON_COEFFICIENT) / PYTHON_COEFFICIENT
        assert sdk_dev <= ACCEPT_TOLERANCE
        assert ACCEPT_TOLERANCE < py_dev <= REJECT_TOLERANCE
        assert sdk_dev < 0.05
        checked += 1
    assert checked == 2


def test_hardware_quality_gate_let_through_a_distorted_cloud() -> None:
    """8% 的残差闸拦不住轴长比 1.5 的椭球——DDCD154C 七次里过了一次。

    那次（30 cm 环境，残差 7.8%）恰是该设备所有采集里残差最小的一次。**闸门通过不等于
    点云是球**，这条方法学限制是数据反过来暴露的。
    """
    ddcd = [
        (res, ok)
        for _ts, dev, _far, _r, res, _mn, _oc, ok in _HARDWARE_2026_09_02
        if dev == "DDCD154C"
    ]
    passed = [res for res, ok in ddcd if ok]
    assert len(ddcd) == 7
    assert len(passed) == 1
    assert passed[0] == min(res for res, _ok in ddcd)
    assert passed[0] <= RESIDUAL_LIMIT
