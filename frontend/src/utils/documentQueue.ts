/**
 * 顺序多文件上传队列的纯逻辑。
 *
 * 只负责状态机，不碰 DOM、网络或文件内容，便于用 Node 测试：
 * 一个 current + 一个 queue + 每项 phase。暂停保留后续，失败停住等用户
 * 重试/跳过，取消当前后继续，取消全部停止。
 */

export type QueuePhase = 'waiting' | 'uploading' | 'paused' | 'ingesting' | 'done' | 'cancelled' | 'failed'

export interface QueueItem {
  /** 稳定标识，用于迟到响应识别与去重 */
  key: string
  file: File
  phase: QueuePhase
  /** 已确认复用同库已有文档 */
  deduplicated: boolean
}

export interface QueueState<T extends QueueItem = QueueItem> {
  current: T | null
  queue: T[]
}

/** 处于这些阶段时不会自动推进，需用户操作（重试/跳过/关闭）。 */
export function isBlocking(phase: QueuePhase): boolean {
  return phase === 'failed'
}

/** 是否仍在推进（上传或入库），用于轮询与忙碌判断。 */
export function isActive(phase: QueuePhase): boolean {
  return phase === 'uploading' || phase === 'ingesting' || phase === 'paused'
}

/** 新建空队列。 */
export function emptyQueue<T extends QueueItem = QueueItem>(): QueueState<T> {
  return { current: null, queue: [] }
}

/**
 * 加入一批文件。重复（同 name+size+lastModified）的文件会被跳过并返回，
 * 由调用方决定是否提示"已在队列中"。
 */
export function enqueue<T extends QueueItem>(
  state: QueueState<T>,
  items: T[]
): { state: QueueState<T>; rejected: T[] } {
  const queue: T[] = [...state.queue]
  const rejected: T[] = []
  const seen = new Set<string>()
  if (state.current) seen.add(signature(state.current.file))
  for (const item of queue) seen.add(signature(item.file))
  for (const item of items) {
    const id = signature(item.file)
    if (seen.has(id)) {
      rejected.push(item)
      continue
    }
    seen.add(id)
    queue.push({ ...item, phase: 'waiting' } as T)
  }
  const next: QueueState<T> = { current: state.current, queue }
  return { state: state.current ? next : advance(next), rejected }
}

/** 同一文件重复上传的判定键。 */
export function signature(file: File): string {
  return `${file.name}\u0000${file.size}\u0000${file.lastModified}`
}

/** 把队首提升为 current；current 已被占用时原样返回。 */
export function advance<T extends QueueItem>(state: QueueState<T>): QueueState<T> {
  if (state.current || state.queue.length === 0) return state
  const [next, ...rest] = state.queue
  return { current: { ...next, phase: next.phase === 'waiting' ? 'uploading' : next.phase }, queue: rest } as QueueState<T>
}

/** 更新 current（不改变队列）。 */
export function patchCurrent<T extends QueueItem>(state: QueueState<T>, patch: Partial<T>): QueueState<T> {
  if (!state.current) return state
  return { current: { ...state.current, ...patch }, queue: state.queue }
}

/**
 * current 结束（done/cancelled/failed）。failed 默认停住等用户处理；
 * 传入 autoAdvance 才继续，避免失败被静默跳过。
 */
export function finishCurrent<T extends QueueItem>(
  state: QueueState<T>,
  phase: QueuePhase,
  autoAdvance = true
): QueueState<T> {
  if (!state.current) return state
  const finished = { ...state.current, phase }
  if (!autoAdvance && phase === 'failed') return { current: finished, queue: state.queue }
  return advance({ current: null, queue: state.queue })
}

/**
 * 用户点击"重试"：把失败的 current 重新置为 uploading（同一文件、同一会话身份）。
 * 失败的文档若已被去重复用，重试应走显式 reindex，由视图层决定。
 */
export function retryCurrent<T extends QueueItem>(state: QueueState<T>): QueueState<T> {
  if (!state.current || state.current.phase !== 'failed') return state
  return { current: { ...state.current, phase: 'uploading' }, queue: state.queue }
}

/** 跳过失败的 current，继续下一个。 */
export function skipCurrent<T extends QueueItem>(state: QueueState<T>): QueueState<T> {
  if (!state.current || state.current.phase !== 'failed') return state
  return advance({ current: null, queue: state.queue })
}

/** 移除一个等待项；current 不能被移除。 */
export function removeWaiting<T extends QueueItem>(state: QueueState<T>, key: string): QueueState<T> {
  return { current: state.current, queue: state.queue.filter((item) => item.key !== key) }
}

/** 取消全部：清空队列并标记 current 为取消（由视图层中止在途请求）。 */
export function cancelAll<T extends QueueItem>(state: QueueState<T>): QueueState<T> {
  if (!state.current) return emptyQueue<T>()
  return { current: { ...state.current, phase: 'cancelled' }, queue: [] }
}

/** 暂停：current 保留，队首不动，后续等待项不动。 */
export function pauseCurrent<T extends QueueItem>(state: QueueState<T>): QueueState<T> {
  if (!state.current || state.current.phase !== 'uploading') return state
  return { current: { ...state.current, phase: 'paused' }, queue: state.queue }
}

/**
 * 建会话请求返回后该怎么做。
 *
 * pauseCurrent 会生成一个新的 current 对象，runEntry 手里的 entry 只是
 * 发起请求时的旧快照；暂停/取消/切换后必须以队列里的实际相位为准，
 * 否则会出现"已暂停却继续上传"或"已取消仍推进旧任务"的竞态。
 */
export type SessionDecision = 'continue' | 'pause' | 'abandon'

export function decideAfterSession<T extends QueueItem>(
  state: QueueState<T>,
  key: string
): SessionDecision {
  const current = state.current
  if (!current || current.key !== key) return 'abandon'
  if (current.phase === 'uploading') return 'continue'
  if (current.phase === 'paused') return 'pause'
  return 'abandon'
}

/**
 * 文档级回执守卫（重建索引、取消入库等）。
 *
 * 与 acceptsResponse 的区别：这些请求还带着文档 ID，跳过/取消当前后
 * 队列可能已经推进到下一个上传条目，仅比对 key 不够，必须同时确认
 * 当前项的 docId 与预期阶段，否则迟到的成功回执会把新条目强置为 ingesting。
 */
export interface DocReceipt {
  key: string
  docId: string
}

export interface DocReceiptTarget {
  key: string
  docId: string
  phase: QueuePhase
}

export function acceptsEntryReceipt(
  receipt: DocReceipt,
  current: DocReceiptTarget | null | undefined,
  phases: QueuePhase[],
  disposed = false
): boolean {
  if (disposed || !current) return false
  if (!receipt.docId || current.docId !== receipt.docId) return false
  if (current.key !== receipt.key) return false
  return phases.includes(current.phase)
}

/**
 * 迟到响应守卫：只有响应对应的 key 仍是当前项且仍处于预期阶段时才接受。
 * 防止取消/切换后旧请求把新任务的状态覆盖。
 */
export function acceptsResponse<T extends QueueItem>(state: QueueState<T>, key: string, phases: QueuePhase[]): boolean {
  return !!state.current && state.current.key === key && phases.includes(state.current.phase)
}

/** 队列摘要，用于界面上的"还有 N 个等待"。 */
export function queueSummary<T extends QueueItem>(state: QueueState<T>): { waiting: number; total: number } {
  return { waiting: state.queue.length, total: state.queue.length + (state.current ? 1 : 0) }
}
