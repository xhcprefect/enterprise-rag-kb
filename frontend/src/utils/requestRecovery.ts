import type { MessageItem } from '../api/index.ts'
import type { ChatCompletion } from './citations.ts'
import type { DocumentScopeItem } from './documentScope.ts'
import { validateStreamEvent, validScope, StreamProtocolError } from './stream.ts'

export interface ChatRequestState {
  request_id: string
  conversation_id: string
  user_message_id: string
  status: 'processing' | 'completed' | 'failed' | 'cancelled'
  message: MessageItem | null
  truncated: boolean
  finish_reason: string | null
  error_code?: string | null
  error_message?: string | null
  document_scope: DocumentScopeItem[]
  scope_revision: number
}

export function validateRequestState(value: unknown, requestId: string, conversationId?: string | null): ChatRequestState {
  const state = value as ChatRequestState | null
  const invalid = (): never => { throw new StreamProtocolError('invalidStream') }
  if (!state || typeof state !== 'object' || Array.isArray(state) || state.request_id !== requestId
    || typeof state.conversation_id !== 'string' || !state.conversation_id
    || typeof state.user_message_id !== 'string' || !state.user_message_id
    || conversationId && state.conversation_id !== conversationId
    || !['processing', 'completed', 'failed', 'cancelled'].includes(state.status)
    || !validScope(state.document_scope) || !Number.isSafeInteger(state.scope_revision) || state.scope_revision < 0
    || typeof state.truncated !== 'boolean'
    || state.finish_reason !== null && (typeof state.finish_reason !== 'string' || state.finish_reason.length > 64)
    || state.error_code != null && (typeof state.error_code !== 'string' || state.error_code.length > 128)
    || state.error_message != null && (typeof state.error_message !== 'string' || state.error_message.length > 4096)) return invalid()
  if (state.status === 'completed') {
    const message = state.message
    if (!message || message.role !== 'assistant' || message.status !== 'completed'
      || typeof message.id !== 'string' || !message.id || typeof message.content !== 'string'
      || message.content.length > 1048576 || !message.content.trim()
      || message.reply_to_id != null && message.reply_to_id !== state.user_message_id) return invalid()
    validateStreamEvent('done', { ...message, citations: message.citations ?? [],
      truncated: state.truncated, finish_reason: state.finish_reason })
  } else if (state.message !== null) return invalid()
  return state
}

export function recoveredCompletion(state: ChatRequestState): ChatCompletion {
  if (state.status !== 'completed' || !state.message) throw new StreamProtocolError('invalidStream')
  return {
    message_id: state.message.id, latency_ms: state.message.latency_ms, citations: state.message.citations ?? [],
    truncated: state.truncated, finish_reason: state.finish_reason,
    document_scope: state.document_scope, scope_revision: state.scope_revision
  }
}

/** Bounded read-only polling; this helper has no generation callback and cannot resubmit a question. */
export async function recoverRequest(
  requestId: string,
  load: (requestId: string, signal: AbortSignal) => Promise<unknown | null>,
  options: { signal?: AbortSignal; conversationId?: string | null; maxAttempts?: number; intervalMs?: number; timeoutMs?: number } = {}
): Promise<ChatRequestState | null> {
  const controller = new AbortController()
  const externalAbort = () => controller.abort(options.signal?.reason)
  options.signal?.addEventListener('abort', externalAbort, { once: true })
  if (options.signal?.aborted) externalAbort()
  const timeout = setTimeout(() => controller.abort(new DOMException('Recovery timed out', 'TimeoutError')), options.timeoutMs ?? 8000)
  let rejectAbort: ((reason: unknown) => void) | undefined
  const aborted = new Promise<never>((_resolve, reject) => { rejectAbort = reject })
  const onAbort = () => rejectAbort?.(controller.signal.reason)
  controller.signal.addEventListener('abort', onAbort, { once: true })
  const attempts = Math.max(1, Math.min(10, options.maxAttempts ?? 4))
  let latest: ChatRequestState | null = null
  let delayTimer: ReturnType<typeof setTimeout> | undefined
  try {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      controller.signal.throwIfAborted()
      const value = await Promise.race([load(requestId, controller.signal), aborted])
      controller.signal.throwIfAborted()
      if (value == null) return null
      latest = validateRequestState(value, requestId, options.conversationId)
      if (latest.status !== 'processing') return latest
      if (attempt < attempts - 1) {
        await Promise.race([new Promise<void>(resolve => { delayTimer = setTimeout(resolve, options.intervalMs ?? 500) }), aborted])
      }
    }
    return latest
  } finally {
    clearTimeout(timeout)
    clearTimeout(delayTimer)
    options.signal?.removeEventListener('abort', externalAbort)
    controller.signal.removeEventListener('abort', onAbort)
  }
}
