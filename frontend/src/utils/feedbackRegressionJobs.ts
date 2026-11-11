import type {
  FeedbackRegressionBatchCreateRequest,
  FeedbackRegressionBatchItem,
  FeedbackRegressionJob,
  FeedbackRegressionJobCreateRequest,
  FeedbackRegressionJobReviewRequest,
  FeedbackRegressionJobStatus,
  FeedbackRegressionJobUsage,
  FeedbackRegressionReviewStatus,
  FeedbackRegressionCancelRequest
} from '../api/feedbackRegressionJobs.ts'
import { newPrepareRequestId } from './feedbackRegression.ts'

/** 批量上限与人工核验说明上限：与冻结后端 DTO 一致。 */
export const MAX_BATCH_ITEMS = 20
export const MAX_REVIEW_NOTES = 1000

export const JOB_STATUSES: readonly FeedbackRegressionJobStatus[] = [
  'queued',
  'running',
  'succeeded',
  'failed',
  'cancelled'
]
export const ACTIVE_JOB_STATUSES: readonly FeedbackRegressionJobStatus[] = ['queued', 'running']
export const REVIEW_STATUSES: readonly FeedbackRegressionReviewStatus[] = [
  'unknown',
  'verified',
  'rejected'
]

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
// 与后端 notes 校验一致：除 \n、\t 以外的 C0/C1 控制字符都不允许。
const CONTROL_CHARACTER_PATTERN = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F]/

export function isUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID_PATTERN.test(value)
}

export function isJobStatus(value: unknown): value is FeedbackRegressionJobStatus {
  return typeof value === 'string' && (JOB_STATUSES as readonly string[]).includes(value)
}

export function isReviewStatus(value: unknown): value is FeedbackRegressionReviewStatus {
  return typeof value === 'string' && (REVIEW_STATUSES as readonly string[]).includes(value)
}

export function isActiveJobStatus(status: FeedbackRegressionJobStatus): boolean {
  return status === 'queued' || status === 'running'
}

export function isTerminalJobStatus(status: FeedbackRegressionJobStatus): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'cancelled'
}

export function isCancelableJob(job: Pick<FeedbackRegressionJob, 'status'>): boolean {
  return isActiveJobStatus(job.status)
}

/** 只有成功且结果指纹已经落盘的作业才允许比较/人工核验。 */
export function isReviewableJob(
  job: Pick<FeedbackRegressionJob, 'status' | 'result'>
): boolean {
  return job.status === 'succeeded' && !!job.result?.result_sha256
}

export type JobOutcomeTone =
  | 'queued'
  | 'running'
  | 'succeededPassed'
  | 'succeededAssertionsFailed'
  | 'succeededUnknown'
  | 'executionFailed'
  | 'cancelled'

/**
 * 明确区分“执行成功但机器断言未通过”和“执行本身失败”：
 * succeeded+passed=false 仍可比较与人工核验，failed 只能查看错误码。
 */
export function jobOutcomeTone(
  job: Pick<FeedbackRegressionJob, 'status' | 'result'>
): JobOutcomeTone {
  if (job.status === 'queued') return 'queued'
  if (job.status === 'running') return 'running'
  if (job.status === 'failed') return 'executionFailed'
  if (job.status === 'cancelled') return 'cancelled'
  if (job.result?.passed === true) return 'succeededPassed'
  if (job.result?.passed === false) return 'succeededAssertionsFailed'
  return 'succeededUnknown'
}

export function jobStatusKey(status: FeedbackRegressionJobStatus): string {
  return `status.${status}`
}

export function reviewStatusKey(status: FeedbackRegressionReviewStatus): string {
  return `reviewStatus.${status}`
}

export function jobOutcomeKey(job: Pick<FeedbackRegressionJob, 'status' | 'result'>): string {
  return `outcome.${jobOutcomeTone(job)}`
}

// ---------------------------------------------------------------------------
// 用量展示：未知保持未知，绝不补 0
// ---------------------------------------------------------------------------

export interface RegressionUsageSummary {
  attempts: number
  hasCalls: boolean
  tokensMeasured: boolean
  inputTokens: number | null
  outputTokens: number | null
  totalTokens: number | null
  knownInputTokens: number | null
  knownOutputTokens: number | null
  knownAttempts: number
  missingAttempts: number
  pricedAttempts: number
  unpricedAttempts: number
  unfinishedAttempts: number
  currency: string | null
  currencyMismatch: boolean
  costComplete: boolean
  totalCost: string | null
  knownCostSubtotal: string | null
}

