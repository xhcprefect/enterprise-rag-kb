import type { Citation } from '../api/index.ts'
import type { DocumentScopeItem } from './documentScope.ts'

/** done 事件是最终引用的唯一来源；旧后端未提供时保守地不显示候选资料。 */
export interface ChatCompletion {
  latency_ms: number
  message_id?: string
  replayed?: boolean
  citations?: Citation[]
  /** 自动选择/换话题后后端实际生效的范围；仅在有该字段时更新 UI。 */
  document_scope?: DocumentScopeItem[]
  scope_revision?: number
}

export function getCompletedCitations(
  completion: ChatCompletion | undefined,
  hasAnswer: boolean,
  aborted: boolean
): Citation[] | undefined {
  if (!completion || !hasAnswer || aborted || !completion.citations?.length) return undefined
  return completion.citations
}
