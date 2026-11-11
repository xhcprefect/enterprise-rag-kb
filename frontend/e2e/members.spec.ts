import {
  expect,
  test,
  type APIRequestContext,
  type Locator,
  type Page,
  type Request,
  type Response,
  type Route
} from '@playwright/test'
import { apiLogin, apiUrl, assertProxiedApi, loadManifest, uiLogin } from './fixtures'

const manifest = loadManifest()
const PASSWORD = 'E2eMember!2026'
let adminToken = ''

const authHeaders = (token = adminToken) => ({ Authorization: `Bearer ${token}` })

let nonceSequence = 0

function uniqueNonce(scope: string): string {
  // 随机/时间尾必须完整保留：manifest.nonce 本身有 32 字符，若先拼长 scope 再截断，
  // 尾部会被整段切掉，同一 harness 内重跑同一 scope 就会撞同名用户（409）。
  nonceSequence += 1
  const tail = `${Date.now().toString(36)}${nonceSequence.toString(36)}${Math.random()
    .toString(36)
    .slice(2, 6)
    .padEnd(4, '0')}`
  const scopePart = scope.replace(/[^A-Za-z0-9_.-]/g, '-').slice(0, 18)
  const nonceBudget = Math.max(4, 48 - tail.length - scopePart.length - 2)
  const noncePart = manifest.nonce.replace(/[^A-Za-z0-9_.-]/g, '-').slice(0, nonceBudget)
  return `${noncePart}-${scopePart}-${tail}`
}

async function api(
  request: APIRequestContext,
  method: 'get' | 'post' | 'put' | 'delete',
  path: string,
  data?: unknown,
  token = adminToken
) {
  const url = `${apiUrl(manifest)}/api/v1${path}`
  const options = { headers: authHeaders(token), data }
  const response =
    method === 'get'
      ? await request.get(url, options)
      : method === 'post'
        ? await request.post(url, options)
        : method === 'put'
          ? await request.put(url, options)
          : await request.delete(url, options)
  expect(response.ok(), await response.text()).toBeTruthy()
  return (await response.json()).data
}

async function loginToken(request: APIRequestContext, username: string, password: string): Promise<string> {
  const response = await request.post(`${apiUrl(manifest)}/api/v1/auth/login`, {
    data: { username, password }
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  const body = (await response.json()) as { data?: { access_token?: string } }
  const token = body.data?.access_token
  if (!token) throw new Error('登录响应缺少 access_token')
  return token
}

async function createUser(
  request: APIRequestContext,
  username: string,
  displayName: string
): Promise<{ id: string; username: string }> {
  return api(request, 'post', '/users', {
    username,
    password: PASSWORD,
    display_name: displayName,
    role: 'user'
  })
}

async function createPrivateKb(request: APIRequestContext, name: string): Promise<{ id: string; name: string }> {
  return api(request, 'post', '/knowledge-bases', {
    name,
    description: '成员权限浏览器验收专用知识库',
    is_public: false
  })
}

async function addMember(
  request: APIRequestContext,
  kbId: string,
  userId: string,
  role: 'reader' | 'maintainer'
) {
  const list = await api(request, 'get', `/knowledge-bases/${kbId}/members?page=1&page_size=20`)
  return api(request, 'put', `/knowledge-bases/${kbId}/members/${userId}`, {
    role,
    expected_revision: list.revision
  })
}

async function uiLoginAs(page: Page, username: string, password: string): Promise<void> {
  await page.goto('/login')
  await page.getByPlaceholder('请输入用户名').fill(username)
  await page.getByPlaceholder('请输入密码').fill(password)
  await page.getByRole('button', { name: /登录工作空间/ }).click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 30_000 })
}

function kbCard(page: Page, kbName: string): Locator {
  return page.locator('article.kb-card').filter({ hasText: kbName }).first()
}

async function openMembersDialog(page: Page, kbName: string, menuLabel: string, dialogTitle: string): Promise<Locator> {
  const card = kbCard(page, kbName)
  await expect(card).toBeVisible()
  await card.locator('.kb-more').click()
  await page.getByRole('menuitem', { name: menuLabel, exact: true }).click()
  const dialog = page.getByRole('dialog', { name: dialogTitle, exact: true })
  await expect(dialog).toBeVisible()
  return dialog
}

