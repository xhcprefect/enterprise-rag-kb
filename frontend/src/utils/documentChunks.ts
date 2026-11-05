/**
 * 分片并发组的纯逻辑。
 *
 * 一组 worker 共享一个内部 AbortController：任一分片重试耗尽后立刻中止
 * 同组其它分片，并等待所有在途请求 settle 才把失败抛给调用方。这样调用方
 * 释放"当前上传占用"时，旧组不会还有活着的请求在写进度或续传记录。
 */

export interface WorkerGroupOptions {
  concurrency: number
  /** 外部取消（暂停 / 取消 / 离开页面） */
  signal?: AbortSignal
}

function abortError(): DOMException {
  return new DOMException('aborted', 'AbortError')
}

function isAbortLike(err: unknown): boolean {
  const value = err as { name?: string; code?: string } | null | undefined
  return value?.name === 'AbortError' || value?.code === 'ERR_CANCELED' || value?.name === 'CanceledError'
}

/**
 * 以给定并发度跑完所有条目。
 *
 * - 任一条目失败：立即 abort 内部 signal，等所有 worker settle 后抛出该失败；
 * - 外部 signal 中止：同样停止并等待 settle，最后抛 AbortError；
 * - 正常完成：全部 settle 后返回。
 *
 * 无论哪条路径，函数返回时都不会有遗留 worker 在跑。
 */
export async function runWorkerGroup<T>(
  items: readonly T[],
  worker: (item: T, signal: AbortSignal) => Promise<void>,
  options: WorkerGroupOptions
): Promise<void> {
  const { concurrency, signal } = options
  if (!items.length) return
  if (signal?.aborted) throw abortError()

  const group = new AbortController()
  const onOuterAbort = () => group.abort()
  if (signal) {
    signal.addEventListener('abort', onOuterAbort, { once: true })
    // 监听器注册前可能已经被中止
    if (signal.aborted) group.abort()
  }

  let cursor = 0
  const fail = (err: unknown) => {
    group.abort()
    return err
  }

  const count = Math.max(1, Math.min(concurrency, items.length))
  const workers = Array.from({ length: count }, async () => {
    while (!group.signal.aborted && cursor < items.length) {
      const item = items[cursor]
      cursor += 1
      try {
        await worker(item, group.signal)
      } catch (err) {
        throw fail(err)
      }
    }
  })

  try {
    const settled = await Promise.allSettled(workers)
    const rejected = settled.filter(
      (outcome): outcome is PromiseRejectedResult => outcome.status === 'rejected'
    )
    if (!rejected.length) {
      if (group.signal.aborted) throw abortError()
      return
    }
    // 优先汇报真正的失败原因（而不是被连带中止的兄弟分片），
    // 全部都是中止时抛 AbortError，交给调用方按取消处理。
    const failure = rejected.find((outcome) => !isAbortLike(outcome.reason))
    throw (failure ?? rejected[0]).reason
  } finally {
    signal?.removeEventListener('abort', onOuterAbort)
  }
}
