import { randomUUID } from 'node:crypto'
import { expect, test } from '@playwright/test'
import type { APIRequestContext, Locator, Page, Response as PlaywrightResponse } from '@playwright/test'
import {
  apiCall,
  apiGetData,
  apiLogin,
  apiPostData,
  apiUrl,
  assertProxiedApi,
  loadManifest,
  stubControl,
  uiLogin
} from './fixtures'

/**
 * 在线回归作业的真实浏览器验收。
 *
 * 这里不 mock 正常业务：反馈、核实、纳入、人工准备和在线作业都走真实 API/UI，
 * 在线执行由真实 regression worker 消费并调用本轮独立模型替身。只有“响应丢失”
 * 与“响应迟到”两类并发场景用浏览器路由拦截制造；被拦截的写入仍通过
 * route.fetch 真正提交到后端。
 */
const manifest = loadManifest()
const STUB = stubControl(manifest)
let token = ''

interface CreatedFeedback {
  feedbackId: string
  messageId: string
  conversationId: string
  question: string
  answer: string
}

interface Job {
  job_id: string
  feedback_id: string
  batch_id: string | null
  run_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  revision: number
  execution_kind: 'online'
  requested_mode: 'live'
  can_import: false
  result: null | {
    passed: boolean | null
    failures: string[]
    result_sha256: string | null
    machine_sha256: string | null
  }
  review: { status: 'unknown' | 'verified' | 'rejected'; notes: string }
  usage: Record<string, number | string | boolean | null>
  error_code: string | null
}

interface Batch {
  batch_id: string
  request_id: string
  revision: number
  counts: {
    total: number
    queued: number
    running: number
    succeeded: number
    failed: number
    cancelled: number
  }
  jobs: Job[]
}

function apiPath(response: PlaywrightResponse): string {
  return new URL(response.url()).pathname
}

async function apiGet<T = any>(request: APIRequestContext, path: string): Promise<T> {
  return apiGetData<T>(request, manifest, token, path)
}

async function apiPost<T = any>(
  request: APIRequestContext,
  path: string,
  data: unknown
): Promise<T> {
  return apiPostData<T>(request, manifest, token, path, data)
}

/** 发送一条真实 SSE 问题并返回服务器会话正文。 */
async function sendQuestion(
  page: Page,
  question: string
): Promise<{ conversationId: string; answer: string }> {
  await page.getByRole('textbox', { name: '输入问题', exact: true }).fill(question)
  const streamPromise = page.waitForResponse(
    response => apiPath(response) === '/api/v1/chat/stream'
      && response.request().method() === 'POST'
  )
  await page.getByTestId('composer-send').click()
  const stream = await streamPromise
  expect(stream.ok()).toBeTruthy()
  const requestId = (stream.request().postDataJSON() as { request_id: string }).request_id
  await expect(page.getByTestId('composer-send')).toBeVisible({ timeout: 20_000 })

  let completed: any = {}
  await expect.poll(async () => {
    completed = await apiGet(page.request, `/chat/requests/${requestId}`)
    return completed.status
  }, { timeout: 30_000 }).toBe('completed')
  const conversationId = completed.conversation_id as string
  expect(conversationId).toBeTruthy()

  const conversation = await apiGet<any>(page.request, `/chat/conversations/${conversationId}`)
  const answer = [...conversation.messages]
    .reverse()
    .find((message: any) => message.role === 'assistant' && message.status === 'completed' && message.content.trim())
    ?.content as string | undefined
  expect(answer, '会话详情应包含已完成的助手回答').toBeTruthy()
  return { conversationId, answer: answer as string }
}

/** 通过真实回答气泡反馈对话框提交反馈，并等管理列表出现唯一备注。 */
async function submitAnswerFeedback(
  page: Page,
  question: string,
  note: string
): Promise<{ feedbackId: string; messageId: string }> {
  const lastAssistant = page.locator('.message.assistant').last()
  await expect(page.locator('.message.user').last()).toContainText(question)
  await lastAssistant.hover()
  await lastAssistant.getByRole('button', { name: '反馈这条回答' }).click()
  const dialog = page.getByRole('dialog', { name: '回答反馈' })
  await expect(dialog).toBeVisible()
  await dialog.getByText('回答不准确', { exact: true }).click()
  await dialog.locator('#feedback-note').fill(note)
  await dialog.getByRole('button', { name: '提交反馈', exact: true }).click()
  await expect(page.locator('.el-message').filter({ hasText: '反馈已提交' }).last()).toBeVisible()
  await expect(dialog).toBeHidden()

  let feedbackId = ''
  let messageId = ''
  await expect.poll(async () => {
    const mine = await apiGet<any>(page.request, '/feedback/mine?page=1&page_size=100')
    const found = mine.items.find((item: any) => item.note === note)
    feedbackId = found?.id ?? ''
    messageId = found?.message_id ?? ''
    return feedbackId
  }, { timeout: 20_000 }).not.toBe('')
  return { feedbackId, messageId }
}

/** 真实 UI 发普通对话，再通过回答气泡的反馈对话框提交反馈。 */
async function createFeedback(page: Page, label: string): Promise<CreatedFeedback> {
  const stamp = Date.now()
  const question = `在线回归验收 ${stamp}：${label}`
  const note = `在线回归验收反馈 ${stamp}：${label}`
  await page.goto('/chat')
  const normalMode = page.getByRole('radio', { name: '普通对话', exact: true })
  await expect(normalMode).toBeEnabled()
  if (!(await normalMode.isChecked())) {
    await page.locator('.mode-switch').getByText('普通对话', { exact: true }).click()
  }
  await expect(normalMode).toBeChecked()
  const { conversationId, answer } = await sendQuestion(page, question)
  const { feedbackId, messageId } = await submitAnswerFeedback(page, question, note)
  return { feedbackId, messageId, conversationId, question, answer }
}

/**
 * 真实上传/RAG 反馈夹具：新建知识库、上传唯一标记文档、等 ingest worker 完成，
 * 在知识库问答模式中真实检索并提交反馈；返回原回答引用的 document_id。
 */
