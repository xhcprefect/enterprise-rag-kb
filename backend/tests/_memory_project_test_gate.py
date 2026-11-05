"""memory/project 专项统一数据库安全测试门（只读共享 helper + 显式准备 CLI）。

被所有需要真实 PostgreSQL 的集成用例共用，把“该 skip 还是该硬 fail”判定收敛到一处：

  - TEST_DATABASE_URL 与 owner（TEST_DATABASE_OWNER，兼容旧 TEST_EXPECTED_OWNER）
    都缺 -> 显式 SkipTest（不碰任何库）；
  - 任何部分配置（只给一个）、owner 标签不合规、URL 不可解析、非 loopback host、
    库名非 ``_test``、或实际 ``app.db.session.engine`` 的 driver/host/port/库名/用户/
    凭据/query 与声明不符 -> 连接前硬 fail（ConfigError，RuntimeError 子类），且绝不当
    skip 掩盖；
  - 只有以上都通过，才连接实际 engine 读取 ``pg_database`` 的 comment，必须与 owner
    逐字节精确匹配才放行 init/fixture/write。

owner 标签格式（通用、可被 CI 显式生成，不绑定某台机器的容器）：

  ``<prefix><token>``，prefix ∈ {``owned-test:``, ``disposable-test:``,
  ``ci-disposable:``}，token 为 8~128 个 ``[A-Za-z0-9._-]`` 且以字母数字开头。
  例如 CI 用 ``ci-disposable:<run_id>-<run_attempt>``；本地一次性库用
  ``owned-test:<随机 hex>``。不带前缀的标签一律拒绝——避免业务库被随手标成测试库。

显式准备（CI / 新建一次性库后执行一次，输出不含凭据）：

  TEST_DATABASE_URL=... TEST_DATABASE_OWNER=... \\
      python -m tests._memory_project_test_gate --prepare-owner

  该命令只在已通过 loopback / ``_test`` / owner 格式 / 无路由 query 的库上，
  把库 comment 设为 owner；已等于 owner 时幂等 no-op；若库已有**其它**非空 comment
  则拒绝覆盖并以非零退出（fail closed）。

安全原则（本文件自身即验收）：
  - 本模块 **不在导入期连接任何库**，也不在导入期抛配置异常（配置坏只会在具体用例
    执行时以 ConfigError 出现，保持 unittest discover 可发现性）；
  - ``classify`` / ``raise_bad_config`` / ``compare_engine`` 全部为纯计算，绝不触发
    ``engine.connect()``；
  - 只有 ``read_owner_comment`` / ``verify`` / ``prepare_owner`` 在配置已核合法后才连接；
  - 用 SQLAlchemy ``URL`` 对象逐字段比较（不是先转 str 再比），错误消息对密码一律
    脱敏为 ``<redacted>``，绝不打印凭据；
  - 绝不硬写 URL/端口/owner/凭据；owner/URL 全部来自启动环境。

调用方运行环境约束：
  RAG_DISABLE_DOTENV=1 LOG_STDOUT_ONLY=true  （另须 unset 真实 TEST_DATABASE_*）。
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from typing import Any, Callable, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

# 启动环境（模块被导入时捕获一次；调用方必须在导入 app.* 之前设置好）。
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
TEST_DATABASE_OWNER = os.environ.get("TEST_DATABASE_OWNER", "").strip()
TEST_EXPECTED_OWNER = os.environ.get("TEST_EXPECTED_OWNER", "").strip()
TARGET_OWNER = TEST_DATABASE_OWNER or TEST_EXPECTED_OWNER

# 允许的 loopback host（大小写已归一化比较）。
_LOOPBACK_HOSTS = {"127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1", "0:0:0:0:0:0:0:1"}

_PASSWORD_REDACTED = "<redacted>"

# owner 标签：必须显式声明“这是一次性测试库”，且不带空格/控制字符。
OWNER_PREFIXES = ("owned-test:", "disposable-test:", "ci-disposable:")
_OWNER_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")


class ConfigError(RuntimeError):
    """坏配置的硬失败。``reason`` 是不含任何凭据的稳定标签，供测试断言匹配。"""

    def __init__(self, reason: str, *detail: str):
        message = "; ".join(str(d) for d in (reason,) + detail if d)
        super().__init__(message)
        self.reason = reason


# ---- 纯配置分类（不连接）----


def owner_format_reason(owner: str) -> Optional[str]:
    """owner 标签不合规时返回稳定原因标签，合规返回 None（纯计算）。"""
    value = (owner or "").strip()
    if not value:
        return "missing_owner"
    prefix = next((p for p in OWNER_PREFIXES if value.startswith(p)), None)
    if prefix is None:
        return "owner_prefix"
    if not _OWNER_TOKEN_RE.match(value[len(prefix):]):
        return "owner_token"
    return None


def _norm_host(url: URL) -> str:
    return (url.host or "").lower()


def _safe_parse(raw: str) -> URL:
    """解析声明 URL，坏 URL 抛 ConfigError。密码不会出现在异常里。"""
    try:
        return make_url(raw)
    except Exception as exc:  # noqa: BLE001 - 统一映射为配置错误
        # 用 from None：原始异常消息可能回显整条 URL（含密码/query 值），
        # 若 raise from exc，其消息会进入格式化 traceback 造成凭据泄露。
        raise ConfigError("TEST_DATABASE_URL 解析失败", f"{exc.__class__.__name__}") from None


def classify(raw_url: str, owner: str) -> str:
    """返回配置状态标签（不连接）。用于定位/断言。"""
    url = (raw_url or "").strip()
    owner = (owner or "").strip()
    if not url and not owner:
        return "missing_both"
    if not owner:
        return "url_only"  # URL 已给、缺 owner（部分配置）
    if not url:
        return "owner_only"  # owner 已给、缺 URL（部分配置）
    owner_reason = owner_format_reason(owner)
    if owner_reason is not None:
        return "bad_owner"
    try:
        parsed = _safe_parse(url)
    except ConfigError:
        return "malformed_url"
    if _norm_host(parsed) not in _LOOPBACK_HOSTS:
        return "non_loopback"
    if not (parsed.database or "").endswith("_test"):
        return "not_test_suffix"
    return "ok"


def raise_bad_config(raw_url: str, owner: str) -> URL:
    """无配置 -> SkipTest；坏配置/部分配置 -> ConfigError（连接前硬 fail）。

    返回解析后的声明 URL（仅配置其余检查都通过时）。本函数绝不连接库。
    """
    state = classify(raw_url, owner)
    if state == "missing_both":
        raise unittest.SkipTest(
            "未配置 TEST_DATABASE_URL 与 owner，跳过需真实 PG 的测试（无配置即不连接）"
        )
    if state == "url_only":
        raise ConfigError("部分配置：已给 TEST_DATABASE_URL 但缺 owner，拒绝")
    if state == "owner_only":
        raise ConfigError("部分配置：已给 owner 但缺 TEST_DATABASE_URL，拒绝")
    if state == "bad_owner":
        reason = owner_format_reason(owner)
        if reason == "owner_prefix":
            raise ConfigError(
                "TEST_DATABASE_OWNER 缺少显式测试库前缀"
                f"（允许 {', '.join(OWNER_PREFIXES)}），拒绝"
            )
        raise ConfigError("TEST_DATABASE_OWNER 标签格式不合法（需 8~128 位 [A-Za-z0-9._-]），拒绝")
    if state == "malformed_url":
        raise ConfigError("TEST_DATABASE_URL 不可解析")
    if state == "non_loopback":
        raise ConfigError("TEST_DATABASE_URL host 非 loopback")
    if state == "not_test_suffix":
        raise ConfigError("TEST_DATABASE_URL 必须指向以 _test 结尾的专用数据库")
    decl = _safe_parse(raw_url)
    _reject_routing_query(decl, "TEST_DATABASE_URL")  # 连接前拒重定向 query
    return decl


# 声明字段名 -> URL 对象实际属性（driver/user 与 URL 属性名不同）。
_FIELD_ATTR = {
    "driver": "drivername",
    "host": "host",
    "port": "port",
    "dbname": "database",
    "user": "username",
    "password": "password",
    "query": "query",
}


def _redirected(url: URL, field: str):
    # 只 host 允许小写归一（PG host 名大小写无关）；库名/用户/driver/凭据/query 均精确。
    raw = getattr(url, _FIELD_ATTR[field], None)
    if field == "host":
        return (raw or "").lower()
    if field in ("user", "driver"):
        return raw or ""
    return raw


def _redact(url: URL, field: str) -> str:
    if field == "password":
        return _PASSWORD_REDACTED if getattr(url, "password", None) is not None else "<none>"
    if field == "query":
        return _PASSWORD_REDACTED if getattr(url, "query", None) else "<none>"
    if field == "host":
        return repr((getattr(url, _FIELD_ATTR[field], None) or "").lower())
    if field in ("user", "driver"):
        return repr(getattr(url, _FIELD_ATTR[field], None) or "")
    return repr(getattr(url, _FIELD_ATTR[field], None))


def _field_matches(actual: URL, decl: URL, field: str) -> bool:
    a = _redirected(actual, field)
    d = _redirected(decl, field)
    return a == d


# 连接 URL 里允许的窄白名单 query 键：只放这几项不改变落地目标的连接参数；
# 其它任何键（host/hostaddr/port/dbname/service/...）都可能把连接重定向到远端，
# 声明与实际两边即使相同也必须在连接前拒绝。值绝不进入错误信息。
_ALLOWED_QUERY_KEYS = {"connect_timeout", "application_name", "sslmode"}


def _reject_routing_query(url: URL, label: str) -> None:
    """连接前拒绝任何非白名单的 query 参数（防 hostaddr/port/service 等重定向）。"""
    query = getattr(url, "query", None) or {}
    bad = [k for k in query.keys() if str(k).lower() not in _ALLOWED_QUERY_KEYS]
    if bad:
        raise ConfigError(f"{label} query 含 {len(bad)} 个不允许的路由/连接参数（连接前拒绝）")


def compare_engine(decl: URL, engine: Any) -> None:
    """核实际 engine.url 与解析后声明 URL 逐字段一致。

    driver/host/port/库名/用户/凭据/query 任一不符 -> ConfigError（连接前硬 fail）。
    纯计算，绝不连接、绝不打印凭据。
    """
    try:
        actual = engine.url if isinstance(engine.url, URL) else make_url(str(engine.url))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError("无法解析实际 engine.url", f"{exc.__class__.__name__}") from None
    _reject_routing_query(actual, "实际 engine")
    mism = []
    for field in ("driver", "host", "port", "dbname", "user", "password", "query"):
        if not _field_matches(actual, decl, field):
            mism.append(
                f"{field} 不符: 实际{_redact(actual, field)} != 声明{_redact(decl, field)}"
            )
    if mism:
        raise ConfigError("实际 app engine 与声明 URL 不一致（连接前拒绝）", *mism)


# ---- 连接读取归属 comment（仅在配置核合法后调用）----

_OWNER_COMMENT_SQL = (
    "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
    "WHERE datname = current_database()"
)


def _read_owner_comment_conn(conn: Any) -> Optional[str]:
    return conn.execute(text(_OWNER_COMMENT_SQL)).scalar()


def read_owner_comment(engine: Any) -> Optional[str]:
    """连接实际 engine，读取当前库的 pg_database comment。会真的连接。"""
    with engine.connect() as conn:
        return _read_owner_comment_conn(conn)


def verify(
    raw_url: str,
    owner: str,
    engine: Any,
    comment_reader: Callable[[Any], Optional[str]] = read_owner_comment,
) -> str:
    """完整门：无配置/坏配置/engine 不符 -> SkipTest 或 ConfigError；之后才连接读取
    comment，必须与 owner 精确匹配才放行。返回匹配的 comment。"""
    decl = raise_bad_config(raw_url, owner)  # 无连接
    compare_engine(decl, engine)  # 无连接
    marker = comment_reader(engine)  # 配置已核合法，此处才允许连接
    if marker is None:
        raise ConfigError("目标库无 owner comment，拒绝破坏性测试")
    if marker != owner:
        raise ConfigError("库 owner comment 与声明 owner 不精确匹配，拒绝破坏性测试")
    return marker


# ---- 显式准备库归属（CI / 新建一次性库后执行一次）----


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_PG_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def _quote_identifier(engine: Any, value: str) -> str:
    """库名加引号；优先用方言的 identifier_preparer，取不到时退回安全校验 + 手写引号。"""
    preparer = getattr(getattr(engine, "dialect", None), "identifier_preparer", None)
    if preparer is not None:
        return preparer.quote(value)
    if not _PG_IDENT.match(value):
        raise ConfigError("目标库名包含不支持的标识符字符，拒绝拼接 COMMENT 语句")
    return '"' + value.replace('"', '""') + '"'


def prepare_owner(
    raw_url: str,
    owner: str,
    engine: Any = None,
    engine_factory: Optional[Callable[[URL], Any]] = None,
) -> str:
    """把目标库 comment 设为 owner（幂等；绝不覆盖其它 owner 的库）。

    连接前完成全部纯配置校验；只有 loopback + ``_test`` + 合规 owner 的库才会连接。
    返回 ``"already-set"`` 或 ``"created"``；库已有其它非空 comment -> ConfigError。
    """
    decl = raise_bad_config(raw_url, owner)  # 无连接
    created_engine = False
    if engine is None:
        factory = engine_factory or (lambda url: create_engine(url, pool_pre_ping=True))
        engine = factory(decl)
        created_engine = True
    try:
        compare_engine(decl, engine)  # 无连接
        with engine.begin() as conn:
            current = _read_owner_comment_conn(conn)
            if current == owner:
                return "already-set"
            if current is not None:
                raise ConfigError("目标库已有其它 owner comment，拒绝覆盖（fail closed）")
            quoted_db = _quote_identifier(engine, decl.database)
            conn.execute(text(f"COMMENT ON DATABASE {quoted_db} IS {_quote_literal(owner)}"))
            return "created"
    finally:
        if created_engine:
            engine.dispose()


def main(
    argv: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    engine_factory: Optional[Callable[[URL], Any]] = None,
) -> int:
    """CLI：``python -m tests._memory_project_test_gate --prepare-owner``。

    只从环境读取 URL/owner；输出不含凭据。返回进程退出码（0 成功）。
    """
    import argparse

    parser = argparse.ArgumentParser(description="一次性测试库归属准备（fail closed）")
    parser.add_argument(
        "--prepare-owner",
        action="store_true",
        help="把 TEST_DATABASE_OWNER 写入 TEST_DATABASE_URL 指向库的 pg_database comment",
    )
    args = parser.parse_args(argv)
    if not args.prepare_owner:
        parser.print_help(sys.stderr)
        return 2
    source = os.environ if env is None else env
    raw = (source.get("TEST_DATABASE_URL") or "").strip()
    owner = (
        (source.get("TEST_DATABASE_OWNER") or source.get("TEST_EXPECTED_OWNER") or "").strip()
    )
    try:
        status = prepare_owner(raw, owner, engine_factory=engine_factory)
    except unittest.SkipTest as exc:
        print(f"error: {exc}（CI 必须显式配置测试库）", file=sys.stderr)
        return 3
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 连接/写入失败：只给稳定诊断，不回显原始消息
        # 原始驱动异常可能回显目标 host/conninfo；这里只给异常类名与操作阶段，
        # 绝不输出 URL/凭据，也避免 CI 里出现大段 traceback。
        print(
            f"error: 无法连接或写入目标测试库（{exc.__class__.__name__}）；"
            "请确认 TEST_DATABASE_URL 指向的可丢弃测试库已就绪",
            file=sys.stderr,
        )
        return 4
    print(f"owner-comment-{status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
