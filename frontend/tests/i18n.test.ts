import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { createI18n } from 'vue-i18n'
import { messages } from '../src/i18n/messages/index.ts'
import { operationsMessages } from '../src/i18n/messages/operations.ts'
import { failureLabelKey, workerLabelKey } from '../src/utils/operations.ts'

/** 唯一允许按需加载的 namespace，键必须精确命中该懒加载目录。 */
const lazyCatalogs = { operations: operationsMessages } as const
const lazyNamespaces = Object.keys(lazyCatalogs) as Array<keyof typeof lazyCatalogs>
const workerStates = ['disabled', 'not_started', 'running', 'stale', 'stopped', 'unknown'] as const
const workerRoles = ['ingest', 'summary', 'project', 'regression'] as const

function catalog(locale: 'zh-CN' | 'en-US'): Record<string, string> {
  const global = flatten(messages[locale])
  for (const namespace of lazyNamespaces) {
    assert.equal(Object.hasOwn(messages[locale], namespace), false,
      `${locale}: ${namespace} must stay out of the global catalog`)
  }
  const lazy = Object.fromEntries(lazyNamespaces.flatMap(namespace =>
    Object.entries(flatten(lazyCatalogs[namespace][locale])).map(([key, value]) => [key, value])))
  return { ...global, ...lazy }
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

test('Chinese and English catalogs cover the same pages and have identical message keys', () => {
  const zh = catalog('zh-CN')
  const en = catalog('en-US')
  assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort())
  for (const locale of ['zh-CN', 'en-US'] as const) {
    assert.equal(Object.hasOwn(messages[locale], 'operations'), false, `${locale}: operations must be lazy`)
    for (const namespace of lazyNamespaces) assert.ok(Object.keys(catalog(locale)).some(key => key.startsWith(`${namespace}.`)), `Missing ${namespace} messages`)
  }
  for (const namespace of ['common', 'nav', 'preferences', 'layout', 'errors', 'login', 'chat', 'knowledge', 'documents', 'usage', 'users', 'audit', 'profile', 'feedback']) {
    assert.ok(Object.keys(zh).some(key => key.startsWith(`${namespace}.`)), `Missing ${namespace} messages`)
  }
  for (const [key, value] of [...Object.entries(zh), ...Object.entries(en)]) assert.ok(value.trim(), `Empty translation: ${key}`)
})

test('the lazy operations namespace is exact and absent from the global entry graph', () => {
  for (const locale of ['zh-CN', 'en-US'] as const) {
    assert.deepEqual(Object.keys(flatten(operationsMessages[locale])).every(key => key.startsWith('operations.')), true)
    assert.equal(Object.keys(catalog(locale)).filter(key => key.startsWith('operations.')).length, 42)
    assert.equal(Object.hasOwn(messages[locale], 'operations'), false)
  }
  const entry = readFileSync(new URL('../src/i18n/messages/index.ts', import.meta.url), 'utf8')
  const page = readFileSync(new URL('../src/views/OperationsView.vue', import.meta.url), 'utf8')
  assert.equal(entry.includes('operations'), false)
  assert.match(page, /from '@\/i18n\/messages\/operations'/)
  assert.match(page, /useOperationsI18n\(\)/)
})

const allKeys = (locale: 'zh-CN' | 'en-US') => {
  const globalKeys = flatten(messages[locale])
  const lazyKeys = Object.fromEntries(lazyNamespaces.flatMap(namespace =>
    Object.entries(flatten(lazyCatalogs[namespace][locale]))))
  return { globalKeys, lazyKeys, all: { ...globalKeys, ...lazyKeys } }
}


