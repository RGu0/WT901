"""真机取证：一次 ``0x71`` 应答到底携带 4 个还是 8 个寄存器。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑。**

    ./dev run python tools/probe_register_width.py [重复轮数]

本库的 ``REGISTERS_PER_RESPONSE = 4``，但三处证据都指向 8：18 字节负载减去 2 字节
起始地址正好是 8 个寄存器；手册摘要 §3.3 明写 8 个；``ray-279`` 的真机应答里第 6 个
寄存器是 ``0x03E8``，不像填充。``decode_register_response`` 只 ``unpack_from`` 前 4 个，
多出来的 8 字节被静默丢弃，所以这个差异至今没暴露过。

RAY-279 那轮原本用序列号当已知明文来判定，但两台设备的序列号寄存器全零，明文没了。
本脚本改用**语义可自证**的量：读一个起始地址，让后半段落在一个「对不对一眼看得出」
的寄存器上。十六进制看不出对错，年月日时分秒和室温看得出。

两个独立判据，任一成立即可判定为 8，两个都成立则相互佐证：

1. **芯片时间**（主判据）。读 ``0x2E``，8 个寄存器依次是
   ``0x2E 0x2F | 0x30 0x31 0x32 0x33 | 0x34 0x35``。其中 ``0x32``（分/秒）与
   ``0x33``（毫秒）**正好落在有争议的后 4 个位置里**，而分秒是会走的——脚本按
   「读 0x30 → 读 0x2E → 再读 0x30」的顺序取证，若 ``0x2E`` 应答第 3–6 个位置解出的
   时间夹在前后两次直读 ``0x30`` 的结果之间，那这 8 字节就是真寄存器数据，不是填充。
   填充不会自己走时间，更不会正好走在两次直读中间。
2. **温度**（旁证）。读 ``0x3A``，第 7 个位置是 ``0x40``（温度，除以 100 得摄氏度）。
   它同样在有争议的区间里，且室温是否合理一眼可判。与直读 ``0x40`` 对照。

**顺带核对已知明文**：``0x2E`` 应答的前两个寄存器是固件版本号，``ray-172`` 已记录
本型号为 ``0x0116 0x89D8``。这一段不在争议区间内，它的作用是确认帧解析没跑偏——
若连它都对不上，后面的判读全部作废。

本脚本**不走** :class:`~wt901.device.WT901Device` 的寄存器通道，直接用传输层加解码
器：设备层只解前 4 个寄存器，而这里要看的正是整个 18 字节负载。
"""

from __future__ import annotations

import asyncio
import sys

from wt901.discovery import DiscoveredDevice, scan
from wt901.protocol import commands
from wt901.protocol.frames import FrameDecoder, FrameFlag
from wt901.transport.ble import BleTransport

DEFAULT_ROUNDS = 3
VERSION_REGISTER = 0x2E
CHIP_TIME_REGISTER = 0x30
MAG_REGISTER = 0x3A
TEMPERATURE_REGISTER = 0x40
RESPONSE_TIMEOUT = 1.0
"""秒。一次读应答的等待上限，超时算这次读失败而不是让脚本挂住。"""

KNOWN_VERSION = (0x0116, 0x89D8)
"""``ray-172`` 记录的本型号版本寄存器值。对不上不代表判定失败，只说明这台设备的
固件与那次记录的不同——此时前两个寄存器就不再是已知明文，判读只能靠时间与温度。"""

PLAUSIBLE_TEMPERATURE_C = (-10.0, 60.0)
"""室内使用的传感器温度合理区间。超出这个范围说明第 7 个位置不是温度。"""


class _Sniffer:
    """把原始字节喂给解码器，收集指定起始地址的 ``0x71`` 帧。

    只收 ``0x71``：设备默认 10 Hz 推 ``0x61`` 实时帧，两者混在同一条链路上。
    与 ``tools/probe_mac.py`` 里的同名类相同，两个脚本各自独立，不互相 import——
    取证脚本是一次性证据的载体，共享代码会让后来者以为改一处就能改两处。
    """

    def __init__(self) -> None:
        self._decoder = FrameDecoder()
        self._wanted: int | None = None
        self._future: asyncio.Future[bytes] | None = None
        self._loop = asyncio.get_running_loop()

    def feed(self, data: bytes) -> None:
        """传输层回调。必须尽快返回。"""
        for frame in self._decoder.feed(data):
            if frame.flag is not FrameFlag.REGISTER:
                continue
            start = frame.payload[0] | (frame.payload[1] << 8)
            if start != self._wanted:
                continue
            if self._future is not None and not self._future.done():
                self._future.set_result(frame.payload)

    async def read(self, transport: BleTransport, register: int) -> bytes | None:
        """发一次读请求，返回 18 字节负载（含 2 字节起始地址），超时返回 ``None``。"""
        self._wanted = register
        self._future = self._loop.create_future()
        await transport.write(commands.read_register(register))
        try:
            return await asyncio.wait_for(self._future, RESPONSE_TIMEOUT)
        except TimeoutError:
            return None
        finally:
            self._wanted = None
            self._future = None


