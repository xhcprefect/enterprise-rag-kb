<template>
  <figure class="usage-chart" :aria-labelledby="titleId">
    <figcaption class="chart-head">
      <div class="chart-copy">
        <div :id="titleId" class="chart-title">{{ title }}</div>
        <p :id="descId" class="chart-desc">{{ description }}</p>
      </div>
      <div class="chart-legend" role="group" :aria-label="legendAria">
        <button
          v-for="item in series"
          :key="item.key"
          type="button"
          class="legend-btn"
          :class="{ off: hiddenKeys.includes(item.key) }"
          :aria-pressed="!hiddenKeys.includes(item.key)"
          :disabled="!canToggleSeries(item.key, hiddenKeys, visibleSeries.length)"
          :title="hiddenKeys.includes(item.key) ? showLabel : hideLabel"
          @click="toggleSeries(item.key)"
        >
          <span class="legend-swatch" :style="{ background: item.color }" aria-hidden="true"></span>
          {{ item.label }}
        </button>
      </div>
    </figcaption>

    <div ref="wrapRef" class="chart-wrap" @mouseleave="clearHover">
      <svg
        v-if="hasData"
        class="chart-svg"
        :viewBox="`0 0 ${width} ${height}`"
        :width="width"
        :height="height"
        role="img"
        :aria-label="ariaLabel"
        :aria-describedby="`${descId} ${tableId}`"
        tabindex="0"
        @mousemove="onMouseMove"
        @focus="onFocus"
        @blur="clearHover"
        @keydown="onKeydown"
      >
        <text class="axis-unit" :x="plot.left" y="12">{{ unitLabel }}</text>

        <g aria-hidden="true">
          <line
            v-for="tick in yTicks"
            :key="`grid-${tick}`"
            class="grid-line"
            :x1="plot.left"
            :x2="plot.left + plot.width"
            :y1="yOf(tick)"
            :y2="yOf(tick)"
          />
          <text
            v-for="tick in yTicks"
            :key="`tick-${tick}`"
            class="axis-label"
            :x="plot.left - 8"
            :y="yOf(tick) + 3.5"
            text-anchor="end"
          >{{ compactNumber(tick) }}</text>
        </g>

        <line
          class="axis-line"
          :x1="plot.left"
          :x2="plot.left + plot.width"
          :y1="plot.top + plot.height"
          :y2="plot.top + plot.height"
        />
        <line
          class="axis-line"
          :x1="plot.left"
          :x2="plot.left"
          :y1="plot.top"
          :y2="plot.top + plot.height"
        />

        <g aria-hidden="true">
          <text
            v-for="(index, position) in xTickIndices"
            :key="`x-${index}`"
            class="axis-label"
            :x="xOf(index)"
            :y="plot.top + plot.height + 19"
            :text-anchor="xLabelAnchor(position)"
          >{{ labels[index] }}</text>
        </g>

        <g aria-hidden="true">
          <g v-for="item in visibleSeries" :key="`series-${item.key}`">
            <path
              v-for="(run, runIndex) in runsOf(item)"
              :key="`run-${item.key}-${runIndex}`"
              class="series-line"
              :d="pathOf(run)"
              :stroke="item.color"
            />
            <circle
              v-for="entry in pointsOf(item)"
              :key="`dot-${item.key}-${entry.index}`"
              class="series-dot"
              :cx="xOf(entry.index)"
              :cy="yOf(entry.point.value!)"
              :r="hoverIndex === entry.index ? 4 : 2.1"
              :fill="item.color"
            />
            <circle
              v-for="entry in partialOf(item)"
              :key="`partial-${item.key}-${entry.index}`"
              class="partial-ring"
              :cx="xOf(entry.index)"
              :cy="yOf(entry.point.value!)"
              r="4.6"
              :stroke="item.color"
            />
            <g
              v-for="entry in unknownOf(item)"
              :key="`unknown-${item.key}-${entry.index}`"
              :transform="`translate(${xOf(entry.index)}, ${plot.top + plot.height - 9 - seriesOffset(item.key)})`"
            >
              <circle class="unknown-ring" r="4" :stroke="item.color" />
              <text class="unknown-mark" y="2.6" text-anchor="middle">?</text>
            </g>
          </g>
        </g>

        <g v-if="hoverIndex !== null" aria-hidden="true">
          <line
            class="hover-line"
            :x1="xOf(hoverIndex)"
            :x2="xOf(hoverIndex)"
            :y1="plot.top"
            :y2="plot.top + plot.height"
          />
          <circle
            v-for="entry in hoverPoints"
            :key="`hover-${entry.key}`"
            :cx="xOf(hoverIndex)"
            :cy="yOf(entry.point.value!)"
            r="4.5"
            :fill="entry.color"
            class="hover-dot"
          />
        </g>
      </svg>

      <div v-else class="chart-empty" role="status">
        <span class="empty-icon" aria-hidden="true">∅</span>
        <span>{{ emptyText }}</span>
      </div>

      <div
        v-if="hoverIndex !== null && tooltipReady"
        class="chart-tip"
        :style="{ left: `${tooltipLeft}px`, top: '6px' }"
        role="status"
        aria-live="polite"
      >
        <strong class="tip-period">{{ labels[hoverIndex] }}</strong>
        <span v-if="hoverHasPartial" class="tip-flag">{{ partialLabel }}</span>
        <span v-for="item in hoverEntries" :key="`tip-${item.key}`" class="tip-row">
          <span class="tip-swatch" :style="{ background: item.color }" aria-hidden="true"></span>
          <span class="tip-label">{{ item.label }}</span>
          <span class="tip-value">{{ item.text }}</span>
        </span>
      </div>
    </div>

    <table :id="tableId" class="visually-hidden">
      <caption>{{ title }}</caption>
      <thead>
        <tr>
          <th scope="col">{{ periodLabel }}</th>
          <th v-for="item in series" :key="`th-${item.key}`" scope="col">{{ item.label }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(label, index) in labels" :key="`tr-${label}-${index}`">
          <th scope="row">{{ label }}</th>
          <td v-for="item in series" :key="`td-${item.key}-${index}`">{{ cellText(item, index) }}</td>
        </tr>
      </tbody>
    </table>
  </figure>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId } from 'vue'
