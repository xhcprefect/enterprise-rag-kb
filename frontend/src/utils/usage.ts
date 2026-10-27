import type {
  ModelPrice,
  UsageBreakdownItem,
  UsageGranularity,
  UsageMetrics,
  UsageModelKind,
  UsageOperation,
  UsageScope
} from '../api/usage.ts'

export type UsagePreset = 'today' | '7d' | '30d' | '90d'

export interface UsageDateRange {
  start: string
  end: string
}

export type UsageRangeIssue =
  | 'required'
  | 'format'
  | 'invalid'
  | 'bounds'
  | 'order'
  | 'span'
  | 'hourSpan'

export type UsageRangeValidation =
  | { ok: true }
  | { ok: false; code: UsageRangeIssue }

const DAY_MS = 86_400_000
const MIN_YEAR = 2000
const MAX_YEAR = 2100
const MAX_RANGE_DAYS = 366
const MAX_HOURLY_DAYS = 31

export const USAGE_OPERATIONS: readonly UsageOperation[] = [
  'generation',
  'rewrite',
  'query_embedding',
  'document_embedding',
  'rerank'
]

export const USAGE_KINDS: readonly UsageModelKind[] = ['chat', 'embedding', 'rerank']

/** 浏览器时区可能与业务时区不同，统计口径固定按 Asia/Shanghai 自然日。 */
export function shanghaiToday(now: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).formatToParts(now)
  const values: Record<string, string> = {}
  for (const part of parts) {
    if (part.type !== 'literal') values[part.type] = part.value
  }
  return `${values.year}-${values.month}-${values.day}`
}

function dayNumber(value: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  if (month < 1 || month > 12 || day < 1 || day > 31) return null
  const stamp = Date.UTC(year, month - 1, day)
  const date = new Date(stamp)
  if (
    date.getUTCFullYear() !== year
    || date.getUTCMonth() !== month - 1
    || date.getUTCDate() !== day
  ) {
    return null
  }
  return Math.trunc(stamp / DAY_MS)
}

