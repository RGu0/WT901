"""设备自报 MAC（寄存器 ``0x66``）。全部离线：喂真机抓到的原始应答字节。

**fixture 是真机字节，不是编出来的。** 2026-08-27 两台 WT901BLE67 各连读 5 次，
逐字节一致，见 ``tools/probe_mac.py`` 与证据 ``ray-279/device-mac/acceptance/``。
排布本身没有厂商文档可依（手册只给了指令 ``FF AA 27 66 00``，没给字节排布），
所以这里钉的是「这串真机字节 → 这个字符串」，而不是某个凭空写下的常量：换句话说，
这些测试的权威来自那次采集，改动它们必须先改证据。

喂的是**完整的 20 字节帧**而不是重新打包的寄存器值——这样解码路径与真机一致，
`0xFD08` 这种最高位为 1 的寄存器不会在测试替身里就被当成有符号数处理掉。
"""

from __future__ import annotations

import asyncio

import pytest

from wt901.device import WT901Device
from wt901.errors import UnexpectedRegisterResponse
from wt901.protocol.frames import FRAME_LENGTH, HEADER, FrameFlag
from wt901.protocol.registers import Register
from wt901.transport.memory import MemoryTransport

# 真机抓到的 0x71 应答负载：2 字节起始地址 + 16 字节寄存器区。
BLE67_A = bytes.fromhex("6600" "47111A9008FD" "00000000" "E803" "0000" "0000")
BLE67_B = bytes.fromhex("6600" "31C9464FB3F9" "00000000" "E803" "0000" "0000")
ALL_ZERO = bytes.fromhex("6600" + "00" * 16)

BLE67_A_MAC = "FD:08:90:1A:11:47"
BLE67_B_MAC = "F9:B3:4F:46:C9:31"



def frame(payload: bytes) -> bytes:
    """把 18 字节负载装回 20 字节帧。"""
    return bytes([HEADER, FrameFlag.REGISTER]) + payload


async def _opened() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("dev")
    device = WT901Device(transport)
    device.registers.read_timeout = 0.05
    await device.open()
    return device, transport


class Responder:
    """盯着已发出的读指令，按地址表回**原始帧**。地址表里没有的一律不作答。

    与 ``test_telemetry`` 里的同名替身一样做成持续运行的后台任务：
    ``read_device_info`` 里故意不作答的项会真实消耗重试时间，跑固定轮数的应答器
    早就停了，后续读取也就拿不到答复——那测出来的失败是替身的，不是被测代码的。
    """

    def __init__(self, transport: MemoryTransport, table: dict[int, bytes]) -> None:
        self._transport = transport
        self._table = table
        self._answered = 0

    async def serve(self) -> None:
        while True:
            for command in self._transport.writes[self._answered :]:
                self._answered += 1
                if len(command) == 5 and command[2] == Register.READADDR:
                    payload = self._table.get(command[3])
                    if payload is not None:
                        self._transport.feed(frame(payload))
            await asyncio.sleep(0.001)


async def _run(
    device: WT901Device,
    transport: MemoryTransport,
    table: dict[int, bytes],
    coro: object,
) -> object:
    loop = asyncio.get_running_loop()
    serving = loop.create_task(Responder(transport, table).serve())
    try:
        return await loop.create_task(coro)  # type: ignore[arg-type]
    finally:
        serving.cancel()
        try:
            await serving
        except asyncio.CancelledError:
            pass


# ----- 验收标准 1：寄存器地址 ----------------------------------------------


def test_register_has_the_mac_address() -> None:
    assert Register.MAC == 0x66


def test_captured_payloads_are_whole_frames() -> None:
    """fixture 必须是完整的 18 字节负载，截短了后面的断言就没有意义。"""
    for payload in (BLE67_A, BLE67_B):
        assert len(payload) == FRAME_LENGTH - 2
        assert payload[0] == Register.MAC


