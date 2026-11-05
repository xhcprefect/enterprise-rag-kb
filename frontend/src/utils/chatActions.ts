/**
 * 回答复制与"重试"的纯逻辑。
 *
 * 复制：只搬运纯文本。代码块通过事件委托读 `pre > code` 的 textContent，
 * 绝不把渲染后的 HTML 再塞回 DOM，也不执行其中的脚本。
 *
 * 重试：失败时冻结当时的 question/mode/kb/conversation/top_k/document ids+revision。
 * UI 上的 status='failed' 不足以判定可重试（断连、403/404/410、GET 失败都可能没有
 * 终态证据），所以调用方必须先查原请求；这里只负责把"原请求状态 + 当前上下文 + 冻结
 * 快照"折算成明确的决定，绝不悄悄改范围重发，也绝不凭历史猜出原来问了什么。
 *
 * 不依赖 vue/api，node 测试可直接运行。
 */
import { normalizeDocumentScope, type DocumentScopeItem } from './documentScope.ts'

export type RetryMode = 'rag' | 'chat'

/** 失败那一刻的完整发送快照；缺字段就用 null 表示"当时没传/不知道"。 */
export interface RetrySnapshot {
  question: string
  mode: RetryMode
  kbId: string | null
  conversationId: string | null
  /** null = 当时没传 top_k，由服务端 RETRIEVAL_TOP_K 决定（重试时同样不传）。 */
  topK: number | null
  /** 新会话草稿范围；已有会话为 null（范围由 revision 决定）。 */
  documentIds: string[] | null
  /** 草稿明细（含 index_version）：同一 ID 重建后版本变化也算依据变了。 */
  documentScope: readonly DocumentScopeItem[] | null
  scopeRevision: number | null
  /**
   * 项目知识库：true 时 document_ids 永远不传（后端显式互斥 → 400），
   * 新会话改带 projectSnapshotId，已有会话只带 scopeRevision。
   */
  project: boolean
  /** 本次实际绑定的项目快照；已有会话仅用于核验，重发请求不再携带此字段。 */
  projectSnapshotId: string | null
}

/** 当前界面上的上下文，用于确认"重发会不会改变依据"。 */
export interface RetryContext {
  mode: RetryMode
  kbId: string | null
  conversationId: string | null
  scopeRevision: number
  documentScope: readonly DocumentScopeItem[]
  /** 当前知识库是否为项目索引库。 */
  project?: boolean
  /** 当前界面选中的项目快照（仅新项目会话的草稿有意义）。 */
  projectSnapshotId?: string | null
}

/** 原请求的可信状态；unknown 表示查不到或读失败，不能据此重发。 */
export type RetryRequestStatus = 'processing' | 'completed' | 'failed' | 'cancelled' | 'unknown'

export type RetryDecision =
  /** 可以重新提问：用 snapshot 里的字段 + 新的 request_id。 */
  | { action: 'resend'; snapshot: RetrySnapshot }
  /** 原请求还没结束或已经成功：只做只读恢复/提示，不重发。 */
  | { action: 'readonly'; reason: 'processing' | 'completed' }
  /** 无法确认原请求结果：提示稍后再查，不重发。 */
  | { action: 'unknown' }
  /** 历史里没有原始提问/范围的快照：提示复制原问题，不能编造重放。 */
  | { action: 'unavailable' }
  /** 模式/知识库/会话/范围已经变了：拒绝静默改依据。 */
  | { action: 'mismatch' }

/** 冻结发送快照；调用方在失败分支里立刻调用，之后再变 UI 也不影响。 */
export function freezeRetrySnapshot(input: {
  question: string
  mode: RetryMode
  kbId?: string | null
  conversationId?: string | null
  topK?: number | null
  documentIds?: readonly string[] | null
  /** 草稿明细；给了就同时冻结 index_version（同 ID 重建不能算同一依据）。 */
  documentScope?: readonly DocumentScopeItem[] | null
  scopeRevision?: number | null
  /** 项目知识库：document_ids 必须整体省略。 */
  project?: boolean
  /** 新项目会话要显式绑定的快照 id。 */
  projectSnapshotId?: string | null
}): RetrySnapshot | null {
  const question = typeof input.question === 'string' ? input.question.trim() : ''
  // 没有原文就没有可重放的内容，宁可让用户复制，也不拼一个"差不多"的问题。
  if (!question || question.length > 4000) return null
  const project = input.project === true
  const documentIds = !project && input.documentIds && input.documentIds.length
    ? Object.freeze([...new Set(input.documentIds)]) as string[]
    : null
  const scope = !project && input.documentScope && input.documentScope.length
    ? Object.freeze(input.documentScope.map((item) => Object.freeze({ ...item }))) as DocumentScopeItem[]
    : null
  return Object.freeze({
    question,
    mode: input.mode,
    kbId: input.kbId || null,
    conversationId: input.conversationId || null,
    topK: normalizeTopK(input.topK),
    documentIds,
    documentScope: scope,
    scopeRevision: normalizeRevision(input.scopeRevision),
    project,
    projectSnapshotId: project && typeof input.projectSnapshotId === 'string'
      ? input.projectSnapshotId
      : null
  })
}

