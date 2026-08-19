# WT901

维特智能 WT901 系列惯性传感器的**硬件接口通用库**：负责设备连接（BLE 5.0 / 串口透传适配器）、
0x55 协议解析、寄存器读写与指令下发。上层应用（如步态分析软件）调用本库，不重复实现协议。

## 开发环境

统一通过 `./dev` 入口执行，禁止直接使用系统 Python、Conda base 或裸 `pip`：

```bash
./dev setup   # uv sync --locked
./dev test    # pytest
./dev lint    # ruff + mypy
./dev build   # compileall
```

Windows 使用 `pwsh -File dev.ps1 <action>`。

运行时锁定：`pyproject.toml` + `uv.lock` + `.python-version`，所有命令走 `uv run --locked`。

## 多设备并发采集

每台 `WT901Device` 各自持有传输、解码器与队列，在同一个 event loop 上并行采集互不
阻塞，样本自带 `device_id`。需要把多路合成一条时用 `wt901.multi.merge`：

```python
stream = merge([left, right])
async for sample in stream.samples():
    print(sample.device_id, sample.t_host)
print(stream.stats)   # emitted / out_of_order / latency_flushes
```

合流按 `t_host` 归并，但**不是严格的 k 路归并**：严格归并要求每条流都交出下一个
样本才能确定谁最小，于是一台设备掉线就会卡住整条流。这里改为有界延迟——最多等
`max_latency`（默认 50 ms），超时就从已有的样本里发最小的。代价是超时路径上可能
乱序，而乱序**被记进 `stats.out_of_order`**，不做隐藏。

`max_latency=None` 恢复严格归并，适用于回放这类有限且不会中断的流。

**不做时钟同步补偿。** `t_host` 是主机收到 BLE 通知的时刻，含蓝牙栈抖动，两台设备
之间的真实采样时刻差无法由它恢复。本库只保证时间戳来源一致且单调。

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
