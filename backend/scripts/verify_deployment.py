#!/usr/bin/env python
"""只读部署验收检查器（默认 dry-run，零网络）。

本工具只回答"这个已部署实例现在能被怎样证明"：默认不创建任何 HTTP 客户端，
只做参数/URL/证据文件校验；只有显式 ``--live`` 才对**单一确认过的目标**执行
只读 GET。它固定使用 GET、不跟随重定向、不自动重试、不读取环境代理、不读取
``.env``，也绝不放宽 TLS 证书校验。

证据文件区分 ``pass/fail/unknown`` 与 ``production/staging/local-synthetic``：
非 production 的通过声明一律降为 unknown，合成或回环目标永远不能变成
``real_server_evidence``；``acceptance`` 恒为 false，仍需人工签收。

退出码：0=dry-run 配置校验通过（不要求证据）或 live 全部通过；1=任一失败
（含证据声明非法）；2=参数/URL/文件/schema 输入错误（触网前，零网络）或报告
写盘失败（保留已执行报告与真实请求数）；3=无失败但存在未知项。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPORT_SCHEMA = "ragkb-deployment-verification/v1"
EVIDENCE_SCHEMA = "ragkb-deployment-acceptance/v1"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INPUT = 2
EXIT_UNKNOWN = 3

CHECK_IDS = (
    "health",
    "worker",
    "metrics_auth",
    "metrics_database",
    "admin_summary",
    "tls",
    "prometheus_readiness",
    "alertmanager_readiness",
)

EVIDENCE_IDS = (
    "version_identity",
    "backup_restore_rto_rpo",
    "alert_delivery_firing_resolved",
    "member_acl_allow_deny",
    "soak_24h",
    "soak_72h",
)

EVIDENCE_STATUSES = frozenset({"pass", "fail", "unknown"})
EVIDENCE_LEVELS = frozenset({"production", "staging", "local-synthetic", "unknown"})
EVIDENCE_KINDS = frozenset({
    "restore_drill", "checksum", "firing", "resolved", "receipt",
    "allow", "deny", "soak", "version", "file", "url", "log", "metric",
})

REQUIRED_KINDS = {
    "backup_restore_rto_rpo": ("restore_drill", "checksum"),
    "alert_delivery_firing_resolved": ("firing", "resolved", "receipt"),
    "member_acl_allow_deny": ("allow", "deny"),
    "soak_24h": ("soak",),
    "soak_72h": ("soak",),
    "version_identity": ("version",),
}

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DRY_RUN_REASON = "dry-run: not executed"
MAX_TOKEN_CHARS = 8192
_TOKEN_RE = re.compile(r"[\x21-\x7e]+")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 报告 detail 只允许固定枚举：响应里的任意字符串一律不外传（白名单外记 null）。
WORKER_STATES = frozenset({"alive", "stale", "idle"})
DATABASE_STATES = frozenset({"ok", "unavailable"})


class _InputError(Exception):
    """参数/URL/文件/schema 输入错误；调用方必须以 2 退出且零网络。"""

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}:{reason}")


class _RedirectDetected(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"redirect_{status}")


class _HttpStatusError(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"http_{status}")


class _ResponseTooLarge(Exception):
    pass


class _TlsVerificationError(Exception):
    pass


class _TransportError(Exception):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """任何 3xx 都直接报错：既不跟随，也不把 Location 当成功。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            fp.close()
        except Exception:  # noqa: BLE001 - 关闭失败不改变"不跟随"语义
            pass
        raise _RedirectDetected(code)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_blank(value: str) -> bool:
    """空串与纯空白都不构成可核对的身份。"""
    return not isinstance(value, str) or not value.strip()


def _is_iso_timestamp(value: str) -> bool:
    """captured_at 必须是可解析的 ISO 8601 日期或日期时间（含非法/空白/前后空格）。"""
    if not isinstance(value, str) or _is_blank(value):
        return False
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


