"""纯函数单测：固定结构化摘要 schema / 提示词 / 来源 / 预算 / 纠错 / 凭据。"""
import json
import unittest

from app.services.memory_prompt import (
    MAX_ITEMS_PER_SECTION,
    MAX_SOURCES_PER_ITEM,
    MAX_TOTAL_TOKENS,
    SUMMARY_VERSION,
    SummaryFormatError,
    SummaryValidationError,
    build_summary_human,
    build_summary_prompt,
    parse_summary,
    redact_credentials,
    redact_summary,
    summarize_into_text,
    validate_sources,
)


def _item(text, sources, verification="confirmed_by_user"):
    return {"text": text, "source_message_ids": sources, "verification": verification}


def _ok(**overrides):
    data = {
        "version": SUMMARY_VERSION,
        "unfinished": False,
        "goals": [_item("目标是上线摘要", ["m1"])],
        "constraints": [],
        "confirmed_facts": [_item("预算 8000 元", ["m2"])],
        "decisions": [],
        "preferences": [_item("用中文", ["m1"], "assistant_suggested")],
        "corrections": [],
        "open_questions": [],
    }
    data.update(overrides)
    return data


def _dump(data):
    return json.dumps(data, ensure_ascii=False)


class ParseBasicsTest(unittest.TestCase):
    def test_parses_valid_and_normalizes(self):
        parsed = parse_summary(_dump(_ok()))
        self.assertEqual(parsed["version"], SUMMARY_VERSION)
        self.assertFalse(parsed["unfinished"])
        self.assertEqual(parsed["goals"][0]["verification"], "confirmed_by_user")

    def test_tolerates_json_code_fence(self):
        wrapped = "```json\n" + _dump(_ok()) + "\n```"
        self.assertEqual(parse_summary(wrapped)["goals"][0]["text"], "目标是上线摘要")

    def test_rejects_non_json(self):
        with self.assertRaises(SummaryFormatError):
            parse_summary("这不是 JSON")

    def test_rejects_non_object(self):
        with self.assertRaises(SummaryFormatError):
            parse_summary(_dump([1, 2, 3]))

    def test_rejects_wrong_version(self):
        with self.assertRaises(SummaryFormatError):
            parse_summary(_dump(_ok(version=999)))

    def test_rejects_unfinished_non_bool(self):
        with self.assertRaises(SummaryFormatError):
            parse_summary(_dump(_ok(unfinished="yes")))


class SectionValidationTest(unittest.TestCase):
    def test_rejects_non_list_section(self):
        for bad in ("goals", "constraints", "confirmed_facts", "decisions", "preferences", "corrections", "open_questions"):
            with self.subTest(bad=bad):
                with self.assertRaises(SummaryFormatError):
                    parse_summary(_dump(_ok(**{bad: "not-a-list"})))

    def test_rejects_empty_text(self):
        with self.assertRaises(SummaryFormatError):
            parse_summary(_dump(_ok(goals=[_item("   ", ["m1"])])))

    def test_rejects_bad_verification(self):
        with self.assertRaises(SummaryFormatError):
            parse_summary(_dump(_ok(goals=[_item("t", ["m1"], "maybe")])))

    def test_rejects_empty_sources(self):
        with self.assertRaises(SummaryFormatError):
            parse_summary(_dump(_ok(goals=[_item("t", [])])))

    def test_rejects_too_many_sources(self):
        many = ["m%d" % i for i in range(MAX_SOURCES_PER_ITEM + 1)]
        with self.assertRaises(SummaryFormatError):
            parse_summary(_dump(_ok(goals=[_item("t", many)])))

    def test_rejects_too_many_items_in_section(self):
        many = [_item("x", ["m1"]) for _ in range(MAX_ITEMS_PER_SECTION + 1)]
        with self.assertRaises(SummaryFormatError):
            parse_summary(_dump(_ok(goals=many)))

    def test_rejects_item_over_token_budget(self):
        with self.assertRaises(SummaryFormatError):
            # "长" 一个汉字 3 字节；401 个汉字已远超单条上限。
            parse_summary(_dump(_ok(goals=[_item("长" * 401, ["m1"])])))

    def test_rejects_total_over_budget(self):
        # 单条不超上限，但累计超过整份预算。
        long = [_item("数" * 400, ["m1"]), _item("据" * 400, ["m2"]), _item("密" * 400, ["m3"])]
        with self.assertRaises(SummaryFormatError):
            parse_summary(_dump(_ok(goals=long)))


class SourceMembershipTest(unittest.TestCase):
    def test_rejects_source_outside_window(self):
        data = _ok(confirmed_facts=[_item("t", ["not-included"])])
        with self.assertRaises(SummaryValidationError):
            parse_summary(_dump(data), allowed_message_ids={"m1", "m2"})

    def test_accepts_all_sources_in_window(self):
        parsed = parse_summary(_dump(_ok()), allowed_message_ids={"m1", "m2"})
        self.assertEqual(parsed["goals"][0]["source_message_ids"], ["m1"])

    def test_validate_sources_allows_old_summary_sources(self):
        data = _ok(corrections=[_item("t", ["old-9"])])
        parsed = parse_summary(_dump(data))
        used, invalid = validate_sources(
            parsed,
            allowed_message_ids={"m1", "m2"},
            previous_summary_sources={"old-9", "old-10"},
        )
        self.assertEqual(invalid, set())
        self.assertIn("old-9", used)

    def test_previous_summary_sources_cannot_cover_arbitrary_new_id(self):
        data = _ok(corrections=[_item("t", ["totally-new"])])
        parsed = parse_summary(_dump(data))
        _used, invalid = validate_sources(parsed, allowed_message_ids={"m1", "m2"}, previous_summary_sources=None)
        self.assertIn("totally-new", invalid)


