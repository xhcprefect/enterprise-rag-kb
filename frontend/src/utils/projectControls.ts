/**
 * 项目视图的统一守卫逻辑（纯函数，可 Node 测试）。
 *
 * 页面在多处并发读写：切换源、切换快照、换 query、翻页、后台 poll、原文预览，
 * 各自带独立 epoch / AbortController。任何迟到响应都必须与"当前正在看的
 * 引用"比对一致才能落界面，否则丢弃——不覆盖新状态、不操作错源。
 *
 * 这些函数不持有任何可变状态：调用方（composable / view）保存 epoch、disposed，
 * 把当前快照、query、页码传进来比对。好处是能脱离 Vue/DOM 做真实 deferred 测试。
 */

export interface ProjectViewQuery {
  /** 正在查看的源；切换源 = 视图整体更换，旧响应一律丢弃。 */
  sourceId: string
  /** 当前视图快照 id；null 表示未选定/未就绪。切换快照同样使旧请求失效。 */
  snapshotId: string | null
  /** 文件分页（1-based）。 */
  page: number
  /** 每页条数。 */
  pageSize: number
  /** 内容搜索关键字（trim 后）。 */
  q: string
}

export function normalizeQueryText(value: string | null | undefined): string {
  if (typeof value !== 'string') return ''
  return value.trim()
}

export function clampPage(value: number): number {
  const page = Math.trunc(value)
  return Number.isFinite(page) && page >= 1 ? page : 1
}

export function clampPageSize(value: number, maxPageSize: number): number {
  const size = Math.trunc(value)
  const safe = Number.isFinite(size) && size >= 1 ? size : 20
  return Math.min(safe, maxPageSize)
}

export function clampOffset(value: number): number {
  const offset = Math.trunc(value)
  return Number.isFinite(offset) && offset >= 0 ? offset : 0
}

export function clampTopK(value: number, min: number, max: number): number {
  const k = Math.trunc(value)
  if (!Number.isFinite(k) || k < min) return min
  return Math.min(k, max)
}

export interface ProjectViewQueryInput {
  sourceId: string
  snapshotId: string | null
  page?: number
  pageSize?: number
  q?: string | null
  maxPageSize?: number
}

export function makeProjectQuery(input: ProjectViewQueryInput): ProjectViewQuery {
  return {
    sourceId: input.sourceId,
    snapshotId: input.snapshotId,
    page: clampPage(input.page ?? 1),
    pageSize: clampPageSize(input.pageSize ?? 20, input.maxPageSize ?? 100),
    q: normalizeQueryText(input.q)
  }
}

/** 两个视图查询是否逐字段一致；任何字段不同都视为换了视图/翻页/query。 */
export function sameProjectQuery(a: ProjectViewQuery, b: ProjectViewQuery): boolean {
  return (
    a.sourceId === b.sourceId &&
    a.snapshotId === b.snapshotId &&
    a.page === b.page &&
    a.pageSize === b.pageSize &&
    a.q === b.q
  )
}

/**
 * 一条带序号的请求/响应是否仍允许落界面。
 * requestEpoch 是发起该请求时的 epoch；latestEpoch 是当前 epoch（每次切源/切快照/
 * 换 query/翻页/卸载都会推进）。只有 epoch 最新、且请求快照与当前快照逐字段一致、
 * 且页面未卸载时才接受。
 */
export function isLatestResponse(
  requestEpoch: number,
  latestEpoch: number,
  requestQuery: ProjectViewQuery,
  currentQuery: ProjectViewQuery,
  disposed = false
): boolean {
  if (disposed) return false
  if (requestEpoch !== latestEpoch) return false
  return sameProjectQuery(requestQuery, currentQuery)
}

/** 一个源是否正处于活动同步（pending/processing），需要继续轮询。 */
export function shouldPollSnapshot(status: string | null | undefined): boolean {
  return status === 'pending' || status === 'processing'
}

/** 快照是否已就绪可搜索/读文件。pending/成功绝不等于就绪。 */
export function isReadyStatus(status: string | null | undefined): boolean {
  return status === 'ready'
}

export interface ProjectPollSignalInput {
  requestEpoch: number
  latestEpoch: number
  requestSourceId: string
  /** 当前源；null 表示未选中（卸载/空视图），此时一律视为过期。 */
  currentSourceId: string | null
  disposed?: boolean
}

/**
 * 后台一轮 poll 的结果是否还能触达界面。poll 是无用户主动请求的读，
 * 其唯一统一守卫是 epoch + 源。只要期间切换了源或卸载（或 page/query 变化
 * 使 epoch 推进），这一轮结果整体过期，不写界面、不触发下一次轮询决策。
 */
export function isStalePoll(input: ProjectPollSignalInput): boolean {
  if (input.disposed) return true
  if (input.requestEpoch !== input.latestEpoch) return true
  return input.requestSourceId !== input.currentSourceId
}

/** 计算文件列表下一页的 offset（0-based），并受 total 上界约束。 */
export function nextOffset(page: number, pageSize: number): number {
  return (clampPage(page) - 1) * clampPageSize(pageSize, 100)
}

/**
 * 当服务端返回的 total 小于当前 offset + 已有条数时（并发同步变了文件数），
 * 把页码收敛到不超过最后一页，避免空白页。返回 -1 表示无需调整。
 */
export function clampPageToTotal(page: number, pageSize: number, total: number): number {
  const size = clampPageSize(pageSize, 100)
  const maxPage = Math.max(1, Math.ceil(total / size))
  const p = clampPage(page)
  return p > maxPage ? maxPage : -1
}

/** top_k 合法区间（后端 MAX_TOP_K=20）。 */
export const PROJECT_TOP_K_MIN = 1
export const PROJECT_TOP_K_MAX = 20

/** 文件分页每页上限（后端 FILE_PAGE_SIZE=100）。 */
export const PROJECT_PAGE_SIZE_MAX = 100

/**
 * 登记项目源时的相对子目录是否合法：只接受纯相对路径 `a/b`，拒绝绝对路径、盘符、
 * UNC、反斜杠、`.` 或 `..` 组件（与后端 split_relative_path 同一安全规则）。
 * 绝不把非法绝对路径 strip 后当作"相对"再放行。
 */
export function isValidRelativeDir(value: string | null | undefined): boolean {
  if (typeof value !== 'string') return false
  const raw = value.trim()
  if (!raw) return true // 可空 = 授权根本身
  if (raw.includes('\\')) return false
  if (raw.startsWith('/')) return false
  if (raw.length >= 2 && raw[1] === ':') return false
  if (raw.startsWith('//')) return false
  if (raw.startsWith('./') || raw.startsWith('../')) return false
  if (raw === '.' || raw === '..') return false
  const parts = raw.split('/')
  return parts.every((p) => p.length > 0 && p !== '.' && p !== '..')
}
