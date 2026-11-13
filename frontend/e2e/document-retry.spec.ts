import { randomUUID } from 'node:crypto'
import { test, expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test'
import {
  apiCall, apiGetData, apiLogin, apiPostData, assertProxiedApi,
  loadManifest, stubControl, uiLogin
} from './fixtures'

const manifest = loadManifest()
const stub = stubControl(manifest)
let token = ''
let kbId = ''
let documentId = ''

test.describe.configure({ mode: 'serial' })

interface DocumentState {
  id: string
  filename: string
  status: 'pending' | 'processing' | 'ready' | 'failed' | 'cancelled'
  error_message: string | null
  ingest_attempts: number
  next_attempt_at: string | null
  last_error_code: string | null
  cancel_requested: boolean
  index_version: number
}

const kbPath = () => `/knowledge-bases/${kbId}`
const documentPath = (id = documentId) => `${kbPath()}/documents/${id}`
const state = (request: APIRequestContext) =>
  apiGetData<DocumentState>(request, manifest, token, documentPath())
const documentRow = (page: Page, filename: string) =>
  page.locator('.el-table__row').filter({ hasText: filename })

async function waitForState(
  request: APIRequestContext,
  predicate: (value: DocumentState) => boolean,
  description: string,
  timeout = 150_000
): Promise<DocumentState> {
  let latest: DocumentState | undefined
  await expect.poll(async () => {
    latest = await state(request)
    return predicate(latest)
  }, { timeout, intervals: [200, 500, 1_000], message: description }).toBe(true)
  return latest!
}

async function upload(page: Page, filename: string, buffer: Buffer, mimeType = 'text/markdown') {
  const completed = page.waitForResponse(response =>
    new URL(response.url()).pathname.startsWith(`/api/v1${kbPath()}/upload-sessions/`) &&
    response.url().endsWith('/complete') && response.request().method() === 'POST')
  await page.locator('input[type="file"]').setInputFiles({ name: filename, mimeType, buffer })
  const response = await completed
  expect(response.ok(), await response.text()).toBeTruthy()
  const body = await response.json() as { data: { document: DocumentState; deduplicated: boolean } }
  documentId = body.data.document.id
  expect(documentId).toBeTruthy()
  expect(body.data.deduplicated).toBe(false)
  return body.data.document
}

async function screenshot(page: Page, testInfo: TestInfo, name: string) {
  const path = testInfo.outputPath(name)
  await page.screenshot({ path, fullPage: true, animations: 'disabled' })
  await testInfo.attach(name, { path, contentType: 'image/png' })
}

async function assertNoPageOverflow(page: Page) {
  const widths = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
  }))
  expect(widths.document).toBeLessThanOrEqual(widths.viewport)
}

async function settleAndDeleteKb(request: APIRequestContext) {
  if (!kbId) return
  const { items } = await apiGetData<{ items: DocumentState[] }>(request, manifest, token,
    `${kbPath()}/documents?page_size=100`)
  for (const document of items) {
    if (document.status !== 'pending' && document.status !== 'processing') continue
    const cancelled = await apiCall(request, manifest, token, 'POST', `${documentPath(document.id)}/cancel`)
    // The worker may publish a terminal state between the list and cancel request.
    expect([200, 400]).toContain(cancelled.status)
    await expect.poll(async () => {
      const current = await apiGetData<DocumentState>(request, manifest, token, documentPath(document.id))
      return current.status
    }, { timeout: 45_000, intervals: [250, 500, 1_000] }).toMatch(/^(ready|failed|cancelled)$/)
  }
  const removed = await apiCall(request, manifest, token, 'DELETE', kbPath())
  expect(removed.ok, JSON.stringify(removed.body)).toBe(true)
  kbId = ''
  documentId = ''
}

