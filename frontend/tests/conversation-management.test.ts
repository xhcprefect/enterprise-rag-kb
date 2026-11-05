/**
 * 会话列表：独立代次、按 ID 合并、迟到响应守卫的 Node 测试。
 *
 * 对齐验收：
 * - 分页按 ID 合并，重复 ID 用新响应覆盖；
 * - 列表用自己的 epoch，不触碰生成用的 sessionEpoch；
 * - 筛选变化后，旧筛选的迟到响应不能覆盖新列表。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  CONVERSATION_SEARCH_MAX,
  conversationFilterKey,
  createConversationListGuard,
  createConversationPager,
  isConversationPinned,
  mergeConversationPages,
  normalizeConversationQuery,
  normalizeConversationTitle,
  updateLocalConversation
} from '../src/utils/conversationList.ts'
import { createSessionEpoch } from '../src/utils/sessionEpoch.ts'

function conversation(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    kb_id: 'kb-1',
    mode: 'rag' as const,
    title: `会话 ${id}`,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    pinned_at: null,
    ...overrides
  }
}

test('pages merge by ID and keep the newer fields without duplicating entries', () => {
  const first = [conversation('a'), conversation('b')]
  const second = [conversation('b', { title: '已重命名' }), conversation('c')]
  const merged = mergeConversationPages(first, second)

  assert.deepEqual(merged.map((item) => item.id), ['a', 'b', 'c'])
  assert.equal(merged[1].title, '已重命名', '重复 ID 以较新响应为准')
  assert.equal(merged.length, 3)
})

test('merging is stable across repeated pages and never mutates the input', () => {
  const current = [conversation('a')]
  const snapshot = JSON.parse(JSON.stringify(current)) as typeof current
  const once = mergeConversationPages(current, [conversation('a'), conversation('b')])
  const twice = mergeConversationPages(once, [conversation('a')])
  assert.deepEqual(twice.map((item) => item.id), ['a', 'b'])
  assert.deepEqual(current, snapshot, '原数组不能被原地修改')
})

test('a new filter invalidates the previous filter response even before a new request starts', () => {
  const guard = createConversationListGuard()
  const old = guard.begin(conversationFilterKey({ mode: 'rag', kbId: 'kb-1', query: '' }))
  assert.equal(guard.isCurrent(old), true)

  // 用户改了搜索词：即使新请求还没发出，旧筛选的响应也已经过期
  const next = guard.begin(conversationFilterKey({ mode: 'rag', kbId: 'kb-1', query: 'policy' }))
  assert.equal(guard.isCurrent(old), false, '旧筛选的迟到响应不能覆盖新列表')
  assert.equal(guard.isCurrent(next), true)
})

test('a stale page response is rejected by the epoch even if the filter text matches', () => {
  const guard = createConversationListGuard()
  const filter = conversationFilterKey({ mode: 'chat', kbId: null, query: '' })
  const pageOne = guard.begin(filter)
  const pageTwo = guard.begin(filter)
  assert.equal(guard.isCurrent(pageOne), false, '同一筛选下的前一次请求也要被取代')
  assert.equal(guard.isCurrent(pageTwo), true)
})

test('list epoch is fully independent from the generation session epoch', () => {
  const session = createSessionEpoch()
  const generation = session.begin()
  const guard = createConversationListGuard()

  // 列表翻页/重新加载不会让生成用的 epoch 前进
  const listRequest = guard.begin(conversationFilterKey({ mode: 'rag', kbId: 'kb-1' }))
  assert.equal(session.current(), generation, '加载列表不能调用 sessionEpoch.begin()')
  assert.equal(session.isCurrent(generation), true)

  // 反过来：开始一次生成也不影响列表自己的代次
  session.begin()
  assert.equal(guard.isCurrent(listRequest), true)
  assert.equal(guard.current(), listRequest.token)
})

test('filter key distinguishes mode, knowledge base and trimmed query', () => {
  const base = conversationFilterKey({ mode: 'rag', kbId: 'kb-1', query: ' x ' })
  assert.equal(base, conversationFilterKey({ mode: 'rag', kbId: 'kb-1', query: 'x' }), '首尾空格不算筛选变化')
  assert.notEqual(base, conversationFilterKey({ mode: 'rag', kbId: 'kb-2', query: 'x' }))
  assert.notEqual(base, conversationFilterKey({ mode: 'chat', kbId: 'kb-1', query: 'x' }))
  assert.notEqual(base, conversationFilterKey({ mode: 'rag', kbId: 'kb-1', query: 'y' }))
  assert.notEqual(
    conversationFilterKey({ mode: 'rag', kbId: 'a\u0000b', query: '' }),
    conversationFilterKey({ mode: 'rag', kbId: 'a', query: 'b' }),
    '分隔符不能把知识库和搜索词串成一个键'
  )
})

test('search text is trimmed, bounded and non-strings fall back to empty', () => {
  assert.equal(normalizeConversationQuery('  policy  '), 'policy')
  assert.equal(normalizeConversationQuery(null), '')
  assert.equal(normalizeConversationQuery(undefined), '')
  assert.equal(normalizeConversationQuery(42), '')
  assert.equal(normalizeConversationQuery('x'.repeat(CONVERSATION_SEARCH_MAX + 30)).length, CONVERSATION_SEARCH_MAX)
  assert.equal(normalizeConversationQuery('中'.repeat(CONVERSATION_SEARCH_MAX)).length, CONVERSATION_SEARCH_MAX)
})

test('rename titles are trimmed and bounded to 1-200 characters', () => {
  assert.equal(normalizeConversationTitle('  季度复盘  '), '季度复盘')
  assert.equal(normalizeConversationTitle('x'.repeat(200)), 'x'.repeat(200))
  assert.equal(normalizeConversationTitle('x'.repeat(201)), null)
  assert.equal(normalizeConversationTitle('   '), null)
  assert.equal(normalizeConversationTitle(''), null)
  assert.equal(normalizeConversationTitle(null), null)
  assert.equal(normalizeConversationTitle(1), null)
})

test('pinned detection treats null and a missing field as unpinned', () => {
  assert.equal(isConversationPinned({ pinned_at: null }), false)
  assert.equal(isConversationPinned({}), false)
  assert.equal(isConversationPinned(null), false)
  assert.equal(isConversationPinned({ pinned_at: '2026-01-01T00:00:00Z' }), true)
})

test('a filter change in the debounce window rejects the in-flight old response', () => {
  const guard = createConversationListGuard()
  const oldFilter = conversationFilterKey({ mode: 'rag', kbId: 'kb-1', query: '' })
  const oldRequest = guard.begin(oldFilter)
  // 用户已经在输入框里改了词，但 220ms 防抖还没过去、新请求尚未 begin：
  // 只比较"最近一次请求的筛选"会误收旧响应，必须同时核验此刻的筛选键。
  const typedFilter = conversationFilterKey({ mode: 'rag', kbId: 'kb-1', query: 'policy' })
  assert.equal(guard.isCurrent(oldRequest), true, '默认参数只看请求快照，不能单独用于落地')
  assert.equal(guard.isCurrent(oldRequest, typedFilter), false, 'UI 已变，旧筛选的响应必须丢弃')
  assert.equal(guard.isCurrent(oldRequest, oldFilter), true)
})

test('page numbers come from the successfully loaded page, not from deduped row count', () => {
  const pager = createConversationPager()
  assert.equal(pager.loadedPage(), 0)
  assert.equal(pager.nextPage(), 1)

  pager.markLoaded(1, 20, 20)
  assert.equal(pager.loadedPage(), 1)
  assert.equal(pager.nextPage(), 2)

  // 第二页与第一页有 5 条重复：去重后列表 35 条，若按 35/20 推算会永远取第 2 页
  pager.markLoaded(2, 20, 20)
  assert.equal(pager.nextPage(), 3, '页码只由已成功页码推进')

  // 末页不满：到底后不再取下一页（即使去重条数仍小于 total）
  pager.markLoaded(3, 5, 20)
  assert.equal(pager.isExhausted(), true)
  assert.equal(pager.nextPage(), 4)

  // 筛选变化重置页码
  pager.reset()
  assert.equal(pager.loadedPage(), 0)
  assert.equal(pager.isExhausted(), false)
  assert.equal(pager.nextPage(), 1)
})

test('a local metadata change replaces only the matching list item', () => {
  const list = [conversation('a'), conversation('b')]
  const pinned = updateLocalConversation(list, conversation('b', { pinned_at: '2026-02-02T00:00:00Z' }))
  assert.deepEqual(pinned.map((item) => item.id), ['a', 'b'])
  assert.equal(isConversationPinned(pinned[1]), true)
  assert.equal(isConversationPinned(pinned[0]), false)

  // 不在列表里的 ID 不应凭空插入一条
  const untouched = updateLocalConversation(list, conversation('zzz'))
  assert.deepEqual(untouched.map((item) => item.id), ['a', 'b'])
})
