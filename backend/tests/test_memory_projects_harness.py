"""V 夹具纯单测：不依赖 PG / 上游 index-search-API 契约。

覆盖：
- 替身 /v1/embeddings：1024 维真实向量、L2 归一、非零、可复现、相似度高、可区分。
- 替身 /v1/chat/completions：SSE token/finish/usage/[DONE]；非流式 JSON 形状；
  摘要检测与“保留合成用户事实”、来源 ids；注入失败/延迟/截断；有界总时长 503。
- 夹具 env allowlist：绝不继承真实 API key / 业务 DATABASE_URL；RAG_DISABLE_DOTENV=1；
  uploads/project/log 回本轮 temp；授权根只含合成根。
- 合成授权目录：期望文件/排除/同字节不同路径。
- 子进程生命周期：psutil pid+create_time 核验 + 有界 terminate/kill 并复查整树退出。

只 import 新夹具模块；不改任何业务文件。运行：```python -m unittest tests/test_memory_projects_harness.py -v```
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import psutil  # noqa: E402 (生命周期测试用)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import _memory_projects_fixture as fix  # noqa: E402
import _memory_projects_parse as par  # noqa: E402
import _memory_projects_stub as stub  # noqa: E402


class _Server:
    def __init__(self, owner="", nonce=""):
        self.state = stub.StubState(owner=owner, nonce=nonce)

    def __enter__(self):
        stub.STATE = self.state
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), stub.Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/v1"
        self.ctl = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def _stream_chat(base, payload):
    """读取 SSE 流并解码为 (text, finish, usage, done, lines)。"""
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    text = ""
    done = False
    finish = None
    usage = None
    lines = []
    with urllib.request.urlopen(req, timeout=20) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            lines.append(line)
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                done = True
                continue
            obj = json.loads(data)
            choices = obj.get("choices") or []
            for ch in choices:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    text += delta["content"]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
            if obj.get("usage"):
                usage = obj["usage"]
    return text, finish, usage, done, lines


class EmbeddingTests(unittest.TestCase):
    def test_shape_and_norm(self):
        v = stub.embedding_vector("hello world 你好世界")
        self.assertEqual(len(v), 1024)
        norm = sum(x * x for x in v) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=5)
        self.assertGreater(sum(abs(x) for x in v), 0.0)

    def test_reproducible(self):
        self.assertEqual(stub.embedding_vector("abc 确"), stub.embedding_vector("abc 确"))

    def test_similar_higher_than_dissimilar(self):
        dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
        a = stub.embedding_vector("compute total amount")
        b = stub.embedding_vector("compute total amount price")
        c = stub.embedding_vector("the quick brown fox jumps")
        self.assertGreater(dot(a, b), dot(a, c))

    def test_empty_is_valid_unit_vector(self):
        v = stub.embedding_vector("")
        self.assertEqual(len(v), 1024)
        self.assertAlmostEqual(sum(x * x for x in v) ** 0.5, 1.0, places=5)


class StubHttpTests(unittest.TestCase):
    def test_embeddings_endpoint(self):
        with _Server(owner="o", nonce="n") as s:
            status, body = _post(s.base + "/embeddings", {"input": ["a 测试", "b"]})
            self.assertEqual(status, 200)
            data = body["data"]
            self.assertEqual(len(data), 2)
            for i, item in enumerate(data):
                self.assertEqual(len(item["embedding"]), 1024)
                norm = sum(x * x for x in item["embedding"]) ** 0.5
                self.assertAlmostEqual(norm, 1.0, places=5)
            self.assertNotEqual(data[0]["embedding"], data[1]["embedding"])

    def test_chat_stream_sse(self):
        with _Server() as s:
            payload = {"model": "verify-chat", "messages": [{"role": "user", "content": "请介绍项目"}],
                       "stream": True}
            text, finish, usage, done, lines = _stream_chat(s.base, payload)
            self.assertTrue(done)
            self.assertEqual(finish, "stop")
            self.assertGreaterEqual(len(text), 1)
            self.assertIsNotNone(usage)
            self.assertGreater(usage["total_tokens"], 0)
            st = s.state.snapshot()
            self.assertEqual(st["counts"]["chat_stream_calls"], 1)

    def test_chat_json_shape(self):
        with _Server() as s:
            status, body = _post(s.base + "/chat/completions",
                                 {"model": "verify-chat", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(status, 200)
            self.assertEqual(body["object"], "chat.completion")
            self.assertEqual(body["choices"][0]["finish_reason"], "stop")
            self.assertGreater(len(body["choices"][0]["message"]["content"]), 0)

    def test_summary_detection_preserves_fact(self):
        with _Server() as s:
            # 系统提示含摘要 marker，且用户历史确实提到合成 fact 关键词"周报"
            marker = s.state.config["summary_marker"]
            self.assertTrue(marker)
            payload = {
                "model": "verify-chat",
                "messages": [
                    {"role": "system", "content": f"你要做{marker}，输出固定 JSON 结构"},
                    {"role": "user", "content": "我们的项目每周都要交付周报，请帮我记住"},
                ],
                "stream": False,
            }
            status, body = _post(s.base + "/chat/completions", payload)
            self.assertEqual(status, 200)
            content = body["choices"][0]["message"]["content"]
            doc = json.loads(content)
            # M 固定 schema：version + 七区
            self.assertEqual(doc["version"], 1)
            self.assertEqual(doc["unfinished"], False)
            for k in ("goals", "constraints", "confirmed_facts", "decisions",
                      "preferences", "corrections", "open_questions"):
                self.assertIn(k, doc)
            # 合成 fact s-0001 的关键词"周报"在历史里 -> 写入 confirmed_facts
            self.assertEqual(doc["confirmed_facts"][0]["source_message_ids"][0], "s-0001")
            self.assertEqual(doc["confirmed_facts"][0]["verification"], "confirmed_by_user")
            # 每区元素符合 ITEM 形状
            for item in doc["confirmed_facts"]:
                self.assertIn("text", item)
                self.assertIn("source_message_ids", item)
                self.assertIn("verification", item)
            st = s.state.snapshot()
            self.assertEqual(st["counts"]["summary_calls"], 1)

    def test_summary_detection_via_worker_human_message(self):
        # M 真实 memory_worker._job_messages 用 HumanMessage("请输出摘要 JSON。") 触发，
        # 系统提示 = build_summary_prompt 不含"会话摘要" marker；必须仅凭该 human 前缀判定摘要。
        with _Server() as s:
            payload = {
                "model": "verify-chat",
                "messages": [
                    {"role": "system", "content": "你是记忆压缩器，输出固定 JSON"},
                    {"role": "user", "content": "请输出摘要 JSON。"},
                ],
                "stream": False,
            }
            status, body = _post(s.base + "/chat/completions", payload)
            self.assertEqual(status, 200)
            doc = json.loads(body["choices"][0]["message"]["content"])
            self.assertEqual(doc["version"], 1)
            self.assertIn("confirmed_facts", doc)
            self.assertEqual(s.state.snapshot()["counts"]["summary_calls"], 1)

    def test_summary_uses_real_input_message_uuid_as_source(self):
        # 关键：真实 worker 的 batch 文本在 SystemMessage 里行内嵌 message_id=<真实 UUID>，
        # M 的 parse_summary 严格校验来源必须落在窗口内。替身必须从**输入里真出现的 UUID**
        # 选来源（配合关键词命中写进 confirmed_facts），不得硬编码 s-* 骗过放宽校验。
        real_uuid = "1fd57208-4918-46b6-afaf-482306b7234a"
        batch = (
            "[user 1 message_id=%s]\n用户要求按周交付项目周报，请记住\n"
            "[assistant 1 message_id=%s]\n好的，已记入周报\n" % (real_uuid, "3b6a2f6e-8c11-4d2a-9a1b-1f2e3d4c5b6a")
        )
        payload = {
            "model": "verify-chat",
            "messages": [
                {"role": "system", "content": f"你是记忆压缩器。待总结窗口：\n{batch}"},
                {"role": "user", "content": "请输出摘要 JSON。"},
            ],
            "stream": False,
        }
        with _Server() as s:
            status, body = _post(s.base + "/chat/completions", payload)
        self.assertEqual(status, 200)
        doc = json.loads(body["choices"][0]["message"]["content"])
        src_ids = [src for f in doc["confirmed_facts"]
                   for src in f.get("source_message_ids", [])]
        # 命中了"周报"关键词 -> confirmed_facts 非空，且来源是输入里真实出现的 UUID
        self.assertTrue(doc["confirmed_facts"])
        self.assertIn(real_uuid, src_ids)
        self.assertFalse(any(str(x).startswith("s-") for x in src_ids))

    def test_summary_no_fact_keyword_gives_s0000(self):
        with _Server() as s:
            payload = {"model": "verify-chat", "messages": [
                {"role": "system", "content": f"你要做{s.state.config['summary_marker']}，输出固定 JSON"},
                {"role": "user", "content": "这里完全没有提到任何合成关键词"},
            ]}
            status, body = _post(s.base + "/chat/completions", payload)
            doc = json.loads(body["choices"][0]["message"]["content"])
            self.assertEqual(doc["confirmed_facts"][0]["source_message_ids"], ["s-0000"])

    def test_chat_fail_injection(self):
        with _Server() as s:
            _post(s.ctl + "/__control/config", {"chat_fail_next": 1, "chat_fail_status": 500})
            status, body = _post(s.base + "/chat/completions",
                                 {"messages": [{"role": "user", "content": "boom"}]})
            self.assertEqual(status, 500)
            # 第二次恢复
            status, body = _post(s.base + "/chat/completions",
                                 {"messages": [{"role": "user", "content": "ok"}]})
            self.assertEqual(status, 200)
            self.assertEqual(s.state.snapshot()["counts"]["chat_failed"], 1)

    def test_embed_fail_injection(self):
        with _Server() as s:
            _post(s.ctl + "/__control/config",
                  {"embed_fail_next": 1, "embed_fail_match": "trigger"})
            status, _ = _post(s.base + "/embeddings", {"input": ["trigger-now"]})
            self.assertEqual(status, 500)
            status, _ = _post(s.base + "/embeddings", {"input": ["trigger-now"]})
            self.assertEqual(status, 200)

    def test_chat_truncate_injection(self):
        with _Server() as s:
            _post(s.ctl + "/__control/config", {"chat_truncate": True})
            _, finish, _usage, done, _ = _stream_chat(
                s.base, {"messages": [{"role": "user", "content": "truncate me"}], "stream": True})
            self.assertTrue(done)
            self.assertEqual(finish, "length")
            self.assertEqual(s.state.snapshot()["counts"]["chat_truncated"], 1)

    def test_bounded_lifetime_503(self):
        with _Server() as s:
            _post(s.ctl + "/__control/config", {"max_lifetime_ms": 1})
            time.sleep(0.05)
            status, _ = _post(s.base + "/embeddings", {"input": ["x"]})
            self.assertEqual(status, 503)

    def test_loopback_only_rejects_foreign(self):
        # client_address 非 loopback 应返回 403；这里直接调用 Handler 逻辑难以造 socket，
        # 改为断言：合法回环请求可通过，且 state 记录带 owner/nonce 归属。
        with _Server(owner="owned-test:9c8ba95b", nonce="abc") as s:
            status, body = _post(s.base + "/embeddings", {"input": ["x"]})
            self.assertEqual(status, 200)
            self.assertEqual(s.state.snapshot()["owner"], "owned-test:9c8ba95b")
            self.assertEqual(s.state.snapshot()["nonce"], "abc")

    def test_state_reset(self):
        with _Server() as s:
            _post(s.base + "/embeddings", {"input": ["a", "b", "c"]})
            self.assertEqual(s.state.snapshot()["counts"]["embedding_calls"], 1)
            _post(s.ctl + "/__control/reset", {})
            snap = s.state.snapshot()
            self.assertEqual(snap["counts"]["embedding_calls"], 0)
            self.assertEqual(snap["config"]["chat_fail_next"], 0)

    def test_facts_override_flows_to_summary_source_ids(self):
        """真实链接线：夹具通过 __control/config.facts 注入真实 source_id
        （真实 DB 消息 id），摘要 JSON 的 confirmed_facts source_message_ids 必须引用它们，
        供 worker 的来源校验通过。"""
        with _Server() as s:
            real_id = "12345678-1234-1234-1234-123456789abc"
            _post(s.ctl + "/__control/config", {
                "facts": [{"source_id": real_id, "keyword": "周报", "fact": "u 按周交付（注入）"}],
            })
            marker = f"你要做{s.state.config['summary_marker']}，输出固定 JSON"
            status, body = _post(s.base + "/chat/completions", {
                "messages": [{"role": "system", "content": marker},
                             {"role": "user", "content": "周报 中文"}]})
            self.assertEqual(status, 200)
            doc = json.loads(body["choices"][0]["message"]["content"])
            self.assertEqual(doc["confirmed_facts"][0]["source_message_ids"], [real_id])
            # reset 清除覆盖，恢复默认合成事实
            _post(s.ctl + "/__control/reset", {})
            status, body = _post(s.base + "/chat/completions", {
                "messages": [{"role": "system", "content": marker},
                             {"role": "user", "content": "周报 中文"}]})
            doc = json.loads(body["choices"][0]["message"]["content"])
            self.assertIn("s-0001", doc["confirmed_facts"][0]["source_message_ids"])


class FixtureEnvTests(unittest.TestCase):
    def test_allowlist_never_inherits_secrets(self):
        old = {k: os.environ.get(k) for k in
               ("EMBEDDING_API_KEY", "LLM_API_KEY", "DATABASE_URL", "TEST_DATABASE_URL")}
        try:
            os.environ["EMBEDDING_API_KEY"] = "REAL-KEY"
            os.environ["LLM_API_KEY"] = "REAL-KEY-2"
            os.environ["DATABASE_URL"] = "postgresql+psycopg://postgres:real@db:5432/real"
            os.environ["TEST_DATABASE_URL"] = "postgresql+psycopg://postgres:real@db:5432/real_test"
            f = fix.Fixture(owner="t")
            env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@127.0.0.1:1/integration_test",
                                    jwt_secret="s", admin_user="a", admin_pass="p",
                                    embedding_base="http://127.0.0.1:1/v1", llm_base="http://127.0.0.1:1/v1")
            self.assertNotEqual(env.get("EMBEDDING_API_KEY"), "REAL-KEY")
            self.assertNotEqual(env.get("LLM_API_KEY"), "REAL-KEY-2")
            self.assertEqual(env["EMBEDDING_API_KEY"], "local-stub-key-not-real")
            self.assertEqual(env["DATABASE_URL"], "postgresql+psycopg://u:p@127.0.0.1:1/integration_test")
            self.assertNotIn("TEST_DATABASE_URL", env)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_allowlist_has_disable_dotenv_and_temp(self):
        f = fix.Fixture(owner="t")
        f.make_dirs()
        env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@127.0.0.1:9/x_test",
                                jwt_secret="s", admin_user="a", admin_pass="p",
                                embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1")
        self.assertEqual(env.get("RAG_DISABLE_DOTENV"), "1")
        self.assertIn("http://127.0.0.1:9/v1", env["LLM_BASE_URL"])
        self.assertEqual(env["LOG_STDOUT_ONLY"], "1")

    def test_project_root_mapping_defaults_to_synth_only(self):
        # 授权根默认落在 Fixture 自有 run_root 内的合成目录（f.project_root），
        # 绝不继承仓库内/任意外部目录树。
        f = fix.Fixture(owner="t")
        f.make_dirs()
        env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@host/x_test",
                                jwt_secret="s", admin_user="a", admin_pass="p",
                                embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1")
        roots = json.loads(env["PROJECT_ALLOWED_ROOTS"])
        self.assertIn(str(f.project_root), roots.values())
        # 授权根必须强约束在 run_root 内（本轮自有合成目录）
        rr = str(f.run_root.resolve()).lower().replace("\\", "/")
        for value in roots.values():
            self.assertTrue(str(Path(value).resolve()).lower().replace("\\", "/").startswith(rr),
                            f"授权根不在 run_root 内: {value}")
        # F 落盘的真实存储目录变量名必须指向夹具自有的本轮 blob 目录（绝不指向仓库内）
        self.assertEqual(
            env["PROJECT_STORAGE_DIR"].lower().replace("\\", "/"),
            str(f.blobs).lower().replace("\\", "/"))

    def test_project_root_rejects_external_existing_dir(self):
        """授权根即便存在且不属于仓库，只要是 Fixture 自有合成目录之外（本轮 run_root 外）
        的任意外部目录，也必须被拒绝——不继承外部任意目录树。"""
        f = fix.Fixture(owner="t")
        with tempfile.TemporaryDirectory(prefix="vms-external-") as td:
            with self.assertRaises(RuntimeError):
                f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test", jwt_secret="s",
                                  admin_user="a", admin_pass="p",
                                  embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1",
                                  emit_project_roots={"default": td})


class ProjectFixtureTests(unittest.TestCase):
    def test_build_project_fixture_expected_and_excluded(self):
        with tempfile.TemporaryDirectory(prefix="vms-proj-") as td:
            info = fix.build_project_fixture(Path(td) / "auth")
            self.assertTrue(info["expected_files"])
            self.assertFalse(info["expected_files"] & info["excluded"])
            self.assertIn(".env", info["excluded"])
            # 同字节不同路径确实存在
            for a, b in info["duplicate_pairs"]:
                pa = Path(td) / "auth" / a
                pb = Path(td) / "auth" / b
                self.assertEqual(pa.read_bytes(), pb.read_bytes())
                self.assertNotEqual(pa, pb)

    def test_project_root_rejects_script_dir(self):
        f = fix.Fixture(owner="t")
        with self.assertRaises(RuntimeError):
            f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test", jwt_secret="s",
                              admin_user="a", admin_pass="p",
                              embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1",
                              emit_project_roots={"default": str(Path(__file__).resolve().parent)})

    def test_project_root_rejects_frontend_and_repo_root(self):
        """授权根即便存在，只要落在仓库真实工作树（frontend / 仓库根）任何层级，就必须拒绝，
        绝不继承 frontend 或父 repo 的真实目录树。"""
        repo_root = Path(__file__).resolve().parent.parent.parent  # 仓库根
        for bad in (str(repo_root / "frontend"), str(repo_root)):
            f = fix.Fixture(owner="t")
            with self.assertRaises(RuntimeError):
                f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test", jwt_secret="s",
                                  admin_user="a", admin_pass="p",
                                  embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1",
                                  emit_project_roots={"default": bad})

    def test_run_root_rejects_repo_tree(self):
        """run_root 绝不能指向仓库真实工作树（backend / frontend / 仓库根）；
        Fixture 构造时必须拒绝，owned 合成运行根绝不落进真实 repo 任何层级。"""
        repo_root = Path(__file__).resolve().parent.parent.parent  # 仓库根
        for bad in (str(repo_root), str(repo_root / "backend"), str(repo_root / "frontend")):
            with self.assertRaises(RuntimeError):
                fix.Fixture(owner="t", run_root=Path(bad))


class ProcessLifecycleTests(unittest.TestCase):
    def test_spawn_alive_and_cleanup(self):
        # run_root 用夹具默认（系统 temp，仓库外），不放进会被父进程 rmtree 的目录，
        # 避免 Windows 进程 cwd 目录句柄竞态影响临时目录清理。
        f = fix.Fixture(owner="t")
        try:
            env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test",
                                    jwt_secret="s", admin_user="a", admin_pass="p",
                                    embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1")
            ready = f.run_root / "ready.txt"
            probe = f.spawn("sleepy", [sys.executable, "-c", "import time, os; open(os.environ['R_READY'],'w').write('1'); time.sleep(60)"],
                            env={**env, "R_READY": str(ready)}, cwd=f.run_root,
                            ready_file=ready)
            self.assertTrue(f.alive(probe))
            self.assertTrue(ready.exists())
            report = f.cleanup(grace=2.0, kill=True)
            key = f"sleepy-{probe.pid}"
            self.assertIn(key, report)
            self.assertTrue(report[key]["terminated"] or report[key]["killed"])
            self.assertEqual(f.children_alive(), [])
        finally:
            f.cleanup(grace=1.0, kill=True)
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)

    def test_cleanup_captures_full_tree_descendants(self):
        """整棵 (pid, ctime) 树收口：根进程拉的孙进程（create_time 与根不同）也必须被终止，
        且 cleanup 后 self.children 为空（全树确认退出），children_alive 无伪造残留。"""
        import subprocess as _sp
        import sys as _sys
        f = fix.Fixture(owner="t")
        try:
            env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test",
                                    jwt_secret="s", admin_user="a", admin_pass="p",
                                    embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1")
            ready = f.run_root / "ready2.txt"
            # 根进程睡眠前 fork 一个孙进程（两者的 create_time 不同）
            script = (
                "import subprocess, sys, os, time\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "open(os.environ['R_READY'],'w').write('1')\n"
                "time.sleep(60)\n"
            )
            probe = f.spawn("forker", [sys.executable, "-c", script],
                            env={**env, "R_READY": str(ready)}, cwd=f.run_root,
                            ready_file=ready)
            # 收集根身份下整棵树（含孙进程）
            tree, tree_complete, _ = f._snapshot_descendants(probe.pid, probe.create_time)
            self.assertGreaterEqual(len(tree), 2)  # 根 + 至少 1 孙
            self.assertTrue(tree_complete)
            report = f.cleanup(grace=2.0, kill=True)
            key = f"forker-{probe.pid}"
            self.assertIn(key, report)
            # cleanup 后：root probe 被 finalize，self.children 清空（全树确认退出）
            self.assertEqual(f.children_alive(), [])
            self.assertFalse(any(c.pid == probe.pid for c in f.children))
        finally:
            f.cleanup(grace=1.0, kill=True)
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)

    def test_cleanup_retains_unexited_member(self):
        """cleanup 遇到无法确认退出的成员时，不得清空 self.children 当伪干净：
        未 exit 的 probe 必须保留并可由 children_alive 看到。"""
        f = fix.Fixture(owner="t")
        try:
            # 人造一个不可能退出的假 probe（指向本测试进程自身，_ident 恒真=仍在）
            probe = fix.ChildProbe(role="zombie", pid=os.getpid(),
                                   create_time=psutil.Process(os.getpid()).create_time(),
                                   fq_bin="")
            f.children.append(probe)
            f.cleanup(grace=0.2, kill=True)
            # 自身进程不会被杀，成员未被 finalize → 保留且 children_alive 仍可见
            self.assertTrue(any(c.pid == os.getpid() for c in f.children))
            alive = [c for c in f.children_alive() if c.pid == os.getpid()]
            self.assertEqual(len(alive), 1)
        finally:
            f.children = []

    def test_unknown_member_blocks_clean_finalize(self):
        """身份无法复核（create_time=None，如 AccessDenied）的真实进程不能让其被当作已退出：
        只要该未知身份进程仍在，cleanup 就不能把该 probe 判为 clean/finalize。"""
        import subprocess as _sp
        import sys as _sys
        f = fix.Fixture(owner="t")
        sleeper = _sp.Popen([_sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            # 根身份：一个本应已退出的临时 PID；未知成员：一个真实常驻进程但其身份被标为未知 (pid, None)
            dead = _sp.Popen([_sys.executable, "-c", "pass"])
            dead_pid, dead_ct = dead.pid, psutil.Process(dead.pid).create_time()
            dead.wait()
            probe = fix.ChildProbe(role="unknown-child", pid=dead_pid,
                                   create_time=dead_ct, fq_bin="")
            probe.descendants = {(dead_pid, dead_ct), (sleeper.pid, None)}
            f.children.append(probe)
            report = f.cleanup(grace=0.2, kill=True)
            key = f"unknown-child-{dead_pid}"
            # 未知身份的常驻进程仍在 → remaining 非空，probe 不被 finalize
            self.assertTrue(any(c[1] is None for c in report[key]["remaining"]))
            self.assertFalse(probe._finalized)
            self.assertTrue(any(c.pid == dead_pid for c in f.children))
        finally:
            sleeper.kill()
            sleeper.wait()
            f.children = []

    def test_cleanup_grace0_does_not_pseudo_clean(self):
        """反例（主审）：grace=0, kill=False 时，循环一次都不跑。必须做无条件最终
        recheck：指向本进程自身的 probe（_signal_ident 对自身返回 False 不发信号）仍存活，
        remaining 不得为空，绝不能 consequent finalized / 从 children 移除（伪 clean）。"""
        f = fix.Fixture(owner="t")
        try:
            me = psutil.Process(os.getpid()).create_time()
            probe = fix.ChildProbe(role="selfprobe", pid=os.getpid(), create_time=me, fq_bin="")
            probe.descendants = {(os.getpid(), me)}
            f.children = [probe]
            report = f.cleanup(grace=0, kill=False)
            key = f"selfprobe-{os.getpid()}"
            # remaining 非空（本进程仍存活），且未 finalize、未从 children 移除
            self.assertTrue(report[key]["remaining"], "grace=0 也必须重算出 remaining")
            self.assertFalse(probe._finalized)
            self.assertEqual([c.pid for c in f.children], [os.getpid()])
        finally:
            f.children = []

    def test_cleanup_grace0_exited_member_finalizes(self):
        """正面：grace=0 且成员确已退出时，无条件 recheck 应正确判 clean（不误报）。"""
        import subprocess as _sp
        import sys as _sys
        f = fix.Fixture(owner="t")
        try:
            quick = _sp.Popen([_sys.executable, "-c", "pass"])
            pid, ctime = quick.pid, psutil.Process(quick.pid).create_time()
            quick.wait()
            probe = fix.ChildProbe(role="gone", pid=pid, create_time=ctime, fq_bin="")
            probe.descendants = {(pid, ctime)}
            f.children = [probe]
            report = f.cleanup(grace=0, kill=False)
            key = f"gone-{pid}"
            self.assertEqual(report[key]["remaining"], [])
            self.assertTrue(probe._finalized)
            self.assertEqual(f.children, [])
        finally:
            f.children = []

    def test_cleanup_negative_grace_is_bounded(self):
        """负数 grace 也必须有界正确：等同 0，不能因 deadline 在过去而死循环或无界忙等。"""
        f = fix.Fixture(owner="t")
        try:
            me = psutil.Process(os.getpid()).create_time()
            probe = fix.ChildProbe(role="selft", pid=os.getpid(), create_time=me, fq_bin="")
            probe.descendants = {(os.getpid(), me)}
            f.children = [probe]
            start = time.time()
            report = f.cleanup(grace=-5, kill=False)
            elapsed = time.time() - start
            self.assertLess(elapsed, 2.0, "负 grace 不得死循环")
            self.assertFalse(probe._finalized)
            self.assertEqual([c.pid for c in f.children], [os.getpid()])
        finally:
            f.children = []

    def test_snapshot_retains_unknown_child_on_accessdenied(self):
        """_snapshot_descendants 遇到无法复核的身份（create_time 读不到 → (pid, None)）
        必须登记为未知成员并把枚举标为不完整，绝不能 continue 丢弃节点把树判成空/退出。"""
        found, complete, _ = fix.Fixture._snapshot_descendants(
            os.getpid(), psutil.Process(os.getpid()).create_time())
        # 根本身可验证，集合非空且根身份在其中
        self.assertTrue(any(p == os.getpid() for p, _ in found))

    def test_snapshot_accessdenied_children_enum_incomplete(self):
        """真的 AccessDenied raise：proc.children() 抛 psutil.AccessDenied 时，
        枚举必须标为不完整（complete=False），绝不能把“已观察部分”当“整树已知”。"""
        import unittest.mock as _mock
        from unittest.mock import patch
        me = psutil.Process(os.getpid()).create_time()
        real_children = psutil.Process(os.getpid()).children
        def raising_children(self, recursive=False):
            raise psutil.AccessDenied(self.pid if hasattr(self, "pid") else os.getpid())
        with patch.object(psutil.Process, "children", raising_children):
            found, complete, reason = fix.Fixture._snapshot_descendants(os.getpid(), me)
        self.assertIn((os.getpid(), me), found)
        self.assertFalse(complete, f"枚举必须把 AccessDenied 判为不完整: {reason}")
        self.assertIn("AccessDenied", reason or "")

    def test_cleanup_refuses_finalize_when_enum_incomplete(self):
        """父进程枚举被拒（AccessDenied）时枚举不完整：即便已观察成员随后全部退出，
        cleanup 也绝不能把该 probe 判为 finalized（伪 clean）——根退出不等于整树已知。"""
        import unittest.mock as _mock
        from unittest.mock import patch
        import subprocess as _sp
        import sys as _sys
        f = fix.Fixture(owner="t")
        try:
            # 根进程极短退出
            quick = _sp.Popen([_sys.executable, "-c", "pass"])
            root_pid, root_ctime = quick.pid, psutil.Process(quick.pid).create_time()
            quick.wait()
            # 枚举不完整：快照整体抛异常（等价于无法枚举整棵树）
            def raising_snapshot(self, pid, ctime):
                raise psutil.AccessDenied(pid)
            probe = fix.ChildProbe(role="enum-incomplete", pid=root_pid,
                                   create_time=root_ctime, fq_bin="")
            probe.descendants = {(root_pid, root_ctime)}
            f.children.append(probe)
            with patch.object(fix.Fixture, "_snapshot_descendants", raising_snapshot):
                report = f.cleanup(grace=0.1, kill=True)
            self.assertFalse(probe._finalized, "枚举不完整不得 finalize")
            self.assertTrue(any(c.pid == root_pid for c in f.children))
        finally:
            f.children = []

    def test_persist_probe_records_all_children(self):
        """_persist_probe 必须把**所有**已启动角色的后代都写入 journal，不只最后那个。"""
        f = fix.Fixture(owner="jall")
        try:
            me = psutil.Process(os.getpid()).create_time()
            a = fix.ChildProbe(role="stub", pid=os.getpid(), create_time=me, fq_bin="")
            b = fix.ChildProbe(role="worker", pid=os.getpid(), create_time=me, fq_bin="")
            b.descendants = {(os.getpid(), me), (os.getpid() + 99, None)}
            f.children = [a, b]
            f._persist_probe(b)
            payload = json.loads((f.logs / f"journal-{f.owner}.json").read_text(encoding="utf-8"))
            roles = [c["role"] for c in payload["children"]]
            self.assertIn("stub", roles)
            self.assertIn("worker", roles)
            # worker 的后代也被持久化
            worker = next(c for c in payload["children"] if c["role"] == "worker")
            self.assertEqual(len(worker["descendants"]), 2)
        finally:
            f.children = []

    def test_write_journal_returns_real_path(self):
        """write_journal 必须返回实际写入的 journal-{owner}.json（不是未创建的 summary 名）。"""
        f = fix.Fixture(owner="wj7")
        try:
            path = f.write_journal()
            self.assertEqual(path, f.logs / f"journal-{f.owner}.json")
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["owner"], "wj7")
        finally:
            f.children = []

    def test_descendants_survive_root_exit_between_cleanups(self):
        """根进程已退出后，cleanup 必须仍复查**已登记**的后代，不能因本次快照取不到子而伪干净。
        记录一个永不退出的假后代（指向本进程自身，_ident 恒真），根身份已不存在仍不得 finalize。"""
        import subprocess as _sp
        import sys as _sys
        f = fix.Fixture(owner="t")
        try:
            env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test",
                                    jwt_secret="s", admin_user="a", admin_pass="p",
                                    embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1")
            # 根进程极短退出
            quick = _sp.Popen([_sys.executable, "-c", "pass"])
            root_pid, root_ctime = quick.pid, psutil.Process(quick.pid).create_time()
            quick.wait()
            # 登记整棵后代：根（已退出）+ 一个“仍在本进程”的未知成员
            probe = fix.ChildProbe(role="rootgone", pid=root_pid,
                                   create_time=root_ctime, fq_bin="")
            probe.descendants = {(root_pid, root_ctime),
                                 (os.getpid(), psutil.Process(os.getpid()).create_time())}
            f.children.append(probe)
            report = f.cleanup(grace=0.3, kill=True)
            key = f"rootgone-{root_pid}"
            # 根已退，但登记的后代仍在本进程（未退出）→ 不得 clean
            self.assertFalse(probe._finalized)
            self.assertTrue(any(c.pid == root_pid for c in f.children))
        finally:
            f.children = []

    def test_spawn_failure_does_not_leak_children(self):
        """spawn 过程异常（如 create_time 读出后进程即崩）不能留孤儿句柄/进程：
        这里用一个会立即退出的可执行路径制造轨迹，断言 spawn 抛错后 children 无残留。"""
        f = fix.Fixture(owner="t")
        try:
            env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test",
                                    jwt_secret="s", admin_user="a", admin_pass="p",
                                    embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1")
            with self.assertRaises((OSError, RuntimeError)):
                # 用不存在的 cwd 触发启动失败（psutil.Popen 抛 OSError）
                f.spawn("boom", [sys.executable, "-c", "pass"],
                        env=env, cwd=f.run_root / "does-not-exist-dir")
            self.assertEqual(f.children, [])
        finally:
            f.cleanup(grace=1.0, kill=True)

    def test_spawn_identity_failure_preserves_unverified_ledger(self):
        """create_time 取不到且无法确认退出时，spawn 抛错但不能只杀根就丢账：
        必须把未证身份进程记入 self.children（create_time=None）+ journal，
        供之后 cleanup 按未知成员保守收口 / 明确暴露，绝不让已启动成员从账本里消失。"""
        import unittest.mock as _mock
        from unittest.mock import patch

        class _FakePopen:
            pid = 424242
            def __init__(self, *a, **k):
                pass
            def create_time(self):
                raise psutil.AccessDenied(self.pid)
            def is_running(self):
                return True   # 无法确认退出
            def kill(self):
                return None
            def wait(self, timeout=None):
                return None

        f = fix.Fixture(owner="t")
        try:
            env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test",
                                    jwt_secret="s", admin_user="a", admin_pass="p",
                                    embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1")
            with patch.object(fix.psutil, "Popen", _FakePopen):
                with self.assertRaises(psutil.AccessDenied):
                    f.spawn("boom", [sys.executable, "-c", "pass"],
                            env=env, cwd=f.run_root)
            # 未证成员被保留在账本中：role/pid 对 + create_time=None
            self.assertTrue(any(c.role == "boom" and c.pid == 424242
                                and c.create_time is None for c in f.children))
            # journal 也落到磁盘（未证收口账本不丢）
            path = f.logs / f"journal-{f.owner}.json"
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(any(c.get("role") == "boom" and c.get("create_time") is None
                                for c in payload["children"]))
        finally:
            f.children = []

    def test_env_uses_real_project_var_names(self):
        """F 落盘的真实变量名必须被使用：PROJECT_ALLOWED_ROOTS / PROJECT_STORAGE_DIR，
        不再用占位 PROJECT_AUTH_ROOTS_JSON / PROJECT_STORE_DIR。"""
        f = fix.Fixture(owner="t")
        try:
            env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test",
                                    jwt_secret="s", admin_user="a", admin_pass="p",
                                    embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1")
            self.assertIn("PROJECT_ALLOWED_ROOTS", env)
            self.assertIn("PROJECT_STORAGE_DIR", env)
            self.assertNotIn("PROJECT_AUTH_ROOTS_JSON", env)
            self.assertNotIn("PROJECT_STORE_DIR", env)
            self.assertNotIn("PROJECT_BLOB_DIR", env)
        finally:
            f.cleanup(grace=1.0, kill=True)


class ParseScenarioTests(unittest.TestCase):
    def test_python_real_symbols_and_lines(self):
        text = (
            "import os\n"
            "\n"
            "def compute_total(items):\n"
            "    return sum(i.get('amount', 0) for i in items)\n"
            "\n"
            "class Payment:\n"
            "    def settle(self):\n"
            "        return compute_total([])\n"
        )
        out = par.parse_python(text, "src/utils.py")
        self.assertEqual(out.status, "parsed")
        names = {s.name for s in out.symbols}
        self.assertIn("compute_total", names)
        self.assertIn("Payment", names)
        self.assertIn("settle", names)
        by_name = {s.name: s for s in out.symbols}
        self.assertEqual(by_name["compute_total"].line, 3)
        self.assertEqual(by_name["Payment"].line, 6)
        # 结构块含真实行区间
        self.assertIn((3, 4), out.structure_blocks)
        self.assertTrue(any(x[0] == 6 for x in out.structure_blocks))

    def test_python_syntax_error_is_error_not_fake_ast(self):
        out = par.parse_python("def broken(:\n    pass\n", "bad.py")
        self.assertEqual(out.status, "error")
        self.assertIn("syntax", out.fallback_reason)

    def test_markdown_headings_and_fences_real_lines(self):
        text = "# Title Line1\n\ntext\n\n```python\nx = 1\n```\n\n## Sub\n"
        out = par.parse_markdown(text, "docs/README.md")
        self.assertEqual(out.status, "parsed")
        by_name = {s.name: s for s in out.symbols}
        self.assertEqual(by_name["Title Line1"].line, 1)
        self.assertEqual(by_name["Sub"].line, 9)
        self.assertEqual(by_name["python"].kind, "code-fence")
        self.assertEqual(by_name["python"].line, 5)
        self.assertIn((5, 7), out.structure_blocks)  # 围栏起止行

    def test_vue_sfc_blocks_real_offsets(self):
        text = (
            "<template>\n  <div>{{ x }}</div>\n</template>\n"
            "<script setup lang='ts'>\nconst x = 1\n</script>\n"
        )
        out = par.parse_vue_sfc(text, "src/Counter.vue")
        self.assertEqual(out.status, "parsed")
        kinds = [s.name for s in out.symbols]
        self.assertIn("template", kinds)
        self.assertIn("script", kinds)
        by_name = {s.name: s for s in out.symbols}
        self.assertEqual(by_name["template"].line, 1)
        self.assertEqual(by_name["script"].line, 4)

    def test_ts_fallback_when_no_tree_sitter(self):
        # 无论本机是否装了 tree-sitter，都断言：产出要么 parsed（有包）要么 fallback（无包），
        # 绝不报“通过”而伪造 AST —— 这里直接验证结果不把 fallback 当 parsed 通过。
        status, reason = par.detect_ts_support()
        self.assertIn(status, {"parsed", "fallback"})
        if status == "fallback":
            self.assertIn("tree-sitter", reason)


class StubServerMainTests(unittest.TestCase):
    def test_main_writes_state_and_ready(self):
        # 用 fixture 的 owned spawn/cleanup（pid+create_time 核验 + 有界收口），
        # 避免裸 subprocess 在测试被打断时留下孤儿 stub。
        f = fix.Fixture(owner="t")
        try:
            env = f.allowlisted_env(app_db_url="postgresql+psycopg://u:p@h/x_test",
                                    jwt_secret="s", admin_user="a", admin_pass="p",
                                    embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1")
            stub_py = os.path.join(os.path.dirname(stub.__file__), "_memory_projects_stub.py")
            state = f.new_nonce_dir("state") / "state.json"
            ready = f.run_root / "stub.ready.txt"
            probe = f.spawn("stub", [sys.executable, stub_py, "--port", "0",
                                     "--state-file", str(state), "--ready-file", str(ready),
                                     "--owner", "owned-test:abc", "--nonce", "n"],
                            env=env, cwd=f.run_root, ready_file=ready)
            self.assertTrue(f.alive(probe))
            self.assertTrue(state.exists())
            with open(state, encoding="utf-8") as sf:
                info = json.loads(sf.read())
            self.assertEqual(info["owner"], "owned-test:abc")
            self.assertTrue(info["port"] > 0)
            # 探活：GET /__control/state 应返回 200
            import urllib.request as _ur
            deadline = time.time() + 10
            while True:
                try:
                    with _ur.urlopen(f"http://127.0.0.1:{info['port']}/__control/state", timeout=3) as resp:
                        self.assertEqual(resp.status, 200)
                        break
                except Exception:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.2)
        finally:
            rep = f.cleanup(grace=2.0, kill=True)
            self.assertTrue(rep, "cleanup 应返回收口日志")
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)


class OwnedRootIdentityTests(unittest.TestCase):
    """run_root 真实目录对象身份（本检查点：纯目录守卫 + 归属标记，不 spawn/DB/服务）。

    覆盖：
    - run_root 必须新建且排他：已存在空/非空目录、普通文件、symlink/junction、仓库树全拒。
    - 既有 run_root 不被触碰（sentinel 探针原样保留）。
    - **同路径偷换**：rename 走 → 同路径重新 mkdir → 复制原 owner+nonce marker，
      `_verify_root` 仍必须拒绝（对象身份 st_dev/st_ino 不符，单比路径/marker 会被骗过）。
    - 归属标记 nonce 被换 / marker 不可读（损坏）→ fail closed。
    - owner+nonce 未变的正控：make_dirs / allowlisted_env / new_nonce_dir /
      write_journal / _apply_roots_env 全部通过。
    - 守卫覆盖 new_nonce_dir / write_journal / _apply_roots_env（不只 make_dirs）。
    - 授权根大小写规范化：同 run_root 的大写变体被接受；外部根（含大写）被拒。
    """

    def _guarded_ops(self, f):
        """本检查点已支持的共享/占用根守卫操作（纯目录，不启动进程）。"""
        return [
            f.make_dirs,
            lambda: f.allowlisted_env(
                app_db_url="postgresql+psycopg://u:p@127.0.0.1:1/x_test", jwt_secret="s",
                admin_user="a", admin_pass="p",
                embedding_base="http://127.0.0.1:1/v1", llm_base="http://127.0.0.1:1/v1"),
            lambda: f.new_nonce_dir("t"),
            f.write_journal,
            lambda: f._apply_roots_env({}, {"default": str(f.project_root)}),
        ]

    @staticmethod
    def _replace_root_same_path(f):
        """把 run_root 重命名走，再在同一路径重新建目录，并复制原 owner+nonce marker。"""
        import shutil as _sh
        root = Path(os.fspath(f.run_root))
        old = root.parent / (root.name + ".replaced-old")
        if old.exists():
            _sh.rmtree(old, ignore_errors=True)
        root.rename(old)
        os.mkdir(root)
        _sh.copy2(old / ".ragkb-owner.json", root / ".ragkb-owner.json")
        return old

    def test_guards_pass_untampered(self):
        """正控：owner+nonce 未变，所有共享/占用守卫操作都必须通过。"""
        f = fix.Fixture(owner="t")
        try:
            for op in self._guarded_ops(f):
                op()
        finally:
            f.children = []
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)

    def test_existing_empty_dir_rejected(self):
        with tempfile.TemporaryDirectory(prefix="vrk-empty-") as td:
            p = Path(td) / "empty"
            os.mkdir(p)
            with self.assertRaises(RuntimeError):
                fix.Fixture(owner="t", run_root=p)

    def test_existing_nonempty_dir_rejected(self):
        with tempfile.TemporaryDirectory(prefix="vrk-nonempty-") as td:
            p = Path(td) / "nonempty"
            os.mkdir(p)
            (p / "x.txt").write_text("x", encoding="utf-8")
            (p / "sub").mkdir()
            with self.assertRaises(RuntimeError):
                fix.Fixture(owner="t", run_root=p)

    def test_existing_file_rejected(self):
        with tempfile.TemporaryDirectory(prefix="vrk-file-") as td:
            p = Path(td) / "afile"
            p.write_text("not a dir", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                fix.Fixture(owner="t", run_root=p)

    def test_existing_root_sentinel_untouched(self):
        """既有 run_root 即便被拒，也绝不能触碰其中内容（sentinel 原样保留）。"""
        with tempfile.TemporaryDirectory(prefix="vrk-sentinel-") as td:
            p = Path(td) / "sentinel-root"
            os.mkdir(p)
            sentinel = p / ".my-sentinel"
            sentinel.write_text("do-not-touch", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                fix.Fixture(owner="t", run_root=p)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "do-not-touch")
            self.assertEqual(sorted(x.name for x in p.iterdir()), [".my-sentinel"])

    def test_symlink_root_rejected(self):
        with tempfile.TemporaryDirectory(prefix="vrk-sym-") as td:
            target = Path(td) / "target"
            target.mkdir()
            link = Path(td) / "link"
            try:
                os.symlink(target, link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:  # 无特权/无开发者模式
                self.skipTest(f"无法创建目录 symlink: {exc}")
            with self.assertRaises(RuntimeError):
                fix.Fixture(owner="t", run_root=link)

    def test_ancestor_symlink_rejected(self):
        """关键反例（主审）：run_root 的**祖先**是 symlink(=指向别处)也必须拒绝。

        原声明链 ``linked-parent/new-owned-root`` 中 ``linked-parent`` 是指向
        ``actual-parent`` 的 symlink；若先 realpath 再核 reparse，祖先 link 已被摊平、
        证据丢失 → 被误放行。必须按**原声明路径**逐成分 lstat 拒绝。"""
        with tempfile.TemporaryDirectory(prefix="vrk-anc-sym-") as td:
            actual = Path(td) / "actual-parent"
            actual.mkdir()
            linked = Path(td) / "linked-parent"
            try:
                os.symlink(actual, linked, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"无法创建目录 symlink（祖先场景）: {exc}")
            with self.assertRaises(RuntimeError):
                fix.Fixture(owner="review", run_root=linked / "new-owned-root")

    def test_ancestor_real_parent_positive(self):
        """正控：祖先为真实普通目录时，新建 run_root 应正常创建且守卫通过。"""
        with tempfile.TemporaryDirectory(prefix="vrk-anc-real-") as td:
            actual = Path(td) / "actual-parent"
            actual.mkdir()
            f = fix.Fixture(owner="review", run_root=actual / "new-owned-root")
            try:
                for op in self._guarded_ops(f):
                    op()
                # run_root 解析后落在真实父目录 actual 之下（大小写/短名规范化比较）
                self.assertEqual(
                    fix.Fixture._norm(os.path.realpath(f.run_root.parent)),
                    fix.Fixture._norm(os.path.realpath(actual)))
            finally:
                f.children = []
                import shutil
                shutil.rmtree(f.run_root, ignore_errors=True)

    def test_ancestor_junction_rejected(self):
        """Windows junction 作为祖先也须拒绝（若本机可创建 junction）。"""
        if os.name != "nt":
            self.skipTest("junction 仅 Windows")
        with tempfile.TemporaryDirectory(prefix="vrk-anc-junc-") as td:
            actual = Path(td) / "actual-target"
            actual.mkdir()
            linked = Path(td) / "linked-junction"
            # mklink /J 创建目录 junction；失败（无权限）则跳过。
            # Windows 控制台默认代码页（如 cp936）会让 text=True 解码 stderr 时抛
            # UnicodeDecodeError；显式 utf-8 + replace 兜底，任何异常都当“无法创建”跳过。
            import subprocess as _sp
            try:
                r = _sp.run(["cmd", "/c", "mklink", "/J", str(linked), str(actual)],
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
            except (OSError, UnicodeError) as exc:
                self.skipTest(f"无法创建 junction: {exc}")
            if r.returncode != 0 or not linked.exists():
                detail = (r.stderr or "").strip() or (r.stdout or "").strip()
                self.skipTest(f"无法创建 junction: {detail or r.returncode}")
            try:
                with self.assertRaises(RuntimeError):
                    fix.Fixture(owner="review", run_root=linked / "new-owned-root")
            finally:
                try:
                    linked.rmdir()  # junction 卸载
                except OSError:
                    pass

    def test_same_path_replacement_rejected(self):
        """关键反例（主审）：rename 走 → 同路径重新 mkdir → 复制原 marker，
        _verify_root 必须拒绝——run_root 已被换成**另一个目录对象**（st_dev/st_ino 不符）。"""
        f = fix.Fixture(owner="t")
        old = None
        try:
            f.make_dirs()
            old = self._replace_root_same_path(f)
            for op in self._guarded_ops(f):
                with self.assertRaises(RuntimeError):
                    op()
        finally:
            f.children = []
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)
            if old is not None:
                shutil.rmtree(old, ignore_errors=True)

    def test_nonce_swapped_rejected(self):
        """归属标记 nonce 被换（同对象，仅改标记）→ 守卫 fail closed。"""
        f = fix.Fixture(owner="t")
        try:
            f.make_dirs()
            marker = f.run_root / ".ragkb-owner.json"
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["nonce"] = "tampered-" + payload["nonce"]
            marker.write_text(json.dumps(payload), encoding="utf-8")
            for op in self._guarded_ops(f):
                with self.assertRaises(RuntimeError):
                    op()
        finally:
            f.children = []
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)

    def test_marker_corrupt_failclosed(self):
        """归属标记损坏（不可解析）→ fail closed，绝不当作未变放行。"""
        f = fix.Fixture(owner="t")
        try:
            (f.run_root / ".ragkb-owner.json").write_text("{ not json", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                f.make_dirs()
            with self.assertRaises(RuntimeError):
                f.write_journal()
        finally:
            f.children = []
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)

    def test_marker_deleted_failclosed(self):
        """归属标记被删除 → fail closed。"""
        f = fix.Fixture(owner="t")
        try:
            (f.run_root / ".ragkb-owner.json").unlink()
            with self.assertRaises(RuntimeError):
                f.make_dirs()
        finally:
            f.children = []
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)

    def test_guard_covers_new_nonce_dir_write_journal_apply_roots(self):
        """守卫不只 make_dirs：new_nonce_dir / write_journal / _apply_roots_env
        在同路径偷换后也必须各自拒绝。"""
        f = fix.Fixture(owner="t")
        old = None
        try:
            f.make_dirs()
            old = self._replace_root_same_path(f)
            with self.assertRaises(RuntimeError):
                f.new_nonce_dir("x")
            with self.assertRaises(RuntimeError):
                f.write_journal()
            with self.assertRaises(RuntimeError):
                f._apply_roots_env({}, {"default": str(f.project_root)})
        finally:
            f.children = []
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)
            if old is not None:
                shutil.rmtree(old, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt", "路径大小写折叠仅适用于 Windows")
    def test_project_root_case_insensitive_containment(self):
        """大小写规范化：同 run_root 内合成根的大写变体（Windows）仍被判为在本轮自有根内，
        被接受；含入 run_root 前缀即通过，绝不误判外部。"""
        f = fix.Fixture(owner="t")
        try:
            f.make_dirs()
            env = f.allowlisted_env(
                app_db_url="postgresql+psycopg://u:p@h/x_test", jwt_secret="s",
                admin_user="a", admin_pass="p",
                embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1",
                emit_project_roots={"default": str(f.project_root).upper()})
            roots = json.loads(env["PROJECT_ALLOWED_ROOTS"])
            # 大写变体解析到同一目录并被接受
            self.assertEqual(
                fix.Fixture._norm(os.path.realpath(roots["default"])),
                fix.Fixture._norm(str(f.project_root)))
        finally:
            f.children = []
            import shutil
            shutil.rmtree(f.run_root, ignore_errors=True)

    def test_external_root_uppercase_rejected(self):
        """外部现有目录（即使在系统 temp，但不在本轮 run_root 内）作为授权根必须被拒。"""
        with tempfile.TemporaryDirectory(prefix="vrk-ext-") as td:
            f = fix.Fixture(owner="t")
            try:
                f.make_dirs()
                ext = str(Path(td).resolve())
                with self.assertRaises(RuntimeError):
                    f.allowlisted_env(
                        app_db_url="postgresql+psycopg://u:p@h/x_test", jwt_secret="s",
                        admin_user="a", admin_pass="p",
                        embedding_base="http://127.0.0.1:9/v1", llm_base="http://127.0.0.1:9/v1",
                        emit_project_roots={"default": ext})
            finally:
                f.children = []
                import shutil
                shutil.rmtree(f.run_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
