/**
 * 会话文档范围的纯状态逻辑。
 *
 * 后端契约：
 * - DocumentScopeItem = { document_id, filename, index_version }
 * - 空数组表示检索整个知识库（不是"无依据"）
 * - scope_revision 单调递增，用于拒绝过期快照
 *
 * 这里刻意不依赖 api/axios/vue，方便在 node 测试里直接跑。
 */
export interface DocumentScopeItem {
  document_id: string
  filename: string
  index_version: number
}

export interface DocumentScopeSnapshot {
  items: DocumentScopeItem[]
  revision: number
}

export interface ScopeSnapshotPayload {
  document_scope?: unknown
  scope_revision?: unknown
}

export interface ScopeSendOptions {
  document_ids?: string[]
  scope_revision?: number
}

export interface ScopePatchBody {
  document_ids: string[]
  expected_revision?: number
}

/** 一次范围确认需要执行的动作。 */
export type ScopeChangeAction = 'skip' | 'draft' | 'patch'

/**
 * PATCH 响应的处理结果。
 * - incomplete：缺少完整的 document_scope/scope_revision，不能据此改动 UI
 * - stale：版本号落后于本地，不能覆盖新状态
 */
export type ScopePatchResolution =
  | { status: 'applied'; snapshot: DocumentScopeSnapshot }
  | { status: 'incomplete' }
  | { status: 'stale' }

/** 后端限制：一次最多限定 5 份文档。 */
export const MAX_SCOPE_DOCUMENTS = 5

function normalizeIndexVersion(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.trunc(value) : 0
}

/**
 * 旧响应可能没有 document_scope；脏数据也不能让界面崩掉。
 * 按 document_id 去重（保留首次出现），filename 缺失时退回 id。
 */
export function normalizeDocumentScope(value: unknown): DocumentScopeItem[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  const items: DocumentScopeItem[] = []
  for (const candidate of value) {
    if (!candidate || typeof candidate !== 'object') continue
    const record = candidate as Record<string, unknown>
    const documentId = typeof record.document_id === 'string' ? record.document_id.trim() : ''
    if (!documentId || seen.has(documentId)) continue
    seen.add(documentId)
    const filename = typeof record.filename === 'string' ? record.filename.trim() : ''
    items.push({
      document_id: documentId,
      filename: filename || documentId,
      index_version: normalizeIndexVersion(record.index_version)
    })
  }
  return items
}

/** 缺失、负数、小数或非数字一律按 0（兼容旧响应）。 */
export function normalizeScopeRevision(value: unknown): number {
  return normalizeIndexVersion(value)
}

export function conversationScope(
  conversation: { document_scope?: unknown; scope_revision?: unknown } | null | undefined
): DocumentScopeSnapshot {
  return {
    items: normalizeDocumentScope(conversation?.document_scope),
    revision: normalizeScopeRevision(conversation?.scope_revision)
  }
}

export function scopeDocumentIds(items: readonly DocumentScopeItem[]): string[] {
  return items.map((item) => item.document_id)
}

/** 顺序无关的指纹：用于判断未发送的草稿是否发生变化，已有会话仍需 PATCH 重验。 */
export function scopeFingerprint(items: readonly DocumentScopeItem[]): string {
  return items
    .map((item) => `${item.document_id}@${item.index_version}`)
    .sort()
    .join('|')
}

export function sameDocumentScope(a: readonly DocumentScopeItem[], b: readonly DocumentScopeItem[]): boolean {
  return scopeFingerprint(a) === scopeFingerprint(b)
}

/**
 * 决定用户确认范围后的动作。
 *
 * 已有会话即使文档 ID 完全相同也必须 PATCH：文件可能已重新索引，
 * 本地缓存的 index_version 已经过期，只有服务端能重新校验并绑定最新代次。
 * 尚未创建会话时只是草稿，指纹相同就无需改动。
 */
export function planScopeChange(
  current: readonly DocumentScopeItem[],
  next: readonly DocumentScopeItem[],
  hasConversation: boolean
): ScopeChangeAction {
  if (hasConversation) return 'patch'
  return sameDocumentScope(current, next) ? 'skip' : 'draft'
}

