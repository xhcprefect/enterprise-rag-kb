/**
 * 文档列表的查询状态与迟到响应守卫（纯逻辑，可 Node 测试）。
 *
 * 搜索、翻页、轮询共用同一份查询快照；只有"发出请求时的快照"仍是当前快照，
 * 响应才允许写入界面，避免慢请求覆盖用户刚切换的筛选/页码。
 */
import { MAX_SCOPE_DOCUMENTS } from './documentScope.ts'

export interface DocumentQuery {
  q: string
  page: number
  pageSize: number
  status: string
}

/** 输入框内容统一 trim；超过 200 字符截断（与后端一致，不是报错）。 */
export const SEARCH_MAX_LENGTH = 200

export function normalizeQueryText(value: string | null | undefined): string {
  if (typeof value !== 'string') return ''
  return value.trim().slice(0, SEARCH_MAX_LENGTH)
}

export function makeQuery(partial: Partial<DocumentQuery>): DocumentQuery {
  return {
    q: normalizeQueryText(partial.q),
    page: Math.max(1, Math.trunc(partial.page ?? 1)),
    pageSize: Math.max(1, Math.trunc(partial.pageSize ?? 20)),
    status: typeof partial.status === 'string' ? partial.status : ''
  }
}

/** 两个查询是否等价；用于判断迟到响应是否仍有效。 */
export function sameQuery(left: DocumentQuery, right: DocumentQuery): boolean {
  return left.q === right.q && left.page === right.page &&
    left.pageSize === right.pageSize && left.status === right.status
}

/** 列表接口参数：空搜索/空状态不发送，避免无意义的查询串。 */
export function queryParams(query: DocumentQuery): {
  page: number
  page_size: number
  status?: string
  q?: string
} {
  const params: { page: number; page_size: number; status?: string; q?: string } = {
    page: query.page,
    page_size: query.pageSize
  }
  if (query.status) params.status = query.status
  if (query.q) params.q = query.q
  return params
}

/**
 * 迟到响应守卫：每次请求递增序号，只有最新序号且快照一致时才接受。
 * 返回 true 表示可以写入界面。
 */
export function isLatestResponse(
  sequence: number,
  latestSequence: number,
  requestQuery: DocumentQuery,
  currentQuery: DocumentQuery,
  disposed = false
): boolean {
  return !disposed && sequence === latestSequence && sameQuery(requestQuery, currentQuery)
}

/**
 * 轮询是否还需要继续：列表里仍有 pending/processing，或当前上传仍在入库。
 * 失败/取消是终态，不触发轮询。
 */
export function shouldPoll(statuses: string[], ingesting: boolean): boolean {
  return ingesting || statuses.some((status) => status === 'pending' || status === 'processing')
}

/**
 * 这一拍的响应是否已过期。
 *
 * 轮询里"列表请求 + 当前文档详情请求"是一对；只有发起这一拍时的条目 key、
 * 列表序号都没变，迟到的 404 才允许推进队列。否则旧 active 文档的 404
 * 会把用户刚切换的新条目误判为"已删除"。
 */
export function isStalePoll(
  pollKey: string | null,
  currentKey: string | null | undefined,
  requestEpoch: number,
  latestEpoch: number,
  disposed = false
): boolean {
  if (disposed) return true
  if (!pollKey) return true
  if (pollKey !== currentKey) return true
  return requestEpoch !== latestEpoch
}

/**
 * 确认范围前，对一份"新鲜"详情做有效性检查。
 *
 * 契约：确认前最多重新获取 5 项详情，检查 retrievable 与选中时的原 index_version；
 * 详情返回的 index_version 与选中时不一致，视为"悄悄换代次"，必须拒绝而不是沿用
 * 本地缓存的旧代次。绝不"只核验一部分就放行"：选中的每一项都必须被核验，
 * 预算不足（maxChecks<=0、非整数或少于选中数）一律不返回 ok。
 */
export interface ScopeFreshCheck {
  documentId: string
  retrievable: boolean
  index_version?: number | null
}