import {
  canToggleSeries,
  compactNumber,
  hoverIndexForX,
  niceTicks,
  splitRuns,
  type UsageChartPoint
} from '@/utils/usage'

export interface UsageChartSeries {
  key: string
  label: string
  color: string
  points: UsageChartPoint[]
}

const props = withDefaults(defineProps<{
  title: string
  description: string
  series: UsageChartSeries[]
  labels: string[]
  unitLabel: string
  emptyText: string
  ariaLabel: string
  legendAria: string
  periodLabel: string
  showLabel: string
  hideLabel: string
  /** 无数据/未知值的文本 */
  unknownLabel: string
  /** 有部分记录未计入时的标签 */
  partialLabel: string
  formatPoint: (point: UsageChartPoint) => string
  height?: number
}>(), {
  height: 258
})

const width = ref(720)
const height = computed(() => props.height)
const wrapRef = ref<HTMLElement | null>(null)
const hoverIndex = ref<number | null>(null)
const hiddenKeys = ref<string[]>([])

const uid = useId()
const titleId = `usage-chart-title-${uid}`
const descId = `usage-chart-desc-${uid}`
const tableId = `usage-chart-table-${uid}`

const visibleSeries = computed(() => props.series.filter(item => !hiddenKeys.value.includes(item.key)))
const pointCount = computed(() => props.labels.length)

const plot = computed(() => {
  const marginLeft = width.value < 480 ? 44 : 56
  return {
    left: marginLeft,
    top: 20,
    width: Math.max(60, width.value - marginLeft - 14),
    height: Math.max(80, height.value - 20 - 36)
  }
})

const maxValue = computed(() => {
  let max = 0
  for (const item of visibleSeries.value) {
    for (const point of item.points) {
      if (point.value !== null && Number.isFinite(point.value)) max = Math.max(max, point.value)
    }
  }
  return max
})

const scales = computed(() => niceTicks(maxValue.value, 4))
const yTicks = computed(() => scales.value.ticks)
const yMax = computed(() => scales.value.max || 1)

function xOf(index: number) {
  if (pointCount.value <= 1) return plot.value.left + plot.value.width / 2
  return plot.value.left + (index / (pointCount.value - 1)) * plot.value.width
}

function yOf(value: number) {
  const ratio = Math.min(1, Math.max(0, value / yMax.value))
  return plot.value.top + plot.value.height - ratio * plot.value.height
}

