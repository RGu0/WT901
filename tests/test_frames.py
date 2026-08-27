"""帧同步与解包测试。

协议没有校验和，帧同步是本库最容易出错也最难在真机上诊断的地方，所以这里穷举
字节流的切分方式。
"""

from __future__ import annotations

import pytest

from conftest import data_frame, register_frame
from wt901.errors import UnexpectedRegisterResponse
from wt901.protocol.frames import (
    FRAME_LENGTH,
    FrameDecoder,
    FrameFlag,
    decode_data_frame,
    decode_register_response,
)

COUNTS = (1, 2, 3, 4, 5, 6, 7, 8, 9)


def test_single_complete_frame() -> None:
    decoder = FrameDecoder()
    frames = decoder.feed(data_frame(COUNTS))
    assert len(frames) == 1
    assert frames[0].flag is FrameFlag.DATA
    assert decoder.resync_count == 0
    assert decoder.dropped_bytes == 0


def test_frame_split_across_two_feeds() -> None:
    decoder = FrameDecoder()
    raw = data_frame(COUNTS)
    assert decoder.feed(raw[:5]) == []
    assert decoder.pending_bytes == 5
    frames = decoder.feed(raw[5:])
    assert len(frames) == 1
    assert decode_data_frame(frames[0]) == COUNTS
    assert decoder.resync_count == 0


def test_frame_split_byte_by_byte() -> None:
    """最坏切分：每次只喂一个字节。"""
    decoder = FrameDecoder()
    raw = data_frame(COUNTS)
    produced = [frame for byte in raw for frame in decoder.feed(bytes([byte]))]
    assert len(produced) == 1
    assert decode_data_frame(produced[0]) == COUNTS
    assert decoder.dropped_bytes == 0


def test_multiple_frames_in_one_feed() -> None:
    """50 Hz 以上是打包传输，一次通知可能带多帧。"""
    decoder = FrameDecoder()
    second = (9, 8, 7, 6, 5, 4, 3, 2, 1)
    frames = decoder.feed(data_frame(COUNTS) + data_frame(second))
    assert len(frames) == 2
    assert decode_data_frame(frames[0]) == COUNTS
    assert decode_data_frame(frames[1]) == second
    assert decoder.resync_count == 0


def test_garbage_before_frame_is_counted() -> None:
    decoder = FrameDecoder()
    frames = decoder.feed(b"\x00\x01\x02" + data_frame(COUNTS))
    assert len(frames) == 1
    assert decoder.dropped_bytes == 3
    assert decoder.resync_count == 1


def test_header_followed_by_invalid_flag_resyncs() -> None:
    """0x55 后跟非法标志位：必须丢掉这个假帧头继续找。"""
    decoder = FrameDecoder()
    frames = decoder.feed(b"\x55\x00" + data_frame(COUNTS))
    assert len(frames) == 1
    assert decode_data_frame(frames[0]) == COUNTS
    assert decoder.dropped_bytes == 2
    assert decoder.resync_count == 1


def test_two_separate_garbage_runs_count_as_two_resyncs() -> None:
    """resync_count 数的是「次数」，dropped_bytes 数的是「字节」。"""
    decoder = FrameDecoder()
    frames = decoder.feed(
        b"\xaa" + data_frame(COUNTS) + b"\xaa\xbb" + data_frame(COUNTS)
    )
    assert len(frames) == 2
    assert decoder.resync_count == 2
    assert decoder.dropped_bytes == 3


def test_pure_garbage_is_fully_discarded() -> None:
    decoder = FrameDecoder()
    assert decoder.feed(b"\x00" * 64) == []
    assert decoder.dropped_bytes == 64
    assert decoder.resync_count == 1
    assert decoder.pending_bytes == 0


def test_trailing_header_byte_is_kept_not_dropped() -> None:
    """末尾的 0x55 可能是下一帧的开头，标志位还没到就不能判它有罪。"""
    decoder = FrameDecoder()
    assert decoder.feed(b"\x55") == []
    assert decoder.dropped_bytes == 0
    assert decoder.pending_bytes == 1
    frames = decoder.feed(data_frame(COUNTS)[1:])
    assert len(frames) == 1
    assert decoder.dropped_bytes == 0