class _HttpClient:
    """单个只读 GET 客户端：无代理、无重定向、无重试、证书校验不可关闭。"""

    def __init__(self, *, timeout: float, max_response_bytes: int,
                 ca_file: str | None):
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.requests = 0
        self.tls_completed_origins: set[str] = set()
        self.tls_failed_origins: set[str] = set()
        try:
            context = (
                ssl.create_default_context(cafile=ca_file)
                if ca_file else ssl.create_default_context()
            )
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise _InputError("ca_file" if ca_file else "tls", "invalid") from exc
        self._opener = urllib.request.build_opener(
            # 空的 ProxyHandler 覆盖默认实现，禁止从环境读取代理。
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirectHandler(),
        )

    def get(self, url: str, *, token: str | None = None,
            tls_origin: str | None = None) -> tuple[int, bytes]:
        """执行且仅执行一次 GET；返回 (状态码, 有界响应体)。

        ``timeout`` 约束的是**单次请求的总耗时**（连接、响应头与响应体读取），
        而不仅是单次 socket IO：慢滴流的响应体同样会在时限内被判定为超时。
        """
        self.requests += 1
        deadline = time.monotonic() + self.timeout
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "*/*")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        response = None
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except _RedirectDetected:
            if tls_origin is not None:
                self.tls_completed_origins.add(tls_origin)
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                exc.close()
            except Exception:  # noqa: BLE001 - 关闭失败不改变判定
                pass
            if tls_origin is not None:
                # 能收到 HTTP 状态就说明 TLS 握手与证书校验已经完成。
                self.tls_completed_origins.add(tls_origin)
            if 300 <= status < 400:
                raise _RedirectDetected(status) from exc
            raise _HttpStatusError(status) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
                if tls_origin is not None:
                    self.tls_failed_origins.add(tls_origin)
                raise _TlsVerificationError(type(reason).__name__) from exc
            if isinstance(reason, TimeoutError):
                raise _TransportError("timeout") from exc
            if isinstance(reason, http.client.HTTPException):
                # urllib 会把 BadStatusLine/RemoteDisconnected 包进 URLError。
                raise _TransportError("protocol") from exc
            raise _TransportError("connection") from exc
        except ssl.SSLError as exc:
            if tls_origin is not None:
                self.tls_failed_origins.add(tls_origin)
            raise _TlsVerificationError(type(exc).__name__) from exc
        except TimeoutError as exc:
            raise _TransportError("timeout") from exc
        except http.client.HTTPException as exc:
            # 坏协议响应（BadStatusLine 等）不是输入错误：记为该项失败并继续。
            raise _TransportError("protocol") from exc
        except OSError as exc:
            raise _TransportError("connection") from exc

        try:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if tls_origin is not None:
                self.tls_completed_origins.add(tls_origin)
            if time.monotonic() >= deadline:
                raise _TransportError("timeout")
            body = self._read_bounded(response, deadline)
            return int(status), body
        except _ResponseTooLarge:
            raise
        except TimeoutError as exc:
            raise _TransportError("timeout") from exc
        except OSError as exc:
            raise _TransportError("connection") from exc
        except http.client.HTTPException as exc:
            raise _TransportError("protocol") from exc
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _set_read_deadline(response, remaining: float) -> None:
        """把底层 socket 的读超时收紧到本次请求的剩余时限。"""
        socket_io = getattr(getattr(response, "fp", None), "raw", None)
        sock = getattr(socket_io, "_sock", None)
        if sock is None:
            return
        try:
            sock.settimeout(max(remaining, 0.001))
        except (OSError, ValueError):
            pass

    def _read_bounded(self, response, deadline: float) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                length = int(declared)
            except (TypeError, ValueError):
                length = None
            if length is not None and length > self.max_response_bytes:
                raise _ResponseTooLarge
        body = bytearray()
        read = getattr(response, "read1", None) or response.read
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TransportError("timeout")
            self._set_read_deadline(response, remaining)
            chunk = read(65536)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > self.max_response_bytes:
                raise _ResponseTooLarge
        return bytes(body)


def _normalize_origin(value: str, field: str) -> tuple[str, bool, str]:
    """校验并规范化 origin；返回 (origin, loopback, scheme)。"""
    if not isinstance(value, str) or not value:
        raise _InputError(field, "missing")
    if value != value.strip() or any(ch in value for ch in " \t\r\n"):
        raise _InputError(field, "whitespace")
    if "?" in value or "#" in value:
        raise _InputError(field, "query_or_fragment")
    try:
        split = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise _InputError(field, "invalid_url") from exc
    scheme = split.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _InputError(field, "scheme")
    if "@" in split.netloc or split.username is not None or split.password is not None:
        raise _InputError(field, "userinfo")
    if split.path not in {"", "/"}:
        raise _InputError(field, "path")
    host = split.hostname
    if not host:
        raise _InputError(field, "host")
    try:
        port = split.port
    except ValueError as exc:
        raise _InputError(field, "port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise _InputError(field, "port")
    host = host.lower()
    if ":" in host:
        if host.count(":") < 2 or not re.fullmatch(r"[0-9a-f:]+", host):
            raise _InputError(field, "host")
        display_host = f"[{host}]"
    else:
        if len(host) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
            host,
        ):
            raise _InputError(field, "host")
        display_host = host
    default_port = 80 if scheme == "http" else 443
    if port == default_port:
        port = None
    origin = f"{scheme}://{display_host}" + (f":{port}" if port is not None else "")
    loopback = host in LOOPBACK_HOSTS
    if scheme != "https" and not loopback:
        raise _InputError(field, "plain_http_must_be_loopback")
    return origin, loopback, scheme


