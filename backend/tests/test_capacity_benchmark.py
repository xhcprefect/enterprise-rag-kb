"""T2 CLI/SSE/配置边界的离线回归；不访问真实 API、模型或生产数据库。"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.capacity_benchmark import (  # noqa: E402
    BenchmarkInputError,
    SseParser,
    SseProtocolError,
    load_config,
    normalize_api_base_url,
    percentile,
    validate_output_path,
)


def _load_cli():
    name = "_benchmark_rag_cli_t2"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, BACKEND / "scripts" / "benchmark_rag.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


cli = _load_cli()
REAL_ASYNC_CLIENT = httpx.AsyncClient


def envelope(data):
    return httpx.Response(200, json={"code": 0, "success": True, "message": "ok", "data": data})


class TempWorkspace:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def close(self):
        self._tmp.cleanup()

    def document(self, name="sample.txt", content=b"hello benchmark\n"):
        path = self.root / name
        path.write_bytes(content)
        return path

    def config(self, **overrides):
        value = base_config(**overrides)
        return self.write("benchmark.json", json.dumps(value, ensure_ascii=False))

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path


def base_config(**overrides):
    config = {
        "version": 1,
        "api_base_url": "https://benchmark.example.test",
        "mode": "chat",
        "auth": {
            "user_token_env": "T2_BENCH_USER_TOKEN",
            "admin_token_env": "T2_BENCH_ADMIN_TOKEN",
        },
        "stages": [{"concurrency": 1, "requests": 1}],
        "total_requests": 1,
        "request_timeout_seconds": 2,
        "run_timeout_seconds": 10,
        "ready_timeout_seconds": 2,
        "planned_scale": {"documents": 1, "users": 1},
        "question": {"code": "baseline", "text": "SECRET_QUESTION_TEXT_ABC"},
        "cost": {"currency": "CNY", "max_cny": "0.50"},
        "documents": [],
        "limits": {
            "max_http_requests": 40,
            "max_upload_files": 2,
            "max_upload_bytes": 1024 * 1024,
            "max_stream_bytes": 1024 * 1024,
            "max_sse_events": 100,
        },
    }
    config.update(overrides)
    return config


def run_cli(argv, *, env=None, transport=None):
    out = io.StringIO()
    token_env = {
        "T2_BENCH_USER_TOKEN": "user-token-123456",
        "T2_BENCH_ADMIN_TOKEN": "admin-token-123456",
    }
    if env is not None:
        token_env.update(env)
    patch_client = patch.object(cli.httpx, "AsyncClient")
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, token_env))
        if transport is not None:
            factory = stack.enter_context(patch_client)
            factory.side_effect = lambda **kwargs: REAL_ASYNC_CLIENT(transport=transport, **kwargs)
        else:
            factory = stack.enter_context(patch_client)
            factory.side_effect = AssertionError("HTTP client must not be constructed")
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
    text = out.getvalue()
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        report = None
    return code, report, text


def fingerprint_of(root: Path, config_path: Path) -> str:
    config = load_config(config_path)
    return config.target_fingerprint


class StrictConfigTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()

    def tearDown(self):
        self.ws.close()

    def _load(self, value):
        path = self.ws.write("benchmark.json", json.dumps(value, ensure_ascii=False))
        return load_config(path)

    def test_valid_config_is_strictly_normalized(self):
        config = self._load(base_config(api_base_url="https://EXAMPLE.test:443/"))
        self.assertEqual(config.api_base_url, "https://example.test")
        self.assertTrue(config.target_fingerprint.startswith("sha256:"))
        self.assertEqual(config.total_requests, 1)

    def test_unknown_keys_duplicates_and_non_finite_numbers_are_rejected(self):
        with self.assertRaises(BenchmarkInputError) as error:
            self._load(base_config(extra_key=True))
        self.assertEqual((error.exception.field, error.exception.reason), ("config", "unknown_key"))

        duplicate = self.ws.write(
            "duplicate.json",
            '{"version":1,"version":1}',
        )
        with self.assertRaises(BenchmarkInputError) as error:
            load_config(duplicate)
        self.assertEqual(error.exception.reason, "duplicate_key")

        for token in ("NaN", "Infinity", "-Infinity"):
            path = self.ws.write("constant.json", '{"version":1,"x":' + token + "}")
            with self.assertRaises(BenchmarkInputError) as error:
                load_config(path)
            self.assertEqual(error.exception.reason, "non_finite_number")

    def test_bool_float_and_out_of_range_numbers_are_rejected(self):
        stage = {"concurrency": True, "requests": 1}
        with self.assertRaises(BenchmarkInputError):
            self._load(base_config(stages=[stage], total_requests=1))
        stage = {"concurrency": 1.0, "requests": 1}
        with self.assertRaises(BenchmarkInputError):
            self._load(base_config(stages=[stage], total_requests=1))
        with self.assertRaises(BenchmarkInputError):
            self._load(base_config(stages=[{"concurrency": 9, "requests": 1}]))
        with self.assertRaises(BenchmarkInputError):
            self._load(base_config(stages=[{"concurrency": 1, "requests": 21}], total_requests=21))

    def test_ladder_must_be_non_decreasing_and_total_must_match(self):
        with self.assertRaises(BenchmarkInputError) as error:
            self._load(base_config(stages=[{"concurrency": 2, "requests": 1},
                                           {"concurrency": 1, "requests": 1}], total_requests=2))
        self.assertEqual(error.exception.field, "stages.concurrency")
        with self.assertRaises(BenchmarkInputError) as error:
            self._load(base_config(total_requests=2))
        self.assertEqual(error.exception.field, "total_requests")

    def test_url_only_allows_one_https_target_or_loopback_http(self):
        for value in ("http://benchmark.example.test", "https://user@host.test",
                      "https://host.test/path", "https://host.test?x=1",
                      "https://host.test#fragment", "ftp://host.test",
                      "https://host.test:0", "https://host.test:99999",
                      "https://bad_host.test"):
            with self.subTest(value=value), self.assertRaises(BenchmarkInputError):
                normalize_api_base_url(value)
        for value in ("https://benchmark.example.test", "https://[::1]:8443",
                      "http://127.0.0.1:8000", "http://localhost:1"):
            with self.subTest(value=value):
                normalized, digest = normalize_api_base_url(value)
                self.assertTrue(normalized.startswith(("https://", "http://")))
                self.assertTrue(digest.startswith("sha256:"))

    def test_documents_require_rag_and_bounded_files(self):
        doc = self.ws.document()
        with self.assertRaises(BenchmarkInputError) as error:
            self._load(base_config(documents=[{"path": str(doc), "code": "one"}]))
        self.assertEqual(error.exception.reason, "requires_rag_kb")

        kb = str(uuid.uuid4())
        config = self._load(base_config(mode="rag", kb_id=kb,
                                        documents=[{"path": str(doc), "code": "one"}]))
        self.assertEqual(len(config.documents), 1)
        self.assertEqual(config.documents[0].payload, b"hello benchmark\n")

        with self.assertRaises(BenchmarkInputError) as error:
            self._load(base_config(mode="rag", kb_id=kb,
                                   documents=[{"path": str(doc), "code": "one"},
                                              {"path": str(doc), "code": "two"}]))
        self.assertEqual(error.exception.reason, "duplicate")

    def test_document_count_bytes_and_extension_are_bounded(self):
        kb = str(uuid.uuid4())
        doc = self.ws.document(content=b"12345")
        with self.assertRaises(BenchmarkInputError):
            self._load(base_config(mode="rag", kb_id=kb,
                                   documents=[{"path": str(doc), "code": "one"}],
                                   limits={"max_upload_bytes": 4}))
        blocked = self.ws.document("sample.exe", b"x")
        with self.assertRaises(BenchmarkInputError):
            self._load(base_config(mode="rag", kb_id=kb,
                                   documents=[{"path": str(blocked), "code": "one"}]))

    def test_oversized_config_and_deep_json_are_rejected(self):
        path = self.ws.write("big.json", "x" * (256 * 1024 + 1))
        with self.assertRaises(BenchmarkInputError) as error:
            load_config(path)
        self.assertEqual(error.exception.reason, "too_large")

        value = current = {}
        for _ in range(20):
            current["x"] = {}
            current = current["x"]
        path = self.ws.write("deep.json", json.dumps(value))
        with self.assertRaises(BenchmarkInputError) as error:
            load_config(path)
        self.assertEqual(error.exception.reason, "too_deep")

    def test_money_is_cny_bounded_decimal_without_exponent(self):
        for value in ("1e3", "-1", "1000001", "0.1234567", " 1"):
            with self.subTest(value=value), self.assertRaises(BenchmarkInputError):
                self._load(base_config(cost={"currency": "CNY", "max_cny": value}))
        with self.assertRaises(BenchmarkInputError):
            self._load(base_config(cost={"currency": "USD", "max_cny": "1"}))


class OutputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()

    def tearDown(self):
        self.ws.close()

    def test_config_document_hardlink_and_symlink_cannot_be_overwritten(self):
        doc = self.ws.document()
        config_path = self.ws.config(mode="rag", kb_id=str(uuid.uuid4()),
                                     documents=[{"path": str(doc), "code": "one"}])
        config = load_config(config_path)
        for target in (config_path, doc):
            with self.subTest(target=target.name), self.assertRaises(BenchmarkInputError) as error:
                validate_output_path(str(target), config_path, config, overwrite=True)
            self.assertEqual(error.exception.reason, "aliases_input")

        hardlink = self.ws.root / "config-hardlink.json"
        try:
            os.link(config_path, hardlink)
        except OSError:
            hardlink = None
        if hardlink is not None:
            with self.assertRaises(BenchmarkInputError) as error:
                validate_output_path(str(hardlink), config_path, config, overwrite=True)
            self.assertEqual(error.exception.reason, "aliases_input")

        symlink = self.ws.root / "config-symlink.json"
        try:
            os.symlink(config_path, symlink)
        except OSError:
            symlink = None
        if symlink is not None:
            with self.assertRaises(BenchmarkInputError) as error:
                validate_output_path(str(symlink), config_path, config, overwrite=True)
            self.assertEqual(error.exception.reason, "aliases_input")

    def test_existing_output_requires_explicit_overwrite_and_is_unchanged_on_dry_run(self):
        config_path = self.ws.config()
        output = self.ws.write("report.json", "old-content")
        original_config = config_path.read_bytes()

        code, report, _ = run_cli(["--config", str(config_path), "--out", str(output)])
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertEqual(output.read_text(encoding="utf-8"), "old-content")
        self.assertEqual(config_path.read_bytes(), original_config)

        code, report, _ = run_cli(["--config", str(config_path), "--out", str(output),
                                   "--overwrite"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["status"], "plan-valid")
        self.assertNotEqual(output.read_text(encoding="utf-8"), "old-content")
        self.assertEqual(config_path.read_bytes(), original_config)

    def test_input_alias_as_output_exits_before_any_write(self):
        config_path = self.ws.config()
        original = config_path.read_bytes()
        code, report, text = run_cli(["--config", str(config_path), "--out", str(config_path),
                                      "--overwrite"])
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertEqual(report["reason"], "aliases_input")
        self.assertEqual(config_path.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "Windows 目标名规则")
    def test_windows_illegal_and_reserved_output_names_fail_before_http(self):
        config_path = self.ws.config()
        fingerprint = fingerprint_of(self.ws.root, config_path)
        for name in ("bad<name>.json", "CON.json", "LPT1.txt", "report. "):
            with self.subTest(name=name):
                output = self.ws.root / name
                code, report, _ = run_cli(
                    ["--config", str(config_path), "--out", str(output),
                     "--live", "--confirm-target", fingerprint, "--accept-model-cost"])
                self.assertEqual(code, cli.EXIT_INPUT)
                self.assertEqual(report["reason"], "illegal_name")
                self.assertEqual(report["actual_network_requests"], 0)
                self.assertFalse(output.exists())

    @unittest.skipUnless(os.name == "nt", "Windows 上标保留设备名")
    def test_windows_superscript_reserved_device_names_fail_before_http(self):
        config_path = self.ws.config()
        fingerprint = fingerprint_of(self.ws.root, config_path)
        # ntpath.isreserved 也接受 COM¹/LPT² 等上标变体。
        for name in ("COM¹.txt", "LPT².json", "COM³", "LPT¹"):
            with self.subTest(name=name):
                output = self.ws.root / name
                code, report, _ = run_cli(
                    ["--config", str(config_path), "--out", str(output),
                     "--live", "--confirm-target", fingerprint, "--accept-model-cost"])
                self.assertEqual(code, cli.EXIT_INPUT)
                self.assertEqual(report["reason"], "illegal_name")
                self.assertEqual(report["actual_network_requests"], 0)
                self.assertFalse(output.exists())

    @unittest.skipUnless(os.name == "nt", "Windows 只读属性")
    def test_read_only_output_with_overwrite_fails_before_http_and_preserves_bytes(self):
        config_path = self.ws.config()
        fingerprint = fingerprint_of(self.ws.root, config_path)
        output = self.ws.write("readonly.json", "ORIGINAL")
        os.chmod(output, stat.S_IREAD)
        try:
            code, report, _ = run_cli(
                ["--config", str(config_path), "--out", str(output), "--overwrite",
                 "--live", "--confirm-target", fingerprint, "--accept-model-cost"])
            self.assertEqual(code, cli.EXIT_INPUT)
            self.assertEqual(report["reason"], "read_only")
            self.assertEqual(report["actual_network_requests"], 0)
            self.assertEqual(output.read_bytes(), b"ORIGINAL")
        finally:
            os.chmod(output, stat.S_IREAD | stat.S_IWRITE)

    def test_default_out_publishes_atomically_without_overwrite_in_hardlink_dir(self):
        # 正常默认 --out 正例：在支持硬链接的自有临时目录里独占发布一次，
        # 与上面的不支持硬链接负例（not_writable）和 --overwrite 正例互补。
        config_path = self.ws.config()
        output = self.ws.root / "report.json"
        original_config = config_path.read_bytes()

        code, report, _ = run_cli(["--config", str(config_path), "--out", str(output)])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["status"], "plan-valid")
        self.assertFalse(report["measured"])
        self.assertFalse(report["acceptance"])
        self.assertEqual(report["actual_network_requests"], 0)
        # stdout 与输出文件内容一致（同一 JSON，保证可复现可核对）。
        try:
            written = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.fail(f"output missing or unreadable: {exc}")
        self.assertEqual(written, report)
        # 输入配置保持原样；发布是独占的，本次运行的临时文件已清理。
        self.assertEqual(config_path.read_bytes(), original_config)
        self.assertEqual(list(self.ws.root.glob(".ragbench*.tmp")), [])

    def test_write_report_failure_never_deletes_a_competing_output(self):
        target = self.ws.root / "competing-report.json"
        other = b"ANOTHER_PROCESS_REPORT"

        def cannot_open(*args, **kwargs):
            # 模拟“调用方尚未创建自己的输出”之前就失败，且另一进程恰好写入目标。
            target.write_bytes(other)
            if args and isinstance(args[0], int):
                os.close(args[0])
            raise PermissionError("synthetic open failure")

        with patch.object(cli, "open", side_effect=cannot_open, create=True), \
                patch.object(cli.os, "fdopen", side_effect=cannot_open):
            with self.assertRaises(PermissionError):
                cli._write_report({"synthetic": True}, target, overwrite=False)
        self.assertEqual(target.read_bytes(), other)
        self.assertEqual(list(self.ws.root.glob(".ragbench-*.tmp")), [])

    def test_unsupported_exclusive_publish_fails_before_http_and_replace_still_works(self):
        config_path = self.ws.config()
        fingerprint = fingerprint_of(self.ws.root, config_path)
        output = self.ws.root / "report.json"

        def no_hardlink(*args, **kwargs):
            raise OSError("synthetic unsupported hard links")

        with patch.object(cli.os, "link", side_effect=no_hardlink):
            # 默认独占发布依赖 os.link：文件系统不支持时必须在 HTTP 之前失败。
            code, report, _ = run_cli(
                ["--config", str(config_path), "--out", str(output),
                 "--live", "--confirm-target", fingerprint, "--accept-model-cost"])
            self.assertEqual(code, cli.EXIT_INPUT)
            self.assertEqual(report["reason"], "not_writable")
            self.assertEqual(report["actual_network_requests"], 0)
            self.assertFalse(output.exists())
        self.assertEqual(list(self.ws.root.glob(".ragbench*.tmp")), [])

        # 显式 --overwrite 走 os.replace，不依赖硬链接，仍然成功（真实 mock 传输）。
        user = str(uuid.uuid4())
        sse = (b"event: delta\ndata: {\"content\":\"answer\"}\n\n"
               b"event: done\ndata: {\"finish_reason\":\"stop\",\"truncated\":false}\n\n")

        def envelope(data):
            return httpx.Response(200, json={"code": 0, "success": True, "data": data})

        def handler(request):
            path = request.url.path
            if path == "/api/v1/auth/me":
                return envelope({"id": user, "role": "admin"})
            if path == "/api/v1/operations/summary":
                return envelope({"queue": None, "degraded": True})
            if path == "/api/v1/usage/measurement":
                return envelope({"schema": "rag-usage-measurement/v1", "currency": "CNY",
                                 "totals": {"attempts": 1, "total_cost": "0.0001000000",
                                            "known_cost_subtotal": "0.0001000000"},
                                 "coverage": {"complete": True, "status": "complete",
                                              "truncated": False},
                                 "requests": [], "documents": []})
            if path == "/api/v1/chat/stream":
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=sse)
            raise AssertionError("unexpected path")

        with patch.object(cli.os, "link", side_effect=no_hardlink):
            code, report, _ = run_cli(
                ["--config", str(config_path), "--out", str(output), "--overwrite",
                 "--live", "--confirm-target", fingerprint, "--accept-model-cost"],
                transport=httpx.MockTransport(handler))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(output.exists())


class SseParserTests(unittest.TestCase):
    def test_standard_multiline_crlf_and_unicode_events(self):
        parser = SseParser(max_bytes=4096, max_line_bytes=1024, max_events=10)
        chunks = [
            b"event: delta\r\ndata: {\"content\":\"",
            "你".encode("utf-8")[:1],
            "你".encode("utf-8")[1:] + b"\"}\r\n\r\n",
            b": keep-alive\n",
            b"event: done\ndata: {\"truncated\":false,\ndata: \"finish_reason\":\"stop\"}\n\n",
        ]
        events = []
        for chunk in chunks:
            events.extend(parser.feed(chunk))
        events.extend(parser.finish())
        self.assertEqual([event.name for event in events], ["delta", "done"])
        self.assertEqual(json.loads(events[0].data)["content"], "你")
        self.assertEqual(json.loads(events[1].data)["finish_reason"], "stop")
        self.assertEqual(parser.event_count, 2)

    def test_finish_dispatches_without_trailing_blank_line(self):
        parser = SseParser(max_bytes=128, max_line_bytes=64, max_events=2)
        self.assertEqual(parser.feed(b"event: delta\ndata: x"), [])
        events = parser.finish()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "delta")

    def test_bounds_and_invalid_utf8_fail_closed(self):
        with self.assertRaises(SseProtocolError) as error:
            SseParser(max_bytes=4, max_line_bytes=4, max_events=2).feed(b"data: 123")
        self.assertEqual(error.exception.code, "stream_too_large")
        with self.assertRaises(SseProtocolError) as error:
            SseParser(max_bytes=128, max_line_bytes=4, max_events=2).feed(b"data: 12345")
        self.assertEqual(error.exception.code, "line_too_long")
        with self.assertRaises(SseProtocolError) as error:
            SseParser(max_bytes=128, max_line_bytes=64, max_events=1).feed(
                b"data: a\n\ndata: b\n\n")
        self.assertEqual(error.exception.code, "too_many_events")
        with self.assertRaises(SseProtocolError) as error:
            SseParser(max_bytes=128, max_line_bytes=64, max_events=2).feed(b"data: \xff\n\n")
        self.assertEqual(error.exception.code, "invalid_utf8")


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()

    def tearDown(self):
        self.ws.close()

    def test_default_dry_run_makes_zero_requests_and_needs_no_tokens(self):
        config_path = self.ws.config()
        code, report, _ = run_cli(["--config", str(config_path)],
                                  env={"T2_BENCH_USER_TOKEN": "", "T2_BENCH_ADMIN_TOKEN": ""})
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["measured"])
        self.assertFalse(report["acceptance"])
        self.assertEqual(report["actual_network_requests"], 0)
        self.assertTrue(report["live_requirements"]["confirm_target_required"])

    def test_live_requires_explicit_gates_before_loading_tokens(self):
        config_path = self.ws.config()
        fingerprint = fingerprint_of(self.ws.root, config_path)
        cases = [
            ["--live"],
            ["--live", "--confirm-target", fingerprint],
            ["--live", "--confirm-target", "sha256:wrong", "--accept-model-cost"],
        ]
        for args in cases:
            with self.subTest(args=args):
                code, report, _ = run_cli(
                    ["--config", str(config_path), *args],
                    env={"T2_BENCH_USER_TOKEN": "", "T2_BENCH_ADMIN_TOKEN": ""},
                )
                self.assertEqual(code, cli.EXIT_INPUT)
                self.assertEqual(report["actual_network_requests"], 0)

    def test_live_missing_token_env_exits_before_client(self):
        config_path = self.ws.config()
        fingerprint = fingerprint_of(self.ws.root, config_path)
        code, report, _ = run_cli(
            ["--config", str(config_path), "--live", "--confirm-target", fingerprint,
             "--accept-model-cost"],
            env={"T2_BENCH_USER_TOKEN": "", "T2_BENCH_ADMIN_TOKEN": ""},
        )
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertEqual(report["field"], "auth.user_token_env")
        self.assertEqual(report["actual_network_requests"], 0)

    def test_zero_cost_budget_stops_before_any_http(self):
        config_path = self.ws.config(cost={"currency": "CNY", "max_cny": "0"})
        fingerprint = fingerprint_of(self.ws.root, config_path)
        code, report, _ = run_cli(
            ["--config", str(config_path), "--live", "--confirm-target", fingerprint,
             "--accept-model-cost"],
        )
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["status"], "stopped-budget")
        self.assertEqual(report["actual_network_requests"], 0)
        self.assertFalse(report["measured"])

    def test_error_output_redacts_question_and_never_echoes_paths(self):
        config_path = self.ws.config(api_base_url="http://benchmark.example.test")
        code, report, text = run_cli(["--config", str(config_path)])
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertEqual(report["reason"], "plain_http_must_be_loopback")
        self.assertNotIn("SECRET_QUESTION_TEXT_ABC", text)
        self.assertNotIn(str(self.ws.root), text)


class LiveMockTransportTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.user_id = uuid.uuid4()
        self.admin_id = uuid.uuid4()
        self.requests = []
        self.sse = (
            b"event: meta\ndata: {\"citations\":[]}\n\n"
            b"event: status\ndata: {\"stage\":\"retrieval\"}\n\n"
            b"event: delta\ndata: {\"content\":\"\\\"SECRET_ANSWER\\\"\"}\n\n"
            b"event: done\ndata: {\"finish_reason\":\"stop\",\"truncated\":false}\n\n"
        )

    def tearDown(self):
        self.ws.close()

    def _measurement(self, body, total="0.0001000000", coverage="complete"):
        complete = coverage == "complete"
        return {
            "schema": "rag-usage-measurement/v1",
            "generated_at": "2026-01-01T00:00:00Z",
            "window_start": body["start"],
            "window_end": body["end"],
            "currency": "CNY",
            "totals": {
                "attempts": 1, "known_cost_subtotal": total,
                "total_cost": total if complete else None,
            },
            "requests": [
                {"user_id": item["user_id"], "request_id": item["request_id"],
                 "found": True, "attempts": 1, "known_cost_subtotal": total,
                 "coverage_status": coverage, "reasons": []}
                for item in body.get("requests", [])
            ],
            "documents": [
                {"document_id": item, "exists": True, "attempts": 1,
                 "known_cost_subtotal": total, "coverage_status": coverage, "reasons": []}
                for item in body.get("document_ids", [])
            ],
            "coverage": {
                "status": coverage, "complete": complete, "truncated": False,
                "missing_requests": 0, "missing_documents": 0,
                "attempts_missing_requests": 0, "attempts_missing_documents": 0,
                "usage_missing_attempts": 0, "unpriced_attempts": 0,
                "unfinished_attempts": 0, "association_mismatch_attempts": 0,
                "currency_mismatch": False, "currencies": ["CNY"],
                "background_operations_unmeasured": ["summary", "project_embedding"],
                "notes": [],
            },
            "boundaries": [],
        }

    def _handler(self, *, total="0.0001000000", coverage="complete", queue=None,
                 chat=None, kb_kind="upload", doc_id=None):
        def handle(request: httpx.Request):
            self.requests.append(request)
            path = request.url.path
            if path == "/api/v1/auth/me":
                if request.headers.get("authorization") == "Bearer admin-token-123456":
                    return envelope({"id": str(self.admin_id), "role": "admin"})
                return envelope({"id": str(self.user_id), "role": "user"})
            if path == "/api/v1/operations/summary":
                return envelope({
                    "generated_at": "2026-01-01T00:00:00Z",
                    "queue": queue,
                    "database": {"status": "ok" if queue is not None else "unavailable"},
                    "degraded": queue is None,
                })
            if path == "/api/v1/usage/measurement":
                body = json.loads(request.content.decode("utf-8"))
                return envelope(self._measurement(body, total=total, coverage=coverage))
            if path.startswith("/api/v1/knowledge-bases") and request.method == "GET" \
                    and path == "/api/v1/knowledge-bases":
                return envelope([{"id": request.url.params.get("kb"), "kind": kb_kind}])
            if path.endswith("/documents") and request.method == "POST":
                # 未在夹具中注册的上传：视为已被服务端去重接收，上传自身不产生费用。
                return envelope({"id": doc_id or str(uuid.uuid4()), "status": "pending",
                                 "deduplicated": False, "chunk_count": 0})
            if path == "/api/v1/chat/stream":
                if chat is not None:
                    return chat(request)
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=self.sse)
            raise AssertionError(f"unexpected path: {path}")
        return handle

    def _live_args(self, config_path, *extra):
        fingerprint = fingerprint_of(self.ws.root, config_path)
        return ["--config", str(config_path), "--live", "--confirm-target", fingerprint,
                "--accept-model-cost", *extra]

    def test_single_target_success_report_is_redacted_and_costed(self):
        config_path = self.ws.config()
        code, report, text = run_cli(
            self._live_args(config_path),
            transport=httpx.MockTransport(self._handler()),
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["measured"])
        self.assertFalse(report["acceptance"])
        self.assertEqual(report["result"]["requests_issued"], 1)
        self.assertEqual(report["result"]["counts"], {"success": 1})
        self.assertEqual(report["requests"][0]["outcome"], "success")
        self.assertIsNotNone(report["requests"][0]["ttft_ms"])
        self.assertEqual(report["cost"]["total_cost"], "0.0001000000")
        self.assertNotIn("SECRET_QUESTION_TEXT_ABC", text)
        self.assertNotIn("SECRET_ANSWER", text)
        self.assertNotIn(str(self.user_id), text)
        self.assertNotIn(str(self.admin_id), text)
        self.assertNotIn("user-token-123456", text)
        self.assertNotIn("benchmark.example.test", text)
        self.assertNotIn("sample.txt", text)

        measurement_calls = [r for r in self.requests
                             if r.url.path == "/api/v1/usage/measurement"]
        self.assertGreaterEqual(len(measurement_calls), 2)
        body = json.loads(measurement_calls[-1].content.decode("utf-8"))
        self.assertEqual(body["requests"][0]["user_id"], str(self.user_id))
        self.assertRegex(body["requests"][0]["request_id"],
                         r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        # 不跟随重定向、不重试：每个路径的调用次数可解释。
        self.assertEqual(len([r for r in self.requests if r.url.path == "/api/v1/auth/me"]), 2)

    def test_missing_done_is_error_not_success(self):
        config_path = self.ws.config()
        incomplete = b"event: delta\ndata: {\"content\":\"partial\"}\n\n"

        def chat(_request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=incomplete)

        code, report, _ = run_cli(self._live_args(config_path),
                                  transport=httpx.MockTransport(self._handler(chat=chat)))
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["requests"][0]["outcome"], "error")
        self.assertEqual(report["requests"][0]["error"], "missing_done")
        # 已收到合法内容增量，故 TTFT 仍如实保留；请求整体仍判失败。
        self.assertIsNotNone(report["requests"][0]["ttft_ms"])

    def test_429_is_rejected_and_not_retried(self):
        config_path = self.ws.config()

        def chat(_request):
            return httpx.Response(429, headers={"content-type": "application/json"},
                                  json={"detail": "server text must not leak"})

        code, report, text = run_cli(self._live_args(config_path),
                                     transport=httpx.MockTransport(self._handler(chat=chat)))
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["requests"][0]["outcome"], "rejected")
        self.assertEqual(report["requests"][0]["http_status"], 429)
        self.assertEqual(len([r for r in self.requests if r.url.path == "/api/v1/chat/stream"]), 1)
        self.assertNotIn("server text must not leak", text)

    def test_redirect_is_not_followed(self):
        config_path = self.ws.config()
        redirected = []

        def chat(_request):
            redirected.append(1)
            return httpx.Response(307, headers={"location": "https://elsewhere.example.test/"})

        code, report, _ = run_cli(self._live_args(config_path),
                                  transport=httpx.MockTransport(self._handler(chat=chat)))
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["requests"][0]["error"], "redirect")
        self.assertEqual(len(redirected), 1)
        self.assertFalse(any(r.url.host == "elsewhere.example.test" for r in self.requests))

    def test_extended_sse_content_type_is_not_accepted_but_charset_parameter_is(self):
        config_path = self.ws.config()

        def chat(_request):
            return httpx.Response(
                200, headers={"content-type": "text/event-stream-other"},
                content=self.sse)

        code, report, _ = run_cli(self._live_args(config_path),
                                  transport=httpx.MockTransport(self._handler(chat=chat)))
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["requests"][0]["outcome"], "error")
        self.assertEqual(report["requests"][0]["error"], "unexpected_content_type")

        def chat_charset(_request):
            return httpx.Response(
                200, headers={"content-type": "Text/Event-Stream;  charset=utf-8"},
                content=self.sse)

        code, report, _ = run_cli(self._live_args(config_path),
                                  transport=httpx.MockTransport(self._handler(chat=chat_charset)))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["requests"][0]["outcome"], "success")

    def test_cost_threshold_stops_next_wave(self):
        config_path = self.ws.config(
            stages=[{"concurrency": 1, "requests": 1}, {"concurrency": 2, "requests": 2}],
            total_requests=3,
            cost={"currency": "CNY", "max_cny": "0.0001"},
        )
        code, report, _ = run_cli(self._live_args(config_path),
                                  transport=httpx.MockTransport(self._handler()))
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["status"], "budget-exceeded")
        self.assertEqual(report["result"]["requests_completed"], 1)
        self.assertEqual(len([r for r in self.requests if r.url.path == "/api/v1/chat/stream"]), 1)
        self.assertEqual(report["cost"]["total_cost"], "0.0001000000")

    def test_unknown_cost_stops_next_batch_and_never_becomes_zero(self):
        config_path = self.ws.config(
            stages=[{"concurrency": 1, "requests": 1}, {"concurrency": 2, "requests": 2}],
            total_requests=3,
        )
        code, report, _ = run_cli(
            self._live_args(config_path),
            transport=httpx.MockTransport(self._handler(coverage="partial", total="0.0001")),
        )
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        self.assertEqual(report["status"], "unknown-cost")
        self.assertIsNone(report["cost"]["total_cost"])
        self.assertEqual(report["result"]["requests_completed"], 1)
        self.assertEqual(len([r for r in self.requests if r.url.path == "/api/v1/chat/stream"]), 1)

    def test_operations_queue_unavailable_is_null_not_zero(self):
        config_path = self.ws.config()
        code, report, _ = run_cli(self._live_args(config_path),
                                  transport=httpx.MockTransport(self._handler(queue=None)))
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIsNone(report["operations"]["before"]["queue"])
        self.assertIsNone(report["operations"]["after"]["queue"])

    def test_upload_requires_allow_flag_and_upload_cost_gate_blocks_waves(self):
        doc = self.ws.document()
        config_path = self.ws.config(
            mode="rag", kb_id=str(uuid.uuid4()),
            documents=[{"path": str(doc), "code": "one"}],
            cost={"currency": "CNY", "max_cny": "0.00005"},
        )
        fingerprint = fingerprint_of(self.ws.root, config_path)
        code, report, _ = run_cli(
            ["--config", str(config_path), "--live", "--confirm-target", fingerprint,
             "--accept-model-cost"],
            transport=httpx.MockTransport(self._handler()),
        )
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertEqual(report["field"], "allow_upload")
        self.assertEqual(report["actual_network_requests"], 0)

        new_doc = uuid.uuid4()

        def handler(request):
            path = request.url.path
            if path == "/api/v1/knowledge-bases":
                return envelope([{"id": json.loads(Path(config_path).read_text())["kb_id"],
                                  "kind": "upload"}])
            if path.endswith("/documents") and request.method == "POST":
                self.requests.append(request)
                return envelope({"id": str(new_doc), "status": "pending",
                                 "deduplicated": False, "chunk_count": 0})
            if path.endswith(f"/documents/{new_doc}"):
                self.requests.append(request)
                return envelope({"id": str(new_doc), "status": "ready",
                                 "chunk_count": 3, "filename": "leak.txt",
                                 "stored_path": "C:/secret/leak.txt"})
            return self._handler()(request)

        code, report, text = run_cli(
            self._live_args(config_path, "--allow-upload"),
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["status"], "budget-exceeded")
        self.assertEqual(report["uploads"]["ready"], 1)
        self.assertEqual(report["uploads"]["chunks_ready_total"], 3)
        self.assertEqual(report["result"]["requests_completed"], 0)
        self.assertNotIn("leak.txt", text)
        self.assertNotIn("C:/secret", text)

    def test_ready_deadline_covers_slow_poll_headers_and_body(self):
        doc = self.ws.document()
        new_doc = uuid.uuid4()
        config_path = self.ws.config(
            mode="rag", kb_id=str(uuid.uuid4()),
            documents=[{"path": str(doc), "code": "one"}],
            ready_timeout_seconds=0.05,
        )
        polls = {"count": 0}
        measurement_bodies = []
        served_ids = []
        kb_id = json.loads(Path(config_path).read_text(encoding="utf-8"))["kb_id"]
        # 非目标请求走原 handler；chat 被刻意拒绝，使运行以失败退出码结束，
        # 而不是把上传超时误报成整体完成。
        other = self._handler(
            chat=lambda _request: httpx.Response(429, headers={"content-type": "application/json"},
                                                 json={"detail": "rejected"}))

        class SlowHeadersTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                path = request.url.path
                if request.method == "GET" and path.endswith("/documents") is False \
                        and path.startswith(f"/api/v1/knowledge-bases/{kb_id}/documents/"):
                    # 只有真正的文档轮询（服务端已按 POST 响应回传该 ID）才模拟
                    # 慢响应头；其余请求必须走原 handler，保证准入 auth/KB 完整。
                    polls["count"] += 1
                    await asyncio.sleep(0.35)
                    return envelope({"id": str(new_doc), "status": "ready", "chunk_count": 3})
                if path == "/api/v1/knowledge-bases":
                    return envelope([{"id": kb_id, "kind": "upload"}])
                if path == "/api/v1/usage/measurement":
                    measurement_bodies.append(json.loads(request.content.decode("utf-8")))
                if path.endswith("/documents") and request.method == "POST":
                    # 服务端返回的文档 ID 才是 CLI 后续轮询/计量所用的真实身份。
                    response = other(request)
                    served_ids.append(json.loads(response.content.decode("utf-8"))["data"]["id"])
                    return response
                return other(request)

        code, report, _ = run_cli(
            self._live_args(config_path, "--allow-upload"),
            transport=SlowHeadersTransport(),
        )
        self.assertGreaterEqual(polls["count"], 1)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["uploads"]["timeout"], 1)
        self.assertEqual(report["uploads"]["ready"], 0)
        upload = next(item for item in report["uploads"]["items"] if item["error"])
        self.assertEqual(upload["error"], "ready_timeout")
        # 就绪超时仍保留确认过的 documentID（服务端回传的真实 ID）：它进入最终计量请求。
        self.assertTrue(served_ids)
        self.assertIn(served_ids[0],
                      [doc_id for body in measurement_bodies
                       for doc_id in body.get("document_ids", [])])

    def test_ready_deadline_covers_slow_poll_body_stream(self):
        doc = self.ws.document()
        new_doc = uuid.uuid4()
        config_path = self.ws.config(
            mode="rag", kb_id=str(uuid.uuid4()),
            documents=[{"path": str(doc), "code": "one"}],
            ready_timeout_seconds=0.05,
        )
        body = json.dumps({"code": 0, "success": True,
                           "data": {"id": str(new_doc), "status": "ready",
                                    "chunk_count": 3}}).encode()
        kb_id = json.loads(Path(config_path).read_text(encoding="utf-8"))["kb_id"]
        # 非目标请求走原 handler；chat 被刻意拒绝，使运行以失败退出码结束，
        # 而不是把上传超时误报成整体完成。
        other = self._handler(
            chat=lambda _request: httpx.Response(429, headers={"content-type": "application/json"},
                                                 json={"detail": "rejected"}))

        class SlowBody(httpx.AsyncByteStream):
            def __init__(self):
                self.closed = False

            async def __aiter__(self):
                try:
                    for offset in range(0, len(body), 8):
                        await asyncio.sleep(0.06)
                        yield body[offset:offset + 8]
                except asyncio.CancelledError:
                    raise

            async def aclose(self):
                self.closed = True

        class SlowBodyTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.current = None

            async def handle_async_request(self, request):
                path = request.url.path
                if request.method == "GET" and path.endswith("/documents") is False \
                        and path.startswith(f"/api/v1/knowledge-bases/{kb_id}/documents/"):
                    # 只有真正的文档轮询（服务端已按 POST 响应回传该 ID）才模拟
                    # 慢响应体；其余请求必须走原 handler，保证准入 auth/KB 完整。
                    stream = SlowBody()
                    self.current = stream
                    return httpx.Response(200, headers={"content-type": "application/json"},
                                          stream=stream)
                if path == "/api/v1/usage/measurement":
                    payload = json.loads(request.content.decode("utf-8"))
                    return envelope(self._measurement(payload))
                if path == "/api/v1/knowledge-bases":
                    return envelope([{"id": kb_id, "kind": "upload"}])
                return other(request)

        transport = SlowBodyTransport()
        code, report, _ = run_cli(
            self._live_args(config_path, "--allow-upload"),
            transport=transport,
        )
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["uploads"]["timeout"], 1)
        self.assertEqual(report["uploads"]["ready"], 0)
        upload = next(item for item in report["uploads"]["items"] if item["error"])
        self.assertEqual(upload["error"], "ready_timeout")
        # 就绪截止必须真正取消/关闭慢响应体流，而不是任其残留。
        current = getattr(transport, "current", None)
        self.assertIsNotNone(current)
        self.assertTrue(current.closed)

    def test_request_timeout_is_bounded_and_reported(self):
        config_path = self.ws.config(request_timeout_seconds=0.2, run_timeout_seconds=3)

        class SlowHeaders(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.url.path == "/api/v1/chat/stream":
                    await asyncio.sleep(10)
                    return httpx.Response(
                        200, headers={"content-type": "text/event-stream"},
                        content=b"event: done\ndata: {\"finish_reason\":\"stop\",\"truncated\":false}\n\n",
                    )
                return await self._inner.handle_async_request(request)

            def __init__(self, inner):
                self._inner = inner

        inner = httpx.MockTransport(self._handler())
        started = time.monotonic()
        code, report, _ = run_cli(self._live_args(config_path),
                                  transport=SlowHeaders(inner))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["requests"][0]["outcome"], "timeout")
        self.assertEqual(report["requests"][0]["error"], "request_timeout")

    def test_shared_run_deadline_cancels_infinite_heartbeat(self):
        config_path = self.ws.config(request_timeout_seconds=5, run_timeout_seconds=0.6)

        class Infinite(httpx.AsyncByteStream):
            def __init__(self):
                self.closed = False

            async def __aiter__(self):
                while True:
                    yield b"event: status\ndata: {}\n\n"
                    await asyncio.sleep(0.02)

            async def aclose(self):
                self.closed = True

        stream = Infinite()

        class Heartbeat(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.url.path == "/api/v1/chat/stream":
                    return httpx.Response(
                        200, headers={"content-type": "text/event-stream"}, stream=stream,
                    )
                return await inner.handle_async_request(request)

        inner = httpx.MockTransport(self._handler())
        started = time.monotonic()
        code, report, _ = run_cli(self._live_args(config_path), transport=Heartbeat())
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        self.assertEqual(report["status"], "timeout")
        self.assertEqual(report["requests"][0]["outcome"], "cancelled")
        self.assertTrue(stream.closed)

    def test_oversized_stream_and_malformed_done_fail_closed(self):
        config_path = self.ws.config(limits={
            "max_http_requests": 40,
            "max_upload_files": 2,
            "max_upload_bytes": 1024 * 1024,
            "max_stream_bytes": 64,
            "max_sse_events": 100,
        })
        huge = b"event: delta\ndata: {\"content\":\"" + b"x" * 200 + b"\"}\n\n"

        def chat(_request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=huge)

        code, report, _ = run_cli(self._live_args(config_path),
                                  transport=httpx.MockTransport(self._handler(chat=chat)))
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["requests"][0]["error"], "stream_too_large")

        malformed = b"event: done\ndata: {\"truncated\":false}\n\n"

        def bad_done(_request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=malformed)

        code, report, _ = run_cli(self._live_args(config_path),
                                  transport=httpx.MockTransport(self._handler(chat=bad_done)))
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["requests"][0]["error"], "invalid_done")

    def test_events_after_done_in_the_same_chunk_are_malformed(self):
        done = b"event: done\ndata: {\"truncated\":false,\"finish_reason\":\"stop\"}\n\n"
        cases = [
            (done, "duplicate_done"),
            (b"event: error\ndata: {}\n\n", "stream_error"),
            (b"event: delta\ndata: {\"content\":\"late\"}\n\n", "event_after_done"),
        ]
        for suffix, expected in cases:
            with self.subTest(expected=expected):
                config_path = self.ws.config()

                def chat(_request, body=done + suffix):
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          content=body)

                code, report, _ = run_cli(
                    self._live_args(config_path),
                    transport=httpx.MockTransport(self._handler(chat=chat)))
                self.assertEqual(code, cli.EXIT_FAILURE)
                self.assertEqual(report["requests"][0]["error"], expected)

    def test_already_received_tail_after_done_is_never_success(self):
        done = b"event: done\ndata: {\"finish_reason\":\"stop\",\"truncated\":false}\n\n"
        tails = (
            b'event: error\ndata: {"code":"boom"}\n\n',
            b'event: error\ndata: {"code":"boom"}',
            b'event: error\ndata: {"code":"boom"}\n',
        )
        for tail in tails:
            with self.subTest(tail=tail):
                config_path = self.ws.config()

                def chat(_request, body=done + tail):
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          content=body)

                code, report, _ = run_cli(
                    self._live_args(config_path),
                    transport=httpx.MockTransport(self._handler(chat=chat)))
                self.assertEqual(code, cli.EXIT_FAILURE)
                self.assertEqual(report["requests"][0]["outcome"], "error")
                self.assertEqual(report["requests"][0]["error"], "stream_error")

    def test_done_without_blank_line_and_comment_tail_are_success(self):
        delta = b'event: delta\ndata: {"content":"ok"}\n\n'
        bodies = (
            delta + b'event: done\ndata: {"finish_reason":"stop","truncated":false}\n',
            delta + b'event: done\ndata: {"finish_reason":"stop","truncated":false}',
            delta + b'event: done\ndata: {"finish_reason":"stop","truncated":false}\n\n'
            b': keep-alive\n\n',
        )
        for body in bodies:
            with self.subTest(body=body):

                def chat(_request, payload=body):
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          content=payload)

                code, report, _ = run_cli(
                    self._live_args(self.ws.config()),
                    transport=httpx.MockTransport(self._handler(chat=chat)))
                self.assertEqual(code, cli.EXIT_OK)
                self.assertEqual(report["requests"][0]["outcome"], "success")

    def _run_unconfirmed_upload_case(self, kind: str, prior_success: bool):
        count = 3
        kb = str(uuid.uuid4())
        docs = []
        for index in range(count):
            path = self.ws.document(
                name=f"upload-{kind}-{prior_success}-{index}.txt",
                content=b"controlled fixture\n")
            docs.append({"path": str(path), "code": f"probe{index}"})
        ids = [uuid.uuid4() for _ in range(count)]
        failed_index = 2 if prior_success else 1
        tracker = {"all": 0, "posts": 0, "chat": 0}

        def handler(request):
            tracker["all"] += 1
            path = request.url.path
            if path == "/api/v1/auth/me":
                return envelope({"id": str(self.user_id), "role": "admin"})
            if path == "/api/v1/knowledge-bases":
                return envelope([{"id": kb, "kind": "upload"}])
            if path == "/api/v1/operations/summary":
                return envelope({"queue": None, "database": {"status": "unavailable"},
                                 "degraded": True})
            if path == "/api/v1/usage/measurement":
                body = json.loads(request.content.decode("utf-8"))
                return envelope(self._measurement(body))
            if path.endswith("/documents") and request.method == "POST":
                tracker["posts"] += 1
                ordinal = tracker["posts"]
                if ordinal == failed_index:
                    if kind == "http500":
                        return httpx.Response(500, json={"detail": "must not leak"})
                    if kind == "read_timeout":
                        raise httpx.ReadTimeout("must not leak", request=request)
                    if kind == "invalid_json":
                        return httpx.Response(200, content=b"not valid json")
                    if kind == "invalid_uuid":
                        return envelope({"id": "not-a-uuid", "deduplicated": False})
                return envelope({"id": str(ids[ordinal - 1]), "status": "pending",
                                 "deduplicated": False})
            if any(path.endswith("/documents/" + str(identity)) for identity in ids):
                return envelope({"status": "ready", "chunk_count": 1})
            if path == "/api/v1/chat/stream":
                tracker["chat"] += 1
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=self.sse)
            raise AssertionError("unexpected_fixture_request")

        config_path = self.ws.config(
            mode="rag", kb_id=kb, documents=docs,
            planned_scale={"documents": count, "users": 1},
            limits={"max_http_requests": 40, "max_upload_files": 5,
                    "max_upload_bytes": 1024 * 1024, "max_stream_bytes": 1024 * 1024,
                    "max_sse_events": 100},
        )
        code, report, _ = run_cli(
            self._live_args(config_path, "--allow-upload"),
            transport=httpx.MockTransport(handler),
        )
        self.assertNotEqual(code, cli.EXIT_OK)
        self.assertIsNone(report["cost"]["total_cost"])
        self.assertEqual(report["cost"]["coverage"], "unconfirmed-upload")
        self.assertEqual(report["uploads"]["new_documents"], 1 if prior_success else 0)
        self.assertEqual(report["uploads"]["confirmed_new"], 1 if prior_success else 0)
        self.assertEqual(report["uploads"]["unconfirmed"], 1)
        self.assertEqual(report["uploads"]["sent_attempts"], failed_index)
        self.assertEqual(tracker["posts"], failed_index)
        self.assertEqual(tracker["chat"], 0)
        self.assertEqual(report["actual_network_requests"], tracker["all"])

    def test_unconfirmed_upload_attempts_stop_work_and_keep_total_unknown(self):
        for prior_success in (False, True):
            for kind in ("http500", "read_timeout", "invalid_json", "invalid_uuid"):
                with self.subTest(prior_success=prior_success, kind=kind):
                    self._run_unconfirmed_upload_case(kind, prior_success)

    def test_pre_dispatch_deadline_counts_only_actual_sends(self):
        config_path = self.ws.config(run_timeout_seconds=1e-9)
        code, report, _ = run_cli(
            self._live_args(config_path),
            transport=httpx.MockTransport(self._handler()),
        )
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        self.assertEqual(self.requests, [])
        self.assertEqual(report["actual_network_requests"], 0)
        self.assertEqual(report["result"]["requests_issued"], 0)
        self.assertFalse(report["measured"])

    def test_chat_request_deadline_before_dispatch_has_no_phantom_request(self):
        config_path = self.ws.config(request_timeout_seconds=1e-9, run_timeout_seconds=10)
        code, report, _ = run_cli(
            self._live_args(config_path),
            transport=httpx.MockTransport(self._handler()),
        )
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["status"], "no-request-sent")
        chat_calls = [item for item in self.requests if item.url.path == "/api/v1/chat/stream"]
        self.assertEqual(chat_calls, [])
        self.assertEqual(report["actual_network_requests"], len(self.requests))
        self.assertEqual(report["result"]["requests_issued"], 0)
        self.assertEqual(report["result"]["requests_completed"], 0)
        self.assertEqual(report["result"]["requests_not_sent"], 1)
        self.assertFalse(report["measured"])

    def test_late_output_conflict_after_http_keeps_execution_facts(self):
        config_path = self.ws.config()
        output = self.ws.root / "late-output.json"
        other = b"ANOTHER_PROCESS_REPORT"

        def handler(request):
            if request.url.path == "/api/v1/auth/me" and not output.exists():
                output.write_bytes(other)
            return self._handler()(request)

        code, report, _ = run_cli(
            self._live_args(config_path, "--out", str(output)),
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(report["status"], "failure")
        self.assertEqual(report["reason"], "output_write_failed")
        self.assertEqual(report["actual_network_requests"], len(self.requests))
        self.assertEqual(len([item for item in self.requests
                              if item.url.path == "/api/v1/chat/stream"]), 1)
        self.assertEqual(output.read_bytes(), other)


class PercentileTests(unittest.TestCase):
    def test_empty_and_small_samples_do_not_invent_latency(self):
        self.assertIsNone(percentile([], 0.5))
        self.assertEqual(percentile([1.24], 0.95), 1.2)
        self.assertIn(percentile([1.0, 2.0], 0.5), (1.0, 2.0))

    def test_nearest_rank_uses_ceil_and_never_rounds_up_to_max(self):
        self.assertEqual(percentile([1.0, 2.0], 0.50), 1.0)
        self.assertEqual(percentile([float(value) for value in range(1, 7)], 0.50), 3.0)
        self.assertEqual(percentile([float(value) for value in range(1, 21)], 0.95), 19.0)
        self.assertEqual(percentile([float(value) for value in range(1, 41)], 0.95), 38.0)


if __name__ == "__main__":
    unittest.main()
