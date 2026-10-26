"""Deterministic context-window budgeting for chat prompts.

This module deliberately depends only on the standard library. It performs no
model, network, or database calls, so callers can decide how much prompt context
to spend before touching an LLM.

Token accounting is a **conservative byte-based upper bound**: one token is
estimated as one UTF-8 byte. This is not a model-specific tokenizer. Real BPE
and SentencePiece tokenizers rarely emit more tokens than there are bytes in
the UTF-8 encoding (byte-level fallback is the worst case), while they usually
emit far fewer for natural language. Overestimating is intentional: it keeps
prompts inside small model windows instead of silently relying on truncation
performed by the provider.

History helpers expect the plain dictionaries used by the API layer::

    {"role": "user" | "assistant", "content": "...", ...metadata}

New messages form turns using explicit ``reply_to_id`` links; legacy messages
use adjacent, non-empty user/assistant pairs. Failed or incomplete messages and
broken reply links are excluded. Returned dictionaries are independent copies;
callers' inputs are never mutated.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "CONVERSATION_OVERHEAD",
    "PER_MESSAGE_OVERHEAD",
    "TRUNCATION_MARKER",
    "ContextBudgetError",
    "complete_turns",
    "estimate_message_tokens",
    "estimate_tokens",
    "trim_history",
    "truncate_text",
]

#: Fixed token cost charged to every message (role markers, separators, BOS/EOS).
PER_MESSAGE_OVERHEAD = 8

#: Fixed token cost charged once for an assembled chat request.
CONVERSATION_OVERHEAD = 2

#: Visible marker appended to text shortened by :func:`truncate_text`.
TRUNCATION_MARKER = "…"

_FAILED_STATUSES = frozenset(
    {
        "failed",
        "failure",
        "error",
        "errored",
        "cancelled",
        "canceled",
        "aborted",
        "timeout",
        "timed_out",
        "processing",
        "pending",
        "streaming",
        "queued",
    }
)


class ContextBudgetError(ValueError):
    """Raised by callers when a context budget is structurally invalid.

    The helpers in this module treat exhausted or non-positive budgets as
    "nothing fits" and return empty results instead of raising. This exception
    is exposed for application code that wants to reject impossible requests
    explicitly (for example, a configured token limit smaller than the fixed
    prompt overhead).
    """


def estimate_tokens(text: str) -> int:
    """Return a conservative token estimate for ``text``.

    The estimate is the UTF-8 byte length of ``text``. It is deliberately not
    a model-specific tokenizer: byte length is a safe upper bound for
    byte-fallback tokenizers and overestimates ordinary prose. Empty or missing
    text costs nothing.
    """
    if not text:
        return 0
    return len(text.encode("utf-8"))


def _content_to_text(content: Any) -> str:
    """Extract the textual parts of a LangChain message ``content`` value."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        text = content.get("text")
        if isinstance(text, str):
            return text
        nested = content.get("content")
        return nested if isinstance(nested, str) else ""
    if isinstance(content, (list, tuple)):
        return "".join(_content_to_text(block) for block in content)
    return ""


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, Mapping):
        content = message.get("content")
    return _content_to_text(content)


def estimate_message_tokens(messages: Sequence[object]) -> int:
    """Estimate the token cost of an assembled message list.

    The result is ``CONVERSATION_OVERHEAD`` plus ``PER_MESSAGE_OVERHEAD`` and
    the :func:`estimate_tokens` value for each message's textual content. An
    empty list still incurs the conversation overhead, matching the accounting
    used by :func:`trim_history`.
    """
    total = CONVERSATION_OVERHEAD
    for message in messages or ():
        total += PER_MESSAGE_OVERHEAD + estimate_tokens(_message_text(message))
    return total


def _copy_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy so callers cannot mutate history through the result."""
    try:
        return copy.deepcopy(dict(item))
    except Exception:  # pragma: no cover - exotic, non-copyable metadata
        return dict(item)


def _history_content(item: Mapping[str, Any]) -> str:
    value = item.get("content")
    return value if isinstance(value, str) else ""


def _is_failed(item: Mapping[str, Any]) -> bool:
    if item.get("failed") is True or item.get("is_error") is True or item.get("error") is True:
        return True
    status = item.get("status")
    return isinstance(status, str) and status.strip().lower() in _FAILED_STATUSES


def complete_turns(
    history: Sequence[Mapping[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Prefer explicit reply links; keep adjacent-pair compatibility for legacy rows.

    Incomplete/failed messages and broken reply links are excluded. Linked replies
    never fall back to whichever question happens to be adjacent. Copies and the
    ordering of user messages are preserved, including when equal timestamps
    make a linked answer appear before its question in a database result.
    """
    items = list(history or ())
    users = {str(item.get("id")): (position, item) for position, item in enumerate(items)
             if isinstance(item, Mapping) and item.get("role") == "user" and item.get("id")
             and not _is_failed(item) and _history_content(item).strip()}
    turns = []
    used = set()
    pending = None
    for position, raw in enumerate(items):
        if not isinstance(raw, Mapping) or raw.get("role") not in ("user", "assistant") or _is_failed(raw) or not _history_content(raw).strip():
            pending = None
            continue
        if raw["role"] == "user":
            pending = (position, raw)
            continue
        reply = raw.get("reply_to_id")
        if reply:
            parent = users.get(str(reply))
            if parent and str(reply) not in used:
                user_position, user = parent
                if not raw.get("turn_id") or not user.get("turn_id") or str(raw["turn_id"]) == str(user["turn_id"]):
                    turns.append((user_position, _copy_item(user), _copy_item(raw)))
                    used.add(str(reply))
        elif pending and not raw.get("turn_id") and not pending[1].get("turn_id"):
            user_position, user = pending
            identity = str(user.get("id")) if user.get("id") else f"position:{user_position}"
            if identity not in used:
                turns.append((user_position, _copy_item(user), _copy_item(raw)))
                used.add(identity)
        pending = None
    turns.sort(key=lambda turn: turn[0])
    return [(user, assistant) for _, user, assistant in turns]