def _read_token(env_name: str) -> tuple[str | None, str]:
    """只从进程环境读取 Bearer 令牌；缺失/不可用时不发请求。"""
    value = os.environ.get(env_name, "")
    if not value:
        return None, "missing"
    if len(value) > MAX_TOKEN_CHARS or not _TOKEN_RE.fullmatch(value):
        return None, "invalid"
    return value, ""


def _empty_environment() -> dict:
    return {"name": "", "version": "", "deployed_at": "", "operator": ""}


def _read_evidence(path_value: str) -> dict:
    path = Path(path_value)
    if not path.is_file():
        raise _InputError("evidence", "not_found")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _InputError("evidence", "unreadable") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _InputError("evidence", "invalid_json") from exc
    if not isinstance(data, dict):
        raise _InputError("evidence", "invalid_schema")
    if data.get("schema") != EVIDENCE_SCHEMA:
        raise _InputError("evidence", "invalid_schema")

    raw_environment = data.get("environment", {})
    if raw_environment is None:
        raw_environment = {}
    if not isinstance(raw_environment, dict):
        raise _InputError("evidence", "invalid_schema")
    environment = {}
    for key in ("name", "version", "deployed_at", "operator"):
        value = raw_environment.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise _InputError("evidence", "invalid_schema")
        environment[key] = value

    raw_items = data.get("items", {})
    if raw_items is None:
        raw_items = {}
    if not isinstance(raw_items, dict):
        raise _InputError("evidence", "invalid_schema")
    items = {}
    for item_id, item in raw_items.items():
        if item_id not in EVIDENCE_IDS:
            raise _InputError("evidence", "invalid_schema")
        if not isinstance(item, dict):
            raise _InputError("evidence", "invalid_schema")
        status = item.get("status", "unknown")
        if not isinstance(status, str) or status not in EVIDENCE_STATUSES:
            raise _InputError("evidence", "invalid_status")
        level = item.get("level", "unknown")
        if not isinstance(level, str) or level not in EVIDENCE_LEVELS:
            raise _InputError("evidence", "invalid_level")
        raw_entries = item.get("evidence", [])
        if raw_entries is None:
            raw_entries = []
        if not isinstance(raw_entries, list):
            raise _InputError("evidence", "invalid_schema")
        entries = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                raise _InputError("evidence", "invalid_schema")
            kind = entry.get("kind", "")
            if not isinstance(kind, str):
                raise _InputError("evidence", "invalid_schema")
            if kind and kind not in EVIDENCE_KINDS:
                raise _InputError("evidence", "invalid_kind")
            cleaned = {}
            for key in ("ref", "sha256", "captured_at"):
                value = entry.get(key, "")
                if value is None:
                    value = ""
                if not isinstance(value, str):
                    raise _InputError("evidence", "invalid_schema")
                cleaned[key] = value
            cleaned["kind"] = kind
            entries.append(cleaned)
        notes = item.get("notes", "")
        if notes is None:
            notes = ""
        if not isinstance(notes, str):
            raise _InputError("evidence", "invalid_schema")
        items[item_id] = {
            "status": status,
            "level": level,
            "evidence": entries,
            "notes": notes,
        }
    return {"environment": environment, "items": items}


def _evaluate_evidence(raw: dict, expected_version: str | None) -> dict:
    """把人工证据映射成固定 6 项；非 production 的 pass 绝不升级为通过。"""
    environment = raw["environment"]
    supplied = raw["items"]
    results = []
    for item_id in EVIDENCE_IDS:
        item = supplied.get(item_id)
        if item is None:
            results.append({
                "id": item_id,
                "status": "unknown",
                "level": "unknown",
                "reason": "item missing",
                "evidence_kinds": {},
            })
            continue
        status = item["status"]
        level = item["level"]
        kinds: dict[str, int] = {}
        for entry in item["evidence"]:
            kind = entry["kind"] or "unknown"
            kinds[kind] = kinds.get(kind, 0) + 1
        result = {
            "id": item_id,
            "status": status,
            "level": level,
            "reason": "",
            "evidence_kinds": dict(sorted(kinds.items())),
        }
        if status == "unknown":
            result["reason"] = "declared unknown"
        elif status == "fail":
            result["reason"] = "declared fail"
        elif level != "production":
            # 合成/staging 的通过声明不能当作生产验收通过，只能降级为未知。
            result["status"] = "unknown"
            result["reason"] = (
                "synthetic/staging evidence cannot satisfy production acceptance"
            )
        else:
            entries = item["evidence"]
            invalid = (
                not entries
                or any(
                    _is_blank(entry["ref"]) or not _is_iso_timestamp(entry["captured_at"])
                    for entry in entries
                )
            )
            if invalid:
                result["status"] = "fail"
                result["reason"] = "invalid production evidence declaration"
            else:
                present = {entry["kind"] for entry in entries}
                missing = [kind for kind in REQUIRED_KINDS.get(item_id, ())
                           if kind not in present]
                if missing:
                    result["status"] = "fail"
                    result["reason"] = (
                        "missing required evidence kinds: " + ", ".join(missing)
                    )
                elif item_id == "version_identity":
                    version = environment.get("version", "")
                    if _is_blank(version):
                        result["status"] = "fail"
                        result["reason"] = "environment.version is empty"
                    elif expected_version is not None and version != expected_version:
                        result["status"] = "fail"
                        result["reason"] = "version mismatch"
                    else:
                        result["status"] = "pass"
                        result["reason"] = "production evidence complete"
                else:
                    result["status"] = "pass"
                    result["reason"] = "production evidence complete"
        results.append(result)
    return {
        "environment": {
            "name": environment.get("name", ""),
            "version": environment.get("version", ""),
            "deployed_at": environment.get("deployed_at", ""),
        },
        "items": results,
    }


