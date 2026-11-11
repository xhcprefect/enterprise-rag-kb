"""文本编码全量校验：ASCII 超长前缀、BOM、GBK/GB18030、截断与有界流式读取。"""
import codecs
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from app.services.parser import (
    TextDecodingError,
    detect_encoding,
    detect_file_encoding,
    iter_document_blocks,
    parse_document,
)
from app.services.structured_documents import iter_structured_chunks
from app.services.user_import import ImportFormatError, _read_csv_rows

# 超过旧的 64KiB 采样窗口，后面才是中文正文。
BIG_ASCII = "A" * (64 * 1024 + 2048)


class TextEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def chunks(self, path, size=256, overlap=32):
        chunks = list(iter_structured_chunks(path, size, overlap))
        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk.content) <= size for chunk in chunks))
        return chunks

    def test_chinese_after_64k_ascii_prefix_is_preserved_for_txt_and_md(self):
        bodies = {
            "txt": BIG_ASCII + "\n中文标题\n中文正文内容。\n",
            "md": BIG_ASCII + "\n# 中文标题\n\n中文正文内容。\n",
        }
        for extension, text in bodies.items():
            with self.subTest(extension=extension):
                path = self.root / f"prefix.{extension}"
                path.write_bytes(text.encode("gbk"))
                self.assertGreater(path.stat().st_size, 64 * 1024)
                self.assertIn(detect_file_encoding(path), {"gbk", "gb18030"})

                joined = "".join(chunk.content for chunk in self.chunks(path))
                self.assertIn("中文正文内容", joined)

                # 兼容入口（旧 parser）也必须使用全量校验后的编码。
                parsed = "".join(block for block, _ in iter_document_blocks(path))
                self.assertIn("中文正文内容", parsed)
                self.assertIn("中文正文内容", parse_document(path))

    def test_csv_chinese_after_64k_ascii_rows_keeps_header_and_rows(self):
        # 表头必须是 ASCII：旧实现只看前 64KiB，若表头是中文会立即选中 GBK，
        # 复现不了「ASCII 前缀掩盖后文编码」的缺陷。
        header = "id,name,note\n"
        rows = [f"{index},user{index:05d},ascii" for index in range(8000)]
        rows.append("8000,中文姓名,中文备注")
        path = self.root / "big.csv"
        path.write_bytes((header + "\n".join(rows)).encode("gbk"))
        self.assertGreater(path.stat().st_size, 64 * 1024)
        # 旧实现的前 64KiB 采样会误判为 UTF-8。
        self.assertEqual(detect_encoding(path.read_bytes()[:65536]), "utf-8")

        chunks = self.chunks(path, 512, 32)
        joined = "".join(chunk.content for chunk in chunks)
        # 最后出现的中文必须完整保留。
        self.assertIn("中文姓名", joined)
        self.assertIn("中文备注", joined)
        self.assertEqual(joined.rstrip().splitlines()[-1], "8000 | 中文姓名 | 中文备注")
        # 表头在每块中重复，原始行号保留。
        self.assertTrue(all("id | name | note" in chunk.content for chunk in chunks))
        self.assertTrue(all(chunk.metadata["row_start"] >= 2 for chunk in chunks))

        parsed = "".join(block for block, _ in iter_document_blocks(path))
        self.assertIn("中文姓名", parsed)

    def test_utf8_bom_is_stripped_for_text_markdown_and_csv(self):
        for extension, text in (("txt", "中文内容\n"), ("md", "# 标题\n正文\n")):
            with self.subTest(extension=extension):
                path = self.root / f"bom.{extension}"
                path.write_bytes(codecs.BOM_UTF8 + text.encode("utf-8"))
                self.assertEqual(detect_file_encoding(path), "utf-8-sig")
                chunks = self.chunks(path)
                joined = "".join(chunk.content for chunk in chunks)
                self.assertNotIn("﻿", joined)
                if extension == "md":
                    self.assertTrue(any(chunk.metadata.get("section") == "标题" for chunk in chunks))

        csv_path = self.root / "bom.csv"
        csv_path.write_bytes(codecs.BOM_UTF8 + "用户名,姓名\nzhangsan,张三\n".encode("utf-8"))
        rows = _read_csv_rows(csv_path.read_bytes())
        self.assertEqual(rows[0][0], "用户名")
        self.assertEqual(rows[1][1], "张三")

    def test_gbk_and_gb18030_are_detected_and_preserved(self):
        gbk_path = self.root / "gbk.txt"
        gbk_path.write_bytes("中文编码测试".encode("gbk"))
        self.assertEqual(detect_file_encoding(gbk_path), "gbk")
        self.assertIn("中文编码测试", "".join(chunk.content for chunk in self.chunks(gbk_path)))

        # U+20000 只能由 GB18030 表示，GBK 会解码失败并回退。
        extended = "\U00020000"
        gb18030_path = self.root / "gb18030.txt"
        gb18030_path.write_bytes(f"扩展字符{extended}结束".encode("gb18030"))
        self.assertEqual(detect_file_encoding(gb18030_path), "gb18030")
        self.assertIn(extended, "".join(chunk.content for chunk in self.chunks(gb18030_path)))

    def test_sample_cut_mid_character_is_tolerated_but_file_probe_uses_full_content(self):
        # 采样末尾恰好切断多字节字符时，不应误判候选。
        self.assertEqual(detect_encoding("前缀".encode("utf-8")[:-1]), "utf-8")
        self.assertEqual(detect_encoding("中文".encode("gbk")[:-1]), "gbk")

        # 只有 ASCII 前缀的采样会误报 utf-8；文件探测必须看完整文件才能识别 GBK。
        full = (BIG_ASCII + "中文").encode("gbk")
        self.assertEqual(detect_encoding(full[:65536]), "utf-8")
        path = self.root / "latent-gbk.txt"
        path.write_bytes(full)
        self.assertIn(detect_file_encoding(path), {"gbk", "gb18030"})
        self.assertIn("中文", parse_document(path))

    def test_undecodable_or_truncated_content_raises_clear_error(self):
        with self.assertRaises(TextDecodingError) as raised:
            detect_encoding(b"\xff\xfe\x00\x00bad")
        self.assertIsInstance(raised.exception, ValueError)
        self.assertIn("编码", str(raised.exception))

        broken = self.root / "broken.txt"
        broken.write_bytes(BIG_ASCII.encode("ascii") + b"\xff")
        with self.assertRaises(TextDecodingError):
            list(iter_structured_chunks(broken, 128, 16))
        with self.assertRaises(TextDecodingError):
            parse_document(broken)

        # 文件末尾四字节 UTF-8 字符被截断：三个候选都无法收尾。
        truncated = self.root / "truncated.txt"
        truncated.write_bytes("\U0001F600".encode("utf-8")[:-1])
        with self.assertRaises(TextDecodingError):
            detect_file_encoding(truncated)

    def test_user_import_csv_uses_full_validation_and_stays_strict(self):
        header = "username,name,role\n"
        rows = [f"user{index:05d},ascii,user" for index in range(8000)]
        rows.append("zhangsan,张三,普通用户")
        data = (header + "\n".join(rows)).encode("gbk")
        self.assertGreater(len(data), 64 * 1024)
        self.assertEqual(detect_encoding(data[:65536]), "utf-8")

        parsed = _read_csv_rows(data)
        self.assertEqual(parsed[0], ["username", "name", "role"])
        self.assertEqual(parsed[-1], ["zhangsan", "张三", "普通用户"])

        # BOM 不粘在第一个表头上。
        bom_csv = codecs.BOM_UTF8 + header.encode("utf-8") + "zhangsan,张三,普通用户\n".encode("utf-8")
        self.assertEqual(_read_csv_rows(bom_csv)[0][0], "username")

        # 损坏/截断内容必须失败，不能被当作导入成功。
        with self.assertRaises(ImportFormatError):
            _read_csv_rows(b"\xff\xfe\x00\x00bad")
        with self.assertRaises(ImportFormatError):
            _read_csv_rows(header.encode("ascii") + "\U0001F600".encode("utf-8")[:-1])

    def test_complete_bytes_probe_handles_gbk_char_that_looks_like_utf8_remnant(self):
        # b"\xe4\xb8" 是完整的 GBK 字符「涓」，但也是 UTF-8 三字节字符的残尾。
        # 采样模式（final=False）无法区分，完整字节探测（final=True）必须淘汰 UTF-8。
        tail = "\xe4\xb8".encode("latin-1")  # 直接构造这两个字节
        # 该 GBK 字符位于整个 CSV 的真实末尾（无结尾换行），正是采样会漏判的场景。
        data = b"username,name,role\nzhangsan,zhangsan,user\nlisi,lisi," + tail
        self.assertEqual(tail.decode("gbk"), "涓")
        self.assertEqual(detect_encoding(data, final=True), "gbk")
        # 采样默认行为保持兼容：末尾残尾不参与判定。
        self.assertEqual(detect_encoding(data), "utf-8")

        parsed = _read_csv_rows(data)
        self.assertEqual(parsed[-1], ["lisi", "lisi", "涓"])

        # 文件探测同样用真实 EOF 淘汰候选。
        path = self.root / "gbk-tail.csv"
        path.write_bytes(data)
        self.assertEqual(detect_file_encoding(path), "gbk")

    def test_file_probe_streams_with_bounded_memory(self):
        path = self.root / "large.txt"
        path.write_bytes(b"A" * (8 * 1024 * 1024) + "中文".encode("utf-8"))
        tracemalloc.start()
        try:
            self.assertEqual(detect_file_encoding(path), "utf-8")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # 若整文件读入内存，峰值会接近文件本身大小（8MB）。
        self.assertLess(peak, 6 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()