"""T3 回答来源快照：严格解析、去重与落库前复核。

本模块只做两件事，二者都不提交事务、不释放事务、不做网络访问：

1. **纯解析**：把一轮问答实际使用的来源规范化成可持久化的 v1 快照
   ``{"source_snapshot_version": 1, "supplied_sources": [...], "history_sources": [...]}``。
   来源只包含身份字段，绝不保存 ``snippet`` / ``score`` / 模型正文。
2. **写前复核**：按来源类型分别核对上传 ``documents`` 或项目
   ``ProjectSource -> ProjectSnapshot -> ProjectFileVersion -> ProjectSnapshotFile -> ProjectChunk``
   仍可读、身份未漂移、快照清单与指纹一致。上传路径绝不查项目表，项目路径绝不查上传
   ``Document``。

## 失败语义（全部 fail closed）

* 旧/坏 trace、坏 provenance、坏来源字段：``SourceSnapshotUnavailable`` /
  ``SourceSnapshotError``——**整轮排除**。
* 去重后来源超过 ``MAX_SNAPSHOT_SOURCES``：``SourceSnapshotOverflow``——**本轮必须失败或
  整轮移除**。绝不裁剪来源后保留正文，也不保存“无 provenance 的成功轮”。
* 落库前复核失败：``SourceVerificationError``——本轮不得写成成功。

## 全局锁顺序

``User -> KnowledgeBase -> ProjectSource -> ProjectSnapshot
-> Conversation -> ChatTurn -> Message -> (上传) UUID 排序的 Document``

``lock_root``（User -> KB）与 ``lock_project_binding``（ProjectSource -> ProjectSnapshot）
必须在会话/轮次锁之前；``lock_document_rows`` 只能在 Message 落库之后。helper 只加自己
那一段锁，绝不 commit/rollback/release。

本模块全程只读（不加锁时）或只加锁（加锁时），从不修改 ORM 对象；所有查询都包在
``Session.no_autoflush`` 内，避免自动 flush 把别处 pending 的写入提前发出、破坏顺序。
``populate_existing`` 只用于“读取并覆盖身份映射里的陈旧值”，从不用于变更之后。

## ``now`` 与时序契约

调用方必须先取**全部**锁（scope 文档 + provenance 文档 + 会话/轮次），**再**取 ``now``，
然后调用 ``validate_sources``。这样 ``now`` 不会早于 Document 行锁等待，过期判定不会
用陈旧时刻放行。

* ``lock=False``：生成期间的新鲜只读校验（项目路径会自己做一次无锁 Source/Snapshot 读取，
  不伪造绑定、不调用加锁函数）。
* ``lock=True``：最终事务校验；上传路径内部调用 ``lock_document_rows``。
* 已用 ``lock_document_rows`` / ``lock_project_binding`` 锁好并复核过的调用方，可传
  ``binding=held`` 再调 ``validate_sources(lock=False, ...)``：此时不再重复读
  Source/Snapshot（避免在自身锁之上重复 populate），只对已锁定行做一致性校验。
"""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import (
    CONVERSATION_MODE_CHAT,
    CONVERSATION_MODE_RAG,
    KNOWLEDGE_BASE_KIND_PROJECT,
    KNOWLEDGE_BASE_KIND_UPLOAD,
    Document,
    DocumentChunk,
    KnowledgeBase,
    ProjectChunk,
    ProjectFileParseStatus,
    ProjectFileVersion,
    ProjectSnapshot,
    ProjectSnapshotFile,
    ProjectSnapshotStatus,
    ProjectSource,
    User,
)
from app.services.document_lifecycle import is_retrievable, retrievable_conditions

# 快照格式版本。旧 RAG 轮次没有该字段（或值不是 1）一律整轮排除，不能当空来源。
SOURCE_SNAPSHOT_VERSION = 1
# 实际来源合计上限（去重后）。超界不得裁剪：整个轮次 fail closed。
MAX_SNAPSHOT_SOURCES = 256

# assistant 消息上携带本轮完整（可传递）来源元数据的显式字段名。
SOURCE_PROVENANCE_FIELD = "source_provenance"

SOURCE_KIND_DOCUMENT = "document"
SOURCE_KIND_PROJECT = "project"
SOURCE_KINDS = (SOURCE_KIND_DOCUMENT, SOURCE_KIND_PROJECT)
MODES = (CONVERSATION_MODE_RAG, CONVERSATION_MODE_CHAT)

# 与 source_documents.read_source / project_retrieval.load_project_citations 的跨度一致。
UPLOAD_MAX_CHUNK_SPAN = 4
PROJECT_MAX_CHUNK_SPAN = 8

_FILENAME_MAX = 512
_PATH_MAX = 2048
_FINGERPRINT_MAX = 64

_TRACE_KEYS = ("supplied_sources", "history_sources")
_PROJECT_STATUSES = (ProjectFileParseStatus.READY.value, ProjectFileParseStatus.FALLBACK.value)
# 项目文档在检索元数据里必须携带的权威身份键（连同 chunk 字段）。
_PROJECT_META_KEYS = ("file_version_id", "snapshot_id", "source_id", "relative_path",
                      "snapshot_manifest_fingerprint", "snapshot_config_fingerprint")


class SourceSnapshotError(ValueError):
    """来源快照不合法；调用方必须 fail closed（整轮排除 / 本轮失败）。"""


class SourceSnapshotUnavailable(SourceSnapshotError):
    """旧轮次缺少或损坏来源快照/provenance。"""


class SourceSnapshotOverflow(SourceSnapshotError):
    """去重后来源超过 ``MAX_SNAPSHOT_SOURCES``；本轮必须失败或整轮移除。"""


