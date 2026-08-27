"""一次 ``0x71`` 应答携带 8 个寄存器——用**真机原始字节**钉住。

这个文件的存在理由是：本库曾把这个数写成 4，而**离线测试永远发现不了**。构造帧的
测试工具与被解码的代码用的是同一个常量，两边一起错就一起「通过」。只有真机能揭穿，
所以这里的输入不是构造出来的，是 2026-08-27 从一台 WT901BLE67 上抓下来的原始负载
（``tools/probe_register_width.py``，证据 ``ray-292/register-response-width/acceptance/``）。

判读靠**语义可自证**的量，不靠「和上次一样」：芯片时间的年月日时分秒必须落在合理
范围，温度必须是个室温。十六进制看不出对错，这两样看得出。两者都落在原先被丢弃的
那 8 个字节里——改回 4 个，本文件立刻失败。
"""

from __future__ import annotations

import pytest

from wt901.errors import UnexpectedRegisterResponse
from wt901.protocol import units
from wt901.protocol.frames import (
    FRAME_LENGTH,
    HEADER,
    FrameDecoder,
    FrameFlag,
    decode_register_response,
)
from wt901.protocol.registers import REGISTERS_PER_RESPONSE, Register

VERSION_READ = bytes.fromhex("2E00" "1601" "D889" "0F01" "0100" "0905" "A703" "3601" "D1FE")
"""真机读 ``0x2E`` 的完整负载（第 1 轮）。

寄存器依次是 ``0x2E``–``0x35``：版本号低/高、芯片时间四个、以及两个本库未登记的。
"""

MAG_READ = bytes.fromhex("3A00" "BF04" "A500" "19FA" "31FA" "CBF9" "7800" "BC0B" "0000")
"""真机读 ``0x3A`` 的完整负载（第 1 轮）。寄存器依次是 ``0x3A``–``0x41``，其中
``0x40`` 是温度。"""


def _decode(payload: bytes):  # type: ignore[no-untyped-def]
    """走完整解码路径：拼帧 → FrameDecoder → decode_register_response。

    不直接构造 :class:`~wt901.protocol.frames.RegisterResponse`：要验的正是「帧里
    这 18 个字节被怎么切」，绕过解码器就把被测的东西绕过去了。
    """
    frame_bytes = bytes([HEADER, FrameFlag.REGISTER]) + payload
    assert len(frame_bytes) == FRAME_LENGTH
    frames = FrameDecoder().feed(frame_bytes)
    assert len(frames) == 1
    return decode_register_response(frames[0])


def test_the_constant_is_eight() -> None:
    """负载 18 字节 = 2 字节起始地址 + 8 个寄存器，一个字节不剩。

    这个算术在改成 4 的年代就成立了，只是没人算。留在这里让下一个人不必再算。
    """
    assert REGISTERS_PER_RESPONSE == 8
    assert 2 + REGISTERS_PER_RESPONSE * 2 == FRAME_LENGTH - 2


def test_real_response_carries_eight_registers() -> None:
    response = _decode(VERSION_READ)
    assert response.start_register == Register.VERSION_LOW
    assert len(response.values) == REGISTERS_PER_RESPONSE


def test_known_plaintext_version_still_decodes() -> None:
    """前两个寄存器不在争议区间内，用来确认帧没被切歪。

    ``0x0116 0x89D8`` 是 ``ray-172`` 记录过的本型号版本寄存器值——若连它都对不上，
    后面两条判据就无从谈起。
    """
    response = _decode(VERSION_READ)
    assert response.value_at(Register.VERSION_LOW) & 0xFFFF == 0x0116
    assert response.value_at(Register.VERSION_HIGH) & 0xFFFF == 0x89D8


def test_chip_time_lives_in_the_bytes_that_used_to_be_discarded() -> None:
    """读 ``0x2E`` 的应答里，第 5、6 个位置是芯片时间的分/秒与毫秒。

    ``0x32``/``0x33`` 落在按 4 个解时被丢掉的那一半里。取证当时的判据是「它夹在前后
    两次直读 ``0x30`` 的结果之间」，这里退一步只验语义合理性——单帧回放没有前后文，
    但一段填充解出来几乎必然会有某个字段越界。
    """
    response = _decode(VERSION_READ)

    year_month = response.value_at(Register.CHIP_TIME_YEAR_MONTH) & 0xFFFF
    day_hour = response.value_at(Register.CHIP_TIME_DAY_HOUR) & 0xFFFF
    minute_second = response.value_at(Register.CHIP_TIME_MINUTE_SECOND) & 0xFFFF
    millisecond = response.value_at(Register.CHIP_TIME_MILLISECOND) & 0xFFFF

    assert 2000 + (year_month & 0xFF) == 2015
    assert (year_month >> 8) & 0xFF == 1
    assert day_hour & 0xFF == 1
    assert (day_hour >> 8) & 0xFF == 0
    assert minute_second & 0xFF == 9
    assert (minute_second >> 8) & 0xFF == 5
    assert millisecond == 935


def test_temperature_lives_in_the_bytes_that_used_to_be_discarded() -> None:
    """读 ``0x3A`` 的应答里，第 7 个位置是 ``0x40`` 温度。

    取证当次直读 ``0x40`` 得到 30.03 °C，与这里的 30.04 °C 相差 0.01——两条独立读取
    的同一个量。室温合理性一眼可判，这正是选温度当旁证的原因。
    """
    response = _decode(MAG_READ)
    assert response.start_register == Register.HX

    raw = response.value_at(Register.TEMPERATURE)
    assert raw == 0x0BBC
    assert units.temperature_to_celsius(raw) == pytest.approx(30.04, abs=0.005)


def test_coverage_now_reaches_the_eighth_register() -> None:
    """``value_at`` 的覆盖范围随之变宽，越界仍然抛异常而不是返回邻居。"""
    response = _decode(MAG_READ)
    assert response.value_at(Register.HX + 7) == 0  # 0x41
    with pytest.raises(UnexpectedRegisterResponse):
        response.value_at(Register.HX + 8)
    with pytest.raises(UnexpectedRegisterResponse):
        response.value_at(Register.HX - 1)
