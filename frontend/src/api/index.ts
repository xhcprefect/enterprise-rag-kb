import client from './client'
import { consumeEventStream, createStreamGuard, StreamProtocolError, type StreamLimits, type StreamPhase } from '@/utils/stream'
import type { ChatRequestState } from '@/utils/requestRecovery'
import { i18n } from '@/i18n'
import type { ChatCompletion } from '@/utils/citations'
import { createChatRequestId } from '@/utils/chatRequest'
import { buildScopePatchBody, type DocumentScopeItem } from '@/utils/documentScope'
import type { MemoryState } from '@/utils/memoryStatus'

export type { DocumentScopeItem } from '@/utils/documentScope'
export type { MemoryState, MemoryDisplay } from '@/utils/memoryStatus'

// ---------- 类型 ----------
export interface User {
  id: string
  username: string
  display_name: string
  email: string | null
  role: 'admin' | 'user'
  is_active: boolean
  created_at: string
}

export interface KnowledgeBase {
  id: string
  name: string
  description: string
  collection_name: string
  is_public: boolean
  document_count: number
  created_at: string
  /** 资源类型：普通上传库为 upload；项目索引专用私有库为 project。旧响应可能缺失。 */
  kind?: 'upload' | 'project'
  /** kind='project' 时的项目源/快照只读状态；普通库为 null/false。 */
  project_source_id?: string | null
  project_active_snapshot_id?: string | null
  project_ready?: boolean
  project_revoked?: boolean
  /** 服务端计算的成员授权版本；旧响应可能缺失，缺失时不能当作已知版本。 */
  membership_revision?: number
  /** 服务端判定当前用户能否维护该库文档；缺失或非 true 一律按 false 处理。 */
  can_manage_documents?: boolean
  /** 服务端判定当前用户能否管理该库成员；缺失或非 true 一律按 false 处理。 */
  can_manage_members?: boolean
}

/** 知识库是否为项目索引库（项目库不走文档上传/公开性编辑）。 */
export function isProjectKb(kb: Pick<KnowledgeBase, 'kind'> | null | undefined): boolean {
  return kb?.kind === 'project'
}

export interface DocumentItem {
  id: string
  kb_id: string
  filename: string
  file_ext: string
  file_size: number
  status: 'pending' | 'processing' | 'ready' | 'failed' | 'cancelled'
  error_message: string | null
  chunk_count: number
  char_count: number
  progress: number
  cancel_requested: boolean
  /**
   * 入库失败重试状态（后端白名单代号）。
   * - ingest_attempts：已失败次数，成功入库后清零；
   * - next_attempt_at：退避解禁时刻，UTC ISO；只有可恢复失败才有值；
   * - last_error_code：稳定代号（不是给用户看的中文，需要前端映射）。
   */
  ingest_attempts?: number
  next_attempt_at?: string | null
  last_error_code?: string | null
  created_at: string
  /** 当前索引代次；非负整数。 */
  index_version: number
  /** 负责人说明（≤200 字符的描述文本，不是权限/ACL）。 */
  maintainer: string
  last_reviewed_at: string | null
  /** 生效窗口 [valid_from, valid_until)，UTC ISO，均可为空。 */
  valid_from: string | null
  valid_until: string | null
  /** 显式停用开关；false 时不可检索。 */
  enabled: boolean
  /** 被替代时间；null 表示未被替代。 */
  superseded_at: string | null
  /** 替代本文档的新文档 id；null 表示未被替代（或替代者已删除）。 */
  superseded_by_id: string | null
  /** 治理版本号，CAS 乐观锁；非负整数，默认 0。 */
  governance_revision: number
  /** 派生生命周期：active | scheduled | expired | disabled | superseded。 */
  lifecycle_state: 'active' | 'scheduled' | 'expired' | 'disabled' | 'superseded'
  /** 服务端实时派生的可检索标志（requires ready + cancel 未请求 + 治理谓词全部满足）。 */
  retrievable: boolean
}

/** PATCH governance 请求体；expected_revision 必须来自打开表单时的快照。 */
export interface GovernancePatchBody {
  expected_revision: number
  maintainer?: string
  last_reviewed_at?: string | null
  valid_from?: string | null
  valid_until?: string | null
  enabled?: boolean
}

export interface SourceLocation {
  page_start?: number | null
  page_end?: number | null
  section?: string | null
  sheet?: string | null
  row_start?: number | null
  row_end?: number | null
  line_start?: number | null
  line_end?: number | null
}

export interface Citation extends SourceLocation {
  /** 参考资料的原始编号；旧消息可能没有此字段。 */
  source_index?: number | null
  index_version?: number | null
  document_id: string | null
  filename: string | null
  chunk_index: number | null
  chunk_start?: number | null
  chunk_end?: number | null
  score: number | null
  snippet: string | null
  /**
   * 项目引用（source_kind='project'）：document_id 恒等于 file_version_id，
   * filename 恒等于 relative_path。读取原文走 GET /projects/{source_id}/files/
   * {file_version_id}/source?snapshot_id=...&chunk_start=...&chunk_end=...。
   */
  source_kind?: 'project' | null
  source_id?: string | null
  snapshot_id?: string | null
  file_version_id?: string | null
  relative_path?: string | null
  language?: string | null
  symbol?: string | null
  symbol_kind?: string | null
  manifest_fingerprint?: string | null
  config_fingerprint?: string | null
}

