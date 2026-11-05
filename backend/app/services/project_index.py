"""项目同步 / 分块 / 向量 / CAS 发布（Q 独占）。

职责：
- 指纹：parser/chunker/embedding/config 各自的确定性指纹（跨配置不得误复用）。
- 入队：行锁 + 幂等创建 pending 快照并修定 base_snapshot_id。
- 领取/租约/心跳/旧租约回收/取消/有限重试/协作停机（镜像 ingest.py）。
- 处理：先扫描+持久化完整 manifest 映射（P 已写不可变 blob），再解析+向量化。
- CAS 发布：全部映射文件 parse+vector ready && base==active 才切换 active。
- 向量：显式创建 collection、确定性 vector id、usage_scope='project_embedding'、
  persist_vectors 幂等 upsert，卷与进度在同一租约事务写。

安全关键实现（对照审查修订）：
- 绝不跨 DB 连接做网络/CPU 工作：先在短事务内读快照+源为纯值（含 collection_name
  等惰性加载字段），随后扫描/解析/嵌入全程不持有 DB 连接；写回一律开新短事务。
- 一致性锁序恒定：源行锁 → 快照行锁；写事务内用 ``populate_existing + FOR UPDATE``
  重核租约/状态/源未撤销（条件 UPDATE 失败即关闭，不覆盖更新租约）。
- CAS 发布是真实 DB 级条件写（在源行锁下，``UPDATE project_sources ... WHERE
  active_snapshot_id = :base``），不是“Python 比较 + ORM 赋值”的伪 CAS。
- 逐文件解析放入 owned 子进程并强制 deadline（超时杀进程），绝不把
  ``ParseResult.truncated`` / 2000 块上限静默当作 ready。
- blob 读取只从受控 BlobStore 按 sha256 键 verify+读，绝不按用户提供的
  ``blob_path`` 任意打开，也不静默 8MiB 截断。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat as _statmod
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langchain_core.documents import Document as LCDocument
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import (
    KNOWLEDGE_BASE_KIND_PROJECT,
    ProjectChunk,
    ProjectFileParseStatus,
    ProjectFileVersion,
    ProjectSnapshot,
    ProjectSnapshotFile,
    ProjectSnapshotStatus,
    ProjectSource,
    User,
)
from app.services import project_sources
from app.services.cloud_models import embedding_stop_scope
from app.services.project_blobs import BlobStore
from app.services.project_parser import (
    SourceSymbol,
    iter_bounded_line_chunks,
    parse_blob,
    read_blob_bounded,
)
from app.services.project_scanner import ProjectScanError, scan_project
from app.services.project_terms import code_terms, path_terms
from app.services.project_types import (
    BlobRef,
    ProjectScanLimits,
    ScanReport,
)
from app.services.usage_tracking import ModelCall, UsageContext, usage_scope
from app.services.vector_store import (
    EMBEDDING_DIM,
    get_embeddings,
    get_vector_store,
    persist_vectors,
)

logger = logging.getLogger(__name__)

EMPTY_HEX = "0" * 64

# 单文件最大分块数（硬界）：超过即该文件标注失败（不当作 ready 发布）。
_MAX_CHUNKS_PER_FILE = 2000


# ---------------- 指纹 ----------------

def _hex(parts: list[str]) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8", "surrogatepass")).hexdigest()


def _dist_version(dist: str) -> str:
    """用 importlib.metadata 取真实发布版本；主包/grammar 通常没有 ``__version__``。"""
    try:
        from importlib import metadata
        return metadata.version(dist)
    except Exception:
        return "?"


def _tree_sitter_grammar_state() -> str:
    """探测 tree-sitter 主包 + 各语言 grammar 包的**真实发布版本与实际可用 grammar**。

    点 8：不能只看 ``tree_sitter`` 可导入；JS/TS 解析行为取决于对应 grammar 模块的
    语言函数是否真实可用：
    - ``tree_sitter_javascript`` 暴露 ``language``（或 ``language_javascript``）；
    - ``tree_sitter_typescript`` 暴露 ``language_typescript`` / ``language_tsx``；
    - 版本一律以 ``importlib.metadata.version`` 为准（主包常无 ``__version__``）。
    """
    try:
        import tree_sitter  # noqa: F401
    except Exception:
        return "tree-sitter=missing"
    details = [f"tree-sitter=={_dist_version('tree-sitter')}"]
    packages = (
        # (模块名, 发行包名们, 实际语言函数属性候选)
        ("tree_sitter_javascript", ("tree-sitter-javascript",),
         ("language", "language_javascript")),
        ("tree_sitter_typescript", ("tree-sitter-typescript",),
         ("language_typescript", "language_tsx")),
    )
    for modname, dists, lang_attrs in packages:
        try:
            mod = __import__(modname)
        except Exception:
            details.append(f"{modname}=missing")
            continue
        versions = "|".join(_dist_version(d) for d in dists)
        langs = [a for a in lang_attrs if getattr(mod, a, None) is not None]
        status = "ok" if langs else "no-language"
        details.append(f"{modname}={status}=={versions}::{','.join(langs) or '-'}")
    return "|".join(details)


def compute_parser_fingerprint() -> str:
    """解析器可用性 + 版本指纹（含具体 grammar 包，跨配置不可复用）。"""
    ts = _tree_sitter_grammar_state()
    pyver = f"py{'.'.join(map(str, os.sys.version_info[:3]))}"
    return _hex(["parser", "python:ast", ts, pyver])


def compute_chunker_fingerprint() -> str:
    """分块策略指纹（行/字符预算 + 是否代码感知）。

    ``long-line=split`` 标记超长单行**按字符预算切片并完整保留**（不再丢弃
    2000 字符后的尾部）。这是分块语义变化，必须进入指纹：否则既有文件版本会
    被当作“同配置”复用旧的截断分块，修复对已入库内容不生效。
    """
    return _hex(
        [
            "chunker", "bounded-line", "max_lines=200", "max_chars=2000",
            "max_chunks=2000", "long-line=split",
            "code-terms=1", "path-terms=1",
        ]
    )


def compute_embedding_fingerprint() -> str:
    """embedding 服务商/模型/实际基址/维度指纹；换配置不得误复用向量。

    点 8：本地（ollama）必须用 OLLAMA_BASE_URL，云端才用 EMBEDDING_BASE_URL。
    """
    provider = str(settings.EMBEDDING_PROVIDER)
    model = str(settings.EMBEDDING_MODEL)
    base = str(settings.OLLAMA_BASE_URL) if provider == "local" else str(settings.EMBEDDING_BASE_URL)
    return _hex(["embedding", provider, model, base, str(EMBEDDING_DIM)])


def compute_config_fingerprint() -> str:
    """文件身份用整体配置指纹 = parser + chunker + embedding。"""
    return _hex([compute_parser_fingerprint(), compute_chunker_fingerprint(),
                 compute_embedding_fingerprint()])


def manifest_fingerprint(entries) -> str:
    """按 (relative_path, sha256, size) 稳定排序后的清单指纹。"""
    lines = sorted(
        f"{e.relative_path}\0{e.sha256}\0{e.size}" for e in entries
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8", "surrogatepass")).hexdigest()


# ---------------- blob / 解析读取 ----------------

def _blob_store() -> BlobStore:
    return BlobStore(Path(settings.PROJECT_STORAGE_DIR))


def _read_blob_verified(store: BlobStore, sha256: str, size: int) -> bytes | None:
    """点 9：只从受控 BlobStore 按 64-hex 键读取**已验证的原始字节**。

    直接委托 ``BlobStore.read_blob_bytes(sha256, expected_size)``：同一安全打开
    的 pin FD 上做有界逐块读取 + before/after fstat 核对 + 内容 SHA-256 校验，
    返回的 bytes 就是该 FD 实际读到的已验证内容。**绝不**先 verify 再按路径重新
    open（那会在 verify 与 open 之间留下 TOCTOU，且违反"只在受控键上读"）。
    任何不符（键非法 / 缺失 / 长度 / 哈希 / 读取期间变化）抛 ``ProjectPathError``
    而上层以 blob 缺失/损坏处理；这里把异常收敛为 ``None`` 以保持既有调用契约。
    """
    from app.services.project_paths import ProjectPathError

    if not store.verify_blob(sha256, size):
        return None
    try:
        return store.read_blob_bytes(sha256, size)
    except ProjectPathError:
        return None


# ---------------- 入队（API 调用） ----------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_project_sync(db: DBSession, source_id: uuid.UUID, user: User) -> ProjectSnapshot:
    """创建/复用 pending 快照。幂等：已有 pending/processing 则返回原任务。

    点 6：**enqueue 自己加锁源行并校验**——绝不留给调用方"去锁 source"再传进来的
    空头承诺。先以 ``populate_existing + FOR UPDATE`` **新鲜重读**源行（放弃可能在
    事务外/identity map 里已过期的脏对象），锁下校验：管理员、源未撤销、快照原租约
    与状态（处理中即幂等返回原任务，绝不重复建）。统一保持 源行锁 -> 快照行锁 顺序。
    返回需再次 commit 的快照。
    """
    # 源行锁（FOR UPDATE）+ 强制从 DB 重读（populate_existing），一致性锁序在
    # enqueue 内部恒定；绝不依据调用方事务外拿到的陈旧 source 对象。
    source = db.execute(
        select(ProjectSource)
        .where(ProjectSource.id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if source is None:
        raise project_sources.ProjectSourceNotFound()
    if source.revoked_at is not None:
        raise project_sources.ProjectSourceRevoked()
    if not user.is_admin:
        raise project_sources.ProjectSourceNotFound()
    # 先修定 base：当前 active（或最近 ready）。行锁保证单写者。
    base_id = source.active_snapshot_id
    if base_id is None:
        last_ready = db.scalar(
            select(ProjectSnapshot.id)
            .where(ProjectSnapshot.source_id == source_id,
                   ProjectSnapshot.status == ProjectSnapshotStatus.READY.value)
            .order_by(ProjectSnapshot.created_at.desc())
            .limit(1)
        )
        base_id = last_ready
    # 点 6：现有 pending/processing 的校验在"源行锁已持"下做，看到的就是锁定的真相。
    active = db.scalars(
        select(ProjectSnapshot)
        .where(
            ProjectSnapshot.source_id == source_id,
            ProjectSnapshot.status.in_([
                ProjectSnapshotStatus.PENDING.value,
                ProjectSnapshotStatus.PROCESSING.value,
            ]),
        )
        .with_for_update()
    ).all()
    if active:
        existing = active[0]
        if existing.base_snapshot_id is None:
            existing.base_snapshot_id = base_id
            db.flush()
        return existing
    snap = ProjectSnapshot(
        source_id=source_id,
        status=ProjectSnapshotStatus.PENDING.value,
        base_snapshot_id=base_id,
        attempts=0,
        available_at=_now(),
        manifest_fingerprint=None,
        config_fingerprint=None,
    )
    db.add(snap)
    db.flush()
    return snap


# ---------------- 领取 / 租约 ----------------

def _fresh_lease() -> uuid.UUID:
    """生成新租约。

    ``ProjectSnapshot.lease_id`` 是 ``UUID(as_uuid=True)``：读回时 ORM 给出
    ``uuid.UUID`` 对象。若这里返回 ``uuid4().hex`` 字符串，Python 侧
    ``snap.lease_id != lease_id`` 会恒为 True（UUID != str），所有租约复核都会
    把本 worker 当成“陈旧租约”拒绝写回/发布，任务静默永不完成。
    """
    return uuid.uuid4()


def _lease_expired(snap: ProjectSnapshot) -> bool:
    if snap.heartbeat_at is None:
        return True
    age = (_now() - snap.heartbeat_at).total_seconds()
    return age > settings.PROJECT_STALE_SECONDS


def recover_stale_jobs(max_recover: int | None = None) -> int:
    """把超时未心跳的 processing 退回 pending（保留 attempts）；退到上限转 failed。

    点 8：
    - **有界**：单轮只取 ``max_recover`` 个候选（默认 PROJECT_RECOVER_BATCH 或 64），
      绝不无界地一把拿起全部 processing 行。
    - **不复活**:只处理仍 processing 且租约确实过期（`_lease_expired`，按真实
      heartbeat 墙钟）的行；已明确 failed/cancelled/ready/pending 的一律不动，
      也绝不把"已取消"当可恢复——退出即失败，不回到 pending。
    - **有限重试可自愈不立即热抢**：退回 pending 时把 available_at 设为
      ``now + bounded backoff``（随 attempts 线性退避，封顶），避免刚失败立刻被抢、
      反复空转；available_at 到期后由 claim 有限重试。
    - 每行用条件 UPDATE（仅当仍 processing 且仍同租约）原子写，防止并发窗口里
      已被他方更新/接管时误降级。
    """
    max_recover = max_recover or getattr(settings, "PROJECT_RECOVER_BATCH", 64)
    recovered = 0
    with SessionLocal() as db:
        now = _now()
        stale_ids = db.scalars(
            select(ProjectSnapshot.id)
            .where(ProjectSnapshot.status == ProjectSnapshotStatus.PROCESSING.value)
            .order_by(ProjectSnapshot.heartbeat_at.asc())
            .limit(max_recover)
        ).all()
        for snap_id in stale_ids:
            # 一致性锁序 源行锁 → 快照行锁（与 claim 相同，防止交叉死锁）。
            src_id = db.scalar(
                select(ProjectSnapshot.source_id).where(ProjectSnapshot.id == snap_id)
            )
            if src_id is None:
                continue
            db.execute(
                select(ProjectSource.id).where(ProjectSource.id == src_id).with_for_update()
            )
            # 快照行锁（skip_locked：正被并发生效写事务持锁的行本轮跳过）+ populate_existing
            # 强制从 DB 重读，避开并发既已被取走/正在被处理的行。
            snap = db.execute(
                select(ProjectSnapshot)
                .where(ProjectSnapshot.id == snap_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if snap is None:
                continue
            if snap.status != ProjectSnapshotStatus.PROCESSING.value:
                continue  # 已被取消/失败/完成：绝不复活
            if not _lease_expired(snap):
                continue  # 还没过期：不碰
            if snap.attempts >= settings.PROJECT_MAX_ATTEMPTS:
                snap.status = ProjectSnapshotStatus.FAILED.value
                snap.lease_id = None
                snap.heartbeat_at = None
                snap.finished_at = _now()
                snap.error_code = "max-attempts"
                snap.error_message = "超过最大重试次数"
            else:
                snap.status = ProjectSnapshotStatus.PENDING.value
                snap.lease_id = None
                snap.heartbeat_at = None
                # 有限退避：随 attempts 线性（每轮多等一个STALE），封顶避免无界等待。
                backoff = _stale_delta() * min(snap.attempts + 1, settings.PROJECT_MAX_ATTEMPTS)
                snap.available_at = now + backoff
            recovered += 1
        db.commit()
    return recovered


@dataclass(frozen=True)
class Claim:
    """一次领取的**不可变**委托（点 5）。

    领取时把快照 id、本次租约、源 id 封装为纯值 DTO；此后``run``/心跳/写回/发布
    一律**只用这份 Claim 里的原始 lease_id**，绝不回头按 id 重新 fetch 拿“当前”
    lease —— 那是旧实现被租约重拍接管的根因（旧 worker 领完 A 后可能看到已被
    恢复重派的 B 新租约，误以为自己仍持有）。frozen 保证 worker 无法中途改绑。

    ``expiry`` 记录领取那一刻的租约到期墙钟，供心跳/发布校验“已被回收但尚未
    恢复”的窗口：租约过期后即使还躺在 recovery 的候选里，也拒绝续租/写回。
    """

    snapshot_id: uuid.UUID
    source_id: uuid.UUID
    lease_id: uuid.UUID
    expiry: datetime

    @property
    def expired(self) -> bool:
        """按领取时记录的到期墙钟判断本租约是否已过期（真实墙钟，非 last_seen）。"""
        return _now() >= self.expiry


def claim_next_project_snapshot() -> Claim | None:
    """领取一个可处理的快照（lease + heartbeat），返回不可变 ``Claim``。

    点 2：一致性锁序 **源行锁 → 快照行锁**（与 ``_lock_snapshot_source`` 相同），
    避免与写入路径对同一对行的加锁顺序相反造成死锁。先只读候选快照 id
    （不持锁），再对每个候选按源→快照顺序加锁并在同一事务内重核：
    状态仍 pending、未取消、available_at 已到期、attempts 未到上限、源未撤销。
    点 6：绝不领取 available_at 尚未到期、已被明确取消、或源已撤销的快照。

    返回的快照 ORM 对象在函数结束（事务关闭）后即失效；调用方只能持有返回的
    ``Claim`` 纯值 DTO，不能碰 ORM、也不能把 ORM 传出事务。
    """
    with SessionLocal() as db:
        now = _now()
        candidate_ids = db.scalars(
            select(ProjectSnapshot.id)
            .where(
                ProjectSnapshot.status == ProjectSnapshotStatus.PENDING.value,
                ProjectSnapshot.cancel_requested.is_(False),
                ProjectSnapshot.available_at.is_(None)
                | (ProjectSnapshot.available_at <= now),
            )
            .order_by(ProjectSnapshot.available_at.asc())
        ).all()
        chosen: ProjectSnapshot | None = None
        for snap_id in candidate_ids:
            snap = db.get(ProjectSnapshot, snap_id)
            if snap is None:
                continue
            # 先锁源行：源撤销则放弃该候选（不领取其快照）。
            source = db.execute(
                select(ProjectSource).where(ProjectSource.id == snap.source_id).with_for_update()
            ).scalar_one_or_none()
            if source is None or source.revoked_at is not None:
                continue
            # 再锁快照行：populate_existing 强制从 DB 重读，重核领取条件，
            # 并发窗口内状态变化/取消/到期重排都据此丢弃。
            snap = db.execute(
                select(ProjectSnapshot)
                .where(ProjectSnapshot.id == snap_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if snap is None:
                continue
            if (snap.status != ProjectSnapshotStatus.PENDING.value
                    or snap.cancel_requested
                    or snap.attempts >= settings.PROJECT_MAX_ATTEMPTS):
                continue
            if snap.available_at is not None and snap.available_at > now:
                continue  # 尚未到期：不领取
            chosen = snap
            break
        if chosen is None:
            return None
        lease = _fresh_lease()
        chosen.status = ProjectSnapshotStatus.PROCESSING.value
        chosen.lease_id = lease
        chosen.heartbeat_at = now
        chosen.attempts += 1
        chosen.started_at = now
        chosen.finished_at = None
        db.commit()
        # 返回不可变 Claim：到期墙钟 = 现在 + STALE 窗口（此后不得续租/写回）。
        return Claim(
            snapshot_id=chosen.id,
            source_id=chosen.source_id,
            lease_id=lease,
            expiry=now + _stale_delta(),
        )


def _stale_delta() -> timedelta:
    return timedelta(seconds=max(1, settings.PROJECT_STALE_SECONDS))


class Heartbeat:
    """在慢扫描/嵌入期间周期刷新 heartbeat + 进度 + 检查取消/旧租约。

    点 6：协作停机（stop_event）与用户取消是两回事。stop 只把快照退回 pending
    （保留 manifest/已嵌入分块检查点），**绝不**把 stop 标记为 cancel_requested；
    只有真实用户取消（DB 里 cancel_requested=True）才置 cancelled。
    心跳用条件 UPDATE，防止覆盖更新的租约。
    """

    def __init__(self, claim: Claim, stop_event: threading.Event):
        self.claim = claim
        self.snapshot_id = claim.snapshot_id
        self.lease_id = claim.lease_id
        self.stop_event = stop_event
        self._hb_ok = True  # 是否仍可信地持有租约（可继续写回/发布）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.progress = 0
        self.file_count = 0
        self.total_bytes = 0

    def hb_ok(self) -> bool:
        """是否仍可继续写回/发布：heartbeat 未丢失、未被接管/撤销。

        不再用领取时固定的 ``claim.expired`` 判死（那会在合法长任务 300s 到期时
        即使有效续租也误断）——**只认心跳线程是否仍成功续租**（``_hb_ok``）。
        心跳线程每轮条件 UPDATE 成功后保持 True；一旦更新失败/租约被接管/恢复/
        撤销/异常即置 False。真正的“租约是否有效”由每次条件写内心跳 freshness
        （``_lock_snapshot_source`` 里的 heartbeat 到期核验）与 ``recover_stale_jobs``
        共同以 ``heartbeat_at`` 墙钟决定，因此有效续租的合法长任务可继续。
        """
        return self._hb_ok

    def start(self) -> None:
        def loop():
            while not self._stop.wait(settings.PROJECT_POLL_SECONDS):
                try:
                    # 收到 stop 事件也先再做一次正常 heartbeat 循环以确认退出；
                    # 淘汰旧实现“join 超时只警告”的不可信路径（点 8）。
                    with SessionLocal() as db:
                        # 不按领取时固定的 claim.expiry 判死：合法长任务靠每次都成功的
                        # 条件心跳（刷新 heartbeat_at）续租。只有心跳更新失败/租约被
                        # 接管/源撤销/被回收（DB 状态变化）才丢租约——这正是"可续租任务
                        # 可继续"的判据；recover_stale_jobs 只对 heartbeat_at 过期的行回收。
                        # 条件更新 heartbeat（仅当仍持本租约且仍 processing）。
                        row = db.execute(
                            update(ProjectSnapshot)
                            .where(
                                ProjectSnapshot.id == self.snapshot_id,
                                ProjectSnapshot.lease_id == self.lease_id,
                                ProjectSnapshot.status == ProjectSnapshotStatus.PROCESSING.value,
                            )
                            .values(
                                heartbeat_at=_now(),
                                progress=self.progress,
                                file_count=self.file_count,
                                total_bytes=self.total_bytes,
                            )
                        )
                        if row.rowcount != 1:
                            # 租约被他方接管/恢复/删除：立即停，且标记不可继续发布。
                            self._hb_ok = False
                            self._stop.set()
                            continue
                        if self.stop_event.is_set():
                            self._stop.set()
                            continue
                        # 先按“用户真实取消”检查（条件更新到 cancelled）。
                        cancel_res = db.execute(
                            update(ProjectSnapshot)
                            .where(
                                ProjectSnapshot.id == self.snapshot_id,
                                ProjectSnapshot.lease_id == self.lease_id,
                                ProjectSnapshot.status == ProjectSnapshotStatus.PROCESSING.value,
                                ProjectSnapshot.cancel_requested.is_(True),
                            )
                            .values(
                                status=ProjectSnapshotStatus.CANCELLED.value,
                                lease_id=None,
                                heartbeat_at=None,
                                finished_at=_now(),
                                error_code="cancelled-user",
                                error_message="用户取消",
                            )
                        )
                        if cancel_res.rowcount == 1:
                            self._hb_ok = False
                            self._stop.set()
                        db.commit()
                except Exception:  # noqa: BLE001
                    # heartbeat 异常/丢失 => 不可继续发布（点 5）。
                    self._hb_ok = False
                    self._stop.set()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """协作停机：等待心跳线程**确认退出**（join），不信任“只发起后返回”。

        点 8：join 超时不能只警告了事——若心跳线程仍存活，主流程必须在返回控制前
        **确认它已退出**，否则工作线程可能在停机后继续写回/发布。心跳线程每轮
        ``self._stop.wait(poll)``，置位后一个 poll 内必然退出；若 join 超时线仍存活
        （卡在 DB 调用等），则置 ``_hb_ok=False`` 并把退出无法确认的事实记录为错误，
        绝不假装成功——发布路径据此放弃。
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=settings.PROJECT_POLL_SECONDS + 5)
            if self._thread.is_alive():
                self._hb_ok = False
                logger.error("心跳线程未能在停机时限内确认退出，视为不可继续发布")
        # 无论 join 是否超时，主流程都只依据 self.hb_ok() 决定是否可发布；
        # 发布路径会在自己的租约事务里再做一次条件核验（双保险）。

    def update(self, progress: int, file_count: int, total_bytes: int) -> None:
        self.progress = progress
        self.file_count = file_count
        self.total_bytes = total_bytes

    def stopped(self) -> bool:
        return self._stop.is_set()