/**
 * 用服务端可信结果补全快照里的会话身份。
 *
 * 新会话的 conversation_id 是服务端在认领事务里生成的：发送前快照只能记 null。
 * meta / 原请求状态返回真实会话后，必须据此重新绑定，否则失败重试要么永远
 * mismatch，要么带着草稿 document_ids 再建一个会话。
 *
 * 只接受服务端给出的值，绝不用界面状态猜会话或版本。已有会话**不会**被改写
 * 版本号：服务端此刻的 revision 可能已经变了，采用它等于悄悄换了重发依据。
 */
export function bindRetrySnapshot(
  snapshot: RetrySnapshot | null,
  binding: { conversationId?: string | null; scopeRevision?: number | null; projectSnapshotId?: string | null }
): RetrySnapshot | null {
  if (!snapshot) return null
  if (snapshot.conversationId) return snapshot
  const conversationId = typeof binding.conversationId === 'string' ? binding.conversationId.trim() : ''
  if (!conversationId) return snapshot
  return Object.freeze({
    ...snapshot,
    conversationId,
    // 会话已经存在：范围由服务端 revision 决定，草稿 document_ids 不能再发。
    documentIds: null,
    documentScope: null,
    // 保留实际绑定用于重试核验；buildRetryPayload 仅在新会话中发送快照字段。
    projectSnapshotId: snapshot.project ? binding.projectSnapshotId ?? snapshot.projectSnapshotId : null,
    // 服务端没给版本时保持 null：宁可 mismatch 提示用户，也不猜一个 0 去重发。
    scopeRevision: normalizeRevision(binding.scopeRevision)
  })
}

/** 服务端对原请求的当前状态；GET 只给"当前"范围，不是发送时的快照。 */
export interface RetryServerState {
  conversationId?: unknown
  documentScope?: unknown
  scopeRevision?: unknown
  /** 原请求那一轮实际绑定的项目快照（turn sid），项目会话的强证据。 */
  projectSnapshotId?: unknown
}

/** 绑定成功后可安全写回界面的范围（与冻结范围已被证明同一批文档）。 */
export interface RetryBindingScope {
  items: DocumentScopeItem[]
  revision: number
}

export type RetryBindingPlan =
  | { action: 'resend'; snapshot: RetrySnapshot; scope: RetryBindingScope | null }
  | { action: 'mismatch' }

/**
 * 用服务端状态**证明**冻结快照仍指向同一依据，再决定能否补会话身份。
 *
 * 关键点：`GET /requests/{id}` 返回的是会话**当前**的 document_scope / scope_revision，
 * 不是失败那一次发送的快照。所以：
 * - 已有会话：当前 revision 必须与冻结 revision 完全一致，否则说明范围已被别处改过，
 *   拒绝重试（绝不采纳新 revision 当作"原依据"）；
 * - 新会话草稿：只有 冻结文档集合 == 界面当前草稿 == 服务端当前范围 三者一致时，
 *   才可以补上会话 ID 并把范围写回界面；任何一处不一致都说明依据变了。
 *
 * 该函数无副作用：调用方必须先拿到 resend 才能改界面/发请求。
 */
