/**
 * 复制与重试的 Node 测试。
 *
 * 复制：只读 textContent / 原始正文，绝不执行或回填 HTML。
 * 重试：UI 的 failed 不算证据，必须先查原请求；上下文变了就拒绝静默改依据。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  bindRetrySnapshot,
  buildRetryPayload,
  codeTextFromTarget,
  copyPlainText,
  freezeRetryContext,
  freezeRetrySnapshot,
  planRetry,
  planRetryBinding,
  resolveRetry,
  sameRetryContext,
  type RetryContext
} from '../src/utils/chatActions.ts'
import type { DocumentScopeItem } from '../src/utils/documentScope.ts'

const scope = (...ids: string[]): DocumentScopeItem[] =>
  ids.map((id) => ({ document_id: id, filename: `${id}.pdf`, index_version: 1 }))

const ragContext = (overrides: Partial<RetryContext> = {}): RetryContext => ({
  mode: 'rag',
  kbId: 'kb-1',
  conversationId: null,
  scopeRevision: 0,
  documentScope: [],
  ...overrides
})

const snapshot = (overrides: Record<string, unknown> = {}) => {
  const ids = ('documentIds' in overrides ? overrides.documentIds : ['doc-1', 'doc-2']) as string[] | null
  return freezeRetrySnapshot({
    question: '  合成安全响应预案有哪些要点？ ',
    mode: 'rag',
    kbId: 'kb-1',
    conversationId: null,
    topK: 8,
    documentIds: ids,
    // 默认冻结的草稿明细与 ids 一致（index_version=1）；已有会话没有草稿明细
    documentScope: 'documentScope' in overrides
      ? overrides.documentScope as never
      : ids?.length
        ? ids.map((id) => ({ document_id: id, filename: `${id}.pdf`, index_version: 1 }))
        : null,
    scopeRevision: 0,
    ...overrides
  })
}

test('project retries retain the bound snapshot but omit snapshot and document fields for existing conversations', () => {
  const context = ragContext({ conversationId: 'project-conv', project: true, projectSnapshotId: 'snapshot-a', scopeRevision: 2 })
  const frozen = snapshot({ conversationId: 'project-conv', project: true, projectSnapshotId: 'snapshot-a', scopeRevision: 2 })!
  const server = { conversationId: 'project-conv', scopeRevision: 2, projectSnapshotId: 'snapshot-a' }
  const decision = resolveRetry({ snapshot: frozen, status: 'failed', server, context })
  assert.equal(decision.action, 'resend')
  if (decision.action !== 'resend') return
  const payload = buildRetryPayload(decision.snapshot)
  assert.equal(payload.scope_revision, 2)
  assert.equal('document_ids' in payload, false)
  assert.equal('project_snapshot_id' in payload, false)
  assert.equal(resolveRetry({ snapshot: frozen, status: 'failed', server,
    context: { ...context, projectSnapshotId: 'snapshot-b' } }).action, 'mismatch')
  assert.equal(resolveRetry({ snapshot: frozen, status: 'failed', context,
    server: { ...server, projectSnapshotId: 'snapshot-b' } }).action, 'mismatch')
})

test('project metadata binds the actual snapshot and permits a verified retry', () => {
  const draft = snapshot({ project: true, projectSnapshotId: 'snapshot-a', conversationId: null })!
  const payload = buildRetryPayload(draft)
  assert.equal(payload.project_snapshot_id, 'snapshot-a')
  assert.equal('document_ids' in payload, false)
  const frozen = bindRetrySnapshot(draft, { conversationId: 'project-conv', scopeRevision: 0, projectSnapshotId: 'snapshot-a' })!
  assert.equal(frozen.projectSnapshotId, 'snapshot-a')
  const decision = resolveRetry({ snapshot: frozen, status: 'cancelled',
    context: ragContext({ conversationId: 'project-conv', project: true, projectSnapshotId: 'snapshot-a' }),
    server: { conversationId: 'project-conv', scopeRevision: 0, projectSnapshotId: 'snapshot-a' } })
  assert.equal(decision.action, 'resend')
})

test('a project request whose metadata was lost binds once using the original turn snapshot', () => {
  const draft = snapshot({ project: true, projectSnapshotId: 'snapshot-a', conversationId: null })!
  const context = ragContext({ project: true, projectSnapshotId: 'snapshot-a' })
  const server = { conversationId: 'project-conv', scopeRevision: 0, projectSnapshotId: 'snapshot-a' }
  const decision = resolveRetry({ snapshot: draft, status: 'failed', context, server })
  assert.equal(decision.action, 'resend')
  if (decision.action !== 'resend') return
  assert.equal(decision.snapshot.projectSnapshotId, 'snapshot-a')
  assert.equal('project_snapshot_id' in buildRetryPayload(decision.snapshot), false)
  assert.equal(resolveRetry({ snapshot: draft, status: 'failed', context,
    server: { ...server, projectSnapshotId: null } }).action, 'mismatch')
})

// ---------------- 复制 ----------------

test('copying an answer uses the raw text and never touches HTML', async () => {
  const written: string[] = []
  const raw = '第一行\n<script>alert(1)</script>\n**不是 Markdown**'
  const ok = await copyPlainText(raw, { writeText: async (value) => { written.push(value) } })
  assert.equal(ok, true)
  assert.deepEqual(written, [raw], '必须原样复制，不做渲染或转义')
})

test('a clipboard failure is reported instead of silently succeeding', async () => {
  assert.equal(await copyPlainText('text', { writeText: async () => { throw new Error('denied') } }), false)
  assert.equal(await copyPlainText('text', null), false, '没有剪贴板 API 时也要给出失败')
  assert.equal(await copyPlainText('', { writeText: async () => undefined }), false)
})

/** 极简的假元素：只实现 closest/contains，用来验证委托选择器。 */
function fakeElement(tag: string, text: string | null, parent: fakeElement | null = null) {
  const element = {
    tagName: tag.toUpperCase(),
    textContent: text,
    parentElement: parent,
    closest(selector: string): fakeElement | null {
      const chain: fakeElement[] = []
      let node: fakeElement | null = element
      while (node) { chain.push(node); node = node.parentElement }
      if (selector !== 'pre > code') throw new Error(`unexpected selector ${selector}`)
      return chain.find((node) =>
        node.tagName === 'CODE' && node.parentElement?.tagName === 'PRE') ?? null
    }
  }
  return element
}

