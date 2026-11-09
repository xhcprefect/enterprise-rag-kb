/**
 * 项目索引页的主控制器：纯 TS、无 Vue，与 memoryController 同风格，可在 Node 直接
 * 测试真实异步路径。组件只做薄绑定（把事件/输入转发进来、把状态折成展示）。
 *
 * 代次分为两层，互不干扰：
 * - sourceEpoch：只有切源 / 卸载 / 手动同步入队时才自增。它守卫"快照读取 + 同步轮询 +
 *   deferred 确认"，因此轮询能挺过 query/翻页（viewEpoch 变化）而不会误停，但切源/卸载会
 *   立即作废。
 * - viewEpoch：任何视图变化（选源 / 选快照 / 换 query / 翻页 / 刷新）都自增。它守卫
 *   "文件/搜索/原文预览"的迟到响应——只有代次最新且查询逐字段一致才允许落界面。
 *
 * 快照有界轮询：pending/processing 视为"未就绪"，轮询直到出现 ready 快照或走到计划
 * (默认 2.5s 间隔 / 24 次 / 60s) 上限。轮询请求异常/迟到绝不当作"空完成"掩盖真实状态。
 * 只有明确 ready 的快照才启用文件分页与真实内容搜索；文件名筛选不当作内容搜索。
 *
 * cancel/revoke 用 freezeConfirm 先冻结原 {sourceId, snapshotId, sourceEpoch}，确认后
 * commit() 只在仍是同一 sourceEpoch 的同一源且未卸载时才发请求——绝不转而操作新源。
 */
import {
  clampPage,
  clampPageSize,
  clampTopK,
  isLatestResponse,
  isReadyStatus,
  isStalePoll,
  makeProjectQuery,
  normalizeQueryText,
  PROJECT_PAGE_SIZE_MAX,
  PROJECT_TOP_K_MAX,
  PROJECT_TOP_K_MIN,
  sameProjectQuery as sameQuery,
  shouldPollSnapshot,
  type ProjectViewQuery
} from './projectControls.ts'
import type { PageResult } from '@/api/index.ts'
import type {
  ProjectFile,
  ProjectSearchHit,
  ProjectSearchResult,
  ProjectSnapshot,
  ProjectSourcePreview
} from '@/api/projects.ts'

export const PROJECT_POLL_INTERVAL_MS = 2500
export const PROJECT_POLL_MAX_ATTEMPTS = 24
export const PROJECT_POLL_MAX_MS = 60000

export type ProjectViewStatus = 'idle' | 'loading' | 'ok' | 'error' | 'no-ready'

/** 当前选中源的文件/搜索视图（q 为空 = 文件分页；q 非空 = 真实内容搜索）。 */
export interface ProjectViewState {
  sourceId: string | null
  snapshotId: string | null
  /** 是否为 ready 快照视图（只有 ready 才允许浏览/搜索）。 */
  ready: boolean
  page: number
  pageSize: number
  q: string
  kind: 'files' | 'search'
  status: ProjectViewStatus
  total: number
  items: ProjectFile[] | ProjectSearchHit[]
  error: string | null
  /** 是否正有活动同步（pending/processing），据此决定是否继续轮询。 */
  hasActiveSync: boolean
  /** 快照列表里最新的 ready 快照 id；null = 无 ready。 */
  latestReadySnapshotId: string | null
}

export type ConfirmKind = 'cancel-sync' | 'revoke'

export interface ProjectControllerDeps {
  listSnapshots: (sourceId: string, signal: AbortSignal) => Promise<ProjectSnapshot[]>
  loadFiles: (
    sourceId: string,
    opts: { snapshotId: string | null; page: number; pageSize: number; signal: AbortSignal }
  ) => Promise<PageResult<ProjectFile>>
  searchContent: (
    sourceId: string,
    opts: { query: string; snapshotId: string | null; topK: number; signal: AbortSignal }
  ) => Promise<ProjectSearchResult>
  loadPreview: (
    sourceId: string,
    fileVersionId: string,
    opts: { snapshotId: string | null; signal: AbortSignal }
  ) => Promise<ProjectSourcePreview>
  syncSource: (sourceId: string) => Promise<ProjectSnapshot>
  cancelSyncSource: (sourceId: string) => Promise<ProjectSnapshot>
  revokeSource: (sourceId: string) => Promise<unknown>

