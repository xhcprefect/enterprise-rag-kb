"""有界 .gitignore/项目忽略规则（纯实现，无 pathspec 依赖）。

pathspec 未安装（主代理决定不新增依赖），故这里原生实现一个**有界且语义明确**
的子集。服从计划约束：
- 负规则（``!`` 重新包含）**必须被支持**，绝不悄悄漏掉；无法可靠解释的
  规则一律 fail closed（抛 ``UnsupportedIgnorePattern``），不默默当作没这条规则。
- 字符类 ``[...]``、``?``、``*``、``**``、前导 ``/``（锚定）、尾随 ``/``（仅目录）、
  ``#`` 注释与空行均明确支持。
- 本实现只做静态判定；扫描时对"祖先目录已被排除"同样生效。

不支持（会抛错、fail closed）：反斜杠转义（含转义尾随空格）、非斜杠分隔的
反斜杠路径语义。这些在真实 .gitignore 里罕见但语义特殊，保守拒绝而非误判。
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.services.project_paths import (
    ProjectPathError,
    ProjectPathExcludedLink,
    ProjectPathMissing,
    RootAnchor,
    open_secure_regular,
)

MAX_PATH = 4096


class UnsupportedIgnorePattern(ValueError):
    """遇到无法可靠解释的忽略规则：拒绝整组规则以 fail closed。"""


@dataclass(frozen=True)
class IgnoreRule:
    raw: str
    negated: bool
    dir_only: bool
    anchored: bool  # 锚定到 base 目录（含前导 / 或内部 /）
    base: tuple[str, ...]  # 该规则所属 .gitignore 所在目录（根相对）
    segments: tuple[str, ...]  # 去前后 / 的段（已处理 **）


def _parse_line(line: str) -> IgnoreRule | None:
    """解析一行忽略规则；注释/空行返回 None；无法解释则抛错。"""
    text = line.rstrip("\r\n")
    if not text:
        return None
    if text.startswith("#"):
        return None
    if "\\" in text:
        raise UnsupportedIgnorePattern(f"不支持反斜杠转义: {line.strip()!r}")
    negated = False
    if text.startswith("!"):
        negated = True
        text = text[1:]
    if not text:
        raise UnsupportedIgnorePattern(f"仅前缀 ! 无内容: {line.strip()!r}")
    # 尾随空格默认被忽略（非转义）；但我们已拒绝转义，故直接 rstrip 空格。
    # 若尾随空格后还有反斜杠属于转义，已被上面的 "\\" 判断拒绝。
    text = text.rstrip(" ")
    if not text:
        raise UnsupportedIgnorePattern(f"忽略规则为空: {line.strip()!r}")

    dir_only = text.endswith("/")
    if dir_only:
        text = text[:-1]
    if not text:
        # 形如 "/" 的仅目录规则，等价于排除根下所有目录——保守拒绝。
        raise UnsupportedIgnorePattern(f"空的仅目录规则: {line.strip()!r}")
    anchored = text.startswith("/")
    if anchored:
        text = text[1:]
    raw_segments = text.split("/")
    if not raw_segments or any(seg == "" for seg in raw_segments):
        # 含空段，形如 "a//b" 或前导/ 已去掉后的空首段——无法可靠解释则拒绝。
        raise UnsupportedIgnorePattern(f"路径段异常: {line.strip()!r}")
    # 非锚定但含内部 / 时按锚定处理（git 语义：有分隔符即相对 .gitignore 位置）。
    effective_anchored = anchored or len(raw_segments) > 1
    return IgnoreRule(
        raw=line.rstrip("\r\n"),
        negated=negated,
        dir_only=dir_only,
        anchored=effective_anchored,
        base=(),  # 由加载方填充
        segments=tuple(raw_segments),
    )


@dataclass(frozen=True)
class _Seg:
    pattern: str
    regex: re.Pattern


def _segment_regex(pattern: str) -> re.Pattern:
    """把单个路径段 glob 转成锚定的正则。

    只有**独立** ``**`` 段视为 globstar（由 ``_match_segments`` 特殊处理成跨段匹配，
    在此返回 None 占位、不会被真正编译/匹配）。含 ``**`` 但非独立的混合段（如
    ``a**b``、``**x``）git 语义特殊且不可靠地解释为普通 glob，保守 fail closed。
    """
    if pattern == "**":
        return None
    if "**" in pattern:
        raise UnsupportedIgnorePattern(f"段内 ** 需独立成段才可解释: {pattern!r}")
    try:
        translated = fnmatch.translate(pattern)
    except re.error as err:
        raise UnsupportedIgnorePattern(f"字符类解析失败: {pattern!r}") from err
    return re.compile(translated)


def _compile_segments(segments: tuple[str, ...]) -> tuple:
    return tuple(_Seg(p, _segment_regex(p)) for p in segments)


def _match_segment(seg: _Seg, component: str) -> bool:
    return seg.regex.fullmatch(component) is not None


def _match_segments(segments: tuple, path: tuple[str, ...]) -> bool:
    """`**` 感知的递归匹配：patterns 是否完整匹配 path 段序列。"""
    segs = list(segments)
    pts = list(path)

    def rec(pi: int, ti: int) -> bool:
        if pi == len(segs):
            return ti == len(pts)
        seg = segs[pi]
        if seg.pattern == "**":
            # 匹配 0..n 个路径段
            for skip in range(len(pts) - ti + 1):
                if rec(pi + 1, ti + skip):
                    return True
            return False
        if ti >= len(pts):
            return False
        if not _match_segment(seg, pts[ti]):
            return False
        return rec(pi + 1, ti + 1)

    return rec(0, 0)


def _rule_matches_segments(segments: tuple[str, ...], anchored: bool,
                           rel: tuple[str, ...]) -> bool:
    """一条规则的段是否匹配相对 base 的路径段序列 rel。

    ``anchored``：锚定则匹配整个 rel；非锚定 basename 规则则匹配任意单个组件
    （git 语义：出现在任意层级）。
    """
    if not rel:
        return False
    compiled = _compile_segments(segments)
    if anchored:
        return _match_segments(compiled, rel)
    return any(_match_segments(compiled, (comp,)) for comp in rel)


_PROJECT_IGNORE_FILES = (".gitignore", ".projectignore")


@dataclass
class IgnoreMatcher:
    """根目录忽略匹配器。``rules`` 以 base 表达，按行加载顺序评估。"""

    rules: list[IgnoreRule] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        root: Path,
        max_ignore_bytes: int,
        max_ignore_lines: int,
        deadline_ms: int | None = None,
        max_depth: int = 64,
        max_ignore_dirs: int = 4_000,
        max_entries: int = 500_000,
        reject_dirs: frozenset[str] = frozenset(),
        anchor: "RootAnchor | None" = None,
    ) -> "IgnoreMatcher":
        """有界读取根下逐层 .gitignore/.projectignore，返回匹配器。

        硬界（均为真界，超限以异常 fail closed，绝不静默空规则或部分规则）：
        - ``max_ignore_bytes`` / ``max_ignore_lines``：**跨目录/sibling 累计**，
          且以**实际读到的字节/行数**计（不是预读 stat 估计），读取有界
          （每次最多读“剩余预算+1”字节以探测溢出）——换大文件不会被无界读入，
          也不会被旧 stat 低估而逃过字节预算。
        - ``deadline_ms``（墙钟毫秒，绝对时间戳）：遍历期间**逐条目**检查，超时抛。
        - ``max_depth``：忽略文件目录深度上限。
        - ``max_ignore_dirs``：遍历的目录总数上限。
        - ``max_entries``：整棵忽略遍历的总条目量上限（宽目录逐条目计 + 查
          deadline，**绝不先把整宽目录 list()/sorted() 进内存再计数**）。
        - ``reject_dirs``：同名目录（如 .git/node_modules/venv）整棵不递归，
          既不读取其 ignore 文件，也不深入。
        - ``anchor``：可选 ``RootAnchor``。传入时，忽略文件的打开**与目录枚举都经
          锚定上下文**（``anchor.open_file`` / ``anchor.children``），而不是按未 pin
          的 Path 重走——保证 ignore 所读的树与文件扫描是同一颗 pin 的树（防 POSIX
          源根 rename 后 ignore/遍历落到外树、引入混规则）。不传（Path 模式）为
          back-compat，走 open_secure_regular + scandir(Path)。
        读取安全：忽略文件**用与摄取相同的逐组件安全打开**（拒符号链接/reparse、
        同 FD 锚定）——普通文件在 open 前一刻被换成本文件外的 symlink 也会被原子
        拒绝跟随，而非“lstat→stat→open”的竞态后仍读到根外规则。子目录读取失败
        一律 fail closed（不吞），不把扫描层后续兜底当本层安全证明。
        """
        started = _now_ms()
        deadline = deadline_ms if deadline_ms is not None else started + (10 * 60 * 1000)
        state = {"bytes": 0, "lines": 0, "dirs": 0, "entries": 0}
        matcher = cls()
        root_path = Path(root)
        matcher._collect(
            root_path, (), state, max_ignore_bytes, max_ignore_lines,
            deadline, max_depth, max_ignore_dirs, max_entries, reject_dirs, anchor,
        )
        return matcher

    @staticmethod
    def _dir_children(
        root: Path, base: tuple[str, ...], anchor: "RootAnchor | None"
    ) -> "Iterable[tuple[str, bool, bool, bool]]":
        """枚举 ``base`` 目录的直接子项，**受控流式**逐条产出
        ``(name, is_dir, is_file, is_link_or_reparse)``。

        传 anchor 走锚定上下文（``anchor.children``，本身流式）；否则 Path 模式
        scandir。**任一子项的类型/链接判定失败一律 fail closed**（抛，不吞成“不是
        链接就递归”）——无法证明某子目录不是 symlink/reparse 就不能把它当作普通
        子目录下层（否则会被换到根外/混规则）。

        流式契约：一次只产出/物化一条子项，调用方循环内逐条目核 deadline/条目预算，
        因此“宽目录未先整列再计数”才真正成立；scandir 句柄由 ``with it`` 在迭代结束/
        close/异常时自动释放（早停 break/raise 也会关闭生成器并回收句柄）。
        """
        if anchor is not None:
            for (n, d, f, l, _) in anchor.children(base):
                yield (n, d, f, l)
            return
        dir_path = root
        for c in base:
            dir_path = dir_path / c
        try:
            it = os.scandir(dir_path)
        except NotADirectoryError:
            return
        except OSError as err:
            raise ProjectPathError(f"扫描忽略子目录失败 {dir_path}: {err}") from err
        with it:
            for child in it:
                try:
                    is_symlink = child.is_symlink()
                    is_dir = child.is_dir(follow_symlinks=False)
                    is_file = child.is_file(follow_symlinks=False)
                    if not is_symlink and not is_dir and not is_file:
                        attrs = getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
                        is_linkort = bool(attrs & 0x400)  # reparse -> 当链接排除
                    else:
                        is_linkort = is_symlink
                except OSError as err:
                    raise ProjectPathError(
                        f"子项状态读取失败 {child.name!r} ({err.strerror})"
                    ) from err
                yield (child.name, is_dir, is_file, is_linkort)

    def _collect(
        self,
        root: Path,
        base: tuple[str, ...],
        state: dict,
        max_bytes: int,
        max_lines: int,
        deadline: int,
        max_depth: int,
        max_dirs: int,
        max_entries: int,
        reject_dirs: frozenset[str],
        anchor: "RootAnchor | None",
    ) -> None:
        if _now_ms() > deadline:
            raise UnsupportedIgnorePattern("读取忽略文件超过 deadline，放弃")
        if len(base) > max_depth:
            raise UnsupportedIgnorePattern(f"忽略文件目录深度超过上限 {max_depth}")
        state["dirs"] += 1
        if state["dirs"] > max_dirs:
            raise UnsupportedIgnorePattern(f"忽略文件目录数超过上限 {max_dirs}")

        def _open_ignore(name: str):
            comps = (*base, name)
            if anchor is not None:
                return anchor.open_file(comps)
            return open_secure_regular(root, comps)

        for name in _PROJECT_IGNORE_FILES:
            try:
                opened = _open_ignore(name)
            except ProjectPathMissing:
                # 末级可证明缺失（ENOENT）：此处确无该忽略文件，良性跳过。
                continue
            except ProjectPathExcludedLink:
                # 末级是符号链接/Windows reparse（同 FD 安全打开已在组件层原子
                # 拒绝跟随，绝不读链接外内容）——安全排除链接，跳过。
                continue
            except ProjectPathError as err:
                # **任何其它** ProjectPathError（祖先缺失/被换、权限失败、根外逃逸
                # 等未知路径安全错误）都是真实失败，整次 load fail closed——绝不把
                # “可能应被忽略的文件收进来”。不匹配报错文案，用类型/原因链判定。
                raise UnsupportedIgnorePattern(
                    f"读取忽略文件失败(非可证明缺失/链接排除)，fail closed: {name}: {err}"
                ) from err
            try:
                remaining = max_bytes - state["bytes"]
                if remaining < 0:
                    raise UnsupportedIgnorePattern(f"忽略累计超过字节预算 {max_bytes}")
                # 有界读取：最多读“剩余预算+1”字节以探测溢出；按真实读到的字节计
                # 算，不用预读 stat 估计（换大文件不会被无界读入 / 不逃过字节预算）。
                target = remaining + 1
                raw = bytearray()
                with os.fdopen(opened.fd, "rb", closefd=False) as fh:
                    while len(raw) < target:
                        chunk = fh.read(target - len(raw))
                        if not chunk:
                            break
                        raw += chunk
                # 同 FD 读前后身份核验（dev/ino/nlink/size/mtime，见
                # OpenIdentity.matches）：读取期间文件被换/被改即 fail closed，
                # 绝不能以“读到的字节”静默当作原规则（主代理反例：stat_after 被
                # patch 成变化异常，load 仍接受且从未调用本校验）。OSError 也统一
                # 转 UnsupportedIgnorePattern，不把原始系统错误漏出/藏掉。
                try:
                    _after = opened.stat_after()
                except OSError as err:
                    raise UnsupportedIgnorePattern(
                        f"忽略文件读取后同FD核验失败(OSError)，fail closed: {name}"
                    ) from err
                if not opened.identity_before.matches(_after):
                    raise UnsupportedIgnorePattern(
                        f"忽略文件读取期间发生变化(同FD身份不符)，fail closed: {name}"
                    )
                state["bytes"] += len(raw)
                if state["bytes"] > max_bytes:
                    raise UnsupportedIgnorePattern(
                        f"忽略文件累计超过字节预算 {max_bytes}: {name}"
                    )
                text = _decode_utf8_repl(bytes(raw))
                file_lines = text.splitlines()
                state["lines"] += len(file_lines)
                if state["lines"] > max_lines:
                    raise UnsupportedIgnorePattern(
                        f"忽略文件累计超过行预算 {max_lines}: {name}"
                    )
                for line in file_lines:
                    rule = _parse_line(line)
                    if rule is None:
                        continue
                    self.rules.append(
                        IgnoreRule(
                            raw=line.rstrip("\r\n"), negated=rule.negated,
                            dir_only=rule.dir_only, anchored=rule.anchored,
                            base=base, segments=rule.segments,
                        )
                    )
            finally:
                opened.close()
        # 递归子目录：只进普通无链接目录，且同名硬排除目录整棵跳过。惰性逐条目
        # 迭代：**绝不把整宽目录 list()/sorted() 进内存再计数**——每条目先查
        # deadline / 条目预算，再决定是否深入。枚举本身由 _dir_children 负责（经
        # anchor.children 或 scandir(Path)），失败一律 fail closed。
        for (name, is_dir, _is_file, is_linkort) in self._dir_children(root, base, anchor):
            state["entries"] += 1
            if state["entries"] > max_entries:
                raise UnsupportedIgnorePattern(
                    f"忽略遍历条目数超过上限 {max_entries}（宽目录未先整列）"
                )
            if _now_ms() > deadline:
                raise UnsupportedIgnorePattern("读取忽略文件超过 deadline，放弃")
            if name in reject_dirs:
                continue
            if is_linkort:
                continue  # 符号链接/junction/reparse 不递归
            if not is_dir:
                continue
            # 子目录读取失败必须 fail closed（不吞）；其内 ignore 规则若读不到
            # 就不能保证忽略语义，不可静默跳过（不靠扫描层后续兜底当本层证明）。
            self._collect(
                root, base + (name,), state, max_bytes, max_lines, deadline,
                max_depth, max_dirs, max_entries, reject_dirs, anchor,
            )

    def _path_is_excluded(self, rel_item: tuple[str, ...], is_dir: bool) -> bool:
        """对一个根相对路径判定是否被忽略规则排除。

        ``rel_item``=根相对完整段序列（含本项自身）。
        评估顺序：按行序，最后一个匹配的规则生效（正规则排除、负规则``!``
        重新包含）。仅目录规则对非目录不生效。
        """
        result = False
        for rule in self.rules:
            if len(rel_item) < len(rule.base) or rel_item[: len(rule.base)] != rule.base:
                continue
            rel = rel_item[len(rule.base):]
            if not rel:
                continue
            if rule.dir_only and not is_dir:
                continue
            if _rule_matches_segments(rule.segments, rule.anchored, rel):
                result = not rule.negated
        return result

    def is_excluded(self, rel_path: str, is_dir: bool) -> bool:
        """对外：判断根相对路径（正斜杠）是否被忽略规则排除。

        ``is_dir`` 用于仅目录规则；对文件也会检查其祖先目录是否被排除。
        """
        if len(rel_path) > MAX_PATH:
            return True
        segs = tuple(p for p in rel_path.split("/") if p)
        # 逐级前缀：祖先目录（除非本项是目录，也包含本项自身）。
        for depth in range(1, len(segs) + 1):
            prefix = segs[:depth]
            is_dir_level = depth < len(segs) or is_dir
            if self._path_is_excluded(prefix, is_dir_level):
                return True
        return False


def _now_ms() -> int:
    import time

    return time.time_ns() // 1_000_000


def _decode_utf8_repl(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")
