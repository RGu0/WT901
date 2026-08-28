"""下行指令字节构造。

所有指令都是定长 5 字节。写寄存器必须先解锁、后保存，那个时序由 device 层
封装成原子操作（见 RAY-171）；本模块只负责把单条指令变成正确的字节。
"""

from __future__ import annotations

import struct

from wt901.errors import ConfigurationError
from wt901.protocol.registers import (
    UNLOCK_KEY,
    Bandwidth,
    CalibrationMode,
    Register,
    ReturnRate,
)

__all__ = [
    "COMMAND_LENGTH",
    "COMMAND_PREFIX",
    "calibrate_acceleration",
    "end_field_calibration",
    "read_register",
    "save",
    "set_bandwidth",
    "set_return_rate",
    "start_field_calibration",
    "unlock",
    "write_register",
]

COMMAND_PREFIX = b"\xff\xaa"
COMMAND_LENGTH = 5

_VALUE = struct.Struct("<H")


def write_register(register: int, value: int) -> bytes:
    """``FF AA <reg> <valL> <valH>``。"""
    if not 0x00 <= register <= 0xFF:
        raise ConfigurationError(f"寄存器地址越界：0x{register:X}")
    if not -0x8000 <= value <= 0xFFFF:
        raise ConfigurationError(f"寄存器取值放不进 16 位：{value}")
    return COMMAND_PREFIX + bytes([register]) + _VALUE.pack(value & 0xFFFF)


def read_register(register: int) -> bytes:
    """``FF AA 27 <reg> 00``。

    设备会回一帧 ``0x55 0x71``，携带该地址起连续 8 个寄存器的值。
    """
    if not 0x00 <= register <= 0xFF:
        raise ConfigurationError(f"寄存器地址越界：0x{register:X}")
    return COMMAND_PREFIX + bytes([Register.READADDR, register, 0x00])


def unlock() -> bytes:
    """``FF AA 69 88 B5``。写任何配置寄存器之前都必须先发它。"""
    return write_register(Register.KEY, UNLOCK_KEY)


def save() -> bytes:
    """``FF AA 00 00 00``。把配置固化，掉电后仍生效。"""
    return write_register(Register.SAVE, 0x0000)


def set_return_rate(rate: ReturnRate) -> bytes:
    """设置回传速率。取值被限定在已核实的档位，见 :class:`ReturnRate`。"""
    return write_register(Register.RRATE, rate)


def set_bandwidth(bandwidth: Bandwidth) -> bytes:
    """设置抗混叠带宽。取值被限定在上游表列出的档位，见 :class:`Bandwidth`。

    「已登记」不等于「已核实」：这些标称频率本库一档都没实测过，登记它们是转述上游
    表，不是背书那个赫兹数。见 :class:`Bandwidth`。
    """
    return write_register(Register.BANDWIDTH, bandwidth)


def calibrate_acceleration() -> bytes:
    """``FF AA 01 01 00``。要求设备水平静置。"""
    return write_register(Register.CALSW, CalibrationMode.ACCELERATION)


def start_field_calibration() -> bytes:
    """``FF AA 01 07 00``。开始后需绕 XYZ 三轴各转一圈。"""
    return write_register(Register.CALSW, CalibrationMode.MAGNETIC_FIELD)


def end_field_calibration() -> bytes:
    """``FF AA 01 00 00``。不发它设备会停留在校准态。"""
    return write_register(Register.CALSW, CalibrationMode.NORMAL)
