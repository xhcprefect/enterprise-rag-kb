<template>
  <div class="page">
    <div class="page-head docs-head">
      <div class="title-group">
        <el-button
          class="back-btn"
          circle
          :aria-label="t('documents.backAria')"
          @click="router.push('/knowledge')"
        >
          <el-icon><ArrowLeft /></el-icon>
        </el-button>
        <div class="title-copy">
          <span class="page-eyebrow">{{ t('documents.eyebrow') }}</span>
          <h2>{{ kb?.name || t('documents.title') }}</h2>
          <p>{{ kb?.description || t('documents.subtitle') }}</p>
        </div>
      </div>
      <div class="page-actions">
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>{{ t('common.refresh') }}
        </el-button>
        <el-button v-if="userStore.isAdmin" type="primary" @click="pickFiles">
          <el-icon><Upload /></el-icon>{{ t('documents.upload') }}
        </el-button>
        <input
          ref="fileInput"
          type="file"
          multiple
          hidden
          accept=".pdf,.docx,.txt,.md,.xlsx,.csv"
          @change="onFilesPicked"
        />
      </div>
    </div>

    <!-- 支持格式与续传说明 -->
    <div class="format-bar">
      <div class="format-tags">
        <span class="format-label">{{ t('documents.formatsLabel') }}</span>
        <span v-for="fmt in FORMATS" :key="fmt" class="format-chip">{{ fmt }}</span>
      </div>
      <span class="format-tip">
        <el-icon><InfoFilled /></el-icon>
        {{ t('documents.formatsTip') }}
      </span>
    </div>

    <!-- 未完成的上传：刷新/关闭页面后可从这里继续（只恢复当前会话） -->
    <el-alert
      v-if="resumeInfo && !uploadTask"
      type="warning"
      :closable="false"
      show-icon
      class="resume-alert"
    >
      <template #title>
        <div class="resume-title">
          <span>
            <i18n-t keypath="documents.resumeNotice" tag="span" scope="global">
              <template #name><strong>{{ resumeInfo.filename }}</strong></template>
              <template #percent>{{ resumeInfo.percent }}</template>
              <template #upload>{{ t('documents.upload') }}</template>
            </i18n-t>
          </span>
          <el-button link type="danger" size="small" @click="discardResume">
            {{ t('documents.discardResume') }}
          </el-button>
        </div>
      </template>
    </el-alert>

    <!-- 当前上传（同一时间只推进一个，完成后自动进入下一个） -->
    <section v-if="uploadTask" class="upload-card" :aria-label="t('documents.uploadAria')">
      <div class="upload-head">
        <div class="upload-file">
          <div class="upload-icon" :class="uploadTask.phase">
            <el-icon :size="18">
              <CircleCheck v-if="uploadTask.phase === 'done'" />
              <CircleClose v-else-if="uploadTask.phase === 'failed'" />
              <VideoPause v-else-if="uploadTask.phase === 'paused'" />
              <Loading v-else-if="uploadTask.phase === 'ingesting'" />
              <UploadFilled v-else />
            </el-icon>
          </div>
          <div class="upload-meta">
            <div class="upload-name-row">
              <span class="upload-name" :title="uploadTask.filename">{{ uploadTask.filename }}</span>
              <el-tag v-if="uploadTask.deduplicated" size="small" type="info" effect="plain">
                {{ t('documents.dedupBadge') }}
              </el-tag>
            </div>
            <span class="upload-size">{{ formatSize(uploadTask.fileSize) }}</span>
          </div>
        </div>
        <div class="upload-actions">
          <el-button
            v-if="uploadTask.phase === 'uploading' && !uploadTask.completing"
            size="small"
            @click="pauseCurrentUpload()"
          >
            {{ t('documents.pause') }}
          </el-button>
          <el-button
            v-if="uploadTask.phase === 'paused'"
            size="small"
            type="primary"
            @click="resumeCurrentUpload"
          >
            {{ t('documents.resumeAction') }}
          </el-button>
          <el-button
            v-if="uploadTask.phase === 'failed' && !canReindexExisting"
            size="small"
            type="primary"
            @click="retryFailed"
          >
            {{ t('documents.retry') }}
          </el-button>
          <el-button
            v-if="uploadTask.phase === 'failed' && canReindexExisting"
            size="small"
            type="primary"
            @click="reindexExistingCurrent"
          >
            {{ t('documents.rebuild') }}
          </el-button>
          <el-button
            v-if="uploadTask.phase === 'failed'"
            size="small"
            @click="skipFailed"
          >
            {{ t('documents.skip') }}
          </el-button>
          <el-button
            v-if="uploadTask.phase === 'done' || uploadTask.phase === 'cancelled'"
            size="small"
            @click="closeCurrent"
          >
            {{ t('common.close') }}
          </el-button>
          <el-button
            v-if="canCancelCurrent"
            size="small"
            type="danger"
            :plain="uploadTask.phase === 'paused'"
            @click="cancelCurrentUpload"
          >
            {{ t('documents.cancelCurrent') }}
          </el-button>
          <el-button
            v-if="waitingCount > 0"
            size="small"
            type="danger"
            plain
            @click="cancelAllUploads"
          >
            {{ t('documents.cancelAll') }}
          </el-button>
        </div>
      </div>

      <!-- 入库刚创建文档时后端 progress 恒为 0（worker 尚未领取，或还没落第一批分块），
           这时没有可信百分比，用流动条纹表示"在排队"，而不是画一根 0% 的定长条。 -->
      <el-progress
        v-if="ingestQueued"
        key="queued"
        :percentage="100"
        striped
        striped-flow
        :duration="1.2"
        :format="queueFormat"
        :stroke-width="8"
        class="upload-progress pending-progress"
      />
      <el-progress
        v-else
        key="determinate"
        :percentage="uploadTask.percent"
        :status="progressStatus"
        :stroke-width="8"
        class="upload-progress"
      />

      <div class="upload-foot">
        <span class="upload-hint">{{ uploadHint }}</span>
        <span v-if="waitingCount > 0" class="upload-chunks">
          {{ t('documents.queue.title', { count: waitingCount }) }}
        </span>
        <span v-if="uploadTask.totalChunks" class="upload-chunks">
          {{
            t('documents.chunks', {
              done: formatNumber(uploadTask.chunksDone),
              total: formatNumber(uploadTask.totalChunks)
            })
          }}
        </span>
      </div>
    </section>

    <!-- 等待队列：顺序上传；刷新后这些 File 不会保留 -->
    <section v-if="waitingEntries.length" class="queue-card">
      <div class="queue-head">
        <span class="queue-title">{{ t('documents.queue.title', { count: waitingEntries.length }) }}</span>
        <span class="queue-note">
          <el-icon><InfoFilled /></el-icon>{{ t('documents.queue.notPersisted') }}
        </span>
      </div>
      <div v-for="item in waitingEntries" :key="item.key" class="queue-row">
        <span class="queue-name" :title="item.file.name">{{ item.file.name }}</span>
        <span class="queue-size">{{ formatSize(item.file.size) }}</span>
        <el-button link type="danger" size="small" @click="removeWaitingUpload(item.key)">
          {{ t('documents.queue.remove') }}
        </el-button>
      </div>
    </section>

    <!-- 文档表 -->
    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          {{ t('documents.listTitle') }}
          <span class="count-badge">{{ formatNumber(total) }}</span>
        </div>
        <div class="panel-tools">
          <el-input
            v-model="searchInput"
            :placeholder="t('documents.searchPlaceholder')"
            :aria-label="t('documents.searchAria')"
            clearable
            class="doc-search"
            @input="onSearchInput"
            @clear="onSearchInput"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
          <el-select
            v-model="statusFilter"
            :placeholder="t('documents.filterPlaceholder')"
            clearable
            class="status-filter"
            :aria-label="t('documents.filterAria')"
            @change="onFilterChange"
          >
            <el-option :label="t('documents.status.ready')" value="ready" />
            <el-option :label="t('documents.status.pending')" value="pending" />
            <el-option :label="t('documents.status.processing')" value="processing" />
            <el-option :label="t('documents.status.failed')" value="failed" />
            <el-option :label="t('documents.status.cancelled')" value="cancelled" />
          </el-select>
        </div>
      </div>

      <el-table v-loading="loading" :data="docs" class="table">
        <el-table-column :label="t('documents.table.filename')" min-width="280">
          <template #default="{ row }">
            <div class="file-cell">
              <span class="file-ext" :class="extClass(row.file_ext)">{{ extLabel(row.file_ext) }}</span>
              <span class="file-name" :title="row.filename">{{ row.filename }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('documents.table.size')" width="100">
          <template #default="{ row }">
            <span class="num">{{ formatSize(row.file_size) }}</span>
          </template>
        </el-table-column>

        <!-- 状态与进度合并：就绪/失败/取消显示标签，排队与处理中直接显示进度条 -->
        <el-table-column :label="t('documents.table.status')" width="180">
          <template #default="{ row }">
            <el-progress
              v-if="row.status === 'pending' || (row.status === 'processing' && !row.progress)"
              :percentage="100"
              striped
              striped-flow
              :duration="1.2"
              :format="queueFormat"
              :stroke-width="6"
              :text-inside="false"
              class="pending-progress"
            />
            <el-progress
              v-else-if="row.status === 'processing'"
              :percentage="row.progress"
              :stroke-width="6"
              :text-inside="false"
            />
            <el-tooltip
              v-else-if="row.status === 'failed'"
              :content="row.error_message || t('documents.failedFallback')"
              placement="top"
            >
              <el-tag type="danger" size="small" effect="light">{{ t('documents.status.failed') }}</el-tag>
            </el-tooltip>
            <el-tooltip
              v-else-if="row.status === 'cancelled'"
              :content="t('documents.cancelledTip')"
              placement="top"
            >
              <el-tag type="info" size="small" effect="light">{{ t('documents.status.cancelled') }}</el-tag>
            </el-tooltip>
            <span v-else-if="row.status === 'ready'" class="ready-mark">
              <el-icon><CircleCheckFilled /></el-icon>{{ t('documents.status.ready') }}
            </span>
            <el-tag v-else :type="statusType(row.status)" size="small" effect="light">
              {{ statusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="chunk_count" :label="t('documents.table.chunks')" width="80" align="center">
          <template #default="{ row }">
            <span class="num">{{ formatNumber(row.chunk_count) }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('documents.table.chars')" width="100" align="right">
          <template #default="{ row }">
            <span class="num">{{ formatNumber(row.char_count) }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('documents.table.uploadedAt')" width="190">
          <template #default="{ row }">
            <span class="time">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column
          v-if="userStore.isAdmin"
          :label="t('documents.table.actions')"
          width="210"
          fixed="right"
        >
          <template #default="{ row }">
            <div class="row-actions">
              <el-button
                v-if="row.status === 'pending' || row.status === 'processing'"
                link
                type="warning"
                size="small"
                @click="cancelIngest(row)"
              >
                {{ t('documents.cancelIngest') }}
              </el-button>
              <el-button link type="primary" size="small" @click="reindex(row)">
                {{ t('documents.rebuild') }}
              </el-button>
              <el-button link type="danger" size="small" @click="remove(row)">
                {{ t('common.delete') }}
              </el-button>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty
            :description="t('documents.empty', { upload: t('documents.upload') })"
            :image-size="80"
          />
        </template>
      </el-table>

      <div class="panel-foot">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  docApi,
  isProjectKb,
  kbApi,
  uploadApi,
  type DocumentItem,
  type KnowledgeBase,
  type UploadCompleteResult,
  type UploadSession
} from '@/api'
import { useUserStore } from '@/stores/user'
import { hashFile, isIngestQueued } from '@/utils/upload'
import { formatDateTime, formatNumber } from '@/i18n/format'
import {
  acceptsEntryReceipt,
  acceptsResponse,
  advance,
  decideAfterSession,
  emptyQueue,
  enqueue,
  patchCurrent,
  pauseCurrent,
  removeWaiting,
  retryCurrent,
  signature,
  skipCurrent
} from '@/utils/documentQueue'
import type { QueueItem, QueuePhase, QueueState } from '@/utils/documentQueue'
import {
  needsExplicitReindex,
  reuseMessageKey,
  reuseOutcome,
  selectPickedFiles
} from '@/utils/documentIdentity'
import { isLatestResponse, isStalePoll, makeQuery, queryParams, shouldPoll } from '@/utils/documentList'
import { runWorkerGroup } from '@/utils/documentChunks'
import {
  canApplyRestoredResume,
  clearResumeRecord,
  readResume,
  resumeKey,
  shouldClearOnResumeFailure,
  writeResume
} from '@/utils/documentResume'

/** 当前任务的进度数据；phase 由队列条目统一持有。 */
interface UploadTask {
  docId: string
  filename: string
  fileSize: number
  percent: number
  // 分片上传状态（入库阶段后保留，便于展示）
  uploadId: string
  chunkSize: number
  totalChunks: number
  chunksDone: number
  /** 服务端文档状态，用于复用失败/取消时提示重建索引 */
  docStatus: string
  /** 分片已传完、正在等待服务端合并；此时不能暂停（合并无法中断） */
  completing: boolean
}

interface UploadEntry extends QueueItem {
  task: UploadTask
  /** 已完整算出的摘要；暂停后继续上传时不必重算整个文件 */
  digest?: string
}

// 只用于"刷新后提示可续传"的轻量记录，真正的进度以服务端为准
interface ResumeInfo {
  uploadId: string
  filename: string
  fileSize: number
  percent: number
}

const RESUME_KEY = resumeKey
// 同一时间最多 3 片在飞：提升带宽利用率，又不会压垮后端
const UPLOAD_CONCURRENCY = 3
// 单片失败重试次数（网络抖动）
const CHUNK_RETRIES = 3
const FORMATS = ['PDF', 'Word', 'TXT', 'Markdown', 'Excel', 'CSV']
const SEARCH_DEBOUNCE_MS = 300

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const { t } = useI18n()

const kbId = route.params.kbId as string
const kb = ref<KnowledgeBase | null>(null)
const docs = ref<DocumentItem[]>([])
const loading = ref(false)
const hashing = ref(false)
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const statusFilter = ref('')
const searchInput = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

// 顺序上传：一个 current + 一个 queue + 每项 phase
const queueState = ref<QueueState<UploadEntry>>(emptyQueue<UploadEntry>())
const uploadAbort = ref<AbortController | null>(null)
const resumeInfo = ref<ResumeInfo | null>(null)
// 唯一标识当前真正在网络上传的条目，避免同一任务被并发启动两次
let activeKey: string | null = null
// 每次 runEntry 递增：上一轮分片 worker 的迟到进度/续传回执不能污染重试后的同一条目
let uploadGeneration = 0
let keyCounter = 0

const currentEntry = computed(() => queueState.value.current)
const waitingEntries = computed(() => queueState.value.queue)
const waitingCount = computed(() => waitingEntries.value.length)

/** 模板统一读取的当前任务快照（phase 来自队列）。 */
const uploadTask = computed(() => {
  const entry = currentEntry.value
  if (!entry) return null
  return { ...entry.task, phase: entry.phase, deduplicated: entry.deduplicated }
})

const canCancelCurrent = computed(() => {
  const phase = uploadTask.value?.phase
  return phase === 'uploading' || phase === 'paused' || phase === 'ingesting'
})

/**
 * 已经有文档记录（新建或复用）且处于失败/取消：只能显式重建索引，
 * 不能假装成功，也不能悄悄重复上传后再计费。
 */
const canReindexExisting = computed(() => {
  const entry = currentEntry.value
  return !!entry && entry.phase === 'failed' && !!entry.task.docId &&
    (entry.task.docStatus === 'failed' || entry.task.docStatus === 'cancelled')
})

// 入库阶段的 0 表示"worker 还没领取 / 还没落第一批进度"，不是"进度为零"。
const ingestQueued = computed(() => {
  const task = uploadTask.value
  return !!task && isIngestQueued(task.phase, task.percent)
})

const queueFormat = () => t('documents.queued')

const uploadHint = computed(() => {
  const task = uploadTask.value
  if (!task) return ''
  if (hashing.value && task.phase === 'uploading') return t('documents.hint.hashing')
  if (task.phase === 'uploading' && task.completing) return t('documents.hint.completing')
  if (task.phase === 'uploading') return t('documents.hint.uploading', { percent: task.percent })
  if (task.phase === 'paused') return t('documents.hint.paused', { percent: task.percent })
  if (task.phase === 'ingesting') {
    if (task.deduplicated) return t('documents.hint.reusedIngesting')
    // 0 时不要显示 "0%"，否则看起来像卡死了
    return task.percent === 0
      ? t('documents.hint.queued')
      : t('documents.hint.ingesting', { percent: task.percent })
  }
  if (task.phase === 'done') return t('documents.hint.done')
  if (task.phase === 'cancelled') return t('documents.hint.cancelled')
  return task.deduplicated
    ? t('documents.hint.reusedNeedsReindex')
    : t('documents.hint.failed')
})

const progressStatus = computed(() => {
  const phase = uploadTask.value?.phase
  if (phase === 'done') return 'success'
  if (phase === 'cancelled' || phase === 'paused') return 'warning'
  if (phase === 'failed') return 'exception'
  return undefined
})

// 搜索、翻页、轮询共用的查询快照
const query = computed(() =>
  makeQuery({
    q: searchInput.value,
    page: page.value,
    pageSize: pageSize.value,
    status: statusFilter.value
  })
)

let timer: number | undefined
let searchTimer: number | undefined
let disposed = false
let pollInFlight = false
// 任意列表请求（含轮询）的序号：迟到的旧响应一律丢弃
const listEpoch = ref(0)
// 最近一次"用户操作触发的加载"序号，仅用于收掉 loading
const loadEpoch = ref(0)

function statusType(status: string) {
  if (status === 'ready') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'cancelled') return 'info'
  return 'warning'
}

function statusText(status: string) {
  return (
    {
      pending: t('documents.status.pending'),
      processing: t('documents.status.processing'),
      ready: t('documents.status.ready'),
      failed: t('documents.status.failed'),
      cancelled: t('documents.status.cancelled')
    }[status] || status
  )
}

const EXT_CLASS: Record<string, string> = {
  '.pdf': 'pdf',
  '.docx': 'doc',
  '.doc': 'doc',
  '.xlsx': 'xls',
  '.xls': 'xls',
  '.csv': 'csv',
  '.md': 'md',
  '.txt': 'txt'
}

function extLabel(ext: string) {
  return ext.replace('.', '').toUpperCase().slice(0, 4)
}

function extClass(ext: string) {
  return EXT_CLASS[ext] || 'txt'
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function isCancel(err: any) {
  return err?.name === 'CanceledError' || err?.code === 'ERR_CANCELED' || err?.name === 'AbortError'
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// ---------- 列表加载 / 搜索 / 轮询 ----------

async function load() {
  if (disposed) return
  const snapshot = query.value
  const sequence = ++listEpoch.value
  loadEpoch.value = sequence
  loading.value = true
  try {
    const [kbRes, docRes] = await Promise.all([
      kbApi.list(),
      docApi.list(kbId, queryParams(snapshot))
    ])
    if (!isLatestResponse(sequence, listEpoch.value, snapshot, query.value, disposed)) return
    kb.value = kbRes.data.find((k) => k.id === kbId) || null
    // 项目索引库没有普通文档：直链进来时改去项目页，绝不停留在上传/重建索引界面。
    if (isProjectKb(kb.value)) {
      const sourceId = kb.value?.project_source_id
      void router.replace(sourceId ? `/projects/${sourceId}` : '/projects')
      return
    }
    docs.value = docRes.data.items
    total.value = docRes.data.total
    if (shouldPoll(docs.value.map((d) => d.status), currentEntry.value?.phase === 'ingesting')) {
      startPolling()
    } else {
      stopPolling()
    }
  } catch (err) {
    if (!isCancel(err) && sequence === listEpoch.value && !disposed) {
      ElMessage.error(t('documents.toast.listFailed'))
    }
  } finally {
    if (loadEpoch.value === sequence) loading.value = false
  }
}

function onSearchInput() {
  if (searchTimer) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    searchTimer = undefined
    page.value = 1
    load()
  }, SEARCH_DEBOUNCE_MS)
}

function onFilterChange(value: string | null | undefined) {
  // 显式写入事件值：不依赖 v-model 更新与 change 事件的先后顺序
  statusFilter.value = value ?? ''
  page.value = 1
  load()
}

function onPageChange(value: number) {
  page.value = value
  load()
}

function onSizeChange(value: number) {
  pageSize.value = value
  page.value = 1
  load()
}

function startPolling() {
  stopPolling()
  if (disposed) return
  timer = window.setInterval(pollOnce, 1500)
}

async function pollOnce() {
  // 用户触发的加载优先：它自会决定是否继续轮询，避免两个响应互相覆盖
  if (pollInFlight || disposed || loading.value) return
  const snapshot = query.value
  const entryAtStart = currentEntry.value
  if (!shouldPoll(docs.value.map((d) => d.status), entryAtStart?.phase === 'ingesting')) {
    stopPolling()
    return
  }
  pollInFlight = true
  // 轮询不推进 epoch：否则会把自己之前发出的用户加载判成迟到响应，
  // 导致 loading 永远收不掉。只认"期间没有更新的用户请求/查询变化"。
  const epoch = listEpoch.value
  // 这一拍所有请求都属于发起时的条目：迟到的 404 只能处置它自己，不能推进新的当前项
  const pollKey = entryAtStart?.key ?? null
  try {
    const { data } = await docApi.list(kbId, queryParams(snapshot))
    if (!isLatestResponse(epoch, listEpoch.value, snapshot, query.value, disposed)) return
    docs.value = data.items
    total.value = data.total
    const entry = currentEntry.value
    if (entry && entry.key === pollKey && entry.phase === 'ingesting' && entry.task.docId) {
      const { data: active } = await docApi.get(kbId, entry.task.docId)
      if (!disposed && queueState.value.current?.key === pollKey) syncUploadEntry(active)
    }
    if (!shouldPoll(data.items.map((d) => d.status), queueState.value.current?.phase === 'ingesting')) {
      stopPolling()
    }
  } catch (err: any) {
    const stale = isStalePoll(pollKey, queueState.value.current?.key, epoch, listEpoch.value, disposed)
    if (err?.response?.status === 404 && !stale && currentEntry.value?.phase === 'ingesting') {
      // 文档被别处删除：不复活卡片、不无限轮询，直接推进队列
      stopPolling()
      ElMessage.warning(t('documents.toast.documentGone'))
      advanceQueue()
      await load()
    }
    // 网络抖动（或已被新条目/新查询取代的迟到失败）下一拍重试，确保同一时间只有一个轮询请求。
  } finally {
    pollInFlight = false
  }
}

function stopPolling() {
  if (timer) {
    clearInterval(timer)
    timer = undefined
  }
}

// ---------- 队列推进 ----------

function makeEntry(file: File): UploadEntry {
  keyCounter += 1
  return {
    key: `${signature(file)}#${keyCounter}`,
    file,
    phase: 'waiting',
    deduplicated: false,
    task: reactive<UploadTask>({
      docId: '',
      filename: file.name,
      fileSize: file.size,
      percent: 0,
      uploadId: '',
      chunkSize: 0,
      totalChunks: 0,
      chunksDone: 0,
      docStatus: '',
      completing: false
    })
  }
}

function startNext() {
  if (disposed || activeKey) return
  const entry = queueState.value.current
  if (!entry || entry.phase !== 'uploading') return
  void runEntry(entry)
}

function isCurrentPhase(key: string, phases: QueuePhase[]) {
  return acceptsResponse(queueState.value, key, phases)
}

function updateCurrentTask(key: string, patch: Partial<UploadTask>) {
  const entry = queueState.value.current
  if (!entry || entry.key !== key) return
  Object.assign(entry.task, patch)
}

function setCurrentPhase(phase: QueuePhase) {
  const state = queueState.value
  if (!state.current) return
  queueState.value = { current: { ...state.current, phase }, queue: state.queue }
}

/** 完成/取消后推进到下一个；失败时停住等用户重试或跳过。 */
function advanceQueue() {
  queueState.value = advance({ current: null, queue: queueState.value.queue })
  startNext()
}

function closeCurrent() {
  const phase = queueState.value.current?.phase
  if (phase === 'done' || phase === 'cancelled') advanceQueue()
  else if (phase === 'failed') skipCurrentUpload()
}

function markDone(key: string) {
  const entry = queueState.value.current
  if (!entry || entry.key !== key || entry.phase !== 'ingesting') return
  entry.task.docStatus = 'ready'
  entry.task.percent = 100
  setCurrentPhase('done')
  if (!entry.deduplicated) ElMessage.success(t('documents.toast.ingestDone'))
  window.setTimeout(() => {
    if (queueState.value.current?.key === key && queueState.value.current.phase === 'done') advanceQueue()
  }, 1500)
}

function markCancelled(key: string) {
  const entry = queueState.value.current
  if (!entry || entry.key !== key || entry.phase !== 'ingesting') return
  entry.task.docStatus = 'cancelled'
  setCurrentPhase('cancelled')
  ElMessage.info(t('documents.hint.cancelled'))
  window.setTimeout(() => {
    if (queueState.value.current?.key === key && queueState.value.current.phase === 'cancelled') advanceQueue()
  }, 1500)
}

function markFailed(key: string, message?: string) {
  const entry = queueState.value.current
  if (!entry || entry.key !== key) return
  const wasIngesting = entry.phase === 'ingesting'
  entry.task.docStatus = 'failed'
  setCurrentPhase('failed')
  if (entry.deduplicated && wasIngesting) {
    // 复用记录的入库失败：状态保持不变，必须显式重建索引
    ElMessage.warning(t('documents.toast.reusedFailed'))
    return
  }
  ElMessage.error(message || t('documents.toast.ingestFailed'))
}

function pauseCurrentUpload(quiet = false) {
  const entry = queueState.value.current
  if (!entry || entry.phase !== 'uploading' || entry.task.completing) return
  queueState.value = pauseCurrent(queueState.value)
  uploadAbort.value?.abort()
  if (!quiet) ElMessage.info(t('documents.toast.paused'))
  void refreshSessionProgress(entry)
}

function resumeCurrentUpload() {
  const entry = queueState.value.current
  if (!entry || entry.phase !== 'paused') return
  queueState.value = patchCurrent(queueState.value, { phase: 'uploading' })
  startNext()
}

async function refreshSessionProgress(entry: UploadEntry) {
  if (!entry.task.uploadId) return
  // 在途分片被掐断，服务端只保留完整落盘的那些：以会话为准校准进度
  await sleep(1200)
  if (disposed) return
  const current = queueState.value.current
  if (!current || current.key !== entry.key || current.phase !== 'paused') return
  try {
    const { data } = await uploadApi.getSession(kbId, entry.task.uploadId)
    // 迟到的会话进度回执：卸载/取消/换了条目/换了会话都不能再写
    if (disposed) return
    const latest = queueState.value.current
    if (!latest || latest.key !== entry.key || latest.phase !== 'paused') return
    if (latest.task.uploadId !== entry.task.uploadId) return
    latest.task.chunksDone = data.received.length
    latest.task.percent = data.file_size
      ? Math.min(99, Math.floor((data.received_bytes / data.file_size) * 100))
      : 0
    saveResume(latest)
  } catch {
    /* 断网时保留本地估算值 */
  }
}

function retryFailed() {
  const entry = queueState.value.current
  if (!entry || entry.phase !== 'failed') return
  if (canReindexExisting.value) {
    // 文档记录已存在（新建或复用）且失败/取消：走显式重建索引，不重复上传
    void reindexExisting(entry)
    return
  }
  // 纯上传阶段失败：同一文件重新上传，服务端会话会续传已落盘分片
  queueState.value = retryCurrent(queueState.value)
  updateCurrentTask(entry.key, { completing: false })
  startNext()
}

/**
 * 对已存在文档显式重建索引。用户主动点击重试等同于确认重新入库，
 * 与列表里的「重建索引」是同一动作。
 */
async function reindexExisting(entry: UploadEntry) {
  // 回执只属于"这个失败条目"：期间用户可能已跳过 / 取消全部，
  // 队列也会推进到下一个上传条目，届时任何副作用都必须丢弃。
  const receipt = { key: entry.key, docId: entry.task.docId }
  const stillOurs = () => {
    const current = queueState.value.current
    if (!current) return false
    return acceptsEntryReceipt(receipt, {
      key: current.key,
      docId: current.task.docId,
      phase: current.phase
    }, ['failed'], disposed)
  }
  try {
    await docApi.reindex(kbId, entry.task.docId)
  } catch {
    // 失败提示同样只对原条目有效，且卸载后静默
    if (!disposed && stillOurs()) ElMessage.error(t('documents.toast.reindexFailed'))
    return
  }
  // 迟到成功回执：不能把下一个上传条目强置为 ingesting
  if (!stillOurs()) return
  updateCurrentTask(entry.key, { docStatus: 'pending', percent: 0 })
  setCurrentPhase('ingesting')
  ElMessage.success(t('documents.toast.reindexQueued'))
  await load()
  startPolling()
}

function skipCurrentUpload() {
  queueState.value = skipCurrent(queueState.value)
  startNext()
}

function skipFailed() {
  skipCurrentUpload()
}

function removeWaitingUpload(key: string) {
  queueState.value = removeWaiting(queueState.value, key)
}

async function reindexExistingCurrent() {
  const entry = queueState.value.current
  if (!entry || !entry.task.docId) return
  await reindexExisting(entry)
}

async function cancelCurrentUpload() {
  const entry = queueState.value.current
  if (!entry) return
  if (entry.phase === 'ingesting') {
    await cancelIngestById(entry.task.docId, entry.task.filename, entry.key)
    return
  }
  if (entry.phase === 'failed') {
    skipFailed()
    return
  }
  if (entry.phase === 'done' || entry.phase === 'cancelled') {
    advanceQueue()
    return
  }
  abortEntryUpload(entry)
  advanceQueue()
  ElMessage.info(t('documents.toast.cancelled'))
}

function abortEntryUpload(entry: UploadEntry) {
  // 合并阶段也走同一个 controller：abort 让 complete 的客户端等待立刻结束，
  // runEntry 的 finally 随后释放 activeKey 并推进队列。
  uploadAbort.value?.abort()
  // 只清本条目的续传记录：同一 KB 的另一个页面实例可能已经写了新会话。
  // 还没有会话（摘要/建会话阶段）时没有属于自己的记录，什么也不清。
  if (entry.task.uploadId) {
    clearResume(entry.task.uploadId)
    void uploadApi.abort(kbId, entry.task.uploadId).catch(() => undefined)
  }
}

async function cancelAllUploads() {
  // 先停掉队列与当前项，再尽力清理服务端资源；迟到的进度/完成回执会被阶段守卫丢弃
  const entry = currentEntry.value
  queueState.value = emptyQueue<UploadEntry>()
  uploadAbort.value?.abort()
  uploadAbort.value = null
  // 取消全部是用户显式动作：只清当前条目对应的记录，别动别的页面实例的会话
  if (entry?.task.uploadId) clearResume(entry.task.uploadId)
  if (entry) {
    if (entry.phase === 'ingesting' && entry.task.docId) {
      try {
        await docApi.cancel(kbId, entry.task.docId)
      } catch {
        /* 已结束则忽略 */
      }
    } else if (entry.task.uploadId) {
      try {
        await uploadApi.abort(kbId, entry.task.uploadId)
      } catch {
        /* 已过期则无需清理 */
      }
    }
  }
  ElMessage.info(t('documents.toast.cancelledAll'))
  await load()
}

function syncUploadEntry(row: DocumentItem) {
  const entry = queueState.value.current
  if (!entry || entry.phase !== 'ingesting' || entry.task.docId !== row.id) return
  entry.task.docStatus = row.status
  if (row.status === 'ready') {
    markDone(entry.key)
  } else if (row.status === 'failed') {
    markFailed(entry.key, row.error_message || undefined)
  } else if (row.status === 'cancelled') {
    markCancelled(entry.key)
  } else {
    entry.task.percent = row.progress
  }
}

// ---------- 文件选择 / 上传 ----------

function pickFiles() {
  fileInput.value?.click()
}

function onFilesPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const picked = Array.from(input.files ?? [])
  // 允许再次选择同一文件
  input.value = ''
  if (!picked.length) return

  // 只有"刷新后恢复的未完成会话、且当前没有任何任务"时，才要求先选择同一文件续传；
  // 正在上传时 resumeInfo 也指向当前会话，此时必须允许追加其它文件到等待队列。
  const pending = resumeInfo.value
  const { accepted, mismatched } = selectPickedFiles(
    picked,
    pending,
    !!queueState.value.current
  )
  if (mismatched.length && pending) {
    ElMessage.warning(t('documents.resumeMismatch', { name: pending.filename }))
  }
  if (!accepted.length) return

  const items = accepted.map(makeEntry)
  const { state, rejected } = enqueue(queueState.value, items)
  queueState.value = state
  if (rejected.length) {
    ElMessage.warning(
      t('documents.queue.duplicateSkipped', {
        count: rejected.length,
        name: rejected[0].file.name
      })
    )
  }
  startNext()
}

async function runEntry(entry: UploadEntry) {
  activeKey = entry.key
  const generation = ++uploadGeneration
  const controller = new AbortController()
  uploadAbort.value = controller
  const task = entry.task

  try {
    // 1) 完整内容摘要识别文件（同时用于服务端去重与续传身份），分块读取
    let digest = entry.digest
    if (!digest) {
      hashing.value = true
      digest = await hashFile(entry.file, controller.signal)
      hashing.value = false
      // pauseCurrent 会生成新的 current 对象：摘要要写到队列里当前那份，
      // 否则恢复上传后还得把整个文件重新哈希一遍。
      const current = queueState.value.current
      if (current && current.key === entry.key) current.digest = digest
      else entry.digest = digest
    }
    if (!isCurrentPhase(entry.key, ['uploading'])) return

    const { data: session } = await uploadApi.createSession(
      kbId,
      entry.file.name,
      entry.file.size,
      digest
    )
    // 卸载后迟到的会话回执：不写续传记录、也不推进界面。
    // 这里刻意不 abort：同一 KB 新实例若选了同一文件，服务端会按指纹复用
    // 同一个会话，abort 会把新实例的上传一起终止；无人认领的会话交给 TTL 清理。
    if (disposed) return
    // pauseCurrent 会生成新的 current 对象，entry.phase 只是请求前的旧快照，
    // 必须按队列里的实际相位决定：继续 / 暂停 / 放弃。
    const decision = decideAfterSession(queueState.value, entry.key)
    if (decision === 'abandon') {
      // 建会话期间被取消/跳过：放弃刚建出的会话，避免留下孤立分片。
      // 暂停不算取消：保留 session 供恢复后续传。
      void uploadApi.abort(kbId, session.upload_id).catch(() => undefined)
      return
    }
    if (decision === 'pause') {
      // 建会话期间被暂停：只记住会话身份，等用户点继续再推进
      Object.assign(task, {
        uploadId: session.upload_id,
        chunkSize: session.chunk_size,
        totalChunks: session.total_chunks
      })
      saveResume(entry, generation)
      return
    }
    Object.assign(task, {
      uploadId: session.upload_id,
      chunkSize: session.chunk_size,
      totalChunks: session.total_chunks
    })

    const done = new Set<number>(session.received)
    task.chunksDone = done.size
    task.percent = session.file_size
      ? Math.min(99, Math.floor((session.received_bytes / session.file_size) * 100))
      : 0
    saveResume(entry, generation)

    // 2) 并发上传剩余分片
    await uploadMissingChunks(entry, session, done, controller.signal, generation)
    if (controller.signal.aborted) throw new DOMException('aborted', 'AbortError')
    if (!isCurrentPhase(entry.key, ['uploading'])) return

    // 3) 合并分片 -> 复用或创建文档。
    // 合并本身不可中断，但客户端的等待必须可中断：把本次上传的 controller.signal
    // 传进去，用户取消/取消全部/离开页面时立刻结束等待，finally 释放 activeKey，
    // 下一个排队条目才能被推进；迟到的服务端回执不会再写任何界面状态。
    task.percent = 100
    task.completing = true
    const { data: completed } = await uploadApi.complete(kbId, session.upload_id, controller.signal)
    // 被取消/卸载：不写回执（current 可能已换成下一条或已清空），也不删续传记录
    if (disposed || controller.signal.aborted) return
    // 先判卸载再清记录：旧实例的迟到 complete 不能删掉新实例写入的续传记录
    clearResume(session.upload_id)
    // 用户点了暂停时服务端仍可能已经合并完成，按完成处理；
    // 只有被取消/跳过/取消全部时才丢弃回执。
    if (!isCurrentPhase(entry.key, ['uploading', 'paused'])) {
      await load()
      return
    }
    handleCompleted(entry.key, completed)
  } catch (err: any) {
    // 卸载后的迟到失败不弹提示、不改队列：新实例可能已经在传别的会话
    if (disposed || isCancel(err)) return
    if (!isCurrentPhase(entry.key, ['uploading'])) return
    markFailed(entry.key, err?.response?.data?.message || t('documents.toast.uploadFailed'))
    // 会话与已传分片仍在服务端：保留 resume 记录，可重试或刷新后继续
  } finally {
    hashing.value = false
    if (uploadAbort.value === controller) uploadAbort.value = null
    // 取消当前后新的条目可能已经开始，不能把它的占用标记清掉
    if (activeKey === entry.key) activeKey = null
    // 取消当前/暂停等场景下，这里可能已经有下一个待推进的条目
    startNext()
  }
}

function handleCompleted(key: string, completed: UploadCompleteResult) {
  const entry = queueState.value.current
  if (!entry || entry.key !== key) return
  const doc = completed.document
  entry.deduplicated = completed.deduplicated
  Object.assign(entry.task, {
    docId: doc.id,
    docStatus: doc.status,
    percent: doc.progress
  })

  if (completed.deduplicated) {
    const outcome = reuseOutcome(true, doc.status)
    const key = reuseMessageKey(outcome)
    if (key) {
      if (needsExplicitReindex(outcome)) ElMessage.warning(t(key))
      else ElMessage.info(t(key))
    }
    if (needsExplicitReindex(outcome)) {
      // 复用记录本身失败/取消：保留状态，等用户显式重建索引或跳过
      setCurrentPhase('failed')
      void load()
      return
    }
  }

  setCurrentPhase('ingesting')
  void load()
  // 复用的文档可能早已就绪：没有可轮询的状态，直接收口
  if (doc.status === 'ready') {
    markDone(entry.key)
    return
  }
  startPolling()
}

function updateEntryPercent(entry: UploadEntry, inflightLoaded: number) {
  if (!entry.file.size) return
  const uploaded = entry.task.chunksDone * entry.task.chunkSize + inflightLoaded
  entry.task.percent = Math.min(99, Math.floor((uploaded / entry.file.size) * 100))
}

/** 按并发度上传所有缺失分片；任一片重试耗尽则中止同组并抛错。 */
async function uploadMissingChunks(
  entry: UploadEntry,
  session: UploadSession,
  done: Set<number>,
  signal: AbortSignal,
  generation: number
) {
  const pending: number[] = []
  for (let i = 0; i < session.total_chunks; i += 1) {
    if (!done.has(i)) pending.push(i)
  }

  // 每片实时进度（只用于让进度条平滑，不计入已完成集合）
  const inflight = new Map<number, number>()
  // 只有本次 runEntry 的 worker 才能写进度/续传记录；重试后旧组一律作废
  const live = () => !signal.aborted && generation === uploadGeneration

  async function runOne(index: number, chunkSignal: AbortSignal) {
    let attempt = 0
    while (true) {
      if (chunkSignal.aborted) throw new DOMException('aborted', 'AbortError')
      const start = index * session.chunk_size
      const blob = entry.file.slice(start, Math.min(start + session.chunk_size, entry.file.size))
      try {
        await uploadApi.uploadChunk(kbId, session.upload_id, index, blob, chunkSignal, (loaded) => {
          // 本组已因兄弟分片失败而中止时，迟到的进度回调不能再写进度条
          if (!live() || chunkSignal.aborted) return
          inflight.set(index, loaded)
          if (isCurrentPhase(entry.key, ['uploading'])) {
            let extra = 0
            inflight.forEach((v) => (extra += v))
            updateEntryPercent(entry, extra)
          }
        })
        inflight.delete(index)
        // 旧组的迟到成功回执：不写进度、不写续传，避免污染重试后的条目
        if (!live()) throw new DOMException('aborted', 'AbortError')
        done.add(index)
        entry.task.chunksDone = done.size
        updateEntryPercent(entry, [...inflight.values()].reduce((a, b) => a + b, 0))
        if (isCurrentPhase(entry.key, ['uploading', 'paused'])) saveResume(entry, generation)
        return
      } catch (err) {
        inflight.delete(index)
        if (isCancel(err) || chunkSignal.aborted) throw err
        attempt += 1
        if (attempt >= CHUNK_RETRIES) throw err
        await sleep(500 * attempt) // 退避后重试同一片（服务端幂等）
      }
    }
  }

  // 失败时组内其它分片立即中止，并等全部在途请求 settle 后才返回；
  // 否则用户立即重试会在同一 key 上叠出第二组 worker，旧组的进度还会写进新任务。
  await runWorkerGroup(pending, runOne, { concurrency: UPLOAD_CONCURRENCY, signal })
}

// ---------- 续传记录 ----------

function saveResume(entry: UploadEntry, generation?: number) {
  if (!entry.task.uploadId) return
  // 卸载后的迟到回执不能写存储或界面：新实例可能已经在传另一个会话
  if (disposed) return
  // 旧一组 worker 的迟到回执不能覆盖当前会话的续传记录
  if (generation !== undefined && generation !== uploadGeneration) return
  const info: ResumeInfo = {
    uploadId: entry.task.uploadId,
    filename: entry.task.filename,
    fileSize: entry.task.fileSize,
    percent: entry.task.percent
  }
  resumeInfo.value = info
  writeResume(localStorage, kbId, info, {
    disposed,
    ownerUploadId: entry.task.uploadId
  })
}

/**
 * 清除续传记录：只会清掉"当前持久记录就属于这个 upload_id"的那条。
 *
 * 组件卸载后旧 complete 迟到，而用户已重新进入并为同一 KB 启动了新上传时，
 * 绝不能清掉新记录；因此这里不接受"无条件清除"。
 */
function clearResume(uploadId: string) {
  if (disposed) return
  const removed = clearResumeRecord(localStorage, kbId, uploadId, { disposed })
  // 界面提示只在"清掉的正是当前展示的那条"时移除
  if (removed || resumeInfo.value?.uploadId === uploadId) resumeInfo.value = null
}

async function restoreResume() {
  const raw = localStorage.getItem(RESUME_KEY(kbId))
  if (!raw) return
  const saved = readResume(localStorage, kbId)
  if (!saved) {
    // 内容损坏：顺手清掉，避免反复解析失败
    localStorage.removeItem(RESUME_KEY(kbId))
    return
  }
  try {
    // 以服务端为准：会话可能已过期或被别处完成
    const { data } = await uploadApi.getSession(kbId, saved.uploadId)
    // 迟到返回：页面已卸载、用户已开始新上传、或持久记录已被新会话取代。
    // 此时什么都不做——记录可能已属于新实例，不能清也不能覆盖界面。
    if (!canApplyRestoredResume(saved, readResume(localStorage, kbId), !!currentEntry.value, disposed)) {
      return
    }
    if (data.status !== 'uploading') {
      clearResume(saved.uploadId)
      return
    }
    const percent = data.file_size
      ? Math.floor((data.received_bytes / data.file_size) * 100)
      : 0
    resumeInfo.value = { ...saved, percent }
  } catch (err: any) {
    // 只有确证会话已不存在（404/410）才清记录；断网/超时/5xx 保留，
    // 否则一次网络抖动会删掉用户正在续传的会话记录。
    if (
      shouldClearOnResumeFailure(
        saved,
        readResume(localStorage, kbId),
        !!currentEntry.value,
        disposed,
        err?.response?.status
      )
    ) {
      clearResume(saved.uploadId)
    }
  }
}

async function discardResume() {
  const pending = resumeInfo.value
  if (!pending) return
  // 用户明确要放弃的只是界面上这条记录
  clearResume(pending.uploadId)
  try {
    await uploadApi.abort(kbId, pending.uploadId)
    if (!disposed) ElMessage.success(t('documents.toast.discarded'))
  } catch {
    /* 会话可能已过期，忽略 */
  }
}

// ---------- 行操作 ----------

async function cancelIngest(row: DocumentItem) {
  await cancelIngestById(row.id, row.filename)
}

async function cancelIngestById(docId: string, filename: string, entryKey?: string) {
  try {
    await ElMessageBox.confirm(
      t('documents.confirm.cancelIngest', { name: filename }),
      t('documents.confirm.cancelIngestTitle'),
      {
        type: 'warning',
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel')
      }
    )
  } catch {
    return // 用户点了取消，不是错误
  }
  try {
    await docApi.cancel(kbId, docId)
  } catch {
    ElMessage.error(t('documents.toast.cancelFailed'))
    return
  }
  ElMessage.success(t('documents.toast.cancelSubmitted'))
  if (entryKey && queueState.value.current?.key === entryKey) {
    updateCurrentTask(entryKey, { docStatus: 'cancelled' })
    advanceQueue()
  }
  await load()
  startPolling()
}

async function reindex(row: DocumentItem) {
  try {
    await docApi.reindex(kbId, row.id)
  } catch {
    ElMessage.error(t('documents.toast.reindexFailed'))
    return
  }
  ElMessage.success(t('documents.toast.reindexQueued'))
  await load()
  startPolling()
}

async function remove(row: DocumentItem) {
  try {
    await ElMessageBox.confirm(
      t('documents.confirm.remove', { name: row.filename }),
      t('common.tip'),
      {
        type: 'warning',
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel')
      }
    )
  } catch {
    return
  }
  try {
    await docApi.remove(kbId, row.id)
  } catch {
    ElMessage.error(t('documents.toast.removeFailed'))
    return
  }
  ElMessage.success(t('common.deleted'))
  await load()
}

onMounted(async () => {
  await load()
  // 恢复"未完成上传"提示：只恢复当前会话，等待队列里的 File 不跨刷新
  void restoreResume()
})
onBeforeUnmount(() => {
  disposed = true
  stopPolling()
  if (searchTimer) window.clearTimeout(searchTimer)
  // 离开页面：在途上传转为暂停并保留会话，回来后（或刷新后）可续传
  if (queueState.value.current?.phase === 'uploading') pauseCurrentUpload(true)
})
</script>

<style scoped>
.title-group {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  min-width: 0;
}

.title-copy {
  min-width: 0;
}

.title-copy h2 {
  overflow-wrap: anywhere;
}

.back-btn {
  flex-shrink: 0;
  margin-top: 23px;
}

.docs-head {
  gap: 20px;
}

/* ---------- 支持格式条 ---------- */
.format-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px 20px;
  padding: 13px 18px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
}

.format-tags {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.format-label {
  margin-right: 4px;
  color: var(--text-3);
  font-size: 12px;
}

.format-chip {
  padding: 2px 9px;
  border: 1px solid var(--accent-border);
  border-radius: 6px;
  background: var(--brand-tint);
  color: var(--brand-dark);
  font-size: 11.5px;
  font-weight: 500;
}

.format-tip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-3);
  font-size: 12px;
}

