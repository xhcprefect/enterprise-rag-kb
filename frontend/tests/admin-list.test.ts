import assert from 'node:assert/strict'
import test from 'node:test'
import { createAdminListLoader } from '../src/utils/adminList.ts'

interface Item { id: string }
interface Params {
  page: number
  page_size: number
  keyword?: string
  action?: string
  username?: string
}
interface Result { items: Item[]; total: number }

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

function result(id: string, total = 37): Result {
  return { items: [{ id }], total }
}

function harness() {
  const writes: string[] = []
  function tracked<T>(name: string, initial: T) {
    let value = initial
    return {
      get value() { return value },
      set value(next: T) { writes.push(name); value = next }
    }
  }
  const state = {
    items: tracked<Item[]>('items', [{ id: 'previous-filter' }]),
    total: tracked('total', 80),
    loading: tracked('loading', false),
    error: tracked('error', false)
  }
  const calls: Array<{ params: Params; signal: AbortSignal; response: ReturnType<typeof deferred<Result>> }> = []
  const loader = createAdminListLoader(state, (params: Params, signal) => {
    const response = deferred<Result>()
    calls.push({ params, signal, response })
    // 故意忽略 abort：必须靠 guard 阻止已完成/不支持取消的适配器写回。
    return response.promise
  })
  const view = () => ({
    items: state.items.value,
    total: state.total.value,
    loading: state.loading.value,
    error: state.error.value
  })
  const load = (keyword: string) => loader.load({ page: 1, page_size: 20, keyword })
  return { state, writes, calls, loader, load, view }
}

const emptyLoading = { items: [], total: 0, loading: true, error: false }

test('starting a request clears old items, total and error before waiting for the API', async () => {
  const h = harness()
  h.state.error.value = true
  const pending = h.load('A')
  assert.deepEqual(h.view(), emptyLoading)
  assert.equal(h.calls[0].signal.aborted, false)
  h.calls[0].response.resolve(result('A', 3))
  await pending
  assert.deepEqual(h.view(), { ...result('A', 3), loading: false, error: false })
})

for (const outcome of ['success', 'failure'] as const) {
  test(`old ${outcome} and finally cannot end loading or write while B is still pending`, async () => {
    const h = harness()
    const a = h.load('A')
    const b = h.load('B')
    assert.equal(h.calls[0].signal.aborted, true)
    assert.equal(h.calls[1].signal.aborted, false)
    const writesBefore = h.writes.length
    if (outcome === 'success') h.calls[0].response.resolve(result('A'))
    else h.calls[0].response.reject(new Error('old request failed'))
    await a
    assert.equal(h.writes.length, writesBefore, '旧 success/catch/finally 全部无写权')
    assert.deepEqual(h.view(), emptyLoading)
    h.calls[1].response.resolve(result('B', 52))
    await b
    assert.deepEqual(h.view(), { ...result('B', 52), loading: false, error: false })
  })

  test(`late A ${outcome} cannot overwrite fast B items, total or error`, async () => {
    const h = harness()
    const a = h.load('A')
    const b = h.load('B')
    h.calls[1].response.resolve(result('B', 21))
    await b
    const writesBefore = h.writes.length
    if (outcome === 'success') h.calls[0].response.resolve(result('A', 99))
    else h.calls[0].response.reject(new Error('late A failed'))
    await a
    assert.equal(h.writes.length, writesBefore)
    assert.deepEqual(h.view(), { ...result('B', 21), loading: false, error: false })
  })
}

test('latest failure shows an empty failed list and a real retry restores the new data', async () => {
  const h = harness()
  const a = h.load('A')
  h.calls[0].response.resolve(result('A', 72))
  await a
  const b = h.load('B')
  assert.deepEqual(h.view(), emptyLoading)
  h.calls[1].response.reject(new Error('<unsafe server message>'))
  await assert.doesNotReject(b)
  assert.deepEqual(h.view(), { items: [], total: 0, loading: false, error: true })
  const retry = h.load('B')
  assert.deepEqual(h.view(), emptyLoading)
  assert.deepEqual(h.calls[2].params, h.calls[1].params)
  assert.notEqual(h.calls[2].signal, h.calls[1].signal)
  h.calls[2].response.resolve(result('B', 5))
  await retry
  assert.deepEqual(h.view(), { ...result('B', 5), loading: false, error: false })
})

