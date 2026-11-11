import assert from 'node:assert/strict'
import test from 'node:test'
import {
  canManageDocuments,
  canManageMembers,
  capabilitiesFromKb,
  capabilityStateAfterFailure,
  classifyKbCapabilityError,
  completionReceiptStillApplies,
  docMissingAction,
  revokesCapability
} from '../src/utils/kbCapabilities.ts'

test('missing capability fields are known but deny both capabilities', () => {
  const legacy = { kind: 'upload' as const }
  assert.deepEqual(capabilitiesFromKb(legacy), {
    status: 'known',
    documents: false,
    members: false
  })
  assert.equal(canManageDocuments(legacy), false)
  assert.equal(canManageMembers(legacy), false)
})

test('null KB is unknown, never treated as a denied or allowed capability', () => {
  assert.deepEqual(capabilitiesFromKb(null), { status: 'unknown' })
  assert.deepEqual(capabilitiesFromKb(undefined), { status: 'unknown' })
  assert.equal(canManageDocuments(null), false)
  assert.equal(canManageMembers(null), false)
})

test('capabilities require strictly true flags and never fall back to an admin role', () => {
  const truthyButNotTrue = {
    kind: 'upload' as const,
    can_manage_documents: 1 as unknown as boolean,
    can_manage_members: 'true' as unknown as boolean
  }
  assert.equal(canManageDocuments(truthyButNotTrue), false)
  assert.equal(canManageMembers(truthyButNotTrue), false)

  // 这个辅助函数没有 admin 参数：勾选 admin 身份也无法让缺失/为 false 的能力放行。
  const denied = { kind: 'upload' as const, can_manage_documents: false, can_manage_members: false }
  assert.equal(canManageDocuments(denied), false)
  assert.equal(canManageMembers(denied), false)
})

test('document maintenance is limited to upload KBs, while member management is not', () => {
  const project = {
    kind: 'project' as const,
    can_manage_documents: true,
    can_manage_members: true
  }
  assert.equal(canManageDocuments(project), false)
  assert.equal(canManageMembers(project), true)

  const legacyUpload = { can_manage_documents: true, can_manage_members: true }
  assert.equal(canManageDocuments(legacyUpload), true)
  assert.equal(canManageMembers(legacyUpload), true)
})

test('permission and not-found failures revoke capabilities; conflicts and transient errors do not', () => {
  for (const status of [401, 403, 404, 410]) {
    assert.equal(revokesCapability({ response: { status } }), true, String(status))
  }
  for (const status of [400, 409, 422, 429, 500, 502, undefined]) {
    assert.equal(revokesCapability({ response: { status } }), false, String(status))
  }
  assert.equal(revokesCapability({ code: 'ERR_NETWORK' }), false)
  assert.equal(revokesCapability(new Error('boom')), false)
  assert.equal(revokesCapability({ code: 'ERR_CANCELED' }), false)
  assert.equal(revokesCapability({ name: 'AbortError' }), false)
})

test('error classification distinguishes auth, forbidden, not-found, conflict, and cancellation', () => {
  assert.equal(classifyKbCapabilityError({ response: { status: 401 } }), 'unauthenticated')
  assert.equal(classifyKbCapabilityError({ response: { status: 403 } }), 'forbidden')
  assert.equal(classifyKbCapabilityError({ response: { status: 404 } }), 'not_found')
  assert.equal(classifyKbCapabilityError({ response: { status: 409 } }), 'conflict')
  assert.equal(classifyKbCapabilityError({ code: 'ERR_CANCELED' }), 'cancelled')
  assert.equal(classifyKbCapabilityError({ response: { status: 500 } }), 'other')
})

test('a failed capability load converges to unknown instead of retaining the previous grant', () => {
  assert.deepEqual(capabilityStateAfterFailure(), { status: 'unknown' })
})

test('a bounded completion receipt is replayed only for the same entry and upload generation', () => {
  const receipt = { key: 'entry#1', generation: 7 }
  assert.equal(completionReceiptStillApplies(receipt, { key: 'entry#1', phase: 'uploading' }, 7, true), true)
  assert.equal(completionReceiptStillApplies(receipt, { key: 'entry#1', phase: 'paused' }, 7, true), true)
  // 明确撤权后绝不重放
  assert.equal(completionReceiptStillApplies(receipt, { key: 'entry#1', phase: 'uploading' }, 7, false), false)
  // 已推进到别的条目 / 重试后新一代 worker / 已进入终态
  assert.equal(completionReceiptStillApplies(receipt, { key: 'entry#2', phase: 'uploading' }, 7, true), false)
  assert.equal(completionReceiptStillApplies(receipt, { key: 'entry#1', phase: 'uploading' }, 8, true), false)
  assert.equal(completionReceiptStillApplies(receipt, { key: 'entry#1', phase: 'done' }, 7, true), false)
  assert.equal(completionReceiptStillApplies(null, { key: 'entry#1', phase: 'uploading' }, 7, true), false)
})

test('a single-document 404 advances only after a fresh capability confirms maintenance', () => {
  assert.equal(docMissingAction({ status: 'known', documents: true, members: false }), 'advance')
  assert.equal(docMissingAction({ status: 'known', documents: false, members: true }), 'stop')
  // 未知（包括查询失败）不能凭旧快照推进
  assert.equal(docMissingAction({ status: 'unknown' }), 'stop')
})