async function chooseUser(page: Page, dialog: Locator, keyword: string, displayName: string) {
  const select = dialog.getByTestId('members-user-select')
  await select.click()
  await select.locator('input').fill(keyword)
  const option = page.getByRole('option', { name: new RegExp(displayName) })
  await expect(option).toBeVisible()
  await option.click()
}

async function selectRole(page: Page, scope: Locator, optionName: string) {
  await scope.click()
  // 同一对话框里可能还残留已关闭的 el-select 下拉；只点当前可见的那一个。
  const dropdown = page.locator('.el-select-dropdown:visible').last()
  await dropdown.getByRole('option', { name: optionName, exact: true }).click()
}

const COMPLETE_GLOB = '**/api/v1/knowledge-bases/*/upload-sessions/*/complete'
const KB_LIST_GLOB = '**/api/v1/knowledge-bases*'

interface UploadRaceControls {
  counters: () => {
    completeRequests: number
    completeStatuses: number[]
    /** 首个 complete 的终态：HTTP 403/503 也走 requestfinished，网络失败才是 requestfailed。 */
    firstCompleteTerminal: 'none' | 'pending' | 'finished' | 'failed'
    kbBlocked: number
    createSessions: number
  }
  setKbBlocked: (value: boolean) => void
  releaseComplete: () => void
  dispose: () => Promise<void>
}

/**
 * 只注入"请求延迟/网络失败"，不伪造任何业务成功或能力响应：
 * - 第一个 /complete POST 用 deferred gate 卡住，放行后 continue 到真实后端；
 * - KB 列表 GET 可按需 abort 成网络失败，模拟能力暂时未知；
 * - 通过真实请求计数观察是否多建上传会话、重复合并。
 * 监听在安装阶段就注册：撤权触发的客户端 abort 可能早于放行，晚注册会漏掉终态。
 */
async function installUploadRaceRoutes(page: Page): Promise<UploadRaceControls> {
  let kbBlockedFlag = false
  let kbBlockedCount = 0
  let completeRequests = 0
  let createSessions = 0
  let released = false
  const completeStatuses: number[] = []
  let firstCompleteUrl = ''
  let firstCompleteTerminal: 'none' | 'pending' | 'finished' | 'failed' = 'none'
  let releaseGate!: () => void
  const gate = new Promise<void>((resolve) => {
    releaseGate = resolve
  })
  const releaseComplete = () => {
    if (released) return
    released = true
    releaseGate()
  }

  const isComplete = (request: Request) => {
    const url = new URL(request.url())
    return (
      request.method() === 'POST' &&
      /\/api\/v1\/knowledge-bases\/[^/]+\/upload-sessions\/[^/]+\/complete$/.test(url.pathname)
    )
  }
  const isCreateSession = (request: Request) => {
    const url = new URL(request.url())
    return (
      request.method() === 'POST' &&
      /\/api\/v1\/knowledge-bases\/[^/]+\/upload-sessions$/.test(url.pathname)
    )
  }
  const isFirstComplete = (request: Request) => !!firstCompleteUrl && request.url() === firstCompleteUrl

  const onRequest = (request: Request) => {
    if (isCreateSession(request)) createSessions += 1
    if (!firstCompleteUrl && isComplete(request)) {
      firstCompleteUrl = request.url()
      firstCompleteTerminal = 'pending'
    }
  }
  const onRequestFinished = (request: Request) => {
    if (isFirstComplete(request)) firstCompleteTerminal = 'finished'
  }
  const onRequestFailed = (request: Request) => {
    if (isFirstComplete(request)) firstCompleteTerminal = 'failed'
  }
  const onResponse = (response: Response) => {
    const request = response.request()
    if (isComplete(request)) completeStatuses.push(response.status())
  }
  page.on('request', onRequest)
  page.on('requestfinished', onRequestFinished)
  page.on('requestfailed', onRequestFailed)
  page.on('response', onResponse)

  const completeRoute = async (route: Route) => {
    completeRequests += 1
    if (completeRequests === 1) await gate
    try {
      await route.continue()
    } catch {
      // 客户端确认撤权后会 abort 在途 complete；这里只负责收口路由，不推断业务结果。
    }
  }

  const kbRoute = async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'GET' && url.pathname === '/api/v1/knowledge-bases' && kbBlockedFlag) {
      kbBlockedCount += 1
      await route.abort('failed')
      return
    }
    await route.continue()
  }

  await page.route(COMPLETE_GLOB, completeRoute)
  await page.route(KB_LIST_GLOB, kbRoute)

  return {
    counters: () => ({
      completeRequests,
      completeStatuses: [...completeStatuses],
      firstCompleteTerminal,
      kbBlocked: kbBlockedCount,
      createSessions
    }),
    setKbBlocked: (value) => {
      kbBlockedFlag = value
    },
    releaseComplete,
    dispose: async () => {
      releaseComplete()
      page.off('request', onRequest)
      page.off('requestfinished', onRequestFinished)
      page.off('requestfailed', onRequestFailed)
      page.off('response', onResponse)
      await page.unroute(COMPLETE_GLOB, completeRoute)
      await page.unroute(KB_LIST_GLOB, kbRoute)
    }
  }
}