async function createRagFeedback(
  page: Page,
  request: APIRequestContext,
  label: string
): Promise<CreatedFeedback & { kbId: string; documentId: string }> {
  const stamp = Date.now()
  const kbName = `在线回归来源 ${stamp} ${label}`
  const marker = `ONLINE_REGRESSION_MARKER_${stamp}`
  const question = `${marker} 的验收标记是什么？`
  const note = `在线回归来源反馈 ${stamp}：${label}`
  const kb = await apiPost<any>(request, '/knowledge-bases', { name: kbName, is_public: true })
  const upload = await request.post(
    `${apiUrl(manifest)}/api/v1/knowledge-bases/${kb.id}/documents`,
    {
      headers: { Authorization: `Bearer ${token}` },
      multipart: {
        file: {
          name: `${marker}.md`,
          mimeType: 'text/markdown',
          buffer: Buffer.from(
            `# 在线回归来源资料\n\n${marker} 是本轮浏览器验收的唯一检索标记。\n\n`
            + '验收规则：回答必须引用本文档，且不得引用其它来源。\n'
          )
        }
      }
    }
  )
  expect(upload.ok(), await upload.text()).toBeTruthy()
  const uploaded = (await upload.json()).data
  const documentId = uploaded.document?.id || uploaded.id
  expect(typeof documentId).toBe('string')
  await expect.poll(async () => {
    const document = await apiGet<any>(
      request,
      `/knowledge-bases/${kb.id}/documents/${documentId}`
    )
    return document.status
  }, { timeout: 45_000 }).toBe('ready')

  await page.goto('/chat')
  await expect(page.getByRole('radio', { name: '知识库问答', exact: true })).toBeChecked()
  await page.locator('.kb-select').click()
  await page.getByRole('option', { name: kbName }).click()
  const { conversationId, answer } = await sendQuestion(page, question)
  const { feedbackId, messageId } = await submitAnswerFeedback(page, question, note)
  const detail = await apiGet<any>(request, `/feedback/${feedbackId}`)
  const citedDocumentId = detail.citations
    .map((citation: any) => citation.document_id)
    .find((value: unknown) => typeof value === 'string' && value)
  expect(citedDocumentId, '真实 RAG 反馈必须保留原回答的文档引用').toBeTruthy()
  return {
    feedbackId,
    messageId,
    conversationId,
    question,
    answer,
    kbId: kb.id,
    documentId: citedDocumentId
  }
}

/** 通过真实管理 UI 核实反馈并纳入回归，返回带当前 revision 的回归状态。 */
async function reviewAndEnroll(
  page: Page,
  feedbackId: string,
  options: { requiredPhrase?: string; selectExpectedSources?: boolean } = {}
): Promise<any> {
  await page.goto('/feedback')
  const row = page.locator('.panel').getByRole('row').filter({ hasText: feedbackId.slice(0, 8) })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: '详情', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '反馈详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer.locator('#feedback-evidence')).toBeVisible()

  await drawer.locator('.review-select').click()
  await page.getByRole('option', { name: '正常回答', exact: true }).click()
  if (options.selectExpectedSources) {
    const sources = drawer.locator('.source-options .el-checkbox')
    await expect(sources.first(), 'RAG 反馈必须展示可核对的真实引用来源').toBeVisible()
    const count = await sources.count()
    for (let index = 0; index < count; index += 1) await sources.nth(index).click()
  }
  await drawer.locator('#feedback-required-phrases').fill(
    options.requiredPhrase ?? '在线回归验收通过标记'
  )
  await drawer.locator('#feedback-forbidden-phrases').fill('虚构证明')
  await drawer.locator('#feedback-evidence').fill(
    `合成在线回归验收说明 ${Date.now()}：仅验证作业链路，不代表真实业务结论`
  )
  const reviewPromise = page.waitForResponse(
    response => apiPath(response) === `/api/v1/feedback/${feedbackId}/review`
      && response.request().method() === 'PATCH'
  )
  await drawer.getByRole('button', { name: '提交核验', exact: true }).click()
  expect((await reviewPromise).status()).toBe(200)
  await expect(page.locator('.el-message').filter({ hasText: '反馈已处理' }).last()).toBeVisible()

  const panel = drawer.getByTestId('feedback-regression')
  await expect(panel.getByTestId('regression-enroll')).toBeVisible()
  await panel.getByTestId('regression-cause').click()
  await page.getByRole('option', { name: '生成问题', exact: true }).click()
  await panel.locator('#regression-cause-note').fill(
    `合成在线回归原因说明 ${Date.now()}：验证作业比较与人工核验`
  )
  const enrollPromise = page.waitForResponse(
    response => apiPath(response) === `/api/v1/feedback/${feedbackId}/regression`
      && response.request().method() === 'POST'
  )
  await panel.getByTestId('regression-enroll-submit').click()
  expect((await enrollPromise).status()).toBe(200)
  await expect(panel.getByTestId('regression-enroll')).toBeHidden()
  return apiGet(page.request, `/feedback/${feedbackId}/regression`)
}

async function openFeedbackJobs(
  page: Page,
  feedbackId: string
): Promise<{ drawer: Locator; panel: Locator; jobs: Locator }> {
  await page.goto('/feedback')
  const row = page.locator('.panel').getByRole('row').filter({ hasText: feedbackId.slice(0, 8) })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: '详情', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '反馈详情' })
  await expect(drawer).toBeVisible()
  const panel = drawer.getByTestId('feedback-regression')
  const jobs = panel.getByTestId('regression-jobs')
  await expect(jobs).toBeVisible()
  return { drawer, panel, jobs }
}

/** 点击真实“发起在线回归”并返回创建响应（不把前端状态当后端结果）。 */
async function submitOnlineJob(page: Page, jobs: Locator): Promise<Job> {
  await expect(jobs.getByTestId('regression-job-unavailable')).toHaveCount(0)
  await jobs.getByTestId('regression-job-ack').click()
  await expect(jobs.getByTestId('regression-job-submit')).toBeEnabled()
  const responsePromise = page.waitForResponse(
    response => apiPath(response) === '/api/v1/feedback/regression/jobs'
      && response.request().method() === 'POST'
  )
  await jobs.getByTestId('regression-job-submit').click()
  const response = await responsePromise
  expect(response.status()).toBe(200)
  return (await response.json()).data as Job
}

async function waitJob(
  request: APIRequestContext,
  jobId: string,
  statuses: Job['status'][],
  timeout = 60_000
): Promise<Job> {
  let latest: Job | null = null
  await expect.poll(async () => {
    latest = await apiGet<Job>(request, `/feedback/regression/jobs/${jobId}`)
    return latest.status
  }, { timeout }).toMatch(new RegExp(`^(${statuses.join('|')})$`))
  return latest as unknown as Job
}

async function waitBatch(
  request: APIRequestContext,
  batchId: string,
  predicate: (batch: Batch) => boolean,
  timeout = 60_000
): Promise<Batch> {
  let latest: Batch | null = null
  await expect.poll(async () => {
    latest = await apiGet<Batch>(request, `/feedback/regression/batches/${batchId}`)
    return predicate(latest)
  }, { timeout }).toBe(true)
  return latest as unknown as Batch
}

