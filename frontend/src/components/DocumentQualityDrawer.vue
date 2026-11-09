<template>
  <el-drawer
    :model-value="modelValue"
    :title="t('documents.quality.title')"
    size="min(680px, 96vw)"
    append-to-body
    @update:model-value="onVisibilityChange"
    @closed="onClosed"
  >
    <div v-if="doc" class="quality-doc">
      <header class="quality-head">
        <div class="quality-head-copy">
          <h2 class="quality-filename">{{ displayFilename }}</h2>
          <div class="quality-head-meta">
            <el-tag :type="statusTagType(displayStatus)" size="small" effect="light">
              {{ statusLabel }}
            </el-tag>
            <span>{{ t('documents.quality.indexVersion', { version: displayIndexVersion }) }}</span>
          </div>
        </div>
        <el-button size="small" :loading="loadingQuality" @click="reload">
          {{ t('documents.quality.retry') }}
        </el-button>
      </header>

      <div v-if="loadingQuality" class="quality-status" role="status">
        {{ t('common.loading') }}
      </div>
      <el-alert
        v-else-if="qualityError"
        class="quality-alert"
        type="error"
        :title="qualityError"
        :closable="false"
        show-icon
      >
        <el-button size="small" @click="reload">{{ t('documents.quality.retry') }}</el-button>
      </el-alert>
      <template v-else-if="quality">
        <el-alert
          v-if="quality.status === 'failed'"
          class="quality-alert"
          type="error"
          :title="quality.error_message || t('documents.failedFallback')"
          :closable="false"
          show-icon
        >
          <el-button size="small" @click="reload">{{ t('documents.quality.retry') }}</el-button>
        </el-alert>
        <section class="quality-section" :aria-label="t('documents.quality.summary.title')">
          <h3>{{ t('documents.quality.summary.title') }}</h3>
          <dl class="quality-grid">
            <div class="quality-cell">
              <dt>{{ t('documents.quality.summary.chunks') }}</dt>
              <dd>{{ formatNumber(quality.chunk_count) }}</dd>
            </div>
            <div class="quality-cell">
              <dt>{{ t('documents.quality.summary.chars') }}</dt>
              <dd>{{ formatNumber(quality.char_count) }}</dd>
            </div>
            <div class="quality-cell">
              <dt>{{ t('documents.quality.summary.embedded') }}</dt>
              <dd>{{ formatNumber(quality.embedded_count) }}</dd>
            </div>
            <div class="quality-cell">
              <dt>{{ t('documents.quality.summary.unembedded') }}</dt>
              <dd>{{ formatNumber(quality.unembedded_count) }}</dd>
            </div>
            <div class="quality-cell">
              <dt>{{ t('documents.quality.summary.scanned') }}</dt>
              <dd>
                {{ formatNumber(quality.scanned_chunks) }} / {{ formatNumber(quality.scan_limit) }}
              </dd>
            </div>
            <div class="quality-cell">
              <dt>{{ t('documents.quality.summary.file') }}</dt>
              <dd>
                <el-tag
                  :type="quality.file_exists ? 'success' : 'danger'"
                  size="small"
                  effect="light"
                >
                  {{
                    quality.file_exists
                      ? t('documents.quality.summary.fileExists')
                      : t('documents.quality.summary.fileMissing')
                  }}
                </el-tag>
              </dd>
            </div>
          </dl>
          <el-alert
            v-if="quality.scan_truncated"
            class="quality-scan-alert"
            type="warning"
            :title="
              t('documents.quality.summary.scanTruncated', {
                scanned: formatNumber(quality.scanned_chunks),
                total: formatNumber(quality.chunk_count)
              })
            "
            :closable="false"
            show-icon
          />
        </section>

        <section class="quality-section" :aria-label="t('documents.quality.issues.title')">
          <h3>{{ t('documents.quality.issues.title') }}</h3>
          <p v-if="!quality.issues.length" class="quality-empty-ok">
            {{ t('documents.quality.issues.none') }}
          </p>
          <ul v-else class="quality-issues">
            <li
              v-for="(issue, index) in quality.issues"
              :key="`${issue.code}-${index}`"
              class="quality-issue"
            >
              <el-tag :type="qualityIssueTagType(issue.severity)" size="small" effect="light">
                {{ t(qualityIssueMessageKey(issue.code)) }}
              </el-tag>
              <span v-if="issue.count !== null" class="quality-issue-count">
                {{ t('documents.quality.issues.count', { count: formatNumber(issue.count) }) }}
              </span>
            </li>
          </ul>
        </section>

        <section class="quality-section" :aria-label="t('documents.quality.stats.title')">
          <h3>{{ t('documents.quality.stats.title') }}</h3>
          <dl class="quality-grid quality-stats">
            <div v-for="row in statRows" :key="row.labelKey" class="quality-cell">
              <dt>{{ t(row.labelKey) }}</dt>
              <dd>{{ formatNumber(row.value) }}</dd>
            </div>
          </dl>
          <h4 class="quality-subtitle">{{ t('documents.quality.stats.coverageTitle') }}</h4>
          <dl class="quality-grid quality-stats">
            <div v-for="row in coverageRows" :key="row.labelKey" class="quality-cell">
              <dt>{{ t(row.labelKey) }}</dt>
              <dd>{{ formatNumber(row.value) }}</dd>
            </div>
          </dl>
        </section>
      </template>

      <section class="quality-section" :aria-label="t('documents.quality.chunks.title')">
        <div class="quality-chunks-head">
          <h3>{{ t('documents.quality.chunks.title') }}</h3>
          <span v-if="chunkTotal" class="quality-chunks-total">
            {{ t('documents.quality.chunks.total', { total: formatNumber(chunkTotal) }) }}
          </span>
        </div>
        <div v-if="loadingChunks && !chunks.length" class="quality-status" role="status">
          {{ t('documents.quality.chunks.loading') }}
        </div>
        <el-alert
          v-else-if="chunksError"
          class="quality-alert"
          type="error"
          :title="chunksError"
          :closable="false"
          show-icon
        >
          <el-button size="small" @click="retryChunks">
            {{ t('documents.quality.retry') }}
          </el-button>
        </el-alert>
        <p v-else-if="!chunks.length" class="quality-empty-ok">
          {{ t('documents.quality.chunks.empty') }}
        </p>
        <ul v-else class="quality-chunk-list">
          <li v-for="chunk in chunks" :key="chunk.chunk_index" class="quality-chunk">
            <div class="quality-chunk-head">
              <span class="quality-chunk-index">
                {{ t('documents.quality.chunks.index', { index: chunk.chunk_index }) }}
              </span>
              <span class="quality-chunk-location">{{ chunkLocation(chunk) }}</span>
              <span>
                {{
                  t('documents.quality.chunks.chars', {
                    count: formatNumber(chunk.content_chars)
                  })
                }}
              </span>
              <el-tag :type="chunk.embedded ? 'success' : 'info'" size="small" effect="plain">
                {{
                  chunk.embedded
                    ? t('documents.quality.chunks.embedded')
                    : t('documents.quality.chunks.unembedded')
                }}
              </el-tag>
              <el-tag v-if="chunk.truncated" type="warning" size="small" effect="plain">
                {{ t('documents.quality.chunks.truncated') }}
              </el-tag>
            </div>
            <pre>{{ chunk.content }}</pre>
          </li>
        </ul>
        <div v-if="chunkTotal > 0" class="quality-pager">
          <el-button
            size="small"
            :disabled="chunkPage <= 1 || loadingChunks"
            @click="goToPage(chunkPage - 1)"
          >
            {{ t('documents.quality.chunks.prev') }}
          </el-button>
          <span>
            {{
              t('documents.quality.chunks.pageOf', {
                page: chunkPage,
                pages: chunkPages
              })
            }}
          </span>
          <el-button
            size="small"
            :disabled="chunkPage >= chunkPages || loadingChunks"
            @click="goToPage(chunkPage + 1)"
          >
            {{ t('documents.quality.chunks.next') }}
          </el-button>
        </div>
      </section>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  docApi,
  type DocumentChunkPreview,
  type DocumentItem,
  type DocumentQuality
} from '@/api'
import { formatNumber } from '@/i18n/format'
import { sourceLocationLabels } from '@/utils/sourceLocation'
import {
  QUALITY_CHUNK_PAGE_SIZE,
  qualityChunkOffset,
  qualityChunkPageCount,
  qualityIssueMessageKey,
  qualityIssueTagType
} from '@/utils/documentQuality'

