<template>
  <div class="page usage-page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">{{ t('usage.eyebrow') }}</span>
        <h2>{{ t('usage.title') }}</h2>
        <p>{{ t('usage.subtitle') }}</p>
      </div>
      <div class="page-actions">
        <div v-if="isAdmin" class="scope-switch" role="group" :aria-label="t('usage.scope.aria')">
          <button
            type="button"
            :class="{ active: scope === 'all' }"
            :aria-pressed="scope === 'all'"
            @click="setScope('all')"
          >{{ t('usage.scope.all') }}</button>
          <button
            type="button"
            :class="{ active: scope === 'mine' }"
            :aria-pressed="scope === 'mine'"
            @click="setScope('mine')"
          >{{ t('usage.scope.mine') }}</button>
        </div>
        <span v-else class="scope-chip">{{ t('usage.scope.mineOnly') }}</span>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>{{ t('usage.refresh') }}
        </el-button>
        <el-button v-if="isAdmin" type="primary" plain @click="priceDialogOpen = true">
          <el-icon><Setting /></el-icon>{{ t('usage.priceSettings') }}
        </el-button>
      </div>
    </div>

    <!-- 筛选：快捷区间 + 原生日期 + 粒度 + 模型/用途 -->
    <section class="filter-panel" :aria-label="t('usage.range.aria')">
      <div class="preset-row" role="group" :aria-label="t('usage.filters.presetsAria')">
        <button
          v-for="item in PRESETS"
          :key="item.value"
          type="button"
          class="preset-btn"
          :class="{ active: preset === item.value }"
          :aria-pressed="preset === item.value"
          @click="applyPreset(item.value)"
        >{{ t(item.labelKey) }}</button>
        <span v-if="preset === 'custom'" class="custom-tag">{{ t('usage.filters.custom') }}</span>
      </div>

      <div class="filter-controls">
        <label class="date-field">
          <span>{{ t('usage.filters.start') }}</span>
          <input
            v-model="range.start"
            type="date"
            :aria-label="t('usage.filters.start')"
            :max="range.end || undefined"
            @change="onDateChange"
          />
        </label>
        <span class="date-sep" aria-hidden="true">–</span>
        <label class="date-field">
          <span>{{ t('usage.filters.end') }}</span>
          <input
            v-model="range.end"
            type="date"
            :aria-label="t('usage.filters.end')"
            :min="range.start || undefined"
            @change="onDateChange"
          />
        </label>

        <el-select
          v-model="granularity"
          class="select granularity"
          :aria-label="t('usage.filters.granularity')"
          @change="load"
        >
          <el-option
            v-for="item in GRANULARITIES"
            :key="item"
            :value="item"
            :label="t(`usage.filters.${item}`)"
            :disabled="!granularityAvailability[item]"
          />
        </el-select>

        <el-select
          v-model="model"
          class="select model-select"
          clearable
          filterable
          :placeholder="t('usage.filters.allModels')"
          :aria-label="t('usage.filters.model')"
          @change="load"
        >
          <el-option v-for="item in modelOptions" :key="item" :value="item" :label="item" />
        </el-select>

        <el-select
          v-model="operation"
          class="select operation-select"
          clearable
          :placeholder="t('usage.filters.allOperations')"
          :aria-label="t('usage.filters.operation')"
          @change="load"
        >
          <el-option
            v-for="item in USAGE_OPERATIONS"
            :key="item"
            :value="item"
            :label="t(`usage.operations.${item}`)"
          />
        </el-select>

        <span class="apply-hint">{{ t('usage.filters.applyHint') }}</span>
      </div>

      <p v-if="rangeIssue" class="range-error" role="alert">
        {{ t(`usage.filters.error.${rangeIssue}`) }}
      </p>
    </section>

    <div v-if="statistics" class="range-strip">
      <div class="range-copy">
        <span class="range-label">{{ t('usage.range.summary') }}</span>
        <strong>{{ formatUsageDay(statistics.start, locale) }} – {{ formatUsageDay(statistics.end, locale) }}</strong>
        <span class="range-meta">{{ granularityLabel }} · {{ statistics.timezone }}</span>
        <span class="range-scope">{{ statistics.scope === 'all' ? t('usage.scope.all') : t('usage.scope.mine') }}</span>
      </div>
      <span class="range-hint">{{ t('usage.range.timezone') }}</span>
    </div>

    <el-alert
      v-if="loadError"
      class="load-error"
      :title="loadError"
      type="error"
      show-icon
      :closable="false"
    >
      <el-button size="small" @click="load">{{ t('usage.retry') }}</el-button>
    </el-alert>

    <div v-loading="loading" :element-loading-text="t('usage.loading')" class="usage-body">
      <!-- 总统计：6 张有主次的指标卡 -->
      <section class="kpi-grid" :aria-label="t('usage.range.summary')">
        <article class="kpi tokens primary">
          <div class="kpi-top">
            <span class="kpi-label">{{ t('usage.cards.totalTokens') }}</span>
            <el-icon class="kpi-icon"><TrendCharts /></el-icon>
          </div>
          <strong class="kpi-value" :class="{ unknown: !totals || totalTokenValue === null }">
            {{ totalTokensText }}
          </strong>
          <span class="kpi-sub">{{ t('usage.cards.totalTokensHint') }}</span>
          <span v-if="totals && coverage.total > 0" class="kpi-foot">
            {{ t('usage.cards.knownSummary', { known: formatNumber(coverage.known), total: formatNumber(coverage.total) }) }}
          </span>
        </article>

        <article class="kpi">
          <div class="kpi-top">
            <span class="kpi-label">{{ t('usage.cards.input') }}</span>
          </div>
          <strong class="kpi-value" :class="{ unknown: !totals || inputTokenValue === null }">
            {{ inputTokensText }}
          </strong>
          <span v-if="totals && totals.cached_input_tokens > 0" class="kpi-foot">
            {{ t('usage.cards.cached', { count: formatNumber(totals.cached_input_tokens) }) }}
          </span>
        </article>

        <article class="kpi">
          <div class="kpi-top">
            <span class="kpi-label">{{ t('usage.cards.output') }}</span>
          </div>
          <strong class="kpi-value" :class="{ unknown: !totals || outputTokenValue === null }">
            {{ outputTokensText }}
          </strong>
        </article>

        <article class="kpi cost primary">
          <div class="kpi-top">
            <span class="kpi-label">{{ t('usage.cards.cost') }}</span>
            <el-icon class="kpi-icon"><Coin /></el-icon>
          </div>
          <strong class="kpi-value cost-value" :class="{ unknown: !totals || totals.estimated_cost === null }">
            {{ costText }}
          </strong>
          <span class="kpi-sub">{{ t('usage.cards.costHint') }}</span>
          <span v-if="totals && totals.unpriced_requests > 0" class="kpi-foot warn">
            {{ t('usage.cards.unpriced', { count: formatNumber(totals.unpriced_requests) }) }}
          </span>
        </article>

        <article class="kpi">
          <div class="kpi-top">
            <span class="kpi-label">{{ t('usage.cards.requests') }}</span>
            <el-icon class="kpi-icon"><Tickets /></el-icon>
          </div>
          <strong class="kpi-value">{{ totals ? formatNumber(totals.requests) : '—' }}</strong>
          <span v-if="totals" class="kpi-foot">
            {{ t('usage.cards.successFail', { success: formatNumber(totals.successful_requests), failed: formatNumber(totals.failed_requests) }) }}
          </span>
        </article>

        <article class="kpi">
          <div class="kpi-top">
            <span class="kpi-label">{{ t('usage.cards.coverage') }}</span>
            <el-icon class="kpi-icon"><DataLine /></el-icon>
          </div>
          <strong class="kpi-value" :class="{ unknown: !totals || coverage.ratio === null }">
            {{ coverageText }}
          </strong>
          <span class="kpi-sub">{{ coverageStateLabel }}</span>
        </article>
      </section>

      <p class="estimate-note">{{ t('usage.notices.cost') }}</p>

      <div v-if="notices.length" class="notices">
        <el-alert
          v-for="notice in notices"
          :key="notice.key"
          :title="t(notice.key, notice.params)"
          :type="notice.type"
          :closable="false"
          show-icon
        />
      </div>

      <!-- 空数据：不画伪造的 0 线 -->
      <section v-if="showEmpty" class="empty-panel">
        <el-empty :description="t('usage.emptyTitle')" :image-size="86">
          <p class="empty-hint">{{ t('usage.emptyHint') }}</p>
        </el-empty>
      </section>

      <template v-else-if="statistics">
        <!-- 趋势：上方的总统计，下方按时间段 -->
        <section class="chart-grid">
          <UsageTrendChart
            :title="t('usage.chart.tokensTitle')"
            :description="t('usage.chart.tokensDesc', { granularity: granularityText })"
            :series="tokenSeries"
            :labels="chartLabels"
            :unit-label="t('usage.chart.unitTokens')"
            :empty-text="t('usage.chart.empty')"
            :ariaLabel="t('usage.chart.aria', { title: t('usage.chart.tokensTitle') })"
            :legend-aria="t('usage.chart.legendAria')"
            :period-label="t('usage.chart.period')"
            :show-label="t('usage.chart.show')"
            :hide-label="t('usage.chart.hide')"
            :unknown-label="t('usage.chart.unknown')"
            :partial-label="t('usage.chart.partial')"
            :format-point="formatTokenPoint"
          />
          <UsageTrendChart
            :title="t('usage.chart.costTitle')"
            :description="t('usage.chart.costDesc')"
            :series="costSeries"
            :labels="chartLabels"
            :unit-label="t('usage.chart.unitCost')"
            :empty-text="t('usage.chart.empty')"
            :ariaLabel="t('usage.chart.aria', { title: t('usage.chart.costTitle') })"
            :legend-aria="t('usage.chart.legendAria')"
            :period-label="t('usage.chart.period')"
            :show-label="t('usage.chart.show')"
            :hide-label="t('usage.chart.hide')"
            :unknown-label="t('usage.chart.unknown')"
            :partial-label="t('usage.chart.partial')"
            :format-point="formatCostPoint"
          />
        </section>

        <!-- 紧凑分布 -->
        <section class="panel breakdown-panel">
          <div class="panel-head">
            <div class="panel-title">{{ t('usage.breakdown.title') }}</div>
            <span class="muted">{{ t('usage.breakdown.hint') }}</span>
          </div>
          <div v-if="breakdownRows.length" class="table-scroll">
            <table class="breakdown-table" :aria-label="t('usage.breakdown.tableAria')">
              <thead>
                <tr>
                  <th scope="col">{{ t('usage.breakdown.colModel') }}</th>
                  <th scope="col">{{ t('usage.breakdown.colOperation') }}</th>
                  <th scope="col">{{ t('usage.breakdown.colRequests') }}</th>
                  <th scope="col">{{ t('usage.breakdown.colTokens') }}</th>
                  <th scope="col">{{ t('usage.breakdown.colCost') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in breakdownRows"
                  :key="`${row.provider}-${row.model}-${row.kind}-${row.operation}`"
                >
                  <td class="model-cell">
                    <strong :title="row.model">{{ row.model }}</strong>
                    <span>{{ row.provider }} · {{ t(`usage.kinds.${row.kind}`) }}</span>
                  </td>
                  <td>{{ t(`usage.operations.${row.operation}`) }}</td>
                  <td class="num-cell">
                    <span>{{ formatNumber(row.requests) }}</span>
                    <span class="share-bar" aria-hidden="true">
                      <span :style="{ width: `${row.share}%` }"></span>
                    </span>
                    <small>{{ t('usage.breakdown.share', { percent: row.share.toFixed(0) }) }}</small>
                  </td>
                  <td class="num-cell" :class="{ mutedCell: row.tokenText === null }">
                    {{ row.tokenText === null ? t('usage.cards.unknown') : formatNumber(row.tokenText) }}
                  </td>
                  <td class="num-cell" :class="{ mutedCell: row.estimated_cost === null }">
                    <span>{{ formatCny(row.estimated_cost) }}</span>
                    <small v-if="row.unpriced_requests > 0">
                      {{ t('usage.breakdown.unpriced', { count: formatNumber(row.unpriced_requests) }) }}
                    </small>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="breakdown-empty">
            <el-empty :description="t('usage.breakdown.empty')" :image-size="64" />
          </div>
        </section>
      </template>
    </div>

    <ModelPriceDialog v-if="isAdmin" v-model="priceDialogOpen" />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  usageApi,
  type UsageGranularity,
  type UsageOperation,
  type UsageScope,
  type UsageStatistics
} from '@/api/usage'
import type { UsageChartSeries } from '@/components/UsageTrendChart.vue'
import UsageTrendChart from '@/components/UsageTrendChart.vue'
import ModelPriceDialog from '@/components/ModelPriceDialog.vue'
import { useUserStore } from '@/stores/user'
import { formatDateTime, formatNumber } from '@/i18n/format'
import {
  USAGE_OPERATIONS,
  breakdownTokenValue,
  chartPoint,
  coerceGranularity,
  coverageState,
  createLatestGuard,
  defaultUsageScope,
  formatBucketLabel,
  formatCny,
  formatPercent,
  formatUsageDay,
  granularityAvailability as granularityAvailabilityOf,
  percentOf,
  presetRange,
  recommendedGranularity,
  resolveUsageScope,
  shanghaiToday,
  tokenFieldValue,
  tokenValue,
  usageCoverage,
  validateUsageRange,
  type UsageChartPoint as ChartPoint,
  type UsageDateRange,
  type UsagePreset,
  type UsageRangeIssue
} from '@/utils/usage'

const { t, locale } = useI18n()
const userStore = useUserStore()
const isAdmin = computed(() => userStore.isAdmin)

const PRESETS: Array<{ value: UsagePreset; labelKey: string }> = [
  { value: 'today', labelKey: 'usage.filters.today' },
  { value: '7d', labelKey: 'usage.filters.last7' },
  { value: '30d', labelKey: 'usage.filters.last30' },
  { value: '90d', labelKey: 'usage.filters.last90' }
]
const GRANULARITIES: readonly UsageGranularity[] = ['hour', 'day', 'month']

const today = ref(shanghaiToday())
const preset = ref<UsagePreset | 'custom'>('30d')
const range = reactive<UsageDateRange>(presetRange('30d', today.value))
const granularity = ref<UsageGranularity>('day')
const scope = ref<UsageScope>('mine')
const model = ref('')
const operation = ref<UsageOperation | ''>('')
const statistics = ref<UsageStatistics | null>(null)
const loading = ref(false)
const loadError = ref('')
const priceDialogOpen = ref(false)

const guard = createLatestGuard()
let controller: AbortController | null = null

const rangeValidation = computed(() => validateUsageRange(range, granularity.value))
const rangeIssue = computed<UsageRangeIssue | null>(() =>
  rangeValidation.value.ok ? null : rangeValidation.value.code
)
const granularityAvailability = computed(() => granularityAvailabilityOf(range))

const totals = computed(() => statistics.value?.totals ?? null)
const coverage = computed(() => usageCoverage(totals.value))
const totalTokenValue = computed(() => tokenValue(totals.value))
const inputTokenValue = computed(() => tokenFieldValue(totals.value, 'input_tokens'))
const outputTokenValue = computed(() => tokenFieldValue(totals.value, 'output_tokens'))

const totalTokensText = computed(() => metricText(totalTokenValue.value))
const inputTokensText = computed(() => metricText(inputTokenValue.value))
const outputTokensText = computed(() => metricText(outputTokenValue.value))
const costText = computed(() => (totals.value ? formatCny(totals.value.estimated_cost) : '—'))
const coverageText = computed(() => {
  if (!totals.value) return '—'
  return formatPercent(coverage.value.ratio)
})
const coverageStateLabel = computed(() => {
  if (!totals.value) return ''
  return t(`usage.coverage.${coverageState(totals.value)}`)
})

const granularityLabel = computed(() =>
  t(`usage.filters.${statistics.value?.granularity ?? granularity.value}`)
)
const granularityText = computed(() =>
  t(`usage.filters.${statistics.value?.granularity ?? granularity.value}`)
)

const modelOptions = computed(() => {
  const values = [...(statistics.value?.models ?? [])]
  if (model.value && !values.includes(model.value)) values.unshift(model.value)
  return values
})

const notices = computed(() => {
  const list: Array<{ key: string; type: 'info' | 'warning'; params: Record<string, string> }> = []
  const current = statistics.value
  if (!current) return list
  if (current.tracking_started_at) {
    list.push({
      key: 'usage.notices.tracking',
      type: 'info',
      params: { time: formatDateTime(current.tracking_started_at) }
    })
  }
  if (current.totals.usage_missing_requests > 0) {
    list.push({
      key: 'usage.notices.missing',
      type: 'warning',
      params: { count: formatNumber(current.totals.usage_missing_requests) }
    })
  }
  if (current.totals.unpriced_requests > 0) {
    list.push({
      key: 'usage.notices.unpriced',
      type: 'warning',
      params: { count: formatNumber(current.totals.unpriced_requests) }
    })
  }
  return list
})

const showEmpty = computed(() => !!statistics.value && statistics.value.totals.requests === 0 && !loading.value)

const seriesPoints = computed(() => statistics.value?.series ?? [])
const chartLabels = computed(() =>
  seriesPoints.value.map(point =>
    formatBucketLabel(
      point.period,
      statistics.value?.granularity ?? granularity.value,
      locale.value
    )
  )
)

const tokenSeries = computed<UsageChartSeries[]>(() => {
  const points = seriesPoints.value
  return [
    {
      key: 'input',
      label: t('usage.chart.input'),
      color: 'var(--brand)',
      points: points.map(point => chartPoint(point, 'input_tokens'))
    },
    {
      key: 'output',
      label: t('usage.chart.output'),
      color: 'var(--warning)',
      points: points.map(point => chartPoint(point, 'output_tokens'))
    }
  ]
})

const costSeries = computed<UsageChartSeries[]>(() => [{
  key: 'cost',
  label: t('usage.chart.cost'),
  color: 'var(--brand)',
  points: seriesPoints.value.map(point => {
    const result = chartPoint(point, 'estimated_cost')
    return { ...result, text: point.estimated_cost }
  })
}])

const breakdownRows = computed(() => {
  const total = statistics.value?.totals.requests ?? 0
  return (statistics.value?.breakdown ?? []).map(row => ({
    ...row,
    share: percentOf(row.requests, total),
    tokenText: breakdownTokenValue(row)
  }))
})

function metricText(value: number | null) {
  if (!totals.value) return '—'
  return value === null ? t('usage.cards.unknown') : formatNumber(value)
}

function formatTokenPoint(point: ChartPoint) {
  return point.value === null ? '—' : formatNumber(point.value)
}

function formatCostPoint(point: ChartPoint) {
  return point.value === null ? '—' : formatCny(point.text ?? String(point.value))
}

function setScope(next: UsageScope) {
  if (!isAdmin.value || scope.value === next) return
  scope.value = next
  void load()
}

function applyPreset(next: UsagePreset) {
  today.value = shanghaiToday()
  preset.value = next
  Object.assign(range, presetRange(next, today.value))
  granularity.value = recommendedGranularity(next)
  void load()
}

function onDateChange() {
  preset.value = 'custom'
  granularity.value = coerceGranularity(range, granularity.value)
  void load()
}

async function load() {
  const validation = validateUsageRange(range, granularity.value)
  if (!validation.ok) {
    // 区间无效也要让在途的旧请求失效，避免它回来覆盖当前筛选
    guard.begin()
    controller?.abort()
    controller = null
    loading.value = false
    return
  }

  const token = guard.begin()
  controller?.abort()
  const current = new AbortController()
  controller = current
  loading.value = true
  loadError.value = ''

  try {
    const { data } = await usageApi.statistics({
      start: range.start,
      end: range.end,
      granularity: granularity.value,
      scope: resolveUsageScope(isAdmin.value, scope.value),
      model: model.value || undefined,
      operation: operation.value || undefined
    }, current.signal)
    if (!guard.isCurrent(token)) return
    statistics.value = data
    scope.value = data.scope
  } catch (error) {
    if (current.signal.aborted || !guard.isCurrent(token)) return
    loadError.value = error instanceof Error ? error.message : t('errors.request')
  } finally {
    // 只有最新请求允许结束 loading，旧响应不能覆盖新筛选的状态
    if (guard.isCurrent(token)) loading.value = false
  }
}

onMounted(() => {
  scope.value = defaultUsageScope(userStore.isAdmin)
  void load()
})

onBeforeUnmount(() => {
  guard.dispose()
  controller?.abort()
  controller = null
})
</script>

<style scoped>
.usage-page {
  gap: 18px;
}

.scope-switch {
  display: inline-flex;
  padding: 3px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface-soft);
}

