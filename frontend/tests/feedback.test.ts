import assert from 'node:assert/strict'
import test from 'node:test'
import type { AnswerFeedbackAdminDetail, FeedbackCitation } from '../src/api/index.ts'
import {
  buildFeedbackSubmission,
  buildReviewPayload,
  canVerify,
  downloadJsonl,
  feedbackCategoryKey,
  feedbackOutcomeKey,
  feedbackStatusKey,
  getServerMessageId,
  isExportableStatus,
  parseExpectedSourceLine,
  parseLines,
  sourcesFromCitations,
  type FeedbackReviewForm
} from '../src/utils/feedback.ts'

const DOC_ID = '3f2a1c4e-8b7d-4a6f-9c2e-1d5b7a9c0e3f'
const OTHER_DOC_ID = '9b8c7d6e-5f4a-4b3c-8d2e-1f0a9b8c7d6e'

function citation(overrides: Partial<FeedbackCitation> = {}): FeedbackCitation {
  return {
    document_id: DOC_ID,
    filename: 'handbook.pdf',
    chunk_index: 2,
    index_version: 1,
    snippet: 'snippet',
    ...overrides
  }
}

function form(overrides: Partial<FeedbackReviewForm> = {}): FeedbackReviewForm {
  return {
    decision: 'verified',
    correctedOutcome: 'answer',
    selectedCitationIndexes: [],
    extraSources: '',
    requiredPhrases: '',
    forbiddenPhrases: '',
    unknowns: '',
    evidenceNote: '查到的原文位置',
    reviewerNote: '',
    ...overrides
  }
}

function detail(overrides: Partial<AnswerFeedbackAdminDetail> = {}): AnswerFeedbackAdminDetail {
  return {
    id: 'fb-1',
    category: 'inaccurate_answer',
    status: 'pending',
    revision: 3,
    created_at: '2026-01-01T00:00:00Z',
    reviewed_at: null,
    reviewer_name: '',
    mode: 'rag',
    message_id: 'msg-1',
    conversation_id: 'conv-1',
    question: 'question',
    answer: 'answer',
    note: '',
    citations: [],
    invalid_source_ids: [],
    result_stale: false,
    corrected_outcome: null,
    expected_sources: [],
    required_phrases: [],
    forbidden_phrases: [],
    unknowns: [],
    evidence_note: '',
    reviewer_note: '',
    ...overrides
  }
}

test('getServerMessageId only trusts the backend message id', () => {
  assert.equal(getServerMessageId({ serverId: 'server-message-1' }), 'server-message-1')
  assert.equal(getServerMessageId({ serverId: '  server-message-2  ' }), 'server-message-2')
  assert.equal(getServerMessageId({ serverId: null }), null)
  assert.equal(getServerMessageId({}), null)
  // 前端临时 UUID 只存在于 id 上，serverId 缺失时绝不上报。
  assert.equal(getServerMessageId({ serverId: null }), null)
})

test('buildFeedbackSubmission sends only message_id, category and a trimmed note', () => {
  assert.deepEqual(
    buildFeedbackSubmission({ messageId: 'server-message-1', category: 'citation_error', note: '  引用错了，应该是第 3 段  ' }),
    { message_id: 'server-message-1', category: 'citation_error', note: '引用错了，应该是第 3 段' }
  )
  const withoutNote = buildFeedbackSubmission({ messageId: 'server-message-2', category: 'missing_material', note: '   ' })
  assert.deepEqual(withoutNote, { message_id: 'server-message-2', category: 'missing_material' })
  for (const payload of [
    buildFeedbackSubmission({ messageId: 'm', category: 'inaccurate_answer', note: '问题' }),
    withoutNote
  ]) {
    assert.deepEqual(Object.keys(payload).sort(), ['category', 'message_id', ...(('note' in payload) ? ['note'] : [])].sort())
    assert.equal('question' in payload, false)
    assert.equal('answer' in payload, false)
    assert.equal('citations' in payload, false)
  }
})

test('parseLines trims, drops blanks and duplicates, and caps at 20', () => {
  assert.deepEqual(parseLines('  first \n\n second\r\nfirst\n   \nthird  '), ['first', 'second', 'third'])
  assert.deepEqual(parseLines(''), [])
  assert.deepEqual(parseLines(null), [])
  const many = Array.from({ length: 25 }, (_, index) => `line-${index}`).join('\n')
  assert.equal(parseLines(many).length, 20)
  assert.equal(parseLines(many)[19], 'line-19')
  assert.deepEqual(parseLines(many, 0), [])
})

