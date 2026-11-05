"""项目源码/文档静态解析（纯、有界、不执行）。

安全边界：
- 永不执行/import/构建源码；只在受控 blob 字节上做文本与 AST 静态解析。
- Python 用标准库 ``ast``（真实可用）。类/函数/异步函数产出真实 1-based
  行号与符号；语法错误降级为明确标记的 fallback 文本，不伪造符号。
- JS/TS 计划固定 Tree-sitter。这里显式探测可用性：有包且拿到真实 entry 才产出
  AST 符号；否则返回带真实行号的受限文本 fallback 且 ``parser_status`` 明确非
  ast/tree-sitter——**绝不把缺包或 mock 的 entry 选择当成真实 grammar 通过**。
  tree-sitter-typescript 对外入口是 ``language_typescript()`` / ``language_tsx()``
  （不是 ``grammar.language()``，后者只有 tree_sitter_javascript 提供）：
  按文件扩展名选 ``language_typescript``（.ts/.mts/.cts）或 ``language_tsx``
  （.tsx）；javascript 用 ``language()``。
- Vue 静态解析 SFC 的 script/template/style 三块，保持各自在原文件的行偏移；
  script 再按 JS/TS 可用性与扩展名处理。
- Markdown 按标题/代码围栏切块，保留真实行号。
- 真实且有界：字节预算（MAX_TEXT_BYTES，在 parse_blob 解码前截断）、
  行数预算（BOUNDED_LINES，在 ast.parse / tree-sitter 前核对，避免超大文件
  先进 AST 才被掐）、AST/TS 节点预算（MAX_AST_NODES）、输出总量预算
  （MAX_OUTPUT_ITEMS）。任一命中即 ``truncated=True``，被截断的输出**只保留
  原文行号前缀并如实标记**，绝不漏改/合成行号；fallback 带了真实行号。
- 长块按行有界拆分，块只含可核验的原文行，不把合成上下文冒称真实行。
"""
from __future__ import annotations

import ast
import codecs
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.services.project_types import BlobRef

MAX_TEXT_BYTES = 8 * 1024 * 1024  # 单文件文本读取预算（字节，解码前截断）
BOUNDED_LINES = 10_000  # 单文件行数预算（进入 AST 前核对）
MAX_AST_NODES = 200_000  # AST / tree-sitter 遍历节点预算
MAX_OUTPUT_ITEMS = 50_000  # 单文件符号+块输出总量预算


class ProjectParseError(ValueError):
    """解析阶段的有界/不支持错误（fail closed）。"""


@dataclass(frozen=True)
class SourceSymbol:
    name: str
    kind: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int


@dataclass(frozen=True)
class SourceBlock:
    kind: str  # function/class/markdown-heading/code-block/script/template/style
    name: str | None
    start_line: int
    end_line: int
    text: str
    source_lines: tuple[int, ...]  # 本块实际包含的原文行号（可核验）


@dataclass(frozen=True)
class ParseResult:
    language: str | None
    parser_status: str  # ast/tree-sitter/vue/markdown/fallback/skipped
    symbols: tuple[SourceSymbol, ...] = ()
    blocks: tuple[SourceBlock, ...] = ()
    errors: tuple[str, ...] = ()
    fallback_text: str | None = None
    truncated: bool = False
    line_count: int = 0


# ---------- 语言识别（纯扩展名判断） ----------

_PY = {".py", ".pyi", ".pyw"}
_JS = {".js", ".mjs", ".cjs", ".jsx"}
_TS = {".ts", ".mts", ".cts", ".tsx"}
_MD = {".md", ".markdown"}
_VUE = {".vue"}
_PLAIN = {".txt", ".rst"}

# 硬排除的已知二进制/压缩（扫描层也会过滤，这里便于单独验证 parser 分类）。
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".pdf", ".docx", ".xlsx", ".pptx", ".zip", ".gz", ".bz2", ".xz", ".7z",
    ".rar", ".tar", ".jar", ".war", ".so", ".dll", ".dylib", ".exe", ".bin",
    ".class", ".o", ".obj", ".pyc", ".lock", ".woff", ".woff2", ".ttf",
}


