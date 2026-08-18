"""指令字节构造测试。

每条指令都与官方 SDK 源码里出现的字节序列逐字节比对——那是唯一权威，注释里的
「等价写法」正是官方自己给出的。
"""

from __future__ import annotations

import pytest

from wt901.errors import ConfigurationError
from wt901.protocol.commands import (
    calibrate_acceleration,
    end_field_calibration,
    read_register,
    save,
    set_bandwidth,
    set_return_rate,
    start_field_calibration,
    unlock,
    write_register,
)
from wt901.protocol.registers import Bandwidth, Register, ReturnRate


def test_unlock_matches_official_bytes() -> None:
    assert unlock() == bytes.fromhex("ffaa6988b5")


def test_save_matches_official_bytes() -> None:
    assert save() == bytes.fromhex("ffaa000000")


@pytest.mark.parametrize(
    ("register", "expected"),
    [
        (Register.HX, "ffaa273a00"),
        (Register.Q0, "ffaa275100"),
        (Register.TEMPERATURE, "ffaa274000"),
        (Register.POWER, "ffaa276400"),
        (Register.MAGTYPE, "ffaa277200"),
        (Register.SERIAL_NUMBER, "ffaa277f00"),
        (Register.VERSION_LOW, "ffaa272e00"),
        (Register.CHIP_TIME_YEAR_MONTH, "ffaa273000"),
        (Register.RRATE, "ffaa270300"),
    ],
)
def test_read_register_matches_official_bytes(register: int, expected: str) -> None:
    assert read_register(register) == bytes.fromhex(expected)


def test_calibration_commands_match_official_bytes() -> None:
    assert calibrate_acceleration() == bytes.fromhex("ffaa010100")
    assert start_field_calibration() == bytes.fromhex("ffaa010700")
    assert end_field_calibration() == bytes.fromhex("ffaa010000")


def test_return_rate_commands_match_official_bytes() -> None:
    assert set_return_rate(ReturnRate.HZ_10) == bytes.fromhex("ffaa030600")
    assert set_return_rate(ReturnRate.HZ_50) == bytes.fromhex("ffaa030800")


def test_bandwidth_commands_match_official_bytes() -> None:
    assert set_bandwidth(Bandwidth.HZ_20) == bytes.fromhex("ffaa1f0400")
    assert set_bandwidth(Bandwidth.HZ_256) == bytes.fromhex("ffaa1f0000")


def test_write_register_is_little_endian() -> None:
    assert write_register(0x69, 0xB588) == bytes.fromhex("ffaa6988b5")
    assert write_register(0x03, 0x0006) == bytes.fromhex("ffaa030600")


def test_every_command_is_five_bytes() -> None:
    """指令定长 5 字节；变长会让设备把两条指令拼成一条解读。"""
    commands = [
        unlock(),
        save(),
        read_register(Register.HX),
        calibrate_acceleration(),
        start_field_calibration(),
        end_field_calibration(),
        set_return_rate(ReturnRate.HZ_50),
        set_bandwidth(Bandwidth.HZ_256),
    ]
    assert all(len(command) == 5 for command in commands)


@pytest.mark.parametrize("register", [-1, 0x100, 0x1000])
def test_out_of_range_register_is_rejected(register: int) -> None:
    with pytest.raises(ConfigurationError):
        write_register(register, 0)
    with pytest.raises(ConfigurationError):
        read_register(register)


@pytest.mark.parametrize("value", [0x10000, -0x8001])
def test_out_of_range_value_is_rejected(value: int) -> None:
    with pytest.raises(ConfigurationError):
        write_register(Register.RRATE, value)


def test_only_verified_rate_and_bandwidth_values_exist() -> None:
    """未在真机核实的档位不进枚举——写入未知编码可能让设备进入未知状态。

    速率档位于 2026-08-18 在 WT901BLE67 上逐档实测（`tools/probe_rates.py`）。
    `0x0A` 被有意排除：通用编码表标它 125 Hz，实测却是 99.29 Hz，与 `0x09` 几乎
    相同；同一次探测里 `0x0B` 跑到 198 Hz，所以不是链路带宽不足。
    """
    assert {int(rate) for rate in ReturnRate} == {0x06, 0x07, 0x08, 0x09, 0x0B}
    assert 0x0A not in {int(rate) for rate in ReturnRate}
    # 带宽尚未逐档探测，仍只开放官方示例演示过的两档。
    assert {int(bandwidth) for bandwidth in Bandwidth} == {0x00, 0x04}


def test_verified_rate_commands_match_expected_bytes() -> None:
    assert set_return_rate(ReturnRate.HZ_20) == bytes.fromhex("ffaa030700")
    assert set_return_rate(ReturnRate.HZ_100) == bytes.fromhex("ffaa030900")
    assert set_return_rate(ReturnRate.HZ_200) == bytes.fromhex("ffaa030b00")
