"""入库失败的稳定分类与有界退避；不保存或回显异常正文。

只把明确的输入错误和鉴权错误判为永久失败。未知异常有限重试，不能凭
同名异常、任意三位数字或正文中的关键词把可恢复故障判成永久失败。
"""
from __future__ import annotations

import errno
import re
from zipfile import BadZipFile

import httpx
from sqlalchemy.exc import SQLAlchemyError

FAILURE_SOURCE_UNAVAILABLE = "source_unavailable"
FAILURE_TIMEOUT = "timeout"
FAILURE_NETWORK = "network"
FAILURE_RATE_LIMITED = "rate_limited"
FAILURE_STORAGE = "storage"
FAILURE_DATABASE = "database"
FAILURE_UNSUPPORTED_FORMAT = "unsupported_format"
FAILURE_EMPTY_CONTENT = "empty_content"
FAILURE_CORRUPT_FILE = "corrupt_file"
FAILURE_FILE_MISSING = "file_missing"
FAILURE_AUTHENTICATION = "authentication"
FAILURE_UNKNOWN = "unknown"

FAILURE_CODES = (
    FAILURE_SOURCE_UNAVAILABLE, FAILURE_TIMEOUT, FAILURE_NETWORK,
    FAILURE_RATE_LIMITED, FAILURE_STORAGE, FAILURE_DATABASE,
    FAILURE_UNSUPPORTED_FORMAT, FAILURE_EMPTY_CONTENT, FAILURE_CORRUPT_FILE,
    FAILURE_FILE_MISSING, FAILURE_AUTHENTICATION, FAILURE_UNKNOWN,
)
PERMANENT_FAILURES = frozenset((
    FAILURE_UNSUPPORTED_FORMAT, FAILURE_EMPTY_CONTENT, FAILURE_CORRUPT_FILE,
    FAILURE_FILE_MISSING, FAILURE_AUTHENTICATION,
))
RETRYABLE_FAILURES = frozenset(FAILURE_CODES) - PERMANENT_FAILURES

_EMPTY_MESSAGES = frozenset((
    "切分后无有效内容",
    "文档内容为空或无法提取文本（扫描件需先做 OCR）",
))
_STORAGE_ERRNOS = {errno.EACCES, errno.EPERM, errno.ENOSPC, errno.EROFS, errno.EDQUOT}
# cloud_models._format_error 的固定模板；只解析包装层的首个状态，不读上游正文。
_CLOUD_STATUS = re.compile(r"^\S+ 返回 (\d{3})：")
_CLOUD_AUTH = re.compile(r"^\S+ 鉴权失败（401）：API Key 无效或已过期$")
_CLOUD_MISSING_KEY = re.compile(r"^未配置 \S+ 的 API Key，请在 backend/\.env 中填写后重启服务$")
_CLOUD_RATE = re.compile(r"^\S+ 触发限流（429）：")
_CLOUD_TIMEOUT = re.compile(r"^调用 [^\r\n]+ 失败：请求超时（[0-9.]+s）$")
_CLOUD_NETWORK = re.compile(r"^调用 [^\r\n]+ 失败：网络错误：")


def _error_text(exc: BaseException) -> str:
    """有界读取仅供匹配本地固定模板；自定义 __str__ 失败也不能破坏收尾。"""
    try:
        return str(exc)[:2000]
    except Exception:  # noqa: BLE001
        return ""


def _status_code(exc: BaseException) -> int | None:
    """仅信任受支持客户端的响应属性，绝不读取任意异常的数字/同名属性。"""
    from ollama import ResponseError

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    if isinstance(exc, ResponseError):
        return exc.status_code
    return None


def _from_status(status: int) -> str:
    if status in {401, 403}:
        return FAILURE_AUTHENTICATION
    if status == 429:
        return FAILURE_RATE_LIMITED
    if status == 408:
        return FAILURE_TIMEOUT
    if status in {409, 425} or 500 <= status <= 599:
        return FAILURE_SOURCE_UNAVAILABLE
    return FAILURE_UNKNOWN


