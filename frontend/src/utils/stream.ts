export type StreamErrorCode = 'invalidStream' | 'streamInterrupted' | 'firstTokenTimeout' | 'idleTimeout' | 'totalTimeout' | 'streamLimit'

/** 传输层报告稳定错误码，由调用处按界面语言显示。 */
export class StreamProtocolError extends Error {
  readonly code: StreamErrorCode
  constructor(code: StreamErrorCode) {
    const messages: Record<StreamErrorCode, string> = {
      invalidStream: '流式响应格式异常，请重试', streamInterrupted: '回答连接提前中断，请检查请求结果',
      firstTokenTimeout: '等待回答正文超时', idleTimeout: '回答正文长时间未更新',
      totalTimeout: '本轮回答超过总处理时限', streamLimit: '流式回答超过允许的大小'
    }
    super(messages[code])
    this.name = 'StreamProtocolError'
    this.code = code
  }
}

export interface StreamLimits {
  first_token_timeout_ms: number
  idle_timeout_ms: number
  total_timeout_ms: number
  heartbeat_interval_ms: number
  max_answer_chars: number
}
export const DEFAULT_STREAM_LIMITS: StreamLimits = {
  first_token_timeout_ms: 60000, idle_timeout_ms: 45000, total_timeout_ms: 300000,
  heartbeat_interval_ms: 10000, max_answer_chars: 131072
}
export type StreamPhase = 'retrieving' | 'generating' | 'saving' | 'recovering'
const MAX_FRAME_BYTES = 65536
const MAX_ANSWER_CHARS = 1048576
const object = (value: unknown): value is Record<string, any> => !!value && typeof value === 'object' && !Array.isArray(value)
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const identifier = (value: unknown) => typeof value === 'string' && value.length > 0 && value.length <= 128
const nullableString = (value: unknown) => value == null || typeof value === 'string'
const invalid = (): never => { throw new StreamProtocolError('invalidStream') }

export function validCitations(value: unknown): boolean {
  if (!Array.isArray(value) || value.length > 100) return false
  return value.every((item) => object(item)
    && ['document_id', 'filename', 'snippet', 'section', 'sheet'].every(key => nullableString(item[key]))
    && ['source_index', 'index_version', 'chunk_index', 'chunk_start', 'chunk_end', 'page_start', 'page_end', 'row_start', 'row_end', 'line_start', 'line_end']
      .every(key => item[key] == null || integer(item[key]))
    && (item.score == null || typeof item.score === 'number' && Number.isFinite(item.score) && item.score >= 0 && item.score <= 1))
}

export function validScope(value: unknown): boolean {
  return Array.isArray(value) && value.length <= 5 && value.every(item => object(item)
    && identifier(item.document_id) && typeof item.filename === 'string' && integer(item.index_version))
}

function validateLimits(value: unknown): asserts value is StreamLimits {
  if (!object(value)) return invalid()
  for (const key of Object.keys(DEFAULT_STREAM_LIMITS)) {
    if (!integer(value[key]) || value[key] < 1) invalid()
    if (key === 'max_answer_chars' ? value[key] > MAX_ANSWER_CHARS : value[key] > 3600000) invalid()
  }
}

export function validateStreamEvent(event: string, data: unknown): asserts data is Record<string, any> {
  if (!object(data)) return invalid()
  if (data.document_scope !== undefined && !validScope(data.document_scope)) invalid()
  if (data.scope_revision !== undefined && !integer(data.scope_revision)) invalid()
  if (event === 'meta') {
    if (!identifier(data.conversation_id)) invalid()
    for (const key of ['request_id', 'user_message_id']) if (data[key] !== undefined && !identifier(data[key])) invalid()
    if (data.mode !== undefined && data.mode !== 'rag' && data.mode !== 'chat') invalid()
    if (data.citations !== undefined && !validCitations(data.citations)) invalid()
    if (data.stream_limits !== undefined) validateLimits(data.stream_limits)
  } else if (event === 'delta') {
    if (typeof data.content !== 'string') invalid()
  } else if (event === 'done') {
    if (typeof data.latency_ms !== 'number' || !Number.isFinite(data.latency_ms) || data.latency_ms < 0) invalid()
    if (data.citations !== undefined && !validCitations(data.citations)) invalid()
    if (data.message_id !== undefined && !identifier(data.message_id)) invalid()
    if (data.truncated !== undefined && typeof data.truncated !== 'boolean') invalid()
    if (!nullableString(data.finish_reason) || typeof data.finish_reason === 'string' && data.finish_reason.length > 64) invalid()
    if (data.first_token_ms != null && !integer(data.first_token_ms)) invalid()
  } else if (event === 'error') {
    if (typeof data.message !== 'string' || data.message.length > 4096) invalid()
    if (data.code !== undefined && (typeof data.code !== 'string' || data.code.length > 128)) invalid()
  } else if (event === 'status') {
    if (!['retrieving', 'generating', 'saving'].includes(data.phase)) invalid()
  }
}

