import type {
  AnswerFeedbackAdminDetail,
  ExpectedSource,
  FeedbackCategory,
  FeedbackCitation,
  FeedbackOutcome,
  FeedbackReviewRequest,
  FeedbackStatus
} from '../api/index.ts'

/** 分类/状态/结果的 i18n 映射键：界面不硬编码中文。 */
export function feedbackCategoryKey(category: FeedbackCategory): string {
  return `feedback.categories.${category}`
}

export function feedbackStatusKey(status: FeedbackStatus): string {
  return `feedback.status.${status}`
}

export function feedbackOutcomeKey(outcome: FeedbackOutcome): string {
  return `feedback.outcomes.${outcome}`
}

export interface FeedbackSubmissionInput {
  messageId: string
  category: FeedbackCategory
  note?: string
}

/** 提交体只含服务端消息 id、分类和备注；绝不携带问题/回答/引用正文。 */
export interface FeedbackSubmission {
  message_id: string
  category: FeedbackCategory
  note?: string
}

export function buildFeedbackSubmission(input: FeedbackSubmissionInput): FeedbackSubmission {
  const note = typeof input.note === 'string' ? input.note.trim() : ''
  return note
    ? { message_id: input.messageId, category: input.category, note }
    : { message_id: input.messageId, category: input.category }
}

/**
 * 只信任后端生成的消息 id。前端临时 UUID（crypto.randomUUID 的兜底）永远不能
 * 作为反馈目标发给服务端，所以缺 serverId 时一律返回 null。
 */
export function getServerMessageId(message: { serverId?: string | null }): string | null {
  const value = typeof message.serverId === 'string' ? message.serverId.trim() : ''
  return value || null
}

/** 多行文本的条数上限，与后端评测素材字段保持一致。 */
export const MAX_REVIEW_LINES = 20
/** 期望来源单行长度上限（document_id 或文件名）。 */
export const MAX_EXPECTED_SOURCE_LENGTH = 200

