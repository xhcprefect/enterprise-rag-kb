import { createLatestGuard } from './usage.ts'

interface ValueRef<T> {
  value: T
}

interface AdminListBindings<T> {
  items: ValueRef<T[]>
  total: ValueRef<number>
  loading: ValueRef<boolean>
  error: ValueRef<boolean>
}

/** 两页共用请求生命周期；不依赖取消是否被适配器执行来判断写权。 */
export function createAdminListLoader<T, Params extends object>(
  state: AdminListBindings<T>,
  fetchPage: (params: Params, signal: AbortSignal) => Promise<{ items: T[]; total: number }>
) {
  const guard = createLatestGuard()
  let controller: AbortController | null = null

  async function load(params: Params): Promise<void> {
    if (guard.disposed) return
    const snapshot = { ...params }
    const token = guard.begin()
    controller?.abort()
    const current = new AbortController()
    controller = current
    const canWrite = () => guard.isCurrent(token) && !current.signal.aborted

    state.loading.value = true
    state.error.value = false
    state.items.value = []
    state.total.value = 0
    try {
      const data = await fetchPage(snapshot, current.signal)
      if (!canWrite()) return
      state.items.value = data.items
      state.total.value = data.total
    } catch {
      // 拦截器负责全局提示；页面只展示安全、可随语言切换的固定错误。
      if (canWrite()) state.error.value = true
    } finally {
      if (canWrite()) {
        state.loading.value = false
        controller = null
      }
    }
  }

  function dispose() {
    guard.dispose()
    controller?.abort()
    controller = null
  }

  return { load, dispose }
}
