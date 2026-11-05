/**
 * 会话摘要（memory）的前端状态控制器：纯 TS、无 Vue。把"何时读取、何时轮询、
 * 何时重置"的代次判定收拢到这里，注入 load/reset/定时器/时钟后可在 Node 直接测试
 * 真实异步路径；组件只做薄绑定（把 watcher 事件转发进来、把 onView 折成展示状态）。
 *
 * 代次（epoch）+ 存活（active）+ 会话 ID 三者的 `current()` 复核是核心：任何异步完成点
 * （延迟的 GET、轮询、reset 响应）都按"发起时冻结的那一代"复核，杜绝迟到结果污染
 * 当前会话、或在卸载/切会话后复活旧轮询。轮询有真实墙钟 deadline 与次数上限。
 *
 * reset 采用"先冻结、再确认后提交"：freezeReset() 在显示确认框前冻结
 * {conversationId, epoch, version}；确认后 commit() 只在仍是同一代次/会话才发，
 * 绝不会用别的会话的 version 重置当前会话，也不会覆盖确认期间刚发布的新版。
 * freezeReset 未提交期间再次调用返回 null（防重复确认堆叠）。
 */
import type { MemoryState } from './memoryStatus.ts'
import { resetExpectedVersion, shouldPollMemory } from './memoryStatus.ts'

export interface MemoryView {
  enabled: boolean
  state: MemoryState
  covered_turn_seq: number
  version: number | null
  lag?: boolean | null
  truncated?: boolean | null
  error_message?: string | null
}

export interface MemoryControllerDeps {
  /** 读取摘要；返回 null 表示读取失败/不可用（保持现状，不猜状态）。 */
  load: (conversationId: string, signal: AbortSignal) => Promise<MemoryView | null>
  /** 重置；返回结果分类，调用方据此提示。 */
  reset: (conversationId: string, expectedVersion: number) => Promise<'ok' | 'conflict' | 'error'>
  /** 状态更新通知（加载到一份 effective 视图时）。 */
  onView: (view: MemoryView) => void
  /** 无 effective 视图时（例如切会话清空）清展示。 */
  onClear?: () => void
  /** 加载进行中/结束（按代次归属，只在当前代生效）。 */
  onLoading?: (loading: boolean) => void
  /** 有界轮询走到顶：通知 UI 显示手动刷新。 */
  onPollExpired?: () => void
  now?: () => number
  schedule?: (fn: () => void, ms: number) => () => void
}

const POLL_INTERVAL_MS = 2500
const POLL_MAX_ATTEMPTS = 24
const POLL_MAX_MS = 60000

export type ResetOutcome = 'ok' | 'conflict' | 'error' | 'stale'

export interface FrozenReset {
  conversationId: string
  epoch: number
  version: number
  /** 确认后提交；上下文已变/已卸载返回 'stale' 且不发请求。 */
  commit: () => Promise<ResetOutcome>
}

export class MemoryController {
  private epoch = 0
  private active = true
  private busy = false
  private conversationId: string | null = null
  private visible = false
  private lastView: MemoryView | null = null
  private pollTimer?: () => void
  private pollAttempts = 0
  private pollStartAt = 0
  private loadController: AbortController | null = null
  private pendingReset: FrozenReset | null = null

  private readonly deps: MemoryControllerDeps
  constructor(deps: MemoryControllerDeps) { this.deps = deps }

  private get now(): number { return this.deps.now?.() ?? Date.now() }

  private schedule(fn: () => void, ms: number): () => void {
    if (this.deps.schedule) return this.deps.schedule(fn, ms)
    const id = setTimeout(fn, ms)
    return () => clearTimeout(id)
  }

  /** 卸载：作废一切进行中的读取/轮询/重置代次。 */
  dispose(): void {
    this.active = false
    this.epoch += 1
    this.clearPoll()
    this.abortLoad()
  }

  /** 会话/可见性变化：作废旧代次，整段重新读取。同值变化为 no-op。 */
  onContext(visible: boolean, conversationId: string | null): void {
    if (this.visible === visible && this.conversationId === conversationId) return
    this.visible = visible
    this.conversationId = conversationId
    this.epoch += 1
    this.clearPoll()
    this.abortLoad()
    this.pollAttempts = 0
    if (!this.visible || !conversationId) {
      this.lastView = null
      this.deps.onClear?.()
      return
    }
    this.readAndMaybePoll(true)
  }

  /** 本轮 chat 生成/恢复结束（busy true→false）：摘要可能刚发布，重新读取。 */
  onBusyEnd(): void {
    if (!this.visible || !this.conversationId) return
    this.refresh()
  }

  /** 父级显式刷新信号（SSE memory 快照 / 手动刷新）。 */
  onRefresh(): void {
    this.refresh()
  }

