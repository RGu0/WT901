"""真机取证：读设备自报 MAC（寄存器 ``0x66``）的应答到底怎么排布。

手动执行，不进 CI。**必须在已授权蓝牙的终端里跑。**

    ./dev run python tools/probe_mac.py [每台设备读取次数]

RAY-279 要给出一个跨主机稳定的设备身份（``DiscoveredDevice.address`` 在 macOS
上是 CoreBluetooth UUID，换机即失效）。手册给了读 MAC 的指令 ``FF AA 27 66 00``，
但**没有给应答里 6 个字节落在哪几个寄存器、字节序如何**——手册摘要自己把这条
列在待核实清单里。猜错的后果是安静的：绑定键会稳定地错成另一个值，两台设备照样
各自绑定，直到有人拿它和别处显示的 MAC 对不上才发现。所以先取证，再实现。

本脚本**不走** :class:`~wt901.device.WT901Device`，直接用传输层加解码器，原因是
当时设备层的寄存器通道只解出前 4 个寄存器，而这里要看的正是**整个 18 字节负载**——
「MAC 落在第 5 个寄存器之后」和「库少读了 4 个寄存器」在只看 4 个寄存器时长得一模
一样。

.. note::
   **后一种情况后来被证实了。** 本脚本第 1 步留下的疑问由 RAY-292 判定：一次
   ``0x71`` 应答携带 **8** 个寄存器，``REGISTERS_PER_RESPONSE`` 已从 4 改正为 8
   （``tools/probe_register_width.py``）。直接用传输层这一点仍然对——它保证看到的
   是原始负载而不是库的解读。

三件事一次做完：

1. **已知明文对照**：先读 ``0x7F``（序列号，12 字节 ASCII）。序列号的内容是可读
   的，所以它能直接把「一帧带几个寄存器」和「寄存器内的字节序」这两件事钉死——
   而 ``0x66`` 的应答是一串看不出对错的十六进制。
2. **读 ``0x66``**：打印整帧原始字节，并按几种排布假设各给出一个候选 MAC 字符串。
   脚本**不选**其中哪个对——那要靠人拿设备标签/系统蓝牙面板上的 MAC 去比。
3. **重复读**：同一台设备连读若干次，确认应答稳定（不稳定的值不能当身份）。

有两台设备时两台都测：两台的候选 MAC 必须互不相同，否则这个字段不是身份。
"""

from __future__ import annotations

import asyncio
import sys

from wt901.discovery import DiscoveredDevice, scan
from wt901.protocol import commands
from wt901.protocol.frames import FrameDecoder, FrameFlag
from wt901.transport.ble import BleTransport

DEFAULT_READS = 5
MAC_REGISTER = 0x66
SERIAL_REGISTER = 0x7F
RESPONSE_TIMEOUT = 1.0
"""秒。一次读应答的等待上限，超时算这次读失败而不是让脚本挂住。"""


