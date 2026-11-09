"""项目索引共享纯数据类型（无 DB / 无配置依赖）。

本项目索引处于纯服务阶段：这些 dataclass 给扫描、解析、blob 与后续
index/API 复用，全部字段均为静态纯数据，不触发任何 I/O/执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProjectScanLimits:
    """扫描阶段的有界预算。**所有值必须为正整数**，拒绝 0=无限与负数。

    Q 在 `config` 硬界相同约束（ge1）；纯服务这里同样拒绝无界值，避免
    API/本地构造宽配置时传入 0 意外关闭上限。
    """

    max_files: int = 50_000
    max_total_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB
    max_single_bytes: int = 32 * 1024 * 1024  # 32 MiB
    max_path_chars: int = 4096
    max_ignore_bytes: int = 2 * 1024 * 1024  # 所有忽略文件合计上限
    max_ignore_lines: int = 200_000
    # 扫描遍历预算：总遍历条目量（目录/文件/被排除项一并计入）
    max_iterations: int = 500_000
    # 被排除/忽略/硬排除项的信息收纳上限（避免百万被忽略文件把 excluded 撑爆）
    max_excluded_items: int = 200_000
    # 真实扫描 deadline：遍历期间按墙钟周期性检查，超时即抛 ProjectScanError
    scan_timeout_ms: int = 30_000
    # 解析建议预算（ms，**非本服务强制项**）：本纯解析器只在进程内做真实有界的
    # 字节/行数预算（见 project_parser.MAX_TEXT_BYTES / BOUNDED_LINES），**并不**
    # 在进程内跑墙钟中断去掐断 CPU。真正 per-file CPU deadline 必须由上层
    # （index worker / Q) 在 owned 子进程内强制。0 表示“上层未给 deadline，
    # 逐文件解析交给调用方以 owned 子进程 + 该建议值控制”。
    parse_timeout_ms: int = 0

    # 必须严格为正的预算字段（0=无限被拒绝，防止意外关闭上限）。
    _STRICT_POSITIVE = (
        "max_files", "max_total_bytes", "max_single_bytes", "max_path_chars",
        "max_ignore_bytes", "max_ignore_lines", "max_iterations",
        "max_excluded_items", "scan_timeout_ms",
    )

    def validate(self) -> None:
        """校验：严格正字段必须 >0；``parse_timeout_ms`` 为建议字段允许 0（未给）。"""
        for name, value in self.__dict__.items():
            if name in self._STRICT_POSITIVE and value <= 0:
                raise ValueError(
                    f"ProjectScanLimits.{name} 必须为正整数，收到 {value}"
                )
            if name == "parse_timeout_ms" and value < 0:
                raise ValueError(
                    f"ProjectScanLimits.parse_timeout_ms 不允许负数，收到 {value}"
                )


@dataclass(frozen=True)
class BlobRef:
    """不可变 blob 的引用：以实际字节 SHA-256 为身份。"""

    sha256: str
    size: int
    # 避免把合成/受控上下文与真实源行混淆的标记，由上层决定是否落库。


@dataclass(frozen=True)
class ManifestEntry:
    """扫描后的安全文件清单项（一个文件一条）。

    ``sha256`` 是**不可变 blob 的实际字节摘要**（受控副本），不是重新打开源
    得到的摘要。``relative_path`` 为相对授权根的正斜杠路径，排序键。
    """

    relative_path: str
    sha256: str
    size: int
    language: str | None = None
    parser_status: str = "pending"  # pending/parsed/fallback/error/unknown
    excluded_reason: str | None = None


@dataclass(frozen=True)
class ScanReport:
    """一次完整扫描的结果。非法/中途失败绝不伪装成“成功空清单”。

    ``is_success`` = 正常收敛且**未被任何预算/排除/reparse/异常截断**。
    一旦 ``truncated`` 为真（触到任一硬界：文件数/字节/遍历量/收纳量/deadline），
    结果**不可作为完整快照发布**。
    相反，``unknown_reason`` 里只放**良性的元数据未知**（如未提供 git commit），
    它不妨碍 ``is_success``——普通非 Git 目录不会被这种纯元数据未知判失败。
    """

    # 稳定排序后的条目（按 relative_path，其次 sha256/大小）
    entries: tuple[ManifestEntry, ...] = ()
    # 被硬排除/忽略规则排除但未作为文件纳入的（信息项，不参与索引）
    excluded: tuple[str, ...] = ()
    # 预算/环境相关的非致命限制说明
    limits: tuple[str, ...] = ()
    # 触到任一硬界（文件数/字节/遍历量/收纳量/deadline/深度）即 True，禁止发布完整快照
    truncated: bool = False
    # 权威出处：仅供 unknown 元数据附加，绝不运行 git
    git_commit: str | None = None
    git_dirty: bool | None = None
    # 元数据报告窗口与未知（扫描不是 FS 原子快照）
    started_ms: int | None = None
    ended_ms: int | None = None
    unknown_reason: tuple[str, ...] = field(default_factory=tuple)
    # 本次扫描**纳入索引**文件的最晚修改时间（来自同一核验 FD 的 fstat，纳秒）。
    # 仅供上层自动同步做安静窗口去抖；不参与 ``is_success`` 判定。
    newest_mtime_ns: int | None = None

    @property
    def is_success(self) -> bool:
        """正常收敛且未被任何硬界截断才算成功。

        - 权限拒绝/中途变化/列表失败/超时都以异常抛出（根本不返回 report）。
        - ``truncated`` 为真则**不能发布为完整快照**。
        - ``unknown_reason`` 只含良性元数据未知（如未提供 git commit），不判失败。
        """
        return self.ended_ms is not None and not self.truncated
