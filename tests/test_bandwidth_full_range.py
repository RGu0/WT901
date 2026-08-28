"""带宽七档全部登记（`0x1F` = `0x00`–`0x06`）。全部离线。

**这个 scope 交付的是接口完整性，不是实测。** 在此之前 `Bandwidth` 只登记三档，
上层要 98 Hz 只能用通用 `write(0x1F, 0x02)` 绕过——而绕过正是 RAY-241 / RAY-291
反复论证要避免的反模式，也正是 RAY-298 当初开 Issue 的理由；同一个缺口只是从
`0x03` 挪到了另外四个编码上。

**本文件一行都不测「98 Hz 是不是 98 Hz」**，因为本库并不知道，将来也可能不知道。
七个标称值载于本型号官方协议文档（RAY-308 更正了出处，此前记的是维特通用编码表），
但一档都没坐实。这里钉住的是三件与赫兹数无关的事：放行哪七个编码、每个编码构造出
什么字节、以及措辞不许暗示这些数字被核实过。
"""

from __future__ import annotations

import pathlib
import re

import pytest

import wt901
from wt901.device import WT901Device
from wt901.errors import UnsupportedRegisterError
from wt901.protocol.registers import Bandwidth
from wt901.transport.memory import MemoryTransport

UNLOCK = bytes.fromhex("ffaa6988b5")
SAVE = bytes.fromhex("ffaa000000")

UPSTREAM_TABLE = {
    0x00: 256,
    0x01: 188,
    0x02: 98,
    0x03: 42,
    0x04: 20,
    0x05: 10,
    0x06: 5,
}
"""本型号官方协议文档逐档抄下来的样子——**本库未实测，不得当作事实引用**。

出处在 RAY-308 更正过：这七个数印在协议文档的 `0x1F` 条目里（默认值 `0x0004`），
不是像此前记的那样只见于维特跨型号的通用编码表。**数字一个没变**，变的是它们有多
可信；「本库未实测」不受影响。

写成字面量而不是从 `Bandwidth` 反推：反推出来的表恒等于枚举，测不出任何东西。
"""


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    device.registers.save_delay = 0.0
    await device.open()
    return device, transport


# ----- 验收标准 1：登记 0x00-0x06 全七档 ------------------------------------


def test_every_code_in_the_upstream_table_is_registered() -> None:
    assert {int(b) for b in Bandwidth} == set(UPSTREAM_TABLE)


@pytest.mark.parametrize(("code", "nominal"), sorted(UPSTREAM_TABLE.items()))
def test_member_name_transcribes_the_upstream_label(code: int, nominal: int) -> None:
    """成员名里的数字必须与上游表对得上。

    名字在这里是**转述**（「上游表把这个编码标为 98 Hz」），不是断言（`ReturnRate`
    的 `HZ_100` 意思是「本库量到过 100 Hz」）。转述可以不实测，但抄错就是另一回事
    了——抄错会让一个本来可查证的说法变成查无实据。
    """
    assert Bandwidth(code).name == f"HZ_{nominal}"


def test_code_descends_as_nominal_frequency_ascends() -> None:
    """编码越大、标称带宽越窄，这个方向不能抄反。

    上游表就是这么排的，而且 RAY-298 的真机取证在 `0x00`/`0x03`/`0x04` 三档上确认
    了单调有序（`0x00` 最宽、`0x04` 最窄、`0x03` 居中）——这是本库对带宽**唯一**
    实测到的与数值有关的事实，另四档只是顺着表填进来的。
    """
    by_code = sorted(Bandwidth, key=int)
    nominals = [UPSTREAM_TABLE[int(b)] for b in by_code]
    assert nominals == sorted(nominals, reverse=True)


# ----- 验收标准 1、6：新档位可用，边界仍在 ----------------------------------


@pytest.mark.parametrize("code", [0x01, 0x02, 0x05, 0x06])
async def test_newly_registered_codes_now_reach_the_device(code: int) -> None:
    """本 scope 的全部目的：这四个编码不再需要通用 `write` 绕过。"""
    device, transport = await _opened()
    assert await device.registers.set_bandwidth(code) is Bandwidth(code)
    assert transport.writes == [UNLOCK, bytes([0xFF, 0xAA, 0x1F, code, 0x00]), SAVE]
    await device.close()


