"""带宽 42 Hz（`0x1F` = `0x03`）纳入具名配置。全部离线。

这一档存在的理由是**适配文档 §3.1 七步序列的第 ③ 步**：在它进枚举之前，那一步要么
被 `_coerce_bandwidth` 拒绝，要么只能用通用 `write(0x1F, 0x03)` 绕过——而绕过正是
RAY-241 / RAY-291 反复论证要避免的反模式，且会绕开「未登记编码一律拒绝」这道防线。

**它的证据强度与另两档相同，不是更弱。** RAY-298 的真机取证试图量出三档的截止频率，
自校验没过（标着 20 Hz 的 `0x04` 测出 10.9 Hz），所以三个标称值一个都没被坐实。取证
真正确立的是「设备接受三个编码」与「截止频率单调有序」，那也正是这里能测的东西——
本文件不测「42 Hz 是不是 42 Hz」，因为本库并不知道。
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import register_frame, registers
from wt901.device import WT901Device
from wt901.errors import UnsupportedRegisterError
from wt901.protocol.registers import (
    AlgorithmMode,
    Bandwidth,
    Mounting,
    Register,
    ReturnRate,
)
from wt901.transport.memory import MemoryTransport

UNLOCK = bytes.fromhex("ffaa6988b5")
SAVE = bytes.fromhex("ffaa000000")
BANDWIDTH_42 = bytes.fromhex("ffaa1f0300")
"""适配文档 §3.1 第 ③ 步逐字节的样子。"""


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.write_delay = 0.0
    device.registers.save_delay = 0.0
    device.registers.read_timeout = 0.05
    await device.open()
    return device, transport


# ----- 验收标准 3：登记 0x03 ------------------------------------------------


def test_the_rung_is_registered_at_0x03() -> None:
    assert Bandwidth.HZ_42 == 0x03


def test_all_three_rungs_are_distinct_codes() -> None:
    """三档必须是三个不同编码——IntEnum 会把重复值悄悄变成别名。"""
    assert len({int(b) for b in Bandwidth}) == 3


async def test_set_bandwidth_emits_the_adaptation_document_bytes() -> None:
    """第 ③ 步逐字节：`FF AA 1F 03 00`，外面裹着解锁与保存。"""
    device, transport = await _opened()
    assert await device.registers.set_bandwidth(Bandwidth.HZ_42) is Bandwidth.HZ_42
    assert transport.writes == [UNLOCK, BANDWIDTH_42, SAVE]
    await device.close()


async def test_raw_int_is_accepted_too() -> None:
    """下游可能从配置文件里读进来一个裸 3，不该被迫先转成枚举。"""
    device, transport = await _opened()
    assert await device.registers.set_bandwidth(0x03) is Bandwidth.HZ_42
    assert transport.writes == [UNLOCK, BANDWIDTH_42, SAVE]
    await device.close()


async def test_bandwidth_is_remembered_for_replay() -> None:
    """带宽是配置不是动作，重连后要被重放。

    这一条对带宽格外要紧：设错了**没有任何可观测量会异常**——速率、丢包、连接全部
    正常，只有数据的频谱不对。退回残留配置不会有人发现。
    """
    device, _ = await _opened()
    await device.registers.set_bandwidth(Bandwidth.HZ_42)

    applied = [e for e in device.registers.applied_writes if e.register == 0x1F]
    assert len(applied) == 1
    assert applied[0].value == Bandwidth.HZ_42
    await device.close()


# ----- 验收标准 1、3：防线不因证据强度而松动 --------------------------------


@pytest.mark.parametrize("code", [0x01, 0x02, 0x05, 0x06, 0xFF])
async def test_unregistered_codes_are_still_refused(code: int) -> None:
    """`0x01`/`0x02`/`0x05`/`0x06` 在通用编码表里各有含义，本库仍不放行。

    拒绝防的是**误**写，与「被放行的那三档证据有多强」无关——所以这道防线不随
    RAY-298 的结论松动。断言 `writes == []`：拒绝必须发生在发出任何字节之前。
    """
    device, transport = await _opened()
    with pytest.raises(UnsupportedRegisterError):
        await device.registers.set_bandwidth(code)
    assert transport.writes == []
    await device.close()


async def test_refusal_no_longer_claims_the_rungs_were_verified() -> None:
    """措辞守卫：拒绝信息不能再说「已核实」。

    速率那条说得没错（每档实测过），带宽这条曾经照抄了它。本库把「已核实」当成一个
    有分量的词用——`ReturnRate` 排除 `0x0A` 正是靠它——所以用错地方要被测试挡住。
    """
    device, _ = await _opened()
    with pytest.raises(UnsupportedRegisterError) as caught:
        await device.registers.set_bandwidth(0xFF)

    message = str(caught.value)
    assert "未核实" not in message
    assert "已核实" not in message
    assert "未实测" in message
    await device.close()


# ----- 验收标准：七步序列可被具名 API 完整表达 -------------------------------


async def test_the_seven_step_sequence_is_now_fully_named() -> None:
    """《BS-BT91 硬件适配与时间同步方案 v0.1》§3.1 的七步，全部走具名 API。

    **这是本 Issue 的全部目的。** 在此之前第 ③ 步会被拒绝，`test_mounting.py` 里那条
    同名测试只能拿 `HZ_20` 顶替，与适配文档对不上。这里逐字节钉住四条指令，其中
    第 ③ 步就是 `FF AA 1F 03 00`。

    ① 解锁与 ⑦ 保存由每次写事务自带；⑤ 是 `0x96`，走通用具名写入。
    """
    device, transport = await _opened()
    async with device.registers.settings() as settings:
        settings.output_rate = ReturnRate.HZ_200
        settings.bandwidth = Bandwidth.HZ_42
        settings.algorithm = AlgorithmMode.SIX_AXIS
        settings.mounting = Mounting.HORIZONTAL

    assert transport.writes == [
        UNLOCK,
        bytes.fromhex("ffaa030b00"),  # ② 200 Hz
        SAVE,
        UNLOCK,
        BANDWIDTH_42,  # ③ 抗混叠带宽
        SAVE,
        UNLOCK,
        bytes.fromhex("ffaa240100"),  # ④ 6 轴
        SAVE,
        UNLOCK,
        bytes.fromhex("ffaa230000"),  # ⑥ 水平安装
        SAVE,
    ]
    await device.close()


async def test_read_back_still_returns_the_raw_code() -> None:
    """设备上可能存着本库未登记的档位，读回不该抛异常。

    取证那台设备连上时 flash 里存的就是 `0x03`；换一台存着 `0x01` 的，`read_bandwidth`
    仍要能如实报出来，否则调用方连「设备现在是什么状态」都问不到。
    """
    device, transport = await _opened()
    task = asyncio.get_running_loop().create_task(device.registers.read_bandwidth())
    await asyncio.sleep(0)
    transport.feed(register_frame(Register.BANDWIDTH, registers(0x01)))
    assert await task == 0x01
    await device.close()
