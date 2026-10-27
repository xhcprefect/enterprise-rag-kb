import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import type { ModelPrice, UsageMetrics } from '../src/api/usage.ts'
import { messages } from '../src/i18n/messages/index.ts'
import {
  buildPricePayload,
  canToggleSeries,
  chartPoint,
  coerceGranularity,
  compactNumber,
  coverageState,
  createDialogRequestGate,
  createLatestGuard,
  daysInclusive,
  decimalToMicros,
  defaultUsageScope,
  draftSignature,
  formatBucketLabel,
  formatCny,
  formatPercent,
  formatRate,
  formatUsageDay,
  granularityAvailability,
  hoverIndexForX,
  isEditorUntouched,
  isPriceConfigured,
  isRevisionConflict,
  niceTicks,
  percentOf,
  presetRange,
  recommendedGranularity,
  resolveUsageScope,
  shanghaiToday,
  shiftIsoDay,
  splitRuns,
  tokenFieldValue,
  tokenValue,
  upsertPrice,
  usageCoverage,
  validatePriceDraft,
  validateUsageRange,
  type UsagePriceDraft
} from '../src/utils/usage.ts'

function metrics(overrides: Partial<UsageMetrics> = {}): UsageMetrics {
  return {
    requests: 0,
    successful_requests: 0,
    failed_requests: 0,
    input_tokens: 0,
    output_tokens: 0,
    cached_input_tokens: 0,
    total_tokens: 0,
    usage_known_requests: 0,
    usage_missing_requests: 0,
    priced_requests: 0,
    unpriced_requests: 0,
    estimated_cost: null,
    input_cost: null,
    output_cost: null,
    ...overrides
  }
}

function price(overrides: Partial<ModelPrice> = {}): ModelPrice {
  return {
    id: null,
    provider: 'openai',
    model: 'gpt-test',
    kind: 'chat',
    currency: 'CNY',
    input_per_million: null,
    output_per_million: null,
    cached_input_per_million: null,
    revision: 0,
    ...overrides
  }
}

function draft(overrides: Partial<UsagePriceDraft> = {}): UsagePriceDraft {
  return { enabled: true, input: '', output: '', cached: '', ...overrides }
}

test('business days follow Asia/Shanghai even when the browser is on UTC', () => {
  assert.equal(shanghaiToday(new Date('2026-10-26T15:59:59Z')), '2026-10-26')
  assert.equal(shanghaiToday(new Date('2026-10-26T16:00:00Z')), '2026-10-27')
  assert.deepEqual(presetRange('today', '2026-10-27'), { start: '2026-10-27', end: '2026-10-27' })
  assert.deepEqual(presetRange('7d', '2026-10-27'), { start: '2026-10-21', end: '2026-10-27' })
  assert.deepEqual(presetRange('30d', '2026-10-27'), { start: '2026-09-28', end: '2026-10-27' })
  assert.deepEqual(presetRange('90d', '2026-10-27'), { start: '2026-07-30', end: '2026-10-27' })
  assert.equal(shiftIsoDay('2024-02-28', 1), '2024-02-29')
  assert.equal(shiftIsoDay('2024-02-29', 1), '2024-03-01')
  assert.equal(daysInclusive('2024-02-28', '2024-03-01'), 3)
  assert.equal(daysInclusive('bad', '2024-03-01'), null)
})

test('range validation enforces 366 days, 31 hourly days, and inclusive natural days', () => {
  assert.deepEqual(validateUsageRange({ start: '2026-01-01', end: '2026-12-31' }, 'day'), { ok: true })
  assert.deepEqual(validateUsageRange({ start: '2024-01-01', end: '2024-12-31' }, 'day'), { ok: true })
  assert.deepEqual(validateUsageRange({ start: '2026-01-01', end: '2027-01-02' }, 'day'), { ok: false, code: 'span' })
  assert.deepEqual(validateUsageRange({ start: '2026-10-01', end: '2026-10-31' }, 'hour'), { ok: true })
  assert.deepEqual(validateUsageRange({ start: '2026-10-01', end: '2026-11-01' }, 'hour'), { ok: false, code: 'hourSpan' })
  assert.deepEqual(validateUsageRange({ start: '2026-10-27', end: '2026-10-26' }, 'day'), { ok: false, code: 'order' })
  assert.deepEqual(validateUsageRange({ start: '2026-02-30', end: '2026-03-01' }, 'day'), { ok: false, code: 'invalid' })
  assert.deepEqual(validateUsageRange({ start: '1999-12-31', end: '2000-01-01' }, 'day'), { ok: false, code: 'bounds' })
  assert.deepEqual(validateUsageRange({ start: '', end: '2026-10-27' }, 'day'), { ok: false, code: 'required' })
  assert.deepEqual(validateUsageRange({ start: '2026/10/01', end: '2026/10/27' }, 'day'), { ok: false, code: 'format' })

  const longRange = { start: '2026-07-30', end: '2026-10-27' }
  assert.deepEqual(granularityAvailability(longRange), { hour: false, day: true, month: true })
  assert.equal(coerceGranularity(longRange, 'hour'), 'day')
  assert.equal(coerceGranularity({ start: '2026-10-27', end: '2026-10-27' }, 'hour'), 'hour')
  assert.equal(recommendedGranularity('today'), 'hour')
  assert.equal(recommendedGranularity('7d'), 'day')
})

