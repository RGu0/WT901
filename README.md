# WT901

维特智能 **WT9011DCL-BT50**（BLE 5.0 九轴姿态传感器）的 Python 硬件接口通用库：
设备发现与连接、`0x55` 协议解析、寄存器读写、校准、多设备并发采集。上层应用
（如步态分析软件）调用本库，不重复实现协议。

对外单位一律为 **SI**：加速度 m/s²、角速度 rad/s、角度 rad、磁场 µT、温度 °C、
位移 m。需要器件原始 int16 计数值时走各数据对象的 `.raw`。

## 安装

**本包未发布到 PyPI**，按 git 引用消费。钉 tag 而不是分支——分支会在你不知情时移动：

```toml
# pyproject.toml
[project]
dependencies = ["wt901"]

[tool.uv.sources]
wt901 = { git = "https://github.com/RGu0/WT901.git", tag = "v0.3.0" }
```

两段分开写是有理由的：`dependencies` 里只放普通包名，来源交给 `[tool.uv.sources]`。
写成 direct reference（`"wt901 @ git+..."`）会被**烘进构建出的 wheel 元数据**，拿到
wheel 的人就得到一个改不掉的 git URL，不少索引还直接拒收。

不用 uv 时才退回 direct reference，且**只适合装成应用、不再被别人依赖的项目**：

```bash
pip install "wt901 @ git+https://github.com/RGu0/WT901.git@v0.3.0"
```

每个 tag 对应 [CHANGELOG](CHANGELOG.md) 里的一节，升级前先读那一节的「升级须知」。

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
| 配置 | `RegisterAccess`（`device.registers`）、`Settings`、`Register`、`ReturnRate`、`Bandwidth`、`AlgorithmMode`（`SIX_AXIS` 才能让 Z 轴角度归零生效）、`Mounting`（安装方向，写默认值也别省；`VERTICAL` **必须 Y 轴箭头朝上**）、`SaveAction` |
| 遥测 | `Telemetry`（`device.telemetry`；`read_mac()` 是唯一可跨主机持久化的设备身份）、`TelemetryPoller`、`PollerConfig`、`ChipTime`、`Battery`、`SerialNumber` |
| 校准 | `Calibration`（`device.calibration`）、`CalibrationMode` |
| 设备级动作 | `registers.restore_defaults()`（打回出厂并清空重放记录）、`registers.reboot()`（会断链）、`registers.exclusive()`（独占 GATT 写特征）、`device.set_bluetooth_name()`（**不是设备身份，别拿它替代 `read_mac()`**）|
| 多设备 | `merge`、`MergedStream`、`MergeStats` |
| 数据模型 | `ImuSample`、`Vec3`、`Euler`、`Quaternion`、`MagneticField`、`DeviceInfo`、`RawImuCounts` |
| 协议层（纯函数、零 I/O） | `wt901.protocol`、`Frame`、`FrameDecoder`、`FrameFlag`、`RegisterResponse` |
| 传输 | `Transport`（可自行实现）、`BleTransport` |
| 异常 | `WT901Error` 及其九个子类 |

协议细节（GATT UUID、帧格式、完整寄存器表、上游资料缺陷清单）见
[`docs/protocol.md`](docs/protocol.md)。

> ⚠ **「设备级动作」那一行的三条、以及 `CalibrationMode` 的 `ZERO_Z_AXIS` /
> `ANGLE_REFERENCE`，本库一条都没有在真机上验证过。** 字节构造照官方协议文档写死并有
> 离线测试，但设备收到之后到底做了什么没有实测数据。本库自己吃过这个亏——见下面
> 「`0x0A` 被有意排除」。**字节构造正确与设备行为符合预期是两件事。**

## 平台差异

`DiscoveredDevice.address` 与 `sample.device_id` **跨平台不可移植**：

| 平台 | 地址是什么 | 跨主机稳定 | 跨会话稳定 |
|---|---|---|---|
| macOS | CoreBluetooth 分配的 UUID | ❌ 每台主机各生成一份 | 一般稳定，但不保证 |
| Linux / Windows | 设备 MAC 地址 | ✅ | ✅ |

要把设备绑到「左脚/右脚」这类角色**并持久化**，用 `await device.telemetry.read_mac()`
——设备自报的蓝牙地址（寄存器 `0x66`）由设备自己给出，与主机无关。别用广播名（同批次
重复）或序列号（真机上读到过全零）。

