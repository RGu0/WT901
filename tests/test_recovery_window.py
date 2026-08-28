"""连接就绪之前的窗口：样本被门控挡下，寄存器通道照常（RAY-311）。

这些路径此前一条都没有覆盖。既有的重连测试全都在配置重放**结束之后**才断言
样本，所以「重放进行中就有帧到达」这件事从来没被构造过——而那正是真机上必然
发生的：链路一恢复设备就在推，重放每条寄存器要花约 0.2 s。
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from conftest import register_frame, registers
from wt901.device import ConnectionState, ReconnectPolicy, WT901Device
from wt901.errors import ConnectionLostError
from wt901.protocol.frames import HEADER, FrameFlag, RegisterResponse
from wt901.protocol.registers import Register
from wt901.transport.memory import MemoryTransport

MOTIONLESS = (0, 0, 2048, 0, 0, 0, 0, 0, 0)


def data_frame() -> bytes:
    return bytes([HEADER, FrameFlag.DATA]) + struct.pack("<9h", *MOTIONLESS)


async def _reconnecting_device() -> tuple[WT901Device, MemoryTransport]:
    transport = MemoryTransport("left-shank")
    device = WT901Device(
        transport,
        auto_reconnect=True,
        reconnect_policy=ReconnectPolicy(initial_delay=0.01, max_delay=0.01),
    )
    await device.open()
    return device, transport


def _queued_samples(device: WT901Device) -> list[object]:
    """取出已入队的样本。

    不能用 ``samples()``：它会等下一个样本，而这些测试要断言的恰恰是「这里
    一个都没有」。
    """
    out: list[object] = []
    while not device._samples.empty():
        item = device._samples.get_nowait()
        if item is not None:
            out.append(item)
    return out


# ----- 重连窗口 ------------------------------------------------------------


async def test_frames_during_config_replay_are_dropped_and_counted() -> None:
    """重放期间到达的帧不进样本队列，计入 dropped_before_ready。"""
    device, transport = await _reconnecting_device()

    async def hook() -> None:
        # 重放跑在链路已恢复之后、CONNECTED 之前——设备此时正按它的上电配置推数据。
        for _ in range(5):
            transport.feed(data_frame())
            await asyncio.sleep(0.001)

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0.06)

    assert device.stats.dropped_before_ready == 5
    assert _queued_samples(device) == []
    # 帧确实到了，帧计数照记——被丢的是样本，不是「什么都没发生」。
    assert device.stats.frames == 5
    # 背压计数不受污染：没有任何消费者跟不上。
    assert device.stats.dropped_samples == 0
    assert device.stats.samples == 0
    await device.close()


async def test_first_sample_after_reconnect_is_seq_zero() -> None:
    """被丢的帧不推进 seq，否则 seq 的缺口会掺进「流还没开始」。"""
    device, transport = await _reconnecting_device()

    async def hook() -> None:
        for _ in range(7):
            transport.feed(data_frame())
            await asyncio.sleep(0.001)

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0.06)

    transport.feed(data_frame())
    (sample,) = _queued_samples(device)
    assert sample.seq == 0  # type: ignore[attr-defined]
    assert device.stats.dropped_before_ready == 7
    await device.close()


async def test_samples_flow_again_after_connected() -> None:
    """门控是暂时的：CONNECTED 之后一切照旧。"""
    device, transport = await _reconnecting_device()
    transport.drop()
    await asyncio.sleep(0.06)

    transport.feed(data_frame() * 3)
    assert len(_queued_samples(device)) == 3
    assert device.stats.dropped_before_ready == 0
    await device.close()


# ----- 首次 open() 的同一个窗口 --------------------------------------------


async def test_frames_during_open_are_dropped() -> None:
    """``connect()`` 一返回通知就已订阅，而 CONNECTED 还没发出。

    成因与重连路径相同，只是窗口小得多。同一个标志管两条路径。
    """

    class ChattyTransport(MemoryTransport):
        """一连上就开始推数据的设备——真机就是这样。"""

        async def connect(self) -> None:
            await super().connect()
            self.feed(data_frame() * 4)

    transport = ChattyTransport("eager")
    device = WT901Device(transport)
    await device.open()

    assert device.stats.dropped_before_ready == 4
    assert _queued_samples(device) == []

    transport.feed(data_frame())
    (sample,) = _queued_samples(device)
    assert sample.seq == 0  # type: ignore[attr-defined]
    await device.close()


# ----- 寄存器通道不受门控影响 ----------------------------------------------


async def test_register_frames_are_dispatched_before_ready() -> None:
    """挡住寄存器回帧会把这个修复变成新的死锁——重放本身就跑在这条链路上。"""
    received: list[RegisterResponse] = []

    class RegisterChattyTransport(MemoryTransport):
        async def connect(self) -> None:
            await super().connect()
            self.feed(register_frame(Register.TEMPERATURE, registers(2500)))

    transport = RegisterChattyTransport("eager")
    device = WT901Device(transport)
    device.on_register_response(received.append)
    await device.open()

    assert len(received) == 1
    assert received[0].start_register == Register.TEMPERATURE
    assert device.stats.register_frames == 1
    assert device.stats.dropped_before_ready == 0
    await device.close()


async def test_default_config_replay_survives_the_window() -> None:
    """端到端：默认接线（RegisterAccess.replay）在门控下仍能完成重放。

    ``replay()`` 靠 ``0x71`` 回帧确认写入是否落地。若门控误伤寄存器通道，重放
    会超时失败，这条测试会看到 CONFIG_REPLAY_FAILED 而不是 CONNECTED。
    """
    from wt901.protocol.registers import ReturnRate

    device, transport = await _reconnecting_device()
    await device.registers.set_output_rate(ReturnRate.HZ_50)
    assert len(device.registers.applied_writes) == 1

    transport.drop()
    await asyncio.sleep(0.6)  # 够跑完一条寄存器的重放

    assert device.is_connected
    assert device.stats.reconnects == 1
    await device.close()


# ----- 与 RAY-306 的失败路径不打架 ------------------------------------------


async def test_flag_stays_closed_when_config_replay_fails() -> None:
    """重放失败的那条连接从来没就绪过，它期间的帧一个都不该交付。"""
    device, transport = await _reconnecting_device()
    calls = 0

    async def hook() -> None:
        nonlocal calls
        calls += 1
        transport.feed(data_frame() * 2)
        if calls == 1:
            raise ConnectionLostError("重连后链路又掉了")

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0.1)

    assert calls == 2
    # 两次尝试各喂 2 帧：失败那次的 2 帧与成功那次 CONNECTED 之前的 2 帧，全丢。
    assert device.stats.dropped_before_ready == 4
    assert _queued_samples(device) == []

    # 标志没有泄漏：第二次成功之后照常交付。
    transport.feed(data_frame())
    (sample,) = _queued_samples(device)
    assert sample.seq == 0  # type: ignore[attr-defined]
    await device.close()


async def test_config_replay_failure_event_sequence_unchanged() -> None:
    """RAY-306 定下的事件序列不因门控而多一条或少一条。"""
    device, transport = await _reconnecting_device()
    calls = 0

    async def hook() -> None:
        nonlocal calls
        calls += 1
        transport.feed(data_frame())
        if calls == 1:
            raise ConnectionLostError("boom")

    device.on_reconnect(hook)
    transport.drop()
    await asyncio.sleep(0.1)

    states = []
    while not device._events.empty():
        event = device._events.get_nowait()
        if event is not None:
            states.append(event.state)
    assert states == [
        ConnectionState.CONNECTED,
        ConnectionState.DISCONNECTED,
        ConnectionState.RECONNECTING,
        ConnectionState.CONFIG_REPLAY_FAILED,
        ConnectionState.RECONNECTING,
        ConnectionState.CONNECTED,
    ]
    await device.close()


# ----- 契约 ----------------------------------------------------------------


async def test_dropped_before_ready_is_separate_from_dropped_samples() -> None:
    """两个计数说的是两件事，合并就等于把「流还没开始」当成「消费者跟不上」。"""
    class ChattyTransport(MemoryTransport):
        async def connect(self) -> None:
            await super().connect()
            self.feed(data_frame())  # 已订阅、CONNECTED 未发出：未就绪

    transport = ChattyTransport("tiny")
    device = WT901Device(transport, queue_size=2)
    await device.open()
    transport.feed(data_frame() * 4)  # 队列只有 2 格，溢出走背压

    stats = device.stats
    assert stats.dropped_before_ready == 1
    assert stats.dropped_samples == 2
    await device.close()


def test_stats_field_is_documented() -> None:
    """新增的公开字段必须自带说明，且明写与 dropped_samples 的区别。"""
    from wt901.device import DeviceStats

    doc = DeviceStats.__dict__["__doc__"]
    assert doc is not None
    source_doc = _field_doc("dropped_before_ready")
    assert "dropped_samples" in source_doc
    assert "丢弃" in source_doc


def _field_doc(name: str) -> str:
    """从源码里取字段后面那段字面量 docstring。

    数据类字段的 docstring 只有 Sphinx 读得到，运行时拿不到——照 RAY-309 在
    ``test_provenance.py`` 里定下的做法用 ``ast`` 解析。
    """
    import ast
    import inspect

    import wt901.device as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "DeviceStats":
            continue
        body = node.body
        for index, stmt in enumerate(body):
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == name
                and index + 1 < len(body)
            ):
                following = body[index + 1]
                if isinstance(following, ast.Expr) and isinstance(
                    following.value, ast.Constant
                ):
                    value = following.value.value
                    assert isinstance(value, str)
                    return value
    raise AssertionError(f"DeviceStats.{name} 没有字面量 docstring")


@pytest.mark.parametrize("attribute", ["dropped_before_ready"])
def test_stats_exposes_new_field(attribute: str) -> None:
    from wt901.device import DeviceStats

    assert hasattr(DeviceStats(), attribute)