def _check(check_id: str, status: str, reason: str, **details) -> dict:
    return {"id": check_id, "status": status, "reason": reason, "details": details}


def _health_check(client: _HttpClient, origin: str) -> tuple[dict, str]:
    """返回 (health 检查, worker 白名单状态)；非白名单取值一律按未知处理。"""
    try:
        status, body = client.get(f"{origin}/health", tls_origin=origin)
    except _RedirectDetected as exc:
        return _check("health", "fail", "redirect not followed",
                      http_status=exc.status), ""
    except _HttpStatusError as exc:
        return _check("health", "fail", "unexpected HTTP status",
                      http_status=exc.status), ""
    except _TlsVerificationError:
        return _check("health", "fail",
                      "TLS handshake or certificate validation failed"), ""
    except _ResponseTooLarge:
        return _check("health", "fail", "response too large"), ""
    except _TransportError as exc:
        return _check("health", "fail", f"request failed: {exc.kind}"), ""

    if status != 200:
        return _check("health", "fail", "unexpected HTTP status",
                      http_status=status), ""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _check("health", "fail", "invalid JSON"), ""
    if not isinstance(payload, dict):
        return _check("health", "fail", "invalid response structure"), ""
    raw_worker = payload.get("worker")
    worker = raw_worker if isinstance(raw_worker, str) and raw_worker in WORKER_STATES else ""
    if payload.get("status") != "ok":
        return _check("health", "fail", "status not ok",
                      worker=worker or None), worker
    return _check("health", "pass", "health ok", worker=worker or None), worker


def _worker_check(worker: str) -> dict:
    if worker == "alive":
        return _check("worker", "pass", "worker alive")
    if worker == "stale":
        return _check("worker", "fail", "worker stale")
    if worker == "idle":
        return _check("worker", "unknown",
                      "worker idle: queue empty, liveness not proven")
    return _check("worker", "unknown", "worker state missing or unknown")


def _parse_metrics(body: bytes) -> tuple[bool | None, bool]:
    text = body.decode("utf-8", "replace")
    available: bool | None = None
    queue_present = False
    for line in text.splitlines():
        match = re.match(r"^ragkb_db_available\s+(\S+)\s*$", line)
        if match:
            token = match.group(1)
            if token == "1":
                available = True
            elif token == "0":
                available = False
            else:
                try:
                    number = float(token)
                except ValueError:
                    return None, queue_present
                if number == 1.0:
                    available = True
                elif number == 0.0:
                    available = False
                else:
                    return None, queue_present
        if re.match(r"^ragkb_ingest_queued_documents\s+\S+\s*$", line):
            queue_present = True
    return available, queue_present


def _metrics_checks(client: _HttpClient, origin: str, token_env: str) -> tuple[dict, dict]:
    token, token_error = _read_token(token_env)
    if token is None:
        reason = ("metrics token env not set" if token_error == "missing"
                  else "metrics token env unusable")
        return (
            _check("metrics_auth", "unknown", reason),
            _check("metrics_database", "unknown", "metrics response unavailable"),
        )
    try:
        status, body = client.get(
            f"{origin}/api/v1/operations/metrics", token=token, tls_origin=origin)
    except _RedirectDetected as exc:
        return (
            _check("metrics_auth", "fail", "redirect not followed",
                   http_status=exc.status),
            _check("metrics_database", "unknown", "metrics response unavailable"),
        )
    except _HttpStatusError as exc:
        if exc.status == 503:
            reason = "metrics collection disabled"
        elif exc.status in (401, 403):
            reason = "metrics token invalid"
        else:
            reason = "unexpected HTTP status"
        return (
            _check("metrics_auth", "fail", reason, http_status=exc.status),
            _check("metrics_database", "unknown", "metrics response unavailable"),
        )
    except _TlsVerificationError:
        return (
            _check("metrics_auth", "fail",
                   "TLS handshake or certificate validation failed"),
            _check("metrics_database", "unknown", "metrics response unavailable"),
        )
    except _ResponseTooLarge:
        return (
            _check("metrics_auth", "fail", "response too large"),
            _check("metrics_database", "unknown", "metrics response unavailable"),
        )
    except _TransportError as exc:
        return (
            _check("metrics_auth", "fail", f"request failed: {exc.kind}"),
            _check("metrics_database", "unknown", "metrics response unavailable"),
        )

    if status != 200:
        return (
            _check("metrics_auth", "fail", "unexpected HTTP status",
                   http_status=status),
            _check("metrics_database", "unknown", "metrics response unavailable"),
        )
    auth = _check("metrics_auth", "pass", "metrics authorized", http_status=status)
    available, queue_present = _parse_metrics(body)
    details = {"db_available": available, "queue_metric_present": queue_present}
    if available is False:
        database = _check("metrics_database", "fail",
                          "database unavailable per metrics", **details)
    elif available is True and queue_present:
        database = _check("metrics_database", "pass",
                          "database available and queue export present", **details)
    else:
        database = _check("metrics_database", "unknown",
                          "queue metric missing or unparseable", **details)
    return auth, database


