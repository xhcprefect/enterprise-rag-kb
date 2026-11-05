"""project_paths 纯路径安全测试：无 DB / 无 dotenv / 仅合成临时目录。

覆盖：Windows 非法名、规避符号链接/reparse、根身份指纹、特殊文件分类。
"""
import os
import tempfile
import unittest
from pathlib import Path

from app.services.project_paths import (
    ProjectPathError,
    RootBinding,
    classify_special,
    make_root_binding,
    open_secure_regular,
    root_fingerprint,
    split_relative_path,
)

IS_WINDOWS = os.name == "nt"


class SplitRelativePathTests(unittest.TestCase):
    def test_valid_relative_paths(self):
        self.assertEqual(split_relative_path("a/b/c.txt"), ("a", "b", "c.txt"))
        self.assertEqual(split_relative_path("a\\b\\c.txt"), ("a", "b", "c.txt"))
        self.assertEqual(split_relative_path("single"), ("single",))

    def test_rejects_absolute_drive_unc(self):
        for bad in ("/abs", "\\\\server\\share", "C:/x", "C:\\x", "/"):
            with self.assertRaises(ProjectPathError):
                split_relative_path(bad)

    def test_rejects_parent_traversal(self):
        for bad in ("..", "../x", "a/../../x", "a/..", "a/../.."):
            with self.assertRaises(ProjectPathError):
                split_relative_path(bad)

    def test_rejects_empty(self):
        for bad in ("", ".", "./", "\\", "a//b"):
            with self.assertRaises(ProjectPathError):
                split_relative_path(bad)

    @unittest.skipUnless(IS_WINDOWS, "Windows 设备名/保留规则")
    def test_windows_device_names(self):
        for bad in ("NUL", "NUL.txt", "CON", "PRN", "AUX", "com1", "lpt9", "COM2.log"):
            with self.assertRaises(ProjectPathError):
                split_relative_path(bad)

    @unittest.skipUnless(IS_WINDOWS, "Windows ADS / 尾点尾空格 / 保留字符 / 控制字符")
    def test_windows_ads_reserved_control(self):
        for bad in (
            "file.txt:stream",           # ADS
            "trailing.",                 # 尾点
            "trailing ",                 # 尾空格
            "a/b<.x", "a/b>.x", "a/b?.x",  # 保留字符
            "a/b*.x", 'a/b".x', "a/b|.x",
            "a\nb.py", "a\x00b.py",      # 控制字符
        ):
            with self.assertRaises(ProjectPathError):
                split_relative_path(bad)


class RootBindingTests(unittest.TestCase):
    def _cmp(self):
        tmp = Path(tempfile.mkdtemp(prefix="paths"))
        return tmp

    def test_binding_requires_dir(self):
        tmp = self._cmp()
        missing = tmp / "nope"
        with self.assertRaises(ProjectPathError):
            make_root_binding("k", missing)
        file = tmp / "f.txt"
        file.write_text("x")
        with self.assertRaises(ProjectPathError):
            make_root_binding("k", file)

    def test_fingerprint_changes_when_tree_moves(self):
        tmp = Path(tempfile.mkdtemp(prefix="paths"))
        a = tmp / "a"
        b = tmp / "b"
        a.mkdir()
        b.mkdir()
        self.assertNotEqual(root_fingerprint(a), root_fingerprint(b))

    def test_same_root_stable_fingerprint(self):
        tmp = Path(tempfile.mkdtemp(prefix="paths"))
        self.assertEqual(root_fingerprint(tmp), root_fingerprint(tmp))

    def test_binding_rejects_symlink_root(self):
        tmp = Path(tempfile.mkdtemp(prefix="paths"))
        real = tmp / "real"
        real.mkdir()
        link = tmp / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("无法创建符号链接")
        with self.assertRaises(ProjectPathError):
            make_root_binding("k", link)


class SecureOpenTests(unittest.TestCase):
    def _root_with_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="secopen"))
        root = tmp / "root"
        root.mkdir()
        sub = root / "sub"
        sub.mkdir()
        f = sub / "a.txt"
        f.write_text("hello secure", encoding="utf-8")
        return root, sub, f

    def test_open_regular_and_identity_matches(self):
        root, _, f = self._root_with_file()
        opened = open_secure_regular(root, ("sub", "a.txt"))
        try:
            import os
            with os.fdopen(opened.fd, "rb", closefd=False) as fh:
                data = fh.read()
            self.assertEqual(data, b"hello secure")
            self.assertTrue(opened.identity_before.matches(opened.stat_after()))
        finally:
            opened.close()

    def test_rejects_symlink_file(self):
        root, sub, f = self._root_with_file()
        link = root / "evil.txt"
        try:
            link.symlink_to(f)
        except OSError:
            self.skipTest("无法创建符号链接")
        with self.assertRaises(ProjectPathError):
            open_secure_regular(root, ("evil.txt",))
        with self.assertRaises(ProjectPathError):
            # 中间目录是符号链接指向外部也必须拒绝
            ext = root.parent / "external"
            ext.mkdir()
            (ext / "x.txt").write_text("x")
            try:
                dlink = root / "dsub"
                dlink.symlink_to(ext, target_is_directory=True)
            except OSError:
                raise
            open_secure_regular(root, ("dsub", "x.txt"))

    def test_rejects_missing_and_non_regular(self):
        root, sub, f = self._root_with_file()
        with self.assertRaises(ProjectPathError):
            open_secure_regular(root, ("nope", "x.txt"))
        with self.assertRaises(ProjectPathError):
            open_secure_regular(root, ("sub", "a.txt", "extra"))


class ClassifySpecialTests(unittest.TestCase):
    def test_regular_and_dir_are_none(self):
        tmp = Path(tempfile.mkdtemp(prefix="cls"))
        self.assertIsNone(classify_special(os.stat(tmp).st_mode))
        f = tmp / "f"
        f.write_text("x")
        self.assertIsNone(classify_special(os.stat(f).st_mode))


if __name__ == "__main__":
    unittest.main()
