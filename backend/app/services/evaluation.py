"""固定问题集评测：确定性断言不等同于语义事实正确率；默认报告不含业务正文。"""
import hashlib
import json
import math
import os
import re
import time
import uuid
from datetime import date
from pathlib import Path


class EvaluationError(ValueError):
    pass


# 指标定义或口径变化时必须递增；比较双方不同则拒绝给出退步结论。
# v2：新增候选/供给来源的 MRR 与前 K 相关率，以及各自的可计算 cohort 覆盖。
METRICS_VERSION = 2
# v2：检索开关/重排参数进入身份，本地语料快照覆盖整棵目录，新增分层与核验计数。
# 旧报告保持可读，但 schema 不同直接判不可比，不做隐式升级。
IDENTITY_SCHEMA = 2
# 结果指纹版本：只覆盖可复核的实际结果内容，不覆盖时间、耗时或环境状态。
RESULT_FINGERPRINT_VERSION = 1
# 独立的机器断言绑定：无正文报告也能校验核验结论所依赖的字段。
MACHINE_FINGERPRINT_VERSION = 1

# 只允许稳定的分层代号；未知值直接拒绝，错误信息不回显原值。
# 前五项对应 templates/README.md 的抽样指南，后几项保留部门/合成样例分层。
CASE_STRATA = ("fact", "cross_doc", "process_sla", "access_compliance", "unanswerable",
               "platform", "hr", "finance", "operations", "security", "compliance", "unclassified")
STRATUM_UNCLASSIFIED = "unclassified"
# 人工核验元数据只允许代号与日期；不接收姓名、邮箱等可识别信息。
_VERIFIED_BY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_VERIFIED_AT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# 严格业务模式只拦截模板留下的完整标记，避免把“str.replace 如何用”这类
# 已核验的技术问题当成占位符。
_PLACEHOLDER_RE = re.compile(r"REPLACE-[A-Za-z0-9_-]+|在此粘贴|（[^）]*(?:请填写|待填写|真实文档名)[^）]*）|TODO[:：]")
# 仅用于聚合口径，不参与数据集身份：标注或分层变化不应让基线失去可比性。
_REVIEW_METADATA_FIELDS = ("verified_by", "verified_at", "unknowns", "stratum", "reviewed")
_INDEX_MANIFEST_FORMAT = "rag-eval-index-manifest/v1"
_CORPUS_MAX_FILES = 5000
_CORPUS_MAX_BYTES = 1024 * 1024 * 1024

# 方向只由指标本身决定，调用方不能把“越高越好”的指标改成越低越好。
METRIC_DIRECTIONS = {
    "assertion_pass_rate": "higher",
    "candidate_source_recall": "higher",
    "supplied_source_recall": "higher",
    "citation_source_recall": "higher",
    "refusal_match_rate": "higher",
    "candidate_source_mrr": "higher",
    "supplied_source_mrr": "higher",
    "candidate_source_precision_at_k": "higher",
    "supplied_source_precision_at_k": "higher",
    "first_token_ms_mean": "lower",
    "total_ms_mean": "lower",
    "first_token_ms_p50": "lower",
    "first_token_ms_p95": "lower",
    "total_ms_p50": "lower",
    "total_ms_p95": "lower",
    "generation_cost_estimate": "lower",
}

# 排名指标只使用实际 trace 顺序与既有来源身份/片段范围匹配，不按分数重排，
# 也不表示语义支持正确。它们必须各自报告 applicable/computable cohort。
RANKING_METRICS = ("candidate_source_mrr", "supplied_source_mrr",
                   "candidate_source_precision_at_k", "supplied_source_precision_at_k")
_RANKING_MRR_METRICS = frozenset(("candidate_source_mrr", "supplied_source_mrr"))
_RANKING_MARKER_KEYS = ("source_ranking_eligible", "source_label_count",
                        "source_labels_complete", "top_k")
_RANKING_RESULT_KEYS = frozenset((*RANKING_METRICS, *_RANKING_MARKER_KEYS))
_RANKING_COVERAGE_KEYS = frozenset(("status", "eligible", "computable",
                                    "eligible_cohort", "cohort", "unknown_cohort"))
_RANKING_COVERAGE_STATUSES = ("no_eligible_cases", "unmeasured", "partial", "computed")

# 默认只拦截确定性断言与来源命中率；延迟和费用需要显式阈值，避免把环境抖动当退步。
DEFAULT_THRESHOLDS = {
    "assertion_pass_rate": {"max_absolute_change": 0.0},
    "candidate_source_recall": {"max_absolute_change": 0.0},
    "supplied_source_recall": {"max_absolute_change": 0.0},
    "citation_source_recall": {"max_absolute_change": 0.0},
    "refusal_match_rate": {"max_absolute_change": 0.0},
}


def _load_json(value):
    def invalid_constant(_value):
        raise ValueError("non-finite JSON number")
    return json.loads(value, parse_constant=invalid_constant)


def read_jsonl(path):
    rows = []
    with Path(path).open(encoding="utf-8-sig") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = _load_json(line)
                if not isinstance(item, dict):
                    raise ValueError
            except ValueError as exc:
                raise EvaluationError(f"JSONL 第 {line_no} 行格式不合法") from exc
            rows.append(item)
            if len(rows) > 10000:
                raise EvaluationError("评测输入超过 10000 条")
    return rows


def _validate_review_metadata(case):
    """核验元数据只接受代号与日期；错误信息不回显任何私有值。"""
    stratum = case.get("stratum")
    if stratum is not None and stratum not in CASE_STRATA:
        raise EvaluationError("stratum 必须是已登记的分层代号")
    verified_by = case.get("verified_by")
    if verified_by is not None and (not isinstance(verified_by, str) or not _VERIFIED_BY_RE.fullmatch(verified_by)):
        raise EvaluationError("verified_by 必须是 1–64 位不含空白的核验人代号")
    verified_at = case.get("verified_at")
    if verified_at is not None:
        match = _VERIFIED_AT_RE.fullmatch(verified_at) if isinstance(verified_at, str) else None
        if match is None:
            raise EvaluationError("verified_at 必须是 YYYY-MM-DD")
        try:
            date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError as exc:
            raise EvaluationError("verified_at 不是有效日期") from exc
    unknowns = case.get("unknowns")
    if unknowns is not None:
        if not isinstance(unknowns, list) or len(unknowns) > 50 \
                or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in unknowns):
            raise EvaluationError("unknowns 必须是不超过 50 条的非空字符串列表")
    if case.get("reviewed") is not None and type(case.get("reviewed")) is not bool:
        raise EvaluationError("reviewed 必须是布尔值")


def review_state(case):
    """只描述核验元数据是否齐全，不据此推导语义准确率。

    显式 reviewed=false 表示作者声明未核验，即使残留核验人/日期也不算已核验。
    """
    if case.get("reviewed") is False:
        return "unreviewed"
    if not (case.get("verified_by") and case.get("verified_at")):
        return "unreviewed"
    return "complete" if not case.get("unknowns") else "incomplete"


def case_stratum(case):
    return case.get("stratum") or STRATUM_UNCLASSIFIED


def _contains_placeholder(case):
    """扫描正文与核验元数据两处；核验字段残留模板同样不算完成核验。"""
    values = [case.get("id"), case.get("question"), *case.get("turns", []), case.get("verified_by")]
    values.extend(source.get("filename") for source in case.get("expected_sources", []) if isinstance(source, dict))
    values.extend(case.get("required_phrases", []))
    values.extend(case.get("forbidden_phrases", []))
    values.extend(case.get("unknowns", []) if isinstance(case.get("unknowns"), list) else [])
    return any(isinstance(value, str) and _PLACEHOLDER_RE.search(value) for value in values)


def require_reviewed_cases(cases):
    """严格业务模式：未完成核验或仍含模板占位符的用例一律拒绝。

    报错只带序号和稳定原因码，不回显工单号、问题或答案正文。
    """
    problems = []
    for index, case in enumerate(cases, 1):
        if _contains_placeholder(case):
            problems.append(f"placeholder_case:{index}")
        elif review_state(case) != "complete":
            problems.append(f"review_incomplete_case:{index}")
    if problems:
        raise EvaluationError("严格业务模式拒绝未完成人工核验的用例: " + ", ".join(problems))
    return cases


def _validate_uuid_text(value, message):
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise EvaluationError(message) from exc
    return str(value)


def _validate_project_source(source):
    if source.get("source_kind") != "project":
        raise EvaluationError("项目来源必须显式标记 source_kind=project")
    document_id = source.get("document_id")
    file_version_id = source.get("file_version_id")
    if document_id is None or file_version_id is None:
        raise EvaluationError("项目来源需要 document_id 与 file_version_id")
    if str(document_id) != str(file_version_id):
        raise EvaluationError("项目来源的 document_id 必须等于 file_version_id")
    _validate_uuid_text(document_id, "项目来源文件版本必须为 UUID")
    _validate_uuid_text(source.get("snapshot_id"), "项目来源 snapshot_id 必须为 UUID")
    _validate_uuid_text(source.get("source_id"), "项目来源 source_id 必须为 UUID")
    relative_path = source.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path or len(relative_path) > 2048:
        raise EvaluationError("项目来源 relative_path 必须是非空字符串")
    for key in ("manifest_fingerprint", "config_fingerprint"):
        value = source.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise EvaluationError("项目来源 manifest/config 指纹不合法")
    if source.get("index_version") is not None:
        raise EvaluationError("项目来源不能携带上传索引代次")
    # 与上传来源同一严格标准：bool/float/负数都不是合法片段编号。
    if source.get("chunk_index") is not None \
            and (type(source["chunk_index"]) is not int or source["chunk_index"] < 0):
        raise EvaluationError("项目来源片段编号不合法")


def _validate_project_identity(value):
    if not isinstance(value, dict):
        raise EvaluationError("项目身份必须是对象")
    _validate_uuid_text(value.get("snapshot_id"), "项目身份 snapshot_id 必须为 UUID")
    _validate_uuid_text(value.get("source_id"), "项目身份 source_id 必须为 UUID")
    for key in ("manifest_fingerprint", "config_fingerprint"):
        item = value.get(key)
        if not isinstance(item, str) or not 1 <= len(item) <= 64:
            raise EvaluationError("项目身份 manifest/config 指纹不合法")


