/**
 * 续传记录跨卸载/重进的 Node 测试。
 *
 * 记录按知识库存在 localStorage，同一 KB 的前后两个页面实例共用一条。
 * 旧实例卸载后迟到的 createSession / complete 回执只能处置自己那个
 * upload_id，绝不能删除或覆盖用户重新进入后新上传写入的记录。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  canApplyRestoredResume,
  clearResumeRecord,
  isResumeGoneStatus,
  memoryResumeStorage,
  readResume,
  resumeKey,
  shouldClearOnResumeFailure,
  writeResume
} from '../src/utils/documentResume.ts'
import type { ResumeRecord } from '../src/utils/documentResume.ts'

const KB = 'kb-1'

function record(uploadId: string, overrides: Partial<ResumeRecord> = {}): ResumeRecord {
  return { uploadId, filename: 'a.pdf', fileSize: 10, percent: 40, ...overrides }
}

test('a record round-trips and survives unrelated storage keys', () => {
  const storage = memoryResumeStorage()
  assert.equal(readResume(storage, KB), null)
  assert.equal(writeResume(storage, KB, record('old'), { disposed: false }), true)
  assert.deepEqual(readResume(storage, KB), record('old'))
  assert.ok(storage.getItem(resumeKey(KB)))
  // 其它知识库互不影响
  assert.equal(readResume(storage, 'kb-2'), null)
})

test('corrupted or non-object json reads as no record', () => {
  const storage = memoryResumeStorage({ [resumeKey(KB)]: '{not json' })
  assert.equal(readResume(storage, KB), null)
  const noId = memoryResumeStorage({ [resumeKey(KB)]: JSON.stringify({ filename: 'a' }) })
  assert.equal(readResume(noId, KB), null)
})

test('a late receipt from an unmounted instance never writes storage', () => {
  const storage = memoryResumeStorage()
  // 旧实例已卸载，迟到的 createSession/complete 想写自己的会话
  const written = writeResume(storage, KB, record('old'), { disposed: true, ownerUploadId: 'old' })
  assert.equal(written, false)
  assert.equal(storage.getItem(resumeKey(KB)), null)
})

test('a receipt may only write the session it belongs to', () => {
  const storage = memoryResumeStorage()
  writeResume(storage, KB, record('new', { percent: 5 }), { disposed: false, ownerUploadId: 'new' })
  // 回执携带的会话与要写的记录不一致：拒绝（属于陈旧条目）
  const written = writeResume(storage, KB, record('old'), { disposed: false, ownerUploadId: 'new' })
  assert.equal(written, false)
  assert.equal(readResume(storage, KB)?.uploadId, 'new')
  // 已卸载实例的迟到回执：即使 upload_id 一致也拒绝
  assert.equal(
    writeResume(storage, KB, record('new', { percent: 90 }), { disposed: true, ownerUploadId: 'new' }),
    false
  )
  assert.equal(readResume(storage, KB)?.percent, 5)
})

test('a live instance switching sessions updates its own record', () => {
  // 刷新后重新上传同一文件会拿到新会话，此时必须能更新记录，
  // 否则续传入口会停留在已被放弃的旧会话。
  const storage = memoryResumeStorage()
  writeResume(storage, KB, record('old'), { disposed: false, ownerUploadId: 'old' })
  writeResume(storage, KB, record('fresh', { percent: 1 }), {
    disposed: false,
    ownerUploadId: 'fresh'
  })
  assert.equal(readResume(storage, KB)?.uploadId, 'fresh')
})

test('write with matching owner overwrites its own older record', () => {
  const storage = memoryResumeStorage()
  writeResume(storage, KB, record('session-1', { percent: 10 }), {
    disposed: false,
    ownerUploadId: 'session-1'
  })
  writeResume(storage, KB, record('session-1', { percent: 70 }), {
    disposed: false,
    ownerUploadId: 'session-1'
  })
  assert.equal(readResume(storage, KB)?.percent, 70)
})

test('a late complete only clears its own record, never the new one', () => {
  const storage = memoryResumeStorage()
  writeResume(storage, KB, record('new'), { disposed: false, ownerUploadId: 'new' })
  // 旧实例的 complete 迟到（组件已卸载）
  assert.equal(clearResumeRecord(storage, KB, 'old', { disposed: true }), false)
  assert.equal(readResume(storage, KB)?.uploadId, 'new')
  // 即使组件还在，也只能清自己那条；别人的记录原样保留
  assert.equal(clearResumeRecord(storage, KB, 'old', { disposed: false }), false)
  assert.equal(readResume(storage, KB)?.uploadId, 'new')
  // 清自己那条才会移除
  assert.equal(clearResumeRecord(storage, KB, 'new', { disposed: false }), true)
  assert.equal(readResume(storage, KB), null)
  // 记录不存在时也安全（uploadId 必传，没有无条件删除路径）
  assert.equal(clearResumeRecord(memoryResumeStorage(), KB, 'new', { disposed: false }), false)
})

test('restore failure clears only on confirmed terminal status and matching identity', () => {
  const saved = record('live')
  const stored = record('live')
  // 断网/超时/5xx：记录必须保留，用户还能继续续传
  for (const status of [undefined, null, 0, 500, 502, 503, 429]) {
    assert.equal(shouldClearOnResumeFailure(saved, stored, false, false, status), false, String(status))
  }
  // 确证会话不存在：可以清
  for (const status of [404, 410]) {
    assert.equal(shouldClearOnResumeFailure(saved, stored, false, false, status), true, String(status))
    assert.equal(isResumeGoneStatus(status), true)
  }
  // 但页面已卸载 / 用户已开始新任务 / 记录已换会话时，即使 404 也不清
  assert.equal(shouldClearOnResumeFailure(saved, stored, false, true, 404), false)
  assert.equal(shouldClearOnResumeFailure(saved, stored, true, false, 404), false)
  assert.equal(shouldClearOnResumeFailure(saved, record('newer'), false, false, 404), false)
  assert.equal(shouldClearOnResumeFailure(saved, null, false, false, 404), false)
})

test('a late network failure never deletes an actively resumed record', () => {
  // 时序：页面恢复提示在途 → 用户选择同一文件开始续传（写入同 upload_id 记录）
  // → 旧的 getSession 网络失败迟到。此时记录仍必须保留。
  const storage = memoryResumeStorage()
  const saved = record('session-1')
  writeResume(storage, KB, saved, { disposed: false, ownerUploadId: 'session-1' })
  // 用户已开始续传：current 存在（同一 upload_id）
  const shouldClear = shouldClearOnResumeFailure(
    saved,
    readResume(storage, KB),
    true,
    false,
    undefined
  )
  assert.equal(shouldClear, false)
  assert.equal(readResume(storage, KB)?.uploadId, 'session-1')

  // 即使之后确证 404，只要用户已开始新任务就不清
  assert.equal(
    shouldClearOnResumeFailure(saved, readResume(storage, KB), true, false, 404),
    false
  )
  assert.equal(readResume(storage, KB)?.uploadId, 'session-1')
})

test('a late restore response cannot overwrite an already started upload', () => {
  const saved = record('restored')
  const stored = record('restored')
  // 空闲、仍在运行、记录未变：可以恢复提示
  assert.equal(canApplyRestoredResume(saved, stored, false, false), true)
  // 用户已开始新上传
  assert.equal(canApplyRestoredResume(saved, stored, true, false), false)
  // 页面已卸载
  assert.equal(canApplyRestoredResume(saved, stored, false, true), false)
  // 持久记录已被新会话取代
  assert.equal(canApplyRestoredResume(saved, record('newer'), false, false), false)
  // 记录已被清掉
  assert.equal(canApplyRestoredResume(saved, null, false, false), false)
})

test('delayed complete after unmount and re-entry keeps the new session record', () => {
  // 完整时序：旧实例上传中 → 卸载 → 用户重新进入并开始新上传（写入新会话）
  // → 旧实例的 complete 迟到返回。
  const storage = memoryResumeStorage()
  const oldSession = 'session-old'
  const newSession = 'session-new'

  // 1) 旧实例在上传过程中写过续传记录
  writeResume(storage, KB, record(oldSession, { percent: 60 }), {
    disposed: false,
    ownerUploadId: oldSession
  })

  // 2) 组件卸载（disposed = true），旧回执此后不得再写
  assert.equal(
    writeResume(storage, KB, record(oldSession, { percent: 61 }), {
      disposed: true,
      ownerUploadId: oldSession
    }),
    false
  )

  // 3) 新实例进入并开始新上传，覆盖为它自己的会话
  writeResume(storage, KB, record(newSession, { percent: 4 }), {
    disposed: false,
    ownerUploadId: newSession
  })

  // 4) 旧实例的 complete 迟到：先判 disposed，再按 upload_id 清理
  const oldDisposed = true
  assert.equal(clearResumeRecord(storage, KB, oldSession, { disposed: oldDisposed }), false)
  // 即便 disposed 标志因实现顺序丢失，按 upload_id 也不会命中新记录
  assert.equal(clearResumeRecord(storage, KB, oldSession, { disposed: false }), false)

  assert.equal(readResume(storage, KB)?.uploadId, newSession)
  assert.equal(readResume(storage, KB)?.percent, 4)
})

test('delayed createSession after unmount never overwrites the new component record', () => {
  // 旧实例的 createSession 在卸载后才返回；若继续走 pause 分支会 saveResume，
  // 从而用旧会话覆盖新组件刚写入的续传记录。
  const storage = memoryResumeStorage()
  const oldSession = 'session-old'
  const newSession = 'session-new'

  writeResume(storage, KB, record(newSession, { percent: 2 }), {
    disposed: false,
    ownerUploadId: newSession
  })

  // 迟到的旧回执：disposed 守卫拒绝（视图 saveResume 的 disposed 早退等价于此）
  const didWrite = writeResume(storage, KB, record(oldSession, { percent: 0 }), {
    disposed: true,
    ownerUploadId: oldSession
  })
  assert.equal(didWrite, false)
  assert.equal(readResume(storage, KB)?.uploadId, newSession)

  // 另外：restore 的迟到响应也不能把界面拉回旧会话
  assert.equal(
    canApplyRestoredResume(record(oldSession), readResume(storage, KB), false, false),
    false
  )
})

test('restore does not resurrect after the record was replaced mid-flight', () => {
  const storage = memoryResumeStorage()
  const saved = record('old')
  writeResume(storage, KB, saved, { disposed: false, ownerUploadId: 'old' })
  // 请求在途期间，同一 KB 的新实例开始上传并写入新记录
  writeResume(storage, KB, record('new'), { disposed: false, ownerUploadId: 'new' })
  // 旧 restore 的响应迟到
  assert.equal(canApplyRestoredResume(saved, readResume(storage, KB), false, false), false)
  assert.equal(readResume(storage, KB)?.uploadId, 'new')
})
