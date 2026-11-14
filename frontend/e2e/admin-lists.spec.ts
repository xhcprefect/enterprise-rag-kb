import { randomUUID } from 'node:crypto'
import { expect, test, type APIRequestContext, type Page, type Route } from '@playwright/test'
import {
  apiCall, apiGetData, apiLogin, apiPostData, apiUrl,
  assertProxiedApi, loadManifest, uiLogin
} from './fixtures'

const manifest = loadManifest()
const PASSWORD = 'E2eAdminLists!2026'
let adminToken = ''

type Kind = 'users' | 'audit'
interface SyntheticUser { id: string; username: string }
interface ListPage { items: Array<{ username: string }>; total: number }
interface ListConfig {
  kind: Kind
  path: string
  apiPath: string
  filter: string
  filterAria: string
  errorId: string
}
const lists: ListConfig[] = [
  { kind: 'users', path: '/users', apiPath: '/api/v1/users', filter: 'keyword', filterAria: '搜索用户', errorId: 'users-load-error' },
  { kind: 'audit', path: '/audit', apiPath: '/api/v1/audit-logs', filter: 'username', filterAria: '按用户名筛选', errorId: 'audit-load-error' }
]

function uniquePrefix() {
  return `al-${manifest.nonce.slice(0, 6)}-${randomUUID().replace(/-/g, '').slice(0, 16)}`
}

async function createUser(request: APIRequestContext, users: SyntheticUser[], username: string) {
  const user = await apiPostData<SyntheticUser>(request, manifest, adminToken, '/users', {
    username, password: PASSWORD, display_name: username, role: 'user'
  })
  users.push(user)
  return user
}

