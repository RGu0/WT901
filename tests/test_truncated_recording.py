"""崩溃截断的录制文件：末行残行不该让此前完好的数据一起失效。

**fixture 是真实录制的前缀，不是手工构造的残行。** 手工写一个「看起来像被截断」
的字符串，测的是我们对截断的想象；把真机录制 `wt901-100hz.jsonl` 从任意字节位置
切开，得到的才是崩溃真正留下的东西（RAY-280）。

设计上有一条被依赖的性质：数据行 ``{"hex":"…","t":…}`` 的**任何真前缀都不是合法
JSON**，所以「末行 JSON 解析失败」既覆盖了全部截断情形，又不会顺带放过别的损坏。
它由 :func:`test_no_proper_prefix_of_a_chunk_line_is_valid_json` 钉住——那条性质
一旦不成立，容忍的边界就不再准确。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wt901.device import WT901Device
from wt901.recording import (
    RECORDING_FORMAT,
    RECORDING_VERSION,
    read_recording,
    write_recording,
)
from wt901.transport.replay import ReplayTransport

BASELINE = Path(__file__).parent / "data" / "recordings" / "wt901-100hz.jsonl"

HEADER = f'{{"format":"{RECORDING_FORMAT}","version":{RECORDING_VERSION}}}'

FRAME_LENGTH = 20
"""帧长。回放出来的样本数应当正好是救回字节数除以它。"""


def _truncate(tmp_path: Path, keep: int) -> Path:
    """把真机录制截到前 ``keep`` 个字节，模拟崩溃。"""
    path = tmp_path / "truncated.jsonl"
    path.write_bytes(BASELINE.read_bytes()[:keep])
    return path


def _mid_line_cut(raw: bytes) -> int:
    """找一个落在某行**中间**的字节位置——崩溃最常留下的那种。"""
    lines = raw.split(b"\n")
    # 头部 + 前 30 个数据行，再进第 31 行一半：足够多完好数据来证明它们没被牵连。
    prefix = b"\n".join(lines[:31]) + b"\n"
    return len(prefix) + len(lines[31]) // 2


# ----- 这条性质是整个容忍边界的依据 -----------------------------------------


def test_no_proper_prefix_of_a_chunk_line_is_valid_json() -> None:
    """截断只可能表现为 JSON 解析失败，不可能表现为「合法但语义不对的一行」。

    这条成立，才轮得到「只容忍末行的 JSON 解析失败」这个规则去覆盖全部截断情形。
    若哪天格式变了（比如 hex 换成裸数组），这里会先红。
    """
    line = BASELINE.read_text(encoding="utf-8").splitlines()[1]
    for cut in range(1, len(line)):
        with pytest.raises(json.JSONDecodeError):
            json.loads(line[:cut])


# ----- 末行截断 --------------------------------------------------------------


def test_strict_mode_still_loses_everything(tmp_path: Path) -> None:
    """默认行为不变——这正是 RAY-280 描述的现象，把它钉住。"""
    raw = BASELINE.read_bytes()
    path = _truncate(tmp_path, _mid_line_cut(raw))

    with pytest.raises(ValueError, match="不是合法 JSON"):
        read_recording(path)


def test_tolerant_mode_keeps_every_intact_chunk(tmp_path: Path) -> None:
    """残行之前的每一段都必须原样取回，且与完整读取逐段相同。"""
    raw = BASELINE.read_bytes()
    path = _truncate(tmp_path, _mid_line_cut(raw))

    salvaged = read_recording(path, tolerate_truncated_tail=True)
    complete = read_recording(BASELINE)

    assert salvaged.truncated
    assert len(salvaged.chunks) == 30
    assert salvaged.chunks == complete.chunks[:30]
    assert salvaged.device_id == complete.device_id
    assert salvaged.note == complete.note


def test_tolerance_reports_itself(tmp_path: Path) -> None:
    """安静地少几行比读不出来更糟：容忍必须可观测。"""
    raw = BASELINE.read_bytes()
    path = _truncate(tmp_path, _mid_line_cut(raw))

    assert read_recording(path, tolerate_truncated_tail=True).truncated is True
    assert read_recording(BASELINE, tolerate_truncated_tail=True).truncated is False


def test_cut_on_a_line_boundary_is_not_truncation(tmp_path: Path) -> None:
    """截断恰好落在换行处时文件本身是完好的——不该报截断，严格模式也该成功。"""
    raw = BASELINE.read_bytes()
    boundary = len(b"\n".join(raw.split(b"\n")[:31])) + 1
    path = _truncate(tmp_path, boundary)

    strict = read_recording(path)
    assert not strict.truncated
    assert len(strict.chunks) == 30
    assert read_recording(path, tolerate_truncated_tail=True) == strict


async def test_salvaged_recording_still_replays(tmp_path: Path) -> None:
    """救回来的数据要**能用**，否则这个改动只是让读取不报错而已。

    整条链路跑一遍：救回的录制 → 回放传输层 → 设备层 → 样本。每 20 字节一帧、
    每帧一个样本，所以样本数必须正好等于救回的字节数除以帧长——少一个都说明救回
    的字节在某处错位了。

    这也顺带说明：容忍模式不需要 ``ReplayTransport`` 再开一个开关，读出来的
    ``Recording`` 直接交给它的构造函数即可。
    """
    raw = BASELINE.read_bytes()
    salvaged = read_recording(
        _truncate(tmp_path, _mid_line_cut(raw)), tolerate_truncated_tail=True
    )
    assert salvaged.total_bytes % FRAME_LENGTH == 0  # 完好的部分仍是整帧

    transport = ReplayTransport(salvaged, speed=None)
    device = WT901Device(transport)
    await device.open()
    collected = []
    async for sample in device.samples():
        collected.append(sample)
        if transport.exhausted and device.pending_samples == 0:
            break
    await device.close()

    assert len(collected) == salvaged.total_bytes // FRAME_LENGTH
    assert device.stats.resync_count == 0
    assert device.stats.dropped_bytes == 0


# ----- 容忍不该扩大到别的损坏 -------------------------------------------------


def test_middle_line_damage_is_refused_in_both_modes(tmp_path: Path) -> None:
    """中间行损坏说明文件被改过或拼接过，与崩溃无关，两种模式都拒绝。"""
    path = tmp_path / "middle.jsonl"
    path.write_text(
        f"{HEADER}\n"
        '{"t":0.0,"hex":"5561"}\n'
        "{写坏了\n"
        '{"t":0.2,"hex":"5561"}\n',
        encoding="utf-8",
    )

    for tolerate in (False, True):
        with pytest.raises(ValueError, match="第 3 行"):
            read_recording(path, tolerate_truncated_tail=tolerate)


def test_time_going_backwards_on_the_last_line_is_still_refused(
    tmp_path: Path,
) -> None:
    """末行合法 JSON 但时刻倒退，是语义损坏而不是截断——容忍模式也必须拒绝。

    这是容忍边界最要紧的一条：只放过「解析不了」，不放过「解析得了但不对」。
    """
    path = tmp_path / "backwards.jsonl"
    path.write_text(
        f"{HEADER}\n" '{"t":1.0,"hex":"5561"}\n' '{"t":0.5,"hex":"5561"}\n',
        encoding="utf-8",
    )

    for tolerate in (False, True):
        with pytest.raises(ValueError, match="第 3 行的时刻 0.5 早于上一行 1.0"):
            read_recording(path, tolerate_truncated_tail=tolerate)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('{"t":0.5,"hex":"zz"}', "hex 无法解析"),
        ('{"t":"soon","hex":"5561"}', "t 不是数字"),
        ('{"t":0.5,"hex":85}', "hex 不是字符串"),
        ("[1, 2]", "必须是 JSON 对象"),
    ],
)
def test_last_line_that_parses_but_is_wrong_is_still_refused(
    tmp_path: Path, line: str, expected: str
) -> None:
    """末行能解析出 JSON，就说明它没被截断——它是坏的，两种模式都该拒绝。"""
    path = tmp_path / "bad-tail.jsonl"
    path.write_text(f"{HEADER}\n" '{"t":0.0,"hex":"5561"}\n' f"{line}\n", encoding="utf-8")

    for tolerate in (False, True):
        with pytest.raises(ValueError, match=expected):
            read_recording(path, tolerate_truncated_tail=tolerate)


# ----- 边界 ------------------------------------------------------------------


def test_empty_file_is_refused_in_both_modes(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")

    for tolerate in (False, True):
        with pytest.raises(ValueError, match="空的"):
            read_recording(path, tolerate_truncated_tail=tolerate)


def test_header_only_is_a_valid_empty_recording(tmp_path: Path) -> None:
    """写了头部就崩溃：文件是完好的，只是没有数据。不是截断。"""
    path = tmp_path / "header-only.jsonl"
    path.write_text(f"{HEADER}\n", encoding="utf-8")

    for tolerate in (False, True):
        recording = read_recording(path, tolerate_truncated_tail=tolerate)
        assert recording.chunks == ()
        assert not recording.truncated
        assert recording.duration == 0.0


def test_truncated_header_is_refused_in_both_modes(tmp_path: Path) -> None:
    """头部自己被截断时，连「是不是 WT901 录制」都无从判断，没有可救的数据。"""
    path = _truncate(tmp_path, 40)

    for tolerate in (False, True):
        with pytest.raises(ValueError, match="头部不是合法 JSON"):
            read_recording(path, tolerate_truncated_tail=tolerate)


def test_header_cut_inside_a_multibyte_character_is_refused_too(
    tmp_path: Path,
) -> None:
    """头部的 note 含中文，切在多字节字符中间时拒绝来自解码而不是 JSON 解析。

    抛的是 ``UnicodeDecodeError``——它是 ``ValueError`` 的子类，所以按 ``ValueError``
    接的调用方拿到的行为一致。这条钉住的是「头部截断两种模式都拒绝」这句话对**两种
    失败路径**都成立，而不是只对其中好看的那条。数据行只含十六进制与数字，走不到
    这条路上。
    """
    header = BASELINE.read_bytes().split(b"\n")[0]
    first_multibyte = next(i for i, byte in enumerate(header) if byte >= 0x80)
    path = tmp_path / "utf8-cut.jsonl"
    path.write_bytes(header[: first_multibyte + 1])

    for tolerate in (False, True):
        with pytest.raises(ValueError):
            read_recording(path, tolerate_truncated_tail=tolerate)


# ----- truncated 的语义 -------------------------------------------------------


def test_truncated_describes_the_source_file_not_the_data(tmp_path: Path) -> None:
    """把救回来的数据写出去，得到的是一份完好的文件——再读回来不该还说截断。"""
    raw = BASELINE.read_bytes()
    salvaged = read_recording(
        _truncate(tmp_path, _mid_line_cut(raw)), tolerate_truncated_tail=True
    )

    rewritten = tmp_path / "rewritten.jsonl"
    write_recording(rewritten, salvaged)
    reread = read_recording(rewritten)

    assert salvaged.truncated
    assert not reread.truncated
    assert reread.chunks == salvaged.chunks
