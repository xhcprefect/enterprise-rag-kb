"""业务基线本地输入预检：只读取显式声明的文件，绝不执行评测或宣称业务验收。

没有响应文件、令牌、服务地址、HTTP 客户端、数据库、模型或应用配置访问；
仅向 stdout 输出固定结构的 JSON。就绪只表示本地声明的输入结构完备。
"""
import argparse
import contextlib
import io
import json
import math
import re
import sys
from pathlib import Path

from app.services import evaluation as ev

RESULT_SCHEMA = "rag-eval-business-baseline-preflight/v1"
LOCAL_INPUT_SCHEMA = "rag-eval-local-input-fingerprints/v1"
MAX_CASES_DEFAULT = 20
MAX_CASES_LIMIT = 1000
EXIT_READY, EXIT_INVALID, EXIT_BLOCKED = 0, 2, 5
LEGAL_OUTCOMES = ("answer", "refuse", "clarify")
_REVIEW_STATES = ("complete", "incomplete", "unreviewed")
_STABLE_ERROR = "PREFLIGHT_INVALID_INPUT"
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_JSON_DEPTH = 16

# preflight 的输出是固定机器结构；不接收任何评测响应、令牌、URL 或写文件开关。
_FLAGS = frozenset((
    "measured", "business_acceptance", "live_execution", "network_access",
    "model_access", "database_access", "responses_access", "token_access",
    "service_url_access", "config_module_access", "dotenv_access",
    "local_files_written", "human_approval",
))
_OUTPUT_FIELDS = _FLAGS | frozenset((
    "schema", "state", "case_count", "max_cases", "strata_counts", "outcome_counts",
    "review_state_counts", "legal_strata", "legal_outcomes", "legal_review_states",
    "blocking_reasons", "input_fingerprints",
))
_ERROR_FIELDS = _FLAGS | frozenset(("schema", "state", "error_code"))
_FP_FIELDS = frozenset(("schema", "cases", "corpus", "config", "index_manifest"))


class _Invalid(Exception):
    """输入或 I/O 不合法；外层只暴露稳定错误码，不回显异常文本。"""


class _PreflightParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 默认会在错误文本里回显未知参数或私有路径；统一丢弃。
        raise _Invalid(message)


def _safe_text(value):
    """拒绝孤立 UTF-16 代理，避免 stdout 编码失败或把正文带进摘要。"""
    return isinstance(value, str) and not _SURROGATE_RE.search(value)


def _json_depth(value, depth=0):
    if depth > _MAX_JSON_DEPTH:
        return False
    if isinstance(value, dict):
        return all(_safe_text(key) and _json_depth(item, depth + 1)
                   for key, item in value.items())
    if isinstance(value, list):
        return all(_json_depth(item, depth + 1) for item in value)
    if isinstance(value, str):
        return _safe_text(value)
    return type(value) in (int, float, bool) or value is None


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _bounded_int(text):
    # 超长整数字面量会触发解释器限制；预检无需处理这种数值。
    if len(text) > 100:
        raise ValueError("oversized JSON integer")
    return int(text)


def _max_cases(value):
    try:
        number = _bounded_int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid case limit") from exc
    if not 1 <= number <= MAX_CASES_LIMIT:
        raise argparse.ArgumentTypeError("invalid case limit")
    return number


def _strict_json(text):
    """严格 JSON：拒绝 NaN/Infinity、重复键、孤立代理和过深结构。"""
    def no_constant(_value):
        raise ValueError("non-finite JSON constant")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(text, parse_constant=no_constant, parse_float=_finite_float,
                       parse_int=_bounded_int, object_pairs_hook=unique)
    if not _json_depth(value):
        raise ValueError("unsafe JSON value")
    return value


def _no_symlink_component(path):
    """显式路径的每一级都不能是链接；避免通过链接父目录绕过检查。"""
    if any(part.is_symlink() for part in (path, *path.parents)):
        return False
    return not path.is_symlink()


def _check_input_file(target):
    """显式给出的文件不得是符号链接；目录内容同样拒绝跟随链接。"""
    if not _no_symlink_component(target) or not target.is_file():
        raise _Invalid()
    if target.stat().st_size > _MAX_JSON_BYTES:
        raise _Invalid()