test('date labels are stable across browser time zones', () => {
  assert.match(formatUsageDay('2026-10-27', 'en-US'), /2026/)
  assert.match(formatUsageDay('2026-10-27', 'zh-CN'), /2026/)
  assert.equal(formatUsageDay('2026-02-30', 'en-US'), '—')
  assert.match(formatBucketLabel('2026-10-01T00:00:00+08:00', 'month', 'en-US'), /2026/)
  assert.match(formatBucketLabel('2026-10-01T00:00:00+08:00', 'day', 'en-US'), /10/)
  assert.match(formatBucketLabel('2026-10-01T00:00:00+08:00', 'hour', 'en-US'), /10/)
  assert.equal(formatBucketLabel('not-a-date', 'day', 'en-US'), '—')
})

test('CNY formatting is string based, distinguishes null from a configured free price, and groups thousands', () => {
  assert.equal(formatCny(null), '—')
  assert.equal(formatCny(undefined), '—')
  assert.equal(formatCny(''), '—')
  assert.equal(formatCny('nope'), '—')
  assert.equal(formatCny('0.0000000000'), '¥0.00')
  // 非零小额不能被舍入成“免费”的 0；显示小于当前精度的阈值。
  assert.equal(formatCny('0.0000004'), '<¥0.000001')
  assert.equal(formatCny('0.0000004000'), '<¥0.000001')
  assert.equal(formatCny('0.0000004', { maxFractionDigits: 2 }), '<¥0.01')
  assert.equal(formatCny('0.0000000001'), '<¥0.000001')
  assert.equal(formatCny('0.0000005'), '¥0.000001')
  assert.equal(formatCny('0.000001'), '¥0.000001')
  assert.equal(formatCny('12.3456789'), '¥12.345679')
  assert.equal(formatCny('999.9999999'), '¥1,000.00')
  assert.equal(formatCny('1234567.5'), '¥1,234,567.50')
  assert.equal(formatCny('12345678901234567890.123456'), '¥12,345,678,901,234,567,890.123456')
  assert.equal(formatCny(null, { empty: 'unknown' }), 'unknown')
  assert.equal(formatRate('1.500000'), '¥1.5')
  assert.equal(formatRate(null), '—')

  assert.equal(decimalToMicros('1000000.000000'), 1_000_000_000_000n)
  assert.equal(decimalToMicros('1000000.000001'), 1_000_000_000_001n)
  assert.equal(decimalToMicros('1.0000001'), null)
  assert.equal(decimalToMicros('-1'), null)
})

test('unknown usage is never presented as zero tokens or zero cost', () => {
  const empty = metrics()
  assert.equal(tokenValue(empty), 0)
  assert.equal(coverageState(empty), 'empty')
  assert.deepEqual(usageCoverage(empty), { known: 0, total: 0, ratio: null })
  assert.equal(formatPercent(null), '—')

  const unknown = metrics({ requests: 3, usage_missing_requests: 3 })
  assert.equal(tokenValue(unknown), null)
  assert.equal(tokenFieldValue(unknown, 'input_tokens'), null)
  assert.equal(coverageState(unknown), 'none')
  assert.equal(formatPercent(usageCoverage(unknown).ratio), '—')

  const partial = metrics({
    requests: 4,
    usage_known_requests: 3,
    usage_missing_requests: 1,
    input_tokens: 90,
    output_tokens: 10,
    total_tokens: 100,
    priced_requests: 2,
    unpriced_requests: 2,
    estimated_cost: '0.1200000000'
  })
  assert.equal(tokenValue(partial), 100)
  assert.equal(tokenFieldValue(partial, 'input_tokens'), 90)
  assert.equal(coverageState(partial), 'partial')
  assert.equal(formatPercent(usageCoverage(partial).ratio), '75%')

  const full = metrics({ requests: 1, usage_known_requests: 1, priced_requests: 1, estimated_cost: '0.0000000000' })
  assert.equal(coverageState(full), 'full')
  assert.equal(percentOf(1, 1), 100)
  assert.equal(percentOf(1, 0), 0)
})

