/**
 * 检索条数的发送规则。
 *
 * 后端契约：`top_k` 省略（null/undefined）时用服务端 `RETRIEVAL_TOP_K`；
 * 显式传入则以调用方为准。滑块初值同样来自该配置（`GET /chat/retrieval-config`）。
 *
 * 为什么要"没动过就不传"：如果每次都显式传，`.env` 里的 `RETRIEVAL_TOP_K`
 * 就永远不生效，调参必须重新发版前端——这正是本次修掉的问题。
 *
 * 不依赖 api/vue，方便在 node 测试里直接跑。
 */
export function resolveTopK(options: {
  mode: string
  topK: number
  defaultTopK: number | null
}): number | undefined {
  const { mode, topK, defaultTopK } = options
  // 普通对话不检索，传了也没有意义。
  if (mode !== 'rag') return undefined
  // 尚未取到服务端默认值（老后端 / 请求失败）：只能显式传，否则用户拖过的值会丢。
  if (defaultTopK === null) return topK
  // 与默认值相同即视为"没动过"，交给服务端决定。
  return topK === defaultTopK ? undefined : topK
}