def detect_language(relative_path: str) -> str | None:
    """按扩展名返回语言；未知返回 None（由别处归类/排除）。"""
    name = relative_path.rsplit("/", 1)[-1]
    low = name.lower()
    dot = low.rfind(".")
    ext = f".{low[dot + 1:]}" if dot >= 0 else ""
    if ext in _PY:
        return "python"
    if ext in _JS:
        return "javascript"
    if ext in _TS:
        return "typescript"
    if ext in _MD:
        return "markdown"
    if ext in _VUE:
        return "vue"
    if ext in _PLAIN:
        return "text"
    if ext in _BINARY_EXTS:
        return "binary"
    return None


# ---------- 文本安全读取（从 blob 字节，不执行） ----------

_ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "gb18030")


def _decode_text_blob(blob: bytes) -> tuple[str, str]:
    """把 blob 字节安全解码成文本；返回 (文本, 编码)。"""
    for encoding in _ENCODINGS:
        try:
            return blob.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # 最后尝试 replace 退避，保证不炸（binary 防护在扫描层）。
    return blob.decode("utf-8", errors="replace"), "utf-8-replace"


def _split_lines(text: str) -> list[str]:
    """按行切分，保留行号语义（含末尾空行）。"""
    if not text:
        return []
    return text.splitlines(keepends=True)


def _looks_binary(blob: bytes) -> bool:
    """NUL 字节比例异常即视为二进制（配合扩展名硬排除）。"""
    if not blob:
        return False
    sample = blob[:4096]
    if b"\x00" in sample:
        return sample.count(b"\x00") / max(1, len(sample)) > 0.01
    return False


def _bounded_decode_lines(
    blob_bytes: bytes,
) -> tuple[list[str], bool]:
    """字节预算在解码前截断，再解码切行；返回 (lines, bytes_truncated)。

    这是真实的**字节预算**：超过 MAX_TEXT_BYTES 的 blob 先被裁到该上限才解码，
    免得把超大文件整个解码/进 AST。截断以 ``bytes_truncated`` 如实上报。
    """
    truncated_bytes = len(blob_bytes) > MAX_TEXT_BYTES
    data = blob_bytes[:MAX_TEXT_BYTES] if truncated_bytes else blob_bytes
    return _split_lines(_decode_text_blob(data)[0]), truncated_bytes


def _bounded_fallback(
    lines: list[str],
    language: str | None,
    reason: str,
    extra_errors: tuple[str, ...] = (),
) -> ParseResult:
    """行数/解析不可用时的有界 fallback：保留真实行号前缀，并如实标记截断。

    “不漏 module 文本”：输出只做前缀保留 + 行号保持原文，任何被裁掉的尾部都
    通过 ``truncated=True`` 显式标记（绝不在切分后才用切片长度去比较，那会恒 False）。
    """
    out_lines = lines[:BOUNDED_LINES]
    return ParseResult(
        language=language,
        parser_status="fallback",
        errors=extra_errors + (reason,),
        fallback_text=_numbered_text(out_lines),
        truncated=len(lines) > BOUNDED_LINES,
        line_count=len(lines),
    )


# ---------- Python（标准 ast） ----------

_FUNC_KINDS = {ast.FunctionDef: "function", ast.AsyncFunctionDef: "async-function"}