def _summary_check(client: _HttpClient, origin: str, token_env: str) -> dict:
    token, token_error = _read_token(token_env)
    if token is None:
        reason = ("admin token env not set" if token_error == "missing"
                  else "admin token env unusable")
        return _check("admin_summary", "unknown", reason)
    try:
        status, body = client.get(
            f"{origin}/api/v1/operations/summary", token=token, tls_origin=origin)
    except _RedirectDetected as exc:
        return _check("admin_summary", "fail", "redirect not followed",
                      http_status=exc.status)
    except _HttpStatusError as exc:
        if exc.status in (401, 403):
            reason = "admin token invalid or not admin"
        else:
            reason = "unexpected HTTP status"
        return _check("admin_summary", "fail", reason, http_status=exc.status)
    except _TlsVerificationError:
        return _check("admin_summary", "fail",
                      "TLS handshake or certificate validation failed")
    except _ResponseTooLarge:
        return _check("admin_summary", "fail", "response too large")
    except _TransportError as exc:
        return _check("admin_summary", "fail", f"request failed: {exc.kind}")

    if status != 200:
        return _check("admin_summary", "fail", "unexpected HTTP status",
                      http_status=status)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _check("admin_summary", "fail", "invalid JSON",
                      http_status=status)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return _check("admin_summary", "fail", "invalid summary response",
                      http_status=status)
    data = payload["data"]
    database = data.get("database")
    raw_database_status = database.get("status") if isinstance(database, dict) else None
    database_status = (
        raw_database_status
        if isinstance(raw_database_status, str) and raw_database_status in DATABASE_STATES
        else None
    )
    queue_present = data.get("queue") is not None
    degraded = data.get("degraded")
    details = {
        "http_status": status,
        "degraded": degraded if isinstance(degraded, bool) else None,
        "database_status": database_status,
        "queue_present": queue_present,
    }
    if degraded is not False:
        return _check("admin_summary", "fail", "summary degraded", **details)
    if database_status != "ok":
        return _check("admin_summary", "fail", "database not ok", **details)
    if not queue_present:
        return _check("admin_summary", "fail", "queue unavailable", **details)
    return _check("admin_summary", "pass", "summary healthy", **details)


def _readiness_check(client: _HttpClient, check_id: str,
                     readiness_url: str | None) -> dict:
    if readiness_url is None:
        return _check(check_id, "unknown", "not configured")
    try:
        status, _body = client.get(readiness_url)
    except _RedirectDetected as exc:
        return _check(check_id, "fail", "redirect not followed",
                      http_status=exc.status)
    except _HttpStatusError as exc:
        return _check(check_id, "fail", f"not ready: HTTP {exc.status}",
                      http_status=exc.status)
    except _TlsVerificationError:
        return _check(check_id, "fail",
                      "TLS handshake or certificate validation failed")
    except _ResponseTooLarge:
        return _check(check_id, "fail", "response too large")
    except _TransportError as exc:
        return _check(check_id, "fail", f"readiness request failed: {exc.kind}")
    if status == 200:
        return _check(check_id, "pass", "ready", http_status=status)
    return _check(check_id, "fail", f"not ready: HTTP {status}",
                  http_status=status)


def _tls_check(client: _HttpClient, scheme: str, origin: str) -> dict:
    if scheme != "https":
        return _check("tls", "unknown", "loopback HTTP: TLS not applicable")
    if origin in client.tls_completed_origins:
        return _check("tls", "pass",
                      "TLS handshake and certificate validation completed")
    if origin in client.tls_failed_origins:
        return _check("tls", "fail",
                      "TLS handshake or certificate validation failed")
    return _check("tls", "unknown", "TLS completion not observed")


