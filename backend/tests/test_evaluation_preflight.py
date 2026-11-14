"""业务基线本地输入预检：纯合成输入、完整批次、脱敏与零外部副作用回归。

不运行真实评测、不创建 HTTP 客户端/套接字，也不读取应用配置、.env 或令牌。
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import evaluation as ev
from scripts import preflight_evaluation as pf
from scripts.evaluate_rag import CONFIG_KEYS

ROOT = Path(__file__).parents[1]
TEMPLATES = ROOT / "evaluation" / "templates"
SECRET_CASE_ID = "PRIVATE-CASE-ID-DO-NOT-EXPORT"
SECRET_QUESTION = "PRIVATE-QUESTION-BODY-DO-NOT-EXPORT"
SECRET_SOURCE = "private-source-filename.md"
SECRET_MODEL = "private-model-name"
SECRET_REVIEWER = "private-reviewer-code"
SECRET_UNKNOWN = "private-unresolved-note"
SECRET_DOCUMENT = "private-document-identity"
SECRET_PATH = "C:/private-inputs/secret-name"

LIVE_CONFIG = {
    "model": "synth-chat-model", "embedding_model": "synth-embed-model",
    "chunk_size": 800, "chunk_overlap": 120, "retrieval_top_k": 6,
    "retrieval_rewrite": "on", "hybrid_retrieval_enabled": "on",
    "keyword_candidates": 60, "rerank_enabled": "off",
    "retrieval_min_similarity": 0.0, "retrieval_neighbor_chunks": 1,
    "retrieval_max_per_doc": 3,
}
MANIFEST = {"documents": [{
    "document_id": "synth-doc-1", "index_version": 1,
    "content_sha256": "a" * 64, "chunk_count": 2,
}]}
SECRETS = (SECRET_CASE_ID, SECRET_QUESTION, SECRET_SOURCE, SECRET_MODEL,
           SECRET_REVIEWER, SECRET_UNKNOWN, SECRET_DOCUMENT, SECRET_PATH,
           "secret-token-value", "PRIVATE-UNKNOWN-CONFIG-KEY")


def _case(index=1, **changes):
    case = {
        "id": f"synth-{index}", "question": f"synthetic question {index}",
        "expected_outcome": "answer",
        "expected_sources": [{"filename": f"synth-{index}.md"}],
        "stratum": "fact", "source_labels_complete": True,
        "verified_by": "rev-1", "verified_at": "2026-01-02", "unknowns": [],
        "reviewed": True,
    }
    case.update(changes)
    return case


class _Fixture:
    """每个测试独立的临时目录；所有业务文本都是合成值。"""

    def __init__(self, temp, cases=None, config=LIVE_CONFIG, manifest=MANIFEST, corpus=True):
        self.root = Path(temp)
        self.dir = self.root / "inputs"
        self.dir.mkdir()
        self.docs = self.root / "documents"
        self.docs.mkdir()
        if corpus:
            (self.docs / "synth.md").write_text("synthetic corpus\n", encoding="utf-8")
        self.cases = self.dir / "cases.jsonl"
        self.config = self.dir / "config.json"
        self.manifest = self.dir / "manifest.json"
        self.write_cases(cases if cases is not None else [_case()])
        if config is not None:
            self.config.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        if manifest is not None:
            self.manifest.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def write_cases(self, cases):
        self.cases.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n"
                                      for case in cases), encoding="utf-8")

    def write_config(self, value):
        self.config.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def write_manifest(self, value):
        self.manifest.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def argv(self, *, dataset=True, docs=True, config=True, manifest=True, extra=()):
        result = []
        if dataset:
            result += ["--dataset", str(self.cases)]
        if docs:
            result += ["--documents-dir", str(self.docs)]
        if config:
            result += ["--config-file", str(self.config)]
        if manifest:
            result += ["--index-manifest", str(self.manifest)]
        return [*result, *extra]


def _call(argv):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        code = pf.main(argv)
    return code, output.getvalue()


def _parse(output):
    return json.loads(output.strip())


def _ready_assertions(test, result):
    test.assertEqual(result["state"], "ready")
    test.assertEqual(result["blocking_reasons"], [])
    test.assertEqual(result["schema"], pf.RESULT_SCHEMA)
    for name in pf._FLAGS:
        test.assertIs(result[name], False)
    test.assertEqual(set(result), pf._OUTPUT_FIELDS)
    test.assertEqual(result["input_fingerprints"]["schema"], pf.LOCAL_INPUT_SCHEMA)
    test.assertEqual(set(result["input_fingerprints"]), pf._FP_FIELDS)


class SharedConfigHelperTests(unittest.TestCase):
    def test_config_allowlist_is_shared_and_unchanged(self):
        self.assertEqual(CONFIG_KEYS, ev.PUBLIC_CONFIG_KEYS)
        self.assertEqual(tuple(CONFIG_KEYS), ev._CRITICAL_CONFIG_FIELDS[1:])
        self.assertEqual(set(CONFIG_KEYS), set(ev._REQUIRED_CONFIG["live"]) |
                         set(ev._CONDITIONAL_CONFIG))
        self.assertEqual(ev.missing_live_config_fields(LIVE_CONFIG), ())
        self.assertEqual(ev.missing_live_config_fields({}), ev._REQUIRED_CONFIG["live"])

    def test_enabled_switches_require_conditional_fields_without_altering_algorithms(self):
        rerank = {**LIVE_CONFIG, "rerank_enabled": "on"}
        self.assertEqual(ev.missing_live_config_fields(rerank),
                         ("rerank_model", "rerank_max_chars"))
        hybrid = {**LIVE_CONFIG, "hybrid_retrieval_enabled": "off",
                  "keyword_candidates": None}
        self.assertEqual(ev.missing_live_config_fields(hybrid), ())
        # 关闭开关时残留条件值仍不进入既有 config_identity。
        off = ev.config_identity(mode="live", **{**LIVE_CONFIG, "keyword_candidates": None})
        self.assertNotIn("keyword_candidates", off)


class ReadyAndFingerprintTests(unittest.TestCase):
    def test_ready_full_batch_reports_counts_flags_and_input_fingerprints(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, cases=[_case(1), _case(2, expected_outcome="refuse",
                                                    expect_no_citations=True,
                                                    expected_sources=[], stratum="unanswerable"),
                                      _case(3, expected_outcome="clarify",
                                            expect_no_citations=True,
                                            expected_sources=[], stratum="process_sla")])
            code, output = _call(f.argv())
            result = _parse(output)
            self.assertEqual(code, pf.EXIT_READY)
            _ready_assertions(self, result)
            self.assertEqual(result["case_count"], 3)
            self.assertEqual(result["max_cases"], 20)
            self.assertEqual(result["strata_counts"]["fact"], 1)
            self.assertEqual(result["strata_counts"]["unanswerable"], 1)
            self.assertEqual(result["strata_counts"]["process_sla"], 1)
            self.assertEqual(result["outcome_counts"],
                             {"answer": 1, "refuse": 1, "clarify": 1,
                              "http_error": 0, "unspecified": 0})
            self.assertEqual(result["review_state_counts"]["complete"], 3)
            self.assertEqual(result["legal_outcomes"], list(pf.LEGAL_OUTCOMES))
            self.assertEqual(result["legal_strata"], list(ev.CASE_STRATA))
            self.assertEqual(result["legal_review_states"], list(pf._REVIEW_STATES))
            fp = result["input_fingerprints"]
            self.assertEqual(fp["cases"]["case_count"], 3)
            self.assertEqual(fp["corpus"]["files"], 1)
            self.assertEqual(fp["index_manifest"]["documents"], 1)
            for part in (fp["cases"], fp["corpus"], fp["config"], fp["index_manifest"]):
                self.assertRegex(part["sha256"], r"[a-f0-9]{64}")
            # 与既有身份算法的摘要逐项一致；输出不含完整 live identity。
            cases = ev.validate_cases(ev.read_jsonl(f.cases))
            identity = ev.build_identity(cases, mode="live", documents_dir=f.docs,
                                         index_manifest=MANIFEST, **LIVE_CONFIG)
            self.assertEqual(fp["cases"]["sha256"], identity["dataset"]["sha256"])
            self.assertEqual(fp["corpus"]["sha256"], identity["dataset"]["documents"]["sha256"])
            self.assertEqual(fp["config"]["sha256"], identity["config"]["sha256"])
            self.assertEqual(fp["index_manifest"]["sha256"],
                             identity["dataset"]["index_manifest"]["sha256"])
            self.assertNotIn("metrics_version", output)
            self.assertNotIn('"identity"', output)
            self.assertNotIn('"config": {"model"', output)

    def test_omitting_manifest_does_not_block_but_empty_manifest_does(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, manifest=None)
            code, output = _call(f.argv(manifest=False))
            self.assertEqual(code, pf.EXIT_READY)
            self.assertIsNone(_parse(output)["input_fingerprints"]["index_manifest"])
            f.write_manifest({"documents": []})
            code, output = _call(f.argv())
            result = _parse(output)
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertIn("index_manifest_empty", result["blocking_reasons"])
            self.assertIsNone(result["input_fingerprints"]["index_manifest"])

    def test_each_declared_input_change_changes_its_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp)
            baseline = _parse(_call(f.argv())[1])["input_fingerprints"]
            f.write_cases([_case(1, question="synthetic question changed")])
            changed_cases = _parse(_call(f.argv())[1])["input_fingerprints"]["cases"]["sha256"]
            self.assertNotEqual(baseline["cases"]["sha256"], changed_cases)
            (f.docs / "synth.md").write_text("synthetic corpus changed\n", encoding="utf-8")
            changed_corpus = _parse(_call(f.argv())[1])["input_fingerprints"]["corpus"]["sha256"]
            self.assertNotEqual(baseline["corpus"]["sha256"], changed_corpus)
            f.write_config({**LIVE_CONFIG, "retrieval_top_k": 7})
            changed_config = _parse(_call(f.argv())[1])["input_fingerprints"]["config"]["sha256"]
            self.assertNotEqual(baseline["config"]["sha256"], changed_config)
            f.write_manifest({"documents": [{**MANIFEST["documents"][0], "index_version": 2}]})
            changed_manifest = _parse(_call(f.argv())[1])["input_fingerprints"]["index_manifest"]["sha256"]
            self.assertNotEqual(baseline["index_manifest"]["sha256"], changed_manifest)


class BlockedPreparationTests(unittest.TestCase):
    def test_missing_config_corpus_and_annotation_problems_aggregate_in_one_pass(self):
        cases = [
            _case(1, id=SECRET_CASE_ID, question=SECRET_QUESTION,
                  expected_sources=[{"filename": SECRET_SOURCE}],
                  reviewed=False, verified_by=SECRET_REVIEWER, unknowns=[SECRET_UNKNOWN]),
            _case(2, question="REPLACE-QUESTION", expected_outcome="refuse",
                  expected_sources=[], stratum="unanswerable"),
            _case(3, expected_outcome=None, stratum=None),
            _case(4, expected_outcome="answer", expected_sources=[]),
            _case(5, question="synthetic permission case",
                  expected_http_status=403, expected_outcome=None,
                  expected_sources=[], stratum="access_compliance"),
            _case(6, expected_outcome="clarify", expected_sources=[], stratum="process_sla"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, cases=cases, config={k: v for k, v in LIVE_CONFIG.items()
                                                     if k not in {"model", "embedding_model"}},
                         corpus=False)
            code, output = _call(f.argv())
            result = _parse(output)
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertEqual(result["state"], "blocked")
            self.assertEqual(result["case_count"], 6)
            reasons = result["blocking_reasons"]
            for expected in ("config_field_missing:model", "config_field_missing:embedding_model",
                             "documents_dir_empty", "review_incomplete_case:1",
                             "placeholder_case:2", "expected_outcome_missing_case:3",
                             "stratum_unclassified_case:3", "expected_sources_missing_case:4"):
                self.assertIn(expected, reasons)
            # 预期 HTTP 4xx 的单请求负例不要求 expected_outcome 或来源，但仍须登记分层。
            self.assertNotIn("expected_outcome_missing_case:5", reasons)
            self.assertNotIn("expected_sources_missing_case:5", reasons)
            self.assertEqual(result["strata_counts"]["access_compliance"], 1)
            self.assertEqual(result["outcome_counts"]["http_error"], 1)
            self.assertEqual(result["review_state_counts"]["unreviewed"], 1)
            self.assertIsNone(result["input_fingerprints"]["corpus"])
            self.assertFalse(result["measured"])
            self.assertFalse(result["business_acceptance"])

    def test_http_negative_needs_registered_stratum_but_not_expected_outcome(self):
        base = {"id": "synth-1", "question": "synthetic permission case",
                "expected_http_status": 403, "stratum": "access_compliance",
                "verified_by": "rev-1", "verified_at": "2026-01-02", "unknowns": [],
                "reviewed": True}
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, cases=[base])
            code, output = _call(f.argv())
            result = _parse(output)
            self.assertEqual(code, pf.EXIT_READY)
            _ready_assertions(self, result)
            self.assertEqual(result["outcome_counts"]["http_error"], 1)
            self.assertEqual(result["strata_counts"]["access_compliance"], 1)
            self.assertNotIn("unclassified", json.dumps(result["blocking_reasons"]))
            unclassified = {**base, "stratum": None}
            f.write_cases([unclassified])
            code, output = _call(f.argv())
            result = _parse(output)
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertIn("stratum_unclassified_case:1", result["blocking_reasons"])
            self.assertNotIn("expected_outcome_missing_case:1", result["blocking_reasons"])
            self.assertNotIn("expected_sources_missing_case:1", result["blocking_reasons"])
            self.assertEqual(result["strata_counts"]["unclassified"], 1)
            self.assertEqual(result["outcome_counts"]["http_error"], 1)

    def test_missing_and_empty_corpus_are_blocked_without_implicit_sibling_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, corpus=False)
            # 故意准备问题集旁 documents/，预检不得自动读取它。
            sibling = f.dir / "documents"
            sibling.mkdir()
            (sibling / "sibling.md").write_text("should-not-be-read", encoding="utf-8")
            code, output = _call(f.argv(docs=False))
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertIn("documents_dir_missing", _parse(output)["blocking_reasons"])
            code, output = _call(f.argv())
            result = _parse(output)
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertIn("documents_dir_empty", result["blocking_reasons"])
            self.assertIsNone(result["input_fingerprints"]["corpus"])
            self.assertEqual(sorted(p.name for p in sibling.iterdir()), ["sibling.md"])

    def test_switches_require_conditional_fields_and_invalid_given_values_are_error(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, config={**LIVE_CONFIG, "rerank_enabled": "on"})
            code, output = _call(f.argv())
            reasons = _parse(output)["blocking_reasons"]
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertIn("config_field_missing:rerank_model", reasons)
            self.assertIn("config_field_missing:rerank_max_chars", reasons)
            f.write_config({**LIVE_CONFIG, "hybrid_retrieval_enabled": "on",
                            "keyword_candidates": None})
            code, output = _call(f.argv())
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertIn("config_field_missing:keyword_candidates",
                          _parse(output)["blocking_reasons"])
            for value in ({**LIVE_CONFIG, "model": "https://user@example.test/model"},
                          {**LIVE_CONFIG, "model": "name@example.test"},
                          {**LIVE_CONFIG, "chunk_size": True},
                          {**LIVE_CONFIG, "chunk_overlap": 800},
                          {**LIVE_CONFIG, "retrieval_min_similarity": 1.5},
                          {**LIVE_CONFIG, "rerank_enabled": True},
                          {**LIVE_CONFIG, "SECRET_TOKEN": "secret-token-value"}):
                with self.subTest(value=value):
                    f.write_config(value)
                    self.assertEqual(_call(f.argv())[0], pf.EXIT_INVALID)

    def test_oversized_full_batch_is_blocked_not_sliced_and_all_cases_are_checked(self):
        cases = [_case(i) for i in range(1, 22)]
        cases[-1] = _case(21, question="REPLACE-LAST-CASE",
                          expected_outcome="answer", expected_sources=[])
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, cases=cases)
            code, output = _call(f.argv(extra=["--max-cases", "20"]))
            result = _parse(output)
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertEqual(result["case_count"], 21)
            self.assertEqual(result["max_cases"], 20)
            self.assertEqual(result["input_fingerprints"]["cases"]["case_count"], 21)
            self.assertIn("case_limit_exceeded", result["blocking_reasons"])
            # 第 21 条超出上限仍完成占位符检查。
            self.assertIn("placeholder_case:21", result["blocking_reasons"])
            self.assertIn("expected_sources_missing_case:21", result["blocking_reasons"])
            code, output = _call(f.argv(extra=["--max-cases", "21"]))
            self.assertNotIn("case_limit_exceeded", _parse(output)["blocking_reasons"])
            self.assertIn("placeholder_case:21", _parse(output)["blocking_reasons"])
            for invalid in ("0", "-1", "1001", "20.5", "nan"):
                with self.subTest(invalid=invalid):
                    self.assertEqual(_call(f.argv(extra=["--max-cases", invalid]))[0],
                                     pf.EXIT_INVALID)
            self.assertEqual(_call(f.argv(extra=["--max-cases", "1000"]))[0],
                             pf.EXIT_BLOCKED)
            self.assertEqual(_call(f.argv(extra=["--max-cases", "1"]))[0],
                             pf.EXIT_BLOCKED)

    def test_empty_dataset_is_blocked_and_review_flag_contract_is_not_reinterpreted(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, cases=[])
            code, output = _call(f.argv())
            result = _parse(output)
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertIn("dataset_empty", result["blocking_reasons"])
            self.assertEqual(result["case_count"], 0)
            # 显式 reviewed=false 即使有核验人/日期也仍是 incomplete 准备项。
            f.write_cases([_case(reviewed=False)])
            self.assertIn("review_incomplete_case:1",
                          _parse(_call(f.argv())[1])["blocking_reasons"])
            # 核验元数据齐全但 unknowns 非空同样不算就绪。
            f.write_cases([_case(unknowns=[SECRET_UNKNOWN])])
            self.assertIn("review_incomplete_case:1",
                          _parse(_call(f.argv())[1])["blocking_reasons"])


class InvalidInputTests(unittest.TestCase):
    def _assert_invalid(self, argv, output, code):
        self.assertEqual(code, pf.EXIT_INVALID)
        result = _parse(output)
        self.assertEqual(result, pf._error_result())
        self.assertEqual(set(result), pf._ERROR_FIELDS)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("blocking_reasons", result)
        self.assertNotIn("input_fingerprints", result)
        for secret in SECRETS:
            self.assertNotIn(secret, output)

    def test_missing_required_argument_and_unknown_private_arguments_are_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp)
            for argv in ([], ["--dataset", str(f.cases), "--live"],
                         ["--dataset", str(f.cases), "--responses", SECRET_PATH],
                         ["--dataset", str(f.cases), "--token", "secret-token-value"],
                         ["--dataset", str(f.cases), "--base-url", SECRET_PATH],
                         ["--bogus", SECRET_PATH],
                         ["--dataset", str(f.cases), "--max-cases", "not-a-number"],
                         ["--dataset", str(f.cases), "--max-cases", "9" * 5000],
                         ["--dataset", str(f.cases), "--max-cases", SECRET_PATH]):
                with self.subTest(argv=argv):
                    code, output = _call(argv)
                    self._assert_invalid(argv, output, code)

    def test_missing_symlinked_or_unreadable_declared_paths_are_input_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp)
            for extra in (["--dataset", str(f.root / "missing.jsonl")],
                          ["--documents-dir", str(f.root / "missing-dir")],
                          ["--config-file", str(f.root / "missing.json")],
                          ["--index-manifest", str(f.root / "missing.json")]):
                with self.subTest(extra=extra):
                    argv = f.argv(extra=extra)
                    self.assertEqual(_call(argv)[0], pf.EXIT_INVALID)
            # 显式语料根目录、目录内文件或 JSON 输入是符号链接均 fail closed。
            if hasattr(os, "symlink"):
                link_root = f.root / "root-link"
                try:
                    link_root.symlink_to(f.docs, target_is_directory=True)
                except (OSError, NotImplementedError):
                    pass
                else:
                    self.assertEqual(_call(f.argv(extra=["--documents-dir", str(link_root)]))[0],
                                     pf.EXIT_INVALID)
                inner = f.docs / "link.md"
                try:
                    inner.symlink_to(f.root / "outside.md")
                except (OSError, NotImplementedError):
                    pass
                else:
                    self.assertEqual(_call(f.argv())[0], pf.EXIT_INVALID)
                    inner.unlink()
                config_link = f.root / "config-link.json"
                try:
                    config_link.symlink_to(f.config)
                except (OSError, NotImplementedError):
                    pass
                else:
                    self.assertEqual(_call(f.argv(extra=["--config-file", str(config_link)]))[0],
                                     pf.EXIT_INVALID)
            # 不可读目录：损坏目录树由既有 corpus_identity 拒绝。
            with patch("app.services.evaluation._hash_file", side_effect=OSError("private-io-detail")):
                code, output = _call(f.argv())
            self._assert_invalid(f.argv(), output, code)

    def test_malformed_json_and_structural_values_fail_without_leaking_text(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp)
            broken = [
                '{"id":"x","question":"q"\n',
                '{"id":"x","question":"q"} trailing',
                '{"id":"x","question":"q","question":"other"}',
                '{"id":"x","question":"q","nan":NaN}',
                '{"id":"x","question":"q","big":1e999}',
                '{"id":"x","question":"q","big":' + "9" * 5000 + '}',
                '{"id":"x","question":"\ud800"}',
                '{"id":"x","question":"q","nested":' + "[" * 20 + "0" + "]" * 20 + "}",
                '[{"id":"x","question":"q"}]',
                '{"id":"x","question":"q","turns":["ok"],"reviewed":"no"}',
            ]
            for line in broken:
                with self.subTest(line=line[:40]):
                    # 用 UTF-8 无代理字节写入，模拟 JSON 转义的孤立 \ud800。
                    f.cases.write_bytes(line.encode("unicode_escape") + b"\n")
                    code, output = _call(f.argv())
                    self._assert_invalid(f.argv(), output, code)
            # 字面 JSON 转义的孤立代理与过深结构确实由严格解析器自身拒绝。
            for text in ('{"x":"\\ud800"}',
                         '{"x":' + "[" * 20 + "0" + "]" * 20 + "}"):
                with self.subTest(strict_json=text[:30]):
                    with self.assertRaises(ValueError):
                        pf._strict_json(text)
            f.write_cases([_case()])
            for text in ('{"model":"x","model":"y"}',
                         '{"model":NaN}', '{"model":"\ud800"}',
                         '{"model":"x","nested":' + "[" * 20 + "0" + "]" * 20 + "}",
                         '["not-an-object"]'):
                with self.subTest(config=text[:40]):
                    f.config.write_bytes(text.encode("unicode_escape"))
                    code, output = _call(f.argv())
                    self._assert_invalid(f.argv(), output, code)
            f.write_config(LIVE_CONFIG)
            for text in ('{"documents":{}}', '{"documents":[{"document_id":"'
                         + SECRET_DOCUMENT + '","index_version":1,'
                         '"content_sha256":"' + "a" * 64 + '","token":"secret-token-value"}]}',
                         '{"documents":[],"documents":[]}'):
                with self.subTest(manifest=text[:40]):
                    f.manifest.write_text(text, encoding="utf-8")
                    code, output = _call(f.argv())
                    self._assert_invalid(f.argv(), output, code)

    def test_regression_protocol_batch_is_rejected_with_stable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, cases=[_case(regression_protocol=1,
                                            regression_mode="live")])
            code, output = _call(f.argv())
            self._assert_invalid(f.argv(), output, code)

    def test_no_example_config_is_used_automatically(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, config=None)
            # 仓库内样例配置存在，但预检不自动查找或读取。
            self.assertTrue((ROOT / "evaluation" / "config.live.example.json").is_file())
            original = pf._validate_config
            seen = []

            def guarded(path):
                seen.append(path)
                if path is not None:
                    raise AssertionError("config file read attempted")
                return original(path)

            with patch("scripts.preflight_evaluation._validate_config", guarded):
                code, output = _call(f.argv(config=False, manifest=False))
            self.assertEqual(seen, [None])
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertIn("config_file_missing", _parse(output)["blocking_reasons"])
            self.assertIsNone(_parse(output)["input_fingerprints"]["config"])


class PrivacyAndLeakTests(unittest.TestCase):
    def test_output_has_no_case_bodies_ids_source_names_models_reviewers_or_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, cases=[_case(id=SECRET_CASE_ID, question=SECRET_QUESTION,
                                            expected_sources=[{"filename": SECRET_SOURCE}],
                                            verified_by=SECRET_REVIEWER,
                                            unknowns=[SECRET_UNKNOWN],
                                            reviewed=False)],
                         config={**LIVE_CONFIG, "model": SECRET_MODEL},
                         manifest={"documents": [{**MANIFEST["documents"][0],
                                                  "document_id": SECRET_DOCUMENT}]})
            code, output = _call(f.argv(extra=["--dataset", str(f.cases)]))
            self.assertEqual(code, pf.EXIT_BLOCKED)
            result = _parse(output)
            self.assertEqual(set(result), pf._OUTPUT_FIELDS)
            self.assertEqual(set(result["blocking_reasons"]),
                             {"review_incomplete_case:1"})
            self.assertRegex(result["blocking_reasons"][0], r"^review_incomplete_case:\d+$")
            for secret in (*SECRETS, "synth-1.md", "synth-chat-model",
                           "C:/", "rev-1"):
                self.assertNotIn(secret, output)
            self.assertNotIn("://", output)
            self.assertNotIn(SECRET_PATH, output)
            self.assertEqual(set(result["input_fingerprints"]["config"]), {"sha256"})
            self.assertEqual(set(result["input_fingerprints"]["corpus"]),
                             {"files", "sha256"})
            self.assertEqual(set(result["input_fingerprints"]["index_manifest"]),
                             {"documents", "sha256"})

    def test_every_blocking_reason_is_a_whitelisted_code_or_one_based_ordinal(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, cases=[_case(id=SECRET_CASE_ID, question="REPLACE-1",
                                            expected_sources=[]),
                                      _case(2, expected_outcome=None, stratum=None),
                                      _case(3, reviewed=False)],
                         config={}, corpus=False,
                         manifest={"documents": []})
            result = _parse(_call(f.argv())[1])
            pattern = re.compile(
                r"(?:config_file_missing|documents_dir_missing|documents_dir_empty|"
                r"index_manifest_empty|dataset_empty|case_limit_exceeded|"
                r"config_field_missing:" + "|".join(ev.PUBLIC_CONFIG_KEYS) + r"|"
                r"(?:placeholder_case|review_incomplete_case|"
                r"expected_outcome_missing_case|stratum_unclassified_case|"
                r"expected_sources_missing_case):[1-9][0-9]*)$")
            self.assertTrue(result["blocking_reasons"])
            for reason in result["blocking_reasons"]:
                self.assertRegex(reason, pattern)
            self.assertNotIn(SECRET_CASE_ID, json.dumps(result))

    def test_error_output_has_no_traceback_or_raw_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp)
            with patch("app.services.evaluation._hash_file",
                       side_effect=RuntimeError("private-exception-body")):
                code, output = _call(f.argv())
            self.assertEqual(code, pf.EXIT_INVALID)
            self.assertNotIn("private-exception-body", output)
            self.assertNotIn("Traceback", output)
            self.assertEqual(_parse(output)["error_code"], pf._STABLE_ERROR)


class ZeroSideEffectTests(unittest.TestCase):
    def test_http_clients_and_sockets_are_failure_traps_during_ready_and_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp)
            for argv in (f.argv(), f.argv(docs=False, config=False, manifest=False)):
                with patch("http.client.HTTPConnection.connect",
                           side_effect=AssertionError("HTTP connection attempted")), \
                     patch("http.client.HTTPSConnection.connect",
                           side_effect=AssertionError("HTTPS connection attempted")), \
                     patch("socket.socket.connect",
                           side_effect=AssertionError("socket connect attempted")), \
                     patch("socket.create_connection",
                           side_effect=AssertionError("socket creation attempted")), \
                     patch.dict(os.environ, {"RAG_EVAL_TOKEN": "secret-token-value",
                                             "RAG_EVAL_MEMBER_TOKEN": "secret-token-value"}):
                    code, output = _call(argv)
                self.assertIn(code, (pf.EXIT_READY, pf.EXIT_BLOCKED))
                self.assertNotIn("secret-token-value", output)

    def test_subprocess_import_has_no_forbidden_modules_or_env_or_dotenv_reads(self):
        script = r'''
import json, os, sys
events = []
FORBIDDEN = ("httpx", "requests", "sqlalchemy", "dotenv", "app.core", "app.db",
             "app.models", "openai", "langchain", "socket", "urllib.request")
CREDENTIALS = ("RAG_EVAL_TOKEN", "RAG_EVAL_MEMBER_TOKEN", "RAG_API_KEY")
def audit(event, args):
    if event.startswith("socket"):
        events.append(["socket", event])
    elif event in ("os.putenv", "os.getenv", "os.environ"):
        events.append(["env", event])
    elif event == "import":
        name = args[0]
        if name.startswith(FORBIDDEN):
            events.append(["import", name])
    elif event == "open":
        path = str(args[0]).lower()
        if ".env" in path or "token" in path:
            events.append(["open", path])
sys.addaudithook(audit)
for name in CREDENTIALS:
    os.environ[name] = "secret-token-value"
class GuardedEnviron(dict):
    def __getitem__(self, key):
        if key in CREDENTIALS:
            events.append(["credential_read", key])
        return super().__getitem__(key)
    def get(self, key, default=None):
        if key in CREDENTIALS:
            events.append(["credential_read", key])
        return super().get(key, default)
os.environ = GuardedEnviron(os.environ)
events.clear()  # 只审计守卫安装后，预检模块自身产生的副作用
import scripts.preflight_evaluation as pf
result = {"events": events,
          "modules": sorted(m for m in sys.modules if m.startswith(FORBIDDEN)),
          "config_keys": len(pf.ev.PUBLIC_CONFIG_KEYS)}
sys.stdout.write(json.dumps(result))
'''
        environment = {**os.environ, "RAG_DISABLE_DOTENV": "1", "LOG_STDOUT_ONLY": "1",
                       "PYTHONPATH": str(ROOT)}
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script], cwd=ROOT,
            env=environment, capture_output=True, text=True, timeout=60, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["modules"], [])
        self.assertEqual(result["config_keys"], len(CONFIG_KEYS))

    def test_cli_only_writes_stdout_and_does_not_overwrite_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp)
            before = {p: p.read_bytes() for p in f.dir.iterdir() if p.is_file()}
            before_docs = {p: p.read_bytes() for p in f.docs.iterdir() if p.is_file()}
            files_before = sorted(p.name for p in f.root.rglob("*") if p.is_file())
            code, output = _call(f.argv())
            self.assertEqual(code, pf.EXIT_READY)
            self.assertTrue(output.strip().endswith("}"))
            after = {p: p.read_bytes() for p in f.dir.iterdir() if p.is_file()}
            after_docs = {p: p.read_bytes() for p in f.docs.iterdir() if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(before_docs, after_docs)
            self.assertEqual(files_before,
                             sorted(p.name for p in f.root.rglob("*") if p.is_file()))


class TemplateTests(unittest.TestCase):
    def test_twenty_slot_template_structure_and_strata(self):
        path = TEMPLATES / "business-baseline-20.template.jsonl"
        cases = ev.read_jsonl(path)
        self.assertEqual(len(cases), 20)
        ev.validate_cases(cases)
        strata = [ev.case_stratum(case) for case in cases]
        self.assertEqual(strata, ["fact"] * 6 + ["cross_doc"] * 4 +
                         ["process_sla"] * 4 + ["unanswerable"] * 4 +
                         ["process_sla"] * 2)
        outcomes = [case.get("expected_outcome") for case in cases]
        self.assertEqual(outcomes, ["answer"] * 14 + ["refuse"] * 4 + ["clarify"] * 2)
        for case in cases:
            self.assertIn(case["stratum"], ev.CASE_STRATA)
            self.assertIs(case["reviewed"], False)
            self.assertIsNone(case["verified_by"])
            self.assertIsNone(case["verified_at"])
            self.assertTrue(case["unknowns"])
            self.assertIs(case["source_labels_complete"], False)
            self.assertTrue(any("REPLACE-" in text or "在此粘贴" in text or
                                "待填写" in text or "请填写" in text
                                for text in [case["id"], case["question"]]))
            self.assertIn("请在仓库外填写经人工核验的问题与标准", case["question"])
            self.assertNotIn("勿填写真实标准", json.dumps(case, ensure_ascii=False))
            self.assertEqual(ev.review_state(case), "unreviewed")
            for value in json.dumps(case, ensure_ascii=False).split():
                self.assertNotIn("http://", value)
        with self.assertRaises(ev.EvaluationError):
            ev.require_reviewed_cases(cases)
        # 模板只是本地采集材料，不能作为业务质量通过的证明。
        with tempfile.TemporaryDirectory() as temp:
            f = _Fixture(temp, cases=cases)
            code, output = _call(f.argv(extra=["--max-cases", "20"]))
            result = _parse(output)
            self.assertEqual(code, pf.EXIT_BLOCKED)
            self.assertEqual(result["case_count"], 20)
            self.assertTrue(any(reason.startswith(("placeholder_case:",
                                                   "review_incomplete_case:"))
                                for reason in result["blocking_reasons"]))
            self.assertEqual(result["review_state_counts"]["unreviewed"], 20)
            self.assertTrue(all(result["strata_counts"][name] == count
                                for name, count in (("fact", 6), ("cross_doc", 4),
                                                    ("process_sla", 6),
                                                    ("unanswerable", 4))))
            self.assertFalse(result["business_acceptance"])

    def test_existing_two_case_template_is_untouched(self):
        path = TEMPLATES / "business-questions.template.jsonl"
        cases = ev.read_jsonl(path)
        self.assertEqual(len(cases), 2)
        ev.validate_cases(cases)
        self.assertEqual(cases[0]["id"], "REPLACE-CASE-001")
        self.assertEqual(cases[1]["id"], "REPLACE-CASE-002")


if __name__ == "__main__":
    unittest.main()
