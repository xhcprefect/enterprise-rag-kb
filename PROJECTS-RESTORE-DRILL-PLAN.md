# 项目存储恢复演练任务卡

2026-11-14；基础main badb3ab，分支feat/projects-restore-drill。业务基线预检已交付；真实资料/目标/预算未到位。下一独立缺口为隔离演练尚未覆盖projects文件归档。当前生产backup.sh/restore.sh与maintenance替身回归已有三件套支持，本任务不改生产恢复行为。

## 目标与验收

扩展deploy/tests/restore-drill.sh，在独占临时Compose PostgreSQL里真实备份/恢复合成DB + uploads + projects，验证项目内容寻址blob字节与SQL引用。结果只算local-synthetic，不自动证明production恢复、RTO/RPO或应用项目来源预览。

- 保留默认dry-run、显式--isolated、--keep、--rows及现有owner/project归属守卫和TCP readiness；不读取业务.env/数据库/DATA_DIR，不加载根Compose，不运行生产backup.sh/restore.sh。
- 合成uploads维持4文件；projects至少4个内容寻址blob，平铺64位sha256文件名，覆盖UTF8文本、二进制、空文件与另一不同内容。不扫描任何真实源码或资料。
- 新增合成drill_project_blobs表，保存blob SHA、字节长度与固定测试标签；备份前/恢复后逐列比对，同时确认SQL记录的SHA/size与实际恢复blob一致。原25行drill_rows与owner marker仍校验。
- 三份归档采用同一drill stamp的ragdb-drill.sql.gz、uploads-drill.tar.gz、projects-drill.tar.gz，manifest-drill.sha256恰好3项并最后生成。恢复/删除合成原始数据前校验三件套，缺失、损坏、清单缺项/重复/外来条目都必须非零拒绝；不执行恢复SQL。
- 用独立小型只读drill helper检查上述固定三件套与SHA（不读取.env或调用Docker，不建立一般用途恢复器）；复用于演练和纯本地回归，避免测试自己实现另一份校验。可调用脚本参数为显式backup目录，帮助/失败不回显秘密；全部输入是脚本拥有的合成产物。
- 恢复到全新空restored目录，原uploads和projects源文件保持用于独立复核；每文件SHA/数量和blob键/size与源一致。分别对恢复后的uploads文件、project blob追加一个字节，两个检测均必须失败并写明确报告；篡改仅影响合成恢复副本，不覆盖原件或备份。
- 日志清楚区分25个普通行、4个项目引用、4个上传文件、至少4个project blobs、三件套校验、两种篡改负向；最终成功不能遗漏projects。脚本末尾明确local-synthetic及非production RTO/RPO。
- main-owned真实验收两次全新owner/空卷、独立SQL/文件/manifest复核，随后精确完整ID+owner+project清理，不按端口/PID或批量Docker命令删除。
- CI maintenance沿用原有守卫和维护回归，再显式拉取pgvector/pgvector:pg18并实际执行一次扩展的--isolated演练（不--keep，脚本自清理）；不修改权限/旧jobs/NodePython/上限，不上传drill.env或临时根。此项约20秒，保持job有界。

## 文件所有权与依赖

单个general-purpose实施者A，先helper/测试再脚本/CI/部署说明；主代理独立准备实际验收驱动与证据。没有并行实施任务。

| 所有者 | 可写文件 | 范围 |
| --- | --- | --- |
| A | deploy/tests/restore-drill.sh | 合成项目blob/SQL/归档/三件套校验/恢复/负向，保留原安全边界 |
| A | deploy/tests/drill-backup-check.sh（新增） | 固定三件套只读校验，不调用Docker/.env/生产恢复 |
| A | deploy/tests/test-drill-backup-check.sh（新增） | 独立临时目录的真实归档回归，成功/缺失/损坏/清单缺项重复外来、拒绝结果 |
| A | deploy/tests/test-drill-guard.sh | 在原8项之后运行新纯本地回归；保留所有原测试 |
| A | .github/workflows/checks.yml | 只在maintenance追加拉取隔离PG镜像与真实--isolated演练 |
| A | DEPLOY.md | 描述扩展覆盖、local-synthetic结论与原生产流程区分 |
| 主代理 | 本任务卡、ACCEPTANCE-EXECUTION-PLAN.md | 计划与真实验收结果，保留原历史证据 |

