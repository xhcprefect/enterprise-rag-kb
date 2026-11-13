# 文档入库失败自动重试收口

日期：2026-11-13。范围：在当前未提交改动上完成入库重试，保持已有上传、权限、治理、索引代次与停机恢复契约。不提交/推送，不改业务 .env，不调用真实模型，不触碰业务数据库。

## 可观察验收

- 前端类型检查通过；原有上传“重试”按钮及中英文消息继续可用。
- 正常排队、等待重试、正在重试、永久失败、次数耗尽可区分；倒计时随时间更新，页面卸载停止计时，未知错误不直接渲染。
- 临时失败写 pending + 有界指数退避；退避前不可执行，重试只处理剩余检查点；成功清空失败状态。
- 永久解析/输入错误直接 failed，不重复调用模型；达到次数上限后停止；手工 reindex 增加代次并恢复完整预算。
- 退避期间用户取消不等待退避到期，不再请求模型；模型失败与取消同时发生时取消优先；旧租约/代次不能写回新任务。
- 运维新增重试等待/终结失败计数真实，数据库不可用时仍遵守 degraded/unknown 契约。
- 使用真实 PostgreSQL/pgvector 验证状态流转与并发隔离，使用回环模型替身和真实浏览器验收完整成功/失败/取消/重建路径，查看桌面和窄屏截图。

## 任务与文件所有权

B 后端：backend/app/services/{ingest.py,ingest_failures.py}、backend/worker.py、backend/app/{core/config.py,models/document.py,schemas/knowledge.py,db/init_db.py,api/v1/operations.py}、backend/.env.example、backend/tests/{test_ingest_failures.py,test_ingest_retry_queue.py,test_ingest_retry_integration.py,test_ingest_shutdown.py,test_operations.py,test_document_lifecycle.py,test_upload_completion_locks.py}。负责状态机、分类、取消/租约/检查点及真实 PG 回归；test_ingest_shutdown.py 仅同步首次网络故障的新退避契约，不放宽断言。追加 test_pdf_ingest.py 的 unknown parse / image probe 两例所有权：仅设置单次预算验证原有终结安全信息，默认 unknown 退避/耗尽由新集成精确覆盖。追加 test_database.py 中 test_partial_failure_is_not_searchable 一例所有权：同步为精确 pending/失败次数/未来退避且已提交向量不可检索，保留原检索隔离目的。

F 前端：frontend/src/{views/DocumentsView.vue,utils/documentRetry.ts,i18n/messages/documents.ts,i18n/messages/documentRetry.ts,api/index.ts}、frontend/tests/document-retry.test.ts、frontend/e2e/document-retry.spec.ts。负责接入、消息键冲突、显示语义、计时生命周期及浏览器行为用例。后端字段固定为 ingest_attempts（失败次数，成功清零）、next_attempt_at、last_error_code；pending/processing/failed 等原 status 不变。

主代理：本计划、测试环境/启动脚本与日志（临时目录）、README.md 的功能/配置文档（实现冻结后）、必要的 E2E 测试环境配置（backend/scripts/e2e_env.py 及其测试）；负责实际 diff 审查与独立命令/浏览器验收。主代理不写产品实现代码。

B 与 F 可并行且无共享可写文件；首次测试使用不同角色的独占资源；主代理全量验收仅在 B 交回测试库后执行。E2E 必须等最终后端与生产前端冻结后执行。

## 验证

- B：禁用 dotenv，模型固定回环，以专用 _test 库及精确 owner comment 通过共享测试门；执行失败分类/队列/真实重试集成、停机、取消与运维回归。
- F：npm run typecheck、npm test、npm run typecheck:e2e、npm run build；本轮新增 E2E 由主代理在独占 harness 执行。
- 主代理：逐项读 diff，运行后端完整 unittest 与前端完整检查；启动真实 API、独立 worker、PG、模型替身和生产 preview；实际操作并查看截图。明确记录跳过项与失败，不用替身结果宣称业务准确率或生产容量。

## 风险与回退

- 原已有未提交改动逐项保留；不以覆盖/恢复整个文件回退。
- 新增字段/索引兼容已有数据库，升级检查覆盖缺列旧表，重试等待不持连接/线程。
- 外部模型可能因重试产生多次调用；本地验证仅模型替身，费用记录不冒充零。
- 若集成未通过，按相同实施者修复并重验，不能以类型检查通过替代行为验收。

## 进度

- 基线：前端 typecheck 退出 2；中英文 retry 同名键冲突，页面 retryTooltip/retryHint 缺定义。
- 后端已冻结并完成主代理独立全量回归：2141 项，失败/错误 0，Windows 平台跳过 3 项；真实 PostgreSQL/pgvector 测试已启用。测试前后源码哈希一致，18 个后端冻结文件复核无变化。
- 前端最终返修版本已完成主代理独立验收：typecheck、404/404 项单测（无跳过）、typecheck:e2e、build 均退出 0；入口约 199.69 kB。重试 E2E 5/5、治理 E2E 3/3 通过；整轮源码哈希一致（main-front-acceptance-receipt.json）。
- 主代理目视截图发现并交回原实施者修复两处遗漏：终结失败通知仍显示通用中文，表格行重建后上传卡片残留旧的次数耗尽状态。最终浏览器断言和截图确认：通知采用本地化安全建议，PDF 专用原因保留，重建后上传卡片与文档行均转为完成。
- 已实际查看最终自动重试、永久失败、390px 英文深色、次数耗尽、手工重建完成及取消截图；另经真实上传控件提交合成认证失败文档，确认停止并显示修复模型凭据的建议。人工验收页 console 无警告/错误。
- 截图归档曾因 Windows 目标路径过长失败；浏览器测试本身通过。已改为短路径截图及 ZIP 归档，未修改产品环境。

## 最终验收记录

- 后端全量：`python -m unittest discover -s tests -t . -v`，2141 项中 2138 通过、3 跳过，无失败/错误；使用独占 PostgreSQL/pgvector 测试库、禁用 dotenv、回环模型替身/黑洞。3 项跳过分别为 POSIX 符号链接锚点测试及 2 项真实 SIGTERM 进程测试，需在 Linux 环境补验，不能计为已通过。
- 浏览器使用真实 API、4 个独立 worker、独占数据库、回环模型替身及生产前端；覆盖临时失败自动恢复、空文档永久失败、PDF 专用提示、3 次任务失败后停止并手工重建成功、退避取消后越过原到期时间仍无模型调用，以及 3 项治理回归。仅验收 wrapper 将退避缩短为 8/16 秒，产品默认仍为 30/600 秒。
- 日志与收据保留于 `C:/Users/Administrator/AppData/Local/Temp/ragkb-retry-20261113-c8e2/`：`main-backend-verified.log`、`main-backend-verified-receipt.json`、`main-front-acceptance-receipt.json`、对应各项前端日志及 ZIP 报告；最终 6 张截图位于其 `screenshots-accepted/` 子目录。
- 测试资源已清理：E2E owner/nonce 停机收据匹配，API/网页端口关闭、4 个 worker 与 API/网页/替身进程退出，E2E 数据库容器删除；后端测试容器经精确 ID、名称和 owner 标签校验后删除。E2E down 首次因 Docker 报告同一容器已在删除而退出 4，随后独立核对容器消失与上述停机证据，未将非零退出误记为成功。
- 本轮完成入库失败自动重试收口，未开始文档模块拆分或数据库迁移框架改造；未提交/推送、未改业务 .env、未连接业务数据库、未调用真实付费模型。替身验收不代表真实模型效果或生产容量。
