/**
 * 检索诊断的纯逻辑：问题规范化、文档范围去重/上限、阶段/分数/状态展示映射。
 * 不依赖 Vue/axios，可直接在 Node 测试中运行。
 */

export const DIAGNOSTICS_MAX_DOCUMENTS = 5
export const DIAGNOSTICS_QUESTION_MAX = 500
/** 可检索文档下拉一次最多加载 100 份；total 更大时必须明确提示并非全部。 */
export const DIAGNOSTICS_DOCUMENT_LIMIT = 100

export interface DiagnosticsRequestBody {
  question: string
  document_ids: string[]
}

export type DiagnosticsRequestPlan =
  | { status: 'ok'; body: DiagnosticsRequestBody }
  | { status: 'questionEmpty' }
  | { status: 'questionTooLong' }
  | { status: 'tooManyDocuments' }

/**
 * 去空、去重并保持原顺序。不在这一层静默截断：超限由请求计划明确拒绝，
 * 避免用户以为全部范围都参与了诊断。
 */
export function uniqueDiagnosticsDocumentIds(
  ids: readonly string[] | null | undefined
): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const raw of ids ?? []) {
    const id = typeof raw === 'string' ? raw.trim() : ''
    if (!id || seen.has(id)) continue
    seen.add(id)
    result.push(id)
  }
  return result
}

/** 展示/表单层的去重 + 上限（最多 5 份）。 */
export function normalizeDiagnosticsDocumentIds(
  ids: readonly string[] | null | undefined
): string[] {
  return uniqueDiagnosticsDocumentIds(ids).slice(0, DIAGNOSTICS_MAX_DOCUMENTS)
}

/** 生成请求体；question 按 trim 后的 1..500 校验，document_ids 去重后最多 5 份。 */
export function buildDiagnosticsRequest(
  question: string,
  documentIds: readonly string[] | null | undefined
): DiagnosticsRequestPlan {
  const trimmed = typeof question === 'string' ? question.trim() : ''
  if (!trimmed) return { status: 'questionEmpty' }
  if (trimmed.length > DIAGNOSTICS_QUESTION_MAX) return { status: 'questionTooLong' }
  const ids = uniqueDiagnosticsDocumentIds(documentIds)
  if (ids.length > DIAGNOSTICS_MAX_DOCUMENTS) return { status: 'tooManyDocuments' }
  return { status: 'ok', body: { question: trimmed, document_ids: ids } }
}

// ---------- 阶段 ----------
const STAGE_NAMES = ['vector', 'keyword', 'fusion', 'rerank', 'select', 'neighbors'] as const
const KNOWN_STAGE_NAMES = new Set<string>(STAGE_NAMES)

/** 阶段 name → i18n key；未知阶段回退到通用「其他阶段」。 */
export function diagnosticsStageNameKey(name: string | null | undefined): string {
  const value = typeof name === 'string' ? name.trim() : ''
  return KNOWN_STAGE_NAMES.has(value)
    ? `knowledge.diagnostics.stages.names.${value}`
    : 'knowledge.diagnostics.stages.names.unknown'
}

const STAGE_STATUSES = ['ok', 'error', 'skipped', 'fallback'] as const
const KNOWN_STAGE_STATUSES = new Set<string>(STAGE_STATUSES)

/** 阶段 status → i18n key；未知状态不伪装成完成。 */
export function diagnosticsStageStatusKey(status: string | null | undefined): string {
  const value = typeof status === 'string' ? status.trim() : ''
  return KNOWN_STAGE_STATUSES.has(value)
    ? `knowledge.diagnostics.stages.statuses.${value}`
    : 'knowledge.diagnostics.stages.statuses.unknown'
}

export type DiagnosticsStageTagType = 'success' | 'danger' | 'info' | 'warning'

/** ok / error / skipped / fallback 使用可区分的颜色。 */
export function diagnosticsStageTagType(
  status: string | null | undefined
): DiagnosticsStageTagType {
  if (status === 'ok') return 'success'
  if (status === 'error') return 'danger'
  if (status === 'fallback') return 'warning'
  return 'info'
}

const STAGE_REASONS = ['disabled', 'not_configured'] as const
const KNOWN_STAGE_REASONS = new Set<string>(STAGE_REASONS)

