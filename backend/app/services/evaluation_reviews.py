"""业务结果人工核验的本地闭环：只读本地报告与核验文件，不创建 HTTP 客户端。

人工核验只记录“核验人逐条读取原文后的宣告”，不是自动语义证明，也不能替代真实
业务或生产验收。所有校验失败使用稳定原因码；错误信息不回显正文、核验人或文件内
敏感字段。
"""
import json
import math
import os
import re
import stat
from datetime import date
from pathlib import Path

from app.services.evaluation import (EvaluationError, MACHINE_FINGERPRINT_VERSION, RESULT_FINGERPRINT_VERSION,
                                     _VERIFIED_AT_RE, _VERIFIED_BY_RE, _digest, machine_sha256,
                                     result_sha256, validate_comparison_report)

SCHEMA = "rag-eval-result-reviews/v1"
SUMMARY_SCHEMA = "rag-eval-result-review-summary/v1"
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_CASES = 10000
MAX_UNKNOWNS = 50
MAX_UNKNOWN_CHARS = 500

_TOP_KEYS = {"schema", "run_id", "identity_sha256", "result_fingerprint_version", "mode", "reviews"}
_REVIEW_KEYS = {"id", "result_sha256", "answer_correct", "citation_supported", "outcome_correct",
                "reviewer", "reviewed_at", "unknowns", "content"}
_JUDGMENTS = ("answer_correct", "citation_supported", "outcome_correct")
_SHA_RE = re.compile(r"[a-f0-9]{64}")
_CASE_ID_RE = re.compile(r".{1,128}", re.DOTALL)


class ReviewError(EvaluationError):
    """带稳定原因码与退出码的核验错误；字符串形式只输出原因码。"""

    def __init__(self, reason, exit_code):
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


def _fail(reason, exit_code):
    raise ReviewError(reason, exit_code)


def _is_sha(value):
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _is_tristate(value):
    return value is None or type(value) is bool


def _is_case_id(value):
    return isinstance(value, str) and bool(_CASE_ID_RE.fullmatch(value))


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("json_duplicate_key", 2)
        result[key] = value
    return result


def _reject_constant(_value):
    _fail("json_non_finite", 2)


def _reject_float(value):
    try:
        number = float(value)
    except (OverflowError, ValueError):
        _fail("json_invalid", 2)
    if not math.isfinite(number):
        # json.loads("1e999") 得到 inf，不会触发 parse_constant；这里必须拒绝。
        _fail("json_non_finite", 2)
    return number


def _normalize_string(value):
    """合并合法 UTF-16 代理对；孤立的高/低代理返回 None。

    Python 的 json.loads 会保留 ``\\ud83d\\ude00`` 这类转义代理对，
    而直接 UTF-8 编码会拒绝所有代理码点。这里只合并成对代理，拒绝孤立代理，
    使 emoji 等合法字符可继续参与规范 JSON 摘要，且不改变普通字符串语义。
    """
    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value
    normalized = []
    index = 0
    while index < len(value):
        code = ord(value[index])
        if 0xD800 <= code <= 0xDBFF:
            if index + 1 >= len(value):
                return None
            low = ord(value[index + 1])
            if not 0xDC00 <= low <= 0xDFFF:
                return None
            normalized.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
            index += 2
            continue
        if 0xDC00 <= code <= 0xDFFF:
            return None
        normalized.append(value[index])
        index += 1
    return "".join(normalized)


def _normalize_json_tree(value, reason, exit_code, depth=0):
    """校验并可安全编码地规范化整个 JSON 树；畸形 Unicode 稳定 fail closed。"""
    if depth > 64:
        _fail(reason, exit_code)
    if isinstance(value, str):
        normalized = _normalize_string(value)
        if normalized is None:
            _fail(reason, exit_code)
        return normalized
    if isinstance(value, dict):
        normalized = {}
        changed = False
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(reason, exit_code)
            new_key = _normalize_string(key)
            if new_key is None or new_key in normalized:
                # 规范化后键冲突会让不同键静默合并，必须拒绝。
                _fail(reason, exit_code)
            new_item = _normalize_json_tree(item, reason, exit_code, depth + 1)
            changed = changed or new_key != key or new_item is not item
            normalized[new_key] = new_item
        return normalized if changed else value
    if isinstance(value, list):
        normalized = []
        changed = False
        for item in value:
            new_item = _normalize_json_tree(item, reason, exit_code, depth + 1)
            changed = changed or new_item is not item
            normalized.append(new_item)
        return normalized if changed else value
    return value