export type ScopeFreshDecision =
  | { status: 'ok' }
  | { status: 'notRetrievable'; documentId: string }
  | { status: 'versionChanged'; documentId: string; selectedVersion: number | null; currentVersion: number | null }
  | { status: 'detailMissing'; documentId: string }
  /** 核验预算非法或不足以覆盖全部选中项：明确拒绝，不能只核验前几项就确认。 */
  | { status: 'insufficientChecks' }

/** 预算是 1..MAX_SCOPE_DOCUMENTS 的整数；0/负数/NaN/小数/超界都不接受。 */
export function isValidCheckBudget(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) &&
    value > 0 && value <= MAX_SCOPE_DOCUMENTS
}

/**
 * 对"选中项 + 新鲜详情"做逐项决策。详情缺失（请求失败）时不假装通过。
 * 预算必须是 1..MAX 的整数且 >= 选中数；否则返回 insufficientChecks，绝不降级为 ok。
 */
export function decideScopeFreshChecks(
  selected: readonly { document_id: string; index_version: number }[],
  checks: readonly (ScopeFreshCheck | null)[],
  maxChecks = MAX_SCOPE_DOCUMENTS
): ScopeFreshDecision {
  // 预算必须是 1..MAX_SCOPE_DOCUMENTS 的整数；越界/坏输入一律明确拒绝
  if (!isValidCheckBudget(maxChecks)) {
    return { status: 'insufficientChecks' }
  }
  if (!Array.isArray(selected) || !Array.isArray(checks)) {
    return { status: 'insufficientChecks' }
  }
  if (selected.length > MAX_SCOPE_DOCUMENTS) {
    return { status: 'insufficientChecks' }
  }
  if (selected.length > maxChecks || checks.length < selected.length) {
    return { status: 'insufficientChecks' }
  }
  for (let i = 0; i < selected.length; i += 1) {
    const item = selected[i]
    if (!item || typeof item.document_id !== 'string' || !item.document_id) {
      return { status: 'insufficientChecks' }
    }
    const detail = checks[i]
    if (!detail || detail.documentId !== item.document_id) {
      return { status: 'detailMissing', documentId: item.document_id }
    }
    if (detail.retrievable !== true) {
      return { status: 'notRetrievable', documentId: item.document_id }
    }
    // 严格版本：两侧代次都必须是严格非负整数，float/bool/null 一律视为"不可核验"，
    // 不能截断后与选中代次"碰巧相等"而放行。
    const version = isStrictIndexVersion(detail.index_version) ? detail.index_version : null
    const selectedVersion = isStrictIndexVersion(item.index_version) ? item.index_version : null
    if (selectedVersion === null || version === null || version !== selectedVersion) {
      return {
        status: 'versionChanged',
        documentId: item.document_id,
        selectedVersion,
        currentVersion: version
      }
    }
  }
  return { status: 'ok' }
}

// ---------- 范围选择器的列表页合并（纯逻辑，Node 可测） ----------

export interface ScopeListDoc {
  id: string
  filename: string
  status: string
  retrievable: boolean
  index_version: number
}

export interface ScopeListMergeResult {
  docs: ScopeListDoc[]
  /** 服务端原始页长：分页是否继续只看它，不看本地过滤后的长度 */
  lastPageSize: number
  total: number
}

/**
 * 把一页服务端响应合并到已加载列表。
 *
 * - 本地过滤只影响展示项，绝不参与"是否还有下一页"的判断；
 *   lastPageSize 取原始 items.length，total 取服务端 total（且不小于已合并数）。
 * - 客户端仍要求 status==='ready'、retrievable===true、严格非负整数 index_version，
 *   不满足的条目不进入可选列表（服务端过滤之外的第二道防线）。
 */
