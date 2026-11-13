"""入库失败分类与退避：真实异常类型，无数据库或模型请求。"""
import errno
import unittest
from zipfile import BadZipFile

import httpx
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from app.services import ingest_failures as failures
from app.services.cloud_models import CloudModelError, _format_error
from app.services.parser import TextDecodingError, UnsupportedFileType
from app.services.pdf_documents import PdfParseError


class ClassifyFailureTests(unittest.TestCase):
    def test_permanent_input_problems_are_never_retried(self):
        cases = [
            (FileNotFoundError("missing"), "file_missing"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), "corrupt_file"),
            (TextDecodingError("bad encoding"), "corrupt_file"),
            (PdfParseError("PDF 已加密"), "corrupt_file"),
            (UnsupportedFileType(".exe"), "unsupported_format"),
            (BadZipFile("damaged central directory"), "corrupt_file"),
            (ValueError("文档内容为空或无法提取文本（扫描件需先做 OCR）"), "empty_content"),
            (ValueError("切分后无有效内容"), "empty_content"),
        ]
        for exc, expected in cases:
            with self.subTest(exception=type(exc).__name__):
                self.assertEqual(failures.classify_failure(exc), expected)
                self.assertFalse(failures.is_retryable(expected))

    def test_transient_typed_problems_are_retried(self):
        cases = [
            (TimeoutError("timeout"), "timeout"),
            (httpx.ReadTimeout("secret 401"), "timeout"),
            (ConnectionResetError("reset by peer"), "network"),
            (PermissionError("denied"), "storage"),
            (OSError(errno.ENOSPC, "full"), "storage"),
            (OSError(errno.EROFS, "read only"), "storage"),
            (OperationalError("SELECT", {}, Exception("connection lost")), "database"),
            (PoolTimeout("pool exhausted"), "database"),
        ]
        for exc, expected in cases:
            with self.subTest(exception=type(exc).__name__):
                self.assertEqual(failures.classify_failure(exc), expected)
                self.assertTrue(failures.is_retryable(expected))

    def test_http_status_comes_from_real_response_not_body_digits(self):
        for status, expected in ((401, "authentication"), (403, "authentication"),
                                 (429, "rate_limited"), (503, "source_unavailable"),
                                 (520, "source_unavailable"), (408, "timeout"),
                                 (400, "unknown")):
            with self.subTest(status=status):
                response = httpx.Response(status, request=httpx.Request("POST", "http://127.0.0.1:9"))
                exc = httpx.HTTPStatusError("body 401 429 503", request=response.request, response=response)
                self.assertEqual(failures.classify_failure(exc), expected)

    def test_cloud_fixed_templates_preserve_classification(self):
        cases = [(401, "authentication"), (403, "authentication"),
                 (429, "rate_limited"), (503, "source_unavailable")]
        for status, expected in cases:
            with self.subTest(status=status):
                exc = CloudModelError(_format_error("local.test", status, "401 invalid API key"))
                self.assertEqual(failures.classify_failure(exc), expected)
        self.assertEqual(failures.classify_failure(CloudModelError(
            "未配置 local.test 的 API Key，请在 backend/.env 中填写后重启服务")), "authentication")
        self.assertEqual(failures.classify_failure(CloudModelError(
            "调用 local.test 失败：请求超时（120s）")), "timeout")
        self.assertEqual(failures.classify_failure(CloudModelError(
            "调用 local.test 失败：网络错误：401")), "network")
        # 结构未知的模型错误正文包含“返回 401”也不能冒充包装层状态。
        self.assertEqual(failures.classify_failure(CloudModelError(
            "payload note 返回 401：untrusted body")), "source_unavailable")

    def test_foreign_names_numbers_and_unrelated_empty_text_are_not_permanent(self):
        for name in ("PdfParseError", "UnsupportedFileType", "TextDecodingError",
                     "BadZipFile", "CloudModelError"):
            fake = type(name, (ValueError,), {})
            code = failures.classify_failure(fake("HTTP 401 empty 429 503"))
            self.assertEqual(code, "unknown", name)
        for message in ("empty upstream response", "字段为空", "记录 401 页，重试 429 次",
                        "invalid API key", "HTTP 503"):
            self.assertEqual(failures.classify_failure(ValueError(message)), "unknown")

    def test_ooxml_missing_parts_require_actual_parsing_context(self):
        exc = KeyError("There is no item named '[Content_Types].xml' in the archive")
        self.assertEqual(failures.classify_failure(exc), "unknown")
        self.assertEqual(failures.classify_failure(exc, parsing=True), "corrupt_file")
        self.assertEqual(failures.classify_failure(ValueError("不支持的文档类型"), parsing=True), "unsupported_format")
        self.assertEqual(failures.classify_failure(KeyError("unrelated"), parsing=True), "unknown")

    def test_unknown_failures_default_to_retryable_and_never_leak(self):
        for exc in (RuntimeError("password=hunter2 /srv/private/report.pdf"), ValueError("unexpected token")):
            code = failures.classify_failure(exc)
            self.assertEqual(code, "unknown")
            self.assertTrue(failures.is_retryable(code))
        self.assertTrue(failures.is_retryable(None))
        self.assertTrue(failures.is_retryable(""))
        self.assertEqual(set(failures.FAILURE_CODES),
                         set(failures.PERMANENT_FAILURES) | set(failures.RETRYABLE_FAILURES))
        self.assertFalse(failures.PERMANENT_FAILURES & failures.RETRYABLE_FAILURES)


class RetryDelayTests(unittest.TestCase):
    def test_exponential_backoff_is_capped(self):
        self.assertEqual([failures.retry_delay_seconds(n, base=30, maximum=600)
                          for n in (1, 2, 3, 10, 10000)], [30, 60, 120, 600, 600])

    def test_non_positive_attempts_do_not_wait(self):
        for n in (0, -5):
            self.assertEqual(failures.retry_delay_seconds(n, base=30, maximum=600), 0)


if __name__ == "__main__":
    unittest.main()
