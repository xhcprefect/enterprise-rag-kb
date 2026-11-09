"""项目会话的项目快照绑定 / 持久请求身份（纯服务 + 事务，不接 UI/网络）。

本模块只承载「项目知识库（kind=project）会话」的项目快照校验与解析逻辑：

- 仅 ``KB.kind == 'project'`` 的项目库使用项目快照；普通上传库 / 普通对话（mode=chat）
  一律不得绑定项目快照，也从不进行项目检索。
- 初次（新会话）在没有显式快照时绑定该源当前 ``active`` 且 ready 的快照；显式快照
  必须同源（同 KB）、ready，并沿当前用户 ACL / revoked。
- 已有会话保持已绑定的快照，绝不随 ``active`` 漂移；切换已绑定快照必须走显式
  ``chat_turns.set_project_snapshot`` 的 CAS 服务及对应快照切换接口。
- 项目快照 / 源 / 知识库一律以数据库为权威，不信检索候选的 cmeta。

安全与复用：授权绑定复用 ``project_retrieval.validate_project_binding``（本模块不编辑
该文件）；写接口仅本人、仅同 KB 同源、可无变化幂等。生成长事务的复核走
``verify_active_project``，把失效映射为 ``ProjectBindingLost`` 由调用的认领服务处理。
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models import KnowledgeBase, ProjectSource, User
from app.services import answer_sources, project_retrieval
from app.services.answer_sources import SourceVerificationError
from app.services.project_retrieval import ProjectSnapshotNotReady
from app.services.project_sources import ProjectSourceNotFound, ProjectSourceRevoked


class ProjectBindingLost(RuntimeError):
    """生成期间项目绑定失效（快照改变 / 撤销 / 失去 ACL / 版本漂移），应结束该轮。"""


def is_project_kb(db: DBSession, kb_id) -> bool:
    """是否项目知识库（kind=project），以 DB 为权威。"""
    if kb_id is None:
        return False
    kb = db.get(KnowledgeBase, kb_id)
    return kb is not None and kb.kind == "project"


def _validate_for_kb(db: DBSession, user: User, snapshot_id: uuid.UUID, kb_id) -> uuid.UUID:
    """复用项目检索的授权绑定校验，并强制同源（同 KB）。"""
    _snap, _source, kb = project_retrieval.validate_project_binding(db, snapshot_id, user)
    if kb.id != kb_id:
        raise ProjectSnapshotNotReady("快照不属于当前知识库")
    return snapshot_id


def _binding_or_http(db, user, sid, kb_id):
    """认领阶段的绑定校验：映射为面向 API 的 HTTP 错误。"""
    try:
        return _validate_for_kb(db, user, sid, kb_id)
    except ProjectSourceRevoked:
        raise HTTPException(410, "项目源已删除或撤销，无法继续检索")
    except ProjectSourceNotFound:
        raise HTTPException(404, "项目源不存在或无权访问")
    except ProjectSnapshotNotReady:
        raise HTTPException(409, "项目快照尚未就绪或不存在，请刷新后重试")


def resolve_new_project_snapshot(db: DBSession, user: User, kb_id, explicit_sid):
    """新项目会话：无显式 sid 选该源 active 且 ready；显式 sid 同源同 KB 校验。"""
    try:
        if explicit_sid is not None:
            return _validate_for_kb(db, user, explicit_sid, kb_id)
        source = db.scalar(select(ProjectSource).where(ProjectSource.kb_id == kb_id))
        if source is None or source.active_snapshot_id is None:
            raise ProjectSnapshotNotReady()
        return _validate_for_kb(db, user, source.active_snapshot_id, kb_id)
    except (ProjectSourceRevoked, ProjectSourceNotFound, ProjectSnapshotNotReady) as exc:
        # 新会话首次项目检索即失败属于用户可见错误，映射为可展示状态。
        if isinstance(exc, ProjectSourceRevoked):
            raise HTTPException(410, "项目源已删除或撤销，无法继续检索") from exc
        if isinstance(exc, ProjectSourceNotFound):
            raise HTTPException(404, "项目源不存在或无权访问") from exc
        raise HTTPException(409, "项目快照尚未就绪或不存在，请刷新后重试") from exc


def resolve_claim_project(db, conv, user_id, document_ids, explicit_sid, *, new_conv):
    """认领时解析项目快照绑定；返回应绑定的项目快照 sid（用于会话与当次 turn）。

    - 项目知识库与 ``document_ids`` 任何显式（即使空列表）互斥 → 400。
    - 新会话：无显式 sid 选 active ready，显式 sid 同源同 KB。
    - 已有会话：保持已绑 sid 不漂移；显式换 sid 必须走 CAS 接口 → 409；并复核 ACL/
      revoked/ready。
    """
    if document_ids is not None:
        raise HTTPException(400, "项目知识库不使用文档范围")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "用户已不可用，请重新登录")
    kb = db.get(KnowledgeBase, conv.kb_id) if conv.kb_id else None
    if kb is None or kb.kind != "project":
        raise HTTPException(400, "仅项目知识库会话支持项目快照")
    if new_conv:
        return resolve_new_project_snapshot(db, user, kb.id, explicit_sid)
    # 已有项目会话：绑定已固定，不得随 active 漂移。
    if conv.project_snapshot_id is None:
        raise HTTPException(409, "会话未绑定项目快照，请刷新或新建会话")
    if explicit_sid is not None and explicit_sid != conv.project_snapshot_id:
        raise HTTPException(409, "已有会话请通过快照切换接口更新项目快照")
    _binding_or_http(db, user, conv.project_snapshot_id, kb.id)
    return conv.project_snapshot_id


def replay_project_check(db: DBSession, user_id, snapshot_id) -> None:
    """重放（只读）的项目授权复核：已存 turn sid 仍 ready / 未 revoke / 当前用户有 ACL。

    源撤销 / 失去 ACL 时映射为 410 / 404，快照未就绪 409；绝不借会话当前 active 漂移。
    """
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "用户已不可用，请重新登录")
    try:
        project_retrieval.validate_project_binding(db, snapshot_id, user)
    except ProjectSourceRevoked:
        raise HTTPException(410, "项目源已删除或撤销，无法继续读取") from None
    except ProjectSourceNotFound:
        raise HTTPException(404, "项目源不存在或无权访问") from None
    except ProjectSnapshotNotReady:
        raise HTTPException(409, "项目快照不再可用，请刷新会话") from None


def plan_claim_project(db: DBSession, *, kb, user: User, peek_conv, explicit_sid,
                       document_ids, new_conv: bool) -> uuid.UUID:
    """认领第一阶段（**不取会话锁**）：只决定本次要绑定并锁定哪个快照 id。

    真正的 ready / revoked / ACL / 同源校验发生在 ``lock_claim_binding``（ProjectSource ->
    ProjectSnapshot 行锁），随后由 ``verify_claim_binding`` 在会话锁内复核探读值未漂移。
    这样项目绑定锁一定先于 Conversation 锁。
    """
    if document_ids is not None:
        raise HTTPException(400, "项目知识库不使用文档范围")
    if kb is None or kb.kind != "project":
        raise HTTPException(400, "仅项目知识库会话支持项目快照")
    if new_conv:
        if explicit_sid is not None:
            return explicit_sid
        source = db.scalar(select(ProjectSource).where(ProjectSource.kb_id == kb.id))
        if source is None or source.active_snapshot_id is None:
            raise HTTPException(409, "项目快照尚未就绪或不存在，请刷新后重试")
        return source.active_snapshot_id
    bound = getattr(peek_conv, "project_snapshot_id", None)
    if bound is None:
        raise HTTPException(409, "会话未绑定项目快照，请刷新或新建会话")
    if explicit_sid is not None and explicit_sid != bound:
        raise HTTPException(409, "已有会话请通过快照切换接口更新项目快照")
    return bound


def lock_claim_binding(db: DBSession, *, kb, user: User, snapshot_id: uuid.UUID):
    """项目绑定第二段：``ProjectSource -> ProjectSnapshot`` 行锁 + ready/ACL/撤销校验。

    必须在会话锁之前调用；失败映射为面向 API 的 HTTP 错误（410/404/409）。
    """
    try:
        binding = answer_sources.lock_project_binding(db, [snapshot_id], kb=kb, user=user)
    except SourceVerificationError as exc:
        if "项目源已撤销" in str(exc):
            raise HTTPException(410, "项目源已删除或撤销，无法继续检索") from exc
        raise HTTPException(409, "项目快照尚未就绪或不存在，请刷新后重试") from exc
    return binding


def verify_claim_binding(db: DBSession, *, conv, binding, snapshot_id: uuid.UUID,
                         new_conv: bool, require_active: bool = True) -> None:
    """会话锁内复核：锁定后的绑定确实是该 KB 的源、会话绑定就是本快照。

    新会话且未显式指定 sid 时，还要求该源当前 ``active_snapshot_id`` 仍等于本快照
    （锁等待期间被切换则拒绝，绝不悄悄绑定刚切走/切来的快照）。
    """
    snapshot = binding.snapshots.get(snapshot_id) if binding is not None else None
    if snapshot is None:
        raise HTTPException(409, "项目快照尚未就绪或不存在，请刷新后重试")
    owner = binding.sources.get(snapshot.source_id)
    if owner is None or owner.kb_id != conv.kb_id or owner.revoked_at is not None:
        raise HTTPException(410, "项目源已删除或撤销，无法继续检索")
    if getattr(conv, "project_snapshot_id", None) != snapshot_id:
        raise HTTPException(409, "会话项目快照已改变，请刷新后重试")
    if new_conv and require_active and owner.active_snapshot_id != snapshot_id:
        raise HTTPException(409, "项目快照已切换，请刷新后重试")


def lock_publication_binding(db: DBSession, *, kb, user: User, reservation, sources):
    """发布阶段：在会话锁之前锁住全部项目来源的 ``ProjectSource -> ProjectSnapshot``。

    所有项目来源必须属于当次请求固定的快照；缺少固定 sid 或跨快照一律拒绝。
    """
    snapshot_id = reservation.project_snapshot_id
    if snapshot_id is None:
        raise SourceVerificationError("项目请求缺少固定的项目快照")
    ids = {uuid.UUID(str(source["snapshot_id"])) for source in sources
           if source.get("source_kind") == answer_sources.SOURCE_KIND_PROJECT}
    ids.add(snapshot_id)
    if len(ids) != 1:
        raise SourceVerificationError("项目来源跨快照")
    return answer_sources.lock_project_binding(db, [snapshot_id], kb=kb, user=user)


def lock_lease_binding(db: DBSession, *, kb, user: User, snapshot_id: uuid.UUID):
    """租约检查阶段：在会话锁前锁定 Source/Snapshot 并核对当前授权。"""
    return answer_sources.lock_project_binding(db, [snapshot_id], kb=kb, user=user)


def verify_active_project(db: DBSession, conv, reservation) -> None:
    """生成长事务的短复核：会话/轮次 sid 未漂移、版本一致、项目仍 ready / 未 revoke / 有 ACL。

    任一失效抛 ``ProjectBindingLost``，由调用方（complete_turn / check_lease）映射为
    租约失效或 false，绝不把失效项目结果写成成功。conf 须为当前（已锁）会话。
    """
    if not getattr(conv, "project_snapshot_id", None):
        raise ProjectBindingLost("会话未绑定项目快照")
    if conv.project_snapshot_id != reservation.project_snapshot_id:
        raise ProjectBindingLost("项目快照已改变，请刷新会话")
    if (getattr(conv, "scope_revision", None) or 0) != reservation.scope_revision:
        raise ProjectBindingLost("项目快照版本已改变，请刷新会话")
    user = db.get(User, reservation.user_id)
    if user is None or not user.is_active:
        raise ProjectBindingLost("用户已不可用，请重新登录")
    try:
        _validate_for_kb(db, user, conv.project_snapshot_id, conv.kb_id)
    except (ProjectSnapshotNotReady, ProjectSourceNotFound, ProjectSourceRevoked):
        raise ProjectBindingLost("项目快照不再可用、被撤销或失去访问权限，请刷新会话")
