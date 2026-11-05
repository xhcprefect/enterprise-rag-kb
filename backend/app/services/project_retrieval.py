"""项目独立检索 / 引用核对 / 快照绑定（Q 独占）。

安全与正确性：
- 向量 SQL 与关键词 SQL 都在**排名 + LIMIT 之前** JOIN 指定 SnapshotFile，并限定
  snapshot ready + 源未 revoke + fileversion parse ready/fallback + chunk embedded
  + **快照映射与 FV 同源**（ProjectSnapshot.source_id == ProjectFileVersion.source_id，
  绝不只信脏 cmeta）+ 当前用户 KB ACL + 可选相对 path_prefix（等值或 prefix + '/'，
  绝不把 src 误配成 src-old）。
- 检索身份一律来自已授权行的权威列（ProjectFileVersion.id / relative_path /
  language / parse_status / content_sha256、ProjectSource.id、ProjectSnapshot.id），
  cmeta 只是辅助副数据，绝不覆盖权威值；parse_status 反映真实 FALLBACK/READY，不伪造。
- 网络 embed/rerank 等待期间先释放 DB 事务（不持打开事务/不 hold checkout 连接）；
  回包后 **fresh 重读授权**——等待期间撤销/改权限/快照不再 mapping/不再 ready 的
  结果一律丢弃且可抛 ProjectSourceRevoked/NotReady，绝不外泄内容。
- fusion 身份用 file_version_id（document_id=file_version_id），同 basename / 不同源
  不合并；chunk_index + index_version=0 在同身份内区分分块。
- 引用读取只查真实请求的 FV（<=20 项、每项 <=9 chunk，SQL 有界 IN 子句），客户端
  source/path/parse_status 等**不能**覆盖权威列；内容有硬字符界（MAX_PREVIEW_CHARS）。
- 公开入口签名向后兼容：project_retrieve_documents / retrieve_project / search_project
  / load_project_citations / default_project_snapshot / validate_project_binding。
"""
from __future__ import annotations

import re
import uuid
from typing import Sequence