const xTickIndices = computed(() => {
  const count = pointCount.value
  if (count <= 0) return []
  const maxLabels = width.value < 520 ? 4 : 6
  const step = Math.max(1, Math.ceil(count / maxLabels))
  const indices: number[] = []
  for (let index = 0; index < count; index += step) indices.push(index)
  if (indices[indices.length - 1] !== count - 1) {
    if (count - 1 - indices[indices.length - 1] < step / 2 && indices.length > 1) {
      indices[indices.length - 1] = count - 1
    } else {
      indices.push(count - 1)
    }
  }
  return indices
})

function xLabelAnchor(position: number) {
  if (xTickIndices.value.length <= 1) return 'middle'
  if (position === 0) return 'start'
  if (position === xTickIndices.value.length - 1) return 'end'
  return 'middle'
}

function hasPoint(item: UsageChartSeries) {
  return item.points.some(point => point.value !== null || point.unknown)
}

const hasData = computed(() => pointCount.value > 0 && visibleSeries.value.some(hasPoint))
const tooltipReady = computed(() => hoverIndex.value !== null && hoverIndex.value < pointCount.value)

function runsOf(item: UsageChartSeries) {
  return splitRuns(item.points)
}

function pointsOf(item: UsageChartSeries) {
  return item.points
    .map((point, index) => ({ index, point }))
    .filter(entry => entry.point.value !== null)
}

function partialOf(item: UsageChartSeries) {
  return item.points
    .map((point, index) => ({ index, point }))
    .filter(entry => entry.point.partial === true && entry.point.value !== null)
}

function unknownOf(item: UsageChartSeries) {
  return item.points
    .map((point, index) => ({ index, point }))
    .filter(entry => entry.point.unknown && entry.point.value === null)
}

function seriesOffset(key: string) {
  const index = Math.max(0, visibleSeries.value.findIndex(item => item.key === key))
  return index * 10
}

function pathOf(run: Array<{ index: number; point: UsageChartPoint }>) {
  return run
    .map((entry, position) =>
      `${position === 0 ? 'M' : 'L'}${xOf(entry.index).toFixed(1)},${yOf(entry.point.value!).toFixed(1)}`
    )
    .join(' ')
}

function pointText(point: UsageChartPoint) {
  return point.value === null
    ? point.unknown ? props.unknownLabel : '—'
    : props.formatPoint(point)
}

function cellText(item: UsageChartSeries, index: number) {
  const point = item.points[index]
  if (!point) return '—'
  const text = pointText(point)
  return point.partial ? `${text} · ${props.partialLabel}` : text
}

const hoverEntries = computed(() => {
  const index = hoverIndex.value
  if (index === null) return []
  return visibleSeries.value.map(item => {
    const point = item.points[index]
    return {
      key: item.key,
      label: item.label,
      color: item.color,
      point,
      text: point ? pointText(point) : '—'
    }
  })
})

const hoverPoints = computed(() =>
  hoverEntries.value.filter(entry => entry.point && entry.point.value !== null)
)

const hoverHasPartial = computed(() =>
  hoverEntries.value.some(entry => entry.point?.partial === true)
)

const tooltipLeft = computed(() => {
  if (hoverIndex.value === null) return 8
  return Math.min(Math.max(xOf(hoverIndex.value) + 12, 6), Math.max(6, width.value - 190))
})

function toggleSeries(key: string) {
  if (hiddenKeys.value.includes(key)) {
    hiddenKeys.value = hiddenKeys.value.filter(item => item !== key)
  } else if (visibleSeries.value.length > 1) {
    hiddenKeys.value = [...hiddenKeys.value, key]
  }
}

function clearHover() {
  hoverIndex.value = null
}

function onMouseMove(event: MouseEvent) {
  const svg = event.currentTarget as SVGSVGElement
  const rect = svg.getBoundingClientRect()
  if (!rect.width) return
  const x = ((event.clientX - rect.left) / rect.width) * width.value
  hoverIndex.value = hoverIndexForX(x, plot.value.left, plot.value.width, pointCount.value)
}

function onFocus() {
  hoverIndex.value = Math.max(0, pointCount.value - 1)
}

