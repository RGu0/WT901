"""包与公开契约的测试。

这些断言守的是「下游能不能正常依赖本库」，与设备行为无关。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import wt901

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
MINIMAL_CAPTURE_LINE_BUDGET = 30


def test_version_is_exposed() -> None:
    assert wt901.__version__ == "0.1.0"


def test_py_typed_marker_ships_with_the_package() -> None:
    """没有这个文件，下游的 mypy 会把整个库当成无类型，静悄悄地什么都不检查。"""
    assert (Path(wt901.__file__).parent / "py.typed").is_file()


def test_every_name_in_all_actually_exists() -> None:
    missing = [name for name in wt901.__all__ if not hasattr(wt901, name)]
    assert not missing


def test_all_is_sorted() -> None:
    """排序不是洁癖：无序的 __all__ 会让每次增删都产生难读的 diff。"""
    assert list(wt901.__all__) == sorted(wt901.__all__)


def test_all_has_no_duplicates() -> None:
    assert len(wt901.__all__) == len(set(wt901.__all__))


@pytest.mark.parametrize(
    "module",
    [
        "wt901.calibration",
        "wt901.config",
        "wt901.device",
        "wt901.discovery",
        "wt901.errors",
        "wt901.models",
        "wt901.multi",
        "wt901.telemetry",
    ],
)
def test_submodule_public_names_are_re_exported(module: str) -> None:
    """子模块 __all__ 里的类型必须在顶层可见。

    下游要给 ``device.telemetry.read_battery()`` 的返回值写标注，就需要
    ``Battery`` 这个名字。它曾经只存在于 ``wt901.telemetry`` 里而没有再导出——
    于是下游要么去 import 一个不属于公开契约的模块路径，要么放弃标注。

    常量（``DEFAULT_*``）不在此列：它们出现在函数签名的默认值里，不需要被 import。
    """
    names = importlib.import_module(module).__all__
    missing = [
        name
        for name in names
        if not name.startswith("DEFAULT_") and not hasattr(wt901, name)
    ]
    assert not missing


# ----- 示例 ----------------------------------------------------------------


def _examples() -> list[Path]:
    return sorted(EXAMPLES.glob("*.py"))


def test_three_examples_are_present() -> None:
    assert [path.name for path in _examples()] == [
        "configure_and_calibrate.py",
        "minimal_capture.py",
        "two_devices.py",
    ]


@pytest.mark.parametrize("path", _examples(), ids=lambda p: p.name)
def test_example_parses(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("path", _examples(), ids=lambda p: p.name)
def test_example_only_imports_the_public_namespace(path: Path) -> None:
    """示例是公开 API 的门面。若它得从 ``wt901.device`` 里捞东西，说明再导出漏了。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    inner = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("wt901.")
    ]
    assert not inner


def test_minimal_capture_fits_the_line_budget() -> None:
    """验收标准要求 ≤30 行完成 扫描 → 连接 → 100 Hz → 打印带时间戳样本。

    钉住行数是为了让它保持「最小」：一个越长越全的最小示例就不是最小示例了。
    """
    source = (EXAMPLES / "minimal_capture.py").read_text(encoding="utf-8")
    assert len(source.splitlines()) <= MINIMAL_CAPTURE_LINE_BUDGET


def test_minimal_capture_covers_the_whole_path() -> None:
    source = (EXAMPLES / "minimal_capture.py").read_text(encoding="utf-8")
    for needle in ("scan(", "WT901Device.connect", "HZ_100", "t_host"):
        assert needle in source


# ----- 文档 ----------------------------------------------------------------


def test_protocol_reference_is_present() -> None:
    """协议基线沉淀进仓库，后续维护者不必重新考据官方 SDK。"""
    text = (ROOT / "docs" / "protocol.md").read_text(encoding="utf-8")
    assert "0x72" in text          # 磁场量纲类型
    assert "0x0A" in text          # 被排除的速率编码
    assert "上游资料缺陷清单" in text


def test_changelog_is_present() -> None:
    assert (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").find("0.1.0") > 0
