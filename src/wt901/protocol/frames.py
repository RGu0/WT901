"""帧同步与解包。

协议是定长 20 字节的 ``0x55 | Flag | 18 字节负载``，**没有校验和**。同步只能
靠结构约束：帧头必须是 ``0x55``，标志位必须是已知的两个之一，长度必须是 20。
随机字节仍有约 1/2048 的概率伪装成合法帧头，所以解码器把重同步次数与丢弃字
节数暴露出来，让上层能观测链路质量，而不是静默吞掉。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from wt901.errors import UnexpectedRegisterResponse
from wt901.protocol.registers import REGISTERS_PER_RESPONSE

__all__ = [
    "FRAME_LENGTH",
    "HEADER",
    "PAYLOAD_LENGTH",
    "Frame",
    "FrameDecoder",
    "FrameFlag",
    "RegisterResponse",
    "decode_data_frame",
    "decode_register_response",
]

HEADER = 0x55
FRAME_LENGTH = 20
PAYLOAD_LENGTH = FRAME_LENGTH - 2


class FrameFlag(IntEnum):
    """帧标志位。"""

    DATA = 0x61
    """实时数据帧。默认持续上报。

    注意：寄存器 ``0x96`` 置 1 后，位移帧复用同一个标志位且字节布局无法区分，
    解析方必须自带配置上下文。
    """

    REGISTER = 0x71
    """寄存器回读帧，由主动读请求触发。"""


_VALID_FLAGS = frozenset(int(flag) for flag in FrameFlag)

_DATA_PAYLOAD = struct.Struct("<9h")
_REGISTER_HEADER = struct.Struct("<H")
_REGISTER_VALUES = struct.Struct(f"<{REGISTERS_PER_RESPONSE}h")


@dataclass(frozen=True, slots=True)
class Frame:
    """一个完整的 20 字节帧，已剥掉帧头与标志位。"""

    flag: FrameFlag
    payload: bytes


@dataclass(frozen=True, slots=True)
class RegisterResponse:
    """``0x71`` 帧的解包结果。

    一次读请求固定返回起始地址起的 4 个连续寄存器——这是协议决定的，不是可选
    的。所以读 ``0x3A`` 一次拿到 HX/HY/HZ，读 ``0x51`` 一次拿到 Q0–Q3。
    """

    start_register: int
    values: tuple[int, ...]

    def value_at(self, register: int) -> int:
        """取指定地址的寄存器值。

        地址不在本次回帧覆盖范围内时抛 :class:`UnexpectedRegisterResponse`，
        而不是返回一个相邻寄存器的值。
        """
        offset = register - self.start_register
        if not 0 <= offset < len(self.values):
            raise UnexpectedRegisterResponse(
                f"寄存器 0x{register:02X} 不在回帧覆盖范围 "
                f"0x{self.start_register:02X}–"
                f"0x{self.start_register + len(self.values) - 1:02X} 内"
            )
        return self.values[offset]


class FrameDecoder:
    """把任意切分的字节流还原成帧。

    BLE 通知的边界与帧边界没有任何关系：50 Hz 以上是打包传输，一次通知可能带
    多帧；低速率下一帧也可能被拆开。所以解码器必须跨调用维持缓冲。
    """

    __slots__ = ("_buffer", "dropped_bytes", "resync_count")

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.resync_count = 0
        """发生重同步的**次数**。连续丢弃的一段只计 1 次。"""
        self.dropped_bytes = 0
        """因重同步而丢弃的字节**总数**。"""

    def feed(self, data: bytes) -> list[Frame]:
        """喂入一段字节，返回本次能够解出的全部帧。

        **已知局限**：帧头合法但内容被截断时，解码器会把后续字节凑满 20 字节
        当成一帧交出去，产生一个内容错位的帧，之后才重新同步。没有校验和就无
        法在帧内发现这件事。这里不做「用下一帧的帧头反查本帧边界」的前瞻校验：
        那会在帧间混入垃圾字节时反过来丢掉合法帧，而 BLE 链路层本身带 CRC 与
        重传，真正会发生的是**整包丢失**——那不破坏帧对齐。用一个罕见故障换一
        个常见故障不划算。上层应监控 :attr:`resync_count` 判断链路是否可信。
        """
        self._buffer.extend(data)
        frames: list[Frame] = []
        skipped = 0
        while self._buffer:
            if self._buffer[0] != HEADER:
                index = self._buffer.find(HEADER, 1)
                if index < 0:
                    skipped += len(self._buffer)
                    self._buffer.clear()
                    break
                skipped += index
                del self._buffer[:index]
                continue
            if len(self._buffer) < 2:
                # 帧头对了但标志位还没到，等下一段字节再判断。
                break
            if self._buffer[1] not in _VALID_FLAGS:
                del self._buffer[0]
                skipped += 1
                continue
            if len(self._buffer) < FRAME_LENGTH:
                break
            if skipped:
                self._record_resync(skipped)
                skipped = 0
            frames.append(
                Frame(
                    flag=FrameFlag(self._buffer[1]),
                    payload=bytes(self._buffer[2:FRAME_LENGTH]),
                )
            )
            del self._buffer[:FRAME_LENGTH]
        if skipped:
            self._record_resync(skipped)
        return frames

    def _record_resync(self, skipped: int) -> None:
        """记一次重同步。一段连续丢弃只算一次，无论丢了多少字节。"""
        self.dropped_bytes += skipped
        self.resync_count += 1

    def reset(self) -> None:
        """丢弃缓冲区，保留计数器。

        重连后调用：旧连接残留的半帧与新连接的字节拼接会产生一个合法长度但内容
        错位的帧，那比丢弃更糟。
        """
        self._buffer.clear()

    @property
    def pending_bytes(self) -> int:
        """缓冲区中尚未构成完整帧的字节数。"""
        return len(self._buffer)


def decode_data_frame(frame: Frame) -> tuple[int, ...]:
    """解 ``0x61`` 帧，返回 9 个 int16 原始计数值。

    顺序为 X/Y/Z 三组，具体语义取决于 ``0x96`` 配置：默认是加速度、角速度、
    角度；置 1 后是位移、位移速度、角度。本函数不做这个判断，只负责取数。
    """
    if frame.flag is not FrameFlag.DATA:
        raise UnexpectedRegisterResponse(
            f"期望数据帧 0x{FrameFlag.DATA:02X}，实际收到 0x{frame.flag:02X}"
        )
    return _DATA_PAYLOAD.unpack(frame.payload)


def decode_register_response(frame: Frame) -> RegisterResponse:
    """解 ``0x71`` 帧，返回起始寄存器地址与其后 4 个寄存器的值。"""
    if frame.flag is not FrameFlag.REGISTER:
        raise UnexpectedRegisterResponse(
            f"期望寄存器帧 0x{FrameFlag.REGISTER:02X}，实际收到 0x{frame.flag:02X}"
        )
    (start_register,) = _REGISTER_HEADER.unpack_from(frame.payload, 0)
    values = _REGISTER_VALUES.unpack_from(frame.payload, _REGISTER_HEADER.size)
    return RegisterResponse(start_register=start_register, values=values)