def _read_json_document(path):
    """显式 JSON 对象文件；不读示例配置、.env 或问题集旁目录。"""
    try:
        target = Path(path)
        _check_input_file(target)
        value = _strict_json(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError, RecursionError, _Invalid):
        raise _Invalid() from None
    if not isinstance(value, dict):
        raise _Invalid()
    return value


def _read_dataset(path):
    """复用 evaluation.read_jsonl 的行格式，再补严格 JSON 内容检查。"""
    try:
        target = Path(path)
        _check_input_file(target)
        text = target.read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            if line.strip():
                item = _strict_json(line)
                if not isinstance(item, dict):
                    raise _Invalid()
        return ev.read_jsonl(target)
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError,
            ev.EvaluationError, _Invalid):
        raise _Invalid() from None


def _reject_regression_cases(cases):
    if any("regression_protocol" in case for case in cases):
        raise _Invalid()


def _validate_cases(cases):
    try:
        return ev.validate_cases(cases)
    except (ev.EvaluationError, TypeError, ValueError):
        raise _Invalid() from None


def _validate_config(path):
    if path is None:
        return None, ["config_file_missing"]
    config = _read_json_document(path)
    if set(config) - set(ev.PUBLIC_CONFIG_KEYS):
        # 未知键名不回显；配置本身由显式声明者负责脱敏。
        raise _Invalid()
    try:
        ev.config_identity(mode="live", **config)
    except (ev.EvaluationError, TypeError):
        raise _Invalid() from None
    reasons = [f"config_field_missing:{name}"
               for name in ev.missing_live_config_fields(config, mode="live")]
    return config, reasons


def _validate_manifest(path):
    if path is None:
        return None, []
    document = _read_json_document(path)
    try:
        summary = ev.index_manifest_identity(document)
    except (ev.EvaluationError, TypeError, ValueError):
        raise _Invalid() from None
    return document, ["index_manifest_empty"] if summary["documents"] == 0 else []


def _validate_corpus_root(path):
    if path is None:
        return None, ["documents_dir_missing"]
    try:
        root = Path(path)
        if not _no_symlink_component(root) or not root.is_dir():
            raise _Invalid()
    except (OSError, ValueError, _Invalid):
        raise _Invalid() from None
    return root, []


def _annotation_problems(cases):
    """严格核验规则 + 逐条完整检查；只返回 1 起始序号和稳定原因码。"""
    problems = []
    try:
        ev.require_reviewed_cases(cases)
    except ev.EvaluationError as exc:
        # 现有严格格式只含 placeholder_case:N / review_incomplete_case:N。
        for item in str(exc).split(": ", 1)[-1].split(", "):
            match = re.fullmatch(r"(placeholder_case|review_incomplete_case):(\d+)", item)
            if not match:
                raise _Invalid() from None
            problems.append(match.group(0))
    for index, case in enumerate(cases, 1):
        # 分层适用于全部 case；预期 HTTP 4xx 负例只豁免 expected_outcome。
        if ev.case_stratum(case) == ev.STRATUM_UNCLASSIFIED:
            problems.append(f"stratum_unclassified_case:{index}")
        if case.get("expected_http_status") is not None:
            continue
        if case.get("expected_outcome") is None:
            problems.append(f"expected_outcome_missing_case:{index}")
        if case.get("mode", "rag") == "rag" and case.get("expected_outcome") == "answer" \
                and not case.get("expected_sources"):
            problems.append(f"expected_sources_missing_case:{index}")
    return problems


def _counts(cases):
    strata = dict.fromkeys(ev.CASE_STRATA, 0)
    outcomes = dict.fromkeys((*LEGAL_OUTCOMES, "http_error", "unspecified"), 0)
    reviews = dict.fromkeys(_REVIEW_STATES, 0)
    for case in cases:
        stratum = ev.case_stratum(case)
        if stratum in strata:
            strata[stratum] += 1
        outcome = case.get("expected_outcome")
        key = "http_error" if case.get("expected_http_status") is not None else (
            outcome if outcome in LEGAL_OUTCOMES else "unspecified")
        outcomes[key] += 1
        reviews[ev.review_state(case)] += 1
    return strata, outcomes, reviews


