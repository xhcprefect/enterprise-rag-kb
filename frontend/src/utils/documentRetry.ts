/** 入库任务级失败次数与展示状态；不使用模型 HTTP 重试次数。 */
export type RetryPhase = 'none' | 'waiting' | 'retrying' | 'permanent' | 'exhausted' | 'failed'

export interface RetryState {
  phase: RetryPhase
  attempts: number
  /** null 表示没有可信的退避时刻；0 表示已到期，仍待 worker 领取。 */
  waitSeconds: number | null
  code: IngestErrorCode | null
}

const KNOWN_CODES = [
  'source_unavailable',
  'timeout',
  'network',
  'rate_limited',
  'storage',
  'database',
  'unsupported_format',
  'empty_content',
  'corrupt_file',
  'file_missing',
  'unknown',
  'authentication'
] as const

export type IngestErrorCode = typeof KNOWN_CODES[number]

const PERMANENT_CODES = new Set<IngestErrorCode>([
  'unsupported_format', 'empty_content', 'corrupt_file', 'file_missing', 'authentication'
])
const IDLE: RetryState = { phase: 'none', attempts: 0, waitSeconds: null, code: null }

export function ingestFailureMessage(errorMessage: unknown, retryAdvice: string, fallback: string): string {
  const message = typeof errorMessage === 'string' ? errorMessage : ''
  if (message && message !== '文档处理失败，请联系管理员检查日志') return message
  return retryAdvice || message || fallback
}

function parseTimestamp(value: unknown): number | null {
  if (typeof value !== 'string' ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function retryState(doc: unknown, nowMs: number = Date.now()): RetryState {
  if (!doc || typeof doc !== 'object') return { ...IDLE }
  const record = doc as Record<string, unknown>
  const attempts = typeof record.ingest_attempts === 'number' &&
    Number.isSafeInteger(record.ingest_attempts) && record.ingest_attempts >= 0
    ? record.ingest_attempts : 0
  const code = KNOWN_CODES.find(value => value === record.last_error_code) ?? null

  if (record.cancel_requested === true) return { ...IDLE, attempts }
  if (record.status === 'failed') {
    // 冻结契约：已知可恢复错误进入 failed 表示预算耗尽；未知/旧响应不作此推断。
    const phase = code && PERMANENT_CODES.has(code) ? 'permanent'
      : code && attempts > 0 ? 'exhausted' : 'failed'
    return { phase, attempts, waitSeconds: null, code }
  }
  if (!attempts || (record.status !== 'pending' && record.status !== 'processing')) {
    return { ...IDLE, attempts }
  }
  if (record.status === 'processing') {
    return { phase: 'retrying', attempts, waitSeconds: null, code }
  }
  const due = parseTimestamp(record.next_attempt_at)
  const waitSeconds = due === null || !Number.isFinite(nowMs)
    ? null : Math.max(0, Math.ceil((due - nowMs) / 1000))
  return { phase: 'waiting', attempts, waitSeconds, code }
}