**连接时请把 `scan()` 返回的整个 `DiscoveredDevice` 传进去，不要只传地址字符串。**
给字符串时 bleak 需要自己再扫一遍做地址→平台句柄的解析，macOS 上这个解析并不
可靠——失败时报的是「设备未找到」，哪怕设备就在眼前、信号很强。

同理，`DiscoveredDevice.handle` **只在本次扫描会话内有效**，不可持久化、不可跨
进程传递，也不能把两次扫描的结果拼在一起用。

### 连接期信号强度（RSSI）只有 macOS 拿得到

`await device.read_rssi()` 与 `TelemetryPoller.rssi` 给出连接期的链路信号强度，
单位 dBm，**拿不到时是 `None`，永远不是 0**（0 dBm 是极强信号）。

| 平台 | 连接期 RSSI |
|---|---|
| macOS | **实测可得**（2026-09-01，60/60，−71..−33 dBm） |
| Linux / Windows | **恒为 `None`** |

macOS 那一行是真机实测的结果，判据取证前预注册，经过与局限见
[`docs/protocol.md`](docs/protocol.md) §8.1。**要注意的局限**：实测确立的是「读得到、
且随信号条件变化」，**不包括**「能在丢包之前预警」——那次采集里 `resync_count`
全程为 0，没有可对照的事件。

这不是本库偷懒：`bleak 0.22.3` 的 `BleakClient` 公开门面根本没有这个方法，
`BaseBleakClient` 也没声明它，只有 CoreBluetooth 后端的实现类上有。细节与本库
为它加的锁和超时见 [`docs/protocol.md`](docs/protocol.md) §8.1。

**扫描期的 RSSI 是另一回事**：`DiscoveredDevice.rssi` 来自广播包，各平台都有，但
连上之后就不再更新。

### macOS：未授权蓝牙时进程会静默终止

在「系统设置 → 隐私与安全性 → 蓝牙」里授权运行脚本的那个应用（Terminal.app、
iTerm、IDE 等）。**没有授权时的表现不止一种**：

| 现象 | 含义 |
|---|---|
| `TransportError: 扫描失败：BLE is not authorized …` | 授权被明确拒绝，bleak 能查到状态并报错 |
| **零输出，退出码 134** | CoreBluetooth 直接 `abort()`（信号 6），Python 来不及抛任何异常 |

第二种最难查——终端上什么都没有，看起来像脚本自己消失了。遇到它先确认授权，
而不是去查代码。授权状态改变后需要**重新启动**那个应用才生效。

### 连续运行两个脚本时留出几秒

上一个进程退出后，BLE 连接不一定立刻释放；紧接着连同一台设备可能超时
（`TransportTimeoutError: 连接 … 超时`）。等几秒再跑，或把设备断电重启。

这一点没有被确认为库的缺陷——观察到的只是「刚用过的设备更容易连接超时」这个
相关性。但**你自己的代码必须保证连接被关闭**：连接循环要写在 `try` 里面，
逐台连上就登记，`finally` 关掉已连上的那些。放在 `try` 外面的话，第一台连上、
第二台失败，第一台就泄漏了，而泄漏的连接会让下一次 `connect` 直接失败。
`examples/two_devices.py` 演示了正确写法。

## 已知限制与实测结论

### 速率档位：`0x0A` 被有意排除

每一档都在真机上逐档实测（2026-08-18，WT901BLE67）：

| 编码 | 枚举 | 实测 | 标称 | 偏差 |
|---|---|---|---|---|
| `0x06` | `HZ_10` | 10.05 Hz | 10 | 0.5% |
| `0x07` | `HZ_20` | 19.86 Hz | 20 | 0.7% |
| `0x08` | `HZ_50` | 49.45 Hz | 50 | 1.1% |
| `0x09` | `HZ_100` | 99.26 Hz | 100 | 0.7% |
| `0x0A` | **未开放** | 99.29 Hz | 125¹ | **20.6%** |
| `0x0B` | `HZ_200` | 198.43 Hz | 200 | 0.8% |

