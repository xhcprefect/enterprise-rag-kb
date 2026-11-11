"""worker --role 契约：默认 ingest 兼容、summary/project/regression 懒加载分发（纯单测，不连库）。"""
import contextlib
import io
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import worker


class WorkerRoleParseTests(unittest.TestCase):
    def test_default_role_is_ingest(self):
        # parse_role(None) 走 argparse 默认 sys.argv；unittest discovery 会往 sys.argv
        # 塞自己的参数（-q、模块名等）。测试里隔离 sys.argv，避免把 discovery 参数
        # 当成 worker 启动参数触发 SystemExit(2)。CLI 读取默认 sys.argv 的语义保持不变。
        with patch.object(worker.sys, "argv", ["worker.py"]):
            self.assertEqual(worker.parse_role(None), worker.ROLE_INGEST)
        self.assertEqual(worker.parse_role([]), worker.ROLE_INGEST)
        for role in worker.ROLES:
            self.assertEqual(worker.parse_role(["--role", role]), role)

    def test_help_lists_all_four_roles_and_default(self):
        # --help 必须与实际 choices 同步，避免文档仍只写三个角色。
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            worker.parse_role(["--help"])
        text = output.getvalue()
        for role in ("ingest", "summary", "project", "regression"):
            self.assertIn(role, text)
        self.assertIn("默认", text)

    def test_invalid_role_rejected(self):
        with self.assertRaises(SystemExit):
            worker.parse_role(["--role", "bogus"])

    def test_roles_and_module_map_are_stable(self):
        self.assertEqual(worker.ROLES, ("ingest", "summary", "project", "regression"))
        # 只有被选中的角色才被 import；缺 tree-sitter 等依赖不能影响其他角色。
        self.assertEqual(worker._ROLE_WORKERS["summary"], ("app.services", "memory_worker"))
        self.assertEqual(worker._ROLE_WORKERS["project"], ("app.services", "project_worker"))
        self.assertEqual(worker._ROLE_WORKERS["regression"], ("app.services", "regression_worker"))
        self.assertNotIn("ingest", worker._ROLE_WORKERS)