export function mergeScopeListPage(
  existing: readonly ScopeListDoc[],
  page: { items?: unknown; total?: unknown } | null | undefined
): ScopeListMergeResult {
  const rawItems = Array.isArray(page?.items) ? page.items : []
  const accepted: ScopeListDoc[] = []
  const seenIds = new Set<string>()
  for (const candidate of rawItems) {
    if (!candidate || typeof candidate !== 'object') continue
    const record = candidate as Record<string, unknown>
    if (typeof record.id !== 'string' || !record.id) continue
    if (record.status !== 'ready') continue
    if (record.retrievable !== true) continue
    if (!isStrictIndexVersion(record.index_version)) continue
    if (seenIds.has(record.id)) continue
    seenIds.add(record.id)
    accepted.push({
      id: record.id,
      filename: typeof record.filename === 'string' && record.filename ? record.filename : record.id,
      status: record.status,
      retrievable: true,
      index_version: record.index_version
    })
  }
  const seen = new Set(existing.map((doc) => doc.id))
  const merged = [...existing, ...accepted.filter((doc) => !seen.has(doc.id))]
  return {
    docs: merged,
    lastPageSize: rawItems.length,
    total: scopeListTotal(page?.total, merged.length)
  }
}

function scopeListTotal(reported: unknown, loaded: number): number {
  const normalized = typeof reported === 'number' && Number.isFinite(reported) ? Math.trunc(reported) : 0
  return Math.max(0, normalized, loaded)
}

/** 是否还有下一页：只用原始页长与总页数，不用本地筛选后的长度。 */
export function hasMoreScopePages(
  lastPageSize: number,
  loadedCount: number,
  total: number,
  pageSize: number,
  loading = false
): boolean {
  if (loading) return false
  return lastPageSize >= pageSize && loadedCount < total
}

/**
 * 选中某一项时绑定的 index_version。
 *
 * - 打开时继承、且用户没有移除过的旧选择：沿用它的原代次（绝不静默升级）。
 * - 新选 / 移除后重选：用列表返回的当前代次。
 * - 任一侧不是严格非负整数：返回 null，调用方必须拒绝这次选择，不能退化成 0。
 */
export function resolveSelectedIndexVersion(
  keptFromOpen: boolean,
  priorVersion: unknown,
  listVersion: unknown
): number | null {
  const source = keptFromOpen ? priorVersion : listVersion
  return isStrictIndexVersion(source) ? source : null
}

// ---------- 列表加载守卫（纯逻辑，Node 可测） ----------

export interface ScopeLoadToken {
  session: number
  generation: number
}

/**
 * 列表请求的会话/代次守卫：重开、切 KB、关闭、卸载都换 session，
 * 迟到响应拿旧 token 比对失败，绝不允许写入新会话的 UI 或清掉新 busy。
 */
export function createScopeLoadGuard() {
  let session = 0
  let generation = 0
  return {
    /** 新会话（重开/切 KB/关闭/卸载）：旧 token 全部失效。 */
    reset(): ScopeLoadToken {
      session += 1
      generation = 0
      return { session, generation }
    },
    /** 开始一次加载：每次 begin 都开新代次，旧代次的迟到响应立即失效。 */
    begin(_reset: boolean): ScopeLoadToken {
      generation += 1
      return { session, generation }
    },
    /** 该 token 是否仍属于当前会话的当前代次。 */
    isCurrent(token: ScopeLoadToken): boolean {
      return token.session === session && token.generation === generation
    }
  }
}

// ---------- 范围确认控制器（纯逻辑，Node 可测、支持 deferred promise） ----------

