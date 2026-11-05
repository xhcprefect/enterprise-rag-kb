"""纯函数单测：摘要触发阈值/预算/完整轮次/连续水位/历史组装。"""
import unittest

from app.services.conversation_memory import (
    DEFAULT_KEEP_RECENT,
    CompletedTurn,
    MemoryState,
    assemble_recall,
    completed_turns_after,
    select_input_window,
    should_summarize,
)


def _msg(seq, role, content, status="completed"):
    return {"seq": seq, "role": role, "content": content, "status": status, "id": f"{role}-{seq}"}


def _turn(seq, user_text="q", assistant_text="a"):
    return CompletedTurn(seq=seq, user=_msg(seq, "user", user_text), assistant=_msg(seq, "assistant", assistant_text))


class ShouldSummarizeTest(unittest.TestCase):
    def _memory(self, **kw):
        base = dict(mode="chat", enabled=True, pending=False, processing=False)
        base.update(kw)
        return MemoryState(**base)

    def test_disabled_or_non_chat_never_triggers(self):
        disabled = self._memory(enabled=False)
        self.assertFalse(should_summarize(memory=disabled, history_tokens=99999, history_chars=99999, unarchived_turns=999).should)
        rag = self._memory(mode="rag")
        self.assertFalse(should_summarize(memory=rag, history_tokens=99999, history_chars=99999, unarchived_turns=999).should)

    def test_queue_state_does_not_suppress_threshold(self):
        # should_summarize 是纯阈值判定：pending/processing 由调用方按"每会话至多
        # 1 pending + 1 processing"的部分唯一约束去合并/新建，不应在此短路丢目标。
        for kw in (dict(pending=True), dict(processing=True), dict(pending=True, processing=True)):
            with self.subTest(kw=kw):
                result = should_summarize(memory=self._memory(**kw), history_tokens=99999, history_chars=99999, unarchived_turns=999)
                self.assertTrue(result.should)
                self.assertNotEqual(result.reason, "already_queued")

    def test_token_ratio_triggers(self):
        result = should_summarize(memory=self._memory(), history_tokens=6000, history_chars=0, unarchived_turns=0, history_token_budget=8000, trigger_ratio=0.75)
        self.assertTrue(result.should)
        self.assertAlmostEqual(result.ratio, 0.75)

    def test_unarchived_turn_count_triggers(self):
        result = should_summarize(memory=self._memory(), history_tokens=0, history_chars=0, unarchived_turns=8, unarchived_trigger=8)
        self.assertTrue(result.should)
        self.assertIn("unarchived_turns=8", result.reason)

    def test_char_ceiling_triggers_before_discard(self):
        result = should_summarize(memory=self._memory(), history_tokens=1000, history_chars=8000, unarchived_turns=2, char_ceiling=8000)
        self.assertTrue(result.should)
        self.assertIn("history_chars>=char_ceiling", result.reason)

    def test_below_all_thresholds(self):
        result = should_summarize(memory=self._memory(), history_tokens=100, history_chars=100, unarchived_turns=2)
        self.assertFalse(result.should)


class CompletedTurnsAfterTest(unittest.TestCase):
    def test_returns_only_completed_full_turns_after_watermark(self):
        messages = [
            _msg(1, "user", "q1"), _msg(1, "assistant", "a1"),
            _msg(2, "user", "q2"), _msg(2, "assistant", "a2"),
            _msg(3, "user", "q3"), _msg(3, "assistant", "a3", status="failed"),
            _msg(4, "user", "q4"),  # 未配对
        ]
        turns = completed_turns_after(messages, covered_turn_seq=1)
        self.assertEqual([t.seq for t in turns], [2])

    def test_sorts_by_seq_ignoring_input_order(self):
        messages = [
            _msg(2, "assistant", "a2"), _msg(1, "user", "q1"),
            _msg(2, "user", "q2"), _msg(1, "assistant", "a1"),
        ]
        turns = completed_turns_after(messages, covered_turn_seq=0)
        self.assertEqual([t.seq for t in turns], [1, 2])

    def test_empty_content_excluded(self):
        messages = [_msg(1, "user", "  "), _msg(1, "assistant", "a1"), _msg(2, "user", "q2"), _msg(2, "assistant", "")][::-1]
        # 反向插入，验证乱序且空内容被排除。
        turns = completed_turns_after(messages, covered_turn_seq=0)
        self.assertEqual(turns, [])