class WorkerRoleDispatchTests(unittest.TestCase):
    def test_default_main_runs_ingest_not_role(self):
        # 默认角色走旧 ingest 路径，不碰 summary/project 的懒加载模块。
        # parse_args([]) 为空参数 -> 走默认 --role=ingest，隔离 unittest discovery 的 sys.argv。
        with patch.object(worker, "_ingest_main", return_value=0) as ingest, \
             patch.object(worker, "_run_role") as role:
            result = worker.main([])
        self.assertEqual(result, 0)
        ingest.assert_called_once()
        role.assert_not_called()

    def test_summary_main_runs_role_with_right_module(self):
        # 验证 summary 触发 _run_role，且 _ROLE_WORKERS 指向 memory_worker。
        with patch.object(worker, "parse_role", return_value=worker.ROLE_SUMMARY), \
             patch.object(worker, "_run_role", return_value=0) as role:
            result = worker.main([])
        self.assertEqual(result, 0)
        role.assert_called_once_with(worker.ROLE_SUMMARY)

    def test_import_role_worker_targets_correct_module(self):
        # 验证 _import_role_worker 返回的模块就是 app.services.* 下的目标模块；懒加载。
        fake = SimpleNamespace(run=lambda ev: None)
        with patch.object(
            worker.importlib, "import_module", return_value=fake
        ) as imp:
            got = worker._import_role_worker(worker.ROLE_SUMMARY)
            self.assertIs(got, fake)
            imp.assert_called_once_with("app.services.memory_worker")
        with patch.object(
            worker.importlib, "import_module", return_value=fake
        ) as imp:
            worker._import_role_worker(worker.ROLE_PROJECT)
            imp.assert_called_once_with("app.services.project_worker")
        with patch.object(
            worker.importlib, "import_module", return_value=fake
        ) as imp:
            worker._import_role_worker(worker.ROLE_REGRESSION)
            imp.assert_called_once_with("app.services.regression_worker")

    def test_missing_regression_module_is_only_reached_for_that_role(self):
        # regression 执行器暂未提供时，import 只会在选择该角色时失败；
        # ingest/summary/project 的启动路径都不会触碰它。
        fake = SimpleNamespace(run=lambda ev: None)
        with patch.object(worker.importlib, "import_module",
                          side_effect=ModuleNotFoundError("app.services.regression_worker")) as imp:
            with self.assertRaises(ModuleNotFoundError):
                worker._import_role_worker(worker.ROLE_REGRESSION)
        imp.assert_called_once_with("app.services.regression_worker")
        with patch.object(worker.importlib, "import_module", return_value=fake) as imp:
            worker._import_role_worker(worker.ROLE_SUMMARY)
        imp.assert_called_once_with("app.services.memory_worker")

    def test_regression_main_runs_role_with_regression_module(self):
        with patch.object(worker, "parse_role", return_value=worker.ROLE_REGRESSION), \
             patch.object(worker, "_run_role", return_value=0) as role:
            result = worker.main(["worker.py", "--role", "regression"])
        self.assertEqual(result, 0)
        role.assert_called_once_with(worker.ROLE_REGRESSION)

    def test_run_role_calls_run_with_stop_event_and_returns(self):
        # 角色 supervisor：setup_logging -> init_db(create_admin=False) -> module.run(stop_event)。
        ran = {}
        fake = SimpleNamespace(run=lambda ev: ran.setdefault("ev", ev))

        def fake_import(role):
            return fake

        with patch.object(worker, "setup_logging") as log, \
             patch.object(worker, "init_db") as init, \
             patch.object(worker.signal, "signal") as sig, \
             patch.object(worker, "_start_heartbeat", return_value="hb"), \
             patch.object(worker, "_stop_heartbeat"), \
             patch.object(worker, "_import_role_worker", side_effect=fake_import):
            code = worker._run_role(worker.ROLE_SUMMARY)
        self.assertEqual(code, 0)
        init.assert_called_once_with(create_admin=False)
        self.assertIs(ran["ev"], worker.stop_event)
        log.assert_called_once_with("worker-summary")
        self.assertEqual(sig.call_count, 2)

    def test_roles_share_single_stop_event(self):
        # 同一角色独立启动，但全部复用一个进程级 stop_event，便于统一停机。
        self.assertIsInstance(worker.stop_event, threading.Event)

    def test_ingest_entry_still_has_old_signal_and_event_members(self):
        # 旧入口兼容：stop_event / _running / _ingest_main 存量成员仍在。
        self.assertTrue(hasattr(worker, "_running"))
        self.assertTrue(hasattr(worker, "stop_event"))
        self.assertTrue(callable(worker._ingest_main))


