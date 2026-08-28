"""守卫「出处」这层措辞（RAY-309）。

本库最核心的规矩是**绝不把推断写成事实**，而 `docs/protocol.md` 与 `registers.py`
的开头曾经违反它整整一句话：「全部条目由维特官方 SDK 源码逐条核实」——那句话对六组
寄存器不成立，SDK 一次都没碰过它们，其中 ``0x7F`` 更是协议文档与 SDK 两边都查不到。

这类回退不会被别的测试抓住：它不改行为，只改读者对证据强度的判断，而**读者的判断
正是这份文档存在的理由**。所以照 ``test_bandwidth_full_range`` 的先例钉住否定式的
说法，不钉具体句子——钉死句子会让每次改文档都要改测试，那样的守卫最后总会被顺手
改掉。
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap

from wt901.protocol import registers, units
from wt901.protocol.registers import Bandwidth, Register, ReturnRate

_PROTOCOL_DOC = (
    pathlib.Path(__file__).resolve().parent.parent / "docs" / "protocol.md"
)


def _attribute_docs(cls: type) -> dict[str, str]:
    """取「成员赋值下面那段字符串字面量」。

    ``IntEnum`` 的成员**没有自己的 ``__doc__``**——``Register.MOUNTING.__doc__`` 拿到
    的是类的 docstring。那些字面量只有 Sphinx 读得到，所以这里照 Sphinx 的规则从源码
    里解析：一个 ``Name = ...`` 赋值后面紧跟的裸字符串就是它的文档。连续赋值共用后面
    那一段（芯片时间四个寄存器就是这么写的），照此归给这一组的每个名字。
    """
    source = inspect.getsource(cls)
    tree = ast.parse(textwrap.dedent(source))
    body = tree.body[0].body  # type: ignore[attr-defined]

    docs: dict[str, str] = {}
    pending: list[str] = []
    for node in body:
        if isinstance(node, ast.Assign):
            pending.extend(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for name in pending:
                docs[name] = node.value.value
            pending.clear()
        else:
            pending.clear()
    return docs


_REGISTER_DOCS = _attribute_docs(Register)

# SDK 全量扫描（钉住 commit 9efaab0f…，2026-08-28）触及的地址。本库是它的超集，
# 差集里的每一条都不能声称「由 SDK 核实」。
_SDK_TOUCHED = frozenset(
    {0x03, 0x2E, 0x3A, 0x40, 0x51, 0x64, 0x72, 0x00, 0x01, 0x1F, 0x27, 0x69}
)

# SDK 没碰过、但协议文档写了的六组的代表地址。
_DOC_ONLY = (
    Register.MOUNTING,
    Register.ALGORITHM,
    Register.CHIP_TIME_YEAR_MONTH,
    Register.MAC,
    Register.DISPLACEMENT_OUTPUT,
)


def test_sdk_touched_set_matches_the_registers_the_library_exposes() -> None:
    """把「本库是 SDK 的超集」这个结论写成可执行的断言。

    它挡的是**这个结论被悄悄推翻**：谁要是删掉或改掉一个 SDK 也用的地址（比如
    :attr:`Register.MAGTYPE`），`docs/protocol.md` §11 那句「本库是超集」就不再成立，
    而纯文字写下来的结论不会有任何东西提醒他。

    它**挡不住**的两件事，说清楚免得误以为有保护：``_SDK_TOUCHED`` 是照 2026-08-28
    那次扫描手抄的常量，补齐 Unity_C# / Windows_C# 的 WitSdk 主体后得连同文档一起
    手改；往 :class:`Register` 里加新地址也不会失败——新地址只是落进
    ``only_in_library`` 而已。
    """
    library = {int(member) for member in Register}
    assert _SDK_TOUCHED <= library, "SDK 触及的地址本库应当全部有，否则「超集」不成立"

    only_in_library = library - _SDK_TOUCHED
    assert Register.SERIAL_NUMBER in only_in_library
    for member in _DOC_ONLY:
        assert member in only_in_library


def test_module_docstring_no_longer_claims_a_single_source() -> None:
    doc = registers.__doc__ or ""
    # 旧说法只提 SDK 一个来源；现在两个来源都要出现，且要说清它们范围不同。
    assert "协议文档" in doc
    assert "官方 SDK" in doc
    assert "9efaab0fdd6a06dc807bf80402e58aa91b431c6f" in doc


def test_每个只有单边背书的寄存器都自报出处() -> None:
    """SDK 没碰过的六组、以及只有 SDK 背书的 ``0x72``，都必须在自己那条里写明。

    写在模块开头不够：读代码的人跳到某个成员上，看不到开头那段。
    """
    for member in _DOC_ONLY:
        doc = _REGISTER_DOCS[member.name]
        assert "出处" in doc, f"{member.name} 没写出处"
        assert "官方 SDK 没有" in doc, f"{member.name} 没写明 SDK 里没有它"

    magtype = _REGISTER_DOCS["MAGTYPE"]
    assert "协议文档没有" in magtype


def test_serial_number_docstring_says_both_upstream_sources_lack_it() -> None:
    """``0x7F`` 是本模块唯一两边都查不到的地址，这一点不能只留在 Issue 里。"""
    doc = _REGISTER_DOCS["SERIAL_NUMBER"]
    assert "协议文档没有" in doc
    assert "官方 SDK 也没有" in doc
    # 真机四台次全零这个现象要留着，但不能被当成「序列号没烧写」的定论。
    assert "全为零" in doc
    assert "分不开" in doc


def test_protocol_doc_dropped_the_blanket_verification_claim() -> None:
    text = _PROTOCOL_DOC.read_text(encoding="utf-8")
    # 旧句子只允许作为「被改正掉的说法」出现一次，和 Bandwidth 那条同一个约定。
    history = "全部条目由维特官方 SDK 源码逐条核实"
    assert text.count(history) == 1
    assert "那句话不成立" in text


def test_protocol_doc_records_the_boundary_of_the_sdk_comparison() -> None:
    """比对没覆盖 Unity_C# 与 Windows_C# 的 WitSdk 主体，这条边界不写下来，

    下一个人就会把「SDK 里没有」当成完整结论。
    """
    text = _PROTOCOL_DOC.read_text(encoding="utf-8")
    assert "未覆盖" in text
    assert "Unity_C#" in text
    assert "WitSdk" in text


def test_protocol_doc_keeps_the_zero_serial_observation_but_not_its_old_explanation() -> (
    None
):
    """现象是实测出来的，删了就丢真实数据；改的只是「它意味着什么」那一层。"""
    text = _PROTOCOL_DOC.read_text(encoding="utf-8")
    assert "序列号寄存器可能全为 0" in text
    assert "成因尚未确定" in text


# ===== RAY-308 scope 1：出处标错与官方矛盾 ===================================
#
# 与上面 RAY-309 那组守的是同一件事的另一半：RAY-309 修的是「把没核实的说成核实
# 了」，这里修的是反过来——**把有官方文档背书的说成只有通用表背书**，然后据此给
# 结论打了不该打的折扣。两个方向的错都会让读者对证据强度做出错误判断。


def test_bandwidth_provenance_was_raised_to_the_model_protocol_doc() -> None:
    """七个标称值印在本型号协议文档里，不是只见于维特跨型号的通用编码表。

    **上调的是出处，不是证据强度。** 「一档都没实测过」必须原样留着——出处更硬不
    等于数字被量过，这两件事分开算，混起来正是 RAY-298 当初要修的毛病。
    """
    doc = Bandwidth.__doc__ or ""
    assert "本型号官方协议文档" in doc
    assert "一档都没实测过" in doc
    # 协议文档那段原文（含默认值）要在文中，否则「出处上调」只是一句无从复核的断言。
    assert "0x00:256Hz" in doc
    assert "0x0004" in doc


def test_bandwidth_docstring_drops_the_broken_analogy() -> None:
    """「同一张通用表在速率上被证伪过，所以带宽也可疑」这个类比不成立。

    协议文档的速率表根本没有 ``0x0A``，被证伪的是通用表；而带宽七档出自协议文档。
    类比拆掉之后**残留风险不许跟着消失**——所以这里同时要求文中写明那三条新理由
    里最硬的一条：唯一一次尝试测量的结果（10.9 Hz）与文档标称（20 Hz）对不上。
    """
    doc = Bandwidth.__doc__ or ""
    assert "那个类比不成立" in doc
    assert "10.9" in doc


def test_bandwidth_register_records_the_factory_default_and_the_mismatch() -> None:
    """官方默认 ``0x0004``，而 ``ray-298`` 那台设备读回 ``0x03``。

    这条对照的价值不在默认值本身，在它推出来的那句话：**那台设备被改过，不是出厂
    态**。「读回什么就是出厂什么」这个假设在本器件上已经被证否一次，任何依赖出厂
    默认的推断都要先确认设备没被动过。
    """
    doc = _REGISTER_DOCS["BANDWIDTH"]
    assert "0x0004" in doc
    assert "不是出厂态" in doc


def test_return_rate_says_the_official_table_has_no_0x0a() -> None:
    """排除 ``0x0A`` 的理由从「实测对不上标称」变成两条，前一条更硬。

    ``0x0A`` 仍然不在枚举里——**结论没变，变的是它站在什么上面**。
    """
    doc = ReturnRate.__doc__ or ""
    assert "根本没有 ``0x0A`` 这个编码" in doc
    assert "通用编码表" in doc  # 得说清 125 Hz 那个数从哪来，否则读者无从判断
    assert 0x0A not in {int(rate) for rate in ReturnRate}


def test_magnetic_unit_docstring_records_all_three_conflicting_sources() -> None:
    """三份上游资料对磁场量纲的说法互相矛盾，此前只记了两份。

    第三份（协议文档说原始值就是 mG，与 ``0x72`` 无关）**出处比 SDK 更硬**，所以
    不能像此前那样用「Python 示例陈旧」一句带过。
    """
    doc = units.magnetic_field_to_ut.__doc__ or ""
    assert "mG" in doc            # 协议文档那一份
    assert "0.15" in doc          # C#/Android SDK 分档
    assert "120" in doc           # Python 示例
    # 不许在实现里选边站：必须写明这是暂定、要单独立项以实测定论。
    assert "暂定" in doc
    assert "实测定论" in doc


def test_protocol_doc_records_the_three_way_magnetic_conflict() -> None:
    text = _PROTOCOL_DOC.read_text(encoding="utf-8")
    assert "三份上游资料对磁场量纲的说法互相矛盾" in text
    assert "不做取舍" in text


# ===== RAY-308 scope 2：官方写了、本库没有的设备知识 =========================
#
# 这一组守的是**缺口**，不是措辞。缺口比错话更难发现：错话读起来别扭，缺口读起来
# 一切正常——`docs/protocol.md` 少一节坐标系说明，看上去和完整的一模一样。
#
# 所以这里断言的是「某几个关键词必须出现在文档里」。它挡不住写得敷衍，但挡得住
# 整段被删掉或重构时丢失。


def _doc() -> str:
    return _PROTOCOL_DOC.read_text(encoding="utf-8")


def test_records_the_coordinate_frame_and_euler_order() -> None:
    """东北天、X 左 / Y 前 / Z 上、Z-Y-X 旋转顺序。

    没有这几条，角度数据是绕哪个轴的都说不清——`Roll` 对应 X 轴这件事本库到处在用，
    却从来没写下依据。
    """
    text = _doc()
    assert "东北天" in text
    assert "Z-Y-X" in text
    assert "向左为 X 轴" in text


def test_records_that_pitch_folds_back_past_90_degrees() -> None:
    """±90° 折回与三轴耦合**不是设备故障**，这句话必须在文档里。

    不写，下游看到「只绕一个轴转、另一个轴的读数却在跳」会去查链路、查解码、查标定
    ——那三处都没有问题。
    """
    text = _doc()
    assert "固有特性" in text
    assert "不是设备故障" in text
    # 折回的具体后果（X 轴角度会大于 180°）要写明，否则读者不知道该拿什么现象对号入座。
    assert "大于 180" in text


def test_register_table_covers_the_official_range_not_just_what_is_exposed() -> None:
    """§5 要如实反映官方表的范围，并标注开放状态。

    只列本库开放的那些，会让下一个人以为官方表就这么大——而它大得多，缺的那部分正是
    下一次要不要补的候选。
    """
    text = _doc()
    # 官方表里本库没开放的条目，至少这几组要在文档里出现。
    for symbol in ("BAUD", "AXOFFSET", "HXOFFSET", "D0MODE"):
        assert symbol in text, f"官方表的 {symbol} 没被记下来"
    # 三种开放状态的记号要都在，否则「标注开放状态」这条落不了地。
    assert "**登记**" in text or "| 登记" in text
    assert "保留" in text


def test_records_that_the_official_register_table_is_itself_incomplete() -> None:
    """`0x1F`/`0x23`/`0x24`/`0x27`/`0x64`/`0x69` 不在官方地址表里，却在指令小节里。

    不写下来，下一个人会以为**本库多写了东西**，然后去删。
    """
    text = _doc()
    assert "官方寄存器地址表本身并不完整" in text
    assert "是那张表漏了，不是本库编的" in text


def test_records_the_vertical_mounting_hard_constraint() -> None:
    """垂直安装**必须 Y 轴箭头朝上**。装错不报错，数据一直偏。"""
    text = _doc()
    assert "Y 轴箭头朝上" in text


def test_bandwidth_row_no_longer_claims_only_three_rungs() -> None:
    """§5 的 `0x1F` 那一行曾停在三档，而 §6.2 已是七档（RAY-304 只改了后者）。

    同一份文档里两处互相矛盾，读者信哪个全看他先翻到哪一页。
    """
    text = _doc()
    assert "开放 `0x00`=256 Hz、`0x03`=42 Hz、`0x04`=20 Hz" not in text
    assert "七档全部登记" in text


def test_records_the_battery_ladder_conflict() -> None:
    """协议文档与 SDK 给了两张不同的电量阶梯表，同一原始值最多差 25 个百分点。

    这条**不在 scope 2 原定的五条里**，是通读协议文档时撞上的。既然发现了，就不能
    让本库那张表继续以「事实」的姿态单独立着——那正是这一系列 Issue 要修的毛病。
    """
    text = _doc()
    assert "官方协议文档给的是另一张表，两者对不上" in text
    # 本库没有改行为，文档必须说清这一点，免得读者以为已经换表了。
    assert "本库的行为一个字没改" in text
