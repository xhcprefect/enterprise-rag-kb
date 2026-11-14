import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import test, { mock } from 'node:test'
import { computed, createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { i18n } from '../src/i18n/index.ts'
import { messages } from '../src/i18n/messages/index.ts'
import {
  operationsMessages, ensureOperationsMessages, useOperationsI18n
} from '../src/i18n/messages/operations.ts'
import type { WorkerState, WorkerRoleName } from '../src/api/operations.ts'
import {
  createRequestGate, failureLabelKey, formatAge, formatCount, formatWorkerTimestamp,
  normalizeWorkerState, statusTone, workerLabelKey
} from '../src/utils/operations.ts'

const knownStates: WorkerState[] = ['disabled', 'not_started', 'running', 'stale', 'stopped', 'unknown']
const knownRoles: WorkerRoleName[] = ['ingest', 'summary', 'project', 'regression']
/** 原 operations 目录的全部固定键；任何键、文案或语言差异都会改变规范化摘要。 */
const expectedKeys = [
  'operations.aliveCount', 'operations.degraded', 'operations.empty', 'operations.enabled',
  'operations.enabledStatus', 'operations.eyebrow', 'operations.failureCodes.generic',
  'operations.failureCodes.taskFailed', 'operations.failureCodes.workerUnavailable',
  'operations.failureUpdatedAt', 'operations.generatedAt', 'operations.heartbeat',
  'operations.lastFailure', 'operations.loadFailed', 'operations.loading', 'operations.noFailure',
  'operations.notEnabled', 'operations.notEnabledExplanation', 'operations.notRecorded',
  'operations.oldestPending', 'operations.panelHint', 'operations.panelTitle',
  'operations.pending', 'operations.processing', 'operations.refresh', 'operations.role',
  'operations.roles.ingest', 'operations.roles.project', 'operations.roles.regression',
  'operations.roles.summary', 'operations.securityNote', 'operations.securityTitle',
  'operations.staleTasks', 'operations.states.disabled', 'operations.states.not_started',
  'operations.states.running', 'operations.states.stale', 'operations.states.stopped',
  'operations.states.unknown', 'operations.subtitle', 'operations.title', 'operations.unknownValue'
] as const
const expectedCatalogDigest = '5b304401c4a9ce1d10a230861ef8f6f5a030249028b24861732df9f6cb3f1019'

function canonicalCatalog(): string {
  const sorted = (locale: 'zh-CN' | 'en-US') =>
    Object.entries(flatten(operationsMessages[locale])).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
  return JSON.stringify({ 'zh-CN': sorted('zh-CN'), 'en-US': sorted('en-US') })
}

function flatten(value: Record<string, unknown>, prefix = ''): Record<string, string> {
  const entries: Record<string, string> = {}
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (typeof item === 'string') entries[path] = item
    else if (item && typeof item === 'object' && !Array.isArray(item)) Object.assign(entries, flatten(item as Record<string, unknown>, path))
    else assert.fail(`Unexpected message value at ${path}`)
  }
  return entries
}

test('lazy operations catalog keeps the complete bilingual texts and stays out of the global entry', () => {
  // Self-contained expectations: no git repository, HEAD snapshot, or legacy source parsing.
  assert.equal(createHash('sha256').update(canonicalCatalog()).digest('hex'), expectedCatalogDigest)
  assert.deepEqual(Object.keys(flatten(operationsMessages['zh-CN'])).sort(), [...expectedKeys].sort())
  assert.deepEqual(Object.keys(flatten(operationsMessages['en-US'])).sort(), [...expectedKeys].sort())
  for (const locale of ['zh-CN', 'en-US'] as const) {
    const zh = flatten(messages['zh-CN'])
    const en = flatten(messages['en-US'])
    assert.equal(Object.hasOwn(messages[locale], 'operations'), false)
    assert.equal(i18n.global.te('operations.title', locale), false)
    const lazy = flatten(operationsMessages[locale])
    assert.equal(Object.keys(lazy).length, 42)
    for (const [key, text] of Object.entries(lazy)) {
      assert.ok(key.startsWith('operations.'))
      assert.ok(text.trim(), `${locale}: ${key}`)
      assert.equal(zh[key], undefined)
      assert.equal(en[key], undefined)
    }
  }
  assert.deepEqual(
    Object.keys(flatten(operationsMessages['zh-CN'])).sort(),
    Object.keys(flatten(operationsMessages['en-US'])).sort()
  )
})

