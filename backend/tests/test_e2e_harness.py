"""E2E harness 自身的单元测试（不启动容器、不连接数据库、不访问模型）。

覆盖的是最容易"看起来能跑、实际在骗人"的部分：
* 子进程环境是否只白名单继承，真实模型凭据/数据库 URL 不会漏进去；
* 回环校验、owner 标记校验与 manifest 过期判定；
* 容量工具的硬上限与 429 语义；
* 模型替身的确定性向量、引用编号与 SSE 帧序（真起一次本地 HTTP，回环内）。
"""
import importlib.util
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "backend" / "scripts"


def _load(name: str):
    """按文件路径加载，避免依赖 pytest 的包发现规则。

    必须先注册到 ``sys.modules``：这些脚本使用 ``from __future__ import
    annotations``，dataclass 处理字符串注解时要回查模块字典。
    """
    module_name = f"_e2e_{name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


e2e_env = _load("e2e_env")
e2e_capacity = _load("e2e_capacity")
stub = _load("e2e_model_stub")


class EnvAllowlistTests(unittest.TestCase):
    parent = {
        "PATH": "/usr/bin",
        "TEMP": "/tmp",
        "SYSTEMROOT": "C:\\Windows",
        # 下面这些绝不能被继承：
        "LLM_API_KEY": "sk-real-model-key",
        "EMBEDDING_API_KEY": "sk-real-embedding-key",
        "DATABASE_URL": "postgresql+psycopg://prod:secret@10.0.0.5:5432/ragdb",
        "JWT_SECRET": "production-jwt-secret",
        "ADMIN_PASSWORD": "prod-admin-password",
        "OPENAI_API_KEY": "sk-proj-xyz",
        "RAG_EVAL_TOKEN": "eval-token",
        "UNRELATED_VAR": "something",
    }

    def test_only_allowlisted_parent_variables_are_inherited(self):
        env = e2e_env.sanitize_env({"RAG_DISABLE_DOTENV": "1"}, parent=self.parent)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["TEMP"], "/tmp")
        self.assertEqual(env["RAG_DISABLE_DOTENV"], "1")
        for leaked in ("LLM_API_KEY", "EMBEDDING_API_KEY", "DATABASE_URL",
                       "JWT_SECRET", "ADMIN_PASSWORD", "OPENAI_API_KEY",
                       "RAG_EVAL_TOKEN", "UNRELATED_VAR"):
            self.assertNotIn(leaked, env, f"{leaked} 不应从父进程继承")

    def test_secret_markers_are_detected(self):
        for name in ("LLM_API_KEY", "JWT_SECRET", "METRICS_TOKEN", "DB_PASSWORD", "X_CREDENTIAL"):
            self.assertTrue(e2e_env.is_secret_name(name), name)
        for name in ("PATH", "TEMP", "LOG_LEVEL", "UPLOAD_DIR"):
            self.assertFalse(e2e_env.is_secret_name(name), name)

    def test_explicit_overrides_are_allowed_even_with_secret_names(self):
        # 本地替身的 key 是显式声明的测试配置，必须能写进去。
        env = e2e_env.sanitize_env(
            {"LLM_API_KEY": "e2e-local-stub", "LLM_BASE_URL": "http://127.0.0.1:1/v1"},
            parent=self.parent,
        )
        self.assertEqual(env["LLM_API_KEY"], "e2e-local-stub")
        self.assertEqual(env["LLM_BASE_URL"], "http://127.0.0.1:1/v1")

    def test_leak_guard_rejects_undeclared_sensitive_names(self):
        env = e2e_env.sanitize_env({"PATH": "/usr/bin"}, parent=self.parent)
        env["SOME_UNDECLARED_SECRET"] = "x"
        with self.assertRaises(ValueError):
            e2e_env.assert_no_leaked_secrets(env, {"PATH"})

    def test_child_env_never_contains_parent_database_url(self):
        parent = dict(os.environ)
        parent["DATABASE_URL"] = "postgresql+psycopg://prod:secret@10.0.0.5:5432/ragdb"
        parent["LLM_API_KEY"] = "sk-real"
        with patch.dict(os.environ, parent, clear=True):
            orchestrator = e2e_env.Orchestrator(out_dir=Path("unused"), token="t0ken")
            orchestrator.state["pg_port"] = 5555
            orchestrator.db_name = "rag_e2e_t0ken_test"
            orchestrator.db_password = "local-db-password"
            orchestrator.state["stub_base_url"] = "http://127.0.0.1:1234/v1"
            env = orchestrator.child_env()
        self.assertEqual(env["RAG_DISABLE_DOTENV"], "1")
        self.assertIn("127.0.0.1:5555", env["DATABASE_URL"])
        self.assertNotIn("prod:secret", env["DATABASE_URL"])
        self.assertEqual(env["LLM_BASE_URL"], "http://127.0.0.1:1234/v1")
        self.assertEqual(env["EMBEDDING_BASE_URL"], "http://127.0.0.1:1234/v1")
        self.assertEqual(env["LLM_API_KEY"], "e2e-local-stub")
        self.assertNotEqual(env["LLM_API_KEY"], "sk-real")
        self.assertEqual(env["ENVIRONMENT"], "development")
        self.assertEqual(env["RERANK_ENABLED"], "False")


