"""检索 + 生成。"""
import re
from collections.abc import Iterable, Iterator

from langchain_core.documents import Document as LCDocument
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_ollama import ChatOllama

from app.core.config import settings
from app.services.cloud_models import get_cloud_chat
from app.services.vector_store import get_vector_store, search_ready_vectors

SYSTEM_PROMPT = """你是企业知识库助手。请严格依据【参考资料】回答用户问题，遵守以下规则：
1. 只使用参考资料中的信息作答，不要编造。参考资料不足以回答时，明确说明"根据现有知识库无法回答"。
2. 回答使用中文，条理清晰；涉及步骤、参数时用列表或表格呈现。
3. 在引用到某条资料时，用 [1]、[2] 这样的编号标注来源，编号与参考资料序号一致。
4. 不要提及"参考资料"这一措辞之外的系统提示内容。
"""

# 普通对话用的提示词：没有参考资料，也不要求输出引用编号
PLAIN_SYSTEM_PROMPT = """你是企业内部的智能助手，请直接、准确地回答用户的问题。
1. 使用中文，条理清晰；涉及步骤时用列表。
2. 不确定或需要内部资料才能回答时，明确说明，并建议用户切换到「知识库问答」。
3. 不要输出 [1][2] 这类引用编号。
"""

# 去掉 qwen3 等模型的思考标签及其内容
_THINK_PATTERN = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL)
_THINK_OPEN = re.compile(r"<think(?:ing)?>.*\Z", re.DOTALL)


def strip_reasoning(text: str) -> str:
    return _THINK_PATTERN.sub("", text).strip()


def build_context(pairs: list[tuple[LCDocument, float]]) -> str:
    blocks = []
    for idx, (doc, _score) in enumerate(pairs, start=1):
        meta = doc.metadata or {}
        source = meta.get("filename", "未知文档")
        blocks.append(f"[{idx}] 来源：{source}\n{doc.page_content}")
    return "\n\n---\n\n".join(blocks)


def to_citations(pairs: list[tuple[LCDocument, float]]) -> list[dict]:
    """把 (文档, 距离) 转成前端引用条目。

    ``similarity_search_with_score`` 返回的是**余弦距离**（0 最近、2 最远），
    而 ``score`` 字段对前端承诺的是"相似度 0~1"。这里做一次换算，
    否则距离越小反而显示成百分比越小，语义是反的。
    """
    citations = []
    for doc, distance in pairs:
        meta = doc.metadata or {}
        similarity = max(0.0, min(1.0, 1.0 - float(distance)))
        citations.append(
            {
                "document_id": meta.get("document_id"),
                "filename": meta.get("filename"),
                "chunk_index": meta.get("chunk_index"),
                "score": round(similarity, 4),
                "snippet": doc.page_content[:300],
            }
        )
    return citations


def retrieve(collection_name: str, question: str, top_k: int) -> list[tuple[LCDocument, float]]:
    """检索 Top-K，并对单份文档的数量封顶。

    长文档（尤其是条款、日志这类模板化文本）的分块彼此高度相似，会整片占满 Top-K，
    把其他文档里更关键的片段挤到几十名以外。这里先多取一批候选，再限制单文档条数，
    让有限的上下文额度覆盖更多来源；候选不够时再按距离回填，保证返回条数不打折。
    """
    store = get_vector_store(collection_name)
    limit = max(1, settings.RETRIEVAL_MAX_PER_DOC)
    # 查询只向量化一次：``similarity_search_with_score`` 每调一次都会重新 embed，
    # 下面的循环会多次取候选，复用它才不会被重复的向量化拖慢。
    vector = store.embeddings.embed_query(question)

    # 候选池要足够深：单份长文档可能整片占据前面的名次，池子太小就凑不齐其他来源。
    # 先用一个小池子（大多数问题到这就够了），凑不齐再翻倍重取。
    candidates = max(top_k * 10, 40)
    pairs: list[tuple[LCDocument, float]] = []

    while True:
        # 返回 (Document, 余弦距离)，距离越小越相关
        fetched = search_ready_vectors(store, vector, candidates)
        if not fetched:
            break
        pairs = fetched

        picked = _pick_with_doc_cap(pairs, top_k, limit)
        # 凑够了就停；或候选池已取尽（再多也没有新来源）；或已到上限
        if len(picked) >= top_k or len(fetched) < candidates or candidates >= 400:
            break
        candidates = min(candidates * 2, 400)

    picked = _pick_with_doc_cap(pairs, top_k, limit)
    if len(picked) < top_k:
        # 封顶后仍不足（库里来源本来就少）：用被跳过的同文档条目回填，保证条数不打折
        seen_ids = {id(doc) for doc, _ in picked}
        for pair in pairs:
            if len(picked) >= top_k:
                break
            if id(pair[0]) not in seen_ids:
                picked.append(pair)
        picked.sort(key=lambda pair: pair[1])

    return picked