function onKeydown(event: KeyboardEvent) {
  if (pointCount.value === 0) return
  const current = hoverIndex.value ?? pointCount.value - 1
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    event.preventDefault()
    const delta = event.key === 'ArrowLeft' ? -1 : 1
    hoverIndex.value = Math.min(pointCount.value - 1, Math.max(0, current + delta))
  } else if (event.key === 'Home') {
    event.preventDefault()
    hoverIndex.value = 0
  } else if (event.key === 'End') {
    event.preventDefault()
    hoverIndex.value = pointCount.value - 1
  } else if (event.key === 'Escape') {
    clearHover()
  }
}

let observer: ResizeObserver | null = null

function measure() {
  const target = wrapRef.value
  if (!target) return
  const next = Math.round(target.clientWidth)
  if (next > 0) width.value = next
}

onMounted(() => {
  measure()
  if (typeof ResizeObserver !== 'undefined' && wrapRef.value) {
    observer = new ResizeObserver(measure)
    observer.observe(wrapRef.value)
  }
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})
</script>

<style scoped>
.usage-chart {
  display: flex;
  flex-direction: column;
  min-width: 0;
  margin: 0;
  padding: 18px 20px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}

.chart-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
}

.chart-copy {
  min-width: 0;
}

.chart-title {
  color: var(--text);
  font-size: 14px;
  font-weight: 650;
  line-height: 1.5;
}

.chart-desc {
  max-width: 46em;
  margin: 4px 0 0;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.65;
}

.chart-legend {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.legend-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-soft);
  color: var(--text-2);
  font-size: 11.5px;
  line-height: 1.5;
  cursor: pointer;
  transition: color .16s, border-color .16s, opacity .16s;
}

.legend-btn:hover:not(:disabled) {
  border-color: var(--accent-border);
  color: var(--text);
}

.legend-btn.off {
  opacity: .48;
}

.legend-btn:disabled {
  cursor: default;
}

.legend-swatch {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.chart-wrap {
  position: relative;
  width: 100%;
  min-width: 0;
  margin-top: 8px;
}

.chart-svg {
  display: block;
  width: 100%;
  height: auto;
  overflow: visible;
  outline: none;
}

.chart-svg:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: 2px;
  border-radius: 6px;
}

.axis-unit {
  fill: var(--text-3);
  font-size: 10px;
  letter-spacing: .06em;
}

.axis-label {
  fill: var(--text-3);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}

.grid-line {
  stroke: var(--line);
  stroke-width: 1;
  stroke-dasharray: 3 4;
}

.axis-line {
  stroke: var(--line-strong);
  stroke-width: 1;
}

.series-line {
  fill: none;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  vector-effect: non-scaling-stroke;
}

.series-dot {
  opacity: .75;
  transition: r .12s;
}

.partial-ring {
  fill: none;
  stroke-width: 1.2;
  stroke-dasharray: 2 2;
  opacity: .9;
  pointer-events: none;
}

.unknown-ring {
  fill: var(--surface);
  stroke-width: 1.4;
  stroke-dasharray: 2 2;
}

.unknown-mark {
  fill: var(--text-3);
  font-size: 7px;
  font-weight: 600;
}

.hover-line {
  stroke: var(--line-strong);
  stroke-width: 1;
  stroke-dasharray: 3 3;
}

.hover-dot {
  stroke: var(--surface);
  stroke-width: 1.5;
}

.chart-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  min-height: 190px;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
  color: var(--text-3);
  font-size: 12.5px;
}

.empty-icon {
  font-size: 18px;
  line-height: 1;
}

.chart-tip {
  position: absolute;
  z-index: 5;
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: max-content;
  min-width: 132px;
  max-width: 190px;
  padding: 8px 10px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--surface);
  box-shadow: var(--shadow-md);
  pointer-events: none;
}

.tip-period {
  color: var(--text);
  font-size: 11px;
  font-weight: 600;
  line-height: 1.4;
}

.tip-flag {
  color: var(--warning);
  font-size: 10px;
  line-height: 1.4;
}

.tip-row {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-2);
  font-size: 11px;
  line-height: 1.5;
}

.tip-swatch {
  width: 7px;
  height: 7px;
  flex-shrink: 0;
  border-radius: 50%;
}

.tip-label {
  overflow: hidden;
  flex: 1;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tip-value {
  color: var(--text);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.visually-hidden {
  position: absolute !important;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}

@media (max-width: 640px) {
  .usage-chart {
    padding: 15px 14px 10px;
  }

  .chart-desc {
    font-size: 11.5px;
  }
}
</style>
