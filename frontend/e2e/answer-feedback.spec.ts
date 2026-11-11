import { readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { test, expect, type APIRequestContext, type Page } from '@playwright/test'
import { apiLogin, apiUrl, assertProxiedApi, loadManifest, stubControl, uiLogin } from './fixtures'

const manifest = loadManifest()
const stub = stubControl(manifest)
let token = ''
const headers = () => ({ Authorization: `Bearer ${token}` })

async function api(
  request: APIRequestContext,
  method: 'get' | 'post' | 'patch' | 'delete',
  path: string,
  data?: unknown
) {
  const response = await request[method](`${apiUrl(manifest)}/api/v1${path}`, {
    headers: headers(),
    data
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  return (await response.json()).data
}

async function send(page: Page, question: string) {
  await page.getByRole('textbox', { name: '输入问题', exact: true }).fill(question)
  const waiting = page.waitForResponse(
    response => response.url().endsWith('/api/v1/chat/stream') && response.request().method() === 'POST'
  )
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

test.beforeAll(async ({ request }) => {
  token = await apiLogin(request, manifest)
  expect(Object.keys(manifest.workers || {}).sort()).toEqual(['ingest', 'project', 'regression', 'summary'])
})

test.beforeEach(async ({ page }) => {
  await assertProxiedApi(page, manifest)
  await stub.reset()
})

test('answer feedback closed loop: submit, idempotency, redacted list, review and JSONL export', async ({ page, request }) => {
  test.setTimeout(120_000)
  const stamp = Date.now()
  const question = '反馈闭环验收：普通对话回答'
  const firstNote = `首次备注 ${stamp}：引用页码与原文不符`
  const secondNote = `再次备注 ${stamp}：希望补充缺失资料`
  const evidence = '浏览器验收证据：普通对话回答已核对'

  await uiLogin(page)
  // 同一个测试库里可能留有其它轮次/验收的反馈，因此不能用“全库总数 == 1”判断。
  // 记下本用例提交前的基线，后面只断言“本次提交新增了正好一条”。
  const mineBefore = await api(request, 'get', '/feedback/mine?page=1&page_size=1')
  const baselineMine = mineBefore.total as number
  const adminBefore = await api(request, 'get', '/feedback?page=1&page_size=1')
  const baselineAdmin = adminBefore.total as number
  await page.goto('/chat')
  await page.getByText('普通对话', { exact: true }).click()
  await expect(page.getByRole('radio', { name: '普通对话', exact: true })).toBeChecked()

  // 1. 普通对话完成一条真实 SSE 回答。
  const turn = await send(page, question)
  const conversationId = turn.meta.conversation_id as string
  expect(conversationId, '普通对话应返回会话 id').toBeTruthy()
  const conversation = await api(request, 'get', `/chat/conversations/${conversationId}`)
  const answer = [...conversation.messages]
    .reverse()
    .find((message: { role: string; status?: string; content: string }) =>
      message.role === 'assistant' && message.status === 'completed' && message.content.trim())
    ?.content as string | undefined
  expect(answer, '会话详情应包含已完成的助手回答').toBeTruthy()
  const answerText = answer as string

  const lastAssistant = page.locator('.message.assistant').last()
  await expect(lastAssistant).toContainText(question)

  // 2. 第一次反馈：引用有误 + 唯一备注。
  await lastAssistant.hover()
  await lastAssistant.getByRole('button', { name: '反馈这条回答' }).click()
  const dialog = page.getByRole('dialog', { name: '回答反馈' })
  await expect(dialog).toBeVisible()
  await dialog.getByText('引用有误', { exact: true }).click()
  await dialog.locator('#feedback-note').fill(firstNote)
  await dialog.getByRole('button', { name: '提交反馈', exact: true }).click()
  await expect(page.locator('.el-message').filter({ hasText: '反馈已提交' }).last()).toBeVisible()
  await expect(dialog).toBeHidden()

  // 3. 重复提交同一回答：后端幂等，分类与备注保持首次内容。
  await lastAssistant.hover()
  await lastAssistant.getByRole('button', { name: '反馈这条回答' }).click()
  await expect(dialog).toBeVisible()
  await dialog.getByText('缺少资料', { exact: true }).click()
  await dialog.locator('#feedback-note').fill(secondNote)
  await dialog.getByRole('button', { name: '提交反馈', exact: true }).click()
  await expect(page.locator('.el-message').filter({ hasText: '反馈已提交' }).last()).toBeVisible()
  await expect(dialog).toBeHidden()

  const mine = await api(request, 'get', '/feedback/mine?page=1&page_size=10')
  // 重复提交同一回答是幂等的：相对基线只新增这一条。
  expect(mine.total).toBe(baselineMine + 1)
  const mineItem = mine.items.find(
    (row: { note: string }) => row.note === firstNote
  ) as { id: string; category: string; note: string } | undefined
  expect(mineItem, '本人反馈列表应包含刚提交的反馈').toBeTruthy()
  expect(mineItem!.category).toBe('citation_error')
  expect(mineItem!.note).toBe(firstNote)
  const mineNotes = mine.items.map((row: { note: string }) => row.note)
  expect(mineNotes).not.toContain(secondNote)

  // 4. 管理列表默认脱敏：行内只有短 id/分类/状态/备注有无，不含任何正文。
  const adminList = await api(request, 'get', '/feedback?page=1&page_size=10')
  expect(adminList.total).toBe(baselineAdmin + 1)
  const item = adminList.items.find(
    (row: { id: string }) => row.id === mineItem!.id
  )
  expect(item, '反馈列表应包含刚提交的反馈').toBeTruthy()
  expect(item.note_present).toBe(true)
  const shortId = (item.id as string).slice(0, 8)

  const rawListResponse = await request.get(
    `${apiUrl(manifest)}/api/v1/feedback?page=1&page_size=10`,
    { headers: headers() }
  )
  expect(rawListResponse.ok()).toBeTruthy()
  const rawListText = await rawListResponse.text()
  for (const secret of [firstNote, secondNote, question, answerText]) {
    expect(rawListText).not.toContain(secret)
  }

  await page.goto('/feedback')
  const panel = page.locator('.panel')
  const row = panel.getByRole('row').filter({ hasText: shortId })
  await expect(row).toBeVisible()
  await expect(row).toContainText('引用有误')
  await expect(row).toContainText('有')
  await expect(panel).not.toContainText(firstNote)
  await expect(panel).not.toContainText(secondNote)
  await expect(panel).not.toContainText(question)
  await expect(panel).not.toContainText(answerText)

  // 5. 详情抽屉才展示问题、回答和本人备注。
  await row.getByRole('button', { name: '详情', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '反馈详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer).toContainText(question)
  await expect(drawer).toContainText(answerText)
  await expect(drawer).toContainText(firstNote)
  await expect(drawer).not.toContainText(secondNote)

  // 6. 核实：普通对话的“正常回答”不允许指定检索来源。
  await drawer.locator('.review-select').click()
  await page.getByRole('option', { name: '正常回答', exact: true }).click()
  await drawer.locator('#feedback-evidence').fill(evidence)
  const reviewButton = drawer.getByRole('button', { name: '提交核验', exact: true })
  await expect(reviewButton).toBeEnabled()
  await reviewButton.click()
  await expect(page.locator('.el-message').filter({ hasText: '反馈已处理' }).last()).toBeVisible()
  await expect(drawer.getByText('该反馈已处理')).toBeVisible()

  // 7. 显式下载 JSONL，逐行解析并校验评测 case 结构。
  const downloadButton = drawer.getByRole('button', { name: '下载 JSONL', exact: true })
  await expect(downloadButton).toBeEnabled()
  const [download] = await Promise.all([page.waitForEvent('download'), downloadButton.click()])
  const downloadPath = await download.path()
  if (!downloadPath) throw new Error('下载的 JSONL 未落盘')
  const jsonl = readFileSync(downloadPath, 'utf-8')
  const lines = jsonl.split(/\r?\n/).filter(line => line.length > 0)
  expect(lines).toHaveLength(1)
  const exported = JSON.parse(lines[0])
  expect(exported.id).toMatch(/^feedback-/)
  expect(exported.mode).toBe('chat')
  expect(exported.expected_outcome).toBe('answer')
  expect(exported.reviewed).toBe(true)
  expect(Array.isArray(exported.turns)).toBe(true)
  expect(exported.turns[exported.turns.length - 1]).toBe(question)
  expect(exported.question).toBe(question)
  expect(exported.expected_sources).toEqual([])
  expect(typeof exported.verified_by).toBe('string')
  expect(exported.verified_by.length).toBeGreaterThan(0)
  expect(exported.verified_at).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  expect(exported).not.toHaveProperty('expected_http_status')
  expect(jsonl).not.toContain(answerText)

  // 8. 服务端状态同步为已核实。
  const reviewedList = await api(request, 'get', '/feedback?page=1&page_size=10')
  const reviewedItem = reviewedList.items.find(
    (row: { id: string }) => row.id === item.id
  ) as { status: string; reviewed_at: string | null } | undefined
  expect(reviewedItem, '核实后管理列表仍应包含该条反馈').toBeTruthy()
  expect(reviewedItem!.status).toBe('verified')
  expect(reviewedItem!.reviewed_at).toBeTruthy()

  // 9. 回归闭环：在详情抽屉中纳入回归。
  const regressionPanel = drawer.getByTestId('feedback-regression')
  await expect(regressionPanel).toBeVisible()
  await expect(regressionPanel).toContainText('尚未纳入回归')
  const detailAfterReview = await api(request, 'get', `/feedback/${item.id}`)
  const causeNote = `回归原因 ${stamp}：检索未命中正确分块`
  await regressionPanel.getByTestId('regression-cause').click()
  await page.getByRole('option', { name: '检索问题', exact: true }).click()
  await regressionPanel.locator('#regression-cause-note').fill(causeNote)
  const enrollPromise = page.waitForResponse(response =>
    response.url().endsWith(`/api/v1/feedback/${item.id}/regression`)
    && response.request().method() === 'POST'
  )
  await regressionPanel.getByRole('button', { name: '纳入回归', exact: true }).click()
  const enrollResponse = await enrollPromise
  expect(enrollResponse.ok()).toBeTruthy()
  const enrolled = (await enrollResponse.json()).data
  expect(enrolled.cause).toBe('retrieval')
  expect(enrolled.cause_note).toBe(causeNote)
  await expect(regressionPanel).toContainText('检索问题')
  await expect(regressionPanel).toContainText(causeNote)

  // 10. 纳入幂等：同原因同说明再次提交，返回同一回归版本且不产生轮次。
  const idempotentResponse = await request.post(
    `${apiUrl(manifest)}/api/v1/feedback/${item.id}/regression`,
    {
      headers: headers(),
      data: {
        expected_feedback_revision: detailAfterReview.revision,
        cause: 'retrieval',
        cause_note: causeNote
      }
    }
  )
  expect(idempotentResponse.ok()).toBeTruthy()
  const idempotent = (await idempotentResponse.json()).data
  expect(idempotent.revision).toBe(enrolled.revision)
  expect(idempotent.current_attempt_no).toBe(0)

  // 11. 准备 offline：自动下载单条 JSONL，响应与文件都绑定本轮 run id/协议。
  const firstPreparePromise = page.waitForResponse(response =>
    response.url().endsWith(`/api/v1/feedback/${item.id}/regression/runs`)
    && response.request().method() === 'POST'
  )
  const [firstDownload] = await Promise.all([
    page.waitForEvent('download'),
    regressionPanel.getByRole('button', { name: '准备并下载 JSONL', exact: true }).click()
  ])
  const firstPrepared = (await (await firstPreparePromise).json()).data
  expect(firstPrepared.run.attempt_no).toBe(1)
  expect(firstPrepared.run.state).toBe('awaiting_report')
  expect(firstPrepared.run.requested_mode).toBe('offline')
  expect(firstPrepared.case_count).toBe(1)
  expect(firstPrepared.cli_hint).toContain('--require-reviewed')
  const firstDownloadPath = await firstDownload.path()
  if (!firstDownloadPath) throw new Error('回归 JSONL 未落盘')
  const firstCase = JSON.parse(readFileSync(firstDownloadPath, 'utf-8').trim())
  expect(firstCase.id).toBe(`feedback-${item.id}`)
  expect(firstCase.regression_protocol).toBe(1)
  expect(firstCase.regression_mode).toBe('offline')
  await expect(regressionPanel).toContainText('--run-id')
  await expect(regressionPanel.getByTestId('regression-history')).toContainText('#1')

  // 12. 再次准备生成新的 run；旧 run 已不是当前轮，导出必须 409。
  const secondPreparePromise = page.waitForResponse(response =>
    response.url().endsWith(`/api/v1/feedback/${item.id}/regression/runs`)
    && response.request().method() === 'POST'
  )
  const [secondDownload] = await Promise.all([
    page.waitForEvent('download'),
    regressionPanel.getByRole('button', { name: '准备并下载 JSONL', exact: true }).click()
  ])
  const secondPrepared = (await (await secondPreparePromise).json()).data
  expect(secondPrepared.run.attempt_no).toBe(2)
  expect(secondPrepared.run.run_id).not.toBe(firstPrepared.run.run_id)
  expect(await secondDownload.path()).toBeTruthy()
  await expect(regressionPanel.getByTestId('regression-history')).toContainText('#2')

  const staleExport = await request.get(
    `${apiUrl(manifest)}/api/v1/feedback/${item.id}/regression/runs/${firstPrepared.run.run_id}/export`,
    { headers: headers() }
  )
  expect(staleExport.status()).toBe(409)
  expect(await staleExport.text()).toContain('run_not_current')

  // 13. 无效报告必须被服务端拒绝，且不写入任何字段、不改变当前轮状态。
  const invalidReport = {
    name: 'invalid-report.json',
    mimeType: 'application/json',
    buffer: Buffer.from('{"schema":"not-a-regression-report"}')
  }
  await regressionPanel.getByTestId('regression-import-input').setInputFiles(invalidReport)
  await expect(regressionPanel.getByTestId('regression-selected-file')).toContainText('invalid-report.json')
  const importPromise = page.waitForResponse(response =>
    response.url().includes(`/api/v1/feedback/${item.id}/regression/runs/${secondPrepared.run.run_id}/import`)
    && response.request().method() === 'POST'
  )
  await regressionPanel.getByTestId('regression-import-confirm').click()
  const importResponse = await importPromise
  expect(importResponse.status()).toBe(400)
  await expect(regressionPanel.getByTestId('regression-import-error')).toBeVisible()
  await expect(regressionPanel).toContainText('等待报告')
  const afterInvalidImport = await api(request, 'get', `/feedback/${item.id}/regression`)
  expect(afterInvalidImport.current_run.state).toBe('awaiting_report')
  expect(afterInvalidImport.current_run.imported_at).toBeNull()
  expect(afterInvalidImport.revision).toBe(secondPrepared.regression_revision)

  // 14. 切换详情不串数据：再生成一条待处理反馈，验证两个详情各自独立加载。
  const conversationBefore = await api(request, 'get', `/chat/conversations/${conversationId}`)
  const beforeAssistantIds = new Set<string>(
    conversationBefore.messages
      .filter((message: { role: string; id: string }) => message.role === 'assistant')
      .map((message: { id: string }) => message.id)
  )
  const secondQuestion = `切换详情隔离校验 ${stamp}`
  const streamResponse = await request.post(`${apiUrl(manifest)}/api/v1/chat/stream`, {
    headers: { ...headers(), 'Content-Type': 'application/json' },
    data: {
      question: secondQuestion,
      conversation_id: conversationId,
      request_id: randomUUID(),
      mode: 'chat'
    }
  })
  const streamText = await streamResponse.text()
  expect(streamResponse.ok(), streamText).toBeTruthy()
  let secondMessageId = ''
  await expect.poll(async () => {
    const conversation = await api(request, 'get', `/chat/conversations/${conversationId}`)
    const assistant = [...conversation.messages]
      .reverse()
      .find((message: { id: string; role: string; status?: string; content: string }) =>
        message.role === 'assistant'
        && message.status === 'completed'
        && message.content.trim()
        && !beforeAssistantIds.has(message.id)
      )
    secondMessageId = assistant?.id ?? ''
    return secondMessageId
  }, { timeout: 20_000 }).not.toBe('')
  const secondFeedbackNote = `第二条待处理反馈 ${stamp}`
  // POST /feedback 返回 AnswerFeedbackOwnerItem，含精确 id；不能从列表里按分类或状态任取。
  const secondFeedback = await api(request, 'post', '/feedback', {
    message_id: secondMessageId,
    category: 'missing_material',
    note: secondFeedbackNote
  }) as {
    id: string
    message_id: string
    category: string
    note: string
    status: string
  }
  expect(secondFeedback.message_id).toBe(secondMessageId)
  expect(secondFeedback.category).toBe('missing_material')
  expect(secondFeedback.note).toBe(secondFeedbackNote)
  expect(secondFeedback.status).toBe('pending')
  const secondFeedbackId = secondFeedback.id
  expect(secondFeedbackId, '第二条反馈 id 不能与第一条相同').not.toBe(item.id)

  // 再用本用例独占的备注反查本人列表，确认精确 id 与 POST 返回一致。
  const mineAfterSecond = await api(request, 'get', '/feedback/mine?page=1&page_size=10')
  const secondMineItem = mineAfterSecond.items.find(
    (row: { id: string; note: string }) => row.note === secondFeedbackNote
  ) as { id: string } | undefined
  expect(secondMineItem, '本人反馈列表应包含第二条反馈的唯一备注').toBeTruthy()
  expect(secondMineItem!.id).toBe(secondFeedbackId)

  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await page.reload()
  const latestList = await api(request, 'get', '/feedback?page=1&page_size=10')
  expect(latestList.total).toBe(baselineAdmin + 2)
  // 管理列表必须用 POST 返回的精确 id 绑定，不能按“不是第一条”或分类/状态任取。
  const secondItem = latestList.items.find(
    (row: { id: string }) => row.id === secondFeedbackId
  ) as { id: string; category: string; status: string; note_present: boolean } | undefined
  expect(secondItem, '管理列表应包含 POST 返回精确 id 的第二条反馈').toBeTruthy()
  expect(secondItem!.category).toBe('missing_material')
  expect(secondItem!.status).toBe('pending')
  expect(secondItem!.note_present).toBe(true)
  const secondShortId = secondFeedbackId.slice(0, 8)
  const secondRow = panel.getByRole('row').filter({ hasText: secondShortId })
  await expect(row).toBeVisible()
  await expect(secondRow).toBeVisible()

  await row.getByRole('button', { name: '详情', exact: true }).click()
  const firstRegressionPanel = drawer.getByTestId('feedback-regression')
  await expect(firstRegressionPanel).toContainText('检索问题')
  await expect(firstRegressionPanel).toContainText(causeNote)
  await expect(firstRegressionPanel.getByTestId('regression-history')).toContainText('#2')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  await secondRow.getByRole('button', { name: '详情', exact: true }).click()
  await expect(drawer).toBeVisible()
  const secondRegressionPanel = drawer.getByTestId('feedback-regression')
  await expect(secondRegressionPanel).toContainText('尚未纳入回归')
  await expect(secondRegressionPanel.getByTestId('regression-unavailable')).toContainText('反馈尚未核实')
  await expect(secondRegressionPanel).not.toContainText(causeNote)
  await expect(secondRegressionPanel).not.toContainText('#2')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  await row.getByRole('button', { name: '详情', exact: true }).click()
  const reopenedPanel = drawer.getByTestId('feedback-regression')
  await expect(reopenedPanel).toContainText(causeNote)
  await expect(reopenedPanel.getByTestId('regression-history')).toContainText('#2')
})