async function advanceRegressionRevision(
  request: APIRequestContext,
  feedbackId: string,
  expectedRevision: number
): Promise<any> {
  return apiPost(request, `/feedback/${feedbackId}/regression/runs`, {
    expected_revision: expectedRevision,
    requested_mode: 'offline',
    prepare_request_id: randomUUID()
  })
}

async function createOnlineJobViaApi(
  request: APIRequestContext,
  feedbackId: string,
  expectedRevision: number
): Promise<Job> {
  return apiPost<Job>(request, '/feedback/regression/jobs', {
    feedback_id: feedbackId,
    expected_revision: expectedRevision,
    request_id: randomUUID()
  })
}

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void
  const promise = new Promise<void>(done => { resolve = done })
  return { promise, resolve }
}

/** 真实治理 PATCH：禁用引用文档，模拟来源/ACL 失效。 */
async function setDocumentEnabled(
  request: APIRequestContext,
  documentId: string,
  enabled: boolean
): Promise<void> {
  const kbs = await apiGet<any[]>(request, '/knowledge-bases')
  for (const kb of kbs) {
    let page: any
    try {
      page = await apiGet<any>(
        request,
        `/knowledge-bases/${kb.id}/documents?page=1&page_size=100`
      )
    } catch {
      // 项目库可能不允许普通文档列表；继续找可维护的上传库。
      continue
    }
    const document = page.items.find((item: any) => item.id === documentId)
    if (!document) continue
    const result = await apiCall(
      request, manifest, token, 'PATCH',
      `/knowledge-bases/${kb.id}/documents/${documentId}/governance`,
      { expected_revision: document.governance_revision, enabled }
    )
    expect(result.status, JSON.stringify(result.body)).toBe(200)
    return
  }
  throw new Error(`未在当前可见知识库中找到引用文档 ${documentId}`)
}

test.beforeAll(async ({ request }) => {
  token = await apiLogin(request, manifest)
  expect(Object.keys(manifest.workers || {}).sort()).toEqual([
    'ingest', 'project', 'regression', 'summary'
  ])
})

test.beforeEach(async ({ page }) => {
  await assertProxiedApi(page, manifest)
  await STUB.reset()
  await uiLogin(page)
})

test('在线作业成功路径：新调用、可读比较、绑定人审与新作业不继承结论', async ({ page, request }, testInfo) => {
  test.setTimeout(300_000)
  const feedback = await createFeedback(page, 'success')
  await reviewAndEnroll(page, feedback.feedbackId)
  const conversationBefore = JSON.stringify(
    await apiGet<any>(request, `/chat/conversations/${feedback.conversationId}`)
  )
  const { drawer, panel, jobs } = await openFeedbackJobs(page, feedback.feedbackId)

  // 旧的人工准备/导入入口仍然存在；在线作业不会删掉这条兼容闭环。
  const prepareResponsePromise = page.waitForResponse(
    response => apiPath(response) === `/api/v1/feedback/${feedback.feedbackId}/regression/runs`
      && response.request().method() === 'POST'
  )
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    panel.getByTestId('regression-prepare').click()
  ])
  const prepareResponse = await prepareResponsePromise
  expect(prepareResponse.status()).toBe(200)
  const prepared = (await prepareResponse.json()).data
  expect(prepared.run.requested_mode).toBe('offline')
  await download.saveAs(testInfo.outputPath('manual-case.jsonl'))
  await expect(panel.getByTestId('regression-import')).toBeVisible()
  await expect(panel.getByTestId('regression-run-execution-kind')).toContainText('人工准备/导入')

  const job = await submitOnlineJob(page, jobs)
  expect(job.execution_kind).toBe('online')
  expect(job.requested_mode).toBe('live')
  expect(job.can_import).toBe(false)
  expect(job.review.status).toBe('unknown')
  expect(Object.keys(job.usage)).toHaveLength(22)

  const succeeded = await waitJob(request, job.job_id, ['succeeded', 'failed'])
  expect(succeeded.status).toBe('succeeded')
  expect(succeeded.run_id, '在线作业必须是新的 run，而不是复用人工准备轮次').not.toBe(prepared.run.run_id)
  expect(succeeded.usage.attempts as number).toBeGreaterThanOrEqual(1)
  expect(succeeded.usage.input_tokens, '模型替身应报告真实输入 token').toEqual(expect.any(Number))
  expect(succeeded.usage.output_tokens, '模型替身应报告真实输出 token').toEqual(expect.any(Number))
  expect(succeeded.usage.total_cost, '未配置价格时费用必须未知，不能补 0').toBeNull()
  expect(succeeded.usage.priced_attempts).toBe(0)

  const conversationAfter = JSON.stringify(
    await apiGet<any>(request, `/chat/conversations/${feedback.conversationId}`)
  )
  expect(conversationAfter, '在线执行绝不能改写原会话/ChatTurn/Message').toBe(conversationBefore)

  const jobItem = jobs.getByTestId(`regression-job-${job.job_id}`)
  await expect(jobItem).toBeVisible({ timeout: 20_000 })
  await expect(jobItem).toContainText('执行成功')
  await expect(jobItem.getByTestId('regression-job-outcome')).toBeVisible()
  await expect(panel.getByTestId('regression-import-unavailable')).toBeVisible()
  await expect(panel.getByTestId('regression-import')).toHaveCount(0)

  const comparison = await apiGet<any>(request, `/feedback/regression/jobs/${job.job_id}/comparison`)
  expect(comparison.job.job_id).toBe(job.job_id)
  expect(comparison.old.answer).toBe(feedback.answer)
  expect(comparison.new.answer.trim().length).toBeGreaterThan(0)
  expect(comparison.old.message_id).toBe(feedback.messageId)
  expect(comparison.run.execution_kind).toBe('online')
  expect(comparison.run.requested_mode).toBe('live')

  await jobItem.getByTestId(`regression-job-compare-${job.job_id}`).click()
  const comparisonDialog = page.getByTestId('regression-job-comparison-dialog')
  await expect(comparisonDialog).toBeVisible()
  const comparisonPanel = comparisonDialog.getByTestId('regression-job-comparison')
  await expect(comparisonPanel.getByTestId('comparison-old-answer')).toContainText(
    comparison.old.answer.slice(0, 60)
  )
  await expect(comparisonPanel.getByTestId('comparison-new-answer')).toContainText(
    comparison.new.answer.slice(0, 60)
  )
  await expect(comparisonPanel.getByTestId('binding-run-id')).toHaveText(comparison.run.run_id)
  await expect(comparisonPanel.getByTestId('binding-result-sha')).toHaveText(comparison.new.result_sha256)
  await expect(comparisonPanel.getByTestId('comparison-review-status')).toContainText('待人工核验')
  if (comparison.old.citations.length) {
    await expect(comparisonPanel.getByTestId('comparison-old-citations').locator('li'))
      .toHaveCount(comparison.old.citations.length)
  }
  if (comparison.new.citations.length) {
    await expect(comparisonPanel.getByTestId('comparison-new-citations').locator('li'))
      .toHaveCount(comparison.new.citations.length)
  }

  const reviewNotes = `人工核验 ${Date.now()}：已阅读旧/新答案与引用，确认绑定当前结果指纹`
  const reviewDecision = comparisonPanel.getByTestId('review-decision')
  // Element Plus 的 el-radio 把原生 input 藏在 label 内，可见的 span 会拦截对 input 的点击；
  // 点击真实可见的文本由 label 触发选中，再断言对应 radio 已选中，禁止 force 绕过。
  await reviewDecision.getByText('人工已确认', { exact: true }).click()
  await expect(reviewDecision.getByRole('radio', { name: '人工已确认' })).toBeChecked()
  // el-input 把 data-testid 透传到原生 textarea 本身，不能再 locator('textarea') 找子节点。
  await comparisonPanel.getByTestId('review-notes').fill(reviewNotes)
  const reviewResponsePromise = page.waitForResponse(
    response => apiPath(response) === `/api/v1/feedback/regression/jobs/${job.job_id}/review`
      && response.request().method() === 'PUT'
  )
  await comparisonPanel.getByTestId('review-submit').click()
  const reviewResponse = await reviewResponsePromise
  expect(reviewResponse.status()).toBe(200)
  const reviewed = (await reviewResponse.json()).data as Job
  expect(reviewed.review.status).toBe('verified')
  expect(reviewed.review.notes).toBe(reviewNotes)

  await expect(comparisonPanel.getByTestId('comparison-review-status')).toContainText('人工已确认')
  await comparisonDialog.screenshot({
    path: testInfo.outputPath('online-comparison-reviewed.png'),
    animations: 'disabled'
  })
  await page.keyboard.press('Escape')
  await expect(comparisonDialog).toBeHidden()

  // 第二个作业是新作业：review 必须重新从 unknown 开始，不继承上一轮人审。
  const second = await submitOnlineJob(page, jobs)
  expect(second.job_id).not.toBe(job.job_id)
  expect(second.review.status).toBe('unknown')
  const firstReloaded = await apiGet<Job>(request, `/feedback/regression/jobs/${job.job_id}`)
  expect(firstReloaded.review.status).toBe('verified')
  await waitJob(request, second.job_id, ['succeeded', 'failed', 'cancelled'])

  // 重新打开成功作业的比较，在 390px 英文深色下检查无横向溢出。
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.theme-button').click()
  await page.getByRole('menuitem', { name: '深色模式' }).click()
  await page.locator('.language-button').click()
  await page.getByRole('menuitem', { name: 'English' }).click()
  await expect(page.locator('html')).toHaveAttribute('lang', 'en-US')

  const englishRow = page.locator('.panel').getByRole('row').filter({ hasText: feedback.feedbackId.slice(0, 8) })
  await englishRow.getByRole('button').last().click()
  const englishDrawer = page.locator('.el-drawer')
  await expect(englishDrawer).toBeVisible()
  const englishJobs = englishDrawer.getByTestId('regression-jobs')
  await englishJobs.getByTestId('regression-jobs-refresh').click()
  await englishJobs.getByTestId(`regression-job-compare-${job.job_id}`).click()
  await expect(comparisonDialog).toBeVisible()
  await expect(comparisonPanel.getByTestId('comparison-review-status')).toContainText('Human verified')
  const widths = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
  }))
  expect(widths.document).toBeLessThanOrEqual(widths.viewport)
  await comparisonDialog.screenshot({
    path: testInfo.outputPath('online-comparison-390-en-dark.png'),
    animations: 'disabled'
  })
  await englishDrawer.screenshot({
    path: testInfo.outputPath('online-panel-390-en-dark.png'),
    animations: 'disabled'
  })
})

