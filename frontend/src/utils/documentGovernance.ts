/**
 * 文档治理（governance）纯逻辑：日期规范化、PATCH 请求计划、替代/重新启用请求体、
 * 可测试的并发写入控制器。
 *
 * 冻结契约要点：
 * - expected_revision 必须来自打开表单时的快照；CAS 冲突返回 409，绝不静默重试
 * - 日期必须产出带时区的 ISO 字符串（aware）；绝不发送 naive/时间戳
 * - maintainer 是描述性文本（≤200），不是权限，也不是任何 ACL
 * - 已替代（superseded）不能通过 enabled=true 绕过；重新启用必须显式调用
 *
 * 这里刻意不依赖 api/axios/vue，方便在 node 测试里直接跑。
 */

export type LifecycleState = 'active' | 'scheduled' | 'expired' | 'disabled' | 'superseded'

/** maintainer 与后端一致的长度上限。 */
export const GOVERNANCE_MAINTAINER_MAX = 200

export interface GovernanceSnapshot {
  /** 快照里的治理版本号；仅在 revision_known 为 true 时可信。 */
  governance_revision: number
  /** 服务端给出了有效（非负整数）的 governance_revision 才为 true；否则禁止一切治理写入。 */
  revision_known: boolean
  maintainer: string
  last_reviewed_at: string | null
  valid_from: string | null
  valid_until: string | null
  /** true/false；服务端缺失或脏数据时为 null（未知），绝不伪造成 true。 */
  enabled: boolean | null
}

export interface GovernancePatchBody {
  expected_revision: number
  maintainer?: string
  last_reviewed_at?: string | null
  valid_from?: string | null
  valid_until?: string | null
  enabled?: boolean
}

export interface SupersedeBody {
  expected_revision: number
  replacement_document_id: string
}

export interface ReenableBody {
  expected_revision: number
}

/**
 * 表单里的可编辑字段。日期字段保存"原始输入"（datetime-local 文本或 aware ISO），
 * 空字符串表示显式清除；解析失败保留原始错误，绝不静默变成 null。
 */
export interface GovernanceFormState {
  maintainer: string
  last_reviewed_at: string
  valid_from: string
  valid_until: string
  enabled: boolean | null
}

export type GovernanceDateField = 'last_reviewed_at' | 'valid_from' | 'valid_until'

export type GovernancePatchPlan =
  | { status: 'ok'; body: GovernancePatchBody }
  | { status: 'empty' }
  | { status: 'invalidDate'; field: GovernanceDateField }
  | { status: 'invalidWindow' }
  | { status: 'maintainerTooLong' }
  | { status: 'invalidRevision' }
  | { status: 'invalidEnabled' }
  /** 已替代文档仍可改普通元数据，但 enabled 只能通过显式 reenable 恢复。 */
  | { status: 'supersededEnabledChange' }

/** datetime-local 文本：YYYY-MM-DDTHH:mm（秒/小数秒可选），T 也接受空格形式。 */
const DATETIME_LOCAL_RE =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$/
/**
 * 带 UTC（Z）或显式偏移的 ISO 时刻；秒必需（与 Date 的 ISO 形态一致）。
 * 小数秒最多 6 位：后端 datetime.isoformat() 会输出微秒（如 .123456+00:00）。
 */
const AWARE_ISO_RE =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:Z|[+-](\d{2}):(\d{2}))$/

/** 小数秒按毫秒截断（'123456' → 123）：控件与 Date 都只到毫秒，绝不四舍五入。 */
function fractionToMs(fraction: string | undefined): number {
  if (!fraction) return 0
  return Number((fraction + '000').slice(0, 3))
}

function isLeapYear(year: number): boolean {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
}