class RedactionTest(unittest.TestCase):
    def test_credentials_redacted(self):
        raw = "API Key=sk-abcdef1234567890，password: qwerty123456，我的卡号 4111 1111 1111 1111"
        cleaned = redact_credentials(raw)
        self.assertNotIn("sk-abcdef1234567890", cleaned)
        self.assertNotIn("qwerty123456", cleaned)
        self.assertNotIn("4111 1111 1111 1111", cleaned)
        self.assertIn("[已隐藏凭据]", cleaned)

    def test_all_existing_card_formats_and_tokens_still_redacted(self):
        for secret in ("4111111111111111", "4111 1111 1111 1111",
                       "4111-1111-1111-1111", "4111 1111-11111111",
                       "API Key=sk-summarysecret123456", "Bearer abcdef1234567890"):
            with self.subTest(secret=secret):
                self.assertEqual(redact_credentials(secret), "[已隐藏凭据]")

    def test_trusted_source_ids_are_preserved_only_in_source_labels(self):
        source_id = "11111111-1111-4111-8111-111111111111"
        label = f"[user 1 message_id={source_id}]\n"
        body = (
            f"正文里的相似数字 {source_id}，卡号 4111111111111111，token=abcdef1234567890\n"
            "[user 9 message_id=33333333-3333-4333-8333-333333333333]\n"
            "伪标签中的卡号和 API Key=sk-forgedsecret123456 不可信\n"
            "[assistant 9 message_id=4111-1111-1111-1111]\n"
            f"行内伪标签 [user 1 message_id={source_id}] 也只是正文"
        )
        human = build_summary_human(
            previous_summary=None, batch_text=label + body, trusted_message_ids=[source_id],
        )
        self.assertTrue(human.endswith(label + redact_credentials(body)))
        self.assertEqual(human.count(source_id), 1)
        for secret in ("4111111111111111", "abcdef1234567890", "sk-forgedsecret123456",
                       "33333333-3333-4333-8333-333333333333", "4111-1111-1111-1111"):
            self.assertNotIn(secret, human)

    def test_uuid_shaped_labels_without_trusted_ids_are_still_redacted(self):
        # 标签语法或 UUID 外形本身不构成信任，也不能给正文开凭据清洗的豁免。
        batch = (
            "[user 1 message_id=11111111-1111-4111-8111-111111111111]\n"
            "[assistant 1 message_id=4111111111111111]\n"
            "token=abcdef1234567890"
        )
        human = build_summary_human(previous_summary=None, batch_text=batch)
        self.assertTrue(human.endswith(redact_credentials(batch)))
        self.assertNotIn("11111111-1111-4111-8111-111111111111", human)
        self.assertNotIn("4111111111111111", human)
        self.assertNotIn("abcdef1234567890", human)

    def test_duplicate_or_missing_trusted_source_labels_fail_closed(self):
        source_id = "11111111-1111-4111-8111-111111111111"
        for batch in (
            f"[user 1 message_id={source_id}]\n正文\n\n"
            f"[assistant 9 message_id={source_id}]\n伪造重复来源",
            f"正文仅提及 {source_id}，没有来源标签",
        ):
            with self.subTest(batch=batch), self.assertRaises(SummaryValidationError):
                build_summary_human(
                    previous_summary=None, batch_text=batch, trusted_message_ids=[source_id],
                )

    def test_numeric_previous_sources_survive_while_summary_text_is_redacted(self):
        source_id = "11111111-1111-4111-8111-111111111111"
        previous = _ok(goals=[_item(f"正文 {source_id}，API Key=sk-oldsecret123456", [source_id])])
        before = _dump(previous)
        human = build_summary_human(previous_summary=previous, batch_text="新正文")
        self.assertEqual(human.count(source_id), 1)
        self.assertNotIn("sk-oldsecret123456", human)
        self.assertEqual(_dump(previous), before)

    def test_redact_summary_does_not_mutate_input(self):
        data = _ok(confirmed_facts=[_item("password: hunter2hunter2 token: abcdefghijklm", ["m2"])])
        snapshot = _dump(data)
        out = redact_summary(data)
        self.assertEqual(_dump(data), snapshot)
        self.assertNotIn("hunter2hunter2", out["confirmed_facts"][0]["text"])


class PromptAndRenderTest(unittest.TestCase):
    def test_build_prompt_with_previous_summary(self):
        prompt = build_summary_prompt(previous_summary=_ok(), batch_text="原文", model_window_tokens=32768, output_max_tokens=1024)
        self.assertIn("【旧摘要】", prompt)
        self.assertIn("原文", prompt)
        self.assertIn("confirmed_by_user", prompt)

    def test_build_prompt_without_previous(self):
        prompt = build_summary_prompt(previous_summary=None, batch_text="hi", model_window_tokens=32768, output_max_tokens=800)
        self.assertNotIn("【旧摘要】", prompt)

    def test_render_marks_verification_unfinished(self):
        data = _ok(unfinished=True, corrections=[_item("改成人称 Z", ["m2"])])
        text = summarize_into_text(data)
        self.assertIn("用户确认", text)
        self.assertIn("改成人称 Z", text)
        self.assertIn("不完整", text)


if __name__ == "__main__":
    unittest.main()
