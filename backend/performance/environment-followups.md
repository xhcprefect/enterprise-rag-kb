# 环境事项待办（2026-11-08 核查与处置完成）

2026-11-07 按用户要求把环境工作从费用修复收尾中移出；2026-11-08 用户要求处理本文件待办。以下为只读核查结论与经确认的处置；未扩范围去动无归属资源。

## 1. 容器归属

- **本轮保留**：`465643c7ab9f` / `five-stage-20261107-4dc5ab5bba9d4a55`
  - 标签：`io.ragkb.acceptance.owner=five-stage-20261107-4dc5ab5bba9d4a55`
  - 库 comment：`owned-test:five-stage-20261107-4dc5ab5bba9d4a55`（逐字节匹配）
  - 监听：`127.0.0.1:62535`；镜像 `pgvector/pgvector:pg18`；运行中
  - 用途：后续数据库回归复跑；需要时再按归属流程回收
- **已回收**：`b1819f927410` / `five-stage-9d1bfb359334470b`
  - 标签匹配 `io.ragkb.acceptance.owner=five-stage-9d1bfb359334470b`；仅 `postgres` 库，无 `ragkb_*` 测试库；`127.0.0.1:55535`
  - 删除前再次核对 name + owner 标签，随后 `docker rm -f -v`；容器与匿名卷 `17db0f…` 均已确认不存在
- **无归属、未动**（不属于本轮工作，不能擅自删除）：
  - `charming_mcnulty`（pgvector，`0.0.0.0:9999` **对外监听**）与 `redis`（`127.0.0.1:6379`）
  - 已退出的 `rag-gl-rabbit2`、`rag-gl-redis`、`rag-gci-pg-50486`
  - 建议：`0.0.0.0:9999` 的 PG 若不再使用，请单独确认用途后改绑回环或回收
- 无 `ragkb` 标签的 compose 项目、网络或卷；本项目没有遗留 compose project。

## 2. tmpfs PostgreSQL

- `465643c7ab9f` 的 `/var/lib/postgresql` 为 tmpfs（`size=1073741824`，1 GiB），无持久卷；容器停止/重建即丢数据，符合一次性测试库定位。
- 生命周期与恢复：需要时用 `pgvector/pgvector:pg18` 重建容器 → 设置 `TEST_DATABASE_URL` / `TEST_DATABASE_OWNER` → `python -m tests._memory_project_test_gate --prepare-owner` → 跑迁移/测试；该库无需备份。
- 回收的 `b1819f927410` 原为具名卷挂载，但从未持有 `ragkb_*` 测试库，已随容器回收。

## 3. 测试挂载

- **E2E**：`backend/scripts/e2e_env.py` 对 PG 显式使用 `--tmpfs /var/lib/postgresql`，避免残留卷；上传目录位于宿主临时 `out_dir/uploads`，不挂业务目录。
- **恢复演练**：`deploy/tests/drill-compose.yml` 使用带 `ragkb.drill.owner` 标签的 `drill_pgdata` 具名卷与网络；`restore-drill.sh` 清理前逐项核对 owner，不使用 `compose down -v`，不加载根 compose/真实 `.env`。
- **权限**：未发现需要放宽的宿主绑定挂载或文件权限；容器内数据目录由镜像用户持有。
- **平台差异**：本机 Git Bash 无 `flock`，`test-maintenance.sh` / `test-remote-backup.sh` 按设计拒绝无锁维护；`test-monitoring.sh` / `test-alerting.sh` 需 CI 预拉 `prom/prometheus:v3.1.0` 等镜像。CI 的 ubuntu `maintenance` job（`checks.yml:71-89`）覆盖全部五项；本地可跑且已通过的只有 `test-drill-guard.sh`。未安装工具、未拉镜像。

## 4. 来源规划测试（已由后续阶段覆盖）

- 原暂停的委派任务未重启，已落盘改动保留。
- 后续 T3 来源规划 4 模块 106 tests、T4 全量 1588 tests、T5 全量 1658 tests 均在归属门后的自有 `_test` 库通过；跳过项仅为固定的 Linux-only 用例。
- 日志：`backend/logs/t3-full-regression-20261108.log`、`backend/logs/t4-full-regression-20261108.log`、`backend/logs/t5-full-regression-20261108.log`。

## 结论

四项待办已核查完毕；容器归属与 tmpfs 生命周期有明确结论，测试挂载未发现需处理项，来源规划测试已被后续回归覆盖。仅回收了一个确认归属且无测试数据的空容器；本轮数据库与所有无归属资源保持原状。
