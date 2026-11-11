import assert from 'node:assert/strict'
import test from 'node:test'
import {
  canSubmitCapturedRemoval,
  classifyMemberError,
  isKbMemberRole,
  isMemberRequestCurrent,
  memberErrorKey,
  memberTargetState,
  normalizeMemberPage,
  normalizeMemberPageSize,
  normalizeMemberRevision,
  pageAfterRemoval,
  shouldRefreshCandidatesAfterMemberError,
  shouldInvalidateMemberState,
  shouldReloadAfterMemberError,
  shouldStopMemberManagement
} from '../src/utils/kbMembers.ts'

test('member roles are an allow-list, not arbitrary server strings', () => {
  assert.equal(isKbMemberRole('reader'), true)
  assert.equal(isKbMemberRole('maintainer'), true)
  assert.equal(isKbMemberRole('admin'), false)
  assert.equal(isKbMemberRole(''), false)
  assert.equal(isKbMemberRole(undefined), false)
})

test('pagination is bounded to positive pages and page_size <= 100', () => {
  assert.equal(normalizeMemberPage(0), 1)
  assert.equal(normalizeMemberPage(-5), 1)
  assert.equal(normalizeMemberPage(Number.NaN), 1)
  assert.equal(normalizeMemberPage('3'), 3)
  assert.equal(normalizeMemberPage(3.9), 3)

  assert.equal(normalizeMemberPageSize(0), 20)
  assert.equal(normalizeMemberPageSize(-1), 20)
  assert.equal(normalizeMemberPageSize(Number.NaN), 20)
  assert.equal(normalizeMemberPageSize(101), 100)
  assert.equal(normalizeMemberPageSize(50.9), 50)
})

test('revision accepts only non-negative integers', () => {
  assert.equal(normalizeMemberRevision(0), 0)
  assert.equal(normalizeMemberRevision(7), 7)
  assert.equal(normalizeMemberRevision(-1), null)
  assert.equal(normalizeMemberRevision(1.5), null)
  assert.equal(normalizeMemberRevision('3'), null)
  assert.equal(normalizeMemberRevision(undefined), null)
})

test('removal page converges when the last page becomes empty', () => {
  assert.equal(pageAfterRemoval(3, 40, 20), 2)
  assert.equal(pageAfterRemoval(2, 20, 20), 1)
  assert.equal(pageAfterRemoval(5, 0, 20), 1)
  assert.equal(pageAfterRemoval(2, 21, 20), 2)
  assert.equal(pageAfterRemoval(1, 1, 100), 1)
})

test('error categories map to stable UI messages and refresh policy', () => {
  assert.equal(classifyMemberError({ response: { status: 409 } }), 'conflict')
  assert.equal(classifyMemberError({ response: { status: 400 } }), 'invalid')
  assert.equal(classifyMemberError({ response: { status: 404 } }), 'not_found')
  assert.equal(classifyMemberError({ response: { status: 401 } }), 'unauthenticated')
  assert.equal(classifyMemberError({ response: { status: 403 } }), 'forbidden')
  assert.equal(classifyMemberError({ response: { status: 500 } }), 'other')
  assert.equal(classifyMemberError({ code: 'ERR_CANCELED' }), 'cancelled')

  assert.equal(memberErrorKey('conflict'), 'knowledge.members.errors.conflict')
  assert.equal(memberErrorKey('invalid'), 'knowledge.members.errors.invalid')
  assert.equal(memberErrorKey('not_found'), 'knowledge.members.errors.notFound')
  assert.equal(memberErrorKey('forbidden'), 'knowledge.members.errors.forbidden')
  assert.equal(memberErrorKey('unauthenticated'), 'knowledge.members.errors.forbidden')
  assert.equal(memberErrorKey('cancelled'), 'knowledge.members.errors.cancelled')
  assert.equal(memberErrorKey('other'), 'knowledge.members.errors.generic')

  // 409 后刷新列表但绝不自动重放；权限错误必须停止成员管理。
  assert.equal(shouldReloadAfterMemberError('conflict'), true)
  assert.equal(shouldReloadAfterMemberError('other'), false)
  assert.equal(shouldRefreshCandidatesAfterMemberError('invalid'), true)
  assert.equal(shouldRefreshCandidatesAfterMemberError('not_found'), true)
  assert.equal(shouldRefreshCandidatesAfterMemberError('conflict'), false)
  assert.equal(shouldStopMemberManagement('forbidden'), true)
  assert.equal(shouldStopMemberManagement('unauthenticated'), true)
  assert.equal(shouldStopMemberManagement('conflict'), false)

  // 未知失败不得保留旧成员/revision；冲突与参数错误不需要整体失效。
  assert.equal(shouldInvalidateMemberState('other'), true)
  assert.equal(shouldInvalidateMemberState('conflict'), false)
  assert.equal(shouldInvalidateMemberState('invalid'), false)
  assert.equal(shouldInvalidateMemberState('cancelled'), false)
})

