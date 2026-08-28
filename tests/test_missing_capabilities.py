"""官方有指令、本库此前没有入口的四条能力（RAY-308 scope 3）。全部离线。

**这个文件测的是「发出去的字节对不对」和「拒绝该拒绝的」，不是「设备收到之后做了
什么」。** 后者本库一条都没有实测数据——四条能力全部只有协议文档背书。所以每个新
入口的 docstring 都写了「未在真机上验证过」，本文件也钉住那句话必须在。

这不是走过场：`0x0A` 的教训是本库自己吃过的——按上游表把它命名为 `HZ_125`，真机
实测却是 99.29 Hz。字节构造正确与设备行为符合预期是两件事。

判据来自协议文档原文（`ray-308` scope 2 已把全文取回逐字核对）：

    FF AA 00 SAVE 00    SAVE：0=保存当前配置  1=恢复默认配置并保存  FF=重启设备
    FF AA 01 04 00      设置 z 轴角度归零（需先切换六轴算法，才可以生效）
    FF AA 01 08 00      设置角度参考（发送后，需要发送保存指令）
    WTWT12345678\\r\\n     设置蓝牙名称（改名后需重启硬件或下发重启命令）
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import register_frame, registers
from test_provenance import _attribute_docs
from wt901.device import WT901Device
from wt901.errors import ConfigurationError
from wt901.protocol import commands
from wt901.protocol.registers import (
    BLUETOOTH_NAME_PREFIX,
    MAX_BLUETOOTH_NAME_SUFFIX_BYTES,
    AlgorithmMode,
    CalibrationMode,
    Mounting,
    Register,
    SaveAction,
)
from wt901.transport.memory import MemoryTransport

UNLOCK = bytes.fromhex("ffaa6988b5")
SAVE = bytes.fromhex("ffaa000000")


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    device.registers.save_delay = 0.0
    await device.open()
    return device, transport


async def _answer(
    transport: MemoryTransport, start: int, values: tuple[int, ...]
) -> None:
    for _ in range(200):
        await asyncio.sleep(0)
        if transport.writes:
            break
    transport.feed(register_frame(start, values))


# ===== 验收标准 1：Mounting.VERTICAL 的物理约束 =============================


def test_vertical_mounting_documents_the_y_axis_constraint() -> None:
    """「必须坐标轴的 Y 轴箭头朝上」——本库此前只抄了「垂直安装」四个字。

    **本库无法替调用方检查这一条**：设备只知道自己被告知装成了垂直，不知道箭头朝
    哪。文档是唯一的防线，所以它必须在，且必须说清装错不会报错。

    这里读源码而不是 `Mounting.VERTICAL.__doc__`——IntEnum 成员没有自己的 `__doc__`
    （见 `test_provenance._attribute_docs` 那段说明）。
    """
    doc = _attribute_docs(Mounting)["VERTICAL"]
    assert "Y 轴箭头朝上" in doc
    assert "不会报错" in doc
    assert Mounting.VERTICAL == 1


# ===== 验收标准 2：save() 的三个官方取值 ====================================


def test_save_action_matches_the_official_encoding() -> None:
    assert SaveAction.SAVE_CURRENT == 0x0000
    assert SaveAction.RESTORE_DEFAULTS == 0x0001
    assert SaveAction.REBOOT == 0x00FF


def test_save_default_is_unchanged() -> None:
    """默认必须仍是「保存当前配置」。

    这不是向后兼容的例行公事：另外两个取值一个抹掉全部配置、一个断链重启。默认值
    要落在最无害的那一档上，「不小心少传一个参数」才不会变成一次出厂复位。
    """
    assert commands.save() == SAVE
    assert commands.save(SaveAction.SAVE_CURRENT) == SAVE


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (commands.restore_defaults, "ffaa000100"),
        (commands.reboot, "ffaa00ff00"),
    ],
)
def test_save_variants_emit_the_documented_bytes(
    build: object, expected: str
) -> None:
    assert build() == bytes.fromhex(expected)  # type: ignore[operator]


async def test_restore_defaults_does_not_append_a_save() -> None:
    """`0x00` 本身就是保存指令，后面再补一条会把刚复位的配置又存回去。

    这条断言是这个入口不走 `RegisterAccess.write()` 的**全部理由**——不是「顺手复用
    一下更省事」，是复用会做错事。
    """
    device, transport = await _opened()
    await device.registers.restore_defaults()
    assert transport.writes == [UNLOCK, bytes.fromhex("ffaa000100")]
    await device.close()


async def test_reboot_does_not_append_a_save() -> None:
    """重启之后那条保存会打在一台正在重启的设备上。"""
    device, transport = await _opened()
    await device.registers.reboot()
    assert transport.writes == [UNLOCK, bytes.fromhex("ffaa00ff00")]
    await device.close()


async def test_restore_defaults_clears_the_replay_record() -> None:
    """不清空的话，下一次自动重连会把刚被抹掉的配置重新放回去。

    调用方看到的是「复位了，过一会儿又变回来了」，而且没有任何报错——这正是本库
    对「静默失效」一贯要挡的那类事。
    """
    device, _transport = await _opened()
    await device.registers.set_mounting(Mounting.HORIZONTAL)
    assert device.registers.applied_writes != ()

    await device.registers.restore_defaults()
    assert device.registers.applied_writes == ()
    await device.close()


async def test_reboot_keeps_the_replay_record() -> None:
    """重启不改变 flash 里的配置，重连后重放仍然是对的。

    与 `restore_defaults` 的区别正是「设备还是不是我配置的样子」——两个动作在这一点
    上必须分开，写反了会在重连后悄悄改变设备状态。
    """
    device, _ = await _opened()
    await device.registers.set_mounting(Mounting.HORIZONTAL)
    before = device.registers.applied_writes

    await device.registers.reboot()
    assert device.registers.applied_writes == before
    await device.close()


# ===== 验收标准 3：CalibrationMode 的 0x04 与 0x08 ==========================


def test_calibration_mode_covers_the_official_values() -> None:
    assert CalibrationMode.ZERO_Z_AXIS == 0x04
    assert CalibrationMode.ANGLE_REFERENCE == 0x08
    assert commands.zero_z_axis() == bytes.fromhex("ffaa010400")
    assert commands.set_angle_reference() == bytes.fromhex("ffaa010800")


async def test_zero_z_axis_refuses_when_the_device_is_in_nine_axis_mode() -> None:
    """9 轴下这条指令**不报错也不生效**，所以本库先读 `0x24` 挡住它。

    「不报错也不生效」是本库最不能容忍的一类：调用方以为归零了，数据却没变。多花
    一次 BLE 往返换掉它是划算的。
    """
    device, transport = await _opened()
    task = asyncio.ensure_future(device.calibration.zero_z_axis())
    await _answer(transport, Register.ALGORITHM, registers(AlgorithmMode.NINE_AXIS))

    with pytest.raises(ConfigurationError) as caught:
        await task
    message = str(caught.value)
    assert "6 轴" in message
    # 拒绝必须发生在发出校准指令之前：只有那次读，没有写。
    assert bytes.fromhex("ffaa010400") not in transport.writes
    await device.close()


async def test_zero_z_axis_sends_the_command_in_six_axis_mode() -> None:
    device, transport = await _opened()
    task = asyncio.ensure_future(device.calibration.zero_z_axis())
    await _answer(transport, Register.ALGORITHM, registers(AlgorithmMode.SIX_AXIS))
    await task

    assert bytes.fromhex("ffaa010400") in transport.writes
    await device.close()


async def test_zero_z_axis_is_an_action_not_a_configuration() -> None:
    """重放一次归零 = 在重连那一刻的朝向上重定零点。与重放加计校准是同一种错。"""
    device, transport = await _opened()
    task = asyncio.ensure_future(device.calibration.zero_z_axis())
    await _answer(transport, Register.ALGORITHM, registers(AlgorithmMode.SIX_AXIS))
    await task

    assert all(
        entry.register != Register.CALSW
        for entry in device.registers.applied_writes
    )
    await device.close()


async def test_set_angle_reference_emits_the_documented_bytes() -> None:
    """官方注明发送后需再发保存指令——本库的写事务本来就以保存收尾。"""
    device, transport = await _opened()
    await device.calibration.set_angle_reference()

    assert transport.writes == [UNLOCK, bytes.fromhex("ffaa010800"), SAVE]
    assert all(
        entry.register != Register.CALSW
        for entry in device.registers.applied_writes
    )
    await device.close()


# ===== 验收标准 4：设置蓝牙名称 =============================================


def test_bluetooth_name_matches_the_official_example() -> None:
    """官方例子：名称 `WT12345678` → 下发 `WTWT12345678\\r\\n`。

    **两个 `WT` 都要有**，第一个是协议头、第二个是名称本身的前缀。这一点极易看成
    写重复了然后被「修掉」，所以拿官方原例钉死。
    """
    assert commands.set_bluetooth_name("WT12345678") == b"WTWT12345678\r\n"


def test_bluetooth_name_length_limit_fills_the_ble_mtu_exactly() -> None:
    """2（协议头）+ 2（`WT`）+ 14（可改部分）+ 2（`\\r\\n`）= 20 = BLE 单次上限。

    协议文档给的 14 与帧长算出来的 14 对得上，这是个能被独立佐证的数，不是抄来的。
    """
    longest = BLUETOOTH_NAME_PREFIX + "A" * MAX_BLUETOOTH_NAME_SUFFIX_BYTES
    assert len(commands.set_bluetooth_name(longest)) == 20

    with pytest.raises(ConfigurationError, match="最多"):
        commands.set_bluetooth_name(longest + "A")


def test_bluetooth_name_must_keep_the_official_prefix() -> None:
    """官方：那两个字符「不可修改，否则会导致 APP 搜索不到」。

    改名是**不可逆**的（改完只能靠新名字或 MAC 找回设备），所以宁可挡在发出之前。
    """
    with pytest.raises(ConfigurationError, match="WT"):
        commands.set_bluetooth_name("LeftFoot")


def test_bluetooth_name_refuses_non_ascii() -> None:
    """多字节字符会让「几个字」与「几个字节」对不上，超出的部分被链路截断。

    那种失败在结果上是「名字改了但不是我要的名字」，比直接拒绝难查得多。
    """
    with pytest.raises(ConfigurationError, match="ASCII"):
        commands.set_bluetooth_name("WT左脚")


async def test_set_bluetooth_name_writes_the_raw_command() -> None:
    """**这不是寄存器写入**：没有解锁、没有保存，封装完全不同。"""
    device, transport = await _opened()
    await device.set_bluetooth_name("WTLeftFoot")

    assert transport.writes == [b"WTWTLeftFoot\r\n"]
    await device.close()


def test_bluetooth_name_docs_refuse_to_be_read_as_an_identity_solution() -> None:
    """改名**不是设备自报的身份**，不能替代 `read_mac()`。

    同批设备广播名重复正是 RAY-279 立项的理由之一，改名看起来能解决它——`ray-308`
    的 Issue 明确要求 docstring 写清这一点，否则容易被误用。
    """
    doc = WT901Device.set_bluetooth_name.__doc__ or ""
    assert "不是设备自报的身份" in doc
    assert "read_mac" in doc


# ===== 四条能力全部未经真机验证，措辞必须说明 ===============================


def test_every_new_capability_admits_it_is_unverified_on_hardware() -> None:
    """字节构造正确与设备行为符合预期是两件事。

    本库自己吃过这个亏：按上游表把 `0x0A` 命名为 `HZ_125`，真机实测却是 99.29 Hz。
    这四条能力全部只有协议文档背书，一条都没上过真机——不写明，下一个人会以为
    「有测试」就等于「验证过了」，而本文件的测试**只覆盖字节构造与拒绝路径**。
    """
    from wt901.config import RegisterAccess

    unverified = "未在真机上验证"
    for doc in (
        commands.restore_defaults.__doc__,
        commands.reboot.__doc__,
        commands.zero_z_axis.__doc__,
        commands.set_angle_reference.__doc__,
        commands.set_bluetooth_name.__doc__,
    ):
        assert unverified in (doc or "")

    for doc in (
        RegisterAccess.restore_defaults.__doc__,
        RegisterAccess.reboot.__doc__,
        WT901Device.set_bluetooth_name.__doc__,
    ):
        assert "没有在真机上验证过" in (doc or "")

    from wt901.calibration import Calibration

    assert "没有在真机上验证过" in (Calibration.zero_z_axis.__doc__ or "")
    assert "没有在真机上验证过" in (Calibration.set_angle_reference.__doc__ or "")
