"""固定问题集离线回放 / 显式真实调用；真实模式只串行提问，不自动上传或删除文档。"""
import argparse
import json
import math
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from app.services.evaluation import (EvaluationError, build_identity, compare_reports, comparison_exit_code,
                                     grade_case, make_report, read_json_document, read_jsonl,
                                     require_reviewed_cases, run_live_case, validate_cases, validate_comparison_report,
                                     validate_responses, validate_thresholds)

# 允许进入配置身份的键；未知键直接拒绝，避免把令牌、URL 等私密值写进报告。
# 这里不含 RERANK_BASE_URL / RERANK_API_KEY：重排服务地址和密钥永远不进报告。
CONFIG_KEYS = ("model", "embedding_model", "chunk_size", "chunk_overlap", "retrieval_top_k", "retrieval_rewrite",
               "hybrid_retrieval_enabled", "rerank_enabled", "rerank_model", "retrieval_min_similarity",
               "retrieval_neighbor_chunks", "retrieval_max_per_doc", "rerank_max_chars", "keyword_candidates")


def _load_config(path):
    if not path:
        return {}
    value = read_json_document(path)
    unknown = sorted(set(value) - set(CONFIG_KEYS))
    if unknown:
        raise EvaluationError("评测配置含未知键: " + ", ".join(unknown))
    return value


def _load_index_manifest(path):
    """可选索引结构清单；格式见 README，只接受白名单字段。"""
    if not path:
        return None
    value = read_json_document(path)
    if isinstance(value, dict):
        unknown = sorted(set(value) - {"documents"})
        if unknown:
            raise EvaluationError("索引清单含未知键: " + ", ".join(unknown))
    return value


def _default_documents_dir(dataset_path):
    sibling = Path(dataset_path).parent / "documents"
    return sibling if sibling.is_dir() else None


