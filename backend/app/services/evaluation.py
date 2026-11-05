"""固定问题集评测：确定性断言不等同于语义事实正确率；默认报告不含业务正文。"""
import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path


class EvaluationError(ValueError):
    pass


# 指标定义或口径变化时必须递增；比较双方不同则拒绝给出退步结论。
METRICS_VERSION = 1
IDENTITY_SCHEMA = 1

# 方向只由指标本身决定，调用方不能把“越高越好”的指标改成越低越好。
METRIC_DIRECTIONS = {
    "assertion_pass_rate": "higher",
    "candidate_source_recall": "higher",
    "supplied_source_recall": "higher",
    "citation_source_recall": "higher",
    "refusal_match_rate": "higher",
    "first_token_ms_mean": "lower",
    "total_ms_mean": "lower",
    "first_token_ms_p50": "lower",
    "first_token_ms_p95": "lower",
    "total_ms_p50": "lower",
    "total_ms_p95": "lower",
    "generation_cost_estimate": "lower",
}

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
        for source in sources:
            if any(source.get(key) is not None and not isinstance(source[key], str) for key in ("filename", "document_id")):
                raise EvaluationError("预期来源身份必须为字符串")
            if source.get("chunk_index") is not None and (type(source["chunk_index"]) is not int or source["chunk_index"] < 0):
                raise EvaluationError("预期片段编号不合法")
            if source.get("index_version") is not None and (type(source["index_version"]) is not int or source["index_version"] < 0):
                raise EvaluationError("预期索引代次必须是非负整数")
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


def _matches(expected, actual):
    if any(expected.get(key) is not None and expected[key] != actual.get(key) for key in ("filename", "document_id")):
        return False
    if expected.get("index_version") is not None and expected["index_version"] != actual.get("index_version"):
        # 旧代次引用必须判失败，不能只看文件名和片段编号。
        return False
    wanted = expected.get("chunk_index")
    if wanted is not None:
        first = actual.get("chunk_start") if actual.get("chunk_start") is not None else actual.get("chunk_index")
        last = actual.get("chunk_end") if actual.get("chunk_end") is not None else actual.get("chunk_index")
        if type(wanted) is not int or type(first) is not int or type(last) is not int or not first <= wanted <= last:
            return False
    return True


def _recall(expected, actual):
    if not expected or actual is None:
        return None
    return sum(any(_matches(source, item) for item in actual if isinstance(item, dict)) for source in expected) / len(expected)


def _number(value):
    if not _is_finite_number(value) or value < 0:
        return None
    try:
        return float(value)
    except (OverflowError, ValueError):
        return None


def grade_case(case, response, *, input_price=None, output_price=None, include_content=False):
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
    result = {"id": case["id"], "passed": not failures, "failures": list(dict.fromkeys(failures)),
              "expected_outcome": case.get("expected_outcome"), "outcome": outcome,
              "candidate_source_recall": candidate_recall, "supplied_source_recall": supplied_recall,
              "citation_source_recall": citation_recall,
              "rewrite_ms": _number(trace.get("rewrite_ms")), "retrieve_ms": _number(trace.get("retrieve_ms")),
              "generation_ms": _number(trace.get("generation_ms")),
              "refusal_match": (outcome == "refuse") == (case["expected_outcome"] == "refuse") if case.get("expected_outcome") in {"answer", "refuse"} else None,
              "first_token_ms": _number(response.get("first_token_ms")), "total_ms": _number(response.get("total_ms")),
              "generation_usage": usage, "generation_cost_estimate": cost}
    if response.get("error"):
        error_type = str(response["error"])
        result["error_type"] = error_type if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_type) else "upstream_error"
    status = response.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    if include_content:
        result["content"] = {"questions": case.get("turns", [case.get("question")]), "answer": answer,
                             "citations": citations, "trace": trace}
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
            "refusal_match_rate": mean("refusal_match"), "first_token_ms_mean": mean("first_token_ms"),
            "total_ms_mean": mean("total_ms"), "generation_cost_estimate": sum(costs) if len(costs) == len(results) and results else None,
            "first_token_ms_p50": _percentile(first_tokens, 0.50), "first_token_ms_p95": _percentile(first_tokens, 0.95),
            "total_ms_p50": _percentile(totals, 0.50), "total_ms_p95": _percentile(totals, 0.95),
            "first_token_ms_samples": len(first_tokens), "total_ms_samples": len(totals),
            "generation_cost_samples": len(costs),
            "notes": ["固定断言和来源匹配不是语义事实正确率。", "费用只包含已返回实际用量的回答生成，不包含追问补全、向量化和重排。", "首字延迟针对最后一轮；总耗时包含 case 的所有准备轮次。",
                      "p50/p95 只使用实际返回的样本；样本数为 0 时保持 null，不补 0。"], "results": results}
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


