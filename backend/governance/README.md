# 文档治理规范（Document Governance）

本规范覆盖 `documents` 表的治理字段、`/api/v1/knowledge-bases/{kb_id}/documents/{doc_id}`
下的 `/governance`、`/supersede`、`/reenable` 三个管理员接口，以及回答发布前的来源复核
门禁。判定逻辑的唯一来源：

| 职责 | 实现 |
| --- | --- |
| 生命周期状态与可检索谓词（对象级 + SQL 谓词同义） | `backend/app/services/document_lifecycle.py` |
| 治理写路径（PATCH / 替代 / 重新启用 / 去重复用校验） | `backend/app/services/document_governance.py` |
| 资料更新差异预览与预览指纹陈旧校验 | `backend/app/services/document_comparison.py` |
| 回答来源快照解析、去重、落库前复核 | `backend/app/services/answer_sources.py` |
| 认领、租约、发布事务与锁序 | `backend/app/services/chat_turns.py` |
| 模型历史装配（仅模型可见的过滤） | `backend/app/services/answer_history.py` |

禁止任何旁路实现：检索、原文读取、会话范围解析、文档列表的可检索过滤都必须引用同一
`retrievable_conditions()` / `is_retrievable()`。

## 1. 治理字段

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `maintainer` | 负责人描述文本（≤200 字符），仅描述用途，**绝不承载任何访问控制语义** | `''` |
| `last_reviewed_at` | 最近一次人工审核时间（UTC，带时区）；仅展示，不参与可检索判定 | `NULL` |
| `valid_from` / `valid_until` | 显式生效窗口 `[valid_from, valid_until)`，半开区间，UTC 存储 | `NULL`（该侧不限） |
| `enabled` | 显式停用开关（`false` → disabled） | `TRUE` |
| `superseded_at` / `superseded_by_id` | 替代标记；任一非空即 superseded。自引用外键 `ON DELETE SET NULL` | `NULL` |
| `governance_revision` | 治理 CAS 修订号，任何治理写 +1 | `0` |

`maintainer`、`last_reviewed_at` 都不是权限字段；任何查询/授权判断都不得读取它们。

## 2. 可检索判定（fail closed）

冻结生命周期优先级：
`superseded` > `disabled` > `scheduled`（now < valid_from）> `expired`（now ≥ valid_until）> `active`。

**可检索（retrievable）** 必须同时满足：

1. `lifecycle_state == 'active'`；
2. `status == 'ready'`；
3. `cancel_requested` 严格为 `False`（`NULL`、`0`、缺失都不算）；
4. `index_version` 是合法的非负整数。

SQL 谓词 `retrievable_conditions()` 的实际条件：`status = 'ready'`、
`cancel_requested IS FALSE`、`index_version >= 0`、`superseded_at IS NULL`、
`superseded_by_id IS NULL`、`enabled IS TRUE`、`valid_from IS NULL OR now >= valid_from`、
`valid_until IS NULL OR now < valid_until`。`now` 是调用方传入的 aware UTC（缺省
`utc_now()`），**不是 PG 语句时间**；任何行锁等待之后，调用方必须重新取时刻再判定，
不能拿锁前的陈旧时刻放行已过期行。

对象级 `is_retrievable()` 在缺字段、`enabled` 非严格布尔、naive datetime、数字/字符串
时间、`index_version` 为 bool/float/负数/缺失时一律返回 `False`（`lifecycle_state()`
对非法输入抛 `ValueError`，由它闭合为不可检索）。SQL 对必填列中的 `NULL` 不返回行；
`valid_from` / `valid_until` 的 `NULL` 按契约表示“该侧不限”，不属于缺字段。
SQL 只能表达 `index_version >= 0`，bool/非整数的类型级拒绝由对象级复核完成。

所有检索入口共享该谓词：向量检索、关键词候选、相邻片段扩展、会话文档范围解析、
原文/PDF 读取、文档列表 `retrievable_only=true`。没有第二条条件链。

## 3. 有效期与审核

- 时间一律 aware UTC；API 输出显式时区 ISO。naive datetime、数字时间戳、无时区
  字符串、无秒宽格式一律 422，不做宽松解析。