test.beforeAll(async ({ request }) => {
  adminToken = await apiLogin(request, manifest)
})

test.beforeEach(async ({ page }) => {
  await assertProxiedApi(page, manifest)
})

test('admin adds, changes, and removes explicit members with CAS conflict refresh', async ({ page, request }, testInfo) => {
  const nonce = uniqueNonce('members-ui')
  const username = `m-${nonce}`
  const displayName = `成员目标 ${nonce}`
  const kbName = `成员验收库 ${nonce}`

  const user = await createUser(request, username, displayName)
  const kb = await createPrivateKb(request, kbName)

  await uiLogin(page)
  await page.goto('/knowledge')
  let dialog = await openMembersDialog(page, kbName, '成员管理', `成员管理：${kbName}`)
  await expect(dialog.getByTestId('members-revision')).toContainText('版本')

  await chooseUser(page, dialog, username, displayName)
  const added = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith(`/knowledge-bases/${kb.id}/members/${user.id}`) &&
      response.request().method() === 'PUT'
  )
  await dialog.getByRole('button', { name: '添加成员', exact: true }).click()
  expect((await added).status()).toBe(200)

  const row = dialog.locator('.member-row').filter({ hasText: username })
  await expect(row).toBeVisible()
  await expect(row.locator('.member-role')).toContainText('读取')
  await page.screenshot({
    path: testInfo.outputPath('members-1440.png'),
    fullPage: true,
    animations: 'disabled'
  })

  // 另一个客户端先做出真实变更；UI 持有的旧 revision 提交必须 409，而不是静默覆盖。
  const memberList = await api(request, 'get', `/knowledge-bases/${kb.id}/members?page=1&page_size=20`)
  const bumped = await api(request, 'put', `/knowledge-bases/${kb.id}/members/${user.id}`, {
    role: 'maintainer',
    expected_revision: memberList.revision
  })
  expect(bumped.revision).toBe(memberList.revision + 1)

  const conflict = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith(`/knowledge-bases/${kb.id}/members/${user.id}`) &&
      response.request().method() === 'PUT'
  )
  await selectRole(page, row.locator('.member-role'), '维护')
  expect((await conflict).status()).toBe(409)
  // ElMessage 挂在 body 上，不在 dialog 内部。
  await expect(page.locator('.el-message').filter({ hasText: '成员版本已变化' })).toBeVisible()
  // 409 后刷新当前列表，但不自动重放：服务端的外部变更已经显示出来。
  await expect(row.locator('.member-role')).toContainText('维护')
  await page.screenshot({
    path: testInfo.outputPath('members-conflict-1440.png'),
    fullPage: true,
    animations: 'disabled'
  })

  // 使用刷新后的 revision 重新提交，角色修改成功。
  const changed = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith(`/knowledge-bases/${kb.id}/members/${user.id}`) &&
      response.request().method() === 'PUT'
  )
  await selectRole(page, row.locator('.member-role'), '读取')
  expect((await changed).status()).toBe(200)
  await expect(row.locator('.member-role')).toContainText('读取')
  await page.screenshot({
    path: testInfo.outputPath('members-recovered-1440.png'),
    fullPage: true,
    animations: 'disabled'
  })

  await dialog.locator('.el-dialog__footer').getByRole('button', { name: '关闭', exact: true }).click()
  await expect(dialog).toBeHidden()

  // 窄屏 + 英文 + 深色：从真实菜单重新打开同一库的成员对话框。
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.theme-button').click()
  await page.getByRole('menuitem', { name: '深色模式' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.locator('.language-button').click()
  await page.getByRole('menuitem', { name: 'English' }).click()
  await expect(page.locator('html')).toHaveAttribute('lang', 'en-US')

  dialog = await openMembersDialog(page, kbName, 'Members', `Members: ${kbName}`)
  const englishRow = dialog.locator('.member-row').filter({ hasText: username })
  await expect(englishRow).toBeVisible()
  const widths = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
  }))
  expect(widths.document).toBeLessThanOrEqual(widths.viewport)
  await page.screenshot({
    path: testInfo.outputPath('members-390-en-dark.png'),
    fullPage: true,
    animations: 'disabled'
  })

  // 移除确认只能承诺删除显式成员授权，不能承诺公开库/owner/admin 一定失去读取权。
  await englishRow.getByRole('button', { name: 'Remove', exact: true }).click()
  const messageBox = page.locator('.el-message-box')
  await expect(messageBox).toContainText('Only this KB grant is deleted')
  const removed = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith(`/knowledge-bases/${kb.id}/members/${user.id}`) &&
      response.request().method() === 'DELETE'
  )
  await page.getByRole('button', { name: 'Remove grant', exact: true }).click()
  expect((await removed).status()).toBe(200)
  await expect(dialog.locator('.member-row').filter({ hasText: username })).toHaveCount(0)
})

