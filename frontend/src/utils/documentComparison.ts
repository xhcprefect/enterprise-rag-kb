/**
 * 文档版本对比的纯逻辑：候选过滤、行过滤/分页、指纹与请求体校验、身份核对。
 *
 * 冻结契约要点：
 * - 对比只基于已入库文本（basis='ingested_chunks'），不触发任何模型调用
 * - comparable=false 时必须按 reason 解释，绝不把空 rows 当成“无差异”
 * - 提交替代必须携带 64 位小写 hex 的 comparison_fingerprint；409 表示预览过期，
 *   必须重新对比，不能静默重试
 * - 版本号/指纹严格校验，绝不静默钳制或补 0
 *
 * 这里刻意不依赖 Vue/axios，方便在 node 测试里直接跑。
 */
import type {
  ComparisonRow,
  DocumentComparison,
  DocumentItem
} from '../api/index.ts'

/** comparison_fingerprint 的唯一合法形态：64 位小写 hex。 */
export const COMPARISON_FINGERPRINT_RE = /^[a-f0-9]{64}$/

/** 对比行本地分页大小。 */
export const COMPARISON_PAGE_SIZE = 50

/** 候选文档每次加载的数量上限（服务端按 page_size 返回 total 供分页）。 */
export const COMPARISON_CANDIDATE_PAGE_SIZE = 100

export const COMPARISON_REASONS = [
  'ok',
  'not_ready',
  'not_eligible',
  'incomplete',
  'empty_document',
  'too_many_chunks',
  'too_many_chars',
  'too_many_units'
] as const

const KNOWN_REASONS = new Set<string>(COMPARISON_REASONS)

/** 后端 reason → i18n key；未知/脏值回退到通用文案，绝不展示原始字符串。 */
export function comparisonReasonKey(reason: string | null | undefined): string {
  const normalized = typeof reason === 'string' ? reason.trim() : ''
  return KNOWN_REASONS.has(normalized)
    ? `documents.comparison.reason.${normalized}`
    : 'documents.comparison.reason.unknown'
}

export const COMPARISON_ROW_KINDS = ['unchanged', 'added', 'removed', 'modified'] as const

const KNOWN_ROW_KINDS = new Set<string>(COMPARISON_ROW_KINDS)

/** 行类型 → i18n key；未知值回退到通用“未知”，不直接展示后端键名。 */
export function comparisonRowKindKey(kind: string | null | undefined): string {
  const normalized = typeof kind === 'string' ? kind.trim() : ''
  return KNOWN_ROW_KINDS.has(normalized)
    ? `documents.comparison.rows.${normalized}`
    : 'documents.comparison.rows.unknown'
}

export type ComparisonRowTagType = 'success' | 'danger' | 'warning' | 'info'

/** 行类型 → Element Plus tag 颜色（仅展示）。 */
export function comparisonRowTagType(kind: string | null | undefined): ComparisonRowTagType {
  if (kind === 'added') return 'success'
  if (kind === 'removed') return 'danger'
  if (kind === 'modified') return 'warning'
  return 'info'
}

/**
 * 可选替代候选：排除自身、非 ready、不可检索、已替代项。
 * 服务端已按 ready+retrievable_only 过滤时这里仍然安全；顺序保持稳定并按 id 去重。
 */
export function availableCandidates(
  items: readonly DocumentItem[] | null | undefined,
  selfId: string | null | undefined
): DocumentItem[] {
  const self = typeof selfId === 'string' ? selfId.trim() : ''
  const seen = new Set<string>()
  const result: DocumentItem[] = []
  for (const item of items ?? []) {
    if (!item || typeof item.id !== 'string') continue
    const id = item.id.trim()
    if (!id || id === self || seen.has(id)) continue
    if (item.status !== 'ready') continue
    if (item.retrievable !== true) continue
    // 后端已过滤时这里不会命中；保留显式判断，避免旧响应把已替代项放进来。
    if (item.superseded_at || item.superseded_by_id) continue
    seen.add(id)
    result.push(item)
  }
  return result
}

/** “只看变化”开关：只保留 added/removed/modified；始终返回新数组。 */
export function filterComparisonRows(
  rows: readonly ComparisonRow[] | null | undefined,
  onlyChanges: boolean
): ComparisonRow[] {
  const all = Array.isArray(rows) ? rows : []
  return onlyChanges ? all.filter((row) => row.kind !== 'unchanged') : all.slice()
}

/** 稳定且唯一的行 key（分页后 index 仍是全局序号，由调用方传入）。 */
export function comparisonRowKey(row: ComparisonRow, index: number): string {
  const original = row?.original
    ? `${row.original.chunk_index}:${row.original.line_index}`
    : '-'
  const replacement = row?.replacement
    ? `${row.replacement.chunk_index}:${row.replacement.line_index}`
    : '-'
  const kind = typeof row?.kind === 'string' && row.kind ? row.kind : 'unknown'
  return `${kind}:${original}:${replacement}:${index}`
}