  onSnapshots?: (sourceId: string, snaps: ProjectSnapshot[]) => void
  onView?: (view: ProjectViewState) => void
  onPreview?: (preview: ProjectSourcePreview | null) => void
  onPreviewLoading?: (loading: boolean) => void
  onPollExpired?: (sourceId: string) => void
  now?: () => number
  schedule?: (fn: () => void, ms: number) => () => void
}

export type ConfirmOutcome = 'ok' | 'stale'

export interface FrozenConfirm {
  kind: ConfirmKind
  sourceId: string
  snapshotId: string | null
  sourceEpoch: number
  commit: () => Promise<ConfirmOutcome>
}

const EMPTY_VIEW = (): ProjectViewState => ({
  sourceId: null,
  snapshotId: null,
  ready: false,
  page: 1,
  pageSize: 20,
  q: '',
  kind: 'files',
  status: 'idle',
  total: 0,
  items: [],
  error: null,
  hasActiveSync: false,
  latestReadySnapshotId: null
})

export class ProjectController {
  private alive = true
  private sourceId: string | null = null
  private sourceEpoch = 0
  private viewEpoch = 0

  // 视图状态
  private snapshotId: string | null = null
  private ready = false
  private page = 1
  private pageSize = 20
  private q = ''
  private latestReadySnapshotId: string | null = null
  private hasActiveSync = false
  private lastLoadedQuery: ProjectViewQuery | null = null
  private lastLoadedError: string | null = null
  /** 最近一次发给页面的完整视图；轻量刷新据此保留文件/搜索结果。 */
  private lastEmittedView: ProjectViewState | null = null

  // 请求/轮询
  private viewController: AbortController | null = null
  private previewController: AbortController | null = null
  private snapshotsController: AbortController | null = null
  private pollTimer?: () => void
  private pollAttempts = 0
  private pollStartAt = 0
  private pendingConfirm: FrozenConfirm | null = null

  private readonly deps: ProjectControllerDeps
  constructor(deps: ProjectControllerDeps) { this.deps = deps }

  private get now(): number { return this.deps.now?.() ?? Date.now() }
  private schedule(fn: () => void, ms: number): () => void {
    if (this.deps.schedule) return this.deps.schedule(fn, ms)
    const id = setTimeout(fn, ms)
    return () => clearTimeout(id)
  }

  private currentQuery(): ProjectViewQuery {
    return makeProjectQuery({
      sourceId: this.sourceId ?? '',
      snapshotId: this.snapshotId,
      page: this.page,
      pageSize: this.pageSize,
      q: this.q,
      maxPageSize: PROJECT_PAGE_SIZE_MAX
    })
  }

  /** 源未变、未卸载、且代次（sourceEpoch）未被切源/卸载推进。 */
  private currentSource(sourceEpoch: number, sourceId: string): boolean {
    return this.alive && this.sourceEpoch === sourceEpoch && this.sourceId === sourceId
  }

  /** 源未变、视图代次也未被 query/翻页/刷新推进。 */
  private currentView(sourceEpoch: number, sourceId: string, viewEpoch: number): boolean {
    return this.currentSource(sourceEpoch, sourceId) && this.viewEpoch === viewEpoch
  }

  /** 卸载：作废一切进行中的读取/轮询/确认代次，清展示。 */
  dispose(): void {
    this.alive = false
    this.sourceEpoch += 1
    this.viewEpoch += 1
    this.clearPoll()
    this.abortView()
    this.abortPreview()
    this.abortSnapshots()
    this.pendingConfirm = null
    this.sourceId = null
    this.emit(EMPTY_VIEW())
    this.deps.onPreview?.(null)
  }

