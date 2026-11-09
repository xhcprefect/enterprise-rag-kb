import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { basename, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  test,
  expect,
  type APIRequestContext,
  type Locator,
  type Page,
  type Request as PlaywrightRequest,
  type Response as PlaywrightResponse,
  type TestInfo
} from '@playwright/test'
import { apiLogin, apiUrl, assertProxiedApi, loadManifest, uiLogin } from './fixtures'

/**
 * 反馈回归面板的真实浏览器验收：
 *
 * 1. 真实 UI 产生普通对话 SSE 回答、提交反馈、管理员核实并纳入回归；
 * 2. 真实 UI 准备并下载评测 case JSONL；
 * 3. 用本仓库离线 CLI + 纯合成响应生成三份报告（失败 / 声明通过 / 带正文复算通过）；
 * 4. 三份报告都通过原生 file input + UI 导入按钮提交，逐轮核对历史结论；
 * 5. 第三轮先用第一轮旧报告验证拒绝与零写入，再导入正确报告；
 * 6. 关闭重开、刷新、手机宽度下仍保留历史，并输出可视图。
 *
 * 报告只由 CLI 生成，不在此文件手写任何指纹或断言摘要。
 */
const HERE = dirname(fileURLToPath(import.meta.url))
const BACKEND_DIR = resolve(HERE, '../../backend')
// 与 API/worker 相同的后端虚拟环境解释器；PATH 上的全局 python 未必装齐依赖。
const VENV_PYTHON = process.platform === 'win32'
  ? resolve(BACKEND_DIR, '.venv/Scripts/python.exe')
  : resolve(BACKEND_DIR, '.venv/bin/python')
const PYTHON = process.env.RAG_E2E_PYTHON || (existsSync(VENV_PYTHON) ? VENV_PYTHON : 'python')

const manifest = loadManifest()
let token = ''