class SelectInputWindowTest(unittest.TestCase):
    def test_keeps_recent_and_selects_contiguous_prefix(self):
        turns = [_turn(seq) for seq in range(1, 7)]  # 6 轮，保留最近 4 轮 → 可归档 1..2
        big_budget = 10_000_000
        selected, covered, lagging = select_input_window(turns, budget_tokens=big_budget, keep_recent=4)
        self.assertEqual([t.seq for t in selected], [1, 2])
        self.assertEqual(covered, 2)
        self.assertFalse(lagging)

    def test_budget_stops_at_first_unfittable_preserving_continuity(self):
        turns = [_turn(1, "q1", "a1"), _turn(2, user_text="x" * 1000, assistant_text="y" * 1000), _turn(3, "q3", "a3")]
        small_budget = 300  # 只能装下 seq=1
        selected, covered, _ = select_input_window(turns, budget_tokens=small_budget, keep_recent=0)
        self.assertEqual([t.seq for t in selected], [1])
        self.assertEqual(covered, 1)
        # 绝不跳过中间旧轮去取更新的轮。
        self.assertNotIn(3, [t.seq for t in selected])

    def test_watermark_is_max_included_not_target(self):
        # 只有本轮真正纳入的范围是水位，不能越过 processing 等尚未 completed 的轮。
        turns = [_turn(1), _turn(2), _turn(3)]
        selected, covered, _ = select_input_window(turns, budget_tokens=10_000, keep_recent=2)
        self.assertEqual(covered, 1)
        self.assertNotIn(3, [t.seq for t in selected])


class AssembleRecallTest(unittest.TestCase):
    def test_summary_then_recent_original(self):
        recent = [_msg(1, "user", "q1"), _msg(1, "assistant", "a1"), _msg(2, "user", "q2"), _msg(2, "assistant", "a2"), _msg(3, "user", "q3"), _msg(3, "assistant", "a3")]
        summary = "【会话长期记忆】事实X"
        messages, truncated, used = assemble_recall(summary_text=summary, recent_turns=recent, budget_tokens=1_000_000, keep_recent=2)
        self.assertFalse(truncated)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles[0], "user")  # 摘要作为 Human 数据，不是 system
        self.assertIn("会话长期记忆", messages[0]["content"])
        # keep_recent=2 完整轮 → 保留 seq2+seq3 两轮（成对，不拆 pair），不含更旧 seq1。
        seqs = [m.get("seq") for m in messages if "seq" in m]
        self.assertIn(3, seqs)
        self.assertIn(2, seqs)
        self.assertNotIn(1, seqs)
        # 成对保留：seq3 的 user 与 assistant 都在。
        self.assertEqual(seqs.count(3), 2)

    def test_summary_too_large_is_dropped_and_truncated(self):
        # 摘要过大放不下时，优先级是保住最近原文：丢弃超长摘要、保留那轮原文。
        recent = [_msg(1, "user", "q1"), _msg(1, "assistant", "a1")]
        messages, truncated, _ = assemble_recall(summary_text="很长的摘要" * 10000, recent_turns=recent, budget_tokens=100, keep_recent=1)
        self.assertTrue(truncated)
        self.assertTrue(messages)
        self.assertFalse(any("会话长期记忆" in m.get("content", "") for m in messages))

    def test_tight_budget_truncates_recent_and_marks_lag(self):
        big_summary = "摘要" * 30
        # keep_recent=1：只保留 seq1（seq2 太旧会先被裁剪掉），故只给一轮原文。
        recent = [_msg(1, "user", "q1"), _msg(1, "assistant", "很长的回答" * 800)]
        # 预算能装下摘要（约 150 字节），但装不下那轮很长的原始回答 → truncated=True 且保留摘要。
        messages, truncated, used = assemble_recall(summary_text=big_summary, recent_turns=recent, budget_tokens=2000, keep_recent=1)
        self.assertTrue(truncated)
        self.assertTrue(messages)
        self.assertIn("会话长期记忆", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
