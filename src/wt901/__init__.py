"""WT901 系列惯性传感器硬件接口通用库。

上层应用（例如步态分析软件）通过本库访问设备，不直接实现协议解析与指令下发。

对外单位一律为 SI：加速度 m/s²、角速度 rad/s、角度 rad、磁场 µT、温度 °C、
位移 m。需要器件原始 int16 计数值时走各数据对象的 ``raw`` 属性。

最小采集::

    import asyncio
    from wt901 import ReturnRate, WT901Device, scan

    async def main() -> None:
        found = await scan()
        async with await WT901Device.connect(found[0]) as device:
            await device.registers.set_output_rate(ReturnRate.HZ_100)
            async for sample in device.samples():
                print(sample.t_host, sample.accel, sample.euler)

    asyncio.run(main())

## 什么构成公开契约

**这个命名空间里的名字。** 其余模块路径（``wt901.config``、``wt901.telemetry``
等）是实现位置，不是契约——它们会随重构移动。唯一的例外是刻意不在这里导出的
两类东西：

* ``wt901.protocol`` —— 纯函数协议层，零 I/O，可单独使用；作为子模块公开。
* ``wt901.recording`` 与 ``wt901.transport.replay`` —— **测试设施**，用于在没有
  硬件的环境里回放真机字节。它们的接口不承诺稳定。

导入本模块会连带导入 bleak（约 18 ms，实测 7.8 ms → 26.2 ms）。这点开销不值得
用惰性导入去省：那会让 ``from wt901 import WT901Device`` 对类型检查器变得不透明，
而 bleak 本来就是必装依赖。
"""

from wt901 import protocol
from wt901.calibration import Calibration
from wt901.config import RegisterAccess, Settings
from wt901.device import (
    ConnectionEvent,
    ConnectionState,
    DeviceStats,
    OutputMode,
    ReconnectPolicy,
    WT901Device,
)
from wt901.discovery import DiscoveredDevice, scan
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
from wt901.multi import MergedStream, MergeStats, merge
from wt901.protocol.frames import Frame, FrameDecoder, FrameFlag, RegisterResponse
from wt901.protocol.registers import (
    AlgorithmMode,
    Bandwidth,
    CalibrationMode,
    Mounting,
    Register,
    ReturnRate,
    SaveAction,
)
from wt901.telemetry import (
    Battery,
    ChipTime,
    PollerConfig,
    SerialNumber,
    Telemetry,
    TelemetryPoller,
)
from wt901.transport.base import Transport
from wt901.transport.ble import BleTransport

__all__ = [
    "AlgorithmMode",
    "Bandwidth",
    "Battery",
    "BleTransport",
    "Calibration",
    "CalibrationMode",
    "ChipTime",
    "ConfigurationError",
    "ConnectionEvent",
    "ConnectionLostError",
    "ConnectionState",
    "DeviceInfo",
    "DeviceNotFoundError",
    "DeviceStats",
    "DiscoveredDevice",
    "Euler",
    "Frame",
    "FrameDecoder",
    "FrameFlag",
    "FrameSyncError",
    "ImuSample",
    "MagneticField",
    "MergeStats",
    "MergedStream",
    "Mounting",
    "OutputMode",
    "PollerConfig",
    "ProtocolError",
    "Quaternion",
    "RawImuCounts",
    "ReconnectPolicy",
    "Register",
    "RegisterAccess",
    "RegisterResponse",
    "ReturnRate",
    "SaveAction",
    "SerialNumber",
    "Settings",
    "Telemetry",
    "TelemetryPoller",
    "Transport",
    "TransportError",
    "TransportTimeoutError",
    "UnexpectedRegisterResponse",
    "UnsupportedRegisterError",
    "Vec3",
    "WT901Device",
    "WT901Error",
    "__version__",
    "merge",
    "protocol",
    "scan",
]

__version__ = "0.3.0"
