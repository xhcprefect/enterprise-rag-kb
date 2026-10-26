"""用固定合成文本核验云端模型已关闭思考；显式 --live 才调用，可能产生少量费用。"""
import argparse
import json
import math
import time

import httpx
from langchain_core.messages import HumanMessage

from app.core.config import settings
from app.services.cloud_models import CloudModelError, get_cloud_chat


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        parser.error("timeout 必须大于 0 且不超过 60 秒")
    if settings.LLM_PROVIDER != "cloud" or settings.LLM_REASONING_EFFORT != "none":
        parser.error("此检查要求 LLM_PROVIDER=cloud、LLM_REASONING_EFFORT=none")

    chat = get_cloud_chat()
    payload = chat._payload([HumanMessage(content="Synthetic connectivity test. Reply OK only.")], True)
    payload["max_tokens"] = min(64, settings.LLM_MAX_OUTPUT_TOKENS)
    summary = {"model": chat.model, "reasoning_effort": payload["reasoning_effort"],
               "passed": False, "content_chars": 0, "reasoning_chars": 0,
               "reasoning_tokens": None, "first_content_ms": None}
    start = time.perf_counter()
    terminal, has_content = False, False
    try:
        # 单次请求，不自动重试；只统计长度和用量，不输出正文、思考、API Key 或原始错误体。
        with httpx.Client(timeout=httpx.Timeout(args.timeout, connect=min(5.0, args.timeout))) as client:
            with client.stream("POST", chat.endpoint.url("/chat/completions"),
                               headers=chat.endpoint.headers(), json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        terminal = True
                        break
                    event = json.loads(data)
                    if not isinstance(event, dict) or event.get("error"):
                        raise CloudModelError("模型流返回错误")
                    usage = event.get("usage") or {}
                    details = usage.get("completion_tokens_details") or {}
                    for key, value in [("prompt_tokens", usage.get("prompt_tokens")),
                                       ("completion_tokens", usage.get("completion_tokens")),
                                       ("reasoning_tokens", details.get("reasoning_tokens"))]:
                        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                            summary[key] = value
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or choice.get("message") or {}
                    for field, counter in [("content", "content_chars"), ("reasoning_content", "reasoning_chars")]:
                        text = delta.get(field)
                        if text is not None and not isinstance(text, str):
                            raise CloudModelError("模型流字段类型异常")
                        summary[counter] += len(text or "")
                    if (delta.get("content") or "").strip():
                        has_content = True
                        if summary["first_content_ms"] is None:
                            summary["first_content_ms"] = round((time.perf_counter() - start) * 1000, 2)
                    if choice.get("finish_reason") is not None:
                        terminal = True
                        if choice["finish_reason"] == "content_filter":
                            raise CloudModelError("模型拒绝生成")
        summary["passed"] = (terminal and has_content and summary["reasoning_chars"] == 0
                             and summary["reasoning_tokens"] in (None, 0))
    except Exception as exc:
        summary["error_type"] = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            summary["http_status"] = exc.response.status_code
    summary["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 2)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