def validate_cases(cases):
    seen = set()
    for case in cases:
        identity = case.get("id")
        turns = case.get("turns", [case.get("question")])
        if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or identity in seen:
            raise EvaluationError("case id 必须是唯一的非空字符串")
        seen.add(identity)
        if not isinstance(turns, list) or not 1 <= len(turns) <= 10 or any(not isinstance(q, str) or not q.strip() or len(q) > 4000 for q in turns):
            raise EvaluationError("每个 case 需要 1–10 个非空问题，每个问题最多 4000 字符")
        if case.get("mode", "rag") not in ("rag", "chat") or case.get("expected_outcome") not in (None, "answer", "refuse", "clarify"):
            raise EvaluationError("case 的模式或预期结果不合法")
        if case.get("actor", "admin") not in ("admin", "member"):
            raise EvaluationError("actor 必须为 admin 或 member")
        if type(case.get("expect_no_citations", False)) is not bool:
            raise EvaluationError("expect_no_citations 必须为布尔值")
        # 缺省 false；只有标注者显式确认 fixed scope 下相关来源已完整列入
        # expected_sources 时才允许计算前 K 相关率。这里不向旧 case 注入默认值。
        if "source_labels_complete" in case and type(case["source_labels_complete"]) is not bool:
            raise EvaluationError("source_labels_complete 必须为布尔值")
        expected_status = case.get("expected_http_status")
        if expected_status is not None and (type(expected_status) is not int or not 400 <= expected_status <= 499):
            raise EvaluationError("预期 HTTP 状态必须为 400–499")
        if expected_status is not None and (len(turns) != 1 or case.get("expected_outcome") or case.get("expected_sources") or case.get("required_phrases")):
            raise EvaluationError("预期 HTTP 错误的 case 只能含单次请求，不能同时要求回答或来源")
        for key in ("kb_id", "conversation_id"):
            if case.get(key) is not None:
                try:
                    uuid.UUID(str(case[key]))
                except (ValueError, TypeError, AttributeError) as exc:
                    raise EvaluationError("知识库或预置会话 ID 必须为 UUID") from exc
        if type(case.get("top_k", 6)) is not int or not 1 <= case.get("top_k", 6) <= 20:
            raise EvaluationError("top_k 必须为 1–20")
        for key in ("required_phrases", "forbidden_phrases"):
            phrases = case.get(key, [])
            if not isinstance(phrases, list) or any(not isinstance(value, str) or not value or len(value) > 2000 for value in phrases):
                raise EvaluationError("断言短语必须是非空字符串列表")
        sources = case.get("expected_sources", [])
        if not isinstance(sources, list) or any(not isinstance(source, dict) or not (source.get("filename") or source.get("document_id")) for source in sources):
            raise EvaluationError("预期来源需要 filename 或 document_id")
        source_kinds = set()
        for source in sources:
            kind = source.get("source_kind")
            if kind is not None and kind not in ("document", "project"):
                raise EvaluationError("来源 source_kind 必须为 document 或 project")
            if kind == "project":
                _validate_project_source(source)
            else:
                if any(source.get(key) is not None for key in _PROJECT_SOURCE_KEYS):
                    raise EvaluationError("上传来源不能携带项目身份字段")
                if any(source.get(key) is not None and not isinstance(source[key], str) for key in ("filename", "document_id")):
                    raise EvaluationError("预期来源身份必须为字符串")
                if source.get("chunk_index") is not None and (type(source["chunk_index"]) is not int or source["chunk_index"] < 0):
                    raise EvaluationError("预期片段编号不合法")
                if source.get("index_version") is not None and (type(source["index_version"]) is not int or source["index_version"] < 0):
                    raise EvaluationError("预期索引代次必须是非负整数")
            source_kinds.add(kind or "document")
        if len(source_kinds) > 1:
            raise EvaluationError("同一 case 不能混用上传与项目来源")
        # 文档范围只属于知识库问答；URL 只允许来自显式登记的文档范围，不能借此变成“白名单”。
        document_ids = case.get("document_ids")
        if document_ids is not None:
            if case.get("mode", "rag") != "rag":
                raise EvaluationError("普通对话不支持 document_ids")
            if case.get("conversation_id") is not None:
                raise EvaluationError("已有会话请通过范围接口调整文档，不能在 case 内指定 document_ids")
            if not isinstance(document_ids, list) or not 1 <= len(document_ids) <= 5:
                raise EvaluationError("document_ids 需要 1–5 个 UUID")
            for value in document_ids:
                try:
                    uuid.UUID(str(value))
                except (ValueError, TypeError, AttributeError) as exc:
                    raise EvaluationError("document_ids 必须为 UUID") from exc
        # 新范围能力：显式限定范围的固定身份。旧 case 没有该键时完全不变。
        document_scope = case.get("document_scope")
        if document_scope is not None:
            if case.get("mode", "rag") != "rag":
                raise EvaluationError("普通对话不支持 document_scope")
            if case.get("conversation_id") is not None or document_ids is not None:
                raise EvaluationError("document_scope 不能与预置会话或 document_ids 同时使用")
            if not isinstance(document_scope, list) or not 1 <= len(document_scope) <= 5:
                raise EvaluationError("document_scope 需要 1–5 个文档身份")
            seen_documents = set()
            for entry in document_scope:
                if not isinstance(entry, dict):
                    raise EvaluationError("document_scope 条目必须是对象")
                identity = _validate_uuid_text(entry.get("document_id"), "document_scope 文档 ID 必须为 UUID")
                if identity in seen_documents:
                    raise EvaluationError("document_scope 文档 ID 不能重复")
                seen_documents.add(identity)
                version = entry.get("index_version")
                if type(version) is not int or version < 0:
                    raise EvaluationError("document_scope 索引代次必须是非负整数")
        # 新项目能力：显式快照/源/manifest/config 身份；仅新 case 出现。
        project_identity = case.get("project_identity")
        if project_identity is not None:
            if case.get("mode", "rag") != "rag":
                raise EvaluationError("普通对话不支持项目身份")
            _validate_project_identity(project_identity)
            if document_ids is not None or document_scope is not None:
                raise EvaluationError("项目身份不能与文档范围同时使用")
        _validate_review_metadata(case)
    return cases


def validate_responses(responses):
    """回放只接受唯一身份和可解释的正文/来源结构，避免坏样本被当成成功。"""
    seen = set()
    for response in responses:
        identity = response.get("id")
        if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or identity in seen:
            raise EvaluationError("离线响应 id 必须是唯一的非空字符串")
        seen.add(identity)
        if response.get("answer") is not None and not isinstance(response["answer"], str):
            raise EvaluationError("离线回答必须为字符串")
        _validate_sources(response)
    return responses


def _validate_sources(response):
    citations = response.get("citations", [])
    trace = response.get("trace", {})
    status = response.get("http_status")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise EvaluationError("HTTP 状态必须为有效整数")
    if not isinstance(citations, list) or any(not isinstance(item, dict) for item in citations):
        raise EvaluationError("引用必须为对象列表")
    if not isinstance(trace, dict):
        raise EvaluationError("检索诊断必须为对象")
    for key in ("candidate_sources", "supplied_sources"):
        if key in trace and (not isinstance(trace[key], list) or any(not isinstance(item, dict) for item in trace[key])):
            raise EvaluationError("候选或实际供给来源必须为对象列表")


def classify_outcome(answer, trace):
    if trace.get("status") == "clarify":
        return "clarify"
    if re.match(r"^[\s#>*_]*(?:抱歉[，,。!！\s]*)?根据现有知识库无法回答", answer):
        return "refuse"
    if re.match(r"^(?:请(?:补充|确认|指出|明确|逐一列明)|您指的是|目前可直接核验)", answer):
        return "clarify"
    return "answer"


_PROJECT_SOURCE_KEYS = ("snapshot_id", "source_id", "file_version_id", "relative_path",
                        "manifest_fingerprint", "config_fingerprint")


def _chunk_range_matches(expected, actual):
    wanted = expected.get("chunk_index")
    if wanted is None:
        return True
    first = actual.get("chunk_start") if actual.get("chunk_start") is not None else actual.get("chunk_index")
    last = actual.get("chunk_end") if actual.get("chunk_end") is not None else actual.get("chunk_index")
    return (type(wanted) is int and type(first) is int and type(last) is int
            and first <= wanted <= last)


def _project_alias_values(actual, primary, *aliases):
    """收集实际来源中所有出现的身份别名值（None 视为未提供）。"""
    return [actual[key] for key in (primary, *aliases) if actual.get(key) is not None]


def _project_alias_matches(actual, primary, aliases, expected):
    """期望身份必须与**所有出现**的别名一致；别名互相矛盾或全部缺失都不命中。"""
    values = _project_alias_values(actual, primary, *aliases)
    return bool(values) and all(str(value) == str(expected) for value in values)


def _matches_project(expected, actual):
    """项目来源严格身份匹配：同文件名的不同快照/文件版本绝不命中。

    兼容两类真实公开形态：``answer_sources`` 规范来源（``manifest_fingerprint`` /
    ``config_fingerprint``）与 ``rag.to_citations`` 引用（``snapshot_manifest_fingerprint``
    / ``snapshot_config_fingerprint``，并带兼容的 ``index_version=0``）。同一身份若有
    多个别名同时出现，它们必须全部一致；任一矛盾或全部缺失都判不命中。
    """
    if not isinstance(actual, dict) or actual.get("source_kind") != "project":
        return False
    expected_file = expected.get("file_version_id") or expected.get("document_id")
    if expected_file is not None and not _project_alias_matches(
        actual, "file_version_id", ("document_id",), expected_file
    ):
        return False
    for key in ("snapshot_id", "source_id"):
        if expected.get(key) is not None:
            if actual.get(key) is None or str(actual[key]) != str(expected[key]):
                return False
    if expected.get("relative_path") is not None and not _project_alias_matches(
        actual, "relative_path", ("filename",), expected["relative_path"]
    ):
        return False
    if expected.get("manifest_fingerprint") is not None and not _project_alias_matches(
        actual, "manifest_fingerprint", ("snapshot_manifest_fingerprint",),
        expected["manifest_fingerprint"],
    ):
        return False
    if expected.get("config_fingerprint") is not None and not _project_alias_matches(
        actual, "config_fingerprint", ("snapshot_config_fingerprint",),
        expected["config_fingerprint"],
    ):
        return False
    return _chunk_range_matches(expected, actual)


def _matches(expected, actual):
    if expected.get("source_kind") == "project" \
            or any(expected.get(key) is not None for key in _PROJECT_SOURCE_KEYS):
        return _matches_project(expected, actual)
    # 上传期望来源绝不能跨类型命中项目实际来源；项目来源的 index_version=0 只是
    # 公开引用兼容字段，不能反向把项目引用当成上传文档。
    if actual.get("source_kind") == "project" \
            or any(actual.get(key) is not None for key in _PROJECT_SOURCE_KEYS):
        return False
    if any(expected.get(key) is not None and expected[key] != actual.get(key) for key in ("filename", "document_id")):
        return False
    if expected.get("index_version") is not None and expected["index_version"] != actual.get("index_version"):
        # 旧代次引用必须判失败，不能只看文件名和片段编号。
        return False
    return _chunk_range_matches(expected, actual)


def _recall(expected, actual):
    if not expected or actual is None:
        return None
    return sum(any(_matches(source, item) for item in actual if isinstance(item, dict)) for source in expected) / len(expected)


def _source_labels_complete(case):
    """严格布尔读取；缺省 false，不改变旧 case 的原始字段与数据集身份。"""
    return case.get("source_labels_complete") is True


def _source_ranking_eligible(case):
    """普通对话与预期 HTTP 负例不进入排序质量分母。"""
    return case.get("mode", "rag") == "rag" and case.get("expected_http_status") is None


def _case_top_k(case):
    top_k = case.get("top_k", 6)
    return top_k if type(top_k) is int and 1 <= top_k <= 20 else 6


def _first_positive_reciprocal_rank(expected, actual):
    """实际 trace 原顺序中首个已知正来源的倒数排名。

    - 没有正来源标注：None（不适用，不是 0）。
    - 对应 trace 缺失：None（应测但未测）。
    - trace 存在但为空，或没有任何命中：0.0（真实观测到的空/全未命中）。
    不按分数排序，也不判断来源是否语义支持答案。
    """
    if not expected:
        return None
    if actual is None:
        return None
    for position, item in enumerate(actual, 1):
        if isinstance(item, dict) and any(_matches(source, item) for source in expected):
            return 1 / position
    return 0.0


def _precision_at_k(expected, actual, top_k, complete):
    """前 K 实际位置中命中已知正来源的比例。

    只有完整来源标注才可计算；固定分母 top_k，未返回的位置按未命中计。
    未完整标注时返回 None，不能把未命中当作负相关。
    """
    if not complete:
        return None
    if actual is None:
        return None
    if type(top_k) is not int or top_k < 1:
        return None
    hits = sum(any(_matches(source, item) for source in expected)
               for item in actual[:top_k] if isinstance(item, dict))
    return hits / top_k


def _row_ranking_eligible(metric, row):
    """从逐条结果标记判断某排名指标是否应测；不信任报告顶层摘要计数。"""
    if row.get("source_ranking_eligible") is not True:
        return False
    if metric in _RANKING_MRR_METRICS:
        count = row.get("source_label_count")
        return type(count) is int and count > 0
    return row.get("source_labels_complete") is True


def rank_cohorts(results):
    """按指标从逐条结果推导 eligible/computable/unknown 的稳定 ID cohort。"""
    coverage = {}
    for metric in RANKING_METRICS:
        eligible, computable = [], []
        for row in results:
            if not isinstance(row, dict) or not _row_ranking_eligible(metric, row):
                continue
            case_id = row.get("id")
            if not isinstance(case_id, str):
                continue
            eligible.append(case_id)
            if _is_unit_interval(row.get(metric)):
                computable.append(case_id)
        eligible = sorted(set(eligible))
        computable = sorted(set(computable))
        unknown = sorted(set(eligible) - set(computable))
        if not eligible:
            status = "no_eligible_cases"
        elif not computable:
            status = "unmeasured"
        elif len(computable) < len(eligible):
            status = "partial"
        else:
            status = "computed"
        coverage[metric] = {"status": status, "eligible": len(eligible),
                            "computable": len(computable), "eligible_cohort": eligible,
                            "cohort": computable, "unknown_cohort": unknown}
    return coverage


def _ranking_mean(results, metric):
    values = [row[metric] for row in results
              if isinstance(row, dict) and _row_ranking_eligible(metric, row)
              and _is_finite_number(row.get(metric)) and 0 <= row[metric] <= 1]
    return sum(values) / len(values) if values else None


def _number(value):
    if not _is_finite_number(value) or value < 0:
        return None
    try:
        return float(value)
    except (OverflowError, ValueError):
        return None


def _result_http_status(response):
    """实际状态码：缺省视为 200；非 100–599 的非法值保持 None，不伪造成功。"""
    status = response.get("http_status", 200)
    return status if type(status) is int and 100 <= status <= 599 else None


def _result_error_code(response):
    """只保留稳定异常类型名；未知形态记为 upstream_error，绝不回显正文。"""
    if not response.get("error"):
        return None
    error_type = str(response["error"])
    return error_type if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_type) else "upstream_error"


# 引用中的评分属于运行期排序信号，不是引用身份或支持性正文；其余字段（含 snippet、
# 索引代次、片段范围）都进入结果指纹。时间/耗时字段同样排除。
_FINGERPRINT_VOLATILE_CITATION_KEYS = frozenset({
    "score", "rerank_score", "latency_ms", "elapsed_ms", "retrieved_at", "created_at", "updated_at",
})


def _citation_fingerprint(citation):
    if not isinstance(citation, dict):
        raise EvaluationError("引用结构不合法，无法生成结果指纹")
    clean = {}
    for key, value in citation.items():
        if key in _FINGERPRINT_VOLATILE_CITATION_KEYS or key.endswith("_ms"):
            continue
        clean[key] = value
    return clean


def result_fingerprint_payload(answer, citations, http_status, error_code=None,
                               *, version=RESULT_FINGERPRINT_VERSION):
    """结果指纹的规范内容：完整答案、引用身份/代次/snippet、实际 HTTP 状态与稳定错误码。

    不包含 run_id、时间、耗时或环境信息；同一逻辑结果重复计算必须得到同一摘要。
    """
    if type(version) is not int or version != RESULT_FINGERPRINT_VERSION:
        raise EvaluationError("不支持的结果指纹版本")
    if not isinstance(citations, list):
        raise EvaluationError("引用结构不合法，无法生成结果指纹")
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        raise EvaluationError("HTTP 状态不合法，无法生成结果指纹")
    return {"version": version, "answer": answer if isinstance(answer, str) else "",
            "citations": [_citation_fingerprint(citation) for citation in citations],
            "http_status": http_status, "error": error_code}


def result_sha256(result):
    """从已导出的结果（含 --include-content 的 content）重算指纹；无正文时返回 None。"""
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("citations"), list):
        return None
    try:
        payload = result_fingerprint_payload(content.get("answer"), content["citations"],
                                            result.get("http_status"), result.get("error_type"))
    except EvaluationError:
        return None
    return _digest(payload)


def machine_fingerprint_payload(result, *, version=MACHINE_FINGERPRINT_VERSION):
    """独立绑定机器断言与结果上下文；不依赖正文，不包含时间、耗时或摘要自身。

    id 与 result_sha256 防止在不同 case 或实际结果间移植摘要；error_type 缺省与
    显式 None 等价。此无密钥摘要只检查一致性，不证明报告真实性。
    """
    if type(version) is not int or version != MACHINE_FINGERPRINT_VERSION:
        raise EvaluationError("不支持的机器断言指纹版本")
    if not isinstance(result, dict):
        raise EvaluationError("结果结构不合法，无法生成机器断言指纹")
    return {"version": version, "id": result.get("id"), "result_sha256": result.get("result_sha256"),
            "passed": result.get("passed"), "failures": result.get("failures"),
            "outcome": result.get("outcome"), "expected_outcome": result.get("expected_outcome"),
            "expected_http_status": result.get("expected_http_status"), "http_status": result.get("http_status"),
            "error_type": result.get("error_type"), "citation_count": result.get("citation_count")}


def machine_sha256(result):
    """对机器断言的规范 JSON 取独立 SHA-256；保持 result_sha256 的 v1 语义不变。"""
    return _digest(machine_fingerprint_payload(result))


