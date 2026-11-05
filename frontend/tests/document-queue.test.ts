import assert from 'node:assert/strict'
import test from 'node:test'
import {
  acceptsEntryReceipt,
  acceptsResponse,
  advance,
  cancelAll,
  decideAfterSession,
  emptyQueue,
  enqueue,
  finishCurrent,
  isActive,
  isBlocking,
  patchCurrent,
  pauseCurrent,
  queueSummary,
  removeWaiting,
  retryCurrent,
  signature,
  skipCurrent
} from '../src/utils/documentQueue.ts'
import type { QueueItem, QueuePhase } from '../src/utils/documentQueue.ts'

function file(name: string, size = 10, lastModified = 1): File {
  return { name, size, lastModified } as File
}

function item(key: string, f = file(key)): QueueItem {
  return { key, file: f, phase: 'waiting', deduplicated: false }
}

function running(keys: string[]) {
  const [head, ...rest] = keys.map((key) => item(key))
  const state = { current: { ...head, phase: 'uploading' as QueuePhase }, queue: rest }
  return { state, ...queueSummary(state) }
}

test('queue starts with one current item and the rest waiting', () => {
  const { state } = enqueue(emptyQueue(), [item('a'), item('b'), item('c')])
  assert.equal(state.current?.key, 'a')
  assert.equal(state.current?.phase, 'uploading')
  assert.deepEqual(state.queue.map((entry) => entry.key), ['b', 'c'])
  assert.deepEqual(state.queue.map((entry) => entry.phase), ['waiting', 'waiting'])
})

test('duplicate files in the same batch and against current are rejected', () => {
  const { state, rejected } = enqueue(emptyQueue(), [item('a'), item('a'), item('b')])
  assert.deepEqual(rejected.map((entry) => entry.key), ['a'])
  assert.equal(state.current?.key, 'a')
  assert.deepEqual(state.queue.map((entry) => entry.key), ['b'])

  const second = enqueue(state, [item('a-again', file('a'))])
  assert.equal(second.rejected.length, 1) // 与 current 同文件
  assert.equal(second.state.queue.length, 1)
})

test('signature distinguishes same-name files by size and mtime', () => {
  assert.equal(signature(file('a', 1, 1)), signature(file('a', 1, 1)))
  assert.notEqual(signature(file('a', 1, 1)), signature(file('a', 2, 1)))
  assert.notEqual(signature(file('a', 1, 1)), signature(file('a', 1, 2)))
})

test('finishing current advances to the next queued item', () => {
  const { state } = running(['a', 'b', 'c'])
  const next = finishCurrent(state, 'done')
  assert.equal(next.current?.key, 'b')
  assert.equal(next.current?.phase, 'uploading')
  assert.deepEqual(next.queue.map((entry) => entry.key), ['c'])
})

test('failure stops the queue until the user retries or skips', () => {
  const { state } = running(['a', 'b'])
  const failed = finishCurrent(state, 'failed', false)
  assert.equal(failed.current?.key, 'a')
  assert.equal(failed.current?.phase, 'failed')
  assert.equal(failed.queue.length, 1) // 后续保留，不静默跳过

  const retried = retryCurrent(failed)
  assert.equal(retried.current?.phase, 'uploading')
  assert.equal(retried.queue.length, 1)

  const skipped = skipCurrent(failed)
  assert.equal(skipped.current?.key, 'b')
  assert.equal(skipped.queue.length, 0)

  // 非失败状态不允许 retry/skip
  const active = running(['a']).state
  assert.deepEqual(retryCurrent(active), active)
  assert.equal(skipCurrent(active), active)
})

test('pause keeps current and the remaining queue', () => {
  const { state } = running(['a', 'b'])
  const paused = pauseCurrent(state)
  assert.equal(paused.current?.phase, 'paused')
  assert.deepEqual(paused.queue.map((entry) => entry.key), ['b'])
  const resumed = patchCurrent(paused, { phase: 'uploading' })
  assert.equal(resumed.current?.phase, 'uploading')
  // 暂停后再 finish 仍能继续下一项
  assert.equal(finishCurrent(resumed, 'done').current?.key, 'b')
})

test('cancel current continues with the next; cancel all stops everything', () => {
  const { state } = running(['a', 'b', 'c'])
  const cancelledOne = finishCurrent(state, 'cancelled')
  assert.equal(cancelledOne.current?.key, 'b')
  assert.deepEqual(cancelledOne.queue.map((entry) => entry.key), ['c'])

  const stopped = cancelAll(state)
  assert.equal(stopped.current?.phase, 'cancelled')
  assert.deepEqual(stopped.queue, [])
  assert.equal(finishCurrent(stopped, 'done').current, null)
})

test('waiting items can be removed but current cannot', () => {
  const { state } = running(['a', 'b', 'c'])
  const removed = removeWaiting(state, 'c')
  assert.deepEqual(removed.queue.map((entry) => entry.key), ['b'])
  assert.equal(removed.current?.key, 'a')
  assert.equal(removeWaiting(removed, 'a').current?.key, 'a')
})