/**
 * 直接映射 JobRead.usage 的已知/未知语义，不做任何“0 补全”：
 * - 任一次缺 token，总量三者均为 null，但 known_input/output 小计仍保留；
 * - 费用仅在全部调用已定价、已结束、单一币种时完整；
 * - 多币种或缺币种时没有可简单相加的总额。
 */
export function summarizeUsage(usage: FeedbackRegressionJobUsage): RegressionUsageSummary {
  const attempts = Number.isFinite(usage.attempts) && usage.attempts > 0 ? usage.attempts : 0
  return {
    attempts,
    hasCalls: attempts > 0,
    tokensMeasured: attempts > 0
      && usage.usage_missing_attempts === 0
      && usage.input_tokens !== null
      && usage.output_tokens !== null
      && usage.total_tokens !== null,
    inputTokens: usage.input_tokens,
    outputTokens: usage.output_tokens,
    totalTokens: usage.total_tokens,
    knownInputTokens: usage.known_input_tokens,
    knownOutputTokens: usage.known_output_tokens,
    knownAttempts: usage.usage_known_attempts,
    missingAttempts: usage.usage_missing_attempts,
    pricedAttempts: usage.priced_attempts,
    unpricedAttempts: usage.unpriced_attempts,
    unfinishedAttempts: usage.unfinished_attempts,
    currency: usage.currency,
    currencyMismatch: usage.currency_mismatch,
    costComplete: usage.total_cost !== null,
    totalCost: usage.total_cost,
    knownCostSubtotal: usage.known_cost_subtotal
  }
}

/** 只做字符串小数整理，不经过浮点数，避免费用精度被改写。 */
export function formatCostAmount(value: string | null | undefined): string {
  if (typeof value !== 'string' || !/^-?\d+(\.\d+)?$/.test(value)) return ''
  const negative = value.startsWith('-')
  const unsigned = negative ? value.slice(1) : value
  const [whole, fraction = ''] = unsigned.split('.')
  const trimmed = fraction.replace(/0+$/, '')
  const normalizedWhole = whole.replace(/^0+(?=\d)/, '')
  const body = trimmed ? `${normalizedWhole}.${trimmed}` : normalizedWhole
  return negative && body !== '0' ? `-${body}` : body
}

// ---------------------------------------------------------------------------
// 请求构造
// ---------------------------------------------------------------------------

export interface BuildResult<T> {
  payload: T | null
  errorKey: string | null
}

function isRevision(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1
}

export function buildJobCreatePayload(
  feedbackId: unknown,
  regressionRevision: unknown,
  requestId: string = newPrepareRequestId()
): BuildResult<FeedbackRegressionJobCreateRequest> {
  if (!isUuid(feedbackId)) return { payload: null, errorKey: 'errors.invalidFeedback' }
  if (!isRevision(regressionRevision)) return { payload: null, errorKey: 'errors.invalidRevision' }
  if (!isUuid(requestId)) return { payload: null, errorKey: 'errors.invalidRequestId' }
  return {
    payload: {
      feedback_id: feedbackId,
      expected_revision: regressionRevision,
      request_id: requestId
    },
    errorKey: null
  }
}

export interface BatchSelectionItem {
  feedback_id: string
  expected_revision: number
}

export function buildBatchCreatePayload(
  items: readonly BatchSelectionItem[],
  requestId: string = newPrepareRequestId()
): BuildResult<FeedbackRegressionBatchCreateRequest> {
  if (!isUuid(requestId)) return { payload: null, errorKey: 'errors.invalidRequestId' }
  if (!Array.isArray(items) || items.length < 1) {
    return { payload: null, errorKey: 'errors.batchEmpty' }
  }
  if (items.length > MAX_BATCH_ITEMS) {
    return { payload: null, errorKey: 'errors.batchTooLarge' }
  }
  const seen = new Set<string>()
  const normalized: FeedbackRegressionBatchItem[] = []
  for (const item of items) {
    if (!item || !isUuid(item.feedback_id) || !isRevision(item.expected_revision)) {
      return { payload: null, errorKey: 'errors.batchInvalidItem' }
    }
    if (seen.has(item.feedback_id)) {
      return { payload: null, errorKey: 'errors.batchDuplicate' }
    }
    seen.add(item.feedback_id)
    normalized.push({
      feedback_id: item.feedback_id,
      expected_revision: item.expected_revision
    })
  }
  return { payload: { request_id: requestId, items: normalized }, errorKey: null }
}