test('排队与运行中的作业都能取消，且不会产生第二个同反馈活动作业', async ({ page, request }, testInfo) => {
  test.setTimeout(300_000)
  const first = await createFeedback(page, 'cancel-running')
  await reviewAndEnroll(page, first.feedbackId)
  const second = await createFeedback(page, 'cancel-queued')
  await reviewAndEnroll(page, second.feedbackId)

  await STUB.configure({ chat_delay_ms: 10_000 })
  try {
    const firstView = await openFeedbackJobs(page, first.feedbackId)
    // 替身 recent 记录形状是 {sequence, kind, at, payload}；先记下提交前的最大序号，
    // 避免前面真实会话产生的历史 chat_stream 冒充这次在线执行的生成请求。
    const baseline = Math.max(
      0,
      ...((await STUB.state()).recent || []).map(entry => Number(entry.sequence) || 0)
    )
    const runningJob = await submitOnlineJob(page, firstView.jobs)
    const running = await waitJob(request, runningJob.job_id, ['running', 'succeeded'], 30_000)
    expect(running.status).toBe('running')
    // 在真正的模型替身上看到这次在线执行的生成请求，而不是只看数据库状态。
    await expect.poll(async () => {
      const state = await STUB.state()
      return (state.recent || []).some(entry => {
        const record = entry as {
          sequence?: number
          kind?: string
          payload?: { user_head?: string }
        }
        return Number(record.sequence || 0) > baseline
          && record.kind === 'chat_stream'
          && String(record.payload?.user_head || '').includes(first.question)
      })
    }, { timeout: 20_000 }).toBe(true)
    await expect(firstView.panel.getByTestId('regression-prepare')).toBeDisabled()
    await expect(firstView.panel.getByTestId('regression-prepare-blocked')).toBeVisible()

    const secondView = await openFeedbackJobs(page, second.feedbackId)
    const queuedJob = await submitOnlineJob(page, secondView.jobs)
    expect(queuedJob.feedback_id).toBe(second.feedbackId)
    const queued = await waitJob(request, queuedJob.job_id, ['queued', 'running'], 30_000)
    expect(queued.status).toBe('queued')

    // 取消排队中的第二个作业：CAS revision 来自真实列表，不在浏览器伪造状态。
    const queuedCancel = page.waitForResponse(
      response => apiPath(response) === `/api/v1/feedback/regression/jobs/${queuedJob.job_id}/cancel`
        && response.request().method() === 'POST'
    )
    await secondView.jobs.getByTestId(`regression-job-cancel-${queuedJob.job_id}`).click()
    expect((await queuedCancel).status()).toBe(200)
    const queuedCancelled = await waitJob(request, queuedJob.job_id, ['cancelled'])
    expect(queuedCancelled.status).toBe('cancelled')

    // 再取消运行中的第一个作业；活动区与历史区都可以发起。
    await page.goto('/feedback')
    const firstRow = page.locator('.panel').getByRole('row').filter({ hasText: first.feedbackId.slice(0, 8) })
    await firstRow.getByRole('button', { name: '详情', exact: true }).click()
    const firstDrawer = page.getByRole('dialog', { name: '反馈详情' })
    await expect(firstDrawer).toBeVisible()
    const active = firstDrawer.getByTestId('regression-job-active')
    await expect(active).toBeVisible({ timeout: 20_000 })
    const runningCancel = page.waitForResponse(
      response => apiPath(response) === `/api/v1/feedback/regression/jobs/${runningJob.job_id}/cancel`
        && response.request().method() === 'POST'
    )
    await active.getByTestId('regression-job-active-cancel').click()
    expect((await runningCancel).status()).toBe(200)
    const runningCancelled = await waitJob(request, runningJob.job_id, ['cancelled'])
    expect(runningCancelled.status).toBe('cancelled')
    await expect(firstDrawer.getByTestId(`regression-job-${runningJob.job_id}`)).toContainText('已取消')
    await expect(firstDrawer.getByTestId('regression-prepare')).toBeEnabled()

    await firstDrawer.screenshot({
      path: testInfo.outputPath('online-cancelled-queued-running.png'),
      animations: 'disabled'
    })
  } finally {
    await STUB.reset()
  }
})

