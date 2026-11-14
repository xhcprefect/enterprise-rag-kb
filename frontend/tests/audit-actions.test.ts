import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import test, { mock } from 'node:test'
import ts from 'typescript'
import { computed, createSSRApp, h, type ComputedRef } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { useI18n } from 'vue-i18n'
import { i18n } from '../src/i18n/index.ts'
import { messages } from '../src/i18n/messages/index.ts'
import {
  auditActionsMessages,
  ensureAuditActionsMessages,
  useAuditActionsI18n
} from '../src/i18n/messages/auditActions.ts'

const source = readFileSync(new URL('../src/views/AuditView.vue', import.meta.url), 'utf8')
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)?.[1]
assert.ok(script, 'AuditView must contain a setup script')
const parsed = ts.createSourceFile('AuditView.ts', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const mapStatement = parsed.statements.find(statement => ts.isVariableStatement(statement)
  && statement.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === 'ACTION_TEXT'))
assert.ok(mapStatement && ts.isVariableStatement(mapStatement), 'Read the actual ACTION_TEXT declaration')
const mapDeclaration = mapStatement.declarationList.declarations.find(declaration => declaration.name.getText(parsed) === 'ACTION_TEXT')!
assert.ok(mapDeclaration.initializer && ts.isCallExpression(mapDeclaration.initializer))
assert.equal(mapDeclaration.initializer.expression.getText(parsed), 'computed')
const mapFactory = mapDeclaration.initializer.arguments[0]
assert.ok(ts.isArrowFunction(mapFactory))
const mapBody = ts.isParenthesizedExpression(mapFactory.body) ? mapFactory.body.expression : mapFactory.body
assert.ok(ts.isObjectLiteralExpression(mapBody), 'Keep an explicit, inspectable event map')
const mappings = mapBody.properties.map(property => {
  assert.ok(ts.isPropertyAssignment(property) && ts.isIdentifier(property.name))
  assert.ok(ts.isCallExpression(property.initializer))
  const translator = property.initializer.expression.getText(parsed)
  assert.ok(translator === 't' || translator === 'aa', `Unexpected translator for ${property.name.text}`)
  const key = property.initializer.arguments[0]
  assert.ok(ts.isStringLiteral(key), `Use an explicit translation key for ${property.name.text}`)
  return { action: property.name.text, key: translator === 'aa' ? `auditActions.${key.text}` : key.text }
})
const actionFunction = parsed.statements.find(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === 'actionText')
assert.ok(actionFunction, 'Read the actual actionText fallback')

type ActionRuntime = { ACTION_TEXT: ComputedRef<Record<string, string>>; actionText(action: string): string }
// Execute the real map and fallback with Vue computed and the real page translators; no API or DOM is involved.
const createActionRuntime = runInNewContext(ts.transpileModule(
  `(function(t, aa) { ${mapStatement.getText(parsed)}\n${actionFunction.getText(parsed)}\nreturn { ACTION_TEXT, actionText } })`,
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }
).outputText, { computed }) as (t: (key: string) => string, aa: (key: string) => string) => ActionRuntime

const additions = [
  ['kb_member_update', 'kbMemberUpdate', '更新知识库成员', 'Knowledge base member updated'],
  ['kb_member_remove', 'kbMemberRemove', '移除知识库成员', 'Knowledge base member removed'],
  ['feedback_regression_job_create', 'feedbackRegressionJobCreate', '创建在线回归作业', 'Online regression job created'],
  ['feedback_regression_batch_create', 'feedbackRegressionBatchCreate', '创建在线回归批次', 'Online regression batch created'],
  ['feedback_regression_job_cancel', 'feedbackRegressionJobCancel', '取消在线回归作业', 'Online regression job cancelled'],
  ['feedback_regression_job_review', 'feedbackRegressionJobReview', '核验在线回归结果', 'Online regression job reviewed'],
  ['feedback_regression_batch_cancel', 'feedbackRegressionBatchCancel', '取消在线回归批次', 'Online regression batch cancelled']
] as const

