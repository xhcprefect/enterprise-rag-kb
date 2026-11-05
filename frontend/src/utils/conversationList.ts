/**
 * 会话列表的分页合并与"迟到响应"守卫。
 *
 * 会话列表是独立于生成/详情的一条异步链路：
 * - 每次筛选（模式/KB/搜索词）变化都开启新的列表代次，旧筛选的响应不能覆盖新列表；
 * - 列表代次与生成用 sessionEpoch 完全隔离，加载列表不能打断正在生成的回答；
 * - 分页按 ID 合并，重复条目以较新的响应为准；
 * - 元数据（title/pinned_at）只改列表项，不能用会话详情覆盖正在流式的消息。
 *
 * 不依赖 vue/api/axios，node 测试可直接运行。
 */
import type { Conversation } from '../api/index.ts'
import { createSessionEpoch } from './sessionEpoch.ts'

export interface ConversationPage {
  total: number
  items: Conversation[]
}

export interface ListRequest {
  token: number
  filter: string
}

export interface ConversationListGuard {
  /** 开始一次列表请求，并把它所属的筛选快照记在请求上。 */
  begin(filter: string): ListRequest
  /**
   * 响应落地前确认：仍是最新一次请求，且筛选未被改动。
   *
   * 第二个参数必须是**调用方此刻的筛选键**。只比对 `begin()` 记下的
   * "最近一次请求的筛选"是不够的：搜索框还在防抖窗口里时，UI 已经变了，
   * 但新请求还没 begin，旧筛选的迟到响应仍会被误认为当前结果。
   */
  isCurrent(request: ListRequest, currentFilter?: string): boolean
  current(): number
}

/** 独立的列表代次：绝不复用生成/详情用的 sessionEpoch。 */
export function createConversationListGuard(initial = 0): ConversationListGuard {
  // 单独实例化，begin()/isCurrent() 与生成用的 epoch 互不影响。
  const epoch = createSessionEpoch(initial)
  let latestFilter = ''
  return {
    begin(filter: string) {
      latestFilter = filter
      return { token: epoch.begin(), filter }
    },
    isCurrent(request: ListRequest, currentFilter = latestFilter) {
      return request.filter === currentFilter && epoch.isCurrent(request.token)
    },
    current() {
      return epoch.current()
    }
  }
}

export interface ConversationPager {
  /** 筛选变化/首屏：回到第一页。 */
  reset(): void
  /** 最近一次成功落地的页码；0 表示还没有成功过任何一页。 */
  loadedPage(): number
  /** 下一次"加载更多"应该请求的页码（严格递增，不看去重后的条数）。 */
  nextPage(): number
  /** 服务端已经返回过不满一页，没有更多可翻。 */
  isExhausted(): boolean
  /**
   * 记录某页已经成功取回。
   *
   * 必须独立于"合并后列表长度"：跨页出现重复 ID 时，去重后的条数会少于
   * 服务端 total，若按条数推算页码就会一直重复请求同一页。
   * 传入本页条数与每页大小时，不满一页即视为到底（偏移分页的常见终止条件）。
   */
  markLoaded(page: number, received?: number, pageSize?: number): void
}

export function createConversationPager(): ConversationPager {
  let page = 0
  let exhausted = false
  return {
    reset() {
      page = 0
      exhausted = false
    },
    loadedPage() {
      return page
    },
    nextPage() {
      return page + 1
    },
    isExhausted() {
      return exhausted
    },
    markLoaded(loaded: number, received?: number, pageSize?: number) {
      if (Number.isInteger(loaded) && loaded > page) page = loaded
      if (typeof received === 'number' && typeof pageSize === 'number'
        && pageSize > 0 && received < pageSize) exhausted = true
    }
  }
}


/**
 * 按 ID 合并分页结果。
 * 保持已有顺序，新条目追加到末尾；重复 ID 用新响应覆盖字段（服务端排序才是最终顺序）。
 */
export function mergeConversationPages(
  current: readonly Conversation[],
  incoming: readonly Conversation[]
): Conversation[] {
  const merged = [...current]
  const positions = new Map(merged.map((item, index) => [item.id, index]))
  for (const item of incoming) {
    const index = positions.get(item.id)
    if (index === undefined) {
      positions.set(item.id, merged.length)
      merged.push(item)
      continue
    }
    merged[index] = { ...merged[index], ...item }
  }
  return merged
}

/** 只替换列表里同一 ID 的条目，不改变顺序（元数据变更后由服务端重排）。 */
export function updateLocalConversation(
  current: readonly Conversation[],
  updated: Conversation
): Conversation[] {
  const index = current.findIndex((item) => item.id === updated.id)
  if (index < 0) return current as Conversation[]
  const next = [...current]
  next[index] = { ...next[index], ...updated }
  return next
}

export function isConversationPinned(conversation: Pick<Conversation, 'pinned_at'> | null | undefined): boolean {
  return !!conversation?.pinned_at
}

/** 列表筛选键：模式、知识库和去空格后的搜索词共同决定一页结果。 */
export function conversationFilterKey(input: {
  mode: string
  kbId?: string | null
  query?: string | null
}): string {
  return [input.mode, input.kbId || '', (input.query || '').trim()].join('\u0000')
}

/** 搜索词长度上限与服务端一致；超出时截断，避免无意义的 422。 */
export const CONVERSATION_SEARCH_MAX = 200

export function normalizeConversationQuery(value: unknown): string {
  if (typeof value !== 'string') return ''
  return value.trim().slice(0, CONVERSATION_SEARCH_MAX)
}

/** 重命名输入校验：trim 后 1–200 个字符（中英文都按字符计）。 */
export function normalizeConversationTitle(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const title = value.trim()
  return title.length >= 1 && title.length <= 200 ? title : null
}
