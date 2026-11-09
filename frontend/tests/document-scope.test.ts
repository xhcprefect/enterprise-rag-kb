/**
 * 会话文档范围：既有 normalize/patch/SSE/revision 兼容覆盖，
 * 以及本轮返回修复新增的严格校验 / 确认控制器 / 列表分页覆盖。
 */
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
import {
  createScopeConfirmController,
  createScopeDetailFetcher,
  createScopeLoadGuard,
  decideScopeFreshChecks,
  hasMoreScopePages,
  isStrictIndexVersion,
  mergeScopeListPage,
  resolveSelectedIndexVersion,
  validateScopeSelection,
  type ScopeFreshCheck
} from '../src/utils/documentList.ts'

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

// ---------- 本轮严格校验 ----------

test('strict index version rejects floats, booleans, null, strings and negatives', () => {
  assert.equal(isStrictIndexVersion(0), true)
  assert.equal(isStrictIndexVersion(7), true)
  for (const bad of [1.5, 2.0000001, -1, -0.5, true, false, null, undefined, '3', NaN, Infinity]) {
    assert.equal(isStrictIndexVersion(bad), false, String(bad))
  }
})

test('selection validation rejects bad max, too many, duplicates and malformed items', () => {
  const ok = validateScopeSelection([handbook, policy], 5)
  assert.equal(ok.ok, true)
  assert.deepEqual(ok.ok ? ok.items : null, [handbook, policy])

  assert.deepEqual(validateScopeSelection([handbook], -1), { ok: false, reason: 'invalidMax' })
  assert.deepEqual(validateScopeSelection([handbook], 1.5), { ok: false, reason: 'invalidMax' })

  const six = ['a', 'b', 'c', 'd', 'e', 'f'].map((id) => ({ document_id: id, filename: id, index_version: 0 }))
  // 不接受"slice 前 5 再发 6 个"：超界直接拒绝
  assert.deepEqual(validateScopeSelection(six, 5), { ok: false, reason: 'tooMany' })

  assert.deepEqual(
    validateScopeSelection([handbook, { ...handbook }], 5),
    { ok: false, reason: 'duplicate', documentId: 'doc-handbook' }
  )
  assert.deepEqual(validateScopeSelection([{ filename: 'x', index_version: 0 }], 5), { ok: false, reason: 'missingDocumentId' })
  assert.deepEqual(
    validateScopeSelection([{ document_id: 'd', filename: '', index_version: 0 }], 5),
    { ok: false, reason: 'invalidFilename', documentId: 'd' }
  )
  assert.deepEqual(
    validateScopeSelection([{ document_id: 'd', filename: 'd', index_version: null }], 5),
    { ok: false, reason: 'invalidIndexVersion', documentId: 'd' }
  )
  assert.deepEqual(
    validateScopeSelection([{ document_id: 'd', filename: 'd', index_version: 1.5 }], 5),
    { ok: false, reason: 'invalidIndexVersion', documentId: 'd' }
  )
})

// ---------- 确认前新鲜详情 ----------

const selected = [
  { document_id: 'doc-a', index_version: 2 },
  { document_id: 'doc-b', index_version: 1 }
]

function check(id: string, retrievable: boolean, version: number | null): ScopeFreshCheck {
  return { documentId: id, retrievable, index_version: version }
}

test('all fresh details retrievable with unchanged versions pass', () => {
  assert.deepEqual(
    decideScopeFreshChecks(selected, [check('doc-a', true, 2), check('doc-b', true, 1)]),
    { status: 'ok' }
  )
})

test('a not-retrievable detail blocks confirmation and names the document', () => {
  assert.deepEqual(
    decideScopeFreshChecks(selected, [check('doc-a', true, 2), check('doc-b', false, 1)]),
    { status: 'notRetrievable', documentId: 'doc-b' }
  )
  assert.deepEqual(
    decideScopeFreshChecks(selected, [check('doc-a', false, 2), check('doc-b', true, 1)]),
    { status: 'notRetrievable', documentId: 'doc-a' }
  )
})

