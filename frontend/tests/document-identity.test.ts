import assert from 'node:assert/strict'
import test from 'node:test'
import {
  matchesResume,
  needsExplicitReindex,
  reuseMessageKey,
  reuseOutcome,
  selectPickedFiles,
  shouldRestrictToResume,
  splitResumeMatches
} from '../src/utils/documentIdentity.ts'

test('fresh upload is reported as created', () => {
  assert.equal(reuseOutcome(false, 'ready'), 'created')
  assert.equal(reuseOutcome(false, 'failed'), 'created')
  assert.equal(needsExplicitReindex('created'), false)
  assert.equal(reuseMessageKey('created'), null)
})

test('reused ready document is reported as reused', () => {
  assert.equal(reuseOutcome(true, 'ready'), 'reused')
  assert.equal(needsExplicitReindex('reused'), false)
  assert.equal(reuseMessageKey('reused'), 'documents.toast.reused')
})

test('reused failed document keeps status and asks for explicit reindex', () => {
  assert.equal(reuseOutcome(true, 'failed'), 'reused-failed')
  assert.equal(needsExplicitReindex('reused-failed'), true)
  assert.equal(reuseMessageKey('reused-failed'), 'documents.toast.reusedFailed')
})

test('reused cancelled document keeps status and asks for explicit reindex', () => {
  assert.equal(reuseOutcome(true, 'cancelled'), 'reused-cancelled')
  assert.equal(needsExplicitReindex('reused-cancelled'), true)
  assert.equal(reuseMessageKey('reused-cancelled'), 'documents.toast.reusedCancelled')
})

test('reused pending/processing document still counts as ordinary reuse', () => {
  // 复用排队中/处理中的文档不改变状态，交给轮询跟进；不提示重建索引。
  assert.equal(reuseOutcome(true, 'pending'), 'reused')
  assert.equal(reuseOutcome(true, 'processing'), 'reused')
  assert.equal(needsExplicitReindex(reuseOutcome(true, 'processing')), false)
})

test('resume identity requires same name and size', () => {
  const resume = { filename: 'a.pdf', fileSize: 10 }
  assert.equal(matchesResume({ name: 'a.pdf', size: 10 }, resume), true)
  assert.equal(matchesResume({ name: 'a.pdf', size: 11 }, resume), false)
  assert.equal(matchesResume({ name: 'b.pdf', size: 10 }, resume), false)
  assert.equal(matchesResume({ name: 'a.pdf', size: 10 }, null), false)
})

test('splitResumeMatches keeps only the resumable file and reports the rest', () => {
  const resume = { filename: 'a.pdf', fileSize: 10 }
  const files = [
    { name: 'b.pdf', size: 10 },
    { name: 'a.pdf', size: 10 },
    { name: 'a.pdf', size: 12 }
  ]
  const { matched, mismatched } = splitResumeMatches(files, resume)
  assert.deepEqual(matched, [{ name: 'a.pdf', size: 10 }])
  assert.deepEqual(mismatched, [
    { name: 'b.pdf', size: 10 },
    { name: 'a.pdf', size: 12 }
  ])
})

test('without a pending resume every file is accepted', () => {
  const files = [{ name: 'a.pdf', size: 1 }, { name: 'b.txt', size: 2 }]
  assert.deepEqual(splitResumeMatches(files, null), { matched: files, mismatched: [] })
})

test('resume restriction applies only when restoring with no current item', () => {
  const resume = { filename: 'a.pdf', fileSize: 10 }
  // 刷新后恢复、页面空闲：必须选同一文件继续
  assert.equal(shouldRestrictToResume(resume, false), true)
  // 正在上传（resumeInfo 指向当前会话）：不能限制，否则无法追加后续文件
  assert.equal(shouldRestrictToResume(resume, true), false)
  // 没有未完成会话
  assert.equal(shouldRestrictToResume(null, false), false)
  assert.equal(shouldRestrictToResume(null, true), false)
})

test('files can be appended to the queue while an upload is active', () => {
  const resume = { filename: 'a.pdf', fileSize: 10 }
  const picked = [{ name: 'b.pdf', size: 20 }, { name: 'a.pdf', size: 10 }]
  // 上传中追加不同文件：全部接受
  const active = selectPickedFiles(picked, resume, true)
  assert.deepEqual(active.accepted, picked)
  assert.deepEqual(active.mismatched, [])
  // 空闲时的续传选择：只留同名同大小
  const restoring = selectPickedFiles(picked, resume, false)
  assert.deepEqual(restoring.accepted, [{ name: 'a.pdf', size: 10 }])
  assert.deepEqual(restoring.mismatched, [{ name: 'b.pdf', size: 20 }])
  // 没有 resume 时不再筛选
  assert.deepEqual(selectPickedFiles(picked, null, false).accepted, picked)
})