test('chart points break on null and mark unknown buckets without drawing them as zero', () => {
  const emptyBucket = metrics()
  assert.deepEqual(chartPoint(emptyBucket, 'estimated_cost'), { value: null, unknown: false, text: null })
  assert.deepEqual(chartPoint(emptyBucket, 'total_tokens'), { value: 0, unknown: false })

  const unknownBucket = metrics({ requests: 2, usage_missing_requests: 2 })
  assert.deepEqual(chartPoint(unknownBucket, 'estimated_cost'), { value: null, unknown: true, text: null })
  assert.deepEqual(chartPoint(unknownBucket, 'total_tokens'), { value: null, unknown: true })

  const freeBucket = metrics({
    requests: 1,
    usage_known_requests: 1,
    priced_requests: 1,
    estimated_cost: '0.0000000000'
  })
  assert.deepEqual(chartPoint(freeBucket, 'estimated_cost'), {
    value: 0,
    unknown: false,
    text: '0.0000000000'
  })

  // 已计价部分仍要画出，但必须标记该桶有数据未计入。
  const partialBucket = metrics({
    requests: 3,
    usage_known_requests: 2,
    usage_missing_requests: 1,
    input_tokens: 10,
    output_tokens: 5,
    total_tokens: 15,
    priced_requests: 1,
    unpriced_requests: 2,
    estimated_cost: '0.0100000000'
  })
  assert.deepEqual(chartPoint(partialBucket, 'total_tokens'), { value: 15, unknown: false, partial: true })
  assert.deepEqual(chartPoint(partialBucket, 'estimated_cost'), {
    value: 0.01,
    unknown: false,
    text: '0.0100000000',
    partial: true
  })

  const runs = splitRuns([
    { value: 1, unknown: false },
    { value: null, unknown: false },
    { value: 2, unknown: false },
    { value: 3, unknown: false },
    { value: null, unknown: true }
  ])
  assert.deepEqual(runs.map(run => run.map(entry => entry.index)), [[0], [2, 3]])

  assert.deepEqual(niceTicks(0).ticks, [0, 0.25, 0.5, 0.75, 1])
  assert.ok(niceTicks(1234).max >= 1234)
  assert.equal(compactNumber(1200), '1.2K')
  assert.equal(compactNumber(2_000_000), '2M')
  assert.equal(compactNumber(950), '950')
  assert.equal(compactNumber(0), '0')
  // 小于 0.01 的费用刻度不能全部变成 0。
  assert.equal(compactNumber(0.004), '0.004')
  assert.equal(compactNumber(0.00012), '0.00012')
  assert.equal(compactNumber(0.000012), '1.2e-5')
  assert.equal(compactNumber(0.0000002), '2e-7')
  assert.equal(compactNumber(-0.25), '-0.25')
  const tinyTicks = niceTicks(0.000004).ticks.map(compactNumber)
  assert.equal(new Set(tinyTicks).size, tinyTicks.length, `tiny ticks must stay distinct: ${tinyTicks.join(',')}`)
  // 今日费用约 ¥0.0028 时，Y 轴刻度必须可区分而不是全为 0。
  assert.deepEqual(niceTicks(0.0028).ticks.map(compactNumber), ['0', '0.001', '0.002', '0.003'])

  assert.equal(hoverIndexForX(0, 10, 100, 5), 0)
  assert.equal(hoverIndexForX(500, 10, 100, 5), 4)
  assert.equal(hoverIndexForX(60, 10, 100, 5), 2)
  assert.equal(hoverIndexForX(60, 10, 100, 1), 0)
  assert.equal(hoverIndexForX(60, 10, 100, 0), null)
})

