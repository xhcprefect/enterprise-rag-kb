import assert from 'node:assert/strict'
import test from 'node:test'
import { createSessionEpoch } from '../src/utils/sessionEpoch.ts'

test('a later action invalidates every earlier async result', () => {
  const epoch = createSessionEpoch()
  const slowLoad = epoch.begin()
  assert.equal(epoch.isCurrent(slowLoad), true)

  // 用户点了另一条会话 / 直接发了新问题
  const newerAction = epoch.begin()
  assert.equal(epoch.isCurrent(slowLoad), false, '慢回来的旧详情必须先被丢弃')
  assert.equal(epoch.isCurrent(newerAction), true)

  // 再开始的生成让"另一条会话的详情"也过期
  const generation = epoch.begin()
  assert.equal(epoch.isCurrent(newerAction), false)
  assert.equal(epoch.isCurrent(generation), true)
})

test('an untouched epoch keeps accepting its own result', () => {
  const epoch = createSessionEpoch()
  const onlyAction = epoch.begin()
  for (let i = 0; i < 3; i += 1) assert.equal(epoch.isCurrent(onlyAction), true)
  assert.equal(epoch.current(), 1)
})

test('epoch tokens are never reused so a stale response cannot match a future action', () => {
  const epoch = createSessionEpoch()
  const tokens = new Set<number>()
  for (let i = 0; i < 100; i += 1) {
    const token = epoch.begin()
    assert.equal(tokens.has(token), false, 'each action owns a distinct token')
    tokens.add(token)
    // 只有最后一次仍算最新
    assert.equal(epoch.isCurrent(token), true)
  }
  assert.equal(epoch.isCurrent(0), false)
})

test('an initial offset still invalidates older in-flight work', () => {
  const epoch = createSessionEpoch(7)
  assert.equal(epoch.current(), 7)
  assert.equal(epoch.isCurrent(6), false)
  const token = epoch.begin()
  assert.equal(token, 8)
  assert.equal(epoch.isCurrent(8), true)
})
