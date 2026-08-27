"""测试用的帧构造工具。

协议无校验和，所以构造一帧就是拼字节，没有需要计算的字段。
"""

from __future__ import annotations

import struct

from wt901.protocol.frames import FRAME_LENGTH, HEADER, FrameFlag
from wt901.protocol.registers import REGISTERS_PER_RESPONSE

__all__ = ["data_frame", "register_frame", "registers"]


def data_frame(counts: tuple[int, ...]) -> bytes:
    """构造一个 0x61 实时数据帧。``counts`` 必须是 9 个 int16。"""
    if len(counts) != 9:
        raise ValueError("0x61 帧的负载固定是 9 个 int16")
    return bytes([HEADER, FrameFlag.DATA]) + struct.pack("<9h", *counts)


def register_frame(start_register: int, values: tuple[int, ...]) -> bytes:
    """构造一个 0x71 寄存器回读帧。``values`` 必须是 8 个 int16。

    个数从 :data:`~wt901.protocol.registers.REGISTERS_PER_RESPONSE` 取，不写死：
    这个常量曾经是 4（错的），而当时**构造帧的工具与被解码的代码用的是同一个错误
    常量**，所以离线测试无论怎么写都发现不了差异，只有真机能（RAY-292）。绑在一起
    不能让常量本身变对，但至少不会出现「工具说 8、解码器说 4」的假绿。

    8 个寄存器正好用满 18 字节负载，所以这里**不再有补零的帧尾**——补零曾经掩盖了
    「负载还剩 8 个字节没有说法」这件事。
    """
    if len(values) != REGISTERS_PER_RESPONSE:
        raise ValueError(f"0x71 帧固定携带 {REGISTERS_PER_RESPONSE} 个寄存器")
    body = (
        bytes([HEADER, FrameFlag.REGISTER])
        + struct.pack("<H", start_register)
        + struct.pack(f"<{REGISTERS_PER_RESPONSE}h", *values)
    )
    if len(body) != FRAME_LENGTH:
        raise AssertionError(f"帧长应为 {FRAME_LENGTH}，实际 {len(body)}")
    return body


def registers(*values: int) -> tuple[int, ...]:
    """把关心的那几个寄存器值补足成一整帧的 8 个，其余补 0。

    补的 0 是**一句陈述**——「这几个地址往后的寄存器读作 0」——不是「后面是什么
    无所谓」。:func:`register_frame` 坚持要满 8 个就是为了逼出这句陈述；需要非零
    内容的测试直接写全 8 个，不要用这个助手把它糊过去。
    """
    if len(values) > REGISTERS_PER_RESPONSE:
        raise ValueError(f"一帧最多 {REGISTERS_PER_RESPONSE} 个寄存器")
    return values + (0,) * (REGISTERS_PER_RESPONSE - len(values))
