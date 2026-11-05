"""不可变 blob 存储：内容寻址、原子 no-clobber 发布、并发同哈希安全、
禁止覆盖差异字节、只清理自有 partial。

安全要点（对照主代理初稿审查）：
- 只写入系统拥有的受控目录（``store_root``）；绝不写入/删除授权原目录。
- 从可信根按组件受限打开（见 project_paths.open_secure_regular），对**同一已
  pin FD** 流式复制到受控临时 partial，读取前后都对该 FD 做 fstat 核对，
  变化即 fail closed；哈希/解析都以**blob 字节**（就是该 FD 读到的字节）为准，
  绝不重新 open 源路径。
- 内容寻址，最终文件名 = sha256。发布用 ``os.link`` 原子 no-clobber；
  已存在则核验既有，绝不覆盖不同字节。并发同哈希安全（内容相同，any 胜出）。
- ``cleanup_partials`` 只清理**本实例创建**的 partial，不做全局 GC，绝不删别的
  worker 的活跃 partial。
- ``verify_blob`` 先校验 64-hex 键，以普通无链接 FD 打开，并按 expected_size
  有界逐块哈希（不无界读被换成巨大 blob）。
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from app.services.project_paths import (
    ProjectPathError,
    RootAnchor,
    open_secure_regular,
)
from app.services.project_types import BlobRef

STREAM_BLOCK = 4 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class HardlinkRejected(ProjectPathError):
    """同一核验 FD 上判定为硬链接（nlink>1）而被拒：内容绝不写入 blob。

    与普通 ProjectPathError 区分，供扫描器把它作为"排除该项"处理（而非让整次
    扫描 fail closed）——但前提是**在该 FD 读取/发布之前**就拒绝，根外硬链内容
    不会持久化进 store。
    """


class BlobStore:
    def __init__(self, store_root: Path):
        self.store_root = Path(store_root)
        self.store_root.mkdir(parents=True, exist_ok=True)
        # 本实例创建过的 partial 名集合，静默清理由实例内记录扩散限制：
        # 我们用“仅删本实例构造路径中的 partial”策略，尽量不依赖跨进程全局。
        self._instance_nonce = f"{os.getpid()}.{uuid.uuid4().hex}"

    def _blob_path(self, sha256: str) -> Path:
        return self.store_root / sha256

    def _new_partial(self, tag: str) -> Path:
        """每次写入/摄取操作一个**随机唯一** partial 名。

        绝不共用固定 tag 名：同实例并发操作若共用同名 partial，会在 "xb" 上互相
        FileExists 冲突，异常路径又会 unlink 别人正在写的 partial。唯一名保证
        ``_safe_unlink`` 只删本操作实际创建的那份。
        """
        return self.store_root / f".{self._instance_nonce}.{tag}.{uuid.uuid4().hex}.partial"

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _validate_key(sha256: str) -> None:
        if not _SHA256_RE.fullmatch(sha256):
            raise ProjectPathError(f"非法的 blob 键: {sha256!r}")

    def verify_blob(self, sha256: str, expected_size: int) -> bool:
        """按 64-hex 键 + expected_size 有界核验既有 blob；不匹配返回 False。

        打开走与摄取相同的安全打开（自 store_root 按组件、拒符号链接/reparse，
        同 FD 锚定），**不做** lstat→os.open（Windows 下无真实 no-reparse）。
        """
        self._validate_key(sha256)
        blob = self._blob_path(sha256)
        if not blob.exists():
            return False
        digest = hashlib.sha256()
        size = 0
        overflow = False
        try:
            opened = open_secure_regular(self.store_root, (blob.name,))
        except ProjectPathError:
            return False
        try:
            with os.fdopen(opened.fd, "rb", closefd=False) as fh:
                while size < expected_size:
                    block = fh.read(min(STREAM_BLOCK, expected_size - size))
                    if not block:
                        break
                    size += len(block)
                    digest.update(block)
                # 有界：只需在 expected_size 后再读一字节判断“被换大成大 blob”。
                if fh.read(1):
                    overflow = True
        except OSError:
            return False
        finally:
            opened.close()
        if overflow or size != expected_size:
            return False
        return digest.hexdigest() == sha256.lower()

    def read_blob_bytes(self, sha256: str, expected_size: int) -> bytes:
        """内容寻址读取：同安全 FD 有界读取并返回**已验证的原始字节**。

        供上层（如 index 子进程）直接取得 blob 内容用于解析/哈希，**不必再按
        路径重新 open 源文件**——读取自 store_root 经逐组件安全打开（拒符号
        链接/reparse、同 FD 锚定）的同 pin FD。全程安全属性与 ``verify_blob``
        /``ingest_verified_file`` 一致：
        - 有界：只读到 ``expected_size`` 再多读一字节探测“被换成更大的 blob”；
        - 读取前后对**同一 FD** fstat 核对（不按路径重开），变化即 fail closed；
        - 内容 SHA-256 与 64-hex 键一致才返回；任何不符抛 ``ProjectPathError``。
        返回的 bytes 即为该 FD 实际读到的、已核验的内容。
        """
        self._validate_key(sha256)
        blob = self._blob_path(sha256)
        opened = open_secure_regular(self.store_root, (blob.name,))
        try:
            before = opened.identity_before
            size = 0
            parts: list[bytes] = []
            with os.fdopen(opened.fd, "rb", closefd=False) as fh:
                while size < expected_size:
                    block = fh.read(min(STREAM_BLOCK, expected_size - size))
                    if not block:
                        break
                    size += len(block)
                    parts.append(block)
                # 有界探测：多读一字节，若还有内容则说明被换成了更大 blob。
                if fh.read(1):
                    raise ProjectPathError(
                        f"blob 超过预期大小: {sha256[:12]}…"
                    )
            after = opened.stat_after()
            if not before.matches(after):
                raise ProjectPathError(f"blob 读取期间发生变化: {sha256[:12]}…")
        finally:
            opened.close()
        if size != expected_size:
            raise ProjectPathError(
                f"blob 长度不符: {sha256[:12]}… (期望 {expected_size} 实得 {size})"
            )
        data = b"".join(parts)
        if hashlib.sha256(data).hexdigest() != sha256.lower():
            raise ProjectPathError(f"blob 哈希不符: {sha256[:12]}…")
        return data

    def write_bytes(self, data: bytes) -> BlobRef:
        """把调用方已拥有的字节写入不可变 blob（内容寻址）。"""
        sha256 = hashlib.sha256(data).hexdigest()
        final = self._blob_path(sha256)
        if final.exists():
            if not self.verify_blob(sha256, len(data)):
                raise ProjectPathError(f"拒绝覆盖已有不同字节 blob={sha256[:12]}…")
            return BlobRef(sha256=sha256, size=len(data))
        partial = self._new_partial(sha256)
        try:
            with partial.open("xb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as err:
            self._safe_unlink(partial)
            raise ProjectPathError(f"写临时 blob 失败 ({err.strerror})") from err
        self._publish_noclobber(partial, final, sha256, len(data))
        return BlobRef(sha256=sha256, size=len(data))

    def _publish_noclobber(self, partial: Path, final: Path, sha256: str, size: int) -> None:
        """原子 no-clobber 发布：os.link 到 final；已存在则核验既有并清理自己的 partial。"""
        try:
            os.link(os.fspath(partial), os.fspath(final))
        except FileExistsError:
            self._safe_unlink(partial)
            if not self.verify_blob(sha256, size):
                raise ProjectPathError(f"并发既有 blob 不一致 sha256={sha256[:12]}…")
            return
        except OSError as err:
            self._safe_unlink(partial)
            raise ProjectPathError(f"发布 blob 失败 ({err.strerror})") from err
        self._safe_unlink(partial)

    def ingest_verified_file(
        self,
        root: Path | RootAnchor,
        rel_components: tuple[str, ...],
        max_bytes: int,
        reject_hardlinks: bool = True,
    ) -> tuple[BlobRef, object]:
        """从可信根按组件打开普通文件，同一 FD 流式复制 + 前后 fstat 核对。

        ``root`` 可为 ``Path``（back-compat）或 ``RootAnchor``（锚定的源根）。
        传入 ``RootAnchor`` 时经 ``anchor.open_file`` 打开——源根被 pin（无
        FILE_SHARE_DELETE / dir_fd），逐组件拒 reparse/junction，防 Windows
        junction 祖先逃逸到“仍被当作可信根的根外目录”。

        返回 (BlobRef, opened) —— ``opened.identity_before`` 供上层使用；
        读取全程走同一 FD，绝不重开源。

        ``reject_hardlinks=True``：在同一核验 FD 上 **读取/发布之前** 就拒绝任何
        nlink>1 的文件（根内/根外硬链均拒），抛 ``HardlinkRejected``，其内容绝不
        写入 blob 目录。这修复“manifest 不收、但 blob 已把根外 hardlink 内容持久化
        保存”的泄漏。
        """
        if max_bytes < 0:
            raise ProjectPathError("单文件字节预算必须非负")
        if isinstance(root, RootAnchor):
            opened = root.open_file(rel_components)
        else:
            opened = open_secure_regular(root, rel_components)
        try:
            # 同一核验 FD 的 nlink 判定：先于读取/发布拒绝，才不会有落盘泄漏。
            if reject_hardlinks and opened.identity_before.st_nlink > 1:
                raise HardlinkRejected(f"硬链接拒绝: {rel_components[-1]!r} (nlink>1)")
            digest = hashlib.sha256()
            total = 0
            partial = self._new_partial("ingest")
        except BaseException:
            opened.close()
            raise
        try:
            with os.fdopen(opened.fd, "rb", closefd=False) as fh, partial.open("xb") as out:
                while block := fh.read(STREAM_BLOCK):
                    total += len(block)
                    if total > max_bytes:
                        raise ProjectPathError(
                            f"单文件超过字节预算 {max_bytes} ({total} 字节)"
                        )
                    digest.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            # 对同一 fd 做复制后的 fstat 核对（不按路径重新 stat）。
            after = opened.stat_after()
            if not opened.identity_before.matches(after):
                raise ProjectPathError(f"文件在复制期间发生变化: {rel_components[-1]!r}")
        except OSError as err:
            self._safe_unlink(partial)
            raise ProjectPathError(f"复制源文件失败 ({err.strerror})") from err
        except BaseException:
            self._safe_unlink(partial)
            raise
        finally:
            opened.close()
        sha256 = digest.hexdigest()
        final = self._blob_path(sha256)
        self._publish_noclobber(partial, final, sha256, total)
        return BlobRef(sha256=sha256, size=total), opened.identity_before

    def cleanup_partials(self) -> int:
        """只清理**本实例**创建的 partial（no-global-GC），绝不删别的 worker 的。

        partial 名形如 ``.{nonce}.{tag}.{uuid}.partial``，这里按本实例 nonce 前缀
        匹配——只清本实例实际创建的文件，绝不误删别的 worker / 别实例的活跃 partial。
        """
        removed = 0
        prefix = f".{self._instance_nonce}."
        for entry in self.store_root.iterdir():
            if entry.name.startswith(prefix) and entry.name.endswith(".partial"):
                if entry.is_file():
                    self._safe_unlink(entry)
                    removed += 1
        return removed