const props = defineProps<{
  modelValue: boolean
  kbId: string
  doc: DocumentItem | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const { t } = useI18n()

const quality = ref<DocumentQuality | null>(null)
const chunks = ref<DocumentChunkPreview[]>([])
const chunkTotal = ref(0)
const chunkPage = ref(1)
const loadingQuality = ref(false)
const loadingChunks = ref(false)
const qualityError = ref('')
const chunksError = ref('')

// 打开会话代次：关闭/切文档/重开都会 +1，迟到的响应按代次与身份双重校验后丢弃。
let generation = 0
let controller: AbortController | null = null
let disposed = false

const displayFilename = computed(() => quality.value?.filename ?? props.doc?.filename ?? '')
const displayStatus = computed<DocumentItem['status']>(
  () => quality.value?.status ?? props.doc?.status ?? 'pending'
)
const displayIndexVersion = computed(
  () => quality.value?.index_version ?? props.doc?.index_version ?? 0
)
const statusLabel = computed(() => t(`documents.status.${displayStatus.value}`))
const chunkPages = computed(() => qualityChunkPageCount(chunkTotal.value, QUALITY_CHUNK_PAGE_SIZE))

const statRows = computed(() => {
  const stats = quality.value?.stats
  if (!stats) return []
  return [
    { labelKey: 'documents.quality.stats.empty', value: stats.empty_chunks },
    { labelKey: 'documents.quality.stats.short', value: stats.short_chunks },
    { labelKey: 'documents.quality.stats.duplicate', value: stats.duplicate_chunks },
    { labelKey: 'documents.quality.stats.replacement', value: stats.replacement_chunks },
    { labelKey: 'documents.quality.stats.control', value: stats.control_chunks },
    { labelKey: 'documents.quality.stats.oversized', value: stats.oversized_chunks }
  ]
})

const coverageRows = computed(() => {
  const stats = quality.value?.stats
  if (!stats) return []
  return [
    { labelKey: 'documents.quality.stats.page', value: stats.page_chunks },
    { labelKey: 'documents.quality.stats.section', value: stats.section_chunks },
    { labelKey: 'documents.quality.stats.line', value: stats.line_chunks },
    { labelKey: 'documents.quality.stats.sheet', value: stats.sheet_chunks },
    { labelKey: 'documents.quality.stats.table', value: stats.table_chunks }
  ]
})

function statusTagType(status: string): 'success' | 'danger' | 'info' | 'warning' {
  if (status === 'ready') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'cancelled') return 'info'
  return 'warning'
}

