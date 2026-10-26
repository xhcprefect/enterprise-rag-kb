import type { Citation } from '../api/index.ts'

/** done 事件是最终引用的唯一来源；旧后端未提供时保守地不显示候选资料。 */
export interface ChatCompletion {
  latency_ms: number
  citations?: Citation[]
}

export function getCompletedCitations(
  completion: ChatCompletion | undefined,
  hasAnswer: boolean,
  aborted: boolean
): Citation[] | undefined {
  if (!completion || !hasAnswer || aborted || !completion.citations?.length) return undefined
  return completion.citations
}
