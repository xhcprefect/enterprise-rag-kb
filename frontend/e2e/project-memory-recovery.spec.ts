import { test, expect, type APIRequestContext, type Page } from '@playwright/test'
import { writeFileSync, realpathSync } from 'node:fs'
import { dirname, resolve, relative, isAbsolute } from 'node:path'
import { apiLogin, apiUrl, assertProxiedApi, loadManifest, stubControl, uiLogin } from './fixtures'

const manifest = loadManifest()
const stub = stubControl(manifest)
let token = ''
const headers = () => ({ Authorization: `Bearer ${token}` })

async function api(request: APIRequestContext, method: 'get' | 'post' | 'patch' | 'delete', path: string, data?: unknown) {
  const response = await request[method](`${apiUrl(manifest)}/api/v1${path}`, { headers: headers(), data })
  expect(response.ok(), await response.text()).toBeTruthy()
  return (await response.json()).data
}

function fixtureVersion(version: 'ONE' | 'TWO') {
  const fixture = manifest.project_fixture
  if (!fixture) throw new Error('manifest 缺少合成项目目录')
  const owned = dirname(resolve(process.env.RAG_E2E_MANIFEST!))
  const file = realpathSync(fixture.file)
  const within = relative(owned, file)
  if (within.startsWith('..') || isAbsolute(within)) throw new Error('合成文件不属于本轮输出目录')
  writeFileSync(file, `# 合成项目\n\nALPHA_VERSION_${version}：后台任务采用${version === 'ONE' ? '第一' : '第二'}版执行规则。\n`)
}

async function readySnapshot(request: APIRequestContext, sourceId: string, snapshotId: string) {
  await expect.poll(async () => {
    const snapshots = await api(request, 'get', `/projects/${sourceId}/snapshots`)
    return snapshots.find((item: { id: string }) => item.id === snapshotId)?.status
  }, { timeout: 30_000 }).toBe('ready')
}

async function send(page: Page, question: string) {
  await page.getByRole('textbox', { name: '输入问题', exact: true }).fill(question)
  const waiting = page.waitForResponse(response => response.url().endsWith('/api/v1/chat/stream') && response.request().method() === 'POST')
  await page.getByTestId('composer-send').click()
  const response = await waiting
  expect(response.ok()).toBeTruthy()
  const payload = response.request().postDataJSON()
  // 浏览器按 done 事件结束消费；SSE 传输不保证触发 DevTools 的 requestFinished。
  await expect(page.getByTestId('composer-send')).toBeVisible({ timeout: 20_000 })
  let completed: Record<string, any> = {}
  await expect.poll(async () => {
    completed = await api(page.request, 'get', `/chat/requests/${payload.request_id}`)
    return completed.status
  }).toBe('completed')
  return { meta: completed, done: completed, payload }
}

async function selectSnapshot(page: Page, snapshotId: string) {
  await page.getByTestId('project-snapshot-bar').locator('.el-select').click()
  await page.getByRole('option', { name: `快照 ${snapshotId}`, exact: true }).click()
}

test.beforeAll(async ({ request }) => {
  token = await apiLogin(request, manifest)
  expect(Object.keys(manifest.workers || {}).sort()).toEqual(['ingest', 'project', 'summary'])
})

test.beforeEach(async ({ page }) => {
  await assertProxiedApi(page, manifest)
  await stub.reset()
})