function blankPdf(): Buffer {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << >> /Contents 4 0 R >>',
    '<< /Length 0 >>\nstream\nendstream'
  ]
  let pdf = '%PDF-1.4\n'
  const offsets = [0]
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(pdf))
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`
  }
  const xref = Buffer.byteLength(pdf)
  pdf += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`
  pdf += offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')
  pdf += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`
  return Buffer.from(pdf)
}

test.beforeAll(async ({ request }, testInfo) => {
  expect(testInfo.config.workers, 'the shared model stub requires one Playwright worker').toBe(1)
  token = await apiLogin(request, manifest)
  expect(Object.keys(manifest.workers || {}).sort()).toEqual(['ingest', 'project', 'regression', 'summary'])
})

test.beforeEach(async ({ page, request }) => {
  await assertProxiedApi(page, manifest)
  await stub.reset()
  const kb = await apiPostData<{ id: string }>(request, manifest, token, '/knowledge-bases', {
    name: `Retry E2E ${randomUUID()}`, is_public: true
  })
  kbId = kb.id
  await uiLogin(page)
  await page.locator('.language-button').click()
  await page.getByRole('menuitem', { name: 'English', exact: true }).click()
  await expect(page.locator('html')).toHaveAttribute('lang', 'en-US')
  await page.goto(`/documents/${kbId}`)
  await expect(page.getByRole('button', { name: 'Upload document', exact: true })).toBeVisible()
})

test.afterEach(async ({ request, page }) => {
  // Unmount the upload queue and settle every document before resetting shared failure counters.
  await page.goto('/knowledge')
  await settleAndDeleteKb(request)
  await stub.reset()
})

test('temporary model failure counts down, retries automatically and reaches ready', async ({ page, request }, testInfo) => {
  test.setTimeout(180_000)
  const marker = `RETRY_AUTOMATIC_${randomUUID()}`
  const filename = `automatic-${randomUUID()}.md`
  await stub.configure({ embed_fail_next: 3, embed_fail_status: 500, embed_fail_match: marker })
  await upload(page, filename, Buffer.from(`# Automatic retry\n\n${marker} is indexed by the real ingest worker.\n`))
  const waiting = await waitForState(request, value => value.status === 'pending' && value.ingest_attempts === 1 &&
    !!value.next_attempt_at, 'three failed HTTP calls cause one task-level failure')
  expect(waiting.last_error_code).toBe('source_unavailable')
  expect((await stub.state()).counts.embedding_failures).toBe(3)

  const row = documentRow(page, filename)
  const hint = row.getByTestId('ingest-retry-hint')
  await expect(hint).toHaveAttribute('data-retry-phase', 'waiting')
  await expect(hint).toContainText(/Retrying in \d+s/)
  const secondsBefore = Number((await hint.innerText()).match(/\d+/)![0])
  await expect.poll(async () => {
    const seconds = (await hint.textContent())?.match(/Retrying in (\d+)s/)
    return seconds ? Number(seconds[1]) : secondsBefore
  }, { timeout: 4_000, intervals: [200, 400] }).toBeLessThan(secondsBefore)
  await expect(page.locator('.upload-hint')).toContainText('1 ingestion failure(s)')
  await assertNoPageOverflow(page)
  await screenshot(page, testInfo, 'automatic-retry-desktop.png')

  // SPA navigation disposes the timer; API conditions keep waiting without a fixed sleep.
  await page.getByRole('button', { name: 'Back to knowledge bases', exact: true }).click()
  await expect(page).toHaveURL(/\/knowledge$/)
  let lateListRequests = 0
  const watch = (request: { method(): string; url(): string }) => {
    if (request.method() === 'GET' && new URL(request.url()).pathname === `/api/v1${kbPath()}/documents`) {
      lateListRequests += 1
    }
  }
  page.on('request', watch)
  const ready = await waitForState(request, value => value.status === 'ready', 'retry finishes without re-uploading')
  page.off('request', watch)
  expect(lateListRequests, 'the unmounted documents page must stop polling').toBe(0)
  expect(ready.ingest_attempts).toBe(0)
  expect(ready.next_attempt_at).toBeNull()
  expect(ready.last_error_code).toBeNull()
  expect((await stub.state()).counts.embedding_calls).toBe(1)
  await page.goto(`/documents/${kbId}`)
  await expect(row.getByText('Ready', { exact: true })).toBeVisible()
  await expect(row.getByTestId('ingest-retry-hint')).toHaveCount(0)
})

