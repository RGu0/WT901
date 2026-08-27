"""录制文件的流式读取入口。全部离线。

`read_recording` 把整份文件读进内存：峰值里同时存在整份文本、切出来的行列表、
以及最终的 chunks 元组——同一份数据的三种表示。一份 30 分钟 200 Hz 的单设备录制
约 14 MB（RAY-200 的压测文件）。

**本文件的核心不是「结果一样」，是「真的没整份读」。** 只断言结果一样的测试，对
一个偷偷 `read_text()` 再逐个 yield 的实现同样会通过——而那种实现满足不了本 Issue
的任何一条。所以这里用一个会记账的文件对象，直接量「取到第一个 chunk 时读了多少」。

一致性那一半反过来不靠测试保证：`read_recording` 现在**建立在**流式路径之上，两者
跑的是同一段解析代码，判定不可能分叉。下面的对照测试是那个结构的守卫，不是它的
替代品。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Any, cast

import pytest

from wt901.recording import (
    RecordedChunk,
    Recording,
    open_recording,
    read_recording,
    write_recording,
)

BASELINE = Path(__file__).parent / "data" / "recordings" / "wt901-100hz.jsonl"

NOTE_WITH_LINE_SEPARATOR = "上\u2028下"
"""U+2028 LINE SEPARATOR。`str.splitlines()` 在它上面断行，文件的换行符不会。"""


def _recording(count: int, *, note: str = "") -> Recording:
    return Recording(
        device_id="dev",
        created_utc="2026-08-27T00:00:00+00:00",
        note=note,
        chunks=tuple(
            RecordedChunk(t=index * 0.01, data=bytes([0x55, 0x61, index % 256]))
            for index in range(count)
        ),
    )


def _write(tmp_path: Path, recording: Recording) -> Path:
    path = tmp_path / "recording.jsonl"
    write_recording(path, recording)
    return path


def _truncate_tail(path: Path) -> None:
    """把末行砍掉一半，模拟 kill -9 / 掉电留下的残行。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1][: len(lines[-1]) // 2]
    # 故意不补末尾换行：截断的文件不会有。
    path.write_text("\n".join(lines), encoding="utf-8")


class _CountingHandle:
    """包着真实句柄，记下逐行读过去的字符数与是否被关闭。"""

    def __init__(self, handle: IO[str], ledger: dict[str, Any]) -> None:
        self._handle = handle
        self._ledger = ledger

    def __iter__(self) -> Any:
        for line in self._handle:
            self._ledger["chars"] += len(line)
            yield line

    def close(self) -> None:
        self._ledger["closed"] = True
        self._handle.close()


class _CountingPath:
    """只实现 `open()` 的 Path 替身。

    做成替身而不是给 `open_recording` 加一个注入点：被测的正是「它怎么读文件」，
    为了测试在生产代码上开一个口子，测到的就变成那个口子了。

    ``read_text`` / ``read_bytes`` **故意做成会炸的**。没有它们，一个退回
    ``path.read_text()`` 的实现会绕开这个替身：账本停在 0，而「已读远小于文件」
    这个断言在 0 上恰好成立——防线会变成一条永远通过的空断言。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.ledger: dict[str, Any] = {"chars": 0, "closed": False}

    def open(self, *args: Any, **kwargs: Any) -> Any:
        return _CountingHandle(self._path.open(*args, **kwargs), self.ledger)

    def read_text(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("整份读入：流式入口不该调用 read_text()")

    def read_bytes(self) -> bytes:
        raise AssertionError("整份读入：流式入口不该调用 read_bytes()")


def _counting(path: Path) -> tuple[Path, dict[str, Any]]:
    spy = _CountingPath(path)
    return cast("Path", spy), spy.ledger


def _chars(path: Path) -> int:
    """文件的字符数。与记账口径一致——句柄是文本模式，数的是字符不是字节。"""
    return len(path.read_text(encoding="utf-8"))


# ----- 验收标准 3：证明它真的没整份读 ----------------------------------------


def test_first_chunk_arrives_before_the_file_is_read(tmp_path: Path) -> None:
    """取到第一个 chunk 时，读进来的量必须远小于整份文件。

    这是本 Issue 的实质要求。量「已读字符数」而不是内存峰值：峰值受 GC 与分配器
    影响，噪声大，而且一个先 `read_text()` 再逐个 yield 的实现在小文件上照样能
    「看起来」峰值很低。已读量是直接证据。
    """
    path = _write(tmp_path, _recording(5000))
    total = _chars(path)
    assert total > 100_000, "样本要显著大于单行，否则这条测试什么也证明不了"

    counted, ledger = _counting(path)
    with open_recording(counted) as reader:
        first = next(iter(reader))

    assert first.t == 0.0
    # 向前看一行，所以此刻读到的是头部 + 前两个数据行，量级几百字符。
    assert 0 < ledger["chars"] < total // 100, "下界防的是替身根本没被用上"


def test_header_alone_reads_almost_nothing(tmp_path: Path) -> None:
    """头部要能单独拿到——否则调用方为了 device_id 又得整份读一遍。"""
    path = _write(tmp_path, _recording(5000, note="基线"))
    total = _chars(path)

    counted, ledger = _counting(path)
    with open_recording(counted) as reader:
        header = reader.header

    assert header.device_id == "dev"
    assert header.note == "基线"
    assert 0 < ledger["chars"] < total // 100, "下界防的是替身根本没被用上"


def test_iterating_fully_reads_everything_once(tmp_path: Path) -> None:
    """完整迭代当然要读完整份——流式不等于少读数据。"""
    path = _write(tmp_path, _recording(200))
    counted, ledger = _counting(path)
    with open_recording(counted) as reader:
        chunks = list(reader)

    assert len(chunks) == 200
    assert ledger["chars"] == _chars(path)


def test_the_handle_is_closed_on_exit(tmp_path: Path) -> None:
    path = _write(tmp_path, _recording(5))
    counted, ledger = _counting(path)
    with open_recording(counted) as reader:
        list(reader)
        assert ledger["closed"] is False
    assert ledger["closed"] is True


def test_the_handle_is_closed_even_if_the_header_is_bad(tmp_path: Path) -> None:
    """打开失败也不能漏句柄。"""
    path = tmp_path / "foreign.jsonl"
    path.write_text('{"format":"something-else"}\n', encoding="utf-8")
    counted, ledger = _counting(path)

    with pytest.raises(ValueError, match="不是 WT901 录制文件"):
        open_recording(counted)
    assert ledger["closed"] is True


# ----- 验收标准 2：两条路径的判定必须一致 ------------------------------------


def test_streaming_and_full_read_agree_on_the_baseline() -> None:
    """真机基线：逐 chunk 与整份读出来的必须逐个相等。"""
    full = read_recording(BASELINE)
    with open_recording(BASELINE) as reader:
        assert reader.header.device_id == full.device_id
        assert reader.header.created_utc == full.created_utc
        assert reader.header.note == full.note
        streamed = list(reader)
    assert streamed == list(full.chunks)


def test_streaming_reports_the_same_truncation(tmp_path: Path) -> None:
    """末行残行：两条路径都容忍，且丢掉的是同一行。"""
    path = _write(tmp_path, _recording(30))
    _truncate_tail(path)

    full = read_recording(path, tolerate_truncated_tail=True)
    with open_recording(path, tolerate_truncated_tail=True) as reader:
        streamed = list(reader)
        assert reader.truncated is True

    assert full.truncated is True
    assert streamed == list(full.chunks)
    assert len(streamed) == 29


def test_streaming_refuses_a_truncated_tail_by_default(tmp_path: Path) -> None:
    """默认严格，与 read_recording 一致。"""
    path = _write(tmp_path, _recording(30))
    _truncate_tail(path)

    with pytest.raises(ValueError, match="不是合法 JSON"), open_recording(path) as reader:
        list(reader)


def test_streaming_still_refuses_a_damaged_middle_line(tmp_path: Path) -> None:
    """中间行损坏说明文件被改过或拼接过，容忍模式下也照旧拒绝，并指明行号。"""
    path = _write(tmp_path, _recording(10))
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[3] = "{坏了"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with (
        pytest.raises(ValueError, match="第 4 行"),
        open_recording(path, tolerate_truncated_tail=True) as reader,
    ):
        list(reader)


def test_streaming_still_refuses_time_going_backwards(tmp_path: Path) -> None:
    """末行时刻倒退是损坏而不是截断，容忍模式下也拒绝——与 read_recording 同。"""
    path = _write(tmp_path, _recording(5))
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = json.dumps({"hex": "5561", "t": 0.0}, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with (
        pytest.raises(ValueError, match="早于上一行"),
        open_recording(path, tolerate_truncated_tail=True) as reader,
    ):
        list(reader)


def test_header_errors_surface_at_open_not_at_iteration(tmp_path: Path) -> None:
    """「不是 WT901 录制」这类判断不该等到迭代到某一行才说。"""
    path = tmp_path / "foreign.jsonl"
    path.write_text('{"format":"something-else"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="不是 WT901 录制文件"):
        open_recording(path)


def test_empty_file_is_refused_at_open(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="空的"):
        open_recording(path)


# ----- truncated 的三态 -------------------------------------------------------


def test_truncated_is_none_until_the_end_is_reached(tmp_path: Path) -> None:
    """迭代完成之前不知道末行完不完整，所以是 ``None`` 而不是 ``False``。

    给 ``False`` 就是把「还不知道」说成「没截断」——RAY-293（序列号空串）与
    RAY-305（`ChipTime` / `Quaternion`）立的是同一条线。
    """
    path = _write(tmp_path, _recording(50))
    with open_recording(path) as reader:
        assert reader.truncated is None
        chunks = iter(reader)
        next(chunks)
        assert reader.truncated is None, "才读了一个 chunk，末行还没见到"
        list(chunks)
        assert reader.truncated is False


def test_a_reader_can_only_be_iterated_once(tmp_path: Path) -> None:
    """句柄是一次性的。第二次迭代要报错，而不是静默产出空序列。"""
    path = _write(tmp_path, _recording(5))
    with open_recording(path) as reader:
        assert len(list(reader)) == 5
        with pytest.raises(RuntimeError, match="只能迭代一次"):
            list(reader)


# ----- 行分隔符 ---------------------------------------------------------------


def test_a_note_containing_a_line_separator_no_longer_splits_the_line(
    tmp_path: Path,
) -> None:
    """``note`` 里的 U+2028 不再被当成换行。

    头部用 ``ensure_ascii=False`` 写出，所以 U+2028（LINE SEPARATOR）原样落进文件。
    `str.splitlines()` 在它上面断行，而 JSON Lines 的行分隔符只有换行——此前的整份
    读入用 `splitlines()`，这种 note 会把头部劈成两半、后半段再被当成数据行。改成
    按文件行读之后不会了。

    这是重构顺带修掉的一处潜在缺陷，不是本 Issue 的目标；钉在这里免得又退回去。
    """
    path = _write(tmp_path, _recording(3, note="上 下"))

    with open_recording(path) as reader:
        assert reader.header.note == "上 下"
        assert len(list(reader)) == 3

    full = read_recording(path)
    assert full.note == "上 下"
    assert len(full.chunks) == 3
