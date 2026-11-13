import assert from 'node:assert/strict'
import test from 'node:test'
import { createI18n } from 'vue-i18n'
import { ingestFailureMessage, retryState } from '../src/utils/documentRetry.ts'
import { ingestRetryMessages } from '../src/i18n/messages/documentRetry.ts'
import messages from '../src/i18n/messages/documents.ts'

const NOW = Date.parse('2026-11-13T12:00:00Z')
const doc = (overrides: Record<string, unknown> = {}) => ({
  status: 'pending', ingest_attempts: 1, next_attempt_at: null,
  last_error_code: 'timeout', ...overrides
})

test('normal queue, initial processing and legacy responses have no retry state', () => {
  for (const status of ['pending', 'processing', 'ready', 'cancelled']) {
    assert.equal(retryState({ status }, NOW).phase, 'none', status)
    assert.equal(retryState(doc({ status, ingest_attempts: 0 }), NOW).phase, 'none', status)
  }
  for (const value of [null, undefined, false, 1, 'failed', []]) {
    assert.deepEqual(retryState(value, NOW), {
      phase: 'none', attempts: 0, waitSeconds: null, code: null
    })
  }
})

test('task-level failures are not the three HTTP transport attempts', () => {
  const state = retryState(doc({ next_attempt_at: '2026-11-13T12:00:30Z' }), NOW)
  assert.deepEqual(state, { phase: 'waiting', attempts: 1, waitSeconds: 30, code: 'timeout' })
})

test('countdown advances with the clock, rounds up and never becomes negative', () => {
  const pending = doc({ next_attempt_at: '2026-11-13T12:00:08.400Z' })
  assert.equal(retryState(pending, NOW).waitSeconds, 9)
  assert.equal(retryState(pending, NOW + 1_500).waitSeconds, 7)
  assert.equal(retryState(pending, NOW + 8_000).waitSeconds, 1)
  for (const now of [NOW + 8_400, NOW + 60_000]) {
    assert.equal(retryState(pending, now).waitSeconds, 0)
    assert.equal(retryState(pending, now).phase, 'waiting', 'a due retry is not processing yet')
  }
})

test('UTC timestamps are independent of browser timezone; explicit offsets are equivalent', () => {
  for (const next_attempt_at of ['2026-11-13T12:00:30Z', '2026-11-13T20:00:30+08:00',
    '2026-11-13T07:00:30-05:00', '2026-11-13T12:00:30.000000+00:00']) {
    assert.equal(retryState(doc({ next_attempt_at }), NOW).waitSeconds, 30)
  }
})

test('missing or invalid backoff time stays queued without inventing a countdown', () => {
  for (const next_attempt_at of [null, undefined, '', 'not-a-date', 123, true,
    '2026-11-13', '2026-11-13T12:00:30', '2026-11-13T25:00:00Z']) {
    const state = retryState(doc({ next_attempt_at }), NOW)
    assert.equal(state.phase, 'waiting')
    assert.equal(state.waitSeconds, null, String(next_attempt_at))
  }
  for (const now of [NaN, Infinity, -Infinity]) {
    assert.equal(retryState(doc({ next_attempt_at: '2026-11-13T12:00:30Z' }), now).waitSeconds, null)
  }
})

test('processing after a failure is retrying even if an old due timestamp remains', () => {
  for (const next_attempt_at of [null, '2026-11-13T11:00:00Z', '2026-11-13T13:00:00Z']) {
    assert.deepEqual(retryState(doc({ status: 'processing', next_attempt_at }), NOW), {
      phase: 'retrying', attempts: 1, waitSeconds: null, code: 'timeout'
    })
  }
})

test('all permanent errors, including authentication, stop without claiming budget exhaustion', () => {
  for (const last_error_code of ['unsupported_format', 'empty_content', 'corrupt_file',
    'file_missing', 'authentication']) {
    for (const ingest_attempts of [0, 1, 3]) {
      const state = retryState(doc({ status: 'failed', last_error_code, ingest_attempts,
        next_attempt_at: '2026-11-13T12:00:30Z' }), NOW)
      assert.equal(state.phase, 'permanent', last_error_code)
      assert.equal(state.code, last_error_code)
      assert.equal(state.waitSeconds, null)
    }
  }
})

test('known recoverable errors entering failed with a failure count mean attempts exhausted', () => {
  for (const last_error_code of ['source_unavailable', 'timeout', 'network', 'rate_limited',
    'storage', 'database', 'unknown']) {
    const state = retryState(doc({ status: 'failed', ingest_attempts: 3, last_error_code }), NOW)
    assert.equal(state.phase, 'exhausted', last_error_code)
    assert.equal(state.attempts, 3)
    assert.equal(state.waitSeconds, null)
  }
})

test('failed responses without the frozen retry contract remain generic failures', () => {
  for (const response of [
    { status: 'failed' },
    doc({ status: 'failed', last_error_code: null }),
    doc({ status: 'failed', last_error_code: 'future_backend_code' }),
    doc({ status: 'failed', ingest_attempts: undefined }),
    doc({ status: 'failed', ingest_attempts: 0 })
  ]) {
    assert.equal(retryState(response, NOW).phase, 'failed', JSON.stringify(response))
  }
})