def _check_json_value(value, depth=0):
    """拒绝超深结构、非有限数字和畸形 Unicode；不回显原文。"""
    if depth > 64:
        _fail("json_too_deep", 2)
    if isinstance(value, str):
        if _normalize_string(value) is None:
            _fail("json_invalid", 2)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _normalize_string(key) is None:
                _fail("json_invalid", 2)
            _check_json_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_json_value(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        _fail("json_non_finite", 2)
    return value


def load_json_bytes(raw, *, max_bytes=MAX_FILE_BYTES):
    """严格解析原始 JSON 字节：拒绝重复键、非有限数字、超限和非法编码。

    与 ``load_json_file`` 共用同一严格解析路径；调用方负责在读取阶段限制字节数，
    这里仍按 ``max_bytes`` 再检查一次，避免直接入口绕过上限。
    """
    if not isinstance(raw, (bytes, bytearray)):
        _fail("json_invalid", 2)
    if len(raw) > max_bytes:
        _fail("file_too_large", 2)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        _fail("json_invalid", 2)
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                           parse_constant=_reject_constant, parse_float=_reject_float)
    except ReviewError:
        raise
    except RecursionError:
        _fail("json_too_deep", 2)
    except (ValueError, TypeError):
        _fail("json_invalid", 2)
    if not isinstance(value, dict):
        _fail("json_invalid", 2)
    try:
        # 先保持原有深度/非有限数字原因码（json_too_deep），再合并合法代理对。
        _check_json_value(value)
    except RecursionError:
        _fail("json_too_deep", 2)
    value = _normalize_json_tree(value, "json_invalid", 2)
    return value


def load_json_file(path, *, max_bytes=MAX_FILE_BYTES):
    """严格读取本地 JSON：拒绝重复键、非有限数字、超限文件和非法编码。

    先确认普通文件，再最多读取 max_bytes + 1 字节；不会把超大文件整体读入内存。
    """
    path = Path(path)
    try:
        info = path.stat()
    except OSError:
        _fail("io_error", 2)
    if not stat.S_ISREG(info.st_mode):
        _fail("io_error", 2)
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError:
        _fail("io_error", 2)
    if len(raw) > max_bytes:
        _fail("file_too_large", 2)
    return load_json_bytes(raw, max_bytes=max_bytes)


def identity_sha256(identity):
    """整个 identity 的规范 JSON 摘要；缺失或不可序列化都 fail closed。"""
    if not isinstance(identity, dict) or not identity:
        _fail("report_identity_missing", 4)
    try:
        return _digest(identity)
    except (TypeError, ValueError):
        _fail("report_identity_invalid", 4)


def _validate_http_status_pair(result):
    expected = result.get("expected_http_status")
    actual = result.get("http_status")
    if expected is not None and (type(expected) is not int or not 400 <= expected <= 499):
        _fail("report_structure_invalid", 2)
    if actual is not None and (type(actual) is not int or not 100 <= actual <= 599):
        _fail("report_structure_invalid", 2)
    count = result.get("citation_count")
    if type(count) is not int or count < 0:
        _fail("report_structure_invalid", 2)


