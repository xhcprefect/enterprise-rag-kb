"""配置与 SSE 的有界解析；本模块不读取应用配置、不读取 .env、不发网络请求。

CLI（``scripts/benchmark_rag.py``）默认 dry-run 只用到这里的纯本地校验；
只有显式 ``--live`` 才会创建 HTTP 客户端。这样即使生产环境的 ``.env``
存在，dry-run 也不会被导入的应用设置影响。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# 硬界：无论配置写什么都不会越过。工具只做小样本测量，不做压测。
# ---------------------------------------------------------------------------
HARD_MAX_CONCURRENCY = 8
HARD_MAX_REQUESTS_TOTAL = 40
HARD_MAX_STAGE_REQUESTS = 20
HARD_MAX_STAGES = 10
HARD_MAX_REQUEST_TIMEOUT = 30.0
HARD_MAX_RUN_TIMEOUT = 120.0
HARD_MAX_READY_TIMEOUT = 60.0
HARD_MAX_HTTP_REQUESTS = 300
HARD_MAX_UPLOAD_FILES = 5
HARD_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
HARD_MAX_STREAM_BYTES = 4 * 1024 * 1024
HARD_MAX_SSE_EVENTS = 5000
HARD_MAX_SSE_LINE_BYTES = 1024 * 1024
HARD_MAX_CONFIG_BYTES = 256 * 1024
HARD_MAX_QUESTION_CHARS = 4000
HARD_MAX_MONEY_CNY = Decimal("1000000")

_ALLOWED_TOP = {
    "version", "api_base_url", "mode", "kb_id", "auth", "stages", "total_requests",
    "request_timeout_seconds", "run_timeout_seconds", "ready_timeout_seconds",
    "planned_scale", "question", "cost", "documents", "limits",
}
_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MONEY = re.compile(r"^(0|[1-9][0-9]{0,6})(\.[0-9]{1,6})?$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# 可执行、脚本与安装包后缀一律拒绝，避免把工具变成投放通道；其余受限后缀放行。
_BLOCKED_SUFFIXES = {
    ".exe", ".dll", ".sys", ".com", ".scr", ".msi", ".bat", ".cmd", ".ps1",
    ".vbs", ".vbe", ".js", ".jse", ".jar", ".sh", ".py", ".pyc", ".php",
    ".app", ".dmg", ".pkg", ".deb", ".rpm", ".so", ".dylib",
}


class BenchmarkInputError(Exception):
    """本地配置/文件/输出路径错误；消息只含字段名与稳定原因，不含原始值。"""

    def __init__(self, field: str, reason: str):
        self.field, self.reason = field, reason
        super().__init__(f"{field}:{reason}")


@dataclass(frozen=True)
class StageSpec:
    concurrency: int
    requests: int


@dataclass(frozen=True)
class DocumentSpec:
    index: int
    code: str
    suffix: str
    size: int
    sha256: str
    source_path: str = field(default="", repr=False)
    payload: bytes = field(default=b"", repr=False, compare=False)


@dataclass(frozen=True)
class LimitsSpec:
    max_http_requests: int = HARD_MAX_HTTP_REQUESTS
    max_upload_files: int = HARD_MAX_UPLOAD_FILES
    max_upload_bytes: int = HARD_MAX_UPLOAD_BYTES
    max_stream_bytes: int = HARD_MAX_STREAM_BYTES
    max_sse_events: int = HARD_MAX_SSE_EVENTS


@dataclass(frozen=True)
class BenchmarkConfig:
    version: int
    api_base_url: str
    target_fingerprint: str
    mode: str
    kb_id: str | None
    user_token_env: str
    admin_token_env: str
    stages: tuple[StageSpec, ...]
    total_requests: int
    request_timeout_seconds: float
    run_timeout_seconds: float
    ready_timeout_seconds: float
    planned_documents: int
    planned_users: int
    question_code: str
    question_text: str
    cost_currency: str
    max_cost_cny: Decimal
    documents: tuple[DocumentSpec, ...]
    upload_total_bytes: int
    limits: LimitsSpec
    config_sha256: str
    config_bytes: int


# ---------------------------------------------------------------------------
# 严格 JSON
# ---------------------------------------------------------------------------

def _reject_constant(value: str):
    raise BenchmarkInputError("config", "non_finite_number")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BenchmarkInputError("config", "duplicate_key")
        result[key] = value
    return result


def _check_json(value: Any, *, depth: int = 0, state: list[int] | None = None) -> None:
    """限制深度/节点数/字符串长度，拒绝孤立代理项与控制字符。"""
    if state is None:
        state = [0]
    state[0] += 1
    if state[0] > 4096:
        raise BenchmarkInputError("config", "too_many_nodes")
    if depth > 16:
        raise BenchmarkInputError("config", "too_deep")
    if isinstance(value, dict):
        if len(value) > 128:
            raise BenchmarkInputError("config", "too_many_keys")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 64:
                raise BenchmarkInputError("config", "invalid_key")
            _check_string(key, field="config", max_len=64)
            _check_json(item, depth=depth + 1, state=state)
    elif isinstance(value, list):
        if len(value) > 256:
            raise BenchmarkInputError("config", "too_many_items")
        for item in value:
            _check_json(item, depth=depth + 1, state=state)
    elif isinstance(value, str):
        _check_string(value, field="config", max_len=HARD_MAX_QUESTION_CHARS)
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, Decimal)):
        return
    else:
        raise BenchmarkInputError("config", "invalid_json_type")


def _check_string(value: str, *, field: str, max_len: int) -> None:
    if len(value) > max_len:
        raise BenchmarkInputError(field, "too_long")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BenchmarkInputError(field, "invalid_unicode") from exc
    for char in value:
        code = ord(char)
        if code == 0 or (code < 32 and char not in "\t\n\r"):
            raise BenchmarkInputError(field, "control_character")


def load_config(path: str | Path) -> BenchmarkConfig:
    """读取受限的版本化配置；文件不存在/过大/JSON 非法都只报稳定原因。"""
    config_path = Path(path)
    try:
        if not config_path.is_file():
            raise BenchmarkInputError("config_path", "not_a_file")
        size = config_path.stat().st_size
    except OSError as exc:
        raise BenchmarkInputError("config_path", "unreadable") from exc
    if size <= 0:
        raise BenchmarkInputError("config_path", "empty")
    if size > HARD_MAX_CONFIG_BYTES:
        raise BenchmarkInputError("config_path", "too_large")
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise BenchmarkInputError("config_path", "unreadable") from exc
    if len(raw) != size:
        raise BenchmarkInputError("config_path", "changed_during_read")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BenchmarkInputError("config_path", "invalid_utf8") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
            parse_float=Decimal,
            parse_int=int,
        )
    except BenchmarkInputError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise BenchmarkInputError("config_path", "invalid_json") from exc
    _check_json(document)
    config = _parse_config(document)
    digest = hashlib.sha256(raw).hexdigest()
    return BenchmarkConfig(
        **{**config, "config_sha256": digest, "config_bytes": len(raw)}
    )


# ---------------------------------------------------------------------------
# 字段解析（unknown key 一律拒绝）
# ---------------------------------------------------------------------------

def _object(value: Any, field: str, allowed: set[str]) -> dict:
    if type(value) is not dict:
        raise BenchmarkInputError(field, "must_be_object")
    if set(value) - allowed:
        raise BenchmarkInputError(field, "unknown_key")
    return value


def _require(obj: dict, field: str, key: str) -> Any:
    if key not in obj:
        raise BenchmarkInputError(field, f"missing_{key}")
    return obj[key]


def _string(value: Any, field: str, *, min_len: int = 1, max_len: int = 256) -> str:
    if type(value) is not str:
        raise BenchmarkInputError(field, "must_be_string")
    _check_string(value, field=field, max_len=max_len)
    if len(value) < min_len:
        raise BenchmarkInputError(field, "too_short")
    return value


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int:  # bool 不是 int；JSON 1.0 会变成 Decimal，也被拒绝
        raise BenchmarkInputError(field, "must_be_integer")
    if value < minimum or value > maximum:
        raise BenchmarkInputError(field, "out_of_range")
    return value


def _number(value: Any, field: str, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise BenchmarkInputError(field, "must_be_number")
    number = Decimal(value) if not isinstance(value, Decimal) else value
    if not number.is_finite() or number <= 0:
        raise BenchmarkInputError(field, "out_of_range")
    result = float(number)
    if not math.isfinite(result) or result > maximum:
        raise BenchmarkInputError(field, "out_of_range")
    return result


def _code(value: Any, field: str) -> str:
    text = _string(value, field, min_len=1, max_len=64)
    if not _CODE.fullmatch(text):
        raise BenchmarkInputError(field, "invalid_code")
    return text


def _uuid_string(value: Any, field: str) -> str:
    text = _string(value, field, min_len=36, max_len=36)
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError) as exc:
        raise BenchmarkInputError(field, "invalid_uuid") from exc


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_api_base_url(raw: Any) -> tuple[str, str]:
    """只接受单一、已归一化的 API 根地址；返回 (归一化地址, 指纹)。"""
    text = _string(raw, "api_base_url", min_len=8, max_len=512)
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise BenchmarkInputError("api_base_url", "invalid_url") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise BenchmarkInputError("api_base_url", "unsupported_scheme")
    if parsed.username is not None or parsed.password is not None:
        raise BenchmarkInputError("api_base_url", "userinfo_forbidden")
    if parsed.query or parsed.fragment:
        raise BenchmarkInputError("api_base_url", "query_or_fragment_forbidden")
    if parsed.path not in {"", "/"}:
        raise BenchmarkInputError("api_base_url", "path_forbidden")
    host = (parsed.hostname or "").lower()
    if not host:
        raise BenchmarkInputError("api_base_url", "missing_host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BenchmarkInputError("api_base_url", "invalid_port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise BenchmarkInputError("api_base_url", "invalid_port")
    if scheme == "http" and not _is_loopback(host):
        raise BenchmarkInputError("api_base_url", "plain_http_must_be_loopback")
    if ":" in host:
        # IPv6：urlsplit 已去掉方括号，重新加回。
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise BenchmarkInputError("api_base_url", "invalid_host") from exc
        netloc = f"[{host}]"
    else:
        clean_host = host.rstrip(".")
        labels = clean_host.split(".")
        if not labels or any(not _HOST_LABEL.fullmatch(label) for label in labels):
            raise BenchmarkInputError("api_base_url", "invalid_host")
        netloc = clean_host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    normalized = f"{scheme}://{netloc}"
    digest = "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return normalized, digest


def _parse_limits(value: Any, documents_count: int, documents_bytes: int) -> LimitsSpec:
    allowed = {"max_http_requests", "max_upload_files", "max_upload_bytes",
               "max_stream_bytes", "max_sse_events"}
    obj = _object(value, "limits", allowed)
    defaults = {
        "max_http_requests": HARD_MAX_HTTP_REQUESTS,
        "max_upload_files": HARD_MAX_UPLOAD_FILES,
        "max_upload_bytes": HARD_MAX_UPLOAD_BYTES,
        "max_stream_bytes": HARD_MAX_STREAM_BYTES,
        "max_sse_events": HARD_MAX_SSE_EVENTS,
    }
    parsed = {}
    for key, hard in defaults.items():
        raw = obj.get(key, hard)
        parsed[key] = _integer(raw, f"limits.{key}", minimum=1, maximum=hard)
    if documents_count > parsed["max_upload_files"]:
        raise BenchmarkInputError("documents", "exceeds_max_files")
    if documents_bytes > parsed["max_upload_bytes"]:
        raise BenchmarkInputError("documents", "exceeds_max_bytes")
    return LimitsSpec(**parsed)


def _parse_documents(value: Any, max_files: int = HARD_MAX_UPLOAD_FILES,
                     max_bytes: int = HARD_MAX_UPLOAD_BYTES) -> tuple[tuple[DocumentSpec, ...], int]:
    if value is None:
        return (), 0
    if type(value) is not list:
        raise BenchmarkInputError("documents", "must_be_array")
    if len(value) > max_files:
        raise BenchmarkInputError("documents", "too_many_files")
    specs: list[DocumentSpec] = []
    codes: set[str] = set()
    paths: set[str] = set()
    total = 0
    for index, item in enumerate(value, start=1):
        obj = _object(item, "documents", {"path", "code"})
        path_text = _string(_require(obj, "documents", "path"), "documents.path",
                            min_len=1, max_len=4096)
        code = _code(_require(obj, "documents", "code"), "documents.code")
        if code in codes:
            raise BenchmarkInputError("documents.code", "duplicate")
        codes.add(code)
        path = Path(path_text)
        try:
            if path.is_symlink():
                raise BenchmarkInputError("documents.path", "symlink_forbidden")
            if not path.is_file():
                raise BenchmarkInputError("documents.path", "not_a_file")
            resolved = str(path.resolve(strict=True))
            size = path.stat().st_size
        except BenchmarkInputError:
            raise
        except OSError as exc:
            raise BenchmarkInputError("documents.path", "unreadable") from exc
        if resolved in paths:
            raise BenchmarkInputError("documents.path", "duplicate")
        paths.add(resolved)
        if size <= 0:
            raise BenchmarkInputError("documents.path", "empty")
        total += size
        if total > max_bytes:
            raise BenchmarkInputError("documents", "exceeds_max_bytes")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise BenchmarkInputError("documents.path", "unreadable") from exc
        if len(payload) != size:
            raise BenchmarkInputError("documents.path", "changed_during_read")
        suffix = path.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,15}", suffix or ""):
            raise BenchmarkInputError("documents.path", "unsupported_extension")
        if suffix in _BLOCKED_SUFFIXES:
            raise BenchmarkInputError("documents.path", "unsupported_extension")
        specs.append(DocumentSpec(
            index=index,
            code=code,
            suffix=suffix,
            size=size,
            sha256=hashlib.sha256(payload).hexdigest(),
            source_path=resolved,
            payload=payload,
        ))
    return tuple(specs), total


def _parse_config(document: Any) -> dict[str, Any]:
    obj = _object(document, "config", _ALLOWED_TOP)
    version = _integer(_require(obj, "config", "version"), "version", minimum=1, maximum=1)
    api_base_url, fingerprint = normalize_api_base_url(_require(obj, "config", "api_base_url"))

    mode = _string(_require(obj, "config", "mode"), "mode", max_len=8)
    if mode not in {"chat", "rag"}:
        raise BenchmarkInputError("mode", "must_be_chat_or_rag")

    kb_id = None
    if mode == "rag":
        kb_id = _uuid_string(_require(obj, "config", "kb_id"), "kb_id")
    elif obj.get("kb_id") is not None:
        raise BenchmarkInputError("kb_id", "forbidden_in_chat_mode")

    auth = _object(_require(obj, "config", "auth"), "auth", {"user_token_env", "admin_token_env"})
    user_token_env = _string(_require(auth, "auth", "user_token_env"), "auth.user_token_env",
                             min_len=1, max_len=128)
    admin_token_env = _string(_require(auth, "auth", "admin_token_env"), "auth.admin_token_env",
                              min_len=1, max_len=128)
    for field, name in (("auth.user_token_env", user_token_env),
                        ("auth.admin_token_env", admin_token_env)):
        if not _ENV_NAME.fullmatch(name):
            raise BenchmarkInputError(field, "invalid_env_name")

    raw_stages = _require(obj, "config", "stages")
    if type(raw_stages) is not list or not 1 <= len(raw_stages) <= HARD_MAX_STAGES:
        raise BenchmarkInputError("stages", "invalid_count")
    stages: list[StageSpec] = []
    previous_concurrency = 0
    for item in raw_stages:
        stage = _object(item, "stages", {"concurrency", "requests"})
        concurrency = _integer(_require(stage, "stages", "concurrency"), "stages.concurrency",
                               minimum=1, maximum=HARD_MAX_CONCURRENCY)
        requests = _integer(_require(stage, "stages", "requests"), "stages.requests",
                            minimum=1, maximum=HARD_MAX_STAGE_REQUESTS)
        if concurrency < previous_concurrency:
            raise BenchmarkInputError("stages.concurrency", "must_be_non_decreasing")
        previous_concurrency = concurrency
        stages.append(StageSpec(concurrency=concurrency, requests=requests))
    total_requests = _integer(_require(obj, "config", "total_requests"), "total_requests",
                              minimum=1, maximum=HARD_MAX_REQUESTS_TOTAL)
    if sum(stage.requests for stage in stages) != total_requests:
        raise BenchmarkInputError("total_requests", "must_equal_stage_sum")

    request_timeout = _number(_require(obj, "config", "request_timeout_seconds"),
                              "request_timeout_seconds", maximum=HARD_MAX_REQUEST_TIMEOUT)
    run_timeout = _number(_require(obj, "config", "run_timeout_seconds"),
                          "run_timeout_seconds", maximum=HARD_MAX_RUN_TIMEOUT)
    ready_timeout = _number(obj.get("ready_timeout_seconds", 30.0),
                            "ready_timeout_seconds", maximum=HARD_MAX_READY_TIMEOUT)

    scale = _object(_require(obj, "config", "planned_scale"), "planned_scale",
                    {"documents", "users"})
    planned_documents = _integer(_require(scale, "planned_scale", "documents"),
                                 "planned_scale.documents", minimum=0, maximum=1_000_000)
    planned_users = _integer(_require(scale, "planned_scale", "users"),
                             "planned_scale.users", minimum=0, maximum=1_000_000)

    question = _object(_require(obj, "config", "question"), "question", {"code", "text"})
    question_code = _code(_require(question, "question", "code"), "question.code")
    question_text = _string(_require(question, "question", "text"), "question.text",
                            min_len=1, max_len=HARD_MAX_QUESTION_CHARS)
    if not question_text.strip():
        raise BenchmarkInputError("question.text", "blank")
    if "\x00" in question_text:
        raise BenchmarkInputError("question.text", "control_character")

    cost = _object(_require(obj, "config", "cost"), "cost", {"currency", "max_cny"})
    currency = _string(_require(cost, "cost", "currency"), "cost.currency", max_len=3)
    if currency != "CNY":
        raise BenchmarkInputError("cost.currency", "must_be_cny")
    money_text = _string(_require(cost, "cost", "max_cny"), "cost.max_cny", max_len=16)
    if not _MONEY.fullmatch(money_text):
        raise BenchmarkInputError("cost.max_cny", "invalid_decimal")
    try:
        max_cost = Decimal(money_text)
    except InvalidOperation as exc:
        raise BenchmarkInputError("cost.max_cny", "invalid_decimal") from exc
    if not max_cost.is_finite() or max_cost < 0 or max_cost > HARD_MAX_MONEY_CNY:
        raise BenchmarkInputError("cost.max_cny", "out_of_range")

    raw_limits = obj.get("limits")
    if raw_limits is not None:
        _object(raw_limits, "limits", {"max_http_requests", "max_upload_files",
                                       "max_upload_bytes", "max_stream_bytes",
                                       "max_sse_events"})
    upload_max_files = HARD_MAX_UPLOAD_FILES
    upload_max_bytes = HARD_MAX_UPLOAD_BYTES
    if raw_limits is not None:
        upload_max_files = _integer(raw_limits.get("max_upload_files", HARD_MAX_UPLOAD_FILES),
                                    "limits.max_upload_files", minimum=1, maximum=HARD_MAX_UPLOAD_FILES)
        upload_max_bytes = _integer(raw_limits.get("max_upload_bytes", HARD_MAX_UPLOAD_BYTES),
                                    "limits.max_upload_bytes", minimum=1, maximum=HARD_MAX_UPLOAD_BYTES)
    documents, total_bytes = _parse_documents(obj.get("documents"), upload_max_files, upload_max_bytes)
    if documents and (mode != "rag" or kb_id is None):
        raise BenchmarkInputError("documents", "requires_rag_kb")
    limits = _parse_limits(raw_limits if raw_limits is not None else {}, len(documents), total_bytes)

    return {
        "version": version,
        "api_base_url": api_base_url,
        "target_fingerprint": fingerprint,
        "mode": mode,
        "kb_id": kb_id,
        "user_token_env": user_token_env,
        "admin_token_env": admin_token_env,
        "stages": tuple(stages),
        "total_requests": total_requests,
        "request_timeout_seconds": request_timeout,
        "run_timeout_seconds": run_timeout,
        "ready_timeout_seconds": ready_timeout,
        "planned_documents": planned_documents,
        "planned_users": planned_users,
        "question_code": question_code,
        "question_text": question_text,
        "cost_currency": currency,
        "max_cost_cny": max_cost,
        "documents": documents,
        "upload_total_bytes": total_bytes,
        "limits": limits,
    }


# ---------------------------------------------------------------------------
# 输出路径安全
# ---------------------------------------------------------------------------

def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and a.samefile(b)
    except OSError:
        return False


_WINDOWS_INVALID_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
# 与 Python 3.13 ntpath._reserved_names 一致：COM/LPT 含上标 ¹²³（U+00B9/U+00B2/U+00B3）。
_SUP = "\xb9\xb2\xb3"
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{index}" for index in "123456789" + _SUP}
    | {f"LPT{index}" for index in "123456789" + _SUP}
)


def _windows_output_component_invalid(component: str) -> bool:
    """Windows 目标名规则：非法字符、尾部点/空格、保留设备名（含扩展名）。"""
    if not component:
        return True
    if _WINDOWS_INVALID_NAME_CHARS.search(component):
        return True
    if component.endswith((" ", ".")):
        return True
    # ntpath 规则：第一个点之前、去掉尾部空格后大写比较。
    stem = component.partition(".")[0].rstrip(" ").upper()
    return stem in _WINDOWS_RESERVED_NAMES


def _probe_output_parent(parent: Path, *, overwrite: bool) -> None:
    """用本次自建临时文件验证父目录可写，并按实际发布方式探测；绝不触碰既有输出。

    默认独占创建使用 ``os.link``（目标必须不存在），覆盖模式使用 ``os.replace``；
    若底层文件系统不支持对应发布原语，必须在 HTTP 之前以输入错误（exit 2）暴露，
    而不是等到运行结束才发现。探测与清理只涉及本次自建的临时路径。
    """
    first: Path | None = None
    second: Path | None = None
    link: Path | None = None
    try:
        fd, first_name = tempfile.mkstemp(prefix=".ragbench-probe-", suffix=".tmp",
                                          dir=parent)
        first = Path(first_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"probe")
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            fd, second_name = tempfile.mkstemp(prefix=".ragbench-probe-", suffix=".tmp",
                                               dir=parent)
            second = Path(second_name)
            os.close(fd)
            os.replace(first, second)
        else:
            # os.link 要求目标不存在：链接到本次自建的、尚不存在的路径。
            link = first.with_name(first.name + ".link")
            os.link(first, link)
    except OSError as exc:
        raise BenchmarkInputError("output", "not_writable") from exc
    finally:
        for path in (first, second, link):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def validate_output_path(output: str | Path | None, config_path: str | Path,
                         config: BenchmarkConfig, *, overwrite: bool) -> Path | None:
    """拒绝把输入配置/文档作为输出目标；默认独占创建，覆盖时也只原子替换输出。"""
    if output in (None, ""):
        return None
    out = Path(output)
    inputs = [Path(config_path)] + [Path(doc_path) for doc_path in _document_paths(config)]
    out_resolved = out.resolve(strict=False)
    for item in inputs:
        try:
            item_resolved = item.resolve(strict=False)
        except OSError as exc:
            raise BenchmarkInputError("output", "input_unresolvable") from exc
        if out_resolved == item_resolved or _same_file(out, item):
            raise BenchmarkInputError("output", "aliases_input")
    if os.name == "nt":
        anchor = out.anchor
        for component in out.parts:
            if component in {anchor, "\\", "/"} or (
                    len(component) == 2 and component.endswith(":")):
                continue
            if _windows_output_component_invalid(component):
                raise BenchmarkInputError("output", "illegal_name")
    if out.exists():
        if not out.is_file():
            raise BenchmarkInputError("output", "not_a_regular_file")
        if not overwrite:
            raise BenchmarkInputError("output", "already_exists")
        try:
            mode = out.stat().st_mode
        except OSError as exc:
            raise BenchmarkInputError("output", "not_a_regular_file") from exc
        if not (mode & stat.S_IWRITE) or not os.access(out, os.W_OK):
            raise BenchmarkInputError("output", "read_only")
    parent = out_resolved.parent
    if not parent.is_dir():
        raise BenchmarkInputError("output", "parent_missing")
    _probe_output_parent(parent, overwrite=overwrite)
    return out


def _document_paths(config: BenchmarkConfig) -> tuple[str, ...]:
    return tuple(doc.source_path for doc in config.documents if doc.source_path)


# ---------------------------------------------------------------------------
# SSE 解析（纯函数式、按字节有界）
# ---------------------------------------------------------------------------

class SseProtocolError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SseEvent:
    name: str
    data: str


class SseParser:
    """增量解析 SSE；字节数/行长度/事件数超过硬界立即失败。"""

    def __init__(self, *, max_bytes: int = HARD_MAX_STREAM_BYTES,
                 max_line_bytes: int = HARD_MAX_SSE_LINE_BYTES,
                 max_events: int = HARD_MAX_SSE_EVENTS):
        self.max_bytes = max_bytes
        self.max_line_bytes = max_line_bytes
        self.max_events = max_events
        self._buffer = b""
        self._event_name = ""
        self._data_lines: list[str] = []
        self._total_bytes = 0
        self._event_count = 0

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def event_count(self) -> int:
        return self._event_count

    def feed(self, chunk: bytes) -> list[SseEvent]:
        if not isinstance(chunk, (bytes, bytearray)):
            raise SseProtocolError("invalid_chunk")
        self._total_bytes += len(chunk)
        if self._total_bytes > self.max_bytes:
            raise SseProtocolError("stream_too_large")
        self._buffer += bytes(chunk)
        events: list[SseEvent] = []
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            event = self._consume_line(line)
            if event is not None:
                events.append(event)
        if len(self._buffer) > self.max_line_bytes:
            raise SseProtocolError("line_too_long")
        return events

    def finish(self) -> list[SseEvent]:
        events: list[SseEvent] = []
        if self._buffer:
            event = self._consume_line(self._buffer)
            self._buffer = b""
            if event is not None:
                events.append(event)
        if self._data_lines or self._event_name:
            event = self._dispatch()
            if event is not None:
                events.append(event)
        return events

    def _consume_line(self, raw: bytes) -> SseEvent | None:
        if len(raw) > self.max_line_bytes:
            raise SseProtocolError("line_too_long")
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if not raw:
            return self._dispatch()
        if raw.startswith(b":"):
            return None
        name, sep, value = raw.partition(b":")
        if not sep:
            value = b""
        if value.startswith(b" "):
            value = value[1:]
        try:
            field = name.decode("utf-8")
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SseProtocolError("invalid_utf8") from exc
        if field == "event":
            if len(text) > 64:
                raise SseProtocolError("event_name_too_long")
            self._event_name = text
        elif field == "data":
            if len(self._data_lines) >= 1000:
                raise SseProtocolError("too_many_data_lines")
            self._data_lines.append(text)
        # id/retry/未知字段按规范忽略。
        return None

    def _dispatch(self) -> SseEvent | None:
        if not self._data_lines and not self._event_name:
            return None
        self._event_count += 1
        if self._event_count > self.max_events:
            raise SseProtocolError("too_many_events")
        event = SseEvent(self._event_name or "message", "\n".join(self._data_lines))
        self._event_name = ""
        self._data_lines = []
        return event


def percentile(values: list[float], ratio: float) -> float | None:
    """最近秩百分位（第 ceil(p*n) 个，1 基）；不插值，不制造未观测到的延迟。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(ratio * len(ordered))))
    return round(ordered[rank - 1], 1)