class WorkerHeartbeatWiringTests(unittest.TestCase):
    """每个角色都在主循环前启动进程心跳，退出时有界停止；观测失败不影响角色。"""

    def test_all_roles_start_heartbeat_with_their_own_role(self):
        for role in worker.ROLES:
            with self.subTest(role=role):
                fake = SimpleNamespace(run=lambda ev: None)
                with patch.object(worker, "setup_logging"),                      patch.object(worker, "init_db"),                      patch.object(worker.signal, "signal"),                      patch.object(worker, "_start_heartbeat", return_value="hb") as start,                      patch.object(worker, "_stop_heartbeat") as stop,                      patch.object(worker, "_import_role_worker", return_value=fake):
                    self.assertEqual(worker._run_role(role), 0)
                start.assert_called_once_with(role)
                stop.assert_called_once_with("hb")

    def test_heartbeat_stops_even_when_role_run_raises(self):
        def boom(_ev):
            raise RuntimeError("boom")

        fake = SimpleNamespace(run=boom)
        with patch.object(worker, "setup_logging"),              patch.object(worker, "init_db"),              patch.object(worker.signal, "signal"),              patch.object(worker, "_start_heartbeat", return_value="hb"),              patch.object(worker, "_stop_heartbeat") as stop,              patch.object(worker, "_import_role_worker", return_value=fake):
            with self.assertRaises(RuntimeError):
                worker._run_role(worker.ROLE_PROJECT)
        stop.assert_called_once_with("hb")

    def test_heartbeat_start_failure_does_not_block_role(self):
        ran = []
        fake = SimpleNamespace(run=lambda ev: ran.append(ev))
        with patch.object(worker, "setup_logging"),              patch.object(worker, "init_db"),              patch.object(worker.signal, "signal"),              patch.object(worker, "_start_heartbeat", return_value=None),              patch.object(worker, "_stop_heartbeat") as stop,              patch.object(worker, "_import_role_worker", return_value=fake):
            self.assertEqual(worker._run_role(worker.ROLE_SUMMARY), 0)
        self.assertTrue(ran)
        stop.assert_called_once_with(None)

    def test_stop_heartbeat_tolerates_none_and_errors(self):
        worker._stop_heartbeat(None)  # 无 reporter 时是 no-op
        with patch.object(worker.worker_status, "stop_worker_heartbeat",
                          side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                worker._stop_heartbeat("hb")


class StartWorkerBatchTests(unittest.TestCase):
    """start-worker.bat 与 CLI 角色清单保持同步；不带参数仍是 ingest。"""

    BAT = Path(__file__).resolve().parents[1] / "start-worker.bat"

    def test_batch_role_list_matches_cli(self):
        text = self.BAT.read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines()]
        self.assertIn("for %%r in (" + " ".join(worker.ROLES) + ")", text)
        # 旧用法（无参数）仍走 ingest：命令行不带 --role。
        self.assertIn('".venv\\Scripts\\python.exe" -X utf8 worker.py', lines)
        # 新角色通过 --role 传入，复用同一个 CLI 校验。
        self.assertIn('".venv\\Scripts\\python.exe" -X utf8 worker.py --role %ROLE%', lines)

    def test_batch_normalizes_case_insensitive_role_before_handing_to_argparse(self):
        # 白名单可用 /i 接受 REGRESSION/Summary，但 worker.py 的 choices 严格小写；
        # 必须把 ROLE 改写成命中的固定小写角色，否则已接受的参数会以 invalid choice 退出。
        text = self.BAT.read_text(encoding="utf-8")
        self.assertIn('if /i "%ROLE%"=="%%r" (set "KNOWN=1" & set "ROLE=%%r")', text)
        # 规范化发生在 whitelist 循环内、argparse 调用之前。
        whitelist = text.index('for %%r in (ingest summary project regression)')
        invoke = text.index("worker.py --role %ROLE%")
        self.assertLess(whitelist, invoke)


class ProjectWorkerAutoSyncTests(unittest.TestCase):
    def test_auto_sync_runs_each_round_and_error_does_not_kill_worker(self):
        # project worker 每轮（≤ AUTO_SYNC_POLL_SECONDS）尝试一次到期自动同步；
        # 调度异常只记录，supervisor 继续走到领取/空闲等待并可按 stop_event 退出。
        from app.services import project_auto_sync, project_index, project_worker

        stop = threading.Event()
        calls = []

        def boom(limit=1):
            calls.append(limit)
            raise RuntimeError("scheduler unavailable")

        def stop_after_first_wait(_timeout):
            stop.set()
            return True

        with patch.object(project_index, "recover_stale_jobs", return_value=0), \
             patch.object(project_index, "claim_next_project_snapshot", return_value=None), \
             patch.object(project_auto_sync, "enqueue_due_sources", side_effect=boom), \
             patch.object(stop, "wait", side_effect=stop_after_first_wait):
            project_worker.run(stop)

        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
