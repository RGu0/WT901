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

SETTLE_SECONDS = 30.0
"""校准结束到开始观测之间的等待。

首次运行只等了 2 秒，测出 101 °/min 的 Yaw 漂移，无法判断那是坏校准还是融合
尚未收敛。9 轴算法在磁场基准变化后需要时间重新锁定，等够再测才有意义。
"""


EARTH_FIELD_MIN_UT = 25.0
EARTH_FIELD_MAX_UT = 75.0
"""地磁场强度的合理区间。全球约 25–65 µT，中国大陆多在 45–55 µT。

读数落在区间外基本只有两种可能：附近有磁干扰源，或硬磁偏置未校准。两种情况下
做磁场校准都会把错误的东西当成地磁场固化下来——**校准前必须先看这个数**。
"""


@dataclass(frozen=True, slots=True)
class Observation:
    """一段静置观测的结果。角度单位为度，仅用于打印；库内部是 rad。"""

    samples: int
    seconds: float
    roll_drift_deg: float
    pitch_drift_deg: float
    yaw_drift_deg: float
    yaw_drift_first_half_deg: float
    yaw_drift_second_half_deg: float
    roll_mean_deg: float
    pitch_mean_deg: float
    accel_magnitude: float
    field_magnitude_ut: float | None
    field_type: int

    @property
    def yaw_rate_deg_per_min(self) -> float:
        return abs(self.yaw_drift_deg) / self.seconds * 60 if self.seconds else 0.0

    @property
    def yaw_is_converging(self) -> bool:
        """后半段漂移明显小于前半段 → 是融合在收敛，不是稳态漂移。

        这个区分很要紧：收敛只需要多等一会儿，稳态漂移则说明校准本身是坏的。
        单看整段的平均漂移速率，两者长得一模一样。
        """
        first, second = abs(self.yaw_drift_first_half_deg), abs(
            self.yaw_drift_second_half_deg
        )
        return first > 0.05 and second < first * 0.5


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

    # 把窗口切成两半分别算漂移：收敛与稳态漂移在整段平均值上长得一模一样，
    # 只有比较前后半段才能分开。
    midpoint = len(yaws) // 2
    return Observation(
        samples=count,
        seconds=elapsed,
        roll_drift_deg=_unwrap_drift(rolls),
        pitch_drift_deg=_unwrap_drift(pitches),
        yaw_drift_deg=_unwrap_drift(yaws),
        yaw_drift_first_half_deg=_unwrap_drift(yaws[:midpoint]),
        yaw_drift_second_half_deg=_unwrap_drift(yaws[midpoint:]),
        roll_mean_deg=_deg(sum(rolls) / count) if count else 0.0,
        pitch_mean_deg=_deg(sum(pitches) / count) if count else 0.0,
        accel_magnitude=accel_sum / count if count else 0.0,
        field_magnitude_ut=(
            field.value.magnitude if field.value is not None else None
        ),
        field_type=field.mag_type,
    )