export interface MessageItem {
  id: string
  role: 'user' | 'assistant'
  status?: 'processing' | 'completed' | 'failed' | 'cancelled'
  turn_id?: string | null
  reply_to_id?: string | null
  content: string
  citations: Citation[] | null
  latency_ms: number
  created_at: string
  truncated?: boolean
  finish_reason?: string | null
}

/** 会话模式：按会话固定，续聊时由后端以会话已存的值为准。 */
export type ChatMode = 'rag' | 'chat'

export interface Conversation {
  id: string
  /** 普通对话（mode=chat）没有知识库 */
  kb_id: string | null
  mode: ChatMode
  title: string
  created_at: string
  updated_at: string
  /** 当前限定的文档范围；空数组表示整个知识库。旧响应可能缺失。 */
  document_scope?: DocumentScopeItem[]
  /** 范围版本号，用于 PATCH 与流式响应的并发保护；旧响应缺失时按 0。 */
  scope_revision?: number
  /**
   * 项目知识库会话绑定的快照 id（会话固定，不随后来的 active 漂移）。
   * 非项目会话为 null/缺失。切换必须走 CAS PATCH，不能靠再次发送改绑。
   */
  project_snapshot_id?: string | null
  /** 置顶时间；null/缺失表示未置顶。旧响应可能没有这个字段。 */
  pinned_at?: string | null
}

export interface ConversationDetail extends Conversation {
  messages: MessageItem[]
}

/** PATCH /chat/conversations/{id}/scope 的 data。 */
export interface ChatScopeResponse {
  document_scope: DocumentScopeItem[]
  scope_revision: number
  /** 项目会话绑定快照；非项目会话为 null。 */
  project_snapshot_id?: string | null
}

/**
 * GET /chat/conversations/{id}/memory 的响应。后端提供完成的摘要状态字段；
 * covered_turn_seq 是摘要实际纳入的最大轮次，绝不代表"全部历史已记住"。
 */
export interface MemoryStatus {
  enabled: boolean
  state: MemoryState
  epoch: number
  /** 当前摘要版本；无摘要时为 null（重置提交 expected_version=0）。 */
  version: number | null
  covered_turn_seq: number
  next_turn_seq: number
  structured: Record<string, unknown> | null
  lag: boolean
  truncated: boolean
  error_code: string | null
  error_message: string | null
  updated_at: string | null
}

/** POST /chat/conversations/{id}/memory/reset 的结果。 */
export interface MemoryResetResult {
  epoch: number
  version: null
  covered_turn_seq: number
  reset: boolean
}

/** SSE meta 或 done 事件里可选的摘要快照（additive，不覆盖完整状态）。 */
export interface MemoryStreamSnapshot {
  enabled: boolean
  state: MemoryState
  covered_turn_seq: number
  lag: boolean
}

/** SSE meta 事件；document_scope/scope_revision/memory 为可选的新字段。 */
export interface ChatStreamMeta {
  conversation_id: string
  request_id?: string
  user_message_id?: string
  citations: Citation[]
  mode?: ChatMode
  stream_limits?: StreamLimits
  document_scope?: DocumentScopeItem[]
  scope_revision?: number
  /** 本次 turn 实际绑定的项目快照；非项目为 null/缺失。 */
  project_snapshot_id?: string | null
  /** 本次 turn id；恢复/引用定位用，旧后端可能缺失。 */
  turn_id?: string | null
  memory?: MemoryStreamSnapshot
}

export interface PageResult<T> {
  total: number
  items: T[]
}

// ---------- 答案反馈 ----------
export type FeedbackCategory = 'inaccurate_answer' | 'citation_error' | 'missing_material'
export type FeedbackStatus = 'pending' | 'verified' | 'rejected'
export type FeedbackOutcome = 'answer' | 'refuse' | 'clarify'

export interface FeedbackCitation {
  document_id?: string | null
  filename?: string | null
  chunk_index?: number | null
  index_version?: number | null
  snippet?: string | null
}

export interface ExpectedSource {
  document_id?: string | null
  filename?: string | null
  chunk_index?: number | null
  index_version?: number | null
}

/** 本人反馈列表项（GET /feedback/mine）。 */
export interface AnswerFeedbackOwnerItem {
  id: string
  message_id: string
  conversation_id: string
  category: FeedbackCategory
  note: string
  status: FeedbackStatus
  revision: number
  created_at: string
  reviewed_at: string | null
}

/** 管理员列表项（GET /feedback）：默认脱敏，不返回问题/回答/引用正文。 */
export interface AnswerFeedbackAdminItem {
  id: string
  category: FeedbackCategory
  status: FeedbackStatus
  revision: number
  created_at: string
  reviewed_at: string | null
  reviewer_name: string
  note_present: boolean
}

/** 管理员详情（GET /feedback/{id}）：显式查看时才返回正文。 */
export interface AnswerFeedbackAdminDetail {
  id: string
  category: FeedbackCategory
  status: FeedbackStatus
  revision: number
  created_at: string
  reviewed_at: string | null
  reviewer_name: string
  /** 原回答所属会话模式；来源校验以它为准，而不是靠引用数量猜测。 */
  mode: 'rag' | 'chat'
  message_id: string
  conversation_id: string
  question: string | null
  answer: string
  note: string
  citations: FeedbackCitation[]
  invalid_source_ids: string[]
  result_stale: boolean
  corrected_outcome: FeedbackOutcome | null
  expected_sources: ExpectedSource[]
  required_phrases: string[]
  forbidden_phrases: string[]
  unknowns: string[]
  evidence_note: string
  reviewer_note: string
}