test('a re-indexed document is rejected, never silently accepted, and null versions never pass', () => {
  assert.deepEqual(
    decideScopeFreshChecks(selected, [check('doc-a', true, 3), check('doc-b', true, 1)]),
    { status: 'versionChanged', documentId: 'doc-a', selectedVersion: 2, currentVersion: 3 }
  )
  assert.deepEqual(
    decideScopeFreshChecks([{ document_id: 'doc-a', index_version: 5 }], [check('doc-a', true, 2)]),
    { status: 'versionChanged', documentId: 'doc-a', selectedVersion: 5, currentVersion: 2 }
  )
  // 详情缺 index_version 或非严格整数：不能当成通过
  assert.deepEqual(
    decideScopeFreshChecks([{ document_id: 'doc-a', index_version: 0 }], [check('doc-a', true, null)]),
    { status: 'versionChanged', documentId: 'doc-a', selectedVersion: 0, currentVersion: null }
  )
  // float 不得截断后与选中值相等而放行（2.5 → 不能被当成 2）
  assert.deepEqual(
    decideScopeFreshChecks([{ document_id: 'doc-a', index_version: 2 }], [check('doc-a', true, 2.5)]),
    { status: 'versionChanged', documentId: 'doc-a', selectedVersion: 2, currentVersion: null }
  )
  assert.deepEqual(
    decideScopeFreshChecks([{ document_id: 'doc-a', index_version: 1 }], [check('doc-a', true, true as unknown as number)]),
    { status: 'versionChanged', documentId: 'doc-a', selectedVersion: 1, currentVersion: null }
  )
})

test('a failed or foreign detail blocks confirmation instead of pretending success', () => {
  assert.deepEqual(
    decideScopeFreshChecks(selected, [check('doc-a', true, 2), null]),
    { status: 'detailMissing', documentId: 'doc-b' }
  )
  assert.deepEqual(
    decideScopeFreshChecks(selected, [check('doc-b', true, 1), check('doc-a', true, 2)]),
    { status: 'detailMissing', documentId: 'doc-a' }
  )
})

// ---------- 列表分页 ----------

function doc(id: string, version: number, overrides: Partial<Record<string, unknown>> = {}) {
  return { id, filename: `${id}.md`, status: 'ready', retrievable: true, index_version: version, ...overrides }
}

test('pagination uses the raw server page length, not the locally filtered count', () => {
  // 一页 20 条原始数据里，本地只接受 2 条（其余不可检索/坏代次），仍必须能继续翻页
  const raw = Array.from({ length: 18 }, (_, i) => doc(`bad-${i}`, 1, { retrievable: false }))
  raw.push(doc('good-1', 2), doc('good-2', 3))
  const merged = mergeScopeListPage([], { items: raw, total: 40 })
  assert.equal(merged.docs.length, 2)
  assert.equal(merged.lastPageSize, 20)
  assert.equal(merged.total, 40)
  assert.equal(hasMoreScopePages(merged.lastPageSize, merged.docs.length, merged.total, 20), true)
  // 只有服务端原始页长不足一页时才停止
  assert.equal(hasMoreScopePages(5, 5, 5, 20), false)
  assert.equal(hasMoreScopePages(20, 20, 20, 20), false)
})

test('list merge drops non-ready, non-retrievable and non-strict versions, and dedupes', () => {
  const merged = mergeScopeListPage([], {
    items: [
      doc('a', 1),
      doc('b', 1, { retrievable: false }),
      doc('c', 1, { status: 'processing' }),
      doc('d', 1.5),
      doc('e', 1, { index_version: null }),
      doc('f', 1, { index_version: true }),
      null,
      'junk',
      doc('a', 9)
    ],
    total: 9
  })
  assert.deepEqual(merged.docs.map((item) => item.id), ['a'])
  assert.equal(merged.lastPageSize, 9)
  assert.equal(merged.total, 9)

  const next = mergeScopeListPage(merged.docs, { items: [doc('a', 2), doc('g', 0)], total: 3 })
  assert.deepEqual(next.docs.map((item) => item.id), ['a', 'g'])
  assert.equal(next.docs[0].index_version, 1, 'existing row keeps its loaded value')
})

test('missing server total falls back to loaded count without fabricating more pages', () => {
  const merged = mergeScopeListPage([], { items: [doc('a', 1)] })
  assert.equal(merged.total, 1)
  assert.equal(hasMoreScopePages(merged.lastPageSize, 1, merged.total, 20), false)
})

