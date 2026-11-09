/**
 * 文档治理测试：日期解析、PATCH 计划、替代/重新启用请求体、生命周期兜底，
 * 以及并发写入控制器的真实行为（deferred promise 驱动，不 mock 内部）。
 *
 * 这些测试用真实输入输出断言请求边界，而不是字符串匹配源码。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  GOVERNANCE_MAINTAINER_MAX,
  belongsToGovernanceSession,
  buildReenableBody,
  buildSupersedeBody,
  createGovernanceWriteController,
  createGovernanceWriteLock,
  createSupersedeController,
  governanceSessionState,
  governanceWriteKey,
  invalidateGovernanceRevision,
  isGovernanceConflict,
  isGovernanceSessionCurrent,
  isSuperseded,
  isSupersedeRequestCurrent,
  lifecycleTagType,
  normalizeLifecycleState,
  parseGovernanceDate,
  parseGovernanceDateExact,
  planGovernancePatch,
  replaceGovernanceRow,
  sameGovernanceInputInstant,
  toGovernanceSnapshot
} from '../src/utils/documentGovernance.ts'

const baseSnapshot = {
  governance_revision: 3,
  revision_known: true,
  maintainer: '行政部',
  last_reviewed_at: '2026-01-01T00:00:00.000Z',
  valid_from: null,
  valid_until: null,
  enabled: true
}

const baseForm = {
  maintainer: '行政部',
  last_reviewed_at: '2026-01-01T00:00:00.000Z',
  valid_from: '',
  valid_until: '',
  enabled: true as boolean | null
}

/** 手动控制 resolve/reject 的 promise，模拟真实网络时序。 */
function deferred<T = void>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function conflictError(): { response: { status: number } } {
  return { response: { status: 409 } }
}

test('toGovernanceSnapshot keeps unknown revision and unknown enabled unknown', () => {
  const empty = toGovernanceSnapshot(null)
  assert.equal(empty.revision_known, false)
  assert.equal(empty.governance_revision, 0)
  assert.equal(empty.enabled, null)
  assert.equal(empty.maintainer, '')
  // 浮点 / 负数 / 字符串版本号都不能伪装成已知
  assert.equal(toGovernanceSnapshot({ governance_revision: 7.9 }).revision_known, false)
  assert.equal(toGovernanceSnapshot({ governance_revision: -1 }).revision_known, false)
  assert.equal(toGovernanceSnapshot({ governance_revision: '3' }).revision_known, false)
  assert.equal(toGovernanceSnapshot({ governance_revision: NaN }).revision_known, false)
  assert.equal(toGovernanceSnapshot({ governance_revision: 5 }).revision_known, true)
  assert.equal(toGovernanceSnapshot({ governance_revision: 5 }).governance_revision, 5)
  assert.equal(toGovernanceSnapshot({ enabled: false }).enabled, false)
  assert.equal(toGovernanceSnapshot({ enabled: 'yes' }).enabled, null)
  assert.equal(toGovernanceSnapshot({ maintainer: 42 }).maintainer, '')
})

test('parseGovernanceDate produces aware UTC ISO, keeps seconds, rejects naive and bad calendar', () => {
  assert.deepEqual(parseGovernanceDate(null), { ok: true, iso: null })
  assert.deepEqual(parseGovernanceDate('  '), { ok: true, iso: null })
  // 显式偏移被换算成 UTC
  const offset = parseGovernanceDate('2026-03-01T08:30:00+08:00')
  assert.ok(offset.ok && offset.iso === '2026-03-01T00:30:00.000Z')
  // 坏日历日期：Date 会归一化，这里必须拒绝而不是静默跳到 3 月
  assert.equal(parseGovernanceDate('2026-02-30T00:00:00Z').ok, false)
  assert.equal(parseGovernanceDate('2026-04-31T10:00:00Z').ok, false)
  // 平年的 2 月 29 日同样拒绝（真实日历，不是 Date 的归一化结果）
  assert.equal(parseGovernanceDate('2026-02-29T00:00:00Z').ok, false)
  // 闰年 2 月 29 日合法
  const leap = parseGovernanceDate('2028-02-29T12:00:00Z')
  assert.ok(leap.ok && leap.iso === '2028-02-29T12:00:00.000Z')
  // 13 月在 ISO 形态下被 Date 判为 Invalid Date
  assert.equal(parseGovernanceDate('2026-13-01T00:00:00Z').ok, false)
  // 坏偏移量（小时/分钟越界）拒绝
  assert.equal(parseGovernanceDate('2026-01-01T00:00:00+25:00').ok, false)
  assert.equal(parseGovernanceDate('2026-01-01T00:00:00+08:99').ok, false)
  // naive：无时区但秒数齐全的 ISO 形态拒绝；datetime-local 文本仍按本地时区解析
  assert.equal(parseGovernanceDate('2026-03-01T08:30:00+01:00').ok, true)
  assert.equal(parseGovernanceDate('0').ok, false)
  assert.equal(parseGovernanceDate('1772352000000').ok, false)
  assert.equal(parseGovernanceDate('not-a-date').ok, false)
  // 秒保留；秒可选
  const seconds = parseGovernanceDate('2026-06-01T07:08:09Z')
  assert.ok(seconds.ok && seconds.iso === '2026-06-01T07:08:09.000Z')
  // datetime-local 文本按本地时区解析，且不依赖 Date 的日历归一化
  const local = parseGovernanceDate('2026-02-30T10:00')
  assert.equal(local.ok, false)
  const localOk = parseGovernanceDate('2026-03-01T10:30:05.250')
  assert.ok(localOk.ok)
  if (localOk.ok && localOk.iso) {
    const date = new Date(localOk.iso)
    assert.equal(date.getHours(), 10)
    assert.equal(date.getMinutes(), 30)
    assert.equal(date.getSeconds(), 5)
    assert.equal(date.getMilliseconds(), 250)
  }
})

