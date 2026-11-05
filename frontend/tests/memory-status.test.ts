/** 会话摘要（memory）纯状态逻辑的 Node 测试；不依赖 vue/api。
 * 覆盖主审点：queued reset 可行、序号 gap/正常保留 4 轮不虚报 lag、有已发布摘要时
 * pending/failed 也可只读查看、poll 在安定/未知滞后时不空转、version 冻结契约。 */
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  canReset,
  canViewSummary,
  coveredWatermark,
  deriveMemoryDisplay,
  hasSummary,
  normalizeLag,
  resetExpectedVersion,
  shouldPollMemory,
  type MemoryDisplay,
  type MemoryState,
  type MemoryStatusLike
} from '../src/utils/memoryStatus.ts'

function display(state: MemoryState, over: Partial<MemoryStatusLike> = {}): MemoryDisplay {
  return deriveMemoryDisplay({
    enabled: true,
    state,
    covered_turn_seq: 10,
    version: null,
    ...over
  })
}

test('coveredWatermark treats invalid/negative seq conservatively as 0', () => {
  assert.equal(coveredWatermark(12), 12)
  assert.equal(coveredWatermark(0), 0)
  assert.equal(coveredWatermark(-5), 0)
  assert.equal(coveredWatermark(1.5), 0)
  assert.equal(coveredWatermark('3'), 0)
  assert.equal(coveredWatermark(null), 0)
  assert.equal(coveredWatermark(undefined), 0)
})

test('hasSummary and resetExpectedVersion follow the version/schema contract', () => {
  assert.equal(hasSummary(null), false)
  assert.equal(hasSummary(undefined), false)
  assert.equal(hasSummary(0), true)
  assert.equal(hasSummary(3), true)
  // 无摘要时提交 expected_version=0，有摘要时用当前版本（确认前冻结凭据）
  assert.equal(resetExpectedVersion(null), 0)
  assert.equal(resetExpectedVersion(undefined), 0)
  assert.equal(resetExpectedVersion(0), 0)
  assert.equal(resetExpectedVersion(7), 7)
})

test('normalizeLag keeps backend boolean truth; unknown stays null, never guessed', () => {
  assert.equal(normalizeLag(true), true)
  assert.equal(normalizeLag(false), false)
  assert.equal(normalizeLag(undefined), null)
  assert.equal(normalizeLag(null), null)
  assert.equal(normalizeLag(0), null)
  assert.equal(normalizeLag('yes'), null)
})

test('deriveMemoryDisplay gates on enabled and maps every state conservatively', () => {
  // 未启用或 state=disabled → disabled
  assert.deepEqual(
    deriveMemoryDisplay({ enabled: false, state: 'ready' as MemoryState, covered_turn_seq: 10, version: 3 }),
    { kind: 'disabled' })
  assert.deepEqual(
    deriveMemoryDisplay({ enabled: true, state: 'disabled' as MemoryState, covered_turn_seq: 10, version: 3 }),
    { kind: 'disabled' })

  assert.deepEqual(display('idle'), { kind: 'idle' })

  // queued/processing → pending：只报已覆盖水位 + 有无历史摘要，不虚报差量计数
  assert.deepEqual(display('queued'), { kind: 'pending', covered: 10, hasSummary: false })
  assert.deepEqual(display('processing', { covered_turn_seq: 12 }), { kind: 'pending', covered: 12, hasSummary: false })
  // 有已发布摘要时 pending 也标 hasSummary=true（连生成中也可只读查看旧版）
  assert.deepEqual(display('queued', { version: 3 }), { kind: 'pending', covered: 10, hasSummary: true })

  // ready：lag 只信后端布尔；truncated 沿用
  assert.deepEqual(display('ready', { covered_turn_seq: 12 }), { kind: 'ready', covered: 12, lag: null, truncated: false })
  assert.deepEqual(display('ready', { lag: false }), { kind: 'ready', covered: 10, lag: false, truncated: false })
  assert.deepEqual(display('ready', { lag: true, truncated: true }), { kind: 'ready', covered: 10, lag: true, truncated: true })

  // failed：展示错误；有历史摘要 hasSummary=true 可只读查看旧版
  assert.deepEqual(
    display('failed', { error_message: '模型超时' }),
    { kind: 'failed', message: '模型超时', hasSummary: false, covered: 10 })
  assert.deepEqual(
    display('failed', { version: 3, error_message: '模型超时' }),
    { kind: 'failed', message: '模型超时', hasSummary: true, covered: 10 })
})

