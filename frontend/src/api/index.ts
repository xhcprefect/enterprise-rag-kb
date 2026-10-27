import client from './client'
import { consumeEventStream, StreamProtocolError } from '@/utils/stream'
import { i18n } from '@/i18n'
import type { ChatCompletion } from '@/utils/citations'
import { createChatRequestId } from '@/utils/chatRequest'
import { buildScopePatchBody, type DocumentScopeItem } from '@/utils/documentScope'

export type { DocumentScopeItem } from '@/utils/documentScope'

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
}

export interface ConversationDetail extends Conversation {
  messages: MessageItem[]
}

/** PATCH /chat/conversations/{id}/scope 的 data。 */
export interface ChatScopeResponse {
  document_scope: DocumentScopeItem[]
  scope_revision: number
}

/** SSE meta 事件；document_scope/scope_revision 为可选的新字段。 */
export interface ChatStreamMeta {
  conversation_id: string
  request_id?: string
  user_message_id?: string
  citations: Citation[]
  mode?: ChatMode
  document_scope?: DocumentScopeItem[]
  scope_revision?: number
}

export interface PageResult<T> {
  total: number
  items: T[]
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
  /** 合并分片并创建文档；可能耗时几十秒（大文件拼接），单独放宽超时。 */
  complete: (kbId: string, uploadId: string) =>
    client.post<{ upload_id: string; document: DocumentItem }>(
      `/knowledge-bases/${kbId}/upload-sessions/${uploadId}/complete`,
      undefined,
      { timeout: 0 }
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
  list: (kbId: string, params: { page?: number; page_size?: number; status?: string }) =>
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
  cancelRequest: (requestId: string) => client.post(`/chat/requests/${requestId}/cancel`, undefined, {
    // 停止可能早于服务端认领，404 交给断连清理兜底，无需重复弹错。
    timeout: 5000,
    validateStatus: status => status === 404 || (status >= 200 && status < 300)
  }),
  /** 检索条数滑块的初值：与后端 RETRIEVAL_TOP_K 同源，避免两处各硬编码一个数字。 */
  retrievalConfig: () => client.get<{ top_k: number }>('/chat/retrieval-config'),
  /** kbId 与 mode 都可选：普通对话只传 mode，RAG 两个都传。 */
  conversations: (params?: { kbId?: string; mode?: ChatMode }) =>
    client.get<Conversation[]>('/chat/conversations', {
      params: { kb_id: params?.kbId, mode: params?.mode }
    }),
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
  removeConversation: (id: string) => client.delete(`/chat/conversations/${id}`)
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
  },
  handlers: {
    onMeta?: (data: ChatStreamMeta) => void
    onDelta?: (text: string) => void
    onDone?: (data: ChatCompletion) => void
    onError?: (message: string) => void
  },
  signal?: AbortSignal
): Promise<void> {
  const token = localStorage.getItem('rag_token')
  let response: Response
  try {
    response = await fetch('/api/v1/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {})
      },
      body: JSON.stringify({ ...payload, request_id: payload.request_id ?? createChatRequestId() }),
      signal
    })
  } catch (error) {
    if (signal?.aborted || (error instanceof Error && error.name === 'AbortError')) throw error
    throw new Error(i18n.global.t('errors.network'))
  }

  if (response.status === 401) {
    localStorage.removeItem('rag_token')
    location.href = '/login'
    return
  }
  if (!response.ok || !response.body) {
    // 失败时后端返回统一结构 {code, success:false, message}，优先取 message
    const text = await response.text()
    let message = ''
    try {
      const body = JSON.parse(text)
      message = typeof body?.message === 'string' ? body.message : ''
    } catch {
      /* 非 JSON（网关错误页等）时退回原文 */
    }
    handlers.onError?.(message || text || i18n.global.t('errors.http', { status: response.status }))
    return
  }

  try {
    await consumeEventStream(response.body, (event, data) => {
      if (event === 'meta') handlers.onMeta?.(data)
      else if (event === 'delta') handlers.onDelta?.(data.content ?? '')
      else if (event === 'done') handlers.onDone?.(data)
      else if (event === 'error') handlers.onError?.(data.message ?? i18n.global.t('errors.unknown'))
    }, signal)
  } catch (error) {
    if (error instanceof StreamProtocolError) throw new Error(i18n.global.t(`errors.${error.code}`))
    throw error
  }
}