def _fingerprints(identity, *, corpus_present, config_present, manifest_present):
    """只从既有 build_identity 抽取输入摘要，不输出完整评测身份或任何原值。"""
    dataset = identity["dataset"]
    documents = dataset.get("documents") or {}
    corpus = None
    if corpus_present and documents.get("files"):
        corpus = {"files": documents["files"], "sha256": documents["sha256"]}
    index = dataset.get("index_manifest") or {}
    manifest = None
    if manifest_present and index.get("documents"):
        manifest = {"documents": index["documents"], "sha256": index["sha256"]}
    config = {"sha256": identity["config"]["sha256"]} if config_present else None
    return {
        "schema": LOCAL_INPUT_SCHEMA,
        "cases": {"schema": dataset["schema"], "case_count": dataset["case_count"],
                  "sha256": dataset["sha256"]},
        "corpus": corpus, "config": config, "index_manifest": manifest,
    }


def _flags():
    return {name: False for name in _FLAGS}


def _status(state, *, cases, max_cases, reasons, fingerprints):
    strata, outcomes, reviews = _counts(cases)
    result = {
        **_flags(), "schema": RESULT_SCHEMA, "state": state,
        "case_count": len(cases), "max_cases": max_cases,
        "strata_counts": strata, "outcome_counts": outcomes, "review_state_counts": reviews,
        "legal_strata": list(ev.CASE_STRATA), "legal_outcomes": list(LEGAL_OUTCOMES),
        "legal_review_states": list(_REVIEW_STATES),
        "blocking_reasons": reasons, "input_fingerprints": fingerprints,
    }
    assert set(result) == _OUTPUT_FIELDS
    assert set(result["input_fingerprints"]) == _FP_FIELDS
    return result


def _error_result():
    result = {**_flags(), "schema": RESULT_SCHEMA, "state": "invalid",
              "error_code": _STABLE_ERROR}
    assert set(result) == _ERROR_FIELDS
    return result


def _write_json(value):
    # 输出结构固定且不含输入正文；编码失败也不回退到会带正文的转义形式。
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"), allow_nan=False) + "\n")


def _parser():
    parser = _PreflightParser(description="业务基线本地输入预检（不执行评测）")
    parser.add_argument("--dataset", required=True, help="本地 JSONL 问题集")
    parser.add_argument("--documents-dir", help="显式声明的本地语料目录")
    parser.add_argument("--config-file", help="显式声明的公开配置 JSON")
    parser.add_argument("--index-manifest", help="显式声明的索引清单 JSON")
    parser.add_argument("--max-cases", type=_max_cases, default=MAX_CASES_DEFAULT,
                        help="本地输入 case 上限（1–1000）")
    return parser


def _run(args):
    if type(args.max_cases) is not int or not 1 <= args.max_cases <= MAX_CASES_LIMIT:
        raise _Invalid()
    cases = _validate_cases(_read_dataset(args.dataset))
    _reject_regression_cases(cases)
    config, config_reasons = _validate_config(args.config_file)
    manifest_doc, manifest_reasons = _validate_manifest(args.index_manifest)
    root, corpus_reasons = _validate_corpus_root(args.documents_dir)
    try:
        # 完整批次进入既有身份算法；只输出其中的输入摘要字段。
        identity = ev.build_identity(cases, mode="live", documents_dir=root,
                                     index_manifest=manifest_doc, **(config or {}))
    except (ev.EvaluationError, OSError, TypeError, ValueError, RecursionError):
        raise _Invalid() from None
    documents = identity["dataset"].get("documents") or {}
    if root is not None and not documents.get("files"):
        corpus_reasons = ["documents_dir_empty"]
    reasons = [*config_reasons, *corpus_reasons, *manifest_reasons,
               *_annotation_problems(cases)]
    if not cases:
        reasons.append("dataset_empty")
    if len(cases) > args.max_cases:
        reasons.append("case_limit_exceeded")
    reasons = list(dict.fromkeys(reasons))
    fingerprints = _fingerprints(
        identity, corpus_present=root is not None, config_present=config is not None,
        manifest_present=manifest_doc is not None)
    state = "blocked" if reasons else "ready"
    _write_json(_status(state, cases=cases, max_cases=args.max_cases,
                        reasons=reasons, fingerprints=fingerprints))
    return EXIT_BLOCKED if reasons else EXIT_READY


def main(argv=None):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            args = _parser().parse_args(argv)
        return _run(args)
    except _Invalid:
        _write_json(_error_result())
        return EXIT_INVALID
    except SystemExit as exc:
        if exc.code == 0:  # 正常 --help 用法
            raise
        _write_json(_error_result())
        return EXIT_INVALID
    except Exception:
        # 不回显 traceback、路径、原异常或任何输入正文。
        _write_json(_error_result())
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