/** One content deadline and one overall deadline; heartbeats/status never renew either. */
export function createStreamGuard(signal?: AbortSignal, options: { limits?: Partial<StreamLimits>; startedAt?: number } = {}) {
  const controller = new AbortController()
  let limits = { ...DEFAULT_STREAM_LIMITS, ...options.limits }
  let first = false
  let lastContent = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let stopped = false
  const startedAt = options.startedAt ?? performance.now()
  const stop = () => {
    stopped = true
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
  const onAbort = () => { controller.abort(signal?.reason); stop() }
  const arm = () => {
    clearTimeout(timer)
    if (stopped || controller.signal.aborted) return
    const now = performance.now()
    const total = startedAt + limits.total_timeout_ms - now
    const content = (first ? lastContent + limits.idle_timeout_ms : startedAt + limits.first_token_timeout_ms) - now
    const code: StreamErrorCode = total <= content ? 'totalTimeout' : first ? 'idleTimeout' : 'firstTokenTimeout'
    const remaining = Math.min(total, content)
    if (remaining <= 0) {
      controller.abort(new StreamProtocolError(code))
      stop()
    } else timer = setTimeout(arm, Math.ceil(remaining))
  }
  signal?.addEventListener('abort', onAbort, { once: true })
  if (signal?.aborted) onAbort()
  else arm()
  return {
    signal: controller.signal,
    get limits() { return limits },
    check() { arm(); controller.signal.throwIfAborted() },
    observe(event: string, data: Record<string, any>) {
      arm()
      controller.signal.throwIfAborted()
      if (event === 'meta' && data.stream_limits) {
        validateLimits(data.stream_limits)
        limits = { ...data.stream_limits }
      }
      if (event === 'delta' && data.content) {
        if (!first && data.content.trim()) first = true
        if (first) lastContent = performance.now()
      }
      if (event === 'done' || event === 'error') stop()
      else arm()
    },
    dispose: stop
  }
}
export type StreamGuard = ReturnType<typeof createStreamGuard>

/** Read SSE with bounded raw bytes, strict fields, cancellable pending reads and natural backpressure. */
export async function consumeEventStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: string, data: any) => void,
  signal?: AbortSignal,
  options: { limits?: Partial<StreamLimits>; startedAt?: number; guard?: StreamGuard; maxFrameBytes?: number } = {}
): Promise<void> {
  const reader = body.getReader()
  const guard = options.guard ?? createStreamGuard(signal, options)
  const decoder = new TextDecoder('utf-8', { fatal: true })
  const limit = options.maxFrameBytes ?? MAX_FRAME_BYTES
  let terminal = false
  let answerChars = 0
  let event = 'message'
  let dataLines: string[] = []
  let fragments: Uint8Array[] = []
  let lineSize = 0
  let frameSize = 0
  let metaSeen = false
  let rejectAbort: ((reason: unknown) => void) | undefined
  const aborted = new Promise<never>((_resolve, reject) => { rejectAbort = reject })
  // A raced promise prevents a non-compliant stream's cancel hook from trapping a pending read.
  const onAbort = () => {
    rejectAbort?.(guard.signal.reason ?? new DOMException('Aborted', 'AbortError'))
    void reader.cancel().catch(() => undefined)
  }
  guard.signal.addEventListener('abort', onAbort, { once: true })

  const dispatch = () => {
    if (dataLines.length) {
      let data: unknown
      try { data = JSON.parse(dataLines.join('\n')) } catch { invalid() }
      validateStreamEvent(event, data)
      if (event === 'meta') {
        if (metaSeen) invalid()
        metaSeen = true
      }
      guard.observe(event, data)
      if (event === 'delta') {
        answerChars += data.content.length
        if (answerChars > guard.limits.max_answer_chars) throw new StreamProtocolError('streamLimit')
      }
      if (event === 'done' || event === 'error') terminal = true
      onEvent(event, data)
    }
    event = 'message'
    dataLines = []
    frameSize = 0
  }
  const finishLine = () => {
    const bytes = new Uint8Array(lineSize)
    let offset = 0
    for (const fragment of fragments) { bytes.set(fragment, offset); offset += fragment.length }
    fragments = []
    lineSize = 0
    let line: string
    try { line = decoder.decode(bytes).replace(/\r$/, '') } catch { return invalid() }
    if (!line) dispatch()
    else if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }

  try {
    guard.check()
    while (!terminal) {
      guard.check()
      const { done, value } = await Promise.race([reader.read(), aborted])
      guard.check()
      if (done) {
        if (lineSize) finishLine()
        if (!terminal && dataLines.length) dispatch()
        if (!terminal) throw new StreamProtocolError('streamInterrupted')
        break
      }
      let start = 0
      while (!terminal && start < value.length) {
        const end = value.indexOf(10, start)
        const stop = end < 0 ? value.length : end
        const size = stop - start
        frameSize += size + (end < 0 ? 0 : 1)
        lineSize += size
        if (frameSize > limit || lineSize > limit) throw new StreamProtocolError('streamLimit')
        if (size) fragments.push(value.slice(start, stop))
        if (end < 0) break
        finishLine()
        start = end + 1
      }
    }
  } finally {
    guard.signal.removeEventListener('abort', onAbort)
    if (!options.guard) guard.dispose()
    // cancel() may itself stall in a custom source. Release the reader without awaiting it.
    void reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}