export function buildCancelRequest(revision: unknown): BuildResult<FeedbackRegressionCancelRequest> {
  if (!isRevision(revision)) return { payload: null, errorKey: 'errors.invalidRevision' }
  return { payload: { expected_revision: revision }, errorKey: null }
}

export function reviewNotesTooLong(notes: string): boolean {
  return Array.from(notes).length > MAX_REVIEW_NOTES
}

export function reviewNotesHaveControlChars(notes: string): boolean {
  return CONTROL_CHARACTER_PATTERN.test(notes)
}

/**
 * 人工核验必须显式绑定当前 job revision、run_id 与 result_sha256；
 * 新作业的 review 初值永远是 unknown，绝不沿用上一轮核验。
 */
export function buildReviewPayload(
  job: Pick<FeedbackRegressionJob, 'revision' | 'run_id' | 'result'>,
  status: unknown,
  notes: string
): BuildResult<FeedbackRegressionJobReviewRequest> {
  if (!isReviewStatus(status)) return { payload: null, errorKey: 'errors.invalidReviewStatus' }
  if (!isRevision(job.revision)) return { payload: null, errorKey: 'errors.invalidRevision' }
  if (!isUuid(job.run_id)) return { payload: null, errorKey: 'errors.invalidRun' }
  const resultSha = job.result?.result_sha256
  if (typeof resultSha !== 'string' || !/^[0-9a-f]{64}$/.test(resultSha)) {
    return { payload: null, errorKey: 'errors.resultUnavailable' }
  }
  const text = typeof notes === 'string' ? notes.trim() : ''
  if (reviewNotesTooLong(text)) return { payload: null, errorKey: 'errors.notesTooLong' }
  if (reviewNotesHaveControlChars(text)) {
    return { payload: null, errorKey: 'errors.notesControlChars' }
  }
  return {
    payload: {
      expected_revision: job.revision,
      run_id: job.run_id,
      result_sha256: resultSha,
      status,
      notes: text
    },
    errorKey: null
  }
}

/**
 * 网络中断、超时或无响应/5xx 属于“写入结果未知”，只允许只读找回；
 * 明确的 4xx 与用户主动取消不是未知写入。
 */
export function isAmbiguousWriteFailure(error: any): boolean {
  if (!error || typeof error !== 'object') return false
  if (error.code === 'ERR_CANCELED') return false
  const status = error.response?.status
  if (typeof status === 'number') return status >= 500
  return error.code === 'ERR_NETWORK' || error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT'
}

/** 后端错误正文形如“说明（revision_conflict）”；只提取稳定 reason 码。 */
export function extractReasonCode(message: string | null | undefined): string {
  if (typeof message !== 'string') return ''
  const match = message.match(/[（(]([a-z][a-z0-9_]*)[）)]\s*$/)
  return match ? match[1] : ''
}

// ---------------------------------------------------------------------------
// 未知写入恢复：只持久化原 request UUID 等有界元数据，绝不保存问答正文
// ---------------------------------------------------------------------------

export type PendingRegressionRequestKind = 'job' | 'batch'

export interface PendingRegressionRequest {
  request_id: string
  kind: PendingRegressionRequestKind
  feedback_ids: string[]
  created_at: number
}

export interface RecoveryStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export const PENDING_STORAGE_PREFIX = 'rag_regression_pending:'
export const PENDING_TTL_MS = 24 * 60 * 60 * 1000
export const MAX_PENDING_REQUESTS = 20

export function pendingStorageKey(origin: string, adminId: string): string {
  const scopeOrigin = typeof origin === 'string' && origin ? origin : 'unknown-origin'
  const scopeAdmin = typeof adminId === 'string' && adminId ? adminId : 'unknown-admin'
  return `${PENDING_STORAGE_PREFIX}${encodeURIComponent(scopeOrigin)}:${encodeURIComponent(scopeAdmin)}`
}

export function browserRecoveryStorage(): RecoveryStorage | null {
  try {
    if (typeof sessionStorage === 'undefined') return null
    return sessionStorage
  } catch {
    return null
  }
}