export function scopeItemFromDocument(
  documentId: string | null | undefined,
  filename: string | null | undefined,
  indexVersion?: number | null
): DocumentScopeItem | null {
  const id = typeof documentId === 'string' ? documentId.trim() : ''
  if (!id) return null
  const name = typeof filename === 'string' ? filename.trim() : ''
  return { document_id: id, filename: name || id, index_version: normalizeIndexVersion(indexVersion ?? undefined) }
}

/**
 * 把 PATCH / SSE 返回的范围合并进当前状态。
 *
 * - 两个字段都没有：返回 null，调用方保持原状（旧后端/普通对话）。
 * - revision 比当前小：过期快照，丢弃。
 * - 只带 scope 不带 revision：接受 scope（调用方需保证来自当前会话的活跃流）。
 * - 只带 revision：只推进版本，不动文档列表。
 */
export function applyScopeUpdate(
  current: DocumentScopeSnapshot,
  payload: ScopeSnapshotPayload | null | undefined
): DocumentScopeSnapshot | null {
  if (!payload) return null
  const hasScope = Array.isArray(payload.document_scope)
  const hasRevision = typeof payload.scope_revision === 'number' && Number.isFinite(payload.scope_revision)
  if (!hasScope && !hasRevision) return null

  const revision = hasRevision ? normalizeScopeRevision(payload.scope_revision) : current.revision
  if (hasRevision && revision < current.revision) return null

  return {
    items: hasScope ? normalizeDocumentScope(payload.document_scope) : current.items,
    revision
  }
}

/**
 * 严格解析 PATCH 响应。
 *
 * 与 SSE 不同，PATCH 是用户主动改范围，响应必须同时给出 document_scope 和
 * scope_revision 才能确认服务端真正接受了什么。缺少任一字段时返回 incomplete，
 * 调用方应保持原范围（不能把请求里的 next 当成已生效结果，尤其是清空/扩大范围时）。
 */
export function resolveScopePatchResponse(
  current: DocumentScopeSnapshot,
  payload: unknown
): ScopePatchResolution {
  if (!payload || typeof payload !== 'object') return { status: 'incomplete' }
  const record = payload as Record<string, unknown>
  const hasScope = Array.isArray(record.document_scope)
  const hasRevision = typeof record.scope_revision === 'number'
    && Number.isFinite(record.scope_revision)
    && record.scope_revision >= 0
  if (!hasScope || !hasRevision) return { status: 'incomplete' }

  const snapshot = applyScopeUpdate(current, {
    document_scope: record.document_scope,
    scope_revision: record.scope_revision
  })
  if (!snapshot) return { status: 'stale' }
  return { status: 'applied', snapshot }
}

/**
 * 多选切换。达到上限时拒绝新增，但永远允许移除。
 * 返回 rejected 让 UI 能给出可见提示，而不是悄悄吞掉点击。
 */
export function toggleDocumentId(
  selected: readonly string[],
  documentId: string,
  max = MAX_SCOPE_DOCUMENTS
): { ids: string[]; rejected: boolean } {
  const ids = [...selected]
  const index = ids.indexOf(documentId)
  if (index >= 0) {
    ids.splice(index, 1)
    return { ids, rejected: false }
  }
  if (ids.length >= max) return { ids, rejected: true }
  ids.push(documentId)
  return { ids, rejected: false }
}

/**
 * SSE 请求里的范围字段：
 * - 新会话携带 draft 的 document_ids（空草稿不传 = 整个知识库）
 * - 已有会话携带 revision 快照；文档列表通过 PATCH 更新
 * - 普通对话两者都不传
 */
export function buildScopeSendOptions(input: {
  mode: 'rag' | 'chat'
  items: readonly DocumentScopeItem[]
  hasConversation: boolean
  revision: number
}): ScopeSendOptions {
  if (input.mode !== 'rag') return {}
  if (input.hasConversation) return { scope_revision: normalizeScopeRevision(input.revision) }
  const documentIds = scopeDocumentIds(input.items).filter(Boolean)
  return documentIds.length ? { document_ids: documentIds } : {}
}

/** PATCH body；expectedRevision 未提供时不传，提供 0 也按真实快照发送。 */
export function buildScopePatchBody(
  documentIds: readonly string[],
  expectedRevision?: number
): ScopePatchBody {
  const body: ScopePatchBody = { document_ids: [...documentIds] }
  if (typeof expectedRevision === 'number' && Number.isFinite(expectedRevision) && expectedRevision >= 0) {
    body.expected_revision = Math.trunc(expectedRevision)
  }
  return body
}