- 窗口为半开区间 `[valid_from, valid_until)`：`now == valid_from` 可检索，
  `now == valid_until` 已过期。任一侧为 `NULL` 表示不限。
- `scheduled` / `expired` 在每次读取时按当前 UTC 派生，自然到期无需后台任务即生效；
  系统不自动续期、不自动恢复、不自动清理。
- `last_reviewed_at` 仅用于展示：缺失时前端提示未审核，**不改变检索资格**；本仓库
  没有“过旧”的自动判定，也不包含自动审核催办、续期或到期裁决。审核节奏由部署方
  在职责表中约定并人工执行。
- PATCH 合并窗口若 `valid_from >= valid_until` 直接 422；先校验后写 ORM，不留下
  半更新行。

## 4. 发布前复核（回答来源快照 v1）

一轮回答发布时，**持久化的权威记录**是 `ChatTurn.trace`（v1 快照）：

```json
{"source_snapshot_version": 1, "supplied_sources": [], "history_sources": []}
```

`Message` 表**没有** `source_provenance` 列；`answer_history.load_rag_history` 装配
模型历史时，才把同形状的 v1 快照作为 `source_provenance` 字段附在 assistant 历史条目
（字典）上（其中 combined 来源归并到 supplied）。它是装配元数据，不是新加的消息列；
读历史不要期望消息表里能查到它。

- **supplied_sources** 必须是**实际进入最终生成上下文**的 `(LCDocument, distance)` 对，
  从原始 metadata 直接严格映射，不经 `rag.to_citations()` 的宽松归一化。候选集、
  最终 citations 都不能替代它；被送入上下文但未被引用的来源同样受约束。
- **history_sources** 是**实际依赖**的历史传递来源（含更早轮次继承），来源是每条
  assistant 历史消息上的 `source_provenance`；只看最终 citations 不足以覆盖“提供过
  但未引用”的旧来源。发布校验用的是**并集**：实际参与追问规划/改写的历史传递来源
  ＋ 最终生成 bounded history 的传递来源，去重后统一复核。旧 B assistant 即使在生成
  阶段已从 bounded history 移除，只要其派生 query 仍进入生成，就仍计入并集。
  `PreparedAnswer.history_sources` 本身仍只代表最终 bounded history；载入但未使用的
  历史不加入——topic switch / 独立问题未用旧史时不得虚加依赖。
- 明确拒答、检索为空、澄清回复等没有资料的轮次也必须写出**显式 v1 快照**，不能省略
  快照字段；`supplied_sources` 为空列表，`history_sources` 由实现按快照规则记录（澄清
  等未调用模型的轮次也可能带非空 `history_sources`）。空来源不等于跳过鉴权——发布
  事务仍先锁 User/KnowledgeBase、复核认证时的原始 `token_version` 与 ACL，项目会话
  还必须在会话锁前锁定并复核项目快照绑定。
- 每条来源按知识库类型分别复核：
  - 上传库 → `Document` 走统一可检索谓词，且 `index_version` 与快照一致，请求范围内
    每个 chunk 必须存在且 `embedded`；
  - 项目库 → `ProjectSource → ProjectSnapshot → ProjectSnapshotFile →
    ProjectFileVersion → ProjectChunk`，并核对 manifest/config 指纹、文件解析状态与
    路径；**绝不**回查上传 `Document`；
  - 一次回答不得混用两类来源；去重后来源超过 256 条、同一身份出现冲突元数据、
    metadata 缺字段/坏类型均整轮失败，绝不裁剪后保留正文。
- 发布顺序固定：严格解析快照 → 根锁（User → KnowledgeBase）→ 项目绑定锁
  （ProjectSource → ProjectSnapshot）→ 会话/轮次/消息 → 按 UUID 排序的上传文档锁 →
  锁后取新鲜 UTC 做最终复核。事务内不做网络访问；任何失败都不写成功 assistant
  消息，即使最终 citations 为空。

## 5. 历史装配与流式生成

- `load_rag_history` 只服务于模型：要求 fresh 的 User 启用状态与原始 `token_version`、
  知识库 ACL、会话模式/范围版本/项目快照绑定；只接受完整 `completed` 的 user/assistant
  配对；每轮 trace 必须是合法 v1 快照且来源当前仍可复核。旧轮次缺版本、缺键、
  坏 provenance、来源失效或跨快照，**整轮排除**在追问改写与生成之前。