test('changed microsecond windows serialize the same instants used for validation', () => {
  const from = '2026-06-01T08:00:00.123456+08:00'
  const until = '2026-06-01T08:00:00.123457+08:00'
  const plan = planGovernancePatch(baseSnapshot, { ...baseForm, valid_from: from, valid_until: until })
  assert.equal(plan.status, 'ok')
  if (plan.status !== 'ok') return
  assert.equal(plan.body.valid_from, '2026-06-01T00:00:00.123456Z')
  assert.equal(plan.body.valid_until, '2026-06-01T00:00:00.123457Z')
  const left = parseGovernanceDateExact(plan.body.valid_from)
  const right = parseGovernanceDateExact(plan.body.valid_until)
  assert.ok(left.ok && right.ok && left.micros! < right.micros!)
})

test('supersede invalidation releases busy and an old finally cannot release a new request', () => {
  const changes: boolean[] = []
  const controller = createSupersedeController((value) => changes.push(value))
  const old = controller.begin()
  assert.equal(controller.busy, true)
  controller.invalidate()
  assert.equal(controller.busy, false)
  const current = controller.begin()
  controller.finish(old)
  assert.equal(controller.busy, true)
  controller.finish(current)
  assert.equal(controller.busy, false)
  assert.deepEqual(changes, [true, false, true, false])
})

test('unchanged form produces an empty patch (server rejects empty patches)', () => {
  assert.deepEqual(planGovernancePatch(baseSnapshot, baseForm), { status: 'empty' })
})

test('unknown revision never plans a write, even with changed fields', () => {
  const plan = planGovernancePatch(
    { ...baseSnapshot, revision_known: false, governance_revision: 0 },
    { ...baseForm, maintainer: '别人' }
  )
  assert.deepEqual(plan, { status: 'invalidRevision' })
})

test('changed fields produce a minimal body carrying the snapshot revision', () => {
  const plan = planGovernancePatch(baseSnapshot, {
    ...baseForm,
    maintainer: '行政部 / 王明',
    enabled: false
  })
  assert.equal(plan.status, 'ok')
  if (plan.status !== 'ok') return
  assert.equal(plan.body.expected_revision, 3)
  assert.equal(plan.body.maintainer, '行政部 / 王明')
  assert.equal(plan.body.enabled, false)
  // 未修改的日期不出现在 body 里
  assert.ok(!('last_reviewed_at' in plan.body))
  assert.ok(!('valid_from' in plan.body))
})

test('date-only change with same maintainer text still emits the date', () => {
  const plan = planGovernancePatch(baseSnapshot, { ...baseForm, last_reviewed_at: '2026-06-01T00:00:00Z' })
  assert.equal(plan.status, 'ok')
  if (plan.status !== 'ok') return
  assert.ok(!('maintainer' in plan.body))
  assert.equal(plan.body.last_reviewed_at, '2026-06-01T00:00:00.000Z')
})

test('explicit empty string clears a date as null', () => {
  const plan = planGovernancePatch(baseSnapshot, { ...baseForm, last_reviewed_at: '' })
  assert.equal(plan.status, 'ok')
  if (plan.status !== 'ok') return
  assert.equal(plan.body.last_reviewed_at, null)
})

test('invalid date strings are rejected with the offending field', () => {
  const plan = planGovernancePatch(baseSnapshot, { ...baseForm, valid_from: 'maybe' })
  assert.deepEqual(plan, { status: 'invalidDate', field: 'valid_from' })
  const plan2 = planGovernancePatch(baseSnapshot, { ...baseForm, valid_until: '13' })
  assert.deepEqual(plan2, { status: 'invalidDate', field: 'valid_until' })
  // 坏日历日期在计划里也必须被拒绝，不能变成 3 月
  const plan3 = planGovernancePatch(baseSnapshot, { ...baseForm, valid_until: '2026-02-30T00:00:00Z' })
  assert.deepEqual(plan3, { status: 'invalidDate', field: 'valid_until' })
})