def inspect_report(report):
    """校验报告结构、run_id、完整 identity 与逐 case 结果指纹绑定。"""
    if not isinstance(report, dict):
        _fail("report_structure_invalid", 2)
    # 直接服务入口也必须在计算指纹前处理代理对；否则 result_sha256 的
    # UTF-8 编码会抛出 UnicodeEncodeError，CLI 只剩 traceback。
    report = _normalize_json_tree(report, "report_structure_invalid", 2)
    try:
        validate_comparison_report(report, "report")
    except EvaluationError:
        _fail("report_structure_invalid", 2)
    run_id = report.get("run_id")
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 128:
        _fail("report_run_id_missing", 4)
    if type(report.get("result_fingerprint_version")) is not int \
            or report.get("result_fingerprint_version") != RESULT_FINGERPRINT_VERSION:
        _fail("report_result_fingerprint_missing", 4)
    identity_digest = identity_sha256(report.get("identity"))
    mode = report.get("mode")
    if mode not in ("offline", "live"):
        _fail("report_mode_invalid", 4)
    identity = report.get("identity")
    config = identity.get("config") if isinstance(identity, dict) else None
    if not isinstance(config, dict) or config.get("mode") != mode:
        # 报告模式与身份中声明的模式必须一致；不能把 offline 报告改标成 live 升级验收。
        _fail("report_mode_identity_mismatch", 4)
    if len(report["results"]) > MAX_CASES:
        _fail("report_structure_invalid", 2)
    results, seen = [], set()
    for result in report["results"]:
        if not isinstance(result, dict):
            _fail("report_structure_invalid", 2)
        case_id = result.get("id")
        if not _is_case_id(case_id):
            _fail("report_structure_invalid", 2)
        if case_id in seen:
            _fail("duplicate_report_case", 4)
        seen.add(case_id)
        if type(result.get("result_fingerprint_version")) is not int \
                or result.get("result_fingerprint_version") != RESULT_FINGERPRINT_VERSION:
            _fail("report_result_fingerprint_missing", 4)
        sha = result.get("result_sha256")
        if not _is_sha(sha):
            _fail("report_result_fingerprint_missing", 4)
        if type(result.get("machine_fingerprint_version")) is not int \
                or result.get("machine_fingerprint_version") != MACHINE_FINGERPRINT_VERSION \
                or not _is_sha(result.get("machine_sha256")):
            _fail("report_result_fingerprint_missing", 4)
        # 机器断言独立于正文绑定；默认无正文报告也必须先校验，不能直接信任 passed 等字段。
        try:
            machine_digest = machine_sha256(result)
        except (TypeError, ValueError, OverflowError):
            _fail("report_result_tampered", 4)
        if machine_digest != result["machine_sha256"]:
            _fail("report_result_tampered", 4)
        if type(result.get("passed")) is not bool:
            _fail("report_structure_invalid", 2)
        _validate_http_status_pair(result)
        recomputed = result_sha256(result)
        if "content" in result:
            if recomputed is None:
                _fail("report_result_tampered", 4)
            if recomputed != sha:
                _fail("report_result_tampered", 4)
            content = result.get("content")
            if not isinstance(content, dict) or content.get("citations") is None:
                _fail("report_result_tampered", 4)
            if len(content["citations"]) != result["citation_count"]:
                _fail("report_result_tampered", 4)
        results.append(result)
    return {"run_id": run_id, "identity_sha256": identity_digest, "mode": mode, "results": results}


def _validate_unknowns(value):
    if not isinstance(value, list) or len(value) > MAX_UNKNOWNS:
        _fail("reviews_entry_invalid", 2)
    if any(not isinstance(item, str) or not item.strip() or len(item) > MAX_UNKNOWN_CHARS for item in value):
        _fail("reviews_entry_invalid", 2)
    return list(value)


def _validate_reviewer(reviewer):
    if reviewer is None:
        return None
    if not isinstance(reviewer, str) or not _VERIFIED_BY_RE.fullmatch(reviewer):
        _fail("reviews_entry_invalid", 2)
    return reviewer


def _validate_reviewed_at(value):
    if value is None:
        return None
    match = _VERIFIED_AT_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        _fail("reviews_entry_invalid", 2)
    try:
        date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        _fail("reviews_entry_invalid", 2)
    return value