class WorkerRoleTests(unittest.TestCase):
    def test_worker_roles_cover_four_independent_processes(self):
        # manifest.workers、子进程启动、崩溃清理的 PID 归属都从这份映射派生。
        self.assertEqual(tuple(e2e_env.WORKER_ROLES.items()), (
            ("worker", "ingest"),
            ("worker-summary", "summary"),
            ("worker-project", "project"),
            ("worker-regression", "regression"),
        ))

    def test_boot_passes_only_the_selected_role_to_worker_argparse(self):
        import subprocess
        import tempfile
        choices = "[" + ", ".join(json.dumps(role) for role in e2e_env.WORKER_ROLES.values()) + "]"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            (path / "worker.py").write_text(
                'import argparse, json, sys\n'
                'parser = argparse.ArgumentParser()\n'
                f'parser.add_argument("--role", choices={choices})\n'
                'args = parser.parse_args()\n'
                'print(json.dumps({"role": args.role, "argv": sys.argv[1:]}))\n', encoding="utf-8",
            )
            for role in e2e_env.WORKER_ROLES.values():
                with self.subTest(role=role):
                    result = subprocess.run([
                        sys.executable, "-I", "-c", e2e_env.WORKER_BOOT, str(path), role,
                        "--ragkb-e2e-nonce", "n" * 32,
                    ], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), {"role": role, "argv": ["--role", role]})

    def test_start_worker_spawns_every_role_with_its_own_nonce_argv(self):
        orchestrator = e2e_env.Orchestrator(out_dir=Path("unused"), token="abc123",
                                            nonce="n" * 32)
        # 用真实 child_env()：只补齐它在真实 start() 里会由 postgres/stub 启动阶段
        # 写入的少量状态，不 mock 待测的启动逻辑本身。
        orchestrator.db_name = "rag_e2e_abc123_test"
        orchestrator.db_password = "local-db-password"
        orchestrator.state.update(pg_port=5432, stub_base_url="http://127.0.0.1:1234/v1")
        spawned = {}

        class FakeChild:
            def __init__(self, name):
                self.log_path = Path(f"{name}.log")

            def alive(self):
                return True

        def fake_spawn(name, argv, *, env, cwd):
            spawned[name] = argv
            orchestrator.children[name] = FakeChild(name)
            return orchestrator.children[name]

        with patch.object(orchestrator, "_spawn", side_effect=fake_spawn), \
                patch.object(e2e_env.time, "sleep"):
            orchestrator._start_worker()

        self.assertEqual(list(spawned), list(e2e_env.WORKER_ROLES))
        for name, role in e2e_env.WORKER_ROLES.items():
            with self.subTest(name=name):
                argv = spawned[name]
                # WORKER_BOOT 的入参契约：<backend> <role> --ragkb-e2e-nonce <nonce>；
                # boot 自己把它转成 worker.py 的 ``--role <role>``（由上一个用例验证）。
                boot = argv.index(e2e_env.WORKER_BOOT)
                self.assertEqual(argv[boot + 1], str(e2e_env.REPO_ROOT / "backend"))
                self.assertEqual(argv[boot + 2], role)
                self.assertEqual(argv[boot + 3], "--ragkb-e2e-nonce")
                self.assertEqual(argv[boot + 4], "n" * 32)

    def test_crash_cleanup_verifies_every_worker_role(self):
        nonce = "n" * 32
        pids = {name: 2000 + index for index, name in enumerate(e2e_env.WORKER_ROLES)}
        seen = {}

        def fake_matches(pid, *, required, optional_any=(), nonce=""):
            seen[pid] = (required, nonce)
            return True

        with patch.object(e2e_env, "_pid_alive", return_value=True), \
                patch.object(e2e_env, "_process_matches", side_effect=fake_matches), \
                patch.object(e2e_env, "_kill_pid") as kill:
            killed, refused = e2e_env._kill_verified_processes({"pids": pids}, nonce=nonce)

        self.assertEqual(refused, [])
        self.assertEqual(sorted(killed), sorted(pids.values()))
        self.assertEqual(kill.call_count, len(pids))
        for name, role in e2e_env.WORKER_ROLES.items():
            with self.subTest(name=name):
                self.assertEqual(seen[pids[name]], (("worker.py", role), nonce))

    def test_fixture_and_blob_roots_are_owned_by_this_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            e2e_env.prepare_output_dir(out, "abc123", "n" * 32)
            orchestrator = e2e_env.Orchestrator(out_dir=out, token="abc123", nonce="n" * 32)
            orchestrator._prepare_project_fixture()
            orchestrator.state.update(pg_port=5432, stub_base_url="http://127.0.0.1:1234/v1")
            env = orchestrator.child_env()
            self.assertEqual(json.loads(env["PROJECT_ALLOWED_ROOTS"]), {"e2e": str(out / "project-source")})
            self.assertEqual(env["PROJECT_STORAGE_DIR"], str(out / "project-blobs"))
            self.assertIn("ALPHA_VERSION_ONE", (out / "project-source" / "notes.md").read_text(encoding="utf-8"))

    def test_summary_stub_returns_verifiable_source_ids(self):
        source_id = "11111111-1111-4111-8111-111111111111"
        raw = stub.Handler._summary_answer(f"[user 1 message_id={source_id}]\n使用独立 worker\n\n")
        from app.services.memory_prompt import parse_summary
        result = parse_summary(raw)
        self.assertEqual(result["confirmed_facts"][0]["source_message_ids"], [source_id])
        self.assertIn("使用独立 worker", result["confirmed_facts"][0]["text"])


class LoopbackTests(unittest.TestCase):
    def test_loopback_accepts_local_hosts(self):
        for url in ("http://127.0.0.1:8000", "http://localhost:5173/x", "http://[::1]:9000/v1"):
            self.assertEqual(e2e_env.assert_loopback_url(url), url)

    def test_loopback_rejects_remote_and_non_http(self):
        for url in ("https://api.openai.com/v1", "http://10.0.0.5:8000",
                    "http://192.168.1.10/v1", "file:///tmp/x", "ftp://127.0.0.1/"):
            with self.assertRaises(ValueError):
                e2e_env.assert_loopback_url(url)

    def test_free_port_is_bindable_and_unique(self):
        ports = {e2e_env.free_port() for _ in range(5)}
        self.assertTrue(ports)
        for port in ports:
            self.assertGreater(port, 0)


class OwnershipTests(unittest.TestCase):
    def test_owner_label_and_name_must_match(self):
        name = e2e_env.container_name("abc123")
        self.assertEqual(name, "ragkb-e2e-abc123")
        self.assertTrue(e2e_env.owns_container({e2e_env.OWNER_LABEL: "abc123"}, name, "abc123"))
        # 标签对、名字不对：不是我们的
        self.assertFalse(e2e_env.owns_container({e2e_env.OWNER_LABEL: "abc123"}, "other", "abc123"))
        # 名字对、标签不对（例如别人手工建的测试容器）：绝不能删
        self.assertFalse(e2e_env.owns_container({}, name, "abc123"))
        self.assertFalse(e2e_env.owns_container({e2e_env.OWNER_LABEL: "someone-else"}, name, "abc123"))

    def test_parse_published_port_handles_docker_output(self):
        self.assertEqual(e2e_env.parse_published_port("127.0.0.1:49153\n"), 49153)
        self.assertEqual(e2e_env.parse_published_port("0.0.0.0:5432\n[::]:5432\n"), 5432)
        with self.assertRaises(ValueError):
            e2e_env.parse_published_port("")

    def test_report_redacts_database_password(self):
        orchestrator = e2e_env.Orchestrator(out_dir=Path("unused"), token="t0ken")
        orchestrator.state["pg_port"] = 5555
        orchestrator.db_name = "rag_e2e_t0ken_test"
        orchestrator.db_password = "super-secret-db-pass"
        url = orchestrator.database_url()
        self.assertIn("super-secret-db-pass", url)
        self.assertEqual(e2e_env.redact(url, ["super-secret-db-pass"]).count("super-secret-db-pass"), 0)

    def test_manifest_rejects_missing_owner_and_remote_targets(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            nonce = "n" * 32
            cases = [
                ({"api": {"base_url": "http://127.0.0.1:1"}, "stub": {"base_url": "http://127.0.0.1:2/v1"}},
                 "owner"),
                ({"owner": "x", "api": {"base_url": "http://127.0.0.1:1"},
                  "stub": {"base_url": "http://127.0.0.1:2/v1"}}, "nonce"),
                ({"owner": "x", "nonce": nonce, "api": {"base_url": "https://api.deepseek.com"},
                  "stub": {"base_url": "http://127.0.0.1:2/v1"}}, "回环"),
                ({"owner": "x", "nonce": nonce, "api": {"base_url": "http://127.0.0.1:1"},
                  "stub": {"base_url": "https://api.openai.com/v1"}}, "回环"),
            ]
            for payload, expected in cases:
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(e2e_capacity.ConfigError) as ctx:
                    e2e_capacity.load_manifest(path)
                self.assertIn(expected, str(ctx.exception))

    def test_manifest_rejects_stale_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps({
                "owner": "x",
                "nonce": "n" * 32,
                "api": {"base_url": "http://127.0.0.1:1"},
                "stub": {"base_url": "http://127.0.0.1:2/v1"},
            }), encoding="utf-8")
            old = time.time() - 10_000
            os.utime(path, (old, old))
            with self.assertRaises(e2e_capacity.ConfigError):
                e2e_capacity.load_manifest(path)


