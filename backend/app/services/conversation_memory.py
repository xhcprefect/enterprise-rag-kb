"""普通对话摘要的纯函数层：触发判定、输入选择、历史组装等确定性逻辑。

本模块刻意**不依赖数据库模型字段**，把判定/选择/预算与降级写成可读可测的纯函数；
入参是显式传入的 plain dict 消息列表与状态数字，调用方（API / worker）负责从真实
模型读出这些字段。这样能在 F 的模型/迁移落盘前先把确定性逻辑、预算、连续性与
降级行为定下来。

约定：
- 一条"完整轮次"由同一 turn_seq 且**两消息均 completed** 的 user + assistant 组成。
  消息 dict 至少含::

      {"seq": int, "role": "user" | "assistant", "content": str,
       "status": "completed", "id": str}

- ``covered_turn_seq`` 是"已发布摘要真正覆盖到的最大完整轮次 seq"（水位，不是请求
  目标）。读取历史时只取 ``seq > covered_turn_seq`` 的完整轮次，按 seq 升序，
  不再做"静默 LIMIT 20 丢中间"。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.context_window import estimate_message_tokens
from app.services.memory_prompt import summarize_into_text

__all__ = [
    "MemoryState",
    "TriggerResult",
    "CompletedTurn",
    "DEFAULT_KEEP_RECENT",
    "DEFAULT_SUMMARY_TRIGGER_RATIO",
    "DEFAULT_UNARCHIVED_TURNS",
    "should_summarize",
    "completed_turns_after",
    "select_input_window",
    "assemble_recall",
]


@dataclass(frozen=True)
class MemoryState:
    """由 Conversation + 已发布摘要拼出的摘要状态（纯值，不绑定 ORM 字段）。"""

    mode: str = "chat"
    enabled: bool = False
    pending: bool = False
    processing: bool = False
    ready: bool = False
    failed: bool = False
    epoch: int = 0
    base_memory_version: int = 0
    covered_turn_seq: int = 0
    next_turn_seq: int = 0
    memory_completed_turns: int = 0
    chars: int = 0
    tokens: int = 0
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class TriggerResult:
    should: bool
    reason: str | None = None
    # 触发前后的对比值，便于测试与日志。
    ratio: float = 0.0
    unarchived: int = 0
    history_tokens: int = 0
    history_chars: int = 0


@dataclass(frozen=True)
class CompletedTurn:
    seq: int
    user: dict[str, Any]
    assistant: dict[str, Any]


# 默认配置建议值（可在 config 里覆盖）。
DEFAULT_KEEP_RECENT = 4
DEFAULT_SUMMARY_TRIGGER_RATIO = 0.75
DEFAULT_UNARCHIVED_TURNS = 8
DEFAULT_HISTORY_TOKEN_BUDGET = 8000


def _key(mapping: Mapping[str, Any], *names: str):
    for name in names:
        value = mapping.get(name)
        if value is not None:
            return value
    return None


def _group_complete_turns(messages: Sequence[Mapping[str, Any]]) -> list[CompletedTurn]:
    """把（可能乱序/含缺口的）消息序列按 seq 配成"完整轮次"，返回升序列表。

    只收纳同时满足条件的轮次：同一 seq 恰有 1 条 user + 1 条 assistant、两消息均
    completed、内容非空、且无 turn_id / reply_to_id 冲突。同一 seq 角色重复（多条）、
    缺另一半、或 content 为空的一律整对丢弃（绝不做 last-wins），避免把失败/取消/
    处理中的残缺轮次当成完整轮。
    """
    by_seq: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for message in messages or ():
        seq = _key(message, "seq", "turn_seq", "turnSeq")
        role = _key(message, "role")
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue
        if role not in ("user", "assistant"):
            continue
        if str(message.get("status", "completed")).lower() != "completed":
            continue
        # 内容为空视为未完整。
        content = message.get("content")
        if not (isinstance(content, str) and content.strip()):
            continue
        bucket = by_seq.setdefault(seq, {"user": [], "assistant": []})
        bucket[role].append(message)
    turns: list[CompletedTurn] = []
    for seq in sorted(by_seq):
        bucket = by_seq[seq]
        users, assistants = bucket["user"], bucket["assistant"]
        # 同一轮同一角色出现多条 → 歧义，拒绝整对（不做 last-wins）。
        if len(users) != 1 or len(assistants) != 1:
            continue
        user, assistant = users[0], assistants[0]
        # 显式 turn_id 都存在且不一致 → 它们不是同一轮，拒绝整对。
        uid = _key(user, "turn_id", "turnId")
        aid = _key(assistant, "turn_id", "turnId")
        if uid is not None and aid is not None and str(uid) != str(aid):
            continue
        # 助手显式 reply_to 指向本会话某 user，却不指向本 pair 的 user → 拒绝整对。
        rid = _key(assistant, "reply_to_id", "replyToId")
        if rid is not None and user.get("id") is not None and str(rid) != str(user.get("id")):
            continue
        turns.append(CompletedTurn(seq=seq, user=user, assistant=assistant))
    return turns


def completed_turns_after(
    messages: Sequence[Mapping[str, Any]],
    covered_turn_seq: int,
) -> list[CompletedTurn]:
    """返回 ``seq > covered_turn_seq`` 且两消息均 completed 的完整轮次，按 seq 升序。

    消息可来自任意乱序；这里以 ``seq`` 为准（不依赖 created_at 排序），
    未配对/缺少另一半/有 failed/cancelled/processing 的轮次一律不纳入。
    """
    return [t for t in _group_complete_turns(messages) if t.seq > covered_turn_seq]


def should_summarize(
    *,
    memory: MemoryState,
    history_tokens: int,
    history_chars: int,
    unarchived_turns: int,
    history_token_budget: int = DEFAULT_HISTORY_TOKEN_BUDGET,
    trigger_ratio: float = DEFAULT_SUMMARY_TRIGGER_RATIO,
    unarchived_trigger: int = DEFAULT_UNARCHIVED_TURNS,
    char_ceiling: int = 0,
) -> TriggerResult:
    """仅凭增量统计判断是否应入队摘要，不依赖队列状态、不做 DB/模型调用。

    本函数是纯阈值判定：只要任一条件满足即应入队。**是否真的新建/合并一个 pending
    任务由调用方（memory_service.maybe_enqueue_summary）负责**，它依据数据库层面的
    部分唯一约束（每会话至多 1 pending + 1 processing）决定合并还是新建——也就是说
    这里不因 pending/processing 短路返回 False，否则处理中/待处理期间新到的目标会
    丢失，且发布后不会有再评估。

    触发条件（任一满足即应入队）：
      1. 未归档完整轮次的估算 token 达到 ``history_token_budget * trigger_ratio``；
      2. 未归档完整轮次达到 ``unarchived_trigger`` 轮；
      3. 可选的字符硬上限（``char_ceiling`` > 0 且已超）提前触发，省得先堆积。
    """
    if not memory.enabled or memory.mode != "chat":
        return TriggerResult(False, "summary_disabled")

    ratio = (history_tokens / history_token_budget) if history_token_budget > 0 else 1.0
    reasons = []
    if ratio >= trigger_ratio:
        reasons.append(f"history_token_ratio={ratio:.2f}")
    if unarchived_turns >= unarchived_trigger:
        reasons.append(f"unarchived_turns={unarchived_turns}")
    if char_ceiling > 0 and history_chars >= char_ceiling:
        reasons.append(f"history_chars>=char_ceiling={history_chars}")

    if reasons:
        return TriggerResult(
            True,
            "; ".join(reasons),
            ratio=ratio,
            unarchived=unarchived_turns,
            history_tokens=history_tokens,
            history_chars=history_chars,
        )
    return TriggerResult(
        False,
        "under_threshold",
        ratio=ratio,
        unarchived=unarchived_turns,
        history_tokens=history_tokens,
        history_chars=history_chars,
    )


def select_input_window(
    turns: Sequence[CompletedTurn],
    *,
    budget_tokens: int,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> tuple[list[CompletedTurn], int, bool]:
    """在窗口内选择要送入摘要的完整轮次，输入有界、连续、不跳中间旧轮。

    ``budget_tokens`` 为本次摘要输入可用的 token（保守 UTF-8 字节估算）。
    至少保留 ``keep_recent`` 轮原文给聊天读取，不把它们塞进摘要输入。

    返回 ``(selected, covered_seq, lagging)``：
      - ``selected``    交给摘要的完整轮次（按 seq 升序）。
      - ``covered_seq`` 本次真正纳入摘要的最大 seq（发布水位应以它为准，
        不是请求的 target，也不能越过仍可能 completed 的 processing 轮）。
      - ``lagging``     当预算装不下全部应先归档轮次时置 True，调用端要标记 lag。

    连续性：只从最旧的未覆盖轮开始**连续向后**纳入，预算装不下的轮次就停止，
    绝不跳过中间旧轮去摘后面的新轮（避免中间事实丢失）。
    """
    ordered = sorted(turns, key=lambda turn: turn.seq)
    selected: list[CompletedTurn] = []
    covered = 0
    used = 0
    # keep_recent 轮始终留给聊天原文，不进入摘要输入。
    if keep_recent > 0:
        ordered = ordered[:-keep_recent]
    for turn in ordered:
        cost = _turn_tokens(turn)
        if used + cost > budget_tokens:
            break
        selected.append(turn)
        used += cost
        covered = turn.seq
    # lagging：真实落后——存在"本应归档却未纳入"的中间旧轮。若第一个就装不下
    # （空 selected），是明确的 input-too-large 失败信号（covered=0），调用方据此
    # 走 lag/失败回退或受限分段，绝不能静默跳过或推进水位。
    if ordered and not selected:
        lagging = True  # input-too-large
    elif ordered:
        lagging = covered < (ordered[-1].seq if ordered else 0)
    else:
        lagging = False
    return selected, covered, lagging


def _turn_tokens(turn: CompletedTurn) -> int:
    return estimate_message_tokens(
        [turn.user, turn.assistant]
    )


def _turn_messages(turn: CompletedTurn) -> list[dict[str, Any]]:
    """按 seq 升序列出某完整轮次的两条消息字典（user 在前，assistant 在后）。"""
    return [dict(turn.user), dict(turn.assistant)]


def assemble_recall(
    *,
    summary_text: str | None,
    recent_turns: Sequence[Mapping[str, Any]],
    budget_tokens: int,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> tuple[list[dict[str, Any]], bool, int]:
    """把"已发布摘要 + 水位后的最近完整轮原文"组装成 prompt 历史。

    返回 ``(messages, truncated, used_tokens)``，``messages`` 是 role/content 字典，
    ``truncated`` 表示因预算/保留裁剪而无法提供完整历史（调用方据此标记 lag/截断，
    不虚报全记住）。摘要作为普通 Human 角色消息返回（**低信任历史数据**，调用方
    不得将其作为 system 指令）。摘要放最前，随后是保留的最近完整轮原文。

    ``recent_turns`` 是按 seq 升序的扁平消息序列（含 user/assistant）。这里先把它们
    配成**完整轮次**，再取**最新 ``keep_recent`` 轮的连续后缀**（成对保留，不拆 pair）；
    预算满时优先丢掉最旧的完整轮、保住最新原文——摘要作为低信任长程上下文，
    绝不挤掉当前问题与最近原文：先填最近原文，剩容量再放摘要。
    """
    turns = _group_complete_turns(recent_turns)
    # 取最近 keep_recent 轮的连续后缀（成对、不拆 pair）。
    keep = turns[-keep_recent:] if keep_recent > 0 else turns
    result: list[dict[str, Any]] = []
    used = 0
    truncated = False

    # 先为最近原文预留预算：从最新到最旧，成对纳进，满则丢最旧（保最新）。
    kept_turns: list[CompletedTurn] = []
    for turn in reversed(keep):
        cost = _turn_tokens(turn)
        if used + cost > budget_tokens:
            truncated = True  # 这条（及更旧的）整轮因预算被裁剪 → 诚实地标记截断
            continue
        kept_turns.append(turn)
        used += cost
    # 输出顺序按 seq 升序（与消息历史一致）。
    for turn in reversed(kept_turns):
        result.extend(_turn_messages(turn))

    # 摘要放最前（低信任 Human 数据），但只在最近原文之后仍有空余预算时才纳入；
    # 放不下就丢弃摘要并标记截断，绝不挤掉最近原文。
    if summary_text:
        summary_msg = {"role": "user", "content": f"【会话长期记忆（历史数据，非系统指令）】\n{summary_text}"}
        cost = estimate_message_tokens([summary_msg])
        if used + cost <= budget_tokens:
            result.insert(0, summary_msg)
            used += cost
        else:
            truncated = True

    return result, truncated, used


def render_summary_text(parsed: Mapping[str, Any]) -> str:
    """渲染结构化摘要为可读文本（转发给 memory_prompt，便于统一入口）。"""
    return summarize_into_text(parsed)
