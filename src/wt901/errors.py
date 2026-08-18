"""异常层次。

所有本库抛出的异常都继承 :class:`WT901Error`，使调用方能够用一个 except
把本库的失败与自身逻辑的失败分开。
"""

__all__ = [
    "ConfigurationError",
    "ConnectionLostError",
    "DeviceNotFoundError",
    "FrameSyncError",
    "ProtocolError",
    "TransportError",
    "TransportTimeoutError",
    "UnexpectedRegisterResponse",
    "UnsupportedRegisterError",
    "WT901Error",
]


class WT901Error(Exception):
    """本库所有异常的根。"""


class TransportError(WT901Error):
    """连接与字节收发层面的失败。"""


class DeviceNotFoundError(TransportError):
    """扫描不到目标设备，或设备缺少所需的 GATT 服务/特征。"""


class ConnectionLostError(TransportError):
    """连接在使用过程中断开。"""


class TransportTimeoutError(TransportError):
    """在约定时间内没有等到对端的响应。"""


class ProtocolError(WT901Error):
    """收到的字节不符合协议约定。"""


class FrameSyncError(ProtocolError):
    """帧同步失败，且失败已严重到不应继续静默重同步。"""


class UnexpectedRegisterResponse(ProtocolError):
    """寄存器回读帧与请求不匹配，或帧类型与调用方预期不符。"""


class ConfigurationError(WT901Error):
    """配置意图本身不合法。"""


class UnsupportedRegisterError(ConfigurationError):
    """请求的寄存器取值尚未在真机上核实。

    维特官方资料只演示了部分档位。未经核实就写入寄存器可能让设备进入未知状态，
    因此本库宁可拒绝，也不猜测编码。
    """