test('late responses are rejected once the item is no longer current', () => {
  const { state } = running(['a', 'b'])
  assert.equal(acceptsResponse(state, 'a', ['uploading']), true)
  assert.equal(acceptsResponse(state, 'b', ['uploading']), false)
  assert.equal(acceptsResponse(state, 'a', ['ingesting']), false)

  const cancelled = cancelAll(state)
  // 取消后旧上传的 complete 回执必须被丢弃，不能把阶段改回 ingesting
  assert.equal(acceptsResponse(cancelled, 'a', ['uploading', 'paused']), false)

  const next = finishCurrent(state, 'cancelled')
  assert.equal(acceptsResponse(next, 'a', ['ingesting', 'done']), false)
  assert.equal(acceptsResponse(next, 'b', ['uploading']), true)
})

test('advance never replaces an occupied current', () => {
  const { state } = running(['a', 'b'])
  assert.equal(advance(state), state)
  const empty = advance({ current: null, queue: [] })
  assert.equal(empty.current, null)
})

test('phase predicates cover blocking and active states', () => {
  assert.equal(isBlocking('failed'), true)
  for (const phase of ['waiting', 'uploading', 'paused', 'ingesting', 'done', 'cancelled'] as QueuePhase[]) {
    assert.equal(isBlocking(phase), false, phase)
  }
  assert.equal(isActive('uploading'), true)
  assert.equal(isActive('paused'), true)
  assert.equal(isActive('ingesting'), true)
  assert.equal(isActive('failed'), false)
  assert.equal(isActive('done'), false)
})

test('queue summary counts current plus waiting', () => {
  assert.deepEqual(queueSummary(emptyQueue()), { waiting: 0, total: 0 })
  const { state } = running(['a', 'b', 'c'])
  assert.deepEqual(queueSummary(state), { waiting: 2, total: 3 })
})

test('pause during createSession is decided from the live current item, not the stale entry', () => {
  // runEntry 手里的 entry 是发起建会话请求前的旧对象
  const { state } = running(['a', 'b'])
  const staleEntry = state.current!
  assert.equal(staleEntry.phase, 'uploading')

  // 请求在途期间用户点了暂停：pauseCurrent 生成新的 current 对象
  const paused = pauseCurrent(state)
  assert.notEqual(paused.current, staleEntry)
  assert.equal(staleEntry.phase, 'uploading', 'stale snapshot keeps its old phase')

  // 关键：以队列里的实际相位为准，而不是旧 entry.phase
  assert.equal(decideAfterSession(state, 'a'), 'continue')
  assert.equal(decideAfterSession(paused, 'a'), 'pause')

  // 暂停后继续：回到 uploading 才能继续推进
  assert.equal(decideAfterSession(patchCurrent(paused, { phase: 'uploading' }), 'a'), 'continue')
})

test('a delayed reindex receipt cannot turn the next upload entry into ingesting', () => {
  // 失败条目 A 已建文档；用户点「重建索引」，请求在途
  const failed: QueueItem & { task: { docId: string } } = {
    ...item('a'),
    phase: 'failed' as QueuePhase,
    task: { docId: 'doc-a' }
  }
  const next: QueueItem & { task: { docId: string } } = {
    ...item('b'),
    phase: 'waiting' as QueuePhase,
    task: { docId: '' }
  }
  const state = { current: failed, queue: [next] }
  const receipt = { key: 'a', docId: 'doc-a' }
  const target = (s: typeof state) =>
    s.current && { key: s.current.key, docId: s.current.task.docId, phase: s.current.phase }

  // 回执仍在原失败条目上：接受
  assert.equal(acceptsEntryReceipt(receipt, target(state), ['failed']), true)

  // 请求在途期间用户点「跳过」：队列推进到下一个上传条目
  const skipped = skipCurrent(state)
  assert.equal(skipped.current?.key, 'b')
  // 迟到成功回执必须被丢弃：否则新条目会被强置 ingesting，丢失上传推进
  assert.equal(acceptsEntryReceipt(receipt, target(skipped), ['failed']), false)

  // 用户点「取消全部」：current 被清空 / 换对象
  const cleared = cancelAll(state)
  assert.equal(acceptsEntryReceipt(receipt, target(cleared), ['failed']), false)
  assert.equal(acceptsEntryReceipt(receipt, null, ['failed']), false)
  // 页面卸载
  assert.equal(acceptsEntryReceipt(receipt, target(state), ['failed'], true), false)
  // 同一 key 但 docId 不同（理论上不会发生，仍须拒绝）
  assert.equal(
    acceptsEntryReceipt(receipt, { key: 'a', docId: 'doc-z', phase: 'failed' }, ['failed']),
    false
  )
  // 阶段不符：已经推进到入库/完成
  assert.equal(
    acceptsEntryReceipt(receipt, { key: 'a', docId: 'doc-a', phase: 'ingesting' }, ['failed']),
    false
  )
})

test('session resolved after cancel/skip/new current is abandoned', () => {
  const { state } = running(['a', 'b'])
  assert.equal(decideAfterSession(cancelAll(state), 'a'), 'abandon')
  assert.equal(decideAfterSession(finishCurrent(state, 'cancelled'), 'a'), 'abandon')
  // 新的当前项与旧请求 key 不符：绝不推进它
  assert.equal(decideAfterSession(finishCurrent(state, 'cancelled'), 'b'), 'continue')
  // 失败停住时也不能继续上传
  assert.equal(decideAfterSession(finishCurrent(state, 'failed', false), 'a'), 'abandon')
})