test('批量提交原子冲突：真实 409、零排队，再成功提交并整批取消', async ({ page, request }, testInfo) => {
  test.setTimeout(300_000)
  const first = await createFeedback(page, 'batch-a')
  const firstRegression = await reviewAndEnroll(page, first.feedbackId)
  const second = await createFeedback(page, 'batch-b')
  const secondRegression = await reviewAndEnroll(page, second.feedbackId)

  await page.goto('/feedback')
  await page.getByTestId(`feedback-batch-select-${first.feedbackId}`).click()
  await page.getByTestId(`feedback-batch-select-${second.feedbackId}`).click()
  const batchPanel = page.getByTestId('regression-batch')
  await expect(batchPanel).toBeVisible()
  await expect(batchPanel.getByTestId(`regression-batch-intake-${first.feedbackId}`)).toContainText('可提交')
  await expect(batchPanel.getByTestId(`regression-batch-intake-${second.feedbackId}`)).toContainText('可提交')

  const jobsBefore = await apiGet<any>(request, `/feedback/regression/jobs?page=1&page_size=100`)
  let heldBody: any = null
  const gate = deferred()
  await page.route('**/api/v1/feedback/regression/batches', async route => {
    if (route.request().method() !== 'POST' || heldBody) {
      await route.continue()
      return
    }
    heldBody = route.request().postDataJSON()
    await gate.promise
    await route.continue()
  })
  try {
    const conflictPromise = page.waitForResponse(
      response => apiPath(response) === '/api/v1/feedback/regression/batches'
        && response.request().method() === 'POST'
    )
    await batchPanel.getByTestId('regression-batch-submit').click()
    await expect.poll(() => !!heldBody).toBe(true)
    expect(heldBody.items).toHaveLength(2)

    // 保持原 POST 在途时，用另一个真实 API 操作推进其中一个成员的回归版本。
    const advanced = await advanceRegressionRevision(request, first.feedbackId, firstRegression.revision)
    expect(advanced.regression_revision).toBeGreaterThan(firstRegression.revision)
    gate.resolve()
    const conflict = await conflictPromise
    expect(conflict.status()).toBe(409)

    await expect(batchPanel.getByTestId('regression-batch-error')).toContainText('原子冲突')
    const lookup = await apiCall(
      request, manifest, token, 'GET',
      `/feedback/regression/requests/${heldBody.request_id}`
    )
    expect(lookup.status).toBe(404)
    const jobsAfter = await apiGet<any>(request, `/feedback/regression/jobs?page=1&page_size=100`)
    expect(jobsAfter.total).toBe(jobsBefore.total)
    const firstJobs = await apiGet<any>(
      request,
      `/feedback/regression/jobs?feedback_id=${first.feedbackId}&page=1&page_size=100`
    )
    expect(firstJobs.total).toBe(0)
    // 整批冲突没有新增任何 run：A 只有上面真实准备的那一轮，B 完全未变。
    const firstAfterConflict = await apiGet<any>(request, `/feedback/${first.feedbackId}/regression`)
    const secondAfterConflict = await apiGet<any>(request, `/feedback/${second.feedbackId}/regression`)
    expect(firstAfterConflict.current_attempt_no).toBe(advanced.run.attempt_no)
    expect(secondAfterConflict.current_attempt_no).toBe(secondRegression.current_attempt_no)
    expect(secondAfterConflict.revision).toBe(secondRegression.revision)
    await batchPanel.screenshot({
      path: testInfo.outputPath('batch-atomic-conflict-zero-queued.png'),
      animations: 'disabled'
    })

    // 冲突后前端重新读取了最新 revision；直接重提应成功。
    await expect(batchPanel.getByTestId(`regression-batch-intake-${first.feedbackId}`)).toContainText('可提交')
    await STUB.configure({ chat_delay_ms: 8_000 })
    const successPromise = page.waitForResponse(
      response => apiPath(response) === '/api/v1/feedback/regression/batches'
        && response.request().method() === 'POST'
    )
    await batchPanel.getByTestId('regression-batch-submit').click()
    const success = await successPromise
    expect(success.status()).toBe(200)
    const batch = (await success.json()).data as Batch
    expect(batch.counts.total).toBe(2)
    expect(batch.jobs).toHaveLength(2)
    expect(batch.counts.queued + batch.counts.running).toBeGreaterThanOrEqual(1)
    await expect(batchPanel.getByTestId('regression-batch-counts')).toBeVisible()
    await expect(batchPanel.getByTestId(`regression-batch-job-${batch.jobs[0].job_id}`)).toBeVisible()

    const cancelPromise = page.waitForResponse(
      response => apiPath(response) === `/api/v1/feedback/regression/batches/${batch.batch_id}/cancel`
        && response.request().method() === 'POST'
    )
    await batchPanel.getByTestId('regression-batch-cancel').click()
    expect((await cancelPromise).status()).toBe(200)
    const cancelled = await waitBatch(request, batch.batch_id, current =>
      current.counts.queued === 0 && current.counts.running === 0
    )
    expect(cancelled.counts.cancelled).toBeGreaterThanOrEqual(1)
    await batchPanel.screenshot({
      path: testInfo.outputPath('batch-success-then-cancelled.png'),
      animations: 'disabled'
    })
  } finally {
    gate.resolve()
    await page.unroute('**/api/v1/feedback/regression/batches')
    await STUB.reset()
  }
})

