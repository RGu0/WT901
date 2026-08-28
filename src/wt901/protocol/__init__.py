"""WT9011DCL-BT50 协议层。

本包是**纯函数、零 I/O** 的：不 import ``asyncio``、``bleak`` 或任何传输实
现。协议无校验和，帧同步与换算是最容易出错的地方，把它们与「蓝牙通不通」彻
底分开，才能在没有硬件的情况下完整测试。这条约束由 ``tests`` 中的一条测试
守着，不要绕过它。
"""

from wt901.protocol import commands, units
from wt901.protocol.frames import (
    FRAME_LENGTH,
    HEADER,
    Frame,
    FrameDecoder,
    FrameFlag,
    RegisterResponse,
    decode_data_frame,
    decode_register_response,
)
from wt901.protocol.registers import (
    BLUETOOTH_NAME_PREFIX,
    MAX_BLUETOOTH_NAME_SUFFIX_BYTES,
    UNLOCK_KEY,
    AlgorithmMode,
    Bandwidth,
    CalibrationMode,
    Mounting,
    Register,
    ReturnRate,
    SaveAction,
)

__all__ = [
    "BLUETOOTH_NAME_PREFIX",
    "FRAME_LENGTH",
    "HEADER",
    "MAX_BLUETOOTH_NAME_SUFFIX_BYTES",
    "UNLOCK_KEY",
    "AlgorithmMode",
    "Bandwidth",
    "CalibrationMode",
    "Frame",
    "FrameDecoder",
    "FrameFlag",
    "Mounting",
    "Register",
    "RegisterResponse",
    "ReturnRate",
    "SaveAction",
    "commands",
    "decode_data_frame",
    "decode_register_response",
    "units",
]
