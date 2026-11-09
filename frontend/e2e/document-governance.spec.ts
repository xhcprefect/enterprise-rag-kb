import { test, expect, type APIRequestContext, type Locator, type Page } from '@playwright/test'
import { apiLogin, apiUrl, assertProxiedApi, loadManifest, uiLogin } from './fixtures'

const manifest = loadManifest()
let token = ''
let kbId = ''
let kbName = ''
let firstId = ''
let secondId = ''
const firstName = '治理验收 A.md'
const secondName = '治理验收 B.md'
const headers = () => ({ Authorization: `Bearer ${token}` })

async function api(request: APIRequestContext, method: 'get' | 'post' | 'patch', path: string, data?: unknown) {
  const response = await request[method](`${apiUrl(manifest)}/api/v1${path}`, { headers: headers(), data })
  expect(response.ok(), await response.text()).toBeTruthy()
  return (await response.json()).data
}

const documentPath = (id: string) => `/knowledge-bases/${kbId}/documents/${id}`

async function patchGovernance(request: APIRequestContext, id: string, changes: Record<string, unknown>) {
  const current = await api(request, 'get', documentPath(id))
  return api(request, 'patch', `${documentPath(id)}/governance`, {
    expected_revision: current.governance_revision, ...changes
  })
}

async function openGovernance(page: Page) {
  await page.goto(`/documents/${kbId}`)
  const row = page.locator('.el-table__row').filter({ hasText: firstName })
  await row.getByRole('button', { name: '治理', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: `文档治理：${firstName}`, exact: true })
  await expect(dialog).toBeVisible()
  return dialog
}

async function openComparison(page: Page) {
  const governance = await openGovernance(page)
  await governance.getByRole('button', { name: '版本对比', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: `版本对比：${firstName}`, exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('.cmp-candidate', { hasText: secondName })).toBeVisible()
  return { governance, dialog }
}

async function selectCandidateAndCompare(page: Page, dialog: Locator) {
  await dialog.locator('.cmp-candidate', { hasText: secondName }).click()
  const compared = page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.pathname.endsWith(`${documentPath(firstId)}/comparison`)
      && url.searchParams.get('replacement_document_id') === secondId
      && response.request().method() === 'GET'
  })
  await dialog.getByRole('button', { name: '开始对比', exact: true }).click()
  expect((await compared).status()).toBe(200)
  await expect(dialog).toContainText('差异汇总')
}

async function openScope(page: Page) {
  await page.goto('/chat')
  // 真实控件点击展开下拉（Enter 会先选中再关闭；input 被 placeholder span 遮挡，
  // 点击 .kb-select 与用户行为一致）。
  await page.locator('.kb-select').click()
  // option 文案带"… N 篇"计数后缀，不能 exact 匹配；但仍须命中本轮 KB 全名。
  await page.getByRole('option', { name: kbName }).click()
  await page.getByRole('button', { name: '选择文档', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '限定回答依据', exact: true })
  await expect(dialog).toBeVisible()
  return dialog
}

test.beforeAll(async ({ request }) => {
  token = await apiLogin(request, manifest)
  kbName = `治理浏览器验收 ${Date.now()}`
  const kb = await api(request, 'post', '/knowledge-bases', { name: kbName, is_public: true })
  kbId = kb.id
  for (const [name, marker] of [[firstName, 'GOVERNANCE_ALPHA'], [secondName, 'GOVERNANCE_BETA']]) {
    const response = await request.post(`${apiUrl(manifest)}/api/v1/knowledge-bases/${kbId}/documents`, {
      headers: headers(), multipart: { file: { name, mimeType: 'text/markdown',
        buffer: Buffer.from(`# 合成治理资料\n\n${marker}：独立 worker 处理文档，审核后由管理员维护有效期。\n`) } }
    })
    expect(response.ok(), await response.text()).toBeTruthy()
    const uploaded = (await response.json()).data
    const id = uploaded.document?.id || uploaded.id
    expect(typeof id).toBe('string')
    if (name === firstName) firstId = id
    else secondId = id
    await expect.poll(async () => (await api(request, 'get', documentPath(id))).status,
      { timeout: 45_000 }).toBe('ready')
  }
})

test.beforeEach(async ({ page }) => {
  await assertProxiedApi(page, manifest)
  await uiLogin(page)
})

test('real CAS conflict refreshes the form and disabled documents leave the scope picker', async ({ page, request }) => {
  const dialog = await openGovernance(page)
  await dialog.getByPlaceholder('如：行政部 王明').fill('旧客户端草稿')
  await patchGovernance(request, firstId, { maintainer: '另一客户端的新版本' })
  const conflict = page.waitForResponse(response => response.url().endsWith(`${documentPath(firstId)}/governance`)
    && response.request().method() === 'PATCH')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect((await conflict).status()).toBe(409)
  await expect(dialog).toContainText('治理版本已变化')
  await expect(dialog.getByPlaceholder('如：行政部 王明')).toHaveValue('另一客户端的新版本')
  await dialog.getByPlaceholder('如：行政部 王明').fill('已核验负责人')
  const saved = page.waitForResponse(response => response.url().endsWith(`${documentPath(firstId)}/governance`)
    && response.request().method() === 'PATCH')
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  expect((await saved).status()).toBe(200)
  await expect(dialog).toBeHidden()
  expect((await api(request, 'get', documentPath(firstId))).maintainer).toBe('已核验负责人')

  const reopened = await openGovernance(page)
  // el-switch 的原生 input 是 0×0 隐藏元素；像用户一样点击可见的开关核心。
  await reopened.locator('.el-switch').click()
  const disabled = page.waitForResponse(response => response.url().endsWith(`${documentPath(firstId)}/governance`)
    && response.request().method() === 'PATCH')
  await reopened.getByRole('button', { name: '保存', exact: true }).click()
  expect((await disabled).status()).toBe(200)
  await expect(reopened).toBeHidden()
  const current = await api(request, 'get', documentPath(firstId))
  expect(current.status).toBe('ready')
  expect(current.retrievable).toBe(false)
  const scope = await openScope(page)
  // el-checkbox 的原生 input 同样是 0×0 隐藏元素：用可见的勾选项条目断言/操作。
  await expect(scope.locator('.scope-checkbox', { hasText: secondName })).toBeVisible()
  await expect(scope.locator('.scope-checkbox', { hasText: firstName })).toHaveCount(0)
  await patchGovernance(request, firstId, { enabled: true })
})

