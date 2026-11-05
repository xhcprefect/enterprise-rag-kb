"""本地项目索引配置默认值与校验（纯单测，不连库；RAG_DISABLE_DOTENV=1）。"""
import os
import unittest
from unittest.mock import patch

from app.core.config import Settings


class ProjectConfigTests(unittest.TestCase):
    def test_allowed_roots_empty_by_default(self):
        config = Settings(_env_file=None)
        self.assertIsInstance(config.PROJECT_ALLOWED_ROOTS, dict)
        self.assertEqual(config.PROJECT_ALLOWED_ROOTS, {})
        # 默认空 = 不创建任何项目源。
        self.assertEqual(len(config.PROJECT_ALLOWED_ROOTS), 0)

    def test_project_defaults_are_sane(self):
        config = Settings(_env_file=None)
        self.assertEqual(config.PROJECT_STORAGE_DIR, "storage/projects")
        self.assertEqual(config.PROJECT_MAX_FILES, 5000)
        self.assertEqual(config.PROJECT_MAX_FILE_BYTES, 4 * 1024 * 1024)
        self.assertEqual(config.PROJECT_MAX_TOTAL_BYTES, 512 * 1024 * 1024)
        self.assertEqual(config.PROJECT_PARSE_BATCH, 8)
        self.assertEqual(config.PROJECT_JOB_LEASE_SECONDS, 300)
        self.assertEqual(config.PROJECT_STALE_SECONDS, 300)
        self.assertEqual(config.PROJECT_MAX_ATTEMPTS, 3)
        self.assertTrue(config.PROJECT_VECTOR_PREFIX.startswith("proj_"))

    def test_allowed_roots_parses_json_map_from_env(self):
        with patch.dict("os.environ", {
            "PROJECT_ALLOWED_ROOTS": '{"lib": "C:/work/lib", "docs": "/srv/docs"}'
        }, clear=False):
            config = Settings(_env_file=None)
            self.assertEqual(config.PROJECT_ALLOWED_ROOTS,
                             {"lib": "C:/work/lib", "docs": "/srv/docs"})

    def test_invalid_roots_json_rejected(self):
        with patch.dict("os.environ", {"PROJECT_ALLOWED_ROOTS": "{not-json"}, clear=False):
            with self.assertRaises(Exception):
                Settings(_env_file=None)

    def test_roots_do_not_accept_arbitrary_absolute_paths_in_keys(self):
        # 服务端只接受 root_key；未知 key 查不到路径 -> 禁止创建源。
        config = Settings(_env_file=None, PROJECT_ALLOWED_ROOTS={"a": "/tmp/a"})
        self.assertNotIn("/etc/passwd", config.PROJECT_ALLOWED_ROOTS)
        self.assertIsNone(config.PROJECT_ALLOWED_ROOTS.get("/etc/passwd"))

    def test_bounds_rejected(self):
        for bad in ({"PROJECT_MAX_FILES": 0}, {"PROJECT_MAX_FILE_BYTES": 0},
                    {"PROJECT_MAX_TOTAL_BYTES": 0}, {"PROJECT_PARSE_BATCH": 0},
                    {"PROJECT_JOB_LEASE_SECONDS": 0}):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                Settings(_env_file=None, **bad)


if __name__ == "__main__":
    unittest.main()
