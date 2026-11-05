/**
 * 普通会话摘要（memory）状态的纯展示逻辑；不依赖 vue/api，Node 测试可直接运行。
 *
 * 约定：摘要指"已输出的、可被纳入后续 prompt 的低信任历史"，绝不是无损记忆，
 * 也不是系统指令。展示必须给出确切覆盖到第几轮（covered_turn_seq 是已发布水位，
 * 可信），且不虚报"全部历史已记住"。
 *
 * 状态机对应后端 MemoryStatus.state：
 *   disabled  功能未启用（或返回不支持）
 *   idle      已启用但尚无摘要、也没有在途任务（空会话/刚重置）
 *   queued    摘要任务已排队
 *   processing 摘要正在生成
 *   ready     已发布，覆盖到 covered_turn_seq
 *   failed    生成失败
 *
 * lag 与 truncated 都是后端的布尔事实，直接采用：
 *   lag=true 表示覆盖范围之外仍存在未纳入摘要的历史；正常保留最近 N 轮原文不设 lag。
 *   truncated=true 表示某次摘要被输出长度上限截断，不可作为无损依据。
 *
 * 注意：next_turn_seq 是"已分配"计数（失败/取消轮次留空号也占序号），所以
 * next - covered 不是"待整理完整轮次数"。展示不得把它当精确轮次；backend 提供
 * 可核实计数前，round 差量一概按未知处理，只信 lag 布尔。
 */
export type MemoryState = 'disabled' | 'idle' | 'queued' | 'processing' | 'ready' | 'failed'

/** 三值布尔：true/false，不能核实时为 null（展示"未知"而非猜）。 */
export type TriState = boolean | null

export interface MemoryStatusLike {
  enabled: boolean
  state: MemoryState
  /** 已发布的水位，可信；非负整数缺省按 0。 */
  covered_turn_seq: number
  version: number | null
  lag?: boolean | null
  truncated?: boolean | null
  error_message?: string | null
}

/** 已发布水位：非负整数，否则保守地按 0。 */
export function coveredWatermark(covered: unknown): number {
  return Number.isInteger(covered) && (covered as number) >= 0 ? (covered as number) : 0
}

/** 是否已生成过一份摘要：有无非空版本号。无摘要时重置提交 expected_version=0。 */
export function hasSummary(version: unknown): boolean {
  return Number.isInteger(version) && (version as number) >= 0
}

/** 重置请求应提交的 expected_version：无摘要时按 0，否则用当前版本号。 */
export function resetExpectedVersion(version: unknown): number {
  return hasSummary(version) ? (version as number) : 0
}

/**
 * 归一化后端 lag 布尔；不是布尔值时返回未知（null），展示走"未知"文案，绝不猜。
 */
export function normalizeLag(lag: unknown): TriState {
  return typeof lag === 'boolean' ? lag : null
}

/** 展示判定结果：分类 + 需要展示的覆盖/差量/错误。 */
export type MemoryDisplay =
  | { kind: 'disabled' }
  | { kind: 'idle' }
  | { kind: 'pending'; covered: number; hasSummary: boolean }
  | { kind: 'ready'; covered: number; lag: TriState; truncated: boolean }
  | { kind: 'failed'; message: string | null; hasSummary: boolean; covered: number }

export interface MemoryDisplayInput {
  enabled: boolean
  state: MemoryState
  covered_turn_seq: number
  version: number | null
  lag?: boolean | null
  truncated?: boolean | null
  error_message?: string | null
}

/**
 * 把后端状态折算成可展示的分类。
 *
 * - 未启用 / state=disabled → disabled（没有可查看/可重置的摘要）
 * - 启用后 idle（尚无摘要且无任务）→ idle
 * - queued / processing → pending（正在生成；若已有 history 摘要则仍可只读查看
 *   hasSummary=true）
 * - ready → ready（覆盖到 covered_turn_seq；lag 只信后端布尔）
 * - failed → failed（显示错误；若此前已发布过摘要 hasSummary=true 则可只读查看旧版）
 */
export function deriveMemoryDisplay(input: MemoryDisplayInput): MemoryDisplay {
  if (!input.enabled || input.state === 'disabled') return { kind: 'disabled' }
  const covered = coveredWatermark(input.covered_turn_seq)
  const summary = hasSummary(input.version)
  switch (input.state) {
    case 'idle':
      return { kind: 'idle' }
    case 'queued':
    case 'processing':
      return { kind: 'pending', covered, hasSummary: summary }
    case 'ready':
      return { kind: 'ready', covered, lag: normalizeLag(input.lag), truncated: !!input.truncated }
    case 'failed':
      return {
        kind: 'failed',
        message: typeof input.error_message === 'string' ? input.error_message : null,
        hasSummary: summary,
        covered
      }
    default:
      return { kind: 'disabled' }
  }
}

/**
 * 从摘要状态自身看能否重置。按 M 契约：后台摘要任务（queued/processing）是可被
 * 重置作废旧 job 的（epoch reset），实时 ChatTurn 生成中的拦截由 UI 的 busy 负责，
 * 不在这里判断。只有 feature disabled（无摘要可重置）返回 false，避免显示无意义按钮。
 */
export function canReset(state: MemoryState): boolean {
  return state !== 'disabled'
}

/** 是否要就摘要再轮询一次：仅在途（queued/processing）或滞后（ready 且 lag=true）。
 * 正常保留最近 N 轮原文不设 lag，因此 ready+lag=false/未知时不轮询，绝不空转。 */
export function shouldPollMemory(s: Pick<MemoryStatusLike, 'state' | 'lag'> | null | undefined): boolean {
  if (!s) return false
  if (s.state === 'queued' || s.state === 'processing') return true
  return s.state === 'ready' && s.lag === true
}

/** 是否应提供摘要的只读查看入口：这也决定了"有已发布摘要时连 pending/failed 也可看"，
 * 与"disabled/idle 无摘要不可看"边界。 */
export function canViewSummary(state: MemoryState, version: unknown): boolean {
  if (state === 'disabled' || state === 'idle') return false
  if (state === 'ready') return true
  // pending（queued/processing）/failed：只有此前已发布过摘要（有版本号）才可只读查看旧版。
  return hasSummary(version)
}