@pytest.mark.parametrize("code", [0x07, 0x08, 0x20, 0xFF, -1])
async def test_codes_outside_the_upstream_table_are_refused(code: int) -> None:
    """白名单的边界挪了位置，但没有消失。

    上游表列到 `0x06` 为止。`0x07` 是紧挨着边界的那一个，`0x20` 是 `0x02` 多打了
    一个零——防**误**写正是这道防线唯一的理由，它与证据强度无关，所以七档齐备之后
    照旧。断言 `writes == []`：拒绝必须发生在发出任何字节之前。
    """
    device, transport = await _opened()
    with pytest.raises(UnsupportedRegisterError):
        await device.registers.set_bandwidth(code)
    assert transport.writes == []
    await device.close()


# ----- 验收标准 2：措辞不许暗示这些数字被核实过 ------------------------------


def test_docstring_does_not_claim_any_rung_was_measured() -> None:
    """守卫 `Bandwidth` 的文档措辞。

    RAY-298 修正过一次言过其实的说法（「只登记已核实的两档」），并加测试挡住它
    退回去。扩到七档后风险更大而不是更小：档位多了，「登记」看起来更像「核实」。

    只查否定式的说法出现在文中，不查具体句子——钉死句子会让每次改文档都要改测试，
    那样的守卫最后总是被顺手改掉。

    RAY-308 把出处从「维特通用编码表」上调为「本型号官方协议文档」，所以这里改查
    后者。**上调的是出处，不是证据强度**：「一档都没实测过」照旧要在文中出现，那
    才是这个测试真正守的东西。
    """
    doc = Bandwidth.__doc__ or ""
    assert "一档都没实测过" in doc
    assert "本型号官方协议文档" in doc

    # 「已核实」这个词在本库有分量（ReturnRate 排除 0x0A 就靠它），不许用在这里。
    # 唯一的例外是引用那句被 RAY-298 改正掉的旧措辞——它是改正记录的一部分，删了
    # 就看不出这段强调是冲着什么来的。除它以外再出现一次就是回退。
    history = "「只登记已核实的两档」"
    assert doc.count(history) == 1
    assert "已核实" not in doc.replace(history, "")


def test_docstring_records_that_the_upstream_table_merges_two_columns() -> None:
    """MPU 类芯片对同一编码给加速度/角速度两个略不同的截止频率，上游表只给一个数。

    这件事对将来的实测判据有直接影响：即使某一档被证实，也只能对上其中一列。
    文档里没有这句话，后来的人就会拿单一数字去比对双列的手册。
    """
    doc = Bandwidth.__doc__ or ""
    assert "加速度" in doc and "角速度" in doc


async def test_refusal_message_lists_the_rungs_without_endorsing_them() -> None:
    """拒绝信息要列出可用档位（否则调用方无从修正），但不得暗示它们被实测过。"""
    device, _ = await _opened()
    with pytest.raises(UnsupportedRegisterError) as caught:
        await device.registers.set_bandwidth(0x07)

    message = str(caught.value)
    for member in Bandwidth:
        assert member.name in message
    assert "未实测" in message
    assert "已核实" not in message
    # 触发拒绝的那个编码要出现在信息里，否则日志里看不出是什么值被挡了。
    assert re.search(r"0x07", message, re.IGNORECASE)
    await device.close()


def test_no_source_file_still_says_the_nominal_values_are_three() -> None:
    """全包扫一遍：不许再有地方把「标称值」的数量说成三档。

    这条是补漏加的。RAY-304 改 `Bandwidth` 时漏了两处转述它的文档——
    `protocol/registers.py` 的模块开头与 `protocol/commands.py` 的 `set_bandwidth`——
    两处都还写着「三档的标称频率」。类型检查和单元测试都不看文档，只有人看得见，
    而人恰恰是这些句子唯一的读者。

    **不禁止「三档」本身**：RAY-298 的取证确实只覆盖 `0x00`/`0x03`/`0x04` 三档，
    描述那次证据时说三档是对的。禁的是把整个枚举的标称值说成三档这一类断言，所以
    只钉住那几个完整短语，扫的是整个包而不是某个文件——下次新增的文件也在网里。

    「只登记已核实的两档」也不在禁列：`Bandwidth` 的文档要引用它才说得清那段强调
    是冲着什么来的。它归 `test_docstring_does_not_claim_any_rung_was_measured` 管，
    那条钉住它只以引文形式出现一次。
    """
    package = pathlib.Path(wt901.__file__).parent
    forbidden = ("三档的标称频率", "三档的标称值", "三个标称值")

    offences = [
        f"{path.relative_to(package)}: {phrase}"
        for path in sorted(package.rglob("*.py"))
        for phrase in forbidden
        if phrase in path.read_text(encoding="utf-8")
    ]
    assert offences == []
