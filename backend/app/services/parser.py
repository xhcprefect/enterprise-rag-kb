"""文档解析：PDF / DOCX / TXT / MD / XLSX / CSV -> 纯文本。

``iter_document_blocks`` 以流式方式产出文本块，避免把 GB 级原文一次性读进内存；
``parse_document`` 保留为兼容入口（内部拼接所有块）。
"""
import codecs
from collections.abc import Iterator
from pathlib import Path

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".xlsx", ".csv"}

# 文本类文件每次读取的字节数
READ_BLOCK = 1024 * 1024
# CSV 分块读取的行数
CSV_ROWS = 50_000
# 编码校验每次读取的字节数（只做有界流式校验，不整文件读入内存）
DETECT_BLOCK = 1024 * 1024

# 候选顺序：UTF-8 优先，GBK 次之，GB18030 作为超集兜底。
ENCODINGS = ("utf-8", "gbk", "gb18030")


class UnsupportedFileType(ValueError):
    pass


class TextDecodingError(ValueError):
    """所有候选编码都无法完整解码；调用方应把它当作明确的文件错误。"""


def _decoding_error(name: str | None = None) -> TextDecodingError:
    target = f"：{name}" if name else ""
    return TextDecodingError(
        "文件编码无法识别或内容已被截断（已尝试 UTF-8 / GBK / GB18030）"
        f"{target}；请另存为 UTF-8 后重试"
    )


def _decode_sample(data: bytes, encoding: str, final: bool) -> None:
    """校验一段字节；``final=False`` 允许采样末尾截断的多字节字符。"""
    codecs.getincrementaldecoder(encoding)().decode(data, final=final)


def _parse_pdf(path: Path) -> str:
    return "".join(text for text, _ in _iter_pdf(path))


def _iter_pdf(path: Path) -> Iterator[tuple[str, float]]:
    """兼容入口：保留 ``<!-- page N -->`` 页标记契约，正文改用结构化 PDF 解析。

    表格识别与扫描页/全空 PDF 的判定与结构化入库共用同一实现；正文与表格按页内
    阅读顺序合并，页标记仍只加在实际有内容的物理页上，页码不随空白页重编号。
    分段按页产出，缓冲只保留当前页的文字。
    """
    from app.services.pdf_documents import PdfTableSegment, iter_pdf_segments

    pieces, current, fraction = [], None, 0.0
    for segment in iter_pdf_segments(path):
        if current is not None and segment.page != current:
            text = "\n".join(part for part in pieces if part).strip()
            if text:
                yield f"<!-- page {current} -->\n{text}\n\n", fraction
            pieces = []
        current, fraction = segment.page, segment.progress
        if isinstance(segment, PdfTableSegment):
            pieces.extend(" | ".join("" if cell is None else str(cell) for cell in row).strip()
                          for row in segment.rows)
        else:
            pieces.append(segment.text.rstrip("\n"))
    if current is not None:
        text = "\n".join(part for part in pieces if part).strip()
        if text:
            yield f"<!-- page {current} -->\n{text}\n\n", fraction


def _parse_docx(path: Path) -> str:
    return "".join(text for text, _ in _iter_docx(path))


def _iter_docx(path: Path) -> Iterator[tuple[str, float]]:
    from docx import Document as DocxDocument

    # python-docx 需要整体加载压缩包，这是格式限制（见计划"已知限制"）
    doc = DocxDocument(str(path))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    total = max(1, len(paragraphs) + len(doc.tables))

    done = 0
    for text in paragraphs:
        done += 1
        yield f"{text}\n", done / total

    for table in doc.tables:
        rows = []
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                rows.append(" | ".join(cells))
        done += 1
        if rows:
            yield "\n".join(rows) + "\n\n", done / total


def detect_encoding(data: bytes, final: bool = False) -> str:
    """判断编码并完整校验传入的全部字节，返回第一个可解码的候选。

    保留旧的公共调用方式：默认 ``final=False``，入参可以只是文件开头的采样，
    采样末尾被切断的多字节字符不参与判定；中间任何非法字节仍会淘汰候选。

    当 ``data`` 是完整文件内容（例如内存中的用户 CSV）时必须传
    ``final=True``：让真实 EOF 参与判定。否则末尾的两字节 UTF-8 残尾
    （如 ``e4 b8``）在 ``final=False`` 下不算错误，而它恰好是完整的 GBK
    字符，会先选中 UTF-8 再在严格解码时失败，错失真正可用的 GBK。
    所有候选都失败时抛出可读的 :class:`TextDecodingError`。
    """
    if data.startswith(codecs.BOM_UTF8):
        try:
            _decode_sample(data, "utf-8-sig", final)
        except UnicodeDecodeError as exc:
            raise _decoding_error() from exc
        return "utf-8-sig"

    for encoding in ENCODINGS:
        try:
            _decode_sample(data, encoding, final)
        except UnicodeDecodeError:
            continue
        return encoding
    raise _decoding_error()


