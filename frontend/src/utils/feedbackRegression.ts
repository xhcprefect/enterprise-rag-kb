import type {
  FeedbackRegressionCause,
  FeedbackRegressionEnrollRequest,
  FeedbackRegressionMode,
  FeedbackRegressionPrepareRequest,
  FeedbackRegressionRun,
  FeedbackRegressionRunState,
  FeedbackRegressionValidationLevel
} from '../api/index.ts'
import type {
  FeedbackRegressionExecutionKind,
  FeedbackRegressionJobStatus
} from '../api/feedbackRegressionJobs.ts'

/** 后端冻结的原因枚举；顺序与下拉展示一致。 */
export const REGRESSION_CAUSES: readonly FeedbackRegressionCause[] = [
  'parsing',
  'retrieval',
  'document_version',
  'generation',
  'scope_context',
  'other'
]

/** 回归执行模式：offline 是本地回放，live 才是声明真实调用。 */
export const REGRESSION_MODES: readonly FeedbackRegressionMode[] = ['offline', 'live']

export const REGRESSION_RUN_STATES: readonly FeedbackRegressionRunState[] = [
  'awaiting_report',
  'imported',
  'stale',
  'online'
]

export const REGRESSION_VALIDATION_LEVELS: readonly FeedbackRegressionValidationLevel[] = [
  'consistency_only',
  'content_regraded'
]

/**
 * GET 回归状态可能返回的阻断码。前十个来自回归适配层；sample_changed 表示
 * 纳入后样例/绑定发生变化（需要重新纳入或重新准备）。未知码走通用回退。
 */
export const REGRESSION_UNAVAILABLE_REASONS: readonly string[] = [
  'feedback_not_verified',
  'feedback_stale',
  'project_unsupported',
  'scope_unsupported',
  'turns_overflow',
  'annotation_incomplete',
  'source_ambiguous',
  'source_invalid',
  'chunk_missing',
  'version_mismatch',
  'sample_changed'
]

/**
 * R1a 新增的固定范围/项目快照阻断码只在懒加载反馈页面使用；对应文案在
 * feedbackRegressionJobs 懒加载 catalog 中，不进入全局入口。
 */
export const REGRESSION_ONLINE_UNAVAILABLE_REASONS: readonly string[] = [
  'manifest_overflow',
  'scope_invalid',
  'project_snapshot_invalid',
  'project_snapshot_changed'
]

/** 原因说明上限：与后端 FeedbackRegressionCreateRequest 保持一致。 */
export const MAX_REGRESSION_NOTE_LENGTH = 1000
/** 报告上限 2 MiB：本地先挡一次，服务端仍会做权威校验。 */
export const MAX_REPORT_BYTES = 2 * 1024 * 1024

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
// Python 的 unicodedata Cc 分类：除 \n、\t 以外都不允许（\r 也在禁止之列）。
const CONTROL_CHARACTER_PATTERN = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F]/

export function isRegressionCause(value: unknown): value is FeedbackRegressionCause {
  return typeof value === 'string' && (REGRESSION_CAUSES as readonly string[]).includes(value)
}

export function isRegressionMode(value: unknown): value is FeedbackRegressionMode {
  return typeof value === 'string' && (REGRESSION_MODES as readonly string[]).includes(value)
}

export function isRegressionRunState(value: unknown): value is FeedbackRegressionRunState {
  return typeof value === 'string' && (REGRESSION_RUN_STATES as readonly string[]).includes(value)
}

export function isValidationLevel(value: unknown): value is FeedbackRegressionValidationLevel {
  return typeof value === 'string' && (REGRESSION_VALIDATION_LEVELS as readonly string[]).includes(value)
}

export function isKnownUnavailableReason(value: string): boolean {
  return REGRESSION_UNAVAILABLE_REASONS.includes(value)
}

/** 已知阻断码映射到专属文案；未知码返回通用回退 key（调用方附带原始 reason）。 */
export function unavailableReasonKey(reason: string | null | undefined): string {
  if (reason && REGRESSION_ONLINE_UNAVAILABLE_REASONS.includes(reason)) {
    return `feedbackJobs.unavailable.${reason}`
  }
  return reason && isKnownUnavailableReason(reason)
    ? `feedback.regression.unavailable.${reason}`
    : 'feedback.regression.unavailable.unknown'
}

