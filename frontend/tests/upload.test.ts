import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'
import { computed, nextTick, reactive, ref } from 'vue'
import { hashFile } from '../src/utils/upload.ts'

test('file hash covers content across multiple bounded read blocks', async () => {
  const bytes = new Uint8Array(4 * 1024 * 1024 + 7).fill(65)
  bytes[bytes.length - 1] = 66
  assert.equal(await hashFile(new Blob([bytes])), createHash('sha256').update(bytes).digest('hex'))
})

test('same name/size is not enough for matching file hashes', async () => {
  assert.notEqual(await hashFile(new Blob(['abcdefgh'])), await hashFile(new Blob(['ABCDEFGH'])))
})

test('file hashing is cancellable', async () => {
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(hashFile(new Blob(['x']), controller.signal), { name: 'AbortError' })
})

test('reactive upload task updates computed progress text', async () => {
  const task = reactive({ percent: 0 })
  const current = ref(task)
  const label = computed(() => `${current.value.percent}%`)
  assert.equal(label.value, '0%')
  task.percent = 50
  await nextTick()
  assert.equal(label.value, '50%')
})
