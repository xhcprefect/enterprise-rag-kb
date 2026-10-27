import assert from 'node:assert/strict'
import test from 'node:test'
import { consumeEventStream, createStreamGuard, DEFAULT_STREAM_LIMITS, StreamProtocolError } from '../src/utils/stream.ts'
import { recoverRequest, recoveredCompletion, validateRequestState, type ChatRequestState } from '../src/utils/requestRecovery.ts'
import { createStreamingMarkdown } from '../src/utils/streamingMarkdown.ts'
import { createSessionEpoch } from '../src/utils/sessionEpoch.ts'

const encoder = new TextEncoder()
const event = (name: string, data: unknown) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`
const delta = (text: string) => event('delta', { content: text })
const done = event('done', { latency_ms: 1, citations: [], truncated: false, finish_reason: 'stop' })
function body(parts: (string | Uint8Array)[], close = true, onCancel = () => {}) {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const part of parts) controller.enqueue(typeof part === 'string' ? encoder.encode(part) : part)
      if (close) controller.close()
    }, cancel: onCancel
  })
}
const code = (expected: string) => (error: unknown) => error instanceof StreamProtocolError && error.code === expected


test('Chinese and emoji survive fragmentation at every UTF-8 byte with CRLF', async () => {
  const bytes = encoder.encode((delta('中文🙂') + done).replace(/\n/g, '\r\n'))
  const parts = [...bytes].map(byte => Uint8Array.of(byte))
  let answer = ''
  await consumeEventStream(body(parts), (name, data) => { if (name === 'delta') answer += data.content })
  assert.equal(answer, '中文🙂')
})

test('multiline SSE data is parsed as a single JSON event', async () => {
  const events: string[] = []
  await consumeEventStream(body(['event: delta\ndata: {"content":\ndata: "中文"}\n\n', done]), (name, data) => events.push(`${name}:${data.content ?? ''}`))
  assert.deepEqual(events, ['delta:中文', 'done:'])
})

test('done terminates without EOF and without awaiting a stalled cancel hook', async () => {
  let cancelled = false
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(encoder.encode(done)) },
    cancel() { cancelled = true; return new Promise<void>(() => {}) }
  })
  await consumeEventStream(stream, () => {})
  assert.equal(cancelled, true)
})

test('abort immediately interrupts a pending read with no incoming bytes', async () => {
  const controller = new AbortController()
  let cancelled = false
  const task = consumeEventStream(body([], false, () => { cancelled = true }), () => {}, controller.signal)
  controller.abort()
  await assert.rejects(task, { name: 'AbortError' })
  assert.equal(cancelled, true)
})

test('heartbeat-only traffic does not extend first-content deadline', async () => {
  let timer: ReturnType<typeof setInterval>
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { timer = setInterval(() => controller.enqueue(encoder.encode(': ping\n\n')), 5) },
    cancel() { clearInterval(timer) }
  })
  await assert.rejects(consumeEventStream(stream, () => {}, undefined, { limits: {
    first_token_timeout_ms: 50, total_timeout_ms: 1000
  } }), code('firstTokenTimeout'))
})

test('status events do not extend the idle deadline after real body', async () => {
  let timer: ReturnType<typeof setInterval>
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(delta('正文')))
      timer = setInterval(() => controller.enqueue(encoder.encode(event('status', { phase: 'saving' }))), 5)
    }, cancel() { clearInterval(timer) }
  })
  await assert.rejects(consumeEventStream(stream, () => {}, undefined, { limits: {
    first_token_timeout_ms: 1000, idle_timeout_ms: 50, total_timeout_ms: 1000
  } }), code('idleTimeout'))
})

test('ongoing real content cannot extend the overall deadline', async () => {
  let timer: ReturnType<typeof setInterval>
  const stream = new ReadableStream<Uint8Array>({
    start(controller) { timer = setInterval(() => controller.enqueue(encoder.encode(delta('字'))), 5) },
    cancel() { clearInterval(timer) }
  })
  await assert.rejects(consumeEventStream(stream, () => {}, undefined, { limits: {
    first_token_timeout_ms: 1000, idle_timeout_ms: 1000, total_timeout_ms: 60
  } }), code('totalTimeout'))
})

test('the deadline guard also covers the response-header waiting period', () => {
  const guard = createStreamGuard(undefined, { startedAt: performance.now() - 1000,
    limits: { first_token_timeout_ms: 50, total_timeout_ms: 2000 } })
  assert.equal(guard.signal.aborted, true)
  assert.equal(guard.signal.reason.code, 'firstTokenTimeout')
  guard.dispose()
})

test('meta can supply validated limits but heartbeats and whitespace are not first content', async () => {
  const limits = { ...DEFAULT_STREAM_LIMITS, first_token_timeout_ms: 40 }
  await assert.rejects(consumeEventStream(body([
    event('meta', { conversation_id: 'conv', stream_limits: limits }), delta('   '), ': ping\n\n'
  ], false), () => {}), code('firstTokenTimeout'))
})

test('malformed protocol fields fail closed rather than being coerced to strings', async () => {
  const malformed: [string, unknown][] = [
    ['meta', {}], ['meta', { conversation_id: 1 }], ['delta', { content: [] }], ['delta', { content: null }],
    ['done', { latency_ms: '1' }], ['done', { latency_ms: 1, citations: {} }],
    ['done', { latency_ms: 1, truncated: 'yes' }], ['done', { latency_ms: 1, finish_reason: {} }],
    ['status', { phase: 'unknown' }], ['error', { message: [] }], ['delta', null],
    ['meta', { conversation_id: 'conv', document_scope: [{ document_id: 'doc', filename: 'x', index_version: -1 }] }],
    ['meta', { conversation_id: 'conv', stream_limits: { ...DEFAULT_STREAM_LIMITS, total_timeout_ms: -1 } }]
  ]
  for (const [name, data] of malformed) {
    await assert.rejects(consumeEventStream(body([event(name, data), done]), () => {}), code('invalidStream'))
  }
})

test('single frames and unterminated buffers are bounded in UTF-8 bytes', async () => {
  for (const payload of ['data: ' + 'x'.repeat(70000), delta('文'.repeat(24000)), ': ' + 'x'.repeat(70000)]) {
    await assert.rejects(consumeEventStream(body([payload], false), () => {}), code('streamLimit'))
  }
})

test('a large transport chunk with many small legal events is accepted', async () => {
  let length = 0
  await consumeEventStream(body([delta('x'.repeat(500)).repeat(200) + done]), (name, data) => {
    if (name === 'delta') length += data.content.length
  })
  assert.equal(length, 100000)
})

test('the complete answer has a cumulative limit independent of frame sizes', async () => {
  await assert.rejects(consumeEventStream(body([delta('abcd'), delta('ef'), done]), () => {}, undefined,
    { limits: { max_answer_chars: 5 } }), code('streamLimit'))
})

test('terminal content ignores trailing bytes and clears the timer', async () => {
  const seen: string[] = []
  await consumeEventStream(body([done + 'x'.repeat(100000)]), name => seen.push(name))
  assert.deepEqual(seen, ['done'])
})

function state(status: ChatRequestState['status'] = 'completed'): ChatRequestState {
  return {
    request_id: 'request', conversation_id: 'conversation', user_message_id: 'question', status,
    message: status === 'completed' ? { id: 'answer', role: 'assistant', status: 'completed', content: '原回答[1]',
      reply_to_id: 'question', latency_ms: 10, created_at: '2026-10-27T08:00:00Z',
      citations: [{ source_index: 1, filename: '原文', document_id: 'document', chunk_index: 0, score: 0.9, snippet: '资料' }] } : null,
    truncated: false, finish_reason: 'stop', document_scope: [], scope_revision: 0
  }
}

test('read-only recovery polls processing until the same original answer is saved', async () => {
  let calls = 0
  const result = await recoverRequest('request', async () => state(++calls < 3 ? 'processing' : 'completed'), { intervalMs: 1 })
  assert.equal(calls, 3)
  assert.equal(result?.message?.content, '原回答[1]')
  assert.equal(recoveredCompletion(result!).citations?.[0]?.source_index, 1)
})

test('recovery polling is bounded and keeps processing distinct from success', async () => {
  let calls = 0
  const result = await recoverRequest('request', async () => { calls += 1; return state('processing') }, { intervalMs: 1, maxAttempts: 3 })
  assert.equal(calls, 3)
  assert.equal(result?.status, 'processing')
  assert.equal(result?.message, null)
})

test('unavailable, failed and cancelled requests are not regenerated or polled forever', async () => {
  for (const status of [null, 'failed', 'cancelled'] as const) {
    let calls = 0
    const result = await recoverRequest('request', async () => { calls += 1; return status ? state(status) : null })
    assert.equal(calls, 1)
    assert.equal(result?.status ?? null, status)
  }
})

test('recovery rejects other request identities, wrong conversations and non-final messages', () => {
  for (const changed of [{ request_id: 'another' }, { conversation_id: 'another' },
    { message: { ...state().message!, role: 'user' } }, { message: { ...state().message!, reply_to_id: 'another' } },
    { status: 'processing', message: state().message }]) {
    assert.throws(() => validateRequestState({ ...state(), ...changed }, 'request', 'conversation'), code('invalidStream'))
  }
})

test('stopping during recovery aborts a stalled state query immediately', async () => {
  const controller = new AbortController()
  const pending = recoverRequest('request', () => new Promise(() => {}), { signal: controller.signal })
  controller.abort()
  await assert.rejects(pending, { name: 'AbortError' })
})

test('recovery has an overall timeout even when a loader ignores cancellation', async () => {
  await assert.rejects(recoverRequest('request', () => new Promise(() => {}), { timeoutMs: 30 }), { name: 'TimeoutError' })
})

test('a late recovered result cannot replace a newer session action', async () => {
  const epoch = createSessionEpoch()
  const original = epoch.begin()
  let complete!: (value: unknown) => void
  const pending = recoverRequest('request', () => new Promise(resolve => { complete = resolve }))
  epoch.begin() // 切换会话、新建会话或开始新问题。
  complete(state())
  const recovered = await pending
  let applied = false
  if (epoch.isCurrent(original) && recovered) applied = true
  assert.equal(applied, false)
})

test('long growing Markdown is throttled and only one streaming value is retained', () => {
  let time = 0
  let parses = 0
  const markdown = createStreamingMarkdown(text => { parses += 1; return `<p>${text}</p>` }, () => time)
  const long = 'x'.repeat(40000)
  const initial = markdown.render(long)
  for (let index = 1; index <= 50; index += 1) { time += 1; assert.equal(markdown.render(long + 'y'.repeat(index)), initial) }
  assert.equal(parses, 1)
  time = 250
  assert.match(markdown.render(long + 'latest'), /latest/)
  assert.equal(parses, 2)
  assert.match(markdown.render('new message'), /new message/)
  assert.equal(parses, 3)
  markdown.clear()
  markdown.render('new message')
  assert.equal(parses, 4)
})
