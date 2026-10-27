import assert from 'node:assert/strict'
import test from 'node:test'
import {
  MAX_SCOPE_DOCUMENTS,
  applyScopeUpdate,
  buildScopePatchBody,
  buildScopeSendOptions,
  conversationScope,
  normalizeDocumentScope,
  normalizeScopeRevision,
  planScopeChange,
  resolveScopePatchResponse,
  sameDocumentScope,
  scopeDocumentIds,
  scopeItemFromDocument,
  toggleDocumentId
} from '../src/utils/documentScope.ts'

const handbook = { document_id: 'doc-handbook', filename: '星海科技内部管理手册.md', index_version: 3 }
const policy = { document_id: 'doc-policy', filename: '差旅制度.md', index_version: 1 }

test('legacy conversations without scope fields normalize to the whole knowledge base', () => {
  assert.deepEqual(conversationScope(undefined), { items: [], revision: 0 })
  assert.deepEqual(conversationScope(null), { items: [], revision: 0 })
  assert.deepEqual(conversationScope({}), { items: [], revision: 0 })
  assert.deepEqual(
    conversationScope({ document_scope: [handbook], scope_revision: 2 }),
    { items: [handbook], revision: 2 }
  )
})

test('scope normalization tolerates malformed entries and duplicate document ids', () => {
  assert.deepEqual(normalizeDocumentScope(undefined), [])
  assert.deepEqual(normalizeDocumentScope({ document_id: 'not-an-array' }), [])
  assert.deepEqual(normalizeDocumentScope([
    null,
    'doc',
    { document_id: '  ' },
    { document_id: ' doc-a ', filename: '', index_version: 2.9 },
    { document_id: 'doc-a', filename: 'duplicate', index_version: 9 },
    { document_id: 'doc-b', filename: ' 制度.md ', index_version: -1 },
    { document_id: 'doc-c', filename: 'c.md', index_version: Number.NaN }
  ]), [
    { document_id: 'doc-a', filename: 'doc-a', index_version: 2 },
    { document_id: 'doc-b', filename: '制度.md', index_version: 0 },
    { document_id: 'doc-c', filename: 'c.md', index_version: 0 }
  ])
})

test('scope revisions only accept non-negative finite numbers', () => {
  assert.equal(normalizeScopeRevision(undefined), 0)
  assert.equal(normalizeScopeRevision('3'), 0)
  assert.equal(normalizeScopeRevision(-2), 0)
  assert.equal(normalizeScopeRevision(1.9), 1)
  assert.equal(normalizeScopeRevision(Number.POSITIVE_INFINITY), 0)
})

test('scope updates ignore payloads without new fields and reject stale revisions', () => {
  const current = { items: [handbook], revision: 4 }

  assert.equal(applyScopeUpdate(current, {}), null)
  assert.equal(applyScopeUpdate(current, { document_scope: undefined, scope_revision: undefined }), null)
  assert.equal(applyScopeUpdate(current, null), null)

  // 过期流（例如用户在另一个标签页改了范围）不能覆盖新状态
  assert.equal(applyScopeUpdate(current, { document_scope: [policy], scope_revision: 3 }), null)

  assert.deepEqual(applyScopeUpdate(current, { document_scope: [policy], scope_revision: 5 }), {
    items: [policy],
    revision: 5
  })

  // 同版本但带了新范围：以收到的内容为准（避免后端重算后 UI 仍显示旧文件名）
  assert.deepEqual(applyScopeUpdate(current, { document_scope: [policy], scope_revision: 4 }), {
    items: [policy],
    revision: 4
  })
})

test('revision-only and scope-only stream payloads update only the supplied half', () => {
  const current = { items: [handbook], revision: 2 }

  assert.deepEqual(applyScopeUpdate(current, { scope_revision: 3 }), {
    items: [handbook],
    revision: 3
  })
  assert.deepEqual(applyScopeUpdate(current, { document_scope: [policy] }), {
    items: [policy],
    revision: 2
  })

  // 只收到一个无法识别的 revision 时，不改文档列表
  assert.deepEqual(applyScopeUpdate(current, { scope_revision: 'broken' }), null)
})

test('send options carry a draft only for new conversations and a revision only for existing ones', () => {
  assert.deepEqual(buildScopeSendOptions({
    mode: 'chat', items: [handbook], hasConversation: false, revision: 0
  }), {})

  assert.deepEqual(buildScopeSendOptions({
    mode: 'rag', items: [handbook, policy], hasConversation: false, revision: 0
  }), { document_ids: ['doc-handbook', 'doc-policy'] })

  assert.deepEqual(buildScopeSendOptions({
    mode: 'rag', items: [], hasConversation: false, revision: 0
  }), {})

  assert.deepEqual(buildScopeSendOptions({
    mode: 'rag', items: [handbook], hasConversation: true, revision: 7
  }), { scope_revision: 7 })

  // 旧会话缺失版本时按 0 发送真实快照，由服务端决定是否冲突
  assert.deepEqual(buildScopeSendOptions({
    mode: 'rag', items: [handbook], hasConversation: true, revision: 0
  }), { scope_revision: 0 })
})

