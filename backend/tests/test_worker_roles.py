"""worker --role 契约：默认 ingest 兼容、summary/project 懒加载分发（纯单测，不连库）。"""
import threading
import unittest
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

    def test_invalid_role_rejected(self):
        with self.assertRaises(SystemExit):
            worker.parse_role(["--role", "bogus"])

    def test_roles_and_module_map_are_stable(self):
        self.assertEqual(worker.ROLES, ("ingest", "summary", "project"))
        # 只有被选中的角色才被 import；缺 tree-sitter 等依赖不能影响其他角色。
        self.assertEqual(worker._ROLE_WORKERS["summary"], ("app.services", "memory_worker"))
        self.assertEqual(worker._ROLE_WORKERS["project"], ("app.services", "project_worker"))
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

    def test_run_role_calls_run_with_stop_event_and_returns(self):
        # 角色 supervisor：setup_logging -> init_db(create_admin=False) -> module.run(stop_event)。
        ran = {}
        fake = SimpleNamespace(run=lambda ev: ran.setdefault("ev", ev))

        def fake_import(role):
            return fake

        with patch.object(worker, "setup_logging") as log, \
             patch.object(worker, "init_db") as init, \
             patch.object(worker.signal, "signal") as sig, \
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
