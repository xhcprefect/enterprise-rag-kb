"""仅发送固定公开合成文本的模型连通性检查；需要显式 --live，可能产生少量费用。"""
import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings
from app.services.cloud_models import _embedding_endpoint, complete_short_chat
from app.services.vector_store import EMBEDDING_DIM


async def _embedding_probe(timeout):
    text = "合成连接测试：计划请假应提前三天申请。"
    cloud = settings.EMBEDDING_PROVIDER == "cloud"
    if cloud:
        endpoint = _embedding_endpoint()
        url, headers = endpoint.url("/embeddings"), endpoint.headers()
        payload = {"model": settings.EMBEDDING_MODEL, "input": [text], "encoding_format": "float"}
    else:
        url, headers = settings.OLLAMA_BASE_URL.rstrip("/") + "/api/embed", {}
        payload = {"model": settings.EMBEDDING_MODEL, "input": [text]}
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    vector = data["data"][0]["embedding"] if cloud else data["embeddings"][0]
    if type(vector) is not list or len(vector) != EMBEDDING_DIM or any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) for value in vector):
        raise ValueError("invalid embedding shape")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--only", choices=("llm", "embedding"))
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--output", default="evaluation-results/model-smoke.json")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60 or not 1 <= args.max_tokens <= 512:
        parser.error("timeout 必须为 0–60 秒，max-tokens 必须为 1–512")
    results = []
    for stage in ((args.only,) if args.only else ("llm", "embedding")):
        start = time.perf_counter()
        row = {"stage": stage, "passed": False}
        try:
            if stage == "llm":
                reply = complete_short_chat([SystemMessage(content="Synthetic connectivity test. Reply OK only."), HumanMessage(content="Reply OK to confirm this synthetic connectivity test.")], timeout=args.timeout, max_tokens=args.max_tokens)
                if not reply.strip():
                    raise ValueError("empty reply")
            else:
                asyncio.run(_embedding_probe(args.timeout))
            row["passed"] = True
        except Exception as exc:
            row["error_type"] = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                row["http_status"] = exc.response.status_code
        row["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 2)
        results.append(row)
        print(stage + ":", "passed" if row["passed"] else row.get("error_type"), row.get("http_status", ""))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"results": results, "scope": "upstream_connectivity_only"}, indent=2), encoding="utf-8")
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