test('worker state mapping is allow-listed, readable, and never promotes disabled roles', () => {
  for (const state of knownStates) {
    assert.equal(normalizeWorkerState({ enabled: true, state }), state)
    assert.equal(workerLabelKey(state), `operations.states.${state}`)
    assert.ok(statusTone(state))
  }
  assert.equal(normalizeWorkerState({ enabled: true, state: 'future_state' as WorkerState }), 'unknown')
  assert.equal(normalizeWorkerState({ enabled: false, state: 'running' }), 'disabled')
})

test('nullable counts and ages stay unknown rather than becoming zero', () => {
  assert.equal(formatCount(null, 'en-US', 'Unknown'), 'Unknown')
  assert.equal(formatCount(undefined, 'en-US', 'Unknown'), 'Unknown')
  assert.equal(formatCount(0, 'en-US', 'Unknown'), '0')
  assert.equal(formatAge(null, 'en-US', 'Unknown'), 'Unknown')
  assert.equal(formatAge(-1, 'en-US', 'Unknown'), 'Unknown')
  assert.equal(formatAge(0, 'en-US', 'Unknown'), '0s')
  assert.equal(formatAge(62, 'en-US', 'Unknown'), '1m 2s')
  assert.equal(formatAge(62, 'zh-CN', '未知'), '1分2秒')
})

test('timestamps format valid ISO values and use an explicit fallback for missing or invalid values', () => {
  const formatted = formatWorkerTimestamp('2026-06-07T12:34:56Z', 'en-US', 'Not recorded')
  assert.match(formatted, /2026/)
  assert.notEqual(formatted, '2026-06-07T12:34:56Z')
  assert.equal(formatWorkerTimestamp(null, 'en-US', 'Not recorded'), 'Not recorded')
  assert.equal(formatWorkerTimestamp('not-a-date', 'en-US', 'Not recorded'), 'Not recorded')
})

test('failure labels expose only safe fixed categories', () => {
  assert.equal(failureLabelKey('task_failed'), 'operations.failureCodes.taskFailed')
  assert.equal(failureLabelKey('worker_unavailable'), 'operations.failureCodes.workerUnavailable')
  assert.equal(failureLabelKey('secret/path/user@example.com'), 'operations.failureCodes.generic')
  assert.equal(failureLabelKey('<script>alert(1)</script>'), 'operations.failureCodes.generic')
  for (const key of [
    workerLabelKey('unknown'), failureLabelKey('task_failed'), failureLabelKey('worker_unavailable'), failureLabelKey('future_code')
  ]) {
    assert.ok(['zh-CN', 'en-US'].every(locale => flatten(operationsMessages[locale])[key]), `${key} must exist in both lazy catalogs`)
  }
  for (const role of knownRoles) assert.ok(['zh-CN', 'en-US'].every(locale => flatten(operationsMessages[locale])[`operations.roles.${role}`]))
})

test('a late response cannot replace the latest refresh result', async () => {
  const gate = createRequestGate()
  let releaseOld!: (value: string) => void
  let releaseNew!: (value: string) => void
  const oldResponse = new Promise<string>(resolve => { releaseOld = resolve })
  const newResponse = new Promise<string>(resolve => { releaseNew = resolve })
  let displayed = ''
  const apply = async (requestId: number, response: Promise<string>) => {
    const result = await response
    if (gate.isCurrent(requestId)) displayed = result
  }

  const oldId = gate.begin()
  const oldApply = apply(oldId, oldResponse)
  const newId = gate.begin()
  const newApply = apply(newId, newResponse)
  releaseNew('new snapshot')
  await newApply
  releaseOld('old snapshot')
  await oldApply
  assert.equal(displayed, 'new snapshot')

  const pendingId = gate.begin()
  gate.invalidate()
  assert.equal(gate.isCurrent(pendingId), false)
})