// ---------- 确认控制器 ----------

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

test('double confirm starts only one detail round and the second reports busy', async () => {
  const gate = deferred<ScopeFreshCheck | null>()
  let calls = 0
  const controller = createScopeConfirmController({
    getDetail: () => { calls += 1; return gate.promise }
  })
  const first = controller.confirm([handbook])
  const second = await controller.confirm([handbook])
  assert.deepEqual(second, { status: 'busy' })
  assert.equal(calls, 1)
  gate.resolve({ documentId: 'doc-handbook', retrievable: true, index_version: 3 })
  assert.deepEqual(await first, { status: 'confirmed', items: [handbook] })
})

test('confirmation freezes the selection snapshot taken at confirm time', async () => {
  const gate = deferred<ScopeFreshCheck | null>()
  const controller = createScopeConfirmController({ getDetail: () => gate.promise })
  const live = [{ ...handbook }]
  const pending = controller.confirm(live)
  // 调用方在等待期间改动数组/对象不应影响本次确认内容
  live.push(policy)
  live[0].index_version = 99
  gate.resolve({ documentId: 'doc-handbook', retrievable: true, index_version: 3 })
  assert.deepEqual(await pending, { status: 'confirmed', items: [handbook] })
})

test('invalidate during await yields invalidated, emits nothing and never frees a new session busy', async () => {
  const firstGate = deferred<ScopeFreshCheck | null>()
  const secondGate = deferred<ScopeFreshCheck | null>()
  let call = 0
  const controller = createScopeConfirmController({
    getDetail: () => { call += 1; return call === 1 ? firstGate.promise : secondGate.promise }
  })
  const stale = controller.confirm([handbook])
  // 关闭/切 KB：作废旧请求并开始新会话
  controller.invalidate()
  assert.equal(controller.inFlight, false)
  const fresh = controller.confirm([handbook])
  assert.equal(controller.inFlight, true)

  // 旧请求迟到完成：不能 emit，也不能把新会话的 busy 清掉
  firstGate.resolve({ documentId: 'doc-handbook', retrievable: true, index_version: 3 })
  assert.deepEqual(await stale, { status: 'invalidated' })
  assert.equal(controller.inFlight, true, 'old finally must not clear the new busy')
  assert.equal(call, 2)

  secondGate.resolve({ documentId: 'doc-handbook', retrievable: true, index_version: 3 })
  assert.deepEqual(await fresh, { status: 'confirmed', items: [handbook] })
  assert.equal(controller.inFlight, false)
})

test('rejection from getDetail is treated as missing detail, not success', async () => {
  const controller = createScopeConfirmController({
    getDetail: async () => { throw new Error('network') }
  })
  assert.deepEqual(await controller.confirm([handbook]), { status: 'detailMissing', documentId: 'doc-handbook' })
  assert.equal(controller.inFlight, false)
})

test('controller rejects invalid max, overflow, duplicates and non-retrievable/version drift before emitting', async () => {
  const seen: string[] = []
  const controller = createScopeConfirmController({
    getDetail: async (id) => {
      seen.push(id)
      const version = id === 'doc-drift' ? 9 : id === 'doc-handbook' ? 3 : 1
      return { documentId: id, retrievable: id !== 'doc-off', index_version: version }
    }
  })
  assert.deepEqual(await controller.confirm([handbook], 6), { status: 'invalidSelection', reason: 'invalidMax' })
  assert.deepEqual(await controller.confirm([handbook], -1), { status: 'invalidSelection', reason: 'invalidMax' })

  const six = ['a', 'b', 'c', 'd', 'e', 'f'].map((id) => ({ document_id: id, filename: id, index_version: 0 }))
  assert.deepEqual(await controller.confirm(six, 5), { status: 'invalidSelection', reason: 'tooMany' })
  assert.equal(seen.length, 0, 'invalid payloads must not hit the network')

  assert.deepEqual(
    await controller.confirm([handbook, { ...policy }], 5),
    // 同一时间没有重复；这里验证真正重复
    { status: 'confirmed', items: [handbook, policy] }
  )
  assert.deepEqual(
    await controller.confirm([handbook, { ...handbook }], 5),
    { status: 'invalidSelection', reason: 'duplicate', documentId: 'doc-handbook' }
  )
  assert.deepEqual(
    await controller.confirm([{ document_id: 'doc-off', filename: 'x', index_version: 1 }, handbook]),
    { status: 'notRetrievable', documentId: 'doc-off' }
  )
  assert.deepEqual(
    await controller.confirm([{ document_id: 'doc-drift', filename: 'x', index_version: 1 }]),
    { status: 'versionChanged', documentId: 'doc-drift', selectedVersion: 1, currentVersion: 9 }
  )
})