def _precheck_regression_cases(parser, args, cases):
    """单条反馈回归协议预检：在任何网络或身份计算前拒绝不匹配的参数。

    旧问题集没有 regression_protocol 标记时完全不受影响；存在标记时必须恰好一个
    case、显式 run ID、模式一致、禁止 --kb-id 覆盖且不得携带 conversation_id。
    """
    marked = any("regression_protocol" in case for case in cases)
    if not marked:
        return
    if len(cases) != 1:
        parser.error("反馈回归评测必须恰好包含 1 个 case")
    case = cases[0]
    if type(case.get("regression_protocol")) is not int or case.get("regression_protocol") != 1:
        parser.error("反馈回归 case 的 regression_protocol 必须为 1")
    if args.run_id is None:
        parser.error("反馈回归评测必须显式指定 --run-id")
    if args.kb_id is not None:
        parser.error("反馈回归评测禁止使用 --kb-id 覆盖知识库")
    mode = case.get("regression_mode")
    if mode not in ("offline", "live"):
        parser.error("反馈回归 case 的 regression_mode 必须为 offline 或 live")
    if bool(args.live) != (mode == "live"):
        parser.error("--live/--responses 必须与 case 的 regression_mode 一致")
    if "conversation_id" in case:
        parser.error("反馈回归 case 不能包含 conversation_id")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(Path(__file__).resolve().parents[1] / "evaluation/cases.example.jsonl"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--responses", help="离线响应 JSONL 文件，不访问模型或数据库")
    mode.add_argument("--live", action="store_true", help="真实调用，可能产生模型费用，需要管理员访问令牌")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--token-env", default="RAG_EVAL_TOKEN")
    parser.add_argument("--member-token-env", default="RAG_EVAL_MEMBER_TOKEN", help="权限用例的普通用户令牌环境变量")
    parser.add_argument("--kb-id", type=uuid.UUID)
    parser.add_argument("--run-id", type=uuid.UUID, help="恢复同一轮评测时复用此 ID，服务端按相同请求标识重放")
    parser.add_argument("--max-cases", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    parser.add_argument("--include-content", action="store_true", help="报告包含问题、答案与引用正文，可能包含业务敏感信息")
    parser.add_argument("--output", default="evaluation-results/report.json")
    parser.add_argument("--documents-dir", help="声明为本地语料快照的目录；整棵目录参与身份计算，默认使用问题集同级的 documents/")
    parser.add_argument("--index-manifest", help="可选 JSON：document_id/index_version/content_sha256/chunk_count 结构清单，生成索引结构指纹")
    parser.add_argument("--require-reviewed", action="store_true",
                        help="严格业务模式：拒绝未完成人工核验或仍含模板占位符的用例；在任何真实调用前检查")
    parser.add_argument("--config-file", help="可选 JSON，仅允许公开检索/模型参数（含 hybrid/rerank 开关），拒绝 URL 与凭据")
    parser.add_argument("--baseline", help="用于比较的基线报告 JSON；缺身份或不可比时以专门退出码拒绝结论")
    parser.add_argument("--thresholds", help="可选阈值 JSON；未提供的指标使用默认门槛")
    parser.add_argument("--comparison-output", help="可选：把比较结果单独写入此 JSON 文件")
    args = parser.parse_args(argv)
    if not 1 <= args.max_cases <= 1000 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
        parser.error("case 上限或请求超时不合法")
    for price in (args.input_price_per_million, args.output_price_per_million):
        if price is not None and (not math.isfinite(price) or price < 0):
            parser.error("价格必须为有限的非负值")
    if args.comparison_output and not args.baseline:
        parser.error("--comparison-output 需要同时提供 --baseline")
    try:
        validated_cases = validate_cases(read_jsonl(args.dataset))
        # 单条反馈回归协议预检必须在读取响应、配置身份或发起网络请求之前完成。
        _precheck_regression_cases(parser, args, validated_cases)
        cases = validated_cases[:args.max_cases]
        if not cases:
            raise EvaluationError("问题集为空")
        if args.require_reviewed:
            # 严格业务模式必须在任何真实调用前拒绝未核验/占位符用例。
            require_reviewed_cases(cases)
        thresholds = validate_thresholds(read_json_document(args.thresholds)) if args.thresholds else {}
        config = _load_config(args.config_file)
        index_manifest = _load_index_manifest(args.index_manifest)
        documents_dir = Path(args.documents_dir) if args.documents_dir else _default_documents_dir(args.dataset)
        if args.documents_dir and not documents_dir.is_dir():
            # 显式指定的文档目录不存在是配置错误，不能悄悄退化成“无文档身份”。
            raise EvaluationError("--documents-dir 不是目录")
        # 配置值和身份必须在任何真实调用之前校验完毕，避免先付费调用再报配置错误。
        identity = build_identity(cases, mode="live" if args.live else "offline",
                                  documents_dir=documents_dir, index_manifest=index_manifest, **config)
        baseline = read_json_document(args.baseline) if args.baseline else None
        if baseline is not None:
            # 坏基线也必须在发起模型调用前拒绝；旧基线缺 identity 仍交由比较判为不可比。
            validate_comparison_report(baseline, "baseline")
        run_id = args.run_id or uuid.uuid4()
        responses = {}
        if not args.live:
            for item in validate_responses(read_jsonl(args.responses)):
                responses[item["id"]] = item
        results = []
        def grade(case, response):
            results.append(grade_case(case, response, input_price=args.input_price_per_million,
                                      output_price=args.output_price_per_million, include_content=args.include_content))
        if args.live:
            tokens = {}
            for case in cases:
                actor = case.get("actor", "admin")
                token = os.environ.get(args.member_token_env if actor == "member" else args.token_env, "").strip()
                if not token:
                    raise EvaluationError("指定的令牌环境变量为空")
                tokens[actor] = token
                if case.get("mode", "rag") == "rag" and not (args.kb_id or case.get("kb_id")):
                    raise EvaluationError("真实知识库评测需要 kb_id")
            url = urlparse(args.base_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise EvaluationError("base-url 必须是不含凭据或查询参数的 HTTP(S) 服务地址")
            if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"} and not args.allow_insecure_http:
                raise EvaluationError("远程明文 HTTP 需要显式 --allow-insecure-http，建议使用 HTTPS")
            with httpx.Client(timeout=httpx.Timeout(args.timeout, connect=min(10, args.timeout)), follow_redirects=False) as client:
                for case in cases:
                    try:
                        record = run_live_case(client, case, base_url=args.base_url,
                                               token=tokens[case.get("actor", "admin")],
                                               run_id=run_id, kb_id=str(args.kb_id) if args.kb_id else None)
                    except Exception as exc:
                        # 上游错误正文、请求头、URL 和密钥均不写入报告或 stdout。
                        record = {"error": type(exc).__name__}
                        if isinstance(exc, EvaluationError) and str(exc).startswith("HTTP ") and str(exc)[5:].isdigit():
                            record["http_status"] = int(str(exc)[5:])
                    grade(case, record)
        else:
            for case in cases:
                grade(case, responses.get(case["id"], {"error": "missing_response"}))
        report = make_report(results, run_id, identity=identity)
        report["mode"] = "live" if args.live else "offline"
        comparison = None
        if baseline is not None:
            # 基线只读取指标字段；旧报告缺少 identity 时比较结论为 incomparable，而不是崩溃。
            comparison = compare_reports(baseline, report, thresholds)
            report["comparison"] = comparison
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        if comparison is not None and args.comparison_output:
            target = Path(args.comparison_output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(comparison, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(f"评测完成: {report['passed']}/{report['cases']} 固定断言通过; mode={report['mode']}; run_id={run_id}")
        if comparison is not None:
            print(f"比较结论: {comparison['status']}/{comparison['verdict']}; reasons={','.join(comparison['reasons']) or '-'}; "
                  f"regressions={','.join(comparison['regressions']) or '-'}")
        print("报告已写入指定输出文件；断言通过不等同于语义事实正确。")
        return comparison_exit_code(comparison, assertions_passed=report["passed"] == report["cases"]) if comparison else (
            0 if report["passed"] == report["cases"] else 1)
    except (EvaluationError, OSError) as exc:
        print("评测未执行或未完成:", str(exc) if isinstance(exc, EvaluationError) else type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
