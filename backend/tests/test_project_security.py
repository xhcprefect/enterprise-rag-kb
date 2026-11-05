"""P 安全专项：ignore 两缺陷、默认硬链接、Windows 授权根/reparse 逃逸。

仅合成临时目录；无 DB、无 dotenv、不索引真实项目。真实 junction/symlink 可创建时
实测，权限不支持则明确 skip（绝不把 skip 当 pass）。所有临时目录 addCleanup 收尾。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.project_blobs import BlobStore, HardlinkRejected
from app.services.project_ignore import IgnoreMatcher, UnsupportedIgnorePattern
from app.services.project_paths import (
    ProjectPathError,
    ProjectPathExcludedLink,
    ProjectPathMissing,
    RootAnchor,
    make_root_binding,
    open_secure_regular,
    secure_source_root,
)
from app.services.project_scanner import ProjectScanError, scan_project
from app.services.project_types import ProjectScanLimits

IS_WINDOWS = os.name == "nt"


def _mk_clean(prefix="sec"):
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    return tmp


class _ScandirCounter:
    """包装 ``os.scandir`` 并统计 ``__next__`` 次数，用于证明“宽目录未被整列”。"""

    def __init__(self, real_scandir):
        self.real_scandir = real_scandir
        self.next_calls = 0

    def __call__(self, path):
        return _CountingIterator(self.real_scandir(path), self)


class _CountingIterator:
    def __init__(self, base, counter):
        self._base = base
        self._counter = counter

    def __iter__(self):
        return self

    def __next__(self):
        self._counter.next_calls += 1
        return next(self._base)

    def close(self):
        self._base.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _reject_dirs():
    return frozenset({d.lower() for d in {
        ".git", "node_modules", ".venv", "venv", "__pycache__",
        ".cache", ".next", "dist", "build", "target"}})


class IgnoreLoadFailsClosedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _mk_clean()
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = self.tmp / "proj"
        self.root.mkdir()

    def _load(self, **kw):
        defaults = dict(
            max_ignore_bytes=10**6, max_ignore_lines=10**6,
            reject_dirs=_reject_dirs(),
        )
        defaults.update(kw)
        return IgnoreMatcher.load(self.root, defaults["max_ignore_bytes"],
                                  defaults["max_ignore_lines"],
                                  reject_dirs=defaults["reject_dirs"])

    def test_missing_ignore_is_benign_skip(self):
        # 正常“无忽略文件”-> 空规则、不抛错（回归：末级可证明缺失=跳过）。
        m = self._load()
        self.assertEqual(m.rules, [])
        self.assertFalse(m.is_excluded("a.py", False))

    def test_real_ignore_file_parses_and_same_fd_verified(self):
        (self.root / ".gitignore").write_text("private.py\n", encoding="utf-8")
        m = self._load()
        self.assertTrue(m.is_excluded("private.py", False))
        self.assertFalse(m.is_excluded("public.py", False))

    def test_leaf_symlink_ignore_is_safe_skip(self):
        # 末级 .gitignore 是指向根外的符号链接：安全打开原子拒绝跟随，
        # 归类为“安全排除链接”，load 不读取链接外内容、也不 fail closed。
        external = self.tmp / "outside-ignore"
        external.write_text("*.py\n", encoding="utf-8")
        (self.root / "a.py").write_text("x", encoding="utf-8")
        (self.root / "b.txt").write_text("x", encoding="utf-8")
        link = self.root / ".gitignore"
        try:
            link.symlink_to(external)
        except OSError:
            self.skipTest("无法创建符号链接")
        m = self._load()
        # 没有读取链接外的“*.py”规则 => a.py 不被忽略（链接忽略文件安全跳过）。
        self.assertFalse(m.is_excluded("a.py", False))
        self.assertEqual(m.rules, [])

    def test_ignore_load_unknown_error_fails_closed_not_continue(self):
        # 主代理反例：把 .gitignore 的 open 抛成“PermissionError 包装的
        # ProjectPathError”（非可证明缺失/链接排除）。修复前 blanket
        # except/continue 会吞掉、不收应忽略规则；现在必须整次 load fail closed。
        (self.root / ".gitignore").write_text("private.py\n", encoding="utf-8")

        def boom(root, comps):
            try:
                raise PermissionError(13, "模拟权限拒绝")
            except PermissionError as e:
                raise ProjectPathError("权限失败（非末级缺失）") from e

        with mock.patch(
            "app.services.project_ignore.open_secure_regular", side_effect=boom
        ):
            with self.assertRaises(UnsupportedIgnorePattern):
                self._load()

    def test_ignore_load_ancestor_change_fails_closed(self):
        # 祖先变化/未知路径安全错误（非末级缺失/链接排除）必须是普通
        # ProjectPathError，load 必须 fail closed，绝不 continue。
        # （scan 层递归已预先把符号链接子目录隔开，故这里直接验证分类边界：
        #   open 抛“祖先变化”型错误 -> 整次 load fail closed。）
        (self.root / ".gitignore").write_text("private.py\n", encoding="utf-8")

        def ancestor_changed(root, comps):
            raise ProjectPathError("祖先组件被换/不可用（非末级）")

        with mock.patch(
            "app.services.project_ignore.open_secure_regular",
            side_effect=ancestor_changed,
        ):
            with self.assertRaises(UnsupportedIgnorePattern):
                self._load()

    def test_ignore_same_fd_change_fails_closed(self):
        # 主代理反例 2：stat_after 之前从未被调用、load 仍接受。现在必须调用
        # 同 FD 身份核验；读取期间身份变化 -> 整次 load fail closed。
        (self.root / ".gitignore").write_text("private.py\n", encoding="utf-8")
        from app.services.project_paths import OpenedFile, OpenIdentity

        def changed_stat_after(self_opened):
            before = self_opened.identity_before
            # 返回一个只有 mtime 偏移、其余同身份的 OpenIdentity -> matches() 为假。
            return OpenIdentity(
                path=before.path, st_dev=before.st_dev, st_ino=before.st_ino,
                st_nlink=before.st_nlink, st_size=before.st_size,
                st_mtime_ns=before.st_mtime_ns + 1,
            )

        with mock.patch.object(OpenedFile, "stat_after", changed_stat_after):
            with self.assertRaises(UnsupportedIgnorePattern):
                self._load()


class DefaultHardlinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _mk_clean("hardlink")
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = self.tmp / "proj"
        self.root.mkdir()
        self.store = BlobStore(self.tmp / "store")

    def test_scan_default_rejects_hardlink(self):
        # 安全默认（不传 reject_hardlinks）：nlink>1 文件被拒、内容不入 blob。
        (self.root / "a.txt").write_bytes(b"shared")
        (self.root / "b.txt").write_bytes(b"independent")
        try:
            os.link(os.fspath(self.root / "a.txt"),
                    os.fspath(self.root / "a_hard.txt"))
        except OSError:
            self.skipTest("无法创建硬链接")
        r = scan_project(self.root, self.store, git_commit="x")
        paths = [e.relative_path for e in r.entries]
        # 默认即拒硬链：独立的 b.txt 入选，硬链对都不入选。
        self.assertNotIn("a.txt", paths)
        self.assertNotIn("a_hard.txt", paths)
        self.assertIn("b.txt", paths)
        blobs = [p for p in (self.tmp / "store").iterdir()
                 if not p.name.endswith(".partial")]
        # 硬链内容绝不落盘：只有 b.txt 的独立 blob。
        self.assertEqual(len(blobs), 1)
        self.assertFalse(r.truncated)
        self.assertTrue(r.is_success)

    def test_ingest_default_rejects_hardlink(self):
        (self.root / "a.txt").write_bytes(b"shared")
        (self.root / "b.txt").write_bytes(b"other")
        try:
            os.link(os.fspath(self.root / "a.txt"),
                    os.fspath(self.root / "a2.txt"))
        except OSError:
            self.skipTest("无法创建硬链接")
        # ingest 默认（True）在同一核验 FD、读取/发布前即拒。
        with self.assertRaises(HardlinkRejected):
            self.store.ingest_verified_file(self.root, ("a.txt",), 10**6)
        # 显式 False 仍兼容旧方案。
        ref, _ = self.store.ingest_verified_file(self.root, ("b.txt",), 10**6,
                                                 reject_hardlinks=False)
        self.assertEqual(ref.size, len(b"other"))
        # 硬链内容绝不落盘：store 里只有 b.txt 的 blob。
        blobs = [p for p in (self.tmp / "store").iterdir()
                 if not p.name.endswith(".partial")]
        self.assertEqual(len(blobs), 1)


class BudgetStreamingTests(unittest.TestCase):
    """宽目录的条目预算必须**逐条**生效，不能先整列 500 再报超限（协调者点 3）。

    ``max_entries=2`` 时底层 scandir 只能消费极少数条目（Path 与 anchor 两支），
    随后以 ``UnsupportedIgnorePattern`` 早拒——证明枚举是受控流式。
    """

    def setUp(self):
        self.tmp = _mk_clean("budget")
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        wide = self.proj / "wide"
        wide.mkdir()
        for i in range(500):
            (wide / f"f{i:03d}.txt").write_text("x", encoding="utf-8")

    def _load(self, anchor=None, max_entries=2):
        return IgnoreMatcher.load(
            self.proj, 10**6, 10**6, max_entries=max_entries,
            reject_dirs=_reject_dirs(), anchor=anchor,
        )

    def test_path_branch_budget_early_rejects(self):
        # Path 模式：宽目录在 ≤ 少量条目时即超预算 fail closed，绝不消费全部 500。
        counter = _ScandirCounter(os.scandir)
        with mock.patch("os.scandir", side_effect=counter):
            with self.assertRaises(UnsupportedIgnorePattern):
                self._load()
        self.assertLessEqual(counter.next_calls, 5)

    @unittest.skipUnless(IS_WINDOWS, "Windows 锚定分支预算早拒")
    def test_anchor_branch_budget_early_rejects(self):
        counter = _ScandirCounter(os.scandir)
        with secure_source_root(self.proj, ()) as anchor:
            with mock.patch("os.scandir", side_effect=counter):
                with self.assertRaises(UnsupportedIgnorePattern):
                    self._load(anchor=anchor)
        self.assertLessEqual(counter.next_calls, 5)

    def test_high_budget_sees_all_entries_control(self):
        # 对照组：预算充足时能枚举到所有 500 条（证明宽目录确有 500 条）。
        m = self._load(max_entries=10**6)
        self.assertEqual(len(m.rules), 0)  # 无规则文件；仅证明遍历未因预算误拒
        # 直接经 anchor.children 也观得全量。
        with secure_source_root(self.proj, ()) as anchor:
            names = [n for (n, _d, _f, _l, _s) in anchor.children(("wide",))]
        self.assertEqual(len(names), 500)


class TypedOpenClassificationTests(unittest.TestCase):
    """open_secure_regular 的末级/祖先分类（不匹配报错文案，用类型判定）。"""

    def setUp(self):
        self.tmp = _mk_clean("cls")
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = self.tmp / "proj"
        self.root.mkdir()

    def test_missing_leaf_is_typed_missing(self):
        with self.assertRaises(ProjectPathMissing):
            open_secure_regular(self.root, ("missing.txt",))

    def test_symlink_leaf_is_typed_excluded_link(self):
        (self.root / "real.txt").write_bytes(b"x")
        link = self.root / "evil.txt"
        try:
            link.symlink_to(self.root / "real.txt")
        except OSError:
            self.skipTest("无法创建符号链接")
        with self.assertRaises(ProjectPathExcludedLink):
            open_secure_regular(self.root, ("evil.txt",))

    def test_missing_ancestor_is_generic_fail_closed(self):
        # 祖先缺失不是末级缺失 -> 普通 ProjectPathError（fail closed），
        # 不是 Missing，绝不在 ignore 层据此 continue。
        with self.assertRaises(ProjectPathError) as cm:
            open_secure_regular(self.root, ("nope", "x.txt"))
        self.assertNotIsInstance(cm.exception, ProjectPathMissing)
        self.assertNotIsInstance(cm.exception, ProjectPathExcludedLink)


def _make_junction(junction_link: Path, target: Path) -> bool:
    """Windows: 真实创建 junction（mklink /J，不需要管理员）。返回 False=无法创建。"""
    r = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction_link), str(target)],
        capture_output=True,
    )
    return r.returncode == 0


@unittest.skipUnless(IS_WINDOWS, "junction 真实目录测试仅适用于 Windows")
class WindowsAnchorEscapeTests(unittest.TestCase):
    """Windows 授权根真实 junction 逃逸：根/祖先/末级 junction 必须拒。

    真实 mklink /J 创建；权限不支持则 skip，绝不把 skip 当 pass。清理 junction
    用 os.rmdir（不跟随），绝不 follow 删除根外 target。
    """

    def setUp(self):
        self.tmp = _mk_clean("junc")
        self.authorized = self.tmp / "authorized"
        self.authorized.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.addCleanup(self._cleanup_links)
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def _cleanup_links(self):
        for link in list(self.tmp.rglob("linked")) + [self.tmp / "linked"]:
            try:
                # 只删 junction 链接本身（os.rmdir 不 follow），绝不删 target。
                if link.exists() and (link / ".").is_dir():
                    os.rmdir(link)
            except Exception:
                pass

    def test_make_root_binding_rejects_junction_root(self):
        _linked = self.authorized / "linked"
        if not _make_junction(_linked, self.outside):
            self.skipTest("无法创建 junction")
        # Path.is_symlink() 在 junction 上为 False（目录+S_IFLNK 判错），主代理
        # 正是漏了这里；现在必须以 reparse 检测拒绝。
        self.assertFalse(_linked.is_symlink())
        with self.assertRaises(ProjectPathError):
            make_root_binding("k", _linked)

    def test_secure_source_root_rejects_junction_component(self):
        # 授权根真实，但相对子目录的祖先组件是 junction（linked/sub 会经 junction
        # 解析到根外）。secure_source_root 必须 fail closed。
        _linked = self.authorized / "linked"
        if not _make_junction(_linked, self.outside):
            self.skipTest("无法创建 junction")
        (self.outside / "sub").mkdir()  # 让 authorized/linked/sub 可解析
        with self.assertRaises(ProjectPathError):
            secure_source_root(self.authorized, ("linked", "sub"))

    def test_secure_source_root_rejects_junction_as_source_root(self):
        # 相对子目录本身就是 junction：源根直接落到根外，必须拒绝。
        _linked = self.authorized / "linked"
        if not _make_junction(_linked, self.outside):
            self.skipTest("无法创建 junction")
        with self.assertRaises(ProjectPathError):
            secure_source_root(self.authorized, ("linked",))

    def test_anchor_children_positive_control(self):
        # 普通根/子目录枚举的正控（作为后换 junction 拒绝的对照组）：不报错、正确列出。
        (self.authorized / "sub").mkdir()
        (self.authorized / "sub" / "f.txt").write_bytes(b"x")
        with secure_source_root(self.authorized, ()) as anchor:
            top = [n for (n, _d, _f, _l, _s) in anchor.children(())]
            self.assertIn("sub", top)
            sub = [n for (n, _d, _f, _l, _s) in anchor.children(("sub",))]
            self.assertIn("f.txt", sub)

    def test_children_rejects_post_swap_junction(self):
        # 锚被持有期间，根下普通子目录被换成 junction -> children 必须拒绝（不返回
        # outside 字节/条目）。（协调者点 3 真 junction：_children_windows 逐级核验
        # rel_dir 祖先为非 reparse 并持有句柄，中途换 junction 也 fail closed。）
        child = self.authorized / "child"
        child.mkdir()
        (self.outside / "sample.py").write_bytes(b"OUTSIDE-SYNTHETIC")
        anchor = secure_source_root(self.authorized, ())
        # 正控：普通子目录可枚举。
        top0 = [n for (n, _d, _f, _l, _s) in anchor.children(())]
        self.assertIn("child", top0)
        os.rmdir(child)  # 移除普通空目录（不 follow）
        if not _make_junction(child, self.outside):
            anchor.close()
            self.skipTest("无法创建 junction")
        with self.assertRaises(ProjectPathError):
            _ = list(anchor.children(("child",)))
        anchor.close()
        try:
            os.rmdir(child)  # 清理 junction 本身
        except OSError:
            pass

    def test_scan_via_anchor_does_not_read_outside_bytes(self):
        # 授权根下真实子目录 sub 里有 junction linked -> outside；outside 里有
        # sample.py（OUTSIDE-BYTES）。经锚定 scan_project 只能收录树内，junction
        # 作为 reparse 被排除，outside 字节绝不进 blob。
        (self.authorized / "sub").mkdir()
        (self.authorized / "sub" / "real.py").write_bytes(b"INSIDE-BYTES")
        _linked = self.authorized / "sub" / "linked"
        if not _make_junction(_linked, self.outside):
            self.skipTest("无法创建 junction")
        out_sub = self.outside / "sub"
        out_sub.mkdir()
        (out_sub / "sample.py").write_bytes(b"OUTSIDE-BYTES")
        store = BlobStore(self.tmp / "store")
        with secure_source_root(self.authorized, ("sub",)) as anchor:
            report = scan_project(anchor, store, git_commit="x")
        paths = [e.relative_path for e in report.entries]
        self.assertIn("real.py", paths)
        self.assertNotIn("linked", paths)
        self.assertNotIn("linked/sample.py", paths)
        self.assertTrue(any("reparse" in x for x in report.excluded))
        # outside 字节绝不落盘。
        all_bytes = b"".join(
            p.read_bytes() for p in (self.tmp / "store").iterdir()
            if not p.name.endswith(".partial")
        )
        self.assertNotIn(b"OUTSIDE-BYTES", all_bytes)
        self.assertIn(b"INSIDE-BYTES", all_bytes)

    def test_anchor_open_file_rejects_junction_descendant(self):
        # 经锚定的源根，open 其下 junction 组件必须 fail closed（不读 outside）。
        _linked = self.authorized / "linked"
        if not _make_junction(_linked, self.outside):
            self.skipTest("无法创建 junction")
        (self.outside / "sample.py").write_bytes(b"OUTSIDE-BYTES")
        with secure_source_root(self.authorized, ()) as anchor:
            with self.assertRaises(ProjectPathError):
                anchor.open_file(("linked", "sample.py"))


class AnchorContractTests(unittest.TestCase):
    """RootAnchor / secure_source_root 通用契约（两平台；POSIX 用 symlink 等价）。"""

    def setUp(self):
        self.tmp = _mk_clean("anchor")
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.authorized = self.tmp / "authorized"
        self.authorized.mkdir()

    def test_anchor_accepts_real_subdir_and_opens(self):
        (self.authorized / "sub").mkdir()
        (self.authorized / "sub" / "f.txt").write_bytes(b"ok")
        with secure_source_root(self.authorized, ("sub",)) as anchor:
            self.assertIsInstance(anchor, RootAnchor)
            self.assertEqual(anchor.source_root_path, self.authorized / "sub")
            op = anchor.open_file(("f.txt",))
            data = os.fdopen(op.fd, "rb", closefd=False).read()
            self.assertEqual(data, b"ok")
            self.assertTrue(op.identity_before.matches(op.stat_after()))
            op.close()

    def test_anchor_identity_and_fingerprint_stable(self):
        (self.authorized / "sub").mkdir()
        with secure_source_root(self.authorized, ("sub",)) as anchor:
            fp1 = anchor.root_fingerprint()
            fp2 = anchor.root_fingerprint()
        self.assertEqual(fp1, fp2)

    def test_scan_project_accepts_anchor_and_manifests(self):
        (self.authorized / "a.py").write_bytes(b"def a():\n pass")
        (self.authorized / "b.py").write_bytes(b"def b():\n pass")
        store = BlobStore(self.tmp / "store")
        with secure_source_root(self.authorized, ()) as anchor:
            report = scan_project(anchor, store, git_commit="x")
        paths = [e.relative_path for e in report.entries]
        self.assertEqual(sorted(paths), ["a.py", "b.py"])
        self.assertTrue(report.is_success)

    def test_anchor_close_frees_and_reopens(self):
        (self.authorized / "sub").mkdir()
        (self.authorized / "sub" / "f.txt").write_bytes(b"x")
        anchor = secure_source_root(self.authorized, ("sub",))
        anchor.close()
        # close 后再 open（重新锚定不同实例）不受影响。
        with secure_source_root(self.authorized, ("sub",)) as a2:
            op = a2.open_file(("f.txt",))
            self.assertEqual(os.fdopen(op.fd, "rb", closefd=False).read(), b"x")
            op.close()

    @unittest.skipUnless(not IS_WINDOWS, "POSIX 用目录符号链接等价验证")
    def test_posix_anchor_rejects_symlink_component(self):
        real = self.tmp / "real"
        real.mkdir()
        try:
            (self.authorized / "sub").symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("无法创建符号链接")
        with self.assertRaises(ProjectPathError):
            secure_source_root(self.authorized, ("sub",))


class AnchorLifecycleTests(unittest.TestCase):
    """close 后任何成员必须抛 ProjectPathError（绝不复活）；close 幂等。"""

    def setUp(self):
        self.tmp = _mk_clean("life")
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.authorized = self.tmp / "authorized"
        self.authorized.mkdir()
        (self.authorized / "a.py").write_bytes(b"def a():\n pass")

    def test_closed_anchor_all_members_raise(self):
        anchor = secure_source_root(self.authorized, ())
        anchor.close()
        for member, args in [
            (anchor.open_file, (("a.py",),)),
            (anchor.children, ((),)),
            (anchor.root_fingerprint, ()),
        ]:
            with self.assertRaises(ProjectPathError, msg=member.__name__):
                member(*args)
        # __enter__（with 语句）在 closed 上也须抛，不静默放行。
        with self.assertRaises(ProjectPathError):
            anchor.__enter__()

    def test_close_is_idempotent(self):
        anchor = secure_source_root(self.authorized, ())
        anchor.close()
        anchor.close()  # 第二次调用无副作用、不抛
        anchor.close()

    def test_anchor_ignore_goes_through_pinned_tree(self):
        # 源根 .gitignore 的规则必须经锚定上下文生效（协调者点 4：ignore 不能丢 anchor
        # 按未 pin 路径重走）。private.py 被 ignore 排除、public.py 入选。
        (self.authorized / ".gitignore").write_text("private.py\n", encoding="utf-8")
        (self.authorized / "private.py").write_text("secret", encoding="utf-8")
        (self.authorized / "public.py").write_text("def p():\n pass", encoding="utf-8")
        store = BlobStore(self.tmp / "store")
        with secure_source_root(self.authorized, ()) as anchor:
            report = scan_project(anchor, store, git_commit="x")
        paths = [e.relative_path for e in report.entries]
        self.assertIn("public.py", paths)
        self.assertNotIn("private.py", paths)
        self.assertTrue(any("ignored" in x for x in report.excluded))
        self.assertTrue(report.is_success)

    def test_raw_invalid_components_rejected(self):
        (self.authorized / "sub").mkdir()
        (self.authorized / "sub" / "f.txt").write_bytes(b"ok")
        with secure_source_root(self.authorized, ("sub",)) as anchor:
            for comps in [("..", "f.txt"), ("/abs", "f.txt"), ("", "f.txt"), ()]:
                with self.assertRaises(ProjectPathError, msg=f"{comps!r}"):
                    anchor.open_file(comps)
            # children 空元组合法（枚举源根本身），但非法组件仍拒。
            self.assertTrue(anchor.children(()))
            with self.assertRaises(ProjectPathError):
                anchor.children(("..",))

    def test_fingerprint_stable_across_two_anchors(self):
        fp1 = secure_source_root(self.authorized, ()).root_fingerprint()
        fp2 = secure_source_root(self.authorized, ()).root_fingerprint()
        self.assertEqual(fp1, fp2)

    def test_fingerprint_changes_on_swap_tree(self):
        # 换树（rename 旧 -> 同声明路径重建）必须换指纹（协调者点 2：指纹来自被 pin
        # 句柄的真实对象身份，不是路径字符串）。Windows 用卷序+文件索引，POSIX 用
        # dev/ino，两平台重建目录后对象身份都不同。
        (self.authorized / "a.py").write_bytes(b"v1")
        anchor = secure_source_root(self.authorized, ())
        fp_before = anchor.root_fingerprint()
        anchor.close()
        old = self.tmp / "authorized_old"
        os.rename(str(self.authorized), str(old))
        self.authorized.mkdir()
        (self.authorized / "a.py").write_bytes(b"v2")
        anchor2 = secure_source_root(self.authorized, ())
        fp_after = anchor2.root_fingerprint()
        anchor2.close()
        self.assertNotEqual(fp_before, fp_after)


@unittest.skipUnless(IS_WINDOWS, "junction 真实目录测试仅适用于 Windows")
class WindowsJunctionAncestorTests(unittest.TestCase):
    """真实 Windows junction 作为**授权根自己的祖先**（协调者点 3）。

    不是只作为 relative_dir 组件，而是 ``authorized_root = junction_link\\authorized``
    本身挂在一个 junction 祖先之下——无论 secure_source_root 还是 make_root_binding
    都必须拒，否则会把外部树升为可信根。可创建则实测，权限不支持则明确 skip。
    """

    def setUp(self):
        self.tmp = _mk_clean("janc")
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def test_anchor_rejects_junction_as_root_ancestor(self):
        target = self.tmp / "target"
        target.mkdir()
        (target / "authorized").mkdir()
        (target / "authorized" / "a.py").write_bytes(b"x")
        link = self.tmp / "link"
        if not _make_junction(link, target):
            self.skipTest("无法创建 junction")
        self.addCleanup(os.rmdir, link)  # 清理 junction 本身，不 follow
        auth = link / "authorized"
        self.assertTrue(auth.exists())
        with self.assertRaises(ProjectPathError):
            secure_source_root(auth, ())

    def test_make_root_binding_rejects_junction_as_root_ancestor(self):
        target = self.tmp / "target"
        target.mkdir()
        (target / "authorized").mkdir()
        link = self.tmp / "link"
        if not _make_junction(link, target):
            self.skipTest("无法创建 junction")
        self.addCleanup(os.rmdir, link)
        auth = link / "authorized"
        self.assertTrue(auth.exists())
        with self.assertRaises(ProjectPathError):
            make_root_binding("k", auth)


if __name__ == "__main__":
    unittest.main()