test('project registration, fixed snapshots, CAS conflicts and historical source previews', async ({ page, request }) => {
  fixtureVersion('ONE')
  const previous = await api(request, 'post', '/projects', {
    name: `项目切换验收 ${Date.now()}`,
    root_key: manifest.project_fixture!.root_key,
    relative_dir: manifest.project_fixture!.relative_dir
  })
  await uiLogin(page)
  await page.goto('/projects')
  await page.getByRole('button', { name: '登记项目源', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '登记项目源' })
  const sourceName = `浏览器项目 ${Date.now()}`
  await dialog.getByPlaceholder('例如：核心服务源码').fill(sourceName)
  // 唯一授权根时表单会自动选中。
  const created = page.waitForResponse(response => response.url().endsWith('/api/v1/projects') && response.request().method() === 'POST')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  const source = (await (await created).json()).data
  await expect(page.locator(`[data-source-id="${source.id}"]`)).toHaveAttribute('aria-current', 'true')
  await expect(page.locator('.detail-title')).toContainText(sourceName)
  await page.locator(`[data-source-id="${previous.id}"]`).click()
  await expect(page.locator('.detail-title')).toContainText(previous.name)
  await page.locator(`[data-source-id="${source.id}"]`).click()
  await expect(page.locator('.detail-title')).toContainText(sourceName)
  const syncing = page.waitForResponse(response => response.url().endsWith(`/projects/${source.id}/sync`))
  await page.getByTestId('sync-button').click()
  const first = (await (await syncing).json()).data
  await readySnapshot(request, source.id, first.id)
  await expect(page.getByTestId('source-status')).toHaveText('已就绪', { timeout: 20_000 })
  await page.getByRole('textbox', { name: '搜索项目内容', exact: true }).fill('ALPHA_VERSION_ONE')
  await page.getByTestId('project-search-run').click()
  await expect(page.locator('.result-area')).toContainText('ALPHA_VERSION_ONE')

  await page.goto('/chat')
  await page.locator('#chat-knowledge-base').press('Enter')
  await page.getByRole('option', { name: sourceName }).click()
  await expect(page.getByTestId('project-snapshot-bar')).toContainText(first.id)
  const firstTurn = await send(page, 'ALPHA_VERSION_ONE 后台任务执行规则是什么？')
  expect(firstTurn.payload.project_snapshot_id).toBe(first.id)
  expect(firstTurn.payload).not.toHaveProperty('document_ids')
  const conversationId = firstTurn.meta.conversation_id
  await expect(page.locator('.message.assistant').last()).toContainText('ALPHA_VERSION_ONE')

  fixtureVersion('TWO')
  const second = await api(request, 'post', `/projects/${source.id}/sync`)
  await readySnapshot(request, source.id, second.id)
  const pinned = await send(page, 'ALPHA_VERSION_ONE 后台任务还是第一版吗？')
  expect(pinned.payload).not.toHaveProperty('project_snapshot_id')
  expect(pinned.payload).not.toHaveProperty('document_ids')
  expect(pinned.done.project_snapshot_id).toBe(first.id)
  await page.getByTestId('project-snapshot-bar').getByRole('button', { name: '重试', exact: true }).click()
  await selectSnapshot(page, second.id)
  await expect.poll(async () => (await api(request, 'get', `/chat/conversations/${conversationId}`)).scope_revision).toBe(1)
  await send(page, 'ALPHA_VERSION_TWO 后台任务执行规则是什么？')
  await expect(page.locator('.message.assistant').last()).toContainText('ALPHA_VERSION_TWO')

  // 第一轮引用仍读取第一版快照，即使会话已显式改绑第二版。
  await page.locator('.message.assistant').first().getByRole('button', { name: /引用来源/ }).click()
  await page.locator('.message.assistant').first().getByRole('button', { name: '查看原文' }).first().click()
  const drawer = page.getByRole('dialog', { name: '引用原文' })
  await expect(drawer).toContainText(first.id)
  await expect(drawer).toContainText('ALPHA_VERSION_ONE')
  await expect(drawer).not.toContainText('ALPHA_VERSION_TWO')
  await page.keyboard.press('Escape')

  // 另一客户端改绑后，UI 的旧 revision 必须收到真实 409，并刷新服务端绑定。
  await api(request, 'patch', `/chat/conversations/${conversationId}/project-snapshot`, { snapshot_id: first.id, expected_revision: 1 })
  const conflict = page.waitForResponse(response => response.url().endsWith('/project-snapshot') && response.request().method() === 'PATCH')
  await selectSnapshot(page, first.id)
  expect((await conflict).status()).toBe(409)
  await expect(page.getByTestId('project-snapshot-bar')).toContainText('会话范围已被更新')
  const detail = await api(request, 'get', `/chat/conversations/${conversationId}`)
  expect(detail.project_snapshot_id).toBe(first.id)
  expect(detail.scope_revision).toBe(2)
})

test('summary worker publishes and resets real conversation memory', async ({ page, request }) => {
  await uiLogin(page)
  await page.goto('/chat')
  await page.getByText('普通对话', { exact: true }).click()
  await expect(page.getByRole('radio', { name: '普通对话', exact: true })).toBeChecked()
  const first = await send(page, '请记住：项目使用三个独立 worker。')
  await send(page, '项目根目录只读，采用固定快照。')
  const path = `/chat/conversations/${first.meta.conversation_id}/memory`
  await expect.poll(async () => (await api(request, 'get', path)).state, { timeout: 30_000 }).toBe('ready')
  await expect(page.getByRole('button', { name: '查看摘要', exact: true })).toBeVisible({ timeout: 20_000 })
  await page.getByRole('button', { name: '查看摘要', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('三个独立 worker')
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '重置摘要', exact: true }).click()
  await page.getByRole('dialog', { name: '重置摘要', exact: true })
    .getByRole('button', { name: '重置摘要', exact: true }).click()
  await expect.poll(async () => (await api(request, 'get', path)).version).toBeNull()
  expect((await stub.state()).counts.summary_calls).toBeGreaterThan(0)
})

