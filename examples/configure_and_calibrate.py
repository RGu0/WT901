"""配置与校准：读回当前配置、改速率与带宽、做加计校准与磁场校准。

    python examples/configure_and_calibrate.py

**校准会写设备的 flash 并改变它的零位。** 加计校准必须在设备水平静止时做，否则
会把一个倾斜姿态固化成「水平」——不报错，只是从此所有角度都偏。磁场校准需要在
远离铁磁物体的环境里把设备绕三轴各转一圈。

本示例默认**不执行**校准，只演示调用形态；确认环境合适后把 DO_CALIBRATE 改成 True。

macOS 上若本终端未获蓝牙授权，进程可能**零输出、以退出码 134 终止**——
CoreBluetooth 直接 abort，Python 来不及抛任何异常。见 README「平台差异」。
"""

import asyncio

from wt901 import Bandwidth, ReturnRate, WT901Device, scan

DO_CALIBRATE = False


async def main() -> None:
    found = await scan()
    if not found:
        raise SystemExit("没有发现 WT 设备")

    async with await WT901Device.connect(found[0]) as device:
        # 读回当前配置。一次寄存器读会返回连续 4 个寄存器，这是协议决定的。
        async with device.registers.settings() as settings:
            print(f"当前速率编码 = 0x{settings.output_rate:02X}")
            print(f"当前带宽编码 = 0x{settings.bandwidth:02X}")

        # 写事务是原子的：解锁 → 写 → 保存，时序由库封装，调用方不需要关心。
        await device.registers.set_output_rate(ReturnRate.HZ_100)
        await device.registers.set_bandwidth(Bandwidth.HZ_20)
        print("已设为 100 Hz / 20 Hz 带宽")

        info = await device.telemetry.read_device_info()
        print(f"版本号 = {info.version}  电量 = {info.battery_percent}%")

        field = await device.telemetry.read_magnetic_field()
        # 量纲类型未知时 value 为 None——本库不猜系数，只给原始值。
        print(f"磁场 = {field.value} µT（原始 {field.raw}）")

        if not DO_CALIBRATE:
            print("\n未执行校准（DO_CALIBRATE = False）")
            return

        print("\n加计校准：请保持设备水平静止 ……")
        await device.calibration.calibrate_acceleration()

        print("磁场校准：请把设备绕三轴各缓慢转一圈 ……")
        async with device.calibration.field_calibration():
            await asyncio.sleep(20)
        print("校准结束")


asyncio.run(main())