def test_garbage_containing_header_byte() -> None:
    """垃圾里混着 0x55 时，find 必须继续推进而不是原地打转。"""
    decoder = FrameDecoder()
    frames = decoder.feed(b"\x55\x55\x55" + data_frame(COUNTS))
    assert len(frames) == 1
    assert decoder.dropped_bytes == 3


def test_truncated_frame_produces_one_corrupt_frame_then_resyncs() -> None:
    """记录一个**已知局限**，不是期望行为。

    帧头合法但内容被截断时，解码器会用后续字节把长度凑满并交出一个内容错位的
    帧。协议没有校验和，帧内无从发现这件事。这里断言的是：错位只波及一帧，之
    后解码器能自行恢复，且 resync_count 会把异常暴露出来。
    """
    decoder = FrameDecoder()
    truncated = data_frame(COUNTS)[: FRAME_LENGTH - 4]
    frames = decoder.feed(truncated + data_frame(COUNTS))
    assert len(frames) == 1
    assert decode_data_frame(frames[0]) != COUNTS
    assert decoder.resync_count == 1

    recovered = decoder.feed(data_frame(COUNTS))
    assert len(recovered) == 1
    assert decode_data_frame(recovered[0]) == COUNTS


def test_reset_clears_buffer_but_keeps_counters() -> None:
    decoder = FrameDecoder()
    decoder.feed(b"\xff" + data_frame(COUNTS)[:6])
    assert decoder.pending_bytes > 0
    decoder.reset()
    assert decoder.pending_bytes == 0
    assert decoder.dropped_bytes == 1
    frames = decoder.feed(data_frame(COUNTS))
    assert len(frames) == 1


def test_negative_counts_are_signed() -> None:
    decoder = FrameDecoder()
    counts = (-1, -32768, 32767, 0, -100, 100, -32768, 32767, 0)
    frames = decoder.feed(data_frame(counts))
    assert decode_data_frame(frames[0]) == counts


def test_register_response_decoding() -> None:
    """一帧带 8 个寄存器，后 4 个同样要解出来——真机上它们是真实数据。

    每个位置给一个互不相同的值：若解码器少读了、或某个位置串了位，断言会指出是
    哪一个，而不是笼统地「不相等」。
    """
    decoder = FrameDecoder()
    values = (10, -20, 30, 40, -50, 60, -70, 80)
    frames = decoder.feed(register_frame(0x3A, values))
    response = decode_register_response(frames[0])
    assert response.start_register == 0x3A
    assert response.values == values
    assert response.value_at(0x3A) == 10
    assert response.value_at(0x3C) == 30
    # 0x41 落在原先被丢弃的后半段里。
    assert response.value_at(0x41) == 80


def test_register_response_rejects_out_of_range_address() -> None:
    """越界时抛异常，而不是悄悄返回相邻寄存器的值。"""
    decoder = FrameDecoder()
    frames = decoder.feed(register_frame(0x3A, (1, 2, 3, 4, 5, 6, 7, 8)))
    response = decode_register_response(frames[0])
    with pytest.raises(UnexpectedRegisterResponse):
        response.value_at(0x42)  # 起始地址 + 8，刚好越界
    with pytest.raises(UnexpectedRegisterResponse):
        response.value_at(0x39)


def test_decoders_reject_wrong_flag() -> None:
    decoder = FrameDecoder()
    data, register = decoder.feed(
        data_frame(COUNTS) + register_frame(0x51, (1, 2, 3, 4, 5, 6, 7, 8))
    )
    with pytest.raises(UnexpectedRegisterResponse):
        decode_register_response(data)
    with pytest.raises(UnexpectedRegisterResponse):
        decode_data_frame(register)


def test_data_and_register_frames_interleave() -> None:
    """寄存器回读与实时数据流共用一条链路，必须能混在一起解。"""
    decoder = FrameDecoder()
    frames = decoder.feed(
        data_frame(COUNTS)
        + register_frame(0x51, (1, 2, 3, 4, 5, 6, 7, 8))
        + data_frame(COUNTS)
    )
    assert [frame.flag for frame in frames] == [
        FrameFlag.DATA,
        FrameFlag.REGISTER,
        FrameFlag.DATA,
    ]
    assert decoder.resync_count == 0