async function actorLogin(request: APIRequestContext, username: string): Promise<string> {
  const response = await request.post(`${apiUrl(manifest)}/api/v1/auth/login`, {
    data: { username, password: PASSWORD }
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  const token = (await response.json()).data?.access_token as string | undefined
  expect(token).toBeTruthy()
  return token!
}

async function createPair(request: APIRequestContext, config: ListConfig, users: SyntheticUser[]) {
  const prefix = uniquePrefix()
  const a = await createUser(request, users, `${prefix}-a`)
  const b = await createUser(request, users, `${prefix}-b`)
  if (config.kind === 'audit') {
    // 两名真实合成操作者各自产生两类事件；不写审计表，也不构造成功响应。
    for (const actor of [a, b]) {
      const token = await actorLogin(request, actor.username)
      const logout = await apiCall(request, manifest, token, 'POST', '/auth/logout')
      expect(logout.status).toBe(200)
    }
  }
  return { a, b }
}

async function cleanupUsers(request: APIRequestContext, users: SyntheticUser[]) {
  const failures: string[] = []
  for (const user of [...users].reverse()) {
    try {
      const response = await apiCall(request, manifest, adminToken, 'DELETE', `/users/${user.id}`)
      if (![200, 404].includes(response.status)) failures.push(`${user.username}: HTTP ${response.status}`)
    } catch (error) {
      failures.push(`${user.username}: ${String(error)}`)
    }
  }
  expect(failures, '只能清理本用例创建的账号，并报告所有清理失败').toEqual([])
  // 审计事件是保留记录，现有 API 没有删除入口；账号删除由真实 API 处理其引用。
}

function matches(url: URL, config: ListConfig, params: Record<string, string>) {
  return url.pathname === config.apiPath && Object.entries(params).every(([key, value]) => url.searchParams.get(key) === value)
}

function listResponse(page: Page, config: ListConfig, params: Record<string, string> = {}) {
  return page.waitForResponse(response => response.request().method() === 'GET' && matches(new URL(response.url()), config, params))
}

async function responseData(response: Awaited<ReturnType<typeof listResponse>>): Promise<ListPage> {
  expect(response.status()).toBe(200)
  const data = (await response.json()).data as ListPage
  expect(Array.isArray(data.items)).toBe(true)
  expect(Number.isInteger(data.total)).toBe(true)
  return data
}

async function search(page: Page, config: ListConfig, value: string) {
  const input = page.getByRole('textbox', { name: config.filterAria, exact: true })
  await input.fill(value)
  await input.press('Enter')
}

async function expectRows(page: Page, config: ListConfig, data: ListPage) {
  await expect(page.getByRole('button', { name: /^(刷新|Refresh)$/, exact: true })).not.toHaveClass(/is-loading/)
  await expect(page.locator('.panel .count-badge')).toHaveText(String(data.total))
  const names = page.locator(config.kind === 'users' ? '.el-table .user-account' : '.el-table .actor-name')
  await expect(names).toHaveText(data.items.map(item => config.kind === 'users' ? `@${item.username}` : item.username))
  await expect(page.getByTestId(config.errorId)).toHaveCount(0)
}

async function expectLoading(page: Page) {
  await expect(page.getByRole('button', { name: /^(刷新|Refresh)$/, exact: true })).toHaveClass(/is-loading/)
  await expect(page.locator('.el-table__row')).toHaveCount(0)
  await expect(page.locator('.panel .count-badge')).toHaveText('0')
}

async function openList(page: Page, config: ListConfig) {
  const response = listResponse(page, config)
  await page.goto(config.path)
  await expectRows(page, config, await responseData(await response))
}

async function searchAndRead(page: Page, config: ListConfig, value: string) {
  const response = listResponse(page, config, { [config.filter]: value, page: '1' })
  await search(page, config, value)
  const data = await responseData(await response)
  await expectRows(page, config, data)
  return data
}

interface HeldRead {
  params: Record<string, string>
  outcome: 'success' | 'failure'
  taken: boolean
  received: boolean
  settled: boolean
  error: string
  data?: ListPage
  release: () => void
  gate: Promise<void>
}

/** 只延迟真实 GET 的原始响应或中断单次请求；其余请求直接经过真实代理。 */
async function installListRoutes(page: Page, config: ListConfig) {
  const holds: HeldRead[] = []
  const failures: Array<Record<string, string>> = []
  const requests: URL[] = []
  const pattern = `**${config.apiPath}?*`
  const handler = async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() !== 'GET' || url.pathname !== config.apiPath) return route.continue()
    requests.push(url)
    const failure = failures.findIndex(params => matches(url, config, params))
    if (failure >= 0) {
      failures.splice(failure, 1)
      await route.abort('failed')
      return
    }
    const held = holds.find(item => !item.taken && matches(url, config, item.params))
    if (!held) return route.continue()
    held.taken = true
    try {
      const response = await route.fetch({ timeout: 15_000 })
      expect(response.status()).toBe(200)
      held.data = (await response.json()).data as ListPage
      held.received = true
      await held.gate
      try {
        if (held.outcome === 'failure') await route.abort('failed')
        else await route.fulfill({ response })
      } catch (error) {
        // 新请求或卸载会主动取消旧 GET；只允许已确认取消的投递失败。
        if (!request.failure()) throw error
      }
    } catch (error) {
      held.error = String(error)
    } finally {
      held.settled = true
    }
  }
  await page.route(pattern, handler)
  return {
    requests,
    hold(params: Record<string, string>, outcome: HeldRead['outcome'] = 'success') {
      let release!: () => void
      const gate = new Promise<void>(resolve => { release = resolve })
      const held: HeldRead = { params, outcome, taken: false, received: false, settled: false, error: '', release, gate }
      holds.push(held)
      return held
    },
    failOnce(params: Record<string, string>) { failures.push(params) },
    async dispose() {
      for (const held of holds) held.release()
      await page.unroute(pattern, handler)
      await expect.poll(() => holds.every(held => !held.taken || held.settled), { timeout: 20_000 }).toBe(true)
      expect(holds.map(held => held.error).filter(Boolean)).toEqual([])
    }
  }
}

async function waitHeld(held: HeldRead) {
  await expect.poll(() => held.received || !!held.error, { timeout: 20_000 }).toBe(true)
  expect(held.error).toBe('')
}

async function releaseHeld(held: HeldRead) {
  held.release()
  await expect.poll(() => held.settled, { timeout: 20_000 }).toBe(true)
  expect(held.error).toBe('')
}

test.beforeAll(async ({ request }) => { adminToken = await apiLogin(request, manifest) })
test.beforeEach(async ({ page }) => {
  await assertProxiedApi(page, manifest)
  await uiLogin(page)
})

