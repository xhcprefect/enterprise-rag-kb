import { expect, test } from '@playwright/test'
import { apiLogin, apiUrl, assertProxiedApi, loadManifest, uiLogin } from './fixtures'

const manifest = loadManifest()
const operationsPath = '/api/v1/operations/workers'

interface WorkerApiRole {
  role: string
  enabled: boolean
  state: string
  alive_count: number | null
  last_heartbeat_at: string | null
}

interface WorkerApiSnapshot {
  roles?: WorkerApiRole[]
}

function isOperationsResponse(response: import('@playwright/test').Response): boolean {
  return new URL(response.url()).pathname.endsWith(operationsPath) && response.request().method() === 'GET'
}

test.beforeEach(async ({ page }) => {
  await assertProxiedApi(page, manifest)
  await uiLogin(page)
})

test('admin sees live role status from the real API and the page fits 390px', async ({ page, request }, testInfo) => {
  const token = await apiLogin(request, manifest)
  const initialResponsePromise = page.waitForResponse(isOperationsResponse)
  await page.goto('/operations')
  expect((await initialResponsePromise).status()).toBe(200)

  const readReadiness = async () => {
    const response = await request.get(`${apiUrl(manifest)}${operationsPath}`, {
      headers: { Authorization: `Bearer ${token}` }
    })
    const body = await response.json() as { data?: WorkerApiSnapshot } & WorkerApiSnapshot
    const snapshot = body.data ?? body
    const roles = snapshot.roles ?? []
    return {
      status: response.status(),
      roles: roles.map(role => ({
        role: role.role,
        enabled: role.enabled === true,
        running: role.state === 'running',
        alive: typeof role.alive_count === 'number' && role.alive_count >= 1,
        heartbeat: typeof role.last_heartbeat_at === 'string' && role.last_heartbeat_at.length > 0
      }))
    }
  }

  // Workers renew heartbeats independently; poll the actual admin API instead of
  // treating a single early snapshot as healthy.
  await expect.poll(readReadiness, {
    timeout: 45_000,
    message: 'waiting for all four real workers to report enabled running heartbeats'
  }).toEqual({
    status: 200,
    roles: [
      { role: 'ingest', enabled: true, running: true, alive: true, heartbeat: true },
      { role: 'summary', enabled: true, running: true, alive: true, heartbeat: true },
      { role: 'project', enabled: true, running: true, alive: true, heartbeat: true },
      { role: 'regression', enabled: true, running: true, alive: true, heartbeat: true }
    ]
  })

  const refreshedResponsePromise = page.waitForResponse(isOperationsResponse)
  await page.getByRole('button', { name: /刷新运行状态|Refresh worker status/ }).click()
  expect((await refreshedResponsePromise).status()).toBe(200)

  await expect(page.getByRole('heading', { level: 2, name: /运行状态|Worker status/ })).toBeVisible()
  const cards = page.getByRole('listitem')
  await expect(cards).toHaveCount(4)
  for (const label of ['文档入库', '会话摘要', '项目索引', '回归重跑']) {
    const card = cards.filter({ hasText: label })
    await expect(card).toHaveCount(1)
    await expect(card.locator('.state-label')).toContainText(/运行中|Running/)
    await expect(card).toContainText(/已启用|Enabled/)
    await expect(card).not.toContainText(/未启用|Not enabled|未记录|Not recorded/)
  }
  await expect(page.getByText(/安全提示|Security note/)).toBeVisible()

  await page.screenshot({
    path: testInfo.outputPath('operations-1440.png'),
    fullPage: true,
    animations: 'disabled'
  })

  await page.setViewportSize({ width: 390, height: 844 })
  const widths = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
  }))
  expect(widths.document).toBeLessThanOrEqual(widths.viewport)
  await page.screenshot({
    path: testInfo.outputPath('operations-390.png'),
    fullPage: true,
    animations: 'disabled'
  })

  await page.locator('.theme-button').click()
  await expect(page.getByRole('menuitem', { name: '深色模式' })).toBeVisible()
  await page.getByRole('menuitem', { name: '深色模式' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.locator('.language-button').click()
  await expect(page.getByRole('menuitem', { name: 'English' })).toBeVisible()
  await page.getByRole('menuitem', { name: 'English' }).click()
  await expect(page.locator('html')).toHaveAttribute('lang', 'en-US')
  await expect(page.getByRole('heading', { level: 2, name: 'Worker status' })).toBeVisible()
  for (const label of ['Document ingestion', 'Conversation summaries', 'Project indexing', 'Regression reruns']) {
    const card = page.getByRole('listitem').filter({ hasText: label })
    await expect(card).toHaveCount(1)
    await expect(card.locator('.state-label')).toContainText('Running')
  }
  await page.screenshot({
    path: testInfo.outputPath('operations-390-en-dark.png'),
    fullPage: true,
    animations: 'disabled'
  })
})

test('network failure hides prior data and refresh recovers from the real API', async ({ page }, testInfo) => {
  let fail = false
  await page.route(`**${operationsPath}`, route => fail ? route.abort('failed') : route.continue())
  const initialResponsePromise = page.waitForResponse(isOperationsResponse)
  await page.goto('/operations')
  expect((await initialResponsePromise).status()).toBe(200)
  await expect(page.getByRole('listitem')).toHaveCount(4)

  fail = true
  await page.getByRole('button', { name: /刷新运行状态|Refresh worker status/ }).click()
  await expect(page.getByTestId('operations-load-error')).toBeVisible()
  await expect(page.getByRole('listitem')).toHaveCount(0)
  await page.screenshot({
    path: testInfo.outputPath('operations-error-1440.png'),
    fullPage: true,
    animations: 'disabled'
  })

  fail = false
  const recoveryResponsePromise = page.waitForResponse(isOperationsResponse)
  await page.getByRole('button', { name: /刷新运行状态|Refresh worker status/ }).click()
  expect((await recoveryResponsePromise).status()).toBe(200)
  await expect(page.getByTestId('operations-load-error')).toHaveCount(0)
  await expect(page.getByRole('listitem')).toHaveCount(4)
  await page.screenshot({
    path: testInfo.outputPath('operations-recovered-1440.png'),
    fullPage: true,
    animations: 'disabled'
  })
})
