"""录制与回放的端到端测试。全部离线。

这是 CI 里唯一能把「字节 → 帧 → 样本」整条链路串起来跑的手段。
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from wt901.device import WT901Device
from wt901.errors import ConnectionLostError
from wt901.protocol.frames import HEADER, FrameFlag
from wt901.recording import RecordedChunk, Recording, read_recording
from wt901.transport.memory import MemoryTransport
from wt901.transport.recording import RecordingTransport
from wt901.transport.replay import ReplayTransport

MOTIONLESS = (0, 0, 2048, 0, 0, 0, 0, 0, 0)


def data_frame(counts: tuple[int, ...] = MOTIONLESS) -> bytes:
    return bytes([HEADER, FrameFlag.DATA]) + struct.pack("<9h", *counts)


def _recording(*chunks: RecordedChunk, device_id: str = "left-shank") -> Recording:
    return Recording(
        device_id=device_id,
        created_utc="2026-08-18T00:00:00+00:00",
        note="",
        chunks=chunks,
    )


async def _collect(device: WT901Device, count: int) -> list[object]:
    collected: list[object] = []
    async for sample in device.samples():
        collected.append(sample)
        if len(collected) == count:
            break
    return collected


# ----- 录制 ----------------------------------------------------------------


async def test_recording_transport_captures_bytes_and_passes_them_through(
    tmp_path: Path,
) -> None:
    inner = MemoryTransport("left-shank")
    path = tmp_path / "session.jsonl"
    ticks = iter([10.0, 10.02])
    transport = RecordingTransport.to_file(inner, path, clock=lambda: next(ticks))

    received: list[bytes] = []
    transport.on_data(received.append)
    async with transport:
        inner.feed(data_frame())
        inner.feed(data_frame())

    assert received == [data_frame(), data_frame()]  # 原样向上传递
    recording = read_recording(path)
    assert recording.device_id == "left-shank"
    assert [chunk.data for chunk in recording.chunks] == [data_frame(), data_frame()]
    assert recording.chunks[1].t == pytest.approx(0.02)


async def test_recording_survives_a_failing_disconnect(tmp_path: Path) -> None:
    """异常断连的那份录制往往最值得看，不能因为 disconnect 抛异常就丢掉。"""

    class Exploding(MemoryTransport):
        async def disconnect(self) -> None:
            raise ConnectionLostError("断连时炸了")

    inner = Exploding("left-shank")
    path = tmp_path / "session.jsonl"
    transport = RecordingTransport.to_file(inner, path, clock=lambda: 0.0)
    await transport.connect()
    inner.feed(data_frame())

    with pytest.raises(ConnectionLostError):
        await transport.disconnect()

    assert len(read_recording(path).chunks) == 1


async def test_record_then_replay_reproduces_the_sample_sequence(
    tmp_path: Path,
) -> None:
    """本 scope 的核心断言：回放产出的样本序列与录制时一致。"""
    counts = [
        (0, 0, 2048, 0, 0, 0, 0, 0, 0),
        (100, -200, 2048, 5, -5, 0, 10, 20, 30),
        (-32768, 32767, 0, 0, 0, 0, 0, 0, 0),
    ]

    inner = MemoryTransport("left-shank")
    path = tmp_path / "session.jsonl"
    ticks = iter([0.0, 0.0, 0.01, 0.02])
    recorder = RecordingTransport.to_file(inner, path, clock=lambda: next(ticks))
    recorded_device = WT901Device(recorder)
    await recorded_device.open()
    for count in counts:
        inner.feed(data_frame(count))
    recorded = await _collect(recorded_device, len(counts))
    await recorded_device.close()

    replayed_device = WT901Device(ReplayTransport.from_file(path, speed=None))
    await replayed_device.open()
    replayed = await _collect(replayed_device, len(counts))
    await replayed_device.close()

    # t_host 是主机接收时刻，回放时必然不同；其余每一项都必须一致。
    assert [s.raw for s in replayed] == [s.raw for s in recorded]  # type: ignore[attr-defined]
    assert [s.accel for s in replayed] == [s.accel for s in recorded]  # type: ignore[attr-defined]
    assert [s.gyro for s in replayed] == [s.gyro for s in recorded]  # type: ignore[attr-defined]
    assert [s.euler for s in replayed] == [s.euler for s in recorded]  # type: ignore[attr-defined]
    assert [s.seq for s in replayed] == [s.seq for s in recorded]  # type: ignore[attr-defined]


# ----- 回放 ----------------------------------------------------------------


async def test_replay_device_id_comes_from_the_recording() -> None:
    transport = ReplayTransport(_recording(device_id="right-shank"), speed=None)
    assert transport.device_id == "right-shank"


async def test_replay_device_id_can_be_overridden() -> None:
    """同一份录制要能扮演两台设备，多设备回放测试才不需要两份录制。"""
    transport = ReplayTransport(_recording(), speed=None, device_id="left")
    assert transport.device_id == "left"


async def test_replay_honours_original_timing() -> None:
    recording = _recording(
        RecordedChunk(t=0.0, data=data_frame()),
        RecordedChunk(t=0.05, data=data_frame()),
    )
    transport = ReplayTransport(recording, speed=1.0)
    device = WT901Device(transport)
    await device.open()

    collected = await _collect(device, 2)
    await device.close()

    first, second = collected
    assert second.t_host - first.t_host == pytest.approx(0.05, abs=0.03)  # type: ignore[attr-defined]


async def test_speed_none_does_not_wait() -> None:
    """CI 要验的是内容不是墙上时钟；10 秒的录制不该让测试跑 10 秒。"""
    recording = _recording(
        RecordedChunk(t=0.0, data=data_frame()),
        RecordedChunk(t=10.0, data=data_frame()),
    )
    device = WT901Device(ReplayTransport(recording, speed=None))
    await device.open()

    collected = await _collect(device, 2)
    await device.close()

    assert len(collected) == 2


async def test_speed_must_be_positive() -> None:
    with pytest.raises(ValueError, match="speed 必须为正数"):
        ReplayTransport(_recording(), speed=0)


async def test_wait_exhausted_reports_the_end_of_the_recording() -> None:
    transport = ReplayTransport(
        _recording(RecordedChunk(t=0.0, data=data_frame())), speed=None
    )
    device = WT901Device(transport)
    await device.open()
    await transport.wait_exhausted()

    assert transport.exhausted
    await device.close()


async def test_replay_can_signal_disconnect_at_the_end() -> None:
    """录制放完等于「链路结束」，需要它时得能触发设备层的断连路径。"""
    transport = ReplayTransport(
        _recording(RecordedChunk(t=0.0, data=data_frame())),
        speed=None,
        disconnect_at_end=True,
    )
    device = WT901Device(transport)
    await device.open()
    await transport.wait_exhausted()

    assert not transport.is_connected
    await device.close()


async def test_replay_records_downstream_writes_but_never_answers() -> None:
    """回放能验数据流，验不了请求/应答事务——这一点必须是可断言的行为。"""
    transport = ReplayTransport(_recording(), speed=None)
    await transport.connect()

    await transport.write(b"\xff\xaa\x27\x3a\x00")

    assert transport.writes == [b"\xff\xaa\x27\x3a\x00"]
    await transport.disconnect()


async def test_write_before_connect_is_refused() -> None:
    transport = ReplayTransport(_recording(), speed=None)
    with pytest.raises(ConnectionLostError):
        await transport.write(b"\x00")