test('scope confirmation rechecks a document that expires after selection', async ({ page, request }) => {
  const scope = await openScope(page)
  await scope.locator('.scope-checkbox', { hasText: firstName }).click()
  await patchGovernance(request, firstId, { valid_until: '2020-01-01T00:00:00Z' })
  await scope.getByRole('button', { name: '应用范围', exact: true }).click()
  await expect(scope).toContainText('有文档已不再可检索')
  await expect(scope).toBeVisible()
  await scope.getByRole('button', { name: '取消', exact: true }).click()
  await expect(scope).toBeHidden()
  await patchGovernance(request, firstId, { valid_until: null })
})

test('version comparison previews ingested text, writes only after confirmation, and keeps old chunks', async ({ page, request }) => {
  // 1) 取消预览：只关闭对比弹窗，不得发生替代写入
  let opened = await openComparison(page)
  await selectCandidateAndCompare(page, opened.dialog)
  await expect(opened.dialog).toContainText('GOVERNANCE_ALPHA')
  await expect(opened.dialog).toContainText('GOVERNANCE_BETA')
  await expect(opened.dialog).not.toContainText('未发现文本差异')
  await opened.dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(opened.dialog).toBeHidden()
  await expect(opened.governance).toBeVisible()
  const untouched = await api(request, 'get', documentPath(firstId))
  expect(untouched.superseded_by_id).toBeNull()
  expect(untouched.lifecycle_state).toBe('active')
  expect(untouched.retrievable).toBe(true)

  // 2) 正式流程：重新打开对比、选择候选、等待 comparison GET、勾选确认后提交
  await opened.governance.getByRole('button', { name: '版本对比', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: `版本对比：${firstName}`, exact: true })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('.cmp-candidate', { hasText: secondName })).toBeVisible()
  await selectCandidateAndCompare(page, dialog)
  await dialog.locator('.cmp-acknowledge').click()
  const replaced = page.waitForResponse(response => response.url().endsWith(`${documentPath(firstId)}/supersede`)
    && response.request().method() === 'POST')
  await dialog.getByRole('button', { name: '确认替代', exact: true }).click()
  const supersededResponse = await replaced
  expect(supersededResponse.status()).toBe(200)
  const body = supersededResponse.request().postDataJSON()
  expect(body.replacement_document_id).toBe(secondId)
  expect(typeof body.expected_revision).toBe('number')
  expect(body.comparison_fingerprint).toMatch(/^[a-f0-9]{64}$/)
  await expect(dialog).toBeHidden()
  await expect(opened.governance).toBeHidden()

  const superseded = await api(request, 'get', documentPath(firstId))
  expect(superseded.lifecycle_state).toBe('superseded')
  expect(superseded.superseded_by_id).toBe(secondId)
  expect(superseded.retrievable).toBe(false)
  expect(superseded.status).toBe('ready')
  // 旧资料只退出检索：文件、分块与正文仍保留
  const chunks = await api(request, 'get', `${documentPath(firstId)}/chunks?offset=0&limit=50`)
  expect(chunks.total).toBeGreaterThan(0)
  expect(chunks.items.some((item: { content?: string }) =>
    String(item.content || '').includes('GOVERNANCE_ALPHA'))).toBe(true)

  // 3) 已替代文档：治理弹窗仍可打开只读对比，但不能再次提交；重新启用后恢复检索
  const reopened = await openGovernance(page)
  await expect(reopened.getByRole('switch')).toBeDisabled()
  await reopened.getByRole('button', { name: '版本对比', exact: true }).click()
  const readOnly = page.getByRole('dialog', { name: `版本对比：${firstName}`, exact: true })
  await expect(readOnly).toBeVisible()
  await expect(readOnly).toContainText('只能查看对比')
  await expect(readOnly.getByRole('button', { name: '确认替代', exact: true })).toBeDisabled()
  await readOnly.getByRole('button', { name: '取消', exact: true }).click()
  await expect(readOnly).toBeHidden()
  await expect(reopened).toBeVisible()

  const reenabled = page.waitForResponse(response => response.url().endsWith(`${documentPath(firstId)}/reenable`)
    && response.request().method() === 'POST')
  await reopened.getByRole('button', { name: '重新启用', exact: true }).click()
  expect((await reenabled).status()).toBe(200)
  await expect(reopened).toBeHidden()
  expect((await api(request, 'get', documentPath(firstId))).retrievable).toBe(true)
})