export function planRetryBinding(
  snapshot: RetrySnapshot,
  server: RetryServerState,
  context: RetryContext
): RetryBindingPlan {
  const conversationId = typeof server.conversationId === 'string' ? server.conversationId.trim() : ''
  // 服务端没给出真实会话（例如 404/410 或旧版本）：没有任何可证明的绑定。
  if (!conversationId) return { action: 'mismatch' }
  if (snapshot.mode !== context.mode) return { action: 'mismatch' }
  if ((snapshot.kbId || null) !== (context.kbId || null)) return { action: 'mismatch' }

  if (snapshot.conversationId) {
    if (snapshot.conversationId !== conversationId) return { action: 'mismatch' }
    if (snapshot.mode !== 'rag') return { action: 'resend', snapshot, scope: null }
    const serverRevision = normalizeRevision(server.scopeRevision)
    if (snapshot.scopeRevision === null || serverRevision === null) return { action: 'mismatch' }
    // 三方一致才算"原依据未变"：冻结的、界面此刻的、服务端当前的范围版本。
    // 少了界面这一环，别的页签改过范围后仍会被当成原依据静默重发。
    if (serverRevision !== snapshot.scopeRevision) return { action: 'mismatch' }
    if (context.scopeRevision !== snapshot.scopeRevision) return { action: 'mismatch' }
    // 项目会话：会话绑定的快照固定，必须与界面当前展示的绑定一致，
    // 否则用户已经通过 CAS 换过快照，重发会落在新快照上。
    if (snapshot.project) {
      if (context.project !== true) return { action: 'mismatch' }
      if (!sameSnapshotBinding(context.projectSnapshotId, server.projectSnapshotId)) {
        return { action: 'mismatch' }
      }
    }
    return { action: 'resend', snapshot, scope: null }
  }

  // 新会话草稿：界面若已经打开了别的会话，说明用户已经换了上下文。
  if (context.conversationId) return { action: 'mismatch' }
  if (snapshot.mode !== 'rag') {
    // 普通对话没有范围可证，只需绑定服务端给出的真实会话。
    return {
      action: 'resend',
      snapshot: Object.freeze({ ...snapshot, conversationId, documentIds: null, scopeRevision: null }),
      scope: null
    }
  }
  if (snapshot.project) {
    // 项目新会话：服务端认领时已把快照落到会话/轮次上。
    // 只有 turn 的 sid 与冻结/界面仍一致时，才允许用同一绑定重发。
    if (context.project !== true) return { action: 'mismatch' }
    const frozenSid = snapshot.projectSnapshotId
    if (!frozenSid) return { action: 'mismatch' }
    if (!sameSnapshotBinding(frozenSid, context.projectSnapshotId)) return { action: 'mismatch' }
    if (!sameSnapshotBinding(frozenSid, server.projectSnapshotId)) return { action: 'mismatch' }
    const revision = normalizeRevision(server.scopeRevision)
    if (revision === null) return { action: 'mismatch' }
    return {
      action: 'resend',
      snapshot: Object.freeze({
        ...snapshot,
        conversationId,
        documentIds: null,
        documentScope: null,
        projectSnapshotId: frozenSid,
        scopeRevision: revision
      }),
      scope: null
    }
  }
  const frozenIds = new Set(snapshot.documentIds ?? [])
  const currentIds = new Set(context.documentScope.map((item) => item.document_id))
  // 失败后用户改过草稿范围：不能覆盖成服务端的范围再静默重发。
  if (!sameIdSet(frozenIds, currentIds)) return { action: 'mismatch' }
  // 同一批 ID 但 index_version 变了（文档重建）也不是同一份依据。
  if (snapshot.documentScope && !sameIdSet(scopeKeys(snapshot.documentScope), scopeKeys(context.documentScope))) {
    return { action: 'mismatch' }
  }
  if (!Array.isArray(server.documentScope)) return { action: 'mismatch' }
  const serverItems = normalizeDocumentScope(server.documentScope)
  const serverIds = new Set(serverItems.map((item) => item.document_id))
  if (!sameIdSet(frozenIds, serverIds)) return { action: 'mismatch' }
  if (snapshot.documentScope && !sameIdSet(scopeKeys(snapshot.documentScope), scopeKeys(serverItems))) {
    return { action: 'mismatch' }
  }
  const revision = normalizeRevision(server.scopeRevision)
  if (revision === null) return { action: 'mismatch' }
  // 新会话的 revision 由 claim_turn 写成 int(bool(scope))：0=全库、1=限定文档。
  // 改走范围再改回来会让它更大，此时"看起来一样"的集合已经不是原依据。
  if (revision !== (frozenIds.size ? 1 : 0)) return { action: 'mismatch' }
  return {
    action: 'resend',
    snapshot: Object.freeze({
      ...snapshot,
      conversationId,
      // 会话已存在：范围由 revision 决定，草稿 document_ids 不能再发。
      documentIds: null,
      documentScope: null,
      scopeRevision: revision
    }),
    scope: { items: serverItems, revision }
  }
}

