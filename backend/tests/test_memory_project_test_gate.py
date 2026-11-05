"""纯 unittest 验收 _memory_project_test_gate 安全门（fake engine，禁止真实 DB/网络）。

仅依赖 SQLAlchemy 的 URL 结构；不连接任何引擎、不触碰真实环境变量（本模块在
启动阶段未设置 TEST_DATABASE_URL / owner，故所有走真实 env 读取的路径天然为
missing_both）。所有“已配置”场景都显式传入 URL / owner，绝不读取真实环境。

``prepare_owner`` 用注入的 fake engine / engine_factory 验收，绝不真连库。
"""
from __future__ import annotations

import io
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout

from sqlalchemy.engine import URL, make_url

from tests import _memory_project_test_gate as g

PASSWORD = "app_pass"
QUERY_SECRET = "sslmode=require"
OWNER = "owned-test:0123456789abcdef"
BASE = f"postgresql+psycopg2://app_user:{PASSWORD}@127.0.0.1:5432/app_test?{QUERY_SECRET}"


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    def __init__(self, comment, sink=None):
        self._comment = comment
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, *args, **kwargs):
        if self.sink is not None:
            self.sink.append(str(statement))
        return _FakeResult(self._comment)


class _FakeBegin:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        self.engine.connect_count += 1
        if self.engine._connect_fail:
            raise AssertionError("connect() 在坏配置下被调用了")
        return _FakeConn(self.engine._comment, self.engine.statements)

    def __exit__(self, *exc):
        return False


class FakeEngine:
    """fake engine：记录 connect 次数；connect_fail=True 时 connect 一被调用即失败。"""

    def __init__(self, url, comment=None, connect_fail=False):
        self.url = url if isinstance(url, URL) else make_url(url)
        self._comment = comment
        self._connect_fail = connect_fail
        self.connect_count = 0
        self.statements = []
        self.dialect = _FakeDialect()
        self.disposed = 0

    def connect(self):
        self.connect_count += 1
        if self._connect_fail:
            raise AssertionError("connect() 在坏配置下被调用了")
        return _FakeConn(self._comment, self.statements)

    def begin(self):
        return _FakeBegin(self)

    def dispose(self):
        self.disposed += 1


class _FakePreparer:
    def quote(self, value):
        return '"' + value.replace('"', '""') + '"'


class _FakeDialect:
    identifier_preparer = _FakePreparer()


def _traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _fake_comment_reader(engine):
    """配合 FakeEngine 的 comment_reader：连接并返回配置的 comment。"""
    return g.read_owner_comment(engine)


