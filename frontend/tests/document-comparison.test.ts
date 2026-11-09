/**
 * 版本对比纯逻辑测试：候选过滤、行过滤/分页、reason 映射、指纹与请求体严格校验、
 * 提交条件、身份核对以及两语言动态文案完整性。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { messages } from '../src/i18n/messages/index.ts'
import type {
  ComparisonDocument,
  ComparisonRow,
  ComparisonRowKind,
  DocumentComparison,
  DocumentItem
} from '../src/api/index.ts'
import {
  COMPARISON_CANDIDATE_PAGE_SIZE,
  COMPARISON_FINGERPRINT_RE,
  COMPARISON_PAGE_SIZE,
  availableCandidates,
  buildComparisonSupersedeBody,
  canConfirmComparison,
  comparisonHasDifferences,
  comparisonPageCount,
  comparisonPageSlice,
  comparisonReasonKey,
  comparisonRowKey,
  comparisonRowKindKey,
  comparisonRowTagType,
  filterComparisonRows,
  hasValidComparisonFingerprint,
  invalidateComparison,
  isComparisonCurrent
} from '../src/utils/documentComparison.ts'

const FINGERPRINT = 'a'.repeat(64)

function candidate(id: string, over: Partial<DocumentItem> = {}): DocumentItem {
  return {
    id,
    kb_id: 'kb-1',
    filename: `${id}.md`,
    file_ext: '.md',
    file_size: 10,
    status: 'ready',
    error_message: null,
    chunk_count: 2,
    char_count: 20,
    progress: 100,
    cancel_requested: false,
    created_at: '2026-01-01T00:00:00Z',
    index_version: 1,
    maintainer: '',
    last_reviewed_at: null,
    valid_from: null,
    valid_until: null,
    enabled: true,
    superseded_at: null,
    superseded_by_id: null,
    governance_revision: 0,
    lifecycle_state: 'active',
    retrievable: true,
    ...over
  }
}

function row(
  kind: ComparisonRowKind,
  originalIndex: number | null,
  replacementIndex: number | null
): ComparisonRow {
  const unit = (index: number) => ({
    chunk_index: index,
    line_index: index,
    text: `text-${index}`,
    location: {}
  })
  return {
    kind,
    original: originalIndex === null ? null : unit(originalIndex),
    replacement: replacementIndex === null ? null : unit(replacementIndex)
  }
}

function comparisonDoc(): ComparisonDocument {
  return {
    id: 'doc-a',
    filename: '旧资料.md',
    index_version: 1,
    governance_revision: 3,
    chunk_count: 2,
    char_count: 20,
    lifecycle_state: 'active',
    retrievable: true
  }
}

function preview(over: Partial<DocumentComparison> = {}): DocumentComparison {
  return {
    comparison_version: 1,
    basis: 'ingested_chunks',
    comparable: true,
    reason: 'ok',
    original: comparisonDoc(),
    replacement: { ...comparisonDoc(), id: 'doc-b', filename: '新资料.md' },
    summary: { added: 1, removed: 0, modified: 0, unchanged: 1 },
    rows: [row('added', null, 0), row('unchanged', 1, 1)],
    comparison_fingerprint: FINGERPRINT,
    ...over
  }
}

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

test('available candidates exclude self, non-ready, non-retrievable and superseded rows', () => {
  const items = [
    candidate('self'),
    candidate('ready-ok'),
    candidate('pending', { status: 'pending' }),
    candidate('processing', { status: 'processing' }),
    candidate('failed', { status: 'failed' }),
    candidate('not-retrievable', { retrievable: false }),
    candidate('disabled', { lifecycle_state: 'disabled', enabled: false, retrievable: false }),
    candidate('superseded', { superseded_at: '2026-01-01T00:00:00Z', superseded_by_id: 'self' }),
    candidate('superseded-link-only', { superseded_by_id: 'self' })
  ]
  assert.deepEqual(
    availableCandidates(items, ' self ').map((item) => item.id),
    ['ready-ok']
  )
  // 顺序保持稳定；重复 id 只保留第一次
  const duplicated = [candidate('b'), candidate('a'), candidate('b')]
  assert.deepEqual(
    availableCandidates(duplicated, '').map((item) => item.id),
    ['b', 'a']
  )
  // 空/null/unknown 输入安全
  assert.deepEqual(availableCandidates(null, 'self'), [])
  assert.deepEqual(availableCandidates([], 'self'), [])
  assert.deepEqual(
    availableCandidates([{ ...candidate('x'), id: '' } as DocumentItem], 'self').length,
    0
  )
})

test('filterComparisonRows keeps only changed rows on demand and always returns a new array', () => {
  const rows = [
    row('unchanged', 0, 0),
    row('added', null, 1),
    row('removed', 2, null),
    row('modified', 3, 3)
  ]
  assert.deepEqual(filterComparisonRows(rows, true).map((item) => item.kind), [
    'added',
    'removed',
    'modified'
  ])
  const all = filterComparisonRows(rows, false)
  assert.deepEqual(all, rows)
  assert.notEqual(all, rows)
  assert.deepEqual(filterComparisonRows(null, true), [])
  assert.deepEqual(filterComparisonRows(undefined, false), [])
})

test('row keys are stable and unique across kinds/positions', () => {
  const keys = [
    comparisonRowKey(row('unchanged', 0, 0), 0),
    comparisonRowKey(row('added', null, 1), 1),
    comparisonRowKey(row('removed', 2, null), 2),
    comparisonRowKey(row('modified', 3, 3), 3)
  ]
  assert.equal(new Set(keys).size, keys.length)
  assert.equal(comparisonRowKey(row('modified', 3, 3), 3), 'modified:3:3:3:3:3')
  // 相同行在不同分页位置的 key 不冲突
  const sample = row('unchanged', 4, 4)
  assert.notEqual(comparisonRowKey(sample, 4), comparisonRowKey(sample, 54))
})

test('pagination helpers handle empty, normal and out-of-range pages', () => {
  assert.equal(COMPARISON_PAGE_SIZE, 50)
  assert.equal(COMPARISON_CANDIDATE_PAGE_SIZE, 100)
  assert.equal(comparisonPageCount(0), 0)
  assert.equal(comparisonPageCount(-1), 0)
  assert.equal(comparisonPageCount(Number.NaN), 0)
  assert.equal(comparisonPageCount(1), 1)
  assert.equal(comparisonPageCount(50), 1)
  assert.equal(comparisonPageCount(51), 2)
  assert.equal(comparisonPageCount(51, 0), 0)

  const rows = Array.from({ length: 51 }, (_, index) => index)
  assert.deepEqual(comparisonPageSlice(rows, 1).length, 50)
  assert.deepEqual(comparisonPageSlice(rows, 2), [50])
  // 越界页码夹到有效范围；非法页码按第一页
  assert.equal(comparisonPageSlice(rows, 99).length, 1)
  assert.equal(comparisonPageSlice(rows, 0).length, 50)
  assert.equal(comparisonPageSlice(rows, 1.5).length, 50)
  assert.deepEqual(comparisonPageSlice([], 1), [])
  assert.deepEqual(comparisonPageSlice(null, 1), [])
})

test('reason mapping only translates known contract values and falls back safely', () => {
  for (const reason of [
    'ok',
    'not_ready',
    'not_eligible',
    'incomplete',
    'empty_document',
    'too_many_chunks',
    'too_many_chars',
    'too_many_units'
  ]) {
    assert.equal(comparisonReasonKey(reason), `documents.comparison.reason.${reason}`)
  }
  assert.equal(comparisonReasonKey('  not_ready  '), 'documents.comparison.reason.not_ready')
  for (const reason of ['future_reason', '', null, undefined]) {
    assert.equal(comparisonReasonKey(reason), 'documents.comparison.reason.unknown')
  }
})

test('row kind mapping and tag colors are conservative for unknown values', () => {
  assert.equal(comparisonRowKindKey('added'), 'documents.comparison.rows.added')
  assert.equal(comparisonRowKindKey('removed'), 'documents.comparison.rows.removed')
  assert.equal(comparisonRowKindKey('modified'), 'documents.comparison.rows.modified')
  assert.equal(comparisonRowKindKey('unchanged'), 'documents.comparison.rows.unchanged')
  assert.equal(comparisonRowKindKey('future_kind'), 'documents.comparison.rows.unknown')
  assert.equal(comparisonRowKindKey(null), 'documents.comparison.rows.unknown')
  assert.equal(comparisonRowTagType('added'), 'success')
  assert.equal(comparisonRowTagType('removed'), 'danger')
  assert.equal(comparisonRowTagType('modified'), 'warning')
  assert.equal(comparisonRowTagType('unchanged'), 'info')
  assert.equal(comparisonRowTagType('future'), 'info')
})

test('fingerprint must be exactly 64 lowercase hex characters', () => {
  assert.equal(COMPARISON_FINGERPRINT_RE.test(FINGERPRINT), true)
  assert.equal(hasValidComparisonFingerprint(FINGERPRINT), true)
  for (const bad of [
    '',
    FINGERPRINT.toUpperCase(),
    FINGERPRINT.slice(0, 63),
    `${FINGERPRINT}0`,
    ` ${FINGERPRINT}`,
    'g'.repeat(64),
    null,
    undefined,
    123
  ]) {
    assert.equal(hasValidComparisonFingerprint(bad), false, String(bad))
  }
})

test('supersede body validates revision, replacement and fingerprint without clamping', () => {
  assert.deepEqual(
    buildComparisonSupersedeBody(3, ' doc-b ', FINGERPRINT, 'doc-a'),
    {
      status: 'ok',
      body: {
        expected_revision: 3,
        replacement_document_id: 'doc-b',
        comparison_fingerprint: FINGERPRINT
      }
    }
  )
  // 非负整数边界
  assert.equal(buildComparisonSupersedeBody(0, 'doc-b', FINGERPRINT, 'doc-a').status, 'ok')
  for (const bad of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, true, '3', undefined, null]) {
    assert.deepEqual(
      buildComparisonSupersedeBody(bad as never, 'doc-b', FINGERPRINT, 'doc-a'),
      { status: 'invalidRevision' }
    )
  }
  // 空替代者 / 自身 / 仅空白
  for (const replacement of ['', '   ', 'doc-a', ' doc-a ']) {
    assert.deepEqual(
      buildComparisonSupersedeBody(1, replacement, FINGERPRINT, 'doc-a'),
      { status: 'invalidReplacement' },
      replacement
    )
  }
  // 指纹必须在构造请求体时也严格校验
  for (const bad of ['', 'A'.repeat(64), 'a'.repeat(63), null, 42]) {
    assert.deepEqual(
      buildComparisonSupersedeBody(1, 'doc-b', bad, 'doc-a'),
      { status: 'invalidFingerprint' },
      String(bad)
    )
  }
})

test('canConfirmComparison requires comparable ok preview, valid fingerprint, rows, ack and idle', () => {
  const base = preview()
  assert.equal(canConfirmComparison(base, true, false), true)
  assert.equal(canConfirmComparison(base, false, false), false)
  assert.equal(canConfirmComparison(base, true, true), false)
  assert.equal(canConfirmComparison(null, true, false), false)
  assert.equal(canConfirmComparison({ ...base, comparable: false }, true, false), false)
  assert.equal(canConfirmComparison({ ...base, reason: 'not_ready' }, true, false), false)
  assert.equal(
    canConfirmComparison({ ...base, comparison_fingerprint: 'bad' }, true, false),
    false
  )
  assert.equal(
    canConfirmComparison({ ...base, comparison_fingerprint: null }, true, false),
    false
  )
  // rows 未加载（null/undefined）不能确认
  assert.equal(
    canConfirmComparison({ ...base, rows: undefined as never }, true, false),
    false
  )
  // 空 rows 是已加载的数组：结构上可确认，但调用方会因不可比较/指纹而禁用
  assert.equal(canConfirmComparison({ ...base, rows: [] }, true, false), true)
})

test('comparisonHasDifferences distinguishes changed rows from empty rows', () => {
  assert.equal(comparisonHasDifferences([row('unchanged', 0, 0)]), false)
  assert.equal(comparisonHasDifferences([row('unchanged', 0, 0), row('added', null, 1)]), true)
  assert.equal(comparisonHasDifferences([]), false)
  assert.equal(comparisonHasDifferences(null), false)
})

test('isComparisonCurrent requires both response identities to match the selection', () => {
  const doc = preview()
  const selection = { kbId: 'kb-1', docId: 'doc-a', replacementId: 'doc-b' }
  assert.equal(isComparisonCurrent(doc, selection), true)
  assert.equal(isComparisonCurrent(doc, { ...selection, docId: 'doc-x' }), false)
  assert.equal(isComparisonCurrent(doc, { ...selection, replacementId: 'doc-x' }), false)
  assert.equal(
    isComparisonCurrent({ ...doc, replacement: { ...doc.replacement, id: 'doc-a' } }, selection),
    false
  )
  assert.equal(isComparisonCurrent(null, selection), false)
  assert.equal(isComparisonCurrent(doc, null), false)
  // 空选择（未选 KB/文档/候选）一律不认
  assert.equal(isComparisonCurrent(doc, { kbId: '', docId: '', replacementId: '' }), false)
})

test('invalidateComparison always yields null', () => {
  assert.equal(invalidateComparison(preview()), null)
  assert.equal(invalidateComparison(null), null)
  assert.equal(invalidateComparison(undefined), null)
})

test('every dynamic reason and row-kind key exists in both locales', () => {
  const keys = new Set<string>()
  for (const reason of [
    'ok',
    'not_ready',
    'not_eligible',
    'incomplete',
    'empty_document',
    'too_many_chunks',
    'too_many_chars',
    'too_many_units',
    'future',
    null
  ]) {
    keys.add(comparisonReasonKey(reason))
  }
  for (const kind of ['added', 'removed', 'modified', 'unchanged', 'future', null]) {
    keys.add(comparisonRowKindKey(kind))
  }
  for (const locale of ['zh-CN', 'en-US'] as const) {
    const catalog = flatten(messages[locale])
    for (const key of keys) assert.ok(catalog[key], `${locale}: missing ${key}`)
  }
})