function scopeKeys(items: readonly DocumentScopeItem[]): ReadonlySet<string> {
  return new Set(items.map((item) => `${item.document_id}@${item.index_version}`))
}

/** 快照绑定比对：两边都必须是同一个非空快照 id。 */
function sameSnapshotBinding(a: unknown, b: unknown): boolean {
  const left = typeof a === 'string' ? a.trim() : ''
  const right = typeof b === 'string' ? b.trim() : ''
  return !!left && left === right
}

export type RetryResolution =
  | RetryBindingPlan
  | { action: 'readonly'; reason: 'processing' | 'completed' }
  | { action: 'unknown' }
  | { action: 'unavailable' }
  | { action: 'mismatch' }

/**
 * 失败 → 重试的**完整决策链**（界面与测试共用同一实现）。
 *
 * 顺序很重要：
 * 1. 先按原请求状态和冻结时的上下文判定能不能重发（UI 的 failed 不算证据）；
 * 2. 只有 failed/cancelled 才用服务端返回的"当前"状态做无副作用绑定校验。
 *
 * 任何一步证据不足都返回 mismatch/unknown，由调用方提示，绝不静默改依据。
 * 不修改任何入参，也不写界面：调用方拿到 resend 后才允许改状态并发送。
 */
export function resolveRetry(input: {
  snapshot: RetrySnapshot | null
  status: RetryRequestStatus
  server?: RetryServerState | null
  context: RetryContext
}): RetryResolution {
  const base = planRetry(input.snapshot, input.status, input.context)
  if (base.action !== 'resend') return base
  // 原请求确实失败/取消：GET 的会话身份与范围必须能证明冻结依据未变。
  return planRetryBinding(base.snapshot, input.server ?? {}, input.context)
}

function sameIdSet(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  if (a.size !== b.size) return false
  for (const value of a) if (!b.has(value)) return false
  return true
}

/**
 * 取一份"当时"的上下文副本。
 *
 * 守卫必须比对**快照**：如果直接把响应式数组传出去，等待 GET 期间任何原地
 * 修改（push/splice/改 item 字段）都会同时改到"之前"那一份，守卫就形同虚设。
 * 复制之后 before 与界面彻底脱钩：此后无论界面是替换数组还是原地改，
 * sameRetryContext 都能看出差异。
 */
export function freezeRetryContext(input: RetryContext): RetryContext {
  return Object.freeze({
    mode: input.mode,
    kbId: input.kbId || null,
    conversationId: input.conversationId || null,
    scopeRevision: input.scopeRevision,
    documentScope: Object.freeze(
      input.documentScope.map((item) => Object.freeze({ ...item }))
    ) as DocumentScopeItem[],
    project: input.project === true,
    projectSnapshotId: typeof input.projectSnapshotId === 'string' ? input.projectSnapshotId : null
  })
}

/**
 * 等待原请求状态期间，界面上下文是否仍未变。
 *
 * GET 是异步的：等待中用户可能新建会话、切换模式/知识库或改了范围。
 * 迟到的结果既不能改界面，也不能再发一次请求。
 */
export function sameRetryContext(before: RetryContext, current: RetryContext): boolean {
  if (before.mode !== current.mode) return false
  if ((before.kbId || null) !== (current.kbId || null)) return false
  if ((before.conversationId || null) !== (current.conversationId || null)) return false
  if (before.scopeRevision !== current.scopeRevision) return false
  if ((before.project === true) !== (current.project === true)) return false
  if ((before.projectSnapshotId || null) !== (current.projectSnapshotId || null)) return false
  const left = new Set(before.documentScope.map((item) => `${item.document_id}@${item.index_version}`))
  const right = new Set(current.documentScope.map((item) => `${item.document_id}@${item.index_version}`))
  return sameIdSet(left, right)
}

function normalizeTopK(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 20 ? value : null
}

function normalizeRevision(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
}

/**
 * 重试前的判定。
 *
 * @param snapshot 失败时冻结的发送快照（可能因历史缺失而为 null）
 * @param status   现查的原请求状态；查询失败/无终态证据一律传 'unknown'
 * @param context  当前界面上下文
 */