test('在线创建响应丢失：只写一次、只读找回，并在刷新后按原 request id 恢复', async ({ page, request }, testInfo) => {
  test.setTimeout(300_000)
  const feedback = await createFeedback(page, 'lost-response')
  await reviewAndEnroll(page, feedback.feedbackId)
  const { jobs } = await openFeedbackJobs(page, feedback.feedbackId)

  let writes = 0
  let writtenBody: any = null
  let abortedRecovery = false
  await page.route('**/api/v1/feedback/regression/jobs', async route => {
    if (route.request().method() !== 'POST' || writes > 0) {
      await route.continue()
      return
    }
    writes += 1
    writtenBody = route.request().postDataJSON()
    // 让真实后端完成写入，但浏览器收不到响应（等价于响应在网络中丢失）。
    await route.fetch()
    await route.abort('failed')
  })
  await page.route('**/api/v1/feedback/regression/requests/*', async route => {
    if (!abortedRecovery && route.request().method() === 'GET') {
      abortedRecovery = true
      await route.abort('failed')
      return
    }
    await route.continue()
  })
  try {
    await jobs.getByTestId('regression-job-ack').click()
    await expect(jobs.getByTestId('regression-job-submit')).toBeEnabled()
    await jobs.getByTestId('regression-job-submit').click()
    await expect(jobs.getByTestId('regression-job-unconfirmed')).toBeVisible({ timeout: 30_000 })
    expect(writes).toBe(1)
    expect(writtenBody?.request_id).toBeTruthy()
    // 第一次自动找回被网络中断，前端保留原 UUID，未自动重发 POST。
    expect(abortedRecovery).toBe(true)

    await page.unroute('**/api/v1/feedback/regression/requests/*')
    await page.reload()
    const reloadedRow = page.locator('.panel').getByRole('row').filter({ hasText: feedback.feedbackId.slice(0, 8) })
    await expect(reloadedRow).toBeVisible()
    await reloadedRow.getByRole('button', { name: '详情', exact: true }).click()
    const drawer = page.getByRole('dialog', { name: '反馈详情' })
    await expect(drawer).toBeVisible()
    const recoveredJobs = drawer.getByTestId('regression-jobs')
    await expect(recoveredJobs.getByTestId('regression-job-recovered')).toBeVisible({ timeout: 30_000 })
    const lookup = await apiCall(
      request, manifest, token, 'GET',
      `/feedback/regression/requests/${writtenBody.request_id}`
    )
    expect(lookup.status).toBe(200)
    const recovered = lookup.data as any
    expect(recovered.kind).toBe('job')
    expect(recovered.job.feedback_id).toBe(feedback.feedbackId)
    await expect(
      recoveredJobs.getByTestId(`regression-job-${recovered.job.job_id}`)
    ).toBeVisible()
    const list = await apiGet<any>(
      request,
      `/feedback/regression/jobs?feedback_id=${feedback.feedbackId}&page=1&page_size=100`
    )
    expect(list.total, '响应丢失不能变成重复提交').toBe(1)
    expect(writes).toBe(1)
    await drawer.screenshot({
      path: testInfo.outputPath('online-recovered-after-refresh.png'),
      animations: 'disabled'
    })
    await waitJob(request, recovered.job.job_id, ['succeeded', 'failed', 'cancelled'], 90_000)
  } finally {
    await page.unroute('**/api/v1/feedback/regression/jobs')
    await page.unroute('**/api/v1/feedback/regression/requests/*')
  }
})

test('取消已提交但响应丢失：旧 CAS 立即失效，重读前不得再次写入', async ({ page, request }, testInfo) => {
  test.setTimeout(240_000)
  const feedback = await createFeedback(page, 'lost-cancel')
  const regression = await reviewAndEnroll(page, feedback.feedbackId)
  await STUB.configure({ chat_delay_ms: 15_000 })
  const job = await createOnlineJobViaApi(request, feedback.feedbackId, regression.revision)
  await waitJob(request, job.job_id, ['running'], 45_000)

  let cancelWrites = 0
  let cancelStatus = 0
  let failReads = false
  const cancelPost = deferred()
  await page.route('**/api/v1/feedback/regression/jobs/*', async route => {
    if (route.request().method() === 'GET' && failReads) {
      await route.abort('failed')
      return
    }
    await route.continue()
  })
  await page.route('**/api/v1/feedback/regression/jobs?*', async route => {
    if (route.request().method() === 'GET' && failReads) {
      await route.abort('failed')
      return
    }
    await route.continue()
  })
  await page.route('**/api/v1/feedback/regression/jobs/*/cancel', async route => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    cancelWrites += 1
    // 真实提交取消，然后让浏览器收不到响应；随后的重读也被阻断。
    const response = await route.fetch()
    cancelStatus = response.status()
    cancelPost.resolve()
    await route.abort('failed')
  })
  try {
    const { jobs } = await openFeedbackJobs(page, feedback.feedbackId)
    const activeCancel = jobs.getByTestId('regression-job-active-cancel')
    await expect(activeCancel).toBeVisible({ timeout: 30_000 })
    // 先正常读到 running 作业，再开启读取失败并提交取消。
    failReads = true
    await activeCancel.click()
    await cancelPost.promise
    expect(cancelStatus).toBe(200)
    expect(cancelWrites).toBe(1)

    // 未知结果 + 重读失败：旧 revision 对应的取消按钮必须消失，保留显式重读入口。
    await expect(jobs.getByTestId('regression-job-cancel-read')).toBeVisible({ timeout: 30_000 })
    await expect(jobs.getByTestId('regression-job-active-cancel')).toHaveCount(0)
    await expect(jobs.getByTestId('regression-job-cancel-error')).toBeVisible()
    expect(cancelWrites, '重读失败期间不得再次发送旧 CAS').toBe(1)

    failReads = false
    const reread = page.waitForResponse(
      response => apiPath(response) === `/api/v1/feedback/regression/jobs/${job.job_id}`
        && response.request().method() === 'GET'
    )
    await jobs.getByTestId('regression-job-cancel-read').click()
    expect((await reread).status()).toBe(200)
    expect(cancelWrites).toBe(1)
    const cancelled = await waitJob(request, job.job_id, ['cancelled'], 60_000)
    expect(cancelled.status).toBe('cancelled')
    await expect(jobs.getByTestId(`regression-job-${job.job_id}`)).toContainText('已取消')
    await expect(jobs.getByTestId('regression-job-cancel-read')).toHaveCount(0)
    await page.getByRole('dialog', { name: '反馈详情' }).screenshot({
      path: testInfo.outputPath('online-cancel-response-lost-old-cas-blocked.png'),
      animations: 'disabled'
    })
  } finally {
    await page.unroute('**/api/v1/feedback/regression/jobs/*')
    await page.unroute('**/api/v1/feedback/regression/jobs?*')
    await page.unroute('**/api/v1/feedback/regression/jobs/*/cancel')
    await STUB.reset()
  }
})