def _words(payload: bytes) -> list[int]:
    """把负载的寄存器区（去掉前 2 字节起始地址）切成 16 位无符号字。"""
    data = payload[2:]
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


def _hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def _decode_chip_time(words: list[int]) -> tuple[str, bool]:
    """按 :meth:`~wt901.telemetry.Telemetry.read_chip_time` 的排布解 4 个寄存器。

    返回 ``(可读字符串, 各字段是否都落在合理范围)``。范围检查是本脚本判读的一半：
    一段填充字节解出来几乎必然会有某个字段越界。
    """
    year_month, day_hour, minute_second, millisecond = words
    year = 2000 + (year_month & 0xFF)
    month = (year_month >> 8) & 0xFF
    day = day_hour & 0xFF
    hour = (day_hour >> 8) & 0xFF
    minute = minute_second & 0xFF
    second = (minute_second >> 8) & 0xFF

    plausible = (
        2000 <= year <= 2099
        and 1 <= month <= 12
        and 1 <= day <= 31
        and 0 <= hour <= 23
        and 0 <= minute <= 59
        and 0 <= second <= 59
        and 0 <= millisecond <= 999
    )
    text = (
        f"{year:04d}-{month:02d}-{day:02d} "
        f"{hour:02d}:{minute:02d}:{second:02d}.{millisecond:03d}"
    )
    return text, plausible


def _seconds_of_day(words: list[int]) -> float:
    """把芯片时间折成「当天第几秒」，只为做前后夹逼比较。

    跨零点会让这个数回绕，脚本对此不做补偿——真出现回绕，夹逼判据会显示为不成立，
    重跑一次即可，而不是让脚本自作聪明地修正一个它无法验证的假设。
    """
    _, day_hour, minute_second, millisecond = words
    hour = (day_hour >> 8) & 0xFF
    minute = minute_second & 0xFF
    second = (minute_second >> 8) & 0xFF
    return hour * 3600 + minute * 60 + second + millisecond / 1000