export function regressionCauseKey(cause: FeedbackRegressionCause): string {
  return `feedback.regression.causes.${cause}`
}

export function regressionModeKey(mode: FeedbackRegressionMode): string {
  return `feedback.regression.modes.${mode}`
}

/** online 轮次没有人工导入语义；其余状态沿用原手工轮次命名空间。 */
export function runStateKey(state: FeedbackRegressionRunState): string {
  return state === 'online' ? 'feedbackJobs.runState.online' : `feedback.regression.state.${state}`
}

/** 轮次执行种类；manual 标签放在懒加载 catalog，避免入口增长。 */
export function executionKindKey(kind: FeedbackRegressionExecutionKind): string {
  return `feedbackJobs.executionKind.${kind}`
}

/** 未导入或历史数据缺失校验级别时返回空串，由界面显示占位符。 */
export function validationLevelKey(level: FeedbackRegressionValidationLevel | null | undefined): string {
  return level && isValidationLevel(level) ? `feedback.regression.validation.${level}` : ''
}

/**
 * 失败码是评测脚本产出的稳定标识（如 expected_source_mismatch）。
 * 前端原样展示，绝不翻译成业务断言，避免把机器码误解成人工结论。
 */
export function failureLabel(code: string): string {
  return code
}

/**
 * 后端显式给出 can_import 后以它为准：在线轮次即使数据形状类似也永远不能导入。
 * 旧响应缺该字段时保持原判断，兼容既有手工闭环。
 */
export function canImport(
  run: (Pick<FeedbackRegressionRun, 'is_current' | 'state'> & { can_import?: boolean }) | null | undefined
): boolean {
  if (!run) return false
  if (run.can_import === false) return false
  return !!run.is_current && run.state === 'awaiting_report'
}

/** imported/stale 都已有最终摘要，不能再导入或改变该轮状态。 */
export function isTerminalRun(
  run: Pick<FeedbackRegressionRun, 'state'> | null | undefined
): boolean {
  return !!run && run.state !== 'awaiting_report'
}

/**
 * 轮次结论的展示 key：
 * - 在线执行按真实 job_status 区分排队/运行/成功+断言/执行失败/取消；
 * - consistency_only 通过：只能称“报告声明通过（未重算断言）”；
 * - content_regraded 通过：称“断言复算通过（待人工复核）”；
 * - 其余 imported 记录（缺少校验级别等）按离线/一般导入说明，绝不推断为通过。
 */
export function describeRunOutcome(
  run: Pick<FeedbackRegressionRun, 'state' | 'validation_level' | 'passed' | 'requested_mode'> & {
    execution_kind?: FeedbackRegressionExecutionKind
    state?: FeedbackRegressionRunState
    job_status?: FeedbackRegressionJobStatus | null
  }
): string {
  if (run.execution_kind === 'online' || run.state === 'online') {
    if (run.job_status === 'queued') return 'feedbackJobs.runOutcome.onlineQueued'
    if (run.job_status === 'running') return 'feedbackJobs.runOutcome.onlineRunning'
    if (run.job_status === 'failed') return 'feedbackJobs.runOutcome.onlineFailed'
    if (run.job_status === 'cancelled') return 'feedbackJobs.runOutcome.onlineCancelled'
    if (run.job_status === 'succeeded') {
      if (run.passed === true) return 'feedbackJobs.runOutcome.onlineSucceededPassed'
      if (run.passed === false) return 'feedbackJobs.runOutcome.onlineSucceededFailed'
    }
    return 'feedbackJobs.runOutcome.onlineUnknown'
  }
  if (run.state === 'stale') return 'feedback.regression.outcome.stale'
  if (run.state === 'awaiting_report') return 'feedback.regression.outcome.awaitingReport'
  if (run.passed === true) {
    if (run.validation_level === 'content_regraded') {
      return 'feedback.regression.outcome.contentRegraded'
    }
    if (run.validation_level === 'consistency_only') {
      return 'feedback.regression.outcome.consistencyOnly'
    }
  }
  if (run.passed === false) return 'feedback.regression.outcome.failed'
  if (run.requested_mode === 'offline') return 'feedback.regression.outcome.offlineReplay'
  return 'feedback.regression.outcome.imported'
}