# ----- 验收标准 2：具名读取，返回规范化字符串 --------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [(BLE67_A, BLE67_A_MAC), (BLE67_B, BLE67_B_MAC)],
)
async def test_read_mac_reverses_the_over_the_air_order(
    payload: bytes, expected: str
) -> None:
    """寄存器里是空口序（低位在前），显示顺序要整体倒过来。

    ``0xFD08`` 作 int16 是负数。按有符号拼字节会得到完全不同的地址，所以这条也
    同时钉住「寄存器是位模式不是有符号数」。
    """
    device, transport = await _opened()
    mac = await _run(
        device, transport, {Register.MAC: payload}, device.telemetry.read_mac()
    )

    assert mac == expected
    await device.close()


async def test_two_devices_get_different_macs() -> None:
    """两台同名设备必须读出不同的 MAC——否则这个字段根本不是身份。

    这正是 RAY-279 的全部目的：两台设备的广播名都是 ``WT901BLE67``，
    ``DiscoveredDevice.address`` 换台主机就变，序列号真机上读到过全零。
    """
    device_a, transport_a = await _opened()
    device_b, transport_b = await _opened()
    mac_a = await _run(
        device_a, transport_a, {Register.MAC: BLE67_A}, device_a.telemetry.read_mac()
    )
    mac_b = await _run(
        device_b, transport_b, {Register.MAC: BLE67_B}, device_b.telemetry.read_mac()
    )

    assert mac_a != mac_b
    await device_a.close()
    await device_b.close()


async def test_read_mac_issues_one_read_at_0x66() -> None:
    """MAC 占 3 个寄存器，一次读取（回 4 个）就够，不该像序列号那样读两次。"""
    device, transport = await _opened()
    await _run(device, transport, {Register.MAC: BLE67_A}, device.telemetry.read_mac())

    reads = [w for w in transport.writes if len(w) == 5 and w[2] == Register.READADDR]
    assert [w[3] for w in reads] == [Register.MAC]
    await device.close()


async def test_all_zero_response_is_refused_not_formatted() -> None:
    """全零不是地址。返回 ``00:00:00:00:00:00`` 会让每台设备拿到同一个绑定键。

    这不是假想：本器件的序列号寄存器就读到过逐字节全零（RAY-172），而那种失败
    是安静的——绑定照样建立，只是所有设备都指向同一条记录。
    """
    device, transport = await _opened()
    with pytest.raises(UnexpectedRegisterResponse):
        await _run(
            device, transport, {Register.MAC: ALL_ZERO}, device.telemetry.read_mac()
        )
    await device.close()


# ----- 验收标准 3：纳入 read_device_info 的逐项容错 --------------------------


async def test_device_info_carries_mac() -> None:
    device, transport = await _opened()
    info = await _run(
        device, transport, {Register.MAC: BLE67_B}, device.telemetry.read_device_info()
    )

    assert info.mac == BLE67_B_MAC  # type: ignore[attr-defined]
    await device.close()


async def test_device_info_survives_an_unreadable_mac() -> None:
    """MAC 读不到只让该字段为 ``None``，不影响其余项——与其余项同构。"""
    device, transport = await _opened()
    info = await _run(
        device,
        transport,
        {Register.TEMPERATURE: bytes.fromhex("4000" "700C" + "00" * 14)},
        device.telemetry.read_device_info(),
    )

    assert info.mac is None  # type: ignore[attr-defined]
    assert info.temperature_c == pytest.approx(31.84)  # type: ignore[attr-defined]
    await device.close()


async def test_device_info_mac_is_none_when_register_reads_all_zero() -> None:
    """全零的 MAC 在 ``DeviceInfo`` 里表现为「没读到」，而不是一个假身份。"""
    device, transport = await _opened()
    info = await _run(
        device, transport, {Register.MAC: ALL_ZERO}, device.telemetry.read_device_info()
    )

    assert info.mac is None  # type: ignore[attr-defined]
    await device.close()