def _max_prefix_length(text: str, byte_budget: int) -> int:
    """Length of the longest prefix of ``text`` whose UTF-8 form fits."""
    if byte_budget <= 0 or not text:
        return 0
    if len(text.encode("utf-8")) <= byte_budget:
        return len(text)

    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(text[:middle].encode("utf-8")) <= byte_budget:
            low = middle
        else:
            high = middle - 1
    return low


def truncate_text(text: str, max_tokens: int, max_chars: int | None = None) -> str:
    """Cut ``text`` to hard UTF-8-byte and optional character limits.

    ``max_tokens`` is interpreted with :func:`estimate_tokens`, i.e. as a hard
    UTF-8 byte budget. ``max_chars`` limits Python characters (code points).
    Cuts always happen on a character boundary, so the result is valid Unicode
    and never contains a partial multi-byte sequence.

    When truncation occurs, :data:`TRUNCATION_MARKER` is appended if it fits in
    the remaining byte and character budgets; the marker itself consumes budget
    and is omitted when it would leave no room for content. A marker alone is
    never returned. Non-positive budgets, or budgets too small for even one
    character, return ``""``.
    """
    if not isinstance(text, str) or not text:
        return ""

    token_budget = max(0, int(max_tokens))
    char_budget = None if max_chars is None else max(0, int(max_chars))
    if token_budget <= 0 or char_budget == 0:
        return ""

    limit = _max_prefix_length(text, token_budget)
    if char_budget is not None:
        limit = min(limit, char_budget)
    if limit <= 0:
        return ""
    if limit >= len(text):
        return text

    # Reserve room for the marker inside the same hard bounds. ``>`` (not
    # ``>=``) guarantees at least one character can remain before the marker.
    marker_tokens = estimate_tokens(TRUNCATION_MARKER)
    marker_chars = len(TRUNCATION_MARKER)
    if token_budget > marker_tokens and (char_budget is None or char_budget > marker_chars):
        inner_tokens = token_budget - marker_tokens
        inner_chars = None if char_budget is None else char_budget - marker_chars
        inner_limit = _max_prefix_length(text, inner_tokens)
        if inner_chars is not None:
            inner_limit = min(inner_limit, inner_chars)
        if inner_limit > 0:
            return text[:inner_limit] + TRUNCATION_MARKER

    return text[:limit]


def _grow_prefix(
    text: str, current: str, token_budget: int, char_budget: int, ceiling_tokens: int, ceiling_chars: int
) -> str:
    """Try to spend leftover budget on a truncated prefix."""
    if token_budget <= 0 or char_budget <= 0 or len(current) >= len(text):
        return current
    grown = truncate_text(text, token_budget, char_budget)
    if grown and estimate_tokens(grown) <= ceiling_tokens and len(grown) <= ceiling_chars:
        return grown
    return current