test('写后响应悬挂时关闭并切换反馈：迟到结果被隔离，重开按原 UUID 只读恢复', async ({ page, request }, testInfo) => {
  test.setTimeout(300_000)
  const first = await createFeedback(page, 'suspended-write-a')
  await reviewAndEnroll(page, first.feedbackId)
  const second = await createFeedback(page, 'suspended-write-b')
  await reviewAndEnroll(page, second.feedbackId)

  const gate = deferred()
  let writes = 0
  let createdJobId = ''
  let writtenRequestId = ''
  await page.route('**/api/v1/feedback/regression/jobs', async route => {
    if (route.request().method() !== 'POST' || writes > 0) {
      await route.continue()
      return
    }
    writes += 1
    writtenRequestId = (route.request().postDataJSON() as { request_id: string }).request_id
    // 后端已真实提交，但响应先挂起，模拟慢网络下的写后切换。
    const response = await route.fetch()
    createdJobId = ((await response.json()).data as Job).job_id
    await gate.promise
    await route.fulfill({ response })
  })
  try {
    const firstView = await openFeedbackJobs(page, first.feedbackId)
    await firstView.jobs.getByTestId('regression-job-ack').click()
    await expect(firstView.jobs.getByTestId('regression-job-submit')).toBeEnabled()
    await firstView.jobs.getByTestId('regression-job-submit').click()
    await expect.poll(() => !!createdJobId).toBe(true)
    expect(writes).toBe(1)

    // 写仍在途时关闭抽屉并打开另一条反馈面板。
    await page.keyboard.press('Escape')
    await expect(firstView.drawer).toBeHidden()
    const secondView = await openFeedbackJobs(page, second.feedbackId)
    await expect(secondView.jobs).toBeVisible()

    gate.resolve()
    await waitJob(request, createdJobId, ['succeeded', 'failed', 'cancelled'], 90_000)
    // 迟到响应不得把 A 的作业/活动轮询注入 B。
    await expect(secondView.jobs.getByTestId(`regression-job-${createdJobId}`)).toHaveCount(0)
    await expect(secondView.jobs.getByTestId('regression-job-active-cancel')).toHaveCount(0)
    const secondJobs = await apiGet<any>(
      request,
      `/feedback/regression/jobs?feedback_id=${second.feedbackId}&page=1&page_size=100`
    )
    expect(secondJobs.total).toBe(0)

    // 重开 A：原 request UUID 只读找回，不重发 POST。
    const reopened = await openFeedbackJobs(page, first.feedbackId)
    await expect(reopened.jobs.getByTestId('regression-job-recovered')).toBeVisible({ timeout: 30_000 })
    await expect(reopened.jobs.getByTestId(`regression-job-${createdJobId}`)).toBeVisible()
    const firstJobs = await apiGet<any>(
      request,
      `/feedback/regression/jobs?feedback_id=${first.feedbackId}&page=1&page_size=100`
    )
    expect(firstJobs.total, '响应迟到不能变成重复提交').toBe(1)
    expect(writes).toBe(1)
    expect(writtenRequestId).toBeTruthy()
    await reopened.drawer.screenshot({
      path: testInfo.outputPath('online-suspended-write-switch-recovered.png'),
      animations: 'disabled'
    })
  } finally {
    gate.resolve()
    await page.unroute('**/api/v1/feedback/regression/jobs')
  }
})

test('执行失败与“执行成功但断言未通过”清楚区分', async ({ page, request }, testInfo) => {
  test.setTimeout(240_000)
  const failedFeedback = await createFeedback(page, 'execution-failure')
  await reviewAndEnroll(page, failedFeedback.feedbackId)
  // 真实 finish_reason=length：按冻结契约属于不可重试的执行失败，不是断言失败。
  await STUB.configure({ chat_truncate: true, chat_delay_ms: 0 })
  try {
    const failedView = await openFeedbackJobs(page, failedFeedback.feedbackId)
    const failedJob = await submitOnlineJob(page, failedView.jobs)
    const failed = await waitJob(request, failedJob.job_id, ['failed'], 90_000)
    expect(failed.error_code).toBeTruthy()
    const failedItem = failedView.jobs.getByTestId(`regression-job-${failedJob.job_id}`)
    await expect(failedItem.getByTestId('regression-job-outcome'))
      .toHaveText('执行本身失败，没有可比较的新答案')
    await expect(failedItem.getByTestId('regression-job-error-code')).toBeVisible()
    await expect(failedItem.getByTestId(`regression-job-compare-${failedJob.job_id}`)).toHaveCount(0)
    await failedView.drawer.screenshot({
      path: testInfo.outputPath('online-execution-failed.png'),
      animations: 'disabled'
    })
  } finally {
    await STUB.reset()
  }

  const assertionFeedback = await createFeedback(page, 'assertion-failure')
  const absentPhrase = `必须出现但模型不会生成的断言短语${Date.now()}`
  await reviewAndEnroll(page, assertionFeedback.feedbackId, { requiredPhrase: absentPhrase })
  // 模型正常结束（非截断），仅因登记的 required_phrase 确实缺席而断言不通过。
  await STUB.configure({ chat_truncate: false, chat_delay_ms: 0 })
  try {
    const assertionView = await openFeedbackJobs(page, assertionFeedback.feedbackId)
    const assertionJob = await submitOnlineJob(page, assertionView.jobs)
    const succeeded = await waitJob(request, assertionJob.job_id, ['succeeded'], 90_000)
    expect(succeeded.result?.passed).toBe(false)
    const item = assertionView.jobs.getByTestId(`regression-job-${assertionJob.job_id}`)
    await expect(item.getByTestId('regression-job-outcome'))
      .toHaveText('执行成功，但机器断言未通过（仍可人工核验）')
    await expect(item.getByTestId(`regression-job-compare-${assertionJob.job_id}`)).toBeVisible()
    await assertionView.drawer.screenshot({
      path: testInfo.outputPath('online-succeeded-assertions-failed.png'),
      animations: 'disabled'
    })
  } finally {
    await STUB.reset()
  }
})