test('unknown error codes and arbitrary error text are never exposed as display codes', () => {
  for (const last_error_code of ['<script>alert(1)</script>', 'SECRET/token/key', '__proto__',
    'constructor', 'TIMEOUT', '', null, {}, true]) {
    const state = retryState(doc({ status: 'failed', last_error_code, error_message: 'secret' }), NOW)
    assert.equal(state.code, null)
    assert.equal(state.phase, 'failed')
    assert.equal(JSON.stringify(state).includes('secret'), false)
  }
})

test('malformed task failure counts never produce bogus retry counts', () => {
  for (const ingest_attempts of [-1, 1.5, '2', true, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(retryState(doc({ ingest_attempts }), NOW).phase, 'none')
    assert.equal(retryState(doc({ status: 'failed', ingest_attempts }), NOW).phase, 'failed')
  }
})

test('success and cancellation clear visible retry state even with stale failure fields', () => {
  for (const status of ['ready', 'cancelled', 'unexpected']) {
    assert.equal(retryState(doc({ status }), NOW).phase, 'none')
  }
  for (const status of ['pending', 'processing']) {
    assert.equal(retryState(doc({ status, cancel_requested: true }), NOW).phase, 'none')
  }
  const waiting = doc({ next_attempt_at: '2026-11-13T12:00:30Z' })
  assert.equal(retryState(waiting, NOW).phase, 'waiting')
  assert.equal(retryState({ ...waiting, status: 'ready', ingest_attempts: 0,
    next_attempt_at: null, last_error_code: null }, NOW).phase, 'none')
})

test('retryState does not mutate input or share mutable idle results', () => {
  const pending = Object.freeze(doc())
  assert.equal(retryState(pending, NOW).phase, 'waiting')
  const first = retryState(null, NOW)
  first.attempts = 99
  assert.equal(retryState(null, NOW).attempts, 0)
})

test('generic failure text yields to safe localized empty-content and authentication advice', () => {
  const instance = createI18n({ legacy: false, locale: 'zh-CN', fallbackLocale: false, messages })
  try {
    for (const locale of ['zh-CN', 'en-US'] as const) {
      instance.global.mergeLocaleMessage(locale, ingestRetryMessages[locale])
      instance.global.locale.value = locale
      for (const code of ['empty_content', 'authentication'] as const) {
        const key = code === 'authentication' ? 'authenticationTip' : 'permanentTip'
        const advice = instance.global.t(`documents.ingestRetry.${key}`, {
          code: instance.global.t(`documents.ingestRetry.code.${code}`)
        })
        const displayed = ingestFailureMessage('文档处理失败，请联系管理员检查日志', advice, 'fallback')
        assert.equal(displayed, advice)
        assert.notEqual(displayed, '文档处理失败，请联系管理员检查日志')
      }
    }
  } finally {
    instance.dispose()
  }
})

test('failure display preserves dedicated PDF details and only replaces the exact generic message', () => {
  for (const message of [
    '第 1、3 页只有图片、没有可提取的文字，无法建立索引；请先对这些页面做 OCR 后重新上传',
    'PDF 中没有可提取的文字或表格（全部页面为空），请检查文件后重新上传',
    '文档处理失败，请联系管理员检查日志。',
    '文档处理失败，请联系管理员检查日志 ',
    '其他专用解析提示'
  ]) {
    assert.equal(ingestFailureMessage(message, 'safe localized advice', 'fallback'), message)
  }
})

test('failure display tolerates missing legacy fields and keeps the existing fallback order', () => {
  for (const message of [undefined, null, '', false, 1, {}]) {
    assert.equal(ingestFailureMessage(message, 'safe advice', 'fallback'), 'safe advice')
    assert.equal(ingestFailureMessage(message, '', 'fallback'), 'fallback')
  }
  assert.equal(ingestFailureMessage('文档处理失败，请联系管理员检查日志', '', 'fallback'),
    '文档处理失败，请联系管理员检查日志')
  assert.equal(ingestFailureMessage('dedicated message', '', 'fallback'), 'dedicated message')
})

test('lazy retry messages preserve the upload button and resolve every phase in both locales', () => {
  const zh = ingestRetryMessages['zh-CN'].documents.ingestRetry
  const en = ingestRetryMessages['en-US'].documents.ingestRetry
  assert.equal(messages['zh-CN'].documents.retry, '重试')
  assert.equal(messages['en-US'].documents.retry, 'Retry')
  assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort())
  assert.deepEqual(Object.keys(zh.code).sort(), Object.keys(en.code).sort())
  assert.match(en.authenticationTip, /credentials/)
  assert.match(zh.authenticationTip, /凭据/)
  const instance = createI18n({ legacy: false, locale: 'zh-CN', fallbackLocale: false, messages })
  try {
    for (const locale of ['zh-CN', 'en-US'] as const) {
      instance.global.mergeLocaleMessage(locale, ingestRetryMessages[locale])
      instance.global.locale.value = locale
      assert.equal(instance.global.t('documents.retry'), messages[locale].documents.retry)
      for (const [key, value] of Object.entries(ingestRetryMessages[locale].documents.ingestRetry)) {
        if (typeof value !== 'string') continue
        assert.ok(value.trim(), key)
        const path = `documents.ingestRetry.${key}`
        assert.equal(instance.global.te(path), true, path)
        const text = instance.global.t(path, { count: 2, failures: '2', code: 'timeout', seconds: 8 })
        assert.notEqual(text, path)
        assert.doesNotMatch(text, /\{(?:count|failures|code|seconds)\}/)
      }
    }
  } finally {
    instance.dispose()
  }
})