- 整轮排除只影响模型上下文，**不修改、不删除**用于展示的原消息；历史列表/详情仍
  按原记录展示。
- 新请求在认领时复核令牌版本与 ACL；流式期间按既有轮询间隔（`CHAT_STREAM_CANCEL_POLL_SECONDS`）
  复核租约与权限，并按租约周期续期；发布前再在一个短事务内加锁、锁后重新取 UTC
  复核租约/范围/来源。数据库行锁不跨模型网络等待。
- 已经流式发出的字节无法追回，这是 SSE 协议的固有限制，不是回滚缺陷；取消/权限
  撤销只能阻止后续增量与最终落库。

## 6. 项目来源与快照

- 项目来源与上传文档是两条独立校验链，互不混查：项目复核只读
  ProjectSource/Snapshot/FileVersion/Chunk，上传复核只读 Document/DocumentChunk。
- 会话一旦绑定项目快照即固定，**不会**随 `ProjectSource.active_snapshot_id` 的升级
  自动漂移；切换必须走显式 CAS 接口（`expected_revision`）。
- 新会话无显式快照时绑定该源当前 `active` 且 ready 的快照；显式快照必须同源同 KB。
- 根绑定拒绝（认领/发布阶段）：仅按实际绑定检查拒绝——源被撤销（`revoked_at`）、
  快照未 ready、manifest/config 指纹**缺失**、失去 ACL；根绑定锁或新鲜复核失败时
  直接拒绝，不写成成功回答。
- 历史装配容错：仅当某条历史 trace 在**装配前**已失效（指纹错误、跨快照不匹配、
  坏/缺键 provenance）时，**该轮整轮排除**，保留其余仍可复核的历史轮次继续装配。
- 发布落库门禁：发布校验复核的是 supplied_sources 与上述**并集**（规划/改写实际依赖
  的历史传递来源 ＋ 最终 bounded history 的传递来源）合并后的集合，任一来源在生成
  期间失效都会阻断本轮发布。装配前已整轮排除的坏旧来源不进入该合集；令牌、ACL、
  会话与租约等门禁仍独立生效。

## 7. 替代、重新启用与删除

- **POST `/supersede`**：同库、非自身、替代目标 fresh ready 且未取消且处于生效窗口；
  替代链防环；原文档保留文件、chunks、向量、入库状态与 `index_version`；原文档已被
  替代时须先显式 reenable；CAS 409。
- **硬删除不同**：`DELETE /documents/{doc_id}` 会删除文档行、chunks、向量与原文件。
  删除替代目标时，自引用外键只把 `superseded_by_id` 置 `NULL`，原文档的
  `superseded_at` 保留，因此仍为 superseded，**绝不复活**。
- **POST `/reenable`**：显式清除替代标记并置回 `enabled=true`；要求当前处于 effective
  窗口——`scheduled` / `expired` 一律 409，**绝不静默抹除期限**；无索引/文件副作用。
- 重复上传命中 inactive canonical 一律 409；dedup/reindex 绝不重置治理字段，也不因
  重新索引把已停用/已替代/已过期文档变回可检索。

## 8. 审计

治理操作审计只记录稳定 ID、`governance_revision` 与变更字段名；不写文档正文、
不写 `maintainer` 自由文本、不写问题/答案内容。审计写入失败会记录指标
（`ragkb_audit_write_failures_total`）与固定事件名（回滚失败时另有回滚失败事件），
但不回滚已提交的治理变更。

## 9. 上线与回滚边界

1. **旧代码忽略治理字段不是安全回滚**：任何绕过 `document_lifecycle` 谓词直接读
   `documents` / `document_chunks` 的旧查询，都会把不可检索文档重新暴露给检索链路。
2. 回滚必须二选一：保留治理读取谓词与发布门禁（新写入可停用），或先进入维护态
   （停止入库/检索/问答入口）再回退。仅回退代码而不处理既有治理字段属于安全缺陷。
3. 本轮不执行生产环境升级或回滚；生产切换需另行评审与演练。
4. 本仓库未实现自动提醒、自动续期或“版本裁决”机制，不应按已具备这些能力描述。

## 10. 职责分工（由部署方填写）