test('sequence gap is not a completed-turn count: covered watermark never implies full coverage', () => {
  // 关键：next_turn_seq 是"已分配"计数，失败/取消会留空号。这里只断言 covered 水位
  // 与后端 lag 布尔，绝不把 (next - covered) 当作待整理精确轮次。
  const gappedReady = deriveMemoryDisplay({
    enabled: true, state: 'ready' as MemoryState,
    covered_turn_seq: 8, version: 3,
    lag: false // 后端明确：虽序号有 gap，但当前无待整理历史
  })
  assert.deepEqual(gappedReady, { kind: 'ready', covered: 8, lag: false, truncated: false })

  // lag 未知（后端没给）→ 展示未知，不猜"已全部覆盖"也不猜"有落后"
  const unknownLag = deriveMemoryDisplay({
    enabled: true, state: 'ready' as MemoryState,
    covered_turn_seq: 8, version: 3
  })
  assert.deepEqual(unknownLag, { kind: 'ready', covered: 8, lag: null, truncated: false })
  assert.equal((unknownLag as Extract<MemoryDisplay, { kind: 'ready' }>).lag, null)
})

test('canReset allows queued/processing (background summary may be epoch-reset), blocks only disabled', () => {
  // 主审点 4：后台摘要任务（queued/processing）允许重置作废旧 job；生成中拦截由 busy 承担。
  assert.equal(canReset('queued'), true)
  assert.equal(canReset('processing'), true)
  assert.equal(canReset('ready'), true)
  assert.equal(canReset('idle'), true)
  assert.equal(canReset('failed'), true)
  // disabled 无摘要可重置
  assert.equal(canReset('disabled'), false)
})

test('shouldPollMemory stops polling once settled or lag is unknown (no busy loop, no revive)', () => {
  // 在途 → 轮询
  assert.equal(shouldPollMemory({ state: 'queued', lag: null }), true)
  assert.equal(shouldPollMemory({ state: 'processing', lag: null }), true)
  // 滞后（ready 且 lag=true）→ 轮询；正常保留最近几轮不设 lag → 不轮询
  assert.equal(shouldPollMemory({ state: 'ready', lag: true }), true)
  assert.equal(shouldPollMemory({ state: 'ready', lag: false }), false)
  // lag 未知/null → 不轮询，避免空转（卸载/未知状态不会"复活"旧轮询）
  assert.equal(shouldPollMemory({ state: 'ready', lag: null }), false)
  assert.equal(shouldPollMemory({ state: 'ready', lag: undefined }), false)
  // 安定/失败/空 → 不轮询
  assert.equal(shouldPollMemory({ state: 'idle', lag: false }), false)
  assert.equal(shouldPollMemory({ state: 'failed', lag: false }), false)
  assert.equal(shouldPollMemory({ state: 'disabled', lag: false }), false)
  assert.equal(shouldPollMemory(null), false)
  assert.equal(shouldPollMemory(undefined), false)
})

test('canViewSummary: published summary is viewable even while pending/failed (read-only entry)', () => {
  // 主审点 6：ready 恒可看；queued/processing/failed 若此前已发布过摘要（有版本号）也可只读查看。
  assert.equal(canViewSummary('ready', 5), true)
  assert.equal(canViewSummary('ready', null), true)
  assert.equal(canViewSummary('queued', 5), true)
  assert.equal(canViewSummary('processing', 3), true)
  assert.equal(canViewSummary('failed', 2), true)
  // 从未发布摘要 → 无可看入口
  assert.equal(canViewSummary('queued', null), false)
  assert.equal(canViewSummary('processing', null), false)
  assert.equal(canViewSummary('failed', null), false)
  // disabled/idle 无摘要可看
  assert.equal(canViewSummary('idle', 5), false)
  assert.equal(canViewSummary('disabled', 5), false)
})