/** 总页数；空列表/非法页码参数为 0 页。 */
export function comparisonPageCount(
  total: number,
  pageSize = COMPARISON_PAGE_SIZE
): number {
  if (!Number.isFinite(total) || total <= 0) return 0
  if (!Number.isFinite(pageSize) || pageSize <= 0) return 0
  return Math.ceil(total / pageSize)
}

/** 1-based 页码切片；页码越界时夹到 [1, 总页数]，空列表返回空数组。 */
export function comparisonPageSlice<T>(
  rows: readonly T[] | null | undefined,
  page: number,
  pageSize = COMPARISON_PAGE_SIZE
): T[] {
  const list = Array.isArray(rows) ? rows : []
  const pages = comparisonPageCount(list.length, pageSize)
  if (pages === 0) return []
  const safePage = Number.isInteger(page) ? Math.min(Math.max(page, 1), pages) : 1
  const start = (safePage - 1) * pageSize
  return list.slice(start, start + pageSize)
}

/** 指纹是否严格合法（64 位小写 hex，不允许空格/大写）。 */
export function hasValidComparisonFingerprint(value: unknown): value is string {
  return typeof value === 'string' && COMPARISON_FINGERPRINT_RE.test(value)
}

/**
 * 是否允许提交替代：
 * 必须可比较（reason='ok'）、指纹合法、rows 已加载（数组存在）、已勾选、且不在提交中。
 */
export function canConfirmComparison(
  preview: DocumentComparison | null | undefined,
  acknowledged: boolean,
  submitting: boolean
): boolean {
  return (
    !!preview &&
    preview.comparable === true &&
    preview.reason === 'ok' &&
    hasValidComparisonFingerprint(preview.comparison_fingerprint) &&
    Array.isArray(preview.rows) &&
    acknowledged === true &&
    submitting === false
  )
}

/** 对比过程中是否存在任何行级差异（空 rows 不算差异，也不等于“无变化”）。 */
export function comparisonHasDifferences(
  rows: readonly ComparisonRow[] | null | undefined
): boolean {
  return Array.isArray(rows) && rows.some((row) => row.kind !== 'unchanged')
}

export interface ComparisonSupersedeBody {
  expected_revision: number
  replacement_document_id: string
  comparison_fingerprint: string
}

export type ComparisonSupersedePlan =
  | { status: 'ok'; body: ComparisonSupersedeBody }
  | { status: 'invalidRevision' }
  | { status: 'invalidReplacement' }
  | { status: 'invalidFingerprint' }

/** 严格版本号：NaN / 浮点 / 负数 / 布尔 / 字符串一律拒绝，绝不钳制为 0。 */
function strictRevision(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
}

/**
 * 构造带预览指纹的替代请求体。
 *
 * - revision 必须是来自当前文档快照的非负整数
 * - 替代者必须非空，且不能是文档自身（documentId 由调用方传入；缺省时只做非空校验）
 * - fingerprint 必须是 64 位小写 hex
 * 任一不满足都返回明确的失败状态，绝不静默修正。
 */
export function buildComparisonSupersedeBody(
  expectedRevision: unknown,
  replacementDocumentId: unknown,
  comparisonFingerprint: unknown,
  documentId?: unknown
): ComparisonSupersedePlan {
  const revision = strictRevision(expectedRevision)
  if (revision === null) return { status: 'invalidRevision' }
  const replacement =
    typeof replacementDocumentId === 'string' ? replacementDocumentId.trim() : ''
  const self = typeof documentId === 'string' ? documentId.trim() : ''
  if (!replacement || (self !== '' && replacement === self)) {
    return { status: 'invalidReplacement' }
  }
  if (!hasValidComparisonFingerprint(comparisonFingerprint)) {
    return { status: 'invalidFingerprint' }
  }
  return {
    status: 'ok',
    body: {
      expected_revision: revision,
      replacement_document_id: replacement,
      comparison_fingerprint: comparisonFingerprint
    }
  }
}

/**
 * 用于选择/参数变化时清空旧预览：返回 null。
 * 保留函数形态，让“失效”在组件与测试里是显式动作而不是随手赋 null。
 */
export function invalidateComparison(
  _preview?: DocumentComparison | null
): null {
  return null
}

export interface ComparisonSelection {
  kbId: string
  docId: string
  replacementId: string
}

/**
 * 响应身份必须与当前选择完全一致：original.id = 当前文档，
 * replacement.id = 当前候选。响应本身不含 kb_id，KB 由组件的会话代次守卫覆盖。
 */
export function isComparisonCurrent(
  preview: DocumentComparison | null | undefined,
  selection: ComparisonSelection | null | undefined
): boolean {
  if (!preview || !selection) return false
  if (!selection.kbId || !selection.docId || !selection.replacementId) return false
  return (
    preview.original?.id === selection.docId &&
    preview.replacement?.id === selection.replacementId
  )
}
