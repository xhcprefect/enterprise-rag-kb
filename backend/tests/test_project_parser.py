"""project_parser / project_terms 纯测试：真实行号、fallback、Vue/MD、长行、词元。

无 DB、无模型；tree_sitter/pathspec 缺失时断言 fallback（绝不伪造 AST）。
"""
import unittest

from app.services.project_parser import (
    SourceSymbol,
    _tree_sitter_available,
    detect_language,
    iter_bounded_line_chunks,
    parse_blob,
    parse_markdown_text,
    parse_python_text,
    parse_script_text,
    parse_vue_text,
)
from app.services.project_terms import code_terms, path_terms, split_identifier
from app.services.project_types import BlobRef


class DetectLanguageTests(unittest.TestCase):
    def test_extensions(self):
        self.assertEqual(detect_language("a.py"), "python")
        self.assertEqual(detect_language("a.js"), "javascript")
        self.assertEqual(detect_language("a.ts"), "typescript")
        self.assertEqual(detect_language("a.vue"), "vue")
        self.assertEqual(detect_language("a.md"), "markdown")
        self.assertEqual(detect_language("a.x.bin"), "binary")
        self.assertIsNone(detect_language("noext"))


class PythonAstTests(unittest.TestCase):
    def test_symbols_have_real_1_based_lines(self):
        text = "def foo():\n    return 1\n\n\nclass Bar:\n    async def baz(self):\n        return 1\n"
        r = parse_python_text(text)
        self.assertEqual(r.parser_status, "ast")
        by_name = {s.name: s for s in r.symbols}
        self.assertEqual(by_name["foo"].kind, "function")
        self.assertEqual(by_name["foo"].start_line, 1)
        self.assertEqual(by_name["Bar"].kind, "class")
        self.assertEqual(by_name["Bar"].start_line, 5)
        self.assertEqual(by_name["baz"].kind, "async-function")
        self.assertEqual(by_name["baz"].start_line, 6)

    def test_syntax_error_falls_back_without_symbols(self):
        r = parse_python_text("def broken(:\n")
        self.assertEqual(r.parser_status, "fallback")
        self.assertEqual(r.symbols, ())
        self.assertIsNotNone(r.fallback_text)
        self.assertTrue(any("syntax-error" in e for e in r.errors))


class ScriptFallbackTests(unittest.TestCase):
    def test_js_ts_missing_parser_never_fabricates_ast(self):
        available = _tree_sitter_available("javascript")
        for lang in ("javascript", "typescript"):
            r = parse_script_text("function f() { return 1; }", lang)
            if available:
                # 装了 parser 才允许有 AST（当前 venv 未装，走 fallback）。
                self.assertEqual(r.parser_status, "tree-sitter")
            else:
                self.assertEqual(r.parser_status, "fallback")
                self.assertEqual(r.symbols, ())  # 缺包绝不伪造符号
                self.assertIsNotNone(r.fallback_text)


class VueTests(unittest.TestCase):
    def test_sfc_blocks_preserve_original_line_offsets(self):
        text = (
            "<template>\n"
            "  <div>hi</div>\n"
            "</template>\n"
            "\n"
            "<script>\n"
            "export default { name: 'X' }\n"
            "</script>\n"
            "\n"
            "<style>\n"
            ".a { color: red }\n"
            "</style>\n"
        )
        r = parse_vue_text(text)
        self.assertEqual(r.parser_status, "vue")
        by_kind = {b.kind: b for b in r.blocks}
        self.assertEqual(by_kind["template"].start_line, 1)
        self.assertEqual(by_kind["script"].start_line, 5)
        self.assertEqual(by_kind["style"].start_line, 9)


    def test_template_only_sfc_does_not_crash(self):
        """没有 <script> 的模板-only SFC：不得 UnboundLocalError，块与行号仍真实。"""
        text = (
            "<template>\n"
            "  <div>only template</div>\n"
            "</template>\n"
            "<style>\n"
            ".x{color:red}\n"
            "</style>\n"
        )
        r = parse_vue_text(text)
        self.assertEqual(r.parser_status, "vue")
        self.assertFalse(r.truncated)
        by_kind = {b.kind: b for b in r.blocks}
        self.assertEqual(by_kind["template"].start_line, 1)
        self.assertEqual(by_kind["style"].start_line, 4)

    def test_template_only_sfc_via_parse_blob(self):
        blob = BlobRef(sha256="0" * 64, size=0)
        r = parse_blob(blob, b"<template>\n  <p>x</p>\n</template>\n", "Only.vue")
        self.assertEqual(r.parser_status, "vue")
        self.assertEqual(r.blocks[0].kind, "template")