function isoDayFromNumber(value: number): string {
  const date = new Date(value * DAY_MS)
  const year = date.getUTCFullYear()
  const month = String(date.getUTCMonth() + 1).padStart(2, '0')
  const day = String(date.getUTCDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function shiftIsoDay(value: string, days: number): string {
  const number = dayNumber(value)
  if (number === null) return value
  return isoDayFromNumber(number + days)
}

export function daysInclusive(start: string, end: string): number | null {
  const left = dayNumber(start)
  const right = dayNumber(end)
  if (left === null || right === null) return null
  return right - left + 1
}

export function presetRange(preset: UsagePreset, today: string = shanghaiToday()): UsageDateRange {
  const span = preset === 'today' ? 1 : preset === '7d' ? 7 : preset === '90d' ? 90 : 30
  return { start: shiftIsoDay(today, -(span - 1)), end: today }
}

export function recommendedGranularity(preset: UsagePreset): UsageGranularity {
  return preset === 'today' ? 'hour' : 'day'
}

export function validateUsageRange(
  range: UsageDateRange,
  granularity: UsageGranularity
): UsageRangeValidation {
  const start = range.start.trim()
  const end = range.end.trim()
  if (!start || !end) return { ok: false, code: 'required' }

  if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !/^\d{4}-\d{2}-\d{2}$/.test(end)) {
    return { ok: false, code: 'format' }
  }
  const startDay = dayNumber(start)
  const endDay = dayNumber(end)
  if (startDay === null || endDay === null) return { ok: false, code: 'invalid' }

  const startYear = Number(start.slice(0, 4))
  const endYear = Number(end.slice(0, 4))
  if (startYear < MIN_YEAR || startYear > MAX_YEAR || endYear < MIN_YEAR || endYear > MAX_YEAR) {
    return { ok: false, code: 'bounds' }
  }
  if (startDay > endDay) return { ok: false, code: 'order' }

  const days = endDay - startDay + 1
  if (days > MAX_RANGE_DAYS) return { ok: false, code: 'span' }
  if (granularity === 'hour' && days > MAX_HOURLY_DAYS) return { ok: false, code: 'hourSpan' }
  return { ok: true }
}

/** 供粒度下拉禁用选项；区间本身无效时只保留业务上安全的天/月。 */
export function granularityAvailability(range: UsageDateRange): Record<UsageGranularity, boolean> {
  const base = validateUsageRange(range, 'day')
  return {
    hour: base.ok && (daysInclusive(range.start.trim(), range.end.trim()) ?? Number.POSITIVE_INFINITY) <= MAX_HOURLY_DAYS,
    day: base.ok,
    month: base.ok
  }
}

export function coerceGranularity(
  range: UsageDateRange,
  granularity: UsageGranularity
): UsageGranularity {
  return granularityAvailability(range)[granularity] ? granularity : 'day'
}

/** 只读日期用 UTC 解释，避免浏览器时区把 00:00 推到前一天。 */
export function formatUsageDay(value: string, locale: string): string {
  const number = dayNumber(value)
  if (number === null) return '—'
  return new Intl.DateTimeFormat(locale, {
    timeZone: 'UTC',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).format(new Date(number * DAY_MS))
}

export function formatBucketLabel(
  period: string,
  granularity: UsageGranularity,
  locale: string
): string {
  const date = new Date(period)
  if (Number.isNaN(date.getTime())) return '—'
  const options: Intl.DateTimeFormatOptions = granularity === 'hour'
    ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }
    : granularity === 'month'
      ? { year: 'numeric', month: '2-digit' }
      : { month: '2-digit', day: '2-digit' }
  return new Intl.DateTimeFormat(locale, { ...options, timeZone: 'Asia/Shanghai' }).format(date)
}

// ---------- 金额：全部基于十进制字符串，不经过浮点数 ----------

const DECIMAL_RE = /^\d+(?:\.\d+)?$/

function stripLeadingZeros(value: string): string {
  return value.replace(/^0+(?=\d)/, '')
}

function incrementDigits(digits: string): string {
  const chars = digits.split('')
  let index = chars.length - 1
  while (index >= 0) {
    if (chars[index] === '9') {
      chars[index] = '0'
      index -= 1
    } else {
      chars[index] = String(Number(chars[index]) + 1)
      return chars.join('')
    }
  }
  return `1${chars.join('')}`
}

function roundDecimal(intPart: string, fracPart: string, digits: number) {
  const kept = `${intPart}${fracPart.slice(0, digits).padEnd(digits, '0')}`
  const next = fracPart.length > digits ? Number(fracPart.charAt(digits)) : 0
  const rounded = next >= 5 ? incrementDigits(kept) : kept
  const cut = rounded.length - digits
  return { int: rounded.slice(0, cut) || '0', frac: rounded.slice(cut) }
}

export interface CnyFormatOptions {
  /** 最多保留的小数位，默认 6；显示时再裁掉末尾 0 */
  maxFractionDigits?: number
  minFractionDigits?: number
  symbol?: string
  /** null 的占位，默认 em dash */
  empty?: string
}

/**
 * 把后端十进制金额格式化为人民币。null 与非法值显示占位，
 * 真正的 0（例如已配置免费价）显示 ¥0.00，二者不会混淆。
 */
export function formatCny(
  value: string | null | undefined,
  options: CnyFormatOptions = {}
): string {
  const empty = options.empty ?? '—'
  const digits = Math.min(Math.max(options.maxFractionDigits ?? 6, 0), 12)
  const minimum = Math.min(Math.max(options.minFractionDigits ?? 2, 0), digits)
  const symbol = options.symbol ?? '¥'
  const raw = (value ?? '').trim()
  if (!raw || !DECIMAL_RE.test(raw)) return empty

  const [rawInt, rawFrac = ''] = raw.split('.')
  const intPart = stripLeadingZeros(rawInt)
  const { int, frac } = roundDecimal(intPart, rawFrac, digits)
  // 非零小额在允许位数内会被舍入为 0；此时显示“小于阈值”，不能伪装成免费的 ¥0。
  if (/[1-9]/.test(intPart + rawFrac) && int === '0' && !/[1-9]/.test(frac)) {
    const threshold = digits > 0 ? `0.${'0'.repeat(digits - 1)}1` : '1'
    return `<${symbol}${threshold}`
  }
  let shownFrac = frac.replace(/0+$/, '')
  shownFrac = shownFrac.padEnd(minimum, '0')
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${symbol}${grouped}${shownFrac ? `.${shownFrac}` : ''}`
}

/** 单价用相同规则，只是不强制两位小数。 */
export function formatRate(value: string | null | undefined): string {
  return formatCny(value, { minFractionDigits: 0 })
}

/** 用于比较与校验；只接受最多 6 位小数的非负十进制字符串。 */
export function decimalToMicros(value: string): bigint | null {
  const raw = value.trim()
  if (!/^\d+(?:\.\d{1,6})?$/.test(raw)) return null
  const [int, frac = ''] = raw.split('.')
  return BigInt(int) * 1_000_000n + BigInt(frac.padEnd(6, '0') || '0')
}

// ---------- 指标展示口径 ----------

export type UsageCoverageState = 'empty' | 'none' | 'partial' | 'full'
export type UsageCostState = 'empty' | 'unknown' | 'partial' | 'complete'

export function tokenValue(metrics: UsageMetrics | null | undefined): number | null {
  if (!metrics) return null
  // 有请求但一条用量都没返回：不能声称是 0。
  if (metrics.requests > 0 && metrics.usage_known_requests === 0) return null
  return metrics.total_tokens
}

export function tokenFieldValue(
  metrics: UsageMetrics | null | undefined,
  field: 'input_tokens' | 'output_tokens' | 'cached_input_tokens'
): number | null {
  if (!metrics) return null
  if (metrics.requests > 0 && metrics.usage_known_requests === 0) return null
  return metrics[field]
}

export function usageCoverage(metrics: UsageMetrics | null | undefined) {
  if (!metrics || metrics.requests <= 0) return { known: 0, total: 0, ratio: null as number | null }
  return {
    known: metrics.usage_known_requests,
    total: metrics.requests,
    // 有请求但一条用量都没返回是“未知”，不能显示成 0% 覆盖。
    ratio: metrics.usage_known_requests > 0
      ? metrics.usage_known_requests / metrics.requests
      : null
  }
}

export function coverageState(metrics: UsageMetrics | null | undefined): UsageCoverageState {
  if (!metrics || metrics.requests <= 0) return 'empty'
  if (metrics.usage_known_requests <= 0) return 'none'
  if (metrics.usage_known_requests < metrics.requests) return 'partial'
  return 'full'
}

export function costState(metrics: UsageMetrics | null | undefined): UsageCostState {
  if (!metrics || metrics.requests <= 0) return 'empty'
  const priced = metrics.priced_requests
  if (priced <= 0) return 'unknown'
  if (priced < metrics.requests) return 'partial'
  return 'complete'
}

/** 百分比条宽度；未知/空数据返回 0，不伪造比例。 */
export function percentOf(part: number, total: number): number {
  if (!Number.isFinite(part) || !Number.isFinite(total) || total <= 0) return 0
  return Math.min(100, Math.max(0, (part / total) * 100))
}

export function formatPercent(ratio: number | null): string {
  if (ratio === null || !Number.isFinite(ratio)) return '—'
  return `${Math.round(ratio * 100)}%`
}

// ---------- 图表数据 ----------

export type UsageChartMetric = 'input_tokens' | 'output_tokens' | 'total_tokens' | 'estimated_cost'

export interface UsageChartPoint {
  value: number | null
  /** 有请求但后端没有可确认的数值：图上要断开并显示未知标记 */
  unknown: boolean
  /** 有部分记录未计入（Token 用量缺失或费用未计价）：数值仍可展示但要明确标签 */
  partial?: boolean
  /** 原始十进制金额，用于精确展示；Token 点不需要 */
  text?: string | null
}

export function chartPoint(
  metrics: UsageMetrics,
  metric: UsageChartMetric
): UsageChartPoint {
  if (metric === 'estimated_cost') {
    const raw = metrics.estimated_cost
    if (raw === null) {
      return { value: null, unknown: metrics.requests > 0, text: null }
    }
    const parsed = Number(raw)
    const known = Number.isFinite(parsed)
    return {
      value: known ? parsed : null,
      unknown: !known,
      text: raw,
      ...(known && metrics.unpriced_requests > 0 ? { partial: true } : {})
    }
  }

  const value = metric === 'total_tokens' ? tokenValue(metrics) : tokenFieldValue(metrics, metric)
  return {
    value,
    unknown: value === null,
    ...(value !== null && metrics.usage_missing_requests > 0 ? { partial: true } : {})
  }
}

/** 只把连续的非空点连成折线；null 会切断路径，不能跨未知/空桶连线。 */
export function splitRuns(points: UsageChartPoint[]): Array<Array<{ index: number; point: UsageChartPoint }>> {
  const runs: Array<Array<{ index: number; point: UsageChartPoint }>> = []
  let run: Array<{ index: number; point: UsageChartPoint }> = []
  points.forEach((point, index) => {
    if (point.value === null) {
      if (run.length) runs.push(run)
      run = []
      return
    }
    run.push({ index, point })
  })
  if (run.length) runs.push(run)
  return runs
}

export function niceTicks(maxValue: number, count = 4): { max: number; ticks: number[] } {
  if (!Number.isFinite(maxValue) || maxValue <= 0) {
    return { max: 1, ticks: [0, 0.25, 0.5, 0.75, 1] }
  }
  const rough = maxValue / count
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const normalized = rough / magnitude
  const step = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude
  const top = Math.ceil(maxValue / step) * step
  const ticks: number[] = []
  for (let value = 0; value <= top + step / 2; value += step) {
    ticks.push(Number(value.toPrecision(12)))
  }
  return { max: Number(top.toPrecision(12)), ticks }
}

/** 去掉小数末尾的 0，同时保留整数部分的 0（例如 0.100 保留第一个 0）。 */
function trimFraction(text: string): string {
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text
}

/** 0 与 1 之间的刻度保留两位有效数字；极小值用科学计数法，避免各档都显示为 0。 */
function smallNumberText(value: number): string {
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''
  if (abs >= 1e-4) return `${sign}${trimFraction(abs.toPrecision(2))}`
  const [mantissa, exponent] = abs.toExponential(1).split('e')
  return `${sign}${trimFraction(mantissa)}e${Number(exponent)}`
}

export function compactNumber(value: number): string {
  if (!Number.isFinite(value)) return '0'
  const abs = Math.abs(value)
  const format = (scaled: number, suffix: string) => {
    const text = scaled >= 10 ? scaled.toFixed(0) : scaled.toFixed(1).replace(/\.0$/, '')
    return `${text}${suffix}`
  }
  if (abs >= 1_000_000_000) return format(value / 1_000_000_000, 'B')
  if (abs >= 1_000_000) return format(value / 1_000_000, 'M')
  if (abs >= 1_000) return format(value / 1_000, 'K')
  if (abs === 0) return '0'
  // 大于等于 1 保持两位小数；小额不能四舍五入成 0，否则费用 Y 轴会失去区分度。
  if (abs >= 1) return String(Math.round(value * 100) / 100)
  return smallNumberText(value)
}

export function hoverIndexForX(
  x: number,
  plotLeft: number,
  plotWidth: number,
  count: number
): number | null {
  if (count <= 0) return null
  if (count === 1) return 0
  const ratio = (x - plotLeft) / Math.max(plotWidth, 1)
  return Math.min(count - 1, Math.max(0, Math.round(ratio * (count - 1))))
}

/**
 * 图例开关是否可用：隐藏的系列任何时候都能重新显示；
 * 只禁止隐藏最后一条可见系列，避免图表变成空白。
 */
export function canToggleSeries(
  key: string,
  hiddenKeys: readonly string[],
  visibleCount: number
): boolean {
  if (hiddenKeys.includes(key)) return true
  return visibleCount > 1
}

// ---------- 价格校验与提交 ----------

export type UsagePriceErrorCode =
  | 'inputRequired'
  | 'outputRequired'
  | 'rateFormat'
  | 'rateRange'
  | 'outputZero'
  | 'cacheRequiresRates'

export interface UsagePriceDraft {
  enabled: boolean
  input: string
  output: string
  cached: string
}

export interface UsagePriceValidation {
  ok: boolean
  code?: UsagePriceErrorCode
  field?: 'input' | 'output' | 'cached'
}

const MAX_RATE_MICROS = 1_000_000n * 1_000_000n

function rateIssue(value: string): 'rateFormat' | 'rateRange' | null {
  const micros = decimalToMicros(value)
  if (micros === null) return 'rateFormat'
  if (micros > MAX_RATE_MICROS) return 'rateRange'
  return null
}

export function validatePriceDraft(
  kind: UsageModelKind,
  draft: UsagePriceDraft
): UsagePriceValidation {
  // 关闭计价就是同时清空输入/输出/缓存，无需校验旧值
  if (!draft.enabled) return { ok: true }

  const input = draft.input.trim()
  const cached = draft.cached.trim()

  if (!input) return { ok: false, code: 'inputRequired', field: 'input' }
  const inputIssue = rateIssue(input)
  if (inputIssue) return { ok: false, code: inputIssue, field: 'input' }

  if (kind === 'chat') {
    const output = draft.output.trim()
    if (!output) return { ok: false, code: 'outputRequired', field: 'output' }
    const outputIssue = rateIssue(output)
    if (outputIssue) return { ok: false, code: outputIssue, field: 'output' }
  } else {
    const output = draft.output.trim()
    if (output && !/^0(?:\.0+)?$/.test(output)) {
      return { ok: false, code: 'outputZero', field: 'output' }
    }
  }

  if (cached) {
    const cachedIssue = rateIssue(cached)
    if (cachedIssue) return { ok: false, code: cachedIssue, field: 'cached' }
  }
  return { ok: true }
}

function normalizeRateInput(value: string): string | null {
  const raw = value.trim()
  if (!raw) return null
  const [int, frac] = raw.split('.')
  const normalizedInt = stripLeadingZeros(int)
  if (frac === undefined) return normalizedInt
  const trimmed = frac.replace(/0+$/, '')
  return trimmed ? `${normalizedInt}.${trimmed}` : normalizedInt
}

export interface UsagePricePayload {
  provider: string
  model: string
  kind: UsageModelKind
  input_per_million: string | null
  output_per_million: string | null
  cached_input_per_million: string | null
  expected_revision: number
}

export function buildPricePayload(item: ModelPrice, draft: UsagePriceDraft): UsagePricePayload {
  if (!draft.enabled) {
    return {
      provider: item.provider,
      model: item.model,
      kind: item.kind,
      input_per_million: null,
      output_per_million: null,
      cached_input_per_million: null,
      expected_revision: item.revision
    }
  }
  return {
    provider: item.provider,
    model: item.model,
    kind: item.kind,
    input_per_million: normalizeRateInput(draft.input),
    // 向量/重排模型没有输出 token，后端也要求输出单价必须为 0。
    output_per_million: item.kind === 'chat' ? normalizeRateInput(draft.output) : '0',
    cached_input_per_million: normalizeRateInput(draft.cached),
    expected_revision: item.revision
  }
}

export function priceKey(item: Pick<ModelPrice, 'provider' | 'model' | 'kind'>): string {
  return `${item.provider}\u0000${item.model}\u0000${item.kind}`
}

export function sortPrices(items: ModelPrice[]): ModelPrice[] {
  return [...items].sort((left, right) =>
    left.provider.localeCompare(right.provider)
    || left.model.localeCompare(right.model)
    || left.kind.localeCompare(right.kind)
  )
}

export function upsertPrice(items: ModelPrice[], updated: ModelPrice): ModelPrice[] {
  const key = priceKey(updated)
  const index = items.findIndex(item => priceKey(item) === key)
  const next = [...items]
  if (index === -1) next.push(updated)
  else next[index] = updated
  return sortPrices(next)
}

export function isPriceConfigured(item: Pick<ModelPrice, 'input_per_million' | 'output_per_million'>): boolean {
  return item.input_per_million !== null || item.output_per_million !== null
}

export function isRevisionConflict(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const response = (error as { response?: { status?: number } }).response
  return response?.status === 409
}

/** 保存前后比较草稿是否被继续编辑；忽略首尾空白，避免无意义差异。 */
export function draftSignature(draft: UsagePriceDraft): string {
  return [draft.enabled ? '1' : '0', draft.input.trim(), draft.output.trim(), draft.cached.trim()]
    .join('\u0000')
}

export interface PriceEditorSnapshot {
  key: string
  draft: string
}

/** 异步价格请求返回时，用户是否仍停留在发起时的模型且没有改过草稿。 */
export function isEditorUntouched(
  before: PriceEditorSnapshot,
  after: PriceEditorSnapshot
): boolean {
  return before.key === after.key && before.draft === after.draft
}

export interface DialogRequestTicket {
  session: number
  ticket: number
}

/**
 * 弹窗请求闸门：关闭或重新打开后，之前所有在途请求都失效；
 * 加载与保存各自只允许最新一次结果写回，旧响应不能覆盖新的列表、选中模型或草稿。
 */
export function createDialogRequestGate() {
  let session = 0
  let loadTicket = 0
  let saveTicket = 0
  return {
    invalidate(): void {
      session += 1
      loadTicket += 1
      saveTicket += 1
    },
    /** 新加载取代旧的加载和旧的保存，避免保存结果反过来覆盖刚刷新的列表。 */
    beginLoad(): DialogRequestTicket {
      loadTicket += 1
      saveTicket += 1
      return { session, ticket: loadTicket }
    },
    beginSave(): DialogRequestTicket {
      saveTicket += 1
      return { session, ticket: saveTicket }
    },
    isLoadCurrent(ticket: DialogRequestTicket): boolean {
      return ticket.session === session && ticket.ticket === loadTicket
    },
    isSaveCurrent(ticket: DialogRequestTicket): boolean {
      return ticket.session === session && ticket.ticket === saveTicket
    }
  }
}

// ---------- 权限与竞态 ----------

export function defaultUsageScope(isAdmin: boolean): UsageScope {
  return isAdmin ? 'all' : 'mine'
}

/** 普通用户不能请求 all；即使本地状态被改成 all，也必须回落为 mine。 */
export function resolveUsageScope(isAdmin: boolean, requested: UsageScope): UsageScope {
  return isAdmin ? requested : 'mine'
}

/**
 * 只让最新一次请求写回页面：旧的慢响应不能覆盖新筛选，
 * dispose 之后（页面卸载）任何响应都不再更新。
 */
export function createLatestGuard() {
  let current = 0
  let disposed = false
  return {
    begin(): number {
      current += 1
      return current
    },
    isCurrent(token: number): boolean {
      return !disposed && token === current
    },
    dispose(): void {
      disposed = true
      current += 1
    },
    get disposed(): boolean {
      return disposed
    }
  }
}

export function breakdownTokenValue(row: UsageBreakdownItem): number | null {
  return tokenValue(row)
}
