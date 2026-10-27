import assert from 'node:assert/strict'
import test from 'node:test'
import { resolveTopK } from '../src/utils/retrievalTopK.ts'

test('an untouched slider omits top_k so the server default applies', () => {
  // 这是本次改动的核心：不省略，.env 里的 RETRIEVAL_TOP_K 就永远不生效。
  assert.equal(resolveTopK({ mode: 'rag', topK: 4, defaultTopK: 4 }), undefined)
})

test('moving the slider sends an explicit override', () => {
  assert.equal(resolveTopK({ mode: 'rag', topK: 8, defaultTopK: 4 }), 8)
  assert.equal(resolveTopK({ mode: 'rag', topK: 1, defaultTopK: 6 }), 1)
})

test('plain chat never sends top_k', () => {
  assert.equal(resolveTopK({ mode: 'chat', topK: 8, defaultTopK: 4 }), undefined)
  assert.equal(resolveTopK({ mode: 'chat', topK: 4, defaultTopK: null }), undefined)
})

test('without a server default the slider value must still be sent', () => {
  // 老后端没有 /chat/retrieval-config：此时省略就等于放弃用户的选择，
  // 只能退回"总是显式传"的旧行为。
  assert.equal(resolveTopK({ mode: 'rag', topK: 9, defaultTopK: null }), 9)
})

test('the omitted field really disappears from the JSON body', () => {
  // 直接验证序列化约定：undefined 会让整个键消失，而不是变成 null。
  const body = JSON.stringify({ question: 'q', top_k: resolveTopK({ mode: 'rag', topK: 4, defaultTopK: 4 }) })
  assert.ok(!body.includes('top_k'), body)
  assert.ok(JSON.stringify({ top_k: resolveTopK({ mode: 'rag', topK: 9, defaultTopK: 4 }) }).includes('"top_k":9'))
})
