"""project_scanner 纯测试：稳定 manifest、两身份、秘密/未知排除、忽略/负规则/globstar、
硬链接/期限/预算硬界。真实合成临时目录，无 DB/模型；不扫描真实工作区。

所有临时目录用 addCleanup 收尾。
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.services.project_blobs import BlobStore
from app.services.project_ignore import IgnoreMatcher, UnsupportedIgnorePattern
from app.services.project_scanner import ProjectScanError, scan_project
from app.services.project_types import ProjectScanLimits


def _mk_clean(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)


class ProjectLimitValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scan"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def test_validate_rejects_zero_and_negative(self):
        for kwargs in (
            {"max_files": 0},
            {"max_single_bytes": -1},
            {"max_iterations": 0},
            {"scan_timeout_ms": 0},
            {"max_total_bytes": -10},
            {"parse_timeout_ms": -1},
        ):
            with self.assertRaises(ValueError):
                ProjectScanLimits(**kwargs).validate()
        # 默认值与正整数一律通过；parse_timeout_ms 建议字段允许 0（未给）。
        ProjectScanLimits().validate()
        ProjectScanLimits(max_files=1, max_iterations=2).validate()
        ProjectScanLimits(parse_timeout_ms=0).validate()

    def test_scan_rejects_zero_limits(self):
        # API/本地宽配置传 0=无限值必须 fail closed，而非关闭上限。
        with self.assertRaises(ValueError):
            scan_project(
                self.tmp, BlobStore(self.tmp / "store"),
                limits=ProjectScanLimits(max_files=0), git_commit="x",
            )


class ScannerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scan"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = self.tmp / "proj"
        self.root.mkdir()
        self.store = BlobStore(self.tmp / "store")

    def _scan(self, **kwargs):
        return scan_project(self.root, self.store, **kwargs)

    def _file(self, rel, content=b"x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        return p


class ManifestTests(ScannerTestCase):
    def test_stable_sorted_manifest(self):
        self._file("z.txt", b"z")
        self._file("a/b/s.py", b"def x(): pass")
        self._file("m.md", b"# t")
        r = self._scan(git_commit="abc")
        paths = [e.relative_path for e in r.entries]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(set(paths), {"a/b/s.py", "m.md", "z.txt"})
        # 两次扫描确定性一致
        r2 = self._scan(git_commit="abc")
        self.assertEqual(r.entries, r2.entries)

    def test_same_bytes_different_paths_two_identities(self):
        content = b"identical"
        self._file("a/f1.txt", content)
        self._file("b/f2.txt", content)
        r = self._scan(git_commit="x")
        e1 = [e for e in r.entries if e.relative_path == "a/f1.txt"]
        e2 = [e for e in r.entries if e.relative_path == "b/f2.txt"]
        self.assertEqual(len(e1), 1)
        self.assertEqual(len(e2), 1)
        self.assertEqual(e1[0].sha256, e2[0].sha256)
        self.assertNotEqual(e1[0].relative_path, e2[0].relative_path)
        blobs = [p for p in (self.tmp / "store").iterdir() if not p.name.endswith(".partial")]
        self.assertEqual(len(blobs), 1)


class ExclusionTests(ScannerTestCase):
    def test_secret_env_and_key_never_reincluded(self):
        self._file(".env", b"SECRET=1")
        self._file("key.pem", b"PRIV")
        self._file(".gitignore", b"!.env\n!key.pem\n")
        r = self._scan(git_commit="x")
        paths = [e.relative_path for e in r.entries]
        self.assertNotIn(".env", paths)
        self.assertNotIn("key.pem", paths)

    def test_credentials_and_unknown_extension_never_reincluded(self):
        # credentials.json：凭据 stem 命中 -> secret；unknown.custom：未知扩展名。
        # 两者都硬排除，且 .gitignore 负规则不能把它们拉回来。
        self._file("credentials.json", b"{}")
        self._file("unknown.custom", b"x")
        self._file(".gitignore", b"!credentials.json\n!unknown.custom\n")
        r = self._scan(git_commit="x")
        paths = [e.relative_path for e in r.entries]
        self.assertNotIn("credentials.json", paths)
        self.assertNotIn("unknown.custom", paths)
        reasons = " | ".join(r.excluded)
        self.assertIn("secret", reasons)
        self.assertIn("unknown-extension", reasons)

    def test_ignore_negation_is_supported(self):
        # 用已知文本扩展名 .txt 隔离负规则语义（.tmp 属于未知扩展名会被硬排除，
        # 那样负规则不该拉回——正确）。
        self._file("a.txt", b"x")
        self._file("keep.txt", b"k")
        self._file(".gitignore", b"*.txt\n!keep.txt\n")
        r = self._scan(git_commit="x")
        paths = [e.relative_path for e in r.entries]
        self.assertIn("keep.txt", paths)  # 负规则真正生效
        self.assertNotIn("a.txt", paths)

    def test_globstar_ignore_parses_and_excludes(self):
        # `**/cache/**` 必须可解析（独立 ** 段），并能排除 cache 目录下的文件。
        self._file("cache/x.txt", b"x")
        self._file("keep.txt", b"k")
        self._file(".gitignore", b"**/cache/**\n")
        r = self._scan(git_commit="x")  # 不应抛 UnsupportedIgnorePattern
        paths = [e.relative_path for e in r.entries]
        self.assertNotIn("cache/x.txt", paths)
        self.assertIn("keep.txt", paths)

    def test_symlink_and_build_dir_excluded(self):
        self._file("real.txt", b"content")
        try:
            (self.root / "sub").mkdir()
            (self.root / "sub" / "f").write_bytes(b"f")
            (self.root / "link").symlink_to(self.root / "sub", target_is_directory=True)
        except OSError:
            self.skipTest("无法创建符号链接")
        self._file(".next/x.js", b"x")
        r = self._scan(git_commit="x")
        paths = [e.relative_path for e in r.entries]
        self.assertNotIn("link/f", paths)
        self.assertNotIn(".next/x.js", paths)

    def test_linked_gitignore_not_read(self):
        # .gitignore 是符号链接指向根外文件：绝不 follow，规则不生效。
        self._file("a.txt", b"x")
        external = self.tmp / "external.gitignore"
        external.write_text("*.txt\n", encoding="utf-8")
        try:
            (self.root / ".gitignore").symlink_to(external)
        except OSError:
            self.skipTest("无法创建符号链接")
        r = self._scan(git_commit="x")
        paths = [e.relative_path for e in r.entries]
        self.assertIn("a.txt", paths)  # 链接的忽略规则未被读取 -> 不放行忽略


class LimitTests(ScannerTestCase):
    def test_max_files_truncates_not_publishable(self):
        for i in range(4):
            self._file(f"f{i}.txt", b"x")
        limits = ProjectScanLimits(max_files=2, max_total_bytes=2**30)
        r = self._scan(limits=limits, git_commit="x")
        self.assertLessEqual(len(r.entries), 2)
        self.assertTrue(any("文件数" in n for n in r.limits))
        self.assertFalse(r.is_success)  # 预算截断不可作为完整快照发布

    def test_max_single_bytes_excludes_large(self):
        self._file("big.txt", b"y" * 5000)
        self._file("small.txt", b"z")
        limits = ProjectScanLimits(max_single_bytes=1000, max_total_bytes=2**30)
        r = self._scan(limits=limits, git_commit="x")
        paths = [e.relative_path for e in r.entries]
        self.assertIn("small.txt", paths)
        self.assertNotIn("big.txt", paths)

    def test_max_total_bytes_truncates_not_publishable(self):
        for i in range(3):
            self._file(f"f{i}.txt", b"a" * 100)
        limits = ProjectScanLimits(max_total_bytes=150, max_single_bytes=10**6)
        r = self._scan(limits=limits, git_commit="x")
        self.assertTrue(any("总字节" in n for n in r.limits))
        self.assertFalse(r.is_success)

    def test_excluded_items_budget_bounded(self):
        self._file(".gitignore", b"*.tmp\n")
        for i in range(50):
            self._file(f"f{i}.tmp", b"x")
        limits = ProjectScanLimits(max_excluded_items=10)
        r = self._scan(limits=limits, git_commit="x")
        self.assertLessEqual(len(r.excluded), 10)
        self.assertTrue(any("收纳上限" in n for n in r.limits))
        entry_paths = [e.relative_path for e in r.entries]
        self.assertNotIn("f0.tmp", entry_paths)
        self.assertIn(".gitignore", entry_paths)

    def test_iteration_budget_bounded(self):
        for i in range(40):
            self._file(f"f{i}.txt", b"x")
        limits = ProjectScanLimits(max_iterations=5, max_excluded_items=10**6)
        r = self._scan(limits=limits, git_commit="x")
        self.assertTrue(any("遍历条目预算" in n for n in r.limits))
        self.assertLessEqual(len(r.entries), 5)
        self.assertFalse(r.is_success)

    def test_scan_deadline_requires_positive(self):
        limits = ProjectScanLimits(scan_timeout_ms=0)
        with self.assertRaises(ValueError):
            self._scan(limits=limits, git_commit="x")

    def test_ignore_reject_dirs_not_walked(self):
        # .git 整棵不递归进 ignore 读取：即便 .git/.gitignore 有规则也不读。
        self._file(".git/inner.txt", b"x")
        self._file("keep.txt", b"k")
        r = self._scan(git_commit="x")
        paths = [e.relative_path for e in r.entries]
        self.assertNotIn(".git/inner.txt", paths)  # 硬排除目录
        self.assertIn("keep.txt", paths)

    def test_cumulative_ignore_budget_fails_closed(self):
        # 两个 ignore 文件共 14B > 预算 10B：跨 sibling/目录累计，命中即 fail closed。
        self._file(".gitignore", "#aaaaa\n")
        self._file("sub/.gitignore", "#bbbbb\n")
        limits = ProjectScanLimits(max_ignore_bytes=10, max_ignore_lines=10**6)
        with self.assertRaises(UnsupportedIgnorePattern):
            IgnoreMatcher.load(
                self.root, limits.max_ignore_bytes, limits.max_ignore_lines,
                reject_dirs=frozenset({d.lower() for d in {
                    ".git", "node_modules", ".venv", "venv", "__pycache__",
                    ".cache", ".next", "dist", "build", "target"}}),
            )
        with self.assertRaises(ProjectScanError):
            self._scan(limits=limits, git_commit="x")


class HardlinkTests(ScannerTestCase):
    def test_hardlink_nlink_rejected_every_duplicate(self):
        # reject_hardlinks：任何 nlink>1 的文件（指向根内/根外他处的硬链）都被拒，
        # 不只拒“第二个同 inode”。first.txt 与 second_hard.txt 共享 inode(nlink=2)，
        # second.txt 是独立文件(nlink=1)。
        self._file("first.txt", b"same")
        (self.root / "second.txt").write_bytes(b"same")
        try:
            os.link(os.fspath(self.root / "first.txt"), os.fspath(self.root / "second_hard.txt"))
        except OSError:
            self.skipTest("无法创建硬链接")
        r = self._scan(git_commit="x", reject_hardlinks=True)
        paths = [e.relative_path for e in r.entries]
        self.assertEqual(paths, ["second.txt"])  # 只有独立 nlink=1 的文件入选
        self.assertNotIn("first.txt", paths)
        self.assertNotIn("second_hard.txt", paths)
        reasons = " | ".join(r.excluded)
        self.assertIn("hardlink", reasons)
        self.assertTrue(r.is_success)  # 排除不是截断，仍可发布（不含硬链的）快照
        # 硬链内容绝不落盘：store 里只应有一个 blob（second.txt 的），绝无硬链两份。
        blobs = [p for p in (self.tmp / "store").iterdir() if not p.name.endswith(".partial")]
        self.assertEqual(len(blobs), 1)

    def test_root_external_hardlink_rejected(self):
        # 授权根内只有一个指向“根外合成文件”的硬链：nlink>1 也须被拒。
        external = self.tmp / "external.txt"
        external.write_bytes(b"external bytes")
        try:
            os.link(os.fspath(external), os.fspath(self.root / "linked.txt"))
        except OSError:
            self.skipTest("无法创建硬链接")
        r = self._scan(git_commit="x", reject_hardlinks=True)
        paths = [e.relative_path for e in r.entries]
        self.assertNotIn("linked.txt", paths)
        self.assertTrue(any("hardlink" in x for x in r.excluded))
        # 关键回归：根外硬链内容**绝不落盘**——store 里不得有任何 blob。
        blobs = [p for p in (self.tmp / "store").iterdir() if not p.name.endswith(".partial")]
        self.assertEqual(blobs, [])


class FailureTests(ScannerTestCase):
    def test_missing_root_raises(self):
        with self.assertRaises(ProjectScanError):
            scan_project(self.tmp / "nope", self.store, git_commit="x")

    def test_non_dir_root_raises(self):
        f = self._file("a.txt")
        with self.assertRaises(ProjectScanError):
            scan_project(f, self.store, git_commit="x")

    def test_git_unknown_is_metadata_not_failure(self):
        # 未提供 git commit 只是元数据未知；普通非 Git 目录仍为成功快照。
        r = self._scan()
        self.assertTrue(any("git" in u for u in r.unknown_reason))
        self.assertTrue(r.is_success)


if __name__ == "__main__":
    unittest.main()