function chunkLocation(chunk: DocumentChunkPreview): string {
  const labels = sourceLocationLabels(chunk.location || {}, t)
  return labels.length ? labels.join(' · ') : t('documents.quality.chunks.locationUnknown')
}

/** 当前响应是否仍属于同一个打开会话、同一 KB/文档，且抽屉仍打开。 */
function isCurrent(session: number, kbId: string, docId: string): boolean {
  return (
    !disposed &&
    generation === session &&
    props.modelValue &&
    props.kbId === kbId &&
    props.doc?.id === docId
  )
}

/** 立即作废在途请求；旧请求的 finally 不会再清掉新会话的 loading。 */
function invalidate() {
  generation += 1
  controller?.abort()
  controller = null
  loadingQuality.value = false
  loadingChunks.value = false
}

async function fetchQuality(
  session: number,
  kbId: string,
  docId: string,
  signal: AbortSignal
) {
  loadingQuality.value = true
  qualityError.value = ''
  try {
    const { data } = await docApi.quality(kbId, docId, signal)
    if (!isCurrent(session, kbId, docId) || signal.aborted) return
    quality.value = data
  } catch (err: any) {
    if (isCurrent(session, kbId, docId) && !signal.aborted) {
      qualityError.value = err?.message || t('documents.quality.loadFailed')
    }
  } finally {
    if (isCurrent(session, kbId, docId)) loadingQuality.value = false
  }
}

