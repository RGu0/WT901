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

**两个读取入口，同一段解析代码。** :func:`read_recording` 一次拿到完整
:class:`Recording`，是绝大多数场景要的；:func:`open_recording` 逐 chunk 产出，峰值
内存与文件大小无关（一份 30 分钟 200 Hz 的单设备录制约 14 MB）。前者建立在后者之上，
所以两者对末行截断与中间行损坏的判定不可能分叉。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
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
    "RecordingHeader",
    "RecordingReader",
    "RecordingWriter",
    "open_recording",
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
    truncated: bool = False
    """来源文件的最后一行是残行，已被丢弃——见
    :func:`read_recording` 的 ``tolerate_truncated_tail``。

    **它描述的是这次读取的来源文件，不是这份数据本身**，因此不进文件格式：把这份
    录制写回文件得到的是一份完好的文件，再读回来 ``truncated`` 就是 ``False``。
    ``write_recording`` 也不会保留它。

    丢掉的是多少数据无从得知——残行本身就是坏的。能确定的只有「有东西没了」，
    所以拿到 ``True`` 时应当把该次会话标记为不完整，而不是当作正常数据用。
    """

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


def _decode_json_line(number: int, line: str) -> Any:
    """只做 JSON 解析。**单独拆出来是有意的**：崩溃截断只会让这一步失败，而
    :func:`read_recording` 的 ``tolerate_truncated_tail`` 只容忍这一步的失败。
    字段校验放在 :func:`_chunk_from_record`，那里的失败任何模式下都不容忍。
    """
    try:
        return json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"录制文件第 {number} 行不是合法 JSON：{error}") from error


def _chunk_from_record(number: int, record: Any, previous: float) -> RecordedChunk:
    """校验一条数据记录并转成 :class:`RecordedChunk`。``previous`` 是上一段的时刻。"""
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
    return RecordedChunk(t=t, data=data)


def _numbered_lines(handle: IO[str]) -> Iterator[tuple[int, str]]:
    """逐行产出 ``(文件行号, 去掉行尾的内容)``，跳过空行。

    行号是**文件里的真实行号**。此前的实现按过滤后列表的下标编号，中间有空行时
    报出来的号会偏——空行只会来自手工编辑，但报错的意义就在于让人去看那一行。
    """
    for number, raw in enumerate(handle, start=1):
        line = raw.rstrip("\r\n")
        if line.strip():
            yield number, line


@dataclass(frozen=True, slots=True)
class RecordingHeader:
    """录制文件的头部。

    与 :class:`Recording` 的前三个字段重复是**有意的**：``Recording`` 的形状是公开
    契约，把它拆成「头部 + 数据」会让每个现有调用方都要改。这里只是给流式路径一个
    不必先读完整份文件就能拿到的头部。
    """

    device_id: str
    created_utc: str
    note: str


class RecordingReader:
    """流式读取一份录制：头部在打开时就有，数据行在迭代时才逐行读。

    由 :func:`open_recording` 构造。**只能迭代一次**——底层文件句柄是一次性的，
    第二次迭代抛 :class:`RuntimeError` 而不是静默产出空序列。

    :func:`read_recording` 就建立在它之上，所以两条路径对截断与损坏的判定**不可能
    分叉**：它们跑的是同一段代码，而不是两份用测试比对的实现。
    """

    __slots__ = ("_consumed", "_handle", "_lines", "_tolerate", "_truncated", "header")

    def __init__(
        self,
        handle: IO[str],
        header: RecordingHeader,
        lines: Iterator[tuple[int, str]],
        *,
        tolerate_truncated_tail: bool,
    ) -> None:
        self._handle = handle
        self._lines = lines
        self._tolerate = tolerate_truncated_tail
        self._truncated: bool | None = None
        self._consumed = False
        self.header = header
        """头部信息，打开时即可用，不需要读完整份文件。"""

    @property
    def truncated(self) -> bool | None:
        """来源文件的末行是不是残行。**迭代完成之前为** ``None``。

        流式读取在读到文件尾之前无从知道末行完不完整，所以这里不能给 ``False``
        ——那是把「还不知道」说成「没截断」。与 :attr:`Recording.truncated` 是
        ``bool`` 不冲突：那个值是在整份读完之后才构造出来的。

        判据与 :func:`read_recording` 相同，见它的文档。
        """
        return self._truncated

    def __iter__(self) -> Iterator[RecordedChunk]:
        if self._consumed:
            raise RuntimeError("RecordingReader 只能迭代一次；要重读请重新 open_recording")
        self._consumed = True
        return self._chunks()

    def _chunks(self) -> Iterator[RecordedChunk]:
        # 向前看一行：截断只可能发生在**最后**一行，而流式读取要到下一行取不出来
        # 时才知道刚才那行是最后一行。所以手里始终压着一行不产出。
        previous = 0.0
        held: tuple[int, str] | None = None
        for item in self._lines:
            if held is not None:
                number, line = held
                chunk = _chunk_from_record(number, _decode_json_line(number, line), previous)
                previous = chunk.t
                yield chunk
            held = item

        if held is None:
            self._truncated = False
            return

        number, line = held
        try:
            record = _decode_json_line(number, line)
        except ValueError:
            if self._tolerate:
                # 崩溃截断只可能留下这一种痕迹：写到一半的最后一行。丢掉它，
                # 前面完好的部分照常产出。见 read_recording 的文档。
                self._truncated = True
                return
            raise
        self._truncated = False
        yield _chunk_from_record(number, record, previous)

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


