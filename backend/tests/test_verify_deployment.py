"""T5 只读部署验收 CLI 的离线/回环回归。

全部用例只绑定 ``127.0.0.1:0``，不访问外网、不读 ``.env``、不启动业务
API/worker/数据库；令牌通过 ``patch.dict(os.environ, ...)`` 注入。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _load_cli():
    name = "_verify_deployment_cli_t5"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, BACKEND / "scripts" / "verify_deployment.py")
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

# 仅用于回环测试的自签名证书（SAN=IP:127.0.0.1, DNS:localhost；CA:TRUE）。
# 运行时由标准库 ssl 加载，测试不依赖 openssl/ cryptography。
CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIBmTCCAT+gAwIBAgIUbqDAyoP6BF1N6exSsmE0qqVgyukwCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJMTI3LjAuMC4xMB4XDTIwMDEwMTAwMDAwMFoXDTQ2MDEwMTAw
MDAwMFowFDESMBAGA1UEAwwJMTI3LjAuMC4xMFkwEwYHKoZIzj0CAQYIKoZIzj0D
AQcDQgAEU2Ex+iFLUlVVqu1oyc9y0s52xxecsmMINpvdroi1Tooll4wztu6bQuPR
+SfA7+Z8iz4rdX0xlU0qAhSY5rulLqNvMG0wHQYDVR0OBBYEFEgcePcZb8KHnkKj
ApjzWhh8E8Q2MB8GA1UdIwQYMBaAFEgcePcZb8KHnkKjApjzWhh8E8Q2MA8GA1Ud
EwEB/wQFMAMBAf8wGgYDVR0RBBMwEYcEfwAAAYIJbG9jYWxob3N0MAoGCCqGSM49
BAMCA0gAMEUCIQDgkW+/rgS3qS9pecjgKTzYJ3IBw40y9LPhYsFcoXkd8wIgCRYE
RDhFqf0Q/kVPZyaL+6XZaf7dcVL6UkorFaHGe6w=
-----END CERTIFICATE-----
"""

KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgORNI1Kr8OoWB49hL
hEL36LI+5rOiVmqYOwUvR1IwTPChRANCAARTYTH6IUtSVVWq7WjJz3LSznbHF5yy
Ywg2m92uiLVOiiWXjDO27ptC49H5J8Dv5nyLPit1fTGVTSoCFJjmu6Uu
-----END PRIVATE KEY-----
"""

ADMIN_ENV = "RAG_DEPLOY_ADMIN_TOKEN"
METRICS_ENV = "RAG_DEPLOY_METRICS_TOKEN"
ADMIN_TOKEN = "t5-admin-token-value"
METRICS_TOKEN = "t5-metrics-token-value"
# 只有白名单枚举允许进入报告 detail：响应里的任意字符串一律不得外泄。
RESPONSE_SENTINEL = "PRIVATE_RESPONSE_SENTINEL_5d2c"


# ---------------------------------------------------------------------------
# 回环 HTTP/HTTPS fixture
# ---------------------------------------------------------------------------
class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # TLS 握手失败等预期错误不打印 traceback，避免污染测试输出。
        pass


class _State:
    def __init__(self):
        self.routes = {}
        self.counts = {}
        self.authorizations = {}
        self.commands = []
        self.lock = threading.Lock()

    def record(self, path, command, authorization):
        with self.lock:
            self.counts[path] = self.counts.get(path, 0) + 1
            self.authorizations.setdefault(path, []).append(authorization)
            self.commands.append(command)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "VerifyDeploymentTest/1.0"

    def log_message(self, format, *args):  # noqa: A002 - 覆盖基类签名
        pass

    def _record_and_route(self):
        state = self.server.state
        state.record(self.path, self.command, self.headers.get("Authorization"))
        route = state.routes.get(self.path)
        if route is None:
            self.send(status=404, content_type="text/plain", body=b"not found")
            return
        try:
            route(self)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._record_and_route()

    def do_POST(self):
        self._record_and_route()

    def do_PUT(self):
        self._record_and_route()

    def do_PATCH(self):
        self._record_and_route()

    def do_DELETE(self):
        self._record_and_route()

    def send(self, *, status, content_type, body, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


class LoopbackServer:
    def __init__(self, routes=None, *, tls=False):
        self.state = _State()
        self.state.routes.update(routes or {})
        self.httpd = _QuietServer(("127.0.0.1", 0), _Handler)
        self.httpd.state = self.state
        self._tls_dir = None
        self.cert_path = None
        if tls:
            self._tls_dir = tempfile.TemporaryDirectory(prefix="t5-tls-")
            cert = Path(self._tls_dir.name) / "cert.pem"
            key = Path(self._tls_dir.name) / "key.pem"
            cert.write_text(CERT_PEM, encoding="ascii")
            key.write_text(KEY_PEM, encoding="ascii")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert), str(key))
            self.httpd.socket = context.wrap_socket(
                self.httpd.socket, server_side=True)
            self.cert_path = cert
        self.scheme = "https" if tls else "http"
        self.port = self.httpd.server_address[1]
        self.origin = f"{self.scheme}://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
        if self._tls_dir is not None:
            self._tls_dir.cleanup()
        return False

    def count(self, path):
        with self.state.lock:
            return self.state.counts.get(path, 0)

    @property
    def total(self):
        with self.state.lock:
            return sum(self.state.counts.values())

    def authorizations(self, path):
        with self.state.lock:
            return list(self.state.authorizations.get(path, []))


def json_route(payload, status=200):
    def route(handler):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send(status=status, content_type="application/json", body=body)
    return route


def text_route(text, status=200):
    def route(handler):
        handler.send(status=status,
                     content_type="text/plain; version=0.0.4; charset=utf-8",
                     body=text.encode("utf-8"))
    return route


def redirect_route(location, status=302):
    def route(handler):
        handler.send(status=status, content_type="text/plain", body=b"",
                     headers={"Location": location})
    return route


def slow_route(seconds, payload=None):
    def route(handler):
        time.sleep(seconds)
        body = json.dumps(payload or {"status": "ok", "worker": "alive"}).encode()
        handler.send(status=200, content_type="application/json", body=body)
    return route


def oversized_route(size):
    def route(handler):
        body = b"x" * size
        handler.send(status=200, content_type="application/json", body=body)
    return route


def drip_route(interval, count):
    """响应头先发出，之后按固定间隔滴写响应体（永不完成的慢服务器）。

    用来证明 ``--timeout`` 约束的是**单次请求的总耗时**，而不是每次 socket IO
    各自续期：只要服务器持续滴字节，逐次 IO 超时的实现就能无限拖下去。
    """
    def route(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(count))
        handler.end_headers()
        handler.wfile.flush()
        try:
            for _ in range(count):
                handler.wfile.write(b"x")
                handler.wfile.flush()
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
    return route


def stalled_body_route(seconds, payload=None):
    """响应头与 Content-Length 已发出，随后长时间不写任何响应体。"""
    def route(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "50")
        handler.end_headers()
        handler.wfile.flush()
        time.sleep(seconds)
        if payload is not None:
            try:
                handler.wfile.write(json.dumps(payload).encode("utf-8"))
            except OSError:
                pass
    return route


class BadProtocolServer:
    """只会吐出非法状态行的服务器：opener.open 阶段就会抛 HTTPException。"""

    def __init__(self, payload=b"NOT-HTTP/9.9 999 BROKEN\r\n\r\n"):
        self.payload = payload
        self.connections = 0
        self.first_lines = []
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            with conn:
                with self._lock:
                    self.connections += 1
                try:
                    data = conn.recv(4096)
                except OSError:
                    data = b""
                if data:
                    with self._lock:
                        self.first_lines.append(
                            data.splitlines()[0].decode("ascii", "replace"))
                try:
                    conn.sendall(self.payload)
                except OSError:
                    pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        self._thread.join(timeout=5)
        return False

    def command_words(self):
        with self._lock:
            return [line.split()[0] for line in self.first_lines if line]


def metrics_text(db_available=1, queue="0", extra=""):
    lines = [
        "# HELP ragkb_db_available Whether the database summary query succeeded.",
        "# TYPE ragkb_db_available gauge",
        f"ragkb_db_available {db_available}",
    ]
    if db_available == 1 and queue is not None:
        lines += [
            "# HELP ragkb_ingest_queued_documents Documents queued for ingest.",
            "# TYPE ragkb_ingest_queued_documents gauge",
            f"ragkb_ingest_queued_documents {queue}",
        ]
    return "\n".join(lines) + "\n" + extra


def summary_data(*, degraded=False, db_status="ok", queue_present=True,
                 extra=None):
    payload = {
        "generated_at": "2026-01-01T00:00:00Z",
        "process_local": True,
        "degraded": degraded,
        "database": {"status": db_status},
        "queue": ({"state": "idle", "queued": 0} if queue_present else None),
    }
    if extra:
        payload.update(extra)
    return payload


def summary_envelope(**kwargs):
    return {
        "code": 0,
        "success": True,
        "message": "ok",
        "data": summary_data(**kwargs),
    }


def healthy_routes(*, worker="alive", db_available=1, queue="0",
                   summary=None, metrics_extra="", health_extra=None,
                   summary_extra=None):
    health = {"status": "ok", "worker": worker}
    if health_extra:
        health.update(health_extra)
    envelope = summary_envelope(extra=summary_extra)
    if summary is not None:
        envelope["data"] = summary
    return {
        "/health": json_route(health),
        "/api/v1/operations/metrics": text_route(
            metrics_text(db_available, queue, metrics_extra)),
        "/api/v1/operations/summary": json_route(envelope),
        "/-/ready": text_route("ready"),
    }


# ---------------------------------------------------------------------------
# 证据构造
# ---------------------------------------------------------------------------
def evidence_element(kind, *, ref=None, captured_at="2026-01-01T00:00:00Z"):
    return {
        "kind": kind,
        "ref": f"ref-{kind}" if ref is None else ref,
        "sha256": "0" * 64,
        "captured_at": captured_at,
    }


def evidence_item(kinds, *, status="pass", level="production", notes=""):
    return {
        "status": status,
        "level": level,
        "evidence": [evidence_element(kind) for kind in kinds],
        "notes": notes,
    }


def full_evidence(version="1.2.3", *, notes=""):
    return {
        "schema": "ragkb-deployment-acceptance/v1",
        "environment": {
            "name": "prod-eu",
            "version": version,
            "deployed_at": "2026-01-01T00:00:00Z",
            "operator": "ops",
        },
        "items": {
            "version_identity": evidence_item(["version"], notes=notes),
            "backup_restore_rto_rpo": evidence_item(
                ["restore_drill", "checksum"], notes=notes),
            "alert_delivery_firing_resolved": evidence_item(
                ["firing", "resolved", "receipt"], notes=notes),
            "member_acl_allow_deny": evidence_item(
                ["allow", "deny"], notes=notes),
            "soak_24h": evidence_item(["soak"], notes=notes),
            "soak_72h": evidence_item(["soak"], notes=notes),
        },
    }


def template_evidence():
    items = {}
    for item_id in cli.EVIDENCE_IDS:
        items[item_id] = {
            "status": "unknown",
            "level": "unknown",
            "evidence": [],
            "notes": "",
        }
    return {
        "schema": "ragkb-deployment-acceptance/v1",
        "environment": {"name": "", "version": "", "deployed_at": "",
                        "operator": ""},
        "items": items,
    }


# ---------------------------------------------------------------------------
# 测试基类
# ---------------------------------------------------------------------------
class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="t5-cli-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def write_text(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, argv, *, env=None):
        out = io.StringIO()
        err = io.StringIO()
        values = {ADMIN_ENV: "", METRICS_ENV: ""}
        if env:
            values.update(env)
        with patch.dict(os.environ, values, clear=False), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def run_json(self, argv, *, env=None):
        code, stdout, stderr = self.run_cli([*argv, "--json"], env=env)
        try:
            report = json.loads(stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"stdout is not JSON: {exc}; stderr={stderr!r}")
        return code, report, stdout, stderr

    def live_args(self, origin, *extra, confirm=None):
        return [
            "--base-url", origin,
            "--confirm-target", origin if confirm is None else confirm,
            "--live",
            "--timeout", "2",
            *extra,
        ]

    def check(self, report, check_id):
        return next(item for item in report["checks"] if item["id"] == check_id)

    def evidence(self, report, item_id):
        return next(item for item in report["evidence"]["items"]
                    if item["id"] == item_id)


# ---------------------------------------------------------------------------
# 1. dry-run
# ---------------------------------------------------------------------------
class DryRunTests(CliTestCase):
    def test_dry_run_makes_zero_network_requests_and_exits_zero(self):
        code, report, _stdout, _stderr = self.run_json(
            ["--base-url", "http://127.0.0.1:9", "--dry-run"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["schema"], "ragkb-deployment-verification/v1")
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["target"],
                         {"origin": "http://127.0.0.1:9", "loopback": True})
        self.assertEqual([c["id"] for c in report["checks"]],
                         list(cli.CHECK_IDS))
        for check in report["checks"]:
            self.assertEqual(check["status"], "unknown")
            self.assertEqual(check["reason"], "dry-run: not executed")
            self.assertEqual(check["details"], {})
        self.assertEqual([item["id"] for item in report["evidence"]["items"]],
                         list(cli.EVIDENCE_IDS))
        self.assertTrue(all(item["status"] == "unknown"
                            for item in report["evidence"]["items"]))
        self.assertFalse(report["evidence"]["real_server_evidence"])
        self.assertEqual(report["summary"],
                         {"pass": 0, "fail": 0, "unknown": 14,
                          "acceptance": False,
                          "human_signoff_required": True})

    def test_dry_run_without_base_url_is_valid_and_zero_network(self):
        code, report, _stdout, _stderr = self.run_json(["--dry-run"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["target"], {"origin": "", "loopback": False})
        self.assertEqual(report["network_requests"], 0)

    def test_dry_run_validates_optional_readiness_urls_without_network(self):
        code, report, _stdout, _stderr = self.run_json(
            ["--base-url", "http://127.0.0.1:9", "--dry-run",
             "--prometheus-url", "http://127.0.0.1:9",
             "--alertmanager-url", "https://monitor.example.test"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(self.check(report, "prometheus_readiness")["status"],
                         "unknown")

    def test_human_summary_lists_every_check_and_counts(self):
        code, stdout, _stderr = self.run_cli(
            ["--base-url", "http://127.0.0.1:9", "--dry-run"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("mode=dry-run", stdout)
        self.assertIn("network_requests=0", stdout)
        for check_id in cli.CHECK_IDS:
            self.assertIn(check_id, stdout)
        self.assertIn("SUMMARY pass=0 fail=0 unknown=14", stdout)
        self.assertNotIn('"schema"', stdout)

    def test_dry_run_with_evidence_validates_evidence_without_network(self):
        path = self.write_json("evidence.json", full_evidence())
        code, report, _stdout, _stderr = self.run_json(
            ["--base-url", "http://127.0.0.1:9", "--dry-run",
             "--evidence", str(path)])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["network_requests"], 0)
        self.assertTrue(all(c["status"] == "unknown" for c in report["checks"]))
        self.assertTrue(all(i["status"] == "pass"
                            for i in report["evidence"]["items"]))
        self.assertFalse(report["evidence"]["real_server_evidence"])

    def test_evidence_only_runs_zero_network_and_can_exit_zero(self):
        path = self.write_json("evidence.json", full_evidence())
        code, report, _stdout, _stderr = self.run_json(
            ["--evidence", str(path)])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["network_requests"], 0)
        self.assertTrue(all(c["status"] == "unknown" for c in report["checks"]))
        self.assertTrue(all(i["status"] == "pass"
                            for i in report["evidence"]["items"]))
        self.assertEqual(report["summary"]["pass"], 6)
        self.assertEqual(report["summary"]["unknown"], 8)
        self.assertFalse(report["evidence"]["real_server_evidence"])


# ---------------------------------------------------------------------------
# 2. live 全通过（回环 HTTPS）
# ---------------------------------------------------------------------------
class LiveSuccessTests(CliTestCase):
    def test_live_https_all_checks_and_production_evidence_exit_zero(self):
        with LoopbackServer(healthy_routes(), tls=True) as server:
            evidence_path = self.write_json("evidence.json", full_evidence())
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(
                    server.origin,
                    "--ca-file", str(server.cert_path),
                    "--prometheus-url", server.origin,
                    "--alertmanager-url", server.origin,
                    "--evidence", str(evidence_path),
                ),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_OK)
            self.assertEqual(report["mode"], "live")
            self.assertEqual(report["target"]["loopback"], True)
            self.assertEqual(report["network_requests"], 5)
            self.assertEqual(server.total, 5)
            self.assertEqual(server.count("/health"), 1)
            self.assertEqual(server.count("/api/v1/operations/metrics"), 1)
            self.assertEqual(server.count("/api/v1/operations/summary"), 1)
            self.assertEqual(server.count("/-/ready"), 2)
            for check in report["checks"]:
                self.assertEqual(check["status"], "pass",
                                 msg=f"{check['id']}: {check['reason']}")
            self.assertEqual(report["summary"],
                             {"pass": 14, "fail": 0, "unknown": 0,
                              "acceptance": False,
                              "human_signoff_required": True})
            self.assertTrue(all(i["status"] == "pass"
                                for i in report["evidence"]["items"]))
            # 回环目标永远不能作为真实服务器证据。
            self.assertFalse(report["evidence"]["real_server_evidence"])
            self.assertFalse(report["summary"]["acceptance"])
            self.assertTrue(report["summary"]["human_signoff_required"])
            self.assertEqual(server.authorizations("/api/v1/operations/metrics"),
                             [f"Bearer {METRICS_TOKEN}"])
            self.assertEqual(server.authorizations("/api/v1/operations/summary"),
                             [f"Bearer {ADMIN_TOKEN}"])
            self.assertEqual(server.authorizations("/health"), [None])
            self.assertEqual(set(server.state.commands), {"GET"})

    def test_live_without_evidence_is_unknown_not_success(self):
        with LoopbackServer(healthy_routes(), tls=True) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(
                    server.origin,
                    "--ca-file", str(server.cert_path),
                    "--prometheus-url", server.origin,
                    "--alertmanager-url", server.origin,
                ),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_UNKNOWN)
            self.assertTrue(all(c["status"] == "pass" for c in report["checks"]))
            self.assertTrue(all(i["status"] == "unknown"
                                for i in report["evidence"]["items"]))
            self.assertEqual(report["summary"]["unknown"], 6)
            self.assertFalse(report["evidence"]["real_server_evidence"])

    def test_https_without_ca_file_fails_certificate_validation(self):
        with LoopbackServer(healthy_routes(), tls=True) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_FAILURE)
            self.assertEqual(self.check(report, "health")["status"], "fail")
            self.assertEqual(self.check(report, "tls")["status"], "fail")
            # 三个已配置令牌的端点各尝试一次；TLS 失败不重试。
            self.assertEqual(report["network_requests"], 3)

    def test_real_server_evidence_requires_live_non_loopback_production(self):
        evidence = cli._evaluate_evidence(full_evidence(), None)
        checks = [
            {"id": check_id, "status": "pass", "reason": "ok", "details": {}}
            for check_id in cli.CHECK_IDS
        ]
        for live, origin, loopback, expected in (
            (True, "https://prod.example.test", False, True),
            (True, "https://127.0.0.1:8443", True, False),
            (False, "https://prod.example.test", False, False),
        ):
            with self.subTest(live=live, loopback=loopback):
                report = cli._build_report(
                    mode="live" if live else "dry-run",
                    origin=origin,
                    loopback=loopback,
                    network_requests=0,
                    checks=checks,
                    evidence=evidence,
                    live=live,
                )
                self.assertEqual(report["evidence"]["real_server_evidence"],
                                 expected)
                self.assertFalse(report["summary"]["acceptance"])


# ---------------------------------------------------------------------------
# 3. worker 状态
# ---------------------------------------------------------------------------
class WorkerTests(CliTestCase):
    def test_worker_idle_is_unknown_and_exits_three(self):
        with LoopbackServer(healthy_routes(worker="idle")) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_UNKNOWN)
            self.assertEqual(self.check(report, "health")["status"], "pass")
            worker = self.check(report, "worker")
            self.assertEqual(worker["status"], "unknown")
            self.assertIn("queue empty", worker["reason"])
            # HTTP 回环目标的 TLS 不适用，也是 unknown，而不是 pass。
            self.assertEqual(self.check(report, "tls")["status"], "unknown")
            self.assertEqual(self.check(report, "tls")["reason"],
                             "loopback HTTP: TLS not applicable")

    def test_worker_stale_fails_and_exits_one(self):
        with LoopbackServer(healthy_routes(worker="stale")) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_FAILURE)
            self.assertEqual(self.check(report, "health")["status"], "pass")
            self.assertEqual(self.check(report, "worker")["status"], "fail")

    def test_missing_worker_field_is_unknown(self):
        routes = healthy_routes()
        routes["/health"] = json_route({"status": "ok"})
        with LoopbackServer(routes) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_UNKNOWN)
            self.assertEqual(self.check(report, "health")["status"], "pass")
            self.assertEqual(self.check(report, "worker")["status"], "unknown")


# ---------------------------------------------------------------------------
# 4. metrics
# ---------------------------------------------------------------------------
class MetricsTests(CliTestCase):
    def test_missing_metrics_token_is_unknown_and_never_requests_metrics(self):
        with LoopbackServer(healthy_routes()) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: ""},
            )
            self.assertEqual(code, cli.EXIT_UNKNOWN)
            self.assertEqual(self.check(report, "metrics_auth")["status"],
                             "unknown")
            self.assertEqual(server.count("/api/v1/operations/metrics"), 0)
            self.assertEqual(self.check(report, "metrics_database")["status"],
                             "unknown")

    def test_metrics_503_disabled_and_401_invalid_fail_without_retry(self):
        routes = healthy_routes()
        routes["/api/v1/operations/metrics"] = text_route(
            "metrics collection is disabled", status=503)
        with LoopbackServer(routes) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_FAILURE)
            self.assertEqual(self.check(report, "metrics_auth")["status"],
                             "fail")
            self.assertEqual(self.check(report, "metrics_auth")["details"]
                             ["http_status"], 503)
            self.assertEqual(server.count("/api/v1/operations/metrics"), 1)

        routes = healthy_routes()
        routes["/api/v1/operations/metrics"] = text_route(
            "unauthorized", status=401)
        with LoopbackServer(routes) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_FAILURE)
            self.assertEqual(self.check(report, "metrics_auth")["status"],
                             "fail")
            self.assertEqual(server.count("/api/v1/operations/metrics"), 1)

    def test_correct_metrics_token_passes(self):
        with LoopbackServer(healthy_routes()) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(self.check(report, "metrics_auth")["status"],
                             "pass")
            self.assertEqual(self.check(report, "metrics_database")["status"],
                             "pass")
            self.assertEqual(code, cli.EXIT_UNKNOWN)

    def test_database_unavailable_metric_fails(self):
        with LoopbackServer(healthy_routes(db_available=0)) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_FAILURE)
            self.assertEqual(self.check(report, "metrics_auth")["status"],
                             "pass")
            database = self.check(report, "metrics_database")
            self.assertEqual(database["status"], "fail")
            self.assertEqual(database["details"]["db_available"], False)

    def test_missing_queue_metric_is_unknown(self):
        with LoopbackServer(healthy_routes(queue=None)) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_UNKNOWN)
            database = self.check(report, "metrics_database")
            self.assertEqual(database["status"], "unknown")
            self.assertEqual(database["details"]["queue_metric_present"],
                             False)


# ---------------------------------------------------------------------------
# 5. admin summary
# ---------------------------------------------------------------------------
class SummaryTests(CliTestCase):
    def test_missing_admin_token_is_unknown_and_never_requests_summary(self):
        with LoopbackServer(healthy_routes()) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: "", METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_UNKNOWN)
            self.assertEqual(self.check(report, "admin_summary")["status"],
                             "unknown")
            self.assertEqual(server.count("/api/v1/operations/summary"), 0)

    def test_summary_401_fails_without_retry(self):
        routes = healthy_routes()
        routes["/api/v1/operations/summary"] = json_route(
            {"detail": "unauthorized"}, status=401)
        with LoopbackServer(routes) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_FAILURE)
            self.assertEqual(self.check(report, "admin_summary")["status"],
                             "fail")
            self.assertEqual(server.count("/api/v1/operations/summary"), 1)

    def test_summary_degraded_or_queue_unavailable_fails(self):
        for kwargs in ({"degraded": True, "db_status": "unavailable",
                        "queue_present": False},
                       {"degraded": False, "db_status": "unavailable",
                        "queue_present": False},
                       {"degraded": False, "db_status": "ok",
                        "queue_present": False}):
            with self.subTest(kwargs=kwargs):
                with LoopbackServer(healthy_routes(summary=summary_data(**kwargs))) as server:
                    code, report, _stdout, _stderr = self.run_json(
                        self.live_args(server.origin),
                        env={ADMIN_ENV: ADMIN_TOKEN,
                             METRICS_ENV: METRICS_TOKEN},
                    )
                    self.assertEqual(code, cli.EXIT_FAILURE)
                    self.assertEqual(
                        self.check(report, "admin_summary")["status"], "fail")


# ---------------------------------------------------------------------------
# 6. 传输安全
# ---------------------------------------------------------------------------
class TransportSafetyTests(CliTestCase):
    def test_redirect_is_not_followed(self):
        with LoopbackServer({"/elsewhere": json_route({"status": "ok"})}) as target:
            with LoopbackServer({
                "/health": redirect_route(f"{target.origin}/elsewhere"),
                "/api/v1/operations/metrics": text_route(
                    metrics_text()),
                "/api/v1/operations/summary": json_route(summary_envelope()),
            }) as server:
                code, report, _stdout, _stderr = self.run_json(
                    self.live_args(server.origin),
                    env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
                )
                self.assertEqual(code, cli.EXIT_FAILURE)
                self.assertEqual(self.check(report, "health")["status"], "fail")
                self.assertEqual(server.count("/health"), 1)
                self.assertEqual(target.total, 0)

    def test_timeout_is_bounded_and_reported(self):
        routes = healthy_routes()
        routes["/health"] = slow_route(1.5)
        with LoopbackServer(routes) as server:
            started = time.monotonic()
            code, report, _stdout, _stderr = self.run_json(
                ["--base-url", server.origin,
                 "--confirm-target", server.origin,
                 "--live", "--timeout", "0.2"],
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.5)
            self.assertEqual(code, cli.EXIT_FAILURE)
            health = self.check(report, "health")
            self.assertEqual(health["status"], "fail")
            self.assertIn("timeout", health["reason"])
            self.assertEqual(server.count("/health"), 1)

    def test_oversized_response_fails_without_reading_unbounded(self):
        routes = healthy_routes()
        routes["/health"] = oversized_route(4096)
        with LoopbackServer(routes) as server:
            code, report, _stdout, _stderr = self.run_json(
                ["--base-url", server.origin,
                 "--confirm-target", server.origin,
                 "--live", "--timeout", "2",
                 "--max-response-bytes", "1024"],
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_FAILURE)
            health = self.check(report, "health")
            self.assertEqual(health["status"], "fail")
            self.assertIn("too large", health["reason"])
            self.assertEqual(server.count("/health"), 1)

    def test_non_json_health_fails(self):
        routes = healthy_routes()
        routes["/health"] = text_route("not json")
        with LoopbackServer(routes) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_FAILURE)
            self.assertEqual(self.check(report, "health")["status"], "fail")
            self.assertEqual(self.check(report, "worker")["status"], "unknown")

    def test_dripping_body_is_cut_off_by_the_total_request_timeout(self):
        """/health 每 0.03s 滴 1 字节：单次 IO 都不超时，但整请求必须按时中止。"""
        routes = healthy_routes()
        routes["/health"] = drip_route(0.03, 100)
        with LoopbackServer(routes) as server:
            started = time.monotonic()
            code, report, _stdout, _stderr = self.run_json(
                ["--base-url", server.origin,
                 "--confirm-target", server.origin,
                 "--live", "--timeout", "0.1"],
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            elapsed = time.monotonic() - started
        # 完整滴完需要约 3 秒；时限必须落在 0.1s 量级而不是被逐次 IO 续期。
        self.assertLess(elapsed, 1.5)
        self.assertEqual(code, cli.EXIT_FAILURE)
        health = self.check(report, "health")
        self.assertEqual(health["status"], "fail")
        self.assertIn("timeout", health["reason"])
        self.assertEqual(server.count("/health"), 1)

    def test_stalled_body_after_headers_is_cut_off(self):
        routes = healthy_routes()
        routes["/health"] = stalled_body_route(3.0)
        with LoopbackServer(routes) as server:
            started = time.monotonic()
            code, report, _stdout, _stderr = self.run_json(
                ["--base-url", server.origin,
                 "--confirm-target", server.origin,
                 "--live", "--timeout", "0.2"],
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertIn("timeout", self.check(report, "health")["reason"])
        self.assertEqual(server.count("/health"), 1)

    def test_bad_protocol_status_line_fails_checks_without_crashing(self):
        """opener.open 阶段的 BadStatusLine 只能让该项失败，不能掀掉整次运行。"""
        out_path = self.root / "protocol-report.json"
        with BadProtocolServer() as server:
            code, stdout, stderr = self.run_cli(
                self.live_args(server.origin, "--out", str(out_path), "--json"),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            commands = server.command_words()
            connections = server.connections
        self.assertEqual(code, cli.EXIT_FAILURE)
        written = out_path.read_text(encoding="utf-8")
        report = json.loads(written)
        self.assertEqual(report, json.loads(stdout))
        self.assertEqual(connections, 3)
        self.assertEqual(len(commands), 3)
        self.assertTrue(all(word == "GET" for word in commands), commands)
        for check_id in ("health", "metrics_auth", "admin_summary"):
            check = self.check(report, check_id)
            self.assertEqual(check["status"], "fail", msg=check_id)
            self.assertIn("protocol", check["reason"])
        self.assertEqual(report["network_requests"], 3)
        # 原始坏状态行既不能出现在 stdout，也不能出现在 stderr 或报告里。
        for stream in (stdout, stderr, written):
            self.assertNotIn("NOT-HTTP", stream)

    def test_remote_disconnect_during_open_is_a_check_failure(self):
        """连接被直接掐断（RemoteDisconnected）也必须走固定检查失败。"""
        routes = healthy_routes()

        def hang_up(handler):
            handler.close_connection = True
            handler.wfile.close()

        routes["/health"] = hang_up
        with LoopbackServer(routes) as server:
            code, report, _stdout, stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
        self.assertEqual(code, cli.EXIT_FAILURE)
        health = self.check(report, "health")
        self.assertEqual(health["status"], "fail")
        self.assertTrue(health["reason"].startswith("request failed: "),
                        health["reason"])
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(server.count("/health"), 1)


# ---------------------------------------------------------------------------
# 7. 参数/URL 门控
# ---------------------------------------------------------------------------
class InputGateTests(CliTestCase):
    def test_live_requires_matching_confirm_target_with_zero_requests(self):
        with LoopbackServer(healthy_routes()) as server:
            for argv in (
                ["--base-url", server.origin, "--live"],
                ["--base-url", server.origin, "--live",
                 "--confirm-target", "http://other.example.test"],
                ["--base-url", server.origin, "--live",
                 "--confirm-target", server.origin + "/"],
            ):
                with self.subTest(argv=argv):
                    code, _stdout, _stderr = self.run_cli(argv)
                    self.assertEqual(code, cli.EXIT_INPUT)
            self.assertEqual(server.total, 0)

    def test_live_requires_base_url(self):
        code, _stdout, _stderr = self.run_cli(
            ["--live", "--confirm-target", "https://example.test"])
        self.assertEqual(code, cli.EXIT_INPUT)

    def test_plain_http_non_loopback_is_rejected_before_network(self):
        for value in ("http://example.test", "http://10.0.0.1:8000"):
            with self.subTest(value=value):
                code, _stdout, _stderr = self.run_cli(
                    ["--base-url", value, "--confirm-target", value, "--live"])
                self.assertEqual(code, cli.EXIT_INPUT)

    def test_url_with_userinfo_query_fragment_or_path_is_rejected(self):
        with LoopbackServer(healthy_routes()) as server:
            values = [
                f"http://user:pass@127.0.0.1:{server.port}",
                f"http://127.0.0.1:{server.port}/?x=1",
                f"http://127.0.0.1:{server.port}/#frag",
                f"http://127.0.0.1:{server.port}/api",
                f"ftp://127.0.0.1:{server.port}",
            ]
            for value in values:
                with self.subTest(value=value):
                    code, _stdout, _stderr = self.run_cli(
                        ["--base-url", value, "--confirm-target", value,
                         "--live"])
                    self.assertEqual(code, cli.EXIT_INPUT)
            self.assertEqual(server.total, 0)

    def test_conflicting_modes_and_out_of_range_options_exit_two(self):
        cases = [
            ["--base-url", "http://127.0.0.1:9", "--live", "--dry-run",
             "--confirm-target", "http://127.0.0.1:9"],
            ["--dry-run", "--timeout", "0.05"],
            ["--dry-run", "--timeout", "31"],
            ["--dry-run", "--max-response-bytes", "1023"],
            ["--dry-run", "--max-response-bytes", "10485761"],
            ["--dry-run", "--admin-token-env", "bad name"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _stdout, _stderr = self.run_cli(argv)
                self.assertEqual(code, cli.EXIT_INPUT)

    def test_invalid_ca_file_is_input_error_before_network(self):
        bad_ca = self.write_text("bad-ca.pem", "not a certificate")
        with LoopbackServer(healthy_routes()) as server:
            code, stdout, stderr = self.run_cli(
                [*self.live_args(server.origin, "--ca-file", str(bad_ca))],
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_INPUT)
            self.assertEqual(server.total, 0)
            self.assertIn("ca_file", stderr)
            self.assertNotIn(ADMIN_TOKEN, stdout + stderr)
            self.assertNotIn(METRICS_TOKEN, stdout + stderr)


# ---------------------------------------------------------------------------
# 8. Prometheus / Alertmanager readiness
# ---------------------------------------------------------------------------
class ReadinessTests(CliTestCase):
    def _run(self, routes, *extra):
        with LoopbackServer(routes) as server:
            return self.run_json(
                self.live_args(server.origin, *extra),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )

    def test_not_configured_is_unknown(self):
        code, report, _stdout, _stderr = self._run(healthy_routes())
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        for check_id in ("prometheus_readiness", "alertmanager_readiness"):
            self.assertEqual(self.check(report, check_id)["status"], "unknown")
            self.assertEqual(self.check(report, check_id)["reason"],
                             "not configured")

    def test_ready_200_passes_and_500_fails(self):
        routes = healthy_routes()
        with LoopbackServer(routes) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(
                    server.origin,
                    "--prometheus-url", server.origin,
                    "--alertmanager-url", server.origin,
                ),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(self.check(report, "prometheus_readiness")
                             ["status"], "pass")
            self.assertEqual(self.check(report, "alertmanager_readiness")
                             ["status"], "pass")
            self.assertEqual(server.count("/-/ready"), 2)

        routes = healthy_routes()
        routes["/-/ready"] = text_route("not ready", status=500)
        with LoopbackServer(routes) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin,
                               "--prometheus-url", server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_FAILURE)
            readiness = self.check(report, "prometheus_readiness")
            self.assertEqual(readiness["status"], "fail")
            self.assertEqual(readiness["details"]["http_status"], 500)


# ---------------------------------------------------------------------------
# 9. 证据语义
# ---------------------------------------------------------------------------
class EvidenceTests(CliTestCase):
    def _evidence_report(self, payload, *extra):
        path = self.write_json("evidence.json", payload)
        return self.run_json(["--evidence", str(path), *extra])

    def test_template_all_unknown_exits_three(self):
        code, report, _stdout, _stderr = self._evidence_report(
            template_evidence())
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        self.assertEqual(report["network_requests"], 0)
        for item in report["evidence"]["items"]:
            self.assertEqual(item["status"], "unknown")
            self.assertEqual(item["level"], "unknown")

    def test_pass_with_local_synthetic_is_downgraded_to_unknown(self):
        payload = full_evidence()
        for item in payload["items"].values():
            item["level"] = "local-synthetic"
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        for item in report["evidence"]["items"]:
            self.assertEqual(item["status"], "unknown")
            self.assertEqual(item["level"], "local-synthetic")
            self.assertEqual(
                item["reason"],
                "synthetic/staging evidence cannot satisfy production acceptance")

    def test_staging_pass_is_downgraded_to_unknown(self):
        payload = full_evidence()
        payload["items"]["soak_24h"]["level"] = "staging"
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        self.assertEqual(self.evidence(report, "soak_24h")["status"],
                         "unknown")

    def test_production_pass_missing_required_kind_fails(self):
        payload = full_evidence()
        payload["items"]["backup_restore_rto_rpo"]["evidence"] = [
            evidence_element("restore_drill")]
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_FAILURE)
        item = self.evidence(report, "backup_restore_rto_rpo")
        self.assertEqual(item["status"], "fail")
        self.assertIn("checksum", item["reason"])

    def test_production_pass_with_empty_or_incomplete_evidence_fails(self):
        payload = full_evidence()
        payload["items"]["soak_72h"]["evidence"] = []
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(self.evidence(report, "soak_72h")["status"], "fail")

        payload = full_evidence()
        payload["items"]["member_acl_allow_deny"]["evidence"] = [
            evidence_element("allow"),
            evidence_element("deny", ref=""),
        ]
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(self.evidence(report, "member_acl_allow_deny")
                         ["status"], "fail")

        payload = full_evidence()
        payload["items"]["version_identity"]["evidence"] = [
            evidence_element("version", captured_at="")]
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(self.evidence(report, "version_identity")["status"],
                         "fail")

    def test_complete_production_evidence_only_exit_zero(self):
        code, report, _stdout, _stderr = self._evidence_report(full_evidence())
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(report["network_requests"], 0)
        self.assertTrue(all(i["status"] == "pass"
                            for i in report["evidence"]["items"]))
        self.assertFalse(report["evidence"]["real_server_evidence"])
        self.assertFalse(report["summary"]["acceptance"])

    def test_expected_version_must_match_exactly(self):
        code, report, _stdout, _stderr = self._evidence_report(
            full_evidence(version="1.2.3"), "--expected-version", "1.2.4")
        self.assertEqual(code, cli.EXIT_FAILURE)
        item = self.evidence(report, "version_identity")
        self.assertEqual(item["status"], "fail")
        self.assertIn("version mismatch", item["reason"])

        code, report, _stdout, _stderr = self._evidence_report(
            full_evidence(version="1.2.3"), "--expected-version", "1.2.3")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(self.evidence(report, "version_identity")["status"],
                         "pass")

    def test_version_identity_requires_environment_version(self):
        payload = full_evidence()
        payload["environment"]["version"] = ""
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(self.evidence(report, "version_identity")["status"],
                         "fail")

    def test_missing_item_is_unknown(self):
        payload = full_evidence()
        del payload["items"]["soak_72h"]
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        self.assertEqual(self.evidence(report, "soak_72h")["status"],
                         "unknown")
        self.assertEqual(self.evidence(report, "soak_72h")["reason"],
                         "item missing")

    def test_invalid_schema_status_level_and_kind_exit_two(self):
        cases = []
        cases.append({"schema": "wrong/v1"})
        cases.append([])
        payload = template_evidence()
        payload["items"]["soak_24h"]["status"] = "ok"
        cases.append(payload)
        payload = template_evidence()
        payload["items"]["soak_24h"]["level"] = "prod"
        cases.append(payload)
        payload = template_evidence()
        payload["items"]["soak_24h"]["evidence"] = [
            {"kind": "bogus", "ref": "x", "sha256": "", "captured_at": "t"}]
        cases.append(payload)
        payload = template_evidence()
        payload["items"]["unknown_item"] = {
            "status": "unknown", "level": "unknown", "evidence": [],
            "notes": ""}
        cases.append(payload)
        for payload in cases:
            with self.subTest(payload=str(payload)[:80]):
                path = self.write_json("invalid.json", payload)
                code, _stdout, _stderr = self.run_cli(
                    ["--evidence", str(path), "--json"])
                self.assertEqual(code, cli.EXIT_INPUT)

    def test_missing_or_non_json_evidence_exits_two(self):
        missing = self.root / "missing.json"
        code, _stdout, _stderr = self.run_cli(
            ["--evidence", str(missing), "--json"])
        self.assertEqual(code, cli.EXIT_INPUT)
        broken = self.write_text("broken.json", "{not json")
        code, _stdout, _stderr = self.run_cli(
            ["--evidence", str(broken), "--json"])
        self.assertEqual(code, cli.EXIT_INPUT)

    def test_declared_fail_is_failure(self):
        payload = full_evidence()
        payload["items"]["soak_24h"] = evidence_item(["soak"], status="fail")
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(self.evidence(report, "soak_24h")["status"], "fail")

    def test_non_iso_captured_at_invalidates_every_production_pass(self):
        """captured_at 必须是可解析的 ISO 日期/时间，不能只做非空判断。"""
        for captured_at in ("not-an-iso-date", "2026/01/01", "01-01-2026",
                            "2026-13-01", "2026-01-01 ", "   ", ""):
            with self.subTest(captured_at=captured_at):
                payload = full_evidence()
                for item in payload["items"].values():
                    for entry in item["evidence"]:
                        entry["captured_at"] = captured_at
                code, report, _stdout, _stderr = self._evidence_report(payload)
                self.assertEqual(code, cli.EXIT_FAILURE)
                for item in report["evidence"]["items"]:
                    self.assertEqual(item["status"], "fail", msg=item["id"])
                    self.assertEqual(
                        item["reason"],
                        "invalid production evidence declaration")

    def test_iso_date_and_offset_timestamps_are_accepted(self):
        for captured_at in ("2026-01-01", "2026-01-01T00:00:00",
                            "2026-01-01T00:00:00Z",
                            "2026-01-01T00:00:00+08:00",
                            "2026-01-01T00:00:00.123456+00:00"):
            with self.subTest(captured_at=captured_at):
                payload = full_evidence()
                for item in payload["items"].values():
                    for entry in item["evidence"]:
                        entry["captured_at"] = captured_at
                code, report, _stdout, _stderr = self._evidence_report(payload)
                self.assertEqual(code, cli.EXIT_OK)
                self.assertTrue(all(i["status"] == "pass"
                                    for i in report["evidence"]["items"]))

    def test_whitespace_only_ref_is_not_valid_production_identity(self):
        for ref in ("", "   ", "\t"):
            with self.subTest(ref=repr(ref)):
                payload = full_evidence()
                for item in payload["items"].values():
                    for entry in item["evidence"]:
                        entry["ref"] = ref
                code, report, _stdout, _stderr = self._evidence_report(payload)
                self.assertEqual(code, cli.EXIT_FAILURE)
                for item in report["evidence"]["items"]:
                    self.assertEqual(item["status"], "fail", msg=item["id"])

    def test_whitespace_only_environment_version_is_not_identity(self):
        for version in ("", "   ", "\t"):
            with self.subTest(version=repr(version)):
                payload = full_evidence(version=version)
                code, report, _stdout, _stderr = self._evidence_report(payload)
                self.assertEqual(code, cli.EXIT_FAILURE)
                item = self.evidence(report, "version_identity")
                self.assertEqual(item["status"], "fail")
                self.assertIn("environment.version is empty", item["reason"])

    def test_whitespace_padded_expected_version_still_must_match_exactly(self):
        code, report, _stdout, _stderr = self._evidence_report(
            full_evidence(version="1.2.3"), "--expected-version", " 1.2.3 ")
        self.assertEqual(code, cli.EXIT_FAILURE)
        item = self.evidence(report, "version_identity")
        self.assertEqual(item["status"], "fail")
        self.assertIn("version mismatch", item["reason"])

    def test_downgraded_pass_never_upgrades_from_invalid_declaration(self):
        """非 production 的非法 pass 仍是 unknown，不会因校验降级成 pass。"""
        payload = full_evidence()
        for item in payload["items"].values():
            item["level"] = "local-synthetic"
            for entry in item["evidence"]:
                entry["captured_at"] = "not-an-iso-date"
        code, report, _stdout, _stderr = self._evidence_report(payload)
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        for item in report["evidence"]["items"]:
            self.assertEqual(item["status"], "unknown")


# ---------------------------------------------------------------------------
# 10. 脱敏
# ---------------------------------------------------------------------------
class RedactionTests(CliTestCase):
    def test_tokens_evidence_refs_notes_and_response_bodies_never_leak(self):
        admin_token = "SENTINEL_ADMIN_TOKEN_7f3a"
        metrics_token = "SENTINEL_METRICS_TOKEN_9c1b"
        reference = "SENTINEL_EVIDENCE_REF_d42e"
        notes = "SENTINEL_EVIDENCE_NOTES_a81f"
        body_marker = "SENTINEL_RESPONSE_BODY_e55c"

        evidence = full_evidence(notes=notes)
        for item in evidence["items"].values():
            for entry in item["evidence"]:
                entry["ref"] = f"{reference}-{entry['kind']}"

        routes = healthy_routes(
            metrics_extra=f"# {body_marker}\n",
            health_extra={"internal": body_marker},
            summary_extra={"debug": body_marker},
        )
        out_path = self.root / "report.json"
        with LoopbackServer(routes, tls=True) as server:
            evidence_path = self.write_json("evidence.json", evidence)
            code, stdout, stderr = self.run_cli(
                self.live_args(
                    server.origin,
                    "--ca-file", str(server.cert_path),
                    "--prometheus-url", server.origin,
                    "--alertmanager-url", server.origin,
                    "--evidence", str(evidence_path),
                    "--out", str(out_path),
                    "--json",
                ),
                env={ADMIN_ENV: admin_token, METRICS_ENV: metrics_token},
            )
        self.assertEqual(code, cli.EXIT_OK)
        written = out_path.read_text(encoding="utf-8")
        for forbidden in (admin_token, metrics_token, reference, notes,
                          body_marker):
            self.assertNotIn(forbidden, stdout)
            self.assertNotIn(forbidden, stderr)
            self.assertNotIn(forbidden, written)
        report = json.loads(written)
        self.assertEqual(report, json.loads(stdout))
        for item in report["evidence"]["items"]:
            self.assertNotIn("ref", item)
            self.assertNotIn("notes", item)
        for check in report["checks"]:
            self.assertNotIn("body", check["details"])

    def test_response_derived_fields_only_emit_whitelisted_enums(self):
        """响应里的任意字符串绝不能被复制进报告 detail。"""
        routes = healthy_routes(
            worker=RESPONSE_SENTINEL,
            summary=summary_envelope(db_status=RESPONSE_SENTINEL),
        )
        out_path = self.root / "sentinel-report.json"
        with LoopbackServer(routes) as server:
            code, stdout, stderr = self.run_cli(
                self.live_args(server.origin, "--out", str(out_path), "--json"),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
        written = out_path.read_text(encoding="utf-8")
        for stream in (stdout, stderr, written):
            self.assertNotIn(RESPONSE_SENTINEL, stream)
        self.assertEqual(code, cli.EXIT_FAILURE)
        report = json.loads(stdout)
        health = self.check(report, "health")
        self.assertEqual(health["status"], "pass")
        self.assertIsNone(health["details"]["worker"])
        worker = self.check(report, "worker")
        self.assertEqual(worker["status"], "unknown")
        self.assertNotIn(RESPONSE_SENTINEL, worker["reason"])
        summary = self.check(report, "admin_summary")
        self.assertEqual(summary["status"], "fail")
        self.assertIsNone(summary["details"]["database_status"])

    def test_whitelisted_enum_values_are_still_reported(self):
        with LoopbackServer(healthy_routes(worker="stale")) as server:
            code, report, _stdout, _stderr = self.run_json(
                self.live_args(server.origin),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertEqual(self.check(report, "health")["details"]["worker"],
                         "stale")
        self.assertEqual(
            self.check(report, "admin_summary")["details"]["database_status"],
            "ok")


# ---------------------------------------------------------------------------
# 11. 模板
# ---------------------------------------------------------------------------
class TemplateTests(CliTestCase):
    def test_emit_template_is_all_unknown_and_matches_repo_template(self):
        path = self.root / "template.json"
        code, _stdout, _stderr = self.run_cli(
            ["--emit-template", str(path)])
        self.assertEqual(code, cli.EXIT_OK)
        emitted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(emitted, template_evidence())
        repo_template = (REPO_ROOT / "deploy" / "acceptance"
                         / "deployment-acceptance.template.json")
        self.assertTrue(repo_template.is_file())
        self.assertEqual(json.loads(repo_template.read_text(encoding="utf-8")),
                         emitted)
        for item in emitted["items"].values():
            self.assertEqual(item["status"], "unknown")
            self.assertEqual(item["level"], "unknown")
            self.assertEqual(item["evidence"], [])
            self.assertEqual(item["notes"], "")

    def test_emit_template_refuses_overwrite_without_flag(self):
        path = self.write_text("template.json", "ORIGINAL")
        code, _stdout, _stderr = self.run_cli(
            ["--emit-template", str(path)])
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertEqual(path.read_text(encoding="utf-8"), "ORIGINAL")

        code, _stdout, _stderr = self.run_cli(
            ["--emit-template", str(path), "--overwrite"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         template_evidence())

    def test_emit_template_rejects_symlink(self):
        victim = self.write_text("victim.txt", "VICTIM")
        link = self.root / "template-link.json"
        try:
            os.symlink(victim, link)
        except OSError as exc:
            self.skipTest(f"symlink unsupported: {exc}")
        code, _stdout, _stderr = self.run_cli(
            ["--emit-template", str(link), "--overwrite"])
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM")


# ---------------------------------------------------------------------------
# 12. --out
# ---------------------------------------------------------------------------
class OutputTests(CliTestCase):
    def test_out_existing_requires_overwrite_and_makes_zero_network(self):
        out_path = self.write_text("report.json", "ORIGINAL")
        with LoopbackServer(healthy_routes()) as server:
            code, _stdout, _stderr = self.run_cli(
                self.live_args(server.origin, "--out", str(out_path)),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(code, cli.EXIT_INPUT)
            self.assertEqual(server.total, 0)
        self.assertEqual(out_path.read_text(encoding="utf-8"), "ORIGINAL")

    def test_out_overwrite_and_atomic_write(self):
        out_path = self.write_text("report.json", "ORIGINAL")
        code, report, stdout, _stderr = self.run_json(
            ["--base-url", "http://127.0.0.1:9", "--dry-run",
             "--out", str(out_path), "--overwrite"])
        self.assertEqual(code, cli.EXIT_OK)
        written = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(written, report)
        self.assertEqual(written, json.loads(stdout))
        self.assertEqual(list(self.root.glob(".ragkb-verify-*.tmp")), [])

    def test_out_rejects_symlink(self):
        victim = self.write_text("victim.txt", "VICTIM")
        link = self.root / "report-link.json"
        try:
            os.symlink(victim, link)
        except OSError as exc:
            self.skipTest(f"symlink unsupported: {exc}")
        code, _stdout, _stderr = self.run_cli(
            ["--base-url", "http://127.0.0.1:9", "--dry-run",
             "--out", str(link), "--overwrite"])
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM")

    def test_out_missing_parent_is_input_error(self):
        missing = self.root / "no-such-dir" / "report.json"
        code, _stdout, _stderr = self.run_cli(
            ["--base-url", "http://127.0.0.1:9", "--dry-run",
             "--out", str(missing)])
        self.assertEqual(code, cli.EXIT_INPUT)

    def test_out_written_on_failure_report(self):
        routes = healthy_routes(worker="stale")
        out_path = self.root / "failure-report.json"
        with LoopbackServer(routes) as server:
            code, stdout, _stderr = self.run_cli(
                self.live_args(server.origin, "--out", str(out_path), "--json"),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
        self.assertEqual(code, cli.EXIT_FAILURE)
        written = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(written, json.loads(stdout))
        self.assertEqual(self.check(written, "worker")["status"], "fail")

    def test_out_created_during_network_keeps_executed_report(self):
        """报告路径在网络期间被第三方创建：不得回退成零网络的伪前置报告。"""
        out_path = self.root / "raced-report.json"
        original = "ORIGINAL_FROM_THIRD_PROCESS"

        def racing_health(handler):
            out_path.write_text(original, encoding="utf-8")
            json_route({"status": "ok", "worker": "alive"})(handler)

        routes = healthy_routes()
        routes["/health"] = racing_health
        with LoopbackServer(routes) as server:
            code, report, stdout, stderr = self.run_json(
                self.live_args(server.origin, "--out", str(out_path)),
                env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
            )
            self.assertEqual(server.total, 3)
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertIn("error: out: already_exists", stderr)
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["network_requests"], 3)
        self.assertEqual(report["summary"]["pass"], 5)
        self.assertEqual(self.check(report, "health")["status"], "pass")
        self.assertNotIn("error", report)
        self.assertEqual(out_path.read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.root.glob(".ragkb-verify-*.tmp")), [])

    def test_report_write_failure_still_prints_executed_report(self):
        out_path = self.root / "unwritable-report.json"
        with patch.object(cli, "_write_atomic",
                          side_effect=cli._InputError("out", "write_failed")):
            with LoopbackServer(healthy_routes()) as server:
                code, report, stdout, stderr = self.run_json(
                    self.live_args(server.origin, "--out", str(out_path)),
                    env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
                )
                human_code, human_stdout, human_stderr = self.run_cli(
                    self.live_args(server.origin, "--out", str(out_path)),
                    env={ADMIN_ENV: ADMIN_TOKEN, METRICS_ENV: METRICS_TOKEN},
                )
                self.assertEqual(server.total, 6)
        self.assertEqual(code, cli.EXIT_INPUT)
        self.assertEqual(human_code, cli.EXIT_INPUT)
        self.assertIn("error: out: write_failed", stderr)
        self.assertIn("error: out: write_failed", human_stderr)
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["network_requests"], 3)
        self.assertEqual(self.check(report, "health")["status"], "pass")
        self.assertEqual(report["summary"],
                         {"pass": 5, "fail": 0, "unknown": 9,
                          "acceptance": False,
                          "human_signoff_required": True})
        self.assertIn("ragkb-deployment-verification/v1", stdout)
        self.assertIn("ragkb-deployment-verification/v1", human_stdout)
        self.assertIn("SUMMARY pass=5 fail=0 unknown=9", human_stdout)
        self.assertFalse(out_path.exists())

    def test_pre_network_out_error_still_reports_zero_requests(self):
        out_path = self.write_text("taken-report.json", "ORIGINAL")
        bad = self.write_text("bad-evidence.json", "{not json")
        code, stdout, stderr = self.run_cli(
            ["--base-url", "http://127.0.0.1:9", "--evidence", str(bad),
             "--out", str(out_path), "--json"])
        self.assertEqual(code, cli.EXIT_INPUT)
        report = json.loads(stdout)
        self.assertEqual(report["network_requests"], 0)
        self.assertEqual(report["error"],
                         {"kind": "input", "field": "evidence",
                          "reason": "invalid_json"})
        self.assertIn("error: out: already_exists", stderr)
        self.assertEqual(out_path.read_text(encoding="utf-8"), "ORIGINAL")

    def test_out_written_on_input_error_with_zero_network(self):
        bad = self.write_text("bad-evidence.json", "{not json")
        out_path = self.root / "input-error-report.json"
        code, stdout, stderr = self.run_cli(
            ["--evidence", str(bad), "--out", str(out_path), "--json"])
        self.assertEqual(code, cli.EXIT_INPUT)
        written = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(written, json.loads(stdout))
        self.assertEqual(written["network_requests"], 0)
        self.assertEqual(written["error"],
                         {"kind": "input", "field": "evidence",
                          "reason": "invalid_json"})
        self.assertIn("error: evidence: invalid_json", stderr)


if __name__ == "__main__":
    unittest.main()
