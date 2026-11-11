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
        <el-button v-if="canManageDocuments" type="primary" @click="pickFiles">
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

    <!-- 维护能力完全由服务端判定；未知/失效时收敛为只读，不沿用旧库权限。 -->
    <el-alert
      v-if="capabilityNotice !== 'none'"
      type="warning"
      :title="capabilityMessage"
      :closable="false"
      show-icon
      class="capability-alert"
      data-testid="documents-capability-error"
    />
    <el-alert
      v-else-if="capabilityState.status === 'known' && !canManageDocuments"
      type="info"
      :title="t('documents.capability.readOnly')"
      :closable="false"
      show-icon
      class="capability-alert"
      data-testid="documents-read-only"
    />

    <!-- 未完成的上传：刷新/关闭页面后可从这里继续（只恢复当前会话） -->
    <el-alert
      v-if="canManageDocuments && resumeInfo && !uploadTask"
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
            :disabled="!canManageDocuments"
            @click="pauseCurrentUpload()"
          >
            {{ t('documents.pause') }}
          </el-button>
          <el-button
            v-if="uploadTask.phase === 'paused'"
            size="small"
            type="primary"
            :disabled="!canManageDocuments"
            @click="resumeCurrentUpload"
          >
            {{ t('documents.resumeAction') }}
          </el-button>
          <el-button
            v-if="uploadTask.phase === 'failed' && !canReindexExisting"
            size="small"
            type="primary"
            :disabled="!canManageDocuments"
            @click="retryFailed"
          >
            {{ t('documents.retry') }}
          </el-button>
          <el-button
            v-if="uploadTask.phase === 'failed' && canReindexExisting"
            size="small"
            type="primary"
            :disabled="!canManageDocuments"
            @click="reindexExistingCurrent"
          >
            {{ t('documents.rebuild') }}
          </el-button>
          <el-button
            v-if="uploadTask.phase === 'failed'"
            size="small"
            :disabled="!canManageDocuments"
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
            :disabled="!canManageDocuments"
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
        <el-button link type="danger" size="small" :disabled="!canManageDocuments" @click="removeWaitingUpload(item.key)">
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
        <el-table-column :label="t('documents.table.filename')" min-width="220">
          <template #default="{ row }">
            <div class="file-cell">
              <span class="file-ext" :class="extClass(row.file_ext)">{{ extLabel(row.file_ext) }}</span>
              <span class="file-name" :title="row.filename">{{ row.filename }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('documents.table.size')" width="80">
          <template #default="{ row }">
            <span class="num">{{ formatSize(row.file_size) }}</span>
          </template>
        </el-table-column>

        <!-- 状态与进度合并：就绪/失败/取消显示标签，排队与处理中直接显示进度条 -->
        <el-table-column :label="t('documents.table.status')" width="160">
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

        <el-table-column prop="chunk_count" :label="t('documents.table.chunks')" width="70" align="center">
          <template #default="{ row }">
            <span class="num">{{ formatNumber(row.chunk_count) }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('documents.table.chars')" width="90" align="right">
          <template #default="{ row }">
            <span class="num">{{ formatNumber(row.char_count) }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('documents.table.lifecycle')" width="110">
          <template #default="{ row }">
            <el-tooltip
              :content="lifecycleTooltip(row)"
              placement="top"
              :disabled="!lifecycleTooltip(row)"
            >
              <el-tag :type="lifecycleType(row.lifecycle_state)" size="small" effect="light">
                {{ lifecycleText(row.lifecycle_state) }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column :label="t('documents.table.maintainer')" min-width="100">
          <template #default="{ row }">
            <span v-if="row.maintainer" class="maintainer-text" :title="row.maintainer">
              {{ row.maintainer }}
            </span>
            <span v-else class="time">—</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('documents.table.validity')" width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.valid_from || row.valid_until" class="time">
              {{ formatValidityWindow(row) }}
            </span>
            <span v-else class="time">—</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('documents.table.uploadedAt')" width="170">
          <template #default="{ row }">
            <span class="time">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column
          v-if="canManageDocuments"
          :label="t('documents.table.actions')"
          width="290"
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
              <el-button link type="primary" size="small" @click="openQuality(row)">
                {{ t('documents.quality.button') }}
              </el-button>
              <el-button link type="primary" size="small" @click="openGovernance(row)">
                {{ t('documents.governance.edit') }}
              </el-button>
              <el-button
                v-if="row.status === 'ready'"
                link
                type="primary"
                size="small"
                @click="openComparison(row)"
              >
                {{ t('documents.comparison.action') }}
              </el-button>
              <el-button link type="danger" size="small" @click="remove(row)">
                {{ t('common.delete') }}
              </el-button>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty :description="emptyDescription" :image-size="80" />
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

    <DocumentGovernanceDialog
      v-model="governanceOpen"
      :kb-id="kbId"
      :doc="governanceDoc"
      @saved="onGovernanceSaved"
      @refreshed="onGovernanceRefreshed"
      @supersede-request="openComparison"
    />
    <DocumentComparisonDialog
      v-model="comparisonOpen"
      :kb-id="kbId"
      :doc="comparisonDoc"
      @superseded="onComparisonSuperseded"
    />
    <DocumentQualityDrawer v-model="qualityOpen" :kb-id="kbId" :doc="qualityDoc" />
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
import DocumentGovernanceDialog from '@/components/DocumentGovernanceDialog.vue'
import DocumentComparisonDialog from '@/components/DocumentComparisonDialog.vue'
import DocumentQualityDrawer from '@/components/DocumentQualityDrawer.vue'
import { hashFile, isIngestQueued } from '@/utils/upload'
import {
  capabilitiesFromKb,
  completionReceiptStillApplies,
  docMissingAction,
  revokesCapability,
  type KbCapabilityState
} from '@/utils/kbCapabilities'
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
  belongsToGovernanceSession,
  lifecycleTagType,
  normalizeLifecycleState,
  replaceGovernanceRow,
  type GovernanceRequestIdentity
} from '@/utils/documentGovernance'
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
const { t } = useI18n()

const kbId = route.params.kbId as string
const kb = ref<KnowledgeBase | null>(null)
// 服务端能力：只有明确 known && documents 才允许维护；unknown/失败一律 fail-closed。
const capabilityState = ref<KbCapabilityState>({ status: 'unknown' })
const capabilityNotice = ref<'none' | 'unavailable' | 'revoked'>('none')
const canManageDocuments = computed(
  () => capabilityState.value.status === 'known' && capabilityState.value.documents
)
const capabilityMessage = computed(() =>
  capabilityNotice.value === 'revoked'
    ? t('documents.capability.revoked')
    : t('documents.capability.unavailable')
)
/**
 * 空表说明必须跟随当前能力，不能把"请求失败/无权限"说成空库：
 * 只有明确可维护时才提示点击上传；只读给只读空态；未知/失效说明无法确认或已失效。
 * 复用已有的能力文案，避免为几个字符串扩大入口包体。
 */
const emptyDescription = computed(() => {
  if (capabilityState.value.status === 'unknown') {
    return capabilityNotice.value === 'revoked'
      ? t('documents.capability.revoked')
      : t('documents.capability.unavailable')
  }
  if (!canManageDocuments.value) return t('documents.capability.readOnly')
  return t('documents.empty', { upload: t('documents.upload') })
})
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
// 能力暂时未知时暂存一次 complete 回执（至多一条），仅在尚可维护且条目/代次未变时复核应用。
let pendingCompletion: {
  key: string
  generation: number
  uploadId: string
  completed: UploadCompleteResult
} | null = null
// 本页实例是否曾看到过可维护能力：只用于区分"初始只读"与"真正被撤销"的提示，
// 不参与是否停止维护的判定。
let sawManageCapability = false

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
  return (
    canManageDocuments.value &&
    (phase === 'uploading' || phase === 'paused' || phase === 'ingesting')
  )
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

// ---------- 治理（admin 编辑 / 替代 / 重新启用） ----------

const governanceOpen = ref(false)
const governanceDoc = ref<DocumentItem | null>(null)
const qualityOpen = ref(false)
const qualityDoc = ref<DocumentItem | null>(null)
const comparisonOpen = ref(false)
const comparisonDoc = ref<DocumentItem | null>(null)
// 治理写请求代次：切库、关窗、卸载都会 +1，迟到的响应不再落地
let governanceEpoch = 0
// 最近一次打开的请求身份：只有它发出的 saved/refreshed 才能改行、关窗
let governanceRequest: GovernanceRequestIdentity | null = null

function lifecycleText(state: string) {
  return t(`documents.lifecycle.${normalizeLifecycleState(state)}`)
}

function lifecycleType(state: string) {
  return lifecycleTagType(normalizeLifecycleState(state))
}

/** 生命周期标签的悬浮说明：说明谁在什么时候不再可检索，不含任何权限语义。 */
function lifecycleTooltip(row: DocumentItem) {  const parts: string[] = []
  if (row.superseded_at) parts.push(t('documents.governance.tip.superseded'))
  else if (!row.enabled) parts.push(t('documents.governance.tip.disabled'))
  else if (row.lifecycle_state === 'scheduled') parts.push(t('documents.governance.tip.scheduled'))
  else if (row.lifecycle_state === 'expired') parts.push(t('documents.governance.tip.expired'))
  if (row.status !== 'ready') parts.push(t('documents.governance.tip.notReady'))
  return parts.join('；') || ''
}

function formatValidityWindow(row: DocumentItem) {
  if (row.valid_from && row.valid_until) {
    return `${formatDateTime(row.valid_from)} → ${formatDateTime(row.valid_until)}`
  }
  if (row.valid_from) return `${formatDateTime(row.valid_from)} →`
  return `→ ${formatDateTime(row.valid_until as string)}`
}

/** 管理员行操作：打开治理表单（快照在弹窗内持有 expected_revision）。 */
function openGovernance(row: DocumentItem) {
  if (!ensureCanManage()) return
  governanceEpoch += 1
  governanceRequest = { kbId, docId: row.id, epoch: governanceEpoch }
  governanceDoc.value = row
  governanceOpen.value = true
}

/** 管理员行操作/治理弹窗入口：打开同一套“版本对比”弹窗。 */
function openComparison(row: DocumentItem) {
  if (!ensureCanManage()) return
  comparisonDoc.value = row
  comparisonOpen.value = true
}

/** 管理员行操作：打开该文档的入库质量抽屉（组件内部自行加载并处理竞态）。 */
function openQuality(row: DocumentItem) {
  if (!ensureCanManage()) return
  qualityDoc.value = row
  qualityOpen.value = true
}

/** 响应是否仍属于当前打开会话（KB/文档/代次/开窗状态全部一致）。 */
function isCurrentGovernance(response: DocumentItem): boolean {
  return belongsToGovernanceSession(governanceRequest, {
    kbId,
    epoch: governanceEpoch,
    open: governanceOpen.value,
    disposed,
    docId: governanceDoc.value?.id ?? null
  }, response)
}

/** 治理写回成功：只接受"这次弹窗发出"的响应，绝不覆盖别的行。 */
function onGovernanceSaved(updated: DocumentItem) {
  if (!canManageDocuments.value) return
  if (!isCurrentGovernance(updated)) return
  governanceOpen.value = false
  governanceRequest = null
  replaceGovernanceRow(docs.value, updated)
  void load()
}

/** 409 后弹窗拉到了新鲜详情：更新行、保持开窗，由用户重审后再提交。 */
function onGovernanceRefreshed(updated: DocumentItem) {
  if (!isCurrentGovernance(updated)) return
  // 同步 doc 属性，让替代标记/启用状态与最新详情一致（不会重开表单）
  governanceDoc.value = updated
  replaceGovernanceRow(docs.value, updated)
}

/** 对比弹窗完成替代：只替换同 id 行；同一文档的治理弹窗同时关闭。 */
function onComparisonSuperseded(updated: DocumentItem) {
  replaceGovernanceRow(docs.value, updated)
  if (governanceOpen.value && governanceDoc.value?.id === updated.id) {
    governanceOpen.value = false
  }
  ElMessage.success(t('documents.comparison.toast.superseded'))
  void load()
}

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

// ---------- 服务端维护能力 ----------

/**
 * 能力只随 KB 列表响应更新；正常刷新开始时绝不清空旧能力，
 * 否则进行中的上传/轮询会被一次短暂加载卡死。
 */
function applyKbCapability(nextKb: KnowledgeBase | null) {
  const wasUnknown = capabilityState.value.status === 'unknown'
  const next = capabilitiesFromKb(nextKb)
  capabilityState.value = next
  if (next.status === 'unknown') {
    capabilityNotice.value = 'unavailable'
    // 临时加载失败：不沿用旧权限发放新的写操作，但已获授权的在途上传继续由服务端裁决；
    // 关闭仍持有旧能力快照的维护弹窗，队列仍保留（startNext 已按能力门禁停止推进）。
    closeMaintenanceDialogs()
    return
  }
  if (!next.documents) {
    // 任何新鲜 known=false 都必须停止维护，不能拿先前的 canManage 快照当条件：
    // true -> unknown -> known false（实际已降为 reader）时旧快照已不可信。
    // 初始 reader 的空队列在这里没有任何副作用，只是提示保持"只读"。
    pendingCompletion = null
    stopMaintenance()
    capabilityNotice.value = sawManageCapability ? 'revoked' : 'none'
    return
  }
  sawManageCapability = true
  capabilityNotice.value = 'none'
  if (wasUnknown) resumeAfterCapabilityRecovery()
}

/**
 * 能力从"未知"恢复：先复核暂存的 complete 回执，否则唤醒被能力门禁暂停的合法队列。
 * 明确撤权的路径不会走到这里，也不会重放撤权前的回执。
 */
function resumeAfterCapabilityRecovery() {
  const receipt = pendingCompletion
  pendingCompletion = null
  const current = currentEntry.value
  if (receipt && completionReceiptStillApplies(
    { key: receipt.key, generation: receipt.generation },
    current ? { key: current.key, phase: current.phase } : null,
    uploadGeneration,
    canManageDocuments.value
  )) {
    clearResume(receipt.uploadId)
    handleCompleted(receipt.key, receipt.completed)
    // 无论服务端文档是否已就绪，都复核一次列表；就绪时 handleCompleted 已收口。
    void load()
    if (queueState.value.current?.phase === 'ingesting') startPolling()
    return
  }
  if (current && current.phase === 'uploading' && !activeKey) startNext()
}

/** 请求已明确返回 401/403/404：收起能力，停止队列与上传，不当作普通网络故障重试。 */
function markCapabilityFailure(error: unknown) {
  capabilityState.value = { status: 'unknown' }
  capabilityNotice.value = revokesCapability(error) ? 'revoked' : 'unavailable'
  // 权限类失败后不再展示旧文档行，也不保留可能属于撤权前后的完成回执。
  docs.value = []
  total.value = 0
  if (revokesCapability(error)) pendingCompletion = null
  stopMaintenance()
}

/**
 * 已确认不可维护（known 且 documents=false）或仍未知：收回列表、revision 与暂存回执并停止队列。
 * 已知拒绝显示"已失效"，未知显示"无法确认"，两者都不允许继续使用旧能力。
 */
function convergeUnavailableCapability(knownDenied: boolean) {
  const keepRevoked = !knownDenied && capabilityNotice.value === 'revoked'
  capabilityState.value = { status: 'unknown' }
  capabilityNotice.value = keepRevoked || knownDenied ? 'revoked' : 'unavailable'
  docs.value = []
  total.value = 0
  pendingCompletion = null
  stopMaintenance()
}

function stopMaintenance() {
  stopPolling()
  const entry = currentEntry.value
  if (entry) {
    if (entry.phase === 'uploading') {
      uploadAbort.value?.abort()
      uploadAbort.value = null
      queueState.value = patchCurrent(queueState.value, { phase: 'paused' })
    }
    // 清空等待队列：能力撤销后不得继续自动推进；当前条目保留为可见状态。
    queueState.value = { current: queueState.value.current, queue: [] }
  } else if (queueState.value.queue.length) {
    queueState.value = { current: null, queue: [] }
  }
  closeMaintenanceDialogs()
}

/** 维护类弹窗持有旧能力快照，能力变化后立即关闭；迟到回执由各自代次守卫丢弃。 */
function closeMaintenanceDialogs() {
  governanceEpoch += 1
  governanceRequest = null
  governanceOpen.value = false
  qualityOpen.value = false
  comparisonOpen.value = false
  comparisonDoc.value = null
}

/** 权限类失败后重新读取当前 KB 能力；查询失败返回未知，绝不推断为仍可维护。 */
async function recheckCapability(): Promise<KbCapabilityState> {
  try {
    const { data } = await kbApi.list()
    const found = data.find((k) => k.id === kbId) || null
    return capabilitiesFromKb(found)
  } catch (err: unknown) {
    if (revokesCapability(err)) {
      capabilityState.value = { status: 'unknown' }
      capabilityNotice.value = 'revoked'
      // 明确撤权：暂存的完成回执不能再在日后恢复时重放。
      pendingCompletion = null
      stopPolling()
    }
    return { status: 'unknown' }
  }
}

/**
 * 能力未知时暂存过完成回执：重新核对一次能力。
 * 恢复且仍可维护则按原条目/代次应用回执；已确认不可维护则收敛；仍未知则保留回执等下次加载。
 */
async function tryRecoverPendingCompletion() {
  if (disposed || !pendingCompletion || capabilityState.value.status !== 'unknown') return
  const fresh = await recheckCapability()
  if (disposed || !pendingCompletion || capabilityState.value.status !== 'unknown') return
  if (fresh.status === 'unknown') return
  if (!fresh.documents) {
    convergeUnavailableCapability(true)
    return
  }
  capabilityState.value = fresh
  sawManageCapability = true
  capabilityNotice.value = 'none'
  resumeAfterCapabilityRecovery()
}

/** 用户主动动作前的能力门禁；未知/只读/已撤销都不发写请求。 */
function ensureCanManage(): boolean {
  if (canManageDocuments.value) return true
  const key =
    capabilityNotice.value === 'revoked'
      ? 'documents.capability.revoked'
      : capabilityState.value.status === 'known'
        ? 'documents.capability.readOnly'
        : 'documents.capability.unknown'
  ElMessage.warning(t(key))
  return false
}

function handleActionError(error: unknown): boolean {
  if (isCancel(error)) return true
  if (revokesCapability(error)) {
    markCapabilityFailure(error)
    return true
  }
  return false
}

// ---------- 列表加载 / 搜索 / 轮询 ----------

async function load() {
  if (disposed) return
  const snapshot = query.value
  const sequence = ++listEpoch.value
  loadEpoch.value = sequence
  loading.value = true
  try {
    // KB 元数据与文档列表分别结算：文档列表的网络抖动不能把已确认的维护能力清空，
    // 而 KB 元数据失败/缺字段则必须收敛为未知（fail-closed）。
    const [kbResult, docResult] = await Promise.allSettled([
      kbApi.list(),
      docApi.list(kbId, queryParams(snapshot))
    ])
    if (!isLatestResponse(sequence, listEpoch.value, snapshot, query.value, disposed)) return

    if (kbResult.status === 'fulfilled') {
      const found = kbResult.value.data.find((k) => k.id === kbId) || null
      kb.value = found
      applyKbCapability(found)
      // 项目索引库没有普通文档：直链进来时改去项目页，绝不停留在上传/重建索引界面。
      if (isProjectKb(found)) {
        const sourceId = found?.project_source_id
        void router.replace(sourceId ? `/projects/${sourceId}` : '/projects')
        return
      }
    } else if (!isCancel(kbResult.reason)) {
      if (revokesCapability(kbResult.reason)) {
        markCapabilityFailure(kbResult.reason)
        // 同一批的文档响应可能早于权限拒绝返回：不能再用它填列表或启动轮询。
        return
      }
      // 能力加载失败：不沿用旧权限，但保留文档只读列表可看性。
      applyKbCapability(null)
    }

    if (docResult.status === 'fulfilled') {
      // KB 读取已明确拒绝时上面已经 return；这里只处理能力仍可读取的情况。
      docs.value = docResult.value.data.items
      total.value = docResult.value.data.total
      if (shouldPoll(docs.value.map((d) => d.status), currentEntry.value?.phase === 'ingesting')) {
        startPolling()
      } else {
        stopPolling()
      }
    } else if (!isCancel(docResult.reason)) {
      if (revokesCapability(docResult.reason)) {
        markCapabilityFailure(docResult.reason)
      } else {
        ElMessage.error(t('documents.toast.listFailed'))
      }
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

  let data
  try {
    ({ data } = await docApi.list(kbId, queryParams(snapshot)))
  } catch (err: any) {
    if (isLatestResponse(epoch, listEpoch.value, snapshot, query.value, disposed) && !isCancel(err)) {
      if (revokesCapability(err)) markCapabilityFailure(err)
    }
    // 普通网络抖动下一拍重试；权限类错误已在上面停止轮询与队列。
    pollInFlight = false
    return
  }

  try {
    if (!isLatestResponse(epoch, listEpoch.value, snapshot, query.value, disposed)) return
    // 已确认撤销：在途轮询的旧响应不能再把撤权前的列表填回来。
    if (capabilityNotice.value === 'revoked') {
      stopPolling()
      return
    }
    docs.value = data.items
    total.value = data.total
    const entry = currentEntry.value
    if (entry && entry.key === pollKey && entry.phase === 'ingesting' && entry.task.docId) {
      try {
        const { data: active } = await docApi.get(kbId, entry.task.docId)
        if (!disposed && queueState.value.current?.key === pollKey) syncUploadEntry(active)
      } catch (err: any) {
        const stale = isStalePoll(pollKey, queueState.value.current?.key, epoch, listEpoch.value, disposed)
        if (err?.response?.status === 404) {
          if (stale) return
          // 单文档 404 可能是"文档已被删除"，也可能是库级拒绝：先取新鲜 KB 能力复核。
          const fresh = await recheckCapability()
          if (disposed || isStalePoll(pollKey, queueState.value.current?.key, epoch, listEpoch.value, disposed)) return
          if (docMissingAction(fresh) === 'advance') {
            capabilityState.value = fresh
            sawManageCapability = true
            capabilityNotice.value = 'none'
            stopPolling()
            ElMessage.warning(t('documents.toast.documentGone'))
            advanceQueue()
            await load()
          } else {
            // 权限未知/已撤销：收敛停止，不能凭旧快照把 404 当删除推进。
            convergeUnavailableCapability(fresh.status === 'known')
          }
        } else if (!stale && revokesCapability(err)) {
          markCapabilityFailure(err)
        }
        return
      }
    }
    if (!shouldPoll(data.items.map((d) => d.status), queueState.value.current?.phase === 'ingesting')) {
      stopPolling()
    }
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
  if (disposed || activeKey || !canManageDocuments.value) return
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
  if (!quiet && !ensureCanManage()) return
  const entry = queueState.value.current
  if (!entry || entry.phase !== 'uploading' || entry.task.completing) return
  queueState.value = pauseCurrent(queueState.value)
  uploadAbort.value?.abort()
  if (!quiet) ElMessage.info(t('documents.toast.paused'))
  void refreshSessionProgress(entry)
}

function resumeCurrentUpload() {
  if (!ensureCanManage()) return
  const entry = queueState.value.current
  if (!entry || entry.phase !== 'paused') return
  queueState.value = patchCurrent(queueState.value, { phase: 'uploading' })
  startNext()
}

async function refreshSessionProgress(entry: UploadEntry) {
  if (!entry.task.uploadId || !canManageDocuments.value) return
  // 在途分片被掐断，服务端只保留完整落盘的那些：以会话为准校准进度
  await sleep(1200)
  if (disposed || !canManageDocuments.value) return
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
  } catch (err: any) {
    // 会话过期/网络抖动保留本地估算；401/403 说明当前维护权限已不可用。
    if (err?.response?.status === 401 || err?.response?.status === 403) {
      markCapabilityFailure(err)
    }
  }
}

function retryFailed() {
  if (!ensureCanManage()) return
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
  if (!ensureCanManage()) return
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
  } catch (err: unknown) {
    if (handleActionError(err)) return
    // 失败提示同样只对原条目有效，且卸载后静默
    if (!disposed && stillOurs()) ElMessage.error(t('documents.toast.reindexFailed'))
    return
  }
  // 迟到成功回执：不能把下一个上传条目强置为 ingesting
  if (!stillOurs() || !canManageDocuments.value) return
  updateCurrentTask(entry.key, { docStatus: 'pending', percent: 0 })
  setCurrentPhase('ingesting')
  ElMessage.success(t('documents.toast.reindexQueued'))
  await load()
  startPolling()
}

function skipCurrentUpload() {
  if (!ensureCanManage()) return
  queueState.value = skipCurrent(queueState.value)
  startNext()
}

function skipFailed() {
  skipCurrentUpload()
}

function removeWaitingUpload(key: string) {
  if (!ensureCanManage()) return
  queueState.value = removeWaiting(queueState.value, key)
}

async function reindexExistingCurrent() {
  if (!ensureCanManage()) return
  const entry = queueState.value.current
  if (!entry || !entry.task.docId) return
  await reindexExisting(entry)
}

async function cancelCurrentUpload() {
  if (!ensureCanManage()) return
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
    void uploadApi.abort(kbId, entry.task.uploadId).catch((err: any) => {
      // 清理请求的 404 可能只是会话已过期；只有 401/403 才说明维护权限失效。
      if (err?.response?.status === 401 || err?.response?.status === 403) {
        markCapabilityFailure(err)
      }
    })
  }
}

async function cancelAllUploads() {
  if (!ensureCanManage()) return
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
      } catch (err: any) {
        if (err?.response?.status === 401 || err?.response?.status === 403) {
          markCapabilityFailure(err)
        }
      }
    } else if (entry.task.uploadId) {
      try {
        await uploadApi.abort(kbId, entry.task.uploadId)
      } catch (err: any) {
        if (err?.response?.status === 401 || err?.response?.status === 403) {
          markCapabilityFailure(err)
        }
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
  if (!ensureCanManage()) return
  fileInput.value?.click()
}

function onFilesPicked(event: Event) {
  if (!ensureCanManage()) return
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
  if (!canManageDocuments.value) return
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
    if (!canManageDocuments.value) {
      // 能力暂时未知（例如一次 KB 元数据加载失败）：暂存一条有界回执，
      // 能力恢复后仅在条目与上传代次都未变时复核应用；明确撤权则直接丢弃。
      if (capabilityState.value.status === 'unknown') {
        pendingCompletion = {
          key: entry.key,
          generation,
          uploadId: session.upload_id,
          completed
        }
        // 有界自愈：最多为这条回执重新核一次能力，避免只能等用户手动刷新。
        void tryRecoverPendingCompletion()
      }
      return
    }
    pendingCompletion = null
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
    // 权限拒绝不是可重试的网络故障：收敛能力并停止队列/上传。
    if (revokesCapability(err)) {
      markCapabilityFailure(err)
      return
    }
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
        // 401/403/404 不是网络抖动：立即中止整组，不把权限拒绝重试三次。
        if (revokesCapability(err)) throw err
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
  // 能力未知或不足时绝不自动恢复上传（恢复可能触发服务端会话写入）。
  if (!canManageDocuments.value) return
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
    if (!canManageDocuments.value) return
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
    if (revokesCapability(err)) markCapabilityFailure(err)
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
  if (!ensureCanManage()) return
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
  if (!ensureCanManage()) return
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
  // 确认框打开期间可能已撤权：此时不再发送取消请求。
  if (!ensureCanManage()) return
  try {
    await docApi.cancel(kbId, docId)
  } catch (err: unknown) {
    if (handleActionError(err)) return
    ElMessage.error(t('documents.toast.cancelFailed'))
    return
  }
  if (!canManageDocuments.value) return
  ElMessage.success(t('documents.toast.cancelSubmitted'))
  if (entryKey && queueState.value.current?.key === entryKey) {
    updateCurrentTask(entryKey, { docStatus: 'cancelled' })
    advanceQueue()
  }
  await load()
  startPolling()
}

async function reindex(row: DocumentItem) {
  if (!ensureCanManage()) return
  try {
    await docApi.reindex(kbId, row.id)
  } catch (err: unknown) {
    if (handleActionError(err)) return
    ElMessage.error(t('documents.toast.reindexFailed'))
    return
  }
  if (!canManageDocuments.value) return
  ElMessage.success(t('documents.toast.reindexQueued'))
  await load()
  startPolling()
}

async function remove(row: DocumentItem) {
  if (!ensureCanManage()) return
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
  // 确认框打开期间能力可能已被撤销：此时不再发送删除请求。
  if (!ensureCanManage()) return
  try {
    await docApi.remove(kbId, row.id)
  } catch (err: unknown) {
    if (handleActionError(err)) return
    ElMessage.error(t('documents.toast.removeFailed'))
    return
  }
  if (!canManageDocuments.value) return
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
  // 卸载后任何治理/对比响应都不能再关窗、改行或提示
  governanceEpoch += 1
  governanceRequest = null
  governanceOpen.value = false
  comparisonOpen.value = false
  comparisonDoc.value = null
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