.scope-switch button {
  padding: 5px 12px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.5;
  cursor: pointer;
  transition: background .16s, color .16s, box-shadow .16s;
}

.scope-switch button:hover {
  color: var(--text);
}

.scope-switch button.active {
  background: var(--surface);
  color: var(--brand);
  font-weight: 600;
  box-shadow: var(--shadow-sm);
}

.scope-chip {
  padding: 5px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-soft);
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.5;
}

.filter-panel {
  display: flex;
  flex-direction: column;
  gap: 13px;
  padding: 15px 18px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}

.preset-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.preset-btn {
  padding: 5px 12px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.5;
  cursor: pointer;
  transition: border-color .16s, background .16s, color .16s;
}

.preset-btn:hover {
  border-color: var(--accent-border);
  color: var(--brand);
}

.preset-btn.active {
  border-color: var(--accent-border);
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 600;
}

.custom-tag {
  padding: 3px 8px;
  border: 1px dashed var(--line-strong);
  border-radius: 999px;
  color: var(--text-3);
  font-size: 10.5px;
  letter-spacing: .04em;
}

.filter-controls {
  display: flex;
  align-items: flex-end;
  gap: 9px;
  flex-wrap: wrap;
}

.date-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--text-3);
  font-size: 10.5px;
}

.date-field input {
  width: 148px;
  height: 34px;
  padding: 0 9px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  transition: border-color .16s, box-shadow .16s;
}