async function apiGet<T = any>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${apiUrl(manifest)}/api/v1${path}`, {
    headers: { Authorization: `Bearer ${token}` }
  })
  expect(response.ok(), await response.text()).toBeTruthy()
  return (await response.json()).data as T
}

/** 真实 UI 发送一条普通对话问题，等 SSE 请求完成且服务端状态为 completed。 */
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
    completed = await apiGet(page.request, `/chat/requests/${payload.request_id}`)
    return completed.status
  }).toBe('completed')
  return { meta: completed, payload }
}

function readSingleCase(casePath: string): Record<string, any> {
  const rows = readFileSync(casePath, 'utf-8').split(/\r?\n/).filter(line => line.trim().length > 0)
  expect(rows, '下载的回归 JSONL 应恰好包含一条 case').toHaveLength(1)
  return JSON.parse(rows[0])
}

function writeResponses(responsesPath: string, caseId: string, answer: string): void {
  const row = { id: caseId, answer, citations: [], trace: {} }
  writeFileSync(responsesPath, `${JSON.stringify(row)}\n`, 'utf-8')
}

function readReport(reportPath: string): Record<string, any> {
  return JSON.parse(readFileSync(reportPath, 'utf-8'))
}

/** 只用本仓库离线 CLI 与纯合成响应生成报告；不访问模型或数据库。 */
function runEvaluation(
  args: string[],
  label: string
): { status: number | null; output: string } {
  const result = spawnSync(
    PYTHON,
    ['-X', 'utf8', '-m', 'scripts.evaluate_rag', ...args],
    {
      cwd: BACKEND_DIR,
      env: { ...process.env, RAG_DISABLE_DOTENV: '1', LOG_STDOUT_ONLY: 'true' },
      encoding: 'utf-8',
      timeout: 120_000,
      maxBuffer: 10 * 1024 * 1024
    }
  )
  const output = `${result.stdout ?? ''}\n${result.stderr ?? ''}`.trim()
  expect(result.error, `${label} CLI 启动失败: ${output}`).toBeUndefined()
  expect(result.signal, `${label} CLI 被信号终止: ${output}`).toBeNull()
  return { status: result.status, output }
}

interface PreparedRun {
  run: {
    run_id: string
    attempt_no: number
    requested_mode: string
    state: string
    validation_level?: string | null
    passed?: boolean | null
  }
  regression_revision: number
  cli_hint: string
  filename: string
  casePath: string
}

/** 点击“准备并下载 JSONL”，同时捕获真实下载与准备响应。 */
async function prepareRun(
  page: Page,
  panel: Locator,
  feedbackId: string,
  testInfo: TestInfo,
  label: string
): Promise<PreparedRun> {
  const preparePromise = page.waitForResponse(
    response => response.url().endsWith(`/api/v1/feedback/${feedbackId}/regression/runs`)
      && response.request().method() === 'POST'
  )
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    panel.getByTestId('regression-prepare').click()
  ])
  const response = await preparePromise
  expect(response.status()).toBe(200)
  const data = (await response.json()).data
  const casePath = testInfo.outputPath(`${label}-case.jsonl`)
  await download.saveAs(casePath)
  return { ...data, casePath } as PreparedRun
}

/** 原生 file input 选择本地报告，再点击真实 UI 导入按钮。 */
async function importReport(
  page: Page,
  panel: Locator,
  feedbackId: string,
  runId: string,
  reportPath: string
): Promise<PlaywrightResponse> {
  await panel.getByTestId('regression-import-input').setInputFiles(reportPath)
  await expect(panel.getByTestId('regression-selected-file')).toContainText(basename(reportPath))
  const importPromise = page.waitForResponse(
    response => response.url().includes(
      `/api/v1/feedback/${feedbackId}/regression/runs/${runId}/import`
    ) && response.request().method() === 'POST'
  )
  const confirm = panel.getByTestId('regression-import-confirm')
  await expect(confirm).toBeEnabled()
  await confirm.click()
  return importPromise
}

/**
 * 仅用于失败诊断的 body 读取：成功路径绝不读取调试正文。
 * Edge CDP 可能在后续请求后驱逐 inspector cache；这里只吞掉明确的取 body 失败，
 * 其他异常照常抛出，业务断言不因此放宽。
 */
async function responseTextIfAvailable(response: PlaywrightResponse): Promise<string | null> {
  try {
    return await response.text()
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    if (message.includes('Network.getResponseBody') || message.toLowerCase().includes('evicted')) {
      return null
    }
    throw error
  }
}

/** 截图前先真实滚动到回归面板（可再滚到具体轮次），截取可见的抽屉，保留原生 UI。 */
async function capturePanel(
  drawer: Locator,
  panel: Locator,
  testInfo: TestInfo,
  name: string,
  target: Locator = panel
): Promise<void> {
  await panel.scrollIntoViewIfNeeded()
  await target.scrollIntoViewIfNeeded()
  await expect(target).toBeVisible()
  await drawer.screenshot({ path: testInfo.outputPath(name), animations: 'disabled' })
}

test.beforeAll(async ({ request }) => {
  token = await apiLogin(request, manifest)
  expect(Object.keys(manifest.workers || {}).sort()).toEqual(['ingest', 'project', 'summary'])
})

test.beforeEach(async ({ page }) => {
  // 只证明浏览器同源代理到本轮 API；不 reset 共享替身，避免打断并行人工验收。
  await assertProxiedApi(page, manifest)
})

test('反馈回归面板成功路径：失败报告、声明通过、复算通过、旧报告拒绝与可视验收', async ({ page, request }, testInfo) => {
  test.setTimeout(240_000)
  mkdirSync(testInfo.outputDir, { recursive: true })

  const stamp = Date.now()
  const question = `反馈回归验收 ${stamp}：普通对话回答`
  const feedbackNote = `回归验收反馈 ${stamp}：普通对话回答需要合成回归复跑`
  const causeNote = `合成回归验收原因说明 ${stamp}：仅验证离线回放闭环，不代表真实业务结论`
  const evidenceNote = `合成验收说明 ${stamp}：仅用于本轮回归面板验收，不涉及真实业务资料`
  const requiredPhrase = '回归验收通过标记'
  const forbiddenPhrase = '虚构证明'

  await uiLogin(page)
  await page.goto('/chat')
  const chatModeRadio = page.getByRole('radio', { name: '普通对话', exact: true })
  await expect(chatModeRadio).toBeEnabled()
  await page.locator('.mode-switch').getByText('普通对话', { exact: true }).click()
  await expect(chatModeRadio).toBeChecked()

  // 1. 真实 UI 产生一条普通对话 SSE 回答。
  const turn = await send(page, question)
  const conversationId = turn.meta.conversation_id as string
  expect(conversationId, '普通对话应返回会话 id').toBeTruthy()
  const conversation = await apiGet(request, `/chat/conversations/${conversationId}`)
  const answer = [...conversation.messages]
    .reverse()
    .find((message: { role: string; status?: string; content: string }) =>
      message.role === 'assistant' && message.status === 'completed' && message.content.trim())
    ?.content as string | undefined
  expect(answer, '会话详情应包含已完成的助手回答').toBeTruthy()

  const lastAssistant = page.locator('.message.assistant').last()
  await expect(lastAssistant).toContainText(question)

  // 2. 真实 UI 提交反馈。
  await lastAssistant.hover()
  await lastAssistant.getByRole('button', { name: '反馈这条回答' }).click()
  const feedbackDialog = page.getByRole('dialog', { name: '回答反馈' })
  await expect(feedbackDialog).toBeVisible()
  await feedbackDialog.getByText('回答不准确', { exact: true }).click()
  await feedbackDialog.locator('#feedback-note').fill(feedbackNote)
  await feedbackDialog.getByRole('button', { name: '提交反馈', exact: true }).click()
  await expect(page.locator('.el-message').filter({ hasText: '反馈已提交' }).last()).toBeVisible()
  await expect(feedbackDialog).toBeHidden()

  let feedbackId = ''
  await expect.poll(async () => {
    const mine = await apiGet(request, '/feedback/mine?page=1&page_size=100')
    feedbackId = mine.items.find((item: { note: string }) => item.note === feedbackNote)?.id ?? ''
    return feedbackId
  }, { timeout: 20_000 }).not.toBe('')
  const shortId = feedbackId.slice(0, 8)

  // 3. 反馈管理 UI 核实：正常回答 + 必须/禁止表述 + 合成证据说明。
  await page.goto('/feedback')
  const panel = page.locator('.panel')
  const row = panel.getByRole('row').filter({ hasText: shortId })
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: '详情', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '反馈详情' })
  await expect(drawer).toBeVisible()
  await expect(drawer.locator('#feedback-evidence')).toBeVisible()

  await drawer.locator('.review-select').click()
  await page.getByRole('option', { name: '正常回答', exact: true }).click()
  await drawer.locator('#feedback-required-phrases').fill(requiredPhrase)
  await drawer.locator('#feedback-forbidden-phrases').fill(forbiddenPhrase)
  await drawer.locator('#feedback-evidence').fill(evidenceNote)
  const reviewPromise = page.waitForResponse(
    response => response.url().endsWith(`/api/v1/feedback/${feedbackId}/review`)
      && response.request().method() === 'PATCH'
  )
  await drawer.getByRole('button', { name: '提交核验', exact: true }).click()
  const reviewResponse = await reviewPromise
  expect(reviewResponse.status()).toBe(200)
  await expect(page.locator('.el-message').filter({ hasText: '反馈已处理' }).last()).toBeVisible()
  await expect(drawer.getByText('该反馈已处理，不能再次核实。')).toBeVisible()

  // 4. 新回归面板选择“生成问题”原因并填说明，真实按钮纳入。
  const regressionPanel = drawer.getByTestId('feedback-regression')
  await expect(regressionPanel.getByTestId('regression-enroll')).toBeVisible()
  await regressionPanel.getByTestId('regression-cause').click()
  await page.getByRole('option', { name: '生成问题', exact: true }).click()
  await regressionPanel.locator('#regression-cause-note').fill(causeNote)
  const enrollPromise = page.waitForResponse(
    response => response.url().endsWith(`/api/v1/feedback/${feedbackId}/regression`)
      && response.request().method() === 'POST'
  )
  await regressionPanel.getByTestId('regression-enroll-submit').click()
  const enrollResponse = await enrollPromise
  expect(enrollResponse.status()).toBe(200)
  await expect(regressionPanel).toContainText('生成问题')
  await expect(regressionPanel).toContainText(causeNote)
  await expect(regressionPanel.getByTestId('regression-enroll')).toBeHidden()
  await expect(regressionPanel.getByTestId('regression-history-empty')).toBeVisible()
  await capturePanel(drawer, regressionPanel, testInfo, 'enrolled.png')

  // 5. 第一轮：准备并真实下载 JSONL，用合成“缺少标记”回答跑出失败报告（exit 1，无正文）。
  const round1 = await prepareRun(page, regressionPanel, feedbackId, testInfo, 'round-1')
  expect(round1.run.attempt_no).toBe(1)
  expect(round1.run.requested_mode).toBe('offline')
  expect(round1.run.state).toBe('awaiting_report')
  expect(round1.cli_hint).toContain('--run-id')
  await expect(regressionPanel.getByTestId('regression-cli-hint')).toContainText('--run-id')
  await expect(regressionPanel.getByTestId('regression-history')).toContainText('#1')
  await expect(regressionPanel.getByTestId('regression-run-1')).toContainText(round1.run.run_id)

  const case1 = readSingleCase(round1.casePath)
  expect(case1.id).toBe(`feedback-${feedbackId}`)
  expect(case1.regression_protocol).toBe(1)
  expect(case1.regression_mode).toBe('offline')
  expect(case1.expected_outcome).toBe('answer')
  expect(case1.expected_sources).toEqual([])
  expect(case1.required_phrases).toEqual([requiredPhrase])
  expect(case1.forbidden_phrases).toEqual([forbiddenPhrase])

  const responses1 = testInfo.outputPath('round-1-responses.jsonl')
  const report1Path = testInfo.outputPath('round-1-report.json')
  writeResponses(responses1, case1.id, '合成离线回放回答：本轮故意缺少约定验收标记。')
  const run1 = runEvaluation([
    '--dataset', round1.casePath,
    '--responses', responses1,
    '--run-id', round1.run.run_id,
    '--require-reviewed',
    '--output', report1Path
  ], '第一轮')
  expect(run1.status, `第一轮应 exit 1，实际输出:\n${run1.output}`).toBe(1)
  const report1 = readReport(report1Path)
  expect(report1.run_id).toBe(round1.run.run_id)
  expect(report1.mode).toBe('offline')
  expect(report1.passed).toBe(0)
  expect(report1.results).toHaveLength(1)
  expect(report1.results[0].failures).toContain('required_phrase_missing')
  expect(report1.results[0]).not.toHaveProperty('content')

  // 5b. 原生 file input 前端校验：非法扩展名与超过 2 MiB 都在本地拦截，不发导入请求。
  const invalidExtension = testInfo.outputPath('round-1-invalid.txt')
  writeFileSync(invalidExtension, '合成非 JSON 报告\n', 'utf-8')
  const oversizeReport = testInfo.outputPath('round-1-oversize.json')
  writeFileSync(oversizeReport, Buffer.alloc(2 * 1024 * 1024 + 1, 'a'))
  const importRequests: string[] = []
  const recordImportRequest = (request: PlaywrightRequest) => {
    if (request.method() !== 'POST') return
    // 实际导入 URL 带 ?expected_revision=...，必须按 pathname 匹配，不能对完整 URL 用 endsWith。
    const pathname = new URL(request.url()).pathname
    if (
      pathname.startsWith(`/api/v1/feedback/${feedbackId}/regression/`)
      && pathname.endsWith('/import')
    ) {
      importRequests.push(request.url())
    }
  }
  page.on('request', recordImportRequest)
  try {
    const fileError = regressionPanel.getByTestId('regression-file-error')
    const importConfirm = regressionPanel.getByTestId('regression-import-confirm')
    const selectedFile = regressionPanel.getByTestId('regression-selected-file')

    await regressionPanel.getByTestId('regression-import-input').setInputFiles(invalidExtension)
    await expect(fileError).toBeVisible()
    await expect(fileError).toContainText('请选择 .json 文件')
    await expect(importConfirm).toBeDisabled()
    await expect(selectedFile).toBeHidden()
    await page.waitForTimeout(400)
    expect(importRequests, '非法扩展名不应发出导入请求').toEqual([])

    await regressionPanel.getByTestId('regression-import-input').setInputFiles(oversizeReport)
    await expect(fileError).toBeVisible()
    await expect(fileError).toContainText('文件不能超过 2 MiB')
    await expect(fileError).toContainText('2,097,152')
    await expect(importConfirm).toBeDisabled()
    await expect(selectedFile).toBeHidden()
    await page.waitForTimeout(400)
    expect(importRequests, '超过 2 MiB 的报告不应发出导入请求').toEqual([])

    const afterLocalRejects = await apiGet(request, `/feedback/${feedbackId}/regression`)
    expect(afterLocalRejects.revision).toBe(round1.regression_revision)
    expect(afterLocalRejects.current_run.state).toBe('awaiting_report')
    expect(afterLocalRejects.current_run.imported_at).toBeNull()
    expect(afterLocalRejects.current_run.report_sha256).toBeNull()

    const import1 = await importReport(page, regressionPanel, feedbackId, round1.run.run_id, report1Path)
    expect(import1.status()).toBe(200)
    await expect(page.locator('.el-message').filter({ hasText: '评测报告已导入' }).last()).toBeVisible()

    // 正向校验：同一个监听器必须在合法导入时捕获恰好一个请求，证明上面的零请求结论不是死匹配。
    await expect.poll(() => importRequests.length).toBe(1)
    const capturedImport = new URL(importRequests[0])
    expect(capturedImport.pathname).toBe(
      `/api/v1/feedback/${feedbackId}/regression/runs/${round1.run.run_id}/import`
    )
    expect(capturedImport.searchParams.get('expected_revision')).toBe(
      String(round1.regression_revision)
    )
  } finally {
    page.off('request', recordImportRequest)
  }

  const run1Item = regressionPanel.getByTestId('regression-run-1')
  await expect(run1Item).toContainText(round1.run.run_id)
  await expect(run1Item).toContainText('断言未通过')
  await expect(run1Item).toContainText('失败')
  await expect(run1Item).toContainText('required_phrase_missing')
  await expect(run1Item).toContainText('一致性校验')
  await expect(run1Item).toContainText('离线回放')
  const regressionAfter1 = await apiGet(request, `/feedback/${feedbackId}/regression`)
  expect(regressionAfter1.current_run.state).toBe('imported')
  expect(regressionAfter1.current_run.validation_level).toBe('consistency_only')
  expect(regressionAfter1.current_run.passed).toBe(false)
  expect(regressionAfter1.current_run.failures).toEqual(['required_phrase_missing'])
  expect(regressionAfter1.current_run.requested_mode).toBe('offline')
  await capturePanel(drawer, regressionPanel, testInfo, 'round-1-failed.png', run1Item)

  // 6. 第二轮：合成“包含标记”回答，默认无正文报告 exit 0，显示“报告声明通过（未重算断言）”。
  const round2 = await prepareRun(page, regressionPanel, feedbackId, testInfo, 'round-2')
  expect(round2.run.attempt_no).toBe(2)
  expect(round2.run.requested_mode).toBe('offline')
  await expect(regressionPanel.getByTestId('regression-run-2')).toContainText(round2.run.run_id)
  const case2 = readSingleCase(round2.casePath)
  expect(case2.id).toBe(case1.id)
  const responses2 = testInfo.outputPath('round-2-responses.jsonl')
  const report2Path = testInfo.outputPath('round-2-report.json')
  writeResponses(responses2, case2.id, '合成离线回放回答：回归验收通过标记已出现。')
  const run2 = runEvaluation([
    '--dataset', round2.casePath,
    '--responses', responses2,
    '--run-id', round2.run.run_id,
    '--require-reviewed',
    '--output', report2Path
  ], '第二轮')
  expect(run2.status, `第二轮应 exit 0，实际输出:\n${run2.output}`).toBe(0)
  const report2 = readReport(report2Path)
  expect(report2.run_id).toBe(round2.run.run_id)
  expect(report2.mode).toBe('offline')
  expect(report2.passed).toBe(1)
  expect(report2.results[0].failures).toEqual([])
  expect(report2.results[0]).not.toHaveProperty('content')

  const import2 = await importReport(page, regressionPanel, feedbackId, round2.run.run_id, report2Path)
  expect(import2.status()).toBe(200)
  await expect(page.locator('.el-message').filter({ hasText: '评测报告已导入' }).last()).toBeVisible()
  const run2Item = regressionPanel.getByTestId('regression-run-2')
  await expect(run2Item).toContainText(round2.run.run_id)
  await expect(run2Item).toContainText('报告声明通过（未重算断言）')
  await expect(run2Item).toContainText('一致性校验')
  await expect(run2Item).toContainText('离线回放')
  await expect(run2Item).toContainText('通过')
  await expect(run2Item).not.toContainText('required_phrase_missing')
  const regressionAfter2 = await apiGet(request, `/feedback/${feedbackId}/regression`)
  expect(regressionAfter2.current_run.state).toBe('imported')
  expect(regressionAfter2.current_run.validation_level).toBe('consistency_only')
  expect(regressionAfter2.current_run.passed).toBe(true)
  expect(regressionAfter2.current_run.failures).toEqual([])
  expect(regressionAfter2.current_run.requested_mode).toBe('offline')
  await capturePanel(drawer, regressionPanel, testInfo, 'round-2-consistency-passed.png', run2Item)

  // 7. 第三轮：先准备，再生成带正文报告（exit 0，--include-content）。
  const round3 = await prepareRun(page, regressionPanel, feedbackId, testInfo, 'round-3')
  expect(round3.run.attempt_no).toBe(3)
  expect(round3.run.requested_mode).toBe('offline')
  await expect(regressionPanel.getByTestId('regression-run-3')).toContainText(round3.run.run_id)
  const case3 = readSingleCase(round3.casePath)
  expect(case3.id).toBe(case1.id)
  const responses3 = testInfo.outputPath('round-3-responses.jsonl')
  const report3Path = testInfo.outputPath('round-3-report.json')
  writeResponses(responses3, case3.id, '合成离线回放回答：回归验收通过标记已出现。')
  const run3 = runEvaluation([
    '--dataset', round3.casePath,
    '--responses', responses3,
    '--run-id', round3.run.run_id,
    '--require-reviewed',
    '--include-content',
    '--output', report3Path
  ], '第三轮')
  expect(run3.status, `第三轮应 exit 0，实际输出:\n${run3.output}`).toBe(0)
  const report3 = readReport(report3Path)
  expect(report3.run_id).toBe(round3.run.run_id)
  expect(report3.mode).toBe('offline')
  expect(report3.passed).toBe(1)
  expect(report3.results[0]).toHaveProperty('content')
  expect(report3.results[0].content.questions).toEqual(case3.turns)

  // 8. 先上传第一轮旧报告：服务端必须拒绝，且当前轮不写入任何摘要。
  const staleImport = await importReport(
    page, regressionPanel, feedbackId, round3.run.run_id, report1Path
  )
  expect(staleImport.status()).toBe(400)
  const importError = regressionPanel.getByTestId('regression-import-error')
  await expect(importError).toBeVisible()
  await expect(importError).toContainText('report_run_mismatch')
  const staleBody = await responseTextIfAvailable(staleImport)
  if (staleBody !== null) {
    expect(staleBody).toContain('report_run_mismatch')
  }
  const afterStale = await apiGet(request, `/feedback/${feedbackId}/regression`)
  expect(afterStale.revision).toBe(round3.regression_revision)
  expect(afterStale.current_run.state).toBe('awaiting_report')
  expect(afterStale.current_run.imported_at).toBeNull()
  expect(afterStale.current_run.report_sha256).toBeNull()
  expect(afterStale.current_run.report_mode).toBeNull()
  expect(afterStale.current_run.validation_level).toBeNull()
  expect(afterStale.current_run.passed).toBeNull()
  expect(afterStale.current_run.failures).toEqual([])
  expect(afterStale.current_run.outcome).toBeNull()
  expect(afterStale.current_run.http_status).toBeNull()
  expect(afterStale.current_run.citation_count).toBeNull()
  expect(afterStale.current_run.result_sha256).toBeNull()
  expect(afterStale.current_run.machine_sha256).toBeNull()
  await expect(regressionPanel.getByTestId('regression-run-3')).toContainText('等待报告')
  await expect(regressionPanel.getByTestId('regression-run-3')).toContainText(round3.run.run_id)

  // 9. 再上传正确报告：断言复算通过，待人工复核；离线回放必须清楚展示。
  const import3 = await importReport(
    page, regressionPanel, feedbackId, round3.run.run_id, report3Path
  )
  expect(import3.status()).toBe(200)
  await expect(page.locator('.el-message').filter({ hasText: '评测报告已导入' }).last()).toBeVisible()
  const run3Item = regressionPanel.getByTestId('regression-run-3')
  await expect(run3Item).toContainText(round3.run.run_id)
  await expect(run3Item).toContainText('断言复算通过（待人工复核）')
  await expect(run3Item).toContainText('断言复算')
  await expect(run3Item).toContainText('离线回放')
  await expect(run3Item).toContainText('通过')
  const regressionAfter3 = await apiGet(request, `/feedback/${feedbackId}/regression`)
  expect(regressionAfter3.current_run.state).toBe('imported')
  expect(regressionAfter3.current_run.validation_level).toBe('content_regraded')
  expect(regressionAfter3.current_run.passed).toBe(true)
  expect(regressionAfter3.current_run.failures).toEqual([])
  expect(regressionAfter3.current_run.requested_mode).toBe('offline')

  // 10. 导入报告不自动关闭/改写反馈：仍是“已核实”。
  const feedbackAfter = await apiGet(request, `/feedback/${feedbackId}`)
  expect(feedbackAfter.status).toBe('verified')
  expect(feedbackAfter.reviewed_at).toBeTruthy()
  await expect(drawer.getByText('该反馈已处理，不能再次核实。')).toBeVisible()
  await capturePanel(drawer, regressionPanel, testInfo, 'round-3-regraded-passed.png', run3Item)

  // 11. 关闭重开 + 刷新后历史仍完整保留。
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await row.getByRole('button', { name: '详情', exact: true }).click()
  await expect(drawer).toBeVisible()
  const reopenedPanel = drawer.getByTestId('feedback-regression')
  await expect(reopenedPanel.getByTestId('regression-run-3')).toContainText(round3.run.run_id)
  await expect(reopenedPanel.getByTestId('regression-run-3')).toContainText('断言复算通过（待人工复核）')
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  await page.reload()
  await expect(row).toBeVisible()
  await row.getByRole('button', { name: '详情', exact: true }).click()
  await expect(drawer).toBeVisible()
  const reloadedPanel = drawer.getByTestId('feedback-regression')
  await expect(reloadedPanel.getByTestId('regression-history')).toContainText('#3')
  await expect(reloadedPanel).toContainText(causeNote)
  await expect(reloadedPanel.getByTestId('regression-run-3')).toContainText(round3.run.run_id)
  await expect(reloadedPanel.getByTestId('regression-run-3')).toContainText('断言复算通过（待人工复核）')
  await expect(reloadedPanel.getByTestId('regression-run-2')).toContainText(round2.run.run_id)
  await expect(reloadedPanel.getByTestId('regression-run-2')).toContainText('报告声明通过（未重算断言）')
  await expect(reloadedPanel.getByTestId('regression-run-1')).toContainText(round1.run.run_id)
  await expect(reloadedPanel.getByTestId('regression-run-1')).toContainText('required_phrase_missing')

  // 12. 手机宽度下重新打开详情并滚动到当前结果，收尾切回桌面。
  const desktopViewport = page.viewportSize() ?? { width: 1440, height: 900 }
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  await page.setViewportSize({ width: 390, height: 844 })
  await row.getByRole('button', { name: '详情', exact: true }).click()
  await expect(drawer).toBeVisible()
  const mobilePanel = drawer.getByTestId('feedback-regression')
  const mobileRun3 = mobilePanel.getByTestId('regression-run-3')
  await expect(mobileRun3).toContainText(round3.run.run_id)
  await expect(mobileRun3).toContainText('断言复算通过（待人工复核）')
  await capturePanel(drawer, mobilePanel, testInfo, 'mobile-390.png', mobileRun3)
  await page.setViewportSize(desktopViewport)
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
})

/**
 * 列表表格的宽度回归。
 *
 * 之前 9 列全部写死 `width`，合计 1150px；1920 桌面下面板有 1480px，
 * 右侧就空出一条 330px 的白带。改成弹性列后必须验证：
 *   1. 1920 初始加载时，表头/表体真实渲染宽度与列宽合计都等于表格宽度；
 *   2. 收窄到 1440 后表格内部横向滚动，再拉宽回 1920 重新铺满；
 *   3. 390 手机宽度页面不横向溢出，但表格内部仍可横向滚动。
 *
 * 不依赖列表数据：即使没有任何反馈，表头与 colgroup 也会正常渲染。
 */
test('反馈列表表格在宽屏撑满面板，且窄屏不把页面撑出横向滚动', async ({ page }, testInfo) => {
  const originalViewport = page.viewportSize() ?? { width: 1440, height: 900 }
  const table = page.locator('.el-table').first()
  const headerCells = table.locator('.el-table__header-wrapper th.el-table__cell')
  const colgroupCols = table.locator('.el-table__header colgroup col')

  const measure = () => table.evaluate(el => {
    const headerTable = el.querySelector<HTMLElement>(
      '.el-table__header-wrapper table.el-table__header'
    )
    const bodyTable = el.querySelector<HTMLElement>(
      '.el-table__body-wrapper table.el-table__body'
    )
    const wrap = el.querySelector<HTMLElement>(
      '.el-table__body-wrapper .el-scrollbar__wrap'
    )
    const panel = el.closest('.panel')
    const colWidths = Array.from(el.querySelectorAll('.el-table__header colgroup col'))
      .map(column => Number(column.getAttribute('width')))
    const headerCellWidths = Array.from(
      el.querySelectorAll<HTMLElement>('.el-table__header-wrapper th.el-table__cell')
    ).map(cell => cell.getBoundingClientRect().width)
    return {
      panelWidth: panel instanceof HTMLElement ? panel.clientWidth : 0,
      tableWidth: el.clientWidth,
      headerTableWidth: headerTable?.getBoundingClientRect().width ?? 0,
      bodyTableWidth: bodyTable?.getBoundingClientRect().width ?? 0,
      colWidths,
      colWidthSum: colWidths.reduce((sum, width) => sum + width, 0),
      headerCellCount: headerCellWidths.length,
      headerCellWidthSum: headerCellWidths.reduce((sum, width) => sum + width, 0),
      wrapClientWidth: wrap?.clientWidth ?? 0,
      wrapScrollWidth: wrap?.scrollWidth ?? 0
    }
  })
  type LayoutSnapshot = Awaited<ReturnType<typeof measure>>

  const pageOverflow = () => page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth
  )

  /** 宽屏：四组真实宽度都必须等于 table.clientWidth，绝对差 <= 1px。 */
  const expectFilledWidth = async (label: string) => {
    await expect.poll(async () => {
      const layout = await measure()
      return Math.max(
        Math.abs(layout.colWidthSum - layout.tableWidth),
        Math.abs(layout.headerTableWidth - layout.tableWidth),
        Math.abs(layout.bodyTableWidth - layout.tableWidth),
        Math.abs(layout.headerCellWidthSum - layout.tableWidth)
      )
    }, {
      timeout: 10_000,
      message: `${label}：列宽合计、表头/表体实际渲染宽度应与 table.clientWidth 一致`
    }).toBeLessThanOrEqual(1)
  }
  const expectFilled = (layout: LayoutSnapshot, label: string) => {
    expect(Math.abs(layout.colWidthSum - layout.tableWidth),
      `${label}：colgroup 列宽合计应等于 table.clientWidth`).toBeLessThanOrEqual(1)
    expect(Math.abs(layout.headerTableWidth - layout.tableWidth),
      `${label}：表头 table 实际渲染宽度应等于 table.clientWidth`).toBeLessThanOrEqual(1)
    expect(Math.abs(layout.bodyTableWidth - layout.tableWidth),
      `${label}：表体 table 实际渲染宽度应等于 table.clientWidth`).toBeLessThanOrEqual(1)
    expect(Math.abs(layout.headerCellWidthSum - layout.tableWidth),
      `${label}：表头单元格宽度合计应等于 table.clientWidth`).toBeLessThanOrEqual(1)
  }

  /** 窄屏：表格收窄到 1150px 以下，列宽合计超过表格宽度，内部出现横向滚动。 */
  const expectNarrowWidth = async (label: string) => {
    await expect.poll(async () => {
      const layout = await measure()
      return layout.tableWidth < 1150
        && layout.colWidthSum > layout.tableWidth
        && layout.wrapScrollWidth > layout.wrapClientWidth
    }, {
      timeout: 10_000,
      message: `${label}：表格应收窄到 1150px 以下并在内部产生横向滚动`
    }).toBe(true)
  }
  const expectNarrow = (layout: LayoutSnapshot, label: string) => {
    expect(layout.tableWidth, `${label}：表格宽度应小于旧版固定列宽合计`).toBeLessThan(1150)
    expect(layout.colWidthSum, `${label}：列宽合计应大于表格宽度`).toBeGreaterThan(layout.tableWidth)
    expect(Math.abs(layout.headerTableWidth - layout.colWidthSum),
      `${label}：表头 table 实际渲染宽度应等于列宽合计`).toBeLessThanOrEqual(1)
    expect(Math.abs(layout.bodyTableWidth - layout.colWidthSum),
      `${label}：表体 table 实际渲染宽度应等于列宽合计`).toBeLessThanOrEqual(1)
    expect(layout.wrapScrollWidth, `${label}：表格内部应可横向滚动`).toBeGreaterThan(layout.wrapClientWidth)
  }

  try {
    // 1. 1920 初始加载：弹性列铺满，右侧不遗留白带。
    await page.setViewportSize({ width: 1920, height: 1080 })
    await uiLogin(page)
    await page.goto('/feedback')
    await expect(table).toBeVisible()
    // 布局不依赖数据行：只要求表头与 colgroup 列存在。
    await expect(headerCells.first()).toBeVisible()
    const headerCount = await headerCells.count()
    expect(headerCount, '反馈表格应有表头列').toBeGreaterThan(0)
    expect(await colgroupCols.count(), '表头列与 colgroup 列数应一致').toBe(headerCount)
    await expect(
      table.locator('.el-table__body-wrapper table.el-table__body')
    ).toHaveCount(1)

    await expectFilledWidth('1920 初始加载')
    const wideInitial = await measure()
    expect(wideInitial.headerCellCount).toBe(headerCount)
    expect(wideInitial.panelWidth,
      '宽屏容器本身必须大于 1150px，否则表格缩在角落也会假通过').toBeGreaterThan(1150)
    expect(wideInitial.tableWidth, '宽屏表格宽度应大于旧版固定列宽合计').toBeGreaterThan(1150)
    expect(wideInitial.wrapScrollWidth,
      '宽屏下表格不应再有内部横向滚动').toBeLessThanOrEqual(wideInitial.wrapClientWidth + 1)
    expect(await pageOverflow(), '1920 宽屏页面不应出现横向滚动').toBe(0)
    expectFilled(wideInitial, '1920 初始加载')
    await page.screenshot({
      path: testInfo.outputPath('feedback-table-1920-initial.png'),
      animations: 'disabled'
    })

    // 2. 收窄到 1440：面板小于 1150px，表格保留最小列宽并只在内部滚动。
    await page.setViewportSize({ width: 1440, height: 900 })
    await expectNarrowWidth('1440 收窄')
    const narrow = await measure()
    expect(narrow.panelWidth, '1440 下容器应同步收窄').toBeLessThan(wideInitial.panelWidth)
    expect(narrow.tableWidth, '1440 下表格应同步收窄').toBeLessThan(wideInitial.tableWidth)
    expectNarrow(narrow, '1440 收窄')
    expect(await pageOverflow(), '1440 收窄后页面不应出现横向滚动').toBe(0)

    // 3. 从 1440 拉宽回 1920：弹性列必须重新分配，再次铺满。
    await page.setViewportSize({ width: 1920, height: 1080 })
    await expectFilledWidth('1440 拉宽回 1920')
    const wideAfterResize = await measure()
    expect(wideAfterResize.tableWidth, '拉宽后表格应比 1440 时更宽').toBeGreaterThan(narrow.tableWidth)
    expect(wideAfterResize.panelWidth,
      '拉宽后容器必须重新大于 1150px').toBeGreaterThan(1150)
    expect(wideAfterResize.wrapScrollWidth,
      '拉宽后表格不应再有内部横向滚动').toBeLessThanOrEqual(wideAfterResize.wrapClientWidth + 1)
    expect(await pageOverflow(), '拉宽回 1920 后页面不应出现横向滚动').toBe(0)
    expectFilled(wideAfterResize, '1440 拉宽回 1920')
    await page.screenshot({
      path: testInfo.outputPath('feedback-table-1920-after-1440.png'),
      animations: 'disabled'
    })

    // 4. 390 手机宽度：页面不横向溢出，但表格内部仍能滚动。
    await page.setViewportSize({ width: 390, height: 844 })
    await expectNarrowWidth('390 手机宽度')
    const mobile = await measure()
    expectNarrow(mobile, '390 手机宽度')
    expect(await pageOverflow(), '390 手机宽度页面不应出现横向滚动').toBe(0)
    await page.screenshot({
      path: testInfo.outputPath('feedback-table-390.png'),
      animations: 'disabled'
    })

    const wrap = table.locator('.el-table__body-wrapper .el-scrollbar__wrap')
    await expect(wrap).toHaveCount(1)
    const scrollRange = await wrap.evaluate(el => ({
      client: el.clientWidth,
      scroll: el.scrollWidth
    }))
    expect(scrollRange.scroll, '390 下内部滚动范围应大于可视宽度').toBeGreaterThan(scrollRange.client)
    await wrap.evaluate(el => { el.scrollLeft = el.scrollWidth - el.clientWidth })
    await expect.poll(
      () => wrap.evaluate(el => el.scrollLeft),
      { timeout: 5_000, message: '390 下表格内部应可通过 scrollLeft 横向滚动' }
    ).toBeGreaterThan(0)
    await wrap.evaluate(el => { el.scrollLeft = 0 })
  } finally {
    await page.setViewportSize(originalViewport)
  }
})
