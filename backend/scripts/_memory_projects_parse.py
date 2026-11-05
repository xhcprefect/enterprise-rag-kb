"""V 夹具的纯解析场景：Python (标准库 ast) 与 Markdown（纯实现）真实结构。

只用于合成目录里的受控文件，绝不解析/import 真实工作区。这里不声称 Tree-sitter
AST 通过：JS/TS/Vue 只有在对应包存在时才产符号，缺包一律标 ``fallback``（带行号
受限文本），不伪造 AST/符号——与 P 的 parser 显式可用性分支一致。

输出 dataclass 供夹具断言“真实行号 / 结构切块 / 语法错误 fallback”。
"""
from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SymbolHit:
    """一个真实解析出的符号（函数/类/标题/围栏）。"""

    name: str
    kind: str            # function/class/method/heading/code-fence/sfc-script...
    line: int            # 1-based 真实源行号
    col: int = 0


@dataclass(frozen=True)
class ParseOutcome:
    path: str
    language: str
    status: str          # parsed / fallback / error
    symbols: tuple[SymbolHit, ...] = ()
    fallback_reason: str = ""
    structure_blocks: tuple[tuple[int, int], ...] = ()   # (start_line, end_line) inclusive


def python_symbols(text: str) -> tuple[SymbolHit, ...]:
    """标准库 ast 解析：真实类/函数（含方法）+ 行号。语法错误抛 SyntaxError。"""
    tree = ast.parse(text)
    hits: list[SymbolHit] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hits.append(SymbolHit(node.name, "function", node.lineno, node.col_offset))
        elif isinstance(node, ast.ClassDef):
            hits.append(SymbolHit(node.name, "class", node.lineno, node.col_offset))
    return tuple(hits)


def parse_python(text: str, path: str = "synth.py") -> ParseOutcome:
    try:
        syms = python_symbols(text)
    except SyntaxError as exc:
        return ParseOutcome(path, "python", "error",
                            fallback_reason=f"syntax:{exc.lineno}")
    # 结构块：函数/类的体型（真实行号）
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ParseOutcome(path, "python", "error", fallback_reason="syntax")
    blocks = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            blocks.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    return ParseOutcome(path, "python", "parsed", symbols=syms, structure_blocks=tuple(blocks))


_MD_HEADING = __import__("re").compile(r"^(#{1,6})\s+(.*)$")
_MD_FENCE = __import__("re").compile(r"^```([a-zA-Z0-9_+-]*)\s*$")


def parse_markdown(text: str, path: str = "README.md") -> ParseOutcome:
    """纯实现：标题层级 + 代码围栏（真实行号/列），不做伪 AST。"""
    headers: list[SymbolHit] = []
    fences: list[tuple[int, int]] = []
    in_fence = False
    fence_start = 0
    fence_lang = "text"
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        fence = _MD_FENCE.match(line)
        if fence:
            if not in_fence:
                in_fence, fence_start, fence_lang = True, i, (fence.group(1) or "text")
                # 打开的围栏：记录语言作为结构符号（真实行号）
                headers.append(SymbolHit(fence_lang, "code-fence", fence_start, 0))
            else:
                in_fence = False
                fences.append((fence_start, i))
            continue
        m = _MD_HEADING.match(line)
        if m and not in_fence:
            level = len(m.group(1))
            headers.append(SymbolHit(m.group(2).strip(), "heading", i, level))
    status = "parsed"
    return ParseOutcome(path, "markdown", status,
                        symbols=tuple(headers), structure_blocks=tuple(fences))


def parse_vue_sfc(text: str, path: str = "Component.vue") -> ParseOutcome:
    """按 SFC 顶层块（<script>/<template>/<style>）切块，保真实原文件行偏移。

    script 块若为纯文本且含 `function */def` 之类，我们用行号定位，不伪造
    AST 符号；JS/TS 的符号化留给有 tree-sitter 的 P parser。
    """
    import re as _re
    block_re = _re.compile(r"<script[^>]*>(.*?)</script>|<template[^>]*>(.*?)</template>|<style[^>]*>(.*?)</style>", _re.S)
    symbols: list[SymbolHit] = []
    blocks: list[tuple[int, int]] = []
    lines = text.splitlines()
    for m in block_re.finditer(text):
        start = text.count("\n", 0, m.start()) + 1
        end = start + m.group(0).count("\n")
        blocks.append((start, end))
        tag = m.group(0).lstrip("<\n\t /")
        kind = tag.split(" ", 1)[0].split(">", 1)[0]
        symbols.append(SymbolHit(kind, "sfc-block", start, m.start()))
    return ParseOutcome(path, "vue", "parsed" if blocks else "fallback",
                        symbols=tuple(symbols), structure_blocks=tuple(blocks))


def has_parser_package(pkg: str) -> bool:
    """缺依赖检测：只探测，不安装。供夹具决定“AST 符号可用 or 走 fallback”。"""
    try:
        return importlib.util.find_spec(pkg) is not None
    except (ImportError, ValueError):
        return False


def detect_ts_support() -> tuple[str, str]:
    """TS/JS 语法层可用性：有 tree_sitter + grammar 则 'parsed'，否则 'fallback'。"""
    if has_parser_package("tree_sitter") and has_parser_package("tree_sitter_javascript"):
        return "parsed", ""
    return "fallback", "tree-sitter / grammar 未安装：TS/JS 走带行号受限文本，不做 AST 符号"


def parse_synthetic_tree(root: Path) -> dict:
    """对合成目录做只读解析，返回按相对路径的 ParseOutcome（用于夹具断言）。

    只读打开，绝不执行/import 项目代码。secret/忽略目录文件由上层（
    build_project_fixture 的 expected/excluded）负责排除，这里不索引它们。
    """
    result = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in {".env", "node_modules/dep.js", ".venv/pyvenv.cfg"}:
            continue  # 由 build_project_fixture 定义排除
        suffix = path.suffix.lower()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            result[rel] = ParseOutcome(rel, "unknown", "fallback", fallback_reason=type(exc).__name__)
            continue
        if suffix in {".py"}:
            result[rel] = parse_python(text, rel)
        elif suffix in {".md", ".markdown"}:
            result[rel] = parse_markdown(text, rel)
        elif suffix in {".vue"}:
            result[rel] = parse_vue_sfc(text, rel)
        elif suffix in {".ts", ".js", ".tsx", ".jsx"}:
            status, reason = detect_ts_support()
            result[rel] = ParseOutcome(rel, "typescript", status, fallback_reason=reason)
        else:
            result[rel] = ParseOutcome(rel, "text", "fallback", fallback_reason="unsupported-ext")
    return result
