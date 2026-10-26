"""固定问题集评测：确定性断言不等同于语义事实正确率；默认报告不含业务正文。"""
import json
import math
import re
import time
import uuid
from pathlib import Path


class EvaluationError(ValueError):
    pass


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
    try:
        return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
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


def make_report(results, run_id=None):
    def mean(key):
        values = [row[key] for row in results if row.get(key) is not None]
        return sum(values) / len(values) if values else None
    costs = [row["generation_cost_estimate"] for row in results if row["generation_cost_estimate"] is not None]
    return {"run_id": str(run_id) if run_id else None, "cases": len(results),
            "passed": sum(row["passed"] for row in results),
            "assertion_pass_rate": sum(row["passed"] for row in results) / len(results) if results else None,
            "candidate_source_recall": mean("candidate_source_recall"), "supplied_source_recall": mean("supplied_source_recall"),
            "citation_source_recall": mean("citation_source_recall"),
            "refusal_match_rate": mean("refusal_match"), "first_token_ms_mean": mean("first_token_ms"),
            "total_ms_mean": mean("total_ms"), "generation_cost_estimate": sum(costs) if len(costs) == len(results) and results else None,
            "notes": ["固定断言和来源匹配不是语义事实正确率。", "费用只包含已返回实际用量的回答生成，不包含追问补全、向量化和重排。", "首字延迟针对最后一轮；总耗时包含 case 的所有准备轮次。"], "results": results}


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