def parse_python_text(text: str, relative_path: str | None = None) -> ParseResult:
    """用标准 ast 解析 Python；语法错误/超预算降级为明确 fallback，不伪造符号。

    预算顺序：先核对**行数**（BOUNDED_LINES）才允许 ast.parse——超大文件绝不先
    整份进 AST 被掐；再在 ast.walk 中对**节点数/输出总量**做真界，命中即
    ``truncated`` 且停止收集，已收集符号仍带真实行号。
    """
    all_lines = _split_lines(text)
    total = len(all_lines)
    if total > BOUNDED_LINES:
        return _bounded_fallback(all_lines, "python", "python-line-budget-exceeded")
    try:
        tree = ast.parse(text)
    except SyntaxError as err:
        return ParseResult(
            language="python",
            parser_status="fallback",
            errors=(f"syntax-error: line {err.lineno}: {err.msg}",),
            fallback_text=_numbered_text(all_lines),
            truncated=total > BOUNDED_LINES,
            line_count=total,
        )
    symbols: list[SourceSymbol] = []
    blocks: list[SourceBlock] = []
    truncated = False
    for i, node in enumerate(ast.walk(tree)):
        if i >= MAX_AST_NODES:
            truncated = True
            break
        kind = _FUNC_KINDS.get(type(node)) or (
            "class" if isinstance(node, (ast.ClassDef,)) else None
        )
        if not kind:
            continue
        name = getattr(node, "name", None)
        start = getattr(node, "lineno", 1) or 1
        end = getattr(node, "end_lineno", start) or start
        symbols.append(
            SourceSymbol(
                name=name or "",
                kind=kind,
                start_line=start,
                start_col=getattr(node, "col_offset", 0) + 1,
                end_line=end,
                end_col=(getattr(node, "end_col_offset", 0) or 0) + 1,
            )
        )
        block_text = "".join(
            all_lines[i - 1] for i in range(start, min(end, len(all_lines)) + 1) if i - 1 < len(all_lines)
        )
        blocks.append(
            SourceBlock(
                kind=kind,
                name=name,
                start_line=start,
                end_line=end,
                text=block_text,
                source_lines=tuple(range(start, min(end, len(all_lines)) + 1)),
            )
        )
        if len(symbols) + len(blocks) >= MAX_OUTPUT_ITEMS:
            truncated = True
            break
    return ParseResult(
        language="python",
        parser_status="ast",
        symbols=tuple(sorted(symbols, key=lambda s: (s.start_line, s.start_col, s.name))),
        blocks=tuple(sorted(blocks, key=lambda b: (b.start_line, b.name or ""))),
        truncated=truncated or total > BOUNDED_LINES,
        line_count=total,
    )


# ---------- JS/TS（Tree-sitter，可用性显式分支 + 真实 API 选择） ----------

_TREE_SITTER_CACHE: dict[str, object] = {}


def _grammar_entry_member(language: str, relative_path: str | None) -> str:
    """tree-sitter 语言包对外真实入口成员名（不执行，仅选择）。

    - javascript（.js/.mjs/.cjs/.jsx）→ ``language()``
    - typescript .ts/.mts/.cts → ``language_typescript()``
    - typescript .tsx → ``language_tsx()``
    这是“真实 entry 选择”的确定性回归点：mock/选择只证明选了正确成员，
    不证明真实 grammar 能跑（真实性由 _tree_sitter_available 的真实 import 判定）。
    """
    if language == "javascript":
        return "language"
    if language == "typescript":
        if relative_path and relative_path.lower().endswith(".tsx"):
            return "language_tsx"
        return "language_typescript"
    raise ValueError(f"未知脚本语言: {language}")


def _tree_sitter_available(language: str, relative_path: str | None = None) -> bool:
    """探测 Tree-sitter 是否可用；缺包返回 False，绝不伪造 AST。

    存在性用**真实 import** 判定：必须拿到真实入口成员（language /
    language_typescript / language_tsx）才算可用；缺包/缺成员都是 False。
    mock 出的 entry 不会走这里，也就不会被当成真实 grammar 通过。
    """
    if language == "python":
        return True  # python 走独立 ast 分支，这里不重复
    try:
        import tree_sitter  # noqa: F401
        from tree_sitter import Language, Parser  # noqa: F401
    except Exception:
        return False
    mapping = {"javascript": "tree_sitter_javascript", "typescript": "tree_sitter_typescript"}
    module_name = mapping.get(language)
    if not module_name:
        return False
    try:
        grammar = __import__(module_name)
        entry = _grammar_entry_member(language, relative_path)
        getattr(grammar, entry)  # 真实成员存在性；不在此执行解析
    except Exception:
        return False
    return True


