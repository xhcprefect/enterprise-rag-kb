/**
 * 文档列表查询与迟到响应的 Node 测试。
 *
 * 搜索/翻页/轮询共用同一份查询快照；只有最新序号且快照一致的响应能落到界面。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SEARCH_MAX_LENGTH,
  isLatestResponse,
  isStalePoll,
  makeQuery,
  normalizeQueryText,
  queryParams,
  sameQuery,
  shouldPoll
} from '../src/utils/documentList.ts'

test('query text is trimmed and bounded to the server limit', () => {
  assert.equal(normalizeQueryText('  policy  '), 'policy')
  assert.equal(normalizeQueryText(null), '')
  assert.equal(normalizeQueryText(undefined), '')
  assert.equal(normalizeQueryText('x'.repeat(SEARCH_MAX_LENGTH + 50)).length, SEARCH_MAX_LENGTH)
  // 200 个字符本身不被截断
  assert.equal(normalizeQueryText('y'.repeat(SEARCH_MAX_LENGTH)).length, SEARCH_MAX_LENGTH)
})

test('makeQuery normalizes text and clamps paging', () => {
  assert.deepEqual(makeQuery({ q: '  a ', page: 0, pageSize: 0, status: 'ready' }), {
    q: 'a',
    page: 1,
    pageSize: 1,
    status: 'ready'
  })
  assert.deepEqual(makeQuery({ q: 'z'.repeat(300) }).q.length, SEARCH_MAX_LENGTH)
})

test('empty search and status are not sent as empty parameters', () => {
  assert.deepEqual(queryParams(makeQuery({ page: 2, pageSize: 50 })), {
    page: 2,
    page_size: 50
  })
  assert.deepEqual(queryParams(makeQuery({ q: '报告', status: 'failed', page: 1, pageSize: 20 })), {
    page: 1,
    page_size: 20,
    status: 'failed',
    q: '报告'
  })
})

test('sameQuery compares every field', () => {
  const base = makeQuery({ q: 'a', page: 1, pageSize: 20, status: 'ready' })
  assert.equal(sameQuery(base, { ...base }), true)
  for (const patch of [
    { q: 'b' },
    { page: 2 },
    { pageSize: 50 },
    { status: 'failed' }
  ]) {
    assert.equal(sameQuery(base, { ...base, ...patch }), false, JSON.stringify(patch))
  }
})

test('late response is rejected when a newer request or query exists', () => {
  const query = makeQuery({ q: 'a', page: 1, pageSize: 20 })
  // 最新请求 + 相同快照：接受
  assert.equal(isLatestResponse(5, 5, query, { ...query }), true)
  // 之后又发出了请求：旧序号拒绝
  assert.equal(isLatestResponse(4, 5, query, query), false)
  // 筛选/翻页已变化：旧快照拒绝，即使序号相同
  assert.equal(isLatestResponse(5, 5, query, makeQuery({ q: 'b', page: 1, pageSize: 20 })), false)
  assert.equal(isLatestResponse(5, 5, query, makeQuery({ q: 'a', page: 2, pageSize: 20 })), false)
  // 页面卸载
  assert.equal(isLatestResponse(5, 5, query, query, true), false)
})

test('polling continues only while a document is pending/processing or ingesting', () => {
  assert.equal(shouldPoll([], false), false)
  assert.equal(shouldPoll(['ready', 'failed', 'cancelled'], false), false)
  assert.equal(shouldPoll(['ready', 'pending'], false), true)
  assert.equal(shouldPoll(['processing'], false), true)
  assert.equal(shouldPoll([], true), true)
  assert.equal(shouldPoll(['failed'], true), true)
})

test('a poll from an older entry cannot act on the current one', () => {
  // 本拍对应当前条目、没有新请求：可以处置
  assert.equal(isStalePoll('a', 'a', 3, 3), false)
  // 旧 entry 的 active 文档 404 迟到，此时 current 已换成 b
  assert.equal(isStalePoll('a', 'b', 3, 3), true)
  // current 已清空（取消当前/取消全部）
  assert.equal(isStalePoll('a', null, 3, 3), true)
  // 期间用户触发了新的列表加载：这一拍整体过期
  assert.equal(isStalePoll('a', 'a', 2, 3), true)
  // 页面卸载或无 current：永不处置
  assert.equal(isStalePoll('a', 'a', 3, 3, true), true)
  assert.equal(isStalePoll(null, 'a', 3, 3), true)
})