def _read_whole_file_bounded(path: Path, encodings: tuple[str, ...]) -> set[str]:
    """单次流式读取，同时淘汰无法解码的候选；内存占用与文件大小无关。"""
    decoders = {
        encoding: codecs.getincrementaldecoder(encoding)() for encoding in encodings
    }
    with path.open("rb") as fh:
        while raw := fh.read(DETECT_BLOCK):
            for encoding in list(decoders):
                try:
                    decoders[encoding].decode(raw, final=False)
                except UnicodeDecodeError:
                    decoders.pop(encoding)
            if not decoders:
                raise _decoding_error(path.name)

    # 文件真实结束：此时仍未收尾的不完整多字节序列说明内容被截断。
    for encoding in list(decoders):
        try:
            decoders[encoding].decode(b"", final=True)
        except UnicodeDecodeError:
            decoders.pop(encoding)
    if not decoders:
        raise _decoding_error(path.name)
    return set(decoders)


def detect_file_encoding(path: Path) -> str:
    """对文件做流式全量校验，返回可用编码。

    与 ``detect_encoding`` 的区别是：这里校验的是整个文件，而不是前 64KiB。
    64KiB 的纯 ASCII 前缀无法暴露后面的中文编码，只有全量校验才能避免
    用错编码后 ``errors="replace"`` 静默破坏正文。
    """
    with path.open("rb") as fh:
        head = fh.read(3)
    if head.startswith(codecs.BOM_UTF8):
        if "utf-8-sig" not in _read_whole_file_bounded(path, ("utf-8-sig",)):
            raise _decoding_error(path.name)
        return "utf-8-sig"

    survivors = _read_whole_file_bounded(path, ENCODINGS)
    for encoding in ENCODINGS:
        if encoding in survivors:
            return encoding
    raise _decoding_error(path.name)


def _parse_text(path: Path) -> str:
    return "".join(text for text, _ in _iter_text(path))


def _iter_text(path: Path) -> Iterator[tuple[str, float]]:
    """按块解码文本文件，内存占用恒定。"""
    size = max(1, path.stat().st_size)
    encoding = detect_file_encoding(path)
    # 编码已经全量校验过，这里用严格解码；出现损坏应报错而不是静默替换。
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")

    read = 0
    with path.open("rb") as fh:
        while raw := fh.read(READ_BLOCK):
            read += len(raw)
            text = decoder.decode(raw, final=False)
            if text:
                yield text, read / size
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail, 1.0


def _parse_tabular(path: Path) -> str:
    return "".join(text for text, _ in _iter_tabular(path))


def _iter_tabular(path: Path) -> Iterator[tuple[str, float]]:
    if path.suffix.lower() == ".csv":
        yield from _iter_csv(path)
    else:
        yield from _iter_excel(path)


def _iter_csv(path: Path) -> Iterator[tuple[str, float]]:
    """CSV 分块读取；进度取自底层字节流位置，大文件进度真实可用。"""
    import io

    import pandas as pd

    size = max(1, path.stat().st_size)
    encoding = detect_file_encoding(path)

    with path.open("rb") as raw:
        # 全量校验后严格解码，避免 errors="replace" 把损坏内容静默写进知识库。
        stream = io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline="")
        try:
            reader = pd.read_csv(stream, chunksize=CSV_ROWS)
            first = True
            for df in reader:
                text = df.to_markdown(index=False)
                prefix = "" if first else "\n"
                first = False
                yield f"{prefix}{text}\n", min(1.0, raw.tell() / size)
        except pd.errors.EmptyDataError:
            return


def _iter_excel(path: Path) -> Iterator[tuple[str, float]]:
    import pandas as pd

    sheets = pd.read_excel(path, sheet_name=None)
    total = max(1, len(sheets))
    for done, (name, df) in enumerate(sheets.items(), start=1):
        yield f"## 工作表: {name}\n{df.to_markdown(index=False)}\n\n", done / total


ITERATORS = {
    ".pdf": _iter_pdf,
    ".docx": _iter_docx,
    ".txt": _iter_text,
    ".md": _iter_text,
    ".xlsx": _iter_excel,
    ".csv": _iter_csv,
}


def iter_document_blocks(path: Path) -> Iterator[tuple[str, float]]:
    """流式产出 (文本块, 完成度 0~1)。"""
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileType(f"不支持的文件类型: {ext}")

    produced = False
    for text, fraction in ITERATORS[ext](path):
        cleaned = text.replace("\x00", "")
        if cleaned.strip():
            produced = True
            yield cleaned, fraction
    if not produced:
        raise ValueError("文档内容为空或无法提取文本（扫描件需先做 OCR）")


def parse_document(path: Path) -> str:
    """按扩展名解析文档，返回纯文本（兼容旧调用）。"""
    text = "".join(block for block, _ in iter_document_blocks(path)).strip()
    if not text:
        raise ValueError("文档内容为空或无法提取文本（扫描件需先做 OCR）")
    return text