test('parseExpectedSourceLine maps UUIDs to document_id and other lines to filename', () => {
  assert.deepEqual(parseExpectedSourceLine(`  ${DOC_ID}  `), { document_id: DOC_ID })
  assert.deepEqual(parseExpectedSourceLine(' handbook-2026.pdf '), { filename: 'handbook-2026.pdf' })
  assert.equal(parseExpectedSourceLine(''), null)
  assert.equal(parseExpectedSourceLine('   '), null)
  assert.equal(parseExpectedSourceLine('x'.repeat(201)), null)
  assert.deepEqual(parseExpectedSourceLine('x'.repeat(200)), { filename: 'x'.repeat(200) })
})

test('sourcesFromCitations converts only selected citations, deduplicates and pre-selects nothing', () => {
  const citations: FeedbackCitation[] = [
    citation(),
    citation({ document_id: OTHER_DOC_ID, filename: 'policy.pdf', chunk_index: 5 }),
    citation(),
    citation({ document_id: null, filename: 'readme.txt', chunk_index: null, index_version: null })
  ]
  assert.deepEqual(sourcesFromCitations(citations, []), [])
  assert.deepEqual(sourcesFromCitations(citations, [0, 2, 3]), [
    { document_id: DOC_ID, filename: 'handbook.pdf', chunk_index: 2, index_version: 1 },
    { filename: 'readme.txt' }
  ])
  assert.deepEqual(sourcesFromCitations(citations, [1, 99]), [
    { document_id: OTHER_DOC_ID, filename: 'policy.pdf', chunk_index: 5, index_version: 1 }
  ])
})

test('canVerify blocks every required case and allows valid RAG/chat reviews', () => {
  const ragDetail = detail({ citations: [citation()] })
  assert.equal(
    canVerify(detail({ ...ragDetail, result_stale: true }), form({ selectedCitationIndexes: [0] })),
    'feedback.admin.staleBlocking'
  )
  assert.equal(
    canVerify(ragDetail, form({ selectedCitationIndexes: [0], unknowns: '需要业务确认' })),
    'feedback.admin.unresolvedBlocking'
  )
  assert.equal(
    canVerify(ragDetail, form({ correctedOutcome: null, selectedCitationIndexes: [0] })),
    'feedback.admin.outcomeRequired'
  )
  assert.equal(
    canVerify(ragDetail, form({ correctedOutcome: 'answer', selectedCitationIndexes: [] })),
    'feedback.admin.sourceRequired'
  )
  assert.equal(
    canVerify(ragDetail, form({ correctedOutcome: 'refuse', selectedCitationIndexes: [] })),
    null
  )
  assert.equal(
    canVerify(ragDetail, form({ correctedOutcome: 'answer', selectedCitationIndexes: [0] })),
    null
  )
  // RAG 模式即使原回答没有任何 citations，answer 也必须有显式期望来源。
  assert.equal(
    canVerify(detail({ mode: 'rag', citations: [] }), form({ correctedOutcome: 'answer' })),
    'feedback.admin.sourceRequired'
  )
  assert.equal(
    canVerify(
      detail({ mode: 'rag', citations: [] }),
      form({ correctedOutcome: 'answer', extraSources: 'manual.pdf' })
    ),
    null
  )

  const chatDetail = detail({ mode: 'chat', citations: [] })
  assert.equal(
    canVerify(chatDetail, form({ correctedOutcome: 'answer', extraSources: 'manual.pdf' })),
    'feedback.admin.chatNoSources'
  )
  assert.equal(
    canVerify(chatDetail, form({ correctedOutcome: 'answer', evidenceNote: '' })),
    'feedback.admin.evidenceRequired'
  )
  assert.equal(
    canVerify(chatDetail, form({ correctedOutcome: 'answer' })),
    null
  )

  assert.equal(canVerify(ragDetail, form({ decision: 'rejected', selectedCitationIndexes: [] })), 'feedback.admin.noteRequired')
  assert.equal(
    canVerify(ragDetail, form({ decision: 'rejected', reviewerNote: '反馈与原文不符' })),
    null
  )
  assert.equal(
    canVerify(detail({ ...ragDetail, result_stale: true }), form({ decision: 'rejected', reviewerNote: '原因' })),
    'feedback.admin.staleBlocking'
  )
})

