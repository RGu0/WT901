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
