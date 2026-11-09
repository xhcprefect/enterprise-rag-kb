/**
 * 检索诊断纯逻辑测试：问题/范围校验、阶段与分数映射、i18n 动态键完整性。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { messages } from '../src/i18n/messages/index.ts'
import {
  DIAGNOSTICS_MAX_DOCUMENTS,
  DIAGNOSTICS_QUESTION_MAX,
  buildDiagnosticsRequest,
  diagnosticsChunkRange,
  diagnosticsLifecycleKey,
  diagnosticsMethodKey,
  diagnosticsMethodTagType,
  diagnosticsRetrievableKey,
  diagnosticsScoreEntries,
  diagnosticsScoreText,
  diagnosticsStageNameKey,
  diagnosticsStageReasonKey,
  diagnosticsStageStatusKey,
  diagnosticsStageTagType,
  normalizeDiagnosticsDocumentIds,
  uniqueDiagnosticsDocumentIds
} from '../src/utils/retrievalDiagnostics.ts'

function flatten(value: Record<string, unknown>, prefix = ''): Record<string, string> {
  const entries: Record<string, string> = {}
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (typeof item === 'string') entries[path] = item
    else if (item && typeof item === 'object' && !Array.isArray(item)) {
      Object.assign(entries, flatten(item as Record<string, unknown>, path))
    }
  }
  return entries
}

test('document ids are trimmed, deduplicated and order-preserving', () => {
  assert.deepEqual(
    uniqueDiagnosticsDocumentIds([' a ', 'b', 'a', '', '  ', 'b', 'c']),
    ['a', 'b', 'c']
  )
  assert.deepEqual(uniqueDiagnosticsDocumentIds(null), [])
  assert.deepEqual(uniqueDiagnosticsDocumentIds(undefined), [])
  assert.deepEqual(normalizeDiagnosticsDocumentIds(['a', 'b', 'c', 'd', 'e', 'f']), [
    'a',
    'b',
    'c',
    'd',
    'e'
  ])
})

test('request validates the trimmed question length and the 5-document cap', () => {
  assert.deepEqual(buildDiagnosticsRequest('  ', []), { status: 'questionEmpty' })
  assert.deepEqual(buildDiagnosticsRequest('', ['a']), { status: 'questionEmpty' })
  assert.deepEqual(buildDiagnosticsRequest('  年度报告  ', [' a ', 'a', 'b']), {
    status: 'ok',
    body: { question: '年度报告', document_ids: ['a', 'b'] }
  })
  assert.deepEqual(
    buildDiagnosticsRequest('q'.repeat(DIAGNOSTICS_QUESTION_MAX), []),
    {
      status: 'ok',
      body: { question: 'q'.repeat(DIAGNOSTICS_QUESTION_MAX), document_ids: [] }
    }
  )
  assert.deepEqual(
    buildDiagnosticsRequest('q'.repeat(DIAGNOSTICS_QUESTION_MAX + 1), []),
    { status: 'questionTooLong' }
  )
  assert.deepEqual(
    buildDiagnosticsRequest('q', ['a', 'b', 'c', 'd', 'e', 'f']),
    { status: 'tooManyDocuments' }
  )
  assert.deepEqual(
    buildDiagnosticsRequest('q', ['a', 'b', 'c', 'd', 'e']),
    { status: 'ok', body: { question: 'q', document_ids: ['a', 'b', 'c', 'd', 'e'] } }
  )
  assert.equal(DIAGNOSTICS_MAX_DOCUMENTS, 5)
})

test('stage names, statuses and methods map known values and fall back safely', () => {
  for (const name of ['vector', 'keyword', 'fusion', 'rerank', 'select', 'neighbors']) {
    assert.equal(
      diagnosticsStageNameKey(name),
      `knowledge.diagnostics.stages.names.${name}`
    )
  }
  assert.equal(diagnosticsStageNameKey('future_stage'), 'knowledge.diagnostics.stages.names.unknown')
  assert.equal(diagnosticsStageNameKey(null), 'knowledge.diagnostics.stages.names.unknown')

  assert.equal(diagnosticsStageStatusKey('skipped'), 'knowledge.diagnostics.stages.statuses.skipped')
  assert.equal(
    diagnosticsStageStatusKey('unexpected'),
    'knowledge.diagnostics.stages.statuses.unknown'
  )
  assert.equal(diagnosticsStageTagType('ok'), 'success')
  assert.equal(diagnosticsStageTagType('error'), 'danger')
  assert.equal(diagnosticsStageTagType('skipped'), 'info')
  assert.equal(diagnosticsStageTagType('fallback'), 'warning')
  assert.equal(diagnosticsStageTagType('other'), 'info')

  // reason 只翻译冻结契约里的已知值；未知/空值不得原样展示，也不猜测含义。
  assert.equal(
    diagnosticsStageReasonKey('disabled'),
    'knowledge.diagnostics.stages.reasons.disabled'
  )
  assert.equal(
    diagnosticsStageReasonKey('not_configured'),
    'knowledge.diagnostics.stages.reasons.not_configured'
  )
  assert.equal(
    diagnosticsStageReasonKey('  disabled  '),
    'knowledge.diagnostics.stages.reasons.disabled'
  )
  for (const reason of ['future_reason', '', null, undefined]) {
    assert.equal(
      diagnosticsStageReasonKey(reason),
      'knowledge.diagnostics.stages.reasons.unknown',
      String(reason)
    )
  }

  assert.equal(
    diagnosticsMethodKey('hybrid'),
    'knowledge.diagnostics.candidates.methods.hybrid'
  )
  assert.equal(
    diagnosticsMethodKey(null),
    'knowledge.diagnostics.candidates.methods.unknown'
  )
  assert.equal(diagnosticsMethodTagType('vector'), 'primary')
  assert.equal(diagnosticsMethodTagType('keyword'), 'warning')
  assert.equal(diagnosticsMethodTagType('hybrid'), 'success')
  assert.equal(diagnosticsMethodTagType('other'), 'info')
})

test('lifecycle and retrievable states never pretend to be active or retrievable', () => {
  assert.equal(diagnosticsLifecycleKey('active'), 'documents.lifecycle.active')
  assert.equal(diagnosticsLifecycleKey('superseded'), 'documents.lifecycle.superseded')
  assert.equal(diagnosticsLifecycleKey('future_state'), 'knowledge.diagnostics.unknown')
  assert.equal(diagnosticsLifecycleKey(null), 'knowledge.diagnostics.unknown')

  assert.equal(diagnosticsRetrievableKey(true), 'knowledge.diagnostics.yes')
  assert.equal(diagnosticsRetrievableKey(false), 'knowledge.diagnostics.no')
  assert.equal(diagnosticsRetrievableKey(null), 'knowledge.diagnostics.unknown')
})

test('scores keep known keys in a fixed order, append unknown keys, and drop non-finite values', () => {
  const entries = diagnosticsScoreEntries({
    rerank_score: 0.87,
    custom_score: 12.5,
    distance: 0.2,
    broken: Number.NaN,
    infinite: Number.POSITIVE_INFINITY,
    fusion_score: 0.5
  })
  assert.deepEqual(entries.map((entry) => entry.key), [
    'distance',
    'fusion_score',
    'rerank_score',
    'custom_score'
  ])
  assert.equal(entries[0].labelKey, 'knowledge.diagnostics.scores.distance')
  assert.equal(entries[3].labelKey, '')
  assert.equal(entries[3].value, 12.5)
  assert.deepEqual(diagnosticsScoreEntries(null), [])
  assert.deepEqual(diagnosticsScoreEntries({}), [])

  assert.equal(diagnosticsScoreText(0.123456), '0.1235')
  assert.equal(diagnosticsScoreText(3), '3')
  assert.equal(diagnosticsScoreText(Number.NaN), '—')
})

test('chunk ranges only use non-negative integer bounds', () => {
  assert.deepEqual(diagnosticsChunkRange(3, 5), { start: 3, end: 5 })
  assert.deepEqual(diagnosticsChunkRange(3, 3), { start: 3, end: 3 })
  // end 缺失/小于 start 时退化为单块，而不是展示反向范围
  assert.deepEqual(diagnosticsChunkRange(3, null), { start: 3, end: 3 })
  assert.deepEqual(diagnosticsChunkRange(3, 2), { start: 3, end: 3 })
  assert.equal(diagnosticsChunkRange(null, 5), null)
  assert.equal(diagnosticsChunkRange(-1, 5), null)
  assert.equal(diagnosticsChunkRange(1.5, 2), null)
})

test('every dynamic translation key used by the diagnostics UI exists in both locales', () => {
  const keys = new Set<string>()
  for (const name of ['vector', 'keyword', 'fusion', 'rerank', 'select', 'neighbors', 'future']) {
    keys.add(diagnosticsStageNameKey(name))
  }
  for (const status of ['ok', 'error', 'skipped', 'fallback', 'future']) {
    keys.add(diagnosticsStageStatusKey(status))
  }
  for (const reason of ['disabled', 'not_configured', 'future', null]) {
    keys.add(diagnosticsStageReasonKey(reason))
  }
  for (const method of ['vector', 'keyword', 'hybrid', null]) {
    keys.add(diagnosticsMethodKey(method))
  }
  for (const state of ['active', 'scheduled', 'expired', 'disabled', 'superseded', 'future', null]) {
    keys.add(diagnosticsLifecycleKey(state))
  }
  for (const value of [true, false, null]) keys.add(diagnosticsRetrievableKey(value))
  for (const entry of diagnosticsScoreEntries({
    distance: 1,
    fusion_score: 2,
    rerank_score: 3,
    citation_score: 4
  })) {
    keys.add(entry.labelKey)
  }
  for (const locale of ['zh-CN', 'en-US'] as const) {
    const catalog = flatten(messages[locale])
    for (const key of keys) assert.ok(catalog[key], `${locale}: missing ${key}`)
  }
})
