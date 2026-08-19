# WT901

维特智能 **WT9011DCL-BT50**（BLE 5.0 九轴姿态传感器）的 Python 硬件接口通用库：
设备发现与连接、`0x55` 协议解析、寄存器读写、校准、多设备并发采集。上层应用
（如步态分析软件）调用本库，不重复实现协议。

对外单位一律为 **SI**：加速度 m/s²、角速度 rad/s、角度 rad、磁场 µT、温度 °C、
位移 m。需要器件原始 int16 计数值时走各数据对象的 `.raw`。

## 安装

```bash
pip install wt901
```

Python ≥ 3.11。唯一运行时依赖是 [bleak](https://github.com/hbldh/bleak)。
包内带 `py.typed`，下游可直接享受完整类型信息（`mypy --strict` 可见）。

## 快速上手

```python
import asyncio
from wt901 import ReturnRate, WT901Device, scan

async def main() -> None:
    found = await scan()
    async with await WT901Device.connect(found[0]) as device:
        await device.registers.set_output_rate(ReturnRate.HZ_100)
        async for sample in device.samples():
            print(sample.t_host, sample.accel, sample.euler)

asyncio.run(main())
```

**出厂默认只有 10 Hz**，所以设速率这一步不能省。

完整可运行的例子在 [`examples/`](examples/)：

| 脚本 | 内容 |
|---|---|
| `minimal_capture.py` | 扫描 → 连接 → 100 Hz → 打印带时间戳样本 |
| `configure_and_calibrate.py` | 读回配置、改速率与带宽、加计校准与磁场校准 |
| `two_devices.py` | 双设备并发采集并按时间合流 |

## API 速览

公开契约就是 `wt901` 这个命名空间里的名字；其余模块路径是实现位置，会随重构移动。

```python
from wt901 import scan, WT901Device, merge, ReturnRate, Bandwidth
```

| 分组 | 名字 |
|---|---|
| 发现与连接 | `scan`、`DiscoveredDevice`、`WT901Device`、`ConnectionEvent`、`ConnectionState`、`ReconnectPolicy`、`DeviceStats`、`OutputMode` |
| 配置 | `RegisterAccess`（`device.registers`）、`Settings`、`Register`、`ReturnRate`、`Bandwidth` |
| 遥测 | `Telemetry`（`device.telemetry`）、`TelemetryPoller`、`PollerConfig`、`ChipTime`、`Battery` |
| 校准 | `Calibration`（`device.calibration`）、`CalibrationMode` |
| 多设备 | `merge`、`MergedStream`、`MergeStats` |
| 数据模型 | `ImuSample`、`Vec3`、`Euler`、`Quaternion`、`MagneticField`、`DeviceInfo`、`RawImuCounts` |
| 协议层（纯函数、零 I/O） | `wt901.protocol`、`Frame`、`FrameDecoder`、`FrameFlag`、`RegisterResponse` |
| 传输 | `Transport`（可自行实现）、`BleTransport` |
| 异常 | `WT901Error` 及其九个子类 |

协议细节（GATT UUID、帧格式、完整寄存器表、上游资料缺陷清单）见
[`docs/protocol.md`](docs/protocol.md)。

## 平台差异

`DiscoveredDevice.address` 与 `sample.device_id` **跨平台不可移植**：

| 平台 | 地址是什么 | 跨主机稳定 | 跨会话稳定 |
|---|---|---|---|
| macOS | CoreBluetooth 分配的 UUID | ❌ 每台主机各生成一份 | 一般稳定，但不保证 |
| Linux / Windows | 设备 MAC 地址 | ✅ | ✅ |

**连接时请把 `scan()` 返回的整个 `DiscoveredDevice` 传进去，不要只传地址字符串。**
给字符串时 bleak 需要自己再扫一遍做地址→平台句柄的解析，macOS 上这个解析并不
可靠——失败时报的是「设备未找到」，哪怕设备就在眼前、信号很强。

同理，`DiscoveredDevice.handle` **只在本次扫描会话内有效**，不可持久化、不可跨
进程传递，也不能把两次扫描的结果拼在一起用。

## 已知限制与实测结论

### 速率档位：`0x0A` 被有意排除

每一档都在真机上逐档实测（2026-08-18，WT901BLE67）：

| 编码 | 枚举 | 实测 | 标称 | 偏差 |
|---|---|---|---|---|
| `0x06` | `HZ_10` | 10.05 Hz | 10 | 0.5% |
| `0x07` | `HZ_20` | 19.86 Hz | 20 | 0.7% |
| `0x08` | `HZ_50` | 49.45 Hz | 50 | 1.1% |
| `0x09` | `HZ_100` | 99.26 Hz | 100 | 0.7% |
| `0x0A` | **未开放** | 99.29 Hz | 125 | **20.6%** |
| `0x0B` | `HZ_200` | 198.43 Hz | 200 | 0.8% |

维特通用编码表把 `0x0A` 标为 125 Hz，实测却是 99.29 Hz——与 `0x09` 几乎相同。
这不是链路带宽不足：同一次探测里 `0x0B` 跑到了 198 Hz。说明该固件并未按通用表
映射这个编码。**测不准的不进枚举**，需要它的人可以直接写寄存器值。

低于 10 Hz 的档位（`0x01`–`0x05`）未探测。`Bandwidth` 同样只开放了官方示例演示
过的 `0x00`（256 Hz）与 `0x04`（20 Hz）。

### 200 Hz 可用，但那是单设备的结论

`HZ_200` 实测 198.43 Hz（≈3970 B/s）稳定。**双设备并发下的实测只做到 100 Hz**：
两台同时 100 Hz 跑满 10 分钟，两路各 99.4 / 99.3 Hz，`dropped_samples`、
`resync_count`、`reconnects` 全为 0。两台同时 200 Hz 没有测过，不要据此假设它可行。

### 多设备合流按 `t_host` 归并，但不是严格归并

严格的 k 路归并要求每条流都交出下一个样本才能确定谁最小，于是一台设备掉线就会
卡住整条流。`merge()` 改为有界延迟：最多等 `max_latency`（默认 50 ms），超时就从
已有样本里发最小的。

代价是超时路径上**可能**乱序，而乱序被记进 `MergeStats.out_of_order`，不做隐藏。
10 分钟双设备实测：`latency_flushes=405`、`out_of_order=0`——这条路径每 1.5 秒被
走到一次，却一次都没造成乱序。

`max_latency=None` 恢复严格归并，只适用于回放这类有限且不会中断的流。

### 不做时钟同步补偿

`t_host` 是**主机收到 BLE 通知的时刻**，不是采样时刻——器件不提供时间戳。它含蓝牙
栈抖动，本库不插值也不伪造均匀时基。多设备之间的真实采样时刻差无法由它恢复，
补偿是上层的职责。

### 位移模式（寄存器 `0x96`）未实现

`0x96` 置 1 后输出切换为位移/位移速度/角度，但**位移帧与实时数据帧共用标志位
`0x61` 且布局无法区分**。本库拒绝在这种模式下按运动语义解析（`OutputMode` 声明为
非 `MOTION` 时 `samples()` 直接抛 `ConfigurationError`），而不是猜。

### 串口透传未实现

v0.1 仅 BLE。串口透传需要额外的适配器硬件，目前没有该硬件，因此不预留看似可用
的接口。

### 周期补充读取默认关闭

`TelemetryPoller` 会周期读磁场/四元数/温度/电量，它与实时数据流抢同一条 BLE 链路。
实测代价很小（默认配置下 100 Hz 采集掉 0.1%–0.4%），**默认关闭的理由不是代价大**，
而是「不用的人不该付费」，以及代价随轮询周期缩短线性增长。

### 一个与本库无关但会遇到的类型检查坑

`float ** 0.5` 在 mypy 下推断为 `Any`（typeshed 里 `float.__pow__` 的返回类型如此，
因为负底数配分数指数会得到复数）。算矢量模请用 `math.sqrt`，否则 `--strict` 会
报 `no-any-return`。

## 许可证

[MIT](LICENSE)。

## 开发环境

统一通过 `./dev` 入口执行，禁止直接使用系统 Python、Conda base 或裸 `pip`：

```bash
./dev setup   # uv sync --locked
./dev test    # pytest
./dev lint    # ruff + mypy strict
./dev build   # uv build（产出 wheel 与 sdist 到 dist/）
```

Windows 使用 `pwsh -File dev.ps1 <action>`。

运行时锁定：`pyproject.toml` + `uv.lock` + `.python-version`，所有命令走
`uv run --locked`。索引在 `pyproject.toml` 里固定为 pypi.org——机器级的镜像配置
会让 `--locked` 比对失败，症状是「换台机器就装不上」而根因在仓库之外。

## 录制与回放（测试设施，非对外 API）

`wt901.recording` 与 `wt901.transport.replay` **不是对外功能**，不在顶层 `wt901`
命名空间导出。它们存在的唯一理由是：本库的价值集中在设备层与协议层的交互上，而
这段交互在 CI 里没有硬件可跑。录一次真机字节流下来，就能在没有硬件的机器上端到端
驱动整条链路。

录制文件是 JSON Lines：第一行头部，其后每行一段收到的字节。

```
{"created_utc":"…","device_id":"…","format":"wt901-recording","note":"…","version":1}
{"t":0.0,"hex":"5561…"}
```

`t` 是相对第一段字节的秒数（不是绝对时间——跨机器没有意义），字节用十六进制。
选文本而不是二进制，是因为基线文件要进仓库：二进制在 code review 里是一团乱码，
在 git 历史里每次改动都是整文件替换。

```bash
./dev run python tools/record_session.py            # 录一份基线
```

回放时 `speed=None` 表示不等待（CI 用），`speed=1.0` 按原时序。**回放不回答下行
指令**：录制文件只有接收方向的字节，寄存器读写这类请求/应答事务在回放中会等到
超时。回放能验证的是数据流，不是事务。
