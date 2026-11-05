"""确定性的 loopback OpenAI 兼容 chat/embedding 模型替身（V 夹具专用，全标准库）。

独立性：这是本轮 V（摘要压缩 + 本地项目索引）新写的替身，**不复用/不修改**旧的
``e2e_model_stub.py`` / ``e2e_env.py``。它只依赖标准库，返回可复现的 1024 维
真实向量（词袋哈希袋，L2 归一化，文本越相似余弦越高）与确定性对话流，并且
**只绑定回环地址**、向外不发任何 HTTP。

为什么单独写：真实集成测试不能访问真实模型，但链路必须走真实 HTTP+SSE 解析、
用量记录、摘要任务执行与项目向量化。本替身在此基础上额外支持：

- **摘要检测**：识别 summary 提示词（可配置 marker），返回**固定 JSON/schema/来源
  IDs** 输出，并**保留合成用户事实**（从一个纯本地的合成 fact 表按会话取）。
  只验证机制，绝不宣称真实模型摘要准确率。
- **内容区分**：不同的 prompt / 文本产生可区分的输出与向量（非零、可复现）。
- **注入**：chat/embedding 下一次失败、匹配文本失败、延迟、截断、usage 缺失等，
  供失败/迟到/重启场景。
- **有界总时长**：可设最长处理时长，超时后对所有新请求返回 503，避免替身因失控
  注入被永久占用。
- **失实零日志**：只保存合成/注入的 body 用于验 prompt，**绝不 log 客户端传来的
  真实凭据**（API key 头、authorization 等一律不落盘不打印）。

控制端点（不是 OpenAI 协议的一部分）：
  GET  /__control/state            => {schema, owner, nonce, config, counts, recent}
  POST /__control/config           => 注入/限时配置
  POST /__control/reset            => 清零计数并恢复默认
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
ATTESTATION_SCHEMA = "ragkb-verify-memory-projects"
_ASCII_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[㐀-鿿]")
_MAX_BODY = 4 * 1024 * 1024
_MAX_RECORDED = 200
# 摘要请求默认识别 marker（M 固定 schema 到位后可经配置覆盖，见 _default_config）。
SUMMARY_MARKER_DEFAULT = "会话摘要"
# 默认的确定性“已确认事实”来源表：按会话/用户问题关键词选取，用于验证
# “检查下一轮实际 LLM 输入包含已压缩事实”。这些是**合成**事实，非真实用户数据。
DEFAULT_FACTS = [
    {"source_id": "s-0001", "keyword": "周报",
     "fact": "用户要求按周交付项目周报（本地替身合成事实）"},
    {"source_id": "s-0002", "keyword": "中文",
     "fact": "用户偏好中文回答，拒绝英文冗长输出（合成）"},
]


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    out = _ASCII_WORD.findall(lowered)
    chars = _CJK.findall(lowered)
    out.extend(chars)
    out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return out


def embedding_vector(text: str) -> list[float]:
    """确定性 1024 维词袋哈希袋向量，L2 归一化；空文本给合法单位向量。"""
    vector = [0.0] * EMBEDDING_DIM
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
        weight = 1.0 if token.isascii() else 0.6
        vector[index] += weight
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        vector[0] = 1.0
        return vector
    return [v / norm for v in vector]


def _safe_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in value)
    return ""


def _usage(prompt: int, completion: int = 0) -> dict:
    return {"prompt_tokens": max(1, int(prompt)),
            "completion_tokens": max(0, int(completion)),
            "total_tokens": max(1, int(prompt) + max(0, int(completion)))}


def _classify_roles(messages: list[dict]) -> tuple[str, str, str]:
    """返回 (system_text, last_user_text, all_users_text)。"""
    system = next((m for m in messages if m.get("role") == "system"), None)
    users = [m for m in messages if m.get("role") == "user"]
    last_user = users[-1] if users else None
    return (_safe_text(system.get("content")) if system else "",
            _safe_text(last_user.get("content")) if last_user else "",
            "|".join(_safe_text(m.get("content")) for m in users))


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def _first_real_message_id(text: str) -> str | None:
    """从**实际输入**里挑一个出现在窗口内的真实消息 UUID。

    真实 worker 的 batch 文本形如 ``[user 3 message_id=<uuid>]\\n...``，M 的
    parse_summary 要求 source_message_ids 必须落在允许消息集内，硬编码 s-* 必被拒。
    这里按“输入里真的出现的 UUID”选来源——绝不放宽业务校验，只选在窗口内的真实 id。
    """
    match = _UUID_RE.search(text or "")
    return match.group(0).lower() if match else None


def build_summary_json(user_text: str, facts: list[dict]) -> str:
    """返回 M 固定的 ConversationMemory.structured JSON schema。

    顶层 ``{"version":1,"unfinished":false}`` + 固定顺序的七个数组区
    （goals/constraints/confirmed_facts/decisions/preferences/corrections/open_questions），
    每区元素 ITEM={"text","source_message_ids","verification"}，verification 三值之一。
    只有当用户历史**确实出现**某个合成 fact 的关键词时，才把它写进
    ``confirmed_facts``（verification=confirmed_by_user）——机制可验证，不是无脑塞表。

    来源 id 选择（关键）：
      - 若输入里**真的出现**了消息 UUID（真实 worker 的 batch 用 message_id=<uuid>），
        则用该真实 UUID 作为 fact 的来源——它必属本窗口，M 的 parse_summary 来源校验
        才能通过（不放宽任何业务校验，只挑窗口内出现过的真实 id）。
      - 否则回退到合成 ``s-*``（纯夹具自测，无真实消息 id 时用；同样属于输入窗口）。
    """
    def item(text, src_ids, verification):
        return {"text": text, "source_message_ids": list(src_ids), "verification": verification}

    real_id = _first_real_message_id(user_text)
    kept = []
    for fact in facts:
        if fact.get("keyword") and fact["keyword"] in user_text:
            # 来源优先级：注入/配置 fact 若已是真实 UUID 直接用；
            # 否则只要输入里真正出现真实 UUID（真实 worker 的 batch 行内 message_id），
            # 就选它——绝不能拿 s-* 冒充真实来源；只有窗口里没有真实 UUID 才回退合成 s-*。
            configured = fact.get("source_id")
            if configured and not str(configured).startswith("s-"):
                src = str(configured).lower()
            elif real_id:
                src = real_id
            else:
                src = configured or "s-0000"
            kept.append((fact["fact"], [src], "confirmed_by_user"))
    if not kept:
        fallback = real_id or "s-0000"
        kept = [("（未发现可保留的合成用户事实）", [str(fallback).lower() if not str(fallback).startswith("s-") else fallback], "to_verify")]

    # 关键（主审）：M 的 parse_summary 用**全部七个区**的来源做严格白名单校验，不只
    # confirmed_facts——goals/open_questions 等非 fact 区若硬编码 s-0101/s-0009（非真实
    # 消息 id），整份摘要仍会被拒。因此只要有真实输入的 UUID，所有带来源的条目都统一用
    # 该真实 id；只有窗口里确实没有真实 UUID（纯夹具自测）才回退合成 s-*。
    def _src(fallback_synth: str) -> list[str]:
        if real_id:
            return [real_id]
        return [fallback_synth]

    doc = {
        "version": 1,
        "unfinished": False,
        "goals": [item("继续推进项目（合成目标）", _src("s-0101"), "assistant_suggested")],
        "constraints": [],
        "confirmed_facts": [item(text, src_ids, verification) for text, src_ids, verification in kept],
        "decisions": [],
        "preferences": [],
        "corrections": [],
        "open_questions": [item("待确认下一交付（合成待办）", _src("s-0009"), "to_verify")],
    }
    return json.dumps(doc, ensure_ascii=False)


class StubState:
    def __init__(self, owner: str = "", nonce: str = "") -> None:
        self.lock = threading.Lock()
        self.owner = owner
        self.nonce = nonce
        self.facts = [dict(f, keyword=f.get("keyword", "")) for f in DEFAULT_FACTS]
        self.config = self._default_config()
        # 真实链注入：夹具可把 fact 的 source_id 设为会话里真实 DB 消息 id，使 stub 摘要
        # 通过 worker 的 parse_summary 来源校验（来源必须在允许消息集内）。
        self.fact_override: list[dict] | None = None
        self.counts = {
            "embedding_calls": 0, "embedding_vectors": 0,
            "chat_calls": 0, "chat_stream_calls": 0, "chat_json_calls": 0,
            "summary_calls": 0, "chat_failed": 0, "chat_truncated": 0,
        }
        self.recent: OrderedDict[int, dict] = OrderedDict()
        self._seq = 0
        self._started = time.monotonic()

    @staticmethod
    def _default_config() -> dict:
        return {
            "summary_marker": SUMMARY_MARKER_DEFAULT,
            "chat_fail_next": 0, "chat_fail_status": 500,
            "chat_delay_ms": 0, "chat_truncate": False,
            "chat_slow_match": "", "chat_slow_ms": 0,
            "embed_fail_next": 0, "embed_fail_status": 500, "embed_fail_match": "",
            "embedding_token_unit": 4,
            "max_lifetime_ms": 0,   # 0 = 不限；>0 则超时后所有新请求 503
            "redact_auth": True,    # 恒 True：绝不记录凭据头；保留该项便于人工核验
        }

    def configure(self, payload: dict) -> dict:
        with self.lock:
            # facts 覆盖：夹具可注入真实 source_id（真实 DB 消息 id），用于真实链通过
            # 来源校验；结构须为 [{source_id, keyword, fact}]。
            facts = payload.pop("facts", None)
            if facts is not None:
                if not isinstance(facts, list):
                    raise ValueError("facts 必须是列表")
                norm = []
                for f in facts:
                    if not isinstance(f, dict):
                        raise ValueError("facts 元素必须是对象")
                    for need in ("source_id", "keyword", "fact"):
                        if need not in f or not isinstance(f[need], str):
                            raise ValueError(f"facts 元素缺少字符串字段 {need}")
                    norm.append({"source_id": f["source_id"],
                                 "keyword": f["keyword"], "fact": f["fact"]})
                self.fact_override = norm
                self.facts = [dict(x) for x in norm]
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
            self.fact_override = None
            self.facts = [dict(f) for f in DEFAULT_FACTS]
            for key in self.counts:
                self.counts[key] = 0
            self.recent.clear()

    def snapshot(self) -> dict:
        with self.lock:
            return {"schema": ATTESTATION_SCHEMA, "owner": self.owner, "nonce": self.nonce,
                    "config": dict(self.config), "counts": dict(self.counts),
                    "recent": list(self.recent.values())}

    def expired(self) -> bool:
        with self.lock:
            limit = int(self.config.get("max_lifetime_ms", 0))
            return bool(limit > 0 and (time.monotonic() - self._started) * 1000 > limit)

    def record(self, kind: str, payload: dict) -> None:
        with self.lock:
            self.counts[f"{kind}_calls"] = self.counts.get(f"{kind}_calls", 0) + 1
            self._seq += 1
            self.recent[self._seq] = {"sequence": self._seq, "kind": kind,
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


class Handler(BaseHTTPRequestHandler):
    server_version = "VerifyMemoryProjectsStub/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - 覆盖基类签名
        # 即便 verbose 也绝不打印凭据头；这里只打行摘要（无 authorization）。
        if getattr(self.server, "verbose", False):
            sys.stderr.write("[vms-stub] " + fmt % args + "\n")

    # ---------- 基础 ----------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_BODY:
            raise ValueError("请求体为空或过大")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
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
    def do_GET(self):  # noqa: N802
        if not self._loopback_only():
            return
        if self.path == "/__control/state":
            self._send_json(HTTPStatus.OK, STATE.snapshot())
        elif self.path in {"/health", "/__health"}:
            self._send_json(HTTPStatus.OK, {"status": "ok", "stub": True})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        if not self._loopback_only():
            return
        if STATE.expired():
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": {"message": "stub 已达配置的最大有界时长"}})
            return
        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": f"body 无效: {exc}"}})
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/v1/embeddings":
                self._embeddings(payload)
            elif path == "/v1/chat/completions":
                self._chat(payload)
            elif path == "/__control/config":
                self._send_json(HTTPStatus.OK, {"config": STATE.configure(payload)})
            elif path == "/__control/reset":
                STATE.reset()
                self._send_json(HTTPStatus.OK, {"reset": True})
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001 - 替身必须把异常变 500，不裸崩
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": {"message": type(exc).__name__}})

    # ---------- /v1/embeddings ----------
    def _embeddings(self, payload: dict) -> None:
        raw_input = payload.get("input")
        texts = [raw_input] if isinstance(raw_input, str) else raw_input
        if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "input 必须非空"}})
            return
        match = STATE.config["embed_fail_match"]
        if match and any(match in item for item in texts) and STATE.take_failure("embed_fail_next"):
            STATE.bump("embedding_failures")
            self._send_json(int(STATE.config["embed_fail_status"]),
                            {"error": {"message": "注入的向量化失败"}})
            return
        # 只记录文本数量/模型名，不记录文本内容（保持极简，避免放大偶发敏感串）。
        STATE.record("embedding", {"count": len(texts), "model": payload.get("model")})
        unit = max(1, int(STATE.config["embedding_token_unit"]))
        tokens = max(1, math.ceil(sum(max(1, len(_tokens(t))) for t in texts) / unit))
        data = [{"object": "embedding", "index": i, "embedding": embedding_vector(t)}
                for i, t in enumerate(texts)]
        STATE.bump("embedding_vectors", len(data))
        self._send_json(HTTPStatus.OK, {
            "object": "list", "model": payload.get("model") or "verify-embedding",
            "data": data, "usage": {"prompt_tokens": tokens, "total_tokens": tokens}})

    # ---------- /v1/chat/completions ----------
    def _chat(self, payload: dict) -> None:
        stream = bool(payload.get("stream"))
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "messages 不能为空"}})
            return
        system_text, user_text, all_users = _classify_roles(messages)
        marker = STATE.config["summary_marker"]
        # 夹具自测（system 含 marker）与真实 worker（memory_worker._job_messages 的 Human
        # 提示“请输出摘要 JSON。”）都视作摘要请求——后者是 M 落盘的真实触发契约。
        is_summary = bool(marker and marker in system_text) or bool(
            user_text.startswith("请输出摘要 JSON"))
        # 只保存**合成/注入**请求的验证信息；系统提示和用户正文可能含敏感串，
        # 但我们在最近记录里只留“前缀截断 + 是否摘要 + 是否命中合成 fact 关键词”，
        # 不落任何疑似 API key / token 的完整正文。HEAD 一律不记录。
        summary_hit = [f["source_id"] for f in STATE.facts
                       if f.get("keyword") and f["keyword"] in all_users]
        STATE.record("chat_stream" if stream else "chat_json", {
            "kind": "summary" if is_summary else "chat",
            "system_head": system_text[:40],
            "user_head": user_text[:80],
            "summary_fact_hits": summary_hit,
            "model": payload.get("model"), "stream": stream,
            "max_tokens": payload.get("max_tokens"),
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
        if STATE.config["chat_slow_match"] and STATE.config["chat_slow_match"] in user_text:
            slow_ms = int(STATE.config["chat_slow_ms"])

        if is_summary:
            STATE.bump("summary_calls")
            # 真实 worker 的 batch（含 message_id=<uuid> 与合成事实关键词）在 SystemMessage
            # 里，用户 Human 只是“请输出摘要 JSON。”——必须把系统+用户文本合并传入，
            # 匹配关键词并就地提取真实消息 UUID，才能通过 M 的来源白名单校验。
            answer = build_summary_json(system_text + "\n" + all_users, STATE.facts)
        else:
            answer = f"（verify-memory-projects 本地替身）已收到：{user_text[:120]}"
        tokens = _chunks(answer)
        truncate = bool(STATE.config["chat_truncate"])
        finish = "length" if truncate else "stop"
        input_tokens = max(1, len(_tokens(all_users)) // 2)
        output_tokens = max(1, len(_tokens(answer)) // 2)
        if stream:
            self._chat_stream(tokens, finish=finish, usage=_usage(input_tokens, output_tokens),
                              slow_ms=slow_ms, model=payload.get("model"))
        else:
            self._send_json(HTTPStatus.OK, {
                "id": "chatcmpl-verify", "object": "chat.completion",
                "model": payload.get("model") or "verify-chat",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                             "finish_reason": finish}],
                "usage": _usage(input_tokens, output_tokens)})

    def _chat_stream(self, tokens: list[str], *, finish: str, usage: dict,
                     slow_ms: int, model) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def frame(data: dict) -> None:
            self.wfile.write(b"data: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        base = {"id": "chatcmpl-verify", "object": "chat.completion.chunk", "model": model or "verify-chat"}
        for token in tokens:
            frame({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": token},
                                        "finish_reason": None}]})
            if slow_ms:
                time.sleep(slow_ms / 1000)
        if truncate := bool(STATE.config["chat_truncate"]):
            STATE.bump("chat_truncated")
        frame({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        frame({**base, "choices": [], "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _chunks(text: str, size: int = 12) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="verify-memory-projects 本地 OpenAI 兼容替身")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--state-file", default="", help="启动后写入监听地址 JSON")
    parser.add_argument("--ready-file", default="", help="就绪标记文件（内容为 base_url）")
    parser.add_argument("--owner", default="")
    parser.add_argument("--nonce", default="")
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
            "owner": args.owner, "nonce": args.nonce, "schema": ATTESTATION_SCHEMA}
    if args.state_file:
        with open(args.state_file, "w", encoding="utf-8") as handle:
            json.dump(info, handle, ensure_ascii=False)
    if args.ready_file:
        with open(args.ready_file, "w", encoding="utf-8") as handle:
            handle.write(base_url)
    sys.stderr.write(f"[verify-memory-projects-stub] listening on {base_url}\n")
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