.resume-alert {
  margin: 0;
}

.resume-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 16px;
  width: 100%;
}

/* ---------- 上传进度卡 ---------- */
.upload-card {
  padding: 17px 20px 15px;
  border: 1px solid var(--success-border);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}

.upload-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px 16px;
  margin-bottom: 14px;
}

.upload-file {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}

.upload-icon {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: var(--brand-soft);
  color: var(--brand);
}

.upload-icon.done {
  background: var(--success-soft);
  color: var(--success);
}

.upload-icon.failed {
  background: var(--danger-soft);
  color: var(--danger);
}

.upload-icon.paused {
  background: var(--warning-soft);
  color: var(--warning);
}

.upload-icon.ingesting :deep(.el-icon) {
  animation: spin 1.4s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* 减少动态效果时停转：旋转的圈会定格成一个歪斜的图标，看不出在转反而更奇怪。
   改为不转的静态图标，进行中的状态由下方的「排队中」/「处理中」文案承担。 */
@media (prefers-reduced-motion: reduce) {
  .upload-icon.ingesting :deep(.el-icon) {
    animation: none !important;
  }
}

.upload-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.upload-name-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.upload-name {
  overflow: hidden;
  color: var(--text);
  font-size: 13.5px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.upload-size {
  color: var(--text-3);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.upload-actions {
  display: flex;
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: 8px;
}

.upload-progress {
  margin-bottom: 2px;
}

.upload-progress :deep(.el-progress-bar__outer) {
  background: var(--line);
}

.upload-progress :deep(.el-progress__text) {
  min-width: 56px;
  color: var(--text-3);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.upload-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  color: var(--text-3);
  font-size: 12px;
}

.upload-chunks {
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}

/* ---------- 等待队列 ---------- */
.queue-card {
  padding: 12px 16px;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
}

.queue-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-bottom: 6px;
}

.queue-title {
  color: var(--text-2);
  font-size: 12.5px;
  font-weight: 600;
}

.queue-note {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--text-3);
  font-size: 11.5px;
}

.queue-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
  border-top: 1px solid var(--line);
}

