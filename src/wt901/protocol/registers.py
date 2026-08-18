"""WT9011DCL-BT50 寄存器定义。

来源：官方 SDK `WITMOTION/WitBluetooth_BWT901BLE5_0` 的 C# 与 Android 实现
（`Wit.Example_BWT901BLE/Form1.cs`、`ble5/Components/Bwt901bleProcessor.cs`、
`DipSensorMagHelper.java`）。**只登记已核实的地址与取值**——官方 BLE 5.0
协议文档并未给出完整寄存器表，凭记忆补全会写入未知语义。
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "MAG_START",
    "QUATERNION_START",
    "REGISTERS_PER_RESPONSE",
    "SERIAL_NUMBER_START",
    "SERIAL_NUMBER_WORDS",
    "UNLOCK_KEY",
    "Bandwidth",
    "CalibrationMode",
    "Register",
    "ReturnRate",
]


class Register(IntEnum):
    """已核实的寄存器地址。"""

    SAVE = 0x00
    """写 0x0000 把当前配置固化到设备。"""

    CALSW = 0x01
    """校准控制，取值见 :class:`CalibrationMode`。"""

    RRATE = 0x03
    """回传速率，取值见 :class:`ReturnRate`。"""

    BANDWIDTH = 0x1F
    """传感器带宽，取值见 :class:`Bandwidth`。"""

    READADDR = 0x27
    """读寄存器指令的操作码（不是一个可读写的数据寄存器）。"""

    VERSION_LOW = 0x2E
    VERSION_HIGH = 0x2F
    """两寄存器拼成 uint32 的固件版本号。"""

    CHIP_TIME_YEAR_MONTH = 0x30
    CHIP_TIME_DAY_HOUR = 0x31
    CHIP_TIME_MINUTE_SECOND = 0x32
    CHIP_TIME_MILLISECOND = 0x33
    """芯片时间，前三个寄存器的高低字节各承载一个字段。"""

    HX = 0x3A
    HY = 0x3B
    HZ = 0x3C
    """磁场三轴原始值，换算系数由 :attr:`MAGTYPE` 决定。"""

    TEMPERATURE = 0x40
    """温度原始值，除以 100 得摄氏度。"""

    Q0 = 0x51
    Q1 = 0x52
    Q2 = 0x53
    Q3 = 0x54
    """四元数，各除以 32768。"""

    POWER = 0x64
    """电池原始值（约等于电压 ×100）。"""

    KEY = 0x69
    """写 :data:`UNLOCK_KEY` 解锁配置寄存器。"""

    MAGTYPE = 0x72
    """磁场量纲类型。磁场换算必须先读它，系数不是常量。"""

    SERIAL_NUMBER = 0x7F
    """序列号首寄存器，共 6 个寄存器 12 字节 ASCII。"""

    DISPLACEMENT_OUTPUT = 0x96
    """置 1 后 0x61 帧的负载语义改为位移/位移速度/角度。"""


UNLOCK_KEY = 0xB588
"""写入 :attr:`Register.KEY` 的解锁魔数。"""

REGISTERS_PER_RESPONSE = 4
"""一次读请求的回帧携带的寄存器个数——这是协议固定的，不是可选的。"""

MAG_START = Register.HX
QUATERNION_START = Register.Q0
SERIAL_NUMBER_START = Register.SERIAL_NUMBER
SERIAL_NUMBER_WORDS = 6


class CalibrationMode(IntEnum):
    """写入 :attr:`Register.CALSW` 的校准模式。"""

    NORMAL = 0x0000
    """退出校准，恢复正常输出。也用于结束磁场校准。"""

    ACCELERATION = 0x0001
    """加计校准，要求设备水平静置。"""

    MAGNETIC_FIELD = 0x0007
    """开始磁场校准，需绕 XYZ 三轴各转一圈后写回 :attr:`NORMAL`。"""


class ReturnRate(IntEnum):
    """写入 :attr:`Register.RRATE` 的回传速率。

    **只登记官方示例演示过的两档。** 器件本身支持 0.2–200 Hz，其余档位沿用维特
    通用编码，但未在现有资料中得到证实；开放它们需要先在真机上验证实际速率。
    见 RAY-171。
    """

    HZ_10 = 0x06
    """出厂默认值。"""

    HZ_50 = 0x08


class Bandwidth(IntEnum):
    """写入 :attr:`Register.BANDWIDTH` 的传感器带宽。

    与 :class:`ReturnRate` 同理，只登记已核实的两档。
    """

    HZ_20 = 0x04
    HZ_256 = 0x00