/** PATCH /feedback/{id}/review 请求体；expected_revision 必须来自打开详情时的快照。 */
export interface FeedbackReviewRequest {
  expected_revision: number
  decision: 'verified' | 'rejected'
  corrected_outcome?: FeedbackOutcome | null
  expected_sources?: ExpectedSource[]
  required_phrases?: string[]
  forbidden_phrases?: string[]
  unknowns?: string[]
  evidence_note?: string
  reviewer_note?: string
}

/** GET /feedback/export 的 data；jsonl 由用户显式下载，不自动保存/上传。 */
export interface FeedbackExportPayload {
  filename: string
  case_count: number
  dataset_sha256: string
  jsonl: string
}

export const feedbackApi = {
  /** 提交反馈；重复提交时后端返回既有记录，按成功处理。 */
  submit: (data: { message_id: string; category: FeedbackCategory; note?: string }, signal?: AbortSignal) =>
    client.post<AnswerFeedbackOwnerItem>('/feedback', data, { signal }),
  mine: (params: { page?: number; page_size?: number; status?: FeedbackStatus }) =>
    client.get<PageResult<AnswerFeedbackOwnerItem>>('/feedback/mine', { params }),
  adminList: (params: {
    page?: number
    page_size?: number
    status?: FeedbackStatus
    category?: FeedbackCategory
    signal?: AbortSignal
  }) =>
    client.get<PageResult<AnswerFeedbackAdminItem>>('/feedback', {
      params: {
        page: params.page,
        page_size: params.page_size,
        status: params.status,
        category: params.category
      },
      signal: params.signal
    }),
  adminDetail: (id: string, signal?: AbortSignal) =>
    client.get<AnswerFeedbackAdminDetail>(`/feedback/${id}`, { signal }),
  review: (id: string, payload: FeedbackReviewRequest) =>
    client.patch<AnswerFeedbackAdminItem>(`/feedback/${id}/review`, payload),
  /** 单个导出传 feedback_id；批量导出不传参数。 */
  export: (feedbackId?: string) =>
    client.get<FeedbackExportPayload>(
      '/feedback/export',
      feedbackId ? { params: { feedback_id: feedbackId } } : undefined
    )
}

// ---------- 反馈回归闭环（管理员） ----------
export type FeedbackRegressionCause =
  | 'parsing'
  | 'retrieval'
  | 'document_version'
  | 'generation'
  | 'scope_context'
  | 'other'

export type FeedbackRegressionMode = 'offline' | 'live'
export type FeedbackRegressionRunState = 'awaiting_report' | 'imported' | 'stale' | 'online'
export type FeedbackRegressionValidationLevel = 'consistency_only' | 'content_regraded'
export type FeedbackRegressionExecutionKind = 'manual' | 'online'
export type FeedbackRegressionJobRunStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

/** 一轮重跑的白名单摘要；不含样例/报告正文。 */
export interface FeedbackRegressionRun {
  run_id: string
  attempt_no: number
  is_current: boolean
  state: FeedbackRegressionRunState
  /** manual 是旧准备/导入轮次；online 只能通过作业比较/核验。 */
  execution_kind: FeedbackRegressionExecutionKind
  /** 在线轮次永远 false；旧响应可能缺省。 */
  can_import?: boolean
  job_id?: string | null
  job_status?: FeedbackRegressionJobRunStatus | null
  job_revision?: number | null
  requested_mode: FeedbackRegressionMode
  protocol_version: number
  case_sha256: string
  dataset_sha256: string
  binding_sha256: string
  prepared_by_name: string
  created_at: string
  imported_at: string | null
  imported_by_name: string | null
  report_mode: string | null
  validation_level: FeedbackRegressionValidationLevel | null
  passed: boolean | null
  failures: string[]
  outcome: string | null
  http_status: number | null
  citation_count: number | null
  dataset_report_sha256: string | null
  report_sha256: string | null
  result_sha256: string | null
  machine_sha256: string | null
}

/** GET /feedback/{id}/regression 的 data；未纳入时为 null。 */
export interface FeedbackRegression {
  feedback_id: string
  revision: number
  cause: FeedbackRegressionCause
  cause_note: string
  created_by_name: string
  created_at: string
  current_attempt_no: number
  available: boolean
  unavailable_reason: string | null
  current_run: FeedbackRegressionRun | null
}

/** POST /feedback/{id}/regression 请求体；expected_feedback_revision 来自详情快照。 */
export interface FeedbackRegressionEnrollRequest {
  expected_feedback_revision: number
  cause: FeedbackRegressionCause
  cause_note: string
}

/** POST /feedback/{id}/regression/runs 请求体；prepare_request_id 为幂等键。 */
export interface FeedbackRegressionPrepareRequest {
  expected_revision: number
  requested_mode: FeedbackRegressionMode
  prepare_request_id: string
}

/** 准备/重新导出当前轮的统一响应；jsonl 由用户显式下载。 */
export interface FeedbackRegressionPrepare {
  regression_revision: number
  run: FeedbackRegressionRun
  filename: string
  case_count: 1
  dataset_sha256: string
  protocol_version: number
  jsonl: string
  cli_hint: string
}