test('maintainer can upload a document but cannot manage members', async ({ page, request }, testInfo) => {
  const nonce = uniqueNonce('members-maintainer')
  const username = `m-${nonce}`
  const displayName = `维护成员 ${nonce}`
  const kbName = `维护验收库 ${nonce}`
  const filename = `maintainer-upload-${nonce}.md`

  const user = await createUser(request, username, displayName)
  const kb = await createPrivateKb(request, kbName)
  await addMember(request, kb.id, user.id, 'maintainer')
  const maintainerToken = await loginToken(request, username, PASSWORD)

  // 维护者可以管理成员接口之外的知识库文档，但成员管理接口仍拒绝。
  const forbiddenMembers = await request.get(
    `${apiUrl(manifest)}/api/v1/knowledge-bases/${kb.id}/members`,
    { headers: authHeaders(maintainerToken) }
  )
  expect(forbiddenMembers.status()).toBe(403)

  await uiLoginAs(page, username, PASSWORD)
  await page.goto('/knowledge')
  const card = kbCard(page, kbName)
  await expect(card).toBeVisible()
  // 维护者不是管理员，不应出现知识库管理员菜单。
  await expect(card.locator('.kb-more')).toHaveCount(0)
  await card.getByRole('button', { name: '管理文档', exact: true }).click()
  await page.waitForURL((url) => url.pathname === `/documents/${kb.id}`, { timeout: 30_000 })

  await expect(page.getByRole('button', { name: '上传文档', exact: true })).toBeVisible()
  await expect(page.getByTestId('documents-read-only')).toHaveCount(0)
  await page.locator('input[type=file]').setInputFiles({
    name: filename,
    mimeType: 'text/markdown',
    buffer: Buffer.from(`# 维护者上传验收\n\nMAINTAINER_UPLOAD_${nonce}\n`)
  })

  await expect.poll(
    async () => {
      const response = await request.get(
        `${apiUrl(manifest)}/api/v1/knowledge-bases/${kb.id}/documents?page=1&page_size=20`,
        { headers: authHeaders(maintainerToken) }
      )
      if (!response.ok()) return `HTTP ${response.status()}`
      const body = (await response.json()) as { data?: { items?: Array<{ filename: string; status: string }> } }
      return body.data?.items?.find((item) => item.filename === filename)?.status ?? 'missing'
    },
    { timeout: 60_000, message: 'waiting for the real ingest worker to finish the maintainer upload' }
  ).toBe('ready')

  const uploadedRow = page.locator('.el-table__row').filter({ hasText: filename })
  await expect(uploadedRow).toBeVisible({ timeout: 30_000 })
  await expect(uploadedRow).toContainText('已就绪')
  await page.screenshot({
    path: testInfo.outputPath('members-maintainer-upload-1440.png'),
    fullPage: true,
    animations: 'disabled'
  })
})