def classify_failure(exc: BaseException, *, parsing: bool = False) -> str:
    """按真实异常类型/固定解析提示分类；未知错误由任务上限兜底。

    ``parsing`` 仅用于 OOXML 解析器把缺失容器部件表达成 KeyError/OSError
    的窄分支；模型/数据库中同样的通用异常不能因此误判文件损坏。
    """
    from docx.opc.exceptions import PackageNotFoundError
    from lxml.etree import XMLSyntaxError
    from openpyxl.utils.exceptions import InvalidFileException

    from app.services.cloud_models import CloudModelError, EmbeddingStopped
    from app.services.parser import TextDecodingError, UnsupportedFileType
    from app.services.pdf_documents import PdfParseError

    if isinstance(exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
        return FAILURE_FILE_MISSING
    if isinstance(exc, PermissionError) or isinstance(exc, OSError) and exc.errno in _STORAGE_ERRNOS:
        return FAILURE_STORAGE
    if isinstance(exc, UnsupportedFileType):
        return FAILURE_UNSUPPORTED_FORMAT
    if isinstance(exc, (TextDecodingError, UnicodeDecodeError, PdfParseError,
                        BadZipFile, PackageNotFoundError, XMLSyntaxError, InvalidFileException)):
        return FAILURE_CORRUPT_FILE

    text = _error_text(exc)
    if isinstance(exc, ValueError) and text in _EMPTY_MESSAGES:
        return FAILURE_EMPTY_CONTENT
    if parsing:
        if isinstance(exc, ValueError) and text == "不支持的文档类型":
            return FAILURE_UNSUPPORTED_FORMAT
        if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str):
            part = exc.args[0]
            if part in {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "xl/workbook.xml"} or re.fullmatch(
                r"There is no item named '(?:\[Content_Types\]\.xml|_rels/\.rels|word/document\.xml|xl/workbook\.xml)' in the archive",
                part,
            ):
                return FAILURE_CORRUPT_FILE
        if isinstance(exc, OSError) and text == "File contains no valid workbook part":
            return FAILURE_CORRUPT_FILE
        if isinstance(exc, ValueError) and text.startswith("file '") and " is not a Word file, content type is '" in text:
            return FAILURE_CORRUPT_FILE

    # SQLAlchemy 的连接/池/事务异常属于数据库，不与文件或模型的错误混淆。
    if isinstance(exc, SQLAlchemyError):
        return FAILURE_DATABASE
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return FAILURE_TIMEOUT
    if isinstance(exc, (ConnectionError, httpx.RequestError)):
        return FAILURE_NETWORK
    status = _status_code(exc)
    if status is not None:
        return _from_status(status)
    if isinstance(exc, EmbeddingStopped):
        return FAILURE_UNKNOWN  # 调用方在失败分类之前按停机处理。
    if isinstance(exc, CloudModelError):
        if _CLOUD_AUTH.match(text) or _CLOUD_MISSING_KEY.match(text):
            return FAILURE_AUTHENTICATION
        match = _CLOUD_STATUS.match(text)
        if match:
            return _from_status(int(match.group(1)))
        if _CLOUD_RATE.match(text):
            return FAILURE_RATE_LIMITED
        if _CLOUD_TIMEOUT.match(text):
            return FAILURE_TIMEOUT
        if _CLOUD_NETWORK.match(text):
            return FAILURE_NETWORK
        return FAILURE_SOURCE_UNAVAILABLE
    return FAILURE_UNKNOWN


def is_retryable(code: str | None) -> bool:
    """缺失/未知代号有限重试；已知永久错误不自动排队。"""
    return code not in PERMANENT_FAILURES


def retry_delay_seconds(attempts: int, *, base: int, maximum: int) -> int:
    """第 n 次失败等待 base * 2**(n-1) 秒，夹到配置上限。"""
    if attempts < 1:
        return 0
    return min(maximum, base * (2 ** min(attempts - 1, 30)))