其他一律只读，包括deploy/common.sh、backup.sh、restore.sh、drill-compose.yml、fake-docker-drill.sh、后端/前端/依赖。需要扩大范围先报告。

## 验证命令与归属

- 不安装本地依赖，不调用真实模型，不改.env，不提交推送（实施者）。
- bash -n所有修改shell；git diff --check。
- 在独占临时容器内跑bash deploy/tests/test-drill-guard.sh及test-maintenance.sh；它们用固定/tmp目录，不能宿主机直接运行。使用现有缓存rag-e2e-backend:local，--rm --network none --entrypoint /bin/bash，仓库只读挂载，只写容器内部tmp；主代理会先确认工具可用。不要启动任何本地服务/真实DB。新test-drill-backup-check.sh使用mktemp自有根，不和别的任务共用路径。
- 实施者不执行真实Docker数据库演练；冻结后主代理真实运行两轮有限命令（每轮180秒上限），独立验证并清理，保留证据。长生命周期服务若需要仍必须PowerShell脱离工具任务；本任务不启动应用服务。
- 云端在明确feature SHA验证push/PR共8项；合并后跟进准确main独立4项及实际maintenance演练日志，不以绿灯替代项目文件内容证据。

## 风险与回滚

此任务只扩大合成演练覆盖，不证明生产脚本真实环境停写/恢复行为、生产数据时点或RTO/RPO；应用层项目来源预览尚未在此演练执行。新helper只验证自建备份一致性，不承诺不受信任归档导入安全。归属冲突立即停止并保留现场，不重放破坏性SQL或覆盖旧失败记录。

回滚只撤销本任务限定diff，不覆盖其他改动；不删除任何无关容器/卷/网络或历史证据。

## 主审记录

首稿6个实施文件范围符合任务卡。主代理亲跑shell语法/diff、独占容器内守卫+helper、maintenance及remote-backup回归均exit0，但真实冷启动cold-1在37.333秒exit1：Windows sha256sum的binary标记`*`被新清单解析器误拒。实际diff另发现SQL引用的文件校验先于解包、helper组合选项覆盖而跳过归档校验（已在容器实际复现bad archive exit0）、测试允许任意覆盖根直接删除等问题，已返回同一实施者修复。约束收窄为单个显式backup目录CLI，不保留未要求的通用子模式；补持久大dump、两种SHA清单标记与独立只读回归。失败现场日志与验证false保留，完整ID+owner+project清理后本项目资源为0，原两个容器未动。

首次主代理后台命令误用1000ms时限被工具终止；该中断不算测试通过。调整为足够的有限时限后才得到上述完整回归/真实失败。

修复稿已亲自全文/实际diff复审，范围仍为6实施文件+2主代理计划，生产脚本/配置/应用/依赖未改。review-3的shell语法、diff、独占容器守卫+新helper、maintenance与remote-backup均exit0；守卫保留bootstrap=false/shutdown=false/ready=true。持久回归覆盖两种GNU清单标记、4MiB合法dump完整读取、重新匹配SHA的结构/路径/链接负例、成功/失败只读性。

真实cold-2/cold-3两次全新owner/容器/空卷/网络均exit0：21.819/18.877秒。主代理独立SQL逐列比对25行NULL/中文及4条项目引用，Node crypto/zlib/只读tar解析逐字节复核3归档/4uploads/4flat64hex blobs；恢复副本各只有一个文件比源追加x，其余一致。独立18个真正helper CLI场景全部符合预期，涵盖text/binary/混合清单、大dump、缺项/重复/篡改/匹配哈希但结构错，输入内容未变。

另一个全新owner真实负向：主代理只在新自有目录将刚建projects归档改成非gzip，之后生成的manifest仍与字节匹配，脚本结构校验exit1且未进入DROP；独立SQL原25行与4引用完整、恢复目录空。负向和两轮成功资源均按完整ID+owner+project核验后清理为0，原两容器持续运行；所有证据在仓库外ragkb-projects-restore-20261114-4e87，首轮失败不覆盖。结论仅local-synthetic，不抵生产备份/RTO/RPO或应用项目来源预览。

本地审查与验收通过，进入已授权提交/PR/准确SHA CI交付流程；云端结果单独保留，不预填为成功。