/** 严格校验：index_version 必须是非负整数（拒绝 float/bool/null/NaN/字符串）。 */
export function isStrictIndexVersion(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

export type ScopeSelectionItem = { document_id: string; filename: string; index_version: number }

export type ScopeSelectionRejection =
  | 'invalidMax'
  | 'tooMany'
  | 'duplicate'
  | 'missingDocumentId'
  | 'invalidFilename'
  | 'invalidIndexVersion'
  /** 入参本身不是数组：明确的坏输入，不退化成"空选择"。 */
  | 'invalidInput'

export type ScopeSelectionValidation =
  | { ok: true; items: ScopeSelectionItem[] }
  | { ok: false; reason: ScopeSelectionRejection; documentId?: string }

/**
 * 严格校验一次确认的选择：上限、重复、ID/文件名、严格代次。
 * 上限与后端契约一致：0 <= max <= MAX_SCOPE_DOCUMENTS，超大 max 不放宽。
 * 只用于本轮确认的安全边界；既有兼容 normalize 不改。
 */
export function validateScopeSelection(
  items: readonly unknown[],
  max: number
): ScopeSelectionValidation {
  if (typeof max !== 'number' || !Number.isInteger(max) || max < 0 || max > MAX_SCOPE_DOCUMENTS) {
    return { ok: false, reason: 'invalidMax' }
  }
  if (!Array.isArray(items)) return { ok: false, reason: 'invalidInput' }
  if (items.length > max) return { ok: false, reason: 'tooMany' }
  const seen = new Set<string>()
  const out: ScopeSelectionItem[] = []
  for (const item of items) {
    if (!item || typeof item !== 'object') return { ok: false, reason: 'missingDocumentId' }
    const record = item as Record<string, unknown>
    if (typeof record.document_id !== 'string' || !record.document_id) {
      return { ok: false, reason: 'missingDocumentId' }
    }
    if (seen.has(record.document_id)) {
      return { ok: false, reason: 'duplicate', documentId: record.document_id }
    }
    if (typeof record.filename !== 'string' || !record.filename) {
      return { ok: false, reason: 'invalidFilename', documentId: record.document_id }
    }
    if (!isStrictIndexVersion(record.index_version)) {
      return { ok: false, reason: 'invalidIndexVersion', documentId: record.document_id }
    }
    seen.add(record.document_id)
    out.push({
      document_id: record.document_id,
      filename: record.filename,
      index_version: record.index_version
    })
  }
  return { ok: true, items: out }
}

export interface ScopeDetailResponse {
  id?: unknown
  kb_id?: unknown
  retrievable?: unknown
  index_version?: unknown
}

/**
 * 把一份文档详情转成核验快照：必须是同一个 KB、同一个文档 id，
 * 否则返回 null（当作"详情不可用"，不允许冒领）。
 */
export function scopeFreshCheckFromDetail(
  kbId: string,
  documentId: string,
  data: unknown
): ScopeFreshCheck | null {
  if (!kbId || !documentId) return null
  if (!data || typeof data !== 'object') return null
  const record = data as ScopeDetailResponse
  if (record.id !== documentId) return null
  if (typeof record.kb_id !== 'string' || record.kb_id !== kbId) return null
  return {
    documentId,
    retrievable: record.retrievable === true,
    index_version: isStrictIndexVersion(record.index_version) ? record.index_version : null
  }
}

export interface ScopeDetailFetcherDeps {
  /** 本次确认发起时绑定的 KB；在 await 前读取。 */
  kbId: () => string
  /** 实际请求（返回详情响应体）。 */
  fetch: (kbId: string, documentId: string) => Promise<unknown>
  /** 发起时的 KB 是否仍是当前 KB；切库/关闭后必须为 false。 */
  isKbCurrent: (kbId: string) => boolean
}

/**
 * 绑定发起 KB 的详情取数器：请求前后都校验 KB，迟到响应不会串到新 KB。
 */
export function createScopeDetailFetcher(deps: ScopeDetailFetcherDeps) {
  return async (documentId: string): Promise<ScopeFreshCheck | null> => {
    const kbId = deps.kbId()
    if (!kbId || !deps.isKbCurrent(kbId)) return null
    let data: unknown
    try {
      data = await deps.fetch(kbId, documentId)
    } catch {
      return null
    }
    // 请求期间切了 KB：迟到的详情不能用于新库的确认
    if (!deps.isKbCurrent(kbId)) return null
    return scopeFreshCheckFromDetail(kbId, documentId, data)
  }
}

export type ScopeConfirmOutcome =
  | { status: 'confirmed'; items: ScopeSelectionItem[] }
  | { status: 'empty' }
  | { status: 'invalidated' }
  | { status: 'busy' }
  | { status: 'invalidSelection'; reason: ScopeSelectionRejection; documentId?: string }
  | { status: 'notRetrievable'; documentId: string }
  | { status: 'versionChanged'; documentId: string; selectedVersion: number | null; currentVersion: number | null }
  | { status: 'detailMissing'; documentId: string }
  /** 每项都必须核验；预算不足时明确拒绝，绝不只核验一部分就确认。 */
  | { status: 'insufficientChecks' }

export interface ScopeConfirmDeps {
  /** 拉取一项"新鲜"详情；请求失败返回 null，不假装成功。 */
  getDetail: (documentId: string) => Promise<ScopeFreshCheck | null>
  /**
   * 前端每次确认最多重取详情数。只有 undefined 才取默认 5（后端上限）；
   * 显式的 0/负数/小数/NaN/超界一律视为配置错误，确认时明确拒绝。
   */
  maxChecks?: number
}

/**
 * 确认流程控制器：confirm 只在"打开且存活"时允许一次；关闭/切 KB/卸载
 * invalidate 后，迟到的详情响应只返回 invalidated，绝不 emit 旧结果，
 * 也不清 busy 状态。确认期间的 selection 是入参快照的冻结副本。
 */
export function createScopeConfirmController(deps: ScopeConfirmDeps) {
  // 只有完全没给值才默认 5；给了坏值保留坏值，由 confirm 明确拒绝
  const configuredChecks: unknown = deps.maxChecks === undefined ? MAX_SCOPE_DOCUMENTS : deps.maxChecks
  let token = 0
  let inFlight = false

  function invalidate() {
    // 新会话开始：旧请求作废，busy 属于旧会话，不再占用新会话；
    // 旧请求的 finally 因 token 不匹配而不会清掉新一次确认的 busy。
    token += 1
    inFlight = false
  }

  async function confirm(
    selected: readonly unknown[],
    maxOption?: number
  ): Promise<ScopeConfirmOutcome> {
    if (inFlight) return { status: 'busy' }
    if (!Array.isArray(selected) || !selected.length) return { status: 'empty' }
    // 契约上限固定为 MAX_SCOPE_DOCUMENTS：任何更大的 max 都不放宽；非法 max 直接拒绝
    const requestedMax = maxOption === undefined ? MAX_SCOPE_DOCUMENTS : maxOption
    if (typeof requestedMax !== 'number' || !Number.isInteger(requestedMax) ||
      requestedMax < 0 || requestedMax > MAX_SCOPE_DOCUMENTS) {
      return { status: 'invalidSelection', reason: 'invalidMax' }
    }
    // 预算配置非法：不能静默退回 5，明确拒绝
    if (!isValidCheckBudget(configuredChecks)) return { status: 'insufficientChecks' }
    const maxChecks = configuredChecks
    const validation = validateScopeSelection(selected, requestedMax)
    if (!validation.ok) {
      return validation.documentId === undefined
        ? { status: 'invalidSelection', reason: validation.reason }
        : { status: 'invalidSelection', reason: validation.reason, documentId: validation.documentId }
    }
    // 每一项都要核验：不再 slice(0, maxChecks) 后只核验一部分却确认全部。
    if (validation.items.length > maxChecks) return { status: 'insufficientChecks' }
    const items = validation.items
    inFlight = true
    const myToken = token
    try {
      const details = await Promise.all(items.map((item) =>
        deps.getDetail(item.document_id).then(
          (data) => data ?? null,
          () => null
        )
      ))
      if (myToken !== token) return { status: 'invalidated' }
      const decision = decideScopeFreshChecks(items, details, maxChecks)
      if (decision.status === 'ok') return { status: 'confirmed', items }
      return decision
    } finally {
      if (myToken === token) inFlight = false
    }
  }

  return {
    invalidate,
    confirm,
    get inFlight() { return inFlight }
  }
}
