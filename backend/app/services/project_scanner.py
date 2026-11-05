"""项目源安全扫描 + 稳定 manifest（纯服务，不连 DB/模型）。

职责：
- 从受控根按组件遍历，逐组件拒绝 symlink/reparse/junction、特殊文件；
- 先硬排除（.env 及变体、密钥/证书/凭据、.git、venv/node_modules、构建/日志/
  缓存、压缩/二进制/未知），**反选规则不能覆盖秘密排除**；
- 应用有界 .gitignore/.projectignore（见 project_ignore，负规则必须支持）；
- 上限（全为硬界，见 ProjectScanLimits.validate，拒绝 0/负数）：
  文件数、单文件/总字节、路径长度、忽略字节/行数、遍历条目量、被排除项收纳量、
  真实墙钟扫描 deadline（遍历期间周期性检查，超时即抛）；
- 每个入选文件经 project_blobs.ingest_verified_file（同一 pin FD 前后 fstat）
  复制为不可变 blob，manifest 以**实际 blob 字节 SHA** + relative_path 稳定排序；
  硬链接去重以**已验证同一 FD 的打开身份**（真实 inode/dev）判同，绝不凭
  DirEntry.stat（Windows 返回 0）或未核验路径 stat；
- 权限拒绝/中途变化/列表失败/超时一律抛 ``ProjectScanError``（fail closed），
  绝不伪装成"文件已删除"或"成功空 manifest"；扫描不是 FS 原子快照，元数据
  报告时间窗口与 unknown。
- 解析 CPU 预算不是本扫描阶段字段：逐文件解析的 owned 子进程 deadline 由上层
  （index worker）按 ProjectScanLimits.parse_timeout_ms（建议值）强制。
- git commit 仅可选附加元数据：本服务默认不运行 git，未提供时如实记 unknown。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from app.services.project_blobs import BlobStore, HardlinkRejected
from app.services.project_ignore import IgnoreMatcher, UnsupportedIgnorePattern
from app.services.project_parser import detect_language
from app.services.project_paths import (
    ProjectPathError,
    RootAnchor,
    split_relative_path,
)
from app.services.project_types import (
    ManifestEntry,
    ProjectScanLimits,
    ScanReport,
)

MAX_DEPTH = 512


class ProjectScanError(OSError):
    """扫描失败（权限/中途变化/列表失败等），调用方不得当作成功空清单。"""


# ---------- 硬排除 ----------
# 这些目录名在任意层级命中即整棵排除，且反选规则（.gitignore re-include）无效。
_HARD_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".cache", ".next",
    "dist", "build", "target", "coverage", ".idea", ".vscode", ".gradle",
    ".egg-info", ".ruff_cache", ".yarn", ".pnpm-store",
}
# 压缩/二进制扩展名（硬排除）。
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".avif",
    ".pdf", ".docx", ".xlsx", ".pptx", ".zip", ".gz", ".bz2", ".xz", ".7z",
    ".rar", ".tar", ".tgz", ".jar", ".war", ".whl", ".so", ".dll", ".dylib",
    ".exe", ".bin", ".class", ".o", ".obj", ".pyc", ".pyo", ".lock",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".min.js", ".min.css",
}
# 密钥/证书/凭据扩展名（硬排除，且不被反选覆盖）。
_SECRET_EXTS = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".pkcs12",
                ".kdbx", ".gpg", ".asc", ".pem", ".crt", ".cer"}
# 凭据类文件名（大小写不敏感，含 .env 及变体）。
_SECRET_BASENAMES = {
    ".env", ".env.example", ".env.local", ".env.production", ".env.development",
    ".htpasswd", ".netrc", ".npmrc", ".dockercfg", ".git-credentials",
    "credentials", "credential", "secret", "secrets", "token",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "clave",
}
_LOG_EXTS = {".log", ".out", ".err", ".pid"}

# 可安全解析/读取的已知文本/代码扩展名白名单。扩展名**既不在白名单、也不在
# 已知二进制**时视为“未知扩展名”硬排除（fail default），绝不默认当文本收进。
_KNOWN_TEXT_EXTS = {
    ".py", ".pyi", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts",
    ".cts", ".tsx", ".vue", ".md", ".markdown", ".rst", ".txt", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash", ".zsh",
    ".bat", ".cmd", ".ps1", ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".sql", ".xml", ".proto", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cc", ".hh", ".cs", ".kt", ".kts", ".swift", ".php", ".rb", ".lua", ".pl",
    ".pm", ".r", ".jl", ".dart", ".ex", ".exs", ".tf", ".tfvars", ".gradle",
    ".sbt", ".dockerfile", ".env-example", ".gitignore", ".editorconfig",
    ".gitconfig", ".npmrc", ".yarnrc", ".babelrc", ".eslintrc", ".eslintignore",
    ".prettierrc", ".prettierignore", ".gitmodules", ".gitattributes",
    ".hcl", ".nix", ".fish", ".mk", ".dockerignore", ".git-blame-ignore-revs",
}


def _unknown_extension(name: str) -> bool:
    """扩展名存在但既非已知文本也非已知二进制 -> 未知扩展名（硬排除）。"""
    low = name.lower()
    if "." not in low:
        return False  # 无扩展名（如 Dockerfile/LICENSE/Makefile）按常规文本放行
    dot = low.rfind(".")
    ext = low[dot:]
    return ext not in _KNOWN_TEXT_EXTS and ext not in _BINARY_EXTS and ext not in _LOG_EXTS


def _is_secret_name(name: str) -> bool:
    """凭据识别（大小写不敏感）：命中凭据basename、凭据扩展名、.env 变体。

    关键：凭据basename也按**去扩展名的 stem** 匹配——``credentials.json`` /
    ``token.txt`` 这类“凭据名 + 非凭据扩展名”的文件也必须被当作 secret 硬排除，
    否则会被误纳入索引。
    """
    low = name.lower()
    if low in _SECRET_BASENAMES:
        return True
    if low.startswith(".env."):
        return True
    # 去最后一个点的 stem：把 credentials.json 的 stem=credentials 认作凭据。
    stem = low.rsplit(".", 1)[0] if "." in low else low
    if stem in _SECRET_BASENAMES:
        return True
    dot = low.rfind(".")
    ext = low[dot:] if dot >= 0 else ""
    return ext in _SECRET_EXTS


def _binary_name(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in _BINARY_EXTS)


def _log_name(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in _LOG_EXTS)


def _hard_exclude(rel_path: str, name: str, is_dir: bool) -> str | None:
    """返回硬排除原因（None=不动）；反选规则不能覆盖这里。"""
    if _is_secret_name(name) and not is_dir:
        return "secret"
    if not is_dir:
        if _binary_name(name):
            return "binary"
        if _log_name(name):
            return "log"
        if _unknown_extension(name):
            return "unknown-extension"
    for comp in rel_path.split("/"):
        if comp.lower() in _HARD_DIRS:
            if comp in {"node_modules", ".venv", "venv", "__pycache__", ".git"}:
                return "dependency-or-build"
            return "build-or-cache"
    if any(comp.lower() == "logs" for comp in rel_path.split("/")):
        return "log-dir"
    return None


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def scan_project(
    root: Path | RootAnchor,
    store: BlobStore,
    limits: ProjectScanLimits | None = None,
    reject_hardlinks: bool = True,
    git_commit: str | None = None,
    git_dirty: bool | None = None,
) -> ScanReport:
    """扫描授权根目录为安全文件清单，成功返回稳定 manifest。

    任何权限拒绝/中途变化/列表失败都抛 ``ProjectScanError``；
    结果 manifest 以 (relative_path, sha256, size) 稳定排序。

    ``root`` 可为 ``Path``（back-compat）或 ``RootAnchor``（见 project_paths）。
    传入 ``RootAnchor`` 时，每次文件摄取经锚定的源根打开（逐组件拒 reparse/
    junction、源根被 pin），从根上防住 Windows junction 祖先逃逸；此时遍历以
    ``anchor.source_root_path`` 为基准。

    ``reject_hardlinks`` 默认 **True**（安全默认）：任何 nlink>1 的文件在同一核验
    FD 上、读取/发布前即拒（HardlinkRejected），其内容绝不写入 blob。显式传
    ``False`` 仍兼容旧调用方，但那是刻意放硬链的旧方案。
    """
    limits = limits or ProjectScanLimits()
    limits.validate()  # 拒绝 0=无限/负数，扫描全程有界
    anchored = isinstance(root, RootAnchor)
    if anchored:
        scan_base = root.source_root_path
    else:
        root = Path(root)
        scan_base = root
    if not scan_base.is_dir():
        raise ProjectScanError(f"根目录不可用: {scan_base}")

    started = _now_ms()
    deadline = started + limits.scan_timeout_ms
    # 忽略装载与扫描共用同一 deadline；硬排除目录整棵不递归进 ignore 读取。
    # 忽略规则无法可靠解释/超预算时 fail closed：一律转为 ProjectScanError，
    # 保持“扫描失败即抛 ProjectScanError”的统一契约（不当作成功空清单）。
    try:
        matcher = IgnoreMatcher.load(
            scan_base,
            limits.max_ignore_bytes,
            limits.max_ignore_lines,
            deadline_ms=deadline,
            reject_dirs=frozenset({d.lower() for d in _HARD_DIRS}),
            anchor=root if anchored else None,
        )
    except UnsupportedIgnorePattern as err:
        raise ProjectScanError(f"忽略规则装载失败: {err}") from err
    except ProjectPathError as err:
        # 忽略装载里的真实失败（祖先缺失/被换、权限、未知路径安全错误）在
        # IgnoreMatcher._collect 已 fail closed；这里把任何逃逸的 ProjectPathError
        # 也统一为 ProjectScanError，保持“扫描失败即抛 ProjectScanError”。
        raise ProjectScanError(f"忽略规则安全装载失败: {err}") from err
    entries: list[ManifestEntry] = []
    excluded: list[str] = []
    limits_notes: list[str] = []
    total_bytes = 0
    file_count = 0
    iterations = 0
    truncated = False

    def _check_deadline() -> None:
        """真实墙钟 deadline：超时即 fail closed，绝不静默返回部分成功清单。"""
        nonlocal truncated
        if _now_ms() >= deadline:
            truncated = True
            raise ProjectScanError(f"扫描超过 deadline {limits.scan_timeout_ms}ms，放弃本次扫描")

    def _record_limit(note: str) -> None:
        nonlocal truncated
        truncated = True
        limits_notes.append(note)

    def _record_excluded(text: str) -> None:
        """收纳被排除项；超过信息预算则只记一次限制，不再无界增长。"""
        if len(excluded) < limits.max_excluded_items:
            excluded.append(text)
        elif not any("被排除项信息超预算" in n for n in limits_notes):
            _record_limit(
                f"被排除项信息超过收纳上限 {limits.max_excluded_items}，仅保留前文"
            )

    def _children(rel_dir: tuple[str, ...]) -> "Iterable[tuple[str, bool, bool, bool, int]]":
        """枚举 ``rel_dir``（root 相对）直接子项，**受控流式**逐条产出
        (name, is_dir, is_file, is_linkort, st_size)。

        anchored 时经 ``anchor.children``（被 pin 句柄链枚举，与文件读取同树，本身
        流式）；Path 模式经 scandir(Path)。**任一子项类型/链接判定失败一律 fail
        closed**——无法证明某子目录不是 symlink/reparse 就不能把它当普通子目录递归
        （否则会被换到根外/混规则）。

        流式契约：一次只产出一条，调用方 ``visit`` 循环内逐条目核 deadline/条目预算，
        因此“宽目录未先整列再计数”才真正成立；scandir 句柄由 ``with it`` 在迭代结束/
        close/异常时自动释放（早停 break/raise 也会关闭生成器并回收句柄）。
        """
        if anchored:
            yield from root.children(rel_dir)
            return
        dir_path = scan_base
        for c in rel_dir:
            dir_path = dir_path / c
        try:
            it = os.scandir(dir_path)
        except NotADirectoryError:
            return
        except OSError as err:
            raise ProjectScanError(f"目录读取失败: {dir_path} ({err.strerror})") from err
        with it:
            for entry in it:
                try:
                    is_symlink = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                    if not is_symlink and not is_dir and not is_file:
                        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                        is_linkort = bool(attrs & 0x400)  # reparse -> 当链接排除
                    else:
                        is_linkort = is_symlink
                    st = entry.stat(follow_symlinks=False)
                    st_size = int(st.st_size)
                except OSError as err:
                    raise ProjectScanError(f"条目状态读取失败: {entry.name!r} ({err.strerror})") from err
                yield (entry.name, is_dir, is_file, is_linkort, st_size)

    def visit(rel_dir: tuple[str, ...]) -> None:
        nonlocal total_bytes, file_count, iterations
        if len(rel_dir) > MAX_DEPTH:
            _record_limit(f"扫描超过最大深度 {MAX_DEPTH}")
            return
        for (name, is_dir, is_file, is_linkort, st_size) in _children(rel_dir):
            iterations += 1
            _check_deadline()  # 每条目查 deadline，不只在每目录
            if iterations > limits.max_iterations:
                _record_limit(f"扫描超过遍历条目预算 {limits.max_iterations}")
                return
            rel_path = "/".join((*rel_dir, name)) if rel_dir else name
            if len(rel_path) > limits.max_path_chars:
                _record_excluded(f"{rel_path}:path-too-long")
                continue
            if is_linkort:
                # 符号链接/junction/reparse 一律排除（不跟随），不递归、不读取。
                _record_excluded(f"{rel_path}:symlink-or-reparse")
                continue
            if is_dir:
                reason = _hard_exclude(rel_path, name, is_dir=True)
                if reason:
                    _record_excluded(f"{rel_path}:{reason}")
                    continue
                if matcher.is_excluded(rel_path, is_dir=True):
                    _record_excluded(f"{rel_path}:ignored-dir")
                    continue
                if file_count >= limits.max_files:
                    _record_limit(f"超过文件数上限 {limits.max_files}")
                    return
                visit((*rel_dir, name))
                continue
            if not is_file:
                _record_excluded(f"{rel_path}:special-file")
                continue
            # 普通文件：用权威 st_size 硬排除“过大文件”（未核验的列表 stat 仅作快速
            # 跳过；权威字节上限仍在复制后用真实 blob 字节判定）。绝不能因 ingest 的
            # 预算溢出把整次扫描 fail closed——那是排除项，不是失败。
            if st_size > limits.max_single_bytes:
                _record_excluded(f"{rel_path}:too-large")
                continue
            reason = _hard_exclude(rel_path, name, is_dir=False)
            if reason:
                _record_excluded(f"{rel_path}:{reason}")
                continue
            if matcher.is_excluded(rel_path, is_dir=False):
                _record_excluded(f"{rel_path}:ignored")
                continue
            if file_count >= limits.max_files:
                _record_limit(f"超过文件数上限 {limits.max_files}")
                return
            try:
                rel_components = split_relative_path(rel_path)
            except ValueError as exc:
                raise ProjectScanError(f"非法相对路径 {rel_path}: {exc}") from exc
            try:
                # reject_hardlinks 由 ingest 在“同一核验 FD 读取/发布前”判定：
                # 硬链内容绝不落盘（修复 manifest 不收、但 blob 已持久化根外
                # hardlink 内容的泄漏）。root 可为 Path 或 RootAnchor。
                _blob_ref, _identity = store.ingest_verified_file(
                    root, rel_components, limits.max_single_bytes,
                    reject_hardlinks=reject_hardlinks,
                )
            except HardlinkRejected as err:
                # 硬链接：同 FD nlink>1 即拒（根内/根外硬链均拒），且内容未入库。
                _record_excluded(f"{rel_path}:hardlink")
                continue
            except Exception as err:
                # 中途变化/权限/复制失败一律 fail closed。
                raise ProjectScanError(f"复制/读取失败 {rel_path}: {err}") from err
            # 权威总字节：以复制出的真实 blob 字节数累计（不用未核验的 st_size 估计）。
            if total_bytes + _blob_ref.size > limits.max_total_bytes:
                _record_limit(f"超过总字节上限 {limits.max_total_bytes}")
                return
            file_count += 1
            total_bytes += _blob_ref.size
            entries.append(
                ManifestEntry(
                    relative_path=rel_path,
                    sha256=_blob_ref.sha256,
                    size=_blob_ref.size,
                    language=detect_language(rel_path),
                    parser_status="pending",
                )
            )

    visit(())
    ended = _now_ms()
    entries.sort(key=lambda e: (e.relative_path, e.sha256, e.size))
    excluded.sort()
    unknown: list[str] = []
    if git_commit is None:
        # 仅元数据未知：不得使普通非 Git 目录 is_success=False。
        unknown.append("git身份未解析：本服务不运行git，需调用方显式提供commit/脏标记")
    return ScanReport(
        entries=tuple(entries),
        excluded=tuple(excluded),
        limits=tuple(limits_notes),
        truncated=truncated,
        git_commit=git_commit,
        git_dirty=git_dirty,
        started_ms=started,
        ended_ms=ended,
        unknown_reason=tuple(unknown),
    )


def load_project_ignore(root: Path, limits: ProjectScanLimits | None = None) -> IgnoreMatcher:
    """公开的忽略规则装载入口（供测试/调试复用）。"""
    limits = limits or ProjectScanLimits()
    started = _now_ms()
    return IgnoreMatcher.load(
        root,
        limits.max_ignore_bytes,
        limits.max_ignore_lines,
        deadline_ms=started + limits.scan_timeout_ms,
        reject_dirs=frozenset({d.lower() for d in _HARD_DIRS}),
    )