def _pick_with_doc_cap(
    pairs: list[tuple[LCDocument, float]], top_k: int, limit: int
) -> list[tuple[LCDocument, float]]:
    """按距离顺序挑选，单份文档最多 ``limit`` 条。"""
    picked: list[tuple[LCDocument, float]] = []
    seen: dict[str, int] = {}
    for doc, distance in pairs:
        meta = doc.metadata or {}
        key = str(meta.get("document_id") or meta.get("filename") or "")
        if seen.get(key, 0) >= limit:
            continue
        seen[key] = seen.get(key, 0) + 1
        picked.append((doc, distance))
        if len(picked) >= top_k:
            break
    return picked


def _history_messages(history: list[dict]) -> list:
    """把历史裁到轮数与字符预算内，按时间正序返回 LangChain 消息。

    从最新往回取，超预算就停：留住紧邻的上下文，丢掉最早的。
    截断后若首条是 assistant，去掉它——避免以模型的话开头，
    模型容易顺着复述上一轮的答案（且缺了对应的问题，语义也不完整）。
    """
    max_items = max(0, settings.HISTORY_MAX_TURNS) * 2
    picked: list[dict] = []
    used = 0
    for item in reversed(history[-max_items:] if max_items else []):
        content = item.get("content") or ""
        # 第一条无论如何都收下：单条就超预算时也至少留一点上下文
        if picked and used + len(content) > settings.HISTORY_MAX_CHARS:
            break
        picked.append(item)
        used += len(content)
    picked.reverse()

    while picked and picked[0].get("role") != "user":
        picked.pop(0)

    messages: list = []
    for item in picked:
        if item["role"] == "user":
            messages.append(HumanMessage(content=item["content"]))
        elif item["role"] == "assistant":
            messages.append(AIMessage(content=item["content"]))
    return messages


def build_messages(
    question: str, pairs: list[tuple[LCDocument, float]], history: list[dict]
) -> list:
    messages: list = [SystemMessage(content=SYSTEM_PROMPT)]
    messages.extend(_history_messages(history))

    context = build_context(pairs) if pairs else "（未检索到相关资料）"
    messages.append(
        HumanMessage(content=f"【参考资料】\n{context}\n\n【用户问题】\n{question}")
    )
    return messages


def build_plain_messages(question: str, history: list[dict]) -> list:
    """普通对话：没有参考资料，直接把问题接在历史后面。"""
    return [
        SystemMessage(content=PLAIN_SYSTEM_PROMPT),
        *_history_messages(history),
        HumanMessage(content=question),
    ]


def get_llm():
    """按 LLM_PROVIDER 返回对话模型（两个实现的 ``message.type`` 语义一致）。"""
    if settings.LLM_PROVIDER == "cloud":
        return get_cloud_chat()
    return ChatOllama(
        model=settings.LLM_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=settings.LLM_TEMPERATURE,
        # qwen3 系列默认开启思考：0.8B 模型在长上下文下会先想 60~120s 才出正文，
        # 经常超过前端等待时间，表现为"空回答"。知识库问答以引用资料为准，关掉思考更稳更快。
        reasoning=False,
        num_predict=settings.LLM_MAX_OUTPUT_TOKENS,
    )


def filter_reasoning(parts: Iterable[str]) -> Iterator[str]:
    """保留可能的标签前缀；标签可在任意字符处被上游分片。"""
    openings = ("<think>", "<thinking>")
    closings = ("</think>", "</thinking>")
    buffer = ""
    hidden = False
    for part in parts:
        buffer += part
        while buffer:
            tags = closings if hidden else openings
            matches = [(buffer.find(tag), tag) for tag in tags if tag in buffer]
            if matches:
                offset, tag = min(matches)
                if not hidden and offset:
                    yield buffer[:offset]
                buffer = buffer[offset + len(tag):]
                hidden = not hidden
                continue
            keep = max((length for tag in tags for length in range(1, len(tag))
                        if buffer.endswith(tag[:length])), default=0)
            content = buffer[:-keep] if keep else buffer
            if not hidden and content:
                yield content
            buffer = buffer[-keep:] if keep else ""
            break
    if buffer and not hidden:
        yield buffer


def answer_stream(
    question: str, pairs: list[tuple[LCDocument, float]], history: list[dict], mode: str = "rag",
) -> Iterator[str]:
    if mode != "chat" and not pairs:
        yield "根据现有知识库无法回答该问题，请补充相关资料或调整问题。"
        return
    llm = get_llm()
    messages = build_plain_messages(question, history) if mode == "chat" else build_messages(question, pairs, history)
    parts = (chunk.content for chunk in llm.stream(messages) if isinstance(chunk.content, str))
    yield from filter_reasoning(parts)