def _validate_review_entry(entry):
    if not isinstance(entry, dict) or set(entry) - _REVIEW_KEYS:
        _fail("reviews_entry_invalid", 2)
    if any(key not in entry for key in ("id", "result_sha256", "answer_correct", "citation_supported",
                                        "outcome_correct", "reviewer", "reviewed_at", "unknowns")):
        _fail("reviews_entry_invalid", 2)
    case_id = entry.get("id")
    if not _is_case_id(case_id):
        _fail("reviews_entry_invalid", 2)
    if not _is_sha(entry.get("result_sha256")):
        _fail("reviews_entry_invalid", 2)
    for key in _JUDGMENTS:
        if not _is_tristate(entry.get(key)):
            _fail("reviews_entry_invalid", 2)
    if "content" in entry and not isinstance(entry["content"], dict):
        _fail("reviews_entry_invalid", 2)
    clean = {"id": case_id, "result_sha256": entry["result_sha256"],
             "answer_correct": entry["answer_correct"], "citation_supported": entry["citation_supported"],
             "outcome_correct": entry["outcome_correct"],
             "reviewer": _validate_reviewer(entry.get("reviewer")),
             "reviewed_at": _validate_reviewed_at(entry.get("reviewed_at")),
             "unknowns": _validate_unknowns(entry.get("unknowns"))}
    if "content" in entry:
        # 保留核验人实际看到的正文；后续必须与报告正文严格一致才允许通过。
        clean["content"] = entry["content"]
    return clean


def validate_review_document(document):
    """核验文件结构；模板中的 null 判定合法但后续会判为未核验。"""
    if not isinstance(document, dict):
        _fail("reviews_schema_invalid", 2)
    # 核验正文、unknowns 和对象键同样可能携带孤立代理；先稳定拒绝。
    document = _normalize_json_tree(document, "reviews_schema_invalid", 2)
    if set(document) - _TOP_KEYS:
        _fail("reviews_schema_invalid", 2)
    if document.get("schema") != SCHEMA:
        _fail("reviews_schema_invalid", 2)
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 128:
        _fail("reviews_schema_invalid", 2)
    if not _is_sha(document.get("identity_sha256")):
        _fail("reviews_schema_invalid", 2)
    if type(document.get("result_fingerprint_version")) is not int \
            or document.get("result_fingerprint_version") != RESULT_FINGERPRINT_VERSION:
        _fail("reviews_schema_invalid", 2)
    mode = document.get("mode")
    if mode is not None and mode not in ("offline", "live"):
        _fail("reviews_schema_invalid", 2)
    reviews = document.get("reviews")
    if not isinstance(reviews, list) or len(reviews) > MAX_CASES:
        _fail("reviews_schema_invalid", 2)
    normalized, seen = [], set()
    for entry in reviews:
        clean = _validate_review_entry(entry)
        if clean["id"] in seen:
            _fail("reviews_duplicate_case", 2)
        seen.add(clean["id"])
        normalized.append(clean)
    return {"schema": SCHEMA, "run_id": run_id, "identity_sha256": document["identity_sha256"],
            "result_fingerprint_version": RESULT_FINGERPRINT_VERSION, "mode": mode, "reviews": normalized}


def create_template(report, *, include_content=False):
    """从报告生成空核验模板；所有判断保持 null，不自动勾选通过，不新建 run_id。"""
    info = inspect_report(report)
    reviews = []
    for result in info["results"]:
        entry = {"id": result["id"], "result_sha256": result["result_sha256"],
                 "answer_correct": None, "citation_supported": None, "outcome_correct": None,
                 "reviewer": None, "reviewed_at": None, "unknowns": []}
        if include_content:
            content = result.get("content")
            if not isinstance(content, dict):
                # 只有报告确实携带正文时才允许附加；不从远端或本地语料另行取正文。
                _fail("report_content_missing", 2)
            entry["content"] = content
        reviews.append(entry)
    return {"schema": SCHEMA, "run_id": info["run_id"], "identity_sha256": info["identity_sha256"],
            "result_fingerprint_version": RESULT_FINGERPRINT_VERSION, "mode": info["mode"],
            "reviews": reviews}


def _empty_judgment():
    return {"samples": 0, "applicable": 0, "verified": 0, "passed": 0, "failed": 0,
            "unverified": 0, "not_applicable": 0, "unknown": 0}


