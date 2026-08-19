"""录制文件的格式、读取与写入。

**这是测试设施，不是对外功能。** 它存在的唯一理由是：本库的价值集中在设备层
与协议层的交互上，而这段交互在 CI 里没有硬件可跑。录一次真机字节流下来，就能
用 :class:`~wt901.transport.replay.ReplayTransport` 在没有硬件的机器上端到端地
驱动整条链路。

格式是 JSON Lines，不是二进制：

* 第一行是头部对象，后续每行是一段收到的字节。
* 录制文件要进仓库当回归基线，而基线的价值在于**出问题时人能读懂它**。
  二进制文件在 code review 里是一团乱码，在 git 历史里每次改动都是整文件替换。

::

    {"format":"wt901-recording","version":1,"device_id":"...","created_utc":"...","note":"..."}
    {"t":0.0,"hex":"5561..."}
    {"t":0.01,"hex":"5561..."}

``t`` 是相对第一段字节的秒数，不是绝对时间：绝对时间跨机器没有意义，而回放只
关心相对时序。字节用十六进制字符串而不是 base64，同样是为了可读——20 字节的帧
用十六进制一眼能看出 ``5561`` 开头。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self

__all__ = [
    "RECORDING_FORMAT",
    "RECORDING_VERSION",
    "RecordedChunk",
    "Recording",
    "RecordingWriter",
    "read_recording",
    "write_recording",
]

RECORDING_FORMAT = "wt901-recording"
RECORDING_VERSION = 1


@dataclass(frozen=True, slots=True)
class RecordedChunk:
    """一次传输层回调收到的字节，连同它到达的相对时刻。"""

    t: float
    """相对录制开始的秒数。"""
    data: bytes


@dataclass(frozen=True, slots=True)
class Recording:
    """一份录制。"""

    device_id: str
    created_utc: str
    note: str
    chunks: tuple[RecordedChunk, ...]

    @property
    def duration(self) -> float:
        """最后一段字节的相对时刻。空录制为 ``0.0``。"""
        return self.chunks[-1].t if self.chunks else 0.0

    @property
    def total_bytes(self) -> int:
        return sum(len(chunk.data) for chunk in self.chunks)


def _header(device_id: str, created_utc: str, note: str) -> dict[str, Any]:
    return {
        "format": RECORDING_FORMAT,
        "version": RECORDING_VERSION,
        "device_id": device_id,
        "created_utc": created_utc,
        "note": note,
    }


def _dump(payload: dict[str, Any]) -> str:
    # sort_keys 让同样的内容产生同样的字节，录制文件在 git 里才有稳定的 diff。
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RecordingWriter:
    """按到达顺序把字节写成录制文件。

    第一段字节到达的时刻定义为 ``t = 0``，而不是构造这个对象的时刻：连接建立与
    第一帧到达之间隔着一段与数据无关的握手时间，把它计进去会让回放在开头空等。
    """

    __slots__ = ("_clock", "_handle", "_start", "_written")

    def __init__(
        self,
        handle: IO[str],
        *,
        device_id: str,
        note: str = "",
        clock: Callable[[], float],
        now_utc: Callable[[], datetime] | None = None,
    ) -> None:
        self._handle = handle
        self._clock = clock
        self._start: float | None = None
        self._written = 0
        stamp = (now_utc or (lambda: datetime.now(UTC)))().isoformat()
        handle.write(_dump(_header(device_id, stamp, note)) + "\n")

    @property
    def chunks_written(self) -> int:
        return self._written

    def write(self, data: bytes) -> None:
        """记下一段字节。"""
        now = self._clock()
        if self._start is None:
            self._start = now
        self._handle.write(
            _dump({"t": round(now - self._start, 6), "hex": data.hex()}) + "\n"
        )
        self._written += 1

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def write_recording(path: Path, recording: Recording) -> None:
    """把一份完整录制写到文件。"""
    lines = [_dump(_header(recording.device_id, recording.created_utc, recording.note))]
    lines.extend(
        _dump({"t": round(chunk.t, 6), "hex": chunk.data.hex()})
        for chunk in recording.chunks
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_header(line: str) -> dict[str, Any]:
    try:
        header = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"录制文件的头部不是合法 JSON：{error}") from error
    if not isinstance(header, dict):
        raise ValueError("录制文件的头部必须是 JSON 对象")  # noqa: TRY004 - 文件内容非法属于 ValueError，不是调用方传错了类型
    if header.get("format") != RECORDING_FORMAT:
        raise ValueError(f"不是 WT901 录制文件：format = {header.get('format')!r}")
    version = header.get("version")
    if version != RECORDING_VERSION:
        # 版本不认识时明说，不要按当前版本去解析——那样错误会推迟到某个字段
        # 缺失的地方才爆出来，届时已经看不出根因是版本。
        raise ValueError(
            f"录制文件版本 {version!r} 无法识别，本库支持 {RECORDING_VERSION}"
        )
    return header


def _parse_chunks(lines: Iterable[tuple[int, str]]) -> tuple[RecordedChunk, ...]:
    chunks: list[RecordedChunk] = []
    previous = 0.0
    for number, line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"录制文件第 {number} 行不是合法 JSON：{error}") from error
        if not isinstance(record, dict):
            raise ValueError(f"录制文件第 {number} 行必须是 JSON 对象")  # noqa: TRY004 - 文件内容非法属于 ValueError，不是调用方传错了类型
        raw_t, raw_hex = record.get("t"), record.get("hex")
        if not isinstance(raw_t, int | float) or isinstance(raw_t, bool):
            raise ValueError(f"录制文件第 {number} 行的 t 不是数字")  # noqa: TRY004 - 文件内容非法属于 ValueError，不是调用方传错了类型
        if not isinstance(raw_hex, str):
            raise ValueError(f"录制文件第 {number} 行的 hex 不是字符串")  # noqa: TRY004 - 文件内容非法属于 ValueError，不是调用方传错了类型
        try:
            data = bytes.fromhex(raw_hex)
        except ValueError as error:
            raise ValueError(f"录制文件第 {number} 行的 hex 无法解析：{error}") from error
        t = float(raw_t)
        if t < previous:
            # 时刻倒退意味着文件被手工改过或拼接过。回放会照着睡负数的时间，
            # 也就是不睡，于是时序静悄悄地失真——宁可在这里就拒绝。
            raise ValueError(f"录制文件第 {number} 行的时刻 {t} 早于上一行 {previous}")
        previous = t
        chunks.append(RecordedChunk(t=t, data=data))
    return tuple(chunks)


def read_recording(path: Path) -> Recording:
    """读取录制文件。格式不合法时抛 :class:`ValueError`，并指明行号。"""
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("录制文件是空的")
    header = _parse_header(lines[0])
    chunks = _parse_chunks(enumerate(lines[1:], start=2))
    return Recording(
        device_id=str(header.get("device_id", "")),
        created_utc=str(header.get("created_utc", "")),
        note=str(header.get("note", "")),
        chunks=chunks,
    )
