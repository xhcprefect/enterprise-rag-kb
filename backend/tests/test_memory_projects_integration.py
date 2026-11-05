"""V 夹具真实 PG 专项：只在本轮 owned 的 integration_test 库上跑，不做任何清库。

安全前置（每个用例都先做 owner 核验）：
  - 只有显式 ``TEST_DATABASE_URL`` 指向本机 loopback 且库名 ``_test`` 结尾才连；
  - 连接后读取 ``pg_database`` 的 comment，必须与 owner 精确匹配
    （owner 取 ``TEST_DATABASE_OWNER``；也兼容旧名 ``TEST_EXPECTED_OWNER``，
    但两者都缺时**不算通过**——配置错误要 fail，缺 owner 不能视 pass）；
  - 仅当二者都满足才执行后续；否则**无配置→skip，有配置但错误→fail**。
  - 本测试只做只读核验 + ``init_db`` 幂等建表（create_all/ALTER，不 drop），并确认
    memory/project 新表存在。不写业务数据、不 truncate。

显式已给 owned DB 时，``init_db`` / app 导入失败必须真失败（fail），绝不隐藏成 skip；
并且必须校验**实际 app engine** 与声明 ``TEST_DATABASE_URL`` 的 host/port/库名一致。

运行（禁 dotenv，指向本轮 owned 库）：
  RAG_DISABLE_DOTENV=1 \
  TEST_DATABASE_URL='postgresql+psycopg://.../integration_test' \
  TEST_DATABASE_OWNER='owned-test:<40hex>' \
  python -m unittest tests/test_memory_projects_integration.py -v
无上述 env 时全部 skip（离线 discover 不报失败）。
直接运行脚本（非 unittest 导入）时，覆写为本轮 owned 库默认，便于手动核对。
"""
from __future__ import annotations

import os
import unittest

import psycopg
from sqlalchemy.engine import make_url

from tests import _memory_project_test_gate as gate

TEST_URL = gate.TEST_DATABASE_URL
# owner 统一优先 TEST_DATABASE_OWNER，兼容既有 TEST_EXPECTED_OWNER；缺 owner 不算 pass。
EXPECTED_OWNER = gate.TARGET_OWNER


def _db_comment(host, port, dbname, user, password):
    # 关键字参数而非拼接 conninfo：密码含空格/引号等字符时不会被误解析。
    with psycopg.connect(host=host, port=port, dbname=dbname, user=user,
                         password=password, connect_timeout=10) as conn:
        row = conn.execute(
            "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
            (dbname,)).fetchone()
        return row[0] if row else None


def _parse_url(url):
    u = make_url(url)
    return {"host": (u.host or "").lower(), "port": u.port or 5432,
            "dbname": u.database or "", "user": u.username or "",
            "password": u.password or ""}


def _owner_ok() -> tuple[bool, str]:
    """有配置(BOTH URL+owner)才核验归属；任一缺 → 配置错误(非 pass)。

    配置合法性（部分配置 / 坏 URL / 非 loopback / 非 ``_test`` / owner 标签格式）
    统一由共享门 ``_memory_project_test_gate`` 判定；这里只用 psycopg 独立读取 comment，
    不依赖 app engine。
    """
    state = gate.classify(TEST_URL, EXPECTED_OWNER)
    if state == "missing_both":
        return False, "缺少 TEST_DATABASE_URL 与 owner"
    try:
        gate.raise_bad_config(TEST_URL, EXPECTED_OWNER)
    except gate.ConfigError as exc:
        return False, str(exc)
    u = _parse_url(TEST_URL)
    try:
        comment = _db_comment(u["host"], u["port"], u["dbname"], u["user"], u["password"])
    except Exception as exc:  # noqa: BLE001
        return False, f"读 comment 失败: {exc}"
    if not comment:
        return False, "目标库无 owner comment"
    if comment != EXPECTED_OWNER:
        return False, f"owner comment 不匹配: {comment!r} != {EXPECTED_OWNER!r}"
    return True, comment


# 在导入任何 app.* 之前按 TEST 配置设 DATABASE_URL（对齐 tests/_env.py 约定）。
if TEST_URL:
    os.environ["DATABASE_URL"] = TEST_URL
    os.environ["JWT_SECRET"] = "memory-project-integration-test-only-not-production"


def _app_engine_matches(declared_url: str, engine) -> tuple[bool, str]:
    """校验实际 app.db.session.engine 绑定的 URL 与声明的 TEST_URL 一致。"""
    try:
        actual = make_url(str(engine.url))
        decl = make_url(declared_url)
    except Exception as exc:  # noqa: BLE001
        return False, f"解析 engine/声明 URL 失败: {exc}"
    mism = []
    if (actual.host or "").lower() != (decl.host or "").lower():
        mism.append(f"host {actual.host!r} != {decl.host!r}")
    if actual.port not in (None, decl.port):
        mism.append(f"port {actual.port} != {decl.port}")
    if (actual.database or "").lower() != (decl.database or "").lower():
        mism.append(f"dbname {actual.database!r} != {decl.database!r}")
    if mism:
        return False, "app engine 与声明 URL 不一致: " + "; ".join(mism)
    return True, "engine URL 与声明一致"


_MISSING = gate.classify(TEST_URL, EXPECTED_OWNER) == "missing_both"


@unittest.skipIf(_MISSING, "未配置 TEST_DATABASE_URL 与 owner（真实 PG 未配置）")
class OwnerGateTest(unittest.TestCase):
    def test_owner_verification_required(self):
        # 无配置 → skip（模块级 skipIf）；有配置（无论对错）→ 必须核验，错则 fail。
        ok, msg = _owner_ok()
        self.assertTrue(ok, msg)
        self.assertEqual(msg, EXPECTED_OWNER)


@unittest.skipIf(_MISSING, "未配置 TEST_DATABASE_URL 与 owner（真实 PG 未配置）")
class InitDbIdempotencyTest(unittest.TestCase):
    def test_init_db_twice_creates_memory_project_tables(self):
        ok, msg = _owner_ok()
        if not ok:
            self.fail(f"owner 校验必须先通过: {msg}")
        # 显式已给 owned DB：app 导入失败必须真失败（fail），不隐藏成 skip。
        from app.db.init_db import init_db
        from app.db.session import engine
        from sqlalchemy import inspect

        # 校验实际 app engine 与声明 URL 一致（防止 app 连到别的库）。
        eok, emsg = _app_engine_matches(TEST_URL, engine)
        self.assertTrue(eok, emsg)
        init_db(create_admin=False)   # 第一遍：建表
        init_db(create_admin=False)   # 第二遍：幂等
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        for name in ("conversation_memories", "summary_jobs", "project_sources",
                     "project_snapshots", "project_file_versions", "project_snapshot_files",
                     "project_chunks"):
            self.assertIn(name, tables, f"缺少新表 {name}")


if __name__ == "__main__":
    # 直接运行（非 unittest 导入）同样只走显式环境，不再硬写本地端口/凭据：
    # TEST_DATABASE_URL / TEST_DATABASE_OWNER 必须由调用方给出，否则整组 skip。
    unittest.main(verbosity=2)