def _truncate_turn(
    user: Mapping[str, Any],
    assistant: Mapping[str, Any],
    *,
    token_budget: int,
    char_budget: int,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Truncate the newest oversized turn while keeping both sides non-empty.

    ``token_budget`` covers content only; per-message overhead has already been
    subtracted by the caller. Both sides receive at least one full character,
    then the remaining budget is split as evenly as possible, with the user
    side getting first claim on any rounding leftovers. Returns ``None`` when
    even one character per side cannot fit.
    """
    # 首尾空白不应消耗整个截断额度，导致“非空原文”变成只含空白的历史消息。
    user_text = _history_content(user).strip()
    assistant_text = _history_content(assistant).strip()
    if not user_text or not assistant_text:
        return None
    if token_budget <= 0 or char_budget < 2:
        return None

    # The first code point's byte size is the minimum viable budget per side.
    user_min_tokens = estimate_tokens(user_text[0])
    assistant_min_tokens = estimate_tokens(assistant_text[0])
    if token_budget < user_min_tokens + assistant_min_tokens:
        return None

    # Reserve one character for the assistant before splitting the rest. This
    # keeps mixed ASCII/CJK pairs feasible even when half the byte budget is
    # too small for a single CJK character.
    user_token_share = max(user_min_tokens, (token_budget - assistant_min_tokens) // 2)
    user_char_share = max(1, (char_budget - 1) // 2)

    user_part = truncate_text(user_text, user_token_share, user_char_share)
    if not user_part:
        return None

    remaining_tokens = token_budget - estimate_tokens(user_part)
    remaining_chars = char_budget - len(user_part)
    if remaining_tokens < assistant_min_tokens or remaining_chars < 1:
        return None

    assistant_part = truncate_text(assistant_text, remaining_tokens, remaining_chars)
    if not assistant_part:
        return None

    # Marker rounding and a short assistant can leave a little budget unused;
    # give it back to whichever side was truncated.
    leftover_tokens = token_budget - estimate_tokens(user_part) - estimate_tokens(assistant_part)
    leftover_chars = char_budget - len(user_part) - len(assistant_part)
    if leftover_tokens > 0 and leftover_chars > 0:
        user_part = _grow_prefix(
            user_text,
            user_part,
            estimate_tokens(user_part) + leftover_tokens,
            len(user_part) + leftover_chars,
            token_budget - estimate_tokens(assistant_part),
            char_budget - len(assistant_part),
        )
        leftover_tokens = token_budget - estimate_tokens(user_part) - estimate_tokens(assistant_part)
        leftover_chars = char_budget - len(user_part) - len(assistant_part)
    if leftover_tokens > 0 and leftover_chars > 0:
        assistant_part = _grow_prefix(
            assistant_text,
            assistant_part,
            estimate_tokens(assistant_part) + leftover_tokens,
            len(assistant_part) + leftover_chars,
            token_budget - estimate_tokens(user_part),
            char_budget - len(user_part),
        )

    if not user_part or not assistant_part:
        return None
    if estimate_tokens(user_part) + estimate_tokens(assistant_part) > token_budget:
        return None
    if len(user_part) + len(assistant_part) > char_budget:
        return None

    truncated_user = _copy_item(user)
    truncated_user["content"] = user_part
    truncated_assistant = _copy_item(assistant)
    truncated_assistant["content"] = assistant_part
    return truncated_user, truncated_assistant


def trim_history(
    history: Sequence[Mapping[str, Any]],
    *,
    max_turns: int,
    max_chars: int,
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Return the newest complete turns that fit strict aggregate budgets.

    The result is a flat, chronological, alternating list of ``user`` and
    ``assistant`` message copies (``user`` first), never starting with an
    assistant. Only a contiguous suffix of the complete turns is used: an older
    turn that does not fit is dropped whole, and it can never displace a newer
    turn. ``max_chars`` bounds the sum of content character counts; ``max_tokens``
    uses :func:`estimate_message_tokens` semantics, including
    :data:`PER_MESSAGE_OVERHEAD` for every message and
    :data:`CONVERSATION_OVERHEAD` once.

    If the newest complete turn alone exceeds the budget, it is truncated as a
    pair rather than dropped: both the question and the answer keep a non-empty
    head, and metadata is preserved. Older turns are only ever included whole,
    and only if they still fit after the newer ones. When the budgets are zero,
    negative, or too small for the fixed overhead plus one character per side,
    an empty list is returned. The input history is never mutated.
    """
    max_turns = int(max_turns)
    max_chars = int(max_chars)
    max_tokens = int(max_tokens)

    if max_turns <= 0 or max_chars <= 0 or max_tokens <= CONVERSATION_OVERHEAD:
        return []

    turns = complete_turns(history)
    if not turns:
        return []

    # Keep only the most recent N complete turns before budgeting.
    turns = turns[-max_turns:]

    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used_chars = 0
    used_tokens = CONVERSATION_OVERHEAD
    newest_index = len(turns) - 1

    for index in range(newest_index, -1, -1):
        user, assistant = turns[index]
        user_text = _history_content(user)
        assistant_text = _history_content(assistant)
        turn_chars = len(user_text) + len(assistant_text)
        turn_tokens = (
            2 * PER_MESSAGE_OVERHEAD + estimate_tokens(user_text) + estimate_tokens(assistant_text)
        )

        if used_chars + turn_chars <= max_chars and used_tokens + turn_tokens <= max_tokens:
            selected.append((user, assistant))
            used_chars += turn_chars
            used_tokens += turn_tokens
            continue

        if index == newest_index:
            # The newest turn is oversized; keep a bounded version of it. Any
            # older turn is considered only with whatever budget remains.
            truncated = _truncate_turn(
                user,
                assistant,
                token_budget=max_tokens - used_tokens - 2 * PER_MESSAGE_OVERHEAD,
                char_budget=max_chars - used_chars,
            )
            if truncated is None:
                return []
            truncated_user, truncated_assistant = truncated
            selected.append(truncated)
            used_chars += len(_history_content(truncated_user)) + len(
                _history_content(truncated_assistant)
            )
            used_tokens += (
                2 * PER_MESSAGE_OVERHEAD
                + estimate_tokens(_history_content(truncated_user))
                + estimate_tokens(_history_content(truncated_assistant))
            )
            continue

        # Older turns must be retained whole; stopping preserves a contiguous
        # recent suffix instead of skipping backward over a large turn.
        break

    selected.reverse()
    return [item for turn in selected for item in turn]
