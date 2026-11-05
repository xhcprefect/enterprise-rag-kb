"""测试环境隔离：RAG_DISABLE_DOTENV 必须真正阻止 .env 被读取。

这些用例都在独立临时目录里启动子进程，避免影响当前解释器已加载的配置。
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

_PROBE = textwrap.dedent(
    """
    import json, os, sys
    from app.core.config import DOTENV_DISABLED, Settings, settings
    print(json.dumps({
        "dotenv_disabled": DOTENV_DISABLED,
        "app_name": settings.APP_NAME,
        "embedding_base_url": settings.EMBEDDING_BASE_URL,
        "llm_base_url": settings.LLM_BASE_URL,
    }))
    """
)


def run_probe(workdir: Path, extra_env: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in {
        "APP_NAME", "EMBEDDING_BASE_URL", "LLM_BASE_URL", "DATABASE_URL", "JWT_SECRET",
        "RAG_DISABLE_DOTENV",
    }}
    env.update(extra_env)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    import json
    return json.loads(result.stdout.strip().splitlines()[-1])


class DotenvSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.temp.name)
        # 诱饵 .env：如果被读取，值会出现在子进程输出里。
        (self.workdir / ".env").write_text(
            "APP_NAME=DECOY_FROM_DOTENV\n"
            "EMBEDDING_BASE_URL=http://decoy.invalid/v1\n"
            "LLM_BASE_URL=http://decoy.invalid\n"
            "DATABASE_URL=postgresql+psycopg://decoy:decoy@127.0.0.1:9/decoy\n"
            "JWT_SECRET=decoy-secret-value\n",
            encoding="utf-8",
        )
        self.addCleanup(self.temp.cleanup)

    def test_disable_dotenv_ignores_decoy_file(self):
        data = run_probe(
            self.workdir,
            {
                "RAG_DISABLE_DOTENV": "1",
                "EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1",
                "LLM_BASE_URL": "http://127.0.0.1:9",
            },
        )
        self.assertTrue(data["dotenv_disabled"])
        self.assertNotEqual(data["app_name"], "DECOY_FROM_DOTENV")
        self.assertEqual(data["embedding_base_url"], "http://127.0.0.1:9/v1")
        self.assertEqual(data["llm_base_url"], "http://127.0.0.1:9")

    def test_without_switch_legacy_dotenv_behaviour_is_preserved(self):
        data = run_probe(self.workdir, {})
        self.assertFalse(data["dotenv_disabled"])
        self.assertEqual(data["app_name"], "DECOY_FROM_DOTENV")
        self.assertEqual(data["embedding_base_url"], "http://decoy.invalid/v1")

    def test_switch_accepts_common_truthy_spellings(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value):
                data = run_probe(
                    self.workdir,
                    {"RAG_DISABLE_DOTENV": value, "EMBEDDING_BASE_URL": "http://127.0.0.1:9/v1"},
                )
                self.assertTrue(data["dotenv_disabled"])
                self.assertNotEqual(data["app_name"], "DECOY_FROM_DOTENV")

    def test_switch_off_by_default_and_for_false_values(self):
        for value in ("", "0", "false", "no"):
            with self.subTest(value=value):
                data = run_probe(self.workdir, {"RAG_DISABLE_DOTENV": value})
                self.assertFalse(data["dotenv_disabled"])
                self.assertEqual(data["app_name"], "DECOY_FROM_DOTENV")


class LoggingSwitchTests(unittest.TestCase):
    """LOG_STDOUT_ONLY 只影响是否额外挂文件 handler，默认行为不变。"""

    def setUp(self):
        import logging

        from app.core import logging as app_logging

        self.app_logging = app_logging
        self.root = logging.getLogger()
        self.original_handlers = list(self.root.handlers)
        self.original_configured = app_logging._CONFIGURED
        self.addCleanup(self._restore)

    def _restore(self):
        for handler in list(self.root.handlers):
            if handler not in self.original_handlers:
                self.root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        self.app_logging._CONFIGURED = self.original_configured

    def _run(self, stdout_only: bool):
        from unittest.mock import patch

        console_calls, file_calls = [], []

        def console():
            import logging
            handler = logging.NullHandler()
            console_calls.append(handler)
            return handler

        def file_handler(component):
            file_calls.append(component)
            import logging
            return logging.NullHandler()

        self.app_logging._CONFIGURED = False
        with patch.object(self.app_logging, "_console_handler", side_effect=console), \
             patch.object(self.app_logging, "_file_handler", side_effect=file_handler), \
             patch.object(self.app_logging.settings, "LOG_STDOUT_ONLY", stdout_only):
            self.app_logging.setup_logging("app")
        return console_calls, file_calls

    def test_default_still_adds_component_file_handler(self):
        console_calls, file_calls = self._run(False)
        self.assertEqual(len(console_calls), 1)
        self.assertEqual(file_calls, ["app"])

    def test_stdout_only_skips_file_handler(self):
        console_calls, file_calls = self._run(True)
        self.assertEqual(len(console_calls), 1)
        self.assertEqual(file_calls, [])


if __name__ == "__main__":
    unittest.main()