test('scope PATCH body sends every supplied revision snapshot, including zero', () => {
  assert.deepEqual(buildScopePatchBody(['doc-a']), { document_ids: ['doc-a'] })
  assert.deepEqual(buildScopePatchBody(['doc-a'], 0), { document_ids: ['doc-a'], expected_revision: 0 })
  assert.deepEqual(buildScopePatchBody([], 4), { document_ids: [], expected_revision: 4 })
  assert.deepEqual(buildScopePatchBody(['doc-a'], -1), { document_ids: ['doc-a'] })

  const ids = ['doc-a']
  const body = buildScopePatchBody(ids, 2)
  ids.push('doc-b')
  assert.deepEqual(body.document_ids, ['doc-a'], 'the request body must own its array copy')
})

test('multi-select allows removal at the limit and rejects a sixth document', () => {
  let selection: string[] = []
  for (const id of ['a', 'b', 'c', 'd', 'e']) {
    const result = toggleDocumentId(selection, id)
    assert.equal(result.rejected, false)
    selection = result.ids
  }
  assert.equal(selection.length, MAX_SCOPE_DOCUMENTS)

  const rejected = toggleDocumentId(selection, 'f')
  assert.equal(rejected.rejected, true)
  assert.deepEqual(rejected.ids, selection)

  const removed = toggleDocumentId(selection, 'c')
  assert.equal(removed.rejected, false)
  assert.deepEqual(removed.ids, ['a', 'b', 'd', 'e'])
  assert.deepEqual(toggleDocumentId(removed.ids, 'c').ids, ['a', 'b', 'd', 'e', 'c'])
  assert.equal(toggleDocumentId([], 'a', 0).rejected, true)
})

test('scope comparisons ignore order but keep index versions significant', () => {
  const reversed = [policy, handbook]
  assert.equal(sameDocumentScope([handbook, policy], reversed), true)
  assert.deepEqual(scopeDocumentIds(reversed), ['doc-policy', 'doc-handbook'])
  assert.equal(
    sameDocumentScope([handbook], [{ ...handbook, index_version: handbook.index_version + 1 }]),
    false
  )
  assert.equal(sameDocumentScope([], []), true)
})

test('citation quick action builds a scope item without inventing missing identity', () => {
  assert.equal(scopeItemFromDocument(null, '手册.md', 1), null)
  assert.equal(scopeItemFromDocument('   ', '手册.md', 1), null)
  assert.deepEqual(scopeItemFromDocument(' doc-a ', null, undefined), {
    document_id: 'doc-a', filename: 'doc-a', index_version: 0
  })
  assert.deepEqual(scopeItemFromDocument('doc-a', ' 手册.md ', 2.8), {
    document_id: 'doc-a', filename: '手册.md', index_version: 2
  })
})

test('an existing conversation always re-PATCHes even for the same document ids', () => {
  // 文件重新索引后，本地缓存的 index_version 已经过期（文档列表也不带最新代次），
  // 用户再选一次同一篇必须真的发 PATCH，让服务端重新校验并绑定最新代次。
  assert.equal(planScopeChange([handbook], [{ ...handbook }], true), 'patch')
  assert.equal(planScopeChange([handbook], [handbook, policy], true), 'patch')
  assert.equal(planScopeChange([], [], true), 'patch')
  assert.equal(planScopeChange([handbook], [], true), 'patch')

  // 尚未创建会话时只是草稿：指纹相同就没有必要改动
  assert.equal(planScopeChange([handbook], [{ ...handbook }], false), 'skip')
  assert.equal(planScopeChange([handbook, policy], [policy, handbook], false), 'skip')
  assert.equal(planScopeChange([], [], false), 'skip')
  assert.equal(planScopeChange([], [handbook], false), 'draft')
  assert.equal(planScopeChange([handbook], [], false), 'draft')
})

test('a PATCH response missing document_scope or scope_revision is never treated as applied', () => {
  const current = { items: [handbook], revision: 5 }

  // 旧后端/代理只回了一部分字段时，不能拿请求里的 next 乐观改 UI：
  // 清空或扩大范围会在界面上"成功"，服务端却仍是旧范围。
  for (const payload of [
    undefined,
    null,
    'ok',
    {},
    { scope_revision: 6 },
    { document_scope: [] },
    { document_scope: [policy] },
    { scope_revision: 6, document_scope: 'broken' },
    { scope_revision: Number.NaN, document_scope: [policy] },
    { scope_revision: -1, document_scope: [policy] }
  ]) {
    assert.deepEqual(resolveScopePatchResponse(current, payload), { status: 'incomplete' }, JSON.stringify(payload))
  }

  assert.deepEqual(resolveScopePatchResponse(current, { document_scope: [], scope_revision: 6 }), {
    status: 'applied',
    snapshot: { items: [], revision: 6 }
  })
  // 服务端回的版本比本地还旧：过期快照，同样不能覆盖
  assert.deepEqual(resolveScopePatchResponse(current, { document_scope: [policy], scope_revision: 4 }), {
    status: 'stale'
  })
  // 版本相同也接受，避免后端重算文件名后 UI 仍显示旧名字
  assert.deepEqual(resolveScopePatchResponse(current, { document_scope: [policy], scope_revision: 5 }), {
    status: 'applied',
    snapshot: { items: [policy], revision: 5 }
  })
})