test('code copy reads pre > code textContent and ignores everything else', () => {
  const pre = fakeElement('pre', null)
  const code = fakeElement('code', 'const a = 1 < 2 && "<b>"', pre)
  assert.equal(codeTextFromTarget(code), 'const a = 1 < 2 && "<b>"')

  // 代码块里的 span / 按钮都能冒泡到 code
  const span = fakeElement('span', 'const a = 1 < 2 && "<b>"', code)
  assert.equal(codeTextFromTarget(span), 'const a = 1 < 2 && "<b>"')

  // 普通段落、行内 code、div 外层的按钮都不是代码块复制
  assert.equal(codeTextFromTarget(fakeElement('p', 'hello')), null)
  assert.equal(codeTextFromTarget(fakeElement('code', 'inline', fakeElement('p', null))), null)
  assert.equal(codeTextFromTarget(null), null)
  assert.equal(codeTextFromTarget({} as EventTarget), null)
})

test('an empty code block yields empty text rather than null so the caller can report failure', () => {
  const pre = fakeElement('pre', null)
  const code = fakeElement('code', '', pre)
  assert.equal(codeTextFromTarget(code), '')
})

// ---------------- 冻结 ----------------

test('the failure snapshot trims the question and keeps the original settings', () => {
  const frozen = snapshot()
  assert.ok(frozen)
  assert.equal(frozen!.question, '合成安全响应预案有哪些要点？')
  assert.equal(frozen!.mode, 'rag')
  assert.equal(frozen!.kbId, 'kb-1')
  assert.equal(frozen!.topK, 8)
  assert.deepEqual([...(frozen!.documentIds ?? [])], ['doc-1', 'doc-2'])
  assert.equal(Object.isFrozen(frozen), true, '冻结后不能再被后续 UI 改动污染')
})

test('a snapshot without a question cannot be frozen', () => {
  assert.equal(snapshot({ question: '   ' }), null)
  assert.equal(snapshot({ question: 'x'.repeat(4001) }), null)
})

// ---------------- 重试判定 ----------------

test('processing and completed originals are read-only and never resent', () => {
  assert.deepEqual(planRetry(snapshot(), 'processing', ragContext()), { action: 'readonly', reason: 'processing' })
  assert.deepEqual(planRetry(snapshot(), 'completed', ragContext()), { action: 'readonly', reason: 'completed' })
})

test('an unknown original status blocks the resend', () => {
  // GET 失败、403/404/410、断连都拿不到终态证据：不能据此再发一次模型请求
  assert.deepEqual(planRetry(snapshot(), 'unknown', ragContext()), { action: 'unknown' })
})

test('only a failed or cancelled original with an unchanged context can be resent', () => {
  // 冻结快照里是没有会话时的草稿范围，重发前当前草稿必须仍是这两篇文档
  const context = ragContext({ documentScope: scope('doc-1', 'doc-2') })
  const failed = planRetry(snapshot(), 'failed', context)
  assert.equal(failed.action, 'resend')
  const cancelled = planRetry(snapshot(), 'cancelled', context)
  assert.equal(cancelled.action, 'resend')
})

test('a missing payload snapshot asks the user to copy the question instead of fabricating a replay', () => {
  assert.deepEqual(planRetry(null, 'failed', ragContext()), { action: 'unavailable' })
  assert.deepEqual(planRetry(null, 'cancelled', ragContext()), { action: 'unavailable' })
})

test('changed mode, knowledge base or conversation is a mismatch, not a silent scope change', () => {
  assert.equal(planRetry(snapshot(), 'failed', ragContext({ mode: 'chat' })).action, 'mismatch')
  assert.equal(planRetry(snapshot(), 'failed', ragContext({ kbId: 'kb-2' })).action, 'mismatch')
  assert.equal(planRetry(snapshot(), 'failed', ragContext({ conversationId: 'conv-2' })).action, 'mismatch')
})

test('a changed draft document scope is a mismatch', () => {
  const base = snapshot({ documentIds: ['doc-1', 'doc-2'] })
  assert.equal(planRetry(base, 'failed', ragContext({ documentScope: scope('doc-1', 'doc-2') })).action, 'resend')
  assert.equal(planRetry(base, 'failed', ragContext({ documentScope: scope('doc-1') })).action, 'mismatch')
  assert.equal(planRetry(base, 'failed', ragContext({ documentScope: scope('doc-1', 'doc-3') })).action, 'mismatch')
  // 顺序无关
  assert.equal(planRetry(base, 'failed', ragContext({ documentScope: scope('doc-2', 'doc-1') })).action, 'resend')
})

test('a changed scope revision on an existing conversation is a mismatch', () => {
  const base = snapshot({ conversationId: 'conv-1', documentIds: null, scopeRevision: 3 })
  const context = ragContext({ conversationId: 'conv-1', scopeRevision: 3, documentScope: scope('doc-9') })
  assert.equal(planRetry(base, 'failed', context).action, 'resend')
  assert.equal(planRetry(base, 'failed', { ...context, scopeRevision: 4 }).action, 'mismatch')
})

test('free chat retries ignore knowledge base and scope entirely', () => {
  const base = freezeRetrySnapshot({ question: '你好', mode: 'chat', kbId: null, topK: null })
  assert.ok(base)
  const context: RetryContext = { mode: 'chat', kbId: null, conversationId: null, scopeRevision: 0, documentScope: [] }
  assert.equal(planRetry(base, 'failed', context).action, 'resend')
})

test('the retry payload reuses frozen values and omits an untouched top_k', () => {
  const payload = buildRetryPayload(snapshot({
    conversationId: 'conv-1', documentIds: null, scopeRevision: 3
  })!)
  assert.equal(payload.question, '合成安全响应预案有哪些要点？')
  assert.equal(payload.kb_id, 'kb-1')
  assert.equal(payload.top_k, 8)
  assert.equal(payload.conversation_id, 'conv-1')
  assert.equal(payload.mode, 'rag')
  // 已有会话必须带冻结的 revision：缺省时服务端会按范围冲突 409
  assert.equal(payload.scope_revision, 3)
  // 已有会话的范围走 scope_revision，不重发草稿 document_ids
  assert.equal('document_ids' in payload, false)
  // request_id 由调用方新生成，快照里不能有它
  assert.equal('request_id' in payload, false)
})

test('a draft resend sends document_ids and never scope_revision', () => {
  // 新会话没有 conversation_id：服务端只接受草稿 document_ids，不接受 revision。
  const payload = buildRetryPayload(snapshot({ conversationId: null, scopeRevision: 2 })!)
  assert.equal(payload.conversation_id, null)
  assert.equal('scope_revision' in payload, false)
  assert.deepEqual(payload.document_ids, ['doc-1', 'doc-2'])
})

test('server metadata binds a new conversation so a failed retry can resend', () => {
  // 发送前只能记 null；但 onMeta 之后界面已经在 conv-9 里，未绑定的快照必然 mismatch。
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })
  assert.equal(planRetry(draft, 'failed', ragContext({ conversationId: 'conv-9' })).action, 'mismatch')
  const bound = bindRetrySnapshot(draft, { conversationId: 'conv-9', scopeRevision: 0 })
  assert.ok(bound)
  assert.equal(bound!.conversationId, 'conv-9')
  assert.equal(bound!.scopeRevision, 0)
  assert.equal(bound!.documentIds, null, '会话已存在，重发不能再带草稿 document_ids')
  assert.deepEqual(planRetry(bound, 'failed', ragContext({ conversationId: 'conv-9' })), {
    action: 'resend', snapshot: bound
  })
  assert.equal(buildRetryPayload(bound!).conversation_id, 'conv-9')
  assert.equal(buildRetryPayload(bound!).scope_revision, 0)
})

test('binding never invents a revision or rewrites an existing conversation', () => {
  // 服务端没给版本：保持 null → 保守 mismatch，不猜 0
  const noRevision = bindRetrySnapshot(snapshot({ conversationId: null, scopeRevision: null }),
    { conversationId: 'conv-9' })
  assert.equal(noRevision!.scopeRevision, null)
  assert.equal(planRetry(noRevision, 'failed', ragContext({ conversationId: 'conv-9' })).action, 'mismatch')
  // 已有会话不会被 meta 里的"当前版本"改写（那等于悄悄换了重发依据）
  const existing = snapshot({ conversationId: 'conv-1', documentIds: null, scopeRevision: 3 })
  assert.equal(bindRetrySnapshot(existing, { conversationId: 'conv-2', scopeRevision: 9 }), existing)
  // 没有快照时也不会凭空造一个
  assert.equal(bindRetrySnapshot(null, { conversationId: 'conv-9', scopeRevision: 1 }), null)
})

// ---------------- 服务端状态的绑定校验 ----------------

test('an unchanged first failure binds the real conversation and can resend', () => {
  // 新会话首字前失败、meta 没收到：界面与会话都还没有 ID。
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })!
  const plan = planRetryBinding(
    draft,
    { conversationId: 'conv-9', documentScope: scope('doc-1'), scopeRevision: 1 },
    ragContext({ documentScope: scope('doc-1') })
  )
  assert.equal(plan.action, 'resend')
  if (plan.action !== 'resend') return
  assert.equal(plan.snapshot.conversationId, 'conv-9')
  assert.equal(plan.snapshot.scopeRevision, 1)
  assert.equal(plan.snapshot.documentIds, null, '会话已存在，重发不再带草稿 document_ids')
  assert.equal(plan.snapshot.documentScope, null)
  assert.deepEqual(plan.scope, { items: scope('doc-1'), revision: 1 })
  // 校验后的快照可以直接重发
  assert.deepEqual(planRetry(plan.snapshot, 'failed', ragContext({
    conversationId: 'conv-9', scopeRevision: 1, documentScope: scope('doc-1')
  })), { action: 'resend', snapshot: plan.snapshot })
})

