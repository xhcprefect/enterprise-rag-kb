import client from './client'

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

export interface Citation {
  document_id: string | null
  filename: string | null
  chunk_index: number | null
  score: number | null
  snippet: string | null
}

export interface MessageItem {
  id: string
  role: 'user' | 'assistant'
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
}

export interface ConversationDetail extends Conversation {
  messages: MessageItem[]
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
  /** 建会话；同库同名同大小的未完成会话会被复用，直接带回已收分片。 */
  createSession: (kbId: string, filename: string, fileSize: number) =>
    client.post<UploadSession>(`/knowledge-bases/${kbId}/upload-sessions`, {
      filename,
      file_size: fileSize
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
export const docApi = {
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
  /** kbId 与 mode 都可选：普通对话只传 mode，RAG 两个都传。 */
  conversations: (params?: { kbId?: string; mode?: ChatMode }) =>
    client.get<Conversation[]>('/chat/conversations', {
      params: { kb_id: params?.kbId, mode: params?.mode }
    }),
  conversation: (id: string) => client.get<ConversationDetail>(`/chat/conversations/${id}`),
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
    conversation_id?: string | null
    top_k?: number
    /** 只在新建会话时生效，续聊以会话已存的模式为准 */
    mode?: ChatMode
  },
  handlers: {
    onMeta?: (data: { conversation_id: string; citations: Citation[]; mode?: ChatMode }) => void
    onDelta?: (text: string) => void
    onDone?: (data: { latency_ms: number }) => void
    onError?: (message: string) => void
  },
  signal?: AbortSignal
): Promise<void> {
  const token = localStorage.getItem('rag_token')
  const response = await fetch('/api/v1/chat/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: JSON.stringify(payload),
    signal
  })

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
    handlers.onError?.(message || text || `请求失败 (${response.status})`)
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    let index: number
    while ((index = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, index)
      buffer = buffer.slice(index + 2)

      let event = 'message'
      const dataLines: string[] = []
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      }
      if (!dataLines.length) continue

      try {
        const data = JSON.parse(dataLines.join('\n'))
        if (event === 'meta') handlers.onMeta?.(data)
        else if (event === 'delta') handlers.onDelta?.(data.content ?? '')
        else if (event === 'done') handlers.onDone?.(data)
        else if (event === 'error') handlers.onError?.(data.message ?? '未知错误')
      } catch {
        /* 忽略无法解析的帧 */
      }
    }
  }
}