test('maintainer capability true -> temporary unknown -> recovered true resumes the held completion once', async ({ page, request }, testInfo) => {
  test.setTimeout(180_000)
  const nonce = uniqueNonce('members-recover')
  const username = `m-${nonce}`
  const displayName = `恢复成员 ${nonce}`
  const kbName = `恢复验收库 ${nonce}`
  const first = `recover-first-${nonce}.md`
  const second = `recover-second-${nonce}.md`

  const user = await createUser(request, username, displayName)
  const kb = await createPrivateKb(request, kbName)
  await addMember(request, kb.id, user.id, 'maintainer')

  await uiLoginAs(page, username, PASSWORD)
  await page.goto(`/documents/${kb.id}`)
  await expect(page.getByRole('button', { name: '上传文档', exact: true })).toBeVisible()

  const controls = await installUploadRaceRoutes(page)
  try {
    await page.locator('input[type=file]').setInputFiles([
      {
        name: first,
        mimeType: 'text/markdown',
        buffer: Buffer.from(`# 恢复一\n\nRECOVER_FIRST_${nonce}\n`)
      },
      {
        name: second,
        mimeType: 'text/markdown',
        buffer: Buffer.from(`# 恢复二\n\nRECOVER_SECOND_${nonce}\n`)
      }
    ])
    // 第一个 /complete 已被 deferred gate 卡住；第二个文件只能停在等待队列。
    await expect.poll(() => controls.counters().completeRequests, { timeout: 30_000 }).toBe(1)
    await expect(page.locator('.queue-card')).toContainText(second)

    // 阻断 KB 列表 GET 并点真实刷新：能力进入未知，上传按钮收起，但队列与在途请求不伪造结果。
    controls.setKbBlocked(true)
    const blockedBeforeRefresh = controls.counters().kbBlocked
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await expect
      .poll(() => controls.counters().kbBlocked, { timeout: 15_000 })
      .toBeGreaterThan(blockedBeforeRefresh)
    await expect(page.getByTestId('documents-capability-error')).toContainText('无法确认维护权限')
    await expect(page.getByRole('button', { name: '上传文档', exact: true })).toHaveCount(0)

    // 放行后拿到真实 200，但能力复核仍失败：回执只能暂存，第二个上传会话不得开始。
    const blockedBeforeRelease = controls.counters().kbBlocked
    controls.releaseComplete()
    await expect
      .poll(() => controls.counters().completeStatuses.join(','), { timeout: 30_000 })
      .toBe('200')
    // response 只到响应头；必须等首个 complete 真正到达 requestfinished/requestfailed 终态。
    await expect
      .poll(() => controls.counters().firstCompleteTerminal, { timeout: 30_000 })
      .toBe('finished')
    await expect
      .poll(() => controls.counters().kbBlocked, { timeout: 15_000 })
      .toBeGreaterThan(blockedBeforeRelease)
    let counters = controls.counters()
    expect(counters.createSessions).toBe(1)
    expect(counters.completeRequests).toBe(1)
    await expect(page.locator('.upload-card')).toContainText('服务端正在校验合并')
    await expect(page.locator('.queue-card')).toContainText(second)
    await page.screenshot({
      path: testInfo.outputPath('members-capability-unknown.png'),
      fullPage: true,
      animations: 'disabled'
    })

    // 解除阻断并真实刷新：能力恢复 true，暂存回执接回，队列继续推进到第二个文件。
    controls.setKbBlocked(false)
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await waitDocumentsReady(request, kb.id, [first, second], 120_000)
    await expect(page.locator('.el-table__row').filter({ hasText: first })).toContainText('已就绪', {
      timeout: 30_000
    })
    await expect(page.locator('.el-table__row').filter({ hasText: second })).toContainText('已就绪', {
      timeout: 30_000
    })
    counters = controls.counters()
    // 每个文件恰好一次会话、一次合并；回执没有导致重复 complete，队列也没有死锁。
    expect(counters.createSessions).toBe(2)
    expect(counters.completeRequests).toBe(2)
    expect(counters.completeStatuses).toEqual([200, 200])
    await expect(page.getByTestId('documents-capability-error')).toHaveCount(0)
    await expect(page.getByRole('button', { name: '上传文档', exact: true })).toBeVisible()
    await page.screenshot({
      path: testInfo.outputPath('members-capability-recovered.png'),
      fullPage: true,
      animations: 'disabled'
    })
  } finally {
    await controls.dispose()
  }
})

