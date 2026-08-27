"""真机冒烟：按需读取 + 周期轮询对采样率的影响。

手动执行，不进 CI。**必须在 Claude 内置终端或 Terminal.app 里跑。**

    ./dev run python tools/smoke_telemetry.py [每段测量秒数]

覆盖的验收点：
  * 真机读到的序列号/版本号与上位机软件显示一致（本脚本负责打印，比对由人做）
  * TelemetryPoller 开启后在 100 Hz 采集下对样本速率的影响

第二项是本 scope 最值得测的东西：周期读取与实时数据流抢同一条 BLE 链路，
官方 SDK 无条件开线程轮询，而「到底影响多大」从来没人量过。
"""

from __future__ import annotations

import asyncio
import sys
import time

from wt901.device import WT901Device
from wt901.discovery import scan
from wt901.protocol.registers import Register, ReturnRate
from wt901.telemetry import PollerConfig, TelemetryPoller


async def measure_rate(device: WT901Device, seconds: float) -> float:
    """排空积压后测速率。积压不排会把短窗口的读数抬高一个固定偏移。"""
    remaining = device.pending_samples
    count = 0
    start = time.monotonic()
    async for _ in device.samples():
        if remaining > 0:
            remaining -= 1
            start = time.monotonic()
            continue
        count += 1
        if time.monotonic() - start >= seconds:
            break
    return count / (time.monotonic() - start)


async def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

    found = await scan(6.0)
    if not found:
        print("没扫到 WT 设备。")
        return 2
    sensor = found[0]
    print(f"连接 {sensor.name} ({sensor.address}) rssi={sensor.rssi}\n")

    async with await WT901Device.connect(sensor) as device:
        # --- 设备身份：与上位机软件比对 ---
        info = await device.telemetry.read_device_info()
        print("设备信息（请与上位机软件显示的值逐项比对）")
        if info.serial_number is None and info.serial_number_raw is not None:
            # 「读到了但内容全零」与「没读到」在这里必须分得开——本器件真机上
            # 前者是常态（RAY-172、RAY-293），打成 None 会让人以为是链路问题。
            print(f"  序列号   = 读到了但内容全零（raw={info.serial_number_raw.hex()}）")
        else:
            print(f"  序列号   = {info.serial_number!r}")
        print(f"  版本号   = {info.version!r}")
        print(f"  温度     = {info.temperature_c} °C")
        print(f"  电量     = {info.battery_percent}%（原始值 {info.battery_raw}）")

        chip_time = await device.telemetry.read_chip_time()
        print(f"  芯片时间 = {chip_time}")

        # 序列号/版本号的原始寄存器值。
        #
        # 首轮真机读到序列号为空字符串——那可能是设备本身没写序列号，也可能是
        # 我们的解析把它丢了。光看解析结果分不清这两种情况，所以把原始值也打出来：
        # 全零说明是设备侧为空，非零说明是解析的问题。
        serial_low = await device.registers.read(Register.SERIAL_NUMBER)
        serial_high = await device.registers.read(Register.SERIAL_NUMBER + 3)
        version_raw = await device.registers.read(Register.VERSION_LOW)
        print(
            "  ── 原始寄存器（用于区分「设备为空」与「解析出错」）"
            f"\n     0x7F..0x82 = {[f'0x{v & 0xFFFF:04X}' for v in serial_low.values]}"
            f"\n     0x82..0x85 = {[f'0x{v & 0xFFFF:04X}' for v in serial_high.values]}"
            f"\n     0x2E/0x2F  = 0x{version_raw.values[0] & 0xFFFF:04X}"
            f" 0x{version_raw.values[1] & 0xFFFF:04X}"
            f"  → uint32 0x{(version_raw.values[0] & 0xFFFF) | ((version_raw.values[1] & 0xFFFF) << 16):08X}"
        )

        field = await device.telemetry.read_magnetic_field()
        if field.value is not None:
            print(
                f"  磁场     = ({field.value.x:.2f}, {field.value.y:.2f}, "
                f"{field.value.z:.2f}) µT，模 {field.value.magnitude:.2f} µT"
                f"（量纲类型 {field.mag_type}）"
            )
        else:
            print(f"  磁场     = 量纲类型 {field.mag_type} 未知，仅原始值 {field.raw}")

        quat = await device.telemetry.read_quaternion()
        print(
            f"  四元数   = ({quat.w:.4f}, {quat.x:.4f}, {quat.y:.4f}, {quat.z:.4f})"
        )

        # --- 周期轮询对采样率的影响 ---
        await device.registers.set_output_rate(ReturnRate.HZ_100)
        await asyncio.sleep(0.5)

        print(f"\n各测 {seconds:.0f} 秒 @ 100 Hz：")

        baseline_stats = device.stats
        without = await measure_rate(device, seconds)
        mid_stats = device.stats
        print(
            f"  轮询关闭：{without:6.2f} Hz  "
            f"resync+{mid_stats.resync_count - baseline_stats.resync_count} "
            f"dropped+{mid_stats.dropped_samples - baseline_stats.dropped_samples}"
        )

        poller = TelemetryPoller(device.telemetry, PollerConfig())
        poller.start()
        try:
            with_polling = await measure_rate(device, seconds)
        finally:
            await poller.stop()
        final_stats = device.stats
        print(
            f"  轮询开启：{with_polling:6.2f} Hz  "
            f"resync+{final_stats.resync_count - mid_stats.resync_count} "
            f"dropped+{final_stats.dropped_samples - mid_stats.dropped_samples}"
        )

        if without > 0:
            impact = (without - with_polling) / without * 100
            print(
                f"\n默认轮询配置（磁场/四元数 1 s，温度/电量 30 s）的代价："
                f"采样率下降 {impact:.1f}%"
            )
            if impact < 2.0:
                print(
                    "  代价很小。默认关闭的理由因此**不是**「代价大」，而是"
                    "「不用的人不该付费」，\n  以及代价随轮询周期缩短而增长——"
                    "把周期从 1 s 调到 0.1 s 就是十倍的读取量。"
                )
            else:
                print(
                    "  代价可观。需要持续磁场/四元数时按需开启，并据此调周期。"
                )

    print("\n连接已释放。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