/** 导入成功后的摘要；不返回报告正文。 */
export interface FeedbackRegressionImport {
  regression_revision: number
  run: FeedbackRegressionRun
}

export const feedbackRegressionApi = {
  /** 回归纳入状态与实时可用性；未纳入时 data 为 null。 */
  get: (id: string, signal?: AbortSignal) =>
    client.get<FeedbackRegression | null>(`/feedback/${id}/regression`, { signal }),
  /** 纳入回归；相同原因与说明重复提交时后端幂等返回既有记录。 */
  enroll: (id: string, payload: FeedbackRegressionEnrollRequest, signal?: AbortSignal) =>
    client.post<FeedbackRegression>(`/feedback/${id}/regression`, payload, { signal }),
  /** 准备下一轮并取得单条 JSONL；每个 prepare_request_id 只创建一轮。 */
  prepare: (id: string, payload: FeedbackRegressionPrepareRequest, signal?: AbortSignal) =>
    client.post<FeedbackRegressionPrepare>(`/feedback/${id}/regression/runs`, payload, { signal }),
  /** 重新下载当前轮已绑定的 JSONL；非当前轮由后端返回 409。 */
  exportRun: (id: string, runId: string, signal?: AbortSignal) =>
    client.get<FeedbackRegressionPrepare>(
      `/feedback/${id}/regression/runs/${runId}/export`,
      { signal }
    ),
  /** 历史按 attempt_no 倒序分页。 */
  history: (id: string, params: { page?: number; page_size?: number }, signal?: AbortSignal) =>
    client.get<PageResult<FeedbackRegressionRun>>(`/feedback/${id}/regression/runs`, {
      params: { page: params.page, page_size: params.page_size },
      signal
    }),
  /**
   * 导入 CLI 生成的报告。请求体必须是原始 JSON 字节，不能包成对象；
   * 文件过大时由服务端返回 413，结构/绑定错误返回 400/409/422。
   */
  importReport: (
    id: string,
    runId: string,
    expectedRevision: number,
    file: File,
    signal?: AbortSignal
  ) =>
    client.post<FeedbackRegressionImport>(
      `/feedback/${id}/regression/runs/${runId}/import`,
      file,
      {
        params: { expected_revision: expectedRevision },
        headers: { 'Content-Type': 'application/json' },
        signal
      }
    )
}

/** 分片上传完成的结果；deduplicated=true 表示服务端复用了已有文档。 */
export interface UploadCompleteResult {
  upload_id: string
  document: DocumentItem
  deduplicated: boolean
}

// ---------- 认证 ----------
export const authApi = {
  login: (username: string, password: string) =>
    client.post<{ access_token: string; expires_in: number }>('/auth/login', { username, password }),
  me: () => client.get<User>('/auth/me'),
  logout: () => client.post('/auth/logout')
}

// ---------- 用户 ----------
/** 导入成功的一行：password 是明文初始密码，只在这个响应里出现一次。 */
export interface ImportedUser {
  row: number
  username: string
  display_name: string
  email: string | null
  role: 'admin' | 'user'
  password: string
}

export interface ImportFailureItem {
  row: number
  username: string
  reason: string
}

export interface ImportResult {
  total: number
  created: ImportedUser[]
  failed: ImportFailureItem[]
  receipt_filename: string
  /** 回执 xlsx 的 base64（明文密码不落库，只有这一次能拿到） */
  receipt_base64: string
}

export const userApi = {
  list: (params: { page?: number; page_size?: number; keyword?: string }) =>
    client.get<PageResult<User>>('/users', { params }),
  create: (data: Partial<User> & { username: string; password: string }) =>
    client.post<User>('/users', data),
  update: (id: string, data: Partial<User> & { password?: string }) =>
    client.patch<User>(`/users/${id}`, data),
  remove: (id: string) => client.delete(`/users/${id}`),
  changePassword: (old_password: string, new_password: string) =>
    client.post('/users/change-password', { old_password, new_password }),
  /** 批量导入：逐行尽力而为，回执 xlsx 随响应一起带回。 */
  importUsers: (file: File, onProgress?: (percent: number) => void) => {
    const form = new FormData()
    form.append('file', file)
    return client.post<ImportResult>('/users/import', form, {
      // 不手写 Content-Type：要让浏览器自己补 multipart 的 boundary
      timeout: 0,
      onUploadProgress: (e) => {
        if (e.total) onProgress?.(Math.round((e.loaded / e.total) * 100))
      }
    })
  },
  /** 导入模板（表头 + 角色下拉 + 填写说明）。 */
  downloadTemplate: () =>
    client.get<Blob>('/users/import/template', { responseType: 'blob' })
}

/** 触发浏览器下载一个 Blob（用完即释放 objectURL）。 */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // 立刻 revoke 会让部分浏览器（Safari）取消下载，放到下一轮事件循环
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** 把 base64 存成本地文件（导入回执用）。 */
export function saveBase64File(base64: string, filename: string, mime = 'application/octet-stream') {
  const bytes = atob(base64)
  const buffer = new Uint8Array(bytes.length)
  for (let i = 0; i < bytes.length; i += 1) buffer[i] = bytes.charCodeAt(i)
  saveBlob(new Blob([buffer], { type: mime }), filename)
}

