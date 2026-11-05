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
  created_at: string
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
  list: (kbId: string, params: { page?: number; page_size?: number; status?: string; q?: string }) =>
    client.get<PageResult<DocumentItem>>(`/knowledge-bases/${kbId}/documents`, { params }),
  reindex: (kbId: string, docId: string) =>
    client.post<DocumentItem>(`/knowledge-bases/${kbId}/documents/${docId}/reindex`),
  cancel: (kbId: string, docId: string) =>
    client.post<DocumentItem>(`/knowledge-bases/${kbId}/documents/${docId}/cancel`),
  remove: (kbId: string, docId: string) =>
    client.delete(`/knowledge-bases/${kbId}/documents/${docId}`)
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