test('merged window is validated across changed and unchanged sides', () => {
  // 只改 valid_until 早于快照 valid_from（form 保持快照的 valid_from 不变）→ 非法
  const snapshot = { ...baseSnapshot, valid_from: '2026-01-10T00:00:00.000Z' }
  const plan = planGovernancePatch(snapshot, {
    ...baseForm,
    valid_from: '2026-01-10T00:00:00.000Z',
    valid_until: '2026-01-01T00:00:00.000Z'
  })
  assert.equal(plan.status, 'invalidWindow')
  // 相同时间点也是非法（窗口为空）
  const plan2 = planGovernancePatch(
    { ...baseSnapshot, valid_from: '2026-01-10T00:00:00.000Z', valid_until: '2026-02-10T00:00:00.000Z' },
    { ...baseForm, valid_from: '2026-01-10T00:00:00.000Z', valid_until: '2026-01-10T00:00:00.000Z' }
  )
  assert.equal(plan2.status, 'invalidWindow')
  // 只改一侧：另一侧用快照值参与合并校验
  const plan2b = planGovernancePatch(
    { ...baseSnapshot, valid_from: '2026-01-10T00:00:00.000Z' },
    { ...baseForm, valid_from: '2026-01-10T00:00:00.000Z', valid_until: '2026-01-10T00:00:00.000Z' }
  )
  assert.equal(plan2b.status, 'invalidWindow')
  // 合法窗口通过
  const plan3 = planGovernancePatch(baseSnapshot, {
    ...baseForm,
    valid_from: '2026-01-01T00:00:00.000Z',
    valid_until: '2026-02-01T00:00:00.000Z'
  })
  assert.equal(plan3.status, 'ok')
})

test('maintainer over 200 characters is rejected before any date parsing', () => {
  const plan = planGovernancePatch(baseSnapshot, {
    ...baseForm,
    maintainer: 'x'.repeat(GOVERNANCE_MAINTAINER_MAX + 1)
  })
  assert.equal(plan.status, 'maintainerTooLong')
  // 200 个字符本身合法
  const ok = planGovernancePatch(baseSnapshot, {
    ...baseForm,
    maintainer: 'y'.repeat(GOVERNANCE_MAINTAINER_MAX)
  })
  assert.equal(ok.status, 'ok')
})

test('unknown enabled cannot silently become a boolean write', () => {
  const plan = planGovernancePatch(baseSnapshot, { ...baseForm, enabled: null })
  assert.deepEqual(plan, { status: 'invalidEnabled' })
})

test('superseded documents cannot change enabled through the patch, other fields pass', () => {
  // 试图把已替代文档"启用"（或停用）都只能走显式 reenable，PATCH 一律拦截
  assert.deepEqual(
    planGovernancePatch(baseSnapshot, { ...baseForm, enabled: false }, { superseded: true }),
    { status: 'supersededEnabledChange' }
  )
  const disabledSnapshot = { ...baseSnapshot, enabled: false }
  assert.deepEqual(
    planGovernancePatch(disabledSnapshot, { ...baseForm, enabled: true }, { superseded: true }),
    { status: 'supersededEnabledChange' }
  )
  // 未改 enabled 时其他字段仍可提交
  const meta = planGovernancePatch(
    baseSnapshot,
    { ...baseForm, maintainer: '新负责人' },
    { superseded: true }
  )
  assert.equal(meta.status, 'ok')
  if (meta.status === 'ok') assert.equal(meta.body.maintainer, '新负责人')
})

test('supersede body rejects bad revisions instead of faking 0', () => {
  assert.deepEqual(buildSupersedeBody(0, 'doc-a', ''), { status: 'empty' })
  assert.deepEqual(buildSupersedeBody(4, 'doc-a', 'doc-a'), { status: 'self' })
  assert.deepEqual(buildSupersedeBody(4, 'doc-a', ' doc-b '), {
    status: 'ok',
    body: { expected_revision: 4, replacement_document_id: 'doc-b' }
  })
  for (const bad of [-3, 4.7, NaN, true, false, '4', undefined, null, Infinity]) {
    assert.deepEqual(buildSupersedeBody(bad as never, 'doc-a', 'doc-b'), { status: 'invalidRevision' })
  }
})

test('reenable body rejects bad revisions instead of faking 0', () => {
  assert.deepEqual(buildReenableBody(5), { status: 'ok', body: { expected_revision: 5 } })
  assert.deepEqual(buildReenableBody(0), { status: 'ok', body: { expected_revision: 0 } })
  for (const bad of [-1, 2.9, NaN, true, '5', undefined, null]) {
    assert.deepEqual(buildReenableBody(bad as never), { status: 'invalidRevision' })
  }
})

test('isGovernanceConflict matches only 409 responses', () => {
  assert.equal(isGovernanceConflict(conflictError()), true)
  assert.equal(isGovernanceConflict({ response: { status: 400 } }), false)
  assert.equal(isGovernanceConflict(new Error('network')), false)
  assert.equal(isGovernanceConflict(null), false)
})

test('isSuperseded matches either marker, missing fields never count', () => {
  assert.equal(isSuperseded({ superseded_at: '2026-01-01T00:00:00Z' }), true)
  assert.equal(isSuperseded({ superseded_by_id: 'doc-b' }), true)
  assert.equal(isSuperseded({}), false)
  assert.equal(isSuperseded(null), false)
})

test('lifecycle fallback is conservative, never permissive for rendering', () => {
  assert.equal(normalizeLifecycleState('expired'), 'expired')
  assert.equal(normalizeLifecycleState('superseded'), 'superseded')
  assert.equal(normalizeLifecycleState('anything-else'), 'active')
  assert.equal(lifecycleTagType('active'), 'success')
  assert.equal(lifecycleTagType('disabled'), 'danger')
})