def grade_case(case, response, *, input_price=None, output_price=None, include_content=False,
               result_fingerprint_version=RESULT_FINGERPRINT_VERSION):
    _validate_sources(response)
    trace = response.get("trace") if isinstance(response.get("trace"), dict) else {}
    answer = response.get("answer") if isinstance(response.get("answer"), str) else ""
    citations = response.get("citations") if isinstance(response.get("citations"), list) else []
    expected = case.get("expected_sources", [])
    expected_status = case.get("expected_http_status")
    outcome = "http_error" if response.get("http_status", 200) != 200 else classify_outcome(answer, trace)
    failures = []
    if expected_status is not None:
        if response.get("http_status", 200) != expected_status:
            failures.append("http_status_mismatch")
        if answer.strip() or citations:
            failures.append("unexpected_answer_for_http_error")
    else:
        if response.get("error") or response.get("http_status", 200) != 200:
            failures.append("request_failed")
        if not answer.strip():
            failures.append("empty_answer")
    if case.get("expected_outcome") and case["expected_outcome"] != outcome:
        failures.append("outcome_mismatch")
    for phrase in case.get("required_phrases", []):
        if phrase.casefold() not in answer.casefold():
            failures.append("required_phrase_missing")
    for phrase in case.get("forbidden_phrases", []):
        if phrase.casefold() in answer.casefold():
            failures.append("forbidden_phrase_present")
    if case.get("expect_no_citations") and citations:
        failures.append("unexpected_citations")
    candidate_recall = _recall(expected, trace.get("candidate_sources"))
    supplied_recall = _recall(expected, trace.get("supplied_sources"))
    citation_recall = _recall(expected, citations)
    ranking_eligible = _source_ranking_eligible(case)
    labels_complete = _source_labels_complete(case)
    top_k = _case_top_k(case)
    candidate_mrr = (_first_positive_reciprocal_rank(expected, trace.get("candidate_sources"))
                     if ranking_eligible else None)
    supplied_mrr = (_first_positive_reciprocal_rank(expected, trace.get("supplied_sources"))
                    if ranking_eligible else None)
    candidate_precision = (_precision_at_k(expected, trace.get("candidate_sources"),
                                           top_k, labels_complete) if ranking_eligible else None)
    supplied_precision = (_precision_at_k(expected, trace.get("supplied_sources"),
                                          top_k, labels_complete) if ranking_eligible else None)
    if candidate_recall is not None and candidate_recall < 1:
        failures.append("missing_expected_candidates")
    if supplied_recall is not None and supplied_recall < 1:
        failures.append("missing_expected_supplied_sources")
    if citation_recall is not None and citation_recall < 1:
        failures.append("missing_expected_citations")
    usage = response.get("generation_usage", trace.get("usage"))
    cost = None
    if isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens")):
        if _number(input_price) is not None and _number(output_price) is not None:
            cost = (usage["input_tokens"] * input_price + usage["output_tokens"] * output_price) / 1000000
    else:
        usage = None
    actual_status = _result_http_status(response)
    error_code = _result_error_code(response)
    result = {"id": case["id"], "passed": not failures, "failures": list(dict.fromkeys(failures)),
              "stratum": case_stratum(case), "review_state": review_state(case),
              "mode": case.get("mode", "rag"),
              "expected_outcome": case.get("expected_outcome"), "outcome": outcome,
              "expected_http_status": expected_status, "http_status": actual_status,
              "citation_count": len(citations),
              "candidate_source_recall": candidate_recall, "supplied_source_recall": supplied_recall,
              "citation_source_recall": citation_recall,
              "candidate_source_mrr": candidate_mrr, "supplied_source_mrr": supplied_mrr,
              "candidate_source_precision_at_k": candidate_precision,
              "supplied_source_precision_at_k": supplied_precision,
              "source_ranking_eligible": ranking_eligible,
              "source_label_count": len(expected) if isinstance(expected, list) else 0,
              "source_labels_complete": labels_complete, "top_k": top_k,
              "rewrite_ms": _number(trace.get("rewrite_ms")), "retrieve_ms": _number(trace.get("retrieve_ms")),
              "generation_ms": _number(trace.get("generation_ms")),
              "refusal_match": (outcome == "refuse") == (case["expected_outcome"] == "refuse") if case.get("expected_outcome") in {"answer", "refuse"} else None,
              "first_token_ms": _number(response.get("first_token_ms")), "total_ms": _number(response.get("total_ms")),
              "generation_usage": usage, "generation_cost_estimate": cost}
    if error_code:
        result["error_type"] = error_code
    result["result_fingerprint_version"] = result_fingerprint_version
    result["result_sha256"] = _digest(result_fingerprint_payload(
        answer, citations, actual_status, error_code, version=result_fingerprint_version))
    if include_content:
        result["content"] = {"questions": case.get("turns", [case.get("question")]), "answer": answer,
                             "citations": citations, "trace": trace}
    result["machine_fingerprint_version"] = MACHINE_FINGERPRINT_VERSION
    result["machine_sha256"] = machine_sha256(result)
    return result


def _percentile(values, ratio):
    """线性插值分位数；样本缺失时返回 None，不把缺测当成 0。"""
    data = sorted(value for value in values if value is not None)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    position = (len(data) - 1) * ratio
    lower = math.floor(position)
    upper = min(lower + 1, len(data) - 1)
    weight = position - lower
    return data[lower] + (data[upper] - data[lower]) * weight


def _failure_reasons(results):
    """按稳定原因码聚合失败次数；不回显正文。"""
    counts = {}
    for row in results:
        for reason in row.get("failures", []):
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def stratum_summary(results):
    """按 stratum 聚合 n / 断言 / 核验计数；核验元数据不等于语义准确率。"""
    groups = {}
    for row in results:
        stratum = row.get("stratum") or STRATUM_UNCLASSIFIED
        group = groups.setdefault(stratum, {"cases": 0, "passed": 0, "assertion_pass_rate": None,
                                            "reviewed": 0, "unreviewed": 0,
                                            "review_complete": 0, "review_incomplete": 0})
        group["cases"] += 1
        group["passed"] += 1 if row.get("passed") else 0
        group["reviewed" if row.get("review_state") in ("complete", "incomplete") else "unreviewed"] += 1
        if row.get("review_state") == "complete":
            group["review_complete"] += 1
        elif row.get("review_state") == "incomplete":
            group["review_incomplete"] += 1
    for name, group in groups.items():
        group["assertion_pass_rate"] = group["passed"] / group["cases"] if group["cases"] else None
        group["failure_reasons"] = _failure_reasons(
            row for row in results if (row.get("stratum") or STRATUM_UNCLASSIFIED) == name)
    return dict(sorted(groups.items()))


def make_report(results, run_id=None, identity=None):
    def values(key):
        return [row[key] for row in results if row.get(key) is not None]

    def mean(key):
        data = values(key)
        return sum(data) / len(data) if data else None
    costs = values("generation_cost_estimate")
    first_tokens = values("first_token_ms")
    totals = values("total_ms")
    report = {"run_id": str(run_id) if run_id else None, "cases": len(results),
            "passed": sum(row["passed"] for row in results),
            "assertion_pass_rate": sum(row["passed"] for row in results) / len(results) if results else None,
            "candidate_source_recall": mean("candidate_source_recall"), "supplied_source_recall": mean("supplied_source_recall"),
            "citation_source_recall": mean("citation_source_recall"),
            "candidate_source_mrr": _ranking_mean(results, "candidate_source_mrr"),
            "supplied_source_mrr": _ranking_mean(results, "supplied_source_mrr"),
            "candidate_source_precision_at_k": _ranking_mean(results, "candidate_source_precision_at_k"),
            "supplied_source_precision_at_k": _ranking_mean(results, "supplied_source_precision_at_k"),
            "ranking_coverage": rank_cohorts(results),
            "refusal_match_rate": mean("refusal_match"), "first_token_ms_mean": mean("first_token_ms"),
            "total_ms_mean": mean("total_ms"), "generation_cost_estimate": sum(costs) if len(costs) == len(results) and results else None,
            "first_token_ms_p50": _percentile(first_tokens, 0.50), "first_token_ms_p95": _percentile(first_tokens, 0.95),
            "total_ms_p50": _percentile(totals, 0.50), "total_ms_p95": _percentile(totals, 0.95),
            "first_token_ms_samples": len(first_tokens), "total_ms_samples": len(totals),
            "generation_cost_samples": len(costs),
            "reviewed_cases": sum(1 for row in results if row.get("review_state") in ("complete", "incomplete")),
            "unreviewed_cases": sum(1 for row in results if row.get("review_state") not in ("complete", "incomplete")),
            "strata": stratum_summary(results),
            "failure_reasons": _failure_reasons(results),
            "result_fingerprint_version": RESULT_FINGERPRINT_VERSION,
            "notes": ["固定断言和来源匹配不是语义事实正确率。", "费用只包含已返回实际用量的回答生成，不包含追问补全、向量化和重排。", "首字延迟针对最后一轮；总耗时包含 case 的所有准备轮次。",
                      "p50/p95 只使用实际返回的样本；样本数为 0 时保持 null，不补 0。",
                      "result_sha256 覆盖完整答案、引用身份/索引代次/实际片段内容和实际 HTTP 状态，不含时间或耗时；默认报告只导出摘要，不导出正文。",
                      "reviewed_cases 只表示核验元数据齐全，不代表语义准确率。",
                      "candidate/supplied_source_mrr 使用实际 trace 原顺序的首个已知正来源倒数排名；空 trace 记 0，缺 trace 或无正来源标注记 null。",
                      "candidate/supplied_source_precision_at_k 只在 source_labels_complete=true 时计算，固定分母为 case.top_k，未返回的位置按未命中计。",
                      "排名指标只回答既有来源身份/片段范围匹配出现多早，不证明引用语义支持，也不代表语义准确率。",
                      "ranking_coverage 从逐条结果推导 eligible/computable/unknown case ID cohort；缺 trace 的应测 case 是 unknown，不补 0，零 eligible 与未测量分开。",
                      "排名指标只是观测，不改变既有机器断言与答案引用指纹，也不代表语义准确率。"], "results": results}
    if identity is not None:
        report["identity"] = identity
    return report