test('maintainer capability true -> temporary unknown -> fresh known false stops uploads and drops the held completion', async ({ page, request }, testInfo) => {
  test.setTimeout(150_000)
  const nonce = uniqueNonce('members-demote')
  const username = `m-${nonce}`
  const displayName = `降级成员 ${nonce}`
  const kbName = `降级验收库 ${nonce}`
  const first = `demote-first-${nonce}.md`
  const second = `demote-second-${nonce}.md`

  const user = await createUser(request, username, displayName)
  const kb = await createPrivateKb(request, kbName)
  await addMember(request, kb.id, user.id, 'maintainer')

  await uiLoginAs(page, username, PASSWORD)
  await page.goto(`/documents/${kb.id}`)
  await expect(page.getByRole('button', { name: '上传文档', exact: true })).toBeVisible()

  const controls = await installUploadRaceRoutes(page)
  try {
    await page.locator('input[type=file]').setInputFiles([
      {
        name: first,
        mimeType: 'text/markdown',
        buffer: Buffer.from(`# 降级一\n\nDEMOTE_FIRST_${nonce}\n`)
      },
      {
        name: second,
        mimeType: 'text/markdown',
        buffer: Buffer.from(`# 降级二\n\nDEMOTE_SECOND_${nonce}\n`)
      }
    ])
    await expect.poll(() => controls.counters().completeRequests, { timeout: 30_000 }).toBe(1)
    await expect(page.locator('.queue-card')).toContainText(second)

    // KB 列表 GET 故障制造临时未知，在途 complete 仍被 gate 卡住。
    controls.setKbBlocked(true)
    const blockedBeforeRefresh = controls.counters().kbBlocked
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await expect
      .poll(() => controls.counters().kbBlocked, { timeout: 15_000 })
      .toBeGreaterThan(blockedBeforeRefresh)
    await expect(page.getByTestId('documents-capability-error')).toContainText('无法确认维护权限')

    // 真实 admin API 通过 CAS revision 把维护者改为 reader。
    const memberList = await api(request, 'get', `/knowledge-bases/${kb.id}/members?page=1&page_size=20`)
    const demoted = await api(request, 'put', `/knowledge-bases/${kb.id}/members/${user.id}`, {
      role: 'reader',
      expected_revision: memberList.revision
    })
    expect(demoted.revision).toBe(memberList.revision + 1)
    expect(demoted.member.role).toBe('reader')

    // 恢复 KB 列表并刷新：新鲜 known=false 必须收回能力、停止上传并清空等待队列。
    controls.setKbBlocked(false)
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await expect(page.getByTestId('documents-capability-error')).toContainText('维护权限已失效')
    await expect(page.locator('.upload-card')).toContainText('已暂停')
    await expect(page.locator('.queue-card')).toHaveCount(0)
    await expect(page.getByRole('button', { name: '上传文档', exact: true })).toHaveCount(0)
    expect(controls.counters().createSessions).toBe(1)
    expect(controls.counters().completeRequests).toBe(1)
    await page.screenshot({
      path: testInfo.outputPath('members-capability-revoked.png'),
      fullPage: true,
      animations: 'disabled'
    })

    // 收口被延迟的 complete：放行后等它真正到达终态（撤权后客户端 abort 会走 requestfailed，
    // 若请求已经发出则可能以 requestfinished 结束），再验没有迟到推进第二个会话。
    controls.releaseComplete()
    await expect
      .poll(() => controls.counters().firstCompleteTerminal, { timeout: 30_000 })
      .toMatch(/^(finished|failed)$/)
    const counters = controls.counters()
    expect(counters.createSessions).toBe(1)
    expect(counters.completeRequests).toBe(1)
    await expect(page.locator('.queue-card')).toHaveCount(0)
    await expect(page.locator('.upload-card')).toContainText('已暂停')
    await expect(page.getByTestId('documents-capability-error')).toContainText('维护权限已失效')
    await expect(page.locator('.el-table__row').filter({ hasText: second })).toHaveCount(0)
    const docsResponse = await request.get(
      `${apiUrl(manifest)}/api/v1/knowledge-bases/${kb.id}/documents?page=1&page_size=50`,
      { headers: authHeaders() }
    )
    expect(docsResponse.ok()).toBeTruthy()
    const docsBody = (await docsResponse.json()) as {
      data?: { items?: Array<{ filename: string }> }
    }
    expect(!!docsBody.data?.items?.some((item) => item.filename === second)).toBe(false)
  } finally {
    await controls.dispose()
  }
})

