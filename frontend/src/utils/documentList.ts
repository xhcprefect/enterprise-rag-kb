/**
 * 文档列表的查询状态与迟到响应守卫（纯逻辑，可 Node 测试）。
 *
 * 搜索、翻页、轮询共用同一份查询快照；只有"发出请求时的快照"仍是当前快照，
 * 响应才允许写入界面，避免慢请求覆盖用户刚切换的筛选/页码。
 */

export interface DocumentQuery {
  q: string
  page: number
  pageSize: number
  status: string
}

/** 输入框内容统一 trim；超过 200 字符截断（与后端一致，不是报错）。 */
export const SEARCH_MAX_LENGTH = 200

export function normalizeQueryText(value: string | null | undefined): string {
  if (typeof value !== 'string') return ''
  return value.trim().slice(0, SEARCH_MAX_LENGTH)
}

export function makeQuery(partial: Partial<DocumentQuery>): DocumentQuery {
  return {
    q: normalizeQueryText(partial.q),
    page: Math.max(1, Math.trunc(partial.page ?? 1)),
    pageSize: Math.max(1, Math.trunc(partial.pageSize ?? 20)),
    status: typeof partial.status === 'string' ? partial.status : ''
  }
}

/** 两个查询是否等价；用于判断迟到响应是否仍有效。 */
export function sameQuery(left: DocumentQuery, right: DocumentQuery): boolean {
  return left.q === right.q && left.page === right.page &&
    left.pageSize === right.pageSize && left.status === right.status
}

/** 列表接口参数：空搜索/空状态不发送，避免无意义的查询串。 */
export function queryParams(query: DocumentQuery): {
  page: number
  page_size: number
  status?: string
  q?: string
} {
  const params: { page: number; page_size: number; status?: string; q?: string } = {
    page: query.page,
    page_size: query.pageSize
  }
  if (query.status) params.status = query.status
  if (query.q) params.q = query.q
  return params
}

/**
 * 迟到响应守卫：每次请求递增序号，只有最新序号且快照一致时才接受。
 * 返回 true 表示可以写入界面。
 */
export function isLatestResponse(
  sequence: number,
  latestSequence: number,
  requestQuery: DocumentQuery,
  currentQuery: DocumentQuery,
  disposed = false
): boolean {
  return !disposed && sequence === latestSequence && sameQuery(requestQuery, currentQuery)
}

/**
 * 轮询是否还需要继续：列表里仍有 pending/processing，或当前上传仍在入库。
 * 失败/取消是终态，不触发轮询。
 */
export function shouldPoll(statuses: string[], ingesting: boolean): boolean {
  return ingesting || statuses.some((status) => status === 'pending' || status === 'processing')
}

/**
 * 这一拍的响应是否已过期。
 *
 * 轮询里"列表请求 + 当前文档详情请求"是一对；只有发起这一拍时的条目 key、
 * 列表序号都没变，迟到的 404 才允许推进队列。否则旧 active 文档的 404
 * 会把用户刚切换的新条目误判为"已删除"。
 */
export function isStalePoll(
  pollKey: string | null,
  currentKey: string | null | undefined,
  requestEpoch: number,
  latestEpoch: number,
  disposed = false
): boolean {
  if (disposed) return true
  if (!pollKey) return true
  if (pollKey !== currentKey) return true
  return requestEpoch !== latestEpoch
}