test('a draft edited after the failure is never overwritten and never resent', () => {
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })!
  // 用户失败后把草稿改成 doc-2：GET 返回的仍是服务端的 doc-1，不能被采纳后静默重发。
  const plan = planRetryBinding(
    draft,
    { conversationId: 'conv-9', documentScope: scope('doc-1'), scopeRevision: 1 },
    ragContext({ documentScope: scope('doc-2') })
  )
  assert.equal(plan.action, 'mismatch')
})

test('a draft scope edited away and back again is rejected by the revision', () => {
  // 失败时限定 doc-1（服务端记得 revision=1）；用户随后清空→再选回 doc-1，
  // 文档集合看起来一样，但服务端 revision 已经是 3，不是这次失败请求的依据。
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })!
  assert.equal(planRetryBinding(
    draft,
    { conversationId: 'conv-9', documentScope: scope('doc-1'), scopeRevision: 3 },
    ragContext({ documentScope: scope('doc-1') })
  ).action, 'mismatch')
  // 全库草稿（revision 0）同理：改走再改回全库也会变成 >0
  const wholeKb = snapshot({ conversationId: null, documentIds: null, documentScope: null, scopeRevision: null })!
  assert.equal(planRetryBinding(
    wholeKb,
    { conversationId: 'conv-9', documentScope: [], scopeRevision: 0 },
    ragContext()
  ).action, 'resend')
  assert.equal(planRetryBinding(
    wholeKb,
    { conversationId: 'conv-9', documentScope: [], scopeRevision: 2 },
    ragContext()
  ).action, 'mismatch')
})

test('the same document rebuilt with a new index_version is not the same evidence', () => {
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })!
  // UI 仍是 doc-1，但重建后 index_version 变成 2
  const rebuiltUi = ragContext({ documentScope: [{ document_id: 'doc-1', filename: 'doc-1.pdf', index_version: 2 }] })
  assert.equal(planRetryBinding(
    draft,
    { conversationId: 'conv-9', documentScope: scope('doc-1'), scopeRevision: 1 },
    rebuiltUi
  ).action, 'mismatch')
  // 服务端绑定到了新版本（index_version=2），冻结的是旧版本
  assert.equal(planRetryBinding(
    draft,
    { conversationId: 'conv-9', documentScope: [{ document_id: 'doc-1', filename: 'doc-1.pdf', index_version: 2 }], scopeRevision: 1 },
    ragContext({ documentScope: scope('doc-1') })
  ).action, 'mismatch')
})

test('a scope changed in another tab blocks the retry instead of adopting the new revision', () => {
  const bound = snapshot({ conversationId: 'conv-1', documentIds: null, scopeRevision: 3 })!
  const context = ragContext({ conversationId: 'conv-1', scopeRevision: 3, documentScope: scope('doc-9') })
  // 界面没变，但服务端当前已经是 revision 4（别的页签改过范围）
  assert.equal(planRetryBinding(bound, {
    conversationId: 'conv-1', documentScope: scope('doc-2'), scopeRevision: 4
  }, context).action, 'mismatch')
  // 服务端当前范围与冻结的一致才允许重发
  assert.equal(planRetryBinding(bound, {
    conversationId: 'conv-1', documentScope: scope('doc-9'), scopeRevision: 3
  }, context).action, 'resend')
  // 服务端没给版本就是证据不足
  assert.equal(planRetryBinding(bound, {
    conversationId: 'conv-1', documentScope: scope('doc-9'), scopeRevision: null
  }, context).action, 'mismatch')
})