def _run_live_checks(*, origin: str, scheme: str, prometheus_url: str | None,
                     alertmanager_url: str | None, timeout: float,
                     max_response_bytes: int, ca_file: str | None,
                     admin_token_env: str, metrics_token_env: str
                     ) -> tuple[list[dict], int]:
    client = _HttpClient(timeout=timeout, max_response_bytes=max_response_bytes,
                         ca_file=ca_file)
    health, worker_raw = _health_check(client, origin)
    worker = _worker_check(worker_raw)
    metrics_auth, metrics_database = _metrics_checks(
        client, origin, metrics_token_env)
    summary = _summary_check(client, origin, admin_token_env)
    prometheus = _readiness_check(client, "prometheus_readiness", prometheus_url)
    alertmanager = _readiness_check(
        client, "alertmanager_readiness", alertmanager_url)
    tls = _tls_check(client, scheme, origin)
    checks = [health, worker, metrics_auth, metrics_database, summary, tls,
              prometheus, alertmanager]
    return checks, client.requests


def _build_report(*, mode: str, origin: str, loopback: bool, network_requests: int,
                  checks: list[dict], evidence: dict, live: bool) -> dict:
    items = evidence["items"]
    statuses = [item["status"] for item in checks] + [
        item["status"] for item in items]
    summary = {
        "pass": statuses.count("pass"),
        "fail": statuses.count("fail"),
        "unknown": statuses.count("unknown"),
        "acceptance": False,
        "human_signoff_required": True,
    }
    real_server_evidence = bool(
        live
        and origin
        and not loopback
        and items
        and all(item["status"] == "pass" and item["level"] == "production"
                for item in items)
    )
    return {
        "schema": REPORT_SCHEMA,
        "mode": mode,
        "generated_at": _iso_now(),
        "target": {"origin": origin, "loopback": loopback},
        "network_requests": network_requests,
        "checks": checks,
        "evidence": {
            "environment": evidence["environment"],
            "items": items,
            "real_server_evidence": real_server_evidence,
        },
        "summary": summary,
    }


def _exit_code(report: dict, *, live: bool, evidence_provided: bool) -> int:
    statuses = [check["status"] for check in report["checks"]] + [
        item["status"] for item in report["evidence"]["items"]]
    if "fail" in statuses:
        return EXIT_FAILURE
    if live:
        return EXIT_OK if all(status == "pass" for status in statuses) else EXIT_UNKNOWN
    if not evidence_provided:
        # dry-run 配置校验通过：检查项未执行是预期状态，不是无法验收。
        return EXIT_OK
    evidence_statuses = [item["status"] for item in report["evidence"]["items"]]
    return EXIT_OK if all(status == "pass" for status in evidence_statuses) else EXIT_UNKNOWN


def _print_human(report: dict) -> None:
    target = report["target"]["origin"] or "-"
    print(f"{report['schema']} mode={report['mode']} target={target} "
          f"network_requests={report['network_requests']}")
    for check in report["checks"]:
        print(f"{check['status']:<7} {check['id']}: {check['reason']}")
    for item in report["evidence"]["items"]:
        print(f"{item['status']:<7} evidence:{item['id']}: {item['reason']}")
    summary = report["summary"]
    print(f"SUMMARY pass={summary['pass']} fail={summary['fail']} "
          f"unknown={summary['unknown']} acceptance=false "
          f"human_signoff_required=true")


def _validate_output_path(value: str, field: str, *,
                          overwrite: bool = False) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_symlink():
        raise _InputError(field, "symlink")
    if path.exists() and path.is_dir():
        raise _InputError(field, "not_a_file")
    if path.exists() and not overwrite:
        raise _InputError(field, "already_exists")
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.is_dir():
        raise _InputError(field, "parent_missing")
    return path


def _write_atomic(path: Path, text: str, *, overwrite: bool, field: str) -> None:
    if path.is_symlink():
        raise _InputError(field, "symlink")
    if path.exists() and not overwrite:
        raise _InputError(field, "already_exists")
    parent = path.parent if str(path.parent) else Path(".")
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".ragkb-verify-", suffix=".tmp", dir=str(parent))
    except OSError as exc:
        raise _InputError(field, "write_failed") from exc
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise _InputError(field, "symlink")
        if overwrite:
            os.replace(temp_path, path)
        else:
            try:
                os.link(temp_path, path)
            except FileExistsError as exc:
                raise _InputError(field, "already_exists") from exc
            except OSError as exc:
                raise _InputError(field, "write_failed") from exc
    except _InputError:
        raise
    except OSError as exc:
        raise _InputError(field, "write_failed") from exc
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _template_payload() -> dict:
    items = {}
    for item_id in EVIDENCE_IDS:
        items[item_id] = {
            "status": "unknown",
            "level": "unknown",
            "evidence": [],
            "notes": "",
        }
    return {
        "schema": EVIDENCE_SCHEMA,
        "environment": _empty_environment(),
        "items": items,
    }