  /**
   * 切换选中源（新建源同理）。整段视图替换：旧代次作废、清快照/分页/query/预览。
   * 读快照决定 ready；存在活动同步则开始有界轮询。
   */
  selectSource(sourceId: string): void {
    this.sourceEpoch += 1
    this.viewEpoch += 1
    this.clearPoll()
    this.abortView()
    this.abortPreview()
    this.abortSnapshots()
    this.pendingConfirm = null
    const mySourceEpoch = this.sourceEpoch
    this.sourceId = sourceId
    this.snapshotId = null
    this.ready = false
    this.page = 1
    this.q = ''
    this.latestReadySnapshotId = null
    this.hasActiveSync = false
    this.lastLoadedQuery = null
    this.lastLoadedError = null
    this.deps.onPreview?.(null)
    if (this.currentSource(mySourceEpoch, sourceId)) {
      this.deps.onSnapshots?.(sourceId, [])
      this.emitView()
    }
    this.loadSnapshotsAndMaybePoll(mySourceEpoch, sourceId)
  }

  /** 在同一源下显式切换快照视图：作废旧分页/query，重新读文件/搜索。 */
  selectSnapshot(snapshotId: string, ready = true): void {
    if (!this.sourceId) return
    if (this.snapshotId === snapshotId && this.ready === ready && this.q === '' && this.page === 1 && this.lastLoadedQuery) return
    const mySourceEpoch = this.sourceEpoch
    this.viewEpoch += 1
    this.abortView()
    this.abortPreview()
    this.snapshotId = snapshotId
    this.ready = ready
    this.page = 1
    this.q = ''
    this.lastLoadedQuery = null
    this.lastLoadedError = null
    this.deps.onPreview?.(null)
    if (!this.currentSource(mySourceEpoch, this.sourceId)) return
    this.emitView()
    this.loadView(mySourceEpoch, this.sourceId, this.viewEpoch, snapshotId, '', 1)
  }

  /** 换 query（真实内容搜索）：trim 后与旧值相同则 no-op；清空则回到文件分页。 */
  setQuery(q: string): void {
    if (!this.sourceId) return
    const next = normalizeQueryText(q)
    if (next === '') {
      if (this.q === '') return
      this.selectSnapshot(this.snapshotId ?? '', this.ready)
      return
    }
    if (next === this.q && this.kind() === 'search' && this.page === 1 && this.lastLoadedQuery !== null) return
    const mySourceEpoch = this.sourceEpoch
    this.viewEpoch += 1
    this.abortView()
    this.q = next
    this.page = 1
    this.lastLoadedQuery = null
    this.lastLoadedError = null
    this.deps.onPreview?.(null)
    if (!this.ready) return
    if (!this.currentSource(mySourceEpoch, this.sourceId)) return
    this.emitView()
    this.loadView(mySourceEpoch, this.sourceId, this.viewEpoch, this.snapshotId, this.q, this.page)
  }

  /** 翻页（文件分页 1-based）；搜索视图不分页。 */
  setPage(page: number, pageSize?: number): void {
    if (!this.sourceId) return
    const p = clampPage(page)
    const size = pageSize !== undefined ? clampPageSize(pageSize, PROJECT_PAGE_SIZE_MAX) : this.pageSize
    if (p === this.page && size === this.pageSize) return
    const mySourceEpoch = this.sourceEpoch
    this.viewEpoch += 1
    this.abortView()
    this.page = p
    this.pageSize = size
    this.lastLoadedQuery = null
    this.lastLoadedError = null
    if (!this.currentSource(mySourceEpoch, this.sourceId)) return
    this.emitView()
    if (this.q !== '') return
    this.loadView(mySourceEpoch, this.sourceId, this.viewEpoch, this.snapshotId, this.q, this.page)
  }

  /**
   * 手动刷新：先重读快照（可能从 no-ready 变为 ready、或出现了新的 ready 快照），
   * 再重载当前文件/搜索视图。no-ready 时也能通过它发现 ready。
   */
  refresh(): void {
    if (!this.sourceId) return
    const mySourceEpoch = this.sourceEpoch
    const sourceId = this.sourceId
    this.viewEpoch += 1
    this.abortView()
    this.lastLoadedQuery = null
    this.lastLoadedError = null
    this.deps.onPreview?.(null)
    this.loadSnapshotsAndMaybePoll(mySourceEpoch, sourceId)
  }