test('inactive users cannot be new grants, existing members are not re-added', () => {
  const existing = new Set(['member-1'])
  assert.equal(memberTargetState({ id: 'member-1', is_active: true }, existing), 'existing')
  assert.equal(memberTargetState({ id: 'disabled', is_active: false }, existing), 'inactive')
  assert.equal(memberTargetState({ id: 'new', is_active: true }, existing), 'ready')
})

test('late member responses cannot update a closed dialog, another KB, or a newer request', () => {
  const identity = { session: 4, kbId: 'kb-1', requestId: 9 }
  const open = { session: 4, kbId: 'kb-1', open: true, disposed: false }
  assert.equal(isMemberRequestCurrent(identity, open, 9), true)

  // 关闭后迟到
  assert.equal(isMemberRequestCurrent(identity, { ...open, open: false }, 9), false)
  // 切库后迟到
  assert.equal(isMemberRequestCurrent(identity, { ...open, kbId: 'kb-2' }, 9), false)
  // 已卸载
  assert.equal(isMemberRequestCurrent(identity, { ...open, disposed: true }, 9), false)
  // 同一 KB 但已有更新的请求（慢搜索/慢列表）
  assert.equal(isMemberRequestCurrent(identity, open, 10), false)
  // 重新打开后 session 已递增
  assert.equal(isMemberRequestCurrent(identity, { ...open, session: 5 }, 9), false)
})

test('a removal captured before confirmation is void after close/reopen or a revision refresh', () => {
  const captured = { session: 4, kbId: 'kb-1', expectedRevision: 7 }
  const live = {
    session: 4,
    kbId: 'kb-1' as string | null,
    open: true,
    disposed: false,
    revision: 7 as number | null,
    busy: false
  }
  assert.equal(canSubmitCapturedRemoval(captured, live), true)

  // 关闭后重开同一 KB：新 session/revision，旧 member + 旧确认不能借用新版本发 DELETE
  assert.equal(canSubmitCapturedRemoval(captured, { ...live, session: 6, revision: 8 }), false)
  assert.equal(canSubmitCapturedRemoval(captured, { ...live, session: 6 }), false)
  // 列表在同一会话内被刷新到新 revision：需要用户重新确认，绝不静默换版本提交
  assert.equal(canSubmitCapturedRemoval(captured, { ...live, revision: 8 }), false)
  // 等待确认期间有其它 mutation 进行中
  assert.equal(canSubmitCapturedRemoval(captured, { ...live, busy: true }), false)
  // 已关闭 / 已卸载 / 切换了 KB
  assert.equal(canSubmitCapturedRemoval(captured, { ...live, open: false }), false)
  assert.equal(canSubmitCapturedRemoval(captured, { ...live, disposed: true }), false)
  assert.equal(canSubmitCapturedRemoval(captured, { ...live, kbId: 'kb-2' }), false)
  // 非整数 revision 永远不能提交
  assert.equal(canSubmitCapturedRemoval({ ...captured, expectedRevision: 1.5 }, live), false)
})