test('empty selection confirms with no detail requests and no busy state', async () => {
  let calls = 0
  const controller = createScopeConfirmController({ getDetail: async () => { calls += 1; return null } })
  assert.deepEqual(await controller.confirm([]), { status: 'empty' })
  assert.equal(calls, 0)
  assert.equal(controller.inFlight, false)
})

test('all 5 selected documents are checked and confirmed, never a partial slice', async () => {
  const ids: string[] = []
  const controller = createScopeConfirmController({
    getDetail: async (id) => { ids.push(id); return { documentId: id, retrievable: true, index_version: 0 } }
  })
  const five = ['d1', 'd2', 'd3', 'd4', 'd5'].map((id) => ({ document_id: id, filename: id, index_version: 0 }))
  assert.deepEqual(await controller.confirm(five, 5), { status: 'confirmed', items: five })
  assert.deepEqual(ids, ['d1', 'd2', 'd3', 'd4', 'd5'])
})

test('a check budget smaller than the selection refuses instead of confirming a partial slice', async () => {
  const ids: string[] = []
  // 预算 3 而选 5：旧实现 slice(0,3) 只核验 3 项却确认 5 项，这里必须明确拒绝
  const controller = createScopeConfirmController({
    maxChecks: 3,
    getDetail: async (id) => { ids.push(id); return { documentId: id, retrievable: true, index_version: 0 } }
  })
  const five = ['d1', 'd2', 'd3', 'd4', 'd5'].map((id) => ({ document_id: id, filename: id, index_version: 0 }))
  assert.deepEqual(await controller.confirm(five, 5), { status: 'insufficientChecks' })
  assert.deepEqual(ids, [], 'an unverifiable selection must not hit the network')
  assert.equal(controller.inFlight, false)
  // 预算内的选择仍然正常核验
  assert.deepEqual(
    await controller.confirm(five.slice(0, 3), 5),
    { status: 'confirmed', items: five.slice(0, 3) }
  )
  assert.deepEqual(ids, ['d1', 'd2', 'd3'])
})

test('decideScopeFreshChecks never returns ok for a bad or insufficient budget', () => {
  // 0/负数/小数/NaN/Infinity/字符串/超过 MAX：都明确拒绝
  for (const budget of [0, -1, 1.5, NaN, Number.POSITIVE_INFINITY, '5' as unknown as number, MAX_SCOPE_DOCUMENTS + 1]) {
    assert.deepEqual(
      decideScopeFreshChecks(selected, [check('doc-a', true, 2), check('doc-b', true, 1)], budget),
      { status: 'insufficientChecks' },
      String(budget)
    )
  }
  // 预算少于选中项：不能只核验前几项就通过
  assert.deepEqual(
    decideScopeFreshChecks(selected, [check('doc-a', true, 2), check('doc-b', true, 1)], 1),
    { status: 'insufficientChecks' }
  )
  // 详情数组比选中项短：同样不通过
  assert.deepEqual(
    decideScopeFreshChecks(selected, [check('doc-a', true, 2)], 5),
    { status: 'insufficientChecks' }
  )
  // 坏输入（非数组）不抛错
  assert.deepEqual(
    decideScopeFreshChecks(selected, null as unknown as ScopeFreshCheck[], 5),
    { status: 'insufficientChecks' }
  )
  assert.deepEqual(
    decideScopeFreshChecks(null as unknown as [], [], 5),
    { status: 'insufficientChecks' }
  )
  // 选中项本身超过后端上限：不核验也不通过
  const six = Array.from({ length: MAX_SCOPE_DOCUMENTS + 1 }, (_, i) => ({
    document_id: `d${i}`, index_version: 0
  }))
  assert.deepEqual(
    decideScopeFreshChecks(six, Array.from({ length: 6 }, (_, i) => check(`d${i}`, true, 0)), 5),
    { status: 'insufficientChecks' }
  )
})

