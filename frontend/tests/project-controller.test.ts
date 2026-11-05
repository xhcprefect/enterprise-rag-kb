/** projectController 的真实异步路径测试：deferred + 假 schedule/时钟驱动。
 *
 * 覆盖主审点：
 * - 正常 ready 首次读：selectSource → listSnapshots 返回 ready → loadFiles 被调用、onView 不再是 no-ready。
 * - pending→ready：先 pending（no-ready + 轮询），下一次轮询返回 ready → 触发 loadView、视图转 ready。
 * - 刷新发现新 ready：no-ready 时 refresh() 重读快照发现 ready。
 * - 保持显式旧 snapshot：用户显式选旧快照后，轮询出现更新 ready 也不覆盖。
 * - query/翻页（viewEpoch 变化）不停死合法同步轮询。
 * - 有界轮询：60s / 24 次预算到顶发 onPollExpired 且不再排。
 * - 轮询请求异常/迟到不伪空成功。
 * - A→B 确认不操作 B；卸载无迟到结果、不复活轮询。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  ProjectController,
  PROJECT_POLL_MAX_ATTEMPTS,
  type ProjectViewState
} from '../src/utils/projectController.ts'
import type { PageResult } from '../src/api/index.ts'
import type { ProjectFile, ProjectSnapshot } from '../src/api/projects.ts'

function flush(): Promise<void> {
  return new Promise((resolve) => { setTimeout(resolve, 0) })
}

interface Deferred<T> {
  resolve: (v: T) => void
  reject: (e: unknown) => void
  promise: Promise<T>
}
function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { resolve, reject, promise }
}

function snap(id: string, status: ProjectSnapshot['status']): ProjectSnapshot {
  return {
    id, source_id: 'ignored', status, base_snapshot_id: null, progress: 0,
    file_count: 0, total_bytes: 0, manifest_fingerprint: null, config_fingerprint: null,
    error_code: null, error_message: null, cancel_requested: false,
    started_at: null, finished_at: null, created_at: ''
  }
}

function file(path: string, fvid: string): ProjectFile {
  return {
    file_version_id: fvid, source_id: 'A', snapshot_id: 'S', relative_path: path,
    content_sha256: 'x'.repeat(64), size: 10, language: 'ts', parse_status: 'ready',
    chunk_count: 1, char_count: 5, symbol: null, symbol_kind: null, created_at: null
  }
}

interface Harness {
  controller: ProjectController
  advance: (ms: number) => void
  availablePolls: () => number
  tickPoll: () => boolean
  resolveSnapshots: (idx: number, snaps: ProjectSnapshot[]) => void
  rejectSnapshots: (idx: number, err: unknown) => void
  resolveFiles: (idx: number, total: number, items: ProjectFile[]) => void
  resolveSearch: (idx: number, hits: unknown[], total: number) => void
  resolvePreview: (idx: number, preview: unknown) => void
  snapCount: () => number
  fileCount: () => number
  filesArgs: Array<{ sourceId: string; snapshotId: string | null; page: number; pageSize: number }>
  lastView: () => ProjectViewState
  snapCalls: Array<{ sourceId: string; snaps: ProjectSnapshot[] }>
  previewCalls: unknown[]
  syncCalls: string[]
  cancelCalls: string[]
  revokeCalls: string[]
  expired: () => number
  syncReject: (e: unknown) => void
}

function makeHarness(): Harness {
  let t = 0
  const pending: Array<{ fn: () => void; ms: number; cleared: boolean }> = []
  const schedule = (fn: () => void, ms: number) => {
    const entry = { fn, ms, cleared: false }
    pending.push(entry)
    return () => { entry.cleared = true }
  }

  const snapshotsQ: Deferred<ProjectSnapshot[]>[] = []
  const filesQ: Deferred<PageResult<ProjectFile>>[] = []
  const searchQ: Deferred<unknown>[] = []
  const previewQ: Deferred<unknown>[] = []
  const syncQ: Deferred<unknown>[] = []
  const viewCalls: ProjectViewState[] = []
  const snapCalls: Array<{ sourceId: string; snaps: ProjectSnapshot[] }> = []
  const previewCalls: unknown[] = []
  const filesArgs: Array<{ sourceId: string; snapshotId: string | null; page: number; pageSize: number }> = []
  const syncCalls: string[] = []
  const cancelCalls: string[] = []
  const revokeCalls: string[] = []
  const counters = { expired: 0 }

  const controller = new ProjectController({
    listSnapshots: (sourceId) => {
      const d = deferred<ProjectSnapshot[]>()
      snapshotsQ.push(d)
      return d.promise
    },
    loadFiles: (sourceId, opts) => {
      filesArgs.push({ sourceId, snapshotId: opts.snapshotId, page: opts.page, pageSize: opts.pageSize })
      const d = deferred<PageResult<ProjectFile>>()
      filesQ.push(d)
      return d.promise
    },
    searchContent: () => {
      const d = deferred<unknown>()
      searchQ.push(d)
      return d.promise as never
    },
    loadPreview: () => {
      const d = deferred<unknown>()
      previewQ.push(d)
      return d.promise as never
    },
    syncSource: (sourceId) => {
      syncCalls.push(sourceId)
      const d = deferred<unknown>()
      syncQ.push(d)
      return d.promise as never
    },
    cancelSyncSource: (sourceId) => { cancelCalls.push(sourceId); return Promise.resolve(snap('c', 'cancelled')) },
    revokeSource: (sourceId) => { revokeCalls.push(sourceId); return Promise.resolve(null) },
    onSnapshots: (sourceId, snaps) => { snapCalls.push({ sourceId, snaps }) },
    onView: (view) => { viewCalls.push(view) },
    onPreview: (p) => { previewCalls.push(p) },
    onPollExpired: () => { counters.expired += 1 },
    now: () => t,
    schedule
  })

  return {
    controller,
    advance: (ms) => { t += ms },
    availablePolls: () => pending.filter((e) => !e.cleared).length,
    tickPoll: () => {
      const entry = pending.find((e) => !e.cleared)
      if (!entry) return false
      entry.cleared = true
      entry.fn()
      return true
    },
    resolveSnapshots: (idx, snaps) => { snapshotsQ[idx]?.resolve(snaps) },
    rejectSnapshots: (idx, err) => { snapshotsQ[idx]?.reject(err) },
    resolveFiles: (idx, total, items) => { filesQ[idx]?.resolve({ total, items }) },
    resolveSearch: (idx, hits, total) => { searchQ[idx]?.resolve({ results: hits, total, snapshot_id: 's' }) },
    resolvePreview: (idx, preview) => { previewQ[idx]?.resolve(preview) },
    snapCount: () => snapshotsQ.length,
    fileCount: () => filesQ.length,
    filesArgs,
    lastView: () => viewCalls[viewCalls.length - 1],
    snapCalls,
    previewCalls,
    syncCalls,
    cancelCalls,
    revokeCalls,
    expired: () => counters.expired,
    syncReject: (e) => { syncQ[syncQ.length - 1]?.reject(e) }
  }
}

// ---------------- 正常 ready 首次读 ----------------
test('selectSource with an initially-ready snapshot loads files and view becomes ready', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  assert.equal(h.snapCount(), 1)
  h.resolveSnapshots(0, [snap('S1', 'ready')])
  await flush()
  assert.equal(h.fileCount(), 1, 'ready 首次读应触发 loadFiles')
  assert.deepEqual(h.filesArgs[0], { sourceId: 'A', snapshotId: 'S1', page: 1, pageSize: 20 })
  assert.equal(h.availablePolls(), 0, '初始即 ready 不应轮询')

  h.resolveFiles(0, 2, [file('a.ts', 'F1')])
  await flush()
  assert.equal(h.lastView().status, 'ok')
  assert.equal(h.lastView().ready, true)
  assert.equal(h.lastView().snapshotId, 'S1')
  assert.equal(h.lastView().items.length, 1)
})

// ---------------- pending → ready ----------------
test('pending keeps view no-ready and polls; a later ready transition triggers the file load', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('SP', 'processing')])
  await flush()
  assert.deepEqual(h.snapCalls.at(-1), { sourceId: 'A', snaps: [snap('SP', 'processing')] }, '页面必须收到活动同步状态')
  assert.equal(h.lastView().status, 'no-ready', 'processing 不视为 ready')
  assert.equal(h.fileCount(), 0, '未就绪绝不能读文件')
  assert.equal(h.availablePolls(), 1)

  h.advance(2500)
  assert.ok(h.tickPoll())
  await flush()
  h.resolveSnapshots(1, [snap('S1', 'ready')])
  await flush()
  assert.deepEqual(h.snapCalls.at(-1), { sourceId: 'A', snaps: [snap('S1', 'ready')] }, '页面快照历史及同步状态必须随轮询更新')
  assert.equal(h.fileCount(), 1, '转 ready 后应补一次文件加载')
  assert.deepEqual(h.filesArgs[0], { sourceId: 'A', snapshotId: 'S1', page: 1, pageSize: 20 })

  h.resolveFiles(0, 1, [file('b.ts', 'F2')])
  await flush()
  assert.equal(h.lastView().status, 'ok')
  assert.equal(h.lastView().ready, true)
  assert.equal(h.availablePolls(), 0, '就绪且无活动同步后不再轮询')
})

// ---------------- 刷新发现新 ready ----------------
test('refresh re-reads snapshots; a no-ready view becomes ready and then loads files', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('SP', 'pending')])
  await flush()
  assert.equal(h.lastView().status, 'no-ready')
  assert.equal(h.fileCount(), 0)

  h.controller.refresh()
  await flush()
  assert.equal(h.snapCount(), 2, 'refresh 应重读快照')
  h.resolveSnapshots(1, [snap('S1', 'ready')])
  await flush()
  assert.equal(h.fileCount(), 1, '刷新发现 ready 后应加载文件')
  h.resolveFiles(0, 1, [file('c.ts', 'F3')])
  await flush()
  assert.equal(h.lastView().status, 'ok')
  assert.equal(h.lastView().ready, true)
})

// ---------------- 保持显式旧 snapshot ----------------
test('an explicitly selected snapshot is preserved when a newer ready appears via poll', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('S1', 'ready'), snap('SP', 'processing')])
  await flush()
  assert.equal(h.lastView().snapshotId, 'S1', '初始自动落到最新 ready')
  assert.equal(h.availablePolls(), 1)

  // 用户显式切到 S2（仍在 ready 集合内）：触发一次 S2 的文件加载。
  h.controller.selectSnapshot('S2', true)
  await flush()
  const svc = h.lastView()
  assert.equal(svc.snapshotId, 'S2')

  // 轮询返回更新的 S3 ready，但 S2 仍 ready —— 不得覆盖用户显式选择。
  h.advance(2500)
  assert.ok(h.tickPoll())
  await flush()
  h.resolveSnapshots(1, [snap('S1', 'ready'), snap('S2', 'ready'), snap('S3', 'ready'), snap('SP', 'processing')])
  await flush()
  assert.equal(h.lastView().ready, true)
  assert.equal(h.lastView().snapshotId, 'S2', '轮询发现更新就绪快照不得覆盖显式选择')
})

// ---------------- query/翻页不停死合法同步轮询 ----------------
test('query and page changes (viewEpoch) do not kill the legitimate sync poll', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('SP', 'processing')])
  await flush()
  assert.equal(h.availablePolls(), 1)

  h.controller.setPage(2)
  h.controller.setQuery('hello')
  assert.equal(h.availablePolls(), 1, '换 query/翻页后同步轮询仍存活')

  h.advance(2500)
  assert.ok(h.tickPoll())
  await flush()
  h.resolveSnapshots(1, [snap('SP', 'processing')])
  await flush()
  assert.equal(h.availablePolls(), 1, '仍在同步应继续轮询')
})

// ---------------- 有界轮询（24 次） ----------------
test('poll is bounded by the attempt budget and fires onPollExpired once', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('SP', 'processing')])
  await flush()
  assert.equal(h.availablePolls(), 1)

  let ticks = 0
  let idx = 1
  while (h.availablePolls() > 0 && ticks < PROJECT_POLL_MAX_ATTEMPTS + 2) {
    h.advance(2500)
    if (!h.tickPoll()) break
    ticks += 1
    await flush()
    h.resolveSnapshots(idx++, [snap('SP', 'processing')])
    await flush()
  }
  assert.ok(h.expired() > 0, '预算应触发 onPollExpired')
  assert.equal(h.availablePolls(), 0, '预算到顶后不得再排轮询')
})

// ---------------- 60s 墙钟 deadline ----------------
test('poll stops at the 60s wall-clock deadline even if attempts remain', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('SP', 'processing')])
  await flush()
  let idx = 1
  for (let i = 0; i < 6 && h.availablePolls() > 0; i++) {
    h.advance(12000)
    h.tickPoll()
    await flush()
    h.resolveSnapshots(idx++, [snap('SP', 'processing')])
    await flush()
  }
  assert.ok(h.expired() >= 1, '60s 墙钟应触发过期')
  assert.equal(h.availablePolls(), 0)
})

// ---------------- 轮询异常不伪空成功 ----------------
test('a poll request error keeps the previous state and never fakes an empty success', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('S1', 'ready'), snap('SP', 'processing')])
  await flush()
  // 完成初始 S1 文件加载，使视图进入稳定 ok。
  h.resolveFiles(0, 1, [file('a.ts', 'F1')])
  await flush()
  assert.equal(h.lastView().status, 'ok')
  assert.equal(h.lastView().ready, true)
  assert.equal(h.availablePolls(), 1)

  // 轮询 fetch 异常：不得清空已有 ready 状态、不得伪装成空完成/回到 no-ready。
  h.advance(2500)
  assert.ok(h.tickPoll())
  await flush()
  h.rejectSnapshots(1, new Error('network down'))
  await flush()
  assert.equal(h.lastView().ready, true, '异常不得清空已就绪状态')
  assert.equal(h.lastView().status, 'ok', '异常不得伪装成空完成/no-ready')
})

// ---------------- A→B 确认不操作 B ----------------
test('a revoke confirmed after switching to source B never operates on B', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('S1', 'ready')])
  await flush()
  const frozen = h.controller.freezeConfirm('revoke')!
  assert.equal(frozen.sourceId, 'A')
  assert.equal(h.revokeCalls.length, 0)

  h.controller.selectSource('B')
  const outcome = await frozen.commit()
  assert.equal(outcome, 'stale')
  assert.deepEqual(h.revokeCalls, [], '切换源后提交必须中止，绝不撤销 B（或任何新源）')
})

// ---------------- 卸载无迟到 ----------------
test('dispose drops late file results and stops the poll', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('S1', 'ready'), snap('SP', 'processing')])
  await flush()
  const fileIdx = h.filesArgs.length // 0：此时文件加载仍 in-flight

  h.controller.dispose() // 卸载发生在文件返回前
  h.resolveFiles(fileIdx, 5, [file('x.ts', 'F9')])
  await flush()
  assert.equal(h.lastView().status, 'idle', '卸载后不得把迟到文件投给已销毁视图')
  assert.equal(h.availablePolls(), 0, '卸载后轮询必须停止')
})

// ---------------- 卸载后迟到轮询不复活 ----------------
test('a late poll result after unmount neither applies nor reschedules', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('SP', 'processing')])
  await flush()
  assert.equal(h.availablePolls(), 1)

  h.controller.dispose()
  h.advance(2500)
  h.tickPoll()
  await flush()
  h.resolveSnapshots(1, [snap('S1', 'ready')])
  await flush()
  assert.equal(h.availablePolls(), 0, '卸载后迟到轮询不得重启轮询')
  assert.equal(h.lastView().status, 'idle', '卸载后迟到快照不得恢复 ready 视图')
})

// ---------------- requestSync 失败不吞 / 返回 null ----------------
test('requestSync returns null on failure instead of pretending success', async () => {
  const h = makeHarness()
  h.controller.selectSource('A')
  await flush()
  h.resolveSnapshots(0, [snap('SP', 'processing')])
  await flush()
  const before = h.snapCount()

  const p = h.controller.requestSync()
  await flush()
  h.syncReject(new Error('sync failed'))
  const result = await p
  assert.equal(result, null, '失败应返回 null 而非伪成功')
  assert.equal(h.snapCount(), before, '失败后不重读快照')
  assert.deepEqual(h.syncCalls, ['A'])
})
