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

    target = found[0]
    print(f"连接 {target.name} ({target.address}) rssi={target.rssi}")
    async with await WT901Device.connect(target) as device:
        # 读当前配置。返回的是**原始编码**而不是枚举：设备上可能存着本库尚未
        # 核实的档位（比如上位机软件设过），硬塞进枚举会抛异常，而调用方只是想
        # 知道设备现在是什么状态。
        print(f"当前速率编码 = 0x{await device.registers.read_output_rate():02X}")
        print(f"当前带宽编码 = 0x{await device.registers.read_bandwidth():02X}")

        # settings() 是**批量写**，不是读：它给你一个空的 Settings，你填要改的项，
        # 退出 async with 时统一下发；None 表示不动。逐项仍走完整的
        # 解锁 → 写 → 保存 时序，不合并成一次解锁多次写。
        async with device.registers.settings() as pending:
            pending.output_rate = ReturnRate.HZ_100
            pending.bandwidth = Bandwidth.HZ_20
        print("已设为 100 Hz / 20 Hz 带宽")

        info = await device.telemetry.read_device_info()
        # 电量同时打印百分比与原始值。百分比是查表得来的，单看它无法区分「电量
        # 真的低」和「这次读数不对」——原始值 <340 才映射到 0%，而一次异常读取
        # 也会落进同一档。库特意返回两者，示例就不该只用一半。
        print(f"版本号 = {info.version}")
        print(f"电量   = {info.battery_percent}%（原始值 {info.battery_raw}）")

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
