"""WT9011DCL-BT50 寄存器定义。

来源：官方 SDK `WITMOTION/WitBluetooth_BWT901BLE5_0` 的 C# 与 Android 实现
（`Wit.Example_BWT901BLE/Form1.cs`、`ble5/Components/Bwt901bleProcessor.cs`、
`DipSensorMagHelper.java`）。**只登记已核实的地址与取值**——官方 BLE 5.0
协议文档并未给出完整寄存器表，凭记忆补全会写入未知语义。
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "MAC_START",
    "MAC_WORDS",
    "MAG_START",
    "QUATERNION_START",
    "REGISTERS_PER_RESPONSE",
    "SERIAL_NUMBER_START",
    "SERIAL_NUMBER_WORDS",
    "UNLOCK_KEY",
    "AlgorithmMode",
    "Bandwidth",
    "CalibrationMode",
    "Mounting",
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

    MOUNTING = 0x23
    """安装方向，取值见 :class:`Mounting`。

    出厂默认是不是 :attr:`Mounting.HORIZONTAL`（0）**本库没有核实过**，所以显式写它
    可能是幂等的，也可能不是。两种情况下这一步都必须写：不写就等于依赖设备残留的
    配置，而模块会被别处用过、配置又固化在 flash。要让「一份配置快照完全决定设备
    状态」成立，这一步不能省。
    """

    ALGORITHM = 0x24
    """姿态解算算法，取值见 :class:`AlgorithmMode`。

    它还门控着 ``CALSW`` 的一部分行为：手册对「Z 轴角度归零」
    （``FF AA 01 04 00``）注明**需先切到 6 轴算法才生效**。
    """

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

    MAC = 0x66
    """设备自报的蓝牙地址首寄存器，共 3 个寄存器 6 字节。

    **字节序是反的**：``0x66``–``0x68`` 按小端取出的 6 个字节，是蓝牙地址的
    **空口顺序**（低位在前），显示顺序要整体倒过来。取值与排布见
    :meth:`~wt901.telemetry.Telemetry.read_mac`。

    这是本库唯一可跨主机持久化的设备身份——:attr:`~wt901.discovery.DiscoveredDevice.address`
    在 macOS 上是 CoreBluetooth UUID，换台主机就变。
    """

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

REGISTERS_PER_RESPONSE = 8
"""一次读请求的回帧携带的寄存器个数——这是协议固定的，不是可选的。

**曾经写作 4，那是错的。** 2026-08-27 于 WT901BLE67 实测（``tools/probe_register_width.py``，
证据 ``ray-292/register-response-width/acceptance/``）：读 ``0x2E`` 的应答里，第 5、6 个
位置解出的分/秒与毫秒（``0x32``/``0x33``）夹在前后两次直读 ``0x30`` 的结果之间；读 ``0x3A``
的应答第 7 个位置（``0x40``）是 30.04 °C，与直读 ``0x40`` 的 30.03 °C 相符。两处都落在
原先被丢弃的那 8 个字节里，各测 3 轮全部成立。

帧长本来就算得出来：负载 18 字节 = 2 字节起始地址 + 16 字节寄存器区 = 8 个寄存器，
按 4 解会有 8 个字节没有说法。``decode_register_response`` 当时用 ``unpack_from`` 只取前
4 个，多出来的部分被静默丢弃，所以这个差异一直没暴露——**离线测试永远发现不了它**，
构造帧的测试工具与被测代码用的是同一个错误常量。
"""

MAG_START = Register.HX
QUATERNION_START = Register.Q0
MAC_START = Register.MAC
MAC_WORDS = 3
"""蓝牙地址占的寄存器个数。6 字节 ÷ 每寄存器 2 字节，一次读取就够。"""

SERIAL_NUMBER_START = Register.SERIAL_NUMBER
SERIAL_NUMBER_WORDS = 6


class AlgorithmMode(IntEnum):
    """写入 :attr:`Register.ALGORITHM` 的姿态解算算法。

    **编码与直觉相反**：值大的 ``1`` 对应的是「轴少」的那个。所以本库要求用具名
    成员而不是裸 0/1——写反了设备不会报错，只会让航向从相对航向变成依赖磁力计的
    绝对航向，在室内金属环境下悄悄劣化。这种错误在数据里看不出来。
    """

    NINE_AXIS = 0
    """9 轴：绝对航向，融合磁力计。磁环境干净时航向不随时间漂移。"""

    SIX_AXIS = 1
    """6 轴：相对航向，不用磁力计。航向会缓慢漂移，但不受磁干扰影响。

    「Z 轴角度归零」只在这个模式下生效。
    """


class Mounting(IntEnum):
    """写入 :attr:`Register.MOUNTING` 的安装方向。

    编码不像 :class:`AlgorithmMode` 那样反直觉（0 水平、1 垂直），但**写错同样不会
    报错**：装反了只是让姿态解算的重力轴对不上，数据一直偏，而链路、速率、丢包这些
    可观测量全部正常。所以仍然要求具名。
    """

    HORIZONTAL = 0
    """水平安装。适配文档 §3.1 要求的就是这一档。

    **它是不是出厂默认，本库没有核实过**——手册没写，也没人读过一台出厂状态设备的
    ``0x23``。正因为不确定，这次写入更不能省：见 :attr:`Register.MOUNTING`。
    """

    VERTICAL = 1
    """垂直安装。"""


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

    **每一档都在真机上实测过。** 2026-08-18 于 WT901BLE67 逐档写入并测量实际样本
    速率，见 ``tools/probe_rates.py`` 与 evidence
    ``ray-171/register-access/acceptance/``：

    ======  ==========  ======
    编码    实测         偏差
    ======  ==========  ======
    0x06    10.05 Hz    0.5%
    0x07    19.86 Hz    0.7%
    0x08    49.45 Hz    1.1%
    0x09    99.26 Hz    0.7%
    0x0B    198.43 Hz   0.8%
    ======  ==========  ======

    **``0x0A`` 被有意排除。** 维特通用编码表标它为 125 Hz，但实测 99.29 Hz——与
    ``0x09`` 几乎相同。这不是链路带宽不足：同一次探测里 ``0x0B`` 跑到了 198 Hz。
    说明该固件并未按通用表映射这个编码。测不准的不进枚举。

    低于 10 Hz 的档位（0x01–0x05）未探测，需要时按同样方式实测后再加。
    """

    HZ_10 = 0x06
    """出厂默认值。"""

    HZ_20 = 0x07
    HZ_50 = 0x08
    HZ_100 = 0x09
    HZ_200 = 0x0B
    """50 Hz 以上为打包传输：一次 BLE 通知可能携带多帧，解码器已覆盖。"""


class Bandwidth(IntEnum):
    """写入 :attr:`Register.BANDWIDTH` 的传感器带宽。

    与 :class:`ReturnRate` 同理，只登记已核实的两档。
    """

    HZ_20 = 0x04
    HZ_256 = 0x00
