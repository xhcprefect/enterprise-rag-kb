"""E2E 环境编排：独占 PostgreSQL 容器 + 真实 API/worker + 回环模型替身 + 生产构建预览。

设计约束（与仓库现有测试基础设施一致，不要为了省事绕过）：

* 所有监听地址都是 127.0.0.1；模型只有本仓库的确定性替身 ``e2e_model_stub.py``。
* 数据库是本次运行独占的 Docker 容器，容器带 ``io.ragkb.e2e.owner`` 标签。
  清理时先核对标签与名字，绝不按 ``*_test`` 后缀之类的弱特征删别人的资源。
* 子进程环境**从头构造**：只从父进程继承运行所必需的少量变量，其余一律显式设置。
  父进程里可能存在的真实模型地址/密钥、DATABASE_URL 等一律不带入。
* ``RAG_DISABLE_DOTENV=1`` 在导入 ``app.*`` 之前生效；子进程工作目录在临时目录，
  即便这个开关回归，那里也没有 ``.env`` 可读。
* 正常退出、启动失败、测试失败、Ctrl+C 都走同一个 teardown；失败路径先写标记再清理。

用法（供 Playwright globalSetup / CI / 手工调试共用）::

    python -X utf8 backend/scripts/e2e_env.py up --out <临时目录>
    python -X utf8 backend/scripts/e2e_env.py up --out <临时目录> --no-web   # 只起 API 链路
    python -X utf8 backend/scripts/e2e_env.py down --out <临时目录>          # 清理残留

``up`` 启动完成后写 ``manifest.json`` 与 ``READY``，然后阻塞等待 ``STOP`` 文件或信号。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

OWNER_LABEL = "io.ragkb.e2e.owner"
NONCE_LABEL = "io.ragkb.e2e.nonce"
CONTAINER_PREFIX = "ragkb-e2e-"
DEFAULT_API_PORT = 0  # 0 = 由系统分配后再探测
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
ATTESTATION_SCHEMA = "ragkb-e2e-attestation"

# 只继承这些变量：少了它们 Node/Python/Windows 起不来，多了就可能把真实凭据带进测试。
ALLOWED_PARENT_ENV = (
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
    "TEMP", "TMP", "TMPDIR",
    "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "HOMEDRIVE", "HOMEPATH", "HOME", "USER", "USERNAME",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432",
    "OS", "LANG", "LC_ALL", "LANG_OVERRIDE",
)

# 显式拒绝：即使有人把父进程变量加进 allowlist，这些名字也永远不继承。
SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "DATABASE_URL")

UVICORN_BOOT = """
import sys
sys.path.insert(0, sys.argv[1])
import uvicorn
uvicorn.run("app.main:app", host=sys.argv[2], port=int(sys.argv[3]),
            log_level="info", access_log=True, timeout_graceful_shutdown=5)
"""

# 只在 harness 启动的 API 进程里注入的只读 attestation。
#
# 为什么不在 app 里加路由：main.py/业务 router 属于其他 owner，且生产进程绝不该
# 暴露这个端点。这里在 boot 脚本中先导入真实 app，再挂一个 /__e2e__/attestation
# 路由——业务路由、鉴权、依赖全部保持原样，只多一个只读探针。
# 它证明"这个 API 进程是我这次启动的、且模型指向我这次启动的替身"，从而让
# capacity/Playwright 在发任何聊天请求前就能拒绝错 owner 或陈旧地址。
ATTEST_BOOT = """
import sys
sys.path.insert(0, sys.argv[1])
import uvicorn
from fastapi import FastAPI
from app.main import app as real_app

OWNER = sys.argv[4]
NONCE = sys.argv[5]
STUB = sys.argv[6]

class AttestedApp:
    \"\"\"只读 attestation 包装：除 /__e2e__/attestation 外全部转交真实 app。\"\"\"

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/__e2e__/attestation":
            body = b'{"schema":"ragkb-e2e-attestation","owner":"' + OWNER.encode() + \\
                   b'","nonce":"' + NONCE.encode() + b'","stub_base_url":"' + STUB.encode() + \\
                   b'","app":"app.main:app"}'
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.inner(scope, receive, send)

uvicorn.run(AttestedApp(real_app), host=sys.argv[2], port=int(sys.argv[3]),
            log_level="info", access_log=True, timeout_graceful_shutdown=5)
"""


def attestation_payload(owner: str, nonce: str, stub_base_url: str) -> dict[str, str]:
    return {"schema": ATTESTATION_SCHEMA, "owner": owner, "nonce": nonce,
            "stub_base_url": stub_base_url.rstrip("/"), "app": "app.main:app"}


def verify_attestation(payload: Any, *, owner: str, nonce: str, stub_base_url: str) -> None:
    """校验 API 就是我们本次启动的那个进程；任何不符都 fail-closed。

    ``/health`` 任何 API 都有，不能作为身份证明；只有 harness 注入的 nonce
    同时出现在 API 与 manifest 里，才能说明"这个进程由本轮启动"。
    """
    if not isinstance(payload, dict):
        raise ValueError("attestation 响应不是对象")
    expected = attestation_payload(owner, nonce, stub_base_url)
    for key in ("schema", "owner", "nonce"):
        if payload.get(key) != expected[key]:
            raise ValueError(f"API attestation 不匹配（{key}）：{payload.get(key)!r} != {expected[key]!r}")
    if (payload.get("stub_base_url") or "").rstrip("/") != expected["stub_base_url"]:
        raise ValueError(
            f"API 的模型地址与本次替身不一致：{payload.get('stub_base_url')!r} != {expected['stub_base_url']!r}"
        )


def verify_stub_owner(state: Any, *, owner: str, nonce: str) -> None:
    """替身控制端点的 counts 只能证明"像替身"，owner/nonce 才证明"是本次替身"。"""
    if not isinstance(state, dict):
        raise ValueError("替身状态不是对象")
    if state.get("owner") != owner:
        raise ValueError(f"替身 owner 不匹配：{state.get('owner')!r} != {owner!r}")
    if state.get("nonce") != nonce:
        raise ValueError("替身 nonce 不匹配：指向了别的运行的替身")

WORKER_ROLES = {
    "worker": "ingest",
    "worker-summary": "summary",
    "worker-project": "project",
    # 第四角色：独立进程消费 feedback regression 在线作业。
    "worker-regression": "regression",
}

WORKER_BOOT = """
import os, runpy, sys
backend, role = sys.argv[1:3]
sys.path.insert(0, backend)
worker_path = os.path.join(backend, "worker.py")
# nonce 留在 OS 命令行供清理核验，生产 argparse 只接收真实角色参数。
sys.argv = [worker_path, "--role", role]
runpy.run_path(worker_path, run_name="__main__")
"""


# --------------------------------------------------------------------------- 环境


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def sanitize_env(overrides: dict[str, str], parent: dict[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境：白名单继承 + 显式覆盖，绝不整份复制父进程环境。

    ``overrides`` 是调用方显式声明的测试配置，因此即使名字里带 KEY/TOKEN
    （如 ``LLM_API_KEY`` 的本地替身）也允许写入；继承侧则一律拦截。
    """
    source = os.environ if parent is None else parent
    env: dict[str, str] = {}
    for name in ALLOWED_PARENT_ENV:
        value = source.get(name)
        if value is not None and not is_secret_name(name):
            env[name] = value
    for name, value in overrides.items():
        if value is None:
            continue
        env[name] = str(value)
    return env