class _Sniffer:
    """把原始字节喂给解码器，收集指定起始地址的 ``0x71`` 帧。

    只收 ``0x71``：设备默认 10 Hz 推 ``0x61`` 实时帧，两者混在同一条链路上。
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
    """把负载的寄存器区（去掉前 2 字节起始地址）切成 16 位字。

    一帧 20 字节：帧头 2 + 起始地址 2 + 寄存器区 16 = 8 个寄存器。**手册说 8 个，
    而本库只解 4 个**——这里全都打出来，让证据自己说话。
    """
    data = payload[2:]
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


def _hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def _mac_candidates(payload: bytes) -> list[tuple[str, str]]:
    """给出几种排布假设下的候选 MAC 字符串。

    MAC 是 6 字节，一个寄存器 2 字节，所以它占 3 个寄存器。未知的只有两件事：
    从哪个寄存器开始（``0x66`` 起，还是偏移到别处），以及 6 个字节的先后。列出
    的这几种是把「寄存器内小端 / 大端」与「整体正序 / 逆序」组合起来的全部结果，
    不含任何偏好——**选哪个要靠与设备标签上的 MAC 逐字节比对**。
    """
    registers = payload[2:]
    first_six = registers[:6]
    candidates = [
        ("寄存器 0x66–0x68，按收到的字节顺序", first_six),
        ("寄存器 0x66–0x68，整体逆序", first_six[::-1]),
        (
            "寄存器 0x66–0x68，每个寄存器内高低字节交换",
            b"".join(first_six[i : i + 2][::-1] for i in range(0, 6, 2)),
        ),
        (
            "寄存器 0x68–0x66 逆序，寄存器内保持原序",
            b"".join(first_six[i : i + 2] for i in (4, 2, 0)),
        ),
    ]
    return [
        (label, ":".join(f"{byte:02X}" for byte in value)) for label, value in candidates
    ]


async def _probe_device(device: DiscoveredDevice, reads: int) -> None:
    print(f"\n{'=' * 70}")
    print(f"设备 {device.name}  address={device.address}  rssi={device.rssi}")
    print(f"{'=' * 70}")

    transport = BleTransport(device)
    sniffer = _Sniffer()
    transport.on_data(sniffer.feed)
    await transport.connect()
    try:
        # ---- 1. 已知明文：序列号 ----------------------------------------
        payload = await sniffer.read(transport, SERIAL_REGISTER)
        print("\n── 读 0x7F（序列号，已知明文对照）")
        if payload is None:
            print("   超时，没收到应答。")
        else:
            registers = payload[2:]
            print(f"   完整负载（{len(payload)} 字节）：{_hex(payload)}")
            print(f"   起始地址：0x{payload[0] | (payload[1] << 8):02X}")
            print(f"   寄存器值：{[f'0x{w:04X}' for w in _words(payload)]}")
            print(f"   按小端逐寄存器解 ASCII：{registers.decode('ascii', 'replace')!r}")
            print(
                "   判读：若后 8 字节也是可读的序列号字符，则一帧确实带 8 个寄存器，\n"
                "         而本库当时的 REGISTERS_PER_RESPONSE=4 少读了一半；\n"
                "         若后 8 字节是零或垃圾，则 4 个寄存器是对的。\n"
                "         （这一步当时因序列号全零而落空，后由 RAY-292 用芯片时间与\n"
                "          温度判定为 8 个。此处保留原文以存证。）"
            )

        # ---- 2. 读 MAC ---------------------------------------------------
        print(f"\n── 读 0x66（MAC），连读 {reads} 次")
        seen: set[bytes] = set()
        for index in range(1, reads + 1):
            payload = await sniffer.read(transport, MAC_REGISTER)
            if payload is None:
                print(f"   第 {index} 次：超时")
                continue
            seen.add(payload)
            print(f"   第 {index} 次：{_hex(payload)}")
            await asyncio.sleep(0.05)

        if not seen:
            print("\n   一次都没读到应答。设备可能不支持 0x66，这本身就是结论。")
            return

        if len(seen) > 1:
            print(
                f"\n   ⚠️ {reads} 次读到 {len(seen)} 种不同的应答——**不稳定的值不能当身份**。"
            )

        payload = next(iter(seen))
        print(f"\n   寄存器值：{[f'0x{w:04X}' for w in _words(payload)]}")
        print("\n   候选 MAC（脚本不选，靠人比对）：")
        for label, mac in _mac_candidates(payload):
            print(f"     {mac}   ← {label}")
    finally:
        transport.on_data(None)
        await transport.disconnect()


async def main() -> int:
    reads = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_READS

    found = await scan(6.0)
    if not found:
        print("没扫到 WT 设备，需要至少一台通电的 WT9011DCL-BT50。")
        return 2

    print(f"扫到 {len(found)} 台设备。")
    for device in found:
        await _probe_device(device, reads)

    print(f"\n{'=' * 70}")
    print(
        "接下来要人做的比对（这是本次取证的全部意义）：\n"
        "  1. 看设备标签上印的 MAC，或在 Windows/Linux 主机的蓝牙面板里看该设备的\n"
        "     MAC —— macOS 看不到，它只给 CoreBluetooth UUID。\n"
        "  2. 上面哪个候选与之逐字节相同？把那一条连同完整负载贴回来。\n"
        "  3. 两台设备的候选必须互不相同，否则这个字段不是身份。\n"
        "把完整输出保存下来：它会成为实现的依据和回归 fixture。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