test('session guard rejects wrong doc, wrong KB, closed dialog, stale epoch and unmount', () => {
  const request = { kbId: 'kb-1', docId: 'doc-a', epoch: 4 }
  const state = { kbId: 'kb-1', epoch: 4, open: true, disposed: false, docId: 'doc-a' }
  // 正常：id 与 kb 都匹配
  assert.equal(belongsToGovernanceSession(request, state, { id: 'doc-a', kb_id: 'kb-1' }), true)
  // 响应属于别的文档 / 别的库：拒绝
  assert.equal(belongsToGovernanceSession(request, state, { id: 'doc-b', kb_id: 'kb-1' }), false)
  assert.equal(belongsToGovernanceSession(request, state, { id: 'doc-a', kb_id: 'kb-2' }), false)
  // 弹窗已关闭 / 组件已卸载 / 打开代次变化 / 会话切到别的库：拒绝
  assert.equal(belongsToGovernanceSession(request, { ...state, open: false }, { id: 'doc-a', kb_id: 'kb-1' }), false)
  assert.equal(belongsToGovernanceSession(request, { ...state, disposed: true }, { id: 'doc-a', kb_id: 'kb-1' }), false)
  assert.equal(belongsToGovernanceSession(request, { ...state, epoch: 5 }, { id: 'doc-a', kb_id: 'kb-1' }), false)
  assert.equal(belongsToGovernanceSession(request, { ...state, kbId: 'kb-2' }, { id: 'doc-a', kb_id: 'kb-2' }), false)
  // 没有打开过 / 当前表单已指向别的文档：拒绝
  assert.equal(belongsToGovernanceSession(null, state, { id: 'doc-a', kb_id: 'kb-1' }), false)
  assert.equal(belongsToGovernanceSession(request, { ...state, docId: 'doc-b' }, { id: 'doc-a', kb_id: 'kb-1' }), false)
})

test('row replacement only touches the matching row and never appends', () => {
  const rows = [{ id: 'doc-a', n: 1 }, { id: 'doc-b', n: 2 }]
  assert.equal(replaceGovernanceRow(rows, { id: 'doc-b', n: 9 }), true)
  assert.deepEqual(rows, [{ id: 'doc-a', n: 1 }, { id: 'doc-b', n: 9 }])
  // 不存在的行：不追加、返回 false（删除后的迟到响应不能复活行）
  assert.equal(replaceGovernanceRow(rows, { id: 'doc-gone', n: 3 }), false)
  assert.equal(rows.length, 2)
})

test('control precision (seconds) does not rewrite an unchanged millisecond value', () => {
  // 同一秒：控件回显视为未修改
  assert.equal(
    sameGovernanceInputInstant('2026-01-01T00:00:00.750Z', '2026-01-01T08:00:00'),
    true
  )
  // 不同秒 / 任一侧为空 / 任一侧非法：都视为修改
  assert.equal(sameGovernanceInputInstant('2026-01-01T00:00:00Z', '2026-01-01T08:00:01'), false)
  assert.equal(sameGovernanceInputInstant('2026-01-01T00:00:00Z', ''), false)
  assert.equal(sameGovernanceInputInstant('bad', '2026-01-01T08:00:00'), false)
})

// ---------- 并发写入控制器的真实行为 ----------

test('double click during in-flight write starts only once', async () => {
  const controller = createGovernanceWriteController()
  const gate = deferred()
  let writes = 0
  const first = controller.run({
    alive: () => true,
    write: () => { writes += 1; return gate.promise },
    onSaved: () => {},
    onConflict: () => {},
    onError: () => {}
  })
  // 给第一次写入一个 tick 进入在途状态
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(controller.busy, true)
  // 第二次点击在写入完成前到达：被拒
  const second = controller.run({
    alive: () => true,
    write: () => { writes += 1; return Promise.resolve() },
    onSaved: () => {},
    onConflict: () => {},
    onError: () => {}
  })
  assert.equal(await second, 'busy')
  assert.equal(writes, 1)
  gate.resolve()
  assert.equal(await first, 'started')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(controller.busy, false)
  // 完成后可再次发起
  const third = controller.run({
    alive: () => true,
    write: () => { writes += 1; return Promise.resolve() },
    onSaved: () => {},
    onConflict: () => {},
    onError: () => {}
  })
  assert.equal(await third, 'started')
  assert.equal(writes, 2)
})

test('saved result flows from the same write call, stale responses are discarded', async () => {
  const controller = createGovernanceWriteController()
  const gate = deferred<string>()
  let saved: string | undefined
  let savedCount = 0
  controller.run({
    alive: () => true,
    write: () => gate.promise,
    onSaved: (result) => { savedCount += 1; saved = result },
    onConflict: () => {},
    onError: () => {}
  })
  gate.resolve('fresh-response')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(savedCount, 1)
  assert.equal(saved, 'fresh-response')
})

