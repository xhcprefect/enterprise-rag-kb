/**
 * 分片并发组的 Node 测试。
 *
 * 关键回归：一片失败时同组其它 worker 立即停止，且函数返回前所有在途请求
 * 都已 settle；否则调用方释放占用后用户立刻重试，会在同一 key 上叠出第二组
 * worker，旧组的进度/续传回执还会写进新任务。
 */
import assert from 'node:assert/strict'
import test from 'node:test'
import { runWorkerGroup } from '../src/utils/documentChunks.ts'

function deferred<T = void>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function abortError() {
  return new DOMException('aborted', 'AbortError')
}

test('a failing chunk aborts its siblings and waits for them to settle', async () => {
  const started: number[] = []
  const settled: number[] = []
  const gates = new Map<number, ReturnType<typeof deferred>>()
  for (const index of [0, 1, 2]) gates.set(index, deferred())

  const running = runWorkerGroup(
    [0, 1, 2],
    async (index, signal) => {
      started.push(index)
      await new Promise<void>((resolve, reject) => {
        signal.addEventListener('abort', () => reject(abortError()), { once: true })
        gates.get(index)!.promise.then(() => {
          settled.push(index)
          resolve()
        })
      })
      if (index === 0) throw new Error('chunk 0 exhausted retries')
    },
    { concurrency: 3 }
  )

  // 三个 worker 都已启动
  await Promise.resolve()
  assert.deepEqual(started.sort(), [0, 1, 2])

  // 分片 0 失败之前，兄弟分片仍然在途
  gates.get(0)!.resolve()
  await assert.rejects(running, /chunk 0 exhausted retries/)
  // 函数返回时兄弟分片已经因 abort 结束，不会遗留后台 worker
  assert.equal(settled.includes(0), true)
  assert.equal(settled.includes(1), false)
  assert.equal(settled.includes(2), false)
})

test('no worker keeps running after the group reports failure', async () => {
  let active = 0
  let maxActive = 0
  let pendingAfterReturn = 0
  let releaseSlow!: () => void
  const slow = new Promise<void>((resolve) => {
    releaseSlow = resolve
  })

  const running = runWorkerGroup(
    [0, 1, 2, 3],
    async (index, signal) => {
      active += 1
      maxActive = Math.max(maxActive, active)
      try {
        if (index === 0) {
          // 立刻失败
          throw new Error('boom')
        }
        // 兄弟分片：只有在被 abort 时才结束
        await new Promise<void>((resolve, reject) => {
          signal.addEventListener('abort', () => reject(abortError()), { once: true })
          slow.then(resolve)
        })
      } finally {
        active -= 1
        if (signal.aborted) pendingAfterReturn += 1
      }
    },
    { concurrency: 2 }
  )

  await assert.rejects(running, /boom/)
  assert.equal(active, 0, 'no in-flight worker when the group returns')
  assert.equal(maxActive <= 2, true)
  // 兄弟分片是被 abort 收掉的（而不是自己跑完），说明同组确实被中止
  assert.equal(pendingAfterReturn > 0, true)
  releaseSlow()
})

test('outer cancellation stops the group and reports an abort', async () => {
  const outer = new AbortController()
  const seen: number[] = []
  const running = runWorkerGroup(
    [0, 1, 2],
    async (index, signal) => {
      seen.push(index)
      await new Promise<void>((resolve, reject) => {
        signal.addEventListener('abort', () => reject(abortError()), { once: true })
        if (index === 2) resolve()
      })
    },
    { concurrency: 1, signal: outer.signal }
  )

  await Promise.resolve()
  outer.abort()
  await assert.rejects(running, (err: unknown) => (err as Error).name === 'AbortError')
  assert.equal(seen.length, 1)
})

test('an already-cancelled signal never starts a worker', async () => {
  const outer = new AbortController()
  outer.abort()
  let started = false
  await assert.rejects(
    runWorkerGroup([1], async () => {
      started = true
    }, { concurrency: 1, signal: outer.signal }),
    (err: unknown) => (err as Error).name === 'AbortError'
  )
  assert.equal(started, false)
})

test('empty work resolves immediately', async () => {
  await runWorkerGroup([], async () => {
    throw new Error('never')
  }, { concurrency: 3 })
})

test('a genuine failure is reported instead of a sibling abort', async () => {
  // 失败发生在等兄弟 settle 之前：抛出原始错误，便于展示服务端消息
  await assert.rejects(
    runWorkerGroup(
      [0, 1],
      async (index) => {
        if (index === 0) throw new Error('server said no')
        await new Promise((resolve) => setTimeout(resolve, 5))
      },
      { concurrency: 2 }
    ),
    /server said no/
  )
})