class OutputDirSafetyTests(unittest.TestCase):
    """负向测试：非本轮目录绝不能被创建、写入或删除。"""

    def _repo_marker_dir(self, root: Path) -> Path:
        # 造一个"看起来像仓库"的目录，验证 assert_safe_output_dir 会拒绝仓库后代。
        # root 本身可能还不存在：先建目录，再逐个子项创建。
        root.mkdir(parents=True)
        for marker in ("backend", "frontend", "docker-compose.yml"):
            target = root / marker
            if marker.endswith(".yml"):
                target.write_text("services: {}\n", encoding="utf-8")
            else:
                target.mkdir()
        return root

    def test_repo_output_dir_is_rejected_without_touching_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            repo = self._repo_marker_dir(Path(temp) / "repo")
            victim = repo / "backend" / "keep.txt"
            victim.write_text("original", encoding="utf-8")
            nested = repo / "backend" / "e2e-out"
            with self.assertRaises(ValueError):
                e2e_env.prepare_output_dir(nested, "abc123", "n" * 32)
            self.assertFalse(nested.exists())
            self.assertEqual(victim.read_text(encoding="utf-8"), "original")

    def test_non_empty_existing_dir_is_rejected_and_untouched(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "user-data"
            out.mkdir()
            keep = out / "report.pdf"
            keep.write_bytes(b"%PDF-1.7 payload")
            with self.assertRaises(ValueError):
                e2e_env.prepare_output_dir(out, "abc123", "n" * 32)
            self.assertEqual(keep.read_bytes(), b"%PDF-1.7 payload")
            self.assertTrue(keep.exists())

    def test_even_empty_existing_dir_is_rejected(self):
        # 安全默认：既有目录一律拒绝（空目录也可能是别人刚建的），
        # 不用"空就复用、非空才拒绝"这种会误删的宽松判定。
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "already-there"
            out.mkdir()
            with self.assertRaises(ValueError):
                e2e_env.prepare_output_dir(out, "abc123", "n" * 32)
            self.assertTrue(out.exists())
            self.assertFalse((out / e2e_env.DIR_MARKER_NAME).exists())

    def test_empty_dir_gets_marker_then_owned_checks(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            e2e_env.prepare_output_dir(out, "abc123", "n" * 32)
            e2e_env.assert_owned_output_dir(out, "abc123", "n" * 32)
            self.assertIsNotNone(e2e_env.read_dir_marker(out))
            # 别的 owner 不能操作它
            with self.assertRaises(ValueError):
                e2e_env.assert_owned_output_dir(out, "other1", "n" * 32)
            # 同 owner 的另一次运行（nonce 不同）也不能
            with self.assertRaises(ValueError):
                e2e_env.assert_owned_output_dir(out, "abc123", "m" * 32)
            self.assertFalse(e2e_env.remove_owned_output_dir(out, "other1"))
            self.assertTrue(out.exists())
            self.assertTrue(e2e_env.remove_owned_output_dir(out, "abc123", "n" * 32))
            self.assertFalse(out.exists())

    def test_tampered_marker_path_refuses_deletion(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            e2e_env.prepare_output_dir(out, "abc123", "n" * 32)
            marker = out / e2e_env.DIR_MARKER_NAME
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["path"] = str(Path(temp) / "somewhere-else")
            marker.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(e2e_env.remove_owned_output_dir(out, "abc123", "n" * 32))
            self.assertTrue(out.exists())

    def test_owner_validation_rejects_injection_shaped_values(self):
        for bad in ("", "AB", "has space", "../escape", "a" * 40, "abc/def", "abc.def"):
            with self.assertRaises(ValueError):
                e2e_env.validate_owner(bad)
        self.assertEqual(e2e_env.validate_owner("abc123"), "abc123")

    def test_down_refuses_dir_without_marker(self):
        import tempfile
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "not-ours"
            out.mkdir()
            keep = out / "notes.txt"
            keep.write_text("mine", encoding="utf-8")
            code = e2e_env.cmd_down(Namespace(out=str(out), wait=1.0))
            self.assertEqual(code, 2)
            self.assertEqual(keep.read_text(encoding="utf-8"), "mine")

    def test_down_refuses_foreign_owner_marker(self):
        import tempfile
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "other-run"
            e2e_env.prepare_output_dir(out, "other1", "n" * 32)
            (out / "manifest.json").write_text(
                json.dumps({"owner": "other1", "nonce": "n" * 32, "pids": {}}), encoding="utf-8")
            keep = out / "logs"
            keep.mkdir()
            (keep / "api.log").write_text("log", encoding="utf-8")
            # 目录 owner 与入参无关；缺 owner 输入时仍必须拒绝，不做任何删除。
            code = e2e_env.cmd_down(Namespace(out=str(out), wait=1.0))
            self.assertIn(code, (0, 2, 3, 4))
            self.assertTrue((keep / "api.log").exists() or not out.exists())

    def test_start_refuses_dir_owned_by_someone_else(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            e2e_env.prepare_output_dir(out, "other1", "n" * 32)
            orchestrator = e2e_env.Orchestrator(out_dir=out.resolve(), token="abc123",
                                                nonce="m" * 32)
            with self.assertRaises(ValueError):
                orchestrator.start()
            self.assertTrue(out.exists())


class KeptOutputShutdownTests(unittest.TestCase):
    def test_down_accepts_owned_shutdown_receipt_and_keeps_diagnostics(self):
        import tempfile
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            nonce = "n" * 32
            e2e_env.prepare_output_dir(out, "abc123", nonce)
            (out / "manifest.json").write_text(json.dumps({
                "owner": "abc123", "nonce": nonce,
                "orchestrator": {"pid": os.getpid()}, "pids": {},
            }), encoding="utf-8")
            log = out / "api.log"
            log.write_text("diagnostics", encoding="utf-8")
            orchestrator = e2e_env.Orchestrator(out_dir=out, token="abc123", nonce=nonce, keep=True)
            orchestrator.stop()
            for running in (True, False):
                with self.subTest(orchestrator_running=running), \
                        patch.object(e2e_env, "_process_matches", return_value=running), \
                        patch.object(e2e_env, "_kill_verified_processes") as fallback:
                    self.assertEqual(e2e_env.cmd_down(Namespace(out=str(out), wait=1.0)), 0)
                    fallback.assert_not_called()
                self.assertEqual(log.read_text(encoding="utf-8"), "diagnostics")

    def test_down_accepts_cleanup_completed_while_checking_process_identity(self):
        import tempfile
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            nonce = "n" * 32
            e2e_env.prepare_output_dir(out, "abc123", nonce)
            (out / "manifest.json").write_text(json.dumps({
                "owner": "abc123", "nonce": nonce,
                "orchestrator": {"pid": os.getpid()}, "pids": {},
            }), encoding="utf-8")

            def finished(*_args, **_kwargs):
                self.assertTrue(e2e_env.remove_owned_output_dir(out, "abc123", nonce))
                return False

            with patch.object(e2e_env, "_process_matches", side_effect=finished), \
                    patch.object(e2e_env, "_kill_verified_processes") as fallback:
                self.assertEqual(e2e_env.cmd_down(Namespace(out=str(out), wait=1.0)), 0)
                fallback.assert_not_called()
            self.assertFalse(out.exists())

    def test_shutdown_receipt_must_match_owner_nonce_and_process(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            nonce = "n" * 32
            e2e_env.prepare_output_dir(out, "abc123", nonce)
            expected = {"owner": "abc123", "nonce": nonce, "orchestrator_pid": os.getpid()}
            for field, value in (("owner", "other1"), ("nonce", "m" * 32), ("orchestrator_pid", os.getpid() + 1)):
                with self.subTest(field=field):
                    (out / "STOPPED.json").write_text(json.dumps({**expected, field: value}), encoding="utf-8")
                    with patch.object(e2e_env.time, "monotonic", side_effect=[0, 0.1, 2]), \
                            patch.object(e2e_env.time, "sleep"):
                        self.assertFalse(e2e_env._wait_for_shutdown(
                            out, 1, token="abc123", nonce=nonce, orchestrator_pid=os.getpid()))
                    self.assertTrue(out.exists())

    def test_failed_container_cleanup_does_not_acknowledge_shutdown(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            nonce = "n" * 32
            e2e_env.prepare_output_dir(out, "abc123", nonce)
            orchestrator = e2e_env.Orchestrator(out_dir=out, token="abc123", nonce=nonce, keep=True)
            with patch.object(orchestrator, "_stop_container", return_value=["cleanup failed"]):
                report = orchestrator.stop()
            self.assertEqual(report["container_failures"], ["cleanup failed"])
            self.assertFalse((out / "STOPPED.json").exists())


class ProcessIdentityTests(unittest.TestCase):
    """负向测试：tampered manifest / 复用 PID 绝不触发 kill。"""

    def test_pid_without_nonce_is_refused(self):
        manifest = {"pids": {"worker": os.getpid(), "stub": os.getpid()}}
        killed, refused = e2e_env._kill_verified_processes(manifest, nonce="deadbeef" * 4)
        self.assertEqual(killed, [])
        self.assertEqual(sorted(refused), sorted([os.getpid(), os.getpid()]))

    def test_unknown_child_name_is_refused(self):
        manifest = {"pids": {"evil": os.getpid()}}
        killed, refused = e2e_env._kill_verified_processes(manifest, nonce="deadbeef" * 4)
        self.assertEqual(killed, [])
        self.assertEqual(refused, [os.getpid()])

    def test_missing_nonce_in_command_line_refuses_match(self):
        # 用一个确定存在的进程（自己），并注入一段不在其命令行里的 nonce。
        self.assertFalse(e2e_env._process_matches(
            os.getpid(), required=("python",), nonce="0123456789abcdef0123456789abcdef"))

    def test_dead_pid_is_ignored(self):
        # 取一个几乎必然不存在的 PID：既不能抛异常，也不能算作"已杀"。
        killed, refused = e2e_env._kill_verified_processes(
            {"pids": {"worker": 999_999_999}}, nonce="deadbeef" * 4)
        self.assertEqual((killed, refused), ([], []))

    def test_tampered_manifest_owner_mismatch_blocks_down(self):
        import tempfile
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            e2e_env.prepare_output_dir(out, "abc123", "n" * 32)
            (out / "manifest.json").write_text(
                json.dumps({"owner": "other1", "nonce": "n" * 32, "pids": {"worker": os.getpid()}}),
                encoding="utf-8")
            code = e2e_env.cmd_down(Namespace(out=str(out), wait=1.0))
            self.assertEqual(code, 2)
            self.assertTrue(out.exists())

    def test_tampered_manifest_nonce_mismatch_blocks_down(self):
        import tempfile
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            e2e_env.prepare_output_dir(out, "abc123", "n" * 32)
            (out / "manifest.json").write_text(
                json.dumps({"owner": "abc123", "nonce": "m" * 32,
                            "pids": {"worker": os.getpid()}}), encoding="utf-8")
            code = e2e_env.cmd_down(Namespace(out=str(out), wait=1.0))
            self.assertEqual(code, 2)
            self.assertTrue(out.exists())

    def test_missing_manifest_fails_closed(self):
        """没有 manifest 就不知道有哪些进程；不许删目录。"""
        import tempfile
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            e2e_env.prepare_output_dir(out, "abc123", "n" * 32)
            keep = out / "logs"
            keep.mkdir()
            (keep / "api.log").write_text("log", encoding="utf-8")
            code = e2e_env.cmd_down(Namespace(out=str(out), wait=1.0))
            self.assertNotEqual(code, 0)
            self.assertTrue((keep / "api.log").exists())

    def test_corrupt_manifest_fails_closed(self):
        import tempfile
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            e2e_env.prepare_output_dir(out, "abc123", "n" * 32)
            (out / "manifest.json").write_text("{not json", encoding="utf-8")
            code = e2e_env.cmd_down(Namespace(out=str(out), wait=1.0))
            self.assertEqual(code, 4)
            self.assertTrue(out.exists())

    def test_spawned_children_are_utf8_isolated(self):
        """子 Python 必须显式 -X utf8：-I 会忽略 PYTHONUTF8，cp936 控制台会乱码/解码失败。"""
        source = Path(e2e_env.__file__).read_text(encoding="utf-8")
        # 所有 python 启动点都同时带 -X utf8 与 -I。
        for marker in ('"-X", "utf8", "-I"',):
            self.assertIn(marker, source)

    def test_live_process_blocks_dir_removal(self):
        """终止后进程仍在（模拟 SIGTERM 被忽略）：必须保留目录、返回非零。"""
        import subprocess
        import tempfile
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            nonce = "n" * 32
            e2e_env.prepare_output_dir(out, "abc123", nonce)
            (out / "manifest.json").write_text(json.dumps({
                "owner": "abc123", "nonce": nonce, "pids": {},
            }), encoding="utf-8")
            # 起一个真实子进程，但 manifest 里的 nonce 与它命令行不符 → 会被拒绝，
            # 于是目录必须保留（证明"进程还在就不删目录"这条路径）。
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

            def _reap() -> None:
                child.kill()
                child.wait(timeout=10)

            self.addCleanup(_reap)
            (out / "manifest.json").write_text(json.dumps({
                "owner": "abc123", "nonce": nonce,
                "pids": {"worker": child.pid},
            }), encoding="utf-8")
            code = e2e_env.cmd_down(Namespace(out=str(out), wait=2.0))
            self.assertEqual(code, 4)
            self.assertTrue(out.exists())
            self.assertIsNone(child.poll(), "非本次进程绝不能被杀")


class ContainerCleanupTests(unittest.TestCase):
    """崩溃残留清理：容器只能按本次 docker run 返回的 ID + owner/nonce 删除。"""

    nonce = "0123456789abcdef0123456789abcdef"

    def _fake_docker(self, outcomes):
        calls = []

        def fake(args, timeout=120.0):
            calls.append(list(args))
            for prefix, result in outcomes:
                if args[:len(prefix)] == list(prefix):
                    return result
            import subprocess
            return subprocess.CompletedProcess(args, 1, "", "no such container")

        return fake, calls

    @staticmethod
    def _cp(args, code, stdout="", stderr=""):
        import subprocess
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    def test_foreign_labels_refuse_remove(self):
        manifest_container = {"id": "cid1", "name": "ragkb-e2e-abc123"}
        fake, calls = self._fake_docker([
            (("inspect",), self._cp([], 0, json.dumps({e2e_env.OWNER_LABEL: "someoneelse"}))),
        ])
        with patch.object(e2e_env, "_docker", fake):
            ok = e2e_env._remove_owned_container(manifest_container, "abc123", self.nonce)
        self.assertFalse(ok)
        self.assertFalse(any(call[0] == "rm" for call in calls), "标签不符绝不能 docker rm")

    def test_nonce_mismatch_refuses_remove(self):
        labels = {e2e_env.OWNER_LABEL: "abc123", e2e_env.NONCE_LABEL: "f" * 32}
        fake, calls = self._fake_docker([
            (("inspect",), self._cp([], 0, json.dumps(labels))),
        ])
        with patch.object(e2e_env, "_docker", fake):
            ok = e2e_env._remove_owned_container({"id": "cid1", "name": "ragkb-e2e-abc123"},
                                                 "abc123", self.nonce)
        self.assertFalse(ok)
        self.assertFalse(any(call[0] == "rm" for call in calls))

    def test_matching_id_labels_are_removed_and_verified_gone(self):
        labels = {e2e_env.OWNER_LABEL: "abc123", e2e_env.NONCE_LABEL: self.nonce}
        calls = []

        def fake(args, timeout=120.0):
            calls.append(list(args))
            if args[0] == "rm":
                return self._cp(args, 0)
            # 第一次 inspect 读标签；删除后的复查必须"不存在"（返回非零）。
            if sum(1 for call in calls if call[0] == "inspect") == 1:
                return self._cp(args, 0, json.dumps(labels))
            return self._cp(args, 1, "", "no such container")

        with patch.object(e2e_env, "_docker", fake):
            ok = e2e_env._remove_owned_container({"id": "cid1", "name": "ragkb-e2e-abc123"},
                                                 "abc123", self.nonce)
        self.assertTrue(ok)
        self.assertTrue(any(call[0] == "rm" and "cid1" in call for call in calls))

    def test_rm_success_but_container_still_present_is_failure(self):
        # rm 返回 0 却仍 inspect 得到 ID（竞态/同名新容器）：不能谎报已清理。
        labels = {e2e_env.OWNER_LABEL: "abc123", e2e_env.NONCE_LABEL: self.nonce}
        calls = []

        def fake(args, timeout=120.0):
            calls.append(list(args))
            if args[0] == "rm":
                return self._cp(args, 0)
            return self._cp(args, 0, json.dumps(labels))

        with patch.object(e2e_env, "_docker", fake):
            ok = e2e_env._remove_owned_container({"id": "cid1", "name": "ragkb-e2e-abc123"},
                                                 "abc123", self.nonce)
        self.assertFalse(ok)

    def test_rm_failure_is_reported_not_swallowed(self):
        labels = {e2e_env.OWNER_LABEL: "abc123", e2e_env.NONCE_LABEL: self.nonce}
        fake, _calls = self._fake_docker([
            (("inspect",), self._cp([], 0, json.dumps(labels))),
            (("rm",), self._cp([], 1, "", "daemon busy")),
        ])
        with patch.object(e2e_env, "_docker", fake):
            ok = e2e_env._remove_owned_container({"id": "cid1", "name": "ragkb-e2e-abc123"},
                                                 "abc123", self.nonce)
        self.assertFalse(ok)

    def test_inspect_daemon_failure_is_not_treated_as_absent(self):
        fake, calls = self._fake_docker([
            (("inspect",), self._cp([], 1, "", "Cannot connect to the Docker daemon")),
        ])
        with patch.object(e2e_env, "_docker", fake):
            ok = e2e_env._remove_owned_container({"id": "cid1", "name": "ragkb-e2e-abc123"},
                                                 "abc123", self.nonce)
        self.assertFalse(ok, "daemon 故障不等同于容器已删除")
        self.assertFalse(any(call[0] == "rm" for call in calls))

    def test_up_never_deletes_existing_container(self):
        """up 发现同名容器：直接失败，且不发出任何 stop/rm。"""
        orchestrator = e2e_env.Orchestrator(out_dir=Path("unused"), token="abc123",
                                            nonce=self.nonce)
        calls = []

        def fake(args, timeout=120.0):
            calls.append(list(args))
            return self._cp(args, 0, "ragkb-e2e-abc123\n" if args[0] == "ps" else "")

        with patch.object(e2e_env, "_docker", fake):
            with self.assertRaises(RuntimeError):
                orchestrator._assert_container_slot_free(e2e_env.container_name("abc123"))
        self.assertEqual(orchestrator.container_id, "")
        self.assertFalse(any(call[0] in {"stop", "rm", "kill"} for call in calls))

    def test_stop_without_created_container_does_nothing(self):
        orchestrator = e2e_env.Orchestrator(out_dir=Path("unused"), token="abc123",
                                            nonce=self.nonce)
        calls = []

        def fake(args, timeout=120.0):
            calls.append(list(args))
            return self._cp(args, 0)

        with patch.object(e2e_env, "_docker", fake):
            failures = orchestrator._stop_container()
        self.assertEqual(failures, [])
        self.assertEqual(calls, [], "没成功 docker run 过，就不应该有容器操作")


class AttestationTests(unittest.TestCase):
    """API 身份证明：只有 owner+nonce+stub 全部匹配才算本次环境。"""

    owner = "abc123"
    nonce = "0123456789abcdef0123456789abcdef"
    stub_url = "http://127.0.0.1:9000/v1"

    def test_matching_payload_passes(self):
        payload = e2e_env.attestation_payload(self.owner, self.nonce, self.stub_url)
        e2e_env.verify_attestation(payload, owner=self.owner, nonce=self.nonce,
                                   stub_base_url=self.stub_url)

    def test_wrong_owner_or_nonce_or_stub_fails(self):
        payload = e2e_env.attestation_payload(self.owner, self.nonce, self.stub_url)
        cases = [
            dict(payload, owner="other1"),
            dict(payload, nonce="f" * 32),
            dict(payload, stub_base_url="http://127.0.0.1:9001/v1"),
            dict(payload, schema="something-else"),
        ]
        for case in cases:
            with self.assertRaises(ValueError):
                e2e_env.verify_attestation(case, owner=self.owner, nonce=self.nonce,
                                           stub_base_url=self.stub_url)
        with self.assertRaises(ValueError):
            e2e_env.verify_attestation("not-a-dict", owner=self.owner, nonce=self.nonce,
                                       stub_base_url=self.stub_url)

    def test_stub_state_requires_owner_and_nonce(self):
        e2e_env.verify_stub_owner({"owner": self.owner, "nonce": self.nonce},
                                  owner=self.owner, nonce=self.nonce)
        with self.assertRaises(ValueError):
            e2e_env.verify_stub_owner({"owner": "other1", "nonce": self.nonce},
                                      owner=self.owner, nonce=self.nonce)
        with self.assertRaises(ValueError):
            e2e_env.verify_stub_owner({"owner": self.owner, "nonce": "f" * 32},
                                      owner=self.owner, nonce=self.nonce)
        with self.assertRaises(ValueError):
            e2e_env.verify_stub_owner({"counts": {}}, owner=self.owner, nonce=self.nonce)

    def test_attested_app_boots_real_app_and_serves_only_attestation(self):
        """boot 脚本必须转交真实 app；只额外应答一个只读路径。"""
        self.assertIn("from app.main import app as real_app", e2e_env.ATTEST_BOOT)
        self.assertIn('scope.get("path") == "/__e2e__/attestation"', e2e_env.ATTEST_BOOT)
        self.assertIn("await self.inner(scope, receive, send)", e2e_env.ATTEST_BOOT)
        # 不得改写业务 app 或注册到生产入口
        self.assertNotIn("add_api_route", e2e_env.ATTEST_BOOT)


class CapacityLimitTests(unittest.TestCase):
    def test_defaults_are_small(self):
        limits = e2e_capacity.Limits.build(
            e2e_capacity.DEFAULT_CONCURRENCY,
            e2e_capacity.DEFAULT_REQUESTS,
            e2e_capacity.DEFAULT_DURATION,
            20.0,
        )
        self.assertLessEqual(limits.concurrency, 8)
        self.assertLessEqual(limits.requests, 40)
        self.assertLessEqual(limits.duration, 30)

    def test_hard_limits_reject_oversized_samples(self):
        with self.assertRaises(e2e_capacity.ConfigError):
            e2e_capacity.Limits.build(e2e_capacity.HARD_MAX_CONCURRENCY + 1, 40, 10, 20)
        with self.assertRaises(e2e_capacity.ConfigError):
            e2e_capacity.Limits.build(2, e2e_capacity.HARD_MAX_REQUESTS + 1, 10, 20)
        with self.assertRaises(e2e_capacity.ConfigError):
            e2e_capacity.Limits.build(2, 4, e2e_capacity.HARD_MAX_DURATION + 1, 20)
        with self.assertRaises(e2e_capacity.ConfigError):
            e2e_capacity.Limits.build(0, 4, 10, 20)

    def test_non_finite_and_wrong_typed_limits_are_rejected(self):
        # 复现主审案例：nan 能绕过 `value <= 0` 与 `value > ceiling` 两道比较。
        for bad in (float("nan"), float("inf"), float("-inf"), 0, -1):
            with self.assertRaises(e2e_capacity.ConfigError):
                e2e_capacity.Limits.build(1, 1, bad, 0.2)
            with self.assertRaises(e2e_capacity.ConfigError):
                e2e_capacity.Limits.build(1, 1, 1.0, bad)
        for bad in (1.5, "4", True, None):
            with self.assertRaises(e2e_capacity.ConfigError):
                e2e_capacity.Limits.build(bad, 4, 1.0, 1.0)

    def test_percentile_uses_observed_values(self):
        values = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(e2e_capacity.percentile(values, 0.5), 20.0)
        self.assertEqual(e2e_capacity.percentile(values, 0.95), 40.0)
        self.assertIsNone(e2e_capacity.percentile([], 0.5))


class CapacityDeadlineTests(unittest.TestCase):
    """真实本地 HTTP：慢流水只能拖住"总时长硬上限"之外的一点点。"""

    def _serve(self, handler_cls):
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # 收口顺序必须是 shutdown -> join -> server_close。用 addCleanup 的 LIFO
        # 很容易变成 close 先于 shutdown，serve_forever 的 select 会在 Windows
        # 抛 WinError 10038，还可能留下仍在跑的线程。
        self.addCleanup(self._shutdown, server, thread)
        return server

    @staticmethod
    def _shutdown(server, thread: threading.Thread) -> None:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    def _run(self, handler_cls, *, duration: float, request_timeout: float):
        server = self._serve(handler_cls)
        port = server.server_address[1]
        started = time.monotonic()
        sample = e2e_capacity._post_stream(
            f"http://127.0.0.1:{port}", "token",
            {"_index": 0, "question": "q", "mode": "chat", "kb_id": None, "request_id": "r"},
            deadline=time.monotonic() + duration,
            request_timeout=request_timeout,
        )
        return sample, time.monotonic() - started

    def test_heartbeat_slow_stream_respects_hard_deadline(self):
        """每 20ms 一个心跳、1.2s 才结束：总时长 0.1s 时必须提前收口并计 timeout。"""
        from http.server import BaseHTTPRequestHandler

        class Heartbeat(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for _ in range(60):  # 60 × 20ms ≈ 1.2s
                        payload = b": keep-alive\n\n"
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(payload), payload))
                        self.wfile.flush()
                        time.sleep(0.02)
                    final = b"event: done\ndata: {}\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(final), final))
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                        OSError):
                    # 客户端到点主动断开是预期行为：安静收场，别让 traceback 逃出 unittest。
                    pass

            def log_message(self, *args):  # noqa: A003
                pass

        sample, elapsed = self._run(Heartbeat, duration=0.1, request_timeout=0.2)
        self.assertLess(elapsed, 0.9, "慢流必须被总时长硬上限截断，而不是等它自己结束")
        self.assertEqual(sample.outcome, "error")
        self.assertIsNotNone(sample.error)
        self.assertIn("timeout", sample.error)
        self.assertIsNotNone(sample.first_event_ms, "心跳应先到达，证明是流内超时而非连接失败")

    def test_slow_headers_respect_hard_deadline(self):
        """响应头每 20ms 只吐一个字节且不含 CRLF：也必须被总期限截断。"""
        from http.server import BaseHTTPRequestHandler

        class SlowHeaders(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                try:
                    for chunk in (b"HTTP/1.1 200 OK\r\nContent-Type: ",
                                  b"text/event-stream\r\n",
                                  b"X-Slow: " + b"a" * 2):
                        for byte in chunk:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.02)
                    time.sleep(1.2)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def log_message(self, *args):  # noqa: A003
                pass

        sample, elapsed = self._run(SlowHeaders, duration=0.1, request_timeout=0.2)
        self.assertLess(elapsed, 0.9, "慢响应头不能拖过硬期限")
        self.assertEqual(sample.outcome, "error")
        self.assertIn("timeout", sample.error)

    def test_no_newline_byte_drip_respects_hard_deadline(self):
        """响应头已结束，但随后每 20ms 一个字节、永不换行：不能被拖穿。"""
        from http.server import BaseHTTPRequestHandler

        class Drip(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for _ in range(60):
                        self.wfile.write(b"a")  # 没有换行，readline 永远不返回
                        self.wfile.flush()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def log_message(self, *args):  # noqa: A003
                pass

        sample, elapsed = self._run(Drip, duration=0.1, request_timeout=0.2)
        self.assertLess(elapsed, 0.9, "无换行滴字节不能拖过硬期限")
        self.assertEqual(sample.outcome, "error")
        self.assertIn("timeout", sample.error)

    def test_slow_chunk_metadata_respects_hard_deadline(self):
        """合法 chunk 之后，chunk-size 行每 20ms 一个字节且不换行。"""
        from http.server import BaseHTTPRequestHandler

        class SlowChunkMeta(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    first = b": ok\n\n"
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(first), first))
                    self.wfile.flush()
                    for _ in range(60):
                        self.wfile.write(b"1")  # 慢 chunk-size，永不换行
                        self.wfile.flush()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def log_message(self, *args):  # noqa: A003
                pass

        sample, elapsed = self._run(SlowChunkMeta, duration=0.1, request_timeout=0.2)
        self.assertLess(elapsed, 0.9, "慢 chunk 元数据不能拖过硬期限")
        self.assertEqual(sample.outcome, "error")
        self.assertIn("timeout", sample.error)
        self.assertIsNotNone(sample.first_event_ms, "首个合法 chunk 应先被读到")

    def test_no_task_keeps_posting_after_return(self):
        """返回后不能还有协程/线程继续 POST：探测结束后模型调用数不再增长。"""
        from http.server import BaseHTTPRequestHandler

        hits: list[float] = []

        class Counting(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                hits.append(time.monotonic())
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for _ in range(60):
                        payload = b": keep-alive\n\n"
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(payload), payload))
                        self.wfile.flush()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def log_message(self, *args):  # noqa: A003
                pass

        sample, _elapsed = self._run(Counting, duration=0.1, request_timeout=0.2)
        self.assertEqual(sample.outcome, "error")
        before = len(hits)
        time.sleep(0.6)
        self.assertEqual(len(hits), before, "返回后不允许再有请求到达服务端")
        self.assertEqual(before, 1)

    def test_missing_attestation_means_zero_chat_posts(self):
        """错 owner / 只有 /health 的 API：必须在登录前拒绝，零聊天 POST。"""
        from http.server import BaseHTTPRequestHandler

        hits: list[str] = []

        class OnlyHealth(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(f"GET {self.path}")
                if self.path == "/health":
                    body = b'{"status":"ok"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self):  # noqa: N802
                hits.append(f"POST {self.path}")
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # noqa: A003
                pass

        server = self._serve(OnlyHealth)
        port = server.server_address[1]
        manifest = {
            "owner": "abc123",
            "nonce": "0123456789abcdef0123456789abcdef",
            "api": {"base_url": f"http://127.0.0.1:{port}"},
            "stub": {"base_url": f"http://127.0.0.1:{port}/v1"},
            "attestation": {
                "api_url": f"http://127.0.0.1:{port}/__e2e__/attestation",
                "stub_state_url": f"http://127.0.0.1:{port}/__control/state",
            },
        }
        with self.assertRaises(e2e_capacity.ConfigError):
            e2e_capacity.verify_environment(manifest)
        self.assertFalse([hit for hit in hits if hit.startswith("POST")],
                         "校验失败时不允许发出任何 POST（尤其聊天）")
        self.assertFalse([hit for hit in hits if "chat" in hit])


class AttestationBindingTests(unittest.TestCase):
    """attestation 必须由实际 POST 目标派生，不能独立声明。"""

    nonce = "0123456789abcdef0123456789abcdef"
    stub = "http://127.0.0.1:9100/v1"

    def _manifest(self, api: str, *, attestation_api: str | None = None,
                  stub: str | None = None) -> dict:
        stub_base = stub or self.stub
        manifest = {
            "owner": "abc123",
            "nonce": self.nonce,
            "api": {"base_url": api},
            "stub": {"base_url": stub_base},
            "attestation": {
                "api_url": attestation_api if attestation_api is not None
                else api.rstrip("/") + "/__e2e__/attestation",
                "stub_state_url": stub_base.rsplit("/v1", 1)[0] + "/__control/state",
            },
        }
        return manifest

    def test_attestation_derives_from_post_target(self):
        manifest = self._manifest("http://127.0.0.1:9200")
        self.assertEqual(e2e_capacity.attestation_url(manifest),
                         "http://127.0.0.1:9200/__e2e__/attestation")

    def test_independent_attestation_url_is_rejected(self):
        # 复现主审案例：attestation 指向 A，api.base_url 指向 B，health 也是 B 的。
        manifest = self._manifest("http://127.0.0.1:9201",
                                  attestation_api="http://127.0.0.1:9200/__e2e__/attestation")
        with self.assertRaises(e2e_capacity.ConfigError) as ctx:
            e2e_capacity.attestation_url(manifest)
        self.assertIn("不一致", str(ctx.exception))

    def test_normalized_equivalent_url_is_allowed(self):
        manifest = self._manifest("http://127.0.0.1:9200")
        manifest["attestation"]["api_url"] = "http://127.0.0.1:9200/__e2e__/attestation"
        self.assertEqual(e2e_capacity.attestation_url(manifest),
                         "http://127.0.0.1:9200/__e2e__/attestation")

    def test_stub_state_url_is_rejected_when_independently_declared(self):
        manifest = self._manifest("http://127.0.0.1:9200")
        manifest["attestation"]["stub_state_url"] = "http://127.0.0.1:9999/__control/state"
        with self.assertRaises(e2e_capacity.ConfigError):
            e2e_capacity.stub_state_url(manifest)

    def test_verify_environment_refuses_before_any_post_when_mismatched(self):
        manifest = self._manifest("http://127.0.0.1:9201",
                                  attestation_api="http://127.0.0.1:9200/__e2e__/attestation")
        with self.assertRaises(e2e_capacity.ConfigError):
            e2e_capacity.verify_environment(manifest)


class StubVectorTests(unittest.TestCase):
    def test_embedding_is_deterministic_and_normalized(self):
        first = stub.embedding_vector("合成数据平台 质量校验 导出")
        second = stub.embedding_vector("合成数据平台 质量校验 导出")
        self.assertEqual(first, second)
        self.assertEqual(len(first), stub.EMBEDDING_DIM)
        norm = sum(value * value for value in first) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_empty_text_still_returns_unit_vector(self):
        vector = stub.embedding_vector("")
        self.assertEqual(len(vector), stub.EMBEDDING_DIM)
        self.assertAlmostEqual(vector[0], 1.0)

    def test_similar_text_scores_higher_than_unrelated(self):
        base = stub.embedding_vector("导出前必须完成质量校验")
        near = stub.embedding_vector("质量校验 包括字段完整性检查")
        far = stub.embedding_vector("今天天气很好适合出门散步")

        def cosine(a, b):
            return sum(x * y for x, y in zip(a, b))
        self.assertGreater(cosine(base, near), cosine(base, far))

    @staticmethod
    def _prompt(blocks: str, question: str) -> str:
        """与 ``app/services/rag.py`` 的提示词同形状：参考资料 + 用户问题。"""
        return f"【参考资料】\n{blocks}\n\n【用户问题】\n{question}"

    def test_answer_only_cites_blocks_present_in_context(self):
        blocks = (
            "[1] 来源：manual.txt（位置：第 1 段）\n导出前必须完成质量校验。\n\n---\n\n"
            "[2] 来源：other.txt（位置：第 2 段）\n质量校验包括字段完整性检查和去重检查。"
        )
        answer, used = stub.build_answer(self._prompt(blocks, "导出前需要完成什么校验？"))
        self.assertIn("[1]", answer)
        self.assertIn("[2]", answer)
        self.assertNotIn("[3]", answer)
        # 每一个 [n] 都必须对应真实存在的资料块编号
        import re
        cited = {int(m) for m in re.findall(r"\[(\d+)\]", answer)}
        self.assertTrue(cited <= {1, 2})
        self.assertEqual(cited, set(used))

    def test_citations_follow_block_numbers_even_when_gaps_exist(self):
        # 检索只返回第 2、5 块时，替身不能把它们重编号成 [1][2]。
        blocks = (
            "[2] 来源：b.txt（位置：第 1 段）\n导出前必须完成质量校验。\n\n---\n\n"
            "[5] 来源：e.txt（位置：第 2 段）\n质量校验包括字段完整性检查。"
        )
        answer, used = stub.build_answer(self._prompt(blocks, "导出前需要完成什么校验？"))
        cited = {int(m) for m in __import__("re").findall(r"\[(\d+)\]", answer)}
        self.assertEqual(cited, set(used))
        self.assertTrue(cited <= {2, 5})
        self.assertNotIn("[1]", answer)

    def test_no_context_answer_has_no_fake_citation(self):
        answer, used = stub.build_answer("你好")
        self.assertNotIn("[1]", answer)
        self.assertNotIn("来源：", answer)
        self.assertEqual(used, [])

    def test_refuses_when_context_has_no_relevant_sentence(self):
        # 词元完全不重叠（纯英文句子 vs 中文问题）时，替身必须明确拒答，
        # 而不是把不相干的句子塞上 [1] 冒充引用。
        blocks = "[1] 来源：weather.txt（位置：第 1 段）\nRiverbank erosion survey results."
        answer, used = stub.build_answer(self._prompt(blocks, "服务器的备份保留多少天？"))
        self.assertEqual(answer, stub._REFUSAL)
        self.assertEqual(used, [])


class StubHttpTests(unittest.TestCase):
    """真起一次回环 HTTP，验证 OpenAI 协议形状（不连数据库、不连真实模型）。"""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        stub.STATE.reset()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), stub.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        stub.STATE.reset()

    def setUp(self):
        stub.STATE.reset()

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(request, timeout=15)

    def test_embeddings_return_one_vector_per_input_with_usage(self):
        with self._post("/v1/embeddings", {"model": "e2e", "input": ["甲", "乙 丙"], "encoding_format": "float"}) as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual([item["index"] for item in body["data"]], [0, 1])
        self.assertEqual(len(body["data"][0]["embedding"]), stub.EMBEDDING_DIM)
        self.assertGreater(body["usage"]["prompt_tokens"], 0)

    def test_stream_has_tokens_finish_usage_and_done(self):
        prompt = "【参考资料】\n[1] 来源：manual.txt（位置：第 1 段）\n导出前必须完成质量校验。\n\n【用户问题】\n导出前需要完成什么校验？"
        payload = {
            "model": "e2e-chat", "stream": True,
            "messages": [{"role": "system", "content": "你是企业知识库助手"},
                         {"role": "user", "content": prompt}],
        }
        frames = []
        with self._post("/v1/chat/completions", payload) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    frames.append("[DONE]")
                    break
                frames.append(json.loads(data))
        # 顺序契约：若干 token 帧 -> 带 finish_reason 的帧 -> usage 帧 -> [DONE]
        self.assertEqual(frames[-1], "[DONE]")
        usage_frame = frames[-2]
        self.assertTrue(usage_frame["choices"] == [])
        self.assertIn("usage", usage_frame)
        self.assertGreater(usage_frame["usage"]["total_tokens"], 0)
        deltas = [f["choices"][0]["delta"].get("content") for f in frames
                  if isinstance(f, dict) and f.get("choices") and f["choices"][0].get("delta", {}).get("content")]
        self.assertTrue(deltas)
        text = "".join(deltas)
        self.assertIn("[1]", text)
        finish = [f["choices"][0]["finish_reason"] for f in frames
                  if isinstance(f, dict) and f.get("choices") and f["choices"][0].get("finish_reason")]
        self.assertEqual(finish, ["stop"])
        self.assertEqual(stub.STATE.snapshot()["counts"]["chat_stream_calls"], 1)

    def test_json_completion_returns_single_message_without_stream(self):
        payload = {"model": "e2e-chat", "messages": [{"role": "user", "content": "你好"}]}
        with self._post("/v1/chat/completions", payload) as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(body["object"], "chat.completion")
        self.assertTrue(body["choices"][0]["message"]["content"])
        self.assertIn("usage", body)
        self.assertEqual(stub.STATE.snapshot()["counts"]["chat_json_calls"], 1)

    def test_truncated_stream_reports_length_finish(self):
        with self._post("/__control/config", {"chat_truncate": True}) as resp:
            resp.read()
        payload = {"model": "e2e-chat", "stream": True,
                   "messages": [{"role": "user", "content": "你好"}]}
        finish = []
        with self._post("/v1/chat/completions", payload) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data: ") or line.endswith("[DONE]"):
                    continue
                frame = json.loads(line[len("data: "):])
                if frame.get("choices") and frame["choices"][0].get("finish_reason"):
                    finish.append(frame["choices"][0]["finish_reason"])
        self.assertEqual(finish, ["length"])
        self.assertEqual(stub.STATE.snapshot()["counts"]["chat_truncated"], 1)

    def test_control_endpoint_rejects_unknown_keys(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            with self._post("/__control/config", {"not_a_real_knob": 1}) as resp:
                resp.read()
        self.assertEqual(ctx.exception.code, 500)

    def test_injected_failure_returns_configurable_status(self):
        with self._post("/__control/config", {"chat_fail_next": 1, "chat_fail_status": 503}) as resp:
            self.assertEqual(resp.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            with self._post("/v1/chat/completions", {"model": "e2e", "messages": [{"role": "user", "content": "hi"}]}) as resp:
                resp.read()
        self.assertEqual(ctx.exception.code, 503)
        with self._post("/__control/reset", {}) as resp:
            resp.read()


if __name__ == "__main__":
    unittest.main()