async function fetchChunkPage(
  session: number,
  kbId: string,
  docId: string,
  page: number,
  signal: AbortSignal
) {
  loadingChunks.value = true
  chunksError.value = ''
  chunks.value = []
  chunkPage.value = page
  try {
    const { data } = await docApi.chunks(
      kbId,
      docId,
      {
        offset: qualityChunkOffset(page, QUALITY_CHUNK_PAGE_SIZE),
        limit: QUALITY_CHUNK_PAGE_SIZE
      },
      signal
    )
    if (!isCurrent(session, kbId, docId) || signal.aborted) return
    chunks.value = data.items
    chunkTotal.value = data.total
  } catch (err: any) {
    if (isCurrent(session, kbId, docId) && !signal.aborted) {
      chunksError.value = err?.message || t('documents.quality.chunks.loadFailed')
    }
  } finally {
    if (isCurrent(session, kbId, docId)) loadingChunks.value = false
  }
}

/** 打开/切换文档/重试：作废旧请求后并行加载质量与第一页分块。 */
function startSession() {
  invalidate()
  if (!props.modelValue || !props.doc || !props.kbId) return
  const kbId = props.kbId
  const docId = props.doc.id
  const session = generation
  controller = new AbortController()
  quality.value = null
  qualityError.value = ''
  chunks.value = []
  chunkTotal.value = 0
  chunkPage.value = 1
  chunksError.value = ''
  void fetchQuality(session, kbId, docId, controller.signal)
  void fetchChunkPage(session, kbId, docId, 1, controller.signal)
}

function reload() {
  startSession()
}

function goToPage(page: number) {
  if (!props.modelValue || !props.doc || !props.kbId || !controller) return
  if (loadingChunks.value || page < 1 || page > chunkPages.value) return
  void fetchChunkPage(generation, props.kbId, props.doc.id, page, controller.signal)
}

function retryChunks() {
  if (!props.modelValue || !props.doc || !props.kbId || !controller) return
  void fetchChunkPage(
    generation,
    props.kbId,
    props.doc.id,
    chunkPage.value,
    controller.signal
  )
}

function onVisibilityChange(value: boolean) {
  if (!value) invalidate()
  emit('update:modelValue', value)
}

function onClosed() {
  if (props.modelValue) return
  invalidate()
}

watch(
  [() => props.modelValue, () => props.doc?.id, () => props.kbId],
  () => {
    if (disposed) return
    if (props.modelValue && props.doc && props.kbId) startSession()
    else invalidate()
  },
  { immediate: true, flush: 'sync' }
)

onBeforeUnmount(() => {
  disposed = true
  invalidate()
})
</script>

<style scoped>
.quality-doc {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.quality-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.quality-head-copy {
  min-width: 0;
}

.quality-filename {
  margin: 0;
  color: var(--text);
  font-size: 16px;
  font-weight: 650;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.quality-head-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
  color: var(--text-3);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.quality-status {
  padding: 24px 0;
  color: var(--text-3);
  font-size: 12px;
  text-align: center;
}

.quality-alert {
  margin-bottom: 8px;
}

.quality-alert .el-button {
  margin-top: 8px;
}

.quality-section h3 {
  margin: 0 0 10px;
  color: var(--text);
  font-size: 13px;
  font-weight: 650;
}

.quality-subtitle {
  margin: 14px 0 8px;
  color: var(--text-3);
  font-size: 11.5px;
  font-weight: 550;
}

.quality-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 8px;
  margin: 0;
}

.quality-cell {
  padding: 9px 11px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
}

.quality-cell dt {
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.6;
}

.quality-cell dd {
  margin: 2px 0 0;
  color: var(--text);
  font-size: 15px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.quality-stats .quality-cell dd {
  font-size: 14px;
}

.quality-scan-alert {
  margin-top: 10px;
}

.quality-empty-ok {
  margin: 0;
  padding: 10px 12px;
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.7;
}

.quality-issues {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.quality-issue {
  display: flex;
  align-items: center;
  gap: 8px;
}

.quality-issue-count {
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.quality-chunks-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.quality-chunks-head h3 {
  margin: 0;
}

.quality-chunks-total {
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.quality-chunk-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.quality-chunk {
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
}

.quality-chunk-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.quality-chunk-index {
  color: var(--brand-dark);
  font-weight: 600;
}

.quality-chunk-location {
  min-width: 0;
  overflow-wrap: anywhere;
}

.quality-chunk pre {
  margin: 8px 0 0;
  color: var(--text);
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.quality-pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 12px;
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

@media (max-width: 560px) {
  .quality-grid {
    grid-template-columns: 1fr 1fr;
  }

  .quality-head {
    flex-direction: column;
  }
}
</style>