test('late A success cannot resurrect rows after the latest B failure', async () => {
  const h = harness()
  const a = h.load('A')
  const b = h.load('B')
  h.calls[1].response.reject(new Error('B failed'))
  await b
  const writesBefore = h.writes.length
  h.calls[0].response.resolve(result('A', 91))
  await a
  assert.equal(h.writes.length, writesBefore)
  assert.deepEqual(h.view(), { items: [], total: 0, loading: false, error: true })
})

test('old failure during retry cannot restore the previous error or finish retry loading', async () => {
  const h = harness()
  const a = h.load('A')
  const b = h.load('B')
  h.calls[1].response.reject(new Error('B failed'))
  await b
  const retry = h.load('B')
  const writesBefore = h.writes.length
  h.calls[0].response.reject(new Error('late A failed'))
  await a
  assert.equal(h.writes.length, writesBefore)
  assert.deepEqual(h.view(), emptyLoading)
  h.calls[2].response.resolve(result('B'))
  await retry
  assert.deepEqual(h.view(), { ...result('B'), loading: false, error: false })
})

for (const outcome of ['success', 'failure'] as const) {
  test(`dispose cancels the request and forbids all late ${outcome}/finally and future load writes`, async () => {
    const h = harness()
    const pending = h.load('A')
    const before = h.view()
    const writesBefore = h.writes.length
    h.loader.dispose()
    h.loader.dispose()
    assert.equal(h.calls[0].signal.aborted, true)
    if (outcome === 'success') h.calls[0].response.resolve(result('A'))
    else h.calls[0].response.reject(new Error('failed after unmount'))
    await assert.doesNotReject(pending)
    await h.load('after-unmount')
    assert.equal(h.calls.length, 1)
    assert.equal(h.writes.length, writesBefore, 'dispose 本身也不写页面状态')
    assert.deepEqual(h.view(), before)

    const reopened = harness()
    const fresh = reopened.load('new-instance')
    reopened.calls[0].response.resolve(result('new-instance', 1))
    await fresh
    assert.deepEqual(reopened.view(), { ...result('new-instance', 1), loading: false, error: false })
  })
}

test('abort rejection is handled when superseding a request or disposing the page', async () => {
  const state = { items: { value: [] as Item[] }, total: { value: 0 }, loading: { value: false }, error: { value: false } }
  const loader = createAdminListLoader(state, (_params: Params, signal) => new Promise<Result>((_ok, reject) => {
    signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
  }))
  const a = loader.load({ page: 1, page_size: 20 })
  const b = loader.load({ page: 2, page_size: 20 })
  await assert.doesNotReject(a)
  assert.equal(state.loading.value, true)
  assert.equal(state.error.value, false)
  loader.dispose()
  await assert.doesNotReject(b)
  assert.equal(state.loading.value, true, '卸载后的 finally 无权结束 loading')
  assert.equal(state.error.value, false)
})

test('request params are captured before the await and cannot drift with mutable filters', async () => {
  const h = harness()
  const params: Params = { page: 3, page_size: 50, keyword: 'A', action: 'login_success', username: 'actor-A' }
  const pending = h.loader.load(params)
  Object.assign(params, { page: 1, page_size: 100, keyword: 'B', action: 'logout', username: 'actor-B' })
  assert.deepEqual(h.calls[0].params, { page: 3, page_size: 50, keyword: 'A', action: 'login_success', username: 'actor-A' })
  h.calls[0].response.resolve(result('captured', 103))
  await pending
  assert.deepEqual(h.view(), { ...result('captured', 103), loading: false, error: false })
})

test('synchronous request failure is handled and an empty successful response recovers', async () => {
  const state = { items: { value: [{ id: 'old' }] }, total: { value: 9 }, loading: { value: false }, error: { value: false } }
  let fail = true
  const loader = createAdminListLoader(state, (_params: Params) => {
    if (fail) throw new Error('synchronous adapter failure')
    return Promise.resolve({ items: [], total: 0 })
  })
  await assert.doesNotReject(loader.load({ page: 1, page_size: 20 }))
  assert.equal(state.error.value, true)
  assert.equal(state.loading.value, false)
  assert.deepEqual(state.items.value, [])
  assert.equal(state.total.value, 0)
  fail = false
  await loader.load({ page: 1, page_size: 20 })
  assert.equal(state.error.value, false)
  assert.equal(state.loading.value, false)
  assert.deepEqual(state.items.value, [])
  assert.equal(state.total.value, 0)
})