async def survey_field(device: WT901Device, seconds: float) -> list[float]:
    """采一段磁场矢量模，用于判断环境是否干净、转动是否到位。"""
    samples: list[float] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        field = await device.telemetry.read_magnetic_field()
        if field.value is not None:
            samples.append(field.value.magnitude)
        await asyncio.sleep(0.2)
    return samples


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
    print(
        f"  Yaw 分段  前半 {obs.yaw_drift_first_half_deg:+7.3f}°  "
        f"后半 {obs.yaw_drift_second_half_deg:+7.3f}°"
        f"   → {'收敛中' if obs.yaw_is_converging else '稳态'}"
    )
    print(f"  加速度模  {obs.accel_magnitude:.4f} m/s² "
          f"（{obs.accel_magnitude / 9.80665:.4f} g）")
    if obs.field_magnitude_ut is not None:
        flag = (
            ""
            if EARTH_FIELD_MIN_UT <= obs.field_magnitude_ut <= EARTH_FIELD_MAX_UT
            else "  ⚠ 超出地磁场合理区间"
        )
        print(f"  磁场模    {obs.field_magnitude_ut:.2f} µT{flag}")
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

        # --- 前置环境检查 ---
        #
        # 在受磁干扰的环境里做磁场校准，等于把干扰当成地磁场固化下来——之后
        # 换个位置就全错。这个检查必须在校准**之前**做，事后才发现就晚了。
        baseline_field = await device.telemetry.read_magnetic_field()
        if baseline_field.value is None:
            print(f"\n⚠ 磁场量纲类型 {baseline_field.mag_type} 未知，无法判断环境。")
        else:
            magnitude = baseline_field.value.magnitude
            print(f"\n磁场环境检查：当前磁场模 {magnitude:.2f} µT")
            if not EARTH_FIELD_MIN_UT <= magnitude <= EARTH_FIELD_MAX_UT:
                print(
                    f"  ⚠ **超出地磁场合理区间 "
                    f"{EARTH_FIELD_MIN_UT:.0f}–{EARTH_FIELD_MAX_UT:.0f} µT**。"
                    "\n  这通常意味着附近有磁干扰源（笔记本电脑、手机、音箱、"
                    "带磁扣的桌面）。"
                    "\n  规格书要求使用时远离电子设备等硬磁性物体至少 20 cm。"
                    "\n  在这种环境下校准会把干扰固化进硬磁偏置，结果不可信。"
                )
                answer = input("  仍要继续吗？(y/N) ").strip().lower()
                if answer != "y":
                    print("已中止。请换个位置再试。")
                    return 3
            else:
                print("  ✅ 在合理区间内")

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
            # 转动期间持续采磁场：读数的分散程度就是「转动是否到位」的证据。
            # 转得不够时磁力计只采到一小片朝向，校准出来的椭球拟合是错的，
            # 而这一点事后从任何单一读数上都看不出来。
            rotation_field = await survey_field(device, ROTATION_SECONDS)
        print("磁场校准已结束。")
        assert not device.calibration.is_field_calibrating

        if rotation_field:
            low, high = min(rotation_field), max(rotation_field)
            spread = high - low
            print(
                f"  转动期间磁场模 {low:.1f} – {high:.1f} µT"
                f"（跨度 {spread:.1f} µT，{len(rotation_field)} 次采样）"
            )
            if spread < 5.0:
                print(
                    "  ⚠ **跨度过小**：磁力计可能没有采到足够多的朝向。"
                    "\n  硬磁偏置的估计依赖于各朝向的读数分布，转动不足会让"
                    "拟合结果严重偏离。"
                )

        # 校准改变了磁场基准，9 轴融合需要时间重新收敛。等太短会把收敛过程
        # 误读成稳态漂移——这正是首次运行踩到的坑。
        print(f"\n等待融合收敛 {SETTLE_SECONDS:.0f} 秒 ...")
        await asyncio.sleep(SETTLE_SECONDS)

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

        # --- 判读 ---
        print("\n判读")
        verdicts: list[str] = []

        if after.field_magnitude_ut is None:
            verdicts.append("? 磁场量纲未知，无法判断磁场校准")
        elif not EARTH_FIELD_MIN_UT <= after.field_magnitude_ut <= EARTH_FIELD_MAX_UT:
            verdicts.append(
                f"✗ 校准后磁场模 {after.field_magnitude_ut:.1f} µT 仍在合理区间外"
                f"——校准没有把硬磁偏置修正到位，或环境本身有干扰"
            )
        else:
            verdicts.append("✓ 校准后磁场模落在地磁场合理区间")

        if after.yaw_is_converging:
            verdicts.append(
                f"~ Yaw 仍在收敛（前半 {after.yaw_drift_first_half_deg:+.2f}° → "
                f"后半 {after.yaw_drift_second_half_deg:+.2f}°），"
                "再等一会儿重测才能定论"
            )
        elif yaw_after <= yaw_before:
            verdicts.append(
                f"✓ Yaw 稳态漂移未变差（{yaw_before:.2f} → {yaw_after:.2f} °/min）"
            )
        else:
            verdicts.append(
                f"✗ Yaw 稳态漂移变差（{yaw_before:.2f} → {yaw_after:.2f} °/min）"
                "——校准结果比校准前更糟"
            )

        accel_g = after.accel_magnitude / 9.80665
        if abs(accel_g - 1.0) <= 0.02:
            verdicts.append(f"✓ 加速度模 {accel_g:.4f} g 接近 1 g")
        else:
            verdicts.append(f"✗ 加速度模 {accel_g:.4f} g 偏离 1 g")

        for line in verdicts:
            print(f"  {line}")

        print(
            "\n注：静置 Roll/Pitch 接近 0 只在**桌面本身水平**时才是有效判据；"
            "\n桌面不平时这两个值反映的是桌面倾角，不是校准质量。"
        )

    print("\n连接已释放。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