export function planRetry(
  snapshot: RetrySnapshot | null,
  status: RetryRequestStatus,
  context: RetryContext
): RetryDecision {
  if (status === 'processing') return { action: 'readonly', reason: 'processing' }
  if (status === 'completed') return { action: 'readonly', reason: 'completed' }
  if (status === 'unknown') return { action: 'unknown' }
  // 到这里原请求确实失败了或被取消，才允许用户真的重新提问。
  if (!snapshot) return { action: 'unavailable' }
  if (!sameContext(snapshot, context)) return { action: 'mismatch' }
  return { action: 'resend', snapshot }
}

function sameContext(snapshot: RetrySnapshot, context: RetryContext): boolean {
  if (snapshot.mode !== context.mode) return false
  if ((snapshot.kbId || null) !== (context.kbId || null)) return false
  if ((snapshot.conversationId || null) !== (context.conversationId || null)) return false
  if (snapshot.mode !== 'rag') return true

  if (snapshot.conversationId) {
    // 已有会话：范围由 revision 决定，重发会用当前 revision，必须与冻结时一致。
    if (snapshot.scopeRevision === null || snapshot.scopeRevision !== context.scopeRevision) return false
    // 项目会话额外要求快照绑定未变（改绑走 CAS，会推进 revision，这里双保险）。
    if (snapshot.project) {
      return context.project === true
        && !!context.projectSnapshotId
        && context.projectSnapshotId === snapshot.projectSnapshotId
    }
    return true
  }

  if (snapshot.project) {
    // 新项目会话草稿：必须仍选中同一快照，且界面还没切到别的会话。
    return context.project === true
      && !!snapshot.projectSnapshotId
      && snapshot.projectSnapshotId === context.projectSnapshotId
  }
  // 新会话草稿：重发会带上当前草稿，必须与冻结时的文档集合一致。
  const frozen = snapshot.documentIds || []
  const current = context.documentScope.map((item) => item.document_id)
  if (frozen.length !== current.length) return false
  const frozenSet = new Set(frozen)
  return current.every((id) => frozenSet.has(id))
}

/** 重试请求体的可变部分；request_id 必须由调用方新生成，不能复用原请求。 */
export function buildRetryPayload(snapshot: RetrySnapshot): {
  kb_id?: string
  question: string
  conversation_id: string | null
  top_k?: number
  mode: RetryMode
  document_ids?: string[]
  scope_revision?: number
  project_snapshot_id?: string
} {
  const payload: ReturnType<typeof buildRetryPayload> = {
    question: snapshot.question,
    conversation_id: snapshot.conversationId,
    mode: snapshot.mode
  }
  if (snapshot.mode === 'rag') {
    if (snapshot.kbId) payload.kb_id = snapshot.kbId
    // 冻结的 top_k 优先：即使用户后来拖过滑块，也要保持失败的这次请求的检索条数。
    if (snapshot.topK !== null) payload.top_k = snapshot.topK
    if (snapshot.conversationId) {
      // 已有会话必须带冻结的版本号：服务端在 revision>0 时缺省会按范围冲突拒绝。
      if (snapshot.scopeRevision !== null) payload.scope_revision = snapshot.scopeRevision
    } else if (snapshot.project) {
      // 项目新会话：只带显式快照；document_ids 必须完全省略。
      if (snapshot.projectSnapshotId) payload.project_snapshot_id = snapshot.projectSnapshotId
    } else if (snapshot.documentIds?.length) {
      payload.document_ids = [...snapshot.documentIds]
    }
  }
  return payload
}

// ---------------- 复制 ----------------

/** 从点击目标上找到代码块的纯文本；不是代码块返回 null。 */
export function codeTextFromTarget(target: EventTarget | null): string | null {
  const element = target as (Element & { closest?: (selector: string) => Element | null }) | null
  if (!element || typeof element.closest !== 'function') return null
  const code = element.closest('pre > code')
  if (!code) return null
  // textContent 是纯文本：不解析实体、不执行脚本，也不看 innerHTML。
  return code.textContent ?? ''
}

/** 复制纯文本。返回 false 表示剪贴板不可用，调用方必须给出可见反馈。 */
export async function copyPlainText(
  text: string,
  clipboard?: { writeText(value: string): Promise<void> } | null
): Promise<boolean> {
  if (!text) return false
  const writer = clipboard ?? (typeof navigator !== 'undefined' ? navigator.clipboard : undefined)
  if (!writer || typeof writer.writeText !== 'function') return false
  try {
    await writer.writeText(text)
    return true
  } catch {
    return false
  }
}