test('price validation enforces paired rates, numeric bounds, and zero output for embeddings', () => {
  assert.deepEqual(validatePriceDraft('chat', draft({ enabled: false, input: 'junk' })), { ok: true })
  assert.deepEqual(validatePriceDraft('chat', draft()), { ok: false, code: 'inputRequired', field: 'input' })
  assert.deepEqual(validatePriceDraft('chat', draft({ input: '1' })), { ok: false, code: 'outputRequired', field: 'output' })
  assert.deepEqual(validatePriceDraft('chat', draft({ input: '1.2345678', output: '1' })), {
    ok: false,
    code: 'rateFormat',
    field: 'input'
  })
  assert.deepEqual(validatePriceDraft('chat', draft({ input: '1000001', output: '1' })), {
    ok: false,
    code: 'rateRange',
    field: 'input'
  })
  assert.deepEqual(validatePriceDraft('chat', draft({ input: '1000000', output: '0.000001' })), { ok: true })
  assert.deepEqual(validatePriceDraft('chat', draft({ input: '0', output: '0', cached: 'nope' })), {
    ok: false,
    code: 'rateFormat',
    field: 'cached'
  })
  assert.deepEqual(validatePriceDraft('chat', draft({ input: '0', output: '0', cached: '0.5' })), { ok: true })

  assert.deepEqual(validatePriceDraft('embedding', draft({ input: '0', output: '5' })), {
    ok: false,
    code: 'outputZero',
    field: 'output'
  })
  assert.deepEqual(validatePriceDraft('embedding', draft({ input: '0', output: '0' })), { ok: true })
  assert.deepEqual(validatePriceDraft('rerank', draft({ input: '0.1' })), { ok: true })
})

test('price payload clears all rates, normalizes decimals, and keeps the revision snapshot', () => {
  const existing = price({
    id: 'p1',
    input_per_million: '1.000000',
    output_per_million: '2.000000',
    cached_input_per_million: '0.500000',
    revision: 7
  })
  assert.deepEqual(buildPricePayload(existing, draft({ enabled: false })), {
    provider: 'openai',
    model: 'gpt-test',
    kind: 'chat',
    input_per_million: null,
    output_per_million: null,
    cached_input_per_million: null,
    expected_revision: 7
  })

  assert.deepEqual(buildPricePayload(existing, draft({
    input: '0001.500000',
    output: '2.000000',
    cached: ''
  })), {
    provider: 'openai',
    model: 'gpt-test',
    kind: 'chat',
    input_per_million: '1.5',
    output_per_million: '2',
    cached_input_per_million: null,
    expected_revision: 7
  })

  const embedding = price({ kind: 'embedding', revision: 0 })
  assert.deepEqual(buildPricePayload(embedding, draft({ input: '0', output: '5' })), {
    provider: 'openai',
    model: 'gpt-test',
    kind: 'embedding',
    input_per_million: '0',
    output_per_million: '0',
    cached_input_per_million: null,
    expected_revision: 0
  })
})

test('price list updates replace the matching model without losing virtual entries', () => {
  const virtual = price()
  const embedding = price({ provider: 'local', model: 'bge', kind: 'embedding' })
  const items = [virtual, embedding]
  assert.equal(isPriceConfigured(virtual), false)
  assert.equal(isPriceConfigured(price({ input_per_million: '0', output_per_million: '0' })), true)

  const saved = { ...virtual, id: 'p2', input_per_million: '1', output_per_million: '2', revision: 1 }
  const next = upsertPrice(items, saved)
  assert.equal(next.length, 2)
  assert.equal(next.find(item => item.model === 'gpt-test')?.revision, 1)
  assert.equal(next[0].provider, 'local', 'sorted output keeps providers deterministic')

  assert.equal(isRevisionConflict({ response: { status: 409 } }), true)
  assert.equal(isRevisionConflict({ response: { status: 500 } }), false)
  assert.equal(isRevisionConflict(new Error('plain')), false)
})

test('scope defaults and permissions force members to their own usage', () => {
  assert.equal(defaultUsageScope(true), 'all')
  assert.equal(defaultUsageScope(false), 'mine')
  assert.equal(resolveUsageScope(true, 'all'), 'all')
  assert.equal(resolveUsageScope(true, 'mine'), 'mine')
  assert.equal(resolveUsageScope(false, 'all'), 'mine')
  assert.equal(resolveUsageScope(false, 'mine'), 'mine')
})

test('the latest-request guard rejects stale responses and stops updates after disposal', () => {
  const guard = createLatestGuard()
  const first = guard.begin()
  assert.equal(guard.isCurrent(first), true)
  const second = guard.begin()
  assert.equal(guard.isCurrent(first), false)
  assert.equal(guard.isCurrent(second), true)
  guard.dispose()
  assert.equal(guard.disposed, true)
  assert.equal(guard.isCurrent(second), false)
  const after = guard.begin()
  assert.equal(guard.isCurrent(after), false)
})

test('legend lets a hidden series return but never hides the last visible one', () => {
  assert.equal(canToggleSeries('cost', ['cost'], 0), true)
  assert.equal(canToggleSeries('cost', ['cost'], 1), true, 'a hidden series must be re-showable')
  assert.equal(canToggleSeries('input', ['output'], 1), false)
  assert.equal(canToggleSeries('input', [], 1), false)
  assert.equal(canToggleSeries('input', ['output'], 2), true)
  assert.equal(canToggleSeries('input', [], 2), true)
})