class SourceVerificationError(SourceSnapshotError):
    """落库前复核失败：来源已失效、漂移或不可读。"""


def _quiet(db: Session | None):
    """查询期间关闭 autoflush，避免把 pending 写入提前 flush 打乱锁顺序。"""
    return db.no_autoflush if db is not None else nullcontext()


# ---------------- 严格标量 ----------------

def _strict_uuid(value: Any, *, field: str) -> uuid.UUID:
    if isinstance(value, bool) or not isinstance(value, (str, uuid.UUID)):
        raise SourceSnapshotError(f"{field} 必须是 UUID")
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise SourceSnapshotError(f"{field} 不是合法 UUID") from exc


def _strict_int(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceSnapshotError(f"{field} 必须是整数（bool/float 不接受）")
    if value < minimum:
        raise SourceSnapshotError(f"{field} 不能小于 {minimum}")
    return value


def _strict_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceSnapshotError(f"{field} 必须是非空字符串")
    if len(value) > maximum:
        raise SourceSnapshotError(f"{field} 超过 {maximum} 字符")
    return value


def _optional_text(value: Any, *, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _strict_text(value, field=field, maximum=maximum)


def _chunk_range(item: Mapping, *, max_span: int) -> tuple[int, int, int]:
    index = _strict_int(item.get("chunk_index"), field="chunk_index")
    start = index if item.get("chunk_start") is None else _strict_int(item["chunk_start"], field="chunk_start")
    end = index if item.get("chunk_end") is None else _strict_int(item["chunk_end"], field="chunk_end")
    if not 0 <= start <= index <= end:
        raise SourceSnapshotError("chunk 范围必须满足 0 <= chunk_start <= chunk_index <= chunk_end")
    if end - start > max_span:
        raise SourceSnapshotError(f"chunk 跨度不能超过 {max_span}")
    return index, start, end


def _source_kind(item: Mapping, default_kind: str) -> str:
    # 只有“键缺省”才允许默认成 document；显式 null / 非法值一律拒绝。
    if "source_kind" not in item:
        raw = default_kind
    else:
        raw = item["source_kind"]
    if not isinstance(raw, str) or raw not in SOURCE_KINDS:
        raise SourceSnapshotError("source_kind 必须是 document 或 project")
    return raw


# ---------------- 单条严格解析 ----------------

def parse_source(item: Mapping, *, default_kind: str = SOURCE_KIND_DOCUMENT) -> dict:
    """解析并规范化单条来源；只保留身份字段，忽略 snippet/score 等正文辅助值。

    ``default_kind`` 只对**缺省** ``source_kind`` 生效（上传来源默认 document）。项目来源
    必须显式带 ``source_kind='project'`` 与完整项目身份，缺失即失败，绝不退化成上传
    文档身份。
    """
    if not isinstance(item, Mapping):
        raise SourceSnapshotError("来源必须是字典")
    kind = _source_kind(item, default_kind)
    if kind == SOURCE_KIND_DOCUMENT:
        if any(key in item and item.get(key) is not None for key in _PROJECT_META_KEYS):
            # 项目身份混进 document 来源：拒绝，避免错查上传 documents。
            raise SourceSnapshotError("document 来源带项目身份字段")
        document_id = _strict_uuid(item.get("document_id"), field="document_id")
        index_version = _strict_int(item.get("index_version"), field="index_version")
        chunk_index, chunk_start, chunk_end = _chunk_range(item, max_span=UPLOAD_MAX_CHUNK_SPAN)
        return {
            "source_kind": SOURCE_KIND_DOCUMENT,
            "document_id": str(document_id),
            "index_version": index_version,
            "chunk_index": chunk_index,
            "chunk_start": chunk_start,
            "chunk_end": chunk_end,
            "filename": _optional_text(item.get("filename"), field="filename", maximum=_FILENAME_MAX),
        }
    # project：document_id 就是不可变 file_version_id，必须与 file_version_id 一致。
    document_id = _strict_uuid(item.get("document_id"), field="document_id")
    file_version_id = _strict_uuid(item.get("file_version_id"), field="file_version_id")
    if document_id != file_version_id:
        raise SourceSnapshotError("项目来源的 document_id 必须等于 file_version_id")
    snapshot_id = _strict_uuid(item.get("snapshot_id"), field="snapshot_id")
    source_id = _strict_uuid(item.get("source_id"), field="source_id")
    chunk_index, chunk_start, chunk_end = _chunk_range(item, max_span=PROJECT_MAX_CHUNK_SPAN)
    return {
        "source_kind": SOURCE_KIND_PROJECT,
        "document_id": str(file_version_id),
        "file_version_id": str(file_version_id),
        "snapshot_id": str(snapshot_id),
        "source_id": str(source_id),
        "relative_path": _strict_text(item.get("relative_path"), field="relative_path", maximum=_PATH_MAX),
        "manifest_fingerprint": _strict_text(item.get("manifest_fingerprint"),
                                            field="manifest_fingerprint", maximum=_FINGERPRINT_MAX),
        "config_fingerprint": _strict_text(item.get("config_fingerprint"),
                                          field="config_fingerprint", maximum=_FINGERPRINT_MAX),
        "chunk_index": chunk_index,
        "chunk_start": chunk_start,
        "chunk_end": chunk_end,
    }


def _strict_field_int(source: Mapping, field: str) -> int:
    return _strict_int(source.get(field), field=field)


def source_key(source: Mapping) -> tuple:
    """粗身份：同一种来源、同一文档身份、同一代次与同一 chunk 跨度。

    用于“历史已由本轮提供”的判定。只接受已规范化的来源；非法输入抛错，不做宽松
    ``int()`` 强转。
    """
    if source.get("source_kind") == SOURCE_KIND_PROJECT:
        return (SOURCE_KIND_PROJECT, source.get("snapshot_id"), source.get("file_version_id"),
                _strict_field_int(source, "chunk_index"), _strict_field_int(source, "chunk_start"),
                _strict_field_int(source, "chunk_end"))
    return (SOURCE_KIND_DOCUMENT, source.get("document_id"),
            _strict_field_int(source, "index_version"), _strict_field_int(source, "chunk_index"),
            _strict_field_int(source, "chunk_start"), _strict_field_int(source, "chunk_end"))


def source_identity(source: Mapping) -> tuple:
    """完整身份：粗身份 + 全部安全元数据。

    项目来源必须带上 source_id / relative_path / manifest / config，否则同快照同文件
    同片段但安全元数据不同的两条会被误当重复而静默丢弃。非法输入抛错。
    """
    key = source_key(source)
    if source.get("source_kind") == SOURCE_KIND_PROJECT:
        return key + (source.get("source_id"), source.get("relative_path"),
                      source.get("manifest_fingerprint"), source.get("config_fingerprint"))
    return key + (source.get("filename"),)


def dedupe_sources(sources: Iterable[Mapping]) -> tuple[dict, ...]:
    """按首次出现顺序去重；粗身份相同但完整身份不同视为冲突，直接抛错。

    绝不静默丢掉安全元数据不一致的重复项——那会让坏项逃过复核。
    """
    result: list[dict] = []
    index_by_key: dict[tuple, int] = {}
    for source in sources:
        key = source_key(source)
        identity = source_identity(source)
        existing = index_by_key.get(key)
        if existing is None:
            index_by_key[key] = len(result)
            result.append(dict(source))
            continue
        if source_identity(result[existing]) != identity:
            raise SourceSnapshotError("同一来源身份出现冲突的安全元数据")
    return tuple(result)


def _check_limit(count: int) -> None:
    if count > MAX_SNAPSHOT_SOURCES:
        raise SourceSnapshotOverflow(
            f"来源合计 {count} 条，超过上限 {MAX_SNAPSHOT_SOURCES}；本轮必须失败或整轮移除")


def parse_sources(items: Any, *, default_kind: str = SOURCE_KIND_DOCUMENT) -> tuple[dict, ...]:
    """严格解析一组来源（list/tuple），去重后返回不可变元组。

    ``None`` 不是合法的来源集合（trace/provenance 缺键必须由调用方显式处理）。
    超界抛 ``SourceSnapshotOverflow``，不裁剪。
    """
    if not isinstance(items, (list, tuple)):
        raise SourceSnapshotError("来源集合必须是列表")
    parsed = [parse_source(item, default_kind=default_kind) for item in items]
    unique = dedupe_sources(parsed)
    _check_limit(len(unique))
    return unique


# ---------------- 从实际 prompt 对构造 ----------------

def _precheck_pair_metadata(meta: Mapping) -> str:
    """在 ``rag.to_citations`` 归一化**之前**严格校验原始 metadata，返回严格的来源类型。

    ``to_citations`` 使用 ``meta.get("index_version") or 0``、``meta.get("source_kind")``
    等宽松写法：缺失/None/bool/float 都会被压成 ``0``，未知 ``source_kind`` 会被当成上传
    文档。若先走它，坏值就被规范化掉了。因此这里先断言：

    * 上传来源（默认 document）**必须显式携带严格非负整数 ``index_version``**；
      缺省/None/True/1.0 一律拒绝，不按 legacy 宽松放行。
    * ``source_kind`` 只有**键缺省**才默认 document；显式 None / 未知字符串拒绝。
    * 项目来源不要求 ``index_version``，但要求完整项目身份与 fingerprint。
    """
    kind = _source_kind(meta, SOURCE_KIND_DOCUMENT)
    if kind == SOURCE_KIND_DOCUMENT:
        _strict_int(meta.get("index_version"), field="index_version")
        if any(meta.get(key) is not None for key in _PROJECT_META_KEYS):
            raise SourceSnapshotError("document 来源带项目身份字段")
    _strict_uuid(meta.get("document_id"), field="document_id")
    chunk_index = _strict_int(meta.get("chunk_index"), field="chunk_index")
    start = meta.get("chunk_start")
    end = meta.get("chunk_end")
    if start is not None:
        _strict_int(start, field="chunk_start")
    if end is not None:
        _strict_int(end, field="chunk_end")
    start = chunk_index if start is None else start
    end = chunk_index if end is None else end
    if not 0 <= start <= chunk_index <= end:
        raise SourceSnapshotError("chunk 范围必须满足 0 <= chunk_start <= chunk_index <= chunk_end")
    if kind == SOURCE_KIND_PROJECT:
        for key in _PROJECT_META_KEYS:
            if key not in meta or meta.get(key) is None:
                raise SourceSnapshotError(f"项目来源缺少 {key}")
        _strict_uuid(meta.get("file_version_id"), field="file_version_id")
        _strict_uuid(meta.get("snapshot_id"), field="snapshot_id")
        _strict_uuid(meta.get("source_id"), field="source_id")
        _strict_text(meta.get("relative_path"), field="relative_path", maximum=_PATH_MAX)
        _strict_text(meta.get("snapshot_manifest_fingerprint"), field="manifest_fingerprint",
                     maximum=_FINGERPRINT_MAX)
        _strict_text(meta.get("snapshot_config_fingerprint"), field="config_fingerprint",
                     maximum=_FINGERPRINT_MAX)
    return kind


def _pair_item(meta: Mapping) -> dict:
    """把严格校验过的原始 metadata 映射成快照来源字段（不经过宽松归一化）。"""
    kind = _precheck_pair_metadata(meta)
    item = {
        "source_kind": kind,
        "document_id": meta.get("document_id"),
        "chunk_index": meta.get("chunk_index"),
    }
    if meta.get("chunk_start") is not None:
        item["chunk_start"] = meta["chunk_start"]
    if meta.get("chunk_end") is not None:
        item["chunk_end"] = meta["chunk_end"]
    if kind == SOURCE_KIND_PROJECT:
        item.update({
            "file_version_id": meta.get("file_version_id"),
            "snapshot_id": meta.get("snapshot_id"),
            "source_id": meta.get("source_id"),
            "relative_path": meta.get("relative_path"),
            "manifest_fingerprint": meta.get("snapshot_manifest_fingerprint"),
            "config_fingerprint": meta.get("snapshot_config_fingerprint"),
        })
    else:
        item["index_version"] = meta.get("index_version")
        item["filename"] = meta.get("filename")
    return item


def sources_from_pairs(pairs: Sequence) -> tuple[dict, ...]:
    """由最终实际送入 prompt 的 ``(LCDocument, distance)`` 对构造来源。

    直接从原始 metadata 映射身份字段，**不经过** ``rag.to_citations``：后者用
    ``meta.get("index_version") or 0`` 与 ``meta.get("source_kind")`` 宽松归一化，会把
    缺失/bool/float 或未知类型“洗白”成合法值。任何一对 metadata 坏掉都整体失败。
    """
    items = []
    for pair in pairs or ():
        doc = pair[0] if isinstance(pair, (tuple, list)) and pair else None
        metadata = getattr(doc, "metadata", None)
        if not isinstance(metadata, Mapping):
            raise SourceSnapshotError("来源文档缺少 metadata")
        items.append(_pair_item(metadata))
    return parse_sources(items, default_kind=SOURCE_KIND_DOCUMENT)


# ---------------- 快照构建 / 读取 ----------------

def _snapshot_lists(snapshot: Mapping) -> tuple[list, list]:
    # trace/provenance 缺键或不是 list 一律视为“坏快照”，走整轮排除分类。
    for key in _TRACE_KEYS:
        if key not in snapshot or not isinstance(snapshot.get(key), list):
            raise SourceSnapshotUnavailable(f"{key} 必须是列表")
    return snapshot["supplied_sources"], snapshot["history_sources"]


def parse_source_snapshot(snapshot: Mapping, *, mode: str = CONVERSATION_MODE_RAG) -> tuple[dict, ...]:
    """解析 v1 快照（trace 与 assistant ``source_provenance`` 共用同一形状）。

    * 版本必须严格为整数 1；两个来源键必须存在且都是 list。
    * 普通对话只允许**显式空**快照；RAG 允许空（明确未检索到资料）。
    """
    if mode not in MODES:
        raise SourceSnapshotError("未知会话模式")
    if not isinstance(snapshot, Mapping):
        raise SourceSnapshotUnavailable("快照不是字典")
    version = snapshot.get("source_snapshot_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SOURCE_SNAPSHOT_VERSION:
        raise SourceSnapshotUnavailable("source_snapshot_version 不是 1")
    supplied_raw, history_raw = _snapshot_lists(snapshot)
    supplied = parse_sources(supplied_raw, default_kind=SOURCE_KIND_DOCUMENT)
    historical = parse_sources(history_raw, default_kind=SOURCE_KIND_DOCUMENT)
    combined = parse_sources([*supplied, *historical])
    if mode == CONVERSATION_MODE_CHAT and combined:
        raise SourceSnapshotError("普通对话不允许携带来源")
    return combined


def sources_from_trace(trace: Mapping, *, mode: str) -> tuple[dict, ...]:
    """读取 trace 中的 v1 来源；返回 ``supplied + history``（去重、限 256）。

    * ``mode == 'rag'``：旧轮次或损坏轮次（缺版本 / 缺键 / 键不是 list）抛
      ``SourceSnapshotUnavailable``，调用方**整轮排除**。
    * ``mode == 'chat'``：trace 完全没有快照字段（且没有非空来源）视为明确的空来源；
      一旦出现来源或快照字段，就必须是合法的 v1 空快照。
    """
    if mode not in MODES:
        raise SourceSnapshotError("未知会话模式")
    if not isinstance(trace, Mapping):
        raise SourceSnapshotUnavailable("trace 不是字典")
    # chat 的“无痕旧数据”豁免必须三个快照键**完全不存在**；显式 None / list / tuple /
    # string 等坏字段一律不算空来源。
    if not any(key in trace for key in ("source_snapshot_version", *_TRACE_KEYS)):
        if mode == CONVERSATION_MODE_CHAT:
            return ()
        raise SourceSnapshotUnavailable("缺少 source_snapshot_version")
    return parse_source_snapshot(trace, mode=mode)


def sources_from_history(
    history: Sequence[Mapping] | None, *, mode: str = CONVERSATION_MODE_RAG,
) -> tuple[dict, ...]:
    """从最终 retained 历史里收集**可传递来源元数据**。

    每个保留的 assistant 条目必须在 ``source_provenance`` 字段携带自己完整（含继承自更早
    轮次）的 v1 快照。只读最终 ``citations`` 是不够的：原轮提供了 A、B 但只引用 A 时，
    后续改写仍受 B 影响，B 也必须跟随。缺字段/坏字段 => ``SourceSnapshotUnavailable``，
    调用方整轮排除。

    普通对话的 assistant 也必须带显式空快照；仅在完全没有来源痕迹的旧数据上容忍缺省。
    """
    if mode not in MODES:
        raise SourceSnapshotError("未知会话模式")
    if history is None:
        history = ()
    if not isinstance(history, (list, tuple)):
        raise SourceSnapshotError("历史必须是列表")
    collected: list[dict] = []
    for item in history:
        if not isinstance(item, Mapping):
            raise SourceSnapshotUnavailable("历史条目必须是字典")
        if item.get("role") != "assistant":
            continue
        # 显式 None 不是“无痕旧记录”：只有键完全不存在才可能是旧数据。
        if SOURCE_PROVENANCE_FIELD not in item:
            if mode == CONVERSATION_MODE_CHAT and not item.get("citations"):
                continue
            raise SourceSnapshotUnavailable(f"历史助手消息缺少 {SOURCE_PROVENANCE_FIELD}")
        collected.extend(parse_source_snapshot(item[SOURCE_PROVENANCE_FIELD], mode=mode))
    unique = dedupe_sources(collected)
    _check_limit(len(unique))
    return unique


def build_source_snapshot(
    supplied_sources: Sequence[Mapping],
    history: Sequence[Mapping] | None,
    *,
    mode: str = CONVERSATION_MODE_RAG,
) -> dict:
    """构建 v1 快照（同时写进 trace 与 assistant ``source_provenance``）。

    超界抛 ``SourceSnapshotOverflow``——不裁剪来源；调用方必须让本轮失败或整轮移除。
    """
    if mode not in MODES:
        raise SourceSnapshotError("未知会话模式")
    # supplied=None 不是“没有来源”：调用方必须显式传 []。显式 None/list 之外的值同样拒绝。
    supplied = parse_sources(supplied_sources, default_kind=SOURCE_KIND_DOCUMENT)
    historical = sources_from_history(history, mode=mode)
    combined = parse_sources([*supplied, *historical])
    if mode == CONVERSATION_MODE_CHAT and combined:
        raise SourceSnapshotError("普通对话不允许携带来源")
    supplied_keys = {source_key(item) for item in supplied}
    historical_only = [item for item in historical if source_key(item) not in supplied_keys]
    return {
        "source_snapshot_version": SOURCE_SNAPSHOT_VERSION,
        "supplied_sources": [dict(item) for item in supplied],
        "history_sources": [dict(item) for item in historical_only],
    }


# ---------------- 第 1 段锁：User -> KnowledgeBase ----------------

@dataclass(frozen=True)
class SourceRoot:
    user: User
    knowledge_base: KnowledgeBase | None


def lock_root(db: Session, user_id: uuid.UUID, kb_id: uuid.UUID | None) -> SourceRoot:
    """最终事务第一段：按 ``User -> KnowledgeBase`` 取行写锁。

    普通对话 ``kb_id is None`` 时只锁用户。必须在任何会话/轮次锁之前调用；
    helper 不 commit/rollback。
    """
    with _quiet(db):
        user = db.scalar(select(User).where(User.id == user_id)
                         .with_for_update().execution_options(populate_existing=True))
        if user is None:
            raise SourceVerificationError("用户不存在")
        kb = None
        if kb_id is not None:
            kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
                           .with_for_update().execution_options(populate_existing=True))
            if kb is None:
                raise SourceVerificationError("知识库不存在")
    return SourceRoot(user=user, knowledge_base=kb)


# ---------------- 第 2 段锁：ProjectSource -> ProjectSnapshot ----------------

@dataclass(frozen=True)
class ProjectBinding:
    snapshots: dict[uuid.UUID, ProjectSnapshot]
    sources: dict[uuid.UUID, ProjectSource]


def _snapshot_ids(sources) -> list[uuid.UUID]:
    """从来源字典或原始 snapshot id 序列取快照 id（去重、按 UUID 排序）。"""
    result: set[uuid.UUID] = set()
    for item in sources or ():
        if isinstance(item, Mapping):
            value = item.get("snapshot_id")
            if value is None:
                continue
            result.add(uuid.UUID(str(value)))
        else:
            result.add(uuid.UUID(str(item)))
    return sorted(result)


def _authorized(kb: KnowledgeBase, user: User) -> bool:
    """统一 ACL：用户必须启用，且管理员/公开库/库拥有者之一。"""
    if getattr(user, "is_active", None) is not True:
        return False
    return bool(user.is_admin or kb.is_public or kb.owner_id == user.id)


def _require_acl(kb: KnowledgeBase, user: User) -> None:
    if not _authorized(kb, user):
        raise SourceVerificationError("无权读取该知识库的来源")


def _lock_project_rows(db: Session, snapshot_ids: Sequence[uuid.UUID], *, lock: bool) -> ProjectBinding:
    """按 ``ProjectSource.id`` -> ``ProjectSnapshot.id`` 的全局 UUID 顺序读取（可选加锁）。"""
    if not snapshot_ids:
        return ProjectBinding(snapshots={}, sources={})
    with _quiet(db):
        pairs = db.execute(
            select(ProjectSnapshot.id, ProjectSnapshot.source_id)
            .where(ProjectSnapshot.id.in_(list(snapshot_ids)))
        ).all()
        expected = {snapshot_id: source_id for snapshot_id, source_id in pairs}
        if set(expected) != set(snapshot_ids):
            raise SourceVerificationError("来源快照不存在")
        source_ids = sorted(set(expected.values()))
        source_stmt = (select(ProjectSource).where(ProjectSource.id.in_(source_ids))
                       .order_by(ProjectSource.id))
        if lock:
            source_stmt = source_stmt.with_for_update(of=ProjectSource)
        source_rows = db.scalars(source_stmt.execution_options(populate_existing=True)).all()
        sources_by_id = {row.id: row for row in source_rows}
        if set(sources_by_id) != set(source_ids):
            raise SourceVerificationError("来源快照缺少项目源")
        snap_stmt = (select(ProjectSnapshot).where(ProjectSnapshot.id.in_(list(snapshot_ids)))
                     .order_by(ProjectSnapshot.id))
        if lock:
            snap_stmt = snap_stmt.with_for_update()
        snapshot_rows = db.scalars(snap_stmt.execution_options(populate_existing=True)).all()
    snapshots = {row.id: row for row in snapshot_rows}
    if set(snapshots) != set(snapshot_ids):
        raise SourceVerificationError("来源快照不存在")
    for snapshot_id, source_id in expected.items():
        # 加锁后复核绑定关系未漂移（锁前读到的 source_id 必须与锁定行一致）。
        if snapshots[snapshot_id].source_id != source_id:
            raise SourceVerificationError("来源快照绑定在加锁期间发生变化")
    return ProjectBinding(snapshots=snapshots, sources=sources_by_id)


def lock_project_binding(
    db: Session,
    sources: Sequence[Mapping] | Sequence[uuid.UUID],
    *,
    kb: KnowledgeBase,
    user: User,
) -> ProjectBinding:
    """项目绑定锁：``ProjectSource``（UUID 序）-> ``ProjectSnapshot``（UUID 序）。

    必须在会话/轮次锁之前、``lock_root`` 之后调用。项目来源一旦在这里锁定并复核，
    后续 ``Conversation.project_snapshot_id`` / ``ChatTurn.project_snapshot_id`` 的写入与
    会话内既有绑定校验即可在同一根上串行。缺失 / 未 ready / 已撤销一律拒绝。
    """
    snapshot_ids = _snapshot_ids(sources)
    if not snapshot_ids:
        return ProjectBinding(snapshots={}, sources={})
    if kb.kind != KNOWLEDGE_BASE_KIND_PROJECT:
        raise SourceVerificationError("非项目知识库不能锁定项目绑定")
    _require_acl(kb, user)
    binding = _lock_project_rows(db, snapshot_ids, lock=True)
    _check_binding_ready(binding, kb)
    return binding


def _read_project_binding(
    db: Session,
    sources: Sequence[Mapping] | Sequence[uuid.UUID],
    *,
    kb: KnowledgeBase,
    user: User,
) -> ProjectBinding:
    """生成期间的无锁新鲜读取：真读 Source/Snapshot，不伪造绑定、不调用加锁函数。"""
    snapshot_ids = _snapshot_ids(sources)
    if not snapshot_ids:
        return ProjectBinding(snapshots={}, sources={})
    if kb.kind != KNOWLEDGE_BASE_KIND_PROJECT:
        raise SourceVerificationError("非项目知识库不能读取项目绑定")
    _require_acl(kb, user)
    binding = _lock_project_rows(db, snapshot_ids, lock=False)
    _check_binding_ready(binding, kb)
    return binding


def read_project_binding(
    db: Session,
    sources: Sequence[Mapping] | Sequence[uuid.UUID],
    *,
    kb: KnowledgeBase,
    user: User,
) -> ProjectBinding:
    """``lock=False`` 的新鲜只读绑定（公开入口）。真读 Source/Snapshot，不加锁、不提交。"""
    return _read_project_binding(db, sources, kb=kb, user=user)


def _check_binding_ready(binding: ProjectBinding, kb: KnowledgeBase) -> None:
    for snapshot_id, snapshot in binding.snapshots.items():
        source_id = snapshot.source_id
        owner = binding.sources.get(source_id)
        if owner is None:
            raise SourceVerificationError("来源快照缺少项目源")
        if owner.kb_id != kb.id:
            raise SourceVerificationError("来源快照不属于当前知识库")
        if owner.revoked_at is not None:
            raise SourceVerificationError("项目源已撤销")
        if snapshot.status != ProjectSnapshotStatus.READY.value:
            raise SourceVerificationError("来源快照未就绪")
        if not snapshot.manifest_fingerprint or not snapshot.config_fingerprint:
            raise SourceVerificationError("来源快照缺少 manifest/config 指纹")


# ---------------- 第 3 段锁：上传 Document（必须在 Message 之后） ----------------

def lock_document_ids(db: Session, kb: KnowledgeBase, document_ids: Iterable[uuid.UUID]) -> tuple[uuid.UUID, ...]:
    """按 UUID 排序锁一批上传文档行（scope 与 provenance 来源合并后的锁集合）。

    必须在 Conversation/ChatTurn/Message 的写入之后调用。只锁行，不判定可检索；
    不把 scope 文档伪装成 chunk 来源。空集合不加锁。
    """
    if kb.kind != KNOWLEDGE_BASE_KIND_UPLOAD:
        raise SourceVerificationError("非上传知识库不能锁定文档行")
    identifiers = sorted({item if isinstance(item, uuid.UUID) else uuid.UUID(str(item))
                          for item in document_ids or ()})
    if not identifiers:
        return ()
    with _quiet(db):
        rows = db.scalars(
            select(Document).where(Document.id.in_(identifiers), Document.kb_id == kb.id)
            .order_by(Document.id)
            .with_for_update().execution_options(populate_existing=True)
        ).all()
    found = {row.id for row in rows}
    if any(identifier not in found for identifier in identifiers):
        raise SourceVerificationError("锁定的文档不存在或不属于当前知识库")
    return tuple(identifiers)


def lock_document_rows(db: Session, kb: KnowledgeBase, sources: Sequence[Mapping]) -> tuple[uuid.UUID, ...]:
    """按 UUID 排序锁上传 ``documents`` 行；只锁行，不做可检索判定。

    必须在 Conversation/ChatTurn/Message 的写入之后调用。项目来源一律拒绝。
    锁完之后调用方应再取 ``now``，然后 ``validate_sources(lock=False, ...)``。
    """
    if kb.kind != KNOWLEDGE_BASE_KIND_UPLOAD:
        raise SourceVerificationError("非上传知识库不能锁定文档行")
    if any(source.get("source_kind") != SOURCE_KIND_DOCUMENT for source in sources):
        raise SourceVerificationError("上传文档锁定收到非 document 来源")
    return lock_document_ids(db, kb, (source["document_id"] for source in sources))


# ---------------- 复核：上传文档 ----------------

def validate_document_sources(
    db: Session, kb: KnowledgeBase, sources: Sequence[Mapping], *, user: User,
    now: datetime | None = None,
) -> tuple[dict, ...]:
    """上传文档来源复核：统一可检索谓词 + 代次一致 + chunk 全覆盖。不加锁、不提交。"""
    if kb.kind == KNOWLEDGE_BASE_KIND_PROJECT:
        raise SourceVerificationError("项目知识库不能按上传文档复核")
    if kb.kind != KNOWLEDGE_BASE_KIND_UPLOAD:
        raise SourceVerificationError("知识库类型不支持上传文档复核")
    _require_acl(kb, user)
    if not sources:
        return ()
    if any(source.get("source_kind") != SOURCE_KIND_DOCUMENT for source in sources):
        raise SourceVerificationError("上传文档复核收到非 document 来源")
    identifiers = sorted({uuid.UUID(str(source["document_id"])) for source in sources})
    with _quiet(db):
        rows = db.scalars(
            select(Document)
            .where(Document.id.in_(identifiers), Document.kb_id == kb.id,
                   *retrievable_conditions(now=now))
            .order_by(Document.id)
            .execution_options(populate_existing=True)
        ).all()
        found = {row.id: row for row in rows}
        for identifier in identifiers:
            if identifier not in found:
                raise SourceVerificationError("来源文档不存在或当前不可检索")
        for source in sources:
            row = found[uuid.UUID(str(source["document_id"]))]
            if not is_retrievable(row, now=now):
                raise SourceVerificationError("来源文档当前不可检索")
            if int(row.index_version) != int(source["index_version"]):
                raise SourceVerificationError("来源文档已重新索引，代次不一致")

        ranges = sorted({(uuid.UUID(str(source["document_id"])),
                          int(source["chunk_start"]), int(source["chunk_end"])) for source in sources})
        present = {(row[0], row[1]) for row in db.execute(
            select(DocumentChunk.document_id, DocumentChunk.chunk_index)
            .where(DocumentChunk.embedded.is_(True),
                   or_(*(and_(DocumentChunk.document_id == identifier,
                              DocumentChunk.chunk_index.between(start, end))
                         for identifier, start, end in ranges)))
        ).all()}
    for identifier, start, end in ranges:
        if not {(identifier, index) for index in range(start, end + 1)}.issubset(present):
            raise SourceVerificationError("来源片段已不可用")
    return tuple(dict(source) for source in sources)


# ---------------- 复核：项目来源（绝不查上传 Document） ----------------

def _project_chunk_rows(db: Session, sources: Sequence[Mapping]):
    """只按 (snapshot, file_version, 请求的 chunk 范围) 精确取行。

    不做“按 file_version 拉全部 chunk”。上界约 ``256 × 9`` 行。
    """
    conditions = [
        and_(ProjectSnapshotFile.snapshot_id == uuid.UUID(str(source["snapshot_id"])),
             ProjectSnapshotFile.file_version_id == uuid.UUID(str(source["file_version_id"])),
             ProjectChunk.chunk_index.between(int(source["chunk_start"]), int(source["chunk_end"])))
        for source in sources
    ]
    with _quiet(db):
        return db.execute(
            select(ProjectSnapshotFile.snapshot_id, ProjectSnapshotFile.file_version_id,
                   ProjectFileVersion.source_id, ProjectFileVersion.relative_path,
                   ProjectFileVersion.parse_status, ProjectChunk.chunk_index)
            .join(ProjectFileVersion, ProjectFileVersion.id == ProjectSnapshotFile.file_version_id)
            .join(ProjectChunk, ProjectChunk.file_version_id == ProjectFileVersion.id)
            .where(or_(*conditions), ProjectChunk.embedded.is_(True))
        ).all()


def validate_project_sources(
    db: Session,
    kb: KnowledgeBase,
    sources: Sequence[Mapping],
    *,
    user: User,
    binding: ProjectBinding,
    now: datetime | None = None,
) -> tuple[dict, ...]:
    """项目来源复核：Source -> Snapshot -> 映射 -> 文件版本 -> chunk，以及 manifest/config。

    绝不读取上传 ``Document``。``binding`` 必须是本次事务中 ``lock_project_binding`` 的
    返回值（或 ``validate_sources`` 已取得的新鲜绑定），用来证明快照已在会话锁之前锁定并
    复核；这里不再重复读取 Source/Snapshot。
    """
    if kb.kind != KNOWLEDGE_BASE_KIND_PROJECT:
        raise SourceVerificationError("非项目知识库不能按项目来源复核")
    _require_acl(kb, user)
    if not sources:
        return ()
    if any(source.get("source_kind") != SOURCE_KIND_PROJECT for source in sources):
        raise SourceVerificationError("项目复核收到非 project 来源")
    if binding is None:
        raise SourceVerificationError("项目复核缺少已锁定的绑定")

    snapshot_ids = _snapshot_ids(sources)
    if set(snapshot_ids) - set(binding.snapshots):
        raise SourceVerificationError("来源快照不在已锁定绑定中")

    for source in sources:
        snapshot = binding.snapshots[uuid.UUID(str(source["snapshot_id"]))]
        owner = binding.sources.get(snapshot.source_id)
        if owner is None or owner.revoked_at is not None:
            raise SourceVerificationError("项目源已撤销或不存在")
        if owner.kb_id != kb.id:
            raise SourceVerificationError("来源快照不属于当前知识库")
        if snapshot.status != ProjectSnapshotStatus.READY.value:
            raise SourceVerificationError("来源快照未就绪")
        if source["source_id"] != str(snapshot.source_id):
            raise SourceVerificationError("来源声明的项目源与快照不一致")
        if source["manifest_fingerprint"] != snapshot.manifest_fingerprint:
            raise SourceVerificationError("manifest 指纹不一致")
        if source["config_fingerprint"] != snapshot.config_fingerprint:
            raise SourceVerificationError("config 指纹不一致")

    rows = _project_chunk_rows(db, sources)
    mapped: dict[tuple[uuid.UUID, uuid.UUID], dict] = {}
    for snapshot_id, file_version_id, source_id, relative_path, parse_status, chunk_index in rows:
        entry = mapped.setdefault((snapshot_id, file_version_id), {
            "source_id": source_id, "relative_path": relative_path,
            "parse_status": parse_status, "chunks": set(),
        })
        if entry["source_id"] != source_id or entry["relative_path"] != relative_path:
            raise SourceVerificationError("来源映射身份不一致")
        entry["chunks"].add(int(chunk_index))

    for source in sources:
        key = (uuid.UUID(str(source["snapshot_id"])), uuid.UUID(str(source["file_version_id"])))
        entry = mapped.get(key)
        if entry is None:
            raise SourceVerificationError("文件版本不在快照映射中")
        if entry["source_id"] != binding.snapshots[key[0]].source_id:
            raise SourceVerificationError("文件版本与快照不同源")
        if entry["parse_status"] not in _PROJECT_STATUSES:
            raise SourceVerificationError("文件版本尚未解析完成")
        if entry["relative_path"] != source["relative_path"]:
            raise SourceVerificationError("文件路径已漂移")
        if not set(range(int(source["chunk_start"]), int(source["chunk_end"]) + 1)).issubset(entry["chunks"]):
            raise SourceVerificationError("来源片段已不可用")
    return tuple(dict(source) for source in sources)


# ---------------- 统一入口 ----------------

def validate_sources(
    db: Session,
    kb: KnowledgeBase | None,
    sources: Sequence[Mapping],
    *,
    user: User,
    lock: bool = False,
    binding: ProjectBinding | None = None,
    now: datetime | None = None,
) -> tuple[dict, ...]:
    """按知识库类型分派复核；空来源直接通过（普通对话）。

    * ``lock=False``：生成期间的新鲜只读校验。项目路径自己无锁读取 Source/Snapshot。
    * ``lock=True``：最终事务校验；上传路径内部 ````lock_document_rows``（调用方必须已进入
      Message 之后的阶段），项目路径使用此行拿到的绑定。
    * 已经把 ``binding`` 锁好并复核过的调用方，可传 ``binding=held`` 再以 ``lock=False``
      调用：不再重复读取 Source/Snapshot，只做已锁定行的一致性校验。

    调用方必须先取全部锁、再取 ``now``、再调用本函数。不 commit、不 rollback。
    """
    parsed = parse_sources(sources, default_kind=SOURCE_KIND_DOCUMENT)
    if not parsed:
        return ()
    if kb is None:
        raise SourceVerificationError("有来源但没有知识库")
    kinds = {source["source_kind"] for source in parsed}
    if len(kinds) != 1:
        raise SourceVerificationError("一次回答不能混用不同来源类型")
    if kb.kind == KNOWLEDGE_BASE_KIND_PROJECT:
        held = binding or (lock_project_binding(db, parsed, kb=kb, user=user) if lock
                           else _read_project_binding(db, parsed, kb=kb, user=user))
        return validate_project_sources(db, kb, parsed, user=user, binding=held, now=now)
    if kb.kind == KNOWLEDGE_BASE_KIND_UPLOAD:
        if lock:
            lock_document_rows(db, kb, parsed)
        return validate_document_sources(db, kb, parsed, user=user, now=now)
    raise SourceVerificationError("未知知识库类型")


__all__ = [
    "MAX_SNAPSHOT_SOURCES",
    "MODES",
    "PROJECT_MAX_CHUNK_SPAN",
    "SOURCE_KIND_DOCUMENT",
    "SOURCE_KIND_PROJECT",
    "SOURCE_PROVENANCE_FIELD",
    "SOURCE_SNAPSHOT_VERSION",
    "UPLOAD_MAX_CHUNK_SPAN",
    "ProjectBinding",
    "SourceRoot",
    "SourceSnapshotError",
    "SourceSnapshotOverflow",
    "SourceSnapshotUnavailable",
    "SourceVerificationError",
    "build_source_snapshot",
    "dedupe_sources",
    "lock_document_ids",
    "lock_document_rows",
    "lock_project_binding",
    "lock_root",
    "parse_source",
    "parse_source_snapshot",
    "parse_sources",
    "read_project_binding",
    "source_identity",
    "source_key",
    "sources_from_history",
    "sources_from_pairs",
    "sources_from_trace",
    "validate_document_sources",
    "validate_project_sources",
    "validate_sources",
]