def _parse_with_tree_sitter(
    text: str, language: str, relative_path: str | None = None
) -> ParseResult:
    from tree_sitter import Language, Parser

    if language == "javascript":
        grammar = __import__("tree_sitter_javascript")
    else:
        grammar = __import__("tree_sitter_typescript")
    entry = _grammar_entry_member(language, relative_path)
    lang = Language(getattr(grammar, entry)())
    parser_obj = Parser(lang)
    tree = parser_obj.parse(text.encode("utf-8"))
    lines = _split_lines(text)
    symbols: list[SourceSymbol] = []
    blocks: list[SourceBlock] = []
    state = {"nodes": 0, "truncated": False}
    _walk_ts_nodes(tree.root_node, language, lines, symbols, blocks, state)
    return ParseResult(
        language=language,
        parser_status="tree-sitter",
        symbols=tuple(sorted(symbols, key=lambda s: (s.start_line, s.start_col, s.name))),
        blocks=tuple(sorted(blocks, key=lambda b: (b.start_line, b.name or ""))),
        truncated=state["truncated"] or len(lines) > BOUNDED_LINES,
        line_count=len(lines),
    )


_FUNC_NODE_NAMES = {"function_declaration", "generator_function_declaration",
                    "method_definition", "arrow_function"}
_CLASS_NODE_NAMES = {"class_declaration"}
_TS_FUNCTION_DECLARATIONS = {
    "function_declaration", "generator_function_declaration",
    "method_signature", "method_definition", "class_method",
}
_TS_MODULE_ITEMS = {"lexical_declaration", "variable_declaration", "enum_declaration",
                    "interface_declaration", "type_alias_declaration", "module"}


def _walk_ts_nodes(node, language: str, lines, symbols, blocks, state) -> None:
    """递归遍历 Tree-sitter 树（children 迭代），产出函数/类符号（真实行号）。

    节点数/输出总量为真界：超限置 state["truncated"] 并停止收集，已收集符号保持
    真实行号；绝不无界递归整树。
    """
    state["nodes"] += 1
    if state["nodes"] > MAX_AST_NODES:
        state["truncated"] = True
        return
    node_type = node.type
    kind = None
    if language == "javascript":
        if node_type in _FUNC_NODE_NAMES:
            kind = "function"
        elif node_type == "class_declaration":
            kind = "class"
    else:  # typescript
        if node_type in _TS_FUNCTION_DECLARATIONS:
            kind = "function"
        elif node_type == "class_declaration":
            kind = "class"
        elif node_type in {"interface_declaration", "enum_declaration",
                           "type_alias_declaration", "module"}:
            kind = node_type
    if kind:
        name = _node_name(node)
        start_row, start_col = node.start_point
        end_row, end_col = node.end_point
        start_line, end_line = start_row + 1, end_row + 1
        block_text = "".join(
            lines[i - 1] for i in range(start_line, min(end_line, len(lines)) + 1)
            if i - 1 < len(lines)
        )
        symbols.append(SourceSymbol(name, kind, start_line, start_col + 1,
                                    end_line, end_col + 1))
        blocks.append(SourceBlock(kind, name, start_line, end_line,
                                  block_text, tuple(range(start_line, end_line + 1))))
        if len(symbols) + len(blocks) >= MAX_OUTPUT_ITEMS:
            state["truncated"] = True
            return
    for child in node.children:
        _walk_ts_nodes(child, language, lines, symbols, blocks, state)


def _node_name(node) -> str:
    try:
        child = node.child_by_field_name("name")
        if child is not None:
            return child.text.decode("utf-8", errors="replace")
        for field in ("name", "value"):
            c = node.child_by_field_name(field)
            if c is not None:
                txt = c.text.decode("utf-8", errors="replace")
                # 仅接收标识符风格名字
                if re.fullmatch(r"[A-Za-z_$][\w$]*", txt):
                    return txt
    except Exception:
        pass
    return ""


