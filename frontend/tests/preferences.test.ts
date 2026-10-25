import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import {
  LOCALE_KEY, THEME_KEY, normalizeLocale, normalizeTheme, readPreferences, resolveTheme
} from '../src/utils/preferences.ts'

const bootstrap = readFileSync(new URL('../public/preferences.js', import.meta.url), 'utf8')
function runBootstrap(saved: Record<string, string>, browserLanguage: string, systemDark: boolean, blockedKey?: string) {
  const root = {
    dark: false, lang: '', style: { colorScheme: '' }, dataset: { theme: '' },
    classList: { toggle(_class: string, value: boolean) { root.dark = value } }
  }
  runInNewContext(bootstrap, {
    navigator: { language: browserLanguage },
    localStorage: { getItem(key: string) { if (blockedKey === '*' || blockedKey === key) throw new Error('blocked'); return saved[key] ?? null } },
    window: { matchMedia: () => ({ matches: systemDark }) }, document: { documentElement: root }
  })
  return root
}

test('theme and locale preferences accept supported values and normalize invalid input', () => {
  assert.equal(normalizeTheme('light'), 'light')
  assert.equal(normalizeTheme('dark'), 'dark')
  for (const input of ['system', 'invalid', null, undefined, 'DARK']) assert.equal(normalizeTheme(input), 'system')
  assert.equal(normalizeLocale('zh-CN', 'en-GB'), 'zh-CN')
  assert.equal(normalizeLocale('en-US', 'zh-TW'), 'en-US')
  assert.equal(normalizeLocale('invalid', 'en-GB'), 'en-US')
  assert.equal(normalizeLocale(null, 'zh-TW'), 'zh-CN')
  assert.equal(normalizeLocale(null, 'fr-FR'), 'zh-CN')
})

test('manual theme takes priority over the system and system mode follows either color scheme', () => {
  assert.equal(resolveTheme('light', true), 'light')
  assert.equal(resolveTheme('dark', false), 'dark')
  assert.equal(resolveTheme('system', true), 'dark')
  assert.equal(resolveTheme('system', false), 'light')
})

test('storage failures and corrupted preference values fall back safely', () => {
  assert.deepEqual(readPreferences({ getItem() { throw new Error('denied') } }, 'en-GB'), { theme: 'system', locale: 'en-US' })
  assert.deepEqual(readPreferences({ getItem() { return 'bad value' } }, 'zh-CN'), { theme: 'system', locale: 'zh-CN' })
  const saved = { [THEME_KEY]: 'dark', [LOCALE_KEY]: 'en-US' }
  assert.deepEqual(readPreferences({ getItem: key => saved[key] ?? null }, 'zh-CN'), { theme: 'dark', locale: 'en-US' })
})

test('pre-render bootstrap applies saved theme and language before the application mounts', () => {
  const dark = runBootstrap({ [THEME_KEY]: 'dark', [LOCALE_KEY]: 'en-US' }, 'zh-CN', false)
  assert.equal(dark.dark, true)
  assert.equal(dark.style.colorScheme, 'dark')
  assert.equal(dark.dataset.theme, 'dark')
  assert.equal(dark.lang, 'en-US')
  const light = runBootstrap({ [THEME_KEY]: 'light', [LOCALE_KEY]: 'zh-CN' }, 'en-US', true)
  assert.equal(light.dark, false)
  assert.equal(light.lang, 'zh-CN')
})

test('bootstrap and application preference resolution agree on defaults, invalid values and blocked storage', () => {
  for (const language of ['zh-CN', 'en-US', 'en-GB', 'fr-FR']) {
    for (const systemDark of [true, false]) {
      for (const saved of [{}, { [THEME_KEY]: 'broken', [LOCALE_KEY]: 'broken' }, { [THEME_KEY]: 'system' }]) {
        const root = runBootstrap(saved, language, systemDark)
        const preference = readPreferences({ getItem: key => saved[key] ?? null }, language)
        assert.equal(root.dataset.theme, resolveTheme(preference.theme, systemDark))
        assert.equal(root.lang, preference.locale)
      }
    }
  }
  assert.equal(runBootstrap({}, 'en-US', true, '*').dataset.theme, 'dark')
  assert.equal(runBootstrap({ [LOCALE_KEY]: 'en-US' }, 'zh-CN', false, THEME_KEY).lang, 'en-US')
})

test('HTML loads the blocking preference bootstrap before the application module', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
  const bootstrapIndex = html.indexOf('src="/preferences.js"')
  assert.ok(bootstrapIndex > 0)
  assert.ok(bootstrapIndex < html.indexOf('src="/src/main.ts"'))
  assert.ok(bootstrapIndex < html.indexOf('</head>'))
})
