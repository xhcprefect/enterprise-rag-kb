/** memoryController 的真实异步路径测试：用注入的 load/reset/定时器/时钟驱动。
 * 覆盖主审点：deferred 确认跨会话不发错、延迟 GET 后卸载/切会话不复活轮询、
 * busy 结束刷新、序号 gap 不虚报、poll 有界到顶。 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { MemoryController, type MemoryView, type FrozenReset } from '../src/utils/memoryController.ts'

/** 冲刷所有待决微任务：让 doLoad 的续体与 .then(beginPolling) 链路真正跑完。 */
function flush(): Promise<void> {
  return new Promise((resolve) => { setTimeout(resolve, 0) })
}

const PROCESSING: MemoryView = { enabled: true, state: 'processing', covered_turn_seq: 10, version: 1, lag: true }
const READY_SETTLED: MemoryView = { enabled: true, state: 'ready', covered_turn_seq: 12, version: 3, lag: false }
const READY_LAG: MemoryView = { enabled: true, state: 'ready', covered_turn_seq: 8, version: 2, lag: true }

interface Harness {
  controller: MemoryController
  loadCalls: string[]
  resetCalls: Array<{ conv: string; version: number }>
  viewCalls: Array<MemoryView | null>
  clearCalls: number
  loadingCalls: boolean[]
  expiredCalls: number
  resolveLoad: (conv: string, value: MemoryView | null) => void
  scheduledCount: () => number
  tick: () => boolean
  advance: (ms: number) => void
  now: () => number
}

function makeController(): Harness {
  let t = 0
  let resolvers: Record<string, (v: MemoryView | null) => void> = {}
  const loadCalls: string[] = []
  const resetCalls: Array<{ conv: string; version: number }> = []
  const viewCalls: Array<MemoryView | null> = []
  let clearCalls = 0
  const loadingCalls: boolean[] = []
  let expiredCalls = 0
  const pending: Array<{ fn: () => void; ms: number; cleared: boolean }> = []

  const schedule = (fn: () => void, ms: number) => {
    const entry = { fn, ms, cleared: false }
    pending.push(entry)
    return () => { entry.cleared = true }
  }

  const controller = new MemoryController({
    load: (conv) => {
      loadCalls.push(conv)
      return new Promise<MemoryView | null>((resolve) => { resolvers[conv] = resolve })
    },
    reset: async (conv, version) => {
      resetCalls.push({ conv, version })
      return 'ok'
    },
    onView: (v) => { viewCalls.push(v) },
    onClear: () => { clearCalls += 1 },
    onLoading: (b) => { loadingCalls.push(b) },
    onPollExpired: () => { expiredCalls += 1 },
    now: () => t,
    schedule
  })

  return {
    controller,
    loadCalls,
    resetCalls,
    viewCalls,
    get clearCalls() { return clearCalls },
    loadingCalls,
    get expiredCalls() { return expiredCalls },
    resolveLoad: (conv, value) => { resolvers[conv]?.(value) },
    scheduledCount: () => pending.filter((e) => !e.cleared).length,
    tick: () => {
      const entry = pending.find((e) => !e.cleared)
      if (!entry) return false
      entry.cleared = true
      entry.fn()
      return true
    },
    advance: (ms) => { t += ms },
    now: () => t
  }
}

// 同步 resolve 的便捷：接收一个会立即 resolve 的 load。
function makeImmediateController(views: Record<string, MemoryView | null>) {
  let t = 0
  const resetCalls: Array<{ conv: string; version: number }> = []
  const pending: Array<{ fn: () => void; ms: number; cleared: boolean }> = []
  const schedule = (fn: () => void, ms: number) => {
    const entry = { fn, ms, cleared: false }
    pending.push(entry)
    return () => { entry.cleared = true }
  }
  const controller = new MemoryController({
    load: async (conv) => views[conv] ?? null,
    reset: async (conv, version) => { resetCalls.push({ conv, version }); return 'ok' },
    onView: () => {},
    onPollExpired: () => {},
    now: () => t,
    schedule
  })
  return {
    controller, resetCalls,
    scheduledCount: () => pending.filter((e) => !e.cleared).length,
    tick: () => {
      const entry = pending.find((e) => !e.cleared)
      if (!entry) return false
      entry.cleared = true
      entry.fn()
      return true
    },
    advance: (ms) => { t += ms }
  }
}

test('busy end triggers a refresh read; an in-flight result starts polling', async () => {
  const h = makeController()
  h.controller.onContext(true, 'A')          // 初始读取
  h.resolveLoad('A', READY_SETTLED)           // 已安定 → 不轮询
  await flush()
  assert.deepEqual(h.loadCalls, ['A'])
  assert.equal(h.scheduledCount(), 0)

  h.controller.onBusyEnd()                    // busy 结束后刷新
  assert.deepEqual(h.loadCalls, ['A', 'A'])
  h.resolveLoad('A', PROCESSING)              // 刷新结果在途 → 开始轮询
  await flush()
  assert.equal(h.scheduledCount(), 1)
})