// ---------- 知识库 ----------
export const kbApi = {
  list: () => client.get<KnowledgeBase[]>('/knowledge-bases'),
  create: (data: { name: string; description: string; is_public: boolean }) =>
    client.post<KnowledgeBase>('/knowledge-bases', data),
  update: (id: string, data: Partial<KnowledgeBase>) =>
    client.patch<KnowledgeBase>(`/knowledge-bases/${id}`, data),
  remove: (id: string) => client.delete(`/knowledge-bases/${id}`)
}

// ---------- 分片上传 ----------
export interface UploadSession {
  upload_id: string
  filename: string
  file_size: number
  chunk_size: number
  total_chunks: number
  received: number[]
  received_bytes: number
  status: 'uploading' | 'completed' | 'aborted'
  document_id: string | null
}

/**
 * complete 的最长等待。合并由服务端同步完成，没有进度可言：给足大文件拼接时间，
 * 但绝不允许无限等待；超时/取消都必须结束客户端的占用位。
 */
export const UPLOAD_COMPLETE_TIMEOUT_MS = 180_000

export const uploadApi = {
  /** 建会话；仅同一用户、同库、同名同大小且完整内容摘要相同才会续传。 */
  createSession: (kbId: string, filename: string, fileSize: number, contentSha256: string) =>
    client.post<UploadSession>(`/knowledge-bases/${kbId}/upload-sessions`, {
      filename,
      file_size: fileSize,
      content_sha256: contentSha256
    }),
  getSession: (kbId: string, uploadId: string) =>
    client.get<UploadSession>(`/knowledge-bases/${kbId}/upload-sessions/${uploadId}`),
  /** 单片上传：裸二进制 body，服务端按字节校验，重复上传幂等。 */
  uploadChunk: (
    kbId: string,
    uploadId: string,
    index: number,
    blob: Blob,
    signal?: AbortSignal,
    onProgress?: (loaded: number) => void
  ) =>
    client.post(
      `/knowledge-bases/${kbId}/upload-sessions/${uploadId}/chunks/${index}`,
      blob,
      {
        headers: { 'Content-Type': 'application/octet-stream' },
        timeout: 600000,
        signal,
        onUploadProgress: (e) => onProgress?.(e.loaded)
      }
    ),
  /**
   * 合并分片并创建文档；合并可能耗时几十秒，但必须有上限且可中断：
   * 取消上传时要能立刻停止等待，release 占用位让队列继续。
   */
  complete: (kbId: string, uploadId: string, signal?: AbortSignal) =>
    client.post<UploadCompleteResult>(
      `/knowledge-bases/${kbId}/upload-sessions/${uploadId}/complete`,
      undefined,
      // 有限超时（不是 0=永不超时）；signal 由调用方在上传被取消时 abort。
      { timeout: UPLOAD_COMPLETE_TIMEOUT_MS, signal }
    ),
  abort: (kbId: string, uploadId: string) =>
    client.delete(`/knowledge-bases/${kbId}/upload-sessions/${uploadId}`)
}

// ---------- 文档 ----------
export interface SourcePreview {
  document_id: string
  filename: string
  file_ext: string
  index_version: number
  chunk_index: number
  chunk_start: number
  chunk_end: number
  location: SourceLocation
  chunks: { chunk_index: number; content: string; location: SourceLocation }[]
  truncated: boolean
}

// ---------- 入库质量 ----------
export type DocumentQualitySeverity = 'info' | 'warning' | 'error'

/** 单条质量问题的数量可能未知（count=null）；details 仅供诊断，不直接展示原始键名。 */
export interface DocumentQualityIssue {
  code: string
  severity: DocumentQualitySeverity
  count: number | null
  details: Record<string, string | number> | null
}

export interface DocumentQualityStats {
  empty_chunks: number
  short_chunks: number
  duplicate_chunks: number
  replacement_chunks: number
  control_chunks: number
  oversized_chunks: number
  min_chars: number
  max_chars: number
  avg_chars: number
  page_chunks: number
  section_chunks: number
  line_chunks: number
  sheet_chunks: number
  table_chunks: number
}

/** GET /knowledge-bases/{kbId}/documents/{docId}/quality */
export interface DocumentQuality {
  document_id: string
  filename: string
  file_ext: string
  status: DocumentItem['status']
  error_message: string | null
  index_version: number
  parse_done: boolean
  cancel_requested: boolean
  chunk_count: number
  char_count: number
  scanned_chunks: number
  scan_limit: number
  scan_truncated: boolean
  embedded_count: number
  unembedded_count: number
  file_exists: boolean
  stats: DocumentQualityStats
  issues: DocumentQualityIssue[]
}

/** 分块预览条目；content 已由服务端截断到 1200 字符。 */
export interface DocumentChunkPreview {
  chunk_index: number
  content: string
  content_chars: number
  truncated: boolean
  embedded: boolean
  location: SourceLocation
}

/** GET /knowledge-bases/{kbId}/documents/{docId}/chunks */
export interface DocumentChunkPage {
  total: number
  offset: number
  limit: number
  items: DocumentChunkPreview[]
}

// ---------- 文档版本对比 ----------

/** 不可比较的原因；'ok' 表示可比较。字段值由后端冻结，不要自行扩展。 */
export type DocumentComparisonReason =
  | 'ok'
  | 'not_ready'
  | 'not_eligible'
  | 'incomplete'
  | 'empty_document'
  | 'too_many_chunks'
  | 'too_many_chars'
  | 'too_many_units'

