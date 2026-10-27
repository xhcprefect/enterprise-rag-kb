import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { createI18n } from 'vue-i18n'
import { messages } from '../src/i18n/messages/index.ts'

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
  const zh = flatten(messages['zh-CN'])
  const en = flatten(messages['en-US'])
  assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort())
  for (const namespace of ['common', 'nav', 'preferences', 'layout', 'errors', 'login', 'chat', 'knowledge', 'documents', 'usage', 'users', 'audit', 'profile']) {
    assert.ok(Object.keys(zh).some(key => key.startsWith(`${namespace}.`)), `Missing ${namespace} messages`)
  }
  for (const [key, value] of [...Object.entries(zh), ...Object.entries(en)]) assert.ok(value.trim(), `Empty translation: ${key}`)
})

test('literal translation keys used by pages, controls, and API errors exist in both catalogs', () => {
  const root = fileURLToPath(new URL('../src/', import.meta.url))
  const files = [join(root, 'App.vue')]
  const collect = (directory: string) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name)
      if (entry.isDirectory()) collect(path)
      else if (/\.(vue|ts)$/.test(entry.name)) files.push(path)
    }
  }
  for (const directory of ['views', 'layouts', 'components', 'api']) collect(join(root, directory))
  for (const locale of ['zh-CN', 'en-US'] as const) {
    const keys = flatten(messages[locale])
    for (const file of files) {
      const source = readFileSync(file, 'utf8')
      for (const match of source.matchAll(/(?<![\w$])t\(\s*['"]([\w.-]+)['"]/g)) {
        assert.ok(keys[match[1]], `${locale}: ${match[1]} used in ${file}`)
      }
    }
  }
})

test('every localized message compiles and resolves without missing keys or invalid message syntax', () => {
  const instance = createI18n({ legacy: false, locale: 'zh-CN', fallbackLocale: false, messages })
  const errors: unknown[][] = []
  const originalError = console.error
  const originalWarn = console.warn
  console.error = (...args) => { errors.push(args) }
  console.warn = (...args) => { errors.push(args) }
  try {
    for (const locale of ['zh-CN', 'en-US'] as const) {
      instance.global.locale.value = locale
      for (const [key, source] of Object.entries(flatten(messages[locale]))) {
        const params = Object.fromEntries([...source.matchAll(/\{(\w+)\}/g)].map(match => [match[1], 2]))
        const translated = instance.global.t(key, params)
        assert.equal(typeof translated, 'string', key)
        assert.ok(translated.length > 0, key)
      }
    }
    assert.deepEqual(errors, [])
  } finally {
    console.error = originalError
    console.warn = originalWarn
    instance.dispose()
  }
})