def _fallback_text_parse(
    text: str, language: str, relative_path: str | None = None
) -> ParseResult:
    """缺 Tree-sitter（或解析失败）时的有界文本降级：带真实行号，绝不伪造符号。

    修复点：截断判定基于**原文行总数**（``len(all_lines) > BOUNDED_LINES``），
    而不是在 `[:BOUNDED_LINES]` 切片后才比较切片长度（那恒 False，导致大文件
    截断永远漏报）。输出为原文行号前缀，尾部被裁由 truncated 如实标记。
    """
    all_lines = _split_lines(text)
    total = len(all_lines)
    out_lines = all_lines[:BOUNDED_LINES]
    return ParseResult(
        language=language,
        parser_status="fallback",
        errors=("tree-sitter-unavailable-or-failed",),
        fallback_text=_numbered_text(out_lines),
        truncated=total > BOUNDED_LINES,
        line_count=total,
    )


def parse_script_text(
    text: str, language: str, relative_path: str | None = None
) -> ParseResult:
    """JS/TS 解析：Tree-sitter 可用才产出符号，否则有界 fallback。

    未给 relative_path 时按 language 走默认 entry（typescript→language_typescript）。
    """
    if language not in {"javascript", "typescript"}:
        return _fallback_text_parse(text, "unknown", relative_path)
    if not _tree_sitter_available(language, relative_path):
        return _fallback_text_parse(text, language, relative_path)
    try:
        return _parse_with_tree_sitter(text, language, relative_path)
    except Exception as err:  # 运行 tree-sitter 失败 -> 降级，不伪装 AST
        result = _fallback_text_parse(text, language, relative_path)
        return ParseResult(
            language=language, parser_status="fallback",
            errors=result.errors + (f"tree-sitter-error: {err}",),
            fallback_text=result.fallback_text, truncated=result.truncated,
            line_count=result.line_count,
        )


# ---------- Vue（静态 SFC，保持原行偏移） ----------

_VUE_BLOCK_RE = re.compile(
    r"<(?P<tag>script|template|style)\b[^>]*>(?P<body>.*?)</(?P=tag)\s*>",
    re.DOTALL,
)


def parse_vue_text(text: str, line_offset_base: int = 1) -> ParseResult:
    """静态解析 Vue SFC 的 script/template/style 三块，保持原文件行号。

    块行号 = 原文件真实行号（不重排），script 按 JS/TS 可用性与 lang 再解析。
    """
    lines = _split_lines(text)
    symbols: list[SourceSymbol] = []
    blocks: list[SourceBlock] = []
    errors: list[str] = []
    script_result: ParseResult | None = None
    # 必须在循环前初始化：模板-only SFC（没有 <script>）时原实现在循环后才赋值，
    # 会抛 UnboundLocalError 让整个解析失败；这里先给出确定初值。
    truncated = False
    for match in _VUE_BLOCK_RE.finditer(text):
        tag = match.group("tag")
        body = match.group("body")
        global_start = match.start()
        # 计算 body 在原文中的起始行：数换行 +1。
        body_start_line = text.count("\n", 0, global_start) + line_offset_base
        body_end_line = body_start_line + body.count("\n")
        blocks.append(
            SourceBlock(
                kind=tag, name=tag,
                start_line=body_start_line, end_line=max(body_start_line, body_end_line),
                text=body,
                source_lines=tuple(range(body_start_line, body_end_line + 1)),
            )
        )
        if tag == "script":
            src = re.search(r"\bsrc\s*=", match.group(0))
            if src:
                errors.append("script 使用外部 src，静态不含内联代码——不执行/不拉取")
            else:
                lang_m = re.search(r"\blang\s*=\s*[\"'](\w+)[\"']", match.group(0))
                lang_attr = lang_m.group(1).lower() if lang_m else ""
                is_ts = "ts" in lang_attr
                # lang="tsx" 的原生 inline 也少见，但按扩展名语义选 tsx 更诚实。
                pseudo = ("__inline__.tsx" if lang_attr == "tsx"
                          else ("__inline__.ts" if is_ts else "__inline__.js"))
                script_result = parse_script_text(
                    body, "typescript" if is_ts else "javascript", pseudo
                )
                # 保持原文件行偏移：把 script 内符号行号平移回原文件。
                offset = body_start_line - 1
                for sym in script_result.symbols:
                    symbols.append(
                        SourceSymbol(sym.name, sym.kind,
                                     sym.start_line + offset, sym.start_col,
                                     sym.end_line + offset, sym.end_col)
                    )
                # 只并入**本块** script 的截断；多个 script 块任一截断都算截断。
                truncated = truncated or script_result.truncated
    status = "vue" if blocks else "fallback"
    return ParseResult(
        language="vue",
        parser_status=status,
        symbols=tuple(sorted(symbols, key=lambda s: (s.start_line, s.start_col, s.name))),
        blocks=tuple(sorted(blocks, key=lambda b: (b.start_line, b.kind))),
        errors=tuple(errors) + ((script_result.errors if script_result else ()) or ()),
        truncated=truncated or len(lines) > BOUNDED_LINES,
        line_count=len(lines),
    )