def _emit_template(path_value: str, *, overwrite: bool) -> int:
    path = _validate_output_path(path_value, "template", overwrite=overwrite)
    if path is None:
        raise _InputError("template", "missing")
    text = json.dumps(_template_payload(), ensure_ascii=False, indent=2) + "\n"
    _write_atomic(path, text, overwrite=overwrite, field="template")
    return EXIT_OK


def _write_report(report: dict, path: Path | None, *, overwrite: bool,
                  json_output: bool) -> str | None:
    """stdout 先落检查报告，再尝试原子写盘。

    写盘阶段失败**不**是输入错误：已执行的检查与真实 ``network_requests`` 必须
    保留。这里返回固定错误标签交给调用方，绝不回退成"零网络"的前置报告。
    """
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if json_output:
        print(text)
    else:
        _print_human(report)
    if path is None:
        return None
    try:
        _write_atomic(path, text, overwrite=overwrite, field="out")
    except _InputError as exc:
        return f"{exc.field}: {exc.reason}"
    return None


def _input_error_report(args, exc: _InputError) -> dict:
    reason = f"input error: {exc.field}: {exc.reason}"
    origin = ""
    loopback = False
    try:
        if args.base_url:
            origin, loopback, _scheme = _normalize_origin(args.base_url, "base_url")
    except _InputError:
        origin, loopback = "", False
    checks = [
        {"id": check_id, "status": "unknown", "reason": reason, "details": {}}
        for check_id in CHECK_IDS
    ]
    items = [
        {
            "id": item_id,
            "status": "unknown",
            "level": "unknown",
            "reason": reason,
            "evidence_kinds": {},
        }
        for item_id in EVIDENCE_IDS
    ]
    statuses = ["unknown"] * (len(checks) + len(items))
    report = {
        "schema": REPORT_SCHEMA,
        "mode": "live" if getattr(args, "live", False) else "dry-run",
        "generated_at": _iso_now(),
        "target": {"origin": origin, "loopback": loopback},
        "network_requests": 0,
        "checks": checks,
        "evidence": {
            "environment": {"name": "", "version": "", "deployed_at": ""},
            "items": items,
            "real_server_evidence": False,
        },
        "summary": {
            "pass": statuses.count("pass"),
            "fail": statuses.count("fail"),
            "unknown": statuses.count("unknown"),
            "acceptance": False,
            "human_signoff_required": True,
        },
        "error": {
            "kind": "input",
            "field": exc.field,
            "reason": exc.reason,
        },
    }
    return report


def _handle_input_error(args, exc: _InputError) -> int:
    print(f"error: {exc.field}: {exc.reason}", file=sys.stderr)
    report = None
    if not getattr(args, "emit_template", ""):
        report = _input_error_report(args, exc)
        if getattr(args, "json", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
    # 参数解析失败无法安全定位输出；其它输入错误尽量留下可核对报告。
    if report is not None and getattr(args, "out", ""):
        if exc.field != "out":
            try:
                path = _validate_output_path(
                    args.out, "out", overwrite=getattr(args, "overwrite", False))
                if path is not None:
                    _write_atomic(
                        path,
                        json.dumps(report, ensure_ascii=False, indent=2),
                        overwrite=getattr(args, "overwrite", False),
                        field="out",
                    )
            except _InputError as write_exc:
                print(f"error: {write_exc.field}: {write_exc.reason}", file=sys.stderr)
    return EXIT_INPUT


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # 不把 argparse 的原始消息（可能含用户输入）写进输出。
        raise _InputError("arguments", "invalid")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = _ArgumentParser(
        description="只读部署验收检查器（默认 dry-run，零网络）")
    parser.add_argument("--base-url", default="",
                        help="验收目标 origin（live 必需）")
    parser.add_argument("--confirm-target", default="",
                        help="必须与规范化后的 base-url origin 完全一致（live 必需）")
    parser.add_argument("--live", action="store_true",
                        help="显式执行只读 GET")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅做本地校验（默认）")
    parser.add_argument("--admin-token-env", default="RAG_DEPLOY_ADMIN_TOKEN",
                        help="管理员 JWT 所在环境变量名")
    parser.add_argument("--metrics-token-env", default="RAG_DEPLOY_METRICS_TOKEN",
                        help="指标采集令牌所在环境变量名")
    parser.add_argument("--prometheus-url", default=None,
                        help="可选；只检查 /-/ready")
    parser.add_argument("--alertmanager-url", default=None,
                        help="可选；只检查 /-/ready")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="单次请求超时秒数（0.1-30）")
    parser.add_argument("--max-response-bytes", type=int, default=1048576,
                        help="响应体上限（1024-10485760）")
    parser.add_argument("--ca-file", default=None,
                        help="可选；HTTPS 校验用 CA 文件（禁止关闭证书校验）")
    parser.add_argument("--evidence", default=None,
                        help="人工验收证据 JSON")
    parser.add_argument("--expected-version", default=None,
                        help="可选；与证据 environment.version 完全比对")
    parser.add_argument("--emit-template", default="",
                        help="生成待填写的证据模板并退出")
    parser.add_argument("--out", default="",
                        help="JSON 报告输出路径（原子写）")
    parser.add_argument("--overwrite", action="store_true",
                        help="允许原子替换已存在的模板/报告")
    parser.add_argument("--json", action="store_true",
                        help="stdout 输出完整 JSON，而不是人类摘要")
    return parser.parse_args(argv)