test('page setup merges the real global composer once and switches both languages', async () => {
  let runtime: ReturnType<typeof useOperationsI18n> | undefined
  const previousLocale = i18n.global.locale.value
  const before = JSON.stringify(['zh-CN', 'en-US'].map(locale => i18n.global.getLocaleMessage(locale)))
  // This test owns the process-global i18n instance; establish a clean first-visit state.
  for (const locale of ['zh-CN', 'en-US'] as const) {
    const catalog = { ...i18n.global.getLocaleMessage(locale) }
    delete catalog.operations
    i18n.global.setLocaleMessage(locale, catalog)
  }
  for (const locale of ['zh-CN', 'en-US'] as const) assert.equal(i18n.global.te('operations.title', locale), false)

  const merge = mock.method(i18n.global, 'mergeLocaleMessage')
  const app = createSSRApp({
    setup() {
      runtime = useOperationsI18n()
      ensureOperationsMessages()
      const label = computed(() => runtime!.t('operations.title'))
      return () => h('span', label.value)
    }
  })
  app.use(i18n)
  try {
    assert.equal(await renderToString(app), '<span>运行状态</span>')
    assert.ok(runtime)
    assert.equal(runtime!.locale, i18n.global.locale, 'the page setup must use the global composer locale')
    assert.match(readFileSync(new URL('../src/views/OperationsView.vue', import.meta.url), 'utf8'),
      /const \{ t, locale \} = useOperationsI18n\(\)/)
    assert.equal(merge.mock.callCount(), 2)
    assert.deepEqual(merge.mock.calls.map(call => call.arguments[0]), ['zh-CN', 'en-US'])
    const withoutLazy = ['zh-CN', 'en-US'].map(locale => {
      const catalog = { ...i18n.global.getLocaleMessage(locale) }
      delete catalog.operations
      return catalog
    })
    assert.equal(JSON.stringify(withoutLazy), before)

    for (const [key, text] of Object.entries(flatten(operationsMessages['zh-CN']))) {
      assert.equal(i18n.global.te(key, 'zh-CN'), true, key)
      const params = Object.fromEntries([...text.matchAll(/\{(\w+)\}/g)].map(match => [match[1], 'TIME']))
      assert.equal(i18n.global.t(key, params, { locale: 'zh-CN' }), text.replace(/\{(\w+)\}/g, 'TIME'), key)
    }
    for (const [key, text] of Object.entries(flatten(operationsMessages['en-US']))) {
      assert.equal(i18n.global.te(key, 'en-US'), true, key)
      const params = Object.fromEntries([...text.matchAll(/\{(\w+)\}/g)].map(match => [match[1], 'TIME']))
      assert.equal(i18n.global.t(key, params, { locale: 'en-US' }), text.replace(/\{(\w+)\}/g, 'TIME'), key)
    }
    for (const locale of ['en-US', 'zh-CN', 'en-US'] as const) {
      i18n.global.locale.value = locale
      assert.equal(runtime!.locale.value, locale)
      assert.equal(runtime!.t('operations.title'), locale === 'zh-CN' ? '运行状态' : 'Worker status')
      assert.equal(runtime!.t('operations.loadFailed'), locale === 'zh-CN'
        ? '运行状态获取失败。当前没有显示旧数据，请检查连接后重试。'
        : 'Could not load worker status. Old data is hidden; check the connection and retry.')
      for (const state of knownStates) {
        const key = workerLabelKey(state)
        assert.equal(runtime!.t(key), flatten(operationsMessages[locale])[key])
        assert.notEqual(runtime!.t(key), key)
      }
      for (const code of ['task_failed', 'worker_unavailable', 'future_code']) {
        const key = failureLabelKey(code)
        assert.equal(runtime!.t(key), flatten(operationsMessages[locale])[key])
        assert.notEqual(runtime!.t(key), key)
      }
      assert.equal(runtime!.t('operations.generatedAt', { time: 'TIME' }), locale === 'zh-CN' ? '数据生成于 TIME' : 'Data generated at TIME')
    }
    assert.equal(runtime!.locale.value, 'en-US')
  } finally {
    i18n.global.locale.value = previousLocale
    merge.mock.restore()
  }
})