/** 对比一侧的文档摘要。 */
export interface ComparisonDocument {
  id: string
  filename: string
  index_version: number
  governance_revision: number
  chunk_count: number
  char_count: number
  lifecycle_state: DocumentItem['lifecycle_state']
  retrievable: boolean
}

/** 已入库文本的最小对比单元。 */
export interface ComparisonUnit {
  chunk_index: number
  line_index: number
  text: string
  location: SourceLocation
}

export type ComparisonRowKind = 'unchanged' | 'added' | 'removed' | 'modified'

export interface ComparisonRow {
  kind: ComparisonRowKind
  original: ComparisonUnit | null
  replacement: ComparisonUnit | null
}

export interface ComparisonSummary {
  added: number
  removed: number
  modified: number
  unchanged: number
}

/** GET /knowledge-bases/{kbId}/documents/{docId}/comparison 的响应（契约冻结）。 */
export interface DocumentComparison {
  comparison_version: number
  basis: 'ingested_chunks'
  comparable: boolean
  reason: DocumentComparisonReason
  original: ComparisonDocument
  replacement: ComparisonDocument
  summary: ComparisonSummary | null
  rows: ComparisonRow[]
  comparison_fingerprint: string | null
}

// ---------- 检索诊断 ----------
export interface RetrievalDiagnosticsScopeItem {
  document_id: string
  filename: string
  index_version: number
}

export interface RetrievalDiagnosticsKbStats {
  total_documents: number
  ready_documents: number
  retrievable_documents: number
}

export interface RetrievalDiagnosticsConfig {
  top_k: number
  min_similarity: number
  max_per_doc: number
  hybrid_retrieval_enabled: boolean
  keyword_candidates: number
  rerank_enabled: boolean
  neighbor_chunks: number
}

export type RetrievalDiagnosticsStageName =
  | 'vector'
  | 'keyword'
  | 'fusion'
  | 'rerank'
  | 'select'
  | 'neighbors'

export type RetrievalDiagnosticsStageStatus = 'ok' | 'error' | 'skipped' | 'fallback'

export interface RetrievalDiagnosticsStage {
  name: RetrievalDiagnosticsStageName
  status: RetrievalDiagnosticsStageStatus
  ms: number
  candidates: number
  error_type: string | null
  applied: boolean | null
  reason: string | null
}

/** 候选片段；excerpt 已由服务端截断到 600 字符。 */
export interface RetrievalDiagnosticsCandidate {
  rank: number
  document_id: string | null
  filename: string | null
  chunk_index: number | null
  chunk_start: number | null
  chunk_end: number | null
  index_version: number | null
  location: SourceLocation
  excerpt: string
  excerpt_chars: number
  excerpt_truncated: boolean
  scores: Record<string, number>
  retrieval_method: 'vector' | 'keyword' | 'hybrid' | null
  lifecycle_state: string | null
  retrievable: boolean | null
}

/**
 * POST /knowledge-bases/{kbId}/retrieval-diagnostics 的 data。
 * 注意：status='error' 仍是 HTTP 200，表示检索执行失败；answer_generated 恒为 false。
 */
export interface RetrievalDiagnosticsResult {
  status: 'ok' | 'error'
  error_type: string | null
  question: string
  kb_id: string
  collection_name: string
  scope: RetrievalDiagnosticsScopeItem[]
  kb: RetrievalDiagnosticsKbStats
  config: RetrievalDiagnosticsConfig
  stages: RetrievalDiagnosticsStage[]
  candidates: RetrievalDiagnosticsCandidate[]
  total_ms: number
  answer_generated: false
}

export interface RetrievalDiagnosticsRequest {
  question: string
  document_ids: string[]
}

