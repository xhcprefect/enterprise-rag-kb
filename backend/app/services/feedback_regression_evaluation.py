"""单条反馈回归的纯评测适配层：稳定来源解析、基准 case、绑定摘要与报告校验。

本模块不写数据库、不创建 HTTP 客户端，也不执行模型。它只把已核实的反馈转换成
可重跑的评测 case，并对管理员导入的报告做严格一致性校验；报告正文仅在当前调用
内使用，绝不写入异常消息或日志。所有 SHA-256 都是无密钥一致性检查，不是签名或
真实性证明。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import select

from app.models import Document, DocumentChunk, KnowledgeBase, User
from app.models.feedback import FEEDBACK_STATUS_VERIFIED
from app.services import answer_feedback, document_lifecycle
from app.services.evaluation import EvaluationError, dataset_identity, grade_case, validate_cases
from app.services.evaluation_reviews import ReviewError, inspect_report, load_json_bytes

PROTOCOL_VERSION = 1
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_FAILURES = 64
REGRESSION_MODES = ("offline", "live")

_FAILURE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_OUTCOME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}")

_PARSE_MESSAGES = {
    "file_too_large": "报告超过 2 MiB 上限",
    "json_too_deep": "报告 JSON 嵌套过深",
    "json_duplicate_key": "报告 JSON 包含重复键",
    "json_non_finite": "报告 JSON 包含非有限数字",
}


class RegressionUnavailable(Exception):
    """反馈当前不满足回归条件；reason 为稳定阻断码。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class RegressionReportError(Exception):
    """导入报告不可接受；reason 为稳定原因码，消息绝不包含报告正文。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _invalid_source(message="期望来源已失效或与当前知识库不一致"):
    raise RegressionUnavailable("source_invalid", message)


def _resolve_single_document(db, kb, source):
    """把一条标注解析成唯一、同库、当前可检索的 Document。"""
    document_id = source.get("document_id")
    filename = source.get("filename")
    if filename is not None and not isinstance(filename, str):
        _invalid_source("来源文件名不合法")
    if not document_id and not filename:
        _invalid_source("来源需要 filename 或 document_id")
    document = None
    if document_id:
        try:
            identity = uuid.UUID(str(document_id))
        except (ValueError, TypeError, AttributeError):
            _invalid_source("来源文档标识不合法")
        document = db.get(Document, identity)
        if (
            document is None
            or document.kb_id != kb.id
            or not document_lifecycle.is_retrievable(document)
        ):
            _invalid_source()
    if filename:
        candidates = db.scalars(
            select(Document).where(Document.kb_id == kb.id, Document.filename == filename)
        ).all()
        current = [item for item in candidates if document_lifecycle.is_retrievable(item)]
        if not current:
            _invalid_source()
        if len(current) > 1:
            raise RegressionUnavailable("source_ambiguous", "来源文件名对应多个当前文档，无法唯一确定")
        named = current[0]
        if document is not None and document.id != named.id:
            _invalid_source("来源文档与文件名不一致")
        document = named
    if document is None:
        _invalid_source()
    return document


def resolve_regression_sources(db, kb, conversation, sources) -> list[dict]:
    """解析并稳定化期望来源；同库、可检索、代次与分块必须真实存在。

    文件名必须在当前可检索文档中唯一；顺序保持，规范化后重复的来源只保留首次。
    所有失败使用稳定阻断码，不回显文件名、文档标识或分块正文。
    """
    if sources is None:
        sources = []
    if not isinstance(sources, list):
        _invalid_source("期望来源必须是列表")
    if not sources:
        return []
    if getattr(conversation, "project_snapshot_id", None) is not None:
        raise RegressionUnavailable("project_unsupported", "项目库或项目快照暂不支持反馈回归")
    if kb is None:
        _invalid_source("普通对话不能标注期望来源")
    if getattr(kb, "kind", "upload") == "project":
        raise RegressionUnavailable("project_unsupported", "项目库或项目快照暂不支持反馈回归")
    if getattr(conversation, "kb_id", None) is not None and conversation.kb_id != kb.id:
        _invalid_source("来源与当前会话知识库不一致")

    resolved: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            _invalid_source("期望来源结构不合法")
        document = _resolve_single_document(db, kb, source)
        explicit_version = source.get("index_version")
        if explicit_version is not None:
            if type(explicit_version) is not int or explicit_version < 0:
                _invalid_source("来源索引代次不合法")
            if explicit_version != document.index_version:
                raise RegressionUnavailable("version_mismatch", "来源索引代次与当前文档不一致")
        item = {"document_id": str(document.id), "filename": document.filename,
                "index_version": document.index_version}
        chunk_index = source.get("chunk_index")
        if chunk_index is not None:
            if type(chunk_index) is not int or chunk_index < 0:
                _invalid_source("来源片段编号不合法")
            exists = db.scalar(
                select(DocumentChunk.id)
                .where(DocumentChunk.document_id == document.id,
                       DocumentChunk.chunk_index == chunk_index)
                .limit(1)
            )
            if exists is None:
                raise RegressionUnavailable("chunk_missing", "来源片段已不存在")
            item["chunk_index"] = chunk_index
        key = _canonical_json(item)
        if key not in seen:
            seen.add(key)
            resolved.append(item)
    return resolved


def build_base_case(db, admin, feedback, message, conversation, *, cause: str) -> dict:
    """把已核实反馈转换成不含执行模式的基准评测 case（只读）。

    cause 只参与后续绑定判断，不写入 case，避免原因分类改变数据集身份。
    """
    if conversation is None:
        raise RegressionUnavailable("feedback_stale", "原回答与会话信息不一致，反馈结果已过期")
    kb = None
    if conversation.kb_id is not None:
        kb = db.get(KnowledgeBase, conversation.kb_id)
        if kb is None:
            _invalid_source("知识库不存在")
    if conversation.project_snapshot_id is not None or (
        kb is not None and getattr(kb, "kind", "upload") == "project"
    ):
        raise RegressionUnavailable("project_unsupported", "项目库或项目快照暂不支持反馈回归")
    if getattr(conversation, "document_scope", None) or getattr(conversation, "scope_revision", 0) != 0:
        raise RegressionUnavailable("scope_unsupported", "曾设置文档范围的会话暂不支持反馈回归")
    if conversation.mode == "rag" and kb is None:
        _invalid_source("知识库不存在")
    if kb is not None and not bool(getattr(admin, "is_admin", False)) \
            and not bool(getattr(kb, "is_public", True)) \
            and getattr(kb, "owner_id", None) != getattr(admin, "id", None):
        _invalid_source("无权访问该知识库")
    if feedback.status != FEEDBACK_STATUS_VERIFIED:
        raise RegressionUnavailable("feedback_not_verified", "仅已核实的反馈可以纳入回归")
    if message is None or conversation is None \
            or message.id != feedback.message_id or conversation.id != feedback.conversation_id:
        raise RegressionUnavailable("feedback_stale", "原回答与会话信息不一致，反馈结果已过期")
    if answer_feedback.feedback_stale(db, feedback, message):
        raise RegressionUnavailable("feedback_stale", "原回答已被修改，反馈结果已过期")
    if (
        feedback.corrected_outcome is None
        or feedback.reviewed_at is None
        or not (feedback.evidence_note or "").strip()
        or feedback.unknowns
    ):
        raise RegressionUnavailable("annotation_incomplete", "反馈核验信息不完整")

    try:
        stored_sources = answer_feedback.stored_sources(feedback.expected_sources)
    except HTTPException:
        raise RegressionUnavailable("source_invalid", "期望来源已失效或标注结构不合法") from None
    if conversation.mode == "chat":
        if stored_sources:
            _invalid_source("普通对话不能标注期望来源")
        resolved_sources: list[dict] = []
    else:
        resolved_sources = resolve_regression_sources(db, kb, conversation, stored_sources)

    try:
        turns = answer_feedback.export_turns(db, message)
    except HTTPException:
        raise RegressionUnavailable("turns_overflow", "多轮上下文超过回归上限，无法自动重放") from None
    if not 1 <= len(turns) <= 10:
        raise RegressionUnavailable("turns_overflow", "多轮上下文超过回归上限，无法自动重放")

    owner = db.get(User, feedback.user_id)
    if owner is None:
        raise RegressionUnavailable("annotation_incomplete", "反馈用户已不存在，无法确定执行角色")
    reviewed_at = feedback.reviewed_at
    if reviewed_at.tzinfo is None:
        reviewed_at = reviewed_at.replace(tzinfo=timezone.utc)
    case = {
        "id": f"feedback-{feedback.id}",
        "turns": turns,
        "question": turns[-1],
        "mode": conversation.mode,
        "expected_outcome": feedback.corrected_outcome,
        "expected_sources": resolved_sources,
        "required_phrases": list(feedback.required_phrases or []),
        "forbidden_phrases": list(feedback.forbidden_phrases or []),
        "actor": "admin" if owner.role == "admin" else "member",
        "verified_by": answer_feedback._verified_by(feedback),
        "verified_at": reviewed_at.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "reviewed": True,
        "unknowns": [],
        "top_k": 6,
    }
    if conversation.mode == "rag" and conversation.kb_id is not None:
        case["kb_id"] = str(conversation.kb_id)
    try:
        validate_cases([case])
    except EvaluationError:
        raise RegressionUnavailable("annotation_incomplete", "反馈标注无法构成合法评测用例") from None
    return case


def mark_mode(case: dict, mode: str) -> dict:
    """返回带协议与执行模式标记的副本；不修改基准 case。"""
    if mode not in REGRESSION_MODES:
        raise ValueError("regression_mode 必须是 offline 或 live")
    marked = dict(case)
    marked["regression_protocol"] = PROTOCOL_VERSION
    marked["regression_mode"] = mode
    return marked


def case_fingerprints(case: dict) -> dict:
    """完整 case 摘要与数据集摘要；执行模式已包含在 mode 化 case 中。"""
    return {"case_sha256": _canonical_sha256(case),
            "dataset_sha256": dataset_identity([case])["sha256"]}


def base_binding_payload(case, *, feedback, resolved_sources, kb_id, mode: str, cause: str) -> dict:
    """构建与具体回归轮次无关的绑定内容。

    不含回归 revision、run UUID 或尝试序号；重新准备同一反馈不会让历史轮次失效。
    来源清单只保留稳定身份字段（document_id/index_version/chunk_index）。
    """
    fingerprints = case_fingerprints(case)
    sources = []
    for source in resolved_sources or []:
        item = {"document_id": str(source["document_id"]), "index_version": source["index_version"]}
        if source.get("chunk_index") is not None:
            item["chunk_index"] = source["chunk_index"]
        sources.append(item)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "feedback_id": str(feedback.id),
        "feedback_revision": feedback.revision,
        "feedback_result_sha256": feedback.result_sha256,
        "cause": cause,
        "case_sha256": fingerprints["case_sha256"],
        "dataset_sha256": fingerprints["dataset_sha256"],
        "sources": sources,
        "kb_id": None if kb_id is None else str(kb_id),
        "mode": mode,
    }


def binding_sha256(payload: dict) -> str:
    """绑定内容的规范 JSON SHA-256。"""
    return _canonical_sha256(payload)


def _validate_failures(value):
    if not isinstance(value, list) or len(value) > MAX_FAILURES:
        raise RegressionReportError("report_invalid", "报告失败原因结构不合法")
    for item in value:
        if not isinstance(item, str) or not _FAILURE_RE.fullmatch(item):
            raise RegressionReportError("report_invalid", "报告失败原因结构不合法")
    return list(value)


def _validate_outcome(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _OUTCOME_RE.fullmatch(value):
        raise RegressionReportError("report_invalid", "报告结果处置标识不合法")
    return value


def _parse_report(raw: bytes):
    if not isinstance(raw, (bytes, bytearray)):
        raise RegressionReportError("json_invalid", "报告必须是原始 JSON 字节")
    try:
        return load_json_bytes(raw, max_bytes=MAX_REPORT_BYTES)
    except ReviewError as exc:
        raise RegressionReportError("json_invalid", _PARSE_MESSAGES.get(exc.reason, "报告 JSON 不合法")) from None


def _recompute_result(case, result):
    """用报告正文中的答案/引用/trace/状态重建响应并重新评分。"""
    content = result.get("content")
    if not isinstance(content, dict):
        raise RegressionReportError("report_invalid", "报告正文结构不合法")
    expected_questions = case.get("turns") or [case.get("question")]
    questions = content.get("questions")
    if not isinstance(questions, list) or questions != expected_questions:
        raise RegressionReportError("report_content_mismatch", "报告问题序列与当前用例不一致")
    trace = content.get("trace") if isinstance(content.get("trace"), dict) else {}
    response = {
        "answer": content.get("answer") if isinstance(content.get("answer"), str) else "",
        "citations": content.get("citations"),
        "trace": trace,
        "http_status": result.get("http_status"),
        "error": result.get("error_type"),
    }
    try:
        return grade_case(case, response, include_content=False)
    except EvaluationError:
        raise RegressionReportError("report_invalid", "报告正文无法重新评分") from None


def validate_regression_report(raw: bytes, *, case: dict, run_id, requested_mode: str,
                               dataset_sha256: str) -> dict:
    """严格校验单条反馈回归的报告，只返回白名单摘要。

    默认无正文报告只做一致性校验（consistency_only）；只有报告携带正文时才用
    ``grade_case`` 对当前 case 重算断言（content_regraded）。报告正文不进入异常。
    """
    if requested_mode not in REGRESSION_MODES:
        raise RegressionReportError("unsupported_validation", "不支持的回归执行模式")
    if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
        raise RegressionReportError("report_invalid", "评测用例缺少稳定 ID")

    report = _parse_report(raw)
    if not isinstance(report, dict):
        raise RegressionReportError("report_invalid", "报告必须是 JSON 对象")
    report_sha256 = _canonical_sha256(report)
    try:
        info = inspect_report(report)
    except ReviewError:
        # 报告正文、摘要或字段值都不回显；原因码收敛为 report_invalid。
        raise RegressionReportError("report_invalid", "报告结构或指纹校验失败") from None

    if info["run_id"] != str(run_id):
        raise RegressionReportError("report_run_mismatch", "报告 run_id 与当前轮次不一致")
    if info["mode"] != requested_mode:
        raise RegressionReportError("report_mode_mismatch", "报告执行模式与当前轮次不一致")

    results = info["results"]
    if len(results) != 1:
        raise RegressionReportError("report_case_count_mismatch", "报告必须恰好包含 1 个结果")
    result = results[0]
    if result.get("id") != case["id"]:
        raise RegressionReportError("report_case_mismatch", "报告结果与当前反馈用例不一致")

    expected_dataset = dataset_identity([case])
    identity = report.get("identity")
    dataset = identity.get("dataset") if isinstance(identity, dict) else None
    if (
        not isinstance(dataset, dict)
        or dataset.get("schema") != expected_dataset["schema"]
        or dataset.get("case_count") != expected_dataset["case_count"]
        or dataset.get("sha256") != expected_dataset["sha256"]
        or dataset_sha256 != expected_dataset["sha256"]
    ):
        raise RegressionReportError("report_dataset_mismatch", "报告数据集身份与当前用例不一致")

    failures = _validate_failures(result.get("failures"))
    outcome = _validate_outcome(result.get("outcome"))
    if type(result.get("passed")) is not bool:
        raise RegressionReportError("report_invalid", "报告断言结果结构不合法")
    http_status = result.get("http_status")
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        raise RegressionReportError("report_invalid", "报告 HTTP 状态不合法")
    citation_count = result.get("citation_count")
    if type(citation_count) is not int or citation_count < 0:
        raise RegressionReportError("report_invalid", "报告引用计数不合法")

    if "content" in result:
        regraded = _recompute_result(case, result)
        declared = (result.get("passed"), failures, outcome, http_status, citation_count,
                    result.get("result_sha256"), result.get("machine_sha256"))
        recomputed = (regraded["passed"], regraded["failures"], regraded["outcome"],
                      regraded["http_status"], regraded["citation_count"],
                      regraded["result_sha256"], regraded["machine_sha256"])
        if declared != recomputed:
            raise RegressionReportError("report_content_mismatch", "报告正文重算结果与声明不一致")
        validation_level = "content_regraded"
        passed, failures = regraded["passed"], regraded["failures"]
        outcome, http_status = regraded["outcome"], regraded["http_status"]
        citation_count = regraded["citation_count"]
        result_sha256 = regraded["result_sha256"]
        machine_sha256 = regraded["machine_sha256"]
    else:
        validation_level = "consistency_only"
        passed = result["passed"]
        result_sha256 = result["result_sha256"]
        machine_sha256 = result["machine_sha256"]

    return {
        "report_sha256": report_sha256,
        "report_mode": info["mode"],
        "validation_level": validation_level,
        "passed": passed,
        "failures": list(failures),
        "outcome": outcome,
        "http_status": http_status,
        "citation_count": citation_count,
        "dataset_report_sha256": dataset["sha256"],
        "result_sha256": result_sha256,
        "machine_sha256": machine_sha256,
    }