def read_json_document(path):
    """读取单个 JSON 对象；NaN/Infinity 等非法数值按配置错误处理。"""
    try:
        value = _load_json(Path(path).read_text(encoding="utf-8-sig"))
    except OSError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise EvaluationError("JSON 文档格式不合法") from exc
    if not isinstance(value, dict):
        raise EvaluationError("JSON 文档必须是对象")
    return value


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_file(path):
    """流式 SHA-256，避免把大文件整体读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _corpus_files(root):
    """遍历目录下的规则文件；任何符号链接或不可读目录都 fail closed。

    接受符号链接会得到不完整（或越界）的语料身份，因此根目录、子目录和
    文件只要出现链接就拒绝，而不是静默跳过。目录与文件都按路径稳定排序，
    保证同一棵目录得到同一摘要。
    """
    if root.is_symlink():
        raise EvaluationError("语料根目录不能是符号链接")
    resolved_root = root.resolve()

    def fail(exc):
        raise EvaluationError("语料目录无法完整读取，拒绝生成身份") from exc

    entries, total_bytes = [], 0
    for current, dirnames, filenames in os.walk(resolved_root, followlinks=False, onerror=fail):
        current_path = Path(current)
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for name in dirnames:
            if (current_path / name).is_symlink():
                raise EvaluationError("语料目录含符号链接，拒绝生成身份")
        for name in sorted(filenames):
            candidate = current_path / name
            if candidate.is_symlink():
                raise EvaluationError("语料目录含符号链接，拒绝生成身份")
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(resolved_root).as_posix()
            if relative.startswith("../") or ".." in Path(relative).parts:
                raise EvaluationError("语料目录含越界路径")
            total_bytes += candidate.stat().st_size
            if total_bytes > _CORPUS_MAX_BYTES:
                raise EvaluationError("语料目录超过 1 GiB，拒绝生成身份")
            if len(entries) >= _CORPUS_MAX_FILES:
                raise EvaluationError(f"语料目录超过 {_CORPUS_MAX_FILES} 个文件，拒绝生成身份")
            entries.append((relative, _hash_file(candidate)))
    entries.sort()
    return entries


def corpus_identity(documents_dir):
    """声明的本地语料快照：整棵目录（含未点名干扰资料与子目录）。

    这是调用者显式给出的本地快照，不等于远端知识库索引状态；报告只写出
    计数与摘要，不写文件名或正文。
    """
    if documents_dir is None:
        return None
    root = Path(documents_dir)
    if not root.is_dir():
        raise EvaluationError("--documents-dir 不是目录")
    entries = _corpus_files(root)
    return {"kind": "declared-local-snapshot", "files": len(entries),
            "sha256": _digest({"files": entries})}


_MANIFEST_ENTRY_KEYS = {"document_id", "index_version", "content_sha256", "chunk_count"}


def _validate_manifest_entry(entry, seen):
    if not isinstance(entry, dict):
        raise EvaluationError("索引清单条目必须是对象")
    # 严格白名单：正文、路径、凭据等额外字段一律拒绝，而不是静默丢弃。
    unknown = sorted(set(entry) - _MANIFEST_ENTRY_KEYS)
    if unknown:
        raise EvaluationError("索引清单条目含未知字段: " + ", ".join(unknown))
    document_id, index_version = entry.get("document_id"), entry.get("index_version")
    if not isinstance(document_id, str) or not document_id or len(document_id) > 128:
        raise EvaluationError("索引清单 document_id 必须是 1–128 字符字符串")
    if document_id in seen:
        raise EvaluationError("索引清单 document_id 重复")
    seen.add(document_id)
    if type(index_version) is not int or index_version < 0:
        raise EvaluationError("索引清单 index_version 必须是非负整数")
    content_sha = entry.get("content_sha256")
    if not isinstance(content_sha, str) or not re.fullmatch(r"[a-f0-9]{64}", content_sha):
        raise EvaluationError("索引清单 content_sha256 必须是 SHA-256 小写十六进制")
    chunk_count = entry.get("chunk_count")
    if chunk_count is not None and (type(chunk_count) is not int or chunk_count < 0):
        raise EvaluationError("索引清单 chunk_count 必须是非负整数")
    return {"document_id": document_id, "index_version": index_version,
            "content_sha256": content_sha,
            "chunk_count": chunk_count if chunk_count is not None else None}


def index_manifest_identity(manifest):
    """可选的索引结构指纹：只接受显式白名单字段，拒绝任何正文或凭据。

    唯一格式（CLI 与服务共用，顶层对象只允许 documents）：
      {"documents": [{"document_id": "...", "index_version": 1,
                      "content_sha256": "<64 hex>", "chunk_count": 12}]}
    生成的指纹只含结构字段的摘要与计数，不写入 document_id 原值。
    它描述调用者声明的索引结构，不证明远端服务实际装载了同一索引。
    """
    if manifest is None:
        return None
    if not isinstance(manifest, dict):
        raise EvaluationError("索引清单必须是含 documents 数组的对象")
    unknown = sorted(set(manifest) - {"documents"})
    if unknown:
        raise EvaluationError("索引清单含未知字段: " + ", ".join(unknown))
    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise EvaluationError("索引清单需要 documents 数组")
    seen, entries = set(), []
    for entry in documents:
        clean = _validate_manifest_entry(entry, seen)
        entries.append([clean["document_id"], clean["index_version"],
                        clean["content_sha256"], clean["chunk_count"]])
    entries.sort()
    return {"format": _INDEX_MANIFEST_FORMAT, "documents": len(entries),
            "sha256": _digest(entries)}


def dataset_identity(cases):
    """不依赖运行环境的用例身份：只包含按 id 排序后的用例内容。

    人工核验元数据（stratum/verified_by/verified_at/unknowns）只影响聚合口径，
    不进摘要，避免补标注就让基线失去可比性。
    """
    stripped = [{key: value for key, value in case.items() if key not in _REVIEW_METADATA_FIELDS} for case in cases]
    ordered = sorted(stripped, key=lambda case: case["id"])
    return {"schema": IDENTITY_SCHEMA, "case_count": len(ordered), "sha256": _digest(ordered)}


# 公开配置身份白名单；两个评测入口共用，避免各维护一套键集合。
# 这里不含 RERANK_BASE_URL / RERANK_API_KEY：重排服务地址和密钥永远不进报告。
_CRITICAL_CONFIG_FIELDS = ("mode", "model", "embedding_model", "chunk_size", "chunk_overlap",
                           "retrieval_top_k", "retrieval_rewrite",
                           "hybrid_retrieval_enabled", "rerank_enabled", "rerank_model",
                           "retrieval_min_similarity", "retrieval_neighbor_chunks",
                           "retrieval_max_per_doc", "rerank_max_chars", "keyword_candidates")
PUBLIC_CONFIG_KEYS = _CRITICAL_CONFIG_FIELDS[1:]
_BOOLEAN_CONFIG_FIELDS = ("hybrid_retrieval_enabled", "rerank_enabled")
# 比较必须能证明影响口径的配置一致。离线回放直接用预置响应打分，
# 不经过分块/检索/模型，因此不强制声明；真实评测缺这些身份就没有可比性。
# 重排模型只有在 rerank_enabled=on 时才强制，关闭时留空不算缺项。
_REQUIRED_CONFIG = {
    "offline": (),
    "live": ("model", "embedding_model", "chunk_size", "chunk_overlap", "retrieval_top_k",
             "retrieval_rewrite", "hybrid_retrieval_enabled", "rerank_enabled",
             "retrieval_min_similarity", "retrieval_neighbor_chunks", "retrieval_max_per_doc"),
}
_CONDITIONAL_CONFIG = {"rerank_model": ("rerank_enabled", "on"),
                       "rerank_max_chars": ("rerank_enabled", "on"),
                       "keyword_candidates": ("hybrid_retrieval_enabled", "on")}


def _config_value(name, value):
    if value is None:
        return None
    if name == "mode":
        if value not in ("offline", "live"):
            raise EvaluationError("评测模式必须是 offline 或 live")
        return value
    if name in ("retrieval_rewrite", "rerank_enabled", "hybrid_retrieval_enabled"):
        # 开关只接受 on/off；布尔值会被 True == 1 之类比较误判，一律拒绝。
        if value not in ("on", "off"):
            raise EvaluationError(f"{name} 必须是 on 或 off")
        return value
    if name in ("chunk_size", "chunk_overlap", "retrieval_top_k", "retrieval_neighbor_chunks",
                "retrieval_max_per_doc", "rerank_max_chars", "keyword_candidates"):
        if type(value) is not int or value < 0:
            raise EvaluationError(f"{name} 必须是非负整数")
        return value
    if name == "retrieval_min_similarity":
        if isinstance(value, bool) or not _is_finite_number(value) or not 0 <= value <= 1:
            raise EvaluationError("retrieval_min_similarity 必须是 0–1 之间的有限数字")
        return float(value)
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise EvaluationError(f"{name} 必须是 1–128 字符的字符串")
    if "://" in value or "@" in value:
        raise EvaluationError(f"{name} 不能包含 URL 或账号信息")
    return value


def _check_config_relations(values):
    if values["chunk_size"] is not None and values["chunk_overlap"] is not None and not 0 <= values["chunk_overlap"] < values["chunk_size"]:
        raise EvaluationError("chunk_overlap 必须大于等于 0 且小于 chunk_size")
    if values["retrieval_top_k"] is not None and not 1 <= values["retrieval_top_k"] <= 20:
        raise EvaluationError("retrieval_top_k 必须为 1–20")
    if values["retrieval_neighbor_chunks"] is not None and not 0 <= values["retrieval_neighbor_chunks"] <= 2:
        raise EvaluationError("retrieval_neighbor_chunks 必须为 0–2")
    if values["retrieval_max_per_doc"] is not None and not 1 <= values["retrieval_max_per_doc"] <= 20:
        raise EvaluationError("retrieval_max_per_doc 必须为 1–20")
    if values["rerank_max_chars"] is not None and not 128 <= values["rerank_max_chars"] <= 8000:
        raise EvaluationError("rerank_max_chars 必须为 128–8000")
    if values["keyword_candidates"] is not None and not 1 <= values["keyword_candidates"] <= 200:
        raise EvaluationError("keyword_candidates 必须为 1–200")


def missing_live_config_fields(config, *, mode="live"):
    """列出公开配置中缺失的 live 必填键和开关开启时的条件键。

    只为本地输入预检提供缺项清单；值的合法性仍由 ``config_identity`` 按原有
    白名单和数值/URL/凭据规则判定。``mode`` 与 ``_REQUIRED_CONFIG`` 的键一致，
    不读取服务配置或环境变量，也不自动使用示例配置补全。
    """
    if not isinstance(config, dict):
        raise EvaluationError("公开配置必须是对象")
    missing = [name for name in _REQUIRED_CONFIG.get(mode, _REQUIRED_CONFIG["live"])
               if config.get(name) is None]
    for name, (gate, gate_value) in _CONDITIONAL_CONFIG.items():
        if config.get(gate) == gate_value and config.get(name) is None:
            missing.append(name)
    return tuple(name for name in PUBLIC_CONFIG_KEYS if name in missing)


def config_identity(*, mode, model=None, embedding_model=None, chunk_size=None, chunk_overlap=None,
                    retrieval_top_k=None, retrieval_rewrite=None, hybrid_retrieval_enabled=None,
                    rerank_enabled=None, rerank_model=None, retrieval_min_similarity=None,
                    retrieval_neighbor_chunks=None, retrieval_max_per_doc=None, rerank_max_chars=None,
                    keyword_candidates=None):
    """可比配置身份；仅记录影响质量口径的公开参数，以及指标版本。"""
    supplied = locals()
    values = {name: _config_value(name, supplied[name]) for name in _CRITICAL_CONFIG_FIELDS}
    _check_config_relations(values)
    if values["mode"] is None:
        raise EvaluationError("配置身份需要评测模式")
    # 开关关闭时残留的条件参数不进入身份，避免把无关值变成“配置变化”。
    if values["rerank_enabled"] != "on":
        values["rerank_model"] = None
        values["rerank_max_chars"] = None
    if values["hybrid_retrieval_enabled"] != "on":
        values["keyword_candidates"] = None
    config = {key: value for key, value in values.items() if value is not None}
    return {**config, "sha256": _digest(config)}


def build_identity(cases, *, mode, documents_dir=None, index_manifest=None, **config):
    documents = corpus_identity(documents_dir)
    if documents is None:
        # 没有 documents 目录时显式标记未知，比较阶段据此拒绝“无退步”结论。
        documents = {"kind": "none", "files": 0, "missing": 0, "sha256": None}
    dataset = {**dataset_identity(cases), "documents": documents}
    index = index_manifest_identity(index_manifest)
    if index is not None:
        dataset["index_manifest"] = index
    return {"schema": IDENTITY_SCHEMA, "metrics_version": METRICS_VERSION,
            "dataset": dataset, "config": config_identity(mode=mode, **config)}


def _threshold_metric(name):
    if name not in METRIC_DIRECTIONS:
        raise EvaluationError(f"未知的阈值指标: {name}")
    return name


def _is_finite_number(value):
    """布尔、非数字和超出 float 范围的整数都视为非有限；math.isfinite 对超大整数会抛 OverflowError。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_thresholds(thresholds):
    """严格校验阈值输入：未知指标/规则、负值、布尔和非有限值都拒绝。"""
    if not isinstance(thresholds, dict):
        raise EvaluationError("阈值必须是对象")
    clean = {}
    for name, rules in thresholds.items():
        _threshold_metric(name)
        if not isinstance(rules, dict):
            raise EvaluationError(f"指标 {name} 的阈值必须是对象")
        clean_rules = {}
        for key, value in rules.items():
            if key not in ("max_absolute_change", "max_relative_change"):
                raise EvaluationError(f"指标 {name} 含未知阈值项: {key}")
            if not _is_finite_number(value) or value < 0:
                raise EvaluationError(f"指标 {name} 的 {key} 必须是有限的非负数")
            clean_rules[key] = float(value)
        clean[name] = clean_rules
    return clean


