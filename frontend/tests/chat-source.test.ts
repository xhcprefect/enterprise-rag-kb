import assert from 'node:assert/strict'
import test from 'node:test'
import { createChatRequestId } from '../src/utils/chatRequest.ts'
import { pdfSourceUrl, sourceLocationLabels } from '../src/utils/sourceLocation.ts'

const translate = (key: string, values?: Record<string, string | number>) => `${key}:${JSON.stringify(values)}`

test('request identifiers are UUIDs even without randomUUID in an HTTP context', () => {
  const id = createChatRequestId(bytes => bytes.fill(0))
  assert.equal(id, '00000000-0000-4000-8000-000000000000')
  assert.match(createChatRequestId(), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
  assert.notEqual(createChatRequestId(), createChatRequestId())
})

test('source positions distinguish pages, sheets, table rows and text lines', () => {
  const labels = sourceLocationLabels({ section: '第一章', sheet: '预算', page_start: 2, page_end: 4,
    row_start: 8, row_end: 8, line_start: 20, line_end: 25 }, translate)
  assert.equal(labels[0], '第一章')
  assert.ok(labels.some(label => label.startsWith('chat.source.sheet:')))
  assert.ok(labels.some(label => label === 'chat.source.pages:{"start":2,"end":4}'))
  assert.ok(labels.some(label => label === 'chat.source.row:{"index":8}'))
  assert.ok(labels.some(label => label.startsWith('chat.source.lines:')))
})

test('legacy or invalid positions do not invent page numbers from chunk indices', () => {
  assert.deepEqual(sourceLocationLabels({}, translate), [])
  assert.deepEqual(sourceLocationLabels({ page_start: -1, row_start: 0, line_start: 1.2 }, translate), [])
  assert.deepEqual(sourceLocationLabels({ page_start: 3, page_end: 1 }, translate), ['chat.source.page:{"index":3}'])
})

test('PDF page fragments only accept positive integer pages', () => {
  assert.equal(pdfSourceUrl('blob:source', 5), 'blob:source#page=5')
  for (const page of [undefined, null, 0, -1, 1.5]) assert.equal(pdfSourceUrl('blob:source', page), 'blob:source')
})