.date-field input:hover {
  border-color: var(--line-strong);
}

.date-field input:focus {
  border-color: var(--brand);
  outline: 2px solid color-mix(in srgb, var(--brand) 14%, transparent);
  outline-offset: 1px;
}

.date-sep {
  padding-bottom: 8px;
  color: var(--text-3);
  font-size: 12px;
}

.select {
  width: 132px;
}

.model-select {
  width: 190px;
}

.operation-select {
  width: 148px;
}

.apply-hint {
  margin-left: auto;
  padding-bottom: 8px;
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.5;
}

.range-error {
  margin: 0;
  padding: 8px 11px;
  border: 1px solid var(--danger-border);
  border-radius: 8px;
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 12px;
  line-height: 1.6;
}

.range-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 16px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface-soft);
}

.range-copy {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  min-width: 0;
}

.range-label {
  color: var(--text-3);
  font-size: 10.5px;
  letter-spacing: .05em;
  text-transform: uppercase;
}

.range-copy strong {
  color: var(--text);
  font-size: 12.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.range-meta,
.range-hint {
  color: var(--text-3);
  font-size: 11px;
}

.range-scope {
  padding: 1px 7px;
  border: 1px solid var(--accent-border);
  border-radius: 999px;
  background: var(--brand-tint);
  color: var(--brand);
  font-size: 10px;
  line-height: 1.7;
}

.load-error {
  align-items: center;
}

.usage-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 220px;
}

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(8, minmax(0, 1fr));
  gap: 12px;
}

