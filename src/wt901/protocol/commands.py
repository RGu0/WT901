"""下行指令字节构造。

写寄存器类指令都是定长 5 字节。写寄存器必须先解锁、后保存，那个时序由 device 层
封装成原子操作（见 RAY-171）；本模块只负责把单条指令变成正确的字节。

**有一条例外**：:func:`set_bluetooth_name` 用的是完全不同的封装
（``WT`` + 名称 + ``\r\n``，变长），它不是寄存器写入。所以 :data:`COMMAND_LENGTH`
只适用于前一类——别拿它去校验全部下行字节（RAY-308）。
"""

from __future__ import annotations

import struct

from wt901.errors import ConfigurationError
from wt901.protocol.registers import (
    BLUETOOTH_NAME_PREFIX,
    MAX_BLUETOOTH_NAME_SUFFIX_BYTES,
    UNLOCK_KEY,
    Bandwidth,
    CalibrationMode,
    Register,
    ReturnRate,
    SaveAction,
)

__all__ = [
    "BLUETOOTH_NAME_TERMINATOR",
    "COMMAND_LENGTH",
    "COMMAND_PREFIX",
    "calibrate_acceleration",
    "end_field_calibration",
    "read_register",
    "reboot",
    "restore_defaults",
    "save",
    "set_angle_reference",
    "set_bandwidth",
    "set_bluetooth_name",
    "set_return_rate",
    "start_field_calibration",
    "unlock",
    "write_register",
    "zero_z_axis",
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


def save(action: SaveAction = SaveAction.SAVE_CURRENT) -> bytes:
    """``FF AA 00 SAVE 00``。取值见 :class:`SaveAction`。

    默认 :attr:`SaveAction.SAVE_CURRENT`（``FF AA 00 00 00``），与本函数此前的行为
    完全一致——写事务的第三步调的就是它，加参数不改变任何既有调用。

    另外两个官方取值做的是与「保存」**完全不同**的事：:attr:`SaveAction.RESTORE_DEFAULTS`
    抹掉全部配置，:attr:`SaveAction.REBOOT` 断链重启。默认值保持 ``SAVE_CURRENT``
    正是因为这个——让「不小心少传一个参数」落在最无害的那一档上。
    """
    return write_register(Register.SAVE, action)


def restore_defaults() -> bytes:
    """``FF AA 00 01 00``。恢复默认配置并保存。

    ⚠ **抹掉设备 flash 里的全部配置**，不只是本次会话写过的那些。未在真机上验证。
    """
    return save(SaveAction.RESTORE_DEFAULTS)


def reboot() -> bytes:
    """``FF AA 00 FF 00``。重启设备。

    ⚠ **会断开 BLE 链路**，且未在真机上验证。
    """
    return save(SaveAction.REBOOT)


def set_return_rate(rate: ReturnRate) -> bytes:
    """设置回传速率。取值被限定在已核实的档位，见 :class:`ReturnRate`。"""
    return write_register(Register.RRATE, rate)


def set_bandwidth(bandwidth: Bandwidth) -> bytes:
    """设置抗混叠带宽。取值被限定在协议文档列出的档位，见 :class:`Bandwidth`。

    「已登记」不等于「已核实」：这些标称频率本库一档都没实测过，登记它们是转述本型号
    官方协议文档，不是背书那个赫兹数。见 :class:`Bandwidth`。
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


def zero_z_axis() -> bytes:
    """``FF AA 01 04 00``。把当前朝向定为航向角零位。

    ⚠ **需先切到 6 轴算法才生效**，9 轴下不报错也不生效。门控由
    :meth:`~wt901.calibration.Calibration.zero_z_axis` 负责检查，本函数只造字节。
    未在真机上验证。
    """
    return write_register(Register.CALSW, CalibrationMode.ZERO_Z_AXIS)


def set_angle_reference() -> bytes:
    """``FF AA 01 08 00``。把当前姿态定为三轴角度零点。

    官方注明发送后需再发保存指令；本库的写事务本来就以保存收尾。未在真机上验证。
    """
    return write_register(Register.CALSW, CalibrationMode.ANGLE_REFERENCE)


BLUETOOTH_NAME_TERMINATOR = b"\r\n"
"""设置蓝牙名称指令的结束符。"""


def set_bluetooth_name(name: str) -> bytes:
    """``WT<蓝牙名称>\r\n``。**这不是寄存器写入**，封装与上面几条完全不同。

    ``name`` 是**完整的蓝牙名称**，必须以 ``WT`` 开头（协议文档：那两个字符「不可
    修改，否则会导致 APP 搜索不到」）。例：``set_bluetooth_name("WT12345678")``
    造出 ``b"WTWT12345678\r\n"``——**第一个 ``WT`` 是协议头，第二个是名称本身的
    前缀**，两个都要有，这一点极易看成写重复了。

    长度上限来自帧长：2（协议头）+ 2（``WT``）+ 14（可改部分）+ 2（``\r\n``）
    = **20 字节，正好用满 BLE 单次上传上限**。所以 ``WT`` 之后最多 14 字节。

    只接受 ASCII：名称按字节下发并按字节计长，多字节字符会让「几个字」与「几个
    字节」对不上，而超出的部分会被链路截断——那种失败在结果上表现为「名字改了但不
    是我要的名字」，比直接拒绝难查得多。

    ⚠ **改名后必须重启才生效**，见 :func:`reboot`。
    ⚠ **未在真机上验证过**：字节构造照协议文档写死并有离线测试，设备收到之后的行为
    没有实测数据。
    """
    if not name.startswith(BLUETOOTH_NAME_PREFIX):
        raise ConfigurationError(
            f"蓝牙名称必须以 {BLUETOOTH_NAME_PREFIX!r} 开头（官方约束：改掉前缀会让"
            f"维特 APP 搜不到设备），收到 {name!r}"
        )
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError:
        raise ConfigurationError(
            f"蓝牙名称只能是 ASCII，收到 {name!r}"
        ) from None

    suffix = len(encoded) - len(BLUETOOTH_NAME_PREFIX)
    if suffix > MAX_BLUETOOTH_NAME_SUFFIX_BYTES:
        raise ConfigurationError(
            f"蓝牙名称 {BLUETOOTH_NAME_PREFIX!r} 之后最多 "
            f"{MAX_BLUETOOTH_NAME_SUFFIX_BYTES} 字节，收到 {suffix} 字节：{name!r}"
        )
    return BLUETOOTH_NAME_PREFIX.encode("ascii") + encoded + BLUETOOTH_NAME_TERMINATOR