def merge_thresholds(*layers):
    """后一层的同一指标完整覆盖前一层；不允许用部分规则暗中保留默认限制。"""
    merged = {}
    for layer in layers:
        for name, rules in (layer or {}).items():
            merged[name] = dict(rules)
    return merged


def default_thresholds():
    return {name: dict(rules) for name, rules in DEFAULT_THRESHOLDS.items()}


_METRIC_SAMPLES = {
    "first_token_ms_mean": "first_token_ms_samples", "first_token_ms_p50": "first_token_ms_samples",
    "first_token_ms_p95": "first_token_ms_samples", "total_ms_mean": "total_ms_samples",
    "total_ms_p50": "total_ms_samples", "total_ms_p95": "total_ms_samples",
    "generation_cost_estimate": "generation_cost_samples",
}


def _coverage_detail(report, metric):
    """延迟/费用指标无法证明全量覆盖时返回原因，绝不默认按全覆盖比较。

    - 有指标值但缺样本计数：无法证明覆盖范围（旧报告字段缺失），记为 coverage_unverifiable。
    - 0 < 样本 < 总数：部分覆盖，均值/分位数会系统性偏低。
    - 样本为 0 且无值：整体未测量，交给取值判空后的 not_applicable/unknown。
    """
    samples_key = _METRIC_SAMPLES.get(metric)
    if samples_key is None:
        return None
    samples = report.get(samples_key)
    if samples is None:
        return "coverage_unverifiable" if report.get(metric) is not None else None
    if isinstance(samples, int) and not isinstance(samples, bool):
        total = report.get("cases")
        if isinstance(total, int) and not isinstance(total, bool) and 0 < samples < total:
            return "partial_samples"
    return None


def _compare_metric(name, baseline, current, rules):
    base_value, current_value = baseline.get(name), current.get(name)
    direction = METRIC_DIRECTIONS[name]
    metric = {"baseline": base_value, "current": current_value, "delta": None, "relative_delta": None,
              "direction": direction, "threshold": rules or None}
    coverage = _coverage_detail(baseline, name) or _coverage_detail(current, name)
    if coverage:
        # 用部分或覆盖不明的样本与全量基线比较，会把缺测当成变快。
        metric["status"] = "unknown"
        metric["detail"] = coverage
        return metric
    if base_value is None and current_value is None:
        # 两侧都未测量：该维度不适用，不等于 0。但显式设了门槛（例如费用覆盖）
        # 时无法证明未退步，必须判 unknown，不能自动通过。
        metric["status"] = "unknown" if rules else "not_applicable"
        metric["detail"] = "unmeasured"
        return metric
    if base_value is None or current_value is None:
        metric["status"] = "unknown"
        metric["detail"] = "one_side_missing"
        return metric
    delta = current_value - base_value
    metric["delta"] = delta
    if base_value > 0:
        metric["relative_delta"] = delta / base_value
    # 正数表示变差；方向只由指标定义决定。
    deterioration = -delta if direction == "higher" else delta
    metric["deterioration"] = deterioration
    if not rules:
        metric["status"] = "observed"
        return metric
    absolute_limit = rules.get("max_absolute_change")
    relative_limit = rules.get("max_relative_change")
    absolute_bad = absolute_limit is not None and deterioration > absolute_limit + 1e-12
    if relative_limit is None:
        relative_bad = False
    elif base_value > 0:
        relative_bad = deterioration / base_value > relative_limit + 1e-9
    else:
        # 零基线无法算相对比例：任何变差都按超出相对阈值处理。
        relative_bad = deterioration > 0
    if absolute_bad or relative_bad:
        metric["status"] = "regressed"
    elif deterioration > 0:
        metric["status"] = "within_tolerance"
    elif deterioration < 0:
        metric["status"] = "improved"
    else:
        metric["status"] = "unchanged"
    return metric


def _compare_ranking_metric(name, baseline, current, rules, baseline_coverage, current_coverage):
    """排序指标比较：先按本指标核对覆盖/cohort，再看数值与门槛。

    应测但缺 trace/response 的 case 是 unknown；相同数量但 case ID 不同的 cohort
    不可比；零 eligible 与 eligible>0 但全部未测量必须区分。摘要计数不能替代
    从逐条结果推导的 cohort；单个指标的摘要错误只影响该指标。
    """
    metric = {"baseline": baseline.get(name), "current": current.get(name),
              "delta": None, "relative_delta": None, "direction": METRIC_DIRECTIONS[name],
              "threshold": rules or None}
    if baseline_coverage is None or current_coverage is None:
        metric["status"] = "unknown"
        metric["detail"] = "coverage_unverifiable"
        return metric
    metric["baseline_coverage"] = baseline_coverage["metrics"][name]
    metric["current_coverage"] = current_coverage["metrics"][name]
    if baseline_coverage["marker_inconsistent"] or current_coverage["marker_inconsistent"]:
        metric["status"] = "unknown"
        metric["detail"] = "ranking_marker_inconsistent"
        return metric
    baseline_detail = baseline_coverage["details"].get(name)
    current_detail = current_coverage["details"].get(name)
    if baseline_detail or current_detail:
        metric["status"] = "unknown"
        metric["detail"] = baseline_detail or current_detail
        return metric
    left, right = baseline_coverage["metrics"][name], current_coverage["metrics"][name]
    if left["status"] == "no_eligible_cases" and right["status"] == "no_eligible_cases":
        metric["status"] = "unknown" if rules else "not_applicable"
        metric["detail"] = "no_eligible_cases"
        return metric
    if left["status"] == "no_eligible_cases" or right["status"] == "no_eligible_cases":
        metric["status"] = "unknown"
        metric["detail"] = "no_eligible_cases_one_side"
        return metric
    if left["eligible_cohort"] != right["eligible_cohort"]:
        metric["status"] = "unknown"
        metric["detail"] = "eligible_cohort_mismatch"
        return metric
    if left["cohort"] != right["cohort"]:
        metric["status"] = "unknown"
        metric["detail"] = "computable_cohort_mismatch"
        return metric
    if not left["cohort"]:
        # 应测 case 存在但全部缺 trace/response；不能当作 0 或通过。
        metric["status"] = "unknown"
        metric["detail"] = "unmeasured"
        return metric
    if left["computable"] < left["eligible"] or right["computable"] < right["eligible"]:
        # 即使两侧漏测的是同一批 case，应测未测仍不能给出“无退步”。
        metric["status"] = "unknown"
        metric["detail"] = "unmeasured_cases"
        return metric
    # cohort 一致且至少有一个可计算样本；复用普通指标比较后补回覆盖结构。
    compared = _compare_metric(name, baseline, current, rules)
    compared["baseline_coverage"] = metric["baseline_coverage"]
    compared["current_coverage"] = metric["current_coverage"]
    return compared


def _is_digest(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))


# 每个样本计数字段对应的指标；两边都缺测时不能与 0 混为一谈。
_SAMPLE_METRICS = {
    "first_token_ms_samples": ("first_token_ms_mean", "first_token_ms_p50", "first_token_ms_p95"),
    "total_ms_samples": ("total_ms_mean", "total_ms_p50", "total_ms_p95"),
    "generation_cost_samples": ("generation_cost_estimate",),
}


def _is_finite_non_negative(value):
    return _is_finite_number(value) and value >= 0


def _is_unit_interval(value):
    return _is_finite_number(value) and 0 <= value <= 1


def _validate_ranking_value(value, label, name):
    if value is not None and not _is_unit_interval(value):
        raise EvaluationError(f"{label} 报告的 {name} 必须是 0–1 之间的有限数字或 null")


def _validate_ranking_cohort(value, label, name):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise EvaluationError(f"{label} 报告的 {name} 必须是 case ID 列表")
    if len(set(value)) != len(value):
        raise EvaluationError(f"{label} 报告的 {name} 含重复 case ID")
    if value != sorted(value):
        raise EvaluationError(f"{label} 报告的 {name} 必须按 case ID 稳定排序")


def _ranking_fields_present(report):
    results = report.get("results")
    if not isinstance(results, list):
        return False
    if any(isinstance(row, dict) and any(key in row for key in _RANKING_RESULT_KEYS)
           for row in results):
        return True
    return report.get("ranking_coverage") is not None


