"""WT901 系列惯性传感器硬件接口通用库。

上层应用（例如步态分析软件）通过本库访问设备，不直接实现协议解析与指令下发。

对外单位一律为 SI：加速度 m/s²、角速度 rad/s、角度 rad、磁场 µT、温度 °C、
位移 m。需要器件原始 int16 计数值时走各数据对象的 ``raw`` 属性。
"""

from wt901 import protocol
from wt901.errors import (
    ConfigurationError,
    ConnectionLostError,
    DeviceNotFoundError,
    FrameSyncError,
    ProtocolError,
    TransportError,
    TransportTimeoutError,
    UnexpectedRegisterResponse,
    UnsupportedRegisterError,
    WT901Error,
)
from wt901.models import (
    DeviceInfo,
    Euler,
    ImuSample,
    MagneticField,
    Quaternion,
    RawImuCounts,
    Vec3,
)
from wt901.protocol.frames import Frame, FrameDecoder, FrameFlag, RegisterResponse
from wt901.protocol.registers import (
    Bandwidth,
    CalibrationMode,
    Register,
    ReturnRate,
)

__all__ = [
    "Bandwidth",
    "CalibrationMode",
    "ConfigurationError",
    "ConnectionLostError",
    "DeviceInfo",
    "DeviceNotFoundError",
    "Euler",
    "Frame",
    "FrameDecoder",
    "FrameFlag",
    "FrameSyncError",
    "ImuSample",
    "MagneticField",
    "ProtocolError",
    "Quaternion",
    "RawImuCounts",
    "Register",
    "RegisterResponse",
    "ReturnRate",
    "TransportError",
    "TransportTimeoutError",
    "UnexpectedRegisterResponse",
    "UnsupportedRegisterError",
    "Vec3",
    "WT901Error",
    "__version__",
    "protocol",
]

__version__ = "0.1.0"