def _count(judgment, outcome, *, verified=True):
    if outcome == "not_applicable":
        judgment["not_applicable"] += 1
        return
    judgment["applicable"] += 1
    if outcome == "pass":
        if verified:
            judgment["verified"] += 1
        judgment["passed"] += 1
    elif outcome == "fail":
        if verified:
            judgment["verified"] += 1
        judgment["failed"] += 1
    else:
        judgment["unverified"] += 1
        judgment["unknown"] += 1


def _judgment_outcome(value, *, required):
    if value is None:
        return "unverified" if required else "not_applicable"
    return "pass" if value is True else "fail"


def evaluate_reviews(report, review_document):
    """把人工核验与报告逐 case 绑定并汇总；返回不含正文/核验人的摘要。"""
    info = inspect_report(report)
    document = validate_review_document(review_document)
    if document["run_id"] != info["run_id"]:
        _fail("run_id_mismatch", 4)
    if document["identity_sha256"] != info["identity_sha256"]:
        _fail("identity_mismatch", 4)
    if document["mode"] is not None and document["mode"] != info["mode"]:
        _fail("mode_mismatch", 4)
    results = {result["id"]: result for result in info["results"]}
    reviews = {review["id"]: review for review in document["reviews"]}
    unexpected = sorted(set(reviews) - set(results))
    if unexpected:
        _fail("unexpected_review_case", 4)
    for case_id in set(reviews) & set(results):
        review = reviews[case_id]
        result = results[case_id]
        if review["result_sha256"] != result["result_sha256"]:
            _fail("result_sha256_mismatch", 4)
        if "content" in review:
            # 核验人若保留了正文，必须证明其看到的就是本轮报告正文；否则旧 sha
            # 可能仍匹配，但核验对象已经被替换。
            if "content" not in result:
                _fail("review_content_mismatch", 4)
            try:
                same_content = _digest(review["content"]) == _digest(result["content"])
            except (TypeError, ValueError, OverflowError):
                _fail("review_content_invalid", 4)
            if not same_content:
                _fail("review_content_mismatch", 4)

    judgments = {key: _empty_judgment() for key in _JUDGMENTS}
    human = {"cases_total": len(info["results"]), "cases_verified": 0, "cases_failed": 0,
             "cases_unverified": 0, "cases_not_applicable": 0, "cases_without_review": 0}
    annotation = {"complete": 0, "incomplete": 0, "unreviewed": 0, "unknown": 0}
    issues = {}
    machine_passed = machine_failed = 0

    def issue(name):
        issues[name] = issues.get(name, 0) + 1

    for result in info["results"]:
        case_id = result["id"]
        state = result.get("review_state")
        annotation[state if state in annotation else "unknown"] += 1
        machine_ok = result["passed"]
        if machine_ok:
            machine_passed += 1
        else:
            machine_failed += 1
            issue("machine_assertion_failed")
        review = reviews.get(case_id)
        refusal_with_citations = (result.get("outcome") == "refuse"
                                  and (result.get("citation_count") or 0) > 0)
        if refusal_with_citations:
            issue("refusal_with_citations")

        http_negative = result.get("expected_http_status") is not None
        if http_negative:
            # 预期 HTTP 负例只核对机器状态，不做语义判断；三个判断均为不适用。
            for key in _JUDGMENTS:
                _count(judgments[key], "not_applicable")
            human["cases_not_applicable"] += 1

        if review is None:
            human["cases_without_review"] += 1
            issue("missing_review")
            if http_negative:
                pass
            else:
                outcome = result.get("expected_outcome")
                for key in _JUDGMENTS:
                    required = key == "outcome_correct" or outcome not in {"refuse", "clarify"}
                    if required:
                        _count(judgments[key], "unverified")
                    else:
                        _count(judgments[key], "not_applicable")
            if machine_ok and not refusal_with_citations:
                human["cases_unverified"] += 1
            else:
                human["cases_failed"] += 1
            continue

        metadata_ok = bool(review["reviewer"] and review["reviewed_at"]) and not review["unknowns"]
        if not metadata_ok:
            issue("review_metadata_incomplete" if not (review["reviewer"] and review["reviewed_at"])
                  else "open_unknowns")
        failed = refusal_with_citations or (not machine_ok)
        unverified = not metadata_ok

        if http_negative:
            for key in _JUDGMENTS:
                value = review[key]
                if value is False:
                    # 负例的语义判断不适用；显式 false 也不改变机器状态结论。
                    issue("semantic_judgment_ignored")
        else:
            outcome = result.get("expected_outcome")
            for key in _JUDGMENTS:
                if key == "outcome_correct":
                    value_outcome = _judgment_outcome(review[key], required=True)
                elif outcome in {"refuse", "clarify"}:
                    if review[key] is None:
                        value_outcome = "not_applicable"
                    else:
                        value_outcome = "pass" if review[key] is True else "fail"
                else:
                    value_outcome = _judgment_outcome(review[key], required=True)
                if not metadata_ok and value_outcome == "pass":
                    # 缺核验人/日期或仍有未决项时，true 不能被算作已核验通过；
                    # 显式 false 仍然保留为失败，继续阻断 reviewed_pass。
                    value_outcome = "unverified"
                _count(judgments[key], value_outcome, verified=metadata_ok)
                if value_outcome == "fail":
                    failed = True
                    issue(f"{key}_failed")
                elif value_outcome == "unverified":
                    unverified = True

        if failed:
            human["cases_failed"] += 1
        elif unverified:
            human["cases_unverified"] += 1
        else:
            human["cases_verified"] += 1

    if human["cases_total"] == 0:
        # 零样本不能宣称“逐条核验通过”。
        issue("no_cases")
    if human["cases_failed"]:
        status, exit_code = "failed", 1
    elif human["cases_total"] == 0 or human["cases_unverified"] or human["cases_without_review"]:
        status, exit_code = "unknown", 5
    else:
        status, exit_code = "reviewed_pass", 0
    for judgment in judgments.values():
        judgment["samples"] = human["cases_total"]

    return {"schema": SUMMARY_SCHEMA, "review_schema": SCHEMA,
            "run_id": info["run_id"], "identity_sha256": info["identity_sha256"],
            "result_fingerprint_version": RESULT_FINGERPRINT_VERSION, "mode": info["mode"],
            "review_status": status, "exit_code": exit_code, "cases": human["cases_total"],
            "annotation_review_state": annotation,
            "machine_assertions": {"passed": machine_passed, "failed": machine_failed},
            "human_review": {**human, **judgments},
            "issue_counts": dict(sorted(issues.items())),
            "boundary": {
                "declaration": "human_declared_not_automatic_semantic_proof",
                "real_business_acceptance": False,
                "production_acceptance": False,
                "mode": info["mode"],
                "notes": ["人工核验只记录核验人读取原文后的宣告，不是自动语义证明。",
                          "本轮未运行真实业务或生产验收；offline 回放不能称真实业务验收通过。",
                          "机器断言失败不能由人工 true 覆盖。"]},
            "notes": ["reviewed_pass 仅表示本轮机器断言与人工逐条核验均通过，不是准确率。",
                      "annotation_review_state 是问题集标注状态，与 human_review 是不同概念。",
                      "未核验、未知或未决项不会被计入通过。"]}


def output_collision(output_path, input_paths):
    """输出不得指向任何输入文件，避免覆盖报告、核验文件或模板。

    除解析符号链接后的路径比较外，还比较已存在文件的 inode/文件身份，
    防止硬链接别名绕过检查。
    """
    target = Path(output_path)
    try:
        target_name = os.path.normcase(str(target.resolve(strict=False)))
    except OSError:
        _fail("input_output_collision", 2)
    for path in input_paths:
        if path is None:
            continue
        candidate = Path(path)
        try:
            candidate_name = os.path.normcase(str(candidate.resolve(strict=False)))
        except OSError:
            _fail("input_output_collision", 2)
        if target_name == candidate_name:
            _fail("input_output_collision", 2)
        try:
            if target.exists() and candidate.exists() and os.path.samefile(target, candidate):
                _fail("input_output_collision", 2)
        except OSError:
            # 路径身份无法确认时 fail closed，不做“看起来不同”的假设。
            _fail("input_output_collision", 2)