async def _probe_once(
    transport: BleTransport, sniffer: _Sniffer, index: int
) -> dict[str, bool]:
    """跑一轮取证，返回各判据的成立情况。"""
    print(f"\n── 第 {index} 轮")
    verdicts: dict[str, bool] = {}

    # ---- 判据 1：芯片时间夹逼 -------------------------------------------
    before = await sniffer.read(transport, CHIP_TIME_REGISTER)
    probe = await sniffer.read(transport, VERSION_REGISTER)
    after = await sniffer.read(transport, CHIP_TIME_REGISTER)

    if before is None or probe is None or after is None:
        print("   读超时，本轮的时间判据作废。")
    else:
        before_words = _words(before)[:4]
        after_words = _words(after)[:4]
        probe_words = _words(probe)
        middle_words = probe_words[2:6]

        before_text, _ = _decode_chip_time(before_words)
        after_text, _ = _decode_chip_time(after_words)
        middle_text, middle_ok = _decode_chip_time(middle_words)

        print(f"   读 0x2E 完整负载：{_hex(probe)}")
        print(f"   寄存器值：{[f'0x{w:04X}' for w in probe_words]}")

        version = tuple(probe_words[:2])
        if version == KNOWN_VERSION:
            print(
                f"   已知明文核对：前两个寄存器 = "
                f"0x{version[0]:04X} 0x{version[1]:04X}，与 ray-172 记录的版本号一致 ✅"
            )
        else:
            print(
                f"   已知明文核对：前两个寄存器 = 0x{version[0]:04X} 0x{version[1]:04X}，"
                f"与 ray-172 记录的 0x{KNOWN_VERSION[0]:04X} 0x{KNOWN_VERSION[1]:04X} 不同。\n"
                f"     固件不同则属正常，但这一段就不再是已知明文，本轮判读只靠时间与温度。"
            )

        print(f"   直读 0x30（之前）        ：{before_text}")
        print(f"   0x2E 应答第 3–6 个位置解 ：{middle_text}   字段合理性：{'✅' if middle_ok else '❌'}")
        print(f"   直读 0x30（之后）        ：{after_text}")

        lower, middle, upper = (
            _seconds_of_day(before_words),
            _seconds_of_day(middle_words),
            _seconds_of_day(after_words),
        )
        bracketed = lower <= middle <= upper
        print(
            f"   夹逼：{lower:.3f} ≤ {middle:.3f} ≤ {upper:.3f} → "
            f"{'✅ 成立' if bracketed else '❌ 不成立'}"
        )
        verdicts["chip_time"] = middle_ok and bracketed

    # ---- 判据 2：温度旁证 -----------------------------------------------
    mag = await sniffer.read(transport, MAG_REGISTER)
    direct = await sniffer.read(transport, TEMPERATURE_REGISTER)

    if mag is None or direct is None:
        print("   读超时，本轮的温度判据作废。")
    else:
        mag_words = _words(mag)
        raw = mag_words[6]
        signed = raw - 0x10000 if raw >= 0x8000 else raw
        celsius = signed / 100
        direct_raw = _words(direct)[0]
        direct_signed = direct_raw - 0x10000 if direct_raw >= 0x8000 else direct_raw
        direct_celsius = direct_signed / 100

        low, high = PLAUSIBLE_TEMPERATURE_C
        plausible = low <= celsius <= high
        close = abs(celsius - direct_celsius) <= 1.0

        print(f"\n   读 0x3A 完整负载：{_hex(mag)}")
        print(f"   寄存器值：{[f'0x{w:04X}' for w in mag_words]}")
        print(f"   第 7 个位置（按 8 个解即 0x40）：{celsius:.2f} °C   "
              f"合理性：{'✅' if plausible else '❌'}")
        print(f"   直读 0x40                     ：{direct_celsius:.2f} °C   "
              f"一致性（差 ≤ 1 °C）：{'✅' if close else '❌'}")
        verdicts["temperature"] = plausible and close

    return verdicts


async def _probe_device(device: DiscoveredDevice, rounds: int) -> None:
    print(f"\n{'=' * 70}")
    print(f"设备 {device.name}  address={device.address}  rssi={device.rssi}")
    print(f"{'=' * 70}")

    transport = BleTransport(device)
    sniffer = _Sniffer()
    transport.on_data(sniffer.feed)
    await transport.connect()
    try:
        results = [await _probe_once(transport, sniffer, index) for index in range(1, rounds + 1)]
    finally:
        transport.on_data(None)
        await transport.disconnect()

    chip_time = [r["chip_time"] for r in results if "chip_time" in r]
    temperature = [r["temperature"] for r in results if "temperature" in r]

    print(f"\n── 本台设备小结（{rounds} 轮）")
    print(f"   芯片时间判据成立：{sum(chip_time)}/{len(chip_time)}")
    print(f"   温度旁证成立    ：{sum(temperature)}/{len(temperature)}")

    if chip_time and all(chip_time):
        print("   → 后 8 字节是真寄存器数据。一次应答带 8 个寄存器。")
    elif chip_time and not any(chip_time):
        print("   → 后 8 字节不是 0x32–0x35。一次应答带 4 个寄存器，手册 §3.3 与本型号不符。")
    else:
        print("   → 各轮结论不一致。**不要据此判定**，把完整输出贴回来再议。")


async def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROUNDS

    found = await scan(6.0)
    if not found:
        print("没扫到 WT 设备，需要至少一台通电的 WT9011DCL-BT50。")
        return 2

    print(f"扫到 {len(found)} 台设备，每台跑 {rounds} 轮。")
    print("本脚本只读不写，不碰 flash。")
    for device in found:
        await _probe_device(device, rounds)

    print(f"\n{'=' * 70}")
    print(
        "判读方式：\n"
        "  · 两个判据都成立 → 一次 0x71 应答带 8 个寄存器，REGISTERS_PER_RESPONSE 改 8。\n"
        "  · 两个判据都不成立 → 确实是 4 个，多出的 8 字节另有说法（或就是填充）。\n"
        "  · 一个成立一个不成立 → 不判定。那说明帧的后半段有结构但不是简单的连续寄存器，\n"
        "    改成 8 会引入一个比现在更难发现的错误。把完整输出贴回来。\n"
        "把完整输出保存下来：它会成为实现的依据和回归 fixture。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