test('delayed GET resolved after unmount does not revive polling or apply to a dead view', async () => {
  const h = makeController()
  h.controller.onContext(true, 'A')
  h.controller.dispose()                      // 卸载发生在 GET 返回前
  h.resolveLoad('A', PROCESSING)              // 迟到结果（本应在途 → 会轮询）
  await flush()
  assert.equal(h.scheduledCount(), 0, '卸载后的迟到 GET 绝不能复活轮询')
  assert.deepEqual(h.viewCalls, [], '卸载后的迟到 GET 不能把数据投给已销毁视图')
})

test('delayed GET resolved after switching conversation neither applies nor polls the old one', async () => {
  const h = makeController()
  h.controller.onContext(true, 'A')           // 对 A 发起读取（延迟未返回）
  h.controller.onContext(true, 'B')           // 切换会话，作废 A 的代次
  assert.deepEqual(h.loadCalls, ['A', 'B'])
  h.resolveLoad('A', PROCESSING)              // A 迟到返回“在途” → 不应为旧会话轮询
  await flush()
  assert.equal(h.scheduledCount(), 0, '旧会话 A 的迟到结果不得为新上下文开启轮询')
  assert.equal(h.viewCalls.length, 0, '旧会话 A 的结果不得写到当前 B 的视图')

  h.resolveLoad('B', READY_SETTLED)
  await flush()
  assert.equal(h.viewCalls.length, 1, '只有当前会话 B 的结果被采纳')
  assert.equal(h.scheduledCount(), 0)
})

test('deferred confirm across-conversation switch never resets the wrong conversation', async () => {
  const h = makeController()
  h.controller.onContext(true, 'A')
  h.resolveLoad('A', READY_SETTLED)           // version=3
  await flush()
  const frozen = h.controller.freezeReset()!
  assert.equal(frozen.conversationId, 'A')
  assert.equal(frozen.version, 3)

  h.controller.onContext(true, 'B')           // 确认期间切到 B（再无摘要）
  const outcome = await frozen.commit()
  assert.equal(outcome, 'stale')
  assert.deepEqual(h.resetCalls, [], '切会话后提交必须中止，绝不误重置 B（或 A）')
})

test('deferred confirm in same context resets with the frozen version', async () => {
  const h = makeController()
  h.controller.onContext(true, 'A')
  h.resolveLoad('A', READY_SETTLED)           // version=3
  await flush()
  const frozen = h.controller.freezeReset()!
  const outcome = await frozen.commit()
  assert.equal(outcome, 'ok')
  assert.deepEqual(h.resetCalls, [{ conv: 'A', version: 3 }])
})

test('deferred confirm aborts when the same conversation starts generating during the dialog', async () => {
  const h = makeController()
  h.controller.onContext(true, 'A')
  h.resolveLoad('A', READY_SETTLED)           // version=3
  await flush()
  const frozen = h.controller.freezeReset()!
  h.controller.setBusy(true)                  // 确认框打开期间同会话开始生成/只读恢复
  const outcome = await frozen.commit()
  assert.equal(outcome, 'stale')
  assert.deepEqual(h.resetCalls, [], 'busy 变 true 后提交必须拒绝，绝不发 reset（只能靠后端 409 兜底）')
})

test('freezeReset re-entry is deduped until abort/commit', async () => {
  const h = makeController()
  h.controller.onContext(true, 'A')
  h.resolveLoad('A', READY_SETTLED)
  await flush()
  const first = h.controller.freezeReset()
  assert.ok(first)
  // 再次进入（第二次点击）应被拒绝，避免多个确认堆叠。
  const second = h.controller.freezeReset()
  assert.equal(second, null)
  h.controller.abortReset()
  const third = h.controller.freezeReset()
  assert.ok(third)
  assert.equal((third as FrozenReset).version, 3)
})

test('poll runs only while in-flight/lagging; settled stays idle (no overclaim on normal 4-turn retention)', async () => {
  const c = makeImmediateController({ A: READY_SETTLED })
  c.controller.onContext(true, 'A')
  assert.equal(c.scheduledCount(), 0, 'ready + lag=false（正常保留最近几轮）绝不轮询')
})

test('poll has a real wall-clock deadline and stops with an expired signal', async () => {
  const h = makeController()
  h.controller.onContext(true, 'A')
  h.resolveLoad('A', PROCESSING)              // 在途 → 开始有界轮询
  await flush()
  assert.equal(h.scheduledCount(), 1)

  h.advance(2500)
  assert.ok(h.tick(), '第一轮轮询触发')
  h.resolveLoad('A', PROCESSING)              // 仍处理中 → 再排
  await flush()
  assert.equal(h.scheduledCount(), 1)

  // 一路推进到超过 60s 墙钟 deadline 后，必须停并通知 UI 手动刷新。
  for (let i = 0; i < 30; i++) {
    h.advance(2500)
    h.tick()
    h.resolveLoad('A', PROCESSING)
    await flush()
  }
  assert.equal(h.expiredCalls, 1, '超过 deadline 应触发一次 pollExpired')
  assert.equal(h.scheduledCount(), 0, 'deadline 后不得再排轮询')
})