| 事项 | 负责人 | 审核节奏 |
| --- | --- | --- |
| 设置/更新 `maintainer` | _部署方填写_ | _部署方填写_ |
| `last_reviewed_at` 审核周期 | _部署方填写_ | _部署方填写_ |
| 生效窗口与到期续期 | _部署方填写_ | _部署方填写_ |
| 替代链管理 | _部署方填写_ | _部署方填写_ |

系统层面不做上述事项的自动提醒或自动处置；到期文档按读取时刻自动进入 `expired`，
恢复必须显式操作。

## 11. 验证范围与未决

本规范的核对依据是当前代码与 diff，包括生命周期/来源快照的合成用例与并发回归。
这些**不等于**真实企业资料或生产服务器验收。以下维度仍属待验证：

- 真实语料的规模、格式分布与解析质量；
- 真实 embedding / rerank / 模型在既有配置下的行为、时延与费用；
- 多实例部署下 PostgreSQL 行锁/advisory 锁的实际争用；
- 长时间运行中到期、撤销、重新索引与流式发布的交叉场景；
- 部署方实际 ACL、身份源与令牌失效策略。

在上述项目完成前，不得以本机合成/替身/并发回归通过作为上线结论。

## 12. 资料更新差异预览与确认（v1）

**GET** `/api/v1/knowledge-bases/{kb_id}/documents/{doc_id}/comparison?replacement_document_id=<uuid>`

- 仅管理员、仅 `kind='upload'` 的普通上传库；两份文档必须同库且非自身
  （自身 400，跨库/不存在 404，项目库 400）。响应 `Cache-Control: no-store`。
- 纯只读：不写审计、不写会话、不生成答案、不在日志/响应外记录正文；比较对象是
  `document_chunks` 中已入库的文本，不解析原文件、不调用模型、不新增表/列。
- 读取顺序：两张 `documents` 行按 UUID 升序加共享行锁（`FOR SHARE`，
  `populate_existing`），锁等待结束后才取当前 UTC，随后按 `chunk_index` 读取
  `document_chunks`；不存在“先读文档、后补锁”的路径。
- 资格：两边 `status='ready'`、`cancel_requested` 严格 `False`、`index_version`
  非负整数，否则 `not_ready`；替代目标当前生命周期必须为 `active`，否则
  `not_eligible`（原文档可以是 `superseded`，允许只读比较历史版本）；实际分块必须
  从 0 连续、条数与 `chunk_count` 一致、全部 `embedded`，否则
  `incomplete`/`empty_document`。每边最多 1000 块、200000 字符（先用 SQL
  `count`/`sum(length(content))` 判断，超限不加载正文），任一边拆分后的比较单元
  最多 2000 个。任何不可比较情形均返回 HTTP 200、`comparable=false`、
  `fingerprint=null`、`rows=[]`、`summary=null`，不返回部分 diff。
- 拆分与对齐：每个 chunk 正文用 `str.splitlines()` 拆行（保留内部空行，统一
  CRLF/CR），不跨 chunk 合并、不去重；`difflib.SequenceMatcher(autojunk=False)`
  的 equal/delete/insert/replace 分别映射为 unchanged/removed/added/modified
  （replace 前 `min(len)` 对为 modified，多出的分别为 removed/added）。
- 响应指纹 `comparison_fingerprint` 为 `ingested-chunks-v1` 的 SHA-256（64 位小写
  十六进制），覆盖两边稳定 ID、`kb_id`、`index_version`、`governance_revision`、
  `status`、`enabled`、生效窗口、替代字段与按 `chunk_index` 排序的分块内容摘要；
  不含正文、路径或用户输入。它只用于陈旧检测，**不是授权凭证**。
- **POST `/supersede`** 可选携带 `comparison_fingerprint`（仅 64 位小写十六进制）。
  服务在原有 User → KB → 按 UUID 升序文档锁内、任何 ORM 变更之前复核两边资格、
  分块布局/上限并重算指纹；不一致返回 409「资料版本已变化，请重新预览差异后再
  确认」，零修改、不自动重试。不传指纹的旧请求保持原有行为。确认后仍走既有替代
  路径：旧文件、分块、向量、入库状态与 `index_version` 全部保留，审计只写稳定
  ID/revision/字段名。
