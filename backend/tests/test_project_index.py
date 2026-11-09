"""project_index 纯函数离线验收（不触 DB / 不触发真实同步）。

使用 stdlib ``unittest``（不依赖 pytest）。覆盖点（全部可离线验证，无 DB、
无模型调用）：
- 点 7：``_map_parse_status`` 状态映射；
- 点 8：指纹确定性；embedding 指纹在本地(ollama)/云端用不同基址；
- manifest_fingerprint 顺序无关；
- 点 9：``_read_blob_verified`` 有界校验读（ok / 大小不符 / 缺失 -> None）；
- 点 7：``_parse_in_subprocess`` owned 子进程解析（无 def/class 的 Python 文件）；
- 分块构建：``_build_chunks`` 接受纯值 dict（含 parse_status/embedding 指纹），
  metadata 不含 snapshot_id；source_metadata 带 source_id/source_kind/file_version_id。

该文件由 Q 独占，不耦合其他模块（scanner/parser/blob）的修复状态；解析子进程
路径只在解析器能正常收敛时做断言。
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.core.config import settings
from app.models import (
    KNOWLEDGE_BASE_KIND_PROJECT,
    ProjectChunk,
    ProjectFileParseStatus,
    ProjectFileVersion,
)
from app.models import ProjectSnapshot
from app.services.project_index import (
    AUTO_FAILED_CODE,
    AUTO_EMPTY_CODE,
    AUTO_EMPTY_MESSAGE,
    AUTO_UNCHANGED_CODE,
    EMBEDDING_DIM,
    _build_chunks,
    _fresh_lease,
    _hex,
    _map_parse_status,
    _parse_in_subprocess,
    _read_blob_verified,
    _safe_auto_error_code,
    _vector_documents,
    compute_chunker_fingerprint,
    compute_config_fingerprint,
    compute_embedding_fingerprint,
    compute_parser_fingerprint,
    manifest_fingerprint,
)
from app.services.project_blobs import BlobStore
from app.services.project_types import ManifestEntry


# ---------------- 点 7：解析状态映射 ----------------

class MapParseStatusTest(unittest.TestCase):
    _CASES = {
        "ast": "ready",
        "tree-sitter": "ready",
        "vue": "ready",
        "markdown": "ready",
        "fallback": "fallback",
        "skipped": "fallback",
        "unknown-thing": "fallback",
    }

    def test_mapping_ready_statuses(self) -> None:
        for raw, expected in self._CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    _map_parse_status(raw), ProjectFileParseStatus(expected).value
                )


# ---------------- 点 8：指纹 ----------------

class FingerprintsTest(unittest.TestCase):
    def test_deterministic(self) -> None:
        self.assertEqual(compute_parser_fingerprint(), compute_parser_fingerprint())
        self.assertEqual(compute_chunker_fingerprint(), compute_chunker_fingerprint())
        self.assertEqual(compute_config_fingerprint(), compute_config_fingerprint())

    def test_config_fingerprint_combines_parts(self) -> None:
        # config = hex(parser + chunker + embedding)；必须是 64-hex 且与任一子分量不同。
        cfg = compute_config_fingerprint()
        self.assertEqual(len(cfg), 64)
        self.assertNotEqual(cfg, compute_parser_fingerprint())
        self.assertNotEqual(cfg, compute_chunker_fingerprint())

    def test_manifest_fingerprint_order_independent(self) -> None:
        entries = [
            ManifestEntry(relative_path="b/x.py", sha256="1" * 64, size=10),
            ManifestEntry(relative_path="a/y.py", sha256="2" * 64, size=20),
        ]
        fwd = manifest_fingerprint(entries)
        rev = manifest_fingerprint(list(reversed(entries)))
        self.assertEqual(fwd, rev)
        self.assertEqual(len(fwd), 64)


class EmbeddingFingerprintProviderTest(unittest.TestCase):
    """embedding 指纹的基址分支必须与 ``vector_store.get_embeddings`` 一致。

    回归：旧实现用 ``provider == "local"`` 选 OLLAMA_BASE_URL，而默认 provider
    是 ``"ollama"``，会错误使用云端基址；自动同步的 unchanged 判定依赖该指纹。
    """

    OLLAMA = "http://ollama.local:11434"
    CLOUD = "https://cloud.example.invalid/v1"

    def _fingerprint(self, provider: str) -> str:
        with (
            patch.object(settings, "EMBEDDING_PROVIDER", provider),
            patch.object(settings, "EMBEDDING_MODEL", "model-x"),
            patch.object(settings, "OLLAMA_BASE_URL", self.OLLAMA),
            patch.object(settings, "EMBEDDING_BASE_URL", self.CLOUD),
        ):
            return compute_embedding_fingerprint()

    def _expected(self, provider: str, base: str) -> str:
        return _hex(["embedding", provider, "model-x", base, str(EMBEDDING_DIM)])

    def test_cloud_provider_uses_cloud_base_url(self) -> None:
        self.assertEqual(self._fingerprint("cloud"), self._expected("cloud", self.CLOUD))

    def test_non_cloud_providers_use_ollama_base_url(self) -> None:
        # get_embeddings 只有 provider == "cloud" 走云端，其余（含 ollama/local）
        # 一律是 Ollama；指纹必须完全一致。
        for provider in ("ollama", "local", "anything-else"):
            with self.subTest(provider=provider):
                self.assertEqual(
                    self._fingerprint(provider),
                    self._expected(provider, self.OLLAMA),
                )

    def test_default_ollama_differs_from_cloud(self) -> None:
        self.assertNotEqual(self._fingerprint("ollama"), self._fingerprint("cloud"))


class SafeAutoErrorCodeTest(unittest.TestCase):
    """自动同步只记录稳定错误码；路径/正文/未知形态一律收敛为安全通用码。"""

    def test_stable_codes_pass_through(self) -> None:
        for code in (
            "sync-error", "parse-error", "root-error", "incomplete",
            AUTO_EMPTY_CODE, AUTO_UNCHANGED_CODE, "max-attempts",
        ):
            with self.subTest(code=code):
                self.assertEqual(_safe_auto_error_code(code), code)

    def test_paths_content_and_unknown_shapes_collapse(self) -> None:
        for code in (
            None, "", "C:\\secret\\project\\a.py", "/abs/path/a.py",
            "UPPER", "with space", "a" * 65, "含正文内容",
        ):
            with self.subTest(code=code):
                self.assertEqual(_safe_auto_error_code(code), AUTO_FAILED_CODE)

    def test_fixed_auto_messages_are_stable(self) -> None:
        self.assertEqual(AUTO_EMPTY_CODE, "auto-empty")
        self.assertEqual(AUTO_EMPTY_MESSAGE, "自动同步得到空清单，已保留当前版本；请手动同步确认")


# ---------------- 点 9：有界校验读 ----------------

def _write_blob(store: BlobStore, data: bytes) -> str:
    return store.write_bytes(data).sha256


class ReadBlobVerifiedTest(unittest.TestCase):
    def test_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlobStore(Path(td))
            data = b"hello project"
            sha = _write_blob(store, data)
            self.assertEqual(_read_blob_verified(store, sha, len(data)), data)

    def test_wrong_size_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlobStore(Path(td))
            data = b"hello project"
            sha = _write_blob(store, data)
            # size 大于实际：verify_blob 按 expected_size 读不到/判不符 -> None
            self.assertIsNone(_read_blob_verified(store, sha, len(data) + 5))

    def test_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlobStore(Path(td))
            missing = "a" * 64
            self.assertIsNone(_read_blob_verified(store, missing, 10))


# ---------------- 点 7：owned 子进程解析 ----------------

class ParseInSubprocessTest(unittest.TestCase):
    def test_no_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlobStore(Path(td))
            code = b"x = 1\ny = x + 2\nprint(y)\n"
            ref = store.write_bytes(code)
            info = {
                "content_sha256": ref.sha256,
                "size": ref.size,
                "relative_path": "sample.py",
                "language": "python",
            }
            payload = _parse_in_subprocess(store, info, 20.0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["parser_status"], "ast")
            self.assertEqual(payload["symbols"], [])


# ---------------- 分块构建（离线构造纯值 dict，不触 DB） ----------------

class BuildChunksTest(unittest.TestCase):
    def test_no_snapshot_in_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = BlobStore(Path(td))
            data = b"def foo():\n    return 1\n"
            ref = store.write_bytes(data)
            version_id = uuid.uuid4()
            source_id = uuid.uuid4()
            # ``_build_chunks`` 只接收纯值 dict（含 parse_status / 嵌入指纹），
            # 绝不接收跨事务的 ORM 对象（见点 2 短事务重构）。
            values = {
                "id": version_id,
                "source_id": source_id,
                "relative_path": "sample.py",
                "content_sha256": ref.sha256,
                "size": ref.size,
                "language": "python",
                "config_fingerprint": compute_config_fingerprint(),
                "embedding_fingerprint": compute_embedding_fingerprint(),
                "parse_status": ProjectFileParseStatus.READY.value,
            }
            payload = {
                "parser_status": "ast",
                "symbols": [
                    {"name": "foo", "kind": "function", "start_line": 1, "start_col": 5,
                     "end_line": 2, "end_col": 15},
                ],
            }
            chunks = _build_chunks(values, data.decode("utf-8"), payload)
            self.assertIsInstance(chunks, list)
            self.assertTrue(chunks)
            first = chunks[0]
            self.assertIsInstance(first, ProjectChunk)
            self.assertEqual(first.symbol, "foo")
            self.assertEqual(first.symbol_kind, "function")
            self.assertEqual(first.source_metadata["source_id"], str(source_id))
            self.assertEqual(
                first.source_metadata["source_kind"], KNOWLEDGE_BASE_KIND_PROJECT
            )
            self.assertEqual(first.source_metadata["file_version_id"], str(version_id))
            # 健康相关：metadata 绝不含 snapshot_id
            self.assertNotIn("snapshot_id", first.source_metadata)

    def test_long_single_line_is_indexed_in_full(self) -> None:
        """>2000 字符的单行必须被完整分块（不静默丢失尾部），且行号如实为第 1 行。"""
        line = "x = '" + ("a" * 5000) + "'\n"
        payload = {"parser_status": "ast", "symbols": []}
        values = {
            "id": uuid.uuid4(), "source_id": uuid.uuid4(), "relative_path": "long.py",
            "content_sha256": "0" * 64, "size": len(line), "language": "python",
            "config_fingerprint": compute_config_fingerprint(),
            "embedding_fingerprint": compute_embedding_fingerprint(),
            "parse_status": ProjectFileParseStatus.READY.value,
        }
        chunks = _build_chunks(values, line, payload)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(c.content for c in chunks), line)
        self.assertTrue(all(c.line_start == 1 and c.line_end == 1 for c in chunks))
        self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))


# ---------------- 租约类型一致性 ----------------

class FreshLeaseTest(unittest.TestCase):
    def test_lease_is_uuid_not_str(self) -> None:
        """``ProjectSnapshot.lease_id`` 是 ``UUID(as_uuid=True)``；租约必须是 UUID。

        若返回 ``uuid4().hex``（str），ORM 读回的 UUID 与 str 比较恒不等，所有租约
        复核（``_lock_snapshot_source`` / 心跳 / CAS 发布）都会拒绝本 worker，任务静默
        永不完成。这里同时锁定本函数类型与模型列类型，防止任一侧回退。
        """
        lease = _fresh_lease()
        self.assertIsInstance(lease, uuid.UUID)
        self.assertNotIsInstance(lease, str)
        self.assertIsInstance(lease, ProjectSnapshot.__table__.c.lease_id.type.python_type)

    def test_each_lease_is_unique(self) -> None:
        self.assertNotEqual(_fresh_lease(), _fresh_lease())


# ---------------- 向量 cmetadata 必须可被检索 JOIN 命中 ----------------

class VectorDocumentsTest(unittest.TestCase):
    def test_metadata_carries_chunk_index_and_file_version(self) -> None:
        version_id = uuid.uuid4()
        batch = [
            {"id": uuid.uuid4(), "chunk_index": 0, "content": "aaa",
             "source_metadata": {"relative_path": "a.py", "source_id": str(uuid.uuid4())}},
            {"id": uuid.uuid4(), "chunk_index": 7, "content": "bbb", "source_metadata": {}},
        ]
        docs = _vector_documents(batch, version_id)
        self.assertEqual([d.metadata["chunk_index"] for d in docs], [0, 7])
        self.assertTrue(all(d.metadata["file_version_id"] == str(version_id) for d in docs))
        self.assertTrue(all(d.metadata["index_version"] == 0 for d in docs))
        self.assertTrue(all(d.metadata["source_kind"] == KNOWLEDGE_BASE_KIND_PROJECT for d in docs))
        # 既有 cmeta 不被丢弃
        self.assertEqual(docs[0].metadata["relative_path"], "a.py")
        self.assertEqual([d.page_content for d in docs], ["aaa", "bbb"])

    def test_stale_metadata_cannot_override_authoritative_identity(self) -> None:
        version_id = uuid.uuid4()
        batch = [{
            "id": uuid.uuid4(), "chunk_index": 3, "content": "x",
            # 脏旧 cmeta：绝不能覆盖本次写入的权威 chunk_index/file_version_id
            "source_metadata": {"chunk_index": 99, "file_version_id": str(uuid.uuid4())},
        }]
        doc = _vector_documents(batch, version_id)[0]
        self.assertEqual(doc.metadata["chunk_index"], 3)
        self.assertEqual(doc.metadata["file_version_id"], str(version_id))


if __name__ == "__main__":
    unittest.main()
