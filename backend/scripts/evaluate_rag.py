"""固定问题集离线回放 / 显式真实调用；真实模式只串行提问，不自动上传或删除文档。"""
import argparse
import json
import math
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from app.services.evaluation import EvaluationError, grade_case, make_report, read_jsonl, run_live_case, validate_cases, validate_responses


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
    args = parser.parse_args(argv)
    if not 1 <= args.max_cases <= 1000 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
        parser.error("case 上限或请求超时不合法")
    for price in (args.input_price_per_million, args.output_price_per_million):
        if price is not None and (not math.isfinite(price) or price < 0):
            parser.error("价格必须为有限的非负值")
    try:
        cases = validate_cases(read_jsonl(args.dataset))[:args.max_cases]
        if not cases:
            raise EvaluationError("问题集为空")
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
        report = make_report(results, run_id)
        report["mode"] = "live" if args.live else "offline"
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(f"评测完成: {report['passed']}/{report['cases']} 固定断言通过; mode={report['mode']}; run_id={run_id}")
        print("报告已写入指定输出文件；断言通过不等同于语义事实正确。")
        return 0 if report["passed"] == report["cases"] else 1
    except (EvaluationError, OSError) as exc:
        print("评测未执行或未完成:", str(exc) if isinstance(exc, EvaluationError) else type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