  /**
   * 轻量刷新快照列表（自动同步的空闲轮询用）：只更新快照/最新 ready/活动同步状态，
   * 不推进 viewEpoch、不清 lastLoadedQuery，因此既不重载当前文件/搜索视图，
   * 也不覆盖用户显式选择的旧快照（新 ready 只体现在 latestReadySnapshotId）。
   * 仍用 sourceEpoch + sourceId 守卫迟到响应；读取失败保持既有展示，绝不当作空完成。
   * 若发现活动任务且当前没有轮询在排，则启动现有的有界轮询。
   */
  async refreshSnapshots(): Promise<void> {
    const sourceId = this.sourceId
    if (!this.alive || !sourceId) return
    const sourceEpoch = this.sourceEpoch
    this.abortSnapshots()
    const controller = new AbortController()
    this.snapshotsController = controller
    let snaps: ProjectSnapshot[] | null = null
    try { snaps = await this.deps.listSnapshots(sourceId, controller.signal) }
    catch { snaps = null }
    if (controller.signal.aborted || !this.currentSource(sourceEpoch, sourceId)) return
    if (this.snapshotsController === controller) this.snapshotsController = null
    if (snaps === null) return

    const previousReady = this.ready
    const previousSnapshotId = this.snapshotId
    this.applySnapshots(sourceId, snaps)
    if (!this.currentSource(sourceEpoch, sourceId)) return
    if (this.ready !== previousReady || this.snapshotId !== previousSnapshotId) this.emitView()
    else this.emitRefreshedView()
    if (this.ready && !this.sameAsLoaded() && this.viewController === null) {
      this.loadView(sourceEpoch, sourceId, this.viewEpoch, this.snapshotId, this.q, this.page)
    }
    if (!this.currentSource(sourceEpoch, sourceId)) return
    if (this.hasActiveSync) {
      if (!this.pollScheduled) this.beginPolling(sourceEpoch, sourceId)
    } else {
      this.clearPoll()
    }
  }

  /** 打开固定 fileVersionId 的原文预览（带 AbortController，迟到自动丢弃）。 */
  openPreview(fileVersionId: string): void {
    if (!this.sourceId) return
    this.abortPreview()
    const mySourceEpoch = this.sourceEpoch
    const myViewEpoch = this.viewEpoch
    const sourceId = this.sourceId
    const snapshotId = this.snapshotId
    const controller = new AbortController()
    this.previewController = controller
    this.deps.onPreviewLoading?.(true)
    void this.deps.loadPreview(sourceId, fileVersionId, { snapshotId, signal: controller.signal })
      .then((preview) => {
        if (controller.signal.aborted || !this.currentView(mySourceEpoch, sourceId, myViewEpoch)) return
        this.deps.onPreview?.(preview)
      })
      .catch(() => {
        if (controller.signal.aborted || !this.currentView(mySourceEpoch, sourceId, myViewEpoch)) return
        this.deps.onPreview?.(null)
      })
      .finally(() => {
        if (this.previewController === controller) { this.previewController = null; this.deps.onPreviewLoading?.(false) }
      })
  }

  closePreview(): void {
    this.abortPreview()
    this.deps.onPreview?.(null)
  }

  private kind(): 'files' | 'search' {
    return this.q === '' ? 'files' : 'search'
  }

  private emit(view: ProjectViewState): void {
    this.lastEmittedView = view
    this.deps.onView?.(view)
  }

  private emitView(): void {
    let status: ProjectViewStatus
    if (!this.ready) status = 'no-ready'
    else if (this.viewController !== null) status = 'loading'
    else if (this.lastLoadedQuery && this.sameAsLoaded() && this.lastLoadedError) status = 'error'
    else if (this.lastLoadedQuery && this.sameAsLoaded()) status = 'ok'
    else status = 'loading'
    const view: ProjectViewState = {
      sourceId: this.sourceId,
      snapshotId: this.snapshotId,
      ready: this.ready,
      page: this.page,
      pageSize: this.pageSize,
      q: this.q,
      kind: this.kind(),
      status,
      total: 0,
      items: [],
      error: this.lastLoadedError,
      hasActiveSync: this.hasActiveSync,
      latestReadySnapshotId: this.latestReadySnapshotId
    }
    this.emit(view)
  }

