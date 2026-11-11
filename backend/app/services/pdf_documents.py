"""可提取文字 PDF 的结构解析：真实表格识别、页内位置合并与扫描页失败边界。

只依赖 PyMuPDF 本机能力，不做 OCR、不下载模型、不调用外部服务：

- 表格用 ``Page.find_tables()`` 真实识别；每张表的 ``extract()``、``header.names``
  与 bbox 必须在 page 生命周期内复制成普通 Python 对象，之后才能在 page 关闭后使用。
- 仅当一张表完整物化成功，才把它的表格区域（含 external header 区域）从正文中
  排除；单表抽取失败保留该表原文，``find_tables`` 整体失败保留整页原文。
- 正文按字符中心是否落在表格区域判断，因此同一行/同一 span 只有部分位于表格内时，
  区域外的文字仍会保留，不会被整行误删。
- 正文与表格按页内 ``(top, left)`` 排序合并；物理页码来自 PDF 页序，不因空页重编号。
- 真正空白页跳过；有实际展示图像却没有可提取文字的页属于扫描页，整份文档明确失败
  并提示页号与 OCR 后重传。

对外只暴露两个生成器：``iter_pdf_segments``（结构化分块用）与 ``iter_pdf_pages``
（``parser._iter_pdf`` 的页标记兼容入口）。两者共用同一页扫描与失败判定。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# OCR 提示里最多列出的页号数量，保证错误文案有界。
MAX_REPORTED_OCR_PAGES = 20
# 同一行相邻文本片段之间的最小水平间距（pt），超过才补一个空格。
SPACE_GAP = 0.5
# 旧版 PyMuPDF 收到未知关键字时报出的稳定片段；只用于退一次旧签名，不吞其它错误。
UNEXPECTED_LAYOUT_KWARG = "unexpected keyword argument 'use_layout'"


class PdfParseError(ValueError):
    """专用 PDF 解析错误：只有 ``public_message`` 允许写入 Document.error_message。

    消息必须由本模块用固定文案与本地页号整数拼出，绝不能包含文件路径、原文或
    第三方异常正文。
    """

    def __init__(self, public_message: str):
        super().__init__(public_message)
        self.public_message = public_message


@dataclass(frozen=True)
class PdfTextSegment:
    """正文的一行（含行尾换行），以及它在页内的排序位置。"""

    page: int
    top: float
    left: float
    text: str
    progress: float


@dataclass(frozen=True)
class PdfTableSegment:
    """一张完整物化成功的表；rows 已含表头行（外部表头在首行），None 保持为空。"""

    page: int
    top: float
    left: float
    rows: tuple
    table_index: int
    progress: float


@dataclass(frozen=True)
class PdfPage:
    """一页的扫描结果：表格区域外的正文行、成功表格与页内位置。

    ``scanned=True`` 表示该页只有实际展示的图像、没有可提取文字；这类页会被
    ``iter_pdf_pages`` 汇总成一次明确的 OCR 失败，不会产出内容。
    """

    number: int
    progress: float
    lines: tuple
    tables: tuple
    scanned: bool = False


@dataclass(frozen=True)
class _Table:
    rows: tuple
    bbox: tuple
    header_bbox: tuple | None


def _finite(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and abs(number) != float("inf")


def _rect(pymupdf, values):
    try:
        rect = pymupdf.Rect(values)
    except Exception:  # noqa: BLE001 - 畸形 bbox 只说明该对象不可用
        return None
    if not all(map(_finite, (rect.x0, rect.y0, rect.x1, rect.y1))):
        return None
    return rect


def _shown_image(pymupdf, page) -> bool:
    """页面是否有实际展示的图像（含 inline），只看与可见页区域相交的有效 bbox。

    与 ``get_images()`` 的资源清单不同：资源被引用但未绘制、或绘制在页区域之外，
    都不算可见，不据此要求 OCR；也不加载任何图像二进制。

    坐标系：除 ``page.rect`` / ``page.bound()`` 外的页面坐标均未应用 ``/Rotate``，
    而 ``page.rect`` 会随旋转改变宽高。因此先把图像 bbox 乘以
    ``page.rotation_matrix`` 再与 ``page.rect`` 求交；这样 90/270 度的扫描页不会
    因坐标系不同被误判为没有图像（cropbox 的偏移同样包含在该变换里）。

    ``get_image_info`` 自身失败时**不能**当作“没有图像”：否则扫描页会被静默跳过、
    文档照常 ready。这里让异常直接向上传播，由入库侧按未知异常给出通用安全提示
    （不伪造 OCR 页号）。
    """
    area = page.rect
    for info in page.get_image_info() or ():
        bbox = info.get("bbox") if isinstance(info, dict) else None
        if not bbox or len(bbox) != 4:
            continue
        rect = _rect(pymupdf, bbox)
        if rect is None or rect.is_empty:
            continue
        visible = rect * page.rotation_matrix
        if not all(map(_finite, (visible.x0, visible.y0, visible.x1, visible.y1))):
            continue
        overlap = visible & area
        if not overlap.is_empty and overlap.width > 0 and overlap.height > 0:
            return True
    return False


def _cell_text(value) -> str:
    return "" if value is None else str(value).strip()


def _materialize(table):
    """在 page 存活期间完整复制一张表；失败由调用方按“保留原文”处理。"""
    headers = list(table.header.names or ())
    external = bool(table.header.external)
    rows = [list(row) for row in table.extract()]
    if external:
        # 外部表头不在 extract() 的第一行，只能单独补一次，不能重复内部表头。
        rows = [headers] + rows
    if not any(_cell_text(cell) for row in rows for cell in row):
        # 全空表没有可物化的内容：不能借“成功”把这块区域的原文也排除掉。
        raise ValueError("表格没有可复制的内容")
    bbox = tuple(table.bbox)
    header_bbox = tuple(table.header.bbox) if external else None
    return _Table(tuple(tuple(cell for cell in row) for row in rows), bbox, header_bbox)


def _blocked_rects(pymupdf, tables) -> list:
    rects = []
    for table in tables:
        for values in (table.bbox, table.header_bbox):
            if values is None:
                continue
            rect = _rect(pymupdf, values)
            if rect is not None and not rect.is_empty:
                rects.append(rect)
    return rects


def _covered(rects, x: float, y: float) -> bool:
    return any(rect.x0 <= x <= rect.x1 and rect.y0 <= y <= rect.y1 for rect in rects)


def _span_pieces(span, blocked):
    """按字符中心把 span 切成表格内（丢弃）与表格外（保留）的片段。"""
    pieces, run, left, right = [], [], None, None
    for char in span.get("chars") or ():
        text = char.get("c")
        if not text:
            continue
        bbox = char.get("bbox") or span.get("bbox") or ()
        if len(bbox) != 4:
            continue
        x, y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        if blocked and _covered(blocked, x, y):
            if run:
                pieces.append((left, right, "".join(run)))
                run, left, right = [], None, None
            continue
        if not run:
            left = bbox[0]
        right = bbox[2]
        run.append(text)
    if run:
        pieces.append((left, right, "".join(run)))
    return pieces


def _join(pieces):
    """拼接同一行保留片段，片段间距足够大时补空格，避免单词被粘在一起。"""
    text, last_right = "", None
    for left, right, value in pieces:
        if (last_right is not None and left - last_right > SPACE_GAP and text
                and not text.endswith(" ") and not value.startswith(" ")):
            text += " "
        text += value
        last_right = right
    return text.rstrip()


def _text_lines(raw, blocked):
    """按行收集表格区域之外的正文，(top, left) 用于与表格一起排序。"""
    lines = []
    for block in raw.get("blocks") or ():
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or ():
            pieces = []
            for span in line.get("spans") or ():
                pieces.extend(_span_pieces(span, blocked))
            if not pieces:
                continue
            text = _join(pieces)
            if not text:
                continue
            box = line.get("bbox") or ()
            top = float(box[1]) if len(box) == 4 and _finite(box[1]) else pieces[0][1]
            lines.append((top, float(pieces[0][0]), text))
    lines.sort(key=lambda item: (item[0], item[1]))
    return tuple(lines)


def _has_text(raw) -> bool:
    for block in raw.get("blocks") or ():
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or ():
            for span in line.get("spans") or ():
                if any(str(char.get("c", "")).strip() for char in span.get("chars") or ()):
                    return True
    return False


def _find_tables(pymupdf, page):
    """真实调用 ``find_tables``；兼容尚未提供 ``use_layout`` 的旧版 PyMuPDF。

    ``requirements.txt`` 允许 ``pymupdf>=1.25.1``，而 ``use_layout`` 参数是较新版本
    才加入的。旧版收到该关键字会立即在参数绑定阶段抛标准 TypeError；这里只在该
    错误明确指向 ``use_layout`` 时退回一次无参调用（旧版行为本来就是只看格线），
    其它 TypeError（检测器内部真实故障）原样上抛，走既有的整页正文回退，不会重试。
    """
    try:
        return page.find_tables(use_layout=False)
    except TypeError as exc:
        if UNEXPECTED_LAYOUT_KWARG not in str(exc):
            raise
    return page.find_tables()


def _scan_page(pymupdf, page, number: int, progress: float):
    """扫描一页；真正空白页返回 None（跳过且不重编号）。"""
    # 默认的 TEXTFLAGS_RAWDICT 含 TEXT_PRESERVE_IMAGES，会把整页图像的二进制解码
    # 进返回结构（扫描件可达数十 MB）。这里显式去掉该位：正文只需要字符，
    # “是否为扫描页”一律用 get_image_info 的位置信息判断。
    raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    has_text = _has_text(raw)
    if not has_text and not _shown_image(pymupdf, page):
        return None
    if not has_text:
        # 有实际展示图像、但没有可提取文字：交由调用方汇总为 OCR 失败。
        return PdfPage(number, progress, (), (), scanned=True)
    tables = []
    if page.rotation == 0:
        # 旋转页（/Rotate != 0）不做表格切分：PyMuPDF 检测期间会临时把旋转归零，
        # 于是 Table.bbox 落在另一套坐标系里（用 derotation_matrix 才能对回本页文字），
        # 而 extract() 的单元格顺序也随旋转被打乱（实测 90/180/270 度行列互换）。
        # 强行排除会删掉错误区域的正文，还可能与错误单元格重复。宁可整页按正文保留：
        # 内容不丢，也不会把错位表格当成结构化数据。
        try:
            # 新版本显式 use_layout=False：只看页面矢量格线，不让可选布局分析器
            # （ONNX）的"表格区域"门禁把真实存在的格线表整页丢弃；旧版本退回其
            # 无参签名（旧版本来就只看格线），不会静默变成永久正文回退。
            candidates = list(_find_tables(pymupdf, page).tables)
        except Exception:  # noqa: BLE001 - 整体检测失败必须保留整页原文
            candidates = []
        for candidate in candidates:
            try:
                tables.append(_materialize(candidate))
            except Exception:  # noqa: BLE001 - 单表失败只保留该表原文
                continue
    # table_index 与阅读顺序一致：按 (top, left) 稳定排序后再编号。
    tables.sort(key=lambda item: (item.bbox[1], item.bbox[0]))
    return PdfPage(number, progress, _text_lines(raw, _blocked_rects(pymupdf, tables)), tuple(tables))


def _ocr_message(pages) -> str:
    shown = pages[:MAX_REPORTED_OCR_PAGES]
    listed = "、".join(str(number) for number in shown)
    pages_hint = (f"第 {listed} 页" if len(pages) <= len(shown)
                  else f"第 {listed} 等 {len(pages)} 页")
    return (f"{pages_hint}只有图片、没有可提取的文字，无法建立索引；"
            "请先对这些页面做 OCR 后重新上传")


def iter_pdf_pages(path: Path):
    """按物理页顺序产出 :class:`PdfPage`；扫描页需 OCR 时在末尾抛 ``PdfParseError``。

    空白页不产出；有可见图像但无文字的页会被记下并让整份文档失败（不静默跳过）。
    整份 PDF 既无文字也无表格时同样抛 ``PdfParseError``。

    注意：前页内容在报错前已经产出。调用方（入库）据此写入的分块属于不完整解析，
    失败收尾必须保持 ``parse_done=False``、不向量化、不发布。
    """
    import pymupdf

    scanned, produced = [], False
    with pymupdf.open(path) as document:
        total = max(1, document.page_count)
        for number, page in enumerate(document, 1):
            scan = _scan_page(pymupdf, page, number, number / total)
            if scan is None:
                continue
            if scan.scanned:
                scanned.append(number)
                continue
            if scan.lines or scan.tables:
                produced = True
            yield scan
    if scanned:
        raise PdfParseError(_ocr_message(scanned))
    if not produced:
        raise PdfParseError("PDF 中没有可提取的文字或表格（全部页面为空），请检查文件后重新上传")


def iter_pdf_segments(path: Path):
    """结构化入口：正文行与成功表格按页内 ``(top, left)`` 顺序合并产出。"""
    table_no = 0
    for page in iter_pdf_pages(path):
        items = [(top, left, 0, text) for top, left, text in page.lines]
        for table in page.tables:
            table_no += 1
            items.append((table.bbox[1], table.bbox[0], 1, (table, table_no)))
        items.sort(key=lambda item: (item[0], item[1], item[2]))
        for top, left, kind, payload in items:
            if kind == 0:
                yield PdfTextSegment(page.number, top, left, payload + "\n", page.progress)
            else:
                table, index = payload
                yield PdfTableSegment(page.number, top, left, table.rows, index, page.progress)