test('literal translation keys used by pages, controls, API errors and lazy catalogs resolve exactly', () => {
  const root = fileURLToPath(new URL('../src/', import.meta.url))
  const page = readFileSync(new URL('../src/views/OperationsView.vue', import.meta.url), 'utf8')
  const utilsSource = readFileSync(new URL('../src/utils/operations.ts', import.meta.url), 'utf8')
  const files = [join(root, 'App.vue')]
  const collect = (directory: string) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name)
      if (entry.isDirectory()) collect(path)
      else if (/\.(vue|ts)$/.test(entry.name)) files.push(path)
    }
  }
  for (const directory of ['views', 'layouts', 'components', 'api', 'utils']) collect(join(root, directory))

  // The page keeps full t(...) keys rather than introducing a new alias.
  assert.match(page, /const \{ t, locale \} = useOperationsI18n\(\)/)
  for (const locale of ['zh-CN', 'en-US'] as const) {
    const { globalKeys, lazyKeys, all } = allKeys(locale)
    assert.deepEqual(Object.keys(flatten(messages[locale])).sort(), Object.keys(globalKeys).sort())
    assert.equal(Object.keys(all).length, Object.keys(globalKeys).length + Object.keys(lazyKeys).length)
    const seenDirect = new Set<string>()
    for (const file of files) {
      const source = readFileSync(file, 'utf8')
      for (const match of source.matchAll(/(?<![\w$])t\(\s*['"]([\w.-]+)['"]/g)) {
        const key = match[1]
        seenDirect.add(key)
        const namespace = key.split('.')[0] as keyof typeof lazyCatalogs
        const exact = lazyNamespaces.includes(namespace)
          ? flatten(lazyCatalogs[namespace][locale])
          : globalKeys
        assert.ok(exact[key], `${locale}: ${key} used in ${file} is not in its exact catalog`)
      }
    }

    // Conditional expressions and utility return values are real page keys too; scan
    // operations literals from both sources and verify the actual dynamic utility keys.
    const operationsLiterals = new Set<string>()
    for (const [source, pattern] of [
      [page, /['"`](operations\.[\w.-]+)['"`]/g],
      [utilsSource, /['"`](operations\.[\w.-]+)['"`]/g]
    ] as const) {
      for (const match of source.matchAll(pattern)) operationsLiterals.add(match[1])
    }
    for (const key of operationsLiterals) assert.ok(lazyKeys[key], `${locale}: ${key} is not in exact operations catalog`)
    for (const key of [
      ...workerStates.map(workerLabelKey),
      ...['task_failed', 'worker_unavailable', 'future_code'].map(failureLabelKey)
    ]) assert.ok(lazyKeys[key], `${locale}: runtime key ${key} is not in exact operations catalog`)
    for (const state of workerStates) assert.ok(lazyKeys[`operations.states.${state}`])
    for (const role of workerRoles) assert.ok(lazyKeys[`operations.roles.${role}`])
    for (const key of [
      'operations.enabled', 'operations.notEnabled', 'operations.unknownValue', 'operations.notRecorded',
      'operations.failureCodes.generic', 'operations.eyebrow'
    ]) assert.ok(operationsLiterals.has(key), `${locale}: missing guarded page key ${key}`)
    for (const key of ['operations.title', 'operations.loadFailed']) {
      assert.ok(seenDirect.has(key), `${locale}: ${key} must still be found by the t(...) scan`)
    }
  }
})

test('every localized message compiles and resolves without missing keys or invalid message syntax', () => {
  for (const [catalogName, source] of [['global', messages], ['operations', operationsMessages]] as const) {
    const instance = createI18n({ legacy: false, locale: 'zh-CN', fallbackLocale: false, messages: source })
    const errors: unknown[][] = []
    const originalError = console.error
    const originalWarn = console.warn
    console.error = (...args) => { errors.push(args) }
    console.warn = (...args) => { errors.push(args) }
    try {
      for (const locale of ['zh-CN', 'en-US'] as const) {
        instance.global.locale.value = locale
        for (const [key, text] of Object.entries(flatten(source[locale]))) {
          const params = Object.fromEntries([...text.matchAll(/\{(\w+)\}/g)].map(match => [match[1], 2]))
          const translated = instance.global.t(key, params)
          assert.equal(typeof translated, 'string', `${catalogName}: ${locale}: ${key}`)
          assert.ok(translated.length > 0, `${catalogName}: ${locale}: ${key}`)
          assert.notEqual(translated, key, `${catalogName}: ${locale}: ${key}`)
        }
      }
      assert.deepEqual(errors, [], `${catalogName}: unexpected translation compiler warnings`)
    } finally {
      console.error = originalError
      console.warn = originalWarn
      instance.dispose()
    }
  }
})