test('processing recovery keeps the original request locked until cancellation', async ({ page, request }) => {
  await stub.configure({ chat_slow_match: 'RECOVERY_SLOW', chat_slow_ms: 2500 })
  // 只注入浏览器读流故障：请求、正文和状态仍来自真实 API，保留上游连接以重放
  // “浏览器已丢失流，但服务端仍在处理”的窗口，不伪造任何业务响应。
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    window.fetch = async (...args) => {
      const response = await original(...args)
      if (!String(args[0]).endsWith('/chat/stream') || !String(args[1]?.body).includes('RECOVERY_SLOW')) return response
      const reader = response.body!.getReader()
      let reads = 0
      const body = new ReadableStream({
        async pull(controller) {
          const chunk = await reader.read()
          if (++reads === 2) { controller.error(new TypeError('E2E injected stream read fault')); return }
          if (chunk.done) controller.close()
          else controller.enqueue(chunk.value)
        }
      })
      return new Response(body, { status: response.status, headers: response.headers })
    }
  })
  await uiLogin(page)
  await page.goto('/chat')
  await page.getByText('普通对话', { exact: true }).click()
  await expect(page.getByRole('radio', { name: '普通对话', exact: true })).toBeChecked()
  let requestId = ''
  let sends = 0
  page.on('request', outgoing => {
    if (outgoing.url().endsWith('/api/v1/chat/stream') && outgoing.method() === 'POST') {
      sends++
      requestId = outgoing.postDataJSON().request_id
    }
  })
  await page.getByRole('textbox', { name: '输入问题', exact: true }).fill('RECOVERY_SLOW 请保持处理以验证恢复锁')
  await page.getByTestId('composer-send').click()
  await expect(page.getByTestId('recovery-actions')).toBeVisible({ timeout: 20_000 })
  await expect(page.getByTestId('composer-send')).toBeVisible()
  expect((await api(request, 'get', `/chat/requests/${requestId}`)).status).toBe('processing')
  await page.getByRole('textbox', { name: '输入问题', exact: true }).fill('这时不能新发请求')
  await expect(page.getByTestId('composer-send')).toBeDisabled()
  await page.getByTestId('recovery-check').click()
  await expect(page.getByTestId('recovery-actions')).toBeVisible()
  await expect(page.getByTestId('composer-send')).toBeDisabled()
  await page.getByTestId('recovery-cancel').click()
  await expect(page.getByTestId('recovery-actions')).toHaveCount(0)
  await expect(page.getByTestId('composer-send')).toBeEnabled()
  expect((await api(request, 'get', `/chat/requests/${requestId}`)).status).toBe('cancelled')
  expect(sends).toBe(1)
})

test('cancelling a held upload completion releases the next queued file', async ({ page, request }) => {
  const kb = await api(request, 'post', '/knowledge-bases', { name: `队列验收 ${Date.now()}`, description: '', is_public: false })
  await uiLogin(page)
  await page.goto(`/documents/${kb.id}`)
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*/upload-sessions/*/complete', requestStage: 'Response' }] })
  let pausedId = ''
  let hold!: () => void
  const held = new Promise<void>(resolve => { hold = resolve })
  cdp.on('Fetch.requestPaused', event => {
    if (!pausedId) { pausedId = event.requestId; hold() }
    else void cdp.send('Fetch.continueResponse', { requestId: event.requestId }).catch(() => undefined)
  })
  await page.locator('input[type=file]').setInputFiles([
    { name: 'first-held.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第一个文件\n\n队列验收内容一。') },
    { name: 'second-ready.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第二个文件\n\n队列验收内容二。') }
  ])
  await held
  await expect(page.locator('.upload-card')).toContainText('first-held.md')
  await page.locator('.upload-card').getByRole('button', { name: '取消当前', exact: true }).click()
  await expect(page.locator('.upload-card')).toContainText('second-ready.md')
  await cdp.send('Fetch.continueResponse', { requestId: pausedId }).catch(() => undefined)
  await expect.poll(async () => {
    const data = await api(request, 'get', `/knowledge-bases/${kb.id}/documents`)
    return data.items.find((item: { filename: string }) => item.filename === 'second-ready.md')?.status
  }, { timeout: 30_000 }).toBe('ready')
  await expect(page.locator('.upload-card')).not.toContainText('first-held.md')
  await cdp.send('Fetch.disable')
  await cdp.detach()
})
