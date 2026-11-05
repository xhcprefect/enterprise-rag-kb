"""E2E 专用的本地 OpenAI 兼容模型替身（只绑定回环地址）。

为什么需要它：真实集成测试不能访问任何真实模型/付费服务，但链路又必须走
真实的 HTTP + SSE 解析、重试、用量记录与引用装配。本文件只依赖标准库，
返回**确定性**的 1024 维向量（词/双字词哈希袋，L2 归一化）与可复现的对话流，
并额外提供仅在回环可用的控制端点，用于注入失败/延迟/截断与统计调用次数。

控制端点（不是 OpenAI 协议的一部分）：
  POST /__control/config   {"chat_fail_next": 3, "chat_fail_status": 500,
                            "chat_delay_ms": 0, "chat_truncate": false,
                            "embed_fail_next": 0, "embed_fail_match": "",
                            "chat_slow_match": "", "chat_slow_ms": 0}
  GET  /__control/state    调用计数 + 最近一次配置
  POST /__control/reset    清零计数并清除注入配置

默认答案由**实际收到的【参考资料】块**产生，并只引用真正采用的块编号，
因此 `[1]` 等编号必然对应本轮上下文，不是伪造的 source API。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import threading
import time
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMBEDDING_DIM = 1024
ATTESTATION_SCHEMA = "ragkb-e2e-attestation"
_ASCII_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[一-鿿]")
_CITATION_BLOCK = re.compile(r"(?m)^\[(\d+)\] 来源：")
_REFUSAL = "根据现有知识库无法回答该问题。"
_MAX_BODY = 4 * 1024 * 1024
_MAX_RECORDED = 200


def _tokens(text: str) -> list[str]:
    """ASCII 词 + 中文单字/双字词，供确定性的“语义”向量使用。"""
    lowered = text.lower()
    tokens = _ASCII_WORD.findall(lowered)
    chars = _CJK.findall(lowered)
    tokens.extend(chars)
    tokens.extend(chars[index] + chars[index + 1] for index in range(len(chars) - 1))
    return tokens


def embedding_vector(text: str) -> list[float]:
    """词袋哈希向量：文本越相似，余弦相似度越高，且完全可复现。"""
    vector = [0.0] * EMBEDDING_DIM
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
        weight = 1.0 if token.isascii() else 0.6
        vector[index] += weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        # 空文本也要给出合法单位向量，避免下游出现 NaN/除零。
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


def _split_context(user_content: str) -> tuple[list[tuple[int, str]], str]:
    """从真实提示词里提取参考资料块与用户问题。"""
    question = user_content
    context = ""
    marker = "【用户问题】"
    if marker in user_content:
        head, _, tail = user_content.partition(marker)
        question = tail.strip()
        context = head
    blocks: list[tuple[int, str]] = []
    matches = list(_CITATION_BLOCK.finditer(context))
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(context)
        body = context[start:end].split("---", 1)[0].strip()
        if body:
            blocks.append((int(match.group(1)), body))
    return blocks, question


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    return [part.strip(" \t\r\n-") for part in parts if part.strip(" \t\r\n-")]


def _score(sentence: str, question: str) -> int:
    wanted = set(_tokens(question))
    return len(wanted & set(_tokens(sentence)))


def build_answer(user_content: str) -> tuple[str, list[int]]:
    """只依据传入的参考资料回答；没有可用资料时明确拒答且不给编号。"""
    blocks, question = _split_context(user_content)
    if not blocks or "【参考资料】" not in user_content:
        # 普通对话（无参考资料）也要给出稳定正文，但不能伪造引用编号。
        return f"（本地模型替身）已收到问题：{question[:80]}", []
    scored: list[tuple[int, int, str]] = []
    for index, body in blocks:
        for sentence in _sentences(body)[:12]:
            if len(sentence) < 4:
                continue
            scored.append((_score(sentence, question), index, sentence))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[int, str]] = []
    seen: set[str] = set()
    for score, index, sentence in scored:
        if score <= 0 or sentence in seen:
            continue
        seen.add(sentence)
        chosen.append((index, sentence[:220]))
        if len(chosen) == 2:
            break
    if not chosen:
        return _REFUSAL, []
    lines, used = [], []
    for number, (index, sentence) in enumerate(chosen, start=1):
        if index not in used:
            used.append(index)
        lines.append(f"{number}. {sentence} [{index}]")
    return "根据参考资料：\n" + "\n".join(lines), used


class StubState:
    def __init__(self, owner: str = "", nonce: str = "") -> None:
        self.lock = threading.Lock()
        # owner/nonce 由 harness 传入：让 capacity 与 Playwright 能证明
        # "这个替身是本次运行启动的"，而不是任何监听回环的相似服务。
        self.owner = owner
        self.nonce = nonce
        self.config = self._default_config()
        self.counts = {"embedding_calls": 0, "embedding_vectors": 0, "chat_calls": 0,
                       "chat_stream_calls": 0, "chat_json_calls": 0, "chat_failed": 0,
                       "chat_truncated": 0}
        self.recent: OrderedDict[int, dict] = OrderedDict()
        self._sequence = 0

    @staticmethod
    def _default_config() -> dict:
        return {
            "chat_fail_next": 0, "chat_fail_status": 500, "chat_delay_ms": 0,
            "chat_truncate": False, "chat_slow_match": "", "chat_slow_ms": 0,
            "embed_fail_next": 0, "embed_fail_status": 500, "embed_fail_match": "",
            "embedding_token_unit": 4,
        }

    def snapshot(self) -> dict:
        with self.lock:
            return {"schema": ATTESTATION_SCHEMA, "owner": self.owner, "nonce": self.nonce,
                    "config": dict(self.config), "counts": dict(self.counts),
                    "recent": list(self.recent.values())}

    def configure(self, payload: dict) -> dict:
        with self.lock:
            for key, value in payload.items():
                if key not in self.config:
                    raise ValueError(f"未知配置项 {key}")
                if isinstance(self.config[key], bool):
                    if not isinstance(value, bool):
                        raise ValueError(f"{key} 必须是布尔值")
                elif isinstance(self.config[key], int):
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        raise ValueError(f"{key} 必须是非负整数")
                elif not isinstance(value, str):
                    raise ValueError(f"{key} 必须是字符串")
                self.config[key] = value
            return dict(self.config)

    def reset(self) -> None:
        with self.lock:
            self.config = self._default_config()
            for key in self.counts:
                self.counts[key] = 0
            self.recent.clear()

    def record(self, kind: str, payload: dict) -> None:
        with self.lock:
            self.counts[f"{kind}_calls"] = self.counts.get(f"{kind}_calls", 0) + 1
            self._sequence += 1
            self.recent[self._sequence] = {"sequence": self._sequence, "kind": kind,
                                           "at": round(time.time(), 3), "payload": payload}
            while len(self.recent) > _MAX_RECORDED:
                self.recent.popitem(last=False)

    def bump(self, key: str, amount: int = 1) -> None:
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + amount

    def take_failure(self, key: str) -> bool:
        with self.lock:
            if self.config[key] > 0:
                self.config[key] -= 1
                return True
            return False


STATE = StubState()


def _usage(prompt: int, completion: int = 0) -> dict:
    prompt = max(1, int(prompt))
    completion = max(0, int(completion))
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion}


def _safe_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in value)
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "RagkbE2EStub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - 覆盖基类签名
        # 用 getattr：任何没有经过 main() 就挂上 Handler 的场景（例如单测里
        # 直接构造 HTTPServer）都不该在发响应前抛 AttributeError 而断开连接。
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[stub] " + fmt % args + "\n")

    # ---------- 基础响应 ----------
    def _send_json(self, status: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY:
            raise ValueError("请求体为空或过大")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _loopback_only(self) -> bool:
        host = self.client_address[0]
        if host in {"127.0.0.1", "::1"}:
            return True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": {"message": "stub 只接受回环连接"}})
        return False

    # ---------- 路由 ----------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        if not self._loopback_only():
            return
        if self.path == "/__control/state":
            self._send_json(HTTPStatus.OK, STATE.snapshot())
            return
        if self.path in {"/health", "/__health"}:
            self._send_json(HTTPStatus.OK, {"status": "ok", "stub": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        if not self._loopback_only():
            return
        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": f"请求体无效: {exc}"}})
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/v1/embeddings":
                self._embeddings(payload)
            elif path == "/v1/chat/completions":
                self._chat(payload)
            elif path == "/__control/config":
                config = STATE.configure(payload)
                self._send_json(HTTPStatus.OK, {"config": config})
            elif path == "/__control/reset":
                STATE.reset()
                self._send_json(HTTPStatus.OK, {"reset": True})
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001 - 替身必须把异常变成 500，不能让线程裸崩
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": type(exc).__name__}})

    # ---------- /v1/embeddings ----------
    def _embeddings(self, payload: dict) -> None:
        raw_input = payload.get("input")
        texts = [raw_input] if isinstance(raw_input, str) else raw_input
        if not isinstance(texts, list) or not texts or not all(isinstance(item, str) for item in texts):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "input 必须是字符串或字符串数组"}})
            return
        match = STATE.config["embed_fail_match"]
        if match and any(match in item for item in texts):
            if STATE.take_failure("embed_fail_next"):
                STATE.bump("embedding_failures")
                self._send_json(int(STATE.config["embed_fail_status"]),
                                {"error": {"message": "注入的向量化失败"}})
                return
        STATE.record("embedding", {"count": len(texts), "model": payload.get("model")})
        unit = max(1, int(STATE.config["embedding_token_unit"]))
        # 用量按“词数 / unit”向上取整，至少 1，保证 usage 恒为真实正数。
        tokens = max(1, math.ceil(sum(max(1, len(_tokens(item))) for item in texts) / unit))
        data = [{"object": "embedding", "index": index, "embedding": embedding_vector(item)}
                for index, item in enumerate(texts)]
        STATE.bump("embedding_vectors", len(data))
        self._send_json(HTTPStatus.OK, {
            "object": "list", "model": payload.get("model") or "e2e-embedding",
            "data": data,
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        })

    # ---------- /v1/chat/completions ----------
    def _chat(self, payload: dict) -> None:
        stream = bool(payload.get("stream"))
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "messages 不能为空"}})
            return
        user = next((item for item in reversed(messages) if item.get("role") == "user"), None)
        system = next((item for item in messages if item.get("role") == "system"), None)
        user_text = _safe_text(user.get("content")) if isinstance(user, dict) else ""
        system_text = _safe_text(system.get("content")) if isinstance(system, dict) else ""
        is_rewrite = "补全检索问题" in system_text
        STATE.record("chat_stream" if stream else "chat_json", {
            "system_head": system_text[:40],
            "user_head": user_text[:80], "model": payload.get("model"),
            "stream": stream, "max_tokens": payload.get("max_tokens"),
        })
        if STATE.take_failure("chat_fail_next"):
            STATE.bump("chat_failed")
            self._send_json(int(STATE.config["chat_fail_status"]),
                            {"error": {"message": "注入的对话失败", "type": "server_error"}})
            return
        delay_ms = int(STATE.config["chat_delay_ms"])
        if delay_ms:
            time.sleep(delay_ms / 1000)
        slow_ms = 0
        slow_match = STATE.config["chat_slow_match"]
        if slow_match and slow_match in user_text:
            slow_ms = int(STATE.config["chat_slow_ms"])
        if "你是后台摘要器" in system_text:
            STATE.bump("summary_calls")
            answer = self._summary_answer(user_text)
            tokens = [answer]
        elif is_rewrite:
            answer = self._rewrite_answer(user_text)
            tokens = [answer]
        else:
            answer, _used = build_answer(user_text)
            tokens = _chunks(answer)
        truncate = bool(STATE.config["chat_truncate"])
        finish = "length" if truncate else "stop"
        input_tokens = max(1, len(_tokens(user_text)) // 2)
        output_tokens = max(1, len(_tokens(answer)) // 2)
        if stream:
            self._chat_stream(tokens, finish=finish, usage=_usage(input_tokens, output_tokens),
                              slow_ms=slow_ms, truncate=truncate, model=payload.get("model"))
        else:
            self._send_json(HTTPStatus.OK, {
                "id": "chatcmpl-e2e", "object": "chat.completion", "model": payload.get("model") or "e2e-chat",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                             "finish_reason": finish}],
                "usage": _usage(input_tokens, output_tokens),
            })

    @staticmethod
    def _summary_answer(user_text: str) -> str:
        result = {"version": 1, "unfinished": False, **{key: [] for key in (
            "goals", "constraints", "confirmed_facts", "decisions", "preferences", "corrections", "open_questions",
        )}}
        source = re.search(r"\[user \d+ message_id=([0-9a-f-]{36})\]\n([^\n]+)", user_text)
        if source:
            result["confirmed_facts"] = [{"text": "用户说：" + source[2][:160],
                "source_message_ids": [source[1]], "verification": "confirmed_by_user"}]
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _rewrite_answer(user_text: str) -> str:
        """追问补全：原样返回问题（确定性），保持 JSON 形状。"""
        try:
            data = json.loads(user_text)
        except ValueError:
            data = {}
        question = data.get("question") if isinstance(data, dict) else None
        if not isinstance(question, str) or not question.strip():
            question = "该问题"
        return json.dumps({"action": "search", "query": question.strip()[:500]}, ensure_ascii=False)

    def _chat_stream(self, tokens: list[str], *, finish: str, usage: dict, slow_ms: int,
                     truncate: bool, model) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def frame(data: dict) -> None:
            self.wfile.write(b"data: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        base = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "model": model or "e2e-chat"}
        for token in tokens:
            frame({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": token},
                                        "finish_reason": None}]})
            if slow_ms:
                time.sleep(slow_ms / 1000)
        if truncate:
            STATE.bump("chat_truncated")
            # 声明 length 后立即结束，客户端据此显示“已达到长度上限”。
            frame({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        else:
            frame({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        # usage 帧在 finish 之后；客户端有独立的宽限窗口，缺 usage 不会破坏已完成正文。
        frame({**base, "choices": [], "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _chunks(text: str, size: int = 12) -> list[str]:
    return [text[index:index + size] for index in range(0, len(text), size)] or [""]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E2E 本地 OpenAI 兼容模型替身")
    parser.add_argument("--host", default="127.0.0.1", help="只能绑定回环地址")
    parser.add_argument("--port", type=int, default=0, help="0 表示由系统分配")
    parser.add_argument("--state-file", default="", help="启动后把实际监听地址写入该 JSON 文件")
    parser.add_argument("--ready-file", default="", help="就绪标记文件（内容为 base_url）")
    parser.add_argument("--owner", default="", help="harness owner 标记，供调用方核验归属")
    parser.add_argument("--nonce", default="", help="本次运行的一次性 nonce，供调用方核验")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("模型替身只允许绑定回环地址")
    global STATE
    STATE = StubState(owner=args.owner, nonce=args.nonce)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.verbose = args.verbose  # type: ignore[attr-defined]
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}/v1"
    info = {"base_url": base_url, "host": host, "port": port, "pid": None,
            "owner": args.owner, "nonce": args.nonce}
    if args.state_file:
        with open(args.state_file, "w", encoding="utf-8") as handle:
            json.dump(info, handle, ensure_ascii=False)
    if args.ready_file:
        with open(args.ready_file, "w", encoding="utf-8") as handle:
            handle.write(base_url)
    sys.stderr.write(f"[stub] listening on {base_url}\n")
    sys.stderr.flush()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
