import { readFileSync } from 'node:fs'
import type { APIRequestContext, Page } from '@playwright/test'

/**
 * 真实 E2E 环境的唯一连接契约。
 *
 * 所有地址只从 `e2e_env.py up` 写出的 manifest 派生，**不盲信** RAG_E2E_WEB_URL：
 * 显式覆盖时只有与 manifest.web 同 origin 才接受，否则直接失败。这样 Playwright
 * 不会把某个陌生 dev server 当成"我们这轮的环境"。
 */
export interface Manifest {
  owner: string
  nonce: string
  api?: { base_url?: string; port?: number }
  web?: { base_url?: string; port?: number } | null
  stub?: { base_url?: string; state_url?: string; config_url?: string; reset_url?: string }
  database?: { host?: string; port?: number; name?: string }
  worker?: { pid?: number }
  workers?: Record<'ingest' | 'summary' | 'project', { pid: number }>
  project_fixture?: { root_key: string; relative_dir: string; root: string; file: string }
}

const DEFAULT_USERNAME = 'e2e-admin'
const DEFAULT_PASSWORD = 'E2eAdmin!2026'

export const CREDENTIALS = {
  username: process.env.RAG_E2E_USERNAME || DEFAULT_USERNAME,
  password: process.env.RAG_E2E_PASSWORD || DEFAULT_PASSWORD
}

export function originOf(url: string): string {
  return new URL(url).origin
}

export function loadManifest(): Manifest {
  const path = process.env.RAG_E2E_MANIFEST
  if (!path) {
    throw new Error(
      '缺少 RAG_E2E_MANIFEST：请先运行 python backend/scripts/e2e_env.py up --out <dir>'
    )
  }
  const manifest = JSON.parse(readFileSync(path, 'utf-8')) as Manifest
  if (!manifest.owner || !manifest.nonce) {
    throw new Error(`manifest 缺少 owner/nonce，拒绝在无主环境上跑浏览器用例: ${path}`)
  }
  if (!manifest.web?.base_url || !manifest.api?.base_url || !manifest.stub?.base_url) {
    throw new Error(`manifest 缺少 web/api/stub 地址: ${path}`)
  }
  for (const address of [manifest.web.base_url, manifest.api.base_url, manifest.stub.base_url]) {
    const url = new URL(address)
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) {
      throw new Error('E2E 地址必须是本轮回环 HTTP 服务')
    }
  }
  return manifest
}

export function webUrl(manifest: Manifest): string {
  const declared = manifest.web?.base_url as string
  const override = process.env.RAG_E2E_WEB_URL
  if (!override) return declared
  if (originOf(override) !== originOf(declared)) {
    throw new Error(
      `RAG_E2E_WEB_URL(${override}) 与 manifest.web(${declared}) 不是同一 origin，拒绝盲信`
    )
  }
  return declared
}

export function apiUrl(manifest: Manifest): string {
  const declared = manifest.api?.base_url as string
  const override = process.env.RAG_E2E_API_URL
  if (!override) return declared
  if (originOf(override) !== originOf(declared)) {
    throw new Error(
      `RAG_E2E_API_URL(${override}) 与 manifest.api(${declared}) 不是同一 origin，拒绝盲信`
    )
  }
  return declared
}

async function assertApiOwner(request: APIRequestContext, base: string, manifest: Manifest): Promise<void> {
  const response = await request.get(`${base}/__e2e__/attestation`)
  if (!response.ok()) throw new Error(`API 归属探针失败: HTTP ${response.status()}`)
  const body = await response.json()
  if (body.schema !== 'ragkb-e2e-attestation' || body.owner !== manifest.owner ||
      body.nonce !== manifest.nonce || body.stub_base_url !== manifest.stub?.base_url) {
    throw new Error('API owner/nonce/模型地址与本轮 manifest 不符')
  }
}

/** 浏览器必须同源经 preview 代理，并证明目标就是本轮 API。 */
export async function assertProxiedApi(page: Page, manifest: Manifest): Promise<void> {
  await assertApiOwner(page.request, webUrl(manifest), manifest)
}

export interface StubControl {
  baseUrl: string
  configure(payload: Record<string, unknown>): Promise<Record<string, unknown>>
  state(): Promise<StubState>
  reset(): Promise<void>
}

export interface StubState {
  owner?: string
  nonce?: string
  counts: Record<string, number>
  config: Record<string, unknown>
}

export function stubControl(manifest: Manifest): StubControl {
  const baseUrl = manifest.stub?.base_url as string
  const origin = baseUrl.replace(/\/v1\/?$/, '')
  const state = async (): Promise<StubState> => {
    const response = await fetch(`${origin}/__control/state`)
    if (!response.ok) throw new Error(`替身 state 失败: HTTP ${response.status}`)
    const result = await response.json() as StubState
    if (result.owner !== manifest.owner || result.nonce !== manifest.nonce) {
      throw new Error('替身 owner/nonce 与本轮 manifest 不符')
    }
    return result
  }
  return {
    baseUrl,
    async configure(payload) {
      await state()
      const response = await fetch(`${origin}/__control/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      })
      if (!response.ok) throw new Error(`替身 config 失败: HTTP ${response.status}`)
      return (await response.json()) as Record<string, unknown>
    },
    state,
    async reset() {
      await state()
      const response = await fetch(`${origin}/__control/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      })
      if (!response.ok) throw new Error(`替身 reset 失败: HTTP ${response.status}`)
    }
  }
}

/** 用 API 层登录拿 token（给非浏览器断言/造数用），不伪造业务结果。 */
export async function apiLogin(
  request: APIRequestContext,
  manifest: Manifest
): Promise<string> {
  await assertApiOwner(request, apiUrl(manifest), manifest)
  const response = await request.post(`${apiUrl(manifest)}/api/v1/auth/login`, {
    data: { username: CREDENTIALS.username, password: CREDENTIALS.password }
  })
  if (!response.ok()) throw new Error(`API 登录失败: HTTP ${response.status()}`)
  const body = (await response.json()) as { data?: { access_token?: string } }
  const token = body.data?.access_token
  if (!token) throw new Error('登录响应缺少 access_token')
  return token
}

/** UI 登录：真实表单输入 + 真实提交按钮。 */
export async function uiLogin(page: Page): Promise<void> {
  await page.goto('/login')
  await page.getByPlaceholder('请输入用户名').fill(CREDENTIALS.username)
  await page.getByPlaceholder('请输入密码').fill(CREDENTIALS.password)
  await page.getByRole('button', { name: /登录工作空间/ }).click()
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 30_000 })
}