/** 去空行、去首尾空白、按首次出现去重，并截断到上限。 */
export function parseLines(raw: string | null | undefined, max: number = MAX_REVIEW_LINES): string[] {
  if (typeof raw !== 'string' || max <= 0) return []
  const seen = new Set<string>()
  const result: string[] = []
  for (const line of raw.split(/\r?\n/)) {
    const value = line.trim()
    if (!value || seen.has(value)) continue
    seen.add(value)
    result.push(value)
    if (result.length >= max) break
  }
  return result
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** UUID 视为 document_id，其余非空短文本视为文件名；空行/超长行丢弃。 */
export function parseExpectedSourceLine(raw: string | null | undefined): ExpectedSource | null {
  if (typeof raw !== 'string') return null
  const value = raw.trim()
  if (!value || value.length > MAX_EXPECTED_SOURCE_LENGTH) return null
  return UUID_PATTERN.test(value) ? { document_id: value } : { filename: value }
}

function sourceKey(source: ExpectedSource): string {
  return [
    source.document_id ?? '',
    source.filename ?? '',
    source.chunk_index ?? '',
    source.index_version ?? ''
  ].join('|')
}

/** 仅转换被勾选的引用；调用方默认传空选择集，绝不预选原引用。 */
export function sourcesFromCitations(
  citations: readonly FeedbackCitation[],
  selectedIndexes: Iterable<number>
): ExpectedSource[] {
  const selected = new Set<number>(selectedIndexes)
  const seen = new Set<string>()
  const result: ExpectedSource[] = []
  for (const index of selected) {
    if (!Number.isInteger(index) || index < 0 || index >= citations.length) continue
    const citation = citations[index]
    const source: ExpectedSource = {}
    if (citation.document_id) source.document_id = citation.document_id
    if (citation.filename) source.filename = citation.filename
    if (citation.chunk_index != null) source.chunk_index = citation.chunk_index
    if (citation.index_version != null) source.index_version = citation.index_version
    if (!source.document_id && !source.filename) continue
    const key = sourceKey(source)
    if (seen.has(key)) continue
    seen.add(key)
    result.push(source)
  }
  return result
}

/** “其他期望来源”每行一个 document_id 或文件名，逐行解析并去重。 */
export function expectedSourcesFromLines(raw: string | null | undefined): ExpectedSource[] {
  const seen = new Set<string>()
  const result: ExpectedSource[] = []
  for (const line of parseLines(raw)) {
    const source = parseExpectedSourceLine(line)
    if (!source) continue
    const key = sourceKey(source)
    if (seen.has(key)) continue
    seen.add(key)
    result.push(source)
  }
  return result
}

export interface FeedbackReviewForm {
  decision: 'verified' | 'rejected'
  correctedOutcome: FeedbackOutcome | null
  selectedCitationIndexes: number[]
  extraSources: string
  requiredPhrases: string
  forbiddenPhrases: string
  unknowns: string
  evidenceNote: string
  reviewerNote: string
}

function collectExpectedSources(
  detail: Pick<AnswerFeedbackAdminDetail, 'citations'>,
  form: FeedbackReviewForm
): ExpectedSource[] {
  const seen = new Set<string>()
  const result: ExpectedSource[] = []
  const candidates = [
    ...sourcesFromCitations(detail.citations, form.selectedCitationIndexes),
    ...expectedSourcesFromLines(form.extraSources)
  ]
  for (const source of candidates) {
    const key = sourceKey(source)
    if (seen.has(key)) continue
    seen.add(key)
    result.push(source)
  }
  return result
}

/**
 * 返回阻塞核实的 i18n key，null 表示可以提交。
 * 来源校验使用后端给出的真实会话模式：RAG 回答必须指定期望来源，
 * 普通对话回答不允许指定检索来源（与原引用数量无关）。
 */
export function canVerify(
  detail: Pick<AnswerFeedbackAdminDetail, 'mode' | 'result_stale' | 'citations'>,
  form: FeedbackReviewForm
): string | null {
  if (detail.result_stale) return 'feedback.admin.staleBlocking'
  if (form.decision === 'rejected') {
    return form.reviewerNote.trim() ? null : 'feedback.admin.noteRequired'
  }
  if (parseLines(form.unknowns).length > 0) return 'feedback.admin.unresolvedBlocking'
  if (!form.correctedOutcome) return 'feedback.admin.outcomeRequired'
  const sources = collectExpectedSources(detail, form)
  if (detail.mode === 'rag' && form.correctedOutcome === 'answer' && sources.length === 0) {
    return 'feedback.admin.sourceRequired'
  }
  if (detail.mode === 'chat' && sources.length > 0) return 'feedback.admin.chatNoSources'
  if (!form.evidenceNote.trim()) return 'feedback.admin.evidenceRequired'
  return null
}

/** 驳回只保留备注；核实保留修正结果、来源与评测素材。 */
export function buildReviewPayload(
  detail: Pick<AnswerFeedbackAdminDetail, 'revision' | 'citations'>,
  form: FeedbackReviewForm
): FeedbackReviewRequest {
  const reviewerNote = form.reviewerNote.trim()
  if (form.decision === 'rejected') {
    return {
      expected_revision: detail.revision,
      decision: 'rejected',
      corrected_outcome: null,
      expected_sources: [],
      required_phrases: [],
      forbidden_phrases: [],
      unknowns: [],
      evidence_note: '',
      reviewer_note: reviewerNote
    }
  }
  return {
    expected_revision: detail.revision,
    decision: 'verified',
    corrected_outcome: form.correctedOutcome,
    expected_sources: collectExpectedSources(detail, form),
    required_phrases: parseLines(form.requiredPhrases),
    forbidden_phrases: parseLines(form.forbiddenPhrases),
    unknowns: parseLines(form.unknowns),
    evidence_note: form.evidenceNote.trim(),
    reviewer_note: reviewerNote
  }
}

export function isExportableStatus(status: FeedbackStatus): boolean {
  return status === 'verified'
}

/**
 * 用户显式点击后把 JSONL 存成本地文件；不自动上传/保存到服务器。
 * 返回是否真的触发了下载（无 DOM 的测试/SSR 环境返回 false）。
 */
export function downloadJsonl(filename: string, jsonl: string): boolean {
  if (typeof document === 'undefined' || typeof URL === 'undefined'
    || typeof URL.createObjectURL !== 'function' || typeof Blob === 'undefined') return false
  const blob = new Blob([jsonl], { type: 'application/x-ndjson;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename || 'feedback.jsonl'
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
  return true
}
