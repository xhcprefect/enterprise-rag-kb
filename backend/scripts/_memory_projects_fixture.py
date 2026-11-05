"""V 夹具的 owned 合成资源：授权目录树 + 子进程 env 构造 + 进程生命周期管理。

只生成本轮**新建**的合成资源，绝不碰真实工作区/真实 .env/业务 DB。

进程生命周期按 PID+create_time 逐节点记账并复核：
- ``_snapshot_descendants`` 返回整棵进程树的 ``(pid, create_time)`` 集合（每个子节点用
  **自己的** create_time 记录，不是根进程的 create_time），父子都继续展开，防误杀已回收 pid。
- ``cleanup`` 在每次 terminate/kill 前都对目标 (pid, create_time) 复核身份；根退出后仍
  复查整棵树所有记录成员，而不是只看根。未退出/无法验证的成员会**保留在 self.children**
  并明确记为失败，绝不把 self.children 清空当成“伪干净”。

Windows 用 psutil 按 pid+create_time+children 判定身份，**不用 /proc 判 Windows**。

这里只造资源与管理生命周期；具体场景步骤放在 verify_memory_projects.py。
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import psutil

# 受控子进程的多项资源路径键 —— 夹具把这些全部指向本轮 temp，不让子进程读真实配置。
RESOURCE_ENV = {
    "UPLOAD_DIR": "storage/uploads",
    "LOG_DIR": "logs",
    # F 已落盘的本地项目索引存储目录（config.PROJECT_STORAGE_DIR）
    "PROJECT_STORAGE_DIR": "storage/projects",  # 实变量名，见 app/core/config.py
}
# 允许继承的自有环境变量白名单（仅本地/通用）：绝不继承真实 API key / DATABASE_URL。
INHERIT_ALLOW = {
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_IDENTIFIER",
}

# 仓库根：脚本目录向上三级（repo root，含 backend / frontend / 测试 / 部署）。
# 授权根与运行根绝不允许解析到仓库真实工作树（backend / frontend / 上层父仓库）任何层级，
# 只允许本轮新建的合成目录。合成 root 必须在本 Fixture 自有 run_root 内。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# 明确拒绝继承的敏感键（防止误继承真实凭据/业务库）。
REFUSE_INHERIT = {
    "EMBEDDING_API_KEY", "LLM_API_KEY", "RERANK_API_KEY", "DATABASE_URL",
    "TEST_DATABASE_URL", "EMBEDDING_BASE_URL", "LLM_BASE_URL", "RERANK_BASE_URL",
}


@dataclass
class ChildProbe:
    """一个由本夹具启动、归本夹具收口的子进程的记账/核验记录。

    ``descendants`` 保留自 spawn 起**已观察到的整棵后代** ``(pid, create_time)`` 集合，
    跨多次 cleanup / 根进程先退出后仍可复查这些成员——绝不让“根已消失”冒充整树已退出。
    create_time 为 ``None`` 的成员表示身份无法复核（如 AccessDenied / 未知），
    一律按“仍在/未知”保守处理，不得从退出判断里剔除。
    """

    role: str                       # e.g. "stub" / "api" / "worker-summary"
    pid: int
    create_time: float              # psutil 启动时间，用于身份核验
    fq_bin: str                     # 实际启动的可执行路径（解释器展开后），若可得
    port: int | None = None
    journal: dict = field(default_factory=dict)
    # 已观察到的后代身份集合（含根）；cleanup 在此集合上逐节点复核，跨重试持续记录
    descendants: set = field(default_factory=set)
    # 后代枚举是否完整：True=自 spawn 起某次快照能完整枚举整棵后代；
    # False=存在无法枚举的分支（如 proc.children 触发 AccessDenied / 快照整体异常）。
    # 枚举不完整 ⇒ 无法声称“整树已知/已退出”，该 probe 绝不 finalize。
    _enum_complete: bool = True
    _enum_reason: str = ""
    # internal: psutil.Popen 句柄与转发到子进程的日志文件句柄（cleanup 时统一关/回收）
    _popen: object = None
    _handles: list = field(default_factory=list)
    # 是否已成功收口（全树退出且枚举完整）：True 表示已确认退出，可安全清出 self.children
    _finalized: bool = False


class Fixture:
    """owned 合成运行根：temp 目录 + 授权前路径前缀 + 子进程代账。

    run_root 必须是**本轮新建且排他**的目录：绝不接受已存在的普通/空/非空目录、
    symlink/junction 或仓库真实工作树。创建后立即写入 ``owner+nonce`` 归属标记
    （``.ragkb-owner.json`` 于根内），随后的 ``make_dirs / allowlisted_env / spawn /
    write_journal`` 都会先复核根的身份与标记未被他人替换/篡改，绝不误用被换掉的根。
    """

    def __init__(self, owner: str = "", run_root: Path | None = None) -> None:
        self.owner = owner or f"v-{uuid.uuid4().hex[:12]}"
        self.nonce = uuid.uuid4().hex
        # 默认运行根放在系统 temp（仓库之外），保证 owned 资源绝不落进真实工作树。
        default_run = Path(tempfile.gettempdir()) / f"ragkb-v-{self.owner}-{uuid.uuid4().hex[:6]}"
        # 排他新建（拒绝已存在/符号链接/junction），并规范化大小写与符号路径。
        self.run_root = self._create_owned_root(Path(run_root) if run_root is not None else default_run)
        self.tmp = self.run_root / "tmp"
        self.logs = self.run_root / "logs"
        self.uploads = self.run_root / "uploads"
        self.project_root = self.run_root / "authorized-src"   # 授权根（合成目录）
        self.blobs = self.run_root / "blobs"
        self.children: list[ChildProbe] = []
        self.dotenv = {}  # 只用于展示 allowlist（不回写、不共享）
        # 归属标记：本轮 owner+nonce。写入后任何共享/占用根的操作都要先复核它不变。
        self._marker_path = self.run_root / ".ragkb-owner.json"
        # 真实目录对象身份：st_dev+st_ino（Windows 为卷序列 + 文件序号）+ 解析路径。
        # 只比较路径/marker 会被“rename 走再按同路径 mkdir + 复制 marker”骗过；必须核到
        # **同一文件系统对象**，对象被替换（不同 inode）即拒绝。
        self._root_identity = os.path.realpath(os.fspath(self.run_root))
        st = os.stat(self.run_root)
        self._root_stat = (int(st.st_dev), int(st.st_ino))
        self._write_marker()
        self.logs.mkdir(parents=True, exist_ok=True)

    # ---------- 归属根（新建且排他 + 标记）----------
    @staticmethod
    def _norm(p) -> str:
        """路径规范化：统一正斜杠，Windows 上再小写——大小写不敏感比较。"""
        s = os.fspath(p).replace("\\", "/")
        return s.lower() if os.name == "nt" else s

    @staticmethod
    def _is_reparse(path) -> bool:
        """该路径成分是否为 symlink / junction / 其它 reparse 点（Windows）。

        用 ``lstat.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`` 权威判定，
        并叠加 islink/isjunction 兜底非 Windows / 旧版本。
        """
        if os.path.islink(path):
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            return True
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if flag:
            try:
                return bool(os.lstat(path).st_file_attributes & flag)
            except (AttributeError, OSError):
                return False
        return False

    @staticmethod
    def _assert_no_reparse(path) -> None:
        """拒绝根及**所有祖先**路径成分为 symlink/junction/reparse 点。

        关键（主审）：`path` 必须按**传入的声明路径本身**逐成分 lstat 检查，**绝不先
        realpath 解析**——resolve 会把祖先 symlink/junction 摊平掉（原声明 link 已消失、
        证据丢失），从而误放行“祖先是指向别处的 link/junction”的根。从盘符根逐段 lstat，
        任一成分是 reparse 即抛错；不读取目录内容，只校验路径成分身份。
        """
        p = os.path.abspath(os.fspath(path))
        drive, tail = os.path.splitdrive(p)
        seg = drive + os.sep
        if Fixture._is_reparse(seg):
            raise RuntimeError(f"拒绝 reparse/symlink/junction 路径根成分: {seg}")
        for comp in tail.replace("\\", "/").split("/"):
            if not comp:
                continue
            seg = os.path.join(seg, comp)
            if Fixture._is_reparse(seg):
                raise RuntimeError(
                    f"拒绝 reparse/symlink/junction 路径成分（根或祖先）: {seg}")

    def _create_owned_root(self, requested: Path) -> Path:
        """新建且排他的 run_root（返回解析后的绝对路径）。

        - 拒绝已存在条目：普通文件、空/非空目录一律拒绝；symbolic link / junction
          及其悬空链接（lexists=True）也拒绝——绝不把别人已建好的目录据为 owned。
        - 拒绝根及**祖先**为 symlink/junction/reparse 点（防止父目录被换成 junction）。
        - 仓库真实工作树任何层级（backend/frontend/父 repo）也拒绝。
        - 通过 ``os.makedirs(exist_ok=False)`` 原子新建（并发竞争导致 FileExistsError
          同样判为拒绝），只有本轮真正新建的目录才被认领。
        - 祖先 symlink/junction/reparse 在**原声明链**上核（resolve 会摊平祖先、丢证据）。
        """
        abspath = os.path.abspath(os.fspath(requested))
        self._declared_root = abspath  # 原声明链(未 resolve)，供 _verify_root 复核祖先 link
        if os.path.lexists(abspath):
            if os.path.islink(abspath) or (hasattr(os.path, "isjunction")
                                           and os.path.isjunction(abspath)):
                raise RuntimeError(f"运行根 run_root 不能是 symbolic link/junction: {requested}")
            raise RuntimeError(
                f"运行根 run_root 必须新建且排他，已存在(文件或目录，含空/非空): {requested}")
        # 先核**原声明链**的祖先 reparse（关键：resolve 会摊平祖先 link、丢证据，必须先核）
        self._assert_no_reparse(abspath)
        resolved = os.path.realpath(abspath)
        self._assert_no_reparse(resolved)  # resolve 后也双保险核一遍
        self._assert_outside_repo(Path(resolved), "运行根 run_root")
        try:
            os.makedirs(resolved, exist_ok=False)
        except FileExistsError:
            raise RuntimeError(
                f"运行根 run_root 必须新建且排他(并发创建竞争到已存在): {requested}")
        return Path(resolved)

    def _write_marker(self) -> None:
        self._marker_path.write_text(
            json.dumps({"owner": self.owner, "nonce": self.nonce}, ensure_ascii=False),
            encoding="utf-8")

    def _verify_root(self) -> None:
        """复核 run_root 仍是**同一个目录对象**且归属标记未变，否则拒绝共享占用。

        三要素缺一即 fail closed：
          1) 解析路径身份一致（realpath 与自建时相同）；
          2) 真实对象身份一致——``os.stat`` 的 ``(st_dev, st_ino)`` 与自建时相等。
             “rename 走 → 同路径重新 mkdir → 复制原 owner+nonce marker”会得到一个新
              inode，对象身份不一致即拒绝（单比路径/marker 会被骗过）；
          3) 归属标记可读且 owner+nonce 与自建时完全一致。
        同时拒绝根及祖先出现 reparse/symlink/junction（父目录被换成 junction 也拒绝）；
        祖先 reparse 须核**原声明链**（resolve 摊平祖先会丢证据），不只已 resolved 路径。
        """
        # 先复核**原声明链**（含祖先 symlink/junction/reparse）——resolve 摊平祖先会丢证据
        self._assert_no_reparse(self._declared_root)
        root = os.path.realpath(os.fspath(self.run_root))
        if self._norm(root) != self._norm(self._root_identity):
            raise RuntimeError("run_root 身份已被替换（解析到的路径与自建时不一致）")
        try:
            st = os.stat(root)
        except OSError as exc:
            raise RuntimeError(f"run_root 无法 stat（可能已被移走/替换）: {exc}")
        if not os.path.isdir(root):
            raise RuntimeError("run_root 不再是目录（已被替换/移走）")
        if (int(st.st_dev), int(st.st_ino)) != self._root_stat:
            raise RuntimeError(
                "run_root 目录对象已被替换（st_dev/st_ino 与自建时不一致，可能是 "
                "rename+mkdir 偷换），拒绝共享占用")
        self._assert_no_reparse(root)  # resolved 路径也核（双保险）
        try:
            payload = json.loads(self._marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"run_root 归属标记缺失或不可读: {exc}")
        if payload.get("owner") != self.owner or payload.get("nonce") != self.nonce:
            raise RuntimeError(
                "run_root 归属标记已变更(owner/nonce 不符)——根被他人换了标记，拒绝共享占用")

    # ---------- 目录 ----------
    def make_dirs(self) -> None:
        self._verify_root()  # 先复核根身份与归属标记未变，再建子目录
        for d in (self.tmp, self.logs, self.uploads, self.project_root, self.blobs):
            d.mkdir(parents=True, exist_ok=True)

    def new_nonce_dir(self, tag: str) -> Path:
        self._verify_root()  # 共享占用前先复核根身份与标记未变
        d = self.tmp / f"{tag}-{uuid.uuid4().hex[:8]}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- env ----------
    def allowlisted_env(self, *, app_db_url: str, jwt_secret: str,
                        admin_user: str, admin_pass: str,
                        embedding_base: str, llm_base: str,
                        emit_project_roots: dict | None = None) -> dict:
        """构造子进程受控 env：显式 URL/凭据/模型端点，root 只含合成目录。

        - 显式 ``RAG_DISABLE_DOTENV=1``（关掉 .env 读取）。
        - 不继承任何真实模型 API key / 业务 DATABASE_URL。
        - uploads/project/blob/logs 全部指向本轮 temp。
        - 授权根映射（project roots）按 F 落盘的真实变量名 ``PROJECT_ALLOWED_ROOTS``
          显式给出，默认只含 ``self.project_root``。
        """
        self.make_dirs()  # 确保合成根/日志/temp 目录就绪，passed 的授权根才是真实存在目录
        env = {}
        for key in INHERIT_ALLOW:
            if key in os.environ:
                env[key] = os.environ[key]
        for key in REFUSE_INHERIT:
            env.pop(key, None)

        env.update({
            "RAG_DISABLE_DOTENV": "1",
            "DATABASE_URL": app_db_url,
            "JWT_SECRET": jwt_secret,          # config.JWT_SECRET（str 字段），键名正确
            "ADMIN_USERNAME": admin_user,
            "ADMIN_PASSWORD": admin_pass,
            # cloud provider -> 本楼盘 stub
            "LLM_PROVIDER": "cloud",
            "LLM_BASE_URL": llm_base,
            "LLM_API_KEY": "local-stub-key-not-real",
            "EMBEDDING_PROVIDER": "cloud",
            "EMBEDDING_BASE_URL": embedding_base,
            "EMBEDDING_API_KEY": "local-stub-key-not-real",
            "LLM_MODEL": "verify-chat",
            "EMBEDDING_MODEL": "verify-embedding",
            # 一律本楼盘 temp，禁止读/写真实上传/日志/项目库
            "UPLOAD_DIR": str(self.uploads),
            "LOG_DIR": str(self.logs),
            "LOG_STDOUT_ONLY": "1",
            # F 已落盘：项目 blob/快照存储目录（config.PROJECT_STORAGE_DIR）
            "PROJECT_STORAGE_DIR": str(self.blobs),
        })
        roots = emit_project_roots or {"default": str(self.project_root)}
        self._apply_roots_env(env, roots)
        self.dotenv = dict(env)
        return env

    @staticmethod
    def _assert_outside_repo(path: Path, what: str) -> None:
        """运行根/授权根绝不能解析到仓库真实工作树(backend/frontend/父 repo)或其父目录内。

        大小写不敏感比较（Windows 下 ``Case` vs `case`` 视为同一路径），避免大小写变体
        绕过仓库排除。
        """
        resolved = Fixture._norm(os.path.realpath(os.fspath(path)))
        repo = Fixture._norm(os.path.realpath(os.fspath(_REPO_ROOT)))
        if resolved == repo or resolved.startswith(repo + "/"):
            raise RuntimeError(f"{what} 落入仓库真实工作树，禁止: {path}")

    def _apply_roots_env(self, env: dict, roots: dict) -> None:
        """把授权根映射写入 env（F 真实变量名 ``PROJECT_ALLOWED_ROOTS``），并做三层检查：
        1) 拒绝任何解析到仓库真实工作树（backend/frontend/父 repo）任何层级的根；
        2) 拒绝不存在的路径；
        3) 拒绝**不在本 Fixture 自有 run_root 内**的根 —— 合成 root 必须强约束在
           本轮自有的合成目录里，绝不继承外部任意目录树。
        大小写不敏感比较（Windows），路径统一用 ``_norm`` 规范化后判定包含关系。
        """
        self._verify_root()  # 共享占用 run_root 前先复核其身份与标记未变
        run_root = Fixture._norm(os.path.realpath(os.fspath(self.run_root)))
        for value in roots.values():
            resolved = Fixture._norm(os.path.realpath(os.path.abspath(value)))
            repo = Fixture._norm(os.path.realpath(os.fspath(_REPO_ROOT)))
            if resolved == repo or resolved.startswith(repo + "/"):
                raise RuntimeError(f"禁止把仓库真实工作树作为授权根: {value}")
            if resolved != run_root and not resolved.startswith(run_root + "/"):
                raise RuntimeError(
                    f"授权根必须落在 Fixture 自有合成 run_root 内: {value} (run_root={self.run_root})")
            if not os.path.isdir(os.path.realpath(os.path.abspath(value))):
                raise RuntimeError(f"授权根不存在或不是目录: {value}")
        env["PROJECT_ALLOWED_ROOTS"] = json.dumps(roots, ensure_ascii=False)

    # =====================================================================
    # 进程生命周期（逐节点 pid+create_time 记账，复核后 terminate/kill）
    # =====================================================================
    def spawn(self, role: str, argv: list[str], *, env: dict, cwd: Path,
              ready_file: Path | None = None, port: int | None = None) -> ChildProbe:
        """启动一个受控子进程并立即核验其真实身份（pid + create_time）。

        保持使用当前 venv 的 ``sys.executable``（绝不切回不能保证 venv 依赖的 base
        Python / 其它解释器）；整棵进程树按**逐成员自己的 create_time** 记账与复核，
        不依赖“launcher 退出即等于 worker 退出”的假设。
        spawn/身份核验/就绪等待任何一步失败，都在 except 里收口已打开的日志句柄与进程；
        create_time 无法获取时，把未证身份进程记入收口账本（含 journal），绝不停下来
        前丢账或留孤儿。
        """
        import sys as _sys
        self.make_dirs()
        resolved = self._resolve_interpreter(argv)
        full_argv = [resolved] + list(argv[1:])
        self._log_journal("spawn-begin", role, {"argv": resolved, "args": argv[1:], "cwd": str(cwd)})
        out_h = err_h = None
        proc = None
        try:
            out_h = open(self.logs / f"{role}.out.log", "ab", buffering=0)
            err_h = open(self.logs / f"{role}.err.log", "ab", buffering=0)
            proc = psutil.Popen(full_argv, env=env, cwd=str(cwd), stdout=out_h, stderr=err_h)
            try:
                create_time = proc.create_time()
            except Exception:  # noqa: BLE001 - create_time 失败也要收口
                proc.kill()
                proc.wait()
                raise
            probe = ChildProbe(role=role, pid=proc.pid, create_time=create_time,
                               fq_bin=resolved, port=port,
                               journal={"owner": self.owner, "nonce": self.nonce,
                                        "spawned": time.time(), "argv": full_argv,
                                        "ready_file": str(ready_file) if ready_file else None},
                               _popen=proc, _handles=[out_h, err_h])
            self.children.append(probe)
            # spawn 后立即持久写身份/nonce（journal 与 ready 无 PID 的问题）+ 快照后代。
            # 后代集合要即刻记录：若根进程很快先退出，后续 cleanup 从根重新快照会丢这些成员，
            # 必须先在这里把整棵后代登记到 probe.descendants。
            members, complete, reason = self._snapshot_descendants(proc.pid, create_time)
            probe.descendants = members
            probe._enum_complete = complete
            probe._enum_reason = reason
            self._persist_probe(probe)
            self._log_journal("spawn-ok", role,
                              {"pid": probe.pid, "create_time": probe.create_time,
                               "port": port, "owner": self.owner, "nonce": self.nonce})
            if ready_file is not None:
                self._wait_ready(probe, ready_file)
            return probe
        except BaseException:
            # 启动失败/身份核验失败：收口已成功打开的日志句柄与进程，不留孤儿。
            # 第二个 open 失败时，第一个已打开的句柄也要 finally 关掉。
            # 若进程已创建但身份未获（create_time 失败/进程提前崩），不能只杀根就丢账：
            # 把未证身份进程记入 children 收口账本 + journal，核到真实身份才收口，
            # 绝不静默把已启动成员从账本里抹掉。
            if proc is not None:
                confirmed = False
                try:
                    if proc.is_running():
                        proc.kill()
                    proc.wait(timeout=2)
                    confirmed = not proc.is_running()
                except Exception:  # noqa: BLE001
                    confirmed = False
                if not confirmed:
                    unverified = ChildProbe(
                        role=role, pid=proc.pid, create_time=None, fq_bin=resolved, port=port,
                        journal={"owner": self.owner, "nonce": self.nonce,
                                 "spawned": time.time(), "argv": full_argv,
                                 "identity": "unverified-create_time-failure",
                                 "ready_file": str(ready_file) if ready_file else None},
                        _handles=[out_h, err_h])
                    unverified.descendants = {(proc.pid, None)}
                    self.children.append(unverified)
                self._log_journal("spawn-failed", role,
                                  {"pid": proc.pid, "create_time_confirmed": False})
                # journal 所有已启动角色（含未证成员），确保未证收口账本不丢
                self._persist_probe(None)
            for handle in (out_h, err_h):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:  # noqa: BLE001
                        pass
            raise

    @staticmethod
    def _probe_journal(probe: ChildProbe) -> dict:
        """序列化一个 probe 的完整身份（含已观测后代）供持久化/恢复。"""
        return {
            "role": probe.role, "pid": probe.pid,
            "create_time": probe.create_time, "port": probe.port,
            "descendants": [[p, c] for p, c in sorted(probe.descendants, key=lambda t: t[0])],
        }

    def _persist_probe(self, probe: ChildProbe) -> None:
        """spawn 后立即把**所有**已启动角色 + 后代身份/nonce 落到 journal。

        （主审）不只写新 probe：就算之后异常退出，主代理也能按 journal 里已启动的所有
        角色 + 后代逐一收口，避免只记最后一个角色而漏掉前面的。
        """
        self._write_journal_file({
            "owner": self.owner, "nonce": self.nonce,
            "children": [self._probe_journal(c) for c in self.children],
        })

    @staticmethod
    def _resolve_interpreter(argv: list[str]) -> str:
        """解释器路径解析：``python``/``python.exe`` 一律用当前 venv 的 ``sys.executable``，
        其它命令原样。

        （主审）绝不在 launcher 与“真实 Python 树”之间切换导致丢 venv 依赖——这里
        保持同一个 venv 解释器（sys.executable）启动，并依靠逐成员自己的 create_time
        跟踪整棵进程树生命周期，而非依赖 launcher 退出即等于 worker 退出。
        """
        head = argv[0]
        name = os.path.basename(head).lower()
        if name in {"python", "python.exe", "python3", "pythonw", "pythonw.exe"}:
            import sys as _sys
            return _sys.executable
        return head

    def _wait_ready(self, probe: ChildProbe, ready_file: Path, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.alive(probe):
                raise RuntimeError(f"子进程提前退出: {probe.role} (pid {probe.pid})")
            if ready_file.exists():
                return
            time.sleep(0.1)
        raise RuntimeError(f"子进程 {probe.role} 在 {timeout}s 内未就绪: {ready_file}")

    @staticmethod
    def _ident(pid: int, create_time: float | None) -> bool:
        """复核 (pid, create_time) 身份是否仍存活。

        create_time 为 ``None``（身份无法复核，如 AccessDenied / 未知）时，
        无法证明已退出 → 一律保守认为“仍在”。未验证成员绝不因无法确认而被当作已退出。
        """
        if pid == os.getpid():
            return True
        try:
            p = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False
        if create_time is None:
            return True  # 未知身份：无法确认退出 → 保守认为仍在
        try:
            if p.create_time() != create_time:
                return False
            # POSIX 的 zombie 已退出，不能再执行或持有工作目录；等待父进程 reap
            # 不应把它误算为活进程。身份不可读仍保持 fail closed。
            return p.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True  # 无法确认 -> 保守认为仍在

    @staticmethod
    def alive(probe: ChildProbe) -> bool:
        """探测单个 ChildProbe 的（根）身份是否仍存活。"""
        return Fixture._ident(probe.pid, probe.create_time)

    @staticmethod
    def _snapshot_descendants(pid: int, create_time: float | None) -> tuple[set, bool, str]:
        """返回 ``(found, complete, reason)``：整棵后代集合（含根）+ 枚举是否完整。

        - 每个子节点记录**自己的** create_time（不是根 create_time），后代会话进程
          不会被根身份误过滤。
        - 对父子都继续展开（BFS/DFS），收集到子树全集后再统一收口。
        - 关键（主审）：**只有能确认已退出的才从“仍在”集合剔除**；无法验证身份的
          节点（create_time 取不到，如 AccessDenied）一律以 ``(pid, None)`` 保留，
          **绝不因 verify 失败而 continue 丢节点 / 使树变空**——空树绝不能被当作“已退出”。
          这种节点在 cleanup 里按“仍在/未知”计，绝不允许 finalized。
        - ``complete`` 显式记录枚举完备性：只要出现任一无法复核身份的节点（AccessDenied），
          或整棵子列表枚举失败（``proc.children()`` 抛 AccessDenied），窗口即不完整，
          绝不能把“已观察的部分成员”误当“整树已知”。cleanup 依据它拒绝 finalize。
        """
        found: set[tuple[int, float | None]] = set()
        complete = True
        reasons: list[str] = []
        stack: list[tuple[int, float | None]] = [(pid, create_time)]
        while stack:
            cur_pid, cur_ct = stack.pop()
            # 未知身份节点（(pid, None)）无法展开：仍登记为“未知被动留在场”，不能丢
            if cur_ct is None:
                found.add((cur_pid, None))
                complete = False
                reasons.append(f"pid={cur_pid}: 身份无法复核(未知)，子树不可枚举")
                continue
            verified_key = (cur_pid, cur_ct)
            if verified_key in found:
                continue
            try:
                proc = psutil.Process(cur_pid)
                if proc.create_time() != cur_ct:
                    continue  # 身份不符（已回收的 pid）→ 跳过，确属已退出
            except psutil.NoSuchProcess:
                continue  # 已退出：可从集合剔除
            except psutil.AccessDenied:
                # 无法复核身份：保守登记为 (pid, None)，不得丢节点
                found.add((cur_pid, None))
                complete = False
                reasons.append(f"pid={cur_pid}: create_time 不可读(AccessDenied)，子树不可枚举")
                continue
            found.add(verified_key)
            try:
                for child in proc.children(recursive=False):
                    try:
                        child_ct = child.create_time()
                        stack.append((child.pid, child_ct))
                    except psutil.NoSuchProcess:
                        continue  # 子已退出
                    except psutil.AccessDenied:
                        # 子身份不可读：保留在场并把枚举标为不完整（其子树同样不可枚举）
                        stack.append((child.pid, None))
                        complete = False
                        reasons.append(f"pid={child.pid}: 子 create_time 不可读(AccessDenied)")
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                # 拿不到整张子列表：这些子的身份与子树全部未知 → 枚举不完整，绝不能伪干净
                complete = False
                reasons.append(f"pid={cur_pid}: proc.children() 枚举被拒(AccessDenied)，子树未知")
        reason = "; ".join(reasons)[:400]
        return found, complete, reason

    @staticmethod
    def _signal_ident(pid: int, create_time: float | None, signame: str) -> bool:
        """对一个 (pid, ctime) 身份发 terminate/kill，发前复核身份，防误杀回收 pid。

        未知身份（create_time 为 None）无法安全发信号，直接返回 False（不动），
        交由 remaining 复核判定“仍留场/非 clean”。
        """
        if pid == os.getpid():
            return False
        try:
            p = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return False
        if create_time is None:
            return False  # 未知身份，绝不动
        try:
            if p.create_time() != create_time:
                return False  # 已回收/换身份，绝不动
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        try:
            if signame == "kill":
                p.kill()
            else:
                p.terminate()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def children_alive(self) -> list[ChildProbe]:
        """返回仍未退出 / 未最终收口的子进程（未收口 = 视为活，不伪造 clean）。"""
        return [c for c in self.children if not c._finalized or self.alive(c)]

    def cleanup(self, grace=3.0, kill=True) -> dict:
        """有界 terminate -> wait 全树 -> kill，并逐节点复核整树退出。

        关键正确性（主审）：
        - 每个 probe 维护**已观察到**的 ``descendants`` 集合；cleanup 每次都把新快照
          **并集**进该集合再复核，根进程先行退出后仍复查这些登记成员，绝不因
          “根已消失、本次快照取不到子”就冒充整树已退出。
        - 每次 terminate/kill 前对目标 (pid, ctime) 复核身份，防误杀回收 pid。
        - 未退出 / 无法验证身份（含 create_time=None）的成员一律保留在 remaining，
          只有确认整树全部退出才 ``_finalized``；未收口成员保留在 self.children 并明确 fail，
          绝不把 self.children 清空当“伪干净”。
        """
        report = {}
        for probe in list(self.children):
            key = f"{probe.role}-{probe.pid}"
            state = {"terminated": [], "killed": [], "not_signal": [],
                     "remaining": [], "exited": []}
            # 0) 生长后代集合：新观测 ∪ 历史已记录（根先退出也不丢之前成员）。
            #    快照返回 (members, complete, reason)；枚举不完整（含快照整体异常）都
            #    对 probe 持久记为不完整，绝不把“已观察部分”当“整树已知”。
            try:
                members, snap_complete, snap_reason = self._snapshot_descendants(
                    probe.pid, probe.create_time)
            except Exception as exc:  # noqa: BLE001
                members, snap_complete, snap_reason = set(), False, (
                    f"快照整体异常: {type(exc).__name__}: {exc}")
            probe.descendants |= members
            if not snap_complete:
                probe._enum_complete = False
                probe._enum_reason = snap_reason or probe._enum_reason
            tree = set(probe.descendants)
            # 1) terminate 一轮（先子后根也可，统一走全部成员）
            for pid, ctime in sorted(tree, key=lambda t: -t[0]):
                if self._signal_ident(pid, ctime, "terminate"):
                    state["terminated"].append(pid)
                else:
                    state["not_signal"].append(pid)
            # 2) 有界等待整树退出（不是只等根）。remaining = 所有仍存活/未知身份成员。
            #    关键（主审）：**无条件最终 recheck**——grace<=0 时循环体一次都不跑，
            #    必须在循环后无条件按整树重算 remaining，绝不因“没等就跳/初始空集合”
            #    把仍在成员判成已退出（伪 clean）。负/零 grace 一律按 0 有界处理。
            deadline = time.time() + max(0.0, float(grace))
            while True:
                if probe._popen is not None:
                    probe._popen.poll()  # 先回收自己的已退出根进程，再判断整树状态
                remaining = {(p, c) for (p, c) in tree if self._ident(p, c)}
                if not remaining:
                    break
                if time.time() >= deadline:
                    break
                time.sleep(0.05)
            # 3) 仍未退出的再复核身份后 kill（未知身份不动，但保留在场判定）
            if remaining and kill:
                killed_tree = {(p, c) for (p, c) in remaining if c is not None}
                for pid, ctime in sorted(killed_tree, key=lambda t: -t[0]):
                    self._signal_ident(pid, ctime, "kill")
                time.sleep(0.2)
                remaining = {(p, c) for (p, c) in tree if self._ident(p, c)}
                state["killed"] = sorted(p for p, _ in killed_tree)
            # 4) 核验整树最终退出（仍存活/未知身份成员 = 未清完）
            exited = {(p, c) for (p, c) in tree if not self._ident(p, c)}
            state["exited"] = sorted(p for p, _ in exited)
            state["remaining"] = [(p, c) for (p, c) in sorted(remaining, key=lambda t: t[0])]

            # 只有 remaining 为空 **且枚举完整** 才允许 finalize；枚举不完整说明整棵树
            # 可能仍有未知分支未被观察到，绝不能伪 clean。
            if not state["remaining"]:
                if probe._enum_complete:
                    probe._finalized = True
                else:
                    state["enum_incomplete"] = probe._enum_reason or "枚举不完整(存在未知分支)"
            report[key] = state
            # 回收 psutil/subprocess 句柄：reap + 关闭转发日志文件
            if probe._popen is not None:
                try:
                    probe._popen.wait(timeout=1)
                except Exception:  # noqa: BLE001
                    try:
                        probe._popen.poll()
                    except Exception:  # noqa: BLE001
                        pass
            for handle in probe._handles:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass
        # 只移除已最终收口（全树退出）的成员；未退出/不可验证的保留并计为失败。
        self.children = [c for c in self.children if not c._finalized]
        # Windows 释放进程 cwd / 句柄有短暂延迟；让目录可被后续 rmtree 干净回收。
        if report:
            time.sleep(0.5)
        return report

    def _log_journal(self, event: str, role: str, detail: dict) -> None:
        line = json.dumps({"event": event, "role": role, "owner": self.owner,
                           "nonce": self.nonce, "detail": detail}, ensure_ascii=False)
        with (self.logs / "journal.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _write_journal_file(self, payload: dict) -> None:
        path = self.logs / f"journal-{self.owner}.json"
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:  # noqa: BLE001 - 日志失败不应中断收口
            pass

    def write_journal(self) -> Path:
        """汇总当前**所有** children 身份（含未收口成员、后代）到 journal 文件，供主代理收口。

        返回实际写入的路径（``journal-{owner}.json``，与 _write_journal_file 一致），
        不返回一个从未创建的 ``journal-summary.json``。
        """
        self._verify_root()  # 落账前先复核根身份与标记未变，避免账记到被换掉的根上
        path = self.logs / f"journal-{self.owner}.json"
        self._write_journal_file({
            "owner": self.owner, "nonce": self.nonce,
            "children": [self._probe_journal(c) for c in self.children],
        })
        return path


# ---------- 合成授权目录 ----------
# 以下固定文本用于制造“同字节不同路径”的身份差异与 secret 排除。
_SNAKE_PY = (
    "def compute_total(items):\n"
    "    \"\"\"合计所有条目(合成).\"\"\"\n"
    "    return sum(item.get('amount', 0) for item in items)\n"
)
_CAMEL_TS = (
    "export function computeTotal(items: Array<{amount:number}>): number {\n"
    "  return items.reduce((a, b) => a + (b?.amount ?? 0), 0);\n"
    "}\n"
)
_VUE = (
    "<template><div class=\"counter\">{{ count }}</div></template>\n"
    "<script setup lang=\"ts\">\n"
    "// 合成计数器组件\n"
    "import { ref } from 'vue'\n"
    "const count = ref(0)\n"
    "export function increment() { count.value += 1 }\n"
    "</script>\n"
)
_MD = (
    "# 合成项目说明\n\n"
    "## 模块\n\n"
    "- `compute_total` 用于合计金额。\n\n"
    "```python\n" + _SNAKE_PY + "```\n"
)
_SECRET_ENV = "API_SECRET=super-secret-should-never-be-indexed\nTOKEN=abc123\n"


def build_project_fixture(root: Path) -> dict:
    """在 ``root``（必须是本轮新建空目录）生成合成授权目录树。

    返回：
      - ``expected_files``: 应被索引的相对路径集合（排除 secret/忽略目录）。
      - ``duplicate_pairs``: 同字节不同路径的示例（一组）。
      - ``excluded``: 必须被排除的路径（.env secret、忽略目录）。
    """
    if root.exists() and any(root.iterdir()):
        raise RuntimeError("合成根必须为空的新目录")
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "node_modules").mkdir(parents=True, exist_ok=True)   # 忽略目录
    (root / ".venv").mkdir(parents=True, exist_ok=True)          # 忽略目录

    # 正常文件
    (root / "src" / "utils.py").write_text(_SNAKE_PY, encoding="utf-8")
    (root / "src" / "main.ts").write_text(_CAMEL_TS, encoding="utf-8")
    (root / "src" / "Counter.vue").write_text(_VUE, encoding="utf-8")
    (root / "docs" / "README.md").write_text(_MD, encoding="utf-8")
    # 同字节不同路径（两份相同内容，身份必须分开保留）
    (root / "lib" / "legacy_utils.py").write_text(_SNAKE_PY, encoding="utf-8")
    # 被排除：secret 文件即便在根下也绝不索引
    (root / ".env").write_text(_SECRET_ENV, encoding="utf-8")
    (root / "node_modules" / "dep.js").write_text("// vendored dep\n", encoding="utf-8")
    (root / ".venv" / "pyvenv.cfg").write_text("home = /ignored\n", encoding="utf-8")

    expected = {
        "src/utils.py", "src/main.ts", "src/Counter.vue", "docs/README.md",
        "lib/legacy_utils.py",
    }
    return {
        "expected_files": expected,
        "duplicate_pairs": [("src/utils.py", "lib/legacy_utils.py")],
        "excluded": {".env", "node_modules/dep.js", ".venv/pyvenv.cfg"},
    }


def default_jwt_secret() -> str:
    return "memory-project-local-test-only-secret-0000000000"
