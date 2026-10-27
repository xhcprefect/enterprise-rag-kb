import assert from 'node:assert/strict'
import test from 'node:test'
import { consumeEventStream } from '../src/utils/stream.ts'

const encode = new TextEncoder()
function stream(parts: string[]) {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const part of parts) controller.enqueue(encode.encode(part))
      controller.close()
    }
  })
}

test('SSE handles fragmented CRLF and a terminal event', async () => {
  const events: string[] = []
  await consumeEventStream(stream(['event: delta\r', '\ndata: {"content":"你好"}\r\n\r', '\nevent: done\r\ndata: {"latency_ms":1}\r\n\r\n']),
    (name, data) => events.push(`${name}:${data.content ?? ''}`))
  assert.deepEqual(events, ['delta:你好', 'done:'])
})

test('SSE EOF without done is an error, including after partial content', async () => {
  await assert.rejects(consumeEventStream(stream(['event: delta\ndata: {"content":"partial"}\n\n']), () => {}), /提前中断/)
})

test('SSE server error is terminal', async () => {
  const events: string[] = []
  await consumeEventStream(stream(['event: error\ndata: {"message":"failed"}\n\n']), (name) => events.push(name))
  assert.deepEqual(events, ['error'])
})

test('SSE invalid JSON fails instead of disappearing silently', async () => {
  await assert.rejects(consumeEventStream(stream(['event: delta\ndata: not-json\n\n']), () => {}), /格式异常/)
})

test('SSE propagates consumer exceptions', async () => {
  await assert.rejects(consumeEventStream(stream(['event: done\ndata: {"latency_ms":1}\n\n']), () => { throw new Error('consumer') }), /consumer/)
})