class MemoryProjectTestGateTest(unittest.TestCase):
    # ---- 无配置 -> SkipTest，绝不被当作 hard fail ----
    def test_missing_both_skip_via_raise(self):
        with self.assertRaises(unittest.SkipTest):
            g.raise_bad_config("", "")

    def test_missing_both_skip_via_verify_no_connect(self):
        bad = FakeEngine(BASE, connect_fail=True)
        with self.assertRaises(unittest.SkipTest):
            g.verify("", "", bad, comment_reader=_fake_comment_reader)
        self.assertEqual(bad.connect_count, 0)

    # ---- owner 标签格式：显式测试库前缀 + token ----
    def test_owner_format_accepts_supported_prefixes(self):
        for owner in (
            "owned-test:0123456789abcdef",
            "disposable-test:abcDEF012-3.4_5",
            "ci-disposable:12345678-1",
        ):
            with self.subTest(owner=owner):
                self.assertIsNone(g.owner_format_reason(owner))

    def test_owner_format_rejects_unsafe_labels(self):
        cases = {
            "missing_owner": "",
            "owner_prefix": "0123456789abcdef",
            "owner_prefix2": "test:0123456789abcdef",
            "owner_token_short": "owned-test:abc1234",
            "owner_token_space": "owned-test:abcd 1234",
            "owner_token_punct": "owned-test:abcdefgh/../x",
            "owner_token_leading": "owned-test:-abcdefgh",
            "owner_token_control": "owned-test:abc\tdefgh",
            "owner_token_long": "owned-test:" + "a" * 129,
        }
        for label, owner in cases.items():
            with self.subTest(label=label):
                self.assertIsNotNone(g.owner_format_reason(owner))

    def test_bad_owner_is_hard_fail_before_connect(self):
        for owner in ("project_owner", "owned-test:short"):
            with self.subTest(owner=owner):
                bad = FakeEngine(BASE, connect_fail=True)
                with self.assertRaises(g.ConfigError):
                    g.verify(BASE, owner, bad, comment_reader=_fake_comment_reader)
                self.assertEqual(bad.connect_count, 0)
                with self.assertRaises(g.ConfigError):
                    g.raise_bad_config(BASE, owner)

    # ---- 部分配置 / 坏 URL / 非 loopback / 非 _test -> ConfigError，绝不动 engine ----
    def test_bad_configs_hard_fail_and_never_connect(self):
        cases = [
            ("url_only", BASE, ""),
            ("owner_only", "", OWNER),
            ("malformed_url", "postgresql://user:p@127.0.0.1:notaport/db_test", OWNER),
            ("non_loopback", "postgresql://u:p@example.com:5432/db_test", OWNER),
            ("not_test_suffix", "postgresql://u:p@127.0.0.1:5432/prod_db", OWNER),
            ("bad_owner_prefix", BASE, "some_owner"),
        ]
        for label, raw, owner in cases:
            with self.subTest(label=label):
                bad = FakeEngine(BASE, connect_fail=True)
                with self.assertRaises(g.ConfigError):
                    g.verify(raw, owner, bad, comment_reader=_fake_comment_reader)
                self.assertEqual(bad.connect_count, 0, f"{label}: 坏配置下竟连接了")
                # 直接 raise_bad_config 同样硬 fail（skip 也不能掩盖）
                with self.assertRaises(g.ConfigError):
                    g.raise_bad_config(raw, owner)

    # ---- 路由 query 键：即使两边 URL 完全相同也必须连接前拒绝 ----
    def test_routing_query_rejected_before_connect(self):
        bad_keys = ["host", "hostaddr", "port", "dbname", "service", "unknownkey", "OPTIONS"]
        for key in bad_keys:
            with self.subTest(key=key):
                raw = f"postgresql://u:p@127.0.0.1/db_test?{key}=whatever"
                with self.assertRaises(g.ConfigError):
                    g.raise_bad_config(raw, OWNER)
                # compare_engine 对实惠 URL 也拒绝（即使与声明逐字段一致）
                same = make_url(raw)
                with self.assertRaises(g.ConfigError):
                    g.compare_engine(same, FakeEngine(raw))
        # 白名单键全部放行
        allowed = "postgresql://u:p@127.0.0.1/db_test?connect_timeout=5&application_name=x&sslmode=require"
        decl = g.raise_bad_config(allowed, OWNER)
        self.assertIsNotNone(decl)
        g.compare_engine(decl, FakeEngine(allowed))  # 不抛即放行

    # ---- 逐字段核对（只有 host 可小写，DB 大小写精确，actual None 端口不匹配指定端口）----
    def test_compare_engine_field_mismatch_matrix(self):
        identical = make_url(BASE)
        g.compare_engine(identical, FakeEngine(BASE))  # 全匹配应通过

        decl = make_url(BASE)
        mismatches = [
            ("driver", "postgresql://app_user:app_pass@127.0.0.1:5432/app_test?sslmode=require"),
            ("host", "postgresql+psycopg2://app_user:app_pass@localhost:5432/app_test?sslmode=require"),
            ("port", "postgresql+psycopg2://app_user:app_pass@127.0.0.1:5433/app_test?sslmode=require"),
            ("port_none_vs_specified", "postgresql+psycopg2://app_user:app_pass@127.0.0.1/app_test?sslmode=require"),
            ("dbname", "postgresql+psycopg2://app_user:app_pass@127.0.0.1:5432/APP_test?sslmode=require"),
            ("user", "postgresql+psycopg2://other:app_pass@127.0.0.1:5432/app_test?sslmode=require"),
            ("password", "postgresql+psycopg2://app_user:other_pass@127.0.0.1:5432/app_test?sslmode=require"),
            ("query", "postgresql+psycopg2://app_user:app_pass@127.0.0.1:5432/app_test?sslmode=disable"),
        ]
        for label, raw in mismatches:
            with self.subTest(field=label):
                with self.assertRaises(g.ConfigError) as cm:
                    g.compare_engine(decl, FakeEngine(raw))
                # 密码与 query 值绝不进入错误文本
                self.assertNotIn("other_pass", str(cm.exception))
                self.assertNotIn(PASSWORD, str(cm.exception))
                self.assertNotIn("disable", str(cm.exception))

    def test_compare_engine_host_lowercase_ok_but_case_sensitive_fields(self):
        # host 大小写归一（仅 host 允许）：声明大写形式的 IPv6 loopback，实际小写等效
        decl = make_url("postgresql://u:p@[::FFFF:127.0.0.1]:5432/app_test")
        g.compare_engine(decl, FakeEngine("postgresql://u:p@[::ffff:127.0.0.1]:5432/app_test"))
        # DB 大小写精确：APP_test != app_test
        with self.assertRaises(g.ConfigError):
            g.compare_engine(
                make_url("postgresql://u:p@127.0.0.1/app_test"),
                FakeEngine("postgresql://u:p@127.0.0.1/APP_test"),
            )

    def test_compare_engine_port_None_never_matches_specified(self):
        decl = make_url("postgresql://u:p@127.0.0.1:5432/app_test")  # 指定 5432
        actual_none = make_url("postgresql://u:p@127.0.0.1/app_test")  # 无端口（None）
        self.assertIsNone(actual_none.port)
        with self.assertRaises(g.ConfigError):
            g.compare_engine(decl, FakeEngine(actual_none))
        # 反向：声明无端口、实际有端口也不匹配
        with self.assertRaises(g.ConfigError):
            g.compare_engine(
                make_url("postgresql://u:p@127.0.0.1/app_test"),
                FakeEngine("postgresql://u:p@127.0.0.1:5432/app_test"),
            )

    # ---- verify 正常路径：真实 fake connect 恰好一次 ----
    def test_verify_happy_path_connects_once(self):
        engine = FakeEngine(BASE, comment=OWNER)
        marker = g.verify(BASE, OWNER, engine, comment_reader=_fake_comment_reader)
        self.assertEqual(marker, OWNER)
        self.assertEqual(engine.connect_count, 1)

    # ---- owner comment 逐字符匹配，绝不 strip 掩盖错值 ----
    def test_owner_comment_exact_does_not_strip(self):
        # owner 带空白，而库 comment 是精确值（无空白）-> 必须按原 owner 精确比较则 fail
        engine = FakeEngine(BASE, comment=OWNER)
        with self.assertRaises(g.ConfigError):
            g.verify(BASE, " " + OWNER, engine, comment_reader=_fake_comment_reader)
        # comment 带尾部空白而 owner 干净 -> fail
        engine2 = FakeEngine(BASE, comment=OWNER + " ")
        with self.assertRaises(g.ConfigError):
            g.verify(BASE, OWNER, engine2, comment_reader=_fake_comment_reader)
        # 库无 comment（None）-> fail
        engine3 = FakeEngine(BASE, comment=None)
        with self.assertRaises(g.ConfigError):
            g.verify(BASE, OWNER, engine3, comment_reader=_fake_comment_reader)
        # verify 成功路径能用 comment_reader 提供的定制 comment 值
        engine4 = FakeEngine(BASE, comment=OWNER)
        self.assertEqual(
            g.verify(BASE, OWNER, engine4, comment_reader=_fake_comment_reader), OWNER
        )

    # ---- 密码 / query 值绝不进入异常 str 或格式化 traceback（含 raise-from 链）----
    def test_password_not_in_exception_string_or_traceback(self):
        # 密码恰好落在 make_url 回显的畸形 token 上：若链条泄露会出现在 traceback
        raw = "postgresql://user:abc@127.0.0.1:abc/db_test"  # 密码 abc == 过错 port 回显
        # 直接 _safe_parse（原本 from exc 会链出含 abc 的原始异常）必须已修复
        with self.assertRaises(g.ConfigError) as cm:
            g._safe_parse(raw)
        self.assertNotIn("abc", str(cm.exception))
        self.assertNotIn("abc", _traceback(cm.exception))
        # 走公开 API 同样不泄露
        with self.assertRaises(g.ConfigError) as cm2:
            g.raise_bad_config(raw, OWNER)
        tb2 = _traceback(cm2.exception)
        self.assertNotIn("abc", str(cm2.exception))
        self.assertNotIn("abc", tb2)

    def test_query_value_not_in_exception_string_or_traceback(self):
        decl = make_url(BASE)
        # 密码在 mismatch 输出里必须 redacted，原始密码不出现在 str / traceback
        with self.assertRaises(g.ConfigError) as cm:
            g.compare_engine(decl, FakeEngine("postgresql+psycopg2://app_user:other@127.0.0.1:5432/app_test?sslmode=require"))
        self.assertNotIn(PASSWORD, str(cm.exception))
        self.assertNotIn(PASSWORD, _traceback(cm.exception))
        self.assertNotIn("other", str(cm.exception))
        self.assertNotIn("other", _traceback(cm.exception))

    # ---- 显式准备 owner（CI 步骤）：幂等、拒绝覆盖他人、拒绝坏配置 ----
    def test_prepare_owner_creates_comment_when_absent(self):
        engine = FakeEngine(BASE, comment=None)
        status = g.prepare_owner(BASE, OWNER, engine=engine)
        self.assertEqual(status, "created")
        self.assertTrue(any("COMMENT ON DATABASE" in s for s in engine.statements))
        self.assertTrue(any(OWNER in s for s in engine.statements))

    def test_prepare_owner_is_idempotent(self):
        engine = FakeEngine(BASE, comment=OWNER)
        self.assertEqual(g.prepare_owner(BASE, OWNER, engine=engine), "already-set")
        self.assertFalse(any("COMMENT ON DATABASE" in s for s in engine.statements))

    def test_prepare_owner_refuses_to_overwrite_foreign_comment(self):
        engine = FakeEngine(BASE, comment="disposable-test:someone-else-1")
        with self.assertRaises(g.ConfigError):
            g.prepare_owner(BASE, OWNER, engine=engine)
        self.assertFalse(any("COMMENT ON DATABASE" in s for s in engine.statements))

    def test_prepare_owner_validates_before_connecting(self):
        for raw, owner in (
            ("", ""),
            (BASE, ""),
            ("", OWNER),
            ("postgresql://u:p@example.com/db_test", OWNER),
            ("postgresql://u:p@127.0.0.1/prod_db", OWNER),
            (BASE, "not-an-owned-label"),
        ):
            with self.subTest(owner=owner):
                def factory(url):  # 若工厂被调用即证明提前连接了
                    raise AssertionError(f"提前连接了 {url!r}")

                with self.assertRaises((g.ConfigError, unittest.SkipTest)):
                    g.prepare_owner(raw, owner, engine_factory=factory)

    def test_prepare_owner_created_engine_is_disposed(self):
        engine = FakeEngine(BASE, comment=None)
        status = g.prepare_owner(BASE, OWNER, engine_factory=lambda url: engine)
        self.assertEqual(status, "created")
        self.assertEqual(engine.disposed, 1)

    def test_prepare_owner_engine_mismatch_refused(self):
        engine = FakeEngine("postgresql+psycopg2://app_user:app_pass@127.0.0.1:5432/OTHER_test?sslmode=require")
        with self.assertRaises(g.ConfigError):
            g.prepare_owner(BASE, OWNER, engine=engine)
        self.assertFalse(any("COMMENT ON DATABASE" in s for s in engine.statements))

    # ---- CLI：配置缺失诊断清晰且不泄露 ----
    def _run_main(self, env):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = g.main(["--prepare-owner"], env=env, engine_factory=lambda url: FakeEngine(url))
        return code, out.getvalue(), err.getvalue()

    def test_cli_missing_env_fails_with_diagnostic(self):
        code, out, err = self._run_main({})
        self.assertEqual(code, 3)
        self.assertIn("TEST_DATABASE", err)
        self.assertNotIn("Traceback", err)

    def test_cli_partial_config_is_diagnostic_not_traceback(self):
        code, out, err = self._run_main({"TEST_DATABASE_URL": BASE})
        self.assertEqual(code, 2)
        self.assertIn("部分配置", err)
        self.assertNotIn("Traceback", err)

    def test_cli_success_reports_status_without_secrets(self):
        code, out, err = self._run_main(
            {"TEST_DATABASE_URL": BASE, "TEST_DATABASE_OWNER": OWNER}
        )
        self.assertEqual(code, 0)
        self.assertIn("owner-comment-", out)
        self.assertNotIn(PASSWORD, out + err)

    def test_cli_connect_failure_is_diagnostic_without_traceback_or_secrets(self):
        # 驱动异常可能回显 conninfo（含密码）；CLI 只输出稳定诊断，不透传原始消息。
        class _UnreachableEngine(FakeEngine):
            def begin(self):
                raise ConnectionError(
                    f"could not connect: host=127.0.0.1 password={PASSWORD} dbname=app_test"
                )

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = g.main(
                ["--prepare-owner"],
                env={"TEST_DATABASE_URL": BASE, "TEST_DATABASE_OWNER": OWNER},
                engine_factory=lambda url: _UnreachableEngine(url),
            )
        self.assertEqual(code, 4)
        self.assertIn("无法连接或写入目标测试库", err.getvalue())
        text = out.getvalue() + err.getvalue()
        self.assertNotIn("Traceback", text)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("dbname=app_test", text)


if __name__ == "__main__":
    unittest.main()
