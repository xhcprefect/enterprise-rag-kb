"""project_retrieval 离线回归：可选文件范围、SQL 过滤语义、权威引用元数据。

无 DB、无模型调用：SQL 片段只编译成 PostgreSQL 方言文本做结构断言，不连库。
真实 Postgres 行为由主控的隔离 pipeline 测试覆盖。
"""
from __future__ import annotations

import unittest
import re
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import postgresql

from app.services import project_retrieval as pr


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class NormalizeFileVersionIdsTest(unittest.TestCase):
    def test_none_means_unrestricted(self) -> None:
        self.assertIsNone(pr.normalize_file_version_ids(None))

    def test_empty_list_means_no_results(self) -> None:
        self.assertEqual(pr.normalize_file_version_ids([]), ())

    def test_accepts_strings_and_uuids_dedupes_and_keeps_order(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        result = pr.normalize_file_version_ids([str(a), b, str(a), b])
        self.assertEqual(result, (a, b))

    def test_drops_unparseable_and_boolean_components(self) -> None:
        a = uuid.uuid4()
        result = pr.normalize_file_version_ids([str(a), "not-a-uuid", None, True, "", 42])
        self.assertEqual(result, (a,))

    def test_all_invalid_collapses_to_empty(self) -> None:
        self.assertEqual(pr.normalize_file_version_ids(["x", object()]), ())


class ScopeConditionSqlTest(unittest.TestCase):
    """文件范围必须在 SQL 侧（排名/LIMIT 前）生效，且空范围不得渲染成 IN ()。"""

    def setUp(self) -> None:
        self.kb = SimpleNamespace(collection_name="kb-project", is_public=True, owner_id=None)
        self.user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, is_active=True, role="user")
        self.snap_id = uuid.uuid4()

    def test_no_scope_has_no_file_version_predicate(self) -> None:
        conds = pr._scope_conditions(self.kb, self.snap_id, self.user, None, None)
        sql = " AND ".join(_sql(c) for c in conds)
        self.assertNotIn("project_file_versions.id IN", sql)

    def test_single_scope_renders_authoritative_id_filter(self) -> None:
        fv = uuid.uuid4()
        conds = pr._scope_conditions(self.kb, self.snap_id, self.user, None, (fv,))
        sql = " AND ".join(_sql(c) for c in conds)
        self.assertIn("project_file_versions.id IN", sql)
        self.assertIn(str(fv), sql)

    def test_empty_scope_renders_false_never_empty_in(self) -> None:
        conds = pr._scope_conditions(self.kb, self.snap_id, self.user, None, ())
        sqls = [_sql(c).lower() for c in conds]
        self.assertIn("false", sqls)
        self.assertFalse(any("in ()" in s for s in sqls))

    def test_scope_keeps_acl_and_same_source_guards(self) -> None:
        fv = uuid.uuid4()
        sql = " AND ".join(
            _sql(c) for c in pr._scope_conditions(
                self.kb, self.snap_id, self.user, ("src",), (fv,)
            )
        )
        # 同源映射 + 未撤销 + 解析状态 + embedded + ACL + 路径前缀 同时存在
        self.assertIn("project_snapshots.source_id = project_file_versions.source_id", sql)
        self.assertIn("project_sources.revoked_at IS NULL", sql)
        self.assertIn("project_chunks.embedded IS true", sql)
        self.assertIn("knowledge_bases.is_public IS true", sql)
        self.assertIn("project_file_versions.relative_path", sql)
        self.assertIn("project_file_versions.id IN", sql)