test('validateScopeSelection enforces the backend max and rejects non-array input explicitly', () => {
  const five = ['a', 'b', 'c', 'd', 'e'].map((id) => ({ document_id: id, filename: id, index_version: 0 }))
  // max 大于后端上限不放宽：即使 items 只有 1 个也拒绝
  assert.deepEqual(validateScopeSelection([handbook], MAX_SCOPE_DOCUMENTS + 1), { ok: false, reason: 'invalidMax' })
  assert.deepEqual(validateScopeSelection([handbook], Number.POSITIVE_INFINITY), { ok: false, reason: 'invalidMax' })
  assert.deepEqual(validateScopeSelection([handbook], NaN), { ok: false, reason: 'invalidMax' })
  // 5 项 / max=5 仍合法
  assert.equal(validateScopeSelection(five, MAX_SCOPE_DOCUMENTS).ok, true)
  // 非数组：明确 invalidInput，不退化成空选择
  assert.deepEqual(
    validateScopeSelection(null as unknown as [], 5),
    { ok: false, reason: 'invalidInput' }
  )
  assert.deepEqual(
    validateScopeSelection('not-an-array' as unknown as [], 5),
    { ok: false, reason: 'invalidInput' }
  )
})

test('a misconfigured check budget is rejected, not silently replaced by 5', async () => {
  for (const bad of [0, -1, 1.5, NaN, Number.POSITIVE_INFINITY]) {
    let calls = 0
    const controller = createScopeConfirmController({
      maxChecks: bad,
      getDetail: async (id) => { calls += 1; return { documentId: id, retrievable: true, index_version: 3 } }
    })
    assert.deepEqual(await controller.confirm([handbook]), { status: 'insufficientChecks' }, String(bad))
    assert.equal(calls, 0, 'a rejected budget must not hit the network')
    assert.equal(controller.inFlight, false)
  }
  // undefined 才走默认 5：正常确认
  const ok = createScopeConfirmController({
    getDetail: async (id) => ({ documentId: id, retrievable: true, index_version: 3 })
  })
  assert.deepEqual(await ok.confirm([handbook]), { status: 'confirmed', items: [handbook] })
})

test('detail fetcher binds the initiating KB and rejects foreign or late responses', async () => {
  let kb = 'kb-a'
  const gate = deferred<unknown>()
  const fetched: string[] = []
  const getDetail = createScopeDetailFetcher({
    kbId: () => 'kb-a',
    isKbCurrent: (kbId) => kbId === kb,
    fetch: async (kbId, documentId) => { fetched.push(`${kbId}/${documentId}`); return gate.promise }
  })

  const pending = getDetail('doc-a')
  // 请求期间切库：迟到的详情不能用于新库
  kb = 'kb-b'
  gate.resolve({ id: 'doc-a', kb_id: 'kb-a', retrievable: true, index_version: 2 })
  assert.equal(await pending, null)
  assert.deepEqual(fetched, ['kb-a/doc-a'])

  kb = 'kb-a'
  const ok = createScopeDetailFetcher({
    kbId: () => 'kb-a',
    isKbCurrent: (kbId) => kbId === kb,
    fetch: async () => ({ id: 'doc-a', kb_id: 'kb-a', retrievable: true, index_version: 2 })
  })
  assert.deepEqual(await ok('doc-a'), { documentId: 'doc-a', retrievable: true, index_version: 2 })

  // 响应里的 kb_id 与发起 KB 不符：当作详情缺失，绝不冒领
  const foreign = createScopeDetailFetcher({
    kbId: () => 'kb-a',
    isKbCurrent: () => true,
    fetch: async () => ({ id: 'doc-a', kb_id: 'kb-b', retrievable: true, index_version: 2 })
  })
  assert.equal(await foreign('doc-a'), null)

  // 响应 id 与请求 id 不符
  const mismatched = createScopeDetailFetcher({
    kbId: () => 'kb-a',
    isKbCurrent: () => true,
    fetch: async () => ({ id: 'doc-other', kb_id: 'kb-a', retrievable: true, index_version: 2 })
  })
  assert.equal(await mismatched('doc-a'), null)

  // 缺少 kb_id（旧后端）：无法确认归属，同样拒绝
  const legacy = createScopeDetailFetcher({
    kbId: () => 'kb-a',
    isKbCurrent: () => true,
    fetch: async () => ({ id: 'doc-a', retrievable: true, index_version: 2 })
  })
  assert.equal(await legacy('doc-a'), null)

  // 同一个 KB 但对话框已关闭：isKbCurrent 必须为 false（不能只看 KB 相等）
  let open = true
  const closeGate = deferred<unknown>()
  const closeAware = createScopeDetailFetcher({
    kbId: () => 'kb-a',
    isKbCurrent: (kbId) => open && kbId === 'kb-a',
    fetch: async () => closeGate.promise
  })
  const closing = closeAware('doc-a')
  open = false
  closeGate.resolve({ id: 'doc-a', kb_id: 'kb-a', retrievable: true, index_version: 2 })
  assert.equal(await closing, null, 'closing the dialog must void an in-flight detail even in the same KB')

  // 发起时 KB 已被清空（关闭）或已切走：不发请求
  let calls = 0
  const closed = createScopeDetailFetcher({
    kbId: () => '',
    isKbCurrent: () => true,
    fetch: async () => { calls += 1; return null }
  })
  assert.equal(await closed('doc-a'), null)
  assert.equal(calls, 0)
})