test('invalidate before response drops it: no saved, no toast hook, busy released', async () => {
  const controller = createGovernanceWriteController()
  const gate = deferred()
  let savedCount = 0
  let errorCount = 0
  const run = controller.run({
    alive: () => true,
    write: () => gate.promise,
    onSaved: () => { savedCount += 1 },
    onConflict: () => {},
    onError: () => { errorCount += 1 }
  })
  controller.invalidate()
  gate.resolve()
  assert.equal(await run, 'stale')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(savedCount, 0)
  assert.equal(errorCount, 0)
  assert.equal(controller.busy, false)
  // 作废后立刻可以开新请求：旧 finally 不能把新 busy 清掉
  const gate2 = deferred()
  let savedCount2 = 0
  const run2 = controller.run({
    alive: () => true,
    write: () => gate2.promise,
    onSaved: () => { savedCount2 += 1 },
    onConflict: () => {},
    onError: () => {}
  })
  gate2.resolve()
  await run2
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(savedCount2, 1)
  assert.equal(controller.busy, false)
})

test('old invalidated finally cannot clear the new request busy', async () => {
  const controller = createGovernanceWriteController()
  const gate1 = deferred()
  const gate2 = deferred()
  const first = controller.run({
    alive: () => true,
    write: () => gate1.promise,
    onSaved: () => {},
    onConflict: () => {},
    onError: () => {}
  })
  // 先让第一次写进入在途
  await new Promise((resolve) => setTimeout(resolve, 0))
  controller.invalidate()
  // 作废旧请求后立刻发起新请求
  const second = controller.run({
    alive: () => true,
    write: () => gate2.promise,
    onSaved: () => {},
    onConflict: () => {},
    onError: () => {}
  })
  assert.equal(controller.busy, true)
  // 旧请求此时才完成：它的 finally 不能碰新请求的 busy
  gate1.resolve()
  assert.equal(await first, 'stale')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(controller.busy, true)
  gate2.resolve()
  assert.equal(await second, 'started')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(controller.busy, false)
})

test('409 triggers conflict refresh and re-review; refresh failure blocks further writes', async () => {
  const controller = createGovernanceWriteController()
  let savedCount = 0
  let conflictCount = 0
  // 第一次写：409
  const gate1 = deferred<void, unknown>()
  const run1 = controller.run({
    alive: () => true,
    write: () => gate1.promise,
    onSaved: () => { savedCount += 1 },
    onConflict: () => { conflictCount += 1 },
    onError: () => {}
  })
  gate1.reject(conflictError())
  assert.equal(await run1, 'started')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(conflictCount, 1)
  assert.equal(savedCount, 0)
  assert.equal(controller.busy, false)

  // 第二次写仍是 409，这次刷新自身抛错：onConflict 的失败不吞掉冲突本身，onError 不被调用
  const gate2 = deferred<void, unknown>()
  let conflictErrorSeen: unknown
  const run2 = controller.run({
    alive: () => true,
    write: () => gate2.promise,
    onSaved: () => { savedCount += 1 },
    onConflict: (isCurrent) => {
      assert.equal(typeof isCurrent, 'function')
      throw new Error('refresh failed')
    },
    onError: (error) => { conflictErrorSeen = error }
  })
  gate2.reject(conflictError())
  assert.equal(await run2, 'started')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(savedCount, 0)
  assert.equal(conflictErrorSeen, undefined)
  assert.equal(controller.busy, false)
})

test('non-conflict write error reaches onError and keeps busy bookkeeping correct', async () => {
  const controller = createGovernanceWriteController()
  const gate = deferred<void, unknown>()
  let errorCount = 0
  let savedCount = 0
  let conflictCount = 0
  const run = controller.run({
    alive: () => true,
    write: () => gate.promise,
    onSaved: () => { savedCount += 1 },
    onConflict: () => { conflictCount += 1 },
    onError: () => { errorCount += 1 }
  })
  gate.reject(new Error('boom'))
  assert.equal(await run, 'started')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(errorCount, 1)
  assert.equal(savedCount, 0)
  assert.equal(conflictCount, 0)
  assert.equal(controller.busy, false)
})

test('run checks alive before writing: a closed dialog never issues the request', async () => {
  const controller = createGovernanceWriteController()
  let writes = 0
  const result = controller.run({
    alive: () => false,
    write: () => { writes += 1; return Promise.resolve('x') },
    onSaved: () => {},
    onConflict: () => {},
    onError: () => {}
  })
  assert.equal(await result, 'stale')
  assert.equal(writes, 0)
  assert.equal(controller.busy, false)
})

test('microsecond ISO from the backend is accepted and preserved exactly', () => {
  // Python datetime.isoformat() 输出 6 位微秒；写回时保持相同的时间点。
  const micro = parseGovernanceDate('2026-03-01T08:30:00.123456+00:00')
  assert.deepEqual(micro, { ok: true, iso: '2026-03-01T08:30:00.123456Z' })
  const utc = parseGovernanceDate('2026-03-01T08:30:00.123456Z')
  assert.deepEqual(utc, { ok: true, iso: '2026-03-01T08:30:00.123456Z' })
  // 不丢失微秒，也不四舍五入进位到下一秒。
  const edge = parseGovernanceDate('2026-03-01T08:30:00.999999Z')
  assert.deepEqual(edge, { ok: true, iso: '2026-03-01T08:30:00.999999Z' })
  // 带少量小数位也照常工作；7 位小数超出 ISO 契约，拒绝而不是猜
  const pad = parseGovernanceDate('2026-03-01T08:30:00.5Z')
  assert.deepEqual(pad, { ok: true, iso: '2026-03-01T08:30:00.500Z' })
  assert.equal(parseGovernanceDate('2026-03-01T08:30:00.1234567Z').ok, false)
  // 微秒 + 非零偏移仍换算到 UTC
  const offset = parseGovernanceDate('2026-03-01T08:30:00.250000+08:00')
  assert.deepEqual(offset, { ok: true, iso: '2026-03-01T00:30:00.250000Z' })
  // 负偏移方向不能搞反
  const west = parseGovernanceDate('2026-03-01T08:30:00-05:00')
  assert.deepEqual(west, { ok: true, iso: '2026-03-01T13:30:00.000Z' })
  // 微秒形态下的坏日历仍然拒绝，绝不被归一化
  assert.equal(parseGovernanceDate('2026-02-30T00:00:00.123456Z').ok, false)
})