test('binding rejects a different conversation or an empty server identity', () => {
  const bound = snapshot({ conversationId: 'conv-1', documentIds: null, scopeRevision: 2 })!
  const context = ragContext({ conversationId: 'conv-1', scopeRevision: 2 })
  assert.equal(planRetryBinding(bound, { conversationId: 'conv-2', scopeRevision: 2 }, context).action, 'mismatch')
  assert.equal(planRetryBinding(bound, { conversationId: '', scopeRevision: 2 }, context).action, 'mismatch')
  assert.equal(planRetryBinding(bound, {}, context).action, 'mismatch')
  // 服务端当前范围缺少 document_scope 时，新会话无法证明依据一致
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })!
  assert.equal(planRetryBinding(draft, { conversationId: 'conv-9' }, ragContext()).action, 'mismatch')
})

test('binding follows the frozen mode and knowledge base, not the current UI', () => {
  const bound = snapshot({ conversationId: 'conv-1', documentIds: null, scopeRevision: 1 })!
  assert.equal(planRetryBinding(bound, { conversationId: 'conv-1', scopeRevision: 1 },
    ragContext({ conversationId: 'conv-1', scopeRevision: 1, mode: 'chat' })).action, 'mismatch')
  assert.equal(planRetryBinding(bound, { conversationId: 'conv-1', scopeRevision: 1 },
    ragContext({ conversationId: 'conv-1', scopeRevision: 1, kbId: 'kb-2' })).action, 'mismatch')
})

test('a live scope array mutated while awaiting the state is still detected via the frozen copy', () => {
  // 真实界面传的是同一个响应式数组引用：等待 GET 期间用户往草稿里加了文档，
  // 如果守卫直接持有该引用，"之前"和"现在"会同时变，上下文守卫就失效了。
  const live: DocumentScopeItem[] = scope('doc-1')
  const context = ragContext({ documentScope: live })
  const before = freezeRetryContext(context)

  // 对照：不拷贝直接持有同一引用时，等待期间的原地修改会让"之前"跟着一起变，
  // 守卫失效——这正是 freezeRetryContext 存在的理由。
  const unfrozen = { ...context }
  assert.equal(sameRetryContext(unfrozen, { ...context, documentScope: live }), true,
    '不拷贝时守卫会被原地修改绕过（反例）')

  // 等待中的原地修改（不是替换数组）
  live.push({ document_id: 'doc-2', filename: 'doc-2.pdf', index_version: 1 })
  assert.equal(sameRetryContext(before, { ...context, documentScope: live }), false,
    '原地 push 也必须被识别为上下文变化')

  // 原地改字段（文档重新索引 → index_version 变化）
  const other: DocumentScopeItem[] = scope('doc-1')
  const snapshotBefore = freezeRetryContext(ragContext({ documentScope: other }))
  other[0]!.index_version = 2
  assert.equal(sameRetryContext(snapshotBefore, ragContext({ documentScope: other })), false,
    '原地改 index_version 也必须被识别')

  // 没变就仍然允许：冻结的是副本，不会自己漂移
  const stable = scope('doc-1')
  const frozenStable = freezeRetryContext(ragContext({ documentScope: stable }))
  stable.push({ document_id: 'doc-3', filename: 'doc-3.pdf', index_version: 1 })
  stable.pop()
  assert.equal(sameRetryContext(frozenStable, ragContext({ documentScope: stable })), true)
})

test('a retry context that moved while awaiting the request state is not the same context', () => {
  const base = ragContext({ conversationId: 'conv-1', scopeRevision: 2, documentScope: scope('doc-1') })
  assert.equal(sameRetryContext(base, { ...base }), true)
  // 等待期间新建会话
  assert.equal(sameRetryContext(base, { ...base, conversationId: null }), false)
  // 切换模式
  assert.equal(sameRetryContext(base, { ...base, mode: 'chat' }), false)
  // 切换知识库
  assert.equal(sameRetryContext(base, { ...base, kbId: 'kb-2' }), false)
  // 范围版本或文档集合变化
  assert.equal(sameRetryContext(base, { ...base, scopeRevision: 3 }), false)
  assert.equal(sameRetryContext(base, { ...base, documentScope: scope('doc-2') }), false)
  // 同一批文档但顺序不同不算变化
  const two = ragContext({ documentScope: scope('doc-1', 'doc-2') })
  assert.equal(sameRetryContext(two, { ...two, documentScope: scope('doc-2', 'doc-1') }), true)
})

