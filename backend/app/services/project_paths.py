"""项目源路径安全：根映射、相对路径校验、Windows 非法名、链接/身份防护。

安全模型：
- API 只接受 ``root_key`` + ``relative_dir``，服务从**显式根映射**读取根；
  映射默认空，命中不到就拒绝，绝不接受任意绝对路径。
- 保存根目录身份指纹；管理员把配置指向新目录树时必须显式换绑，
  否则以后台静默切换到别的树（防"换树即改变被索引内容"）。
- 每个路径组件逐级校验：拒绝绝对/UNC/盘符、``..``、ADS、Windows 设备名、
  尾点尾空格、保留字符(``* ? < > | "``)与控制字符。
- 打开文件**不做前置 realpath**（那会擦掉 symlink/junction 痕迹）；改为
  按组件用受限句柄遍历（POSIX dir_fd+O_NOFOLLOW；Windows CreateFileW +
  OPEN_REPARSE_POINT/目录 anchor），逐组件拒绝链接/reparse。
- 读取前后对**同一已打开 FD** 做 fstat 核对身份/大小，变化即 fail closed；
  绝不按路径重新 stat（防"路径换外部文件→open→换回"绕过）。
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat as _stat
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Windows 保留设备名（含尾随扩展名形式，如 NUL.txt、COM1.log）。
_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *{f"COM{i}" for i in range(1, 10)},
    *{f"LPT{i}" for i in range(1, 10)},
}
# 上标数字字符（Windows 也识别 COM¹…COM⁹、LPT²…）：⁰U+2070 ¹U+00B9 ²U+00B2 ³U+00B3 ⁴–⁹U+2074–U+2079。
_SUPERSCRIPT = "".join(chr(c) for c in (0x00B2, 0x00B3, 0x00B9, *range(0x2070, 0x207A)))
# Windows 保留/非法命名字符（不含 / \ 与 .；这些已单独处理）。
_WINDOWS_RESERVED = set('*?<>|"')
MAX_PATH = 4096


class ProjectPathError(ValueError):
    """路径/根不安全或被拒。"""


class ProjectPathMissing(ProjectPathError):
    """``open_secure_regular`` 的**末级组件可证明不存在**（ENOENT / 文件未找到）。

    这是唯一一种在“读取忽略规则”层可安全跳过的缺失：末级对象本身就没了，
    属于“此处确无可读对象”，绝非祖先被换/权限失败/未知错误。祖先缺失一律
    抛普通 ``ProjectPathError``（fail closed）——祖先变化可能被路由到别处。
    """


class ProjectPathExcludedLink(ProjectPathError):
    """末级组件是**被拒的链接/reparse 点**（O_NOFOLLOW 得 ELOOP / Windows
    打开出 reparse attr）。

    这是“安全排除链接”：该末级本身是链接，安全打开层在原子性上拒绝跟随，
    因此绝无可能读到链接外内容。“此处有一个不可跟随的链接”是良性跳过项。
    **任何祖先**是链接/reparse 仍抛普通 ``ProjectPathError``（fail closed）。
    """


@dataclass(frozen=True)
class RootBinding:
    root_key: str
    root: Path
    fingerprint: str


@dataclass(frozen=True)
class OpenIdentity:
    """打开文件的身份——用于对同一 FD 复制后核对，变化即 fail closed。"""

    path: str
    st_dev: int
    st_ino: int
    st_nlink: int
    st_size: int
    st_mtime_ns: int

    @classmethod
    def from_fd(cls, fd: int, path: str) -> "OpenIdentity":
        st = os.fstat(fd)
        return cls(
            path=str(path),
            st_dev=st.st_dev,
            st_ino=st.st_ino,
            st_nlink=getattr(st, "st_nlink", 1),
            st_size=st.st_size,
            st_mtime_ns=getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
        )

    def matches(self, other: "OpenIdentity | None") -> bool:
        if other is None:
            return False
        return (
            self.st_dev == other.st_dev
            and self.st_ino == other.st_ino
            and self.st_nlink == other.st_nlink
            and self.st_size == other.st_size
            and self.st_mtime_ns == other.st_mtime_ns
        )


def root_fingerprint(root: Path) -> str:
    """根目录身份指纹：真实路径 + 设备/文件系统索引（换树必须显式重绑）。"""
    try:
        st = os.stat(os.fspath(root), follow_symlinks=True)
        # 不用 realpath 擦去链接痕迹而失去判据：指纹只含路径文本 + inode。
        resolved = os.path.realpath(os.fspath(root))
    except OSError as err:
        raise ProjectPathError(f"无法读取根目录: {root}") from err
    payload = f"{resolved}\0{st.st_dev}\0{st.st_ino}\0{_stat.S_ISDIR(st.st_mode)}"
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def make_root_binding(root_key: str, root: Path) -> RootBinding:
    """构造显式根绑定。

    根来自**服务端配置的显式根映射**，因此 POSIX 绝对路径（``/...``）与 Windows
    本机盘符绝对路径（``C:\\...``）都是合法的服务端根——只对这个根做相对路径解析，
    绝不接收客户端任意绝对路径。但 **Windows UNC 网络根（``\\\\server\\share``）必须
    拒绝**：网络共享无法保证本机锚定/reparse 核验语义。根本身不得是链接/reparse。
    """
    root = Path(root).expanduser()
    s = str(root)
    # 拒绝 Windows UNC 网络根（\\server\share）：网络共享无法保证本机锚定/
    # reparse 核验语义。POSIX 绝对单斜杠（/...）与 Windows 盘符绝对（C:\...）
    # 都是合法服务端根，照常放行。用字符串前缀判断，不依赖 pathlib 的 UNC 探测。
    if s.startswith("\\\\"):
        raise ProjectPathError(f"根目录为 Windows UNC 网络路径，拒绝绑定: {root}")
    if not root.is_dir():
        raise ProjectPathError(f"根目录不是目录: {root}")
    # 根不得是符号链接/reparse（含 Windows junction/mount point/one-way reparse）：
    # 是则拒绝绑定（防换指）。末级 ``is_symlink()`` 只判 S_IFLNK，**会漏掉
    # junction**（junction 是目录 + FILE_ATTRIBUTE_REPARSE_POINT），故 Windows
    # 还要对根句柄用 OPEN_REPARSE_POINT 检查 reparse 属性，绝不把链接/junction
    # 树升格为可信根。
    if root.is_symlink():
        raise ProjectPathError(f"根目录是符号链接/reparse点，拒绝绑定: {root}")
    if IS_WINDOWS:
        if _root_is_reparse_win32(root):
            raise ProjectPathError(f"根目录是 Windows reparse 点(junction等)，拒绝绑定: {root}")
        # 根不仅要自身不是 reparse，祖先链（从文件系统根到根每一级）也不能是
        # junction/mount-point：否则 ``authorized\\..\\junction-祖先下的普通子目录``
        # 会被当作可信根而把 anchor 落到外部树。任一祖先无法核验（句柄失败）一律
        # fail closed（抛 ProjectPathError），不乐观放行。
        _verify_root_anon_up_win32(root)
    # 核心路径含任何控制字符/保留命名空间也不绑定——防根被接成非法字符串。
    if _has_control_char(s):
        raise ProjectPathError(f"根目录含控制字符，拒绝绑定: {root!r}")
    return RootBinding(root_key=root_key, root=root, fingerprint=root_fingerprint(root))


def _verify_root_anon_up_win32(root: Path) -> None:
    """Windows：从文件系统根（盘符）按组件核验根的所有祖先不是 reparse 点。

    只核验（打开即关，不保留句柄）——绑定是静态记录，不需要持有 pin；持有由
    ``secure_source_root`` 的 ``_anchor_windows`` 负责。任何组件无法验证（句柄/
    信息失败）或命中 reparse 一律抛 ``ProjectPathError``（fail closed）。
    """
    api = _win32()
    root_s = os.fspath(root).rstrip("\\/")
    if root_s.startswith("\\\\"):
        raise ProjectPathError("根目录为 Windows UNC 网络路径，拒绝绑定")
    m = re.match(r"^([A-Za-z]:)(\\|$)", root_s)
    if not m:
        # 非盘符开头（少见）：交给调用方其它判定；这里不做越权假设。
        return
    drive = m.group(1) + "\\"
    h = api.open_handle(drive, is_dir=True)
    try:
        di = api.info(h)
        if di.dwFileAttributes & api.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ProjectPathError(f"文件系统根是 reparse 点(卷挂载/junction)，拒绝绑定: {drive}")
    finally:
        api.CloseHandle_([h])
    cur = drive
    parts = [p for p in root_s[len(drive):].split("\\") if p]
    for comp in parts:
        cur = cur.rstrip("\\/") + "\\" + comp
        h = api.open_handle(cur, is_dir=True)
        try:
            ci = api.info(h)
            if not (ci.dwFileAttributes & api.FILE_ATTRIBUTE_DIRECTORY):
                raise ProjectPathError(f"根祖先不是目录: {comp!r}")
            if ci.dwFileAttributes & api.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ProjectPathError(
                    f"根祖先/组件是 reparse 点(junction等)，拒绝绑定: {comp!r}"
                )
        finally:
            api.CloseHandle_([h])


def _has_control_char(component: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in component)


def _root_is_reparse_win32(root: Path) -> bool:
    """Windows：以 OPEN_REPARSE_POINT 打开根目录句柄，检查 reparse 属性。

    不用 ``Path.is_symlink()``（junction 是目录而非 S_IFLNK，会漏掉）。直接以
    FILE_FLAG_OPEN_REPARSE_POINT 打开根自身——真 junction 会带
    FILE_ATTRIBUTE_REPARSE_POINT；普通目录不会。
    """
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    kernel32 = ctypes.windll.kernel32

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FILETIME),
            ("ftLastAccessTime", FILETIME),
            ("ftLastWriteTime", FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    # 用 WINFUNCTYPE(use_last_error=True) 定义 CreateFileW：失败后 ctypes 才可靠
    # 保留 Win32 last-error，供 get_last_error() 判定末级缺失等精确原因。
    _CFW = ctypes.WINFUNCTYPE(
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        use_last_error=True,
    )
    CreateFileW = _CFW(("CreateFileW", kernel32))
    GetFileInformationByHandle = kernel32.GetFileInformationByHandle
    GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
    GetFileInformationByHandle.restype = wintypes.BOOL
    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    handle = CreateFileW(
        os.fspath(root), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle == INVALID_HANDLE_VALUE or not handle:
        # fail closed：打不开根句柄 = 无法证明根不是 reparse/可用，绝不当 True。
        # 不做“假定 is_dir 先把关”的乐观返回，否则 junction 祖先下的普通子目录
        # 会被当作安全根而把外部树升为可信根。
        from ctypes import get_last_error
        raise ProjectPathError(
            f"无法打开根目录句柄(判定 reparse 失败，fail closed): {root} (err={get_last_error()})"
        )
    try:
        buf = BY_HANDLE_FILE_INFORMATION()
        if not GetFileInformationByHandle(handle, ctypes.byref(buf)):
            raise ProjectPathError(f"获取根目录句柄信息失败(判定 reparse 失败，fail closed): {root}")
        return bool(buf.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT)
    finally:
        CloseHandle(handle)


def _windows_reserved_char(component: str) -> bool:
    return any(ch in _WINDOWS_RESERVED for ch in component)


def _is_relative_component_windows(component: str) -> bool:
    """Windows 下校验单个相对路径组件的合法性（不含链接判定，链接由打开层拒绝）。"""
    if not component:
        return False
    if component in {".", ".."}:
        return False
    if component.startswith(("\\\\", "/", "\\")):
        return False  # UNC / 绝对 / 单反斜杠根式
    if ":" in component:
        return False  # ADS 或盘符
    if component.endswith((".", " ")):
        return False  # 尾点/尾空格
    if _has_control_char(component) or _windows_reserved_char(component):
        return False
    base = component.split(".", 1)[0].rstrip()
    if base.upper() in _DEVICE_NAMES or _DEVICE_NAME_RE.match(component):
        return False
    return True


_DEVICE_NAME_RE = re.compile(
    rf"^(COM[1-9]|LPT[1-9])([{_SUPERSCRIPT}]+.*)?$", re.IGNORECASE
)


def _is_relative_component_posix(component: str) -> bool:
    if not component:
        return False
    if component in {".", ".."}:
        return False
    if component.startswith("/"):
        return False
    if "\x00" in component or _has_control_char(component):
        return False
    return True


def split_relative_path(relative: str) -> tuple[str, ...]:
    """把客户端相对路径标准化为正斜杠组件元组；非法即抛错。"""
    if not isinstance(relative, str) or not relative:
        raise ProjectPathError("相对路径为空")
    if relative.startswith(("/", "\\\\")) or re.match(r"^[A-Za-z]:", relative):
        raise ProjectPathError("只允许相对路径，不接受绝对/盘符/UNC")
    if len(relative) > MAX_PATH:
        raise ProjectPathError("相对路径过长")
    normalized = relative.replace("\\", "/").strip("/")
    if not normalized or normalized in {".", ".."}:
        raise ProjectPathError("相对路径为空")
    parts = normalized.split("/")
    for part in parts:
        if IS_WINDOWS:
            if not _is_relative_component_windows(part):
                raise ProjectPathError(f"非法路径组件: {part!r}")
        else:
            if not _is_relative_component_posix(part):
                raise ProjectPathError(f"非法路径组件: {part!r}")
    return tuple(parts)


def _validate_raw_components(rel_components: tuple[str, ...]) -> None:
    """校验**原始**相对路径组件元组（不假设调用方已经 split 过）。

    防任何直接以路径/元组入口（RootAnchor.open_file / children、open_secure_regular、
    secure_source_root）传入的组件含：空组件、``.``/``..``、绝对/UNC/盘符/分隔符、
    控制字符、Windows 保留非法名。逐组件校验失败即抛 ``ProjectPathError``（fail
    closed），绝不以“调用方会 split”为前提猜测。

    注意：空元组 ``()`` **不在此判定为错误**——对 ``children(())`` /
    ``secure_source_root(root, ())`` 而言“空 rel”=“源根本身”，是合法的；对
    ``open_file``/``open_secure_regular`` 这类“打开一个普通文件”的语义，调用方须
    另行显式拒绝空元组。
    """
    for part in rel_components:
        if IS_WINDOWS:
            if not _is_relative_component_windows(part):
                raise ProjectPathError(f"非法相对路径组件: {part!r}")
        else:
            if not _is_relative_component_posix(part):
                raise ProjectPathError(f"非法相对路径组件: {part!r}")


def classify_special(mode: int) -> str | None:
    """按 stat 模式返回特殊文件类别（None 表示普通文件/目录）。"""
    if _stat.S_ISDIR(mode) or _stat.S_ISREG(mode):
        return None
    if _stat.S_ISLNK(mode):
        return "symlink"
    if _stat.S_ISFIFO(mode):
        return "fifo"
    if _stat.S_ISSOCK(mode):
        return "socket"
    if _stat.S_ISBLK(mode):
        return "block-device"
    if _stat.S_ISCHR(mode):
        return "char-device"
    return "other"


# ============ 安全打开（往来同一 FD 的 fstat 核对） ============

@dataclass
class OpenedFile:
    """安全打开的普通文件：fd 已 pin，可供读取并做 before/after fstat。"""

    fd: int
    identity_before: OpenIdentity
    _release: callable | None = None

    def stat_after(self) -> OpenIdentity:
        """对同一 fd 重新 fstat（反映读取中的真实文件，不依赖路径）。"""
        return OpenIdentity.from_fd(self.fd, self.identity_before.path)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass
        if self._release is not None:
            self._release()


def open_secure_regular(root: Path, rel_components: tuple[str, ...]) -> OpenedFile:
    """按组件受限遍历打开普通文件，返回 pin 住的 fd + 打开时身份。

    POSIX：dir_fd + O_NOFOLLOW 逐组件，拒绝符号链接；
    Windows：CreateFileW OPEN_REPARSE_POINT/目录 anchor 逐组件，拒绝 reparse。
    打开者必须随后对**同一 fd** 做 before/after fstat 核对。
    """
    _validate_raw_components(rel_components)
    if IS_WINDOWS:
        return _open_secure_windows(root, rel_components)
    return _open_secure_posix(root, rel_components)


def _walk_posix_from_fd(
    root_fd: int,
    root_label: str,
    rel_components: tuple[str, ...],
    *,
    borrowed_root: bool = False,
) -> OpenedFile:
    """从已 pin 的 root_fd(dirfd) 逐组件 O_NOFOLLOW 安全打开普通文件。

    ``borrowed_root=True``：root_fd 由调用方（RootAnchor 等）持有，release 时
    不关闭它（由持有者负责）；否则（路径入口）由本函数在 release 时一并关闭。
    组件打开、末级 Missing/ExcludedLink 分类与 ``_open_secure_posix`` 完全一致。
    """
    o_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_dir = getattr(os, "O_DIRECTORY", 0)
    held: list[int] = []

    def release() -> None:
        while held:
            try:
                os.close(held.pop())
            except OSError:
                pass
        if not borrowed_root:
            try:
                os.close(root_fd)
            except OSError:
                pass

    parent = root_fd
    try:
        for comp in rel_components[:-1]:
            try:
                child = os.open(comp, o_flags | o_dir | o_nofollow, dir_fd=parent)
            except OSError as err:
                raise ProjectPathError(f"目录组件不可用 {comp!r} ({err.strerror})") from err
            held.append(child)
            cst = os.fstat(child)
            if not _stat.S_ISDIR(cst.st_mode):
                raise ProjectPathError(f"组件不是目录: {comp!r}")
            parent = child  # 保持父句柄链按序，全部 open 到结束才 close
        last = rel_components[-1]
        try:
            file_fd = os.open(last, o_flags | o_nofollow, dir_fd=parent)
        except FileNotFoundError as err:
            # 末级可证明缺失（ENOENT）：良性“此处无该文件”，可安全跳过。
            raise ProjectPathMissing(f"末级文件不存在 {last!r}") from err
        except OSError as err:
            # ELOOP = “末级是链接、O_NOFOLLOW 原子拒绝跟随”-> 安全排除链接。
            # 其余（EACCES 权限失败等）-> 真实失败，fail closed。
            if err.errno == errno.ELOOP:
                raise ProjectPathExcludedLink(f"末级是链接被排除 {last!r}") from err
            raise ProjectPathError(f"文件组件不可用 {last!r} ({err.strerror})") from err
        fst = os.fstat(file_fd)
        if not _stat.S_ISREG(fst.st_mode):
            os.close(file_fd)
            raise ProjectPathError(f"组件不是普通文件: {last!r}")
        identity = OpenIdentity.from_fd(
            file_fd, str(Path(root_label) / "/".join(rel_components))
        )
        # 祖先 fd 全部保留在 held 中（防止 rename/delete 换指），读取期间不释放。
        return OpenedFile(fd=file_fd, identity_before=identity, _release=release)
    except BaseException:
        release()
        raise


def _open_secure_posix(root: Path, rel_components: tuple[str, ...]) -> OpenedFile:
    o_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_dir = getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open(os.fspath(root), o_flags | o_dir | o_nofollow)
    # borrowed_root=False：release 时关闭 root_fd。
    return _walk_posix_from_fd(
        root_fd, os.fspath(root), rel_components, borrowed_root=False
    )


def _open_secure_windows(root: Path, rel_components: tuple[str, ...]) -> OpenedFile:
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    # 故意**不含 FILE_SHARE_DELETE**：打开期间文件/祖先目录不可被 rename/delete
    # 换指，这才是真正的“句柄锚定”——否则 share-delete 下路径仍可被换走。
    FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_DEVICE = 0x40
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    VOLUME_NAME_DOS = 0
    FILE_NAME_NORMALIZED = 0

    kernel32 = ctypes.windll.kernel32

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FILETIME),
            ("ftLastAccessTime", FILETIME),
            ("ftLastWriteTime", FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    # 用 WINFUNCTYPE(use_last_error=True) 定义 CreateFileW：失败后 ctypes 才可靠
    # 保留 Win32 last-error，供 get_last_error() 判定末级缺失等精确原因。
    _CFW = ctypes.WINFUNCTYPE(
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        use_last_error=True,
    )
    CreateFileW = _CFW(("CreateFileW", kernel32))
    GetFileInformationByHandle = kernel32.GetFileInformationByHandle
    GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
    GetFileInformationByHandle.restype = wintypes.BOOL
    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL
    GetFinalPathNameByHandleW = kernel32.GetFinalPathNameByHandleW
    GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                          wintypes.DWORD, wintypes.DWORD]
    GetFinalPathNameByHandleW.restype = wintypes.DWORD

    def final_path_win32(handle) -> str:
        """把句柄解析回其最终（已解析链接的）规范化文件路径，用于越界核验。"""
        buf = ctypes.create_unicode_buffer(32768)
        n = GetFinalPathNameByHandleW(handle, buf, len(buf), FILE_NAME_NORMALIZED)
        if n == 0 or n >= len(buf):
            raise ProjectPathError("GetFinalPathNameByHandle 失败")
        return buf.value

    def open_handle(path: str, is_dir: bool, leaf: bool = False):
        flags = FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if is_dir else 0)
        handle = CreateFileW(
            path, GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, flags, None)
        if handle == INVALID_HANDLE_VALUE or not handle:
            from ctypes import get_last_error
            err = get_last_error()
            # 末级 CreateFileW 失败：ERROR_FILE_NOT_FOUND(2)/ERROR_PATH_NOT_FOUND(3)
            # 可证明末级对象缺失（良性跳过）；祖先早已在上一步依次打开成功，此处
            # 无法到达的祖先缺失不适用。其余（EACCES=5 等）为真实失败，fail closed。
            if leaf and err in (2, 3):
                raise ProjectPathMissing(f"末级对象不存在 {path!r} (err={err})")
            raise ProjectPathError(f"CreateFileW 失败: {path} (err={err})")
        return handle

    held: list[int] = []

    def release() -> None:
        while held:
            CloseHandle(held.pop())

    def info(handle) -> BY_HANDLE_FILE_INFORMATION:
        buf = BY_HANDLE_FILE_INFORMATION()
        if not GetFileInformationByHandle(handle, ctypes.byref(buf)):
            raise ProjectPathError("GetFileInformationByHandle 失败")
        return buf

    _PREFIX = "\\\\?\\"

    def _norm(p: str) -> str:
        return p[len(_PREFIX):].rstrip("\\/") if p.startswith(_PREFIX) else p.rstrip("\\/")

    try:
        root_path = os.fspath(root)
        root_handle = open_handle(root_path, is_dir=True)
        held.append(root_handle)
        ri = info(root_handle)
        if not (ri.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY):
            raise ProjectPathError(f"根目录不是目录: {root}")
        if ri.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise ProjectPathError(f"根目录是 reparse 点: {root}")
        # 记录“真实根”的规范化最终路径，供最终文件做越界核验（锚定）。
        real_root = _norm(final_path_win32(root_handle)).lower()

        cur = root_path
        # 逐组件打开并 pin 祖先目录句柄（防中途 rename/delete 换指）。
        for comp in rel_components[:-1]:
            cur = cur.rstrip("\\/") + "\\" + comp
            h = open_handle(cur, is_dir=True)
            held.append(h)
            ci = info(h)
            if not (ci.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY):
                raise ProjectPathError(f"组件不是目录: {comp!r}")
            if ci.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
                raise ProjectPathError(f"组件是 reparse 点: {comp!r}")
        cur = cur.rstrip("\\/") + "\\" + rel_components[-1]
        # 文件用 OPEN_REPARSE_POINT 防跟随；末级缺失(ENOENT)归为可跳过 Missing。
        file_handle = open_handle(cur, is_dir=False, leaf=True)
        fi = info(file_handle)
        attrs = fi.dwFileAttributes
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            # 末级是被拒的链接/reparse（含 junction）——安全排除链接，可跳过。
            CloseHandle(file_handle)
            raise ProjectPathExcludedLink(f"末级是链接/reparse 被排除: {rel_components[-1]!r}")
        if attrs & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_DEVICE):
            CloseHandle(file_handle)
            raise ProjectPathError(f"文件组件不是普通文件: {rel_components[-1]!r}")
        # 越界核验：打开的文件句柄解析出的最终路径必须在“真实根”之下；
        # 配合不含 FILE_SHARE_DELETE 的锚定句柄，杜绝被换到根外。
        file_final = _norm(final_path_win32(file_handle)).lower()
        if not (file_final == real_root or file_final.startswith(real_root + "\\")):
            CloseHandle(file_handle)
            raise ProjectPathError(f"文件解析到授权根之外: {rel_components[-1]!r}")
        # 用 msvcrt 转为 fd 供 os.fdopen/fstat（只读）。
        import msvcrt
        fd = msvcrt.open_osfhandle(file_handle, os.O_RDONLY)
        identity = OpenIdentity.from_fd(fd, str(root / "/".join(rel_components)))
        # file_handle 的所有权转给 fd；祖先仍 pin 在 held。
        return OpenedFile(fd=fd, identity_before=identity, _release=release)
    except BaseException:
        release()
        raise


# ============ 授权根 -> 相对子目录 的目录锚定 helper（防 junction 逃逸） ============
#
# 解决 Windows 授权根真实 junction 逃逸：`make_root_binding` 过去只判
# `root.is_symlink()`（漏 junction），`_reject_link_leading` 只判 S_IFLNK（也漏
# junction），于是把 ``authorized\\linked\\sub`` 这种经 junction 祖先解析到根外的
# 目录当作受信任“源根”，随后 open 读到 outside 字节。
#
# 这里提供**不可仅 lstat 校验后返回 Path** 的锚定 helper：从授权根开始，用 pin 的
# 目录句柄逐组件打开并核验“是目录且非 reparse/junction”，任何组件是 junction 即
# fail closed；源根目录句柄被 pin（Windows 无 FILE_SHARE_DELETE / POSIX dir_fd），
# 到实际 open 期间换祖先也无法逃逸——文件打开要么经锚定的源根句柄（POSIX dir_fd
# 相对），要么经“源根已被 pin、路径不可换”的受控打开（Windows，逐组件再核验
# reparse + 最终路径在授权根之下）。
#
# Q2/index 用法（精确契约，见下方 RootAnchor docstring）：用 `secure_source_root`
# 拿到锚定，把锚定传入 `scan_project(anchor, ...)`（scan_project 接受 RootAnchor），
# 不要再走会 escape 的 `resolve_source_root -> Path -> scan_project(Path)`。


@dataclass
class RootAnchor:
    """授权根 + 相对子目录的**安全目录锚定**（句柄层，防 junction 祖先逃逸）。

    生命周期契约（**close 后用任何成员都会抛 ProjectPathError，绝不复活**）：
    - 由 ``secure_source_root(authorized_root, rel_components)`` 创建：**从文件系统
      根（Windows 盘符根 / POSIX ``/``）按组件**逐级用受限句柄打开并核验“是目录且
      非符号链接/reparse”，把 **root 自身及其每一个祖先**、以及相对子目录每一级
      全部 pin（Windows 无 FILE_SHARE_DELETE；POSIX 保留 dir_fd 链）。任一组件是
      junction/mount-point/reparse/symlink 立即抛 ``ProjectPathError``（fail
      closed）；任一组件无法核验（句柄/信息失败）同样抛（fail closed），**绝不把
      “无法验证”当“安全”**。
    - 持有期间所有访问（``open_file`` / ``children``）都经由被 pin 的句柄链完成，
      绝不按未 pin 的路径重走源根——路径可被换，而 pin 句柄链不可。Windows 链从盘符
      根即无 FILE_SHARE_DELETE，整链路径在持有期间不可被 rename/delete 换指；
      POSIX 因目录 fd 不禁止 rename，故用 dir_fd 相对打开 + 目录 fd 枚举，保证
      遍历、ignore、读取落在**同一颗 pin 的树**（不做 FS 原子快照；窗口内真实内容
      变更可用同 FD 身份核验 fail closed，但不能读外树/混规则）。
    - ``root_fingerprint()`` 基于 **pin 身份**（Windows：被 pin 源根的规范化最终
      路径；POSIX：被 pin 源根 dirfd 的 dev/ino）+ **稳定声明路径**，不每次按路径
      stat（路径可被换，pin 身份不可）。
    - 用 ``with anchor:`` 或显式 ``anchor.close()`` 释放全部 pin 句柄；``close``
      幂等，close 后再次调用无害，但任何其它成员调用抛 ``ProjectPathError``。

    Q2/index 集成契约（**后续只复用，不重写本安全层**）：
    - ``anchor = secure_source_root(authorized_root, rel_components)``
      （authorized_root 应来自配置 PROJECT_ALLOWED_ROOTS[root_key]；rel 为
      split_relative_path 的组件元组。）
    - 把 ``anchor`` 传给 ``scan_project(anchor, store, limits, ...)`` 作为 root
      参数：**ignore 装载与目录遍历也接锚定上下文**（经 ``anchor.children`` /
      ``anchor.open_file``），不再是“只有文件摄取用 anchor、遍历/规则仍按 Path”。
    - ``anchor.source_root_path`` / ``anchor.authorized_root`` 仅供元数据/报告；
      ``anchor.root_fingerprint()`` 是稳定源根身份指纹（换树换绑）。
    - 完成后 ``anchor.close()``（或用 with 块自动释放）。
    不要：把它当普通 Path 返回给调用方去自由 join/open；它不是 lstat 校验后的
    Path，而是带 pin 句柄链的锚定。
    """

    authorized_root: Path
    source_root_path: Path
    rel_components: tuple[str, ...]
    # 平台句柄链（含文件系统根、root 祖先、root 自身、相对子目录各级；源根是末个）。
    _pins: list = None
    _source_pin: object = None  # Windows：源根 HANDLE；POSIX：源根 dirfd
    _real_source: str = ""  # Windows：被 pin 源根的规范化最终路径（小写，\\?\ 去除），用于越界核验
    _w32_identity: tuple = ()  # Windows：(dwVolumeSerialNumber, nFileIndexHigh, nFileIndexLow) of pinned source handle ＝pin 身份
    _posix_identity: tuple = ()  # POSIX：(dev, ino) of pinned source dirfd ＝pin 身份
    _declared: str = ""  # 稳定声明路径（源根的声明路径文本）
    _closed: bool = False

    def _ensure_alive(self):
        if self._closed:
            raise ProjectPathError("RootAnchor 已 close，任何成员调用都会 fail closed")

    def open_file(self, rel_components: tuple[str, ...]) -> OpenedFile:
        """在锚定的源根之下安全打开普通文件（防 junction 祖先逃逸）。

        - 先校验原始组件（不假设已 split）；已 close 则抛。
        - POSIX：逐组件 dir_fd + O_NOFOLLOW，纯句柄相对，绝不在 pin 树之外重走路径。
        - Windows：整链从盘符根已被 pin（无 FILE_SHARE_DELETE，路径不可换），复用
          逐组件 reparse 拒绝 + 最终路径在授权根之下核验的安全打开。
        """
        self._ensure_alive()
        if not rel_components:
            raise ProjectPathError("相对路径为空（open_file 需要一个普通文件）")
        _validate_raw_components(rel_components)
        if IS_WINDOWS:
            return _open_secure_windows(self.source_root_path, rel_components)
        return _walk_posix_from_fd(
            int(self._source_pin), self._declared, rel_components, borrowed_root=True,
        )

    def children(
        self, rel_dir: tuple[str, ...]
    ) -> "Iterable[tuple[str, bool, bool, bool, int]]":
        """枚举锚定源根下 ``rel_dir`` 的直接子项，逐条产出 ``(name, is_dir, is_file,
        is_link_or_reparse, st_size)``。

        ``st_size`` 为不跟随链接的 lstat/属性字节数（供扫描器对“过大文件”作
        fail-closed 前的硬排除）。**受控流式**：返回惰性迭代器，一次只物化/产出
        一条子项，调用方（ignore 装载 / 扫描）在循环里逐条目核 deadline/条目预算，
        因此“宽目录未先整列”才真正成立（不再先耗费整目录再计数）。目录句柄由
        生成器自持、在迭代结束/close/异常时于 ``finally`` 释放。

        Windows：枚举期间**逐组件打开并核验** ``rel_dir`` 的每一级祖先（必须“是目录
        且非 reparse/junction”、持有受限句柄直至枚举结束，任一组件是 junction 立即
        fail closed），再从被 pin 的叶句柄（fd）scandir——子目录中途被换成 junction
        也无法把枚举重定向到根外。POSIX：在被 pin 源根 dirfd 下逐组件 dirfd 进入并
        fd-based scandir，同样流式。枚举失败抛 ``ProjectPathError``（fail closed）。

        已 close（或迭代开始前 close）的锚调用本方法抛 ``ProjectPathError``，绝不复活。
        """
        self._ensure_alive()
        _validate_raw_components(rel_dir)
        if IS_WINDOWS:
            return _children_windows(self, rel_dir)
        return _children_posix(self, rel_dir)

    def root_fingerprint(self) -> str:
        """源根稳定身份指纹 = pin 身份 + 稳定声明路径；换树必须显式换绑。

        不使用“每次按 source_root_path stat”方案（路径可被换；pin 身份不可）。
        Windows 的 pin 身份是**被 pin 源根句柄**经 GetFileInformationByHandle 解析出的
        真实对象身份（卷序列号 + 文件索引，见 ``_w32_identity``），不是路径字符串——
        同一路径被换树（rename 旧 -> 同路径重建）会得到不同的文件索引，指纹随之变化，
        从而“换树必须换绑”成立。POSIX 同理用被 pin 源根 dirfd 的 dev/ino。
        """
        self._ensure_alive()
        if IS_WINDOWS:
            if not self._w32_identity:
                raise ProjectPathError("Windows pin 身份缺失，无法计算指纹（fail closed）")
            pin_id = (
                f"{self._w32_identity[0]}:"
                f"{self._w32_identity[1]}:{self._w32_identity[2]}"
            )
        else:
            pin_id = f"{self._posix_identity[0]}:{self._posix_identity[1]}"
        payload = f"{pin_id}\0{self._declared}"
        return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()

    def __enter__(self) -> "RootAnchor":
        self._ensure_alive()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """幂等释放全部 pin 句柄；close 后所有成员调用抛 ProjectPathError。"""
        if self._closed:
            return
        if IS_WINDOWS:
            _win32().CloseHandle_(self._pins)
        else:
            for fd in self._pins or []:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._pins = []
        self._source_pin = None
        self._closed = True


def _children_posix(
    anchor: RootAnchor, rel_dir: tuple[str, ...]
) -> "Iterable[tuple[str, bool, bool, bool, int]]":
    """POSIX：在被 pin 源根 dirfd 下逐组件 dirfd 进入，fd-based scandir 流式产出。

    受控流式：用 ``os.scandir(fd)``（而非 ``os.listdir``，那会整目录物化），逐条
    ``yield``，调用方循环内再核 deadline/条目预算。临时 dirfd 链在迭代结束/close/
    异常时于 ``finally`` 释放。源根 dirfd 由 anchor 持有（借用，不在此关闭）。
    """
    o_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_dir = getattr(os, "O_DIRECTORY", 0)
    anchor._ensure_alive()
    chain: list[int] = [int(anchor._source_pin)]  # borrowed_source：source pin 由 anchor 持有
    try:
        parent = chain[0]
        for comp in rel_dir:
            try:
                child = os.open(comp, o_flags | o_dir | o_nofollow, dir_fd=parent)
            except OSError as err:
                raise ProjectPathError(f"目录不可枚举(祖先/组件不可用): {comp!r} ({err.strerror})") from err
            chain.append(child)
            parent = child
        try:
            it = os.scandir(parent)  # fd-based：遵循被 pin 的 inode，非路径重解析
        except OSError as err:
            raise ProjectPathError(f"目录读取失败 {rel_dir} ({err.strerror})") from err
        with it:
            for e in it:
                try:
                    lst = e.stat(follow_symlinks=False)
                except OSError as err:
                    raise ProjectPathError(f"子项状态读取失败 {e.name!r} ({err.strerror})") from err
                yield (
                    e.name,
                    _stat.S_ISDIR(lst.st_mode),
                    _stat.S_ISREG(lst.st_mode),
                    _stat.S_ISLNK(lst.st_mode),
                    int(lst.st_size),
                )
    finally:
        for fd in chain[1:]:
            try:
                os.close(fd)
            except OSError:
                pass


def _children_windows(
    anchor: RootAnchor, rel_dir: tuple[str, ...]
) -> "Iterable[tuple[str, bool, bool, bool, int]]":
    """Windows：枚举期间**逐组件核验并持有** ``rel_dir`` 的每一级祖先。

    源根链本身已被 anchor 从头 pin（无 FILE_SHARE_DELETE，源根及更上不可改名），
    但**相对子目录（rel_dir 的组件）不在 anchor 的 pin 链里**——若只按声明路径
    join+scandir，根下子目录被换成 junction 就会读到根外。故这里在枚举的整个生命
    周期内：对 rel_dir 每一级用 ``open_handle(OPEN_REPARSE_POINT, is_dir=True)``
    打开并核验“是目录且非 reparse/junction”、持有句柄；任一组件是 junction / 无法
    核验立即 ``ProjectPathError``（fail closed）。源根与 rel_dir 各级都被 pin 后，
    该不可替换路径不会再被 rename/delete 换指，故对其 ``os.scandir(str)`` 流式枚举
    不会逃逸。空 ``rel_dir`` 时直接借用 anchor 已 pin 的源根路径（不创建、不关闭
    anchor 拥有的句柄）。受控流式逐条 ``yield``；临时句柄在迭代结束/close/异常时于
    ``finally`` 释放。
    """
    api = _win32()
    anchor._ensure_alive()
    pins: list = []  # rel_dir 逐级祖先的临时 pin

    def release():
        api.CloseHandle_(pins)

    try:
        if rel_dir:
            cur = os.fspath(anchor.source_root_path)
            for comp in rel_dir:
                cur = cur.rstrip("\\/") + "\\" + comp
                h = api.open_handle(cur, is_dir=True)
                pins.append(h)
                ci = api.info(h)
                if not (ci.dwFileAttributes & api.FILE_ATTRIBUTE_DIRECTORY):
                    raise ProjectPathError(f"组件不是目录: {comp!r}")
                if ci.dwFileAttributes & api.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ProjectPathError(
                        f"组件是 reparse 点(junction等)，拒绝枚举: {comp!r}"
                    )
            leaf = cur
        else:
            # 空 rel_dir：源根本身已被 anchor pin，直接借用其不可替换路径。
            leaf = os.fspath(anchor.source_root_path)
        with os.scandir(leaf) as it:
            for e in it:
                try:
                    is_symlink = e.is_symlink()
                except OSError as err:
                    raise ProjectPathError(f"子项符号链接判定失败 {e.name!r} ({err.strerror})") from err
                try:
                    st = e.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                except OSError as err:
                    raise ProjectPathError(f"子项状态读取失败 {e.name!r} ({err.strerror})") from err
                is_reparse = bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                    is_file = e.is_file(follow_symlinks=False)
                except OSError as err:
                    raise ProjectPathError(f"子项类型判定失败 {e.name!r} ({err.strerror})") from err
                yield (e.name, is_dir, is_file, is_symlink or is_reparse,
                       int(getattr(st, "st_size", 0)))
    except ProjectPathError:
        raise
    except OSError as err:
        raise ProjectPathError(f"目录读取失败 {leaf} ({err.strerror})") from err
    finally:
        release()


def secure_source_root(
    authorized_root: Path, rel_components: tuple[str, ...]
) -> RootAnchor:
    """把“授权根 + 客户端相对子目录”解析为**锚定的源根**（句柄层，防 junction）。

    从文件系统根（Windows 盘符根 / POSIX ``/``）按组件逐级核验并 pin：**root 自身
    及其每一个祖先**、以及相对子目录每一级都必须“是目录且非符号链接/reparse”，
    任何组件是 junction/mount-point/reparse/symlink、或无法核验，立即抛
    ``ProjectPathError``（fail closed）——绝不把 junction 祖先下的普通子目录升为
    可信根。这是 ``make_root_binding`` + ``resolve_source_root`` 那条只 lstat 校验、
    会被 Windows junction 祖先逃逸的路线的安全替代。
    """
    authorized_root = Path(authorized_root)
    _validate_raw_components(rel_components)
    if IS_WINDOWS:
        return _anchor_windows(authorized_root, rel_components)
    return _anchor_posix(authorized_root, rel_components)


def _win32():
    """Windows ctypes 绑定 + 打开辅助（惰性、单例、仅 IS_WINDOWS 时可用）。"""
    return _Win32Load()


class _Win32Load:
    """Windows: CreateFileW(OPEN_REPARSE_POINT)/CloseHandle/Info/FinalPath 封装。"""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.GENERIC_READ = 0x80000000
        self.FILE_SHARE_READ, self.FILE_SHARE_WRITE = 1, 2  # 故意无 FILE_SHARE_DELETE
        self.OPEN_EXISTING = 3
        self.FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        self.FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
        self.FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
        self.FILE_ATTRIBUTE_DIRECTORY = 0x10
        self.FILE_ATTRIBUTE_DEVICE = 0x40
        self.FILE_NAME_NORMALIZED = 0
        kernel32 = ctypes.windll.kernel32

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD),
                        ("dwHighDateTime", wintypes.DWORD)]

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", FILETIME),
                ("ftLastAccessTime", FILETIME),
                ("ftLastWriteTime", FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        _CFW = ctypes.WINFUNCTYPE(
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            use_last_error=True,
        )
        self.CreateFileW = _CFW(("CreateFileW", kernel32))

        self.GetFileInformationByHandle = kernel32.GetFileInformationByHandle
        self.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
        self.GetFileInformationByHandle.restype = wintypes.BOOL

        self.CloseHandle = kernel32.CloseHandle
        self.CloseHandle.argtypes = [wintypes.HANDLE]
        self.CloseHandle.restype = wintypes.BOOL

        self.GetFinalPathNameByHandleW = kernel32.GetFinalPathNameByHandleW
        self.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        self.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self._BY_HANDLE = BY_HANDLE_FILE_INFORMATION

    def open_handle(self, path: str, is_dir: bool, leaf: bool = False):
        import ctypes
        flags = self.FILE_FLAG_OPEN_REPARSE_POINT | (
            self.FILE_FLAG_BACKUP_SEMANTICS if is_dir else 0)
        handle = self.CreateFileW(
            path, self.GENERIC_READ,
            self.FILE_SHARE_READ | self.FILE_SHARE_WRITE,
            None, self.OPEN_EXISTING, flags, None)
        if handle == ctypes.c_void_p(-1).value or not handle:
            from ctypes import get_last_error
            err = get_last_error()
            if leaf and err in (2, 3):  # 末级对象缺失（良性跳过）
                raise ProjectPathMissing(f"末级对象不存在 {path!r} (err={err})")
            raise ProjectPathError(f"CreateFileW 失败: {path} (err={err})")
        return handle

    def info(self, handle):
        import ctypes
        buf = self._BY_HANDLE()
        if not self.GetFileInformationByHandle(handle, ctypes.byref(buf)):
            raise ProjectPathError("GetFileInformationByHandle 失败")
        return buf

    def final_path_win32(self, handle) -> str:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = self.GetFinalPathNameByHandleW(
            handle, buf, len(buf), self.FILE_NAME_NORMALIZED)
        if n == 0 or n >= len(buf):
            raise ProjectPathError("GetFinalPathNameByHandle 失败")
        return buf.value

    def _norm(self, p: str) -> str:
        PREFIX = "\\\\?\\"
        return p[len(PREFIX):].rstrip("\\/") if p.startswith(PREFIX) else p.rstrip("\\/")

    def CloseHandle_(self, handles: list) -> None:
        while handles:
            try:
                self.CloseHandle(handles.pop())
            except Exception:
                pass


def _anchor_windows(authorized_root: Path, rel_components: tuple[str, ...]) -> RootAnchor:
    """Windows：从盘符根按组件 OPEN_REPARSE_POINT 核验根及其祖先并 pin，防 junction 祖先逃逸。"""
    api = _win32()
    pins: list = []

    def release():
        api.CloseHandle_(pins)

    try:
        root_s = os.fspath(authorized_root).rstrip("\\/")
        if root_s.startswith("\\\\"):
            raise ProjectPathError("授权根是 Windows UNC 网络路径，拒绝锚定")
        m = re.match(r"^([A-Za-z]:)(\\|$)", root_s)
        if not m:
            raise ProjectPathError(f"授权根不是本机盘符绝对路径: {root_s!r}")
        drive = m.group(1) + "\\"
        # 文件系统根也 pin（验证不是卷挂载/reparse）。
        drive_handle = api.open_handle(drive, is_dir=True)
        pins.append(drive_handle)
        di = api.info(drive_handle)
        if di.dwFileAttributes & api.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ProjectPathError(f"文件系统根是 reparse 点(卷挂载/junction)，拒绝锚定: {drive}")

        cur = drive
        # 授权根的每一级祖先：共组件解析 + 核验“目录且非 reparse”+ pin。
        for comp in [p for p in root_s[len(drive):].split("\\") if p]:
            cur = cur.rstrip("\\/") + "\\" + comp
            h = api.open_handle(cur, is_dir=True)
            pins.append(h)
            ci = api.info(h)
            if not (ci.dwFileAttributes & api.FILE_ATTRIBUTE_DIRECTORY):
                raise ProjectPathError(f"授权根祖先不是目录: {comp!r}")
            if ci.dwFileAttributes & api.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ProjectPathError(
                    f"授权根祖先/组件是 reparse 点(junction等)，拒绝锚定: {comp!r}"
                )
        real_root = api._norm(api.final_path_win32(pins[-1])).lower()

        # 相对子目录每一级：解析 + 核验 + pin。
        for comp in rel_components:
            cur = cur.rstrip("\\/") + "\\" + comp
            h = api.open_handle(cur, is_dir=True)
            pins.append(h)
            ci = api.info(h)
            if not (ci.dwFileAttributes & api.FILE_ATTRIBUTE_DIRECTORY):
                raise ProjectPathError(f"相对子目录组件不是目录: {comp!r}")
            if ci.dwFileAttributes & api.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ProjectPathError(
                    f"相对子目录组件是 reparse 点(junction等)，拒绝锚定: {comp!r}"
                )
        source_handle = pins[-1]
        real_source = api._norm(api.final_path_win32(source_handle)).lower()
        if not (real_source == real_root or real_source.startswith(real_root + "\\")):
            raise ProjectPathError(f"源根解析到授权根之外: {authorized_root}")
        si = api.info(source_handle)
        w32_identity = (
            int(si.dwVolumeSerialNumber),
            int(si.nFileIndexHigh),
            int(si.nFileIndexLow),
        )
        return RootAnchor(
            authorized_root=authorized_root,
            source_root_path=Path(cur),
            rel_components=rel_components,
            _pins=pins,
            _source_pin=source_handle,
            _real_source=real_source,
            _w32_identity=w32_identity,
            _declared=str(Path(cur)),
        )
    except BaseException:
        release()
        raise


def _anchor_posix(authorized_root: Path, rel_components: tuple[str, ...]) -> RootAnchor:
    """POSIX：从 ``/`` 按组件 dir_fd + O_NOFOLLOW 核验根及其祖先、相对子目录并 pin。

    O_NOFOLLOW 原子拒绝符号链接组件；根祖先（到 ``/``）每个组件都被 pin 成 dir_fd，
    保证后续枚举（fd-based listdir）与读取（dir_fd 相对 open）落在同一颗 pin 的树。
    """
    o_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_dir = getattr(os, "O_DIRECTORY", 0)
    pins: list[int] = []

    def release():
        while pins:
            try:
                os.close(pins.pop())
            except OSError:
                pass

    try:
        root_s = os.fspath(authorized_root)
        if not root_s.startswith("/"):
            raise ProjectPathError(f"授权根不是绝对路径: {root_s!r}")
        root_fd = os.open("/", o_flags | o_dir | o_nofollow)
        pins.append(root_fd)
        st = os.fstat(root_fd)
        if not _stat.S_ISDIR(st.st_mode):
            raise ProjectPathError("文件系统根不是目录: /")
        parent = root_fd
        for comp in [c for c in root_s.split("/") if c]:
            try:
                child = os.open(comp, o_flags | o_dir | o_nofollow, dir_fd=parent)
            except OSError as err:
                raise ProjectPathError(f"授权根祖先/组件不可用: {comp!r} ({err.strerror})") from err
            pins.append(child)
            cst = os.fstat(child)
            if not _stat.S_ISDIR(cst.st_mode):
                raise ProjectPathError(f"授权根祖先不是目录: {comp!r}")
            parent = child
        for comp in rel_components:
            try:
                child = os.open(comp, o_flags | o_dir | o_nofollow, dir_fd=parent)
            except OSError as err:
                raise ProjectPathError(f"相对子目录组件不可用: {comp!r} ({err.strerror})") from err
            pins.append(child)
            cst = os.fstat(child)
            if not _stat.S_ISDIR(cst.st_mode):
                raise ProjectPathError(f"相对子目录组件不是目录: {comp!r}")
            parent = child
        declared = str(Path(root_s) / "/".join(rel_components))
        src_st = os.fstat(parent)
        return RootAnchor(
            authorized_root=authorized_root,
            source_root_path=Path(declared),
            rel_components=rel_components,
            _pins=pins,
            _source_pin=parent,
            _posix_identity=(src_st.st_dev, src_st.st_ino),
            _declared=declared,
        )
    except BaseException:
        release()
        raise