def _validate_ranking_structure(report, label):
    """校验逐条排名字段和覆盖摘要的类型/范围；与结果的 cohort 一致性在比较阶段核对。"""
    if not _ranking_fields_present(report):
        return
    results = report["results"]
    if not results:
        # 空报告由 cases/results 计数和后续核验流程处理，避免覆盖既有原因码。
        return
    present = [isinstance(row, dict) and any(key in row for key in _RANKING_RESULT_KEYS)
               for row in results]
    if not all(present):
        raise EvaluationError(f"{label} 报告的排名指标字段不完整")
    for row in results:
        if not _RANKING_RESULT_KEYS.issubset(row):
            raise EvaluationError(f"{label} 报告的排名指标字段不完整")
        if not isinstance(row.get("id"), str) or not 1 <= len(row["id"]) <= 128:
            raise EvaluationError(f"{label} 报告的排名结果 case ID 不合法")
        if row.get("mode") not in ("rag", "chat"):
            raise EvaluationError(f"{label} 报告的排名结果 mode 不合法")
        if type(row.get("source_ranking_eligible")) is not bool:
            raise EvaluationError(f"{label} 报告的 source_ranking_eligible 必须是布尔值")
        count = row.get("source_label_count")
        if type(count) is not int or count < 0:
            raise EvaluationError(f"{label} 报告的 source_label_count 必须是非负整数")
        if type(row.get("source_labels_complete")) is not bool:
            raise EvaluationError(f"{label} 报告的 source_labels_complete 必须是布尔值")
        top_k = row.get("top_k")
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise EvaluationError(f"{label} 报告的 top_k 必须为 1–20")
        for name in RANKING_METRICS:
            _validate_ranking_value(row.get(name), label, name)
    coverage = report.get("ranking_coverage")
    if not isinstance(coverage, dict):
        raise EvaluationError(f"{label} 报告缺少 ranking_coverage")
    if set(coverage) != set(RANKING_METRICS):
        raise EvaluationError(f"{label} 报告的 ranking_coverage 指标集合不完整")
    cases = report["cases"]
    for name in RANKING_METRICS:
        entry = coverage.get(name)
        if not isinstance(entry, dict) or set(entry) != _RANKING_COVERAGE_KEYS:
            raise EvaluationError(f"{label} 报告的 {name} 覆盖结构不合法")
        if entry.get("status") not in _RANKING_COVERAGE_STATUSES:
            raise EvaluationError(f"{label} 报告的 {name} 覆盖状态不合法")
        eligible, computable = entry.get("eligible"), entry.get("computable")
        if type(eligible) is not int or type(computable) is not int:
            raise EvaluationError(f"{label} 报告的 {name} eligible/computable 必须是整数")
        if not 0 <= computable <= eligible <= cases:
            raise EvaluationError(f"{label} 报告的 {name} eligible/computable 越界")
        eligible_cohort = entry.get("eligible_cohort")
        cohort = entry.get("cohort")
        unknown = entry.get("unknown_cohort")
        _validate_ranking_cohort(eligible_cohort, label, f"{name}.eligible_cohort")
        _validate_ranking_cohort(cohort, label, f"{name}.cohort")
        _validate_ranking_cohort(unknown, label, f"{name}.unknown_cohort")
        if len(eligible_cohort) != eligible or len(cohort) != computable:
            raise EvaluationError(f"{label} 报告的 {name} 覆盖计数与 cohort 不一致")
        if not set(cohort).issubset(set(eligible_cohort)):
            raise EvaluationError(f"{label} 报告的 {name} computable cohort 不在 eligible cohort 内")
        if set(unknown) != set(eligible_cohort) - set(cohort):
            raise EvaluationError(f"{label} 报告的 {name} unknown cohort 与 eligible/computable 不一致")


def _ranking_coverage_from_results(report):
    """按指标分别从逐条结果推导覆盖，并核对对应摘要。

    返回 {"metrics": {name: 推导覆盖}, "details": {name: 摘要错误码或 None},
    "marker_inconsistent": bool}。任一指标摘要不一致只影响该指标；全局 marker
    矛盾（source_ranking_eligible 与 mode/预期 HTTP 状态不符）会阻止所有排名
    指标给出结论，但不影响非排名指标。
    """
    derived = rank_cohorts(report["results"])
    if not _ranking_fields_present(report):
        # 旧报告完全没有排名字段：无法证明覆盖，不能当作 0 或全量通过。
        return {"metrics": derived,
                "details": {name: "coverage_unverifiable" for name in RANKING_METRICS},
                "marker_inconsistent": False}
    details = {}
    marker_inconsistent = False
    for row in report["results"]:
        if not isinstance(row, dict):
            continue
        expected_eligible = row.get("mode") == "rag" and row.get("expected_http_status") is None
        if row.get("source_ranking_eligible") is not expected_eligible:
            marker_inconsistent = True
    declared = report.get("ranking_coverage")
    if not isinstance(declared, dict):
        return {"metrics": derived, "details": {name: "coverage_missing" for name in RANKING_METRICS},
                "marker_inconsistent": marker_inconsistent}
    for name in RANKING_METRICS:
        entry = declared.get(name)
        if not isinstance(entry, dict):
            details[name] = "coverage_missing"
        elif entry != derived[name]:
            details[name] = "coverage_summary_mismatch"
        else:
            details[name] = None
    return {"metrics": derived, "details": details, "marker_inconsistent": marker_inconsistent}


def _validate_identity_structure(identity, label, cases):
    """identity 的静态结构校验；缺失（None/不存在）留给比较阶段判为不可比。"""
    if identity is None:
        return
    if not isinstance(identity, dict):
        raise EvaluationError(f"{label} 报告的 identity 必须是对象")
    dataset = identity.get("dataset")
    if dataset is not None:
        if not isinstance(dataset, dict):
            raise EvaluationError(f"{label} 报告的 dataset 身份必须是对象")
        sha = dataset.get("sha256")
        if sha is not None and not _is_digest(sha):
            raise EvaluationError(f"{label} 报告的 dataset.sha256 必须是 SHA-256")
        count = dataset.get("case_count")
        if count is not None and (type(count) is not int or count != cases):
            raise EvaluationError(f"{label} 报告的 dataset.case_count 与 cases 不一致")
        documents = dataset.get("documents")
        if documents is not None:
            if not isinstance(documents, dict):
                raise EvaluationError(f"{label} 报告的 documents 身份必须是对象")
            for key in ("files", "missing"):
                value = documents.get(key)
                if value is not None and (type(value) is not int or value < 0):
                    raise EvaluationError(f"{label} 报告的 documents.{key} 必须是非负整数")
            docs_sha = documents.get("sha256")
            if docs_sha is not None and not _is_digest(docs_sha):
                raise EvaluationError(f"{label} 报告的 documents.sha256 必须是 SHA-256")
            kind = documents.get("kind")
            if kind is not None and kind not in ("none", "declared-local-snapshot"):
                raise EvaluationError(f"{label} 报告的 documents.kind 不合法")
        manifest = dataset.get("index_manifest")
        if manifest is not None:
            if not isinstance(manifest, dict):
                raise EvaluationError(f"{label} 报告的 index_manifest 必须是对象")
            if manifest.get("documents") is not None and (type(manifest.get("documents")) is not int
                                                          or manifest["documents"] < 0):
                raise EvaluationError(f"{label} 报告的 index_manifest.documents 必须是非负整数")
            manifest_sha = manifest.get("sha256")
            if manifest_sha is not None and not _is_digest(manifest_sha):
                raise EvaluationError(f"{label} 报告的 index_manifest.sha256 必须是 SHA-256")
    config = identity.get("config")
    if config is not None:
        if not isinstance(config, dict):
            raise EvaluationError(f"{label} 报告的 config 身份必须是对象")
        values = {}
        for name in _CRITICAL_CONFIG_FIELDS:
            if name not in config:
                continue
            try:
                values[name] = _config_value(name, config[name])
            except EvaluationError as exc:
                raise EvaluationError(f"{label} 报告的 config.{name} 不合法: {exc}") from exc
        try:
            _check_config_relations({**{name: None for name in _CRITICAL_CONFIG_FIELDS}, **values})
        except EvaluationError as exc:
            raise EvaluationError(f"{label} 报告的 config 关系不合法: {exc}") from exc


def validate_comparison_report(report, label="report"):
    """比较输入 fail-closed 校验：结构、计数、指标值都必须是可安全相减的有限数字。"""
    if not isinstance(report, dict):
        raise EvaluationError(f"{label} 报告必须是对象")
    results = report.get("results")
    if not isinstance(results, list):
        raise EvaluationError(f"{label} 报告缺少 results 列表")
    cases = report.get("cases")
    if type(cases) is not int or cases < 0:
        raise EvaluationError(f"{label} 报告 cases 必须是非负整数")
    if cases != len(results):
        raise EvaluationError(f"{label} 报告 cases 与 results 数量不一致")
    passed = report.get("passed")
    if passed is not None and (type(passed) is not int or not 0 <= passed <= cases):
        raise EvaluationError(f"{label} 报告 passed 计数不合法")
    for name in METRIC_DIRECTIONS:
        value = report.get(name)
        if value is not None and not _is_finite_non_negative(value):
            raise EvaluationError(f"{label} 报告的指标 {name} 必须是有限的非负数字")
        if name in RANKING_METRICS and value is not None and not _is_unit_interval(value):
            raise EvaluationError(f"{label} 报告的排名指标 {name} 必须在 0–1 之间")
    for samples_key, metrics in _SAMPLE_METRICS.items():
        samples = report.get(samples_key)
        if samples is None:
            continue
        if type(samples) is not int or not 0 <= samples <= cases:
            raise EvaluationError(f"{label} 报告的 {samples_key} 必须是 0–{cases} 的整数")
        if samples == 0 and any(report.get(name) is not None for name in metrics):
            # 样本为 0 却有指标值，说明缺测被补成了数字；不能参与比较。
            raise EvaluationError(f"{label} 报告的 {samples_key}=0 但仍有指标值")
    _validate_ranking_structure(report, label)
    _validate_identity_structure(report.get("identity"), label, cases)
    return report


