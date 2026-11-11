"""真实临时 PDF 验证表格识别、页内合并、扫描页边界与安全失败（不连数据库/模型）。

正常表格路径全部调用本机真实的 ``Page.find_tables()``；只有“检测失败/单表抽取
失败/图像探测不可用”这三类故障用注入制造，且注入点都在 PyMuPDF API 边界。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

import pymupdf

from app.services.parser import iter_document_blocks
from app.services.pdf_documents import (
    MAX_REPORTED_OCR_PAGES,
    PdfParseError,
    PdfTableSegment,
    iter_pdf_pages,
    iter_pdf_segments,
)
from app.services.structured_documents import iter_structured_chunks


def rule_table(page, left, top, rows, cols, values, cell_w=90, cell_h=22, font="helv"):
    """画真实格线并写单元格文字；values 为行列文本，空串/None 表示空单元格。"""
    for row in range(rows + 1):
        page.draw_line((left, top + row * cell_h), (left + cols * cell_w, top + row * cell_h))
    for col in range(cols + 1):
        page.draw_line((left + col * cell_w, top), (left + col * cell_w, top + rows * cell_h))
    for row in range(rows):
        for col in range(cols):
            text = values[row][col]
            if text:
                page.insert_text((left + col * cell_w + 4, top + row * cell_h + cell_h - 7), text,
                                 fontname=font)


def tiny_png(color=120):
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 24, 24), 0)
    pixmap.clear_with(color)
    return pixmap.tobytes("png")


class PdfFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build(self, name, pages):
        """pages: 每页一个 ``draw(page)`` 回调；返回保存后的 PDF 路径。"""
        document = pymupdf.open()
        try:
            for draw in pages:
                draw(document.new_page())
            path = self.root / name
            document.save(path)
        finally:
            document.close()
        return path

    def chunks(self, path, size=200, overlap=20):
        chunks = list(iter_structured_chunks(path, size, overlap))
        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk.content) <= size for chunk in chunks))
        return chunks

    def joined(self, chunks):
        return "\n".join(chunk.content for chunk in chunks)

    def table_chunks(self, chunks, index):
        return [chunk for chunk in chunks if chunk.metadata.get("table_index") == index]

    @staticmethod
    def table_page(page):
        page.insert_text((60, 50), "Context before first table.")
        rule_table(page, 60, 80, 3, 3,
                   [["Plan", "Owner", "Budget"], ["Alpha", "North", "120"],
                    ["Beta", "", "240"]])


class PdfTableParsingTests(PdfFixture):
    def test_ruled_table_keeps_empty_cells_header_and_logical_row_numbers(self):
        path = self.build("table.pdf", [self.table_page])
        segments = list(iter_pdf_segments(path))
        tables = [segment for segment in segments if isinstance(segment, PdfTableSegment)]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].rows[0], ("Plan", "Owner", "Budget"))
        self.assertEqual(tables[0].rows[2], ("Beta", "", "240"))  # 空单元格保持为 ""
        chunks = self.chunks(path)
        table = self.table_chunks(chunks, 1)
        self.assertTrue(table)
        for chunk in table:
            self.assertIn("Plan | Owner | Budget", chunk.content)
            self.assertIn("--- | --- | ---", chunk.content)
            # 表头只重复在分块前缀里，不再作为正文行重复。
            self.assertEqual(chunk.content.count("Plan | Owner | Budget"), 1)
            self.assertGreaterEqual(chunk.metadata["row_start"], 2)
            self.assertEqual(chunk.metadata["page_start"], 1)
            self.assertEqual(chunk.metadata["page_end"], 1)
        joined = self.joined(table)
        self.assertIn("Beta |  | 240", joined)
        # 表格成功物化后，单元格文字不再作为正文重复出现。
        body = [chunk for chunk in chunks if not chunk.metadata.get("table_index")]
        self.assertNotIn("Alpha", self.joined(body))
        self.assertIn("Context before first table.", self.joined(body))

    def test_two_tables_on_one_page_keep_order_and_stable_index(self):
        def page(page):
            page.insert_text((60, 45), "Lead in text.")
            rule_table(page, 60, 60, 2, 2, [["Quarter", "Actual"], ["Q1", "10"]])
            page.insert_text((60, 200), "Between the two tables.")
            rule_table(page, 60, 220, 2, 2, [["Region", "Sales"], ["East", "42"]])
            page.insert_text((60, 360), "Trailing text.")

        path = self.build("two.pdf", [page])
        chunks = self.chunks(path, 120, 10)
        first, second = self.table_chunks(chunks, 1), self.table_chunks(chunks, 2)
        self.assertTrue(first and second)
        self.assertTrue(all("Quarter" in chunk.content for chunk in first))
        self.assertTrue(all("Region" in chunk.content for chunk in second))
        self.assertFalse(any("Region" in chunk.content for chunk in first))
        self.assertFalse(any("Quarter" in chunk.content for chunk in second))
        order = self.joined(chunks)
        self.assertLess(order.index("Lead in text"), order.index("Quarter"))
        self.assertLess(order.index("Quarter | Actual"), order.index("Between the two tables"))
        self.assertLess(order.index("Region | Sales"), order.index("Trailing text"))
        # table_index 严格按页内阅读顺序递增，且不跨页混块。
        self.assertEqual([table[0].metadata["table_index"] for table in (first, second)], [1, 2])

    def test_external_header_is_added_exactly_once(self):
        def page(page):
            page.insert_text((45, 62), "Name")
            page.insert_text((135, 62), "Age")
            rule_table(page, 40, 70, 2, 2, [["Alice", "30"], ["Bob", "25"]])

        path = self.build("external.pdf", [page])
        tables = [segment for segment in iter_pdf_segments(path)
                  if isinstance(segment, PdfTableSegment)]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].rows[0], ("Name", "Age"))
        self.assertEqual([row[0] for row in tables[0].rows[1:]], ["Alice", "Bob"])
        chunks = self.chunks(path)
        table = self.table_chunks(chunks, 1)
        self.assertTrue(table)
        for chunk in table:
            self.assertEqual(chunk.content.count("Name | Age"), 1)
            self.assertNotIn("Alice | 30\nName", chunk.content)

    def test_blank_page_between_text_pages_does_not_shift_page_numbers(self):
        def write(text):
            def draw(page):
                page.insert_text((60, 60), text)
            return draw

        path = self.build("blank.pdf", [write("first physical page"), lambda page: None,
                                        write("third physical page")])
        pages = list(iter_pdf_pages(path))
        self.assertEqual([page.number for page in pages], [1, 3])
        chunks = self.chunks(path)
        self.assertEqual({chunk.metadata["page_start"] for chunk in chunks}, {1, 3})

    def test_body_and_table_interleave_in_page_order(self):
        def page(page):
            page.insert_text((60, 50), "Before.")
            rule_table(page, 60, 70, 2, 2, [["A", "B"], ["1", "2"]])
            page.insert_text((60, 200), "After.")

        path = self.build("mixed.pdf", [page])
        chunks = self.chunks(path)
        joined = self.joined(chunks)
        self.assertLess(joined.index("Before."), joined.index("A | B"))
        self.assertLess(joined.index("1 | 2"), joined.index("After."))
        self.assertEqual(joined.count("A | B"), 1)

    def test_text_on_the_same_row_outside_the_table_is_kept(self):
        def page(page):
            rule_table(page, 40, 100, 2, 2, [["A", "1"], ["B", "2"]])
            page.insert_text((300, 114), "SIDE-MARKER")

        path = self.build("side.pdf", [page])
        chunks = self.chunks(path)
        table = self.table_chunks(chunks, 1)
        body = [chunk for chunk in chunks if not chunk.metadata.get("table_index")]
        self.assertTrue(table and body)
        self.assertIn("SIDE-MARKER", self.joined(body))
        self.assertNotIn("SIDE-MARKER", self.joined(table))
        self.assertIn("A | 1", self.joined(table))

    def test_span_partially_inside_a_cell_keeps_the_outside_text(self):
        def page(page):
            rule_table(page, 100, 200, 2, 2, [["A", "B"], ["C", "D"]])
            # 起点在表格左侧、后续字符落入表格：只保留表格外的部分，不整行删除。
            page.insert_text((20, 240), "left-outside-then-inside")
            # 起点在表格内、尾部越出右边界的独立文本行。
            page.insert_text((330, 216), "RIGHT-EDGE-TEXT")

        path = self.build("partial.pdf", [page])
        joined = self.joined(self.chunks(path))
        self.assertIn("left-outside-then", joined)
        self.assertIn("RIGHT-EDGE-TEXT", joined)
        self.assertNotIn("left-outside-then-inside", joined)

    def test_page_without_any_table_keeps_plain_text(self):
        def page(page):
            page.insert_text((60, 60), "No table on this page.")
            page.insert_text((60, 90), "Second line stays.")

        path = self.build("no-table.pdf", [page])
        chunks = self.chunks(path)
        self.assertFalse(any(chunk.metadata.get("table_index") for chunk in chunks))
        self.assertIn("No table on this page.", self.joined(chunks))
        self.assertIn("Second line stays.", self.joined(chunks))

    def test_rotated_page_keeps_table_text_as_body_instead_of_deleting_wrong_area(self):
        """旋转页的表格 bbox 与文字坐标不同源：宁可整页保留正文，也不能误删。"""

        def page(page):
            page.insert_text((60, 60), "Text above table.")
            rule_table(page, 60, 100, 2, 2, [["Name", "Qty"], ["Alpha", "120"]], cell_w=60)
            page.set_rotation(90)

        path = self.build("rotated-table.pdf", [page])
        chunks = self.chunks(path, 400, 20)
        joined = self.joined(chunks)
        self.assertFalse(any(chunk.metadata.get("table_index") for chunk in chunks))
        for value in ("Text above table.", "Name", "Qty", "Alpha", "120"):
            self.assertIn(value, joined)

    def test_section_headings_survive_next_to_a_table(self):
        def page(page):
            page.insert_text((60, 40), "# 第一章 总则", fontname="china-s")
            rule_table(page, 60, 60, 2, 2, [["项", "值"], ["甲", "1"]], font="china-s")

        path = self.build("section.pdf", [page])
        chunks = self.chunks(path)
        table = self.table_chunks(chunks, 1)
        self.assertTrue(table)
        self.assertTrue(all("第一章 总则" in chunk.content for chunk in table))
        self.assertTrue(all(chunk.metadata.get("section") == "第一章 总则" for chunk in table))

    def test_long_table_stays_within_budget_and_repeats_the_header(self):
        rows = [[f"item{index:03d}", str(index)] for index in range(30)]

        def page(page):
            rule_table(page, 40, 60, 30, 2, rows, cell_w=120, cell_h=16)

        path = self.build("long.pdf", [page])
        chunks = self.chunks(path, 120, 10)
        table = self.table_chunks(chunks, 1)
        self.assertGreater(len(table), 3)
        for chunk in table:
            self.assertIn("item000 | 0", chunk.content)
            self.assertLessEqual(len(chunk.content), 120)
        self.assertIn("item029", self.joined(table))


class PdfFailureBoundaryTests(PdfFixture):
    def test_table_detection_failure_keeps_the_whole_page_text(self):
        path = self.build("detect-fail.pdf", [self.table_page])
        with patch.object(pymupdf.Page, "find_tables",
                          side_effect=RuntimeError("C:/secret/path.pdf boom")):
            chunks = self.chunks(path, 400, 20)
        self.assertFalse(any(chunk.metadata.get("table_index") for chunk in chunks))
        joined = self.joined(chunks)
        for text in ("Context before first table.", "Alpha", "North", "120", "Beta", "240"):
            self.assertIn(text, joined)

    def test_single_table_extraction_failure_keeps_only_that_table_text(self):
        def page(page):
            page.insert_text((60, 45), "Lead.")
            rule_table(page, 60, 60, 2, 2, [["First", "One"], ["a", "1"]])
            rule_table(page, 60, 220, 2, 2, [["Second", "Two"], ["b", "2"]])

        path = self.build("one-fail.pdf", [page])
        real_extract = pymupdf.table.Table.extract
        failed = []

        def flaky(self):
            rows = real_extract(self)
            # 只让第一张表失败：header 计算内部也会调用 extract，因此按内容判定。
            if rows and rows[0] and rows[0][0] == "First":
                failed.append(rows[0][0])
                raise RuntimeError("extract failed for the first table")
            return rows

        with patch.object(pymupdf.table.Table, "extract", flaky):
            chunks = self.chunks(path, 400, 20)
        self.assertTrue(failed)  # 故障注入确实命中了第一张表
        indices = {chunk.metadata.get("table_index") for chunk in chunks if chunk.metadata.get("table_index")}
        self.assertEqual(indices, {1})
        joined = self.joined(chunks)
        # 失败的表格保留原文（逐行文本），成功表仍按表格分块且表头只出现一次。
        for text in ("First", "One", "a\n", "\n1\n"):
            self.assertIn(text, joined + "\n")
        self.assertEqual(joined.count("Second | Two"), 1)
        self.assertIn("b | 2", joined)

    def test_header_failure_on_one_table_also_falls_back_to_text(self):
        path = self.build("header-fail.pdf", [self.table_page])
        with patch.object(pymupdf.table.Table, "header",
                          new_callable=PropertyMock,
                          side_effect=RuntimeError("header boom")):
            chunks = self.chunks(path, 400, 20)
        self.assertFalse(any(chunk.metadata.get("table_index") for chunk in chunks))
        self.assertIn("Alpha", self.joined(chunks))

    def test_legacy_find_tables_signature_still_extracts_real_tables(self):
        """旧版 PyMuPDF 没有 ``use_layout``：适配器退回无参调用后仍须真实识别表格。

        旧签名的注入点与主代理复现一致（``Page.find_tables(self)`` 只接受 self），
        内部仍调用真实 finder 产出真实 Table，不 mock 任何表格对象。
        """
        path = self.build("legacy-signature.pdf", [self.table_page])
        original = pymupdf.table.find_tables
        body_calls = []
        received = []

        def legacy_find_tables(self, *args, **kwargs):
            received.append(dict(kwargs))
            if kwargs:
                raise TypeError("legacy_find_tables() got an unexpected keyword "
                                "argument 'use_layout'")
            body_calls.append(1)
            return original(self, use_layout=False)  # 旧版默认只看格线

        with patch.object(pymupdf.Page, "find_tables", legacy_find_tables):
            segments = list(iter_pdf_segments(path))
        tables = [segment for segment in segments if isinstance(segment, PdfTableSegment)]
        self.assertEqual(len(body_calls), 1)  # 旧签名真的进入实际提取（不是永久回退）
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].rows[0], ("Plan", "Owner", "Budget"))
        self.assertEqual([row[0] for row in tables[0].rows[1:]], ["Alpha", "Beta"])
        # 先按新签名试一次（显式禁用 layout），只在参数不兼容时退回一次无参调用。
        self.assertEqual(received, [{"use_layout": False}, {}])
        joined = self.joined(self.chunks(path))
        self.assertEqual(joined.count("Plan | Owner | Budget"), 1)
        self.assertNotIn("Alpha\nNorth", joined)

    def test_real_type_error_inside_find_tables_still_falls_back_to_whole_page(self):
        """检测器内部的 TypeError 不是签名不兼容：只回退全文一次，不无限重试。"""
        path = self.build("finder-typeerror.pdf", [self.table_page])
        calls = []

        def broken(self, **kwargs):
            calls.append(dict(kwargs))
            raise TypeError("internal detector failure")

        with patch.object(pymupdf.Page, "find_tables", broken):
            chunks = self.chunks(path, 400, 20)
        self.assertEqual(calls, [{"use_layout": False}])  # 没有第二次尝试
        self.assertFalse(any(chunk.metadata.get("table_index") for chunk in chunks))
        joined = self.joined(chunks)
        for text in ("Context before first table.", "Alpha", "North", "240"):
            self.assertIn(text, joined)

    def test_scan_only_pdf_reports_the_physical_page_and_ocr_hint(self):
        def page(page):
            page.insert_image(pymupdf.Rect(50, 50, 250, 250), stream=tiny_png())

        path = self.build("scan.pdf", [page, page])
        with self.assertRaises(PdfParseError) as error:
            list(iter_pdf_pages(path))
        self.assertIn("第 1、2 页", error.exception.public_message)
        self.assertIn("OCR", error.exception.public_message)
        self.assertNotIn(str(self.root), error.exception.public_message)
        with self.assertRaises(PdfParseError):
            self.chunks(path)

    def test_mixed_text_and_scan_pdf_fails_instead_of_publishing_partial_pages(self):
        def text_page(text):
            def draw(page):
                page.insert_text((60, 60), text)
            return draw

        def scan_page(page):
            page.insert_image(pymupdf.Rect(50, 50, 250, 250), stream=tiny_png())

        path = self.build("text-and-scan.pdf", [text_page("page one text"), scan_page,
                                                text_page("page three text")])
        emitted = []
        with self.assertRaises(PdfParseError) as error:
            for page in iter_pdf_pages(path):
                emitted.append(page.number)
        # 前后文字页已产出（入库侧据此写入的分块属于不完整解析），但整份必须失败。
        self.assertEqual(emitted, [1, 3])
        self.assertIn("第 2 页", error.exception.public_message)

    def test_blank_only_pdf_fails_as_empty(self):
        path = self.build("empty.pdf", [lambda page: None, lambda page: None])
        with self.assertRaises(PdfParseError) as error:
            list(iter_pdf_pages(path))
        self.assertIn("全部页面为空", error.exception.public_message)
        with self.assertRaises(ValueError):
            self.chunks(path)

    def test_ocr_hint_lists_a_bounded_number_of_pages(self):
        def scan_page(page):
            page.insert_image(pymupdf.Rect(50, 50, 150, 150), stream=tiny_png())

        total = MAX_REPORTED_OCR_PAGES + 3
        path = self.build("many-scans.pdf", [scan_page] * total)
        with self.assertRaises(PdfParseError) as error:
            list(iter_pdf_pages(path))
        message = error.exception.public_message
        self.assertIn(f"等 {total} 页", message)
        # 只列出有界的前 N 个页号，不把全部页号铺满错误文案。
        self.assertIn("第 1、2、3", message)
        self.assertNotIn(f"{MAX_REPORTED_OCR_PAGES + 1}、", message)

    def test_image_drawn_outside_the_page_area_is_not_an_ocr_page(self):
        def page(page):
            page.insert_image(pymupdf.Rect(900, 900, 980, 980), stream=tiny_png())

        path = self.build("offpage-image.pdf", [page])
        with self.assertRaises(PdfParseError) as error:
            list(iter_pdf_pages(path))
        self.assertIn("全部页面为空", error.exception.public_message)

    def test_image_clipped_away_by_the_cropbox_is_not_an_ocr_page(self):
        def outside(page):
            page.insert_image(pymupdf.Rect(550, 750, 590, 790), stream=tiny_png())
            page.set_cropbox(pymupdf.Rect(100, 100, 500, 700))

        path = self.build("cropped-out.pdf", [outside])
        with self.assertRaises(PdfParseError) as error:
            list(iter_pdf_pages(path))
        self.assertIn("全部页面为空", error.exception.public_message)

        def inside(page):
            page.insert_image(pymupdf.Rect(150, 300, 200, 340), stream=tiny_png())
            page.set_cropbox(pymupdf.Rect(100, 100, 500, 700))

        path = self.build("cropped-in.pdf", [inside])
        with self.assertRaises(PdfParseError) as error:
            list(iter_pdf_pages(path))
        self.assertIn("第 1 页", error.exception.public_message)

    def test_unused_image_resource_without_placement_is_not_an_ocr_page(self):
        """资源字典里有图像、但页面上没有绘制：不能因此判 OCR。

        ``get_images()`` 只列资源清单，``get_image_info()`` 只列实际展示位置；
        这里构造一个仅被引用、从未绘制的图像资源。
        """
        document = pymupdf.open()
        try:
            first = document.new_page()
            first.insert_text((60, 60), "first page has text")
            first.insert_image(pymupdf.Rect(50, 100, 250, 300), stream=tiny_png())
            xref = first.get_images()[0][0]
            second = document.new_page()
            second.insert_text((60, 60), "second page only text")
            if document.xref_get_key(second.xref, "Resources")[0] == "xref":
                # 资源是间接对象时，改为直接重写整份 Resources（保住原有字体）。
                document.xref_set_key(second.xref, "Resources",
                                      f"<</XObject<</Im0 {xref} 0 R>>>>")
            else:
                document.xref_set_key(second.xref, "Resources/XObject", f"<</Im0 {xref} 0 R>>")
            path = self.root / "unused-resource.pdf"
            document.save(path)
        finally:
            document.close()
        with pymupdf.open(path) as pdf:
            page = pdf[1]
            self.assertTrue(page.get_images())      # 资源清单里确实有图像
            self.assertEqual(page.get_image_info(), [])  # 但页面没有绘制它
            self.assertIn("second page only text", page.get_text("text"))
        pages = list(iter_pdf_pages(path))
        # 第一页只按文字产出（不把“仅被引用”的图像当扫描页），第二页文字照常入库。
        self.assertEqual([page.number for page in pages], [1, 2])
        self.assertIn("second page only text",
                      "".join(line[2] for page in pages for line in page.lines))

    def test_rotated_scan_pages_with_bottom_image_are_detected_at_90_and_270(self):
        for rotation in (90, 270):
            with self.subTest(rotation=rotation):
                def text_page(page):
                    page.insert_text((60, 60), "text page before rotation test")

                def scan_page(page):
                    # 旋转后“可见区域底部”的窄条图像：未旋转 bbox 与 page.rect 不同坐标系。
                    page.insert_image(pymupdf.Rect(100, 700, 180, 780), stream=tiny_png())
                    page.set_rotation(rotation)

                path = self.build(f"rotated-{rotation}.pdf", [text_page, scan_page])
                with self.assertRaises(PdfParseError) as error:
                    list(iter_pdf_pages(path))
                self.assertIn("第 2 页", error.exception.public_message)

    def test_image_probe_failure_is_not_silently_treated_as_blank(self):
        def text_page(page):
            page.insert_text((60, 60), "text before the probe failure")

        def image_page(page):
            page.insert_image(pymupdf.Rect(50, 50, 250, 250), stream=tiny_png())

        path = self.build("probe-fail.pdf", [text_page, image_page])
        with patch.object(pymupdf.Page, "get_image_info",
                          side_effect=RuntimeError("probe failed at C:/secret/path")):
            # 探测不可用不能当作“没有图像”：异常上抛，由入库侧给通用安全提示，
            # 整份文档失败，绝不跳过扫描页后继续 ready。
            with self.assertRaises(RuntimeError):
                list(iter_pdf_pages(path))
            with self.assertRaises(RuntimeError):
                self.chunks(path)

    def test_rawdict_does_not_decode_image_binary(self):
        def page(page):
            page.insert_image(pymupdf.Rect(50, 50, 400, 700), stream=tiny_png())
            page.insert_text((60, 760), "caption text")

        path = self.build("big-image.pdf", [page])
        flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
        with pymupdf.open(path) as document:
            default_blocks = document[0].get_text("rawdict")["blocks"]
            stripped_blocks = document[0].get_text("rawdict", flags=flags)["blocks"]
        # 默认标志位确实会解码图像二进制；去掉该位后不再返回图像块。
        self.assertTrue(any("image" in block for block in default_blocks))
        self.assertFalse(any("image" in block for block in stripped_blocks))

        seen = []
        real = pymupdf.Page.get_text

        def spy(self, *args, **kwargs):
            if args and args[0] == "rawdict":
                seen.append(kwargs.get("flags"))
            return real(self, *args, **kwargs)

        with patch.object(pymupdf.Page, "get_text", spy):
            self.assertEqual([page.number for page in iter_pdf_pages(path)], [1])
        self.assertIn(flags, seen)  # 产品路径显式传入了不带图像位的 rawdict 标志


class PdfParserCompatTests(PdfFixture):
    def test_parser_entry_keeps_page_markers_and_physical_numbers(self):
        def write(text):
            def draw(page):
                page.insert_text((60, 60), text)
            return draw

        path = self.build("legacy.pdf", [write("first page evidence"), lambda page: None,
                                         write("third page evidence")])
        blocks = list(iter_document_blocks(path))
        self.assertTrue(blocks)
        text = "".join(block for block, _ in blocks)
        self.assertIn("<!-- page 1 -->", text)
        self.assertIn("<!-- page 3 -->", text)
        self.assertNotIn("<!-- page 2 -->", text)
        self.assertLess(text.index("first page evidence"), text.index("third page evidence"))

    def test_parser_entry_includes_table_text_and_shared_ocr_boundary(self):
        path = self.build("compat-table.pdf", [self.table_page])
        text = "".join(block for block, _ in iter_document_blocks(path))
        for value in ("Context before first table.", "Plan", "Alpha", "240"):
            self.assertIn(value, text)

        def scan_page(page):
            page.insert_image(pymupdf.Rect(50, 50, 250, 250), stream=tiny_png())

        mixed = self.build("compat-scan.pdf", [scan_page])
        with self.assertRaises(PdfParseError):
            list(iter_document_blocks(mixed))


if __name__ == "__main__":
    unittest.main()