export const docApi = {
  source: (kbId: string, citation: Citation) => client.get<SourcePreview>(`/knowledge-bases/${kbId}/documents/${citation.document_id}/source`, {
    params: { chunk_index: citation.chunk_index, index_version: citation.index_version ?? 0,
      chunk_start: citation.chunk_start, chunk_end: citation.chunk_end }
  }),
  pdf: (kbId: string, docId: string, indexVersion: number) => client.get<Blob>(`/knowledge-bases/${kbId}/documents/${docId}/pdf`, {
    params: { index_version: indexVersion }, responseType: 'blob'
  }),
  get: (kbId: string, docId: string) => client.get<DocumentItem>(`/knowledge-bases/${kbId}/documents/${docId}`),
  /** q 为文件名关键字（服务端做字面匹配、trim、最长 200 字符）。 */
  list: (
    kbId: string,
    params: {
      page?: number
      page_size?: number
      status?: string
      q?: string
      /** true = 只返回当前可检索的文档（范围选择器专用）。 */
      retrievable_only?: boolean
    }
  ) =>
    client.get<PageResult<DocumentItem>>(`/knowledge-bases/${kbId}/documents`, { params }),
  reindex: (kbId: string, docId: string) =>
    client.post<DocumentItem>(`/knowledge-bases/${kbId}/documents/${docId}/reindex`),
  cancel: (kbId: string, docId: string) =>
    client.post<DocumentItem>(`/knowledge-bases/${kbId}/documents/${docId}/cancel`),
  remove: (kbId: string, docId: string) =>
    client.delete(`/knowledge-bases/${kbId}/documents/${docId}`),
  /**
   * 管理员治理编辑。expected_revision 为打开表单时的快照；
   * 409 = 版本冲突，调用方应提示刷新并重新打开表单，不能静默重试。
   */
  updateGovernance: (kbId: string, docId: string, body: GovernancePatchBody) =>
    client.patch<DocumentItem>(
      `/knowledge-bases/${kbId}/documents/${docId}/governance`,
      body
    ),
  /** 显式替代：同库、非自身、替代者当前可检索；409 = CAS 冲突、已被替代或预览已过期。 */
  supersede: (
    kbId: string,
    docId: string,
    body: {
      expected_revision: number
      replacement_document_id: string
      /** 对比预览指纹；服务端据此拒绝过期预览。旧调用可省略。 */
      comparison_fingerprint?: string
    }
  ) =>
    client.post<DocumentItem>(
      `/knowledge-bases/${kbId}/documents/${docId}/supersede`,
      body
    ),
  /**
   * 查看「本文档」与同库候选文档的已入库文本差异（管理员）。
   * comparable=false 时按 reason 展示不可比较原因，绝不当作“无差异”。
   */
  comparison: (kbId: string, docId: string, replacementId: string, signal?: AbortSignal) =>
    client.get<DocumentComparison>(
      `/knowledge-bases/${kbId}/documents/${docId}/comparison`,
      { params: { replacement_document_id: replacementId }, signal }
    ),
  /** 显式重新启用：清除替代标记并置 enabled=true；不删除文件、不触发重建索引。 */
  reenable: (kbId: string, docId: string, body: { expected_revision: number }) =>
    client.post<DocumentItem>(
      `/knowledge-bases/${kbId}/documents/${docId}/reenable`,
      body
    ),
  /** 单份文档的入库质量汇总（管理员）。 */
  quality: (kbId: string, docId: string, signal?: AbortSignal) =>
    client.get<DocumentQuality>(
      `/knowledge-bases/${kbId}/documents/${docId}/quality`,
      { signal }
    ),
  /** 分块分页预览；limit 由服务端限制为 1..50，界面固定 20。 */
  chunks: (
    kbId: string,
    docId: string,
    params: { offset: number; limit: number },
    signal?: AbortSignal
  ) =>
    client.get<DocumentChunkPage>(
      `/knowledge-bases/${kbId}/documents/${docId}/chunks`,
      { params, signal }
    )
}

// ---------- 检索诊断 ----------
export const diagnosticsApi = {
  /** 执行一次真实检索（不生成答案）；status='error' 也走 HTTP 200。 */
  run: (kbId: string, body: RetrievalDiagnosticsRequest, signal?: AbortSignal) =>
    client.post<RetrievalDiagnosticsResult>(
      `/knowledge-bases/${kbId}/retrieval-diagnostics`,
      body,
      { signal }
    )
}

// ---------- 问答 ----------
export const chatApi = {
  requestState: (requestId: string, signal?: AbortSignal) => client.get<ChatRequestState>(`/chat/requests/${requestId}`, {
    signal, timeout: 3000,
    validateStatus: status => [200, 403, 404, 410].includes(status)
  }),
  cancelRequest: (requestId: string) => client.post(`/chat/requests/${requestId}/cancel`, undefined, {
    // 停止可能早于服务端认领，404 交给断连清理兜底，无需重复弹错。
    timeout: 5000,
    validateStatus: status => status === 404 || (status >= 200 && status < 300)
  }),
  /** 检索条数滑块的初值：与后端 RETRIEVAL_TOP_K 同源，避免两处各硬编码一个数字。 */
  retrievalConfig: () => client.get<{ top_k: number }>('/chat/retrieval-config'),
  /** 旧接口：数组响应，最多 100 条，保留兼容。 */
  conversations: (params?: { kbId?: string; mode?: ChatMode }) =>
    client.get<Conversation[]>('/chat/conversations', {
      params: { kb_id: params?.kbId, mode: params?.mode }
    }),
  /**
   * 分页会话列表。q 是标题字面搜索（服务端 trim、最长 200）。
   * 服务端按 pinned_at DESC NULLS LAST, updated_at DESC, id DESC 稳定排序。
   */
  conversationsPage: (params: {
    kbId?: string
    mode?: ChatMode
    q?: string
    page?: number
    pageSize?: number
    signal?: AbortSignal
  } = {}) =>
    client.get<PageResult<Conversation>>('/chat/conversations/page', {
      params: { kb_id: params.kbId, mode: params.mode, q: params.q, page: params.page, page_size: params.pageSize },
      signal: params.signal
    }),
  /** 更新会话元数据；只接受 title / pinned，至少一项。 */
  updateConversation: (id: string, data: { title?: string; pinned?: boolean }) =>
    client.patch<Conversation>(`/chat/conversations/${id}`, data),
  conversation: (id: string) => client.get<ConversationDetail>(`/chat/conversations/${id}`),
  /**
   * 更新会话限定的文档范围。空数组表示恢复整个知识库。
   * expectedRevision 来自当前会话快照；服务端忙碌或版本冲突返回 409。
   */
  setDocumentScope: (id: string, documentIds: string[], expectedRevision?: number) =>
    client.patch<ChatScopeResponse>(
      `/chat/conversations/${id}/scope`,
      buildScopePatchBody(documentIds, expectedRevision)
    ),
  /**
   * CAS 切换项目会话绑定的快照。expectedRevision 必须等于会话当前 scope_revision。
   * 409 = 忙碌/版本过期，400 = 非项目会话，404/410 = 源被撤销或无权访问。
   * 相同快照幂等，不递增版本。
   */
  setProjectSnapshot: (id: string, snapshotId: string, expectedRevision: number) =>
    client.patch<ChatScopeResponse>(
      `/chat/conversations/${id}/project-snapshot`,
      { snapshot_id: snapshotId, expected_revision: expectedRevision }
    ),
  removeConversation: (id: string) => client.delete(`/chat/conversations/${id}`)
}