¹ **本型号官方协议文档的速率表根本没有 `0x0A` 这个编码**（它只有 `0x09:100Hz`、
`0x0B:200Hz`）。「125 Hz」出自维特跨型号的通用编码表，本型号文档从未这么说。

实测与之一致：`0x0A` 测出 99.29 Hz，与 `0x09` 几乎相同。这不是链路带宽不足——同一次
探测里 `0x0B` 跑到了 198 Hz。**排除它的理由因此有两条**：本型号官方表没定义这个编码，
实测又证实了一遍。需要它的人可以直接写寄存器值。

低于 10 Hz 的档位（`0x01`–`0x05`）未探测。

### 带宽档位是另一回事：七档全开，七个标称值都没实测过

`Bandwidth` 覆盖 `0x1F` 的 `0x00`–`0x06` 全七档（256 / 188 / 98 / 42 / 20 / 10 / 5 Hz），
**这七个数字载于本型号官方协议文档，但本库一个都没坐实**——别把它与上面那张逐档实测
的速率表看成同一种东西。出厂默认是 `0x04`（标称 20 Hz）。

两处的命名规矩其实是同一条「名字不许说超出证据的话」，只是能说的话不一样：
`ReturnRate.HZ_100` 是**断言**（本库量到过 100 Hz），所以实测 99.29 Hz 的 `0x0A`
不能叫 `HZ_125`；`Bandwidth.HZ_98` 是**转述**（协议文档把这个编码标为 98 Hz），转述
要标明出处，但不必先实测。

原因在观测量：速率可以写进去再直接量出来，观测量与被测量是同一个东西；`0x1F` 改的是
内部抗混叠滤波器的截止频率，它不改变样本速率，只改变样本内容的频率成分。2026-08-27
用噪声底功率谱比值量过一次，方法自带自校验——先拿标着 20 Hz 的 `0x04` 跑，结果读回
10.9 Hz，**自校验没过**，所以同一次测出的 `0x03` 同样不算数。经过见
[`docs/protocol.md`](docs/protocol.md) §6.2。

那次只覆盖 `0x00`/`0x03`/`0x04`，确立了两件不依赖标定的事：设备接受这三个编码并照常
出数；三档截止频率单调有序（`0x00` 最宽、`0x04` 最窄、`0x03` 居中）。新增的另四档连
这两件事都没测过。

**要拿这些数字做采样率/抗混叠的设计决策，先自己测。** 带宽滤波上位机自己也能做，放在
下位机只是实时性更好；本库的职责是如实开放接口并标清证据强度。`0x07` 及以上仍然拒绝
——那道防线防的是误写，与证据强度无关。

### 200 Hz 单台与双台并发均已实测

| 条件 | 时长 | 实测速率 | `dropped` / `resync` / `reconnects` |
| -- | -- | -- | -- |
| 单设备 `HZ_200` | —— | 198.43 Hz（≈3970 B/s） | —— |
| 双设备同时 `HZ_100` | 10 分钟 | 99.4 / 99.3 Hz | 0 / 0 / 0 |
| 双设备同时 `HZ_200` | 15 分钟 | **198.7 / 198.6 Hz** | **0 / 0 / 0** |

双设备 200 Hz 与单设备没有可测量的差别：合流共发出 357543 个样本（397.3 Hz，
约 7.9 kB/s），`out_of_order` 为 0 且与逐样本独立核对一致。**两台并发不分带宽。**

三台及以上没有测过。两台可行不蕴含三台可行——上表每一行都是各自跑出来的。

> 采集开始前的丢弃与采集期无关：设备一连上就开始推送，而第二台还在扫描/连接/
> 配置，先连上的那台没有消费者，队列（`DEFAULT_QUEUE_SIZE` = 1024）填满即丢。
> `tools/smoke_multi_device.py` 把它与采集期的计数分开报告。

### 多设备合流按 `t_host` 归并，但不是严格归并

严格的 k 路归并要求每条流都交出下一个样本才能确定谁最小，于是一台设备掉线就会
卡住整条流。`merge()` 改为有界延迟：最多等 `max_latency`（默认 50 ms），超时就从
已有样本里发最小的。

代价是超时路径上**可能**乱序，而乱序被记进 `MergeStats.out_of_order`，不做隐藏。
真机上这条路径被走到过很多次，却一次都没造成乱序：几次双设备长跑累计五十余万个
样本，`out_of_order` 始终为 0，且与逐样本独立核对一致。