function daysInMonth(year: number, month: number): number {
  const lengths = [31, isLeapYear(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return lengths[month - 1] ?? 0
}

/** 真实日历校验：Date 会把 2026-02-30 归一化成 3 月，绝不能依赖它。 */
function validCalendarParts(
  y: number, mo: number, d: number, h: number, mi: number, s: number
): boolean {
  return mo >= 1 && mo <= 12 && d >= 1 && d <= daysInMonth(y, mo) &&
    h >= 0 && h <= 23 && mi >= 0 && mi <= 59 && s >= 0 && s <= 59
}

/**
 * 把日期输入严格解析成 aware UTC ISO 字符串。
 *
 * - null / undefined / 空白 → { ok: true, iso: null }（显式清除）
 * - datetime-local 文本按本地时区解析；带 Z/偏移的 ISO 按其时区解析
 * - '0'、时间戳数字、无时区 ISO（naive）、越界或坏日历日期（如 2026-02-30）
 *   一律 { ok: false }，绝不归一化、绝不静默清除
 * - 秒与微秒表示的时间点精确保留
 */
export function parseGovernanceDate(
  value: string | null | undefined
): { ok: true; iso: string | null } | { ok: false } {
  const exact = parseGovernanceDateExact(value)
  return exact.ok ? { ok: true, iso: exact.iso } : { ok: false }
}

function fractionToMicros(fraction: string | undefined): { value: number; digits: number } {
  if (!fraction) return { value: 0, digits: 0 }
  const digits = fraction.length
  return { value: Number((fraction + '000000').slice(0, 6)), digits }
}

function exactUtcIso(date: Date, fraction: { value: number; digits: number }): string {
  const iso = date.toISOString()
  return fraction.digits > 3
    ? iso.replace(/\.\d{3}Z$/, `.${String(fraction.value).padStart(6, '0')}Z`)
    : iso
}

/**
 * 返回精确保留微秒的 UTC ISO，并给出微秒（bigint）时刻供窗口比较使用。
 * 例：from=.123456 / until=.123457 是合法窗口，但毫秒截断后两者相等，
 * 若用 Date 比较就会被误判为 invalidWindow。
 */
export function parseGovernanceDateExact(
  value: string | null | undefined
): { ok: true; micros: bigint | null; iso: string | null; fractionDigits: number } | { ok: false } {
  return parseGovernanceInternal(value)
}

function parseGovernanceInternal(
  value: string | null | undefined
): { ok: true; micros: bigint | null; iso: string | null; fractionDigits: number } | { ok: false } {
  if (value === null || value === undefined) return { ok: true, micros: null, iso: null, fractionDigits: 0 }
  if (typeof value !== 'string') return { ok: false }
  const text = value.trim()
  if (!text) return { ok: true, micros: null, iso: null, fractionDigits: 0 }

  // datetime-local 文本（可带空格代替 T）；浏览器控件只能产出这种形态
  const local = DATETIME_LOCAL_RE.exec(text)
  if (local) {
    const y = Number(local[1])
    const mo = Number(local[2])
    const d = Number(local[3])
    const h = Number(local[4])
    const mi = Number(local[5])
    const s = local[6] !== undefined ? Number(local[6]) : 0
    const frac = fractionToMicros(local[7])
    const ms = fractionToMs(local[7])
    if (!validCalendarParts(y, mo, d, h, mi, s)) return { ok: false }
    const date = new Date(y, mo - 1, d, h, mi, s, ms)
    if (Number.isNaN(date.getTime())) return { ok: false }
    // 本地时区的 DST 空缺/归一化也会在这里暴露：任一分量不一致即拒绝
    if (date.getFullYear() !== y || date.getMonth() !== mo - 1 || date.getDate() !== d ||
      date.getHours() !== h || date.getMinutes() !== mi || date.getSeconds() !== s ||
      date.getMilliseconds() !== ms) {
      return { ok: false }
    }
    return {
      ok: true,
      // 本地时区已由 date 吸收；微秒部分直接加回截断掉的亚毫秒，保持精确比较
      micros: BigInt(date.getTime()) * BigInt(1000) + BigInt(frac.value % 1000),
      iso: exactUtcIso(date, frac),
      fractionDigits: frac.digits
    }
  }

  const aware = AWARE_ISO_RE.exec(text)
  if (aware) {
    const y = Number(aware[1])
    const mo = Number(aware[2])
    const d = Number(aware[3])
    const h = Number(aware[4])
    const mi = Number(aware[5])
    const s = Number(aware[6])
    if (!validCalendarParts(y, mo, d, h, mi, s)) return { ok: false }
    const frac = fractionToMicros(aware[7])
    let offsetMinutes = 0
    if (aware[8] !== undefined) {
      const offsetHour = Number(aware[8])
      const offsetMinute = Number(aware[9])
      if (offsetHour > 23 || offsetMinute > 59) return { ok: false }
      const sign = text.slice(-6, -5) === '-' ? -1 : 1
      offsetMinutes = sign * (offsetHour * 60 + offsetMinute)
    }
    // 手工换算而非 new Date(text)：部分引擎不接受 6 位小数秒，会静默判为 Invalid Date
    const date = new Date(0)
    date.setUTCFullYear(y, mo - 1, d)
    date.setUTCHours(h, mi, s, fractionToMs(aware[7]))
    const utcMs = date.getTime() - offsetMinutes * 60000
    if (Number.isNaN(utcMs)) return { ok: false }
    return {
      ok: true,
      micros: BigInt(utcMs) * BigInt(1000) + BigInt(frac.value % 1000),
      iso: exactUtcIso(new Date(utcMs), frac),
      fractionDigits: frac.digits
    }
  }

  return { ok: false }
}

/** 把 aware ISO 渲染成 datetime-local 控件文本（本地时区、精确到秒，不含毫秒）。 */
export function formatLocalGovernanceInput(value: string | null | undefined): string {
  if (!value) return ''
  const parsed = parseGovernanceDate(value)
  if (!parsed.ok || parsed.iso === null) return ''
  const date = new Date(parsed.iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/**
 * 两个日期输入在控件可表示的精度（秒）内是否表示同一时刻。
 * 控件只有秒精度；用它判断"用户其实没改"，从而保留原始毫秒值。
 */
export function sameGovernanceInputInstant(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return !a && !b
  const left = parseGovernanceDate(a)
  const right = parseGovernanceDate(b)
  if (!left.ok || !right.ok || left.iso === null || right.iso === null) return false
  return formatLocalGovernanceInput(left.iso) === formatLocalGovernanceInput(right.iso)
}

/** 治理写入的响应身份：只有本次会话、同一 KB/文档的响应才允许落地。 */
export interface GovernanceRequestIdentity {
  kbId: string
  docId: string
  epoch: number
}

export interface GovernanceSessionState {
  kbId: string
  epoch: number
  open: boolean
  disposed: boolean
  /** 当前表单绑定的文档 id（打开时捕获，之后随最新详情更新但 id 不变）。 */
  docId: string | null
}

/**
 * 在途请求的会话是否仍然有效：KB、文档、打开代次一致，且未关闭、未卸载。
 * 关闭/切 KB/切文档/卸载/重新打开都会让代次变化，迟到的结果一律作废。
 */
export function isGovernanceSessionCurrent(
  request: GovernanceRequestIdentity | null,
  state: GovernanceSessionState
): boolean {
  return !!request && !state.disposed && state.open &&
    request.epoch === state.epoch &&
    request.kbId === state.kbId &&
    request.docId === state.docId
}

/**
 * 响应是否属于当前打开会话：会话仍有效，且响应自身的 id/kb 与请求一致。
 * 错 ID/KB、旧代次、已关闭、已卸载一律拒绝。
 */
export function belongsToGovernanceSession(
  request: GovernanceRequestIdentity | null,
  state: GovernanceSessionState,
  response: { id: string; kb_id: string }
): boolean {
  return isGovernanceSessionCurrent(request, state) &&
    response.id === request!.docId &&
    response.kb_id === request!.kbId
}

/**
 * CAS 冲突（或刷新失败）后立刻作废版本号：旧 revision 绝不能再用于提交，
 * 只有重新拉到"属于本会话且 revision 有效"的详情才会恢复。其余字段原样保留。
 */
export function invalidateGovernanceRevision(
  snapshot: GovernanceSnapshot | null
): GovernanceSnapshot | null {
  if (!snapshot) return null
  return { ...snapshot, revision_known: false, governance_revision: 0 }
}

/** 原地替换与响应同 id 的行；找不到或 id 不匹配时不做任何改动。 */
export function replaceGovernanceRow<T extends { id: string }>(rows: T[], updated: T): boolean {
  const index = rows.findIndex((row) => row.id === updated.id)
  if (index < 0 || rows[index].id !== updated.id) return false
  rows.splice(index, 1, updated)
  return true
}

/** 从文档行提取治理快照；缺失/脏的 revision 与 enabled 都明确记为"未知"。 */
export function toGovernanceSnapshot(doc: {
  governance_revision?: unknown
  maintainer?: unknown
  last_reviewed_at?: unknown
  valid_from?: unknown
  valid_until?: unknown
  enabled?: unknown
} | null | undefined): GovernanceSnapshot {
  const raw = doc?.governance_revision
  const revisionKnown = typeof raw === 'number' && Number.isFinite(raw) &&
    Number.isInteger(raw) && raw >= 0
  return {
    governance_revision: revisionKnown ? raw : 0,
    revision_known: revisionKnown,
    maintainer: typeof doc?.maintainer === 'string' ? doc.maintainer : '',
    last_reviewed_at: typeof doc?.last_reviewed_at === 'string' ? doc.last_reviewed_at : null,
    valid_from: typeof doc?.valid_from === 'string' ? doc.valid_from : null,
    valid_until: typeof doc?.valid_until === 'string' ? doc.valid_until : null,
    enabled: doc?.enabled === true ? true : doc?.enabled === false ? false : null
  }
}

/** 时刻是否同一瞬间（精确到微秒）；任一解析失败视为不等。 */
function sameInstant(a: string | null, b: string | null): boolean {
  if (a === b) return true
  if (!a || !b) return false
  const left = parseGovernanceDateExact(a)
  const right = parseGovernanceDateExact(b)
  if (!left.ok || !right.ok) return false
  if (left.micros === null || right.micros === null) return left.micros === right.micros
  return left.micros === right.micros
}

/** 时刻（aware ISO 或 null）转精确微秒；未知/坏值返回 null。 */
function instantMicros(value: string | null): bigint | null {
  if (!value) return null
  const parsed = parseGovernanceDateExact(value)
  if (!parsed.ok) return null
  return parsed.micros
}

/**
 * 计算治理 PATCH 请求体：只包含相对快照发生变化的字段。
 *
 * - revision 未知 → { status: 'invalidRevision' }（绝不拿 0 去赌 CAS）
 * - 任何字段都没变 → { status: 'empty' }（后端拒绝空补丁，客户端先挡住）
 * - 日期解析失败 → { status: 'invalidDate' }（保留字段名；原始输入不会被改成 null）
 * - 合并快照与表单后的有效期窗口不是 from < until → { status: 'invalidWindow' }
 * - maintainer 超长 → { status: 'maintainerTooLong' }
 * - enabled 不是严格布尔（未知未解决）→ { status: 'invalidEnabled' }
 */
export function planGovernancePatch(
  snapshot: GovernanceSnapshot,
  form: GovernanceFormState,
  options?: { superseded?: boolean }
): GovernancePatchPlan {
  if (!snapshot.revision_known) return { status: 'invalidRevision' }
  if (options?.superseded && form.enabled !== snapshot.enabled) {
    // 已替代时 enabled 一律不可写；恢复检索必须走显式 reenable
    if (form.enabled !== true && form.enabled !== false) return { status: 'invalidEnabled' }
    return { status: 'supersededEnabledChange' }
  }

  const maintainer = typeof form.maintainer === 'string' ? form.maintainer.trim() : ''
  if (maintainer.length > GOVERNANCE_MAINTAINER_MAX) return { status: 'maintainerTooLong' }

  const review = parseGovernanceDate(form.last_reviewed_at)
  if (!review.ok) return { status: 'invalidDate', field: 'last_reviewed_at' }
  const from = parseGovernanceDate(form.valid_from)
  if (!from.ok) return { status: 'invalidDate', field: 'valid_from' }
  const until = parseGovernanceDate(form.valid_until)
  if (!until.ok) return { status: 'invalidDate', field: 'valid_until' }

  // "用户是否改过"按控件精度（秒）判断：没改就整字段不发，原值（含微秒）原样保留，
  // 绝不因控件只有秒精度而把 .123456 改写成 .123。
  const reviewChanged = !sameGovernanceInputInstant(form.last_reviewed_at, snapshot.last_reviewed_at)
  const fromChanged = !sameGovernanceInputInstant(form.valid_from, snapshot.valid_from)
  const untilChanged = !sameGovernanceInputInstant(form.valid_until, snapshot.valid_until)

  const body: GovernancePatchBody = { expected_revision: snapshot.governance_revision }
  if (maintainer !== snapshot.maintainer.trim()) body.maintainer = maintainer
  if (reviewChanged) body.last_reviewed_at = review.iso
  if (fromChanged) body.valid_from = from.iso
  if (untilChanged) body.valid_until = until.iso
  if (form.enabled !== snapshot.enabled) {
    if (form.enabled !== true && form.enabled !== false) return { status: 'invalidEnabled' }
    body.enabled = form.enabled
  }

  // 窗口比较与实际发送的精确 ISO 一致；未改字段仍保留服务端原始微秒值。
  const mergedFrom = fromChanged ? from.iso : snapshot.valid_from
  const mergedUntil = untilChanged ? until.iso : snapshot.valid_until
  const fromMicros = instantMicros(mergedFrom)
  const untilMicros = instantMicros(mergedUntil)
  if (fromMicros !== null && untilMicros !== null && fromMicros >= untilMicros) {
    return { status: 'invalidWindow' }
  }

  if (Object.keys(body).length === 1) return { status: 'empty' }
  return { status: 'ok', body }
}

/** axios/网络错误是否为治理写入的 409 版本冲突。 */
export function isGovernanceConflict(error: unknown): boolean {
  const status = (error as { response?: { status?: unknown } } | null | undefined)?.response?.status
  return status === 409
}

export type SupersedePlan =
  | { status: 'ok'; body: SupersedeBody }
  | { status: 'empty' }
  | { status: 'self' }
  | { status: 'invalidRevision' }

/** 严格版本号：NaN / 浮点 / 负数 / 布尔 / 字符串一律拒绝，绝不钳制为 0。 */
function strictRevision(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
}

/** 替代请求体：替代者必须是同库的另一篇文档，不能是自己。 */
export function buildSupersedeBody(
  expectedRevision: unknown,
  documentId: string,
  replacementDocumentId: string
): SupersedePlan {
  const revision = strictRevision(expectedRevision)
  if (revision === null) return { status: 'invalidRevision' }
  const replacement = typeof replacementDocumentId === 'string' ? replacementDocumentId.trim() : ''
  const self = typeof documentId === 'string' ? documentId.trim() : ''
  if (!replacement) return { status: 'empty' }
  if (replacement === self) return { status: 'self' }
  return { status: 'ok', body: { expected_revision: revision, replacement_document_id: replacement } }
}

export type ReenablePlan =
  | { status: 'ok'; body: ReenableBody }
  | { status: 'invalidRevision' }

export function buildReenableBody(expectedRevision: unknown): ReenablePlan {
  const revision = strictRevision(expectedRevision)
  if (revision === null) return { status: 'invalidRevision' }
  return { status: 'ok', body: { expected_revision: revision } }
}

/** 行上是否处于替代状态（替代标记或替代链接任一存在）。 */
export function isSuperseded(doc: { superseded_at?: unknown; superseded_by_id?: unknown } | null | undefined): boolean {
  return !!doc && (!!doc.superseded_at || !!doc.superseded_by_id)
}

/** 未知/旧响应的生命周期兜底；仅用于渲染标签，不做任何放行判断。 */
export function normalizeLifecycleState(value: unknown): LifecycleState {
  return value === 'scheduled' || value === 'expired' || value === 'disabled' || value === 'superseded'
    ? value
    : 'active'
}

/** Element Plus tag 类型映射（纯展示）。 */
export function lifecycleTagType(state: LifecycleState): 'success' | 'primary' | 'warning' | 'danger' | 'info' {
  if (state === 'active') return 'success'
  if (state === 'scheduled') return 'primary'
  if (state === 'expired') return 'warning'
  if (state === 'disabled') return 'danger'
  return 'info'
}

// ---------- 可测试的治理写入控制器 ----------

/**
 * 一次治理写入期间注入的钩子。alive 在每个 await 之后都会被重查：
 * 弹窗已关闭、切换 KB/文档、组件卸载都会让它变 false，迟到的结果被丢弃。
 */
export interface GovernanceWriteHooks<T> {
  alive: () => boolean
  write: () => Promise<T>
  /** 只有本次请求仍属于当前代次/文档时才会被调用，结果直接来自本次 write。 */
  onSaved: (result: T) => void | Promise<void>
  /** 409 时调用；isCurrent 在自身 await 之后重查，刷新失败应自行阻断后续提交。 */
  onConflict: (isCurrent: () => boolean) => Promise<void> | void
  onError: (error: unknown) => void
}

export type GovernanceWriteStart = 'started' | 'busy' | 'stale'

/**
 * 治理写入的并发守卫控制器（与框架无关，node 可直接测）。
 *
 * - busy 期间拒绝并发启动（双击）
 * - invalidate()（关闭/切 KB/切文档/卸载/重新打开）立即作废在途请求并复位 busy
 * - 每个 await 之后重查代次与 alive：失效后既不调 onSaved/onConflict/onError，也不弹提示
 * - finally 只在"本次仍是活跃请求"时清 busy；被作废时由 invalidate 复位，
 *   因此迟到的旧 finally 不会清掉新请求的 busy
 */
export function createGovernanceWriteController(onBusyChange?: (busy: boolean) => void) {
  let epoch = 0
  let busy = false
  const setBusy = (next: boolean) => {
    if (busy === next) return
    busy = next
    onBusyChange?.(next)
  }
  return {
    invalidate() {
      epoch += 1
      setBusy(false)
    },
    get busy() {
      return busy
    },
    async run<T>(hooks: GovernanceWriteHooks<T>): Promise<GovernanceWriteStart> {
      if (busy) return 'busy'
      // 写入前先查 alive：已关闭/已切文档/已卸载时根本不发请求
      if (!hooks.alive()) return 'stale'
      setBusy(true)
      const at = epoch
      const isCurrent = () => epoch === at && hooks.alive()
      try {
        let result: T
        try {
          result = await hooks.write()
        } catch (error) {
          if (!isCurrent()) return 'stale'
          if (isGovernanceConflict(error)) {
            try {
              await hooks.onConflict(isCurrent)
            } catch {
              /* 冲突已确定；恢复失败由调用方按"不可再用旧版本提交"处理，不吞掉冲突本身 */
            }
          } else {
            hooks.onError(error)
          }
          return 'started'
        }
        if (!isCurrent()) return 'stale'
        await hooks.onSaved(result)
        return 'started'
      } finally {
        if (epoch === at) setBusy(false)
      }
    }
  }
}

export type GovernanceWriteController = ReturnType<typeof createGovernanceWriteController>

// ---------- 同一文档的写入互斥 ----------

/** 文档级写入键：同一 KB 的同一文档共用一个键。 */
export function governanceWriteKey(kbId: string, docId: string): string {
  return `${kbId}\u0000${docId}`
}

export interface GovernanceWriteLock {
  /** 抢占成功返回 true；已被占用返回 false（调用方必须放弃写入）。 */
  acquire(kbId: string, docId: string): boolean
  /** 只释放自己持有的锁；不是持有者时不做任何事。 */
  release(kbId: string, docId: string): void
  isLocked(kbId: string, docId: string): boolean
  clear(): void
}

/**
 * 跨组件的文档级写入锁：治理弹窗的保存/重启用与父组件的"标记被替代"
 * 共享同一把锁，杜绝两个入口对同一文档并行写入（后到的将被 409 打成无谓冲突）。
 * 锁只是客户端互斥，绝不替代服务端 CAS。
 */
export function createGovernanceWriteLock(): GovernanceWriteLock {
  const held = new Set<string>()
  return {
    acquire(kbId, docId) {
      const key = governanceWriteKey(kbId, docId)
      if (held.has(key)) return false
      held.add(key)
      return true
    },
    release(kbId, docId) {
      held.delete(governanceWriteKey(kbId, docId))
    },
    isLocked(kbId, docId) {
      return held.has(governanceWriteKey(kbId, docId))
    },
    clear() {
      held.clear()
    }
  }
}

/** 应用内共享的那把锁：同一次会话里所有治理写入入口都从这里取。 */
export const governanceWriteLock = createGovernanceWriteLock()

// ---------- 替代请求的会话守卫 ----------

export interface SupersedeRequest {
  kbId: string
  docId: string
  /** 本轮替代的序号（由 createSupersedeController 发放）。 */
  epoch: number
  /**
   * 发起时绑定的治理弹窗代次；null 表示独立入口（行菜单），
   * 不随治理弹窗开关而失效，只受卸载/切库/文档消失影响。
   */
  governanceEpoch: number | null
}

export interface SupersedeState {
  disposed: boolean
  kbId: string
  /** 治理弹窗此刻是否打开。 */
  governanceOpen: boolean
  governanceEpoch: number
  /** 治理弹窗此刻绑定的文档 id。 */
  governanceDocId: string | null
  /** 当前列表中仍存在的文档 id。 */
  docIds: readonly string[]
}

/**
 * 替代请求是否仍然有效。绑定治理会话的请求会在弹窗关闭、切换文档
 * 或重新打开（代次变化）时被取消；独立入口只受卸载/切库/文档消失影响。
 */
export function isSupersedeRequestCurrent(
  request: SupersedeRequest,
  state: SupersedeState
): boolean {
  if (state.disposed) return false
  if (state.kbId !== request.kbId) return false
  if (!state.docIds.includes(request.docId)) return false
  if (request.governanceEpoch === null) return true
  return state.governanceOpen &&
    state.governanceEpoch === request.governanceEpoch &&
    state.governanceDocId === request.docId
}

/**
 * 替代轮次控制器：begin() 开始新一轮并作废旧 prompt 的确认；
 * invalidate() 在治理弹窗关闭/换文档/重新打开时取消绑定它的旧 prompt。
 */
export function createSupersedeController(onBusyChange?: (busy: boolean) => void) {
  let epoch = 0
  let busy = false
  const setBusy = (value: boolean) => {
    busy = value
    onBusyChange?.(value)
  }
  return {
    get epoch() { return epoch },
    get busy() { return busy },
    begin(): number {
      epoch += 1
      setBusy(true)
      return epoch
    },
    finish(requestEpoch: number) {
      if (requestEpoch === epoch) setBusy(false)
    },
    invalidate(): number {
      epoch += 1
      setBusy(false)
      return epoch
    }
  }
}

export type SupersedeController = ReturnType<typeof createSupersedeController>

/** 可控的会话状态投影：组件用它构造 belongsToGovernanceSession 的输入。 */
export function governanceSessionState(
  kbId: string,
  epoch: number,
  open: boolean,
  disposed: boolean,
  docId: string | null
): GovernanceSessionState {
  return { kbId, epoch, open, disposed, docId }
}
