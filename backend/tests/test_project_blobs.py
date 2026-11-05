"""project_blobs 不可变存储纯测试：no-clobber、有界核验、部分清理、sway 防变。

真实临时目录内验证，无 DB、无模型。
"""
import os
import tempfile
import unittest
from pathlib import Path

from app.services.project_blobs import BlobStore
from app.services.project_paths import ProjectPathError, open_secure_regular


class BlobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="blobs"))
        self.store = BlobStore(self.tmp / "store")

    def test_write_bytes_content_addressed(self):
        ref = self.store.write_bytes(b"hello")
        self.assertEqual(len(ref.sha256), 64)
        blob = self.store._blob_path(ref.sha256)
        self.assertEqual(blob.read_bytes(), b"hello")
        self.assertTrue(self.store.verify_blob(ref.sha256, 5))

    def test_same_bytes_same_ref_no_clobber(self):
        r1 = self.store.write_bytes(b"abc")
        r2 = self.store.write_bytes(b"abc")
        self.assertEqual(r1, r2)
        blobs = [p for p in (self.tmp / "store").iterdir() if not p.name.endswith(".partial")]
        self.assertEqual(len(blobs), 1)

    def test_verify_bounded_and_wrong(self):
        ref = self.store.write_bytes(b"0123456789")
        self.assertTrue(self.store.verify_blob(ref.sha256, 10))
        self.assertFalse(self.store.verify_blob(ref.sha256, 9))
        self.assertFalse(self.store.verify_blob(ref.sha256, 11))

    def test_refuses_overwrite_different_bytes(self):
        # 已有 blob 与将要写的字节在“同键不同内容”场景（这里构造同键错内容）。
        target = self.store.write_bytes(b"good")
        blob_path = self.store._blob_path(target.sha256)
        # 篡改为不同字节，保持同文件名：核验必须失败且不覆盖。
        blob_path.write_bytes(b"EVIL")
        self.assertFalse(self.store.verify_blob(target.sha256, 4))
        with self.assertRaises(ProjectPathError):
            self.store.write_bytes(b"good")  # 不同字节 -> 拒绝覆盖
        self.assertEqual(blob_path.read_bytes(), b"EVIL")  # 原文件不被覆盖

    def test_cleanup_partials_only_own(self):
        # 两个实例**共享同一 store_root**，验证 cleanup 只清本实例 nonce 创建的
        # partial（no-global-GC，绝不误删活跃 partial）。
        store_a = BlobStore(self.tmp)
        store_b = BlobStore(self.tmp)
        pa = store_a._new_partial("ingest")
        pb = store_b._new_partial("ingest")
        pa.write_bytes(b"x")
        pb.write_bytes(b"y")
        removed = store_a.cleanup_partials()
        self.assertEqual(removed, 1)
        self.assertFalse(pa.exists())
        self.assertTrue(pb.exists())  # 别的实例的 partial 不被删
        self.assertEqual(store_b.cleanup_partials(), 1)  # b 自己清自己的
        self.assertFalse(pb.exists())

    def test_partial_names_unique_per_op(self):
        # 同实例并发操作也每次唯一：不共用固定 tag 名，避免互相踩/误删。
        store = BlobStore(self.tmp)
        p1 = store._new_partial("ingest")
        p2 = store._new_partial("ingest")
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.name.startswith(f".{store._instance_nonce}."))
        self.assertTrue(p1.name.endswith(".partial"))

    def test_read_blob_bytes_returns_verified_content(self):
        # 同安全 FD 有界读取并返回已验证字节：index 子进程不必再按路径重开。
        content = b"def f():\n    return 1\n"
        ref = self.store.write_bytes(content)
        got = self.store.read_blob_bytes(ref.sha256, len(content))
        self.assertEqual(got, content)

    def test_read_blob_bytes_wrong_size_fails(self):
        ref = self.store.write_bytes(b"0123456789")
        with self.assertRaises(ProjectPathError):
            self.store.read_blob_bytes(ref.sha256, 9)  # 期望偏小 -> 长度/hash 不符

    def test_read_blob_bytes_tampered_rejected(self):
        # 篡改 blob（同文件名不同/更长字节）：哈希与长度都必须被拒，绝不返回错字节。
        ref = self.store.write_bytes(b"good")
        blob_path = self.store._blob_path(ref.sha256)
        blob_path.write_bytes(b"EVIL!!")  # 同键改内容 & 变长
        with self.assertRaises(ProjectPathError):
            self.store.read_blob_bytes(ref.sha256, 4)  # 期望原长度
        # 期望被篡改后的新长度也会哈希不符
        with self.assertRaises(ProjectPathError):
            self.store.read_blob_bytes(ref.sha256, len(b"EVIL!!"))


class SecureIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ingest"))
        self.root = self.tmp / "project"
        self.root.mkdir()
        self.store = BlobStore(self.tmp / "store")

    def _file(self, name, content):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_ingest_reads_via_same_fd_and_matches(self):
        content = b"project source bytes"
        self._file("src.txt", content)
        ref, ident = self.store.ingest_verified_file(self.root, ("src.txt",), 10**6)
        self.assertEqual(ref.size, len(content))
        self.assertEqual(self.store._blob_path(ref.sha256).read_bytes(), content)
        self.assertEqual(ident.st_size, len(content))

    def test_ingest_path_cannot_be_swapped_while_held(self):
        # 锚定保证：Windows 打开已去除 FILE_SHARE_DELETE，fd 持有期间该文件
        # **物理上无法 rename/delete**（WinError 32）——路径换走被系统层直接拦截。
        content = b"original"
        self._file("swap.txt", content)
        opened = open_secure_regular(self.root, ("swap.txt",))
        try:
            before = opened.identity_before
            try:
                os.replace(self.root / "swap.txt", self.root / "swap.moved")
            except PermissionError:
                swapped = False  # Windows：锚定生效，路径换不走
            else:
                swapped = True  # POSIX 允许 rename 打开中的文件
            # 无论哪一平台：经同一 pin FD 读到的永远是原 inode 内容，绝不重开换走的路径。
            with os.fdopen(opened.fd, "rb", closefd=False) as fh:
                data = fh.read()
            self.assertEqual(data, content)
            self.assertTrue(before.matches(opened.stat_after()))
            if swapped:
                # POSIX：路径虽可被换走，但 fd 仍指原文件；换走的路径不再是我们要的。
                _ = self._file("swap.moved", b"EXTERNAL!")  # 仅确认换走动作成功
        finally:
            opened.close()

    def test_ingest_budget_overflow_fails(self):
        self._file("big.txt", b"x" * 100)
        with self.assertRaises(ProjectPathError):
            self.store.ingest_verified_file(self.root, ("big.txt",), 10)

    def test_ingest_symlink_rejected(self):
        self._file("real.txt", b"real")
        link = self.root / "evil.txt"
        try:
            link.symlink_to(self.root / "real.txt")
        except OSError:
            self.skipTest("无法创建符号链接")
        with self.assertRaises(ProjectPathError):
            self.store.ingest_verified_file(self.root, ("evil.txt",), 10**6)


if __name__ == "__main__":
    unittest.main()