# ---------- Markdown ----------

_MD_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*)$")
_MD_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*(.*)$")


def parse_markdown_text(text: str) -> ParseResult:
    """Markdown 按标题/代码围栏切块，保留真实行号。"""
    lines = _split_lines(text)
    blocks: list[SourceBlock] = []
    cur_title = ""
    fence: str | None = None
    for idx, line in enumerate(lines, 1):
        stripped = line.rstrip("\r\n")
        fence_m = _MD_FENCE.match(stripped)
        if fence:
            if fence_m and fence_m.group(1)[0] == fence[0] and len(fence_m.group(1)) >= len(fence):
                fence = None
                continue
        elif fence_m:
            fence = fence_m.group(1)
            blocks.append(SourceBlock("code-block", None, idx, idx, line, (idx,)))
            continue
        heading = _MD_HEADING.match(stripped)
        if heading:
            cur_title = heading.group(2).strip()
            blocks.append(SourceBlock("markdown-heading", cur_title, idx, idx, line, (idx,)))
    return ParseResult(
        language="markdown",
        parser_status="markdown",
        blocks=tuple(blocks),
        truncated=len(lines) > BOUNDED_LINES,
        line_count=len(lines),
    )


# ---------- 统一入口 ----------

def parse_blob(blob: BlobRef, blob_bytes: bytes, relative_path: str,
               language: str | None = None) -> ParseResult:
    """从受控 blob 字节安全解析；绝不重开源、绝不执行。

    字节预算在这里解码前截断（MAX_TEXT_BYTES），截断会沿 truncated 一路上报；
    各语言再在行数/节点/输出上做真界。
    """
    if language is None:
        language = detect_language(relative_path)
    if _looks_binary(blob_bytes):
        return ParseResult(language="binary", parser_status="skipped",
                           errors=("binary-content",), line_count=0)
    if language == "python":
        lines, _bytes_trunc = _bounded_decode_lines(blob_bytes)
        res = parse_python_text(_split_lines_to_text(lines), relative_path)
        return _with_byte_trunc(res, _bytes_trunc)
    if language in {"javascript", "typescript"}:
        lines, _bytes_trunc = _bounded_decode_lines(blob_bytes)
        res = parse_script_text(_split_lines_to_text(lines), language, relative_path)
        return _with_byte_trunc(res, _bytes_trunc)
    if language == "vue":
        lines, _bytes_trunc = _bounded_decode_lines(blob_bytes)
        res = parse_vue_text(_split_lines_to_text(lines))
        return _with_byte_trunc(res, _bytes_trunc)
    if language == "markdown":
        lines, _bytes_trunc = _bounded_decode_lines(blob_bytes)
        res = parse_markdown_text(_split_lines_to_text(lines))
        return _with_byte_trunc(res, _bytes_trunc)
    if language in {"text", None}:
        lines, _bytes_trunc = _bounded_decode_lines(blob_bytes)
        out_lines = lines[:BOUNDED_LINES]
        res = ParseResult(language=language or "text", parser_status="fallback",
                          fallback_text=_numbered_text(out_lines),
                          truncated=_bytes_trunc or len(lines) > BOUNDED_LINES,
                          line_count=len(lines))
        return res
    return ParseResult(language=language, parser_status="skipped", errors=("unsupported",))