export function currentOrigin(): string {
  try {
    return typeof location === 'undefined' ? '' : location.origin
  } catch {
    return ''
  }
}

function sanitizePending(value: unknown): PendingRegressionRequest | null {
  if (!value || typeof value !== 'object') return null
  const candidate = value as Record<string, unknown>
  if (!isUuid(candidate.request_id)) return null
  if (candidate.kind !== 'job' && candidate.kind !== 'batch') return null
  const rawIds = Array.isArray(candidate.feedback_ids) ? candidate.feedback_ids : []
  const feedbackIds: string[] = []
  for (const identifier of rawIds) {
    if (!isUuid(identifier) || feedbackIds.includes(identifier)) continue
    feedbackIds.push(identifier)
    if (feedbackIds.length >= MAX_BATCH_ITEMS) break
  }
  if (!feedbackIds.length) return null
  const createdAt = typeof candidate.created_at === 'number' && Number.isFinite(candidate.created_at)
    ? candidate.created_at
    : 0
  return {
    request_id: candidate.request_id,
    kind: candidate.kind,
    feedback_ids: feedbackIds,
    created_at: createdAt
  }
}

function readPending(
  storage: RecoveryStorage | null,
  origin: string,
  adminId: string,
  now: number
): PendingRegressionRequest[] {
  if (!storage) return []
  let raw: string | null = null
  try {
    raw = storage.getItem(pendingStorageKey(origin, adminId))
  } catch {
    return []
  }
  if (!raw) return []
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return []
  }
  if (!Array.isArray(parsed)) return []
  const seen = new Set<string>()
  const result: PendingRegressionRequest[] = []
  for (const entry of parsed) {
    const sanitized = sanitizePending(entry)
    if (!sanitized) continue
    if (sanitized.created_at > 0 && now - sanitized.created_at > PENDING_TTL_MS) continue
    if (seen.has(sanitized.request_id)) continue
    seen.add(sanitized.request_id)
    result.push(sanitized)
  }
  result.sort((left, right) => right.created_at - left.created_at)
  return result.slice(0, MAX_PENDING_REQUESTS)
}

function writePending(
  storage: RecoveryStorage | null,
  origin: string,
  adminId: string,
  entries: PendingRegressionRequest[],
  now: number
): void {
  if (!storage) return
  const seen = new Set<string>()
  const bounded: PendingRegressionRequest[] = []
  for (const entry of [...entries].sort((left, right) => right.created_at - left.created_at)) {
    const sanitized = sanitizePending(entry)
    if (!sanitized) continue
    if (sanitized.created_at > 0 && now - sanitized.created_at > PENDING_TTL_MS) continue
    if (seen.has(sanitized.request_id)) continue
    seen.add(sanitized.request_id)
    bounded.push(sanitized)
    if (bounded.length >= MAX_PENDING_REQUESTS) break
  }
  try {
    if (!bounded.length) {
      storage.removeItem(pendingStorageKey(origin, adminId))
    } else {
      storage.setItem(pendingStorageKey(origin, adminId), JSON.stringify(bounded))
    }
  } catch {
    /* 存储被禁用时只是失去跨刷新恢复，不影响本次只读找回。 */
  }
}

export function loadPendingRequests(
  storage: RecoveryStorage | null,
  origin: string,
  adminId: string,
  now: number = Date.now()
): PendingRegressionRequest[] {
  return readPending(storage, origin, adminId, now)
}

export function savePendingRequest(
  storage: RecoveryStorage | null,
  origin: string,
  adminId: string,
  entry: PendingRegressionRequest,
  now: number = Date.now()
): void {
  const sanitized = sanitizePending(entry)
  if (!sanitized) return
  const current = readPending(storage, origin, adminId, now)
    .filter(item => item.request_id !== sanitized.request_id)
  writePending(storage, origin, adminId, [sanitized, ...current], now)
}

export function removePendingRequest(
  storage: RecoveryStorage | null,
  origin: string,
  adminId: string,
  requestId: string,
  now: number = Date.now()
): void {
  if (!isUuid(requestId)) return
  const current = readPending(storage, origin, adminId, now)
  writePending(
    storage,
    origin,
    adminId,
    current.filter(item => item.request_id !== requestId),
    now
  )
}