// ---------------- 完整链路：发送失败 → 查状态 → 重试决策 ----------------

test('first meta lost plus a failed GET still retries the same conversation exactly once', () => {
  // 发送时新会话：快照 conversationId=null，UI 也还没有会话（meta 从未到达）。
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })!
  const context = ragContext({ documentScope: scope('doc-1') })
  const decision = resolveRetry({
    snapshot: draft,
    status: 'failed',
    server: { conversationId: 'conv-9', documentScope: scope('doc-1'), scopeRevision: 1 },
    context
  })
  assert.equal(decision.action, 'resend')
  if (decision.action !== 'resend') return
  // 补的是服务端真实会话，不是新建一个；范围由 revision 决定
  assert.equal(decision.snapshot.conversationId, 'conv-9')
  assert.equal(decision.snapshot.scopeRevision, 1)
  assert.deepEqual(buildRetryPayload(decision.snapshot), {
    question: '合成安全响应预案有哪些要点？',
    conversation_id: 'conv-9',
    mode: 'rag',
    kb_id: 'kb-1',
    top_k: 8,
    scope_revision: 1
  })
})

test('an unknown original status sends nothing even with a whole server state', () => {
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })!
  const decision = resolveRetry({
    snapshot: draft,
    status: 'unknown',
    server: { conversationId: 'conv-9', documentScope: scope('doc-1'), scopeRevision: 1 },
    context: ragContext({ documentScope: scope('doc-1') })
  })
  assert.deepEqual(decision, { action: 'unknown' })
})

test('a draft changed while the state was being read is rejected by the full chain', () => {
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })!
  // 用户失败后手动把草稿改成 doc-2；GET 仍然返回服务端的 doc-1。
  assert.deepEqual(resolveRetry({
    snapshot: draft,
    status: 'failed',
    server: { conversationId: 'conv-9', documentScope: scope('doc-1'), scopeRevision: 1 },
    context: ragContext({ documentScope: scope('doc-2') })
  }), { action: 'mismatch' })
  // UI 已经打开了另一个会话，也不能把这次失败塞回新会话
  assert.deepEqual(resolveRetry({
    snapshot: draft,
    status: 'failed',
    server: { conversationId: 'conv-9', documentScope: scope('doc-1'), scopeRevision: 1 },
    context: ragContext({ conversationId: 'conv-other', documentScope: scope('doc-1') })
  }), { action: 'mismatch' })
})

test('a failed GET with no server identity cannot fabricate a conversation', () => {
  const draft = snapshot({ conversationId: null, documentIds: ['doc-1'], scopeRevision: null })!
  assert.deepEqual(resolveRetry({
    snapshot: draft,
    status: 'failed',
    server: null,
    context: ragContext({ documentScope: scope('doc-1') })
  }), { action: 'mismatch' })
})

test('a frozen top_k of null means "let the server decide"', () => {
  const payload = buildRetryPayload(freezeRetrySnapshot({
    question: '问题', mode: 'rag', kbId: 'kb-1', topK: null
  })!)
  assert.equal('top_k' in payload, false)
})

test('free chat retry payload carries no knowledge base or retrieval fields', () => {
  const payload = buildRetryPayload(freezeRetrySnapshot({
    question: '你好', mode: 'chat', kbId: 'kb-1', topK: 6
  })!)
  assert.equal(payload.mode, 'chat')
  assert.equal('kb_id' in payload, false)
  assert.equal('top_k' in payload, false)
  assert.equal('document_ids' in payload, false)
})
