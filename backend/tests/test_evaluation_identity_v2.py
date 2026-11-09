"""身份 schema v2：检索开关、完整本地语料、索引清单、分层核验与严格业务模式。

全部使用临时目录与内存回放；不访问网络、数据库或模型。
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import evaluation as ev
from scripts.evaluate_rag import main

ROOT = Path(__file__).parents[1] / "evaluation"
RUN_ID = "00000000-0000-4000-8000-000000000010"


def _lf_documents_copy(temp):
    """基线按 LF 语料生成；Windows 工作区用临时副本复现，不改真实语料。"""
    target = Path(temp) / "documents-lf"
    for source in sorted((ROOT / "documents").rglob("*")):
        if not source.is_file():
            continue
        destination = target / source.relative_to(ROOT / "documents")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    return target


def _cases(name="cases.platform.example.jsonl"):
    return ev.validate_cases(ev.read_jsonl(ROOT / name))


def _responses(name="responses.platform.example.jsonl"):
    return {row["id"]: row for row in ev.validate_responses(ev.read_jsonl(ROOT / name))}


def _report(cases, responses, *, mode="offline", documents_dir=ROOT / "documents", **config):
    results = [ev.grade_case(case, responses.get(case["id"], {"error": "missing_response"})) for case in cases]
    identity = ev.build_identity(cases, mode=mode, documents_dir=documents_dir, **config)
    return ev.make_report(results, RUN_ID, identity=identity)


LIVE_CONFIG = {
    "model": "demo-model", "embedding_model": "demo-embed", "chunk_size": 800, "chunk_overlap": 120,
    "retrieval_top_k": 6, "retrieval_rewrite": "on", "hybrid_retrieval_enabled": "on",
    "rerank_enabled": "off", "retrieval_min_similarity": 0.0, "retrieval_neighbor_chunks": 1,
    "retrieval_max_per_doc": 3, "keyword_candidates": 60,
}


class RetrievalConfigIdentityTests(unittest.TestCase):
    def test_each_retrieval_switch_changes_identity(self):
        cases = _cases()
        # 变体测试以开启重排的完整身份为基准，双侧都声明模型/截断参数。
        base_config = {**LIVE_CONFIG, "rerank_enabled": "on", "rerank_model": "r1", "rerank_max_chars": 2000}
        base = ev.config_identity(mode="live", **base_config)
        variants = {
            "hybrid_retrieval_enabled": {"hybrid_retrieval_enabled": "off", "keyword_candidates": None},
            "rerank_enabled": {"rerank_enabled": "off", "rerank_model": None, "rerank_max_chars": None},
            "rerank_model": {"rerank_model": "r2"},
            "rerank_max_chars": {"rerank_max_chars": 1000},
            "retrieval_min_similarity": {"retrieval_min_similarity": 0.2},
            "retrieval_neighbor_chunks": {"retrieval_neighbor_chunks": 0},
            "retrieval_max_per_doc": {"retrieval_max_per_doc": 1},
        }
        for key, change in variants.items():
            with self.subTest(key=key):
                other = ev.config_identity(mode="live", **{**base_config, **change})
                self.assertNotEqual(base["sha256"], other["sha256"], key)
                comparison = ev.compare_reports(
                    _report(cases, _responses(), mode="live", **base_config),
                    _report(cases, _responses(), mode="live", **{**base_config, **change}))
                self.assertEqual(comparison["status"], "incomparable")
                self.assertIn(f"config_changed:{key}", comparison["reasons"])
        self.assertNotEqual(ev.config_identity(mode="live", **base_config)["sha256"],
                            ev.config_identity(mode="live", **{**base_config, "keyword_candidates": 40})["sha256"])

    def test_rerank_off_does_not_require_model_and_stays_comparable(self):
        cases = _cases()
        left = _report(cases, _responses(), mode="live", **LIVE_CONFIG)
        right = _report(cases, _responses(), mode="live", **{**LIVE_CONFIG, "rerank_model": None})
        comparison = ev.compare_reports(left, right)
        self.assertEqual(comparison["verdict"], "no_regression")
        self.assertNotIn("config_unverifiable:rerank_model", comparison["reasons"])
        self.assertNotIn("rerank_model", comparison["unknown_config_fields"])

    def test_rerank_on_requires_model_on_both_sides_even_if_both_absent(self):
        cases = _cases()
        on = {**LIVE_CONFIG, "rerank_enabled": "on", "rerank_model": "r1"}
        missing = {key: value for key, value in on.items() if key != "rerank_model"}
        comparison = ev.compare_reports(_report(cases, _responses(), mode="live", **on),
                                        _report(cases, _responses(), mode="live", **missing))
        self.assertEqual(comparison["status"], "incomparable")
        self.assertIn("config_unverifiable:rerank_model", comparison["reasons"])
        both_missing = ev.compare_reports(_report(cases, _responses(), mode="live", **missing),
                                          _report(cases, _responses(), mode="live", **missing))
        self.assertEqual(both_missing["status"], "incomparable")
        self.assertIn("config_unverifiable:rerank_model", both_missing["reasons"])
        self.assertIn("config_unverifiable:rerank_max_chars", both_missing["reasons"])

    def test_hybrid_on_requires_keyword_candidates_and_off_ignores_it(self):
        cases = _cases()
        missing = {key: value for key, value in LIVE_CONFIG.items() if key != "keyword_candidates"}
        comparison = ev.compare_reports(_report(cases, _responses(), mode="live", **LIVE_CONFIG),
                                        _report(cases, _responses(), mode="live", **missing))
        self.assertEqual(comparison["status"], "incomparable")
        self.assertIn("config_unverifiable:keyword_candidates", comparison["reasons"])
        off = {**LIVE_CONFIG, "hybrid_retrieval_enabled": "off", "keyword_candidates": None}
        off_with_value = {**off, "keyword_candidates": 40}
        self.assertEqual(ev.compare_reports(_report(cases, _responses(), mode="live", **off),
                                            _report(cases, _responses(), mode="live", **off_with_value))["verdict"],
                         "no_regression")

    def test_explicit_null_is_unverifiable_not_agreement(self):
        base = _report(_cases(), _responses(), mode="live", **LIVE_CONFIG)
        for name in ("mode", "model", "retrieval_top_k", "hybrid_retrieval_enabled"):
            with self.subTest(name=name):
                left, right = json.loads(json.dumps(base)), json.loads(json.dumps(base))
                left["identity"]["config"][name] = None
                right["identity"]["config"][name] = None
                comparison = ev.compare_reports(left, right)
                self.assertEqual(comparison["status"], "incomparable")
                self.assertIn(f"config_unverifiable:{name}", comparison["reasons"])
                self.assertEqual(ev.comparison_exit_code(comparison, assertions_passed=True), 4)

    def test_live_identity_missing_offline_optional(self):
        cases = _cases()
        offline = ev.build_identity(cases, mode="offline", documents_dir=ROOT / "documents")
        self.assertNotIn("hybrid_retrieval_enabled", offline["config"])
        live = ev.build_identity(cases, mode="live", documents_dir=ROOT / "documents", **LIVE_CONFIG)
        self.assertEqual(live["config"]["rerank_enabled"], "off")
        self.assertNotIn("rerank_model", live["config"])  # 关闭时模型不进入身份

    def test_bad_types_and_secrets_are_rejected_without_echo(self):
        cases = _cases()
        for config in ({"rerank_enabled": True}, {"hybrid_retrieval_enabled": "yes"},
                       {"retrieval_min_similarity": 1.5}, {"retrieval_min_similarity": True},
                       {"retrieval_neighbor_chunks": 3}, {"retrieval_max_per_doc": 0},
                       {"keyword_candidates": 201}, {"rerank_max_chars": 10},
                       {"rerank_model": "https://secret.example/r"}):
            with self.subTest(config=config):
                with self.assertRaises(ev.EvaluationError) as error:
                    ev.build_identity(cases, mode="offline", documents_dir=None, **config)
                self.assertNotIn("secret.example", str(error.exception))


class CorpusIdentityTests(unittest.TestCase):
    def _corpus(self, temp):
        root = Path(temp) / "docs"
        (root / "sub").mkdir(parents=True)
        (root / "a.md").write_text("alpha", encoding="utf-8")
        (root / "sub" / "b.md").write_text("beta", encoding="utf-8")
        return root

    def test_extra_unreferenced_file_and_subdirectory_change_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._corpus(temp)
            cases = _cases()
            first = ev.corpus_identity(root)
            self.assertEqual(first["files"], 2)
            (root / "distractor.md").write_text("noise", encoding="utf-8")
            second = ev.corpus_identity(root)
            self.assertNotEqual(first["sha256"], second["sha256"])
            (root / "sub" / "b.md").write_text("beta2", encoding="utf-8")
            third = ev.corpus_identity(root)
            self.assertNotEqual(second["sha256"], third["sha256"])
            self.assertNotEqual(ev.dataset_identity(cases)["sha256"],
                                ev.dataset_identity(cases)["sha256"] + "x")
            # 改名同样是变化
            (root / "distractor.md").rename(root / "renamed.md")
            self.assertNotEqual(third["sha256"], ev.corpus_identity(root)["sha256"])

    def test_identity_does_not_leak_names_or_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._corpus(temp)
            identity = ev.build_identity(_cases(), mode="offline", documents_dir=root)
            text = json.dumps(identity, ensure_ascii=False)
            self.assertNotIn("a.md", text)
            self.assertNotIn("alpha", text)
            self.assertEqual(identity["dataset"]["documents"]["kind"], "declared-local-snapshot")

    @unittest.skipUnless(hasattr(os, "symlink"), "需要符号链接支持")
    def test_symlinks_are_rejected_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._corpus(temp)
            outside = Path(temp) / "outside.md"
            outside.write_text("secret", encoding="utf-8")
            try:
                (root / "link.md").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("当前平台不允许创建符号链接")
            with self.assertRaises(ev.EvaluationError) as error:
                ev.corpus_identity(root)
            self.assertNotIn("secret", str(error.exception))
            (root / "link.md").unlink()
            (root / "dirlink").symlink_to(Path(temp), target_is_directory=True)
            with self.assertRaises(ev.EvaluationError):
                ev.corpus_identity(root)
            (root / "dirlink").unlink()
            linked_root = Path(temp) / "rootlink"
            linked_root.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ev.EvaluationError):
                ev.corpus_identity(linked_root)

    def test_old_schema_reports_read_but_stay_incomparable(self):
        cases, responses = _cases(), _responses()
        current = _report(cases, responses, documents_dir=ROOT / "documents")
        legacy = json.loads(json.dumps(current))
        legacy["identity"]["schema"] = 1
        legacy["identity"]["dataset"]["schema"] = 1
        comparison = ev.compare_reports(legacy, current)
        self.assertEqual(comparison["status"], "incomparable")
        self.assertIn("identity_schema_mismatch", comparison["reasons"])
        self.assertEqual(ev.comparison_exit_code(comparison, assertions_passed=True), 4)
        # 旧 schema 不会被悄悄升级成新身份
        self.assertEqual(legacy["identity"]["schema"], 1)


class IndexManifestTests(unittest.TestCase):
    ENTRY = {"document_id": "doc-1", "index_version": 2,
             "content_sha256": "a" * 64, "chunk_count": 4}

    def test_manifest_digest_is_structural_and_stable(self):
        first = ev.index_manifest_identity({"documents": [dict(self.ENTRY)]})
        second = ev.index_manifest_identity({"documents": [dict(self.ENTRY)]})
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["documents"], 1)
        text = json.dumps(first)
        self.assertNotIn("doc-1", text)
        changed = ev.index_manifest_identity({"documents": [{**self.ENTRY, "index_version": 3}]})
        self.assertNotEqual(first["sha256"], changed["sha256"])
        with self.assertRaises(ev.EvaluationError):
            ev.index_manifest_identity([dict(self.ENTRY)])  # 唯一格式是 {"documents": [...]}

    def test_unknown_or_extra_fields_are_rejected(self):
        for manifest in ({"documents": [{**self.ENTRY, "content": "private body"}]},
                         {"documents": [{"document_id": "doc-1", "index_version": 1, "content_sha256": "a" * 64,
                                         "filename": "私有.md"}]},
                         {"documents": [self.ENTRY], "token": "sk-secret"},
                         {"documents": [{"document_id": "doc-1", "index_version": 1}]}):
            with self.subTest(manifest=manifest):
                with self.assertRaises(ev.EvaluationError) as error:
                    ev.index_manifest_identity(manifest)
                self.assertNotIn("private", str(error.exception))
                self.assertNotIn("sk-secret", str(error.exception))

    def test_one_sided_or_changed_manifest_is_incomparable(self):
        cases, responses = _cases(), _responses()
        base = ev.build_identity(cases, mode="offline", documents_dir=ROOT / "documents",
                                 index_manifest={"documents": [self.ENTRY]})
        none = ev.build_identity(cases, mode="offline", documents_dir=ROOT / "documents")
        current = ev.make_report([ev.grade_case(case, responses[case["id"]]) for case in cases], RUN_ID, identity=base)
        without = ev.make_report([ev.grade_case(case, responses[case["id"]]) for case in cases], RUN_ID, identity=none)
        self.assertEqual(ev.compare_reports(without, current)["reasons"], ["index_manifest_unverifiable"])
        changed = dict(base)
        changed["dataset"] = {**base["dataset"], "index_manifest": ev.index_manifest_identity(
            {"documents": [{**self.ENTRY, "index_version": 5}]})}
        changed_report = ev.make_report([ev.grade_case(case, responses[case["id"]]) for case in cases], RUN_ID,
                                        identity=changed)
        comparison = ev.compare_reports(current, changed_report)
        self.assertEqual(comparison["status"], "incomparable")
        self.assertIn("index_manifest_changed", comparison["reasons"])

    def test_cli_manifest_roundtrip_and_unknown_field_exit_two(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            manifest = temp_path / "manifest.json"
            manifest.write_text(json.dumps({"documents": [self.ENTRY]}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output, patch("scripts.evaluate_rag.httpx.Client") as client:
                code = main(["--dataset", str(ROOT / "cases.platform.example.jsonl"),
                             "--responses", str(ROOT / "responses.platform.example.jsonl"),
                             "--index-manifest", str(manifest),
                             "--output", str(temp_path / "report.json")])
            client.assert_not_called()
            self.assertEqual(code, 0)
            body = json.loads((temp_path / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(body["identity"]["dataset"]["index_manifest"]["documents"], 1)
            self.assertNotIn("doc-1", json.dumps(body, ensure_ascii=False))
            self.assertNotIn("doc-1", output.getvalue())
            manifest.write_text(json.dumps({"documents": [{**self.ENTRY, "body": "private"}]}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = main(["--dataset", str(ROOT / "cases.platform.example.jsonl"),
                             "--responses", str(ROOT / "responses.platform.example.jsonl"),
                             "--index-manifest", str(manifest), "--output", str(temp_path / "r2.json")])
            self.assertEqual(code, 2)
            self.assertNotIn("private", output.getvalue())


class StratumAndReviewTests(unittest.TestCase):
    def test_stratum_and_review_counts_are_reported_per_group(self):
        case = {"id": "a", "question": "q", "stratum": "fact",
                "verified_by": "rev-1", "verified_at": "2026-01-02"}
        other = {"id": "b", "question": "q", "stratum": "fact"}
        results = [ev.grade_case(case, {"answer": "ok"}), ev.grade_case(other, {"answer": "ok"})]
        report = ev.make_report(results)
        group = report["strata"]["fact"]
        self.assertEqual((group["cases"], group["passed"], group["reviewed"], group["unreviewed"]), (2, 2, 1, 1))
        self.assertEqual((report["reviewed_cases"], report["unreviewed_cases"]), (1, 1))
        # 兼容旧字段
        for key in ("cases", "passed", "assertion_pass_rate", "results"):
            self.assertIn(key, report)
        self.assertIn("不代表语义准确率", report["notes"][-1])

    def test_unknowns_make_metadata_incomplete_but_not_review_failure_by_itself(self):
        case = {"id": "a", "question": "q", "verified_by": "rev-1", "verified_at": "2026-01-02",
                "unknowns": ["still open"]}
        self.assertEqual(ev.review_state(case), "incomplete")
        with self.assertRaises(ev.EvaluationError):
            ev.require_reviewed_cases([case])

    def test_explicit_unreviewed_flag_cannot_pass_strict_mode(self):
        case = {"id": "a", "question": "q", "verified_by": "rev-1", "verified_at": "2026-01-02",
                "reviewed": False}
        self.assertEqual(ev.review_state(case), "unreviewed")
        with self.assertRaises(ev.EvaluationError):
            ev.require_reviewed_cases([case])
        report = ev.make_report([ev.grade_case(case, {"answer": "ok"})])
        self.assertEqual(report["reviewed_cases"], 0)

    def test_invalid_stratum_and_metadata_error_is_redacted(self):
        for extra in ({"stratum": "私有部门-机密"}, {"verified_by": "张三"},
                      {"verified_at": "2026-13-01"}, {"verified_at": "2026-1-1"},
                      {"unknowns": "open"}, {"reviewed": "no"}):
            with self.subTest(extra=extra):
                with self.assertRaises(ev.EvaluationError) as error:
                    ev.validate_cases([{"id": "x", "question": "q", **extra}])
                self.assertNotIn("私有部门-机密", str(error.exception))
                self.assertNotIn("张三", str(error.exception))

    def test_template_is_rejected_in_strict_mode_but_not_by_default(self):
        template = ev.read_jsonl(ROOT / "templates" / "business-questions.template.jsonl")
        ev.validate_cases(template)  # 默认允许模板存在
        with self.assertRaises(ev.EvaluationError) as error:
            ev.require_reviewed_cases(template)
        self.assertNotIn("在此粘贴", str(error.exception))
        reviewed = [{"id": "biz-1", "question": "真实问题", "stratum": "fact",
                     "verified_by": "rev-1", "verified_at": "2026-01-02"}]
        ev.require_reviewed_cases(ev.validate_cases(reviewed))

    def test_strict_mode_distinguishes_technical_text_from_review_placeholders(self):
        for question in ("如何使用 str.replace？", "如何管理 TODO 标记？"):
            case = {"id": "biz-1", "question": question, "stratum": "fact",
                    "verified_by": "rev-1", "verified_at": "2026-01-02", "unknowns": []}
            ev.require_reviewed_cases(ev.validate_cases([case]))
        case["verified_by"] = "REPLACE-reviewer"
        with self.assertRaises(ev.EvaluationError) as error:
            ev.require_reviewed_cases(ev.validate_cases([case]))
        self.assertIn("placeholder_case:1", str(error.exception))
        self.assertNotIn("REPLACE-reviewer", str(error.exception))

    def test_review_metadata_does_not_change_dataset_identity(self):
        cases = _cases()
        annotated = [dict(case) for case in cases]
        annotated[0] = {**annotated[0], "stratum": "fact", "verified_by": "rev-1", "verified_at": "2026-01-02", "reviewed": True}
        self.assertEqual(ev.dataset_identity(cases)["sha256"], ev.dataset_identity(annotated)["sha256"])

    def test_cli_require_reviewed_blocks_before_any_live_call(self):
        with tempfile.TemporaryDirectory() as temp, patch("scripts.evaluate_rag.httpx.Client") as client, \
             patch.dict(os.environ, {"RAG_EVAL_TOKEN": "token-value"}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            dataset = Path(temp) / "cases.jsonl"
            dataset.write_text((ROOT / "templates" / "business-questions.template.jsonl").read_text(encoding="utf-8"),
                               encoding="utf-8")
            code = main(["--live", "--require-reviewed", "--dataset", str(dataset),
                         "--base-url", "http://127.0.0.1:8000",
                         "--kb-id", str(__import__("uuid").uuid4()),
                         "--output", str(Path(temp) / "r.json")])
        self.assertEqual(code, 2)
        client.assert_not_called()
        self.assertNotIn("token-value", output.getvalue())

    def test_reports_do_not_echo_verifier_or_content(self):
        case = {"id": "a", "question": "私有问题正文", "turns": ["私有问题正文"], "stratum": "fact",
                "verified_by": "rev-secret", "verified_at": "2026-01-02"}
        report = ev.make_report([ev.grade_case(case, {"answer": "私有答案正文"})])
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("rev-secret", text)
        self.assertNotIn("私有问题正文", text)
        self.assertNotIn("私有答案正文", text)


class CliRegressionTests(unittest.TestCase):
    def test_offline_self_comparison_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as temp, patch("scripts.evaluate_rag.httpx.Client") as client, \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(["--dataset", str(ROOT / "cases.platform.example.jsonl"),
                         "--responses", str(ROOT / "responses.platform.example.jsonl"),
                         "--documents-dir", str(_lf_documents_copy(temp)),
                         "--baseline", str(ROOT / "baseline.platform.example.json"),
                         "--output", str(Path(temp) / "r.json")])
        client.assert_not_called()
        self.assertEqual(code, 0)

    def test_new_config_key_is_accepted_and_secret_keys_still_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            good = temp_path / "good.json"
            good.write_text(json.dumps({"hybrid_retrieval_enabled": "on", "keyword_candidates": 60,
                                        "rerank_enabled": "off"}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["--dataset", str(ROOT / "cases.platform.example.jsonl"),
                             "--responses", str(ROOT / "responses.platform.example.jsonl"),
                             "--config-file", str(good), "--output", str(temp_path / "r.json")])
            self.assertEqual(code, 0)
            for bad in ({"rerank_base_url": "https://secret.example"}, {"rerank_api_key": "sk-x"}):
                with self.subTest(bad=bad):
                    path = temp_path / "bad.json"
                    path.write_text(json.dumps(bad), encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        code = main(["--dataset", str(ROOT / "cases.platform.example.jsonl"),
                                     "--responses", str(ROOT / "responses.platform.example.jsonl"),
                                     "--config-file", str(path), "--output", str(temp_path / "r.json")])
                    self.assertEqual(code, 2)
                    self.assertNotIn("secret.example", output.getvalue())
                    self.assertNotIn("sk-x", output.getvalue())


if __name__ == "__main__":
    unittest.main()