  /**
   * 轻量刷新只更新与当前内容无关的快照字段（最新 ready / 活动同步），
   * 保留已加载的文件/搜索结果与分页，避免 15 秒刷新清空用户视图。
   */
  private emitRefreshedView(): void {
    const previous = this.lastEmittedView
    if (!previous || previous.sourceId !== this.sourceId) {
      this.emitView()
      return
    }
    this.emit({
      ...previous,
      snapshotId: this.snapshotId,
      ready: this.ready,
      hasActiveSync: this.hasActiveSync,
      latestReadySnapshotId: this.latestReadySnapshotId
    })
  }

  private sameAsLoaded(): boolean {
    return !!this.lastLoadedQuery && sameQuery(this.lastLoadedQuery, this.currentQuery())
  }

  /** 快照读取（首次或刷新）；成功、仍当前源、且未卸载时才应用。 */
  private async loadSnapshotsAndMaybePoll(sourceEpoch: number, sourceId: string): Promise<void> {
    this.abortSnapshots()
    const controller = new AbortController()
    this.snapshotsController = controller
    let snaps: ProjectSnapshot[] = []
    try { snaps = await this.deps.listSnapshots(sourceId, controller.signal) }
    catch { snaps = [] }
    if (controller.signal.aborted || !this.currentSource(sourceEpoch, sourceId)) return
    if (this.snapshotsController === controller) this.snapshotsController = null
    this.applySnapshots(sourceId, snaps)
    if (!this.currentSource(sourceEpoch, sourceId)) return
    this.afterSnapshots(sourceEpoch, sourceId)
    // 首次/刷新/手动同步后：进入活动同步则重置预算并开启轮询；否则无轮询。
    if (this.currentSource(sourceEpoch, sourceId) && this.hasActiveSync) this.beginPolling(sourceEpoch, sourceId)
    else this.clearPoll()
  }

  /** 供页面/轮询在收到最新快照列表后回调。绝不自增代次、不覆盖显式选择的旧快照。 */
  applySnapshots(sourceId: string, snaps: ProjectSnapshot[]): void {
    if (sourceId !== this.sourceId) return
    const active = snaps.find((s) => shouldPollSnapshot(s.status))
    const readySnap = snaps.find((s) => isReadyStatus(s.status)) ?? null
    this.hasActiveSync = !!active
    this.latestReadySnapshotId = readySnap?.id ?? null
    // 显式选择的快照若已不再是 ready（被取代/撤销），降级为无快照。
    if (this.ready && this.snapshotId && !snaps.some((s) => s.id === this.snapshotId && isReadyStatus(s.status))) {
      this.ready = false
      this.snapshotId = null
      this.lastLoadedQuery = null
    }
    // 仅当尚未处于 ready 视图（含用户尚未显式选快照）时，自动落到最新 ready。
    if (!this.ready && readySnap) {
      this.ready = true
      this.snapshotId = readySnap.id
      this.lastLoadedQuery = null
    }
    this.deps.onSnapshots?.(sourceId, snaps)
  }

  /** 快照应用后的公共后续：发状态并按需加载视图（轮询由各调用点决定）。 */
  private afterSnapshots(sourceEpoch: number, sourceId: string): void {
    if (!this.currentSource(sourceEpoch, sourceId)) return
    // 快照身份（源/快照/ready）没变时只做轻量发射，保留当前文件/搜索内容；
    // 身份变化（no-ready→ready、快照被降级、切源）才发完整视图。
    const previous = this.lastEmittedView
    const sameIdentity = !!previous &&
      previous.sourceId === this.sourceId &&
      previous.snapshotId === this.snapshotId &&
      previous.ready === this.ready
    if (sameIdentity) this.emitRefreshedView()
    else this.emitView()
    if (this.ready && !this.sameAsLoaded()) {
      this.loadView(sourceEpoch, sourceId, this.viewEpoch, this.snapshotId, this.q, this.page)
    }
  }

  private beginPolling(sourceEpoch: number, sourceId: string): void {
    this.pollAttempts = 0
    this.pollStartAt = this.now
    this.schedulePoll(sourceEpoch, sourceId)
  }