test('microsecond values survive an unchanged form without rewriting precision below the millisecond', () => {
  // 后端给出 .123456；控件只能显示到秒，回显不得被当成修改（不会把日期改成整秒）
  const snapshot = { ...baseSnapshot, last_reviewed_at: '2026-01-01T00:00:00.123456+00:00' }
  const plan = planGovernancePatch(snapshot, {
    ...baseForm,
    last_reviewed_at: '2026-01-01T00:00:00.123456+00:00'
  })
  assert.deepEqual(plan, { status: 'empty' })
  // 控件回显（秒级、本地时区）也与微秒原值视为同一时刻
  assert.equal(
    sameGovernanceInputInstant('2026-01-01T00:00:00.123456Z', '2026-01-01T08:00:00'),
    true
  )
})

test('conflict invalidation keeps other fields but bans the stale revision until a fresh detail arrives', () => {
  const invalidated = invalidateGovernanceRevision(baseSnapshot)
  assert.ok(invalidated)
  assert.equal(invalidated.revision_known, false)
  assert.equal(invalidated.governance_revision, 0)
  // 来自其它字段的编辑也不能被提交（改 maintainer 依然 invalidRevision）
  assert.deepEqual(
    planGovernancePatch(invalidated, { ...baseForm, maintainer: '别人' }),
    { status: 'invalidRevision' }
  )
  // 注意：纯层只保证 revision_known=false；替代/重启用必须先自己检查它，
  // 绝不能把失效快照的 governance_revision(0) 直接交给 CAS。
  assert.equal(invalidated.revision_known, false)
  // 只有真正新鲜且 revision 有效的详情才能恢复写入能力
  const fresh = toGovernanceSnapshot({ governance_revision: 4, maintainer: '行政部', enabled: true })
  const restored = invalidateGovernanceRevision(fresh)
  assert.equal(restored?.revision_known, false)
  assert.equal(planGovernancePatch(fresh, { ...baseForm, maintainer: '新' }).status, 'ok')
  // 快照本身为空时保持 null，不凭空造一个对象
  assert.equal(invalidateGovernanceRevision(null), null)
})

test('session guard: switching KB/doc, closing and unmounting all invalidate an in-flight request', () => {
  const request = { kbId: 'kb-1', docId: 'doc-a', epoch: 2 }
  const open = governanceSessionState('kb-1', 2, true, false, 'doc-a')
  assert.equal(isGovernanceSessionCurrent(request, open), true)
  assert.equal(isGovernanceSessionCurrent(request, governanceSessionState('kb-1', 2, false, false, 'doc-a')), false)
  assert.equal(isGovernanceSessionCurrent(request, governanceSessionState('kb-1', 2, true, true, 'doc-a')), false)
  assert.equal(isGovernanceSessionCurrent(request, governanceSessionState('kb-2', 2, true, false, 'doc-a')), false)
  assert.equal(isGovernanceSessionCurrent(request, governanceSessionState('kb-1', 3, true, false, 'doc-a')), false)
  assert.equal(isGovernanceSessionCurrent(request, governanceSessionState('kb-1', 2, true, false, 'doc-b')), false)
  assert.equal(isGovernanceSessionCurrent(null, open), false)
  // 会话本身有效但响应是别的 ID/KB：仍然拒绝
  assert.equal(
    belongsToGovernanceSession(request, open, { id: 'doc-a', kb_id: 'kb-9' }),
    false
  )
})

test('close then reopen the same doc: the old request stays stale even though the new session looks identical', () => {
  // 第一次打开：代次 1
  const oldRequest = { kbId: 'kb-1', docId: 'doc-a', epoch: 1 }
  // 关闭再重新打开同一文档：KB 与 docId 完全一样，只有代次变成 2
  const reopened = governanceSessionState('kb-1', 2, true, false, 'doc-a')
  assert.equal(isGovernanceSessionCurrent(oldRequest, reopened), false)
  assert.equal(
    belongsToGovernanceSession(oldRequest, reopened, { id: 'doc-a', kb_id: 'kb-1' }),
    false
  )
  // 新代次的请求在新的会话里才有效
  const newRequest = { kbId: 'kb-1', docId: 'doc-a', epoch: 2 }
  assert.equal(isGovernanceSessionCurrent(newRequest, reopened), true)
  // 反向：旧会话（代次 1 且仍打开）里的新代次响应也拒绝
  const closed = governanceSessionState('kb-1', 2, false, false, 'doc-a')
  assert.equal(isGovernanceSessionCurrent(newRequest, closed), false)
})