export interface RegressionEnrollResult {
  payload: FeedbackRegressionEnrollRequest | null
  errorKey: string | null
}

function noteLength(value: string): number {
  // 后端按 Unicode 码点计数；Array.from 比 UTF-16 .length 更接近。
  return Array.from(value).length
}

/**
 * 构造纳入请求：说明 trim 后 1–1000 码点，禁止控制字符（\n、\t 除外）；
 * cause 仍由独立类型守卫校验，避免把下拉的空值发给服务端。
 */
export function buildEnrollPayload(
  detailRevision: number,
  cause: unknown,
  note: string | null | undefined
): RegressionEnrollResult {
  if (!Number.isInteger(detailRevision) || detailRevision < 1) {
    return { payload: null, errorKey: 'feedback.regression.errors.revisionInvalid' }
  }
  if (!isRegressionCause(cause)) {
    return { payload: null, errorKey: 'feedback.regression.errors.causeRequired' }
  }
  const text = typeof note === 'string' ? note.trim() : ''
  if (!text) return { payload: null, errorKey: 'feedback.regression.errors.noteRequired' }
  if (noteLength(text) > MAX_REGRESSION_NOTE_LENGTH) {
    return { payload: null, errorKey: 'feedback.regression.errors.noteTooLong' }
  }
  if (CONTROL_CHARACTER_PATTERN.test(text)) {
    return { payload: null, errorKey: 'feedback.regression.errors.noteControlChars' }
  }
  return {
    payload: {
      expected_feedback_revision: detailRevision,
      cause,
      cause_note: text
    },
    errorKey: null
  }
}

export interface RegressionPrepareResult {
  payload: FeedbackRegressionPrepareRequest | null
  errorKey: string | null
}

function fallbackUuid(): string {
  const bytes = new Uint8Array(16)
  const cryptoApi = (globalThis as { crypto?: CryptoLike }).crypto
  if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    cryptoApi.getRandomValues(bytes)
  } else {
    // 极端运行环境（无 Web Crypto）的兜底；该值只是幂等键，不是安全令牌。
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256)
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

interface CryptoLike {
  randomUUID?: () => string
  getRandomValues?: (array: Uint8Array) => Uint8Array
}

/** 优先 crypto.randomUUID；否则用 getRandomValues 生成 RFC 4122 v4。 */
export function newPrepareRequestId(): string {
  const cryptoApi = (globalThis as { crypto?: CryptoLike }).crypto
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID()
  }
  return fallbackUuid()
}

/** 构造准备请求；requestId 可复用（网络超时重试同一准备请求，服务端按幂等键返回同一轮）。 */
export function buildPreparePayload(
  revision: number,
  mode: unknown,
  requestId: string = newPrepareRequestId()
): RegressionPrepareResult {
  if (!Number.isInteger(revision) || revision < 1) {
    return { payload: null, errorKey: 'feedback.regression.errors.revisionInvalid' }
  }
  if (!isRegressionMode(mode)) {
    return { payload: null, errorKey: 'feedback.regression.errors.modeRequired' }
  }
  if (!UUID_PATTERN.test(requestId)) {
    return { payload: null, errorKey: 'feedback.regression.errors.requestInvalid' }
  }
  return {
    payload: {
      expected_revision: revision,
      requested_mode: mode,
      prepare_request_id: requestId
    },
    errorKey: null
  }
}

export interface ReportFileLike {
  name: string
  size: number
  type?: string
}

/**
 * 本地报告文件校验：只接受 .json 扩展名且不超过 2 MiB。
 * 返回 i18n 错误 key；null 表示可以上传（服务端仍做严格内容校验）。
 */
export function validateReportFile(file: ReportFileLike | null | undefined): string | null {
  if (!file || typeof file.name !== 'string') {
    return 'feedback.regression.errors.fileInvalid'
  }
  const name = file.name.trim()
  if (!name) return 'feedback.regression.errors.fileInvalid'
  if (!name.toLowerCase().endsWith('.json')) {
    return 'feedback.regression.errors.fileExtension'
  }
  if (!Number.isFinite(file.size) || file.size < 0) {
    return 'feedback.regression.errors.fileInvalid'
  }
  if (file.size > MAX_REPORT_BYTES) {
    return 'feedback.regression.errors.fileTooLarge'
  }
  return null
}
