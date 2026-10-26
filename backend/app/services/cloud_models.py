"""OpenAI 兼容协议（硅基流动、sheapi.top 等）的向量与对话适配器。

为什么不直接用 ``langchain_openai``：项目约定不新增第三方依赖，而这类服务的
``/embeddings`` 与 ``/chat/completions`` 都是 OpenAI 协议，用已在依赖里的
``httpx`` 实现只有一百多行，反而少一个需要跟着 LangChain 版本走的包。

向量与对话**各自独立配置**（``EMBEDDING_*`` / ``LLM_*``），因此可以用 A 家的向量
模型配 B 家的对话模型。

向量空间说明：云端 ``BAAI/bge-m3`` 与本地 Ollama 的 ``bge-m3:latest`` 是同一份权重，
实测同文本余弦相似度 ≥0.9992（跨文本 0.53~0.61），因此切换服务商后历史向量无需重建。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessageChunk, BaseMessage

from app.core.config import settings

logger = logging.getLogger(__name__)

# 429/5xx 属于可重试；4xx（除 429）是请求本身的问题，重试没有意义
_RETRY_STATUS = {429, 500, 502, 503, 504}


class CloudModelError(RuntimeError):
    """模型服务返回错误，消息里带上可读的原因。"""


@dataclass(frozen=True)
class _Endpoint:
    """一个 OpenAI 兼容服务的连接信息。"""

    base_url: str
    api_key: str
    timeout: float

    @property
    def label(self) -> str:
        """用域名当服务名，报错时能一眼看出是哪家出的问题。"""
        return urlparse(self.base_url).hostname or "模型服务"

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def headers(self) -> dict[str, str]:
        key = (self.api_key or "").strip()
        if not key:
            raise CloudModelError(
                f"未配置 {self.label} 的 API Key，请在 backend/.env 中填写后重启服务"
            )
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


# 复用连接：入库时每批都要发一次请求，每次重新 TLS 握手会白白多花上百毫秒。
# httpx.Client 是线程安全的，FastAPI 的线程池里共用同一个实例没问题。
_clients: dict[str, httpx.Client] = {}


def _shared_client(endpoint: _Endpoint) -> httpx.Client:
    key = f"{endpoint.base_url}|{endpoint.timeout:g}"
    if key not in _clients:
        _clients[key] = httpx.Client(
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            timeout=httpx.Timeout(endpoint.timeout, connect=15),
        )
    return _clients[key]


def _post(endpoint: _Endpoint, path: str, payload: dict[str, Any], stream: bool = False):
    """带指数退避的 POST。

    ``stream=False`` 返回 ``httpx.Response``；``stream=True`` 返回
    ``(client, response)`` 二元组，调用方负责关闭两者。
    """
    url = endpoint.url(path)
    headers = endpoint.headers()
    last_error = ""

    for attempt in range(3):
        client = None
        try:
            if stream:
                # 流式响应不能被复用连接池回收，单独开一个客户端随用随关
                client = httpx.Client(
                    timeout=httpx.Timeout(endpoint.timeout, connect=15)
                )
                request = client.build_request("POST", url, headers=headers, json=payload)
                response = client.send(request, stream=True)
                if response.status_code in _RETRY_STATUS and attempt < 2:
                    response.close()
                    client.close()
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", "replace")
                    response.close()
                    client.close()
                    raise CloudModelError(
                        _format_error(endpoint.label, response.status_code, body)
                    )
                return client, response

            response = _shared_client(endpoint).post(url, headers=headers, json=payload)
            if response.status_code in _RETRY_STATUS and attempt < 2:
                last_error = _format_error(endpoint.label, response.status_code, response.text)
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise CloudModelError(
                    _format_error(endpoint.label, response.status_code, response.text)
                )
            return response
        except httpx.TimeoutException:
            if stream and client is not None:
                client.close()
            last_error = f"请求超时（{endpoint.timeout:g}s）"
        except httpx.RequestError as exc:
            if stream and client is not None:
                client.close()
            last_error = f"网络错误：{exc}"

        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))

    raise CloudModelError(f"调用 {endpoint.label} 失败：{last_error}")


def _format_error(label: str, status: int, body: str) -> str:
    """把 OpenAI 风格的错误体压成一行中文提示，避免把整段 HTML 抛给调用方。"""
    message = ""
    try:
        data = json.loads(body)
        message = data.get("message") or (data.get("error") or {}).get("message") or ""
    except (ValueError, AttributeError):
        pass
    if not message:
        # 5xx 常由 Cloudflare / 网关吐出整页 HTML（例如 sheapi.top 的 520），
        # 截前 300 字仍是 <!DOCTYPE html><html>… 的噪声，对用户毫无意义。
        # 这类响应体只说明"对方站点挂了"，换成一句可读的说明。
        if _looks_like_markup(body):
            message = _status_hint(status)
        else:
            message = body.strip()[:300] or _status_hint(status)
    if status == 401:
        return f"{label} 鉴权失败（401）：API Key 无效或已过期"
    if status == 402:
        return f"{label} 账户余额不足（402）：{message}"
    if status == 429:
        return f"{label} 触发限流（429）：{message}"
    return f"{label} 返回 {status}：{message}"


def _looks_like_markup(body: str) -> bool:
    """判断响应体是不是 HTML/XML 页面而非接口错误信息。"""
    head = body.lstrip()[:200].lower()
    return head.startswith(("<!doctype", "<html", "<?xml", "<head", "<body"))


def _status_hint(status: int) -> str:
    """给没有可读错误体的状态码配一句中文说明。"""
    if status >= 500:
        return "模型服务商站点异常，请稍后重试"
    if status == 404:
        return "接口地址不存在，请检查 LLM_BASE_URL 配置"
    if status == 403:
        return "无权访问该模型，请检查账号权限"
    return "服务端未返回可读的错误信息"


def _embedding_endpoint() -> _Endpoint:
    return _Endpoint(
        base_url=settings.EMBEDDING_BASE_URL,
        api_key=settings.EMBEDDING_API_KEY,
        timeout=settings.EMBEDDING_TIMEOUT,
    )


def _llm_endpoint() -> _Endpoint:
    return _Endpoint(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        timeout=settings.LLM_TIMEOUT,
    )


class OpenAICompatibleEmbeddings(Embeddings):
    """``langchain_core.embeddings.Embeddings`` 的最小实现，供 PGVector 使用。"""

    def __init__(self, endpoint: _Endpoint, model: str, batch_size: int):
        self.endpoint = endpoint
        self.model = model
        self.batch_size = max(1, batch_size)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = [
                t if t.strip() else " "  # 空串会被部分服务直接拒绝
                for t in texts[start : start + self.batch_size]
            ]
            response = _post(
                self.endpoint,
                "/embeddings",
                {"model": self.model, "input": batch, "encoding_format": "float"},
            )
            data = response.json()
            items = data.get("data") or []
            if len(items) != len(batch):
                raise CloudModelError(
                    f"向量条数不匹配：请求 {len(batch)} 条，返回 {len(items)} 条"
                )
            # 接口保证 data 与 input 同序，但仍按 index 排序，避免实现差异导致错位
            items.sort(key=lambda item: item.get("index", 0))
            vectors.extend(item["embedding"] for item in items)

        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class OpenAICompatibleChat:
    """只实现本项目用到的 ``stream()`` / ``invoke()``，接口形状对齐 LangChain。"""

    def __init__(self, endpoint: _Endpoint, model: str, temperature: float):
        self.endpoint = endpoint
        self.model = model
        self.temperature = temperature

    def _payload(self, messages: list[BaseMessage], stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_openai_message(m) for m in messages],
            "temperature": self.temperature,
            "stream": stream,
            "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
        }
        # 与本地 ChatOllama(reasoning=False) 对齐：Qwen3 系列不进入思考模式。
        # 思考模式会先吐 reasoning_content，长上下文下能拖到几十秒才出正文。
        # 只对 Qwen3 发送：其他模型（DeepSeek、Qwen2.5 等）不认识这个参数，
        # 部分网关会直接 400，所以按模型名判断而不是无条件带上。
        if stream and settings.LLM_STREAM_INCLUDE_USAGE:
            payload["stream_options"] = {"include_usage": True}
        if "qwen3" in self.model.lower():
            payload["enable_thinking"] = False
        return payload

    def invoke(self, messages: list[BaseMessage]) -> AIMessageChunk:
        response = _post(self.endpoint, "/chat/completions", self._payload(messages, False))
        data = response.json()
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError) as exc:
            raise CloudModelError(f"对话返回结构异常：{data}") from exc
        return AIMessageChunk(content=content)

    def stream(self, messages: list[BaseMessage]) -> Iterator[AIMessageChunk]:
        client, response = _post(
            self.endpoint, "/chat/completions", self._payload(messages, True), stream=True
        )
        terminal = False
        try:
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    terminal = True
                    break
                try:
                    event = json.loads(data)
                except ValueError as exc:
                    raise CloudModelError("模型服务返回了无效的流式数据") from exc
                if not isinstance(event, dict) or event.get("error"):
                    raise CloudModelError("模型服务在生成过程中返回错误，请稍后重试")
                usage = event.get("usage")
                if isinstance(usage, dict):
                    incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
                    if (isinstance(incoming, int) and not isinstance(incoming, bool) and incoming >= 0
                            and isinstance(outgoing, int) and not isinstance(outgoing, bool) and outgoing >= 0):
                        yield AIMessageChunk(content="", usage_metadata={"input_tokens": incoming, "output_tokens": outgoing,
                                                                      "total_tokens": incoming + outgoing})
                choices = event.get("choices") or []
                if not choices:
                    continue  # usage 等独立统计帧可以没有 choices。
                choice = choices[0]
                if choice.get("finish_reason") is not None:
                    terminal = True
                    if choice["finish_reason"] == "content_filter":
                        raise CloudModelError("模型服务拒绝生成该回答")
                delta = choice.get("delta") or choice.get("message") or {}
                text = delta.get("content")
                if text:
                    yield AIMessageChunk(content=text)
            if not terminal:
                raise CloudModelError("模型响应提前中断，请重试")
        finally:
            response.close()
            client.close()


def _to_openai_message(message: BaseMessage) -> dict[str, str]:
    role_map = {"system": "system", "human": "user", "ai": "assistant"}
    role = role_map.get(message.type, "user")
    content = message.content
    if not isinstance(content, str):
        # 多模态内容本项目用不到，退化成纯文本，避免把结构化对象直接丢给接口
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return {"role": role, "content": content}


async def _complete_short_chat(messages: list[BaseMessage], timeout: float, max_tokens: int) -> str:
    """单次、可取消的补全请求；总截止时间覆盖连接、响应读取和 JSON 解析。"""
    if settings.LLM_PROVIDER == "cloud":
        endpoint = _llm_endpoint()
        url = endpoint.url("/chat/completions")
        headers = endpoint.headers()
        # 主问答本就要求服务支持 SSE；部分网关的非流式接口只返回 usage，
        # 因此轻量调用也请求 SSE，在总截止时间内收集正文并主动关闭连接。
        payload = get_cloud_chat()._payload(messages, True)
        payload.update(temperature=0, max_tokens=max_tokens)
    else:
        url = settings.OLLAMA_BASE_URL.rstrip("/") + "/api/chat"
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": settings.LLM_MODEL,
            "messages": [_to_openai_message(message) for message in messages],
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": max_tokens,
                        "num_ctx": settings.LLM_CONTEXT_WINDOW_TOKENS},
        }
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(2.0, timeout))) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                if settings.LLM_PROVIDER == "cloud" and "text/event-stream" in response.headers.get("content-type", ""):
                    # 同一请求/总截止时间内消费到终止帧；兼容网关包裹的 message 帧。
                    parts, size, terminal = [], 0, False
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        value = line[5:].strip()
                        if value == "[DONE]":
                            terminal = True
                            break
                        event = json.loads(value)
                        if not isinstance(event, dict) or event.get("error"):
                            raise CloudModelError("追问补全服务返回错误")
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        text = (choice.get("delta") or choice.get("message") or {}).get("content")
                        if text is not None:
                            if not isinstance(text, str):
                                raise CloudModelError("追问补全返回结构异常")
                            parts.append(text)
                            size += len(text)
                            if size > min(32768, max(2000, max_tokens * 16)):
                                raise CloudModelError("追问补全响应超过长度限制")
                        if choice.get("finish_reason"):
                            if choice["finish_reason"] == "content_filter":
                                raise CloudModelError("追问补全服务拒绝生成")
                            terminal = True
                            break
                    if not terminal:
                        raise CloudModelError("追问补全流提前中断")
                    content = "".join(parts)
                else:
                    await response.aread()
                    data = response.json()
                    content = data["choices"][0]["message"]["content"] if settings.LLM_PROVIDER == "cloud" else data["message"]["content"]
                if not isinstance(content, str):
                    raise CloudModelError("追问补全返回结构异常")
                return content


def complete_short_chat(messages: list[BaseMessage], *, timeout: float, max_tokens: int) -> str:
    """由同步问答工作线程调用；失败不重试，由上层回退原始检索问题。"""
    return asyncio.run(_complete_short_chat(messages, timeout, max_tokens))


def get_cloud_embeddings() -> OpenAICompatibleEmbeddings:
    return OpenAICompatibleEmbeddings(
        _embedding_endpoint(),
        model=settings.EMBEDDING_MODEL,
        batch_size=settings.EMBEDDING_BATCH_SIZE,
    )


def get_cloud_chat() -> OpenAICompatibleChat:
    return OpenAICompatibleChat(
        _llm_endpoint(),
        model=settings.LLM_MODEL,
        temperature=settings.LLM_TEMPERATURE,
    )