def open_recording(
    path: Path, *, tolerate_truncated_tail: bool = False
) -> RecordingReader:
    """打开一份录制做**流式**读取：峰值内存与文件大小无关。

    头部在这里就解析完，所以「不是 WT901 录制文件」「版本不认识」这类错误在打开时
    就抛出，而不是等到迭代到某一行。数据行一行也没读。

    ::

        with open_recording(path) as reader:
            print(reader.header.device_id)
            for chunk in reader:
                ...

    ``tolerate_truncated_tail`` 的语义与 :func:`read_recording` 完全一致——两者跑的
    是同一段解析代码。迭代结束后看 :attr:`RecordingReader.truncated`。

    **不是尾随读取。** 它读的是一份已经写完的文件；一份**正在被写入**的文件在这里
    会表现为「末行是残行」，而下次再读时那一行可能已经完整了。要跟着写入方一起读
    是另一件事，涉及「读到一半的行下次要重读」这类语义，本函数不提供。
    """
    handle = path.open(encoding="utf-8")
    try:
        lines = _numbered_lines(handle)
        first = next(lines, None)
        if first is None:
            raise ValueError("录制文件是空的")
        raw_header = _parse_header(first[1])
        header = RecordingHeader(
            device_id=str(raw_header.get("device_id", "")),
            created_utc=str(raw_header.get("created_utc", "")),
            note=str(raw_header.get("note", "")),
        )
    except BaseException:
        handle.close()
        raise
    return RecordingReader(
        handle, header, lines, tolerate_truncated_tail=tolerate_truncated_tail
    )


def read_recording(path: Path, *, tolerate_truncated_tail: bool = False) -> Recording:
    """读取录制文件。格式不合法时抛 :class:`ValueError`，并指明行号。

    ``tolerate_truncated_tail=True`` 时，**只有最后一行**的「写到一半」会被容忍：
    丢掉那一行，返回此前全部完好的数据，并把 :attr:`Recording.truncated` 置为
    ``True``。

    **为什么需要它。** 进程被 ``kill -9``、掉电、或写到一半被打断时，文件的最后
    一行必然是残行。严格解析会让此前**全部完好的数据一起变成不可读**——真机上
    一份 30 分钟 200 Hz 的录制截断后，前 633 行完好，一行都取不出来（RAY-280）。
    而采集轮次中途被打断是常态，不是例外。

    **为什么默认仍然是严格的。** 静默容忍会把「这份文件坏了」变成一个没人注意到
    的事实。要容忍就得明写出来，拿到结果还要看 ``truncated`` 才知道发生了什么。

    **为什么只容忍最后一行、且只容忍 JSON 解析失败。** 中间行损坏说明文件被改过
    或拼接过，那与崩溃无关，照旧拒绝并指明行号；时刻倒退的检查同样保留，末行也
    不例外。而崩溃截断在这个格式下**只可能**表现为末行的 JSON 解析失败：数据行
    形如 ``{"hex":"…","t":…}``，它的任何一个真前缀都不是合法 JSON（有测试钉住这
    一点）。所以「末行 JSON 解析失败」既覆盖了全部截断情形，又不会顺带放过别的
    损坏。截断恰好落在行边界时文件本身是完好的，不需要容忍，也不会报截断。

    头部自身被截断不在容忍范围内：那种文件连「是不是 WT901 录制」都无从判断，
    两种模式都拒绝。头部里的 ``note`` / ``device_id`` 可能含非 ASCII，切在多字节
    字符中间时拒绝来自解码而不是 JSON 解析，抛的是 ``UnicodeDecodeError``——它是
    :class:`ValueError` 的子类，按 ``ValueError`` 接的调用方不受影响。数据行只含
    十六进制与数字，不会走到这条路上。
    """
    with open_recording(
        path, tolerate_truncated_tail=tolerate_truncated_tail
    ) as reader:
        chunks = tuple(reader)
        truncated = reader.truncated is True
        header = reader.header
    return Recording(
        device_id=header.device_id,
        created_utc=header.created_utc,
        note=header.note,
        chunks=chunks,
        truncated=truncated,
    )