/** 会话摘要（memory）读取与重置。本人会话限定（admin 也只读本人）。 */
export const memoryApi = {
  /** 读取摘要状态；GET 失败/404 由调用方按 unknown/不可用处理，不弹全局错误。 */
  get: (conversationId: string, signal?: AbortSignal) =>
    client.get<MemoryStatus>(`/chat/conversations/${conversationId}/memory`, {
      signal, timeout: 3000,
      validateStatus: status => [200, 403, 404].includes(status)
    }),
  /**
   * 重置摘要。expected_version 与服务端当前版本不符（已被别处更新）、或会话正在
   * 生成/恢复时返回 409；这里不弹全局错误，由调用方刷新状态而不是覆盖旧版。
   */
  reset: (conversationId: string, expectedVersion: number) =>
    client.post<MemoryResetResult>(`/chat/conversations/${conversationId}/memory/reset`, {
      expected_version: expectedVersion
    }, { timeout: 5000, validateStatus: status => [200, 409].includes(status) })
}

/**
 * SSE 流式问答。fetch 无法使用 axios 拦截器，这里手动带 token。
 */
export async function streamChat(
  payload: {
    /** 普通对话不传 */
    kb_id?: string
    question: string
    request_id?: string
    conversation_id?: string | null
    top_k?: number
    /** 只在新建会话时生效，续聊以会话已存的模式为准 */
    mode?: ChatMode
    /** 新会话创建时携带的草稿范围；已有会话改用 PATCH scope */
    document_ids?: string[]
    /** 已有会话发送时的范围版本快照，用于并发保护 */
    scope_revision?: number
    /**
     * 项目知识库：仅新会话可显式指定快照；已有会话沿用绑定，改绑必须走 CAS PATCH。
     * 项目请求必须完全省略 document_ids（后端把显式空数组也视为互斥 → 400）。
     */
    project_snapshot_id?: string
  },
  handlers: {
    onMeta?: (data: ChatStreamMeta) => void
    onDelta?: (text: string) => void
    onDone?: (data: ChatCompletion) => void
    onStatus?: (phase: StreamPhase) => void
    onError?: (message: string) => void
  },
  signal?: AbortSignal
): Promise<void> {
  const token = localStorage.getItem('rag_token')
  // Starts before fetch: stalled response headers are covered by the same deadlines.
  const guard = createStreamGuard(signal)
  try {
    let response: Response
    try {
      response = await fetch('/api/v1/chat/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {})
        },
        body: JSON.stringify({ ...payload, request_id: payload.request_id ?? createChatRequestId() }),
        signal: guard.signal
      })
    } catch (error) {
      if (guard.signal.aborted) throw guard.signal.reason
      if (error instanceof Error && error.name === 'AbortError') throw error
      throw new Error(i18n.global.t('errors.network'))
    }
    if (response.status === 401) {
      localStorage.removeItem('rag_token')
      location.href = '/login'
      return
    }
    if (!response.ok || !response.body) {
      // Bound even a proxy HTML error body instead of accumulating it without limit.
      const reader = response.body?.getReader()
      let text = ''
      if (reader) {
        const decoder = new TextDecoder()
        let bytes = 0
        try {
          while (bytes < 4096) {
            guard.check()
            const { done, value } = await reader.read()
            if (done) break
            const part = value.subarray(0, 4096 - bytes)
            bytes += part.length
            text += decoder.decode(part, { stream: true })
          }
          text += decoder.decode()
        } finally {
          void reader.cancel().catch(() => undefined)
          reader.releaseLock()
        }
      }
      let message = ''
      try {
        const body = JSON.parse(text)
        message = typeof body?.message === 'string' ? body.message : ''
      } catch { /* Non-JSON proxy failures use a bounded fallback. */ }
      handlers.onError?.(message || i18n.global.t('errors.http', { status: response.status }))
      return
    }
    if (!response.headers.get('content-type')?.includes('text/event-stream')) throw new StreamProtocolError('invalidStream')
    await consumeEventStream(response.body, (event, data) => {
      if (event === 'meta') handlers.onMeta?.(data)
      else if (event === 'status') handlers.onStatus?.(data.phase)
      else if (event === 'delta') handlers.onDelta?.(data.content)
      else if (event === 'done') handlers.onDone?.(data)
      else if (event === 'error') handlers.onError?.(data.message)
    }, signal, { guard })
  } catch (error) {
    if (signal?.aborted) throw signal.reason
    const cause = guard.signal.aborted ? guard.signal.reason : error
    if (cause instanceof StreamProtocolError) throw new Error(i18n.global.t(`errors.${cause.code}`))
    throw cause
  } finally {
    guard.dispose()
  }
}