test('empty Markdown fails permanently without model calls; mobile English dark state stays readable', async ({ page, request }, testInfo) => {
  const filename = `empty-${randomUUID()}.md`
  await upload(page, filename, Buffer.from('\n \t\n'))
  const failed = await waitForState(request, value => value.status === 'failed', 'empty Markdown is a permanent input failure')
  const notification = page.locator('.el-message--error').last()
  await expect(notification).toContainText('no extractable content')
  await expect(notification).toContainText('Fix the source file and upload again')
  await expect(notification).not.toContainText('文档处理失败，请联系管理员检查日志')
  expect(failed.last_error_code).toBe('empty_content')
  expect(failed.ingest_attempts).toBe(1)
  expect(failed.next_attempt_at).toBeNull()
  expect((await stub.state()).counts.embedding_calls).toBe(0)
  expect((await stub.state()).counts.embedding_failures || 0).toBe(0)

  const row = documentRow(page, filename)
  const hint = row.getByTestId('ingest-retry-hint')
  await expect(hint).toHaveAttribute('data-retry-phase', 'permanent')
  await expect(hint).toHaveText('File issue; retries stopped')
  await expect(page.locator('.upload-hint')).toContainText('no extractable content')
  await expect(page.locator('.upload-hint')).toContainText('Fix the source file and upload again')
  await expect(page.locator('.upload-hint')).not.toContainText('文档处理失败，请联系管理员检查日志')
  await hint.hover()
  await expect(page.getByRole('tooltip').filter({ hasText: 'no extractable content' })).toBeVisible()
  await screenshot(page, testInfo, 'empty-document-permanent-desktop.png')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.theme-button').click()
  await page.getByRole('menuitem', { name: 'Dark mode', exact: true }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.locator('html')).toHaveAttribute('lang', 'en-US')
  await hint.scrollIntoViewIfNeeded()
  await expect(hint).toBeInViewport()
  const geometry = await hint.evaluate(element => ({
    width: element.clientWidth, scrollWidth: element.scrollWidth,
    left: element.getBoundingClientRect().left, right: element.getBoundingClientRect().right
  }))
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.width)
  expect(geometry.left).toBeGreaterThanOrEqual(0)
  expect(geometry.right).toBeLessThanOrEqual(390)
  await assertNoPageOverflow(page)
  await screenshot(page, testInfo, 'permanent-retry-390-en-dark.png')
})

test('PDF-specific failure details remain available alongside permanent retry guidance', async ({ page, request }) => {
  const filename = `blank-${randomUUID()}.pdf`
  await upload(page, filename, blankPdf(), 'application/pdf')
  const failed = await waitForState(request, value => value.status === 'failed', 'textless PDF returns its dedicated parser message')
  const notification = page.locator('.el-message--error').last()
  await expect(notification).toContainText(failed.error_message!)
  await expect(notification).not.toContainText('文档处理失败，请联系管理员检查日志')
  expect(failed.last_error_code).toBe('corrupt_file')
  expect(failed.ingest_attempts).toBe(1)
  expect(failed.error_message).toContain('PDF 中没有可提取的文字或表格')
  await expect(page.locator('.upload-hint')).toContainText(failed.error_message!)
  expect((await stub.state()).counts.embedding_calls).toBe(0)
  const row = documentRow(page, filename)
  await expect(row.getByTestId('ingest-retry-hint')).toHaveAttribute('data-retry-phase', 'permanent')
  await row.getByText('Failed', { exact: true }).hover()
  await expect(page.getByRole('tooltip').filter({ hasText: failed.error_message! })).toBeVisible()
})