def assert_no_leaked_secrets(env: dict[str, str], allowed_keys: Iterable[str]) -> None:
    """检查子进程环境里没有把父进程的真实模型地址/密钥带进来。"""
    allowed = set(allowed_keys)
    for name in env:
        if name in allowed or name in ALLOWED_PARENT_ENV:
            continue
        if is_secret_name(name):
            raise ValueError(f"E2E 子进程环境含未声明的敏感变量: {name}")


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def assert_loopback_url(url: str, label: str = "url") -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{label} 必须是 http(s) 地址: {url}")
    if not is_loopback_host(parsed.hostname or ""):
        raise ValueError(f"{label} 必须指向回环地址，拒绝: {url}")
    return url


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate: Callable[[], Any], timeout: float, interval: float = 0.2, what: str = "条件") -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - 轮询期间任何异常都只代表"还没好"
            last_error = exc
        time.sleep(interval)
    detail = f"（最后一次错误: {last_error}）" if last_error else ""
    raise TimeoutError(f"等待{what}超时 {timeout:.1f}s{detail}")


def http_json(url: str, timeout: float = 3.0, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- Docker


def docker_bin() -> str:
    return os.environ.get("RAG_E2E_DOCKER") or "docker"


def _docker(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [docker_bin(), *args],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


def container_name(token: str) -> str:
    return f"{CONTAINER_PREFIX}{token}"


def owns_container(labels: dict[str, str], name: str, token: str) -> bool:
    """只有标签与本次运行 token 完全一致、且名字符合前缀时才算自己的容器。"""
    return labels.get(OWNER_LABEL) == token and name == container_name(token)


def parse_published_port(text: str) -> int:
    """``docker port`` 输出形如 ``127.0.0.1:49153``，取最后一段端口。"""
    candidate = text.strip().splitlines()[0].strip() if text.strip() else ""
    port = candidate.rsplit(":", 1)[-1]
    if not port.isdigit():
        raise ValueError(f"无法解析 Docker 发布端口: {text!r}")
    return int(port)


# --------------------------------------------------------------- owner / 目录安全

# owner token 会进入容器名、库名和目录标记，因此只能是短 ASCII 标识；
# 任意字符串拼进 `docker run --name` 或路径都是注入面。
OWNER_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{3,31}")
DIR_MARKER_NAME = ".ragkb-e2e-out"
DIR_MARKER_SCHEMA = "ragkb-e2e-output"
REPO_ROOT_MARKERS = ("backend", "frontend", "docker-compose.yml")

# 这些目录即便带标记也绝不递归删除：仓库本身、仓库祖先、用户主目录、盘根。
PROTECTED_ANCESTOR_NAMES = {"users", "windows", "program files", "program files (x86)"}


def validate_owner(token: str) -> str:
    """校验 owner 形态；不合法就直接拒绝，不拼进容器名/库名/路径。"""
    if not isinstance(token, str) or not OWNER_PATTERN.fullmatch(token):
        raise ValueError(
            f"owner 只能是小写字母/数字/短横线，长度 4–32: {token!r}"
        )
    # 纯数字或前缀让它和容器名/库名混淆时也不好排查，但不影响安全，允许。
    return token


def new_owner() -> str:
    return secrets.token_hex(6)


def _repo_root_for(path: Path) -> Path | None:
    """从某个目录向上找仓库根（同时有 backend/frontend/compose 才认）。"""
    current = path.resolve()
    for candidate in [current, *current.parents]:
        if all((candidate / marker).exists() for marker in REPO_ROOT_MARKERS):
            return candidate
    return None


def assert_safe_output_dir(path: Path) -> Path:
    """输出目录必须位于仓库之外、不是盘根/主目录，才允许后续创建与删除。

    这条检查与"是否为空"分开：即便目录是我们自己刚建的，落在仓库里也可能
    被下一次误操作当成源码删除，因此直接拒绝。
    """
    resolved = path.resolve()
    # 盘根：resolved.parent == resolved（POSIX）或与驱动器根重合（Windows）。
    if resolved.parent == resolved or (resolved.drive and resolved == Path(resolved.drive + os.sep)):
        raise ValueError(f"拒绝使用盘符根目录作为 E2E 输出目录: {resolved}")
    if resolved == Path.home().resolve():
        raise ValueError(f"拒绝使用用户主目录作为 E2E 输出目录: {resolved}")
    # 仓库内（含仓库根）一律拒绝：输出含 uploads/日志/构建产物，不能被当源码误删或误提交。
    repo = _repo_root_for(resolved) or REPO_ROOT.resolve()
    if resolved == repo or repo in resolved.parents:
        raise ValueError(f"拒绝在仓库目录内创建 E2E 输出目录: {resolved}（仓库: {repo}）")
    return resolved


def read_dir_marker(path: Path) -> dict | None:
    marker = path / DIR_MARKER_NAME
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def assert_owned_output_dir(path: Path, token: str, nonce: str | None = None) -> Path:
    """确认目录由本轮本次 owner 原子创建，才允许写入或删除。

    manifest 是普通文件、可被改写，因此"目录归谁"不能只看 manifest：
    目录里另有一个启动时原子写入的标记文件，重复校验 schema、owner 与规范路径。
    给了 nonce 时还要求标记里的 nonce 一致，防止"同名 owner 的另一次运行"。
    """
    resolved = assert_safe_output_dir(path)
    marker = read_dir_marker(resolved)
    if marker is None:
        raise ValueError(f"输出目录缺少本次运行的标记文件，拒绝操作: {resolved / DIR_MARKER_NAME}")
    if marker.get("schema") != DIR_MARKER_SCHEMA:
        raise ValueError(f"输出目录标记 schema 不符，拒绝操作: {resolved}")
    if marker.get("owner") != token:
        raise ValueError(
            f"输出目录属于另一个 owner（标记={marker.get('owner')!r} 本次={token!r}），拒绝操作: {resolved}"
        )
    if nonce is not None and marker.get("nonce") != nonce:
        raise ValueError(f"输出目录属于同一个 owner 的另一次运行（nonce 不符），拒绝操作: {resolved}")
    recorded = marker.get("path")
    if not isinstance(recorded, str) or Path(recorded).resolve() != resolved:
        raise ValueError(f"输出目录标记的路径与实际路径不一致，拒绝操作: {resolved}")
    return resolved


def prepare_output_dir(path: Path, token: str, nonce: str) -> Path:
    """原子地创建并登记本次运行的输出目录；**任何已存在路径一律拒绝**。

    安全默认必须偏保守：空目录也可能属于别人（例如用户刚 mkdir 准备放资料），
    而复用后 ``stop``/``rmtree`` 会把这个目录整体删掉。因此这里只接受一个
    尚不存在的路径，由本函数创建；需要复用请显式传入一个新子目录。

    标记里同时写入本次 nonce：崩溃后 ``down`` 只有拿到 nonce 才能证明某个进程属于
    本次运行（PID 会被复用，光看进程名/端口不足以判断归属）。
    """
    resolved = assert_safe_output_dir(path)
    if resolved.exists() or resolved.is_symlink():
        raise ValueError(f"输出路径已存在，拒绝复用/覆盖（请换一个不存在的新目录）: {resolved}")
    resolved.mkdir(parents=True, exist_ok=False)
    try:
        write_dir_marker(resolved, token, nonce)
    except OSError:
        # 刚创建的空目录还没登记成功：只能收回自己刚建的空目录。
        try:
            resolved.rmdir()
        except OSError:
            pass
        raise
    return resolved


def write_dir_marker(path: Path, token: str, nonce: str) -> None:
    payload = {"schema": DIR_MARKER_SCHEMA, "owner": token, "nonce": nonce,
               "path": str(path.resolve()), "created_at": round(time.time(), 3)}
    target = path / DIR_MARKER_NAME
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def remove_owned_output_dir(path: Path, token: str, nonce: str | None = None) -> bool:
    """只删除带本轮标记的输出目录；返回**实际是否已删除**。

    不能 ``shutil.rmtree(..., ignore_errors=True)`` 后无条件返回 True：Windows 上
    被占用的文件、权限问题都会让删除悄悄失败，报告却声称"已清理"。这里收集错误
    并在删除后复查目录是否真的不见了。
    """
    try:
        resolved = assert_owned_output_dir(path, token, nonce)
    except ValueError as exc:
        print(f"[e2e] 拒绝删除输出目录: {exc}", file=sys.stderr)
        return False
    errors: list[str] = []

    def _on_error(_func, target, exc_info):  # noqa: ANN001
        errors.append(f"{target}: {exc_info[1]}")

    shutil.rmtree(resolved, onerror=_on_error)
    if resolved.exists():
        detail = f"（{errors[0]}）" if errors else ""
        print(f"[e2e] 输出目录删除未完成，保留现场: {resolved}{detail}", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------- 进程


@dataclass
class Child:
    name: str
    process: subprocess.Popen[bytes]
    log_path: Path

    def alive(self) -> bool:
        return self.process.poll() is None

    def terminate(self, timeout: float = 10.0) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
        except Exception:  # noqa: BLE001
            return
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


@dataclass
class Orchestrator:
    out_dir: Path
    token: str
    repo: Path = REPO_ROOT
    python: Path = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    with_web: bool = True
    build_frontend: bool = True
    keep: bool = False
    api_port: int = 0
    web_port: int = 0
    children: dict[str, Child] = field(default_factory=dict)
    container: str = ""
    # docker run 成功返回的精确 ID；stop 只认它，不认名字（名字可能被他人复用）。
    container_id: str = ""
    db_name: str = ""
    db_password: str = ""
    jwt_secret: str = field(default_factory=lambda: secrets.token_hex(32))
    metrics_token: str = field(default_factory=lambda: secrets.token_hex(16))
    # 一次性 nonce：同时注入 API 的只读 attestation 与模型替身状态，调用方据此
    # 证明"这两个地址都属于本次运行"，而不是任意一个 /health 返回 ok 的服务。
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    state: dict[str, Any] = field(default_factory=dict)
    _stopped: bool = False

    # -- 路径
    def logs(self) -> Path:
        return self.out_dir / "logs"

    def manifest_path(self) -> Path:
        return self.out_dir / "manifest.json"

    def ready_path(self) -> Path:
        return self.out_dir / "READY"

    def stop_path(self) -> Path:
        return self.out_dir / "STOP"

    def failed_path(self) -> Path:
        return self.out_dir / "FAILED"

    def report_path(self) -> Path:
        return self.out_dir / "report.json"

    # -- 启动
    def start(self) -> dict[str, Any]:
        # 目录已在 cmd_up 里用 prepare_output_dir 原子创建并登记，这里只验证归属，
        # 绝不再 mkdir(exist_ok=True) 一个可能属于别人的目录。nonce 一并校验，
        # 防止同一个 owner 的另一次运行把我们的目录当成它的。
        assert_owned_output_dir(self.out_dir, self.token, self.nonce or None)
        self.logs().mkdir(parents=False, exist_ok=True)
        if not self.python.exists():
            raise FileNotFoundError(f"缺少后端虚拟环境解释器: {self.python}")

        started = time.monotonic()
        self._prepare_project_fixture()
        self._start_postgres()
        self._start_stub()
        self._start_api()
        self._start_worker()
        if self.with_web:
            self._start_web()

        manifest = {
            "owner": self.token,
            "nonce": self.nonce,
            # 只用于崩溃残留（down）时**请求**这个 orchestrator 自己收口；它持有所属
            # 子进程的 Popen 句柄。down 绝不直接按这些 PID 杀进程。
            "orchestrator": {"pid": os.getpid()},
            "attestation": {
                "schema": ATTESTATION_SCHEMA,
                "api_url": f"{self.state['api_base_url']}/__e2e__/attestation",
                "stub_state_url": self.state["stub_base_url"].replace("/v1", "/__control/state"),
            },
            "container": {
                "name": self.container,
                # 本次 docker run 返回的精确 ID：down 只按它对容器做二次核验。
                "id": self.container_id,
                "label": OWNER_LABEL,
                "nonce_label": NONCE_LABEL,
                "host_port": self.state["pg_port"],
                "database": self.db_name,
            },
            # 故意不含密码：测试只通过 API 造数，凭据仅存在于子进程环境里。
            "database": {
                "host": "127.0.0.1",
                "port": self.state["pg_port"],
                "name": self.db_name,
                "user": "postgres",
            },
            "stub": {
                "base_url": self.state["stub_base_url"],
                "port": self.state["stub_port"],
                "state_url": self.state["stub_base_url"].replace("/v1", "/__control/state"),
                "config_url": self.state["stub_base_url"].replace("/v1", "/__control/config"),
                "reset_url": self.state["stub_base_url"].replace("/v1", "/__control/reset"),
            },
            "api": {"base_url": self.state["api_base_url"], "port": self.state["api_port"]},
            "worker": {"pid": self.children["worker"].process.pid},
            "workers": {role: {"pid": self.children[name].process.pid}
                        for name, role in WORKER_ROLES.items()},
            "project_fixture": {"root_key": "e2e", "relative_dir": "",
                                "root": str(self.out_dir / "project-source"),
                                "file": str(self.out_dir / "project-source" / "notes.md")},
            "web": (
                {"base_url": self.state["web_base_url"], "port": self.state["web_port"]}
                if self.with_web else None
            ),
            "pids": {name: child.process.pid for name, child in self.children.items()},
            "log_dir": str(self.logs()),
            "started_in_seconds": round(time.monotonic() - started, 2),
            "notes": [
                "模型只有回环确定性替身，测的是本仓库链路，不是真实模型能力。",
                "Windows 上的子进程终止不等于 SIGTERM 语义。",
            ],
        }
        self._write_json(self.manifest_path(), manifest)
        self.ready_path().write_text(manifest["api"]["base_url"], encoding="utf-8")
        self.write_report(manifest, status="ready")
        return manifest

    def _start_postgres(self) -> None:
        self.db_name = f"rag_e2e_{self.token}_test"
        self.db_password = secrets.token_hex(16)
        name = container_name(self.token)
        self._assert_container_slot_free(name)
        # 先确认镜像存在：缺失时给出可执行的提示，而不是让后面的轮询超时。
        inspect = _docker(["image", "inspect", "pgvector/pgvector:pg18"])
        if inspect.returncode != 0:
            raise RuntimeError("缺少缓存镜像 pgvector/pgvector:pg18；本 harness 不会自行拉取")
        result = _docker([
            "run", "-d",
            "--name", name,
            # owner + nonce 双标签：nonce 只存在于本次运行，清理时可据此证明归属。
            "--label", f"{OWNER_LABEL}={self.token}",
            "--label", f"{NONCE_LABEL}={self.nonce}",
            "-e", "POSTGRES_PASSWORD=" + self.db_password,
            "-e", "POSTGRES_DB=" + self.db_name,
            "-e", "POSTGRES_INITDB_ARGS=--data-checksums",
            # PG18 镜像的数据目录在 /var/lib/postgresql 之下；挂 tmpfs 可免残留卷。
            "--tmpfs", "/var/lib/postgresql",
            "--publish", "127.0.0.1::5432",
            "pgvector/pgvector:pg18",
        ], timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"启动 PostgreSQL 容器失败: {result.stderr.strip()}")
        # docker run 成功后才登记：stop 只处理**本次确实创建**的容器 ID。
        # 名字可能在 run 与登记之间被别的进程占用，ID 不会。
        self.container = name
        self.container_id = (result.stdout or "").strip().splitlines()[0].strip() if result.stdout else ""
        if not self.container_id:
            raise RuntimeError("docker run 未返回容器 ID，无法安全跟踪本次容器")
        port_text = _docker(["port", self.container_id, "5432/tcp"])
        if port_text.returncode != 0:
            raise RuntimeError(f"读取容器端口失败: {port_text.stderr.strip()}")
        self.state["pg_port"] = parse_published_port(port_text.stdout)

        def pg_ready() -> bool:
            probe = _docker(["exec", self.container_id, "pg_isready", "-U", "postgres",
                             "-d", self.db_name], timeout=15)
            return probe.returncode == 0
        wait_until(pg_ready, timeout=90, what="PostgreSQL 就绪")

    def _assert_container_slot_free(self, name: str) -> None:
        """同名容器一律拒绝，**即便标签相同**——那不是删除别人的授权。

        owner 由调用方传入，用户可以故意复用一个已知 owner；"标签一致"只说明
        名字与标签撞了，不能证明那个容器已经死了。清理旧运行是 ``down`` 的职责，
        ``up`` 永远不替别人删容器。
        """
        listing = _docker(["ps", "-a", "--filter", f"name=^/{name}$",
                           "--format", "{{.Names}}"])
        if listing.returncode != 0:
            # 查不到就当作可能被占用：宁可不启动，也不在有歧义时往下走。
            raise RuntimeError(f"无法确认容器名是否空闲: {listing.stderr.strip()}")
        if name in listing.stdout.split():
            raise RuntimeError(
                f"容器名 {name} 已被占用；请先运行 e2e_env.py down --out <dir> "
                "或用另一个 --owner，up 不会删除任何已存在的容器"
            )

    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://postgres:{self.db_password}"
            f"@127.0.0.1:{self.state['pg_port']}/{self.db_name}"
        )

    def _start_stub(self) -> None:
        state_file = self.out_dir / "stub-state.json"
        self._spawn(
            "stub",
            [
                str(self.python), "-X", "utf8", "-I",
                str(self.repo / "backend" / "scripts" / "e2e_model_stub.py"),
                # 端口 0 交给系统分配，再从状态文件读取真实端口，避免自己抢端口。
                "--port", "0",
                "--state-file", str(state_file),
                # owner/nonce 让调用方能证明"这是本次运行的替身"，而不是相似的本地服务。
                "--owner", self.token,
                "--nonce", self.nonce,
            ],
            # 替身只需要标准库与回环地址，不注入数据库/密钥之类的后端配置。
            env=sanitize_env({"PYTHONDONTWRITEBYTECODE": "1"}),
            cwd=self.out_dir,
        )
        payload = wait_until(
            lambda: json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else None,
            timeout=30, what="模型替身启动",
        )
        if payload.get("owner") != self.token or payload.get("nonce") != self.nonce:
            raise RuntimeError("模型替身状态文件不是本次运行写出的（owner/nonce 不符）")
        self.state["stub_port"] = int(payload["port"])
        self.state["stub_base_url"] = assert_loopback_url(payload["base_url"], "stub.base_url")

        def stub_ready() -> bool:
            child = self.children["stub"]
            if not child.alive():
                raise RuntimeError(f"模型替身提前退出，日志: {child.log_path}")
            state = http_json(self.state["stub_base_url"].replace("/v1", "/__control/state"))
            verify_stub_owner(state, owner=self.token, nonce=self.nonce)
            return True
        wait_until(stub_ready, timeout=20, what="模型替身归属校验")

    def _assert_port_free(self, port: int) -> None:
        """指定端口时先自己占一次：被占用就立刻失败，不去连别人的服务。

        不做这一步的话，"已有进程占用该端口 + 自己的子进程还没退出" 这个窗口里
        ``/health`` 可能由别人的 API 应答，从而把不属于本次运行的环境当成自己的。
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"API 端口 {port} 已被占用，拒绝复用别人的服务: {exc}") from exc

    def _start_api(self) -> None:
        port = self.api_port or free_port()
        if self.api_port:
            self._assert_port_free(port)
        self.state["api_port"] = port
        self.state["api_base_url"] = f"http://127.0.0.1:{port}"
        self._spawn(
            "api",
            [str(self.python), "-X", "utf8", "-I", "-c", ATTEST_BOOT,
             str(self.repo / "backend"), "127.0.0.1", str(port),
             self.token, self.nonce, self.state["stub_base_url"]],
            env=self.child_env(),
            cwd=self.out_dir,
        )

        def api_ready() -> bool:
            child = self.children["api"]
            if not child.alive():
                raise RuntimeError(f"API 进程提前退出，日志: {child.log_path}")
            # 只看 /health 会把别人的 API 当成自己的：必须同时拿到本次 nonce 的 attestation。
            payload = http_json(f"{self.state['api_base_url']}/__e2e__/attestation")
            verify_attestation(payload, owner=self.token, nonce=self.nonce,
                               stub_base_url=self.state["stub_base_url"])
            health = http_json(f"{self.state['api_base_url']}/health")
            if health.get("status") != "ok":
                raise RuntimeError(f"API 已带本次 attestation 但健康检查失败: {health}")
            return True
        wait_until(api_ready, timeout=120, what="API 启动、建表与归属校验")

    def _prepare_project_fixture(self) -> None:
        assert_owned_output_dir(self.out_dir, self.token, self.nonce)
        root = self.out_dir / "project-source"
        root.mkdir()
        (root / "notes.md").write_text(
            "# 合成项目\n\nALPHA_VERSION_ONE：后台任务采用第一版执行规则。\n", encoding="utf-8",
        )

    def _start_worker(self) -> None:
        for name, role in WORKER_ROLES.items():
            self._spawn(
                name,
                [str(self.python), "-X", "utf8", "-I", "-c", WORKER_BOOT,
                 str(self.repo / "backend"), role, "--ragkb-e2e-nonce", self.nonce],
                env=self.child_env(), cwd=self.out_dir,
            )
        # 每个角色各自消费自己的队列；任何角色启动即退出都不能发布 READY，
        # 包括 regression——少起一个角色会让在线回归作业无人消费。
        time.sleep(1.0)
        for name in WORKER_ROLES:
            child = self.children[name]
            if not child.alive():
                raise RuntimeError(f"{name} 启动即退出，日志: {child.log_path}")

    def _start_web(self) -> None:
        node = shutil.which("node")
        if not node:
            raise RuntimeError("找不到 node，无法启动前端生产构建")
        vite = self.repo / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if not vite.exists():
            raise RuntimeError(f"找不到 Vite: {vite}")
        # 前端进程不需要数据库/模型配置：只给代理目标，避免把后端凭据扩散到 Node。
        env = sanitize_env(self.web_env())
        if self.build_frontend:
            build = subprocess.run(
                [node, str(vite), "build"],
                cwd=str(self.repo / "frontend"), env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600,
            )
            (self.logs() / "frontend-build.log").write_text(
                (build.stdout or "") + "\n" + (build.stderr or ""), encoding="utf-8"
            )
            if build.returncode != 0:
                raise RuntimeError("前端生产构建失败，见 logs/frontend-build.log")
        port = free_port()
        self.state["web_port"] = port
        self.state["web_base_url"] = f"http://127.0.0.1:{port}"
        env["RAG_E2E_API_TARGET"] = self.state["api_base_url"]
        self._spawn(
            "web",
            # `--` 之后的参数 Vite 不解析，只留在命令行里：崩溃残留清理时用它证明
            # 这个 node 进程属于本次运行。不能不加 `--`，否则 Vite 会当成未知选项报错。
            [node, str(vite), "preview", "--host", "127.0.0.1", "--port", str(port),
             "--strictPort", "--", f"--ragkb-e2e-nonce={self.nonce}"],
            env=env,
            cwd=self.repo / "frontend",
        )
        wait_until(lambda: http_ok(self.state["web_base_url"] + "/", timeout=2),
                   timeout=90, what="Vite preview 就绪")

    # -- 环境构造
    def child_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        overrides = {
            # 关键：导入 app.core.config 之前生效，禁止读取任何 .env。
            "RAG_DISABLE_DOTENV": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ENVIRONMENT": "development",
            "DATABASE_URL": self.database_url(),
            "JWT_SECRET": self.jwt_secret,
            "ADMIN_USERNAME": "e2e-admin",
            "ADMIN_PASSWORD": "E2eAdmin!2026",
            "LOG_STDOUT_ONLY": "1",
            "LOG_DIR": str(self.logs() / "app"),
            "LOG_LEVEL": "INFO",
            "UPLOAD_DIR": str(self.out_dir / "uploads"),
            "PROJECT_ALLOWED_ROOTS": json.dumps({"e2e": str(self.out_dir / "project-source")}),
            "PROJECT_STORAGE_DIR": str(self.out_dir / "project-blobs"),
            "CHAT_MEMORY_ENABLED": "True",
            "MEMORY_TRIGGER_TURNS": "2",
            "MEMORY_KEEP_RECENT_TURNS": "1",
            # 浏览器始终同源访问 Vite preview，再由 preview 代理 /api；这里的 CORS
            # 只覆盖开发端口，避免把测试 API 暴露给任意来源。
            "CORS_ORIGINS": "http://127.0.0.1:5173,http://localhost:5173",
            # 模型链路只指向本仓库替身。
            "EMBEDDING_PROVIDER": "cloud",
            "LLM_PROVIDER": "cloud",
            "EMBEDDING_BASE_URL": self.state["stub_base_url"],
            "EMBEDDING_API_KEY": "e2e-local-stub",
            "EMBEDDING_MODEL": "e2e-embedding",
            "LLM_BASE_URL": self.state["stub_base_url"],
            "LLM_API_KEY": "e2e-local-stub",
            "LLM_MODEL": "e2e-chat",
            "LLM_REASONING_EFFORT": "none",
            "EMBEDDING_BATCH_SIZE": "8",
            # 检索/生成：不启用重排（真实服务）；追问改写走替身。
            "RERANK_ENABLED": "False",
            "RETRIEVAL_REWRITE_ENABLED": "True",
            "RETRIEVAL_TOP_K": "6",
            "HYBRID_RETRIEVAL_ENABLED": "True",
            # 收紧流式超时，让 stop/超时用例在几十秒内收敛。
            "CHAT_STREAM_HEARTBEAT_SECONDS": "2",
            "CHAT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS": "30",
            "CHAT_STREAM_IDLE_TIMEOUT_SECONDS": "15",
            "CHAT_STREAM_TOTAL_TIMEOUT_SECONDS": "60",
            "CHAT_STREAM_CANCEL_POLL_SECONDS": "0.2",
            "LLM_STREAM_USAGE_GRACE_SECONDS": "0.5",
            # 默认 2 会让"并发 4"的容量探针有一半是入口拒绝而不是链路结果；
            # 这里显式提到 4，让默认小样本正好压在限额上，仍能真实触发 429。
            "CHAT_MAX_PER_USER": "4",
            "CHAT_MAX_CONCURRENT": "64",
            "METRICS_TOKEN": self.metrics_token,
        }
        if extra:
            overrides.update(extra)
        env = sanitize_env(overrides)
        assert_no_leaked_secrets(env, overrides.keys())
        return env

    def web_env(self) -> dict[str, str]:
        return {
            "RAG_E2E": "1",
            "NODE_ENV": "production",
            "RAG_E2E_API_TARGET": self.state.get("api_base_url", "http://127.0.0.1:8000"),
        }

    def _spawn(self, name: str, argv: list[str], *, env: dict[str, str], cwd: Path) -> Child:
        log_path = self.logs() / f"{name}.log"
        handle = log_path.open("wb")
        process = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        # 句柄交给子进程后本地关闭：避免父进程缓冲影响日志可见性。
        handle.close()
        child = Child(name=name, process=process, log_path=log_path)
        self.children[name] = child
        # 每个子进程一创建就落盘到 manifest：启动中途崩溃时，down 也能拿到
        # 完整 PID 清单并逐个用 nonce 核验，而不是误以为"没有子进程"。
        self._record_pid(name, process.pid)
        return child

    def _record_pid(self, name: str, pid: int) -> None:
        try:
            current = json.loads(self.manifest_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        pids = current.get("pids") if isinstance(current.get("pids"), dict) else {}
        pids[name] = pid
        current.update({"owner": self.token, "nonce": self.nonce, "pids": pids,
                        "orchestrator": {"pid": os.getpid()}})
        try:
            self._write_json(self.manifest_path(), current)
        except OSError as exc:
            # 写不进去就直接让本次启动失败：不允许出现"子进程在跑但没有身份记录"。
            raise RuntimeError(f"无法把 {name} 的 PID 写入 manifest: {exc}") from exc

    # -- 停止
    def request_stop(self) -> None:
        self.stop_path().write_text("stop", encoding="utf-8")

    def should_stop(self) -> bool:
        return self.stop_path().exists()

    def check_children(self) -> None:
        for name, child in self.children.items():
            if not child.alive():
                raise RuntimeError(f"{name} 进程已退出（code={child.process.returncode}），日志: {child.log_path}")

    def stop(self, *, status: str = "stopped", error: str | None = None) -> dict[str, Any]:
        """终止自己创建的子进程与容器。

        正常的 ``stopped`` 在未指定 ``--keep`` 时删除本次运行**自己创建的**输出
        目录（日志与上传文件都是临时物）；``--keep`` 保留诊断并发布停机收据。
        任何异常路径都保留目录，避免失败现场和报告被一并删掉。

        清理只作用于本进程 ``Popen`` 持有的子进程——它们由本进程创建，身份可靠。
        绝不再按 manifest 里的 PID 做 ``kill``：manifest 是可改的普通文件，
        而 PID 会被系统复用，照它杀进程可能杀掉无关进程。
        """
        if self._stopped:
            return self.state.get("report", {})
        self._stopped = True
        child_failures: list[str] = []
        for name in ("web", *WORKER_ROLES, "api", "stub"):
            child = self.children.get(name)
            if child is None:
                continue
            child.terminate()
            if child.alive():
                child_failures.append(f"{name} pid={child.process.pid}")
        container_failures = self._stop_container()
        cleanup = status == "stopped" and not self.keep and not child_failures and not container_failures
        report = {
            "status": status,
            "owner": self.token,
            "nonce": self.nonce,
            "error": error,
            "api_base_url": self.state.get("api_base_url"),
            "stub_base_url": self.state.get("stub_base_url"),
            "web_base_url": self.state.get("web_base_url"),
            "database": redact(self.database_url() if self.state.get("pg_port") else "", [self.db_password]),
            "log_dir": str(self.logs()),
            "artifacts_removed": cleanup,
            "surviving_children": child_failures,
            "container_failures": container_failures,
        }
        self.state["report"] = report
        try:
            self.write_report({}, status=status, error=error)
        except Exception:  # noqa: BLE001
            pass
        if cleanup:
            # 目录必须带本轮 owner+nonce 标记才删；子进程/容器没清干净时保留现场。
            if not remove_owned_output_dir(self.out_dir, self.token, self.nonce):
                report["artifacts_removed"] = False
        elif status == "stopped" and self.keep and not child_failures and not container_failures:
            # --keep 保留诊断目录，不能把“目录未删除”误报为仍在停机。
            assert_owned_output_dir(self.out_dir, self.token, self.nonce)
            self._write_json(self.out_dir / "STOPPED.json", {
                "owner": self.token, "nonce": self.nonce, "orchestrator_pid": os.getpid(),
            })
        return report

    def _stop_container(self) -> list[str]:
        """只处理**本次 docker run 成功**的那个容器 ID；返回失败清单（空=确认已清理）。

        不按名字删：名字可能被另一个进程复用。先按 ID 查标签，owner+nonce 都对上
        才 ``rm -f``，并检查 rm 的退出码——否则"docker rm 失败"会被当成清理成功。
        """
        failures: list[str] = []
        if not self.container_id:
            return failures  # 本次没成功创建过容器，没有可删的东西
        ident = self.container_id
        inspect = _docker(["inspect", "-f", "{{json .Config.Labels}}", ident], timeout=30)
        if inspect.returncode != 0:
            stderr = (inspect.stderr or "").lower()
            if "no such" in stderr or "没有那个" in stderr:
                return failures  # 已经不存在
            failures.append(f"inspect {ident} 失败: {inspect.stderr.strip()}")
            return failures
        try:
            labels = json.loads(inspect.stdout or "{}")
        except json.JSONDecodeError:
            failures.append(f"inspect {ident} 标签不是合法 JSON")
            return failures
        if not owns_container(labels, self.container, self.token) or \
                labels.get(NONCE_LABEL) != self.nonce:
            # ID 对上但标签对不上：说明这个 ID 已经不是我们创建的那个容器。
            print(f"[e2e] 拒绝删除标签不符的容器 {ident}", file=sys.stderr)
            failures.append(f"容器 {ident} 的 owner/nonce 标签不符，保留")
            return failures
        removed = _docker(["rm", "-f", ident], timeout=120)
        if removed.returncode != 0:
            failures.append(f"docker rm -f {ident} 失败: {removed.stderr.strip()}")
        return failures

    # -- 报告
    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def write_report(self, manifest: dict[str, Any] | None = None, *,
                     status: str, error: str | None = None) -> None:
        """只写脱敏后的运行摘要：端口、配置开关与错误，不含密码/密钥。"""
        manifest = manifest or {}
        report = {
            "status": status,
            "owner": self.token,
            "error": redact(error or "", [self.db_password, self.state.get("jwt_secret", "")]),
            "api_base_url": self.state.get("api_base_url"),
            "stub_base_url": self.state.get("stub_base_url"),
            "web_base_url": self.state.get("web_base_url"),
            "database": redact(self.database_url() if self.state.get("pg_port") else "",
                               [self.db_password]),
            "settings": {
                "RAG_DISABLE_DOTENV": "1",
                "EMBEDDING_PROVIDER": "cloud",
                "LLM_PROVIDER": "cloud",
                "RERANK_ENABLED": "False",
                "models": "loopback deterministic stub only",
            },
            "notes": manifest.get("notes", [
                "模型只有回环确定性替身，测的是本仓库链路，不是真实模型能力。",
            ]),
        }
        self._write_json(self.report_path(), report)


def redact(text: str, secrets_: Iterable[str]) -> str:
    result = text or ""
    for value in secrets_:
        if value:
            result = result.replace(value, "***")
    return result


# --------------------------------------------------------------------------- CLI


def _resolve_python(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    candidate = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return candidate
    # 非 Windows（CI 的 Linux 容器）使用当前解释器。
    return Path(sys.executable)


def cmd_up(args: argparse.Namespace) -> int:
    token = args.owner or new_owner()
    try:
        validate_owner(token)
    except ValueError as exc:
        print(f"[e2e] 参数无效: {exc}", file=sys.stderr)
        return 2
    if args.api_port and not 1 <= args.api_port <= 65535:
        print(f"[e2e] --api-port 非法: {args.api_port}", file=sys.stderr)
        return 2
    if not 0 < args.poll_interval <= 10:
        print(f"[e2e] --poll-interval 非法: {args.poll_interval}", file=sys.stderr)
        return 2
    nonce = secrets.token_hex(16)
    try:
        # 先做目录安全与原子登记：绝不在已有非空目录（尤其仓库里）上继续。
        out_dir = prepare_output_dir(Path(args.out), token, nonce)
    except (ValueError, OSError) as exc:
        print(f"[e2e] 拒绝使用该输出目录: {exc}", file=sys.stderr)
        return 2

    orchestrator = Orchestrator(
        out_dir=out_dir,
        token=token,
        nonce=nonce,
        repo=Path(args.repo).resolve() if args.repo else REPO_ROOT,
        python=_resolve_python(args.python),
        with_web=not args.no_web,
        build_frontend=not args.skip_frontend_build,
        keep=args.keep,
        api_port=args.api_port,
    )
    try:
        # 启动前先登记最小 manifest（owner/nonce/orchestrator pid）。这样即使
        # 启动中途崩溃、完整 manifest 还没写出，``down`` 也知道该请求谁收口；
        # 缺失该文件时 down 只能 fail-closed 保留现场。
        orchestrator._write_json(orchestrator.manifest_path(), {
            "owner": token, "nonce": nonce, "orchestrator": {"pid": os.getpid()},
            "pids": {}, "phase": "starting",
        })
    except OSError as exc:
        print(f"[e2e] 无法写入初始 manifest: {exc}", file=sys.stderr)
        try:
            remove_owned_output_dir(out_dir, token, nonce)
        except Exception:  # noqa: BLE001
            pass
        return 2
    try:
        manifest = orchestrator.start()
    except BaseException as exc:  # noqa: BLE001 - 启动失败也必须清理自己创建的资源
        message = f"{type(exc).__name__}: {exc}"
        print(f"[e2e] 启动失败: {message}", file=sys.stderr)
        # 启动失败时只停自己已起的子进程/容器，**保留**输出目录里的日志与报告，
        # 否则失败现场被删掉就无从排查。目录由本轮创建，不会误删他人内容。
        report = orchestrator.stop(status="start-failed", error=message)
        print(json.dumps({"status": "start-failed", "error": message,
                          "out_dir": str(out_dir), "log_dir": report.get("log_dir")},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps({
        "manifest": str(orchestrator.manifest_path()),
        "api": manifest["api"]["base_url"],
        "stub": manifest["stub"]["base_url"],
        "web": (manifest.get("web") or {}).get("base_url"),
        "owner": token,
    }, ensure_ascii=False), flush=True)

    exit_code = 0
    try:
        while True:
            if orchestrator.should_stop():
                break
            if args.parent_pid and not _pid_alive(args.parent_pid):
                # 测试进程被强杀时没人写 STOP；父进程消失即视为中断，仍然走完整 teardown。
                print("[e2e] 父进程已退出，开始清理", file=sys.stderr)
                break
            orchestrator.check_children()
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        pass
    except BaseException as exc:  # noqa: BLE001
        exit_code = 1
        orchestrator.stop(status="failed", error=f"{type(exc).__name__}: {exc}")
        return exit_code
    orchestrator.stop(status="stopped")
    return exit_code


def cmd_down(args: argparse.Namespace) -> int:
    """请求清理残留环境。

    安全顺序（任何一步校验失败都 fail-closed，绝不删目录）：

    1. 目录必须带本轮标记（owner/nonce/规范路径一致），否则直接拒绝；
    2. 优先**请求仍在运行的 orchestrator 自己收口**：它持有自己 Popen 的子进程，
       身份可靠。我们只写 STOP 文件，然后等它完成；
    3. 只有当 orchestrator 进程确实不在、但目录仍在（崩溃残留）时，才做受限清理：
       - 进程必须能用命令行里的**本次 nonce** 证明身份，才允许终止；
       - 容器必须同时满足名字前缀 + owner 标签 + manifest 与标记 owner 一致；
    4. 已有本轮停机收据时保留诊断；崩溃残留全部清理通过后才删除本目录。
    """
    try:
        out_dir = assert_safe_output_dir(Path(args.out))
    except ValueError as exc:
        print(f"[e2e] 拒绝清理: {exc}", file=sys.stderr)
        return 2
    marker = read_dir_marker(out_dir)
    if marker is None or marker.get("schema") != DIR_MARKER_SCHEMA:
        print(f"[e2e] 目录缺少本轮运行标记，拒绝清理: {out_dir}", file=sys.stderr)
        return 2
    token = marker.get("owner")
    nonce = marker.get("nonce")
    try:
        validate_owner(token)
    except ValueError as exc:
        print(f"[e2e] 目录标记的 owner 非法，拒绝清理: {exc}", file=sys.stderr)
        return 2
    if not isinstance(nonce, str) or len(nonce) < 16:
        print("[e2e] 目录标记缺少 nonce，拒绝清理", file=sys.stderr)
        return 2
    try:
        assert_owned_output_dir(out_dir, token, nonce)
    except ValueError as exc:
        print(f"[e2e] 拒绝清理: {exc}", file=sys.stderr)
        return 2

    manifest: dict[str, Any] = {}
    manifest_state = "missing"
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict) and loaded:
            manifest = loaded
            manifest_state = "ok"
        else:
            manifest_state = "corrupt"
    if manifest and manifest.get("owner") != token:
        print("[e2e] manifest 的 owner 与目录标记不一致，拒绝清理", file=sys.stderr)
        return 2
    if manifest and manifest.get("nonce") != nonce:
        # nonce 对不上说明 manifest 被改过或复制自别的运行：不能据此杀进程/删容器。
        print("[e2e] manifest 的 nonce 与目录标记不一致，拒绝清理", file=sys.stderr)
        return 2
    if manifest_state != "ok":
        # 缺 manifest 或 manifest 损坏时，无法知道有哪些子进程/容器属于本轮；
        # 不知道进程状态就不能删目录（可能删掉仍在运行的进程的日志与工作目录）。
        print(
            f"[e2e] manifest {manifest_state}，无法确认残留进程/容器，保留输出目录: {out_dir}",
            file=sys.stderr,
        )
        return 4

    # 1) 请仍存活的 orchestrator 自己收口（它持有子进程句柄，身份可靠）。
    (out_dir / "STOP").write_text("stop", encoding="utf-8")
    orchestrator_pid = _manifest_int(manifest, "orchestrator", "pid")
    running = orchestrator_pid and _process_matches(
        orchestrator_pid, required=("e2e_env.py",), optional_any=(str(out_dir), out_dir.name)
    )
    # 读命令行期间原进程可能已经停妥；保留日志的收据也必须在崩溃清理之前识别。
    if _shutdown_complete(out_dir, token=token, nonce=nonce, orchestrator_pid=orchestrator_pid):
        print(f"[e2e] orchestrator 已完成清理: {out_dir}", file=sys.stderr)
        return 0
    if running:
        if _wait_for_shutdown(out_dir, args.wait, token=token, nonce=nonce,
                              orchestrator_pid=orchestrator_pid):
            print(f"[e2e] orchestrator 已完成清理: {out_dir}", file=sys.stderr)
            return 0
        print(
            f"[e2e] orchestrator(pid={orchestrator_pid}) 仍在运行，未完成清理；"
            "为避免打断它的收口，本次不强制终止任何进程。请稍后重试。",
            file=sys.stderr,
        )
        return 3

    # 2) 崩溃残留：只清理可用 nonce 证明身份的进程。
    killed, refused = _kill_verified_processes(manifest, nonce=nonce)
    for pid in killed:
        print(f"[e2e] 已按 nonce 核验并终止残留进程 pid={pid}", file=sys.stderr)
    for pid in refused:
        print(f"[e2e] 拒绝终止身份不符的进程 pid={pid}", file=sys.stderr)
    if refused:
        # 有进程无法证明归属时保留目录：宁可留残留，也不越权清理。
        print("[e2e] 存在无法证明归属的进程，保留输出目录", file=sys.stderr)
        return 4
    # 发出终止信号不等于已经退出（POSIX 上 SIGTERM 也会被忽略）。必须先确认
    # 这些进程真的消失了，才能删掉它们的日志与工作目录。
    survivors = [pid for pid in killed if not _wait_until_dead(pid, args.wait)]
    if survivors:
        print(f"[e2e] 进程仍在运行，保留输出目录: {survivors}", file=sys.stderr)
        return 4

    container = manifest.get("container") or {}
    if not _remove_owned_container(container, token, nonce):
        ident = container.get("id") or container.get("name") or "?"
        print(f"[e2e] 容器 {ident} 无法核验归属或删除失败，保留输出目录", file=sys.stderr)
        return 4

    if _shutdown_complete(out_dir, token=token, nonce=nonce, orchestrator_pid=orchestrator_pid):
        print(f"[e2e] orchestrator 已完成清理: {out_dir}", file=sys.stderr)
        return 0
    if remove_owned_output_dir(out_dir, token, nonce) or not out_dir.exists():
        print(f"[e2e] 已清理: {out_dir}", file=sys.stderr)
        return 0
    return 4


def _wait_until_dead(pid: int, timeout: float) -> bool:
    """等到进程确实消失；超时返回 False（调用方据此保留现场）。"""
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def _manifest_int(manifest: dict[str, Any], *keys: str) -> int:
    node: Any = manifest
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    try:
        value = int(node)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _shutdown_complete(path: Path, *, token: str, nonce: str, orchestrator_pid: int) -> bool:
    if not path.exists():
        return True
    if orchestrator_pid <= 0:
        return False
    try:
        assert_owned_output_dir(path, token, nonce)
        receipt = json.loads((path / "STOPPED.json").read_text(encoding="utf-8"))
        return receipt == {"owner": token, "nonce": nonce, "orchestrator_pid": orchestrator_pid}
    except (OSError, ValueError):
        return False


def _wait_for_shutdown(path: Path, timeout: float, *, token: str, nonce: str,
                       orchestrator_pid: int) -> bool:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if _shutdown_complete(path, token=token, nonce=nonce, orchestrator_pid=orchestrator_pid):
            return True
        time.sleep(0.2)
    return _shutdown_complete(path, token=token, nonce=nonce, orchestrator_pid=orchestrator_pid)


def _process_command_line(pid: int) -> str | None:
    """读取进程命令行；取不到（权限/已退出）就返回 None，绝不猜测。"""
    if os.name == "nt":
        script = (
            "$p = Get-CimInstance Win32_Process -Filter 'ProcessId=" + str(int(pid)) + "';"
            "if ($p) { [Console]::Out.Write($p.CommandLine) }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout if result.returncode == 0 and result.stdout.strip() else None
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", "replace")
    except OSError:
        return None


def _process_matches(pid: int, *, required: tuple[str, ...],
                     optional_any: tuple[str, ...] = (), nonce: str = "") -> bool:
    """进程身份校验：命令行必须包含全部 ``required``（及 nonce，若给定）。

    PID 会被复用，所以不能只看"这个号还活着"。子进程命令行里带本次随机 nonce，
    这是别的东西不可能伪造的；orchestrator 自己的命令行没有 nonce（它在进程内
    生成），改用唯一的输出目录路径证明。
    """
    if pid <= 0:
        return False
    command = _process_command_line(pid)
    if not command:
        return False
    if nonce and nonce not in command:
        return False
    if not all(needle and needle in command for needle in required):
        return False
    if optional_any and not any(needle in command for needle in optional_any if needle):
        return False
    return True


def _kill_verified_processes(manifest: dict[str, Any], *, nonce: str) -> tuple[list[int], list[int]]:
    """按 manifest 的 PID 列表**逐个核验后**终止；返回 (已终止, 拒绝)。

    每个进程的命令行里都必须出现本次 nonce（随机、只存在于本轮启动参数与目录标记
    中）以及对应脚本名，否则一律拒绝：那只会是 PID 复用或被人改过的 manifest。
    """
    killed: list[int] = []
    refused: list[int] = []
    pids = manifest.get("pids") if isinstance(manifest.get("pids"), dict) else {}
    expected_markers = {
        "stub": ("e2e_model_stub.py",),
        **{name: ("worker.py", role) for name, role in WORKER_ROLES.items()},
        "api": ("app.main", "uvicorn"),
        "web": ("vite",),
    }
    for name, raw in pids.items():
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if not _pid_alive(pid):
            continue
        markers = expected_markers.get(str(name))
        if not markers:
            # manifest 里出现未知子进程名：不猜，也不杀。
            refused.append(pid)
            continue
        if not _process_matches(pid, required=markers, nonce=nonce):
            refused.append(pid)
            continue
        _kill_pid(pid)
        killed.append(pid)
    return killed, refused


def _remove_owned_container(container: dict[str, Any], token: str, nonce: str) -> bool:
    """崩溃残留清理：按 manifest 记录的 **ID** 核验 owner+nonce 后删除。

    返回 True 表示"确认不存在或已删除"；任何歧义都返回 False（调用方据此保留目录，
    绝不在未确认时假报清理成功）。
    """
    ident = container.get("id") or ""
    name = container.get("name") or ""
    if not ident and not name:
        return True
    target = ident or name
    inspect = _docker(["inspect", "-f", "{{json .Config.Labels}}", target], timeout=30)
    if inspect.returncode != 0:
        stderr = (inspect.stderr or "").lower()
        # 只有 Docker 明确说"不存在"才算已清理；其余（daemon 故障/权限）一律不确定。
        return "no such" in stderr or "没有那个" in stderr
    try:
        labels = json.loads(inspect.stdout or "{}")
    except json.JSONDecodeError:
        return False
    if not owns_container(labels, name or target, token) or labels.get(NONCE_LABEL) != nonce:
        return False
    removed = _docker(["rm", "-f", target], timeout=120)
    if removed.returncode != 0:
        print(f"[e2e] docker rm -f {target} 失败: {removed.stderr.strip()}", file=sys.stderr)
        return False
    # 复查：rm 返回 0 也可能因竞态留下同名新容器，只认"目标 ID 不在了"。
    verify = _docker(["inspect", "-f", "{{.Id}}", target], timeout=30)
    return verify.returncode != 0


def _pid_alive(pid: int) -> bool:
    """跨平台判断 PID 是否仍存在；只用于清理看门狗，不做进程管理。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return f'"{pid}"' in (result.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
        return
    # 先 SIGTERM 让进程自己收口；调用方必须再等它真的消失（见 _wait_until_dead）。
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E2E 环境编排（独占容器 + 回环模型替身）")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="启动环境并阻塞，直到出现 STOP 文件或收到中断")
    up.add_argument("--out", required=True, help="本次运行的独占输出目录")
    up.add_argument("--owner", default="", help="所有者标记；默认随机")
    up.add_argument("--repo", default="", help="仓库根目录（默认按脚本位置推断）")
    up.add_argument("--python", default="", help="后端解释器；默认 backend/.venv")
    up.add_argument("--api-port", type=int, default=0, help="API 端口；0 表示自动")
    up.add_argument("--no-web", action="store_true", help="不构建/启动前端预览")
    up.add_argument("--skip-frontend-build", action="store_true", help="复用已有 dist")
    up.add_argument("--keep", action="store_true", help="退出时保留输出目录")
    up.add_argument("--poll-interval", type=float, default=0.3)
    up.add_argument("--parent-pid", type=int, default=0,
                    help="父进程 PID；它消失时自动清理（应对测试进程被强杀）")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser(
        "down",
        help="清理残留：先请存活的原 orchestrator 自己收口，再按 nonce 核验身份清理崩溃残留",
    )
    down.add_argument("--out", required=True, help="up 使用的输出目录（必须带本轮标记）")
    down.add_argument("--wait", type=float, default=60.0,
                      help="等待原 orchestrator 完成收口的秒数（1–600）")
    down.set_defaults(func=cmd_down)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