test('buildReviewPayload carries expected_revision and clears rejected corrections', () => {
  const current = detail({ revision: 7, citations: [citation()] })
  const verified = buildReviewPayload(current, form({
    selectedCitationIndexes: [0],
    extraSources: `${OTHER_DOC_ID}\nmanual.pdf\nmanual.pdf`,
    requiredPhrases: '必须提到 A\n必须提到 B',
    forbiddenPhrases: '禁止说 C',
    unknowns: '',
    evidenceNote: '  原文第 3 段  ',
    reviewerNote: '  已核对  '
  }))
  assert.equal(verified.expected_revision, 7)
  assert.equal(verified.decision, 'verified')
  assert.equal(verified.corrected_outcome, 'answer')
  assert.deepEqual(verified.expected_sources, [
    { document_id: DOC_ID, filename: 'handbook.pdf', chunk_index: 2, index_version: 1 },
    { document_id: OTHER_DOC_ID },
    { filename: 'manual.pdf' }
  ])
  assert.deepEqual(verified.required_phrases, ['必须提到 A', '必须提到 B'])
  assert.deepEqual(verified.forbidden_phrases, ['禁止说 C'])
  assert.deepEqual(verified.unknowns, [])
  assert.equal(verified.evidence_note, '原文第 3 段')
  assert.equal(verified.reviewer_note, '已核对')

  const rejected = buildReviewPayload(current, form({
    decision: 'rejected',
    selectedCitationIndexes: [0],
    extraSources: OTHER_DOC_ID,
    requiredPhrases: 'A',
    forbiddenPhrases: 'B',
    unknowns: 'C',
    evidenceNote: 'evidence',
    reviewerNote: '  驳回原因  '
  }))
  assert.equal(rejected.expected_revision, 7)
  assert.equal(rejected.decision, 'rejected')
  assert.equal(rejected.corrected_outcome, null)
  assert.deepEqual(rejected.expected_sources, [])
  assert.deepEqual(rejected.required_phrases, [])
  assert.deepEqual(rejected.forbidden_phrases, [])
  assert.deepEqual(rejected.unknowns, [])
  assert.equal(rejected.evidence_note, '')
  assert.equal(rejected.reviewer_note, '驳回原因')
})

test('isExportableStatus only allows verified feedback', () => {
  assert.equal(isExportableStatus('verified'), true)
  assert.equal(isExportableStatus('pending'), false)
  assert.equal(isExportableStatus('rejected'), false)
})

test('translation key helpers map to stable feedback namespaces', () => {
  assert.equal(feedbackCategoryKey('inaccurate_answer'), 'feedback.categories.inaccurate_answer')
  assert.equal(feedbackStatusKey('verified'), 'feedback.status.verified')
  assert.equal(feedbackOutcomeKey('clarify'), 'feedback.outcomes.clarify')
})

test('downloadJsonl builds a JSONL blob and triggers one explicit local download', async () => {
  const originalDocument = (globalThis as { document?: unknown }).document
  const originalCreateObjectURL = URL.createObjectURL
  const originalRevokeObjectURL = URL.revokeObjectURL
  const blobs: Blob[] = []
  const appended: Array<{ download: string }> = []
  const clicks: string[] = []
  ;(globalThis as { document?: unknown }).document = {
    createElement: () => ({
      href: '',
      download: '',
      click() { clicks.push(this.download) },
      remove() { /* no-op */ }
    }),
    body: { appendChild: (element: { download: string }) => { appended.push(element) } }
  }
  ;(URL as unknown as { createObjectURL: (blob: Blob) => string }).createObjectURL = (blob: Blob) => {
    blobs.push(blob)
    return 'blob:feedback-jsonl'
  }
  ;(URL as unknown as { revokeObjectURL: (url: string) => void }).revokeObjectURL = () => undefined
  try {
    assert.equal(downloadJsonl('feedback-verified.jsonl', '{"case":1}\n'), true)
    assert.equal(blobs.length, 1)
    assert.equal(blobs[0].type, 'application/x-ndjson;charset=utf-8')
    assert.deepEqual(clicks, ['feedback-verified.jsonl'])
    assert.equal(appended.length, 1)
    assert.equal(appended[0].download, 'feedback-verified.jsonl')
    // 等 revoke 的 setTimeout 执行完再恢复全局，避免定时器调用到已还原的实现。
    await new Promise(resolve => setTimeout(resolve, 0))
  } finally {
    if (originalDocument === undefined) delete (globalThis as { document?: unknown }).document
    else (globalThis as { document?: unknown }).document = originalDocument
    ;(URL as unknown as { createObjectURL: typeof originalCreateObjectURL }).createObjectURL = originalCreateObjectURL
    ;(URL as unknown as { revokeObjectURL: typeof originalRevokeObjectURL }).revokeObjectURL = originalRevokeObjectURL
  }
})