test('exhausted retries stop; manual rebuild resets the budget and succeeds', async ({ page, request }, testInfo) => {
  test.setTimeout(240_000)
  const marker = `RETRY_MANUAL_${randomUUID()}`
  const filename = `manual-${randomUUID()}.md`
  await stub.configure({ embed_fail_next: 100, embed_fail_status: 500, embed_fail_match: marker })
  await upload(page, filename, Buffer.from(`# Manual rebuild\n\n${marker} remains a recoverable model failure.\n`))
  const exhausted = await waitForState(request, value => value.status === 'failed', 'task-level retry budget is exhausted')
  const notification = page.locator('.el-message--error').last()
  await expect(notification).toContainText('3 ingestion failure(s)')
  await expect(notification).toContainText('rebuild the index manually after the service recovers')
  await expect(notification).not.toContainText('文档处理失败，请联系管理员检查日志')
  expect(exhausted.ingest_attempts).toBe(3)
  expect(exhausted.last_error_code).toBe('source_unavailable')
  expect((await stub.state()).counts.embedding_failures).toBe(9)
  const row = documentRow(page, filename)
  const hint = row.getByTestId('ingest-retry-hint')
  await expect(hint).toHaveAttribute('data-retry-phase', 'exhausted')
  await expect(hint).toHaveText('Automatic retries exhausted')
  await screenshot(page, testInfo, 'automatic-retries-exhausted-desktop.png')

  await stub.configure({ embed_fail_next: 0 })
  const rebuilt = page.waitForResponse(response => response.url().endsWith(`${documentPath()}/reindex`) &&
    response.request().method() === 'POST')
  await row.getByRole('button', { name: 'Rebuild index', exact: true }).click()
  const response = await rebuilt
  expect(response.status()).toBe(200)
  const reset = (await response.json() as { data: DocumentState }).data
  expect(reset.index_version).toBe(exhausted.index_version + 1)
  expect(reset.ingest_attempts).toBe(0)
  expect(reset.next_attempt_at).toBeNull()
  expect(reset.last_error_code).toBeNull()
  await expect(page.locator('.upload-card').filter({
    hasText: /Automatic retries exhausted|3 ingestion failure\(s\)/
  })).toHaveCount(0)
  await expect(page.locator('.upload-card .upload-icon.failed')).toHaveCount(0)
  const ready = await waitForState(request, value => value.status === 'ready', 'manual rebuild succeeds after the service recovers')
  expect(ready.index_version).toBe(reset.index_version)
  expect(ready.ingest_attempts).toBe(0)
  await expect(row.getByText('Ready', { exact: true })).toBeVisible({ timeout: 20_000 })
  await expect(hint).toHaveCount(0)
  await expect.poll(async () => {
    const card = page.locator('.upload-card')
    return await card.count() === 0 || await card.locator('.upload-icon.done').count() === 1
  }, { timeout: 20_000, message: 'the matching upload card must finish or leave the queue after a row rebuild' }).toBe(true)
  await expect(page.locator('.upload-card .upload-icon.failed')).toHaveCount(0)
  await screenshot(page, testInfo, 'manual-rebuild-ready-desktop.png')
})

test('cancelling during backoff prevents model calls after the original due time', async ({ page, request }, testInfo) => {
  test.setTimeout(150_000)
  const marker = `RETRY_CANCEL_${randomUUID()}`
  const filename = `cancel-${randomUUID()}.md`
  await stub.configure({ embed_fail_next: 100, embed_fail_status: 500, embed_fail_match: marker })
  await upload(page, filename, Buffer.from(`# Cancel backoff\n\n${marker} must not run after cancellation.\n`))
  const waiting = await waitForState(request, value => value.status === 'pending' && value.ingest_attempts === 1 &&
    !!value.next_attempt_at, 'document is in its first backoff')
  const row = documentRow(page, filename)
  await expect(row.getByTestId('ingest-retry-hint')).toHaveAttribute('data-retry-phase', 'waiting')
  const beforeCancel = await stub.state()
  const cancelResponse = page.waitForResponse(response => response.url().endsWith(`${documentPath()}/cancel`) &&
    response.request().method() === 'POST')
  await row.getByRole('button', { name: 'Cancel indexing', exact: true }).click()
  const confirmation = page.locator('.el-message-box')
  await expect(confirmation).toContainText(filename)
  await confirmation.getByRole('button', { name: 'Confirm', exact: true }).click()
  expect((await cancelResponse).status()).toBe(200)
  await waitForState(request, value => value.status === 'cancelled', 'worker cancels the queued retry without waiting for its due time', 15_000)
  await expect(row.getByText('Cancelled', { exact: true })).toBeVisible()
  await expect(row.getByTestId('ingest-retry-hint')).toHaveCount(0)
  const due = Date.parse(waiting.next_attempt_at!)
  expect(Number.isFinite(due)).toBe(true)
  await expect.poll(async () => {
    expect((await state(request)).status).toBe('cancelled')
    const current = await stub.state()
    expect(current.counts.embedding_calls).toBe(beforeCancel.counts.embedding_calls)
    expect(current.counts.embedding_failures).toBe(beforeCancel.counts.embedding_failures)
    return Date.now() >= due + 3_000
  }, { timeout: 90_000, intervals: [500, 1_000], message: 'no task is executed past the cancelled retry deadline' }).toBe(true)
  await screenshot(page, testInfo, 'cancelled-backoff-desktop.png')
})