class EmptyScopeShortCircuitTest(unittest.TestCase):
    def test_empty_scope_returns_no_docs_without_embedding(self) -> None:
        db = MagicMock()
        db.in_transaction.return_value = False
        snap, source, kb = object(), object(), object()
        user = SimpleNamespace(id=uuid.uuid4())
        with patch.object(pr, "validate_project_binding", return_value=(snap, source, kb)), \
             patch.object(pr, "get_embeddings") as emb, \
             patch.object(pr, "get_vector_store") as store:
            docs, got = pr.retrieve_project(db, uuid.uuid4(), user, "query", 5,
                                            file_version_ids=[]), None
        self.assertEqual(docs, [])
        emb.assert_not_called()
        store.assert_not_called()

    def test_none_scope_keeps_legacy_behaviour(self) -> None:
        """旧调用（不传 file_version_ids）语义不变：仍走正常检索路径。"""
        db = MagicMock()
        db.in_transaction.return_value = False
        fake_embeddings = MagicMock()
        fake_embeddings.embed_query.side_effect = RuntimeError("no model in tests")
        kb = SimpleNamespace(collection_name="kb-project", is_public=True, owner_id=None)
        with patch.object(pr, "validate_project_binding", return_value=(object(), object(), kb)), \
             patch.object(pr, "get_vector_store", return_value=MagicMock()), \
             patch.object(pr, "get_embeddings", return_value=fake_embeddings) as emb:
            try:
                pr.retrieve_project(db, uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()), "query", 5)
            except Exception:  # noqa: BLE001 只为证明路径没被空范围提前短路
                pass
        # 未短路：确实走到了 embed（失败的桩只用于证明检索路径被执行过）
        emb.assert_called_once()
        fake_embeddings.embed_query.assert_called_once()


class HitMetaAuthorityTest(unittest.TestCase):
    def test_filename_is_relative_path_and_fingerprints_preserved(self) -> None:
        fv = uuid.uuid4()
        meta = pr._hit_meta(
            cmeta={"chunk_index": 999, "relative_path": "spoofed.py", "filename": "spoofed.py"},
            file_version_id=fv, relative_path="src/worker.py", language="python",
            parse_status="ready", sha="a" * 64, ls=3, le=8, sym="run", sym_kind="function",
            ci=4, snap_id=uuid.uuid4(), source_id=uuid.uuid4(),
            snapshot_config="c" * 64, snapshot_manifest="m" * 64,
            fv_config="f" * 64, fv_parser="p" * 64,
        )
        self.assertEqual(meta["filename"], "src/worker.py")
        self.assertEqual(meta["relative_path"], "src/worker.py")
        # 权威值覆盖脏 cmeta
        self.assertEqual(meta["chunk_index"], 4)
        self.assertEqual(meta["document_id"], str(fv))
        self.assertEqual(meta["snapshot_config_fingerprint"], "c" * 64)
        self.assertEqual(meta["snapshot_manifest_fingerprint"], "m" * 64)


class StillAuthorizedScopeTest(unittest.TestCase):
    def test_empty_scope_short_circuits(self) -> None:
        db = MagicMock()
        result = pr._still_authorized_fv_ids(
            db, object(), uuid.uuid4(), SimpleNamespace(id=uuid.uuid4()),
            [str(uuid.uuid4())], None, file_scope=(),
        )
        self.assertEqual(result, set())
        db.execute.assert_not_called()


class StillAuthorizedSqlShapeTest(unittest.TestCase):
    """回归：授权复核查询不得与 project_chunks 笛卡尔积。

    原实现把 ``ProjectChunk.embedded`` 塞进未关联 chunk 的 SELECT，导致
    ``project_chunks`` 出现在 FROM 与本查询其余表做笛卡尔积（真实日志 SAWarning），
    一条 FV 被放大成 N 行再被 Python ``set()`` 掩盖。现在必须用相关 EXISTS。
    """

    def setUp(self) -> None:
        self.kb = SimpleNamespace(collection_name="kb", is_public=True, owner_id=None)
        self.user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, is_active=True, role="user")

    def _sql(self, scope=None) -> str:
        stmt = pr._still_authorized_statement(
            self.kb, uuid.uuid4(), self.user, [uuid.uuid4()], None, scope
        )
        return re.sub(r"\s+", " ", _sql(stmt))

    def test_embedded_requirement_is_correlated_exists(self) -> None:
        sql = self._sql()
        self.assertIn("EXISTS (SELECT project_chunks.id", sql)
        self.assertIn(
            "project_chunks.file_version_id = project_file_versions.id", sql
        )
        self.assertIn("project_chunks.embedded IS true", sql)

    def test_no_bare_project_chunks_in_from(self) -> None:
        sql = self._sql()
        # project_chunks 只能出现在 EXISTS 子查询里；外层 FROM/JOIN 链中不得出现。
        outer = sql.split("EXISTS (")[0]
        self.assertNotIn("project_chunks", outer)
        self.assertNotIn("JOIN project_chunks", sql.replace("FROM project_chunks", ""))
        self.assertNotIn("FROM project_file_versions JOIN project_chunks", sql)
        self.assertEqual(sql.count("FROM project_chunks"), 1)  # 仅 EXISTS 内部那一个

    def test_statement_is_distinct(self) -> None:
        self.assertTrue(self._sql().startswith("SELECT DISTINCT"))

    def test_file_scope_is_applied(self) -> None:
        fv = uuid.uuid4()
        self.assertIn(str(fv), self._sql((fv,)))


class VectorJoinShapeTest(unittest.TestCase):
    """向量关联：cmetadata 之外再用确定性向量 id 兜底，兼容缺 chunk_index 的旧数据。"""

    def test_key_matches_write_side_format(self) -> None:
        from app.models import ProjectChunk

        expr = pr._project_vector_key(ProjectChunk.file_version_id, ProjectChunk.chunk_index)
        compiled = _sql(expr)
        self.assertIn("||", compiled)
        self.assertIn("':'" .replace("'", "'") if False else "':'", compiled)
        self.assertIn("project_chunks.chunk_index", compiled)

    def test_empty_prefix_disables_id_fallback(self) -> None:
        from app.models import ProjectChunk

        with patch.object(pr, "_project_vector_prefix", return_value=""):
            self.assertIsNone(
                pr._project_vector_key(ProjectChunk.file_version_id, ProjectChunk.chunk_index)
            )


class VectorPairsExecutionTest(unittest.TestCase):
    """真正执行 ``_project_vector_pairs``（构造 SQL 并 compile），而非只测 helper。

    原缺陷 ``if vector_key:`` 会对 SQLAlchemy 表达式求 bool，抛
    ``TypeError: Boolean value of this clause is not defined``；调用方会用
    ``except Exception`` 静默退回关键词，因此必须用“vector-only”路径（关键词为空）
    做断言，才能把这个异常暴露出来，而不是被 fallback 掩盖。
    """

    def setUp(self) -> None:
        from langchain_postgres.vectorstores import _get_embedding_collection_store

        self.rows = _get_embedding_collection_store(1024)[0]

    def _store(self):
        store = MagicMock()
        store.EmbeddingStore = self.rows
        store.get_collection.return_value = SimpleNamespace(uuid=uuid.uuid4())
        return store

    def _db(self):
        db = MagicMock()
        captured = {}

        def _execute(statement, *a, **k):
            # 立即 compile：布尔表达式误用会在构造/编译阶段抛 TypeError
            str(statement.compile(dialect=postgresql.dialect()))
            captured["sql"] = statement
            result = MagicMock()
            result.all.return_value = []
            return result

        db.execute.side_effect = _execute
        return db, captured

    def test_builds_query_without_type_error(self) -> None:
        from app.models import ProjectChunk

        chunk = ProjectChunk(
            file_version_id=uuid.uuid4(), chunk_index=0, content="x",
            search_text="x", source_metadata={}, embedded=True,
        )
        db, captured = self._db()
        pairs = pr._project_vector_pairs(
            db, self._store(), SimpleNamespace(collection_name="kb", is_public=True, owner_id=None),
            uuid.uuid4(), [0.0] * 1024, 10,
            min_similarity=0.0, user=SimpleNamespace(id=uuid.uuid4(), is_admin=False, is_active=True, role="user"),
            prefix_parts=None,
        )
        self.assertEqual(pairs, [])
        self.assertIn("sql", captured)
        compiled = _sql(captured["sql"])
        # 两个关联键都要在：cmetadata 新格式 + 确定性向量 id 旧格式兜底
        self.assertIn("cmetadata", compiled)
        self.assertIn("||", compiled)

    def test_legacy_metadata_still_joins_via_vector_id(self) -> None:
        """旧 cmetadata 缺 chunk_index 时，JOIN 仍含 id 兜底分支。"""
        db, captured = self._db()
        pr._project_vector_pairs(
            db, self._store(), SimpleNamespace(collection_name="kb", is_public=True, owner_id=None),
            uuid.uuid4(), [0.0] * 1024, 5,
            min_similarity=0.0, user=SimpleNamespace(id=uuid.uuid4(), is_admin=False, is_active=True, role="user"),
            prefix_parts=None,
        )
        compiled = _sql(captured["sql"])
        join_part = compiled.split("JOIN project_chunks ON")[1].split("WHERE")[0]
        self.assertIn("OR", join_part)


if __name__ == "__main__":
    unittest.main()