.kpi {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  min-height: 118px;
  padding: 15px 16px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}

.kpi.tokens,
.kpi.cost {
  grid-column: span 2;
}

.kpi.primary::before {
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: var(--brand);
  content: '';
}

.kpi.cost.primary::before {
  background: var(--warning);
}

.kpi-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.kpi-label {
  overflow: hidden;
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.5;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kpi-icon {
  flex-shrink: 0;
  color: var(--text-3);
  font-size: 14px;
}

.kpi-value {
  margin-top: 2px;
  overflow: hidden;
  color: var(--text);
  font-size: 22px;
  font-weight: 650;
  line-height: 1.3;
  letter-spacing: -.03em;
  font-variant-numeric: tabular-nums;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kpi.tokens .kpi-value,
.kpi.cost .kpi-value {
  font-size: 28px;
}

.kpi-value.cost-value {
  color: var(--text);
}

.kpi-value.unknown {
  color: var(--text-3);
  font-weight: 500;
  letter-spacing: 0;
}

.kpi-sub {
  overflow: hidden;
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.55;
  text-overflow: ellipsis;
}

.kpi-foot {
  margin-top: auto;
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.55;
  font-variant-numeric: tabular-nums;
}

.kpi-foot.warn {
  color: var(--warning);
}

.estimate-note {
  margin: -4px 2px 0;
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.6;
}

.notices {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.empty-panel {
  padding: 18px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}

.empty-hint {
  margin: 0;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.7;
}

.chart-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.38fr) minmax(0, 1fr);
  align-items: stretch;
  gap: 16px;
}

.breakdown-panel {
  min-width: 0;
}

.table-scroll {
  width: 100%;
  overflow-x: auto;
}

.breakdown-table {
  width: 100%;
  min-width: 660px;
  border-collapse: collapse;
  font-size: 12.5px;
}

.breakdown-table th {
  padding: 10px 18px;
  border-bottom: 1px solid var(--line);
  background: var(--table-header);
  color: var(--text-3);
  font-size: 10.5px;
  font-weight: 550;
  letter-spacing: .05em;
  text-align: left;
  text-transform: uppercase;
  white-space: nowrap;
}

.breakdown-table td {
  padding: 11px 18px;
  border-bottom: 1px solid var(--table-line);
  color: var(--text-2);
  vertical-align: middle;
}

.breakdown-table tbody tr:last-child td {
  border-bottom: 0;
}

.breakdown-table tbody tr:hover {
  background: var(--table-hover);
}

.model-cell {
  min-width: 180px;
}

.model-cell strong {
  display: block;
  max-width: 240px;
  overflow: hidden;
  color: var(--text);
  font-size: 12.5px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-cell span {
  display: block;
  margin-top: 2px;
  color: var(--text-3);
  font-size: 10.5px;
}

.num-cell {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.num-cell small {
  display: block;
  margin-top: 3px;
  color: var(--text-3);
  font-size: 10px;
}

.num-cell.mutedCell {
  color: var(--text-3);
}

.share-bar {
  display: block;
  width: 100%;
  max-width: 124px;
  height: 3px;
  margin-top: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--line);
}

.share-bar > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--brand);
}

.breakdown-empty {
  padding: 8px 16px;
}

@media (max-width: 1240px) {
  .kpi-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .kpi.tokens,
  .kpi.cost {
    grid-column: span 2;
  }

  .chart-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 900px) {
  .apply-hint {
    width: 100%;
    margin-left: 0;
    padding-bottom: 0;
  }
}

@media (max-width: 760px) {
  .scope-chip {
    display: none;
  }

  .kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .kpi {
    min-height: 108px;
    padding: 13px 13px;
  }

  .kpi.tokens,
  .kpi.cost {
    grid-column: span 2;
  }

  .kpi.tokens .kpi-value,
  .kpi.cost .kpi-value {
    font-size: 25px;
  }

  .date-field {
    flex: 1;
    min-width: 132px;
  }

  .date-field input {
    width: 100%;
  }

  .select,
  .model-select,
  .operation-select {
    flex: 1;
    min-width: 132px;
    width: auto;
  }

  .filter-controls {
    gap: 8px;
  }
}

@media (max-width: 520px) {
  .date-sep {
    display: none;
  }
}
</style>