**`out_of_order` 的基准是一条高水位线**：它数的是每一个早于「此前已发出的最大
`t_host`」的样本，不是相邻逆序的处数。一条流卡住几个采样周期、再把攒下的一批一次
吐出时，前者等于攒下的样本个数，后者只有 1。取前者是因为它就是**调用方要重排的
样本总数**——做时间对齐的下游每一个都得单独处理。想知道「链路抖了几次」看
`latency_flushes`，那才是频次。

**`latency_flushes` 的绝对值不要当基线用。** 两次同为双设备 200 Hz 的长跑，双活期
的抖动频次分别是 30 次/分与 3.3 次/分，相差近一个数量级；两次的 RSSI 不同
（-32/-36 对 -34/-45），但没有证据支持这就是原因。它反映的是当次链路状况，不是
器件特征。有意义的读法是**它有没有随时间失控地增长**，而不是它等于几。

`max_latency=None` 恢复严格归并，只适用于回放这类有限且不会中断的流。

### 一条流长期静默时，合流是「单边」的

等待预算属于**样本**，不属于流：一条流超过预算就被移出等待集，直到它自己再开口。
这段时间合流按存活流全速推进——`max_latency` 不会变成存活流的产出上限。

这不是优化。若按「每发一个样本都重新等一遍那条不说话的流」来付预算，存活流就被
钉死在 `1 / max_latency`（默认 20 Hz），**与设备速率无关**。真机上双设备 200 Hz
关掉一台，存活那台从 198.6 Hz 塌到 19.5 Hz，其余九成样本在设备队列里被丢掉，
且不报错。修复前后同一段离线复现：19.4 Hz → 不再受预算约束。

代价是这段输出没有等齐所有流，顺序只在存活流之间成立。它被记进
`MergeStats.emitted_while_stalled`，与 `emitted` 的比值就是「这次采集有多大一部分
并没有真正在归并」。**不看这个字段就发现不了**——合流不会因此变慢，也不会报错。

静默不是结束：掉电设备的流并没有结束，重连策略仍在重试，所以它不计入
`sources_finished`。它恢复后自动回到归并，由此产生的乱序照常计入 `out_of_order`。

`latency_flushes` 的含义也因此更干净：一条持续静默的流只贡献**一次**，这个数反映
的是抖动频次，不会被一台掉线设备刷爆。

### 电量读数可能是「不可用」而不是「很低」

`Battery.percent` 为 `None` 表示原始值不可能是一次真实测量（电压 ×100 不会是非
正数），此时只有 `raw` 可用。真机上读到过原始值 0——若照阶梯表原样映射，那会变成
一个看着正常的 0%，调用方据此去换电池，而真正的问题在别处。

判断读数是否可用用 `battery.is_plausible`；`DeviceInfo` 里则看 `battery_raw`：
它为 `None` 说明没读到，它有值而 `battery_percent` 为 `None` 说明读到了但数值不
可能。**阈值以下的合法低电量读数不受影响**（339 → 0%，340 → 5%）。

### 序列号可能整块读回全零

同一形状，同一理由。真机上读到过逐字节全零的序列号（两台不同主机、两台不同设备），
那不是一个空序列号，是一次读不出内容的读取：

```python
serial = await device.telemetry.read_serial_number()
if not serial.is_plausible:
    ...  # serial.value 是 None，只有 serial.raw 可用
```

`DeviceInfo` 里同样靠 `serial_number_raw` 分辨：它为 `None` 说明没读到，它有值而
`serial_number` 为 `None` 说明读到了但内容全零。**要做跨主机持久化的设备身份，用
`read_mac()`，不要用序列号。**

### 全零回读：一条统一的判据

寄存器整块回读全零是这个器件**有据可查**的现象（`docs/protocol.md` §10）。凡是这种
读数不可能是真实测量的地方，本库都不给一个看着正常的结果：

| 读取 | 全零时 |
|---|---|
| `read_battery()` | `percent` 为 `None` |
| `read_mac()` | 抛 `UnexpectedRegisterResponse` |
| `read_serial_number()` | `value` 为 `None` |
| `read_version()` | 抛 `UnexpectedRegisterResponse` |
| `read_chip_time()` | `is_plausible` 为 `False`（月 0 日 0 不是日期） |
| `read_quaternion()` | `is_plausible` 为 `False`（模为 0 不是朝向） |
| `read_temperature()` | **照常返回 0 °C** |