test('dialog gate makes close/reopen and newer loads win over stale loads and saves', () => {
  const gate = createDialogRequestGate()
  const firstLoad = gate.beginLoad()
  const secondLoad = gate.beginLoad()
  assert.equal(gate.isLoadCurrent(firstLoad), false)
  assert.equal(gate.isLoadCurrent(secondLoad), true)

  const save = gate.beginSave()
  assert.equal(gate.isSaveCurrent(save), true)
  gate.invalidate()
  assert.equal(gate.isLoadCurrent(secondLoad), false, 'closing the dialog invalidates the load')
  assert.equal(gate.isSaveCurrent(save), false, 'closing the dialog invalidates the save')

  const reopened = gate.beginLoad()
  assert.equal(gate.isLoadCurrent(reopened), true)
  const staleSave = gate.beginSave()
  const freshLoad = gate.beginLoad()
  assert.equal(gate.isSaveCurrent(staleSave), false, 'a newer load supersedes an in-flight save')
  assert.equal(gate.isLoadCurrent(freshLoad), true)
  assert.equal(gate.isLoadCurrent(reopened), false)

  const newestSave = gate.beginSave()
  assert.equal(gate.isSaveCurrent(newestSave), true)
  assert.equal(gate.isSaveCurrent(staleSave), false, 'only the newest save result is applied')
})

test('draft signatures detect edits while ignoring insignificant whitespace', () => {
  assert.equal(draftSignature(draft({ input: ' 1.5 ' })), draftSignature(draft({ input: '1.5' })))
  assert.notEqual(draftSignature(draft({ input: '1.5' })), draftSignature(draft({ input: '1.6' })))
  assert.notEqual(draftSignature(draft({ enabled: false })), draftSignature(draft({ enabled: true })))
})

test('late price responses apply only when the model and draft are untouched', () => {
  assert.equal(isEditorUntouched({ key: 'openai:gpt:a', draft: '1\u00001' }, { key: 'openai:gpt:a', draft: '1\u00001' }), true)
  assert.equal(isEditorUntouched({ key: 'openai:gpt:a', draft: '1' }, { key: 'openai:gpt:b', draft: '1' }), false)
  assert.equal(isEditorUntouched({ key: 'openai:gpt:a', draft: '1' }, { key: 'openai:gpt:a', draft: '2' }), false)
})

test('usage copy describes per-call tracking with verifiable history and avoids doubled granularity', () => {
  for (const locale of ['zh-CN', 'en-US'] as const) {
    const usage = messages[locale].usage
    // 后端会从旧生成补录带明确 usage 的历史记录，不能宣称更早调用全部不含。
    assert.match(usage.notices.tracking, /历史|historical/, locale)
    assert.match(usage.notices.tracking, /核验|verifiable/, locale)
    assert.doesNotMatch(usage.notices.tracking, /不在统计内|not included/, locale)
    // 粒度标签本身已带“按”，描述里不能再拼出“按按天”。
    assert.ok(usage.chart.tokensDesc.includes('{granularity}'), locale)
    assert.equal(usage.chart.tokensDesc.includes('按{granularity}'), false, locale)
  }
})

test('chart legend and price dialog wire the stale-response and conflict guards into the UI', () => {
  const chart = readFileSync(new URL('../src/components/UsageTrendChart.vue', import.meta.url), 'utf8')
  assert.match(chart, /:disabled="!canToggleSeries\(/)
  assert.equal(
    chart.includes('hiddenKeys.includes(item.key) && visibleSeries.length'),
    false,
    'a hidden series must stay re-showable'
  )

  const dialog = readFileSync(new URL('../src/components/ModelPriceDialog.vue', import.meta.url), 'utf8')
  assert.match(dialog, /load\(true, true\)/, 'conflict refresh must keep the conflict banner')
  assert.match(dialog, /selectItem\(target, !keepConflict\)/, 'refreshing must not clear the conflict')
  assert.match(dialog, /gate\.isLoadCurrent\(ticket\)/)
  assert.match(dialog, /gate\.isSaveCurrent\(ticket\)/)
  assert.match(dialog, /const untouched = isEditorUntouched\(before, \{/, 'late loads must not override a switched model or draft')
  assert.equal(
    dialog.match(/isEditorUntouched\(/g)?.length,
    2,
    'both load and save responses must guard against late overwrites'
  )
})
