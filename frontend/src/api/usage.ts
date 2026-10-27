import client from './client'

/** 与后端 schemas/usage.py 对齐；金额和单价都是十进制字符串，前端不做浮点运算。 */
export type UsageGranularity = 'hour' | 'day' | 'month'
export type UsageScope = 'all' | 'mine'
export type UsageOperation =
  | 'generation'
  | 'rewrite'
  | 'query_embedding'
  | 'document_embedding'
  | 'rerank'
export type UsageModelKind = 'chat' | 'embedding' | 'rerank'

export interface UsageMetrics {
  requests: number
  successful_requests: number
  failed_requests: number
  input_tokens: number
  output_tokens: number
  cached_input_tokens: number
  total_tokens: number
  usage_known_requests: number
  usage_missing_requests: number
  priced_requests: number
  unpriced_requests: number
  estimated_cost: string | null
  input_cost: string | null
  output_cost: string | null
}

export interface UsageSeriesPoint extends UsageMetrics {
  /** ISO 8601，带 +08:00 的桶起点 */
  period: string
}

export interface UsageBreakdownItem extends UsageMetrics {
  provider: string
  model: string
  kind: UsageModelKind
  operation: UsageOperation
}

export interface UsageStatistics {
  start: string
  end: string
  timezone: 'Asia/Shanghai'
  granularity: UsageGranularity
  scope: UsageScope
  currency: 'CNY'
  tracking_started_at: string | null
  totals: UsageMetrics
  series: UsageSeriesPoint[]
  models: string[]
  breakdown: UsageBreakdownItem[]
}

export interface ModelPrice {
  id: string | null
  provider: string
  model: string
  kind: UsageModelKind
  currency: 'CNY'
  input_per_million: string | null
  output_per_million: string | null
  cached_input_per_million: string | null
  /** 没有配置过的模型是 revision=0、id=null 的虚拟项 */
  revision: number
}

export interface UsagePriceList {
  currency: 'CNY'
  items: ModelPrice[]
}

export interface UsageStatisticsParams {
  /** Asia/Shanghai 自然日，包含首尾 */
  start?: string
  end?: string
  granularity?: UsageGranularity
  scope?: UsageScope
  model?: string
  operation?: UsageOperation
}

export interface ModelPriceUpdate {
  provider: string
  model: string
  kind: UsageModelKind
  input_per_million: string | null
  output_per_million: string | null
  cached_input_per_million?: string | null
  expected_revision: number
}

/** 与 chatApi 相同，client 拦截器已解包 {code, success, message, data}。 */
export const usageApi = {
  statistics: (params: UsageStatisticsParams, signal?: AbortSignal) =>
    client.get<UsageStatistics>('/usage/statistics', { params, signal }),
  prices: () => client.get<UsagePriceList>('/usage/prices'),
  updatePrice: (payload: ModelPriceUpdate) => client.put<ModelPrice>('/usage/prices', payload)
}
