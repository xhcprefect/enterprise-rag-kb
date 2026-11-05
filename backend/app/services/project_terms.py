"""code-aware 检索词：把符号、驼峰、下划线、路径拆成可搜索的词元。

纯函数，无 I/O。不能照搬通用中文/空格分词——那会破坏代码标识符。
保证：返回的每个词元都来自输入文本的可核验子串，绝不凭空补词。
"""
from __future__ import annotations

import re

# 拆分标识符：字母数字开头，允许 _ $ 和若干连字符风格的 token。
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# 大小写边界：myVar -> my, Var；HTTPServer -> HTTP, Server
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|[0-9]+")
# 下划线/点/斜杠/连字符分隔（把路径组件也当作一组词元分开）。
_SEP = re.compile(r"[_.\-/\\]+")

# 极小停用词（保留绝大多数标识符；忽略列在此处的本身不是有效标识符的噪音）。
_STOPWORDS = frozenset({
    "def", "class", "return", "import", "from", "as", "if", "else", "elif",
    "for", "while", "try", "except", "finally", "with", "yield", "lambda",
    "pass", "break", "continue", "raise", "and", "or", "not", "in", "is",
    "this", "self", "new", "function", "const", "let", "var", "export",
    "default", "async", "await", "using", "namespace", "public", "private",
    "protected", "static", "void", "int", "string", "bool", "true", "false",
    "null", "undefined", "None", "True", "False", "the", "a", "an", "of",
    "to", "on", "for", "in", "at", "by",
})


def split_identifier(ident: str) -> list[str]:
    """把一个标识符按驼峰/下划线/数字拆成词元（全小写，去空）。"""
    parts: list[str] = []
    for chunk in _SEP.split(ident):
        if not chunk:
            continue
        parts.extend(m.group(0).lower() for m in _CAMEL.finditer(chunk) if m.group(0))
    return [p for p in parts if p and p not in _STOPWORDS]


def iter_identifier_tokens(text: str):
    """流式产出文本中每个标识符的 (词元, 起始列, 结束列)，列基于 0 基元组。"""
    for match in _IDENT.finditer(text):
        ident = match.group(0)
        for token in split_identifier(ident):
            if not token:
                continue
            start = match.start()
            # 词元相对标识符内的偏移由 split 无法直接给出，这里只保证：
            # 词元确实是该标识符的子串（可核验），不粗列偏移，避免误报行号。
            if token.lower() in ident.lower():
                yield token, match.start(), match.end()


def code_terms(text: str) -> list[str]:
    """收集去重后的小写检索词元（含路径/标识符拆词），保序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for token in _IDENT.finditer(text):
        lower = token.group(0).lower()
        if lower in seen or lower in _STOPWORDS:
            continue
        seen.add(lower)
        out.append(lower)
        for part in split_identifier(token.group(0)):
            if part and part not in seen and part not in _STOPWORDS:
                seen.add(part)
                out.append(part)
    return out


def path_terms(relative_path: str) -> list[str]:
    """把相对路径（正斜杠）拆成检索词：整个路径、各组件、组件拆词。"""
    parts = [p for p in relative_path.split("/") if p]
    stems = [parts[-1]] if parts else []
    if "." in stems[0]:
        stems[0] = stems[0].rsplit(".", 1)[0]
    seen: set[str] = set()
    out: list[str] = []
    lowered = relative_path.lower()
    for candidate in [lowered, *[p.lower() for p in parts], *stems]:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
        for part in split_identifier(candidate):
            if part and part not in seen:
                seen.add(part)
                out.append(part)
    return out