.queue-name {
  overflow: hidden;
  min-width: 0;
  color: var(--text);
  font-size: 12.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.queue-size {
  margin-left: auto;
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* ---------- 表格 ---------- */
.panel-tools {
  display: flex;
  align-items: center;
  gap: 8px;
}

.doc-search {
  width: 220px;
}

.status-filter {
  width: 142px;
}

.file-cell {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.file-ext {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 38px;
  height: 24px;
  border: 1px solid transparent;
  border-radius: 6px;
  font-size: 9.5px;
  font-weight: 700;
  letter-spacing: 0.03em;
}

.file-ext.pdf {
  border-color: var(--danger-border);
  background: var(--danger-soft);
  color: var(--danger);
}

.file-ext.doc {
  border-color: var(--line);
  background: var(--slate-soft);
  color: var(--slate-text);
}

.file-ext.xls,
.file-ext.csv {
  border-color: var(--success-border);
  background: var(--success-soft);
  color: var(--success);
}

.file-ext.md {
  border-color: var(--warning-border);
  background: var(--warning-soft);
  color: var(--warning);
}

.file-ext.txt {
  border-color: var(--line);
  background: var(--surface-soft);
  color: var(--text-3);
}

.file-name {
  overflow: hidden;
  color: var(--text);
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.num {
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
}

.time {
  color: var(--text-3);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.ready-mark {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--success);
  font-size: 12.5px;
}

.row-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
}

.row-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

@media (max-width: 900px) {
  .back-btn {
    margin-top: 21px;
  }

  .format-bar {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .panel-head {
    align-items: stretch;
  }

  .panel-tools {
    width: 100%;
  }

  .doc-search {
    flex: 1;
    width: auto;
  }
}

@media (max-width: 640px) {
  .docs-head {
    flex-wrap: wrap;
  }

  .title-group {
    gap: 10px;
  }

  .back-btn {
    width: 32px;
    height: 32px;
    margin-top: 21px;
  }

  .doc-search,
  .status-filter {
    width: 100%;
  }

  .upload-card {
    padding: 15px 16px 14px;
  }

  .upload-head {
    align-items: flex-start;
  }

  .upload-actions {
    width: 100%;
    justify-content: flex-end;
  }

  .upload-foot {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }
}
</style>
