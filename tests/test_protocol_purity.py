"""守住协议层的零 I/O 约束。

这条约束是整个库可测试性的地基：协议没有校验和，帧同步与换算是最容易出错的
地方，只有把它们与传输实现彻底分开，才能在没有硬件的情况下完整测试。一旦有人
在 protocol 包里 import 了 bleak 或 asyncio，这个包就再也不能离线跑了，而且退
化是渐进的、不会有任何一条测试变红——所以要有一条测试专门盯着它。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import wt901.protocol

FORBIDDEN_ROOTS = frozenset({"asyncio", "bleak", "serial", "socket", "threading"})

PROTOCOL_DIR = Path(wt901.protocol.__file__).parent


def _protocol_sources() -> list[Path]:
    sources = sorted(PROTOCOL_DIR.glob("*.py"))
    assert sources, "协议层源码没找到，测试本身失效了"
    return sources


def _imported_roots(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("source", _protocol_sources(), ids=lambda path: path.name)
def test_protocol_module_has_no_io_dependency(source: Path) -> None:
    offenders = _imported_roots(source) & FORBIDDEN_ROOTS
    assert not offenders, f"{source.name} 引入了传输层依赖：{sorted(offenders)}"