def _validate_args(args) -> dict:
    if args.live and args.dry_run:
        raise _InputError("mode", "conflicting_modes")
    if not 0.1 <= args.timeout <= 30:
        raise _InputError("timeout", "out_of_range")
    if not 1024 <= args.max_response_bytes <= 10485760:
        raise _InputError("max_response_bytes", "out_of_range")
    for field, value in (("admin_token_env", args.admin_token_env),
                         ("metrics_token_env", args.metrics_token_env)):
        if not isinstance(value, str) or not _ENV_NAME_RE.fullmatch(value):
            raise _InputError(field, "invalid_name")

    ca_file = args.ca_file or None
    if ca_file is not None:
        if not Path(ca_file).is_file():
            raise _InputError("ca_file", "not_found")

    origin = ""
    loopback = False
    scheme = ""
    if args.base_url:
        origin, loopback, scheme = _normalize_origin(args.base_url, "base_url")
    elif args.live:
        raise _InputError("base_url", "missing")

    if args.live:
        if not args.confirm_target:
            raise _InputError("confirm_target", "missing")
        if args.confirm_target != origin:
            raise _InputError("confirm_target", "mismatch")

    prometheus_url = None
    if args.prometheus_url:
        prometheus_origin, _loopback, _scheme = _normalize_origin(
            args.prometheus_url, "prometheus_url")
        prometheus_url = prometheus_origin + "/-/ready"
    alertmanager_url = None
    if args.alertmanager_url:
        alertmanager_origin, _loopback, _scheme = _normalize_origin(
            args.alertmanager_url, "alertmanager_url")
        alertmanager_url = alertmanager_origin + "/-/ready"

    evidence_raw = None
    if args.evidence:
        evidence_raw = _read_evidence(args.evidence)

    # 输出路径只在触网之前做前置校验：此后任何失败都属于"写盘阶段"，
    # 必须保留已执行的报告，而不是回退成零网络的前置报告。
    output_path = _validate_output_path(args.out, "out", overwrite=args.overwrite)

    return {
        "origin": origin,
        "loopback": loopback,
        "scheme": scheme,
        "prometheus_url": prometheus_url,
        "alertmanager_url": alertmanager_url,
        "evidence_raw": evidence_raw,
        "ca_file": ca_file,
        "output_path": output_path,
    }


def _run(args) -> int:
    # 模板生成是独立动作：不触网，也不要求 live/base-url 参数。
    if args.emit_template:
        return _emit_template(args.emit_template, overwrite=args.overwrite)

    config = _validate_args(args)

    if config["evidence_raw"] is None:
        evidence = _evaluate_evidence(
            {"environment": _empty_environment(), "items": {}}, None)
    else:
        evidence = _evaluate_evidence(
            config["evidence_raw"], args.expected_version)

    if not args.live:
        checks = [
            {"id": check_id, "status": "unknown",
             "reason": DRY_RUN_REASON, "details": {}}
            for check_id in CHECK_IDS
        ]
        report = _build_report(
            mode="dry-run",
            origin=config["origin"],
            loopback=config["loopback"],
            network_requests=0,
            checks=checks,
            evidence=evidence,
            live=False,
        )
    else:
        checks, network_requests = _run_live_checks(
            origin=config["origin"],
            scheme=config["scheme"],
            prometheus_url=config["prometheus_url"],
            alertmanager_url=config["alertmanager_url"],
            timeout=args.timeout,
            max_response_bytes=args.max_response_bytes,
            ca_file=config["ca_file"],
            admin_token_env=args.admin_token_env,
            metrics_token_env=args.metrics_token_env,
        )
        report = _build_report(
            mode="live",
            origin=config["origin"],
            loopback=config["loopback"],
            network_requests=network_requests,
            checks=checks,
            evidence=evidence,
            live=True,
        )

    failure = _write_report(report, config["output_path"],
                            overwrite=args.overwrite, json_output=args.json)
    if failure is not None:
        print(f"error: {failure}", file=sys.stderr)
        return EXIT_INPUT

    return _exit_code(report, live=args.live,
                      evidence_provided=bool(args.evidence))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except _InputError as exc:
        print(f"error: {exc.field}: {exc.reason}", file=sys.stderr)
        return EXIT_INPUT
    try:
        return _run(args)
    except _InputError as exc:
        return _handle_input_error(args, exc)
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - 不回显异常正文/路径/响应内容
        print(f"error: unexpected_failure: {type(exc).__name__}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
