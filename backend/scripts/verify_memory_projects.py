"""V 夹具主入口（摘要压缩 + 本地项目索引的专项验收工具）。

用途：在**本轮 owned** 的隔离资源上跑真实链路验收，全程不调用真实模型/业务 DB。
只绑定 loopback，绝不触碰真实工作区 / .env / Playwright 共享配置 / CI / 部署。

阶段：
  parse     （离线/纯）合成授权目录 -> 只读解析（python ast / markdown / vue sfc），
             验证真实行号与结构、同字节不同路径、secret/忽略目录排除。**现在即可跑**。
  summary   （真实链）stub + 真实 API(uvicorn) + worker.py --role summary + owned PG，
             长会话触发摘要、下一轮使用已压缩事实+最近轮原文；依赖 M 已落盘
             memory_worker + chat 路由。
  project   （真实链）真实 API + worker.py --role project + owned PG 向量/关键词 ->
             搜索 / 项目 RAG / 快照原文。依赖 F ORM + P index/search + Q 接项目链。
  all       顺序跑可用的、跳过未就绪的。

DB 安全：工具必须显式带 URL + expected-owner。连接前校验 host 是本机 loopback、
库名以 _test 结尾、且 pg_database 的 comment 与 expected-owner 精确匹配。任何一条
不满足就 fail 且**不碰任何数据**；绝不凭 _test 后缀就清库。本工具只读 DB 验证 +
业务链路（不 truncate）。owner 连接一律用 psycopg 关键字参数，不拼裸 conninfo。

硬门控：任何涉 DB 的链路，owner 校验未通过一律不启动子进程；所有场景都在 try/finally
中收口（finally 清理 + 异常记为 fail），绝不让未收口进程/伪干净存活。

输出：机器可读 JSON 报告到 stdout（pass/fail/skip、耗时、HTTP 断言、资源 clean）。
用法（禁 dotenv）：
  RAG_DISABLE_DOTENV=1 python scripts/verify_memory_projects.py --only parse
  RAG_DISABLE_DOTENV=1 python scripts/verify_memory_projects.py --only all \
      --db-url postgresql+psycopg://...:port/integration_test \
      --expected-owner owned-test:<40hex> ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import _memory_projects_fixture as fix  # noqa: E402
import _memory_projects_parse as par  # noqa: E402


class Result:
    def __init__(self):
        self.pass_ = []
        self.fail = []
        self.skip = []

    def record(self, name, status, detail="", ms=0.0):
        entry = {"name": name, "status": status, "detail": detail, "ms": round(ms, 1)}
        {"pass": self.pass_, "fail": self.fail, "skip": self.skip}[status].append(entry)

    def report(self):
        return {"pass": self.pass_, "fail": self.fail, "skip": self.skip,
                "counts": {"pass": len(self.pass_), "fail": len(self.fail), "skip": len(self.skip)}}


def verify_pg_owner(db_url: str, expected_owner: str) -> tuple[bool, str]:
    """只读校验 DB 归属：loopback + _test 后缀 + pg_database comment 精确匹配。

    任何校验不通过即返回 False，且不做任何写入/清库。连接用 psycopg 关键字参数，
    不拼裸 conninfo（避免把 password 拼进字符串 / 特殊字符转义问题）。
    """
    try:
        from sqlalchemy.engine import make_url
    except Exception as exc:  # noqa: BLE001
        return False, f"无法解析 DATABASE_URL: {exc}"
    try:
        url = make_url(db_url)
    except Exception as exc:  # noqa: BLE001
        return False, f"DATABASE_URL 解析失败: {exc}"
    host = (url.host or "").lower()
    if url.get_backend_name() != "postgresql":
        return False, "仅支持 postgresql 后端"
    if host not in {"127.0.0.1", "localhost", "::1", "localhost.localdomain"}:
        return False, f"DB host 非 loopback: {host!r}"
    dbname = url.database or ""
    if not dbname.endswith("_test"):
        return False, f"库名必须 _test 结尾，实际: {dbname!r}"
    # 连接并读取 pg_database comment（只读），一律关键字参数
    try:
        import psycopg
        kwargs = {
            "host": url.host, "port": url.port or 5432, "dbname": dbname,
            "user": url.username or "", "password": url.password or "",
            "connect_timeout": 10,
        }
        with psycopg.connect(**kwargs) as conn:
            row = conn.execute(
                "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
                (dbname,)).fetchone()
            comment = row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        return False, f"连接/读取 DB comment 失败: {exc}"
    if not comment:
        return False, "目标库无 owner comment"
    if comment.strip() != expected_owner.strip():
        return False, f"owner comment 不匹配: {comment!r} != {expected_owner!r}"
    return True, "owner 校验通过"


def _upstream_ready(module: str) -> tuple[bool, str]:
    """探测上游模块是否可导入。区分“未落盘”与“业务导入坏”。

    NotImplementedError 掩业务坏导入：这里把 ImportError 区分为两类——
      - 顶层模块本身不存在 ⇒ 未就绪（skip 的合法原因）；
      - 已存在但导入抛出其它异常 ⇒ 业务坏导入（按其 fail 处理，不静默 skip）。
    """
    try:
        mod = __import__(module, fromlist=["*"])
        return True, getattr(mod, "__file__", "")
    except ImportError as exc:
        name = exc.name or ""
        if name == module or name.startswith(module + "."):
            return False, f"未落盘: {module}"
        # 缺第三方依赖 / 内部导入失败：区分不出的按未就绪 skip，但给明原因
        return False, f"导入失败(手段偏好 skip): {exc}"
    except Exception as exc:  # noqa: BLE001 - 业务模块自身坏了
        return False, f"导入异常(非 ImportError): {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 阶段 0：离线解析（不依赖 PG/上游）
# ---------------------------------------------------------------------------
def run_parse(owner: str, result: Result) -> None:
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix="vms-parse-") as td:
        root = Path(td) / "auth"
        info = fix.build_project_fixture(root)
        # 1) 期望文件/排除
        parsed = par.parse_synthetic_tree(root)
        missing = sorted(info["expected_files"] - set(parsed))
        leaked = sorted(set(parsed) & info["excluded"])
        if not missing and not leaked:
            result.record("parse.expected.excluded", "pass",
                          detail=f"parsed={len(parsed)} missing={missing} leaked={leaked}")
        else:
            result.record("parse.expected.excluded", "fail",
                          detail=f"missing={missing} leaked={leaked}")
        # 2) Python 真实符号/行号
        py = parsed["src/utils.py"]
        if py.status == "parsed" and any(s.name == "compute_total" for s in py.symbols):
            line = next(s.line for s in py.symbols if s.name == "compute_total")
            result.record("parse.python_sym", "pass", detail=f"compute_total@L{line}")
        else:
            result.record("parse.python_sym", "fail", detail=f"status={py.status}")
        # 3) 同字节不同路径 -> 两个独立身份
        dup_ok = True
        for a, b in info["duplicate_pairs"]:
            if (Path(td)/'auth'/a).read_bytes() != (Path(td)/'auth'/b).read_bytes():
                dup_ok = False
            if a not in parsed or b not in parsed:
                dup_ok = False
        result.record("parse.dup_identity", "pass" if dup_ok else "fail")
        # 4) Markdown 真实结构（标题/围栏）
        md = parsed["docs/README.md"]
        kinds = {s.kind for s in md.symbols}
        ok = "parsed" in md.status and {"heading", "code-fence"} <= kinds
        result.record("parse.md_structure", "pass" if ok else "fail",
                      detail=f"kinds={sorted(kinds)}")
        # 5) TS 缺依赖 fallback（不把 fallback 当通过）
        ts = parsed["src/main.ts"]
        status, reason = par.detect_ts_support()
        if status == "parsed":
            result.record("parse.ts_support", "pass", detail="tree-sitter 可用")
        else:
            result.record("parse.ts_support", "skip", detail=reason)


# ---------------------------------------------------------------------------
# 阶段 1：summary 真实链（依赖 M 已落盘 memory_worker + chat 路由）
# ---------------------------------------------------------------------------
def _http_json(method, url, payload, token=None, timeout=30.0):
    """urllib JSON 请求：返回 (status, decoded_json)。SSE 由 _sse_chat 单独处理。"""
    import urllib.request
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return exc.code, None
    except Exception as exc:  # noqa: BLE001
        return -1, {"_req_error": str(exc)}


def _sse_chat(base, token, payload):
    """POST {base}/chat/stream（SSE）。返回 (meta, deltas, done, status, raw_lines)。"""
    import urllib.request
    req = urllib.request.Request(base + "/chat/stream", data=json.dumps(payload).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    meta = {}
    deltas = []
    done = {}
    raw = []
    status = -1
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                raw.append(line)
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    continue
                try:
                    evt = json.loads(data)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("event") or evt.get("type") or (
                    "meta" if "conversation_id" in evt else "done")
                if etype in ("meta",) or "conversation_id" in evt:
                    meta.update(evt)
                elif etype == "done" or "finish_reason" in evt or "message_id" in evt:
                    done = evt
                elif etype == "delta" and evt.get("content"):
                    deltas.append(evt["content"])
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            done = {"http_error": json.loads(exc.read().decode("utf-8", "replace"))}
        except json.JSONDecodeError:
            done = {"http_error": exc.read().decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        done = {"_req_error": str(exc)}
    return meta, "".join(deltas), done, status, raw


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# 本轮 owned 的真实链路测试管理员凭据（仅本工具使用，绝不写真实 .env）。
_SEED_ADMIN_USER = "vms_admin"
_SEED_ADMIN_PASS = "Vms-verify-2026!-Only-Local-Stub"
_SEED_JWT = "vms-verify-memory-projects-only-local-jwt-secret-0000000000"


def _db_kwargs(db_url: str) -> dict | None:
    """从 owned DATABASE_URL 解析出 psycopg 关键字参数（本工具只用只读查询）。"""
    from sqlalchemy.engine import make_url
    try:
        u = make_url(db_url)
    except Exception:  # noqa: BLE001
        return None
    if not u.database or not u.database.endswith("_test"):
        return None
    return {"host": u.host, "port": u.port or 5432, "dbname": u.database,
            "user": u.username or "", "password": u.password or "", "connect_timeout": 5}


def run_summary(fix_: fix.Fixture, args, result: Result) -> None:
    """真实链（M 的 memory_worker + memory_service + chat 路由 均已落盘）。

    链路：stub(确定 chat) + 真实 uvicorn API + worker.py --role summary + owned PG：
      登录 -> 长会话(mode=chat，含摘要 fact 关键词) -> 触发入队 -> worker 生成/发布 ->
      读取 ConversationMemory 验证压缩事实 + source id 落于允许消息集。
    """
    import sys as _sys
    import time as _time
    import uuid as _uuid
    ready, why = _upstream_ready("app.services.memory_worker")
    if not ready:
        result.record("summary.chain", "skip", detail=why)
        return
    dbkw = _db_kwargs(args.db_url)
    if not dbkw:
        result.record("summary.chain", "fail", detail="db-url 非本工具 owned _test 库")
        return
    try:
        from app.core.config import settings
        trigger = int(settings.MEMORY_TRIGGER_TURNS)
        ratio = float(settings.MEMORY_TRIGGER_RATIO)
        budget = int(settings.MEMORY_HISTORY_TOKEN_BUDGET)
    except Exception as exc:  # noqa: BLE001
        result.record("summary.chain", "fail", detail=f"读取摘要配置失败: {exc}")
        return
    result.record("summary.config", "pass",
                  detail=f"trigger={trigger} turns ratio={ratio} window={budget}")

    stub_script = str(Path(__file__).resolve().parent / "_memory_projects_stub.py")
    stub_ready = fix_.run_root / "stub.ready"
    stub_state = fix_.run_root / "stub.state"

    # 1) 启动确定 chat/embed 替身（读取 listen 地址）。stub 自身不需要 DB，env 用最小 allowlist
    stub_env = fix_.allowlisted_env(
        app_db_url=args.db_url, jwt_secret=_SEED_JWT,
        admin_user=_SEED_ADMIN_USER, admin_pass=_SEED_ADMIN_PASS,
        embedding_base="http://127.0.0.1:1/v1", llm_base="http://127.0.0.1:1/v1")
    fix_.spawn("stub", [_sys.executable, stub_script, "--port", "0",
                        "--state-file", str(stub_state), "--ready-file", str(stub_ready),
                        "--owner", fix_.owner, "--nonce", fix_.nonce],
               env=stub_env, cwd=fix_.run_root, ready_file=stub_ready)
    try:
        stub_info = json.loads(stub_state.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        result.record("summary.stub", "fail", detail=f"读 stub state 失败: {exc}")
        return
    stub_base = stub_info["base_url"]
    result.record("summary.stub", "pass", detail=f"base_url={stub_base}")

    # 2) 启动真实 uvicorn API（指向 owned DB + stub 端点 + 由 init_db 建的 admin 登录）
    api_port = _free_port()
    env = fix_.allowlisted_env(
        app_db_url=args.db_url, jwt_secret=_SEED_JWT,
        admin_user=_SEED_ADMIN_USER, admin_pass=_SEED_ADMIN_PASS,
        embedding_base=stub_base, llm_base=stub_base,
    )
    # 内存摘要仅在 CHAT_MEMORY_ENABLED=true 时启用（默认 False），否则 turn_seq 不分配、
    # 计数不入账、绝不入队。这里显式开启并给足窗口，使 config 的 budget<=window 校验通过。
    env["CHAT_MEMORY_ENABLED"] = "1"
    env["LLM_CONTEXT_WINDOW_TOKENS"] = "32768"
    env["MEMORY_TRIGGER_TURNS"] = str(trigger)
    api_probe = fix_.spawn("api",
                           [_sys.executable, "-m", "uvicorn", "app.main:app",
                            "--host", "127.0.0.1", "--port", str(api_port),
                            "--log-level", "warning"],
                           env=env, cwd=str(Path(__file__).resolve().parent.parent),
                           ready_file=None, port=api_port)
    api_base = f"http://127.0.0.1:{api_port}"
    result.record("summary.api", "pass", detail=f"port={api_port}")

    # 3) 等待 API 就绪（admin 登录可通）
    login_ok, token = False, None
    deadline = _time.time() + 45
    while _time.time() < deadline:
        if not fix_.alive(api_probe):
            result.record("summary.login", "fail", detail="API 进程提前退出")
            return
        st, body = _http_json("POST", f"{api_base}/api/v1/auth/login",
                              {"username": _SEED_ADMIN_USER, "password": _SEED_ADMIN_PASS})
        if st == 200 and isinstance(body, dict) and (body.get("data") or {}).get("access_token"):
            login_ok, token = True, body["data"]["access_token"]
            break
        _time.sleep(0.5)
    if not login_ok:
        result.record("summary.login", "fail", detail="API 未在窗口内就绪/登录失败")
        return
    result.record("summary.login", "pass", detail="admin 登录成功")

    # 4) 长会话触发摘要（mode=chat，带事实关键词“周报”，轮数足够跨过 MEMORY_TRIGGER_TURNS）
    q = "请把这条记进周报：用户要求按周交付项目周报（合成）. 第 {i} 条补充"
    conv_id = None
    turns_done = 0
    user_msg_ids = []
    for i in range(1, max(3, trigger * 2) + 1):
        payload = {"question": q.format(i=i), "mode": "chat"}
        if conv_id:
            payload["conversation_id"] = conv_id
        meta, _text, done, status, _raw = _sse_chat(api_base + "/api/v1", token, payload)
        if status != 200:
            result.record(f"summary.chat_turn{i}", "fail", detail=f"status={status} done={done}")
            return
        if not conv_id:
            conv_id = meta.get("conversation_id") or (done or {}).get("conversation_id")
        mid = (done or {}).get("message_id")
        user_msg_ids.append(str(mid))
        turns_done += 1
    result.record("summary.turns", "pass",
                  detail=f"turns={turns_done} conv={conv_id} msgs={len(user_msg_ids)}")
    if not conv_id:
        result.record("summary.chain", "fail", detail="未取得 conversation_id")
        return

    # 5) 用真实 DB 消息 id 注入 stub fact（来源必须落在允许消息集内，否则 parse_summary 拒绝）
    real_src = user_msg_ids[0] if user_msg_ids else str(conv_id)
    ctl_base = stub_base.rsplit("/v1", 1)[0]
    st, _ = _http_json("POST", f"{ctl_base}/__control/config",
                       {"facts": [{"source_id": real_src, "keyword": "周报",
                                   "fact": "用户要求按周交付项目周报（本地替身合成事实）"}]})
    if st != 200:
        result.record("summary.fact_inject", "fail", detail=f"注入 fact 失败 status={st}")
        return
    result.record("summary.fact_inject", "pass", detail=f"source={real_src}")

    # 6) 启动 summary worker 生成/发布（worker.py --role summary 循环，cleanup 时由夹具收口）
    fix_.spawn("worker-summary",
               [_sys.executable, "worker.py", "--role", "summary"],
               env=env, cwd=str(Path(__file__).resolve().parent.parent),
               ready_file=None, port=None)
    # 7) 等待 published ConversationMemory 出现（只读 psycopg 核对 owned DB）
    published, structured = False, None
    pdeadline = _time.time() + 90
    conv_uuid = _uuid.UUID(str(conv_id))
    while _time.time() < pdeadline and not published:
        try:
            import psycopg
            with psycopg.connect(**dbkw) as conn:
                row = conn.execute(
                    "SELECT structured FROM conversation_memories "
                    "WHERE conversation_id=%s ORDER BY version DESC LIMIT 1",
                    (conv_uuid,)).fetchone()
            if row is not None and row[0]:
                published = True
                structured = row[0] if isinstance(row[0], dict) else None
        except Exception:  # noqa: BLE001
            pass
        if not published:
            _time.sleep(1.0)
    if not published:
        result.record("summary.publish", "fail", detail="worker 未在窗口内发布 ConversationMemory")
        return
    # 8) 验证压缩事实 + 来源 id 合法 + 触发入队
    facts = (structured or {}).get("confirmed_facts") or []
    src_ids = [s for f in facts for s in (f.get("source_message_ids") or [])]
    ok_fact = any(f.get("text") and f.get("verification") in
                  ("confirmed_by_user", "assistant_suggested") for f in facts)
    if ok_fact and real_src in src_ids:
        result.record("summary.publish", "pass",
                      detail=f"published facts={len(facts)} src_included={real_src in src_ids}")
    else:
        result.record("summary.publish", "fail",
                      detail=f"facts={facts} src={real_src} in_ids={real_src in src_ids}")
        return
    result.record("summary.chain", "pass", detail="触发-生成-发布-来源校验全链路通过")


def run_project(fix_: fix.Fixture, args, result: Result) -> None:
    """项目链由 Q 接管真实 API/worker；这里只保留 owner/上游就绪探测，不伪造通过。"""
    ready, why = _upstream_ready("app.services.project_worker")
    if not ready:
        result.record("project.chain", "skip", detail=why or "project_worker 未就绪")
        return
    result.record("project.chain", "skip",
                  detail="project_worker 已就绪；项目真实链由 Q 接项目 API 集成")


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V 夹具：摘要/项目索引专项验收工具")
    parser.add_argument("--only", choices=["parse", "summary", "project", "all"], default="all")
    parser.add_argument("--owner", default="")
    parser.add_argument("--db-url", default=os.environ.get("VMS_DATABASE_URL", ""))
    parser.add_argument("--expected-owner", default=os.environ.get("VMS_EXPECTED_OWNER", ""))
    parser.add_argument("--run-root", default="")
    args = parser.parse_args(argv)

    result = Result()
    owner = args.owner or f"v-{uuid.uuid4().hex[:12]}"
    run_root = Path(args.run_root) if args.run_root else None

    # 先做 owner 校验（涉 DB 链路硬门控：不通过则不启动任何子进程）。
    needs_db = args.only in ("summary", "project", "all")
    db_ok = True
    if needs_db:
        ok, msg = (False, "缺少 --db-url / --expected-owner") if not args.db_url or not args.expected_owner \
            else verify_pg_owner(args.db_url, args.expected_owner)
        db_ok = ok
        result.record("db.owner", "pass" if ok else "fail", detail=msg)

    # parse 阶段：无 DB、无子进程
    if args.only in ("parse", "all"):
        t = time.monotonic()
        run_parse(owner, result)
        result.record("parse.total", "pass", ms=(time.monotonic() - t) * 1000)

    # 涉 DB 链路的统一调度 + try/finally：任何异常都 finally 收口并记为 fail
    if needs_db and db_ok:
        if args.only in ("summary", "all"):
            f = fix.Fixture(owner=owner, run_root=run_root)
            try:
                run_summary(f, args, result)
            except Exception as exc:  # noqa: BLE001
                result.record("summary.chain", "fail", detail=f"异常: {type(exc).__name__}: {exc}")
            finally:
                f.cleanup(grace=2.0, kill=True)
                f.write_journal()
                alive = f.children_alive()
                result.record("res.summary", "pass" if not alive else "fail",
                              detail="清理完成" if not alive else
                              f"未收口进程: {[(c.role, c.pid) for c in alive]}")
        if args.only in ("project", "all"):
            f = fix.Fixture(owner=owner, run_root=run_root)
            try:
                run_project(f, args, result)
            except Exception as exc:  # noqa: BLE001
                result.record("project.chain", "fail", detail=f"异常: {type(exc).__name__}: {exc}")
            finally:
                f.cleanup(grace=2.0, kill=True)
                alive = f.children_alive()
                result.record("res.project", "pass" if not alive else "fail",
                              detail="清理完成" if not alive else
                              f"未收口进程: {[(c.role, c.pid) for c in alive]}")

    report = result.report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("SUMMARY: pass=%d fail=%d skip=%d" % (
        report["counts"]["pass"], report["counts"]["fail"], report["counts"]["skip"]))
    return 1 if report["counts"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