from langchain_core.documents import Document as LCDocument
from sqlalchemy import Integer, String, and_, cast, false, func, or_, select
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.models import (
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
from app.schemas.project import (
    MAX_PREVIEW_CHARS,
    MAX_SEARCH_QUERY,
    MAX_TOP_K,
    ProjectSearchHit,
    ProjectSearchRead,
)
from app.services import project_sources
from app.services.hybrid_search import fuse_results, keyword_terms, rerank_results
from app.services.project_paths import split_relative_path
from app.services.project_terms import code_terms
from app.services.vector_store import get_embeddings, get_vector_store

logger = __import__("logging").getLogger(__name__)

# tsquery 表达式上限（词元个数与拼接后字符串双重有界，防畸形超长表达式）。
_TSQUERY_MAX_CHARS = 2000
_TSQUERY_MAX_TERMS = 128
# 引用读取每项最多覆盖的 chunk 跨度（含锚点所在 chunk）。
_CITATION_MAX_SPAN = 8


class ProjectSnapshotNotReady(Exception):
    """目标快照未 ready（或不存在），外部读/搜不得把空结果当作成功。"""


class ProjectPrefixError(ValueError):
    """path_prefix 非法（绝对/盘符/UNC/.. /控制字符等），检索前即拒绝。"""


def _release_db(db: DBSession) -> None:
    """结束当前读事务，释放连接，供网络等待期间不暂占任何 DB 资源。"""
    try:
        if db.in_transaction():
            db.commit()
    except Exception:  # noqa: BLE001 只读阶段 commit 失败则回滚，绝不残留打开事务
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def validate_project_binding(
    db: DBSession, snapshot_id: uuid.UUID, user: User
) -> tuple[ProjectSnapshot, ProjectSource, KnowledgeBase]:
    """校验并返回授权快照绑定。未授权/不存在 404，源已撤销 410。

    **必须从 DB 重新读**：``SessionLocal`` 配了 ``expire_on_commit=False``，网络/重排
    等待期间 ``_release_db`` 的 commit 不会使 identity map 里的对象过期；若沿用
    ``db.get`` 的缓存对象，等待期间发生的撤销/改权限/快照不再 ready 都会被旧值
    掩盖，最终把已失效快照的内容当成成功返回。这里统一走 ``populate_existing``
    的显式查询，强制覆盖这三类变化。调用方仍可继续使用返回的 ORM 对象（已刷新）。
    """
    snap = db.execute(
        select(ProjectSnapshot)
        .where(ProjectSnapshot.id == snapshot_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if snap is None or snap.status != ProjectSnapshotStatus.READY.value:
        raise ProjectSnapshotNotReady()
    source = db.execute(
        select(ProjectSource)
        .where(ProjectSource.id == snap.source_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if source is None:
        # 快照指向已不存在的源：无法行使 ACL，按不可读（404 语义）处理。
        raise project_sources.ProjectSourceNotFound()
    kb = db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.id == source.kb_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if kb is None:
        raise project_sources.ProjectSourceNotFound()
    # ACL 先于 revoke：私有已 revoke 源对无权限 stranger 呈 404，而非 410。
    # 只有授权（管理员/拥有者）才见到 revoke 语义（410）。
    if not user.is_admin and not kb.is_public and kb.owner_id != user.id:
        raise project_sources.ProjectSourceNotFound()
    if source.revoked_at is not None:
        raise project_sources.ProjectSourceRevoked()
    return snap, source, kb


# ---------------- 词元 / tsquery（含中文，code-aware，防注入） ----------------

_TERM_KEEP = re.compile(r"[A-Za-z0-9_〇一-鿿]")


def _clean_term(term: str) -> str | None:
    """只保留字母数字/下划线/CJK 词元（含中文），其余替换为空格去留。"""
    t = _TERM_KEEP.sub(" ", term or "").strip()
    if not t or not re.search(r"[A-Za-z0-9_〇一-鿿]", t):
        return None
    return t


def _tsquery_quote(term: str) -> str:
    """把单个安全词元包成 tsquery 短语字面量，杜绝查询语法注入。"""
    return '"' + term.replace('"', '""') + '"'


def _keyword_tsquery(query: str) -> str | None:
    """由 code_terms + keyword_terms 构造 OR 检索表达式（含中文，纯绑定参数）。

    只保留字母数字/下划线/CJK 词元；每个词元以双引号短语字面量包裹，词元本身只含
    安全字符，因此即使用户输入 ``& | ! ( ) : * '`` 也不会注入 tsquery 语法。空表达式
    返回 None（调用方跳过关键词检索）。长度与词元数均有界。
    """
    terms: list[str] = []
    for t in code_terms(query) + keyword_terms(query, max_terms=_TSQUERY_MAX_TERMS):
        cleaned = _clean_term(t)
        if cleaned and cleaned not in terms:
            terms.append(cleaned)
    if not terms:
        return None
    joined = " | ".join(_tsquery_quote(t) for t in terms)
    return joined[:_TSQUERY_MAX_CHARS]


# ---------------- path_prefix 规范化（绝/绝对与 .. 等） ----------------

def normalize_path_prefix(prefix: str | None) -> tuple[str, ...] | None:
    """规范化相对 path_prefix 为正斜杠组件元组；None -> None；空 -> ()。

    拒绝绝对/盘符/UNC/反斜杠与 ``..`` /控制字符（复用 P 的 split_relative_path）。
    非法即抛 ProjectPrefixError（检索前 fail，绝不 strip 成"看起来相对"再放行）。
    """
    if prefix is None:
        return None
    raw = prefix or ""
    if "\\" in raw:
        raise ProjectPrefixError("path_prefix 不允许反斜杠，请使用 /")
    if raw.startswith(("/", "\\\\")) or (len(raw) >= 2 and raw[1] == ":"):
        raise ProjectPrefixError("path_prefix 必须是相对路径，不接受绝对/盘符/UNC")
    raw = raw.strip("/")
    if not raw:
        return ()
    try:
        return split_relative_path(raw)
    except ValueError as exc:
        raise ProjectPrefixError(f"path_prefix 含非法路径组件: {exc}") from exc


def _prefix_str(parts: tuple[str, ...] | None) -> str | None:
    if not parts:
        return None
    return "/".join(parts)


def _like_escape(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _like_path_prefix(column, prefix_str: str) -> object:
    """SQL 侧的路径过滤：等值或 prefix + '/'。绝不把 src 误匹配成 src-old。"""
    return or_(
        column == prefix_str,
        column.like(_like_escape(prefix_str) + "/%", escape="!"),
    )


def _path_matches(relative_path: str, parts: tuple[str, ...] | None) -> bool:
    if not parts:
        return True
    prefix = "/".join(parts)
    norm = (relative_path or "").strip("/")
    return norm == prefix or norm.startswith(prefix + "/")


# ---------------- 候选文档构造 ----------------

def _project_doc(meta: dict, content: str, chunk_index: int) -> LCDocument:
    """构造带融合身份的项目文档。document_id=file_version_id 区分不同源身份。"""
    m = dict(meta or {})
    m["document_id"] = str(m.get("file_version_id") or "")
    m["chunk_index"] = chunk_index
    m["index_version"] = 0
    m["source_kind"] = "project"
    return LCDocument(page_content=content, metadata=m)


def _hit_meta(
    *,
    cmeta: dict,
    file_version_id,
    relative_path: str,
    language: str | None,
    parse_status: str,
    sha: str,
    ls: int | None,
    le: int | None,
    sym: str | None,
    sym_kind: str | None,
    ci: int,
    snap_id: uuid.UUID,
    source_id: uuid.UUID,
    snapshot_config: str | None,
    snapshot_manifest: str | None,
    fv_config: str | None,
    fv_parser: str | None,
) -> dict:
    """权威元数据一律来自 SQL 选中列；cmeta 仅作保留的辅助副数据，不覆盖权威值。"""
    meta = dict(cmeta or {})
    meta.update(
        {
            "document_id": str(file_version_id),
            "file_version_id": str(file_version_id),
            "snapshot_id": str(snap_id),
            "source_id": str(source_id),
            "chunk_index": ci,
            "index_version": 0,
            "source_kind": "project",
            "relative_path": relative_path,
            # 权威文件名：检索与历史引用统一取快照内相对路径。
            "filename": relative_path,
            "language": language,
            "parse_status": parse_status,
            "content_sha256": sha,
            "line_start": ls,
            "line_end": le,
            "symbol": sym,
            "symbol_kind": sym_kind,
            "snapshot_config_fingerprint": snapshot_config,
            "snapshot_manifest_fingerprint": snapshot_manifest,
            "file_config_fingerprint": fv_config,
            "file_parser_fingerprint": fv_parser,
        }
    )
    return meta


# ---------------- 共享过滤条件（排名/LIMIT 之前应用） ----------------

def normalize_file_version_ids(
    file_version_ids: Sequence | None,
) -> tuple[uuid.UUID, ...] | None:
    """规范化可选的文件范围过滤；``None`` 表示不限制，空序列表示**无结果**。

    - ``None`` -> ``None``（调用方不加文件范围条件）。
    - 非法 UUID 组件直接丢弃（绝不把不可解析的字符串拼进 SQL）。
    - 去重保序。返回 ``()`` 表示过滤后没有任何可行 id —— 调用方必须据此短路成
      空结果，绝不退化成"不过滤"而把范围外的文件泄漏给调用方。
    """
    if file_version_ids is None:
        return None
    normalized: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw in file_version_ids:
        if isinstance(raw, bool):
            continue  # bool 是 int 子类，绝不能当 id
        try:
            value = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
        except (ValueError, TypeError, AttributeError):
            continue
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _scope_conditions(
    kb: KnowledgeBase,
    snap_id: uuid.UUID,
    user: User,
    prefix_parts: tuple[str, ...] | None,
    fv_ids: tuple[uuid.UUID, ...] | None = None,
    *,
    require_chunks: bool = True,
) -> list:
    """共享过滤条件：全部在排名/LIMIT **之前**应用。

    ``fv_ids`` 为 ``None`` 时不限文件；为 ``()`` 时追加恒假条件（无结果）；否则限定
    到这些 file_version（仍是与快照映射/同源/ACL 取交集的权威列过滤，不靠 cmeta）。

    ``require_chunks``：``ProjectChunk.embedded`` 条件需要 ``project_chunks`` 参与
    FROM/JOIN。向量/关键词检索本身就从 ``ProjectChunk`` 出发（显式关联），传 True。
    仅按 file_version 复核（``_still_authorized_fv_ids``）**不得**把该条件塞进
    未关联 chunk 的查询——那会让 ``project_chunks`` 成为 FROM 里的独立表而与本查询
    其余表做笛卡尔积（一条 FV 会被放大成 N 条）。那里传 False，并改用相关 EXISTS。
    """
    conds: list = [
        ProjectSnapshot.id == snap_id,
        ProjectSnapshot.status == ProjectSnapshotStatus.READY.value,
        # 快照映射必须与 FV 同源：绝对不靠脏 cmeta 判定归属。
        ProjectSnapshot.source_id == ProjectFileVersion.source_id,
        ProjectSource.revoked_at.is_(None),
        ProjectFileVersion.parse_status.in_(
            [ProjectFileParseStatus.READY.value, ProjectFileParseStatus.FALLBACK.value]
        ),
    ]
    if require_chunks:
        conds.append(ProjectChunk.embedded.is_(True))
    # KB ACL 在 SQL 内再过滤一次（初始授权之外的双保险；按当前用户）。
    if not user.is_admin:
        conds.append(
            or_(
                KnowledgeBase.is_public.is_(True),
                KnowledgeBase.owner_id == user.id,
            )
        )
    prefix_str = _prefix_str(prefix_parts)
    if prefix_str:
        conds.append(_like_path_prefix(ProjectFileVersion.relative_path, prefix_str))
    if fv_ids is not None:
        # 空元组 -> 恒假（显式空范围的语义就是“没有文件可检索”）。
        conds.append(
            ProjectFileVersion.id.in_(list(fv_ids)) if fv_ids else false()
        )
    return conds


def _project_vector_prefix() -> str:
    """项目向量 id 前缀（与写入侧 ``_embed_file`` 使用同一配置项）。"""
    return str(getattr(settings, "PROJECT_VECTOR_PREFIX", "") or "")


def _project_vector_key(version_id_column, chunk_index_column):
    """由确定性向量 id ``{prefix}{file_version_id}:{chunk_index}`` 生成关联表达式。

    旧的 ready 快照在本次修复前写入的 cmetadata 缺 ``chunk_index``，只按 cmetadata
    关联会永远查不到；而写入时 id 就是确定的，用 id 关联既不伪造数据也不需要回填。
    ``file_version_id`` 是 UUID（标准 36 字符小写形式），与写入侧的 ``str(uuid)``
    一致；chunk_index 为整数。前缀为空时返回 None（调用方退回纯 cmetadata 关联）。
    """
    prefix = _project_vector_prefix()
    if not prefix:
        return None
    return (
        prefix
        + cast(version_id_column, String)
        + ":"
        + cast(chunk_index_column, String)
    )


def _project_vector_pairs(
    db: DBSession,
    store,
    kb: KnowledgeBase,
    snap_id: uuid.UUID,
    vector: list[float],
    limit: int,
    *,
    min_similarity: float,
    user: User,
    prefix_parts: tuple[str, ...] | None,
    fv_ids: tuple[uuid.UUID, ...] | None = None,
) -> list[tuple[LCDocument, float]]:
    """向量检索：先 JOIN SnapshotFile 再排名 LIMIT（过滤在 rank/LIMIT 前）。"""
    rows = store.EmbeddingStore
    collection = store.get_collection(db)
    if collection is None:
        return []
    distance = rows.embedding.cosine_distance(vector).label("distance")
    conditions = _scope_conditions(kb, snap_id, user, prefix_parts, fv_ids)
    # 旧 ready 快照兼容：本修复前写入的向量 cmetadata **没有** chunk_index
    # （ProjectChunk.source_metadata 原本就不含它），只靠 cmetadata 关联会让这些
    # 快照的向量检索永远为空。向量 id 是写入时的确定性格式
    # ``{PROJECT_VECTOR_PREFIX}{file_version_id}:{chunk_index}``，因此按该 id 作为
    # 权威关联键：既覆盖新数据，也让旧数据可检索——不需要伪造/回填任何 index。
    vector_key = _project_vector_key(ProjectChunk.file_version_id, ProjectChunk.chunk_index)
    # 必须用 ``is not None``：SQLAlchemy 表达式对象没有布尔语义，
    # ``if vector_key:`` 会直接抛 TypeError（Boolean value of this clause is not defined）。
    if vector_key is not None:
        chunk_link = or_(
            and_(
                cast(ProjectChunk.file_version_id, String)
                == cast(rows.cmetadata["file_version_id"].astext, String),
                ProjectChunk.chunk_index
                == cast(rows.cmetadata["chunk_index"].astext, Integer),
            ),
            rows.id == vector_key,
        )
    else:
        chunk_link = and_(
            cast(ProjectChunk.file_version_id, String)
            == cast(rows.cmetadata["file_version_id"].astext, String),
            ProjectChunk.chunk_index
            == cast(rows.cmetadata["chunk_index"].astext, Integer),
        )
    statement = (
        select(
            ProjectChunk.content, ProjectChunk.source_metadata, distance,
            ProjectChunk.line_start, ProjectChunk.line_end,
            ProjectChunk.symbol, ProjectChunk.symbol_kind, ProjectChunk.chunk_index,
            ProjectFileVersion.id, ProjectFileVersion.relative_path,
            ProjectFileVersion.language, ProjectFileVersion.content_sha256,
            ProjectFileVersion.source_id, ProjectFileVersion.parse_status,
            ProjectFileVersion.config_fingerprint, ProjectFileVersion.parser_fingerprint,
            ProjectSnapshot.config_fingerprint, ProjectSnapshot.manifest_fingerprint,
        )
        .select_from(rows)
        .join(ProjectChunk, chunk_link)
        .join(ProjectFileVersion, ProjectFileVersion.id == ProjectChunk.file_version_id)
        .join(ProjectSnapshotFile, ProjectSnapshotFile.file_version_id == ProjectFileVersion.id)
        .join(ProjectSnapshot, ProjectSnapshot.id == ProjectSnapshotFile.snapshot_id)
        .join(ProjectSource, ProjectSource.id == ProjectFileVersion.source_id)
        .join(KnowledgeBase, KnowledgeBase.id == ProjectSource.kb_id)
        .where(
            rows.collection_id == collection.uuid,
            KnowledgeBase.collection_name == kb.collection_name,
            *conditions,
        )
    )
    if min_similarity > 0:
        statement = statement.where(distance <= 1.0 - min_similarity)
    statement = statement.order_by(distance).limit(limit)

    pairs: list[tuple[LCDocument, float]] = []
    for row in db.execute(statement).all():
        (
            content, cmeta, dist, ls, le, sym, sym_kind, ci,
            fv_id, path, lang, sha, source_id, parse_status,
            fv_config, fv_parser, snap_config, snap_manifest,
        ) = row
        meta = _hit_meta(
            cmeta=cmeta, file_version_id=fv_id, relative_path=path, language=lang,
            parse_status=parse_status, sha=sha, ls=ls, le=le, sym=sym, sym_kind=sym_kind,
            ci=ci, snap_id=snap_id, source_id=source_id,
            snapshot_config=snap_config, snapshot_manifest=snap_manifest,
            fv_config=fv_config, fv_parser=fv_parser,
        )
        pairs.append((_project_doc(meta, content, ci), float(dist)))
    return pairs


def _project_keyword_pairs(
    db: DBSession,
    kb: KnowledgeBase,
    snap_id: uuid.UUID,
    user: User,
    query: str,
    limit: int,
    *,
    prefix_parts: tuple[str, ...] | None,
    fv_ids: tuple[uuid.UUID, ...] | None = None,
) -> list[tuple[LCDocument, float]]:
    """关键词检索：JOIN SnapshotFile 后按 ts_rank 排序 LIMIT（含中文可检索）。"""
    tsquery = _keyword_tsquery(query)
    if not tsquery:
        return []
    score = func.ts_rank(
        func.to_tsvector("simple", ProjectChunk.search_text),
        func.to_tsquery("simple", tsquery),
    ).label("rank")
    conditions = _scope_conditions(kb, snap_id, user, prefix_parts, fv_ids)
    statement = (
        select(
            ProjectChunk.content, ProjectChunk.source_metadata, score,
            ProjectChunk.line_start, ProjectChunk.line_end,
            ProjectChunk.symbol, ProjectChunk.symbol_kind, ProjectChunk.chunk_index,
            ProjectFileVersion.id, ProjectFileVersion.relative_path,
            ProjectFileVersion.language, ProjectFileVersion.content_sha256,
            ProjectFileVersion.source_id, ProjectFileVersion.parse_status,
            ProjectFileVersion.config_fingerprint, ProjectFileVersion.parser_fingerprint,
            ProjectSnapshot.config_fingerprint, ProjectSnapshot.manifest_fingerprint,
        )
        .select_from(ProjectChunk)
        .join(ProjectFileVersion, ProjectFileVersion.id == ProjectChunk.file_version_id)
        .join(ProjectSnapshotFile, ProjectSnapshotFile.file_version_id == ProjectFileVersion.id)
        .join(ProjectSnapshot, ProjectSnapshot.id == ProjectSnapshotFile.snapshot_id)
        .join(ProjectSource, ProjectSource.id == ProjectFileVersion.source_id)
        .join(KnowledgeBase, KnowledgeBase.id == ProjectSource.kb_id)
        .where(
            KnowledgeBase.collection_name == kb.collection_name,
            *conditions,
            func.to_tsvector("simple", ProjectChunk.search_text).op("@@")(  # noqa: W604
                func.to_tsquery("simple", tsquery)
            ),
        )
    )
    statement = statement.order_by(score.desc()).limit(limit)

    pairs: list[tuple[LCDocument, float]] = []
    for row in db.execute(statement).all():
        (
            content, cmeta, r, ls, le, sym, sym_kind, ci,
            fv_id, path, lang, sha, source_id, parse_status,
            fv_config, fv_parser, snap_config, snap_manifest,
        ) = row
        meta = _hit_meta(
            cmeta=cmeta, file_version_id=fv_id, relative_path=path, language=lang,
            parse_status=parse_status, sha=sha, ls=ls, le=le, sym=sym, sym_kind=sym_kind,
            ci=ci, snap_id=snap_id, source_id=source_id,
            snapshot_config=snap_config, snapshot_manifest=snap_manifest,
            fv_config=fv_config, fv_parser=fv_parser,
        )
        pairs.append((_project_doc(meta, content, ci), float(r)))
    return pairs


def _fusion_limit(top_k: int) -> int:
    return max(top_k * 8, 40)


def _still_authorized_fv_ids(
    db: DBSession,
    kb: KnowledgeBase,
    snap_id: uuid.UUID,
    user: User,
    fv_ids: Sequence,
    prefix_parts: tuple[str, ...] | None,
    file_scope: tuple[uuid.UUID, ...] | None = None,
) -> set[str]:
    """网络等待后 fresh 重读：返回当前仍授权的 file_version_id 集合。

    以与检索相同的权威约束（同源映射/原未撤/ACL/ready|fallback/embedded/路径）复核，
    但只针对本次候选 fv_id（IN 有界），绝不无界枚举快照。撤销/改权限/映射变化即淘汰。

    ``file_scope`` 为本次可选文件范围：即使等待期间客户端范围被放大/篡改，回包里的
    候选也只能落在原始范围内（服务端范围是唯一权威）。恒假时无任何授权 id。
    """
    if not fv_ids:
        return set()
    if file_scope is not None and not file_scope:
        return set()
    uuids = []
    for raw in fv_ids:
        try:
            uuids.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            continue
    if not uuids:
        return set()
    rows = db.execute(
        _still_authorized_statement(kb, snap_id, user, uuids, prefix_parts, file_scope)
    ).all()
    return {str(row[0]) for row in rows}


def _still_authorized_statement(
    kb: KnowledgeBase,
    snap_id: uuid.UUID,
    user: User,
    fv_ids: Sequence,
    prefix_parts: tuple[str, ...] | None,
    file_scope: tuple[uuid.UUID, ...] | None,
):
    """构造“仍授权 file_version”查询（独立出来便于离线断言 SQL 结构）。

    这里只按 file_version 复核，**不能**把 ProjectChunk 放进 FROM：改用相关
    EXISTS 表达“该 FV 自己至少有一个 embedded chunk”。原实现把
    ``ProjectChunk.embedded`` 塞进未关联 chunk 的查询，使 project_chunks 与其余表
    做笛卡尔积（日志实际 SAWarning），一条 FV 会被放大成 N 行，随后被 Python
    ``set()`` 掩盖——既掩盖重复，也让“目标 FV 无 embedded chunk、别的 FV 有”
    这种情形难以正确表达。
    """
    conditions = _scope_conditions(
        kb, snap_id, user, prefix_parts, require_chunks=False
    )
    has_embedded = (
        select(ProjectChunk.id)
        .where(
            ProjectChunk.file_version_id == ProjectFileVersion.id,
            ProjectChunk.embedded.is_(True),
        )
        .correlate(ProjectFileVersion)
        .exists()
    )
    return (
        select(ProjectFileVersion.id)
        .join(ProjectSnapshotFile, ProjectSnapshotFile.file_version_id == ProjectFileVersion.id)
        .join(ProjectSnapshot, ProjectSnapshot.id == ProjectSnapshotFile.snapshot_id)
        .join(ProjectSource, ProjectSource.id == ProjectFileVersion.source_id)
        .join(KnowledgeBase, KnowledgeBase.id == ProjectSource.kb_id)
        .where(
            ProjectFileVersion.id.in_(list(fv_ids)),
            *conditions,
            has_embedded,
            *(
                [ProjectFileVersion.id.in_(list(file_scope))]
                if file_scope is not None
                else []
            ),
        )
        .distinct()
    )


def project_retrieve_documents(
    db: DBSession,
    snapshot_id: uuid.UUID,
    user: User,
    query: str,
    *,
    top_k: int = settings.RETRIEVAL_TOP_K,
    path_prefix: str | None = None,
    file_version_ids: Sequence | None = None,
) -> tuple[list[LCDocument], ProjectSnapshot, ProjectSource, KnowledgeBase]:
    """项目检索核心：返回融合/重排后的文档列表（供 API 与 M 复用）。

    默认限当前知识库 top_k；返回 (docs, snap, source, kb)。
    ``file_version_ids``（可选）把检索限定到指定的文件版本：``None`` 不限；
    显式 ``[]`` 表示本次没有可检索文件 -> 直接返回空结果（绝不退化为全量）。
    该范围在**向量/关键词 SQL 与排名/LIMIT 之前**生效，回包后的 fresh 授权复核
    再叠加一次同一范围。

    网络 embed/rerank 期间不持 DB 事务；回包后 fresh 重读授权，等待期间被撤/改权的
    结果绝不外泄。
    """
    top_k = min(max(1, int(top_k)), MAX_TOP_K)
    snap, source, kb = validate_project_binding(db, snapshot_id, user)
    prefix_parts = normalize_path_prefix(path_prefix)
    file_scope = normalize_file_version_ids(file_version_ids)
    q = (query or "").strip()
    limit = _fusion_limit(top_k)

    if file_scope == ():
        # 显式空文件范围：没有任何候选，但绑定校验仍然已经执行（未授权即抛）。
        _release_db(db)
        return [], snap, source, kb

    # 读完授权即释放 DB，再开始网络 embed（无打开事务/无 checkout 连接）。
    _release_db(db)

    vector_pairs: list[tuple[LCDocument, float]] = []
    keyword_pairs: list[tuple[LCDocument, float]] = []
    if q:
        vector: list[float] | None = None
        try:
            store = get_vector_store(kb.collection_name)
            vector = get_embeddings().embed_query(q)
        except Exception as exc:  # noqa: BLE001 向量失败保留关键词
            logger.warning("项目向量 embed 失败: %s", type(exc).__name__)
            vector = None
        if vector:
            try:
                vector_pairs = _project_vector_pairs(
                    db, store, kb, snapshot_id, vector, limit,
                    min_similarity=settings.RETRIEVAL_MIN_SIMILARITY,
                    user=user, prefix_parts=prefix_parts, fv_ids=file_scope,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("项目向量检索失败: %s", type(exc).__name__)
        if settings.HYBRID_RETRIEVAL_ENABLED or not vector_pairs:
            try:
                keyword_pairs = _project_keyword_pairs(
                    db, kb, snapshot_id, user, q, limit,
                    prefix_parts=prefix_parts, fv_ids=file_scope,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("项目关键词检索失败: %s", type(exc).__name__)

    # 读完检索即释放 DB，再开始网络 rerank。
    _release_db(db)

    fused = fuse_results(vector_pairs, keyword_pairs, limit=limit)
    if settings.RERANK_ENABLED:
        fused = rerank_results(
            q, fused,
            base_url=settings.RERANK_BASE_URL,
            api_key=settings.RERANK_API_KEY,
            model=settings.RERANK_MODEL,
            timeout=settings.RERANK_TIMEOUT,
            max_chars=settings.RERANK_MAX_CHARS,
        )

    # fresh 重读授权：撤销/改权限/快照不再 ready 或不再映射 → 直接抛，不泄内容。
    validate_project_binding(db, snapshot_id, user)
    candidates = [
        doc for doc, _ in fused[:top_k]
        if doc.page_content and doc.page_content.strip()
    ]
    candidate_ids = [doc.metadata.get("document_id") for doc in candidates]
    still_ok = _still_authorized_fv_ids(
        db, kb, snapshot_id, user, [c for c in candidate_ids if c], prefix_parts,
        file_scope=file_scope,
    )
    docs: list[LCDocument] = []
    for doc in candidates:
        if path_prefix is not None and not _path_matches(
            doc.metadata.get("relative_path", ""), prefix_parts
        ):
            continue
        doc_id = doc.metadata.get("document_id")
        if not doc_id or str(doc_id) not in still_ok:
            continue
        docs.append(doc)
    return docs, snap, source, kb


def retrieve_project(
    db: DBSession,
    snapshot_id: uuid.UUID,
    user: User,
    query: str,
    top_k: int,
    *,
    file_version_ids: Sequence | None = None,
) -> list[LCDocument]:
    """M 接入的项目检索入口（供主 chat 调用）。

    ``file_version_ids``：可选文件范围（``list[str]`` / ``list[UUID]``）。``None``
    保持旧行为（全快照）；显式空列表 -> 无结果。范围在 SQL 排名/LIMIT 之前生效，
    并在回包授权复核时再次叠加，绝不让范围外文件泄漏。
    """
    docs, _snap, _src, _kb = project_retrieve_documents(
        db, snapshot_id, user, query, top_k=top_k,
        file_version_ids=file_version_ids,
    )
    return docs


def search_project(
    db: DBSession,
    snapshot_id: uuid.UUID,
    user: User,
    query: str,
    *,
    top_k: int = settings.RETRIEVAL_TOP_K,
    path_prefix: str | None = None,
    file_version_ids: Sequence | None = None,
) -> ProjectSearchRead:
    """API 用的独立搜索（不生成回答）。"""
    q = (query or "")[:MAX_SEARCH_QUERY]
    docs, _snap, _src, _kb = project_retrieve_documents(
        db, snapshot_id, user, q, top_k=top_k, path_prefix=path_prefix,
        file_version_ids=file_version_ids,
    )
    hits: list[ProjectSearchHit] = []
    for doc in docs:
        meta = doc.metadata
        hits.append(
            ProjectSearchHit(
                source_id=meta["source_id"],
                snapshot_id=meta["snapshot_id"],
                file_version_id=meta["file_version_id"],
                relative_path=meta.get("relative_path", ""),
                language=meta.get("language"),
                symbol=meta.get("symbol"),
                symbol_kind=meta.get("symbol_kind"),
                line_start=meta.get("line_start"),
                line_end=meta.get("line_end"),
                chunk_index=int(meta.get("chunk_index", 0)),
                snippet=doc.page_content[:500],
                score=meta.get("fusion_score") if meta.get("fusion_score") is not None
                else meta.get("rerank_score"),
                parse_status=meta.get("parse_status", "ready"),
            )
        )
    return ProjectSearchRead(results=hits, total=len(hits), snapshot_id=snapshot_id)


def load_project_citations(
    db: DBSession,
    snapshot_id: uuid.UUID,
    citations: Sequence[dict],
    user: User,
) -> list[tuple[LCDocument, float]]:
    """按历史引用身份读取仍有效的原文（对固定 snapshot 校验，不漂移）。

    每个 citation 的 document_id=file_version_id；仅当该 version 仍在指定 snapshot
    映射且 chunk 已嵌入时返回原文，否则跳过。SQL 有界：(<=20) 项、每项 <=9 chunk，
    只查真实请求的 FV（不进拉全快照）。source/snapshot/source_kind/path/parse_status
    一律来自权威列，客户端字段不能覆盖。
    """
    snap, _source, _kb = validate_project_binding(db, snapshot_id, user)
    snapshot_id_uuid = uuid.UUID(str(snapshot_id))

    requests: list[tuple[uuid.UUID, int, int, int]] = []
    seen: set[tuple[str, int, int, int]] = set()
    for citation in (citations or [])[:20]:
        try:
            fv = uuid.UUID(str(citation.get("document_id")))
        except (ValueError, TypeError, AttributeError):
            continue
        ci = citation.get("chunk_index")
        start = citation.get("chunk_start") if citation.get("chunk_start") is not None else ci
        end = citation.get("chunk_end") if citation.get("chunk_end") is not None else ci
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (ci, start, end)):
            continue
        if not 0 <= start <= ci <= end or end - start > _CITATION_MAX_SPAN:
            continue
        key = (str(fv), ci, start, end)
        if key in seen:
            continue
        seen.add(key)
        requests.append((fv, ci, start, end))

    if not requests:
        return []

    # 有界 IN 子句：只查请求的 FV 与 chunk 范围，绝不枚举整个快照。
    clause = or_(
        *[
            and_(
                ProjectChunk.file_version_id == fv,
                ProjectChunk.chunk_index >= start,
                ProjectChunk.chunk_index <= end,
            )
            for fv, _ci, start, end in requests
        ]
    )
    rows = db.execute(
        select(
            ProjectChunk.file_version_id, ProjectChunk.chunk_index,
            ProjectChunk.content, ProjectChunk.source_metadata,
            ProjectChunk.line_start, ProjectChunk.line_end,
            ProjectChunk.symbol, ProjectChunk.symbol_kind,
            ProjectFileVersion.relative_path, ProjectFileVersion.language,
            ProjectFileVersion.content_sha256, ProjectFileVersion.source_id,
            ProjectFileVersion.parse_status,
            ProjectSnapshot.config_fingerprint, ProjectSnapshot.manifest_fingerprint,
        )
        .join(ProjectFileVersion, ProjectFileVersion.id == ProjectChunk.file_version_id)
        .join(ProjectSnapshotFile, ProjectSnapshotFile.file_version_id == ProjectFileVersion.id)
        .join(ProjectSnapshot, ProjectSnapshot.id == ProjectSnapshotFile.snapshot_id)
        .join(ProjectSource, ProjectSource.id == ProjectFileVersion.source_id)
        .where(
            ProjectSnapshot.id == snapshot_id_uuid,
            ProjectSnapshot.source_id == ProjectFileVersion.source_id,
            ProjectSource.revoked_at.is_(None),
            ProjectFileVersion.parse_status.in_(
                [ProjectFileParseStatus.READY.value, ProjectFileParseStatus.FALLBACK.value]
            ),
            ProjectChunk.embedded.is_(True),
            clause,
        )
    ).all()

    found: dict[tuple[str, int], dict] = {}
    for (
        fv_id, ci, content, meta, ls, le, sym, sym_kind,
        path, lang, sha, source_id, parse_status, snap_config, snap_manifest,
    ) in rows:
        found[(str(fv_id), ci)] = {
            "content": content, "meta": dict(meta or {}),
            "line_start": ls, "line_end": le,
            "symbol": sym, "symbol_kind": sym_kind,
            "relative_path": path, "language": lang,
            "sha": sha,
            "source_id": str(source_id),
            "parse_status": parse_status,
            "file_version_id": str(fv_id),
            "snapshot_config_fingerprint": snap_config,
            "snapshot_manifest_fingerprint": snap_manifest,
        }

    result: list[tuple[LCDocument, float]] = []
    for fv, chunk_index, start, end in requests:
        fv_key = str(fv)
        chunks = [found.get((fv_key, index)) for index in range(start, end + 1)]
        if any(c is None or not c["content"].strip() for c in chunks):
            continue
        # 硬字符界：拼出的引用原文不得超过可安全进入上下文的上限。
        joined = "".join(c["content"] for c in chunks)
        truncated = False
        if len(joined) > MAX_PREVIEW_CHARS:
            joined = joined[:MAX_PREVIEW_CHARS]
            truncated = True
        anchor = found.get((fv_key, chunk_index))
        # 权威字段一律取 DB 列；客户端 source_id / relative_path / parse_status 等
        # 永远不能覆盖。source_kind=project、snapshot 固定为本次请求快照（不漂移）。
        base = {
            "document_id": fv_key,
            "file_version_id": fv_key,
            "snapshot_id": str(snapshot_id_uuid),
            "source_id": anchor["source_id"],
            "relative_path": anchor["relative_path"],
            # 权威文件名 = 快照内的相对路径（不取客户端 filename）。
            "filename": anchor["relative_path"],
            "language": anchor["language"],
            "line_start": anchor["line_start"],
            "line_end": anchor["line_end"],
            "symbol": anchor["symbol"],
            "symbol_kind": anchor["symbol_kind"],
            "content_sha256": anchor["sha"],
            "chunk_index": chunk_index,
            "index_version": 0,
            "chunk_start": start,
            "chunk_end": end,
            "citation_score": None,
            "source_kind": "project",
            "parse_status": anchor["parse_status"],
            "citation_truncated": truncated,
            # 历史引用必须能证明它属于哪个快照配置/清单，绝不靠调用方自述。
            "snapshot_config_fingerprint": anchor["snapshot_config_fingerprint"],
            "snapshot_manifest_fingerprint": anchor["snapshot_manifest_fingerprint"],
        }
        # 保留与检索一致的 cmeta 副数据（若有），但权威键已在上方显式覆盖。
        base = {**(anchor["meta"]), **base}
        result.append((LCDocument(page_content=joined, metadata=base), 0.0))
    return result


def default_project_snapshot(
    db: DBSession, source: ProjectSource, user: User, snapshot_id=None
) -> uuid.UUID:
    """外部检索默认走 active；可显式指定任一 ready 快照。

    显式 sid 还须与 source 同源（否则快照映射了别的源也必须在此拒绝，防止把
    另一授权源的快照误当作本项目源用）。
    """
    sid = snapshot_id or source.active_snapshot_id
    if sid is None:
        raise ProjectSnapshotNotReady()
    snap, _src, _kb = validate_project_binding(db, sid, user)
    if snapshot_id is not None and snap.source_id != source.id:
        raise ProjectSnapshotNotReady()
    return sid