# ---------------- 处理 ----------------

def _fail_snapshot(snap, code: str, message: str) -> None:
    snap.status = ProjectSnapshotStatus.FAILED.value
    snap.lease_id = None
    snap.heartbeat_at = None
    snap.finished_at = _now()
    snap.error_code = code
    snap.error_message = message
    snap.progress = 0


def _fail_snapshot_db(snapshot_id: uuid.UUID, lease_id: uuid.UUID, code: str, message: str) -> None:
    """条件失败快照：仅当仍持本租约且仍 processing，绝不误伤其他租约。"""
    with SessionLocal() as db:
        db.execute(
            update(ProjectSnapshot)
            .where(
                ProjectSnapshot.id == snapshot_id,
                ProjectSnapshot.lease_id == lease_id,
                ProjectSnapshot.status == ProjectSnapshotStatus.PROCESSING.value,
            )
            .values(
                status=ProjectSnapshotStatus.FAILED.value,
                lease_id=None,
                heartbeat_at=None,
                finished_at=_now(),
                error_code=code,
                error_message=str(message)[:2000],
                progress=0,
            )
        )
        db.commit()


def _settle_stop(snapshot_id: uuid.UUID, lease_id: uuid.UUID) -> None:
    """点 6 反悔兜底：协作停机时若快照仍在 processing 且持本租约，退回 pending。"""
    with SessionLocal() as db:
        db.execute(
            update(ProjectSnapshot)
            .where(
                ProjectSnapshot.id == snapshot_id,
                ProjectSnapshot.lease_id == lease_id,
                ProjectSnapshot.status == ProjectSnapshotStatus.PROCESSING.value,
                ProjectSnapshot.cancel_requested.is_(False),
            )
            .values(
                status=ProjectSnapshotStatus.PENDING.value,
                lease_id=None,
                heartbeat_at=None,
                available_at=_now(),
            )
        )
        db.commit()