// ---------- 代次选择与列表守卫 ----------

test('kept selections keep their original version; fresh picks bind the list version', () => {
  assert.equal(resolveSelectedIndexVersion(true, 3, 9), 3)
  assert.equal(resolveSelectedIndexVersion(false, 3, 9), 9)
  // 移除后重选（kept=false）才升级到当前代次
  assert.equal(resolveSelectedIndexVersion(false, undefined, 0), 0)
  // 非严格代次（float/bool/null）一律拒绝，不退化成 0
  assert.equal(resolveSelectedIndexVersion(true, 1.5, 2), null)
  assert.equal(resolveSelectedIndexVersion(false, 2, true), null)
  assert.equal(resolveSelectedIndexVersion(true, null, 2), null)
  assert.equal(resolveSelectedIndexVersion(false, 2, undefined), null)
})

test('load guard invalidates old sessions on reset and old finally cannot clear new busy', () => {
  const guard = createScopeLoadGuard()
  const first = guard.begin(true)
  assert.equal(guard.isCurrent(first), true)
  assert.equal(guard.isCurrent(guard.begin(false)), true, 'pagination token shares the session')

  const oldToken = first
  // 关闭/切 KB/卸载：会话重置，旧 token 全部失效
  guard.reset()
  assert.equal(guard.isCurrent(oldToken), false)
  const fresh = guard.begin(true)
  assert.equal(guard.isCurrent(fresh), true)
  assert.equal(guard.isCurrent(oldToken), false, 'stale token never acts after reset')

  // 同会话内翻页（begin 不带 reset）：上一个代次的 token 也立即失效
  const pageOne = guard.begin(false)
  assert.equal(guard.isCurrent(fresh), false)
  assert.equal(guard.isCurrent(pageOne), true)

  // 新会话又翻页：每次 begin 都开新代次，reset 时的 token 也不再是当前代次
  const pageTwo = guard.begin(false)
  assert.equal(guard.isCurrent(fresh), false)
  assert.equal(guard.isCurrent(pageTwo), true)
  // 代次单调递增，不会回到旧代次
  assert.ok(pageTwo.generation > fresh.generation)
})

test('close, KB switch and unmount before the details arrive never emit a confirm', async () => {
  // 关闭/切 KB/卸载都会 invalidate：即便详情"成功后"才到，也不能变成 confirmed
  for (const action of ['close', 'kbSwitch', 'unmount'] as const) {
    const gate = deferred<ScopeFreshCheck | null>()
    const controller = createScopeConfirmController({ getDetail: () => gate.promise })
    const pending = controller.confirm([handbook])
    if (action === 'close' || action === 'kbSwitch' || action === 'unmount') controller.invalidate()
    gate.resolve({ documentId: 'doc-handbook', retrievable: true, index_version: 3 })
    assert.deepEqual(await pending, { status: 'invalidated' }, action)
  }
})