  /** 记录生成/恢复中（busy）。确认框打开期间同会话开始生成时，提交阶段据此拒绝。 */
  setBusy(busy: boolean): void {
    this.busy = busy
  }

  private refresh(): void {
    if (!this.active || !this.visible || !this.conversationId) return
    this.readAndMaybePoll(false)
  }

  /** 新代次读取；clearBefore 表示清空旧展示（切会话），否则保留（busy/refresh）。 */
  private readAndMaybePoll(clearBefore: boolean): void {
    this.epoch += 1
    this.clearPoll()
    this.abortLoad()
    this.pollAttempts = 0
    const myEpoch = this.epoch
    const conv = this.conversationId
    void this.doLoad(clearBefore).then(() => {
      // 不要依赖 doLoad 上一步返回的过时布尔（那是上一微任务的快照）：
      // 排 poll 前按冻结的代次+会话+存活复核，迟到的载荷绝不能复活轮询。
      if (conv !== null && this.current(myEpoch, conv)) this.beginPolling()
    })
  }

  private current(myEpoch: number, conv: string): boolean {
    return this.active && this.epoch === myEpoch && this.conversationId === conv
  }

  private async doLoad(clearBefore: boolean): Promise<boolean> {
    const myEpoch = this.epoch
    const conv = this.conversationId
    if (!this.visible || !conv) return false
    this.abortLoad()
    const controller = new AbortController()
    this.loadController = controller
    this.deps.onLoading?.(true)
    if (clearBefore && this.current(myEpoch, conv)) {
      this.lastView = null
      this.deps.onClear?.()
    }
    let view: MemoryView | null = null
    try { view = await this.deps.load(conv, controller.signal) }
    catch { view = null }
    if (this.current(myEpoch, conv)) {
      this.deps.onLoading?.(false)
      if (view) {
        this.lastView = view
        this.deps.onView(view)
      }
    }
    if (this.loadController === controller) this.loadController = null
    return this.current(myEpoch, conv)
  }

  private beginPolling(): void {
    if (!this.active || !this.visible || !this.conversationId) return
    if (!shouldPollMemory(this.lastView)) return
    this.pollAttempts = 0
    this.pollStartAt = this.now
    this.schedulePoll()
  }

  private schedulePoll(): void {
    this.clearPoll()
    if (!this.active || !this.visible || !this.conversationId) return
    const myEpoch = this.epoch
    const conv = this.conversationId
    // 轮询闭包绑定发起时的代次+会话：即使后面切会话/卸载，跑起来也会先复核代次。
    this.pollTimer = this.schedule(() => { void this.runPoll(myEpoch, conv) }, POLL_INTERVAL_MS)
  }

  private async runPoll(myEpoch: number, conv: string): Promise<void> {
    if (!this.current(myEpoch, conv)) return
    const ok = await this.doLoad(false)
    if (!ok || !this.current(myEpoch, conv)) return
    if (!shouldPollMemory(this.lastView)) return
    this.pollAttempts += 1
    if (this.pollAttempts >= POLL_MAX_ATTEMPTS || this.now - this.pollStartAt >= POLL_MAX_MS) {
      this.clearPoll()
      this.deps.onPollExpired?.()
      return
    }
    this.schedulePoll()
  }

  private clearPoll(): void { this.pollTimer?.(); this.pollTimer = undefined }
  private abortLoad(): void { this.loadController?.abort(); this.loadController = null }

  /**
   * 在确认框之前冻结一次重置目标。已有未提交的冻结（pendingReset）时返回 null，
   * 阻止重复确认堆叠。确认后调用返回的 commit()；取消则调用 abortReset()。
   */
  freezeReset(): FrozenReset | null {
    if (this.pendingReset) return null
    if (!this.active || !this.visible || !this.conversationId) return null
    if (!this.lastView || !this.lastView.enabled) return null
    const target = {
      conversationId: this.conversationId,
      epoch: this.epoch,
      version: resetExpectedVersion(this.lastView.version ?? null)
    }
    const frozen: FrozenReset = {
      conversationId: target.conversationId,
      epoch: target.epoch,
      version: target.version,
      commit: async (): Promise<ResetOutcome> => {
        if (this.pendingReset !== frozen) return 'stale'
        this.pendingReset = null
        // 确认期间切会话/卸载/代次变/同会话开始生成(busy) → 不再发，绝不误重置。
        if (!this.current(target.epoch, target.conversationId) || this.busy) return 'stale'
        const outcome = await this.deps.reset(target.conversationId, target.version)
        if (!this.current(target.epoch, target.conversationId)) return 'stale'
        return outcome
      }
    }
    this.pendingReset = frozen
    return frozen
  }

  abortReset(): void { this.pendingReset = null }

  get pollScheduled(): boolean { return this.pollTimer !== undefined }
}
