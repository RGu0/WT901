"""录制文件格式测试。

格式的价值全在「出问题时人能读懂」上，所以这里既验证往返一致，也逐条验证
非法输入报出的是**指向根因的错误**，而不是某个字段缺失引发的连锁异常。
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

from wt901.recording import (
    RECORDING_FORMAT,
    RECORDING_VERSION,
    RecordedChunk,
    Recording,
    RecordingWriter,
    read_recording,
    write_recording,
)


def _recording(*chunks: RecordedChunk) -> Recording:
    return Recording(
        device_id="left-shank",
        created_utc="2026-08-18T00:00:00+00:00",
        note="测试",
        chunks=chunks,
    )


# ----- 往返 ----------------------------------------------------------------


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    original = _recording(
        RecordedChunk(t=0.0, data=b"\x55\x61\x01\x02"),
        RecordedChunk(t=0.01, data=b"\x55\x61\x03\x04"),
    )
    path = tmp_path / "session.jsonl"
    write_recording(path, original)

    assert read_recording(path) == original


def test_file_is_line_oriented_and_greppable(tmp_path: Path) -> None:
    """录制要进仓库当基线，diff 的可读性是格式选择的理由，必须钉住。"""
    path = tmp_path / "session.jsonl"
    write_recording(path, _recording(RecordedChunk(t=0.0, data=b"\x55\x61")))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert f'"format":"{RECORDING_FORMAT}"' in lines[0]
    assert f'"version":{RECORDING_VERSION}' in lines[0]
    assert '"hex":"5561"' in lines[1]


def test_empty_recording_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    write_recording(path, _recording())

    restored = read_recording(path)
    assert restored.chunks == ()
    assert restored.duration == 0.0
    assert restored.total_bytes == 0


def test_duration_and_total_bytes() -> None:
    recording = _recording(
        RecordedChunk(t=0.0, data=b"\x00" * 20),
        RecordedChunk(t=1.5, data=b"\x00" * 20),
    )
    assert recording.duration == 1.5
    assert recording.total_bytes == 40


# ----- 写入器 --------------------------------------------------------------


class Ticks:
    """按序返回时刻，并记下被问了几次。"""

    def __init__(self, *values: float) -> None:
        self._values = iter(values)
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return next(self._values)


def test_first_chunk_defines_time_zero() -> None:
    """连接建立到第一帧之间的握手时间不该计入，否则回放开头会空等。

    实现方式就是「构造时根本不看时钟」，所以这里直接断言这件事——它比通过
    时刻数值去反推更不容易在重构中被悄悄改掉。
    """
    ticks = Ticks(100.5, 101.25)
    handle = StringIO()
    writer = RecordingWriter(
        handle,
        device_id="left-shank",
        clock=ticks,
        now_utc=lambda: datetime(2026, 8, 18, tzinfo=UTC),
    )

    assert ticks.calls == 0  # 握手期间不取时钟

    writer.write(b"\x55\x61")  # 这一刻定义为 t = 0
    writer.write(b"\x55\x61")

    lines = handle.getvalue().splitlines()
    assert '"t":0.0' in lines[1]
    assert '"t":0.75' in lines[2]
    assert writer.chunks_written == 2


def test_writer_closes_handle_on_context_exit() -> None:
    handle = StringIO()
    with RecordingWriter(handle, device_id="d", clock=lambda: 0.0):
        pass
    assert handle.closed


# ----- 非法输入 ------------------------------------------------------------


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="空的"):
        read_recording(path)


def test_foreign_format_is_named(tmp_path: Path) -> None:
    path = tmp_path / "other.jsonl"
    path.write_text('{"format":"something-else"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="不是 WT901 录制文件"):
        read_recording(path)


def test_unknown_version_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """按当前版本硬解会让错误推迟到某个字段缺失处，届时看不出根因是版本。"""
    path = tmp_path / "future.jsonl"
    path.write_text(
        f'{{"format":"{RECORDING_FORMAT}","version":99}}\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="版本 99"):
        read_recording(path)


def test_time_going_backwards_is_refused(tmp_path: Path) -> None:
    """回放会对负延迟直接不睡，时序静悄悄失真——必须在读取时就拒绝。"""
    path = tmp_path / "backwards.jsonl"
    path.write_text(
        f'{{"format":"{RECORDING_FORMAT}","version":{RECORDING_VERSION}}}\n'
        '{"t":1.0,"hex":"5561"}\n'
        '{"t":0.5,"hex":"5561"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="第 3 行的时刻 0.5 早于上一行 1.0"):
        read_recording(path)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('{"t":0.0,"hex":"zz"}', "hex 无法解析"),
        ('{"t":"soon","hex":"5561"}', "t 不是数字"),
        ('{"t":0.0,"hex":85}', "hex 不是字符串"),
        ("[1, 2]", "必须是 JSON 对象"),
        ("{not json}", "不是合法 JSON"),
    ],
)
def test_malformed_chunk_names_the_line_and_the_cause(
    tmp_path: Path, line: str, expected: str
) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        f'{{"format":"{RECORDING_FORMAT}","version":{RECORDING_VERSION}}}\n{line}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=expected) as caught:
        read_recording(path)
    assert "第 2 行" in str(caught.value)


def test_boolean_is_not_accepted_as_a_timestamp(tmp_path: Path) -> None:
    """bool 是 int 的子类，不加防护会让 True 变成 t = 1.0。"""
    path = tmp_path / "bool.jsonl"
    path.write_text(
        f'{{"format":"{RECORDING_FORMAT}","version":{RECORDING_VERSION}}}\n'
        '{"t":true,"hex":"5561"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="t 不是数字"):
        read_recording(path)