  private schedulePoll(sourceEpoch: number, sourceId: string): void {
    this.clearPoll()
    if (!this.currentSource(sourceEpoch, sourceId)) return
    this.pollTimer = this.schedule(() => { void this.runPoll(sourceEpoch, sourceId) }, PROJECT_POLL_INTERVAL_MS)
  }

  private async runPoll(sourceEpoch: number, sourceId: string): Promise<void> {
    // 本轮定时器已经触发，不再处于"有下一轮在排"状态；轻量刷新可据此恢复轮询。
    this.pollTimer = undefined
    if (isStalePoll({ requestEpoch: sourceEpoch, latestEpoch: this.sourceEpoch, requestSourceId: sourceId, currentSourceId: this.sourceId })) return
    this.abortSnapshots()
    const controller = new AbortController()
    this.snapshotsController = controller
    let snaps: ProjectSnapshot[] | null = null
    try { snaps = await this.deps.listSnapshots(sourceId, controller.signal) }
    catch { snaps = null }
    if (controller.signal.aborted || !this.currentSource(sourceEpoch, sourceId)) return
    if (this.snapshotsController === controller) this.snapshotsController = null
    if (snaps === null) {
      // 轮询请求异常：保留既有快照状态，绝不伪装成"空完成"；仍在预算内则继续排下一轮。
      if (this.currentSource(sourceEpoch, sourceId) && this.hasActiveSync) {
        this.pollAttempts += 1
        if (this.pollAtBudget()) { this.clearPoll(); this.deps.onPollExpired?.(sourceId) }
        else this.schedulePoll(sourceEpoch, sourceId)
      }
      return
    }
    this.applySnapshots(sourceId, snaps)
    if (this.currentSource(sourceEpoch, sourceId)) this.afterSnapshots(sourceEpoch, sourceId)
    if (!this.currentSource(sourceEpoch, sourceId)) return
    if (!this.hasActiveSync) return
    this.pollAttempts += 1
    if (this.pollAtBudget()) { this.clearPoll(); this.deps.onPollExpired?.(sourceId) }
    else this.schedulePoll(sourceEpoch, sourceId)
  }

  private pollAtBudget(): boolean {
    return this.pollAttempts >= PROJECT_POLL_MAX_ATTEMPTS || this.now - this.pollStartAt >= PROJECT_POLL_MAX_MS
  }

  private async loadView(
    sourceEpoch: number,
    sourceId: string,
    viewEpoch: number,
    snapshotId: string | null,
    q: string,
    page: number
  ): Promise<void> {
    if (!this.ready) return
    this.abortView()
    const controller = new AbortController()
    this.viewController = controller
    const size = this.pageSize
    const requestQuery = makeProjectQuery({ sourceId, snapshotId, page, pageSize: size, q, maxPageSize: PROJECT_PAGE_SIZE_MAX })
    if (!this.currentView(sourceEpoch, sourceId, viewEpoch)) return

    this.emit({
      sourceId, snapshotId, ready: true, page, pageSize: size, q,
      kind: q === '' ? 'files' : 'search',
      status: 'loading', total: 0, items: [], error: null,
      hasActiveSync: this.hasActiveSync, latestReadySnapshotId: this.latestReadySnapshotId
    })

    const fail = (err: string | null) => {
      if (controller.signal.aborted) return
      if (!this.currentView(sourceEpoch, sourceId, viewEpoch)) return
      this.lastLoadedQuery = requestQuery
      this.lastLoadedError = err
      this.emit({
        sourceId, snapshotId, ready: true, page, pageSize: size, q,
        kind: q === '' ? 'files' : 'search', status: 'error', total: 0, items: [], error: err,
        hasActiveSync: this.hasActiveSync, latestReadySnapshotId: this.latestReadySnapshotId
      })
    }

    try {
      if (q === '') {
        const result = await this.deps.loadFiles(sourceId, { snapshotId, page, pageSize: size, signal: controller.signal })
        if (controller.signal.aborted) return
        if (!isLatestResponse(viewEpoch, this.viewEpoch, requestQuery, this.currentQuery())) return
        if (!this.currentView(sourceEpoch, sourceId, viewEpoch)) return
        this.lastLoadedQuery = requestQuery
        this.lastLoadedError = null
        this.emit({
          sourceId, snapshotId, ready: true, page, pageSize: size, q,
          kind: 'files', status: 'ok', total: result.total, items: result.items, error: null,
          hasActiveSync: this.hasActiveSync, latestReadySnapshotId: this.latestReadySnapshotId
        })
      } else {
        const result = await this.deps.searchContent(sourceId, {
          query: q,
          snapshotId,
          topK: clampTopK(8, PROJECT_TOP_K_MIN, PROJECT_TOP_K_MAX),
          signal: controller.signal
        })
        if (controller.signal.aborted) return
        if (!isLatestResponse(viewEpoch, this.viewEpoch, requestQuery, this.currentQuery())) return
        if (!this.currentView(sourceEpoch, sourceId, viewEpoch)) return
        this.lastLoadedQuery = requestQuery
        this.lastLoadedError = null
        this.emit({
          sourceId, snapshotId, ready: true, page, pageSize: size, q,
          kind: 'search', status: 'ok', total: result.total, items: result.results, error: null,
          hasActiveSync: this.hasActiveSync, latestReadySnapshotId: this.latestReadySnapshotId
        })
      }
    } catch {
      fail('loadFailed')
    } finally {
      // 结束后清掉 in-flight 标记：后续轻量刷新才能按 lastLoadedQuery 判断是否真的需要重载。
      if (this.viewController === controller) this.viewController = null
    }
  }