class MarkdownTests(unittest.TestCase):
    def test_headings_and_fences_real_lines(self):
        text = "# Title\n\nSome text\n\n```py\nprint(1)\n```\n\n## Sub\n"
        r = parse_markdown_text(text)
        headings = [b for b in r.blocks if b.kind == "markdown-heading"]
        fences = [b for b in r.blocks if b.kind == "code-block"]
        self.assertEqual([b.name for b in headings], ["Title", "Sub"])
        self.assertEqual([b.start_line for b in headings], [1, 9])
        self.assertEqual(fences[0].start_line, 5)


class BoundedSplitTests(unittest.TestCase):
    def test_long_single_line_truncated_with_real_line(self):
        lines = ["a" * 3000, "short\n"]
        chunks = list(iter_bounded_line_chunks(lines, base_line=1, max_lines=200, max_chars=100))
        long = [c for c in chunks if 1 in c.source_lines]
        self.assertTrue(long)
        self.assertTrue(all(len(c.text) <= 100 for c in chunks))
        self.assertEqual(long[0].source_lines, (1,))

    def test_long_single_line_fully_covered_with_real_line_number(self):
        """>2000 字符的单行不得被截断丢弃：片段拼接 == 原文，且全部指向真实行号。"""
        long_line = "z" * 2500 + "\n"
        lines = ["head\n", long_line, "tail\n"]
        chunks = list(iter_bounded_line_chunks(lines, base_line=1, max_lines=200, max_chars=2000))
        covered = [c for c in chunks if 2 in c.source_lines]
        self.assertEqual("".join(c.text for c in covered), long_line)
        self.assertTrue(all(c.source_lines == (2,) for c in covered))
        self.assertTrue(all(len(c.text) <= 2000 for c in chunks))
        # 头尾真实行不丢
        self.assertIn("head\n", "".join(c.text for c in chunks))
        self.assertIn("tail\n", "".join(c.text for c in chunks))

    def test_exact_budget_line_not_split(self):
        line = "a" * 1999 + "\n"  # 恰好 2000 字符（含换行）
        self.assertEqual(len(line), 2000)
        chunks = list(iter_bounded_line_chunks([line], base_line=5, max_chars=2000))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].source_lines, (5,))

    def test_chunks_carry_only_real_source_lines(self):
        lines = ["aa\n", "bb\n", "cc\n", "dd\n"]
        chunks = list(iter_bounded_line_chunks(lines, base_line=10, max_lines=2, max_chars=1000))
        joined = set(n for c in chunks for n in c.source_lines)
        self.assertTrue(all(n in {10, 11, 12, 13} for n in joined))
        # 行号必须来自原文，不存在合成行号。
        self.assertNotIn(9, joined)
        self.assertNotIn(14, joined)


class TermsTests(unittest.TestCase):
    def test_split_identifier_camelcase_underscore(self):
        self.assertIn("http", split_identifier("HTTPServer"))
        self.assertIn("server", split_identifier("HTTPServer"))
        self.assertIn("user", split_identifier("get_user_id"))
        self.assertIn("id", split_identifier("get_user_id"))

    def test_code_terms(self):
        terms = code_terms("def build_rag_index():\n    pass\n")
        self.assertIn("build", terms)
        self.assertIn("rag", terms)
        self.assertIn("index", terms)

    def test_path_terms(self):
        terms = path_terms("services/projectScanner.py")
        joined = " ".join(terms)
        self.assertIn("services", joined)
        self.assertIn("projectscanner", joined)
        self.assertIn("project", joined)
        self.assertIn("scanner", joined)


class ParseBlobTests(unittest.TestCase):
    def test_python_via_blob_bytes(self):
        blob = BlobRef(sha256="0" * 64, size=0)
        r = parse_blob(blob, b"def x():\n    pass\n", "a.py")
        self.assertEqual(r.parser_status, "ast")
        self.assertEqual([s.name for s in r.symbols], ["x"])

    def test_binary_skipped(self):
        blob = BlobRef(sha256="0" * 64, size=0)
        r = parse_blob(blob, b"\x00\x00\x00abc", "a.txt")
        self.assertEqual(r.parser_status, "skipped")
        self.assertTrue(any("binary" in e for e in r.errors))


if __name__ == "__main__":
    unittest.main()