test('比较重新校验来源：文档禁用后 409 隐藏正文与核验，恢复后可重新读取', async ({ page, request }, testInfo) => {
  test.setTimeout(300_000)
  const feedback = await createRagFeedback(page, request, 'source-loss')
  const regression = await reviewAndEnroll(page, feedback.feedbackId, {
    selectExpectedSources: true
  })
  const reviewed = await apiGet<any>(request, `/feedback/${feedback.feedbackId}`)
  expect(reviewed.expected_sources.length, '核实时必须登记真实引用来源').toBeGreaterThan(0)
  const job = await createOnlineJobViaApi(request, feedback.feedbackId, regression.revision)
  const succeeded = await waitJob(request, job.job_id, ['succeeded'], 90_000)
  expect(succeeded.result?.passed).not.toBeNull()

  const comparison = await apiGet<any>(
    request,
    `/feedback/regression/jobs/${job.job_id}/comparison`
  )
  const comparisonDocumentIds = [...comparison.old.citations, ...comparison.new.citations]
    .map((citation: any) => citation.document_id)
    .filter((value: unknown) => typeof value === 'string' && value)
  expect(comparisonDocumentIds).toContain(feedback.documentId)
  const documentId = feedback.documentId

  const { jobs } = await openFeedbackJobs(page, feedback.feedbackId)
  await jobs.getByTestId(`regression-job-compare-${job.job_id}`).click()
  const dialog = page.getByTestId('regression-job-comparison-dialog')
  const panel = dialog.getByTestId('regression-job-comparison')
  await expect(panel.getByTestId('comparison-old-answer')).toBeVisible()

  let disabled = false
  try {
    await setDocumentEnabled(request, documentId as string, false)
    disabled = true
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    const failedReload = page.waitForResponse(
      response => apiPath(response) === `/api/v1/feedback/regression/jobs/${job.job_id}/comparison`
        && response.request().method() === 'GET'
    )
    await jobs.getByTestId(`regression-job-compare-${job.job_id}`).click()
    expect((await failedReload).status()).toBe(409)
    await expect(panel.getByTestId('comparison-error')).toBeVisible()
    await expect(panel.getByTestId('comparison-old-answer')).toHaveCount(0)
    await expect(panel.getByTestId('comparison-new-answer')).toHaveCount(0)
    await expect(panel.getByTestId('regression-job-review')).toHaveCount(0)
    await dialog.screenshot({
      path: testInfo.outputPath('online-comparison-source-lost-hidden.png'),
      animations: 'disabled'
    })

    await setDocumentEnabled(request, documentId as string, true)
    disabled = false
    const okReload = page.waitForResponse(
      response => apiPath(response) === `/api/v1/feedback/regression/jobs/${job.job_id}/comparison`
        && response.request().method() === 'GET'
    )
    await panel.getByTestId('comparison-reload').click()
    expect((await okReload).status()).toBe(200)
    await expect(panel.getByTestId('comparison-old-answer')).toBeVisible()
    await expect(panel.getByTestId('regression-job-review')).toBeVisible()
    await dialog.screenshot({
      path: testInfo.outputPath('online-comparison-source-restored.png'),
      animations: 'disabled'
    })
  } finally {
    if (disabled) await setDocumentEnabled(request, documentId as string, true)
  }
})

test('迟到作业列表响应被反馈切换隔离，不污染另一条反馈面板', async ({ page, request }, testInfo) => {
  test.setTimeout(300_000)
  const first = await createFeedback(page, 'late-a')
  const firstRegression = await reviewAndEnroll(page, first.feedbackId)
  const second = await createFeedback(page, 'late-b')
  await reviewAndEnroll(page, second.feedbackId)
  const firstJob = await createOnlineJobViaApi(request, first.feedbackId, firstRegression.revision)
  await waitJob(request, firstJob.job_id, ['succeeded', 'failed'], 90_000)

  let heldHit = false
  const gate = deferred()
  await page.route('**/api/v1/feedback/regression/jobs?*', async route => {
    const url = new URL(route.request().url())
    if (
      !heldHit
      && url.pathname === '/api/v1/feedback/regression/jobs'
      && url.searchParams.get('feedback_id') === first.feedbackId
    ) {
      heldHit = true
      await gate.promise
    }
    await route.continue()
  })
  try {
    await page.goto('/feedback')
    const firstRow = page.locator('.panel').getByRole('row').filter({ hasText: first.feedbackId.slice(0, 8) })
    await firstRow.getByRole('button', { name: '详情', exact: true }).click()
    await expect.poll(() => heldHit).toBe(true)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog', { name: '反馈详情' })).toBeHidden()

    const secondRow = page.locator('.panel').getByRole('row').filter({ hasText: second.feedbackId.slice(0, 8) })
    await secondRow.getByRole('button', { name: '详情', exact: true }).click()
    const secondJobs = page.getByRole('dialog', { name: '反馈详情' }).getByTestId('regression-jobs')
    await expect(secondJobs.getByTestId('regression-jobs-empty')).toBeVisible()

    gate.resolve()
    await page.waitForTimeout(700)
    await expect(secondJobs.getByTestId('regression-jobs-empty')).toBeVisible()
    await expect(secondJobs.getByTestId(`regression-job-${firstJob.job_id}`)).toHaveCount(0)
    await page.getByRole('dialog', { name: '反馈详情' }).screenshot({
      path: testInfo.outputPath('late-list-isolated.png'),
      animations: 'disabled'
    })
  } finally {
    gate.resolve()
    await page.unroute('**/api/v1/feedback/regression/jobs?*')
  }
})

test('四角色 worker 均由真实 API 报告运行中', async ({ page, request }, testInfo) => {
  test.setTimeout(90_000)
  const readRoles = async () => {
    const snapshot = await apiGet<any>(request, '/operations/workers')
    return (snapshot.roles ?? []).map((role: any) => ({
      role: role.role,
      enabled: role.enabled === true,
      running: role.state === 'running',
      alive: typeof role.alive_count === 'number' && role.alive_count >= 1
    }))
  }
  await expect.poll(readRoles, { timeout: 45_000 }).toEqual([
    { role: 'ingest', enabled: true, running: true, alive: true },
    { role: 'summary', enabled: true, running: true, alive: true },
    { role: 'project', enabled: true, running: true, alive: true },
    { role: 'regression', enabled: true, running: true, alive: true }
  ])

  await page.goto('/operations')
  const cards = page.getByRole('listitem')
  await expect(cards).toHaveCount(4)
  const regressionCard = cards.filter({ hasText: '回归重跑' })
  await expect(regressionCard).toHaveCount(1)
  await expect(regressionCard.locator('.state-label')).toContainText('运行中')
  await page.screenshot({
    path: testInfo.outputPath('four-role-operations.png'),
    fullPage: true,
    animations: 'disabled'
  })
})