def _lock_snapshot_source(
    db: DBSession, snapshot_id: uuid.UUID, lease_id: uuid.UUID
) -> tuple[ProjectSnapshot, ProjectSource] | None:
    """点 2：一致性锁序 源行锁 → 快照行锁，并在写入事务内重核（防陈旧租约）。

    ``populate_existing`` 强制从 DB 重读（不走陈旧 identity map）。
    校验：lease==ours、status==processing、源未撤销、未取消、**heartbeat 未过期**
    （点 4：写入须在“读锁内核验总心跳 > deadline”；heartbeat 已过期会先被
    ``recover_stale_jobs`` 退回 pending——这里再兜底一次保险）。旧 owner 的写不得
    借新租约：只认传入的本租约 id。
    """
    snap = db.get(ProjectSnapshot, snapshot_id)
    if snap is None:
        return None
    source = db.execute(
        select(ProjectSource).where(ProjectSource.id == snap.source_id).with_for_update()
    ).scalar_one_or_none()
    if source is None or source.revoked_at is not None:
        return None
    snap = db.execute(
        select(ProjectSnapshot)
        .where(ProjectSnapshot.id == snapshot_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if snap is None:
        return None
    if (snap.lease_id != lease_id
            or snap.status != ProjectSnapshotStatus.PROCESSING.value
            or snap.cancel_requested):
        return None
    if _lease_expired(snap):
        # 心跳已过期：本 worker 已不可信持有租约，拒绝在此事务内写回/发布。
        return None
    return snap, source


def _check_root_unchanged(anchor, expected_fp: str) -> None:
    """授权根身份指纹校验（**同一 pin 内**）：换树必须失败而非静默换源。

    ``anchor`` 由调用方持有（``project_sources.open_source_anchor`` 的 RootAnchor）；
    指纹取 ``anchor.root_fingerprint()``（被 pin 源根的 pin 身份），与创建时落库的
    ``expected_fp`` 比较。绝不 verify 后返回 Path 再重开树——那会把 pin 与后续
    open 之间留出换树窗口。
    """
    current_fp = anchor.root_fingerprint()
    if current_fp != expected_fp:
        raise ProjectScanError("授权根目录身份指纹已变化，拒绝扫描（需显式换绑）")


def _scan_for_snapshot(anchor, source_root_fp: str, store: BlobStore) -> ScanReport:
    """点 5/点 10：用 P 修订契约扫描；``truncated`` 或非空 ``limits`` 一律不可发布。

    ``anchor`` 为 P 的安全 RootAnchor（调用方 already 持有）、纹理扫描全程被 pin；
    指纹在同一 pin 内校验。``reject_hardlinks=True``（安全默认）：任何同一核验 FD
    上 nlink>1 的文件在读取/发布前即拒，其内容绝不写入 blob。git 未知仅附加元数据。
    """
    _check_root_unchanged(anchor, source_root_fp)
    limits = ProjectScanLimits(
        max_files=settings.PROJECT_MAX_FILES,
        max_total_bytes=settings.PROJECT_MAX_TOTAL_BYTES,
        max_single_bytes=settings.PROJECT_MAX_FILE_BYTES,
        parse_timeout_ms=int(max(1, settings.PROJECT_EMBEDDING_TIMEOUT_SECONDS * 1000)),
    )
    report = scan_project(anchor, store, limits, reject_hardlinks=True)
    # 触到任一硬界（truncated）或收纳了非致命限制 => 不能当作完整 ready 快照。
    if report.truncated or report.limits:
        raise ProjectScanError(
            "扫描未能完整收敛（触达文件/字节/遍历/收纳上限或 deadline），拒绝发布"
        )
    if not report.is_success:
        raise ProjectScanError("扫描未能正常收敛，拒绝发布")
    return report


def _persist_manifest(db: DBSession, snap: ProjectSnapshot, report: ScanReport,
                      config_fp: str, store: BlobStore) -> None:
    """持久化完整 manifest 映射（先扫描后解析，恢复复用固定 manifest）。

    - 每个 manifest 条目复用既有 file_version（同源+同路径+同 hash+同配置），否则新建。
    - 为每个条目写 SnapshotFile 映射（完整清单，绝不部分）。
    - manifest_fingerprint / config_fingerprint 写入快照。
    - blob 存在性校验只走受控 store 键（点 9），绝不用任意源路径。
    """
    parser_fp = compute_parser_fingerprint()
    chunker_fp = compute_chunker_fingerprint()
    embedding_fp = compute_embedding_fingerprint()
    snap.manifest_fingerprint = manifest_fingerprint(report.entries)
    snap.config_fingerprint = config_fp
    snap.file_count = len(report.entries)
    snap.total_bytes = sum(e.size for e in report.entries)

    existing = {
        (v.relative_path, v.content_sha256): v
        for v in db.scalars(
            select(ProjectFileVersion).where(
                ProjectFileVersion.source_id == snap.source_id,
                ProjectFileVersion.config_fingerprint == config_fp,
            )
        )
    }
    snap_files: list[ProjectSnapshotFile] = []
    for entry in report.entries:
        version = existing.get((entry.relative_path, entry.sha256))
        if version is None:
            # 扫描阶段 P 已把每个入选文件写成不可变 blob（内容寻址 = sha256）。
            # 这里仅做完整性防护：缺失即 fail closed（点 5/9）。
            if not store.verify_blob(entry.sha256, entry.size):
                raise ProjectScanError(f"manifest 缺失 blob: {entry.relative_path}")
            version = ProjectFileVersion(
                source_id=snap.source_id,
                relative_path=entry.relative_path,
                content_sha256=entry.sha256,
                blob_path=str(store.store_root / entry.sha256),
                size=entry.size,
                language=entry.language,
                config_fingerprint=config_fp,
                parser_fingerprint=parser_fp,
                chunker_fingerprint=chunker_fp,
                embedding_fingerprint=embedding_fp,
                parse_status=ProjectFileParseStatus.PENDING.value,
            )
            db.add(version)
            db.flush()
            existing[(entry.relative_path, entry.sha256)] = version
        snap_files.append(
            ProjectSnapshotFile(
                snapshot_id=snap.id,
                relative_path=entry.relative_path,
                file_version_id=version.id,
            )
        )
    # 全量重写映射（本快照首次建所有行即可；已有则跳过避免重复）。
    if snap_files:
        already = db.scalar(
            select(ProjectSnapshotFile.snapshot_id).where(
                ProjectSnapshotFile.snapshot_id == snap.id
            ).limit(1)
        )
        if already is None:
            db.add_all(snap_files)
            db.flush()


def _list_snapshot_version_ids(snapshot_id: uuid.UUID, lease_id: uuid.UUID) -> list[uuid.UUID]:
    with SessionLocal() as db:
        pair = _lock_snapshot_source(db, snapshot_id, lease_id)
        if pair is None:
            return []
        return list(db.scalars(
            select(ProjectSnapshotFile.file_version_id).where(
                ProjectSnapshotFile.snapshot_id == snapshot_id
            )
        ).all())


# ---------------- 解析（owned 子进程 + deadline） ----------------

# 逐文件解析在 owned 子进程内执行：读受控 blob（read_blob_bytes 已验证字节）→
# parse_blob → 回传结构化摘要。主进程不在此持有任何 DB 连接。
#
# 安全要点（对照审查修订）：
# - 用 ``-I``（隔离模式）启动，且**绝不**把 cwd 设为 blob 目录、绝不用
#   ``os.getcwd()`` 进 sys.path（防不可信目录里被植入 json.py 等）。
#   app.* 由显式传入的 ``backend_root`` 参数加入 sys.path（受信 backend 代码根）。
# - blob 读取走 ``BlobStore.read_blob_bytes(sha256, expected_size)``（同一安全
#   pin FD 有界读取 + before/after fstat + 内容哈希核对），不是 verify 后按路径
#   path.open（那有 TOCTOU，且会重开可能被换的文件）。
# - 解析只在 blob 字节上做静态 parse_blob，绝不 import/构建/执行源码。
_PARSE_WORKER = r"""
import json, sys
from pathlib import Path

# argv: backend_root store_root sha256 size_s relative_path language
backend_root, store_root, sha, size_s, rel_path, language = sys.argv[1:7]
# 明确受信 backend 代码根；绝不引用 os.getcwd()。``-I`` 已隔离 cwd 与用户 site。
sys.path.insert(0, backend_root)
from app.services.project_blobs import BlobStore
from app.services.project_parser import parse_blob
from app.services.project_types import BlobRef

def main():
    size = int(size_s)
    store = BlobStore(Path(store_root))
    try:
        # 受控 Store 的已验证读取：同安全 FD 有界读 + 前后 fstat + 哈希核对。
        blob = store.read_blob_bytes(sha, size)
    except Exception as exc:  # noqa: BLE001 - 上报给父进程统一处理
        json.dump({"ok": False, "error": f"blob-read-verified:{type(exc).__name__}"},
                  sys.stdout)
        return
    result = parse_blob(BlobRef(sha256=sha, size=size), blob, rel_path,
                        (language or None))
    json.dump({
        "ok": True,
        "language": result.language,
        "parser_status": result.parser_status,
        "truncated": bool(result.truncated),
        "line_count": int(result.line_count),
        "errors": list(result.errors),
        "symbols": [{"name": s.name, "kind": s.kind,
                     "start_line": int(s.start_line), "start_col": int(s.start_col),
                     "end_line": int(s.end_line), "end_col": int(s.end_col)}
                    for s in result.symbols],
        "blocks": [{"kind": b.kind, "name": b.name,
                    "start_line": int(b.start_line), "end_line": int(b.end_line),
                    "text": b.text}
                   for b in result.blocks],
    }, sys.stdout, ensure_ascii=False)

main()
"""


def _backend_root() -> str:
    """返回受信 backend 代码根（含 app/ 包的上级目录）。"""
    return str(Path(__file__).resolve().parents[2])


def _process_creation_ns(pid: int) -> int | None:
    """返回进程创建时间（Windows FILETIME 100ns since 1601），POSIX/失败返回 None。

    用于在超时杀进程前核对“此 pid 仍是当初启动的那个进程”，绝不误杀被 pid 复用
    的他人进程；也用做语句验证"杀的是真正 Python 解释器进程，不是外壳 launcher"。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            class FT(wintypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD),
                            ("dwHighDateTime", wintypes.DWORD)]
            c, e, k, u = FT(), FT(), FT(), FT()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e),
                                            ctypes.byref(k), ctypes.byref(u)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 身份核对是尽力而为，失败退化为按 pid kill
        return None


def _terminate_parse_process(proc, created_ns: int | None) -> bool:
    """终止 parse 子进程（超时路径）。核 pid+ctime 后才杀，绝不误杀 pid 复用者。

    返回是否确认杀掉了我们启动的那个进程。Windows 上若身份核对失败则拒绝误杀，
    但会再尝试按普通 ``proc.kill()``（进程是小核算法直接启动，无包裹 shell）。
    """
    if os.name == "nt" and created_ns is not None:
        try:
            now_ns = _process_creation_ns(proc.pid)
            if now_ns != created_ns:
                return False  # pid 已被复用：不杀他人进程
            import ctypes
            from ctypes import wintypes
            SYNCHRONIZE = 0x00100000
            PROCESS_TERMINATE = 0x0001
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, False, int(proc.pid))
            if handle:
                try:
                    kernel32.TerminateProcess(handle, 1)
                finally:
                    kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    else:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    return True


def _parse_in_subprocess(store: BlobStore, v_info: dict,
                         timeout_s: float) -> dict:
    """点 7：强制 per-file CPU deadline 的 owned 子进程解析（真实 -I 隔离）。

    - 用 ``sys.executable -I`` 直接启动真正的 Python 解释器（Windows 下
      ``python.exe`` 即真实进程，不是 .bat 包裹 launcher）。
    - 超时用 Popen + 显式杀进程，而非 ``subprocess.run`` 的隐式 kill：杀前记录
      pid/ctime 并核对身份，确认杀掉的是我们启动的那个真正 Python 进程与其子树。
    - cwd 保持调用方工作目录（**绝不**切到 blob 目录）；blob 根/键作参数传，
      受信 backend 根作参数让 worker 自行加入 sys.path。
    """
    code = _PARSE_WORKER
    argv = [
        sys.executable, "-I", "-c", code,
        _backend_root(), str(store.store_root),
        v_info["content_sha256"], str(v_info["size"]),
        v_info["relative_path"], v_info["language"] or "",
    ]
    _rel = v_info.get("relative_path")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=None,  # 继承调用方 cwd，绝不切到 blob/不可信目录
        )
    except OSError as exc:
        raise ProjectScanError(f"文件解析子进程启动失败: {_rel}: {exc.strerror or exc}")
    created_ns = _process_creation_ns(proc.pid)
    try:
        out, _err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_parse_process(proc, created_ns)
        raise ProjectScanError(f"文件解析超时，放弃: {_rel}")
    if proc.returncode != 0 or not out:
        raise ProjectScanError(f"文件解析子进程失败(rc={proc.returncode}): {_rel}")
    try:
        payload = json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ProjectScanError(f"解析结果不可读: {_rel}: {exc}")
    return payload


def _map_parse_status(parser_status: str) -> str:
    """点 7：把 P 的 parser_status（ast/tree-sitter/vue/markdown/fallback/skipped）
    映射到 F 的 ProjectFileParseStatus 枚举值，绝不存原始值。"""
    if parser_status in {"ast", "tree-sitter", "vue", "markdown"}:
        return ProjectFileParseStatus.READY.value
    if parser_status == "fallback":
        return ProjectFileParseStatus.FALLBACK.value
    # 'skipped'（二进制/不支持）与未知 -> 降级标记（不伪造符号，不影响发布）
    return ProjectFileParseStatus.FALLBACK.value


def _build_chunks(values: dict, text: str, payload: dict) -> list[ProjectChunk]:
    """按行有界切分整文（保留可核验原文行号），按行范围匹配符号。

    只读取 ``values`` 里的**纯不可变值**（id / relative_path / language /
    content_sha256 / size / config / embedding fingerprint），绝不接收跨事务的
    ORM 对象——旧实现把 ``ProjectFileVersion`` 传进来，在短事务 ``rollback`` 后
    再访问会抛 ``DetachedInstanceError``。这里 ``values`` 由调用方在持有事务内
    收集的纯 dict 提供，分块构建可安全地在事务外进行。
    """
    version_id = values["id"]
    relative_path = values["relative_path"]
    language = values["language"]
    content_sha256 = values["content_sha256"]
    size = values["size"]
    config_fingerprint = values["config_fingerprint"]
    embedding_fingerprint = values["embedding_fingerprint"]
    symbols = [
        SourceSymbol(s["name"], s["kind"], s["start_line"], s["start_col"],
                     s["end_line"], s["end_col"])
        for s in payload.get("symbols") or []
    ]
    out: list[ProjectChunk] = []
    idx = 0
    for lc in iter_bounded_line_chunks(_split_text_lines(text), 1, 200, 2000):
        if idx >= _MAX_CHUNKS_PER_FILE:
            break
        srclines = list(lc.source_lines)
        symbol, symbol_kind = _symbol_for(relative_path, srclines, symbols)
        search_terms = _search_terms(lc.text, relative_path)
        out.append(
            ProjectChunk(
                file_version_id=version_id,
                chunk_index=idx,
                content=lc.text,
                search_text=search_terms,
                source_metadata={
                    "source_id": str(values["source_id"]),
                    "source_kind": KNOWLEDGE_BASE_KIND_PROJECT,
                    "file_version_id": str(version_id),
                    "relative_path": relative_path,
                    "language": language,
                    "content_sha256": content_sha256,
                    "size": size,
                    "parse_status": values["parse_status"],
                    "config_fingerprint": config_fingerprint,
                    "embedding_fingerprint": embedding_fingerprint,
                },
                line_start=srclines[0] if srclines else None,
                line_end=srclines[-1] if srclines else None,
                symbol=symbol,
                symbol_kind=symbol_kind,
                embedded=False,
            )
        )
        idx += 1
    return out


def _split_text_lines(text: str) -> list[str]:
    return text.splitlines(keepends=True) if text else []


def _symbol_for(relative_path: str, lines: list[int], symbols: list[SourceSymbol]):
    if not lines or not symbols:
        return None, None
    start = lines[0]
    best = None
    for s in symbols:
        if s.start_line <= start <= s.end_line:
            if best is None or (
                s.end_line - s.start_line < best.end_line - best.start_line
                and start - s.start_line <= start - best.start_line
            ):
                best = s
    return (best.name, best.kind) if best else (None, None)


def _search_terms(content: str, relative_path: str) -> str:
    terms: list[str] = []
    for t in path_terms(relative_path):
        if t not in terms:
            terms.append(t)
    for t in code_terms(content):
        if t not in terms:
            terms.append(t)
    return " ".join(terms[:4000])


def _recompute_file_hash(blob_bytes: bytes) -> str:
    return hashlib.sha256(blob_bytes).hexdigest()


def _process_one_file(snapshot_id: uuid.UUID, lease_id: uuid.UUID, vals: dict,
                      version_id: uuid.UUID, stop_event: threading.Event,
                      store: BlobStore) -> None:
    """点 1/2/7：单个文件的 解析→嵌入，严格短事务，网络/CPU 不持 DB 连接。"""
    # 1) 短事务：锁源+快照，读版本为纯值，随即释放（不持 DB 锁做任何 I/O/CPU）。
    need_parse = False
    with SessionLocal() as db:
        pair = _lock_snapshot_source(db, snapshot_id, lease_id)
        if pair is None:
            return
        version = db.get(ProjectFileVersion, version_id)
        if version is None:
            return
        need_parse = not version.parse_done
        # 收集纯值（避免跨会话惰性加载）；跨短事务只传递这份不可变 dict。
        v_info = {
            "id": version.id,
            "source_id": version.source_id,
            "relative_path": version.relative_path,
            "content_sha256": version.content_sha256,
            "size": version.size,
            "language": version.language,
            "config_fingerprint": version.config_fingerprint,
            "embedding_fingerprint": version.embedding_fingerprint,
            "parse_status": version.parse_status,
        }
        db.rollback()

    parse_status: str | None = None
    parse_error: str | None = None
    chunk_objs: list[ProjectChunk] | None = None
    if need_parse:
        # 读受控 blob（本地 I/O，不持 DB 连接；点 9 只按 sha256 键 verify+读）。
        blob_bytes = _read_blob_verified(store, v_info["content_sha256"], v_info["size"])
        if blob_bytes is None:
            _fail_snapshot_db(snapshot_id, lease_id, "blob-missing",
                              f"blob 缺失或校验失败: {v_info['relative_path']}")
            return
        # 请求负载指纹校验：内容必须与清单一致（点 9）。
        if v_info["content_sha256"] != _recompute_file_hash(blob_bytes):
            _fail_snapshot_db(snapshot_id, lease_id, "blob-corrupt",
                              f"blob 内容与清单指纹不符: {v_info['relative_path']}")
            return
        timeout_s = max(1.0, float(settings.PROJECT_EMBEDDING_TIMEOUT_SECONDS))
        payload = _parse_in_subprocess(store, v_info, timeout_s)
        if not payload.get("ok"):
            _fail_snapshot_db(snapshot_id, lease_id, "parse-error",
                              payload.get("error") or "解析失败")
            return
        status = _map_parse_status(payload.get("parser_status") or "skipped")
        truncated = bool(payload.get("truncated"))
        errors = list(payload.get("errors") or [])
        # 点 7：截断或超块上限绝不能当 ready 发布。
        if truncated:
            parse_status = ProjectFileParseStatus.FAILED.value
            parse_error = "解析被预算截断（文件过大/行数超限），不作为 ready 发布"
            chunk_objs = []
            parse_error = "; ".join([parse_error] + errors)[:4000]
        else:
            text, _bounded = read_blob_bounded(blob_bytes)
            chunk_objs = _build_chunks(v_info, text, payload)
            if len(chunk_objs) >= _MAX_CHUNKS_PER_FILE:
                parse_status = ProjectFileParseStatus.FAILED.value
                parse_error = f"分块超过上限 {_MAX_CHUNKS_PER_FILE}，不作为 ready 发布"
                chunk_objs = []
            else:
                parse_status = status
                parse_error = "; ".join(errors)[:4000] if errors else None
        # 2) 短事务：条件写版本 parse 状态 + 分块（同一租约事务）。
        _persist_parse_db(snapshot_id, lease_id, v_info, parse_status,
                          parse_error, chunk_objs)

    # 3) 嵌入（自身内部再次短事务，网络调用不持 DB）。
    _embed_file(snapshot_id, lease_id, vals["collection_name"], vals["source_id"],
                vals["kb_id"], vals["created_by"], version_id, stop_event)


def _persist_parse_db(snapshot_id: uuid.UUID, lease_id: uuid.UUID, v_info: dict,
                      parse_status: str, parse_error: str | None,
                      chunk_objs: list[ProjectChunk]) -> None:
    """条件写版本 parse 结果 + 分块（点 2/7）；租约变了则拒绝（放弃本次快照）。"""
    with SessionLocal() as db:
        pair = _lock_snapshot_source(db, snapshot_id, lease_id)
        if pair is None:
            return
        version = db.get(ProjectFileVersion, v_info["id"])
        if version is None:
            return
        db.query(ProjectChunk).filter(
            ProjectChunk.file_version_id == version.id
        ).delete()
        version.parse_done = True
        version.parse_status = parse_status
        version.parse_error = parse_error
        if chunk_objs is not None:
            version.char_count = sum(len(c.content) for c in chunk_objs)
            version.chunk_count = len(chunk_objs)
            db.add_all(chunk_objs)
        else:
            version.char_count = 0
            version.chunk_count = 0
        db.commit()


# ---------------- 嵌入 ----------------

def _vector_documents(batch_values: list[dict], version_id: uuid.UUID) -> list[LCDocument]:
    """构造待嵌入文档：向量 cmetadata 必须携带检索 JOIN 所需的权威键。

    ``ProjectChunk.source_metadata`` 本身**不含** ``chunk_index``；若原样落库，检索
    侧 ``cmetadata->>'chunk_index' = chunk.chunk_index``（以及 file_version_id）的
    JOIN 永远无法命中，所有项目向量检索都会静默返回空。这里显式补齐 file_version_id
    / chunk_index / index_version，并统一 source_kind；其余 cmeta 原样保留。
    """
    documents: list[LCDocument] = []
    for value in batch_values:
        metadata = dict(value.get("source_metadata") or {})
        metadata["file_version_id"] = str(version_id)
        metadata["chunk_index"] = int(value["chunk_index"])
        metadata["index_version"] = 0
        metadata["source_kind"] = KNOWLEDGE_BASE_KIND_PROJECT
        documents.append(
            LCDocument(page_content=value["content"], metadata=metadata)
        )
    return documents


def _embed_file(snapshot_id: uuid.UUID, lease_id: uuid.UUID, collection_name: str,
                source_id: uuid.UUID, kb_id: uuid.UUID | None,
                created_by: uuid.UUID | None, version_id: uuid.UUID,
                stop_event: threading.Event) -> None:
    """把某文件全部未嵌入分块向量化并持久化（短事务；网络调用不持 DB）。

    点 2/3：每次 DB 写都重开短事务并在同一事务内 re-check 租约（条件引用拒绝写），
    卷与 embedded 进度同租约写。向量 id/metadata 不含 snapshot_id。
    usage 用量以源的真实所属（kb_id/created_by）记账（点 5），而非 None。
    """
    while not stop_event.is_set():
        # 读一批未嵌入分块（短事务，与进度在同一条件事务外先取数）。
        with SessionLocal() as db:
            pair = _lock_snapshot_source(db, snapshot_id, lease_id)
            if pair is None:
                return
            pending = db.scalars(
                select(ProjectChunk).where(
                    ProjectChunk.file_version_id == version_id,
                    ProjectChunk.embedded.is_(False),
                ).order_by(ProjectChunk.chunk_index)
                .limit(settings.PROJECT_PARSE_BATCH)  # LIMIT 必须在 select 上，不在结果上
            ).all()
            if not pending:
                return
            batch_values = [
                {"id": c.id, "chunk_index": c.chunk_index,
                 "content": c.content, "source_metadata": c.source_metadata or {}}
                for c in pending
            ]
            db.rollback()

        documents = _vector_documents(batch_values, version_id)
        ids = [
            f"{settings.PROJECT_VECTOR_PREFIX}{version_id}:{v['chunk_index']}"
            for v in batch_values
        ]
        store = get_vector_store(collection_name)
        store.create_collection()  # idempotent
        embeddings = get_embeddings()
        # 网络向量化（不持任何 DB 连接）。
        with embedding_stop_scope(stop_event), usage_scope(
            UsageContext(
                user_id=created_by, kb_id=kb_id, operation="project_embedding"
            ).for_operation("project_embedding")
        ):
            if settings.EMBEDDING_PROVIDER == "cloud":
                vectors = embeddings.embed_documents([d.page_content for d in documents])
            else:
                call = ModelCall("embedding", "ollama", settings.EMBEDDING_MODEL)
                try:
                    vectors = embeddings.embed_documents(
                        [d.page_content for d in documents]
                    )
                except BaseException:
                    call.finish("failed")
                    raise
                else:
                    call.finish()
        if any(len(v) != EMBEDDING_DIM for v in vectors):
            raise ValueError("embedding 维度与 EMBEDDING_DIM 不符")

        # 同一事务：条件 re-check 租约（点 2，防陈旧/被接管）→ 写向量 + 进度。
        with SessionLocal() as db:
            pair = _lock_snapshot_source(db, snapshot_id, lease_id)
            if pair is None:
                return  # 租约已变：拒绝写（放弃本批，不覆盖他人进度）
            # per-lease 拒绝写：仅当这些分块仍 embedded=False（未被别批写）。
            rows = db.execute(
                select(ProjectChunk.id).where(
                    ProjectChunk.file_version_id == version_id,
                    ProjectChunk.id.in_([v["id"] for v in batch_values]),
                    ProjectChunk.embedded.is_(False),
                )
            ).scalars().all()
            if len(rows) != len(batch_values):
                return  # 已有批次写入：本轮跳过，避免重复向量
            collection = store.get_collection(db)
            if collection is None:
                store.create_collection()
                collection = store.get_collection(db)
            persist_vectors(db, store, documents, vectors, ids)
            db.execute(
                update(ProjectChunk)
                .where(ProjectChunk.id.in_([v["id"] for v in batch_values]))
                .values(embedded=True)
            )
            db.commit()
        logger.info("embed %d chunks version %s", len(batch_values), version_id)


# ---------------- CAS 发布 ----------------

def _file_all_ready(db: DBSession, version: ProjectFileVersion) -> bool:
    if not version.parse_done or version.parse_status == ProjectFileParseStatus.FAILED.value:
        return False
    missing = db.scalar(
        select(ProjectChunk.id)
        .where(ProjectChunk.file_version_id == version.id,
               ProjectChunk.embedded.is_(False))
        .limit(1)
    )
    return missing is None


def _publish_cas(snapshot_id: uuid.UUID, lease_id: uuid.UUID) -> bool:
    """点 3：真实 DB 级 CAS 发布（源行锁下条件 UPDATE），返回是否成功发布。

    成功发布时把快照置 ready 并清算租约；绝不把成功发布误报为 cancelled。
    """
    with SessionLocal() as db:
        pair = _lock_snapshot_source(db, snapshot_id, lease_id)
        if pair is None:
            return False
        snap, source = pair
        # 全部映射文件 ready 才算可发布。``legal_empty``：已持久化 manifest 且零
        # 文件的“合法空清单”（源目录确实为空/全部被硬排除）也允许发布为 ready；
        # 但“根本没持久化 manifest”绝不能当作空清单假 ready（点 7）。
        legal_empty = snap.manifest_fingerprint is not None
        version_ids = db.scalars(
            select(ProjectSnapshotFile.file_version_id).where(
                ProjectSnapshotFile.snapshot_id == snapshot_id
            )
        ).all()
        if not version_ids and not legal_empty:
            snap.status = ProjectSnapshotStatus.FAILED.value
            snap.lease_id = None
            snap.heartbeat_at = None
            snap.finished_at = _now()
            snap.error_code = "incomplete"
            snap.error_message = "快照无任何映射文件且无已持久化 manifest，不发布"
            db.commit()
            return False
        versions = db.scalars(
            select(ProjectFileVersion).where(ProjectFileVersion.id.in_(version_ids))
        ).all()
        if not all(_file_all_ready(db, v) for v in versions):
            # 存在 failed/pending/skipped 文件绝不能假 ready；保留旧 active。
            snap.status = ProjectSnapshotStatus.FAILED.value
            snap.lease_id = None
            snap.heartbeat_at = None
            snap.finished_at = _now()
            snap.error_code = "incomplete"
            snap.error_message = "存在未就绪文件，不发布"
            db.commit()
            return False
        # 点 3/点 7：CAS —— 源行已锁，仅当 active_snapshot_id 仍等于 base 才切 active，
        # 用条件 UPDATE WHERE ... 原子保证；绝不是 Python 比较后赋值。
        # base 可能为 NULL（首次发布：active 也应为 NULL），SQL 需显式 IS NULL 分支。
        expected_base = snap.base_snapshot_id
        if expected_base is None:
            cas_where = (
                ProjectSource.active_snapshot_id.is_(None)
            )
        else:
            cas_where = (ProjectSource.active_snapshot_id == expected_base)
        cas_res = db.execute(
            update(ProjectSource)
            .where(ProjectSource.id == source.id,
                   cas_where,
                   ProjectSource.revoked_at.is_(None))
            .values(active_snapshot_id=snapshot_id)
        )
        # 点 7：真实 CAS 行数核对——rowcount != 1 表示 base 已被他人推进（不一致），
        # **绝不**假定本快照已就位而假 ready；保持旧 active 不动，快照留待恢复
        # （对新的正确 base 重跑）。此时不清租约、不标记 ready。
        if cas_res.rowcount != 1:
            db.rollback()
            return False
        db.execute(
            update(ProjectSnapshot)
            .where(ProjectSnapshot.id == snapshot_id,
                   ProjectSnapshot.lease_id == lease_id)
            .values(
                status=ProjectSnapshotStatus.READY.value,
                progress=100,
                lease_id=None,
                heartbeat_at=None,
                finished_at=_now(),
                error_code=None,
                error_message=None,
            )
        )
        db.commit()
        return True


# ---------------- 主处理 ----------------

def _run_sync_phases(snapshot_id: uuid.UUID, lease_id: uuid.UUID,
                     stop_event: threading.Event, hb: Heartbeat,
                     claim: Claim) -> None:
    store = _blob_store()
    # Phase 0: 读快照 + 源为纯值（源行锁 -> 快照行锁），随后不持 DB 做实际工作。
    with SessionLocal() as db:
        pair = _lock_snapshot_source(db, snapshot_id, lease_id)
        if pair is None:
            return
        snap, source = pair
        kb = source.knowledge_base
        vals = {
            "source_id": snap.source_id,
            "root_key": source.root_key,
            "relative_dir": source.relative_dir,
            "source_root_fingerprint": source.root_fingerprint,
            "kb_id": source.kb_id,
            "created_by": source.created_by,
            "collection_name": kb.collection_name,
            "base_snapshot_id": snap.base_snapshot_id,
            "has_manifest": snap.manifest_fingerprint is not None,
            "has_any_files": db.scalar(
                select(ProjectSnapshotFile.snapshot_id).where(
                    ProjectSnapshotFile.snapshot_id == snapshot_id
                ).limit(1)
            ) is not None,
        }
        db.rollback()

    config_fp = compute_config_fingerprint()
    _ensure_manifest(snapshot_id, lease_id, vals, config_fp, store)
    hb.update(10, _snapshot_count(snapshot_id, lease_id), _snapshot_bytes(snapshot_id, lease_id))

    version_ids = _list_snapshot_version_ids(snapshot_id, lease_id)
    total = len(version_ids)
    for i, version_id in enumerate(version_ids):
        if stop_event.is_set():
            break
        _process_one_file(snapshot_id, lease_id, vals, version_id, stop_event, store)
        hb.update(10 + int(80 * (i + 1) / max(1, total)),
                  total, 0)

    # 点 6：协作停机 -> 退回 pending（保留检查点），与用户取消区分。
    if stop_event.is_set():
        _settle_stop(snapshot_id, lease_id)
        return

    # 点 5：发布前可信闸门。heartbeat 丢失/异常/claim 已过期/租约被接管 =>
    # 本 worker 已不再可信持有租约，绝不能继续 publish（即便 CAS 条件写碰巧命中也
    # 会因 heartbeat 掉线而让"ready"成为不可信结论）。
    if not hb.hb_ok():
        return

    # CAS 发布。
    _publish_cas(snapshot_id, lease_id)


def _snapshot_count(snapshot_id: uuid.UUID, lease_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        if not _lease_held(db, snapshot_id, lease_id):
            return 0
        counts = db.scalar(
            select(func.count(ProjectSnapshotFile.relative_path)).where(
                ProjectSnapshotFile.snapshot_id == snapshot_id
            )
        ) or 0
        db.rollback()
        return int(counts)


def _snapshot_bytes(snapshot_id: uuid.UUID, lease_id: uuid.UUID) -> int:
    with SessionLocal() as db:
        if not _lease_held(db, snapshot_id, lease_id):
            return 0
        total = db.scalar(
            select(func.coalesce(func.sum(ProjectFileVersion.size), 0))
            .select_from(ProjectSnapshotFile)
            .join(ProjectFileVersion, ProjectFileVersion.id == ProjectSnapshotFile.file_version_id)
            .where(ProjectSnapshotFile.snapshot_id == snapshot_id)
        ) or 0
        db.rollback()
        return int(total)


def _lease_held(db: DBSession, snapshot_id: uuid.UUID, lease_id: uuid.UUID) -> bool:
    """只读核验（不加锁）本租约仍持有该 processing 快照；快速读，不入写事务。

    点 4：同时核验 **heartbeat 未过期**（读锁内核验 fresh heartbeat > deadline），
    否则已被回收/接手——绝不据此继续写回/发布。
    """
    snap = db.get(ProjectSnapshot, snapshot_id)
    return (snap is not None
            and snap.lease_id == lease_id
            and snap.status == ProjectSnapshotStatus.PROCESSING.value
            and not snap.cancel_requested
            and not _lease_expired(snap))


def _ensure_manifest(snapshot_id: uuid.UUID, lease_id: uuid.UUID, vals: dict,
                     config_fp: str, store: BlobStore) -> bool:
    """点 4：恢复复用已持久化的 manifest/快照映射，否则扫描+持久化。

    返回是否复用了既有 manifest（True=复用，False=本次扫描）。
    若源中途变化或配置变化，不静默混扫——直接失败。

    全程**持有同一个 RootAnchor**（P 的 pin 身份）：指纹在创建时/同步时同一 pin 内
    比较，扫描也走被 pin 的 anchor（防 Windows junction 祖先逃逸），绝不 verify 后
    返回 Path 再重开树。anchor 在 with 块内释放；源中途换树/换配置都 fail closed。
    """
    try:
        anchor = project_sources.open_source_anchor(
            vals["root_key"], vals["relative_dir"]
        )
    except project_sources.ProjectRootError as exc:
        _fail_snapshot_db(snapshot_id, lease_id, "root-error", str(exc)[:2000])
        raise ProjectScanError(str(exc))
    with anchor:
        # 无论复用否，先在同一 pin 内校验根身份（换树 -> 失败）。
        _check_root_unchanged(anchor, vals["source_root_fingerprint"])
        # 点 7：固定 manifest 恢复必须**不重扫源目录**。已持久化 manifest（无论是否含
        # 文件——合法的空 manifest 同样要复用）即复用既有映射，绝不再对源目录做一次
        # 扫描（否则固定 manifest / 合法空 manifest 会在第二次运行被重扫混进新源内容）。
        # 复用仍须重核配置未变（旧 manifest 与当前配置不匹配则失败不混用）。
        if vals["has_manifest"]:
            with SessionLocal() as db:
                snap = db.get(ProjectSnapshot, snapshot_id)
                if snap is not None and snap.config_fingerprint != config_fp:
                    db.rollback()
                    _fail_snapshot_db(snapshot_id, lease_id, "config-changed",
                                      "同步期间配置指纹变化，旧 manifest 不可复用")
                    raise ProjectScanError("配置指纹中途变化，拒绝复用旧 manifest")
                db.rollback()
            logger.info("复用既有 manifest 快照 %s", snapshot_id)
            return True

        # 扫描 + 持久化 manifest（点 5：截断/超限绝不可发布），同一 anchor 全程 pin。
        report = _scan_for_snapshot(anchor, vals["source_root_fingerprint"], store)
        with SessionLocal() as db:
            pair = _lock_snapshot_source(db, snapshot_id, lease_id)
            if pair is None:
                return False
            snap, _ = pair
            _persist_manifest(db, snap, report, config_fp, store)
            db.commit()
        return False


def run_project_sync(claim: Claim, stop_event: threading.Event) -> str:
    """处理一个**已领取的 Claim**，返回落库状态。

    点 5：只认调用方传入的不可变 ``Claim``（含原始 lease_id）。全程用
    ``claim.snapshot_id`` / ``claim.lease_id`` 参与条件写；**绝不**回头按 id
    fetch“当前”lease——旧的按 id 重读会让已被恢复/重派的新租约顶替本 worker 的
    原始租约，从而让旧 worker 误以为自己仍持有。
    """
    snapshot_id = claim.snapshot_id
    lease = claim.lease_id
    hb = Heartbeat(claim, stop_event)
    try:
        hb.start()
        _run_sync_phases(snapshot_id, lease, stop_event, hb, claim)
        with SessionLocal() as db:
            snap = db.get(ProjectSnapshot, snapshot_id)
            # 可信发布闸门：heartbeat 丢失/租约过期/被接管 => 即使落库看似 OK
            # 也不得自报成功（点 5：心跳掉了绝不能继续 publish）。
            if not hb.hb_ok():
                if snap is None:
                    return "failed"
                # 如实在返回前按当前状态：cancel/pending/failed 都可能。
                status = snap.status
                if stop_event.is_set() and status != ProjectSnapshotStatus.READY.value:
                    return "pending"
                return status or "failed"
            if snap is None or snap.lease_id != lease:
                # 租约已变（停止/取消/接管）：如实返回当前落库状态。
                if snap is None:
                    return "failed"
                if snap.status == ProjectSnapshotStatus.READY.value:
                    return "ready"
                if snap.status == ProjectSnapshotStatus.CANCELLED.value:
                    return "cancelled"
                if snap.status == ProjectSnapshotStatus.PENDING.value:
                    return "pending"
                return snap.status or "failed"
            if stop_event.is_set() and snap.status != ProjectSnapshotStatus.READY.value:
                return "pending"
            return snap.status
    except (ProjectScanError, ValueError) as exc:
        _fail_snapshot_db(snapshot_id, lease, "sync-error", str(exc)[:2000])
        return "failed"
    finally:
        hb.stop()