  /**
   * 确认框前冻结一次操作目标。已有未提交的冻结时返回 null（防重复确认堆叠）。
   * 确认后调用 commit()；取消则调用 abortConfirm()。
   * 冻结 {sourceId, snapshotId, sourceEpoch}：commit 只在仍是同一 sourceEpoch 的同一源
   * 且未卸载时才发请求——绝不转而操作新源。
   */
  freezeConfirm(kind: ConfirmKind): FrozenConfirm | null {
    if (this.pendingConfirm) return null
    if (!this.alive || !this.sourceId) return null
    const target = { kind, sourceId: this.sourceId, snapshotId: this.snapshotId, sourceEpoch: this.sourceEpoch }
    const frozen: FrozenConfirm = {
      ...target,
      commit: async (): Promise<ConfirmOutcome> => {
        if (this.pendingConfirm !== frozen) return 'stale'
        this.pendingConfirm = null
        if (!this.currentSource(target.sourceEpoch, target.sourceId)) return 'stale'
        if (kind === 'cancel-sync') await this.deps.cancelSyncSource(target.sourceId)
        else await this.deps.revokeSource(target.sourceId)
        if (!this.currentSource(target.sourceEpoch, target.sourceId)) return 'stale'
        return 'ok'
      }
    }
    this.pendingConfirm = frozen
    return frozen
  }

  abortConfirm(): void { this.pendingConfirm = null }

  /** 手动同步入队。不吞错误：失败返回 null 由调用方提示；成功后被源代次守卫才重读快照。 */
  async requestSync(): Promise<ProjectSnapshot | null> {
    const sourceId = this.sourceId
    if (!this.alive || !sourceId) return null
    const mySourceEpoch = this.sourceEpoch
    let snap: ProjectSnapshot
    try {
      snap = await this.deps.syncSource(sourceId)
    } catch {
      return null
    }
    if (!this.currentSource(mySourceEpoch, sourceId)) return null
    this.sourceEpoch += 1
    this.viewEpoch += 1
    this.clearPoll()
    this.abortSnapshots()
    this.lastLoadedQuery = null
    void this.loadSnapshotsAndMaybePoll(this.sourceEpoch, sourceId)
    return snap
  }

  private clearPoll(): void { this.pollTimer?.(); this.pollTimer = undefined }
  private abortView(): void { this.viewController?.abort(); this.viewController = null }
  private abortPreview(): void { this.previewController?.abort(); this.previewController = null }
  private abortSnapshots(): void { this.snapshotsController?.abort(); this.snapshotsController = null }

  get hasPendingConfirm(): boolean { return this.pendingConfirm !== null }
  get pollScheduled(): boolean { return this.pollTimer !== undefined }
}