for (const config of lists) {
  test(`${config.kind}: late success/failure keep fast B, and old finally cannot finish pending B`, async ({ page, request }, testInfo) => {
    test.setTimeout(120_000)
    const users: SyntheticUser[] = []
    const controls = await installListRoutes(page, config)
    const pageErrors: string[] = []
    page.on('pageerror', error => pageErrors.push(error.message))
    try {
      const { a, b } = await createPair(request, config, users)
      await openList(page, config)
      for (const outcome of ['success', 'failure'] as const) {
        const old = controls.hold({ [config.filter]: a.username }, outcome)
        await search(page, config, a.username)
        await waitHeld(old)
        await expectLoading(page)
        expect(old.data?.items.every(item => item.username === a.username)).toBe(true)
        const latest = await searchAndRead(page, config, b.username)
        expect(latest.total).toBe(config.kind === 'users' ? 1 : 2)
        expect(latest.items.every(item => item.username === b.username)).toBe(true)
        await releaseHeld(old)
        await expectRows(page, config, latest)
      }

      const old = controls.hold({ [config.filter]: a.username })
      const latest = controls.hold({ [config.filter]: b.username })
      await search(page, config, a.username)
      await waitHeld(old)
      await search(page, config, b.username)
      await waitHeld(latest)
      await expectLoading(page)
      await releaseHeld(old)
      await expectLoading(page)
      await releaseHeld(latest)
      await expectRows(page, config, latest.data!)
      await page.screenshot({ path: testInfo.outputPath(`${config.kind}-latest-success.png`), fullPage: true, animations: 'disabled' })
      expect(pageErrors).toEqual([])
    } finally {
      try { await controls.dispose() } finally { await cleanupUsers(request, users) }
    }
  })

  test(`${config.kind}: failed B clears A, bilingual refresh recovers, and unmount isolates held callbacks`, async ({ page, request }, testInfo) => {
    test.setTimeout(120_000)
    const users: SyntheticUser[] = []
    const controls = await installListRoutes(page, config)
    const pageErrors: string[] = []
    page.on('pageerror', error => pageErrors.push(error.message))
    try {
      const { a, b } = await createPair(request, config, users)
      await openList(page, config)
      await searchAndRead(page, config, a.username)
      controls.failOnce({ [config.filter]: b.username })
      await search(page, config, b.username)
      const error = page.getByTestId(config.errorId)
      await expect(error).toContainText('请求失败')
      await expect(page.locator('.el-table__row')).toHaveCount(0)
      await expect(page.locator('.panel .count-badge')).toHaveText('0')
      await expect(page.getByRole('button', { name: '刷新', exact: true })).toBeEnabled()
      await page.screenshot({ path: testInfo.outputPath(`${config.kind}-error-zh.png`), fullPage: true, animations: 'disabled' })

      await page.locator('.language-button').click()
      await page.getByRole('menuitem', { name: 'English', exact: true }).click()
      await expect(error).toContainText('Request failed')
      const refresh = page.getByRole('button', { name: 'Refresh', exact: true })
      await expect(refresh).toBeEnabled()
      await page.screenshot({ path: testInfo.outputPath(`${config.kind}-error-en.png`), fullPage: true, animations: 'disabled' })
      const response = listResponse(page, config, { [config.filter]: b.username })
      await refresh.click()
      const recovered = await responseData(await response)
      expect(recovered.total).toBe(config.kind === 'users' ? 1 : 2)
      await expectRows(page, config, recovered)
      await page.screenshot({ path: testInfo.outputPath(`${config.kind}-recovered-en.png`), fullPage: true, animations: 'disabled' })

      // 在同一 SPA 内切走，才能检验组件 dispose；不以整页导航销毁 JS 代替卸载。
      const old = controls.hold({ [config.filter]: a.username })
      const englishFilter = config.kind === 'users' ? 'Search users' : 'Filter by username'
      const input = page.getByRole('textbox', { name: englishFilter, exact: true })
      await input.fill(a.username)
      await input.press('Enter')
      await waitHeld(old)
      await page.locator('.side-profile').click()
      await page.waitForURL('**/profile')
      await expect(page.getByRole('heading', { level: 2, name: 'My account', exact: true })).toBeVisible()
      await releaseHeld(old)
      await expect(page.getByRole('heading', { level: 2, name: 'My account', exact: true })).toBeVisible()
      const reopened = listResponse(page, config)
      await page.locator(`.nav a[href="${config.path}"]`).click()
      await expectRows(page, config, await responseData(await reopened))
      const fresh = listResponse(page, config, { [config.filter]: b.username })
      await page.getByRole('textbox', { name: englishFilter, exact: true }).fill(b.username)
      await page.getByRole('textbox', { name: englishFilter, exact: true }).press('Enter')
      await expectRows(page, config, await responseData(await fresh))
      expect(pageErrors).toEqual([])
    } finally {
      try { await controls.dispose() } finally { await cleanupUsers(request, users) }
    }
  })

  test(`${config.kind}: real pagination captures page and size, and size/filter changes return to page one once`, async ({ page, request }) => {
    test.setTimeout(180_000)
    const users: SyntheticUser[] = []
    const controls = await installListRoutes(page, config)
    try {
      let filter: string
      let single: string
      if (config.kind === 'users') {
        const prefix = uniquePrefix()
        for (let index = 0; index < 21; index += 1) await createUser(request, users, `${prefix}-${String(index).padStart(2, '0')}`)
        filter = prefix
        single = users[0].username
      } else {
        const { a } = await createPair(request, config, users)
        for (let index = 0; index < 19; index += 1) await actorLogin(request, a.username)
        filter = a.username
        single = a.username
      }
      await openList(page, config)
      const first = await searchAndRead(page, config, filter)
      expect(first.total).toBe(21)
      expect(first.items).toHaveLength(20)
      const beforePage = controls.requests.length
      const secondResponse = listResponse(page, config, { [config.filter]: filter, page: '2', page_size: '20' })
      await page.locator('.el-pagination .btn-next').click()
      const second = await responseData(await secondResponse)
      expect(second.total).toBe(21)
      expect(second.items).toHaveLength(1)
      await expectRows(page, config, second)
      await expect(page.locator('.el-pagination .el-pager .is-active')).toHaveText('2')
      expect(controls.requests.length - beforePage).toBe(1)

      const beforeSize = controls.requests.length
      const resizedResponse = listResponse(page, config, { [config.filter]: filter, page: '1', page_size: '50' })
      await page.locator('.el-pagination .el-select').click()
      await page.locator('.el-select-dropdown:visible').getByRole('option', { name: /^50/ }).click()
      const resized = await responseData(await resizedResponse)
      expect(resized.total).toBe(21)
      expect(resized.items).toHaveLength(21)
      await expectRows(page, config, resized)
      await expect(page.locator('.el-pagination .el-pager .is-active')).toHaveText('1')
      expect(controls.requests.length - beforeSize).toBe(1)

      const resetSizeResponse = listResponse(page, config, { page: '1', page_size: '20' })
      await page.locator('.el-pagination .el-select').click()
      await page.locator('.el-select-dropdown:visible').getByRole('option', { name: /^20/ }).click()
      await expectRows(page, config, await responseData(await resetSizeResponse))
      const pageTwo = listResponse(page, config, { page: '2' })
      await page.locator('.el-pagination .btn-next').click()
      await expectRows(page, config, await responseData(await pageTwo))
      const beforeFilter = controls.requests.length
      if (config.kind === 'users') {
        const filtered = await searchAndRead(page, config, single)
        expect(filtered.total).toBe(1)
      } else {
        await page.locator('.page-actions .el-select .el-select__wrapper').click()
        const [filteredResponse] = await Promise.all([
          listResponse(page, config, { username: single, action: 'logout', page: '1' }),
          page.locator('.el-select-dropdown:visible').getByRole('option', { name: '退出登录', exact: true }).click()
        ])
        const filtered = await responseData(filteredResponse)
        expect(filtered.total).toBe(1)
        await expectRows(page, config, filtered)
      }
      await expect(page.locator('.el-pagination .el-pager .is-active')).toHaveText('1')
      expect(controls.requests.length - beforeFilter).toBe(1)
      // 独立读取真实 API，确认全部 21 条合成记录仍在，翻页没有修改业务数据。
      const params = new URLSearchParams({ [config.filter]: filter, page: '1', page_size: '50' })
      const api = await apiGetData<ListPage>(request, manifest, adminToken, `${config.apiPath.replace('/api/v1', '')}?${params}`)
      expect(api.total).toBe(21)
    } finally {
      try { await controls.dispose() } finally { await cleanupUsers(request, users) }
    }
  })
}