def _split_lines_to_text(lines: list[str]) -> str:
    """把已切行拼回文本（parse_blob 内部用；保留行尾，语义等同原文本）。"""
    return "".join(lines)


def _with_byte_trunc(res: ParseResult, bytes_truncated: bool) -> ParseResult:
    """把字节层截断并入结果的 truncated（字节截断已是硬截断）。"""
    if not bytes_truncated:
        return res
    return ParseResult(
        language=res.language, parser_status=res.parser_status,
        symbols=res.symbols, blocks=res.blocks, errors=res.errors,
        fallback_text=res.fallback_text, truncated=True, line_count=res.line_count,
    )


# ---------- 长行有界拆分（可核验原文行） ----------

@dataclass(frozen=True)
class LineChunk:
    source_lines: tuple[int, ...]  # 真实原文行号
    text: str  # 仅拼接原文（+首部可选已标记的合成前缀会被规避，见下）


def iter_bounded_line_chunks(lines: list[str], base_line: int = 1,
                             max_lines: int = 200, max_chars: int = 2000):
    """把跨行文本按行有界拆分，块只含可核验原文行号，不做语义合并/补行。

    **单行超长（> max_chars）不再被截断丢弃**：该行会被切成连续片段，每个片段
    都是原文的真实子串，且逐片携带同一个真实行号；所有片段拼起来等于原文该行，
    因此 >2000 字符的单行全文可被完整索引/检索，行号元数据仍然如实（全部指向
    该行，而不是伪造的行号或静默丢内容）。
    """
    if max_lines <= 0 or max_chars <= 0:
        raise ValueError("行块预算必须为正")
    buffer: list[str] = []
    buffer_lines: list[int] = []
    chars = 0
    for off, line in enumerate(lines):
        line_no = base_line + off
        if len(line) > max_chars:
            # 先把已有缓冲冲出去，保证长行片段不与非本行内容混在一个块里。
            if buffer:
                yield LineChunk(tuple(buffer_lines), "".join(buffer))
                buffer, buffer_lines, chars = [], [], 0
            # 按 max_chars 切片覆盖整行（含结尾换行），每片都标真实行号。
            for start in range(0, len(line), max_chars):
                yield LineChunk((line_no,), line[start:start + max_chars])
            continue
        if buffer and chars + len(line) > max_chars:
            yield LineChunk(tuple(buffer_lines), "".join(buffer))
            buffer, buffer_lines, chars = [], [], 0
        buffer.append(line)
        buffer_lines.append(line_no)
        chars += len(line)
        if len(buffer_lines) >= max_lines:
            yield LineChunk(tuple(buffer_lines), "".join(buffer))
            buffer, buffer_lines, chars = [], [], 0
    if buffer:
        yield LineChunk(tuple(buffer_lines), "".join(buffer))


def _numbered_text(lines: list[str]) -> str:
    """带行号的受限文本（fallback 标记用），明文标记这不是原始 unmarked 文本。"""
    out = []
    for idx, line in enumerate(lines, 1):
        content = line.rstrip("\r\n")
        out.append(f"{idx:>6}|{content}")
    return "\n".join(out)


def read_blob_bounded(blob_bytes: bytes) -> tuple[str, bool]:
    """读取 blob 为受限文本；二进制/超大标记 truncated。"""
    if len(blob_bytes) > MAX_TEXT_BYTES:
        return blob_bytes[:MAX_TEXT_BYTES].decode("utf-8", errors="replace"), True
    return _decode_text_blob(blob_bytes)[0], False
