import assert from 'node:assert/strict'
import test from 'node:test'
import type { Citation } from '../src/api/index.ts'
import { getCompletedCitations, type ChatCompletion } from '../src/utils/citations.ts'
import { consumeEventStream } from '../src/utils/stream.ts'

const citation: Citation = {
  source_index: 3,
  document_id: 'doc-3',
  filename: 'law.txt',
  chunk_index: 2,
  score: 0.8,
  snippet: '相关条文'
}

function stream(frames: string[]) {
  const encoder = new TextEncoder()
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    }
  })
}

function event(name: string, data: unknown) {
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`
}

test('only a successful completed answer exposes final citations without changing source numbers', () => {
  const completion = { latency_ms: 1, citations: [citation] }
  const result = getCompletedCitations(completion, true, false)
  assert.deepEqual(result, [citation])
  assert.equal(result?.[0].source_index, 3)
})

test('empty answers, aborted requests and missing completion never expose citations', () => {
  const completion = { latency_ms: 1, citations: [citation] }
  assert.equal(getCompletedCitations(completion, false, false), undefined)
  assert.equal(getCompletedCitations(completion, true, true), undefined)
  assert.equal(getCompletedCitations(undefined, true, false), undefined)
})

test('a refusal with empty final citations overrides any candidates sent by an older meta event', async () => {
  let completion: ChatCompletion | undefined
  let answer = ''
  await consumeEventStream(stream([
    event('meta', { conversation_id: 'conv', citations: [citation] }),
    event('delta', { content: '根据现有知识库无法回答。' }),
    event('done', { latency_ms: 1, citations: [] })
  ]), (name, data) => {
    if (name === 'delta') answer += data.content
    if (name === 'done') completion = data
  })
  assert.ok(answer)
  assert.deepEqual(completion?.citations, [])
  assert.equal(getCompletedCitations(completion, !!answer, false), undefined)
})

test('the final SSE payload, rather than meta candidates, supplies the displayed sources', async () => {
  let completion: ChatCompletion | undefined
  await consumeEventStream(stream([
    event('meta', { conversation_id: 'conv', citations: [] }),
    event('delta', { content: '依据见[3]。' }),
    event('done', { latency_ms: 1, citations: [citation] })
  ]), (name, data) => {
    if (name === 'done') completion = data
  })
  assert.deepEqual(getCompletedCitations(completion, true, false), [citation])
})

test('partial or failed streams cannot turn retrieval candidates into citations', async () => {
  let completion: ChatCompletion | undefined
  const onEvent = (name: string, data: any) => { if (name === 'done') completion = data }
  await assert.rejects(consumeEventStream(stream([
    event('meta', { citations: [citation] }),
    event('delta', { content: '部分回答[3]' })
  ]), onEvent), /提前中断/)
  assert.equal(getCompletedCitations(completion, true, false), undefined)
  await consumeEventStream(stream([
    event('meta', { citations: [citation] }),
    event('error', { message: 'failed' })
  ]), onEvent)
  assert.equal(getCompletedCitations(completion, true, false), undefined)
})

test('old servers that omit final citations fail closed rather than reuse meta candidates', () => {
  assert.equal(getCompletedCitations({ latency_ms: 1 }, true, false), undefined)
})