def _identity_reasons(baseline, current):
    reasons, unknown = [], []
    left, right = baseline.get("identity"), current.get("identity")
    if not isinstance(left, dict):
        return ["baseline_identity_missing"], unknown
    if not isinstance(right, dict):
        return ["current_identity_missing"], unknown
    if left.get("schema") != IDENTITY_SCHEMA or right.get("schema") != IDENTITY_SCHEMA:
        # 旧 schema（如 v1 只哈希被点名的文档）不可靠；不自动升级、不比较。
        return ["identity_schema_mismatch"], unknown
    if left.get("metrics_version") != METRICS_VERSION or right.get("metrics_version") != METRICS_VERSION:
        reasons.append("metrics_version_mismatch")
    left_dataset, right_dataset = left.get("dataset"), right.get("dataset")
    if not isinstance(left_dataset, dict) or not isinstance(right_dataset, dict):
        reasons.append("dataset_identity_missing")
    else:
        left_sha, right_sha = left_dataset.get("sha256"), right_dataset.get("sha256")
        if not _is_digest(left_sha) or not _is_digest(right_sha):
            # 两侧都缺 sha 时 None == None 不能当作“同一数据集”。
            reasons.append("dataset_identity_missing")
        elif left_sha != right_sha:
            reasons.append("dataset_changed")
        elif left_dataset.get("case_count") != right_dataset.get("case_count"):
            reasons.append("dataset_case_count_changed")
        left_docs, right_docs = left_dataset.get("documents"), right_dataset.get("documents")
        if not isinstance(left_docs, dict) or not isinstance(right_docs, dict) \
                or not left_docs.get("sha256") or not right_docs.get("sha256"):
            # 没有公开文档内容身份时无法证明两次评测看到的是同一批资料。
            reasons.append("documents_identity_missing")
        elif left_docs.get("sha256") != right_docs.get("sha256"):
            reasons.append("documents_changed")
        elif left_docs.get("missing") or right_docs.get("missing"):
            reasons.append("documents_incomplete")
        # 索引结构是可选身份：只有两侧都声明时才比较，单侧声明无法证明一致。
        left_index, right_index = left_dataset.get("index_manifest"), right_dataset.get("index_manifest")
        if left_index is not None or right_index is not None:
            if not isinstance(left_index, dict) or not isinstance(right_index, dict) \
                    or not left_index.get("sha256") or not right_index.get("sha256"):
                reasons.append("index_manifest_unverifiable")
            elif left_index.get("sha256") != right_index.get("sha256"):
                reasons.append("index_manifest_changed")
    left_config, right_config = left.get("config"), right.get("config")
    if not isinstance(left_config, dict) or not isinstance(right_config, dict):
        reasons.append("config_identity_missing")
    else:
        left_mode, right_mode = left_config.get("mode"), right_config.get("mode")
        if left_mode is None or right_mode is None:
            # 模式本身是关键身份：缺失或 null 都不证明两次评测口径一致。
            reasons.append("config_unverifiable:mode")
        elif left_mode != right_mode:
            reasons.append("config_changed:mode")
        mode = left_mode if left_mode in _REQUIRED_CONFIG else "live"
        required = {"mode", *_REQUIRED_CONFIG[mode]}
        for name in _CRITICAL_CONFIG_FIELDS:
            # 显式 null 与缺键一样无法证明可比；导入的报告可能带 "field": null。
            in_left = left_config.get(name) is not None
            in_right = right_config.get(name) is not None
            if name in _CONDITIONAL_CONFIG:
                # 条件字段：开关关闭时缺失是正常的，也不列为未知阻断项；
                # 开关开启时等同必填——两侧都缺也算不可比，不能因“一致地缺”放过。
                gate, gate_value = _CONDITIONAL_CONFIG[name]
                if {left_config.get(gate), right_config.get(gate)} != {gate_value}:
                    if not in_left and not in_right:
                        continue
                    if gate_value in (left_config.get(gate), right_config.get(gate)):
                        reasons.append(f"config_unverifiable:{name}")
                    continue
                if not in_left and not in_right:
                    reasons.append(f"config_unverifiable:{name}")
                    continue
            if in_left and in_right:
                if left_config[name] != right_config[name]:
                    reasons.append(f"config_changed:{name}")
            elif in_left or in_right:
                # 只有一侧声明时无法证明配置一致，不能声称“无退步”。
                reasons.append(f"config_unverifiable:{name}")
            elif name in required:
                # 真实评测缺少模型/检索身份时，“无退步”可能只是换了后端。
                reasons.append(f"config_unverifiable:{name}")
            else:
                unknown.append(name)
    return reasons, unknown


def compare_reports(baseline, current, thresholds=None):
    """比较两份报告；不可比与缺指标都返回明确的非零结论，绝不默认通过。"""
    if not isinstance(baseline, dict) or not isinstance(current, dict):
        raise EvaluationError("比较输入必须是报告对象")
    validate_comparison_report(baseline, "baseline")
    validate_comparison_report(current, "current")
    rules = merge_thresholds(default_thresholds(), validate_thresholds(thresholds or {}))
    reasons, unknown_config = _identity_reasons(baseline, current)
    baseline_ranking = _ranking_coverage_from_results(baseline)
    current_ranking = _ranking_coverage_from_results(current)
    metrics = {}
    for name in METRIC_DIRECTIONS:
        if name in RANKING_METRICS:
            metrics[name] = _compare_ranking_metric(name, baseline, current, rules.get(name),
                                                    baseline_ranking, current_ranking)
        else:
            metrics[name] = _compare_metric(name, baseline, current, rules.get(name))
    regressions = [name for name, metric in metrics.items() if metric["status"] == "regressed"]
    improvements = [name for name, metric in metrics.items() if metric["status"] == "improved"]
    unknown_metrics = [name for name, metric in metrics.items() if metric["status"] == "unknown"]
    not_applicable = [name for name, metric in metrics.items() if metric["status"] == "not_applicable"]
    gated_unknown = [name for name in unknown_metrics if name in rules]
    if reasons:
        status, verdict = "incomparable", "incomparable"
    elif regressions:
        # 已确认的退步优先于缺指标；缺指标不能反过来掩盖退步。
        status, verdict = "comparable", "regression"
    elif unknown_metrics:
        # 任何一项只有单侧有值都说明样本不完整，不能给出“无退步”。
        status, verdict = "unknown", "unknown"
    else:
        status, verdict = "comparable", "no_regression"
    report = {"status": status, "verdict": verdict, "reasons": list(dict.fromkeys(reasons)),
              "unknown_config_fields": list(dict.fromkeys(unknown_config)),
              "unknown_metrics": unknown_metrics, "gated_unknown_metrics": gated_unknown,
              "not_applicable_metrics": not_applicable,
              "regressions": regressions, "improvements": improvements,
              "thresholds": rules, "metrics": metrics,
              "notes": ["delta = current - baseline；relative_delta 在 baseline 为 0 时不计算，改用绝对变化。",
                        "零基线时任何变差都超出相对阈值；缺失或覆盖不全的指标记为 unknown，不当作 0 或通过。",
                        "排名指标按逐条结果推导的 eligible/computable case ID cohort 比较；缺 trace/response 为 unknown，零 eligible 与未测量分开。",
                        "断言失败优先于比较结论；不可比时不声称无退步。"]}
    return report


def comparison_exit_code(comparison, *, assertions_passed):
    """退出码优先级：断言失败 1 > 不可比 4 > 退步 3 > 指标未知 5 > 0。"""
    if not assertions_passed:
        return 1
    if comparison["status"] == "incomparable":
        return 4
    if comparison["verdict"] == "regression":
        return 3
    if comparison["verdict"] == "unknown":
        return 5
    return 0


def consume_sse(lines, started):
    event, data, answer, meta, done, first = "", [], [], {}, None, None
    length = frame_length = 0
    def parse():
        nonlocal first, meta, done, length
        if not data:
            return
        try:
            value = _load_json("\n".join(data))
            if not isinstance(value, dict):
                raise ValueError
        except ValueError as exc:
            raise EvaluationError("流式数据格式不合法") from exc
        if event == "meta":
            meta = value
        elif event == "delta":
            token = value.get("content", "")
            if not isinstance(token, str):
                raise EvaluationError("流式正文格式不合法")
            length += len(token)
            if length > 200000:
                raise EvaluationError("流式正文超过评测上限")
            if token and first is None:
                first = (time.perf_counter() - started) * 1000
            answer.append(token)
        elif event == "error":
            raise EvaluationError("问答服务返回 error 事件")
        elif event == "done":
            done = value
    for line in lines:
        line = line.rstrip("\r\n")
        if not line:
            parse()
            event, data, frame_length = "", [], 0
            if done is not None:
                break
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            frame_length += len(line)
            if frame_length > 1000000:
                raise EvaluationError("流式单帧超过评测上限")
            data.append(line[5:].lstrip())
    if data and done is None:
        parse()
    if done is None:
        raise EvaluationError("流式响应缺少 done")
    _validate_sources(done)
    return {"answer": "".join(answer), "citations": done.get("citations", []), "trace": done.get("trace", {}),
            "first_token_ms": first, "conversation_id": meta.get("conversation_id")}


def run_live_case(client, case, *, base_url, token, run_id, kb_id=None):
    started, conversation_id, records = time.perf_counter(), case.get("conversation_id"), []
    for index, question in enumerate(case.get("turns", [case.get("question")])):
        payload = {"question": question, "mode": case.get("mode", "rag"), "top_k": case.get("top_k", 6),
                   "request_id": str(uuid.uuid5(run_id, f"{case['id']}:{index}")),
                   "include_trace": case.get("actor", "admin") == "admin"}
        if payload["mode"] == "rag":
            payload["kb_id"] = kb_id or case.get("kb_id")
            if not payload["kb_id"]:
                raise EvaluationError("真实知识库评测需要 kb_id")
            # 文档范围只在新会话首轮传入；续聊沿用会话已保存的范围。
            if index == 0 and case.get("document_ids"):
                payload["document_ids"] = [str(value) for value in case["document_ids"]]
        if conversation_id:
            payload["conversation_id"] = conversation_id
        turn_started = time.perf_counter()
        with client.stream("POST", base_url.rstrip("/") + "/api/v1/chat/stream", json=payload,
                           headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}) as response:
            if response.status_code != 200:
                # 权限场景只记录状态码，不读取或导出错误正文。
                return {"http_status": response.status_code, "error": "http_error",
                        "total_ms": (time.perf_counter() - started) * 1000}
            record = consume_sse(response.iter_lines(), turn_started)
        returned_conversation = record.get("conversation_id")
        try:
            identity = uuid.UUID(str(returned_conversation))
            if conversation_id is not None and identity != uuid.UUID(str(conversation_id)):
                raise EvaluationError("续聊响应切换了会话，评测已停止")
        except (ValueError, TypeError, AttributeError) as exc:
            raise EvaluationError("响应缺少有效的会话标识") from exc
        conversation_id = str(identity)
        records.append(record)
    result = records[-1]
    usages = [row.get("trace", {}).get("usage") for row in records]
    result["generation_usage"] = ({key: sum(usage[key] for usage in usages) for key in ("input_tokens", "output_tokens")}
                                  if all(isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens")) for usage in usages) else None)
    result["total_ms"] = (time.perf_counter() - started) * 1000
    return result