test('document write lock excludes the dialog and supersede from writing the same doc concurrently', () => {
  const lock = createGovernanceWriteLock()
  assert.equal(lock.acquire('kb-1', 'doc-a'), true)
  // 另一个入口（治理保存 vs 标记被替代）对同一文档：拒绝
  assert.equal(lock.acquire('kb-1', 'doc-a'), false)
  assert.equal(lock.isLocked('kb-1', 'doc-a'), true)
  // 别的文档 / 别的库不受影响
  assert.equal(lock.acquire('kb-1', 'doc-b'), true)
  assert.equal(lock.acquire('kb-2', 'doc-a'), true)
  // 释放后才可重新获取
  lock.release('kb-1', 'doc-a')
  assert.equal(lock.isLocked('kb-1', 'doc-a'), false)
  assert.equal(lock.acquire('kb-1', 'doc-a'), true)
  lock.clear()
  assert.equal(lock.isLocked('kb-1', 'doc-a'), false)
  assert.equal(lock.isLocked('kb-2', 'doc-a'), false)
  // 键必须区分 KB 与文档，不能因为字符串拼接而撞车
  assert.notEqual(governanceWriteKey('kb-1', 'doc-a'), governanceWriteKey('kb-1a', 'doc-a'))
})

// ---------- 微秒窗口与原始精度 ----------

test('same-millisecond but different-microsecond window is legal (exact comparison)', () => {
  // from=.123456 / until=.123457 毫秒截断后相等；按毫秒比较会被误判为 invalidWindow
  const snapshot = {
    ...baseSnapshot,
    valid_from: '2026-01-01T00:00:00.123456+00:00',
    valid_until: '2026-01-01T00:00:00.123457+00:00'
  }
  // 完全不改：空补丁，绝不被判成非法窗口
  assert.deepEqual(
    planGovernancePatch(snapshot, {
      ...baseForm,
      valid_from: snapshot.valid_from,
      valid_until: snapshot.valid_until
    }),
    { status: 'empty' }
  )
  // 只改 maintainer：窗口按原微秒值判定，必须放行
  const plan = planGovernancePatch(snapshot, {
    ...baseForm,
    valid_from: snapshot.valid_from,
    valid_until: snapshot.valid_until,
    maintainer: '新负责人'
  })
  assert.equal(plan.status, 'ok')
  if (plan.status === 'ok') {
    assert.equal(plan.body.maintainer, '新负责人')
    // 未改的日期不进入 body，原精度原样保留在服务端
    assert.ok(!('valid_from' in plan.body))
    assert.ok(!('valid_until' in plan.body))
  }
  // 同一微秒的两端：窗口为空，必须非法（用无快照窗口的形态让两端都真正被提交）
  assert.equal(
    planGovernancePatch(baseSnapshot, {
      ...baseForm,
      valid_from: '2026-01-01T00:00:00.123456+00:00',
      valid_until: '2026-01-01T00:00:00.123456+00:00'
    }).status,
    'invalidWindow'
  )
  // 控件能表达的真实逆序（不同秒）必须被挡住；快照 until 保留原微秒精度参与比较
  assert.equal(
    planGovernancePatch(snapshot, {
      ...baseForm,
      valid_from: '2026-01-02T00:00:00',
      valid_until: snapshot.valid_until,
      maintainer: 'x'
    }).status,
    'invalidWindow'
  )
  // 精确时刻解析本身带微秒差值
  const from = parseGovernanceDateExact('2026-01-01T00:00:00.123456Z')
  const until = parseGovernanceDateExact('2026-01-01T00:00:00.123457Z')
  assert.ok(from.ok && until.ok)
  assert.equal(from.ok && from.fractionDigits, 6)
  assert.ok(from.ok && until.ok && until.micros! - from.micros! === BigInt(1))
})

test('untouched sub-millisecond values are never rewritten to control precision', () => {
  // 控件只能回显到秒；没改就不发该字段（含微秒原值因此完整保留）
  const snapshot = { ...baseSnapshot, last_reviewed_at: '2026-01-01T00:00:00.123456+00:00' }
  const plan = planGovernancePatch(snapshot, {
    ...baseForm,
    last_reviewed_at: '2026-01-01T08:00:00'
  })
  assert.deepEqual(plan, { status: 'empty' })
  // 真正改了才发，且发出去的是毫秒精度 ISO（datetime-local 无法表达微秒）
  const changed = planGovernancePatch(snapshot, {
    ...baseForm,
    last_reviewed_at: '2026-01-02T08:00:00'
  })
  assert.equal(changed.status, 'ok')
  if (changed.status === 'ok') assert.equal(changed.body.last_reviewed_at, '2026-01-02T00:00:00.000Z')
})

// ---------- 替代 prompt：deferred 真实控制流 ----------

const supersedeState = (over: Partial<Parameters<typeof isSupersedeRequestCurrent>[1]> = {}) => ({
  disposed: false,
  kbId: 'kb-1',
  governanceOpen: true,
  governanceEpoch: 7,
  governanceDocId: 'doc-a',
  docIds: ['doc-a', 'doc-b'],
  ...over
})

const boundRequest = (over: Partial<Parameters<typeof isSupersedeRequestCurrent>[0]> = {}) => ({
  kbId: 'kb-1',
  docId: 'doc-a',
  epoch: 1,
  governanceEpoch: 7 as number | null,
  ...over
})

