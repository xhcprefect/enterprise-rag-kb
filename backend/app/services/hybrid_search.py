"""无额外依赖的中文/标识符关键词、排名融合和可选 HTTP 重排。"""
import asyncio
import copy
import logging
import math
import re
import unicodedata

import httpx
from langchain_core.documents import Document

logger = logging.getLogger(__name__)
_STOP = ("请问", "请解释", "是什么", "为什么", "怎么", "如何", "是否", "哪些", "什么", "一下", "谢谢")


def _chinese_number(text):
    if text.isascii() and text.isdigit():
        return int(text)
    digits = dict(zip("零一二三四五六七八九两", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2)))
    total = current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char in "十百千":
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
        elif char == "万":
            total = (total + current) * 10000
            current = 0
        else:
            return None
    return total + current


def keyword_terms(text: str, max_terms: int = 64) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    result = []
    for match in re.finditer(r"第\s*([0-9零一二三四五六七八九十百千万两]+)\s*条", text):
        number = _chinese_number(match.group(1))
        if number is not None:
            result.append(f"article{number}")
    for word in re.findall(r"[a-z0-9]+(?:[_./:-][a-z0-9]+)*", text):
        if len(word) > 96:
            word = word[:96]
        if len(word) > 1:
            # 标点分隔的型号编码成一个纯字母数字词，避免全文解析器拆掉符号。
            result.append("x" + word.encode().hex() if re.search(r"[_./:-]", word) else word)
            result.extend(part for part in re.split(r"[_./:-]", word) if len(part) > 1)
    for stop in _STOP:
        text = text.replace(stop, " ")
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        if len(segment) <= 16:
            result.append(segment)
        for size in (3, 2):
            result.extend(segment[index:index + size] for index in range(len(segment) - size + 1))
    return list(dict.fromkeys(result))[:max(0, max_terms)]


def build_search_text(content: str, metadata: dict | None = None) -> str:
    context = " ".join(str((metadata or {}).get(key) or "") for key in ("filename", "section", "sheet"))
    return " ".join(keyword_terms(context + " " + content, max_terms=8192))


def _identity(doc):
    meta = doc.metadata
    return (str(meta.get("document_id") or meta.get("filename") or ""), meta.get("chunk_index"),
            meta.get("index_version") or 0, doc.page_content if meta.get("chunk_index") is None else "")


def fuse_results(vector_pairs, keyword_pairs, limit=200):
    merged = {}
    for method, pairs in (("vector", vector_pairs), ("keyword", keyword_pairs)):
        seen = set()
        for rank, (doc, distance) in enumerate(pairs, 1):
            key = _identity(doc)
            if key in seen:
                continue
            seen.add(key)
            if key not in merged:
                metadata = copy.deepcopy(doc.metadata)
                if method == "keyword":
                    metadata["citation_score"] = None
                merged[key] = [Document(page_content=doc.page_content, metadata=metadata), distance, 0.0, set(), len(merged)]
            entry = merged[key]
            entry[2] += 1.0 / (60 + rank)
            entry[3].add(method)
    ordered = sorted(merged.values(), key=lambda item: (-item[2], item[4]))[:limit]
    result = []
    for doc, distance, score, methods, _ in ordered:
        doc.metadata.update(fusion_score=score, retrieval_method="hybrid" if len(methods) == 2 else next(iter(methods)))
        result.append((doc, distance))
    return result


async def _rerank(query, pairs, base_url, api_key, model, timeout, max_chars):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "query": query, "documents": [doc.page_content[:max_chars] for doc, _ in pairs],
               "top_n": len(pairs), "return_documents": False}
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(2, timeout))) as client:
            response = await client.post(base_url.rstrip("/") + "/rerank", headers=headers, json=payload)
            response.raise_for_status()
            results = response.json().get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("invalid rerank results")
    selected, seen = [], set()
    for item in results:
        if not isinstance(item, dict):
            raise ValueError("invalid rerank item")
        index, score = item.get("index"), item.get("relevance_score")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(pairs) or index in seen:
            raise ValueError("invalid rerank index")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
            raise ValueError("invalid rerank score")
        seen.add(index)
        doc, distance = pairs[index]
        selected.append((Document(page_content=doc.page_content, metadata={**copy.deepcopy(doc.metadata), "rerank_score": float(score)}), distance))
    selected.extend(pair for index, pair in enumerate(pairs) if index not in seen)
    return selected


def rerank_results(query, pairs, *, base_url, api_key, model, timeout=3, max_chars=2000):
    if not pairs or not base_url or not model:
        return pairs
    head, tail = pairs[:40], pairs[40:]
    try:
        return [*asyncio.run(_rerank(query, head, base_url, api_key, model, timeout, max_chars)), *tail]
    except Exception as exc:
        logger.warning("重排失败，保留原候选顺序: %s", type(exc).__name__)
        return pairs
