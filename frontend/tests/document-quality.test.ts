/**
 * 入库质量纯逻辑测试：issue code 已知/未知回退、severity 颜色、分块分页。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { messages } from '../src/i18n/messages/index.ts'
import {
  QUALITY_CHUNK_PAGE_SIZE,
  QUALITY_ISSUE_CODES,
  clampQualityChunkPage,
  qualityChunkOffset,
  qualityChunkPageCount,
  qualityIssueMessageKey,
  qualityIssueTagType
} from '../src/utils/documentQuality.ts'

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

test('known issue codes map to their specific translation keys', () => {
  for (const code of QUALITY_ISSUE_CODES) {
    assert.equal(
      qualityIssueMessageKey(code),
      `documents.quality.issues.codes.${code}`,
      code
    )
  }
  // 已知 code 两两不同，不会互相覆盖
  assert.equal(
    new Set(QUALITY_ISSUE_CODES.map(qualityIssueMessageKey)).size,
    QUALITY_ISSUE_CODES.length
  )
})

test('unknown or malformed issue codes fall back to the generic message, never the raw key', () => {
  for (const code of ['future_code', '', '  file_missing  ', null, undefined]) {
    const key = qualityIssueMessageKey(code)
    if (code === '  file_missing  ') {
      assert.equal(key, 'documents.quality.issues.codes.file_missing')
      continue
    }
    assert.equal(key, 'documents.quality.issues.unknown', String(code))
    assert.equal(key.includes('future_code'), false)
  }
})

test('severity maps to distinct Element Plus tag colors', () => {
  assert.equal(qualityIssueTagType('error'), 'danger')
  assert.equal(qualityIssueTagType('warning'), 'warning')
  assert.equal(qualityIssueTagType('info'), 'info')
  // 未知 severity 按信息级处理，不冒充错误
  assert.equal(qualityIssueTagType('fatal'), 'info')
  assert.equal(qualityIssueTagType(null), 'info')
})

test('chunk pagination uses a fixed 20 per page and 1-based offsets', () => {
  assert.equal(QUALITY_CHUNK_PAGE_SIZE, 20)
  assert.equal(qualityChunkPageCount(0), 0)
  assert.equal(qualityChunkPageCount(-3), 0)
  assert.equal(qualityChunkPageCount(1), 1)
  assert.equal(qualityChunkPageCount(20), 1)
  assert.equal(qualityChunkPageCount(21), 2)
  assert.equal(qualityChunkPageCount(40), 2)
  assert.equal(qualityChunkPageCount(41), 3)

  assert.equal(qualityChunkOffset(1), 0)
  assert.equal(qualityChunkOffset(2), 20)
  assert.equal(qualityChunkOffset(5), 80)
  // 非法页码按第 1 页处理
  assert.equal(qualityChunkOffset(0), 0)
  assert.equal(qualityChunkOffset(-2), 0)

  assert.equal(clampQualityChunkPage(3, 40), 2)
  assert.equal(clampQualityChunkPage(0, 40), 1)
  assert.equal(clampQualityChunkPage(9, 0), 1)
  assert.equal(clampQualityChunkPage(2, 1), 1)
})

test('every issue-code translation exists in both locales', () => {
  for (const locale of ['zh-CN', 'en-US'] as const) {
    const catalog = flatten(messages[locale])
    for (const code of QUALITY_ISSUE_CODES) {
      const key = qualityIssueMessageKey(code)
      assert.ok(catalog[key], `${locale}: missing ${key}`)
    }
    assert.ok(catalog['documents.quality.issues.unknown'], `${locale}: missing generic fallback`)
    assert.ok(catalog['documents.quality.issues.none'], `${locale}: missing empty state`)
    // 统计与位置覆盖的 labelKey 由组件动态传入，单测兜住键名漂移。
    for (const key of [
      'documents.quality.stats.empty',
      'documents.quality.stats.short',
      'documents.quality.stats.duplicate',
      'documents.quality.stats.replacement',
      'documents.quality.stats.control',
      'documents.quality.stats.oversized',
      'documents.quality.stats.page',
      'documents.quality.stats.section',
      'documents.quality.stats.line',
      'documents.quality.stats.sheet',
      'documents.quality.stats.table'
    ]) {
      assert.ok(catalog[key], `${locale}: missing ${key}`)
    }
  }
})
