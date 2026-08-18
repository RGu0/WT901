"""真机冒烟：校准前后对比。

手动执行，不进 CI。**必须在 Claude 内置终端或 Terminal.app 里跑，且需要人参与
转动传感器。**

    ./dev run python tools/smoke_calibration.py [观测秒数]

流程：

1. 校准前：静置观测一段，记录 Roll/Pitch/Yaw 的漂移量与磁场矢量模
2. 加计校准（要求水平静置）
3. 磁场校准（引导操作者绕 XYZ 三轴各转一圈）
4. 校准后：再静置观测同样长的一段
5. 对比

覆盖的验收点（RAY-173）：
  * 磁场校准前后对比磁场矢量模
  * 加计校准后水平静置时 Roll/Pitch 接近 0

**为什么盯 Yaw 漂移**：RAY-169 的真机数据记录过一个现象——静置状态下 Yaw 持续
漂移（5 帧内 -49.38° → -50.23°）而同期陀螺读数为 0。陀螺不动而航向角在变，说明
漂移来自 9 轴算法里的地磁融合，正是磁场校准该解决的问题。所以本脚本把「静置时
Yaw 的漂移速率」当作校准效果的主指标。
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from dataclasses import dataclass

from wt901.device import WT901Device
from wt901.discovery import scan
from wt901.protocol.registers import ReturnRate

ROTATION_SECONDS = 20.0
"""磁场校准的转动时长。要够操作者从容转完三轴——转太快磁力计采不到足够朝向。"""


@dataclass(frozen=True, slots=True)
class Observation:
    """一段静置观测的结果。角度单位为度，仅用于打印；库内部是 rad。"""

    samples: int
    seconds: float
    roll_drift_deg: float
    pitch_drift_deg: float
    yaw_drift_deg: float
    roll_mean_deg: float
    pitch_mean_deg: float
    accel_magnitude: float
    field_magnitude_ut: float | None
    field_type: int


def _deg(radians: float) -> float:
    return math.degrees(radians)


def _unwrap_drift(values: list[float]) -> float:
    """首末之差，按最短弧处理跨 ±π 的情况。

    Yaw 在 ±180° 附近会翻折；直接相减会得到 360° 的假漂移。
    """
    if len(values) < 2:
        return 0.0
    delta = values[-1] - values[0]
    while delta > math.pi:
        delta -= 2 * math.pi
    while delta < -math.pi:
        delta += 2 * math.pi
    return _deg(delta)


async def observe(device: WT901Device, seconds: float, label: str) -> Observation:
    """静置观测一段时间，统计角度漂移与加速度矢量模。"""
    print(f"\n[{label}] 请保持传感器**水平静止** {seconds:.0f} 秒 ...")

    # 排空积压：之前的写事务期间堆的样本不属于这段观测。
    remaining = device.pending_samples
    rolls: list[float] = []
    pitches: list[float] = []
    yaws: list[float] = []
    accel_sum = 0.0
    count = 0
    start = time.monotonic()

    async for sample in device.samples():
        if remaining > 0:
            remaining -= 1
            start = time.monotonic()
            continue
        rolls.append(sample.euler.roll)
        pitches.append(sample.euler.pitch)
        yaws.append(sample.euler.yaw)
        accel_sum += sample.accel.magnitude
        count += 1
        if time.monotonic() - start >= seconds:
            break

    elapsed = time.monotonic() - start
    field = await device.telemetry.read_magnetic_field()

    return Observation(
        samples=count,
        seconds=elapsed,
        roll_drift_deg=_unwrap_drift(rolls),
        pitch_drift_deg=_unwrap_drift(pitches),
        yaw_drift_deg=_unwrap_drift(yaws),
        roll_mean_deg=_deg(sum(rolls) / count) if count else 0.0,
        pitch_mean_deg=_deg(sum(pitches) / count) if count else 0.0,
        accel_magnitude=accel_sum / count if count else 0.0,
        field_magnitude_ut=(
            field.value.magnitude if field.value is not None else None
        ),
        field_type=field.mag_type,
    )


def report(label: str, obs: Observation) -> None:
    print(f"\n[{label}] {obs.samples} 样本 / {obs.seconds:.1f}s")
    print(
        f"  角度漂移  roll {obs.roll_drift_deg:+7.3f}°  "
        f"pitch {obs.pitch_drift_deg:+7.3f}°  yaw {obs.yaw_drift_deg:+7.3f}°"
        f"   （{obs.yaw_drift_deg / obs.seconds * 60:+.2f} °/min）"
    )
    print(
        f"  静置姿态  roll {obs.roll_mean_deg:+7.3f}°  "
        f"pitch {obs.pitch_mean_deg:+7.3f}°"
    )
    print(f"  加速度模  {obs.accel_magnitude:.4f} m/s² "
          f"（{obs.accel_magnitude / 9.80665:.4f} g）")
    if obs.field_magnitude_ut is not None:
        print(f"  磁场模    {obs.field_magnitude_ut:.2f} µT")
    else:
        print(f"  磁场模    量纲类型 {obs.field_type} 未知，无法换算")


async def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

    found = await scan(6.0)
    if not found:
        print("没扫到 WT 设备。")
        return 2
    sensor = found[0]
    print(f"连接 {sensor.name} ({sensor.address}) rssi={sensor.rssi}")

    async with await WT901Device.connect(sensor.address) as device:
        await device.registers.set_output_rate(ReturnRate.HZ_50)
        await asyncio.sleep(0.5)

        before = await observe(device, seconds, "校准前")
        report("校准前", before)

        # --- 加计校准 ---
        print("\n加计校准：请确保传感器**水平静置**，5 秒后开始 ...")
        await asyncio.sleep(5)
        await device.calibration.calibrate_acceleration()
        print("加计校准已下发。")
        await asyncio.sleep(2)

        # --- 磁场校准 ---
        print(
            f"\n磁场校准：即将开始，请在 {ROTATION_SECONDS:.0f} 秒内"
            "**绕 X / Y / Z 三轴各缓慢转一圈**。"
        )
        print("  （转太快磁力计采不到足够朝向；远离电脑、手机、音箱等磁干扰源）")
        await asyncio.sleep(3)

        async with device.calibration.field_calibration():
            assert device.calibration.is_field_calibrating
            for remaining in range(int(ROTATION_SECONDS), 0, -5):
                print(f"  ... 还剩 {remaining} 秒")
                await asyncio.sleep(min(5, remaining))
        print("磁场校准已结束。")
        assert not device.calibration.is_field_calibrating
        await asyncio.sleep(2)

        after = await observe(device, seconds, "校准后")
        report("校准后", after)

        # --- 对比 ---
        print("\n" + "=" * 60)
        print("对比")
        print("=" * 60)

        yaw_before = abs(before.yaw_drift_deg) / before.seconds * 60
        yaw_after = abs(after.yaw_drift_deg) / after.seconds * 60
        print(f"  Yaw 漂移速率  {yaw_before:6.2f} → {yaw_after:6.2f} °/min", end="")
        if yaw_before > 0:
            print(f"   （{(yaw_before - yaw_after) / yaw_before * 100:+.0f}%）")
        else:
            print()

        print(
            f"  静置 Roll     {before.roll_mean_deg:+7.3f}° → "
            f"{after.roll_mean_deg:+7.3f}°"
        )
        print(
            f"  静置 Pitch    {before.pitch_mean_deg:+7.3f}° → "
            f"{after.pitch_mean_deg:+7.3f}°"
        )
        print(
            f"  加速度模      {before.accel_magnitude / 9.80665:.4f} g → "
            f"{after.accel_magnitude / 9.80665:.4f} g"
        )
        if (
            before.field_magnitude_ut is not None
            and after.field_magnitude_ut is not None
        ):
            print(
                f"  磁场模        {before.field_magnitude_ut:.2f} → "
                f"{after.field_magnitude_ut:.2f} µT"
            )

        print(
            "\n判读提示："
            "\n  * 加计校准有效 → 水平静置时 Roll/Pitch 更接近 0，加速度模更接近 1 g"
            "\n  * 磁场校准有效 → Yaw 漂移速率下降；磁场模更接近当地地磁场量级"
            "\n    （中国大陆多在 45–55 µT，可查当地地磁场强度核对）"
            "\n  * 若磁场模远超该量级，说明附近有磁干扰源，校准结果不可信"
        )

    print("\n连接已释放。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