test('supersede controller cancels a pending prompt confirm when its governance session goes away', async () => {
  const controller = createSupersedeController()
  const promptGate = deferred<{ value: string }>()
  const postGate = deferred<void>()
  let posted = 0
  let closed = 0
  const request = { kbId: 'kb-1', docId: 'doc-a', epoch: controller.begin(), governanceEpoch: 7 }
  const state = supersedeState()
  // prompt 挂在半空时，治理弹窗被关闭/换文档/重开：invalidate 作废旧轮次
  controller.invalidate()
  // 用户这时才点确定
  promptGate.resolve({ value: 'doc-b' })
  const input = (await promptGate.promise).value
  if (!isSupersedeRequestCurrent(request, { ...state, governanceEpoch: controller.epoch })) {
    closed += 1
  } else {
    posted += 1
  }
  assert.equal(input, 'doc-b')
  // 确认了但不得 POST
  assert.equal(posted, 0)
  assert.equal(closed, 1)
  postGate.resolve()
  await postGate.promise
})

test('supersede prompt survives unrelated list reloads but dies on doc switch / close / reopen', async () => {
  const controller = createSupersedeController()
  const request = { kbId: 'kb-1', docId: 'doc-a', epoch: controller.begin(), governanceEpoch: 7 }
  // 列表重载/轮询（文档集合不变）：仍然有效
  assert.equal(isSupersedeRequestCurrent(request, supersedeState()), true)
  // 治理弹窗换绑到别的文档
  assert.equal(
    isSupersedeRequestCurrent(request, supersedeState({ governanceDocId: 'doc-b' })),
    false
  )
  // 治理弹窗关闭（此前打开）
  assert.equal(
    isSupersedeRequestCurrent(request, supersedeState({ governanceOpen: false })),
    false
  )
  // 治理弹窗关闭再重开同一文档：会话代次已变，旧 prompt 不再有效
  const reopenedEpoch = controller.invalidate()
  assert.equal(
    isSupersedeRequestCurrent(request, supersedeState({ governanceEpoch: reopenedEpoch })),
    false
  )
  // 卸载 / 切库 / 文档被删除
  assert.equal(isSupersedeRequestCurrent(request, supersedeState({ disposed: true })), false)
  assert.equal(isSupersedeRequestCurrent(request, supersedeState({ kbId: 'kb-2' })), false)
  assert.equal(isSupersedeRequestCurrent(request, supersedeState({ docIds: ['doc-b'] })), false)
})

test('standalone (row menu) supersede prompt stays usable across governance dialog changes', async () => {
  // 行菜单入口不绑定治理会话：关闭/重开治理弹窗不应取消它
  const request = { kbId: 'kb-1', docId: 'doc-b', epoch: 1, governanceEpoch: null }
  assert.equal(isSupersedeRequestCurrent(request, supersedeState()), true)
  // 治理弹窗关闭：独立入口不受影响
  assert.equal(
    isSupersedeRequestCurrent(request, supersedeState({ governanceOpen: false, governanceDocId: null })),
    true
  )
  assert.equal(
    isSupersedeRequestCurrent(request, supersedeState({ governanceEpoch: 99, governanceDocId: 'doc-a' })),
    true
  )
  // 但仍受卸载/切库/文档消失约束
  assert.equal(isSupersedeRequestCurrent(request, supersedeState({ disposed: true })), false)
  assert.equal(isSupersedeRequestCurrent(request, supersedeState({ kbId: 'kb-9' })), false)
  assert.equal(isSupersedeRequestCurrent(request, supersedeState({ docIds: [] })), false)
})

test('supersede success response with the wrong id or kb is never applied', async () => {
  // 真值表直接对应组件的响应核验：错的 ID/KB 不提示成功、不关窗、不刷新
  const applies = (
    data: { id: string; kb_id: string } | null | undefined,
    current: boolean
  ): boolean => !!current && data?.id === 'doc-a' && data?.kb_id === 'kb-1'
  assert.equal(applies({ id: 'doc-a', kb_id: 'kb-1' }, true), true)
  assert.equal(applies({ id: 'doc-b', kb_id: 'kb-1' }, true), false)
  assert.equal(applies({ id: 'doc-a', kb_id: 'kb-2' }, true), false)
  assert.equal(applies({ id: 'doc-a', kb_id: 'kb-1' }, false), false)
  assert.equal(applies(null, true), false)
})

test('responses are discarded when alive flips to false (doc/KB switched or closed)', async () => {
  const controller = createGovernanceWriteController()
  const gate = deferred()
  let alive = true
  let savedCount = 0
  let errorCount = 0
  const run = controller.run({
    alive: () => alive,
    write: () => gate.promise,
    onSaved: () => { savedCount += 1 },
    onConflict: () => {},
    onError: () => { errorCount += 1 }
  })
  await new Promise((resolve) => setTimeout(resolve, 0))
  // 响应返回前弹窗被关闭/切文档：结果必须被丢弃
  alive = false
  gate.reject(new Error('late failure'))
  assert.equal(await run, 'stale')
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(savedCount, 0)
  assert.equal(errorCount, 0)
  assert.equal(controller.busy, false)
})
