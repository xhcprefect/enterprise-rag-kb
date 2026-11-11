import { defineConfig, devices } from '@playwright/test'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 浏览器端 E2E 配置。
 *
 * 这里**不**启动任何被测服务：API / worker / PostgreSQL / 模型替身 / Vite preview
 * 都由 `backend/scripts/e2e_env.py up` 起，Playwright 只负责开 Chromium。
 * 原因有两个：
 *   1. 真实链路要求 API 与 ingest / summary / project / regression 四个 worker 各自独立运行；
 *   2. 环境必须独占且可清理，teardown 逻辑只应有一处。
 *
 * 环境变量：
 *   RAG_E2E_MANIFEST  harness 写出的 manifest.json（首选）
 *   RAG_E2E_WEB_URL   显式覆盖浏览器访问地址（必须与 manifest.web 一致才是同一环境）
 */
const MANIFEST = process.env.RAG_E2E_MANIFEST || ''
// package.json 里是 "type": "module"，没有 __dirname；与 vite.config.ts 用同一种写法。
const HERE = dirname(fileURLToPath(import.meta.url))
const OUTPUT_DIR = resolve(HERE, 'e2e/.artifacts')
const REPORT_DIR = resolve(HERE, 'e2e/.report')

interface Manifest {
  owner?: string
  api?: { base_url?: string }
  web?: { base_url?: string } | null
  stub?: { base_url?: string }
}

function loadManifest(): Manifest {
  if (!MANIFEST) return {}
  if (!existsSync(MANIFEST)) {
    // 宁可在这里明确失败，也不要让 all the specs 各自超时后给出误导性的错误。
    throw new Error(
      `RAG_E2E_MANIFEST 指向的文件不存在: ${MANIFEST}\n` +
      '请先运行: python backend/scripts/e2e_env.py up --out <dir>'
    )
  }
  return JSON.parse(readFileSync(MANIFEST, 'utf-8')) as Manifest
}

const manifest = loadManifest()
const baseURL = process.env.RAG_E2E_WEB_URL || manifest.web?.base_url || ''

if (!baseURL) {
  throw new Error(
    '缺少被测站点地址：设置 RAG_E2E_MANIFEST（推荐）或 RAG_E2E_WEB_URL。\n' +
    '浏览器用例必须打在 e2e_env.py 启动的真实 API + 生产构建上，不允许 page.route 伪造后端。'
  )
}

export default defineConfig({
  testDir: './e2e',
  // 用例之间有共享状态（同一个知识库、同一批会话），并行会互相踩。
  // 需要并发的场景在用例内部用两个 page 表达。
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [
    ['list'],
    ['html', { outputFolder: resolve(REPORT_DIR, 'html'), open: 'never' }],
    ['json', { outputFile: resolve(REPORT_DIR, 'results.json') }]
  ],
  outputDir: OUTPUT_DIR,
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    // 默认 zh-CN：断言直接用真实中文 UI 文案，不依赖 i18n 回退。
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    viewport: { width: 1440, height: 900 }
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        locale: 'zh-CN',
        // RAG_E2E_BROWSER_CHANNEL：显式指定本机已装浏览器渠道（如 msedge），
        // 用于没有 Playwright 下载浏览器缓存的环境；未设置时行为完全不变。
        channel: process.env.RAG_E2E_BROWSER_CHANNEL || undefined
      }
    }
  ]
})