def document_identity(cases, documents_dir):
    """对问题集实际引用的公开文档做内容身份；不记录文件名或正文。"""
    if documents_dir is None:
        return None
    root = Path(documents_dir)
    names = sorted({source["filename"] for case in cases
                    for source in case.get("expected_sources", []) if source.get("filename")})
    entries, missing = [], []
    for name in names:
        target = root / name
        if target.is_file() and target.resolve().parent == root.resolve():
            entries.append([name, hashlib.sha256(target.read_bytes()).hexdigest()])
        else:
            missing.append(name)
    return {"files": len(entries), "missing": len(missing),
            "sha256": _digest({"files": entries, "missing": missing})}


def dataset_identity(cases):
    """不依赖运行环境的用例身份：只包含按 id 排序后的用例内容。"""
    ordered = sorted(cases, key=lambda case: case["id"])
    return {"schema": IDENTITY_SCHEMA, "case_count": len(ordered), "sha256": _digest(ordered)}


# 配置身份只接受这些字段；其余字段由调用方显式登记，避免把凭据写进报告。
_CRITICAL_CONFIG_FIELDS = ("mode", "model", "embedding_model", "chunk_size", "chunk_overlap",
                           "retrieval_top_k", "retrieval_rewrite")
# 比较必须能证明影响口径的配置一致。离线回放直接用预置响应打分，
# 不经过分块/检索/模型，因此不强制声明；真实评测缺这些身份就没有可比性。
_REQUIRED_CONFIG = {
    "offline": (),
    "live": ("model", "embedding_model", "chunk_size", "chunk_overlap", "retrieval_top_k", "retrieval_rewrite"),
}


def _config_value(name, value):
    if value is None:
        return None
    if name == "mode":
        if value not in ("offline", "live"):
            raise EvaluationError("评测模式必须是 offline 或 live")
        return value
    if name == "retrieval_rewrite":
        if value not in ("on", "off"):
            raise EvaluationError("retrieval_rewrite 必须是 on 或 off")
        return value
    if name in ("chunk_size", "chunk_overlap", "retrieval_top_k"):
        if type(value) is not int or value < 0:
            raise EvaluationError(f"{name} 必须是非负整数")
        return value
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


def config_identity(*, mode, model=None, embedding_model=None, chunk_size=None, chunk_overlap=None,
                    retrieval_top_k=None, retrieval_rewrite=None):
    """可比配置身份；仅记录影响质量口径的公开参数，以及指标版本。"""
    values = {"mode": _config_value("mode", mode), "model": _config_value("model", model),
              "embedding_model": _config_value("embedding_model", embedding_model),
              "chunk_size": _config_value("chunk_size", chunk_size),
              "chunk_overlap": _config_value("chunk_overlap", chunk_overlap),
              "retrieval_top_k": _config_value("retrieval_top_k", retrieval_top_k),
              "retrieval_rewrite": _config_value("retrieval_rewrite", retrieval_rewrite)}
    _check_config_relations(values)
    if values["mode"] is None:
        raise EvaluationError("配置身份需要评测模式")
    config = {key: value for key, value in values.items() if value is not None}
    return {**config, "sha256": _digest(config)}


def build_identity(cases, *, mode, documents_dir=None, **config):
    documents = document_identity(cases, documents_dir)
    if documents is None:
        # 没有 documents 目录时显式标记未知，比较阶段据此拒绝“无退步”结论。
        documents = {"files": 0, "missing": 0, "sha256": None}
    return {"schema": IDENTITY_SCHEMA, "metrics_version": METRICS_VERSION,
            "dataset": {**dataset_identity(cases), "documents": documents},
            "config": config_identity(mode=mode, **config)}


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
    for samples_key, metrics in _SAMPLE_METRICS.items():
        samples = report.get(samples_key)
        if samples is None:
            continue
        if type(samples) is not int or not 0 <= samples <= cases:
            raise EvaluationError(f"{label} 报告的 {samples_key} 必须是 0–{cases} 的整数")
        if samples == 0 and any(report.get(name) is not None for name in metrics):
            # 样本为 0 却有指标值，说明缺测被补成了数字；不能参与比较。
            raise EvaluationError(f"{label} 报告的 {samples_key}=0 但仍有指标值")
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
    left_config, right_config = left.get("config"), right.get("config")
    if not isinstance(left_config, dict) or not isinstance(right_config, dict):
        reasons.append("config_identity_missing")
    else:
        mode = left_config.get("mode") if left_config.get("mode") in _REQUIRED_CONFIG else "live"
        required = set(_REQUIRED_CONFIG[mode])
        for name in _CRITICAL_CONFIG_FIELDS:
            in_left, in_right = name in left_config, name in right_config
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
    metrics = {name: _compare_metric(name, baseline, current, rules.get(name)) for name in METRIC_DIRECTIONS}
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