test('seven lazy audit labels have identical explicit bilingual keys and stay out of the entry catalog', () => {
  const zh = auditActionsMessages['zh-CN'].auditActions
  const en = auditActionsMessages['en-US'].auditActions
  assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort())
  assert.deepEqual(Object.keys(zh).sort(), additions.map(([, key]) => key).sort())
  for (const [, key, chinese, english] of additions) {
    assert.equal(zh[key], chinese)
    assert.equal(en[key], english)
  }
  for (const locale of ['zh-CN', 'en-US'] as const) {
    assert.equal(Object.hasOwn(messages[locale], 'auditActions'), false)
    assert.equal(i18n.global.te('auditActions.kbMemberUpdate', locale), false)
  }
  const entryCatalog = readFileSync(new URL('../src/i18n/messages/index.ts', import.meta.url), 'utf8')
  assert.equal(entryCatalog.includes('auditActions'), false)
})

test('the actual audit filter and row labels share all 34 explicit computed mappings', () => {
  assert.equal(mappings.length, 34)
  assert.equal(new Set(mappings.map(entry => entry.action)).size, 34)
  for (const [action, key] of additions) {
    assert.equal(mappings.find(entry => entry.action === action)?.key, `auditActions.${key}`)
  }
  assert.match(source, /v-for="\(label, key\) in ACTION_TEXT"[^>]*:label="label"[^>]*:value="key"/)
  assert.match(source, /\{\{ actionText\(row\.action\) \}\}/)
  assert.match(script, /import \{ useAuditActionsI18n \} from '@\/i18n\/messages\/auditActions'/)
  assert.match(script, /const \{ aa \} = useAuditActionsI18n\(\)/)
})

test('lazy registration merges both locales once and preserves existing account and audit labels', () => {
  const before = JSON.stringify(['zh-CN', 'en-US'].map(locale => i18n.global.getLocaleMessage(locale)))
  const merge = mock.method(i18n.global, 'mergeLocaleMessage')
  try {
    ensureAuditActionsMessages()
    ensureAuditActionsMessages()
    assert.equal(merge.mock.callCount(), 2)
    assert.deepEqual(merge.mock.calls.map(call => call.arguments[0]), ['zh-CN', 'en-US'])
    const withoutAdditions = ['zh-CN', 'en-US'].map(locale => {
      const catalog = { ...i18n.global.getLocaleMessage(locale) }
      delete catalog.auditActions
      return catalog
    })
    assert.equal(JSON.stringify(withoutAdditions), before)
    for (const locale of ['zh-CN', 'en-US'] as const) {
      for (const [, key] of additions) assert.equal(i18n.global.te(`auditActions.${key}`, locale), true)
    }
  } finally {
    merge.mock.restore()
  }
})

test('the page translators resolve every real mapping across locale switches and keep unknown actions verbatim', async () => {
  let runtime: ActionRuntime | undefined
  const previousLocale = i18n.global.locale.value
  const app = createSSRApp({
    setup() {
      const { t } = useI18n()
      const { aa } = useAuditActionsI18n()
      runtime = createActionRuntime(t, aa)
      return () => h('span', runtime!.actionText('kb_member_update'))
    }
  })
  app.use(i18n)
  const errors: unknown[][] = []
  const warn = mock.method(console, 'warn', (...args: unknown[]) => { errors.push(args) })
  const error = mock.method(console, 'error', (...args: unknown[]) => { errors.push(args) })
  try {
    await renderToString(app)
    assert.ok(runtime)
    for (const locale of ['en-US', 'zh-CN', 'en-US'] as const) {
      i18n.global.locale.value = locale
      for (const { action, key } of mappings) {
        assert.equal(i18n.global.te(key, locale), true, `${locale}: ${action} -> ${key}`)
        const label = runtime.actionText(action)
        assert.equal(label, i18n.global.t(key), `${locale}: ${action}`)
        assert.ok(label.trim() && label !== action && label !== key, `${locale}: ${action} must resolve`)
        assert.equal(runtime.ACTION_TEXT.value[action], label)
      }
      for (const [action, , chinese, english] of additions) {
        assert.equal(runtime.actionText(action), locale === 'zh-CN' ? chinese : english)
      }
      for (const action of ['future_audit_event', '', '__proto__', 'constructor', 'toString', 'hasOwnProperty']) {
        assert.equal(runtime.actionText(action), action)
      }
    }
    assert.deepEqual(errors, [])
  } finally {
    i18n.global.locale.value = previousLocale
    warn.mock.restore()
    error.mock.restore()
  }
})