最后一行不是遗漏：0 °C 是可能的真实测量。本库只挡**不可能**的值，多挡一个就是发明
规则。

两种形状（抛异常 / 附标志）的取舍按用途定，逐条写在各自的 docstring 里：值只有一个
用途、不可信就毫无用处的抛异常（MAC、版本号）；「读到了全零」本身是线索、或原始值
仍有价值的附标志（电量、序列号、芯片时间、四元数）。

**四元数归一化前先看 `is_plausible`** —— 对模为 0 的四元数归一化得到 NaN，它会一路
飘进姿态解算。`TelemetryPoller` 默认每秒轮询四元数，不可信的值照常写进
`poller.quaternion`，标志随值一起走。

### ⚠ 磁场：6 轴模式下读数可能任意陈旧

上面那张表管的是「值不可能是真实测量」。磁场还有另一种失效——**值本身完全正常，只是
不是现在的**：

```python
field = await device.telemetry.read_magnetic_field()
if field.may_be_stale:        # 6 轴模式下为 True
    ...                       # 数值可能是几小时前的
if field.is_calibrated_unit:  # 与上面互相独立：说的是单位，不是新鲜度
    use(field.value)
```

真机确立（两台 WT901BLE67）：设备处于 6 轴解算（`0x24 = 1`）时**不采样磁力计**，磁场
寄存器停在一个固定值上不再更新——转动设备、开着实时流、跨连接会话都不变。**两台设备
的出厂状态都是 6 轴**，所以这是默认配置就会遇到的情形，不是配错才有的边角。

**陈旧的时间尺度没有已知上界。** 两台切回 6 轴后读到的都是切去 9 轴**之前**的旧值，
逐字节相同，中间隔了十几个小时、一整段 9 轴运行和多次断连重连。别按「大概几秒前」
来用。

`TelemetryPoller` 在 6 轴下**每个周期都会写进一个新对象**，而里面的 `raw` 可能自上电
起就没变过——轮询器只保证「这是刚读回来的」，保证不了「这是刚测出来的」。经过与两条
已知局限见 [`docs/protocol.md`](docs/protocol.md) §5.7.1。

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

### RSSI 是原因侧的量，别拿它替代结果侧指标

`resync_count`、`dropped_samples`、`MergeStats.out_of_order` 都是**结果**——链路
已经出问题之后它们才动。RSSI 是**原因**侧的，也是唯一一个能在丢包发生**之前**
给出预警的量。

两者要配合读，不能互相替代：

* 一次 30 分钟采集中途 `resync_count` 开始涨，只看它就只能事后猜「是不是走远
  了」；配上 RSSI 曲线就能直接看出来。
* 两台设备同采、其中一台 `dropped_samples` 偏高——是它自己链路差，还是主机侧带
  宽不够？**RSSI 是区分这两者的那个量。**
* 反过来，RSSI 一路平稳而 `resync_count` 猛涨，说明问题不在距离上。这同样是结论。

用 `TelemetryPoller` 取（默认每 5 秒一次）。它走链路层，不占 `0x61`/`0x71` 通道
的带宽。

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

### 崩溃截断的录制

进程被 `kill -9`、掉电、或写到一半被打断时，文件的最后一行必然是残行。默认读取是
严格的，一行残行会让**此前全部完好的数据一起变成不可读**。要把它们救回来：

```python
recording = read_recording(path, tolerate_truncated_tail=True)
if recording.truncated:
    ...  # 这次会话不完整，别当完整数据用
```

只有**最后一行**的「写到一半」会被容忍。中间行损坏说明文件被改过或拼接过，照旧
拒绝并指明行号；末行若能解析出 JSON 但时刻倒退或 hex 非法，那是损坏而不是截断，
同样拒绝。**默认严格是有意的**：静默容忍会把「这份文件坏了」变成一个没人注意到的
事实，所以要容忍就得明写，拿到结果还要看 `truncated`。

`ReplayTransport` 不需要另开开关——救回来的 `Recording` 直接传给它的构造函数即可
（`ReplayTransport.from_file` 走的是默认的严格读取）。