test('reader is read-only, and revoking the grant fails closed on refresh', async ({ page, request }, testInfo) => {
  const nonce = uniqueNonce('members-reader')
  const username = `m-${nonce}`
  const displayName = `读取成员 ${nonce}`
  const kbName = `读取验收库 ${nonce}`

  const user = await createUser(request, username, displayName)
  const kb = await createPrivateKb(request, kbName)
  await addMember(request, kb.id, user.id, 'reader')
  const readerToken = await loginToken(request, username, PASSWORD)

  await uiLoginAs(page, username, PASSWORD)
  await page.goto(`/documents/${kb.id}`)
  await expect(page.getByTestId('documents-read-only')).toBeVisible()
  await expect(page.getByRole('button', { name: '上传文档', exact: true })).toHaveCount(0)
  await expect(page.getByTestId('documents-capability-error')).toHaveCount(0)
  await page.screenshot({
    path: testInfo.outputPath('members-reader-readonly-1440.png'),
    fullPage: true,
    animations: 'disabled'
  })

  // 客户端隐藏不是安全控制：真实写接口仍由服务端拒绝。
  const deniedWrite = await request.post(
    `${apiUrl(manifest)}/api/v1/knowledge-bases/${kb.id}/upload-sessions`,
    {
      headers: authHeaders(readerToken),
      data: { filename: 'reader-denied.txt', file_size: 1, content_sha256: '0'.repeat(64) }
    }
  )
  expect(deniedWrite.status()).toBe(403)
  // 管理员通过真实 API 撤销显式授权；reader 刷新后必须收敛为未知/失效，
  // 不保留旧能力，也不显示上传或维护入口。
  const memberList = await api(request, 'get', `/knowledge-bases/${kb.id}/members?page=1&page_size=20`)
  await api(
    request,
    'delete',
    `/knowledge-bases/${kb.id}/members/${user.id}?expected_revision=${memberList.revision}`
  )

  const refreshed = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith(`/api/v1/knowledge-bases/${kb.id}/documents`) &&
      response.request().method() === 'GET'
  )
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  const listResponse = await refreshed
  expect([403, 404]).toContain(listResponse.status())

  await expect(page.getByTestId('documents-capability-error')).toBeVisible()
  await expect(page.getByTestId('documents-capability-error')).toContainText(/权限已失效|无法确认/)
  await expect(page.getByRole('button', { name: '上传文档', exact: true })).toHaveCount(0)
  await expect(page.getByTestId('documents-read-only')).toHaveCount(0)
  await page.screenshot({
    path: testInfo.outputPath('members-reader-revoked-1440.png'),
    fullPage: true,
    animations: 'disabled'
  })
})

async function waitDocumentsReady(
  request: APIRequestContext,
  kbId: string,
  filenames: string[],
  timeout: number,
  token = adminToken
) {
  await expect
    .poll(
      async () => {
        const response = await request.get(
          `${apiUrl(manifest)}/api/v1/knowledge-bases/${kbId}/documents?page=1&page_size=50`,
          { headers: authHeaders(token) }
        )
        if (!response.ok()) return `HTTP ${response.status()}`
        const body = (await response.json()) as {
          data?: { items?: Array<{ filename: string; status: string }> }
        }
        const statuses = filenames.map(
          (filename) => body.data?.items?.find((item) => item.filename === filename)?.status ?? 'missing'
        )
        return statuses.join(',')
      },
      { timeout, message: `waiting for the real ingest worker: ${filenames.join(', ')}` }
    )
    .toBe(filenames.map(() => 'ready').join(','))
}

/**
 * 说明：本文件只使用真实 API 与真实浏览器，API 仅用于生成独占 nonce 的合成用户/知识库/授权；
 * 成员添加、改角色、移除、上传和维护拒绝都从 UI 或真实 HTTP 鉴权路径验证，不 mock 业务响应。
 */