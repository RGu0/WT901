"""测试用的帧构造工具。

协议无校验和，所以构造一帧就是拼字节，没有需要计算的字段。
"""

from __future__ import annotations

import struct

from wt901.protocol.frames import FRAME_LENGTH, HEADER, FrameFlag

__all__ = ["data_frame", "register_frame"]


def data_frame(counts: tuple[int, ...]) -> bytes:
    """构造一个 0x61 实时数据帧。``counts`` 必须是 9 个 int16。"""
    if len(counts) != 9:
        raise ValueError("0x61 帧的负载固定是 9 个 int16")
    return bytes([HEADER, FrameFlag.DATA]) + struct.pack("<9h", *counts)


def register_frame(start_register: int, values: tuple[int, ...]) -> bytes:
    """构造一个 0x71 寄存器回读帧。

    协议固定回 4 个寄存器；帧尾剩余字节在真机上内容不定，这里补 0，正好也用来
    验证解码器不会去读它们。
    """
    if len(values) != 4:
        raise ValueError("0x71 帧固定携带 4 个寄存器")
    body = (
        bytes([HEADER, FrameFlag.REGISTER])
        + struct.pack("<H", start_register)
        + struct.pack("<4h", *values)
    )
    return body.ljust(FRAME_LENGTH, b"\x00")