/** 阶段原因 → i18n key；未知/空原因不猜测含义，统一回退到「其他原因」。 */
export function diagnosticsStageReasonKey(reason: string | null | undefined): string {
  const value = typeof reason === 'string' ? reason.trim() : ''
  return KNOWN_STAGE_REASONS.has(value)
    ? `knowledge.diagnostics.stages.reasons.${value}`
    : 'knowledge.diagnostics.stages.reasons.unknown'
}

// ---------- 候选 ----------
export type DiagnosticsMethodTagType = 'success' | 'primary' | 'warning' | 'info'

export function diagnosticsMethodKey(method: string | null | undefined): string {
  return method === 'vector' || method === 'keyword' || method === 'hybrid'
    ? `knowledge.diagnostics.candidates.methods.${method}`
    : 'knowledge.diagnostics.candidates.methods.unknown'
}

export function diagnosticsMethodTagType(
  method: string | null | undefined
): DiagnosticsMethodTagType {
  if (method === 'vector') return 'primary'
  if (method === 'keyword') return 'warning'
  if (method === 'hybrid') return 'success'
  return 'info'
}

const LIFECYCLE_STATES = ['active', 'scheduled', 'expired', 'disabled', 'superseded'] as const
const KNOWN_LIFECYCLE_STATES = new Set<string>(LIFECYCLE_STATES)

/** 生命周期沿用文档列表的文案；未知值不猜测为 active。 */
export function diagnosticsLifecycleKey(state: string | null | undefined): string {
  const value = typeof state === 'string' ? state.trim() : ''
  return KNOWN_LIFECYCLE_STATES.has(value)
    ? `documents.lifecycle.${value}`
    : 'knowledge.diagnostics.unknown'
}

/** 可检索状态：true/false/未知（null）三态。 */
export function diagnosticsRetrievableKey(value: boolean | null | undefined): string {
  if (value === true) return 'knowledge.diagnostics.yes'
  if (value === false) return 'knowledge.diagnostics.no'
  return 'knowledge.diagnostics.unknown'
}

// ---------- 分数 ----------
const KNOWN_SCORE_KEYS = ['distance', 'fusion_score', 'rerank_score', 'citation_score'] as const
const KNOWN_SCORE_SET = new Set<string>(KNOWN_SCORE_KEYS)

export interface DiagnosticsScoreEntry {
  key: string
  /** 已知键的 i18n key；未知键为空字符串，只展示原始键名与数值。 */
  labelKey: string
  value: number
}

/**
 * 已知分数键按固定顺序映射文案；未知键按原顺序追加，并保持键名 + 数值。
 * 非有限数（NaN/Infinity）不展示，避免把脏数据当成有效分数。
 */
export function diagnosticsScoreEntries(
  scores: Record<string, number> | null | undefined
): DiagnosticsScoreEntry[] {
  if (!scores) return []
  const entries: DiagnosticsScoreEntry[] = []
  for (const key of KNOWN_SCORE_KEYS) {
    const value = scores[key]
    if (typeof value === 'number' && Number.isFinite(value)) {
      entries.push({ key, labelKey: `knowledge.diagnostics.scores.${key}`, value })
    }
  }
  for (const [key, value] of Object.entries(scores)) {
    if (KNOWN_SCORE_SET.has(key)) continue
    if (typeof value !== 'number' || !Number.isFinite(value)) continue
    entries.push({ key, labelKey: '', value })
  }
  return entries
}

/** 分数展示：最多 4 位小数，去掉无意义的尾随 0；非有限数为「—」。 */
export function diagnosticsScoreText(value: number): string {
  if (!Number.isFinite(value)) return '—'
  return String(Number(value.toFixed(4)))
}

export interface DiagnosticsChunkRange {
  start: number
  end: number
}

/** chunk_start/chunk_end 都是非负整数时才展示范围；end 缺失时退化为单块。 */
export function diagnosticsChunkRange(
  start: number | null | undefined,
  end: number | null | undefined
): DiagnosticsChunkRange | null {
  if (typeof start !== 'number' || !Number.isInteger(start) || start < 0) return null
  const last =
    typeof end === 'number' && Number.isInteger(end) && end >= start ? end : start
  return { start, end: last }
}
