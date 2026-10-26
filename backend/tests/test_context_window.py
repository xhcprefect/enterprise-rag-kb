"""Focused tests for ``app.services.context_window``.

The module under test is intentionally pure: these tests do not start the app,
touch the database, or call a model/network. They run with plain unittest::

    python -m unittest discover -s tests -p test_context_window.py -v
"""

from __future__ import annotations

import copy
import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.services.context_window import (
    CONVERSATION_OVERHEAD,
    PER_MESSAGE_OVERHEAD,
    TRUNCATION_MARKER,
    ContextBudgetError,
    complete_turns,
    estimate_message_tokens,
    estimate_tokens,
    trim_history,
    truncate_text,
)


def message(role: str, content: str, **extra):
    return {"role": role, "content": content, **extra}


def complete_history(*turn_ids: int):
    """Build alternating history; ids make retained turns easy to assert."""
    history = []
    for turn_id in turn_ids:
        history.append(
            message("user", f"question-{turn_id}", id=f"u{turn_id}", turn=turn_id)
        )
        history.append(
            message("assistant", f"answer-{turn_id}", id=f"a{turn_id}", turn=turn_id)
        )
    return history


class EstimateTokensTests(unittest.TestCase):
    def test_utf8_byte_estimates_for_ascii_chinese_and_emoji(self):
        cases = [
            ("", 0),
            ("hello", 5),
            ("你好", 6),
            ("世界", 6),
            ("😀", 4),
            ("a你😀", 8),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(estimate_tokens(text), expected)
                self.assertEqual(estimate_tokens(text), len(text.encode("utf-8")))

    def test_message_tokens_include_fixed_overheads(self):
        messages = [HumanMessage(content="hello"), AIMessage(content="你好")]
        expected = (
            CONVERSATION_OVERHEAD
            + PER_MESSAGE_OVERHEAD
            + 5
            + PER_MESSAGE_OVERHEAD
            + 6
        )
        self.assertEqual(estimate_message_tokens(messages), expected)

    def test_empty_message_list_still_has_conversation_overhead(self):
        self.assertEqual(estimate_message_tokens([]), CONVERSATION_OVERHEAD)

    def test_content_blocks_count_only_their_text(self):
        message = AIMessage(
            content=[
                {"type": "text", "text": "你好"},
                {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
                "ok",
            ]
        )
        self.assertEqual(
            estimate_message_tokens([message]),
            CONVERSATION_OVERHEAD + PER_MESSAGE_OVERHEAD + 6 + 2,
        )

    def test_system_and_empty_messages_use_the_same_overhead(self):
        messages = [SystemMessage(content=""), HumanMessage(content="")]
        self.assertEqual(
            estimate_message_tokens(messages),
            CONVERSATION_OVERHEAD + 2 * PER_MESSAGE_OVERHEAD,
        )


class TruncateTextTests(unittest.TestCase):
    def test_no_budget_returns_empty(self):
        for budget in (0, -1, -999):
            with self.subTest(budget=budget):
                self.assertEqual(truncate_text("abcdef", budget), "")
        self.assertEqual(truncate_text("", 100), "")
        self.assertEqual(truncate_text("abcdef", 100, 0), "")
        self.assertEqual(truncate_text("abcdef", 100, -3), "")

    def test_short_text_is_returned_unchanged(self):
        for text in ("abc", "你好", "😀"):
            with self.subTest(text=text):
                self.assertEqual(truncate_text(text, 100), text)

    def test_ascii_byte_bound_and_marker(self):
        self.assertEqual(truncate_text("abcdefghij", 6), "abc" + TRUNCATION_MARKER)
        self.assertEqual(truncate_text("abcdefghij", 4), "a" + TRUNCATION_MARKER)
        # Three bytes cannot hold both content and the three-byte marker.
        self.assertEqual(truncate_text("abcdefghij", 3), "abc")
        self.assertEqual(truncate_text("abcdefghij", 2), "ab")
        for budget in range(0, 12):
            result = truncate_text("abcdefghij", budget)
            with self.subTest(budget=budget):
                self.assertLessEqual(estimate_tokens(result), budget)
                self.assertNotEqual(result, TRUNCATION_MARKER)

    def test_optional_character_bound_is_hard(self):
        self.assertEqual(truncate_text("abcdefgh", 100, 4), "abc" + TRUNCATION_MARKER)
        self.assertEqual(truncate_text("abcdefgh", 100, 5), "abcd" + TRUNCATION_MARKER)
        # A one-character budget leaves no room for both content and marker.
        self.assertEqual(truncate_text("abcdefgh", 100, 1), "a")
        for limit in range(1, 9):
            result = truncate_text("abcdefgh", 100, limit)
            with self.subTest(limit=limit):
                self.assertLessEqual(len(result), limit)

    def test_multibyte_characters_are_never_split(self):
        text = "你好世界😀"
        for budget in range(1, 22):
            result = truncate_text(text, budget)
            with self.subTest(budget=budget):
                self.assertLessEqual(estimate_tokens(result), budget)
                result.encode("utf-8")  # must be valid, complete UTF-8
                body = result.removesuffix(TRUNCATION_MARKER)
                self.assertTrue(text.startswith(body))
                self.assertNotIn("\ufffd", result)

        self.assertEqual(truncate_text("你好世界", 9), "你好" + TRUNCATION_MARKER)
        self.assertEqual(truncate_text("你好世界", 8), "你" + TRUNCATION_MARKER)
        self.assertEqual(truncate_text("😀😀😀", 7), "😀" + TRUNCATION_MARKER)
        self.assertEqual(truncate_text("😀😀😀", 4), "😀")

    def test_combined_byte_and_character_bounds(self):
        result = truncate_text("你好世界", 9, 2)
        self.assertEqual(result, "你" + TRUNCATION_MARKER)
        self.assertLessEqual(estimate_tokens(result), 9)
        self.assertLessEqual(len(result), 2)

    def test_marker_is_never_returned_alone(self):
        for text in ("abcdef", "你好世界", "😀😀"):
            for budget in (1, 2, 3):
                result = truncate_text(text, budget)
                with self.subTest(text=text, budget=budget):
                    self.assertNotEqual(result, TRUNCATION_MARKER)
                    self.assertTrue(result == "" or result.startswith(text[:1]))


class CompleteTurnsTests(unittest.TestCase):
    def test_only_adjacent_complete_turns_are_returned(self):
        history = [
            message("assistant", "orphan-before", id="orphan-0"),
            message("user", "q1", id="u1"),
            message("assistant", "a1", id="a1"),
            message("user", "q2", id="u2"),  # never answered
            message("user", "q3", id="u3"),
            message("assistant", "a3", id="a3"),
            message("assistant", "orphan-after", id="orphan-3"),
        ]
        pairs = complete_turns(history)
        self.assertEqual(
            [(user["id"], answer["id"]) for user, answer in pairs],
            [("u1", "a1"), ("u3", "a3")],
        )

    def test_explicit_reply_links_survive_interleaving_and_never_use_adjacent_question(self):
        items = [message("user", "first", id="u1", turn_id="t1"),
                 message("user", "second", id="u2", turn_id="t2"),
                 message("assistant", "answer first", id="a1", turn_id="t1", reply_to_id="u1"),
                 message("assistant", "answer second", id="a2", turn_id="t2", reply_to_id="u2")]
        pairs = complete_turns(items)
        self.assertEqual([(user["id"], answer["id"]) for user, answer in pairs], [("u1", "a1"), ("u2", "a2")])
        self.assertEqual(complete_turns([items[1], items[2]]), [])
        self.assertEqual(complete_turns([{**items[0], "status": "processing"}, items[2]]), [])
        self.assertEqual(complete_turns([items[0], {**items[2], "turn_id": "wrong"}]), [])

    def test_equal_timestamp_order_does_not_break_explicit_reply_links(self):
        user = message("user", "question", id="u", turn_id="t")
        answer = message("assistant", "answer", id="a", turn_id="t", reply_to_id="u")
        self.assertEqual(complete_turns([answer, user]), [(user, answer)])

    def test_unknown_roles_are_hard_boundaries(self):
        cases = [
            (
                [message("user", "q"), message("system", "s"), message("assistant", "a")],
                0,
            ),
            (
                [
                    message("user", "q1"),
                    message("assistant", "a1"),
                    message("system", "s"),
                    message("assistant", "orphan"),
                ],
                1,
            ),
            (
                [
                    message("user", "q1"),
                    message("tool", "t"),
                    message("assistant", "a1"),
                    message("user", "q2"),
                    message("assistant", "a2"),
                ],
                1,
            ),
        ]
        for history, expected in cases:
            with self.subTest(history=history):
                self.assertEqual(len(complete_turns(history)), expected)

    def test_empty_whitespace_and_failed_messages_never_form_turns(self):
        cases = [
            [message("user", "   "), message("assistant", "a")],
            [message("user", "q"), message("assistant", " \n\t")],
            [message("user", "q"), message("assistant", "partial", failed=True)],
            [message("user", "q", status="failed"), message("assistant", "a")],
            [message("user", "q"), message("assistant", "a", is_error=True)],
            [message("user", "q"), message("assistant", "a", status="timeout")],
        ]
        for history in cases:
            with self.subTest(history=history):
                self.assertEqual(complete_turns(history), [])

    def test_duplicate_users_and_assistants_do_not_cross_pair(self):
        pairs = complete_turns(
            [
                message("user", "first"),
                message("user", "second", id="u2"),
                message("assistant", "answer", id="a2"),
                message("assistant", "extra"),
            ]
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0]["content"], "second")
        self.assertEqual(pairs[0][1]["content"], "answer")

    def test_metadata_is_preserved_via_deep_copies_and_input_is_untouched(self):
        history = [
            message(
                "user",
                "q",
                id="u1",
                citations=[{"source_index": 1, "filename": "doc.txt"}],
            ),
            message("assistant", "a", id="a1"),
        ]
        snapshot = copy.deepcopy(history)
        pairs = complete_turns(history)

        self.assertEqual(pairs[0][0]["id"], "u1")
        self.assertEqual(pairs[0][0]["citations"], [{"source_index": 1, "filename": "doc.txt"}])
        self.assertIsNot(pairs[0][0], history[0])
        self.assertIsNot(pairs[0][0]["citations"], history[0]["citations"])

        pairs[0][0]["content"] = "changed"
        pairs[0][0]["citations"][0]["source_index"] = 99
        self.assertEqual(history, snapshot)

    def test_non_dict_entries_and_missing_roles_are_boundaries(self):
        self.assertEqual(complete_turns([None, "junk", {"content": "q"}]), [])
        self.assertEqual(
            complete_turns(
                [
                    message("user", "q"),
                    "junk",
                    message("assistant", "a"),
                    message("user", "q2"),
                    message("assistant", "a2"),
                ]
            ),
            [
                (
                    {"role": "user", "content": "q2"},
                    {"role": "assistant", "content": "a2"},
                )
            ],
        )


class TrimHistoryTests(unittest.TestCase):
    def test_empty_history(self):
        self.assertEqual(trim_history([], max_turns=10, max_chars=1000, max_tokens=1000), [])

    def test_max_turns_zero_and_non_positive_budgets_return_empty(self):
        history = complete_history(1, 2)
        for max_turns in (0, -1, -50):
            with self.subTest(max_turns=max_turns):
                self.assertEqual(
                    trim_history(history, max_turns=max_turns, max_chars=1000, max_tokens=1000),
                    [],
                )
        for max_chars in (0, -1):
            with self.subTest(max_chars=max_chars):
                self.assertEqual(
                    trim_history(history, max_turns=10, max_chars=max_chars, max_tokens=1000),
                    [],
                )
        for max_tokens in (0, -1, CONVERSATION_OVERHEAD):
            with self.subTest(max_tokens=max_tokens):
                self.assertEqual(
                    trim_history(history, max_turns=10, max_chars=1000, max_tokens=max_tokens),
                    [],
                )

    def test_recent_turns_are_retained_and_chronological(self):
        history = complete_history(1, 2, 3)
        # Each turn is 20 content characters, so 40 fits exactly two turns.
        trimmed = trim_history(history, max_turns=10, max_chars=40, max_tokens=1000)
        self.assertEqual([item["id"] for item in trimmed], ["u2", "a2", "u3", "a3"])
        self.assertEqual([item["role"] for item in trimmed], ["user", "assistant", "user", "assistant"])

    def test_max_turns_limits_the_complete_turns_considered(self):
        history = complete_history(1, 2, 3)
        trimmed = trim_history(history, max_turns=2, max_chars=1000, max_tokens=1000)
        self.assertEqual([item["id"] for item in trimmed], ["u2", "a2", "u3", "a3"])

    def test_trailing_unanswered_user_is_excluded(self):
        history = complete_history(1) + [message("user", "never answered", id="loose")]
        trimmed = trim_history(history, max_turns=10, max_chars=1000, max_tokens=1000)
        self.assertEqual([item["id"] for item in trimmed], ["u1", "a1"])

    def test_starts_with_user_even_when_history_starts_with_orphan(self):
        history = [message("assistant", "orphan", id="orphan")] + complete_history(1, 2)
        trimmed = trim_history(history, max_turns=10, max_chars=1000, max_tokens=1000)
        self.assertEqual([item["id"] for item in trimmed], ["u1", "a1", "u2", "a2"])
        self.assertEqual(trimmed[0]["role"], "user")

    def test_unknown_roles_do_not_create_cross_role_turns(self):
        history = [
            message("user", "q1", id="u1"),
            message("system", "instruction"),
            message("assistant", "a1", id="a1"),
            message("user", "q2", id="u2"),
            message("assistant", "a2", id="a2"),
        ]
        trimmed = trim_history(history, max_turns=10, max_chars=1000, max_tokens=1000)
        self.assertEqual([item["id"] for item in trimmed], ["u2", "a2"])

    def test_older_turns_are_kept_whole_and_never_displace_newer_turns(self):
        history = [
            message("user", "o" * 20, id="u-old"),
            message("assistant", "p" * 20, id="a-old"),
            message("user", "n" * 10, id="u-new"),
            message("assistant", "m" * 10, id="a-new"),
        ]
        # Newest turn is 20 chars and fits; adding the older 40-char turn would
        # exceed 30, but the older turn must not be truncated in its place.
        trimmed = trim_history(history, max_turns=10, max_chars=30, max_tokens=1000)
        self.assertEqual([item["id"] for item in trimmed], ["u-new", "a-new"])
        self.assertEqual(trimmed[0]["content"], "n" * 10)
        self.assertEqual(trimmed[1]["content"], "m" * 10)

    def test_oversized_newest_turn_is_truncated_as_a_pair(self):
        user = message(
            "user",
            "请解释这个非常长的问题：" * 20,
            id="u-long",
            citations=[{"source_index": 1}],
        )
        assistant = message("assistant", "这是一个很长的回答。" * 300, id="a-long")
        history = [user, assistant]
        snapshot = copy.deepcopy(history)

        trimmed = trim_history(history, max_turns=10, max_chars=100, max_tokens=80)

        self.assertEqual(len(trimmed), 2)
        self.assertEqual([item["role"] for item in trimmed], ["user", "assistant"])
        self.assertTrue(trimmed[0]["content"])
        self.assertTrue(trimmed[1]["content"])
        self.assertTrue(trimmed[0]["content"].startswith("请解释"))
        self.assertTrue(trimmed[1]["content"].startswith("这是一个很长的回答"))
        self.assertTrue(trimmed[1]["content"].endswith(TRUNCATION_MARKER))
        self.assertLessEqual(sum(len(item["content"]) for item in trimmed), 100)
        self.assertLessEqual(
            estimate_message_tokens(
                [HumanMessage(content=trimmed[0]["content"]), AIMessage(content=trimmed[1]["content"])]
            ),
            80,
        )
        # Metadata survives truncation and the input remains untouched.
        self.assertEqual(trimmed[0]["id"], "u-long")
        self.assertEqual(trimmed[0]["citations"], [{"source_index": 1}])
        self.assertEqual(trimmed[1]["id"], "a-long")
        self.assertEqual(history, snapshot)

    def test_oversized_newest_pair_does_not_fall_back_to_older_turns(self):
        history = [
            message("user", "old-question", id="u-old"),
            message("assistant", "old-answer", id="a-old"),
            message("user", "新的问题" * 80, id="u-new"),
            message("assistant", "新的回答" * 300, id="a-new"),
        ]
        trimmed = trim_history(history, max_turns=10, max_chars=90, max_tokens=90)
        self.assertEqual([item["role"] for item in trimmed], ["user", "assistant"])
        self.assertTrue(trimmed[0]["content"].startswith("新的问题"))
        self.assertNotIn("old-question", trimmed[0]["content"])
        self.assertEqual(trimmed[0]["id"], "u-new")
        self.assertEqual(trimmed[1]["id"], "a-new")

    def test_truncated_pairs_keep_content_even_with_long_leading_whitespace(self):
        original = [message("user", " " * 1000 + "有效问题"),
                    message("assistant", "\n" * 1000 + "有效回答")]
        trimmed = trim_history(original, max_turns=1, max_chars=20, max_tokens=50)
        self.assertEqual(len(trimmed), 2)
        self.assertTrue(all(item["content"].strip() for item in trimmed))
        self.assertLessEqual(estimate_message_tokens(trimmed), 50)
        self.assertTrue(original[0]["content"].startswith(" " * 1000))

    def test_metadata_and_immutability_for_retained_turns(self):
        history = []
        for index in (1, 2, 3):
            history.append(
                message(
                    "user",
                    f"question-{index}",
                    id=f"u{index}",
                    citations=[{"source_index": index}],
                )
            )
            history.append(message("assistant", f"answer-{index}", id=f"a{index}"))
        snapshot = copy.deepcopy(history)

        trimmed = trim_history(history, max_turns=2, max_chars=1000, max_tokens=1000)
        self.assertEqual([item["id"] for item in trimmed], ["u2", "a2", "u3", "a3"])
        self.assertEqual(trimmed[0]["citations"], [{"source_index": 2}])
        self.assertIsNot(trimmed[0], history[2])
        self.assertIsNot(trimmed[0]["citations"], history[2]["citations"])

        trimmed[0]["content"] = "changed"
        trimmed[0]["citations"][0]["source_index"] = 99
        self.assertEqual(history, snapshot)

    def test_tiny_budgets(self):
        history = [
            message("user", "a" * 50, id="u"),
            message("assistant", "b" * 50, id="a"),
        ]
        # 19 tokens cannot cover conversation + two message overheads + two bytes.
        self.assertEqual(
            trim_history(history, max_turns=10, max_chars=1000, max_tokens=19),
            [],
        )
        # One character per side is the smallest pairable budget (18 + 2).
        trimmed = trim_history(history, max_turns=10, max_chars=1000, max_tokens=20)
        self.assertEqual([item["role"] for item in trimmed], ["user", "assistant"])
        self.assertEqual([item["content"] for item in trimmed], ["a", "b"])
        self.assertEqual(
            estimate_message_tokens(
                [HumanMessage(content=trimmed[0]["content"]), AIMessage(content=trimmed[1]["content"])]
            ),
            20,
        )
        # Character budget too small for two non-empty sides.
        self.assertEqual(
            trim_history(history, max_turns=10, max_chars=1, max_tokens=1000),
            [],
        )

    def test_chinese_tiny_budget_needs_three_bytes_per_side(self):
        history = [
            message("user", "问" * 20),
            message("assistant", "答" * 20),
        ]
        # 23 tokens leaves 5 content bytes, less than two 3-byte characters.
        self.assertEqual(
            trim_history(history, max_turns=10, max_chars=1000, max_tokens=23),
            [],
        )
        trimmed = trim_history(history, max_turns=10, max_chars=1000, max_tokens=24)
        self.assertEqual([item["content"] for item in trimmed], ["问", "答"])
        self.assertEqual(
            estimate_message_tokens(
                [HumanMessage(content=trimmed[0]["content"]), AIMessage(content=trimmed[1]["content"])]
            ),
            24,
        )

    def test_strict_bounds_and_shape_across_budget_grid(self):
        history = [
            message("user", "第一问：系统如何部署？" * 6, id="u1"),
            message("assistant", "部署步骤如下。" * 12, id="a1"),
            message("user", "Second question about configuration. " * 5, id="u2"),
            message("assistant", "Configuration details follow. " * 8, id="a2"),
            message("user", "Emoji question 😀😀😀", id="u3"),
            message("assistant", "Emoji answer 😀😀😀", id="a3"),
            message("user", "第四问：还有补充吗？" * 3, id="u4"),
            message("assistant", "还有几点补充。" * 10, id="a4"),
        ]
        token_budgets = [0, 1, 2, 17, 18, 19, 20, 21, 30, 47, 60, 90, 130, 200]
        char_budgets = [0, 1, 2, 3, 10, 25, 60, 120, 1000]
        turn_limits = [0, 1, 2, 10]

        for max_tokens in token_budgets:
            for max_chars in char_budgets:
                for max_turns in turn_limits:
                    with self.subTest(
                        max_tokens=max_tokens, max_chars=max_chars, max_turns=max_turns
                    ):
                        trimmed = trim_history(
                            history,
                            max_turns=max_turns,
                            max_chars=max_chars,
                            max_tokens=max_tokens,
                        )
                        if max_turns == 0:
                            self.assertEqual(trimmed, [])
                            continue
                        if trimmed:
                            self.assertEqual(len(trimmed) % 2, 0)
                            self.assertEqual(
                                [item["role"] for item in trimmed],
                                ["user", "assistant"] * (len(trimmed) // 2),
                            )
                            self.assertTrue(all(isinstance(item["content"], str) for item in trimmed))
                            self.assertTrue(all(item["content"] for item in trimmed))
                            self.assertLessEqual(
                                sum(len(item["content"]) for item in trimmed), max_chars
                            )
                            messages = [
                                HumanMessage(content=item["content"])
                                if item["role"] == "user"
                                else AIMessage(content=item["content"])
                                for item in trimmed
                            ]
                            self.assertLessEqual(estimate_message_tokens(messages), max_tokens)
                        else:
                            # Empty is only acceptable when the fixed overhead or
                            # the character budget cannot fit one character per side.
                            self.assertTrue(max_chars <= 1 or max_tokens <= 23)

    def test_retained_turns_are_a_contiguous_recent_suffix(self):
        history = complete_history(1, 2, 3, 4)
        trimmed = trim_history(history, max_turns=3, max_chars=40, max_tokens=1000)
        self.assertEqual(
            [item["turn"] for item in trimmed],
            [3, 3, 4, 4],  # newest two turns, in chronological order
        )

    def test_context_budget_error_is_a_value_error(self):
        self.assertTrue(issubclass(ContextBudgetError, ValueError))
        with self.assertRaises(ValueError):
            raise ContextBudgetError("budget too small")


if __name__ == "__main__":
    unittest.main()
