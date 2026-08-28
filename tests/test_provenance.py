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

from wt901.protocol import registers
from wt901.protocol.registers import Register

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
    """这份差集是本次比对的结论本身，写死在这里好过散落在文字里。

    若将来补齐 Unity_C# / Windows_C# 的 WitSdk 主体后差集变了，这个测试会失败——
    那正是应该回头改文档的时刻。
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
