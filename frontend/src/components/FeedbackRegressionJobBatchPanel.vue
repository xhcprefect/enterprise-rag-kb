<template>
  <section v-if="isVisible" class="batch-panel" data-testid="regression-batch">
    <div class="batch-head">
      <div>
        <h4>{{ fj('batch.title') }}</h4>
        <p class="hint">
          {{ fj('batch.selected', { count: formatNumber(selected.length) }) }}
          · {{ fj('batch.limit', { max: formatNumber(MAX_BATCH_ITEMS) }) }}
        </p>
      </div>
      <div class="batch-head-actions">
        <el-button
          size="small"
          plain
          :loading="intakeLoading"
          :disabled="!selected.length || submitting"
          data-testid="regression-batch-refresh"
          @click="refreshIntake"
        >
          <el-icon><Refresh /></el-icon>{{ fj('refresh') }}
        </el-button>
        <el-button
          size="small"
          :disabled="!selected.length || submitting"
          data-testid="regression-batch-clear"
          @click="clearSelection"
        >
          {{ fj('batch.clear') }}
        </el-button>
      </div>
    </div>

    <template v-if="selected.length">
      <section class="intake" data-testid="regression-batch-intake">
        <div class="section-head">
          <h5>{{ fj('batch.intakeTitle') }}</h5>
          <span class="hint">{{ fj('batch.intakeHint') }}</span>
        </div>

        <div v-if="intakeLoading" class="hint" role="status">{{ fj('loading') }}</div>
        <el-alert
          v-if="intakeError"
          :title="intakeError"
          type="error"
          :closable="false"
          show-icon
          data-testid="regression-batch-intake-error"
        />

        <ul class="intake-list">
          <li
            v-for="row in intake"
            :key="row.feedback_id"
            class="intake-item"
            :class="{ blocked: !row.eligible }"
            :data-testid="`regression-batch-intake-${row.feedback_id}`"
          >
            <div class="intake-main">
              <span class="mono">{{ row.label }}</span>
              <span class="intake-status">
                <el-icon v-if="row.eligible" class="ok"><CircleCheck /></el-icon>
                <el-icon v-else class="blocked-icon"><WarningFilled /></el-icon>
                {{ row.eligible ? fj('batch.eligible') : row.reason }}
              </span>
            </div>
            <div class="intake-meta">
              <span>{{ fj('batch.revision') }}: {{ row.regressionRevision != null ? formatNumber(row.regressionRevision) : '—' }}</span>
              <span>{{ t(feedbackStatusKey(row.feedbackStatus)) }}</span>
            </div>
          </li>
        </ul>

        <p v-if="!intakeLoading && intake.length && !allEligible" class="blocked-hint" data-testid="regression-batch-blocked">
          {{ fj('batch.blocked') }}
        </p>

        <div class="intake-actions">
          <el-button
            type="primary"
            :loading="submitting"
            :disabled="!canSubmit"
            data-testid="regression-batch-submit"
            @click="submitBatch"
          >
            {{ submitting ? fj('batch.submitting') : fj('batch.submit') }}
          </el-button>
          <span class="hint">{{ fj('batch.jobsHint') }}</span>
        </div>
      </section>

      <el-alert
        v-if="submitError"
        :title="submitError"
        type="error"
        :closable="false"
        show-icon
        data-testid="regression-batch-error"
      />
    </template>

    <el-alert
      v-if="recoveryError"
      :title="recoveryError"
      type="error"
      :closable="false"
      show-icon
      data-testid="regression-batch-recovery-error"
    />
    <el-alert
      v-if="recoveryNotice"
      :title="recoveryNotice"
      type="success"
      :closable="false"
      show-icon
      data-testid="regression-batch-recovered"
    />

    <div v-if="unconfirmedRequestId" class="recovery" data-testid="regression-batch-unconfirmed">
      <div class="section-title">{{ fj('unconfirmedTitle') }}</div>
      <p class="hint">
        {{ fj('unconfirmedHint', { requestId: unconfirmedRequestId }) }}
      </p>
      <el-button
        size="small"
        plain
        :loading="recovering"
        data-testid="regression-batch-recover"
        @click="recoverRequest(unconfirmedRequestId)"
      >
        <el-icon><Refresh /></el-icon>{{ fj('actions.retryRead') }}
      </el-button>
    </div>
    <p v-if="recovering && !unconfirmedRequestId" class="hint" data-testid="regression-batch-recovering">
      {{ fj('recoveryReading') }}
    </p>

    <section v-if="batch" class="batch-result" data-testid="regression-batch-result">
      <div class="section-head">
        <h5>{{ fj('batch.resultTitle') }}</h5>
        <div class="result-actions">
          <el-button
            size="small"
            plain
            :loading="batchLoading"
            :disabled="batchLoading"
            data-testid="regression-batch-result-refresh"
            @click="refreshBatch"
          >
            <el-icon><Refresh /></el-icon>{{ fj('batch.refresh') }}
          </el-button>
          <el-button size="small" text data-testid="regression-batch-result-close" @click="closeBatchResult">
            {{ fj('batch.closeResult') }}
          </el-button>
        </div>
      </div>
      <p class="hint">
        {{ fj('batch.requestId') }}: <code class="mono break">{{ batch.request_id }}</code>
      </p>
      <dl class="counts" data-testid="regression-batch-counts">
        <div>
          <dt>{{ fj('batch.total') }}</dt>
          <dd>{{ formatNumber(batch.counts.total) }}</dd>
        </div>
        <div>
          <dt>{{ fj(jobStatusKey('queued')) }}</dt>
          <dd>{{ formatNumber(batch.counts.queued) }}</dd>
        </div>
        <div>
          <dt>{{ fj(jobStatusKey('running')) }}</dt>
          <dd>{{ formatNumber(batch.counts.running) }}</dd>
        </div>
        <div>
          <dt>{{ fj(jobStatusKey('succeeded')) }}</dt>
          <dd>{{ formatNumber(batch.counts.succeeded) }}</dd>
        </div>
        <div>
          <dt>{{ fj(jobStatusKey('failed')) }}</dt>
          <dd>{{ formatNumber(batch.counts.failed) }}</dd>
        </div>
        <div>
          <dt>{{ fj(jobStatusKey('cancelled')) }}</dt>
          <dd>{{ formatNumber(batch.counts.cancelled) }}</dd>
        </div>
      </dl>

      <el-alert
        v-if="batchError"
        :title="batchError"
        type="error"
        :closable="false"
        show-icon
        data-testid="regression-batch-load-error"
      />
      <el-alert
        v-if="cancelError"
        :title="cancelError"
        type="warning"
        :closable="false"
        show-icon
        data-testid="regression-batch-cancel-error"
      />

      <div class="batch-actions">
        <el-button
          v-if="hasActiveJobs"
          size="small"
          plain
          :loading="cancelling"
          :disabled="cancelling || !canCancelBatch"
          data-testid="regression-batch-cancel"
          @click="cancelBatch"
        >
          <el-icon><VideoPause /></el-icon>
          {{ cancelling ? fj('batch.cancelling') : fj('batch.cancel') }}
        </el-button>
        <span v-else class="hint">{{ fj('batch.cancelNotAllowed') }}</span>
        <el-button
          v-if="cancelReadBlocked"
          size="small"
          plain
          :loading="cancelReadRetrying"
          :disabled="cancelReadRetrying"
          data-testid="regression-batch-cancel-read"
          @click="retryCancelRead"
        >
          <el-icon><Refresh /></el-icon>{{ fj('actions.retryRead') }}
        </el-button>
      </div>

      <h5 class="jobs-title">{{ fj('batch.jobs') }}</h5>
      <p v-if="!batch.jobs.length" class="hint">{{ fj('batch.noJobs') }}</p>
      <ul v-else class="batch-jobs">
        <li
          v-for="job in batch.jobs"
          :key="job.job_id"
          class="batch-job"
          :data-testid="`regression-batch-job-${job.job_id}`"
        >
          <div class="batch-job-top">
            <span class="status-line" :class="`status-${job.status}`">
              <el-icon><component :is="statusIcon(job.status)" /></el-icon>
              <strong>{{ fj(jobStatusKey(job.status)) }}</strong>
            </span>
            <span class="mono">{{ fj('batch.feedback') }} {{ job.feedback_id.slice(0, 8) }}</span>
            <span>{{ fj('attemptLabel') }} #{{ formatNumber(job.run_attempt_no) }}</span>
            <el-tag :type="reviewTagType(job.review.status)" size="small" effect="light">
              {{ fj(reviewStatusKey(job.review.status)) }}
            </el-tag>
          </div>
          <p class="job-outcome">{{ fj(jobOutcomeKey(job)) }}</p>
          <FeedbackRegressionJobUsage :usage="job.usage" />
          <div class="batch-job-actions">
            <el-button
              v-if="isReviewableJob(job)"
              size="small"
              type="primary"
              plain
              :data-testid="`regression-batch-compare-${job.job_id}`"
              @click="openComparison(job)"
            >
              {{ fj('actions.compare') }}
            </el-button>
          </div>
        </li>
      </ul>
    </section>

    <el-dialog
      v-model="comparisonVisible"
      :title="fj('comparison.title')"
      width="min(1080px, 96vw)"
      append-to-body
      destroy-on-close
      data-testid="regression-batch-comparison-dialog"
      @closed="comparisonJob = null"
    >
      <FeedbackRegressionJobComparison
        v-if="comparisonJob"
        :job-id="comparisonJob.job_id"
        @updated="onComparisonUpdated"
        @failed="onComparisonFailed"
      />
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch, type Component } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import {
  CircleCheck,
  CircleClose,
  Clock,
  Loading,
  Refresh,
  VideoPause,
  WarningFilled
} from '@element-plus/icons-vue'
import {
  feedbackRegressionApi,
  type AnswerFeedbackAdminItem,
  type FeedbackStatus
} from '@/api'
import {
  feedbackRegressionJobsApi,
  type FeedbackRegressionBatch,
  type FeedbackRegressionJob,
  type FeedbackRegressionJobStatus
} from '@/api/feedbackRegressionJobs'
import {
  feedbackJobsHasReason,
  useFeedbackRegressionJobsI18n
} from '@/i18n/messages/feedbackRegressionJobs'
import { formatNumber } from '@/i18n/format'
import { useUserStore } from '@/stores/user'
import { feedbackStatusKey } from '@/utils/feedback'
import { unavailableReasonKey } from '@/utils/feedbackRegression'
import { createLatestGuard } from '@/utils/usage'
import {
  MAX_BATCH_ITEMS,
  buildBatchCreatePayload,
  buildCancelRequest,
  browserRecoveryStorage,
  currentOrigin,
  extractReasonCode,
  isActiveJobStatus,
  isAmbiguousWriteFailure,
  isCancelableJob,
  isReviewableJob,
  jobOutcomeKey,
  jobStatusKey,
  loadPendingRequests,
  removePendingRequest,
  reviewStatusKey,
  savePendingRequest
} from '@/utils/feedbackRegressionJobs'
import FeedbackRegressionJobComparison from '@/components/FeedbackRegressionJobComparison.vue'
import FeedbackRegressionJobUsage from '@/components/FeedbackRegressionJobUsage.vue'

const props = defineProps<{
  selected: AnswerFeedbackAdminItem[]
  resetToken: number
}>()

const emit = defineEmits<{
  (event: 'clear-selection'): void
  (event: 'busy-change', busy: boolean): void
  (event: 'batch-created'): void
}>()

const { t } = useI18n()
const { fj } = useFeedbackRegressionJobsI18n()
const userStore = useUserStore()
const storage = browserRecoveryStorage()
const origin = currentOrigin()

const BATCH_POLL_INTERVAL_MS = 3000
const BATCH_POLL_MAX_FAILURES = 3

interface IntakeRow {
  feedback_id: string
  label: string
  feedbackStatus: FeedbackStatus
  regressionRevision: number | null
  eligible: boolean
  reason: string
}

const intake = ref<IntakeRow[]>([])
const intakeLoading = ref(false)
const intakeError = ref('')
const intakeGuard = createLatestGuard()
let intakeController: AbortController | null = null

const submitting = ref(false)
const submitError = ref('')
let submitController: AbortController | null = null

const batch = ref<FeedbackRegressionBatch | null>(null)
const batchLoading = ref(false)
const batchError = ref('')
const batchGuard = createLatestGuard()
let batchController: AbortController | null = null
let batchTimer: ReturnType<typeof setTimeout> | null = null
let batchPollController: AbortController | null = null
let batchPollFailures = 0

const cancelling = ref(false)
const cancelError = ref('')
let cancelController: AbortController | null = null

const recovering = ref(false)
const unconfirmedRequestId = ref('')
const recoveryError = ref('')
const recoveryNotice = ref('')
let recoveryController: AbortController | null = null

const comparisonVisible = ref(false)
const comparisonJob = ref<FeedbackRegressionJob | null>(null)
const cancelReadBlocked = ref(false)
const cancelReadRetrying = ref(false)
let cancelReadController: AbortController | null = null
let lifecycleGeneration = 0

const adminId = computed(() => userStore.profile?.id || '')
const selectedIdsKey = computed(() => props.selected.map(item => item.id).join(','))

function invalidateLifecycle() {
  lifecycleGeneration += 1
}

/**
 * 批次上下文 = 生命周期 generation + 当前选择集合 + 当前管理员。
 * await 之后必须重新校验，避免切换选择/关闭面板后旧响应回填或重启轮询。
 */
function batchOperationIsCurrent(
  generation: number,
  selectionKey: string,
  scopedAdminId: string,
  controller: AbortController
): boolean {
  return !controller.signal.aborted
    && generation === lifecycleGeneration
    && selectionKey === selectedIdsKey.value
    && scopedAdminId === adminId.value
}
const isVisible = computed(() =>
  props.selected.length > 0
  || !!batch.value
  || recovering.value
  || !!unconfirmedRequestId.value
)
const allEligible = computed(() =>
  props.selected.length > 0
  && intake.value.length === props.selected.length
  && intake.value.every(row => row.eligible)
)
const canSubmit = computed(() =>
  props.selected.length >= 1
  && props.selected.length <= MAX_BATCH_ITEMS
  && allEligible.value
  && !intakeLoading.value
  && !submitting.value
  // 旧 request id 尚未确认时必须先只读恢复，不能用旧 revision 再建一批。
  && !unconfirmedRequestId.value
)
const hasActiveJobs = computed(() =>
  !!batch.value && batch.value.counts.queued + batch.value.counts.running > 0
)
const canCancelBatch = computed(() => hasActiveJobs.value && !cancelReadBlocked.value)

function serverMessage(err: any): string {
  const body = err?.response?.data
  if (body && typeof body === 'object' && typeof body.message === 'string' && body.message.trim()) {
    return body.message.trim()
  }
  return typeof err?.message === 'string' ? err.message.trim() : ''
}

function jobErrorMessage(err: any, fallbackKey: string): string {
  const detail = serverMessage(err)
  const code = extractReasonCode(detail)
  if (code && feedbackJobsHasReason(code)) return fj(`reason.${code}`)
  return detail || fj(fallbackKey)
}

function statusIcon(status: FeedbackRegressionJobStatus): Component {
  if (status === 'queued') return Clock
  if (status === 'running') return Loading
  if (status === 'succeeded') return CircleCheck
  return CircleClose
}

function reviewTagType(
  status: FeedbackRegressionJob['review']['status']
): 'success' | 'warning' | 'info' | 'danger' {
  if (status === 'verified') return 'success'
  if (status === 'rejected') return 'danger'
  return 'info'
}

async function refreshIntake(): Promise<IntakeRow[] | null> {
  if (!props.selected.length) {
    intake.value = []
    intakeError.value = ''
    return []
  }
  const token = intakeGuard.begin()
  intakeController?.abort()
  const controller = new AbortController()
  intakeController = controller
  const generation = lifecycleGeneration
  const selectionKey = selectedIdsKey.value
  const scopedAdminId = adminId.value
  intakeLoading.value = true
  intakeError.value = ''
  const items = [...props.selected]
  try {
    const rows = await Promise.all(items.map(async (item): Promise<IntakeRow> => {
      const label = item.id.slice(0, 8)
      try {
        const { data } = await feedbackRegressionApi.get(item.id, controller.signal)
        if (!data) {
          return {
            feedback_id: item.id,
            label,
            feedbackStatus: item.status,
            regressionRevision: null,
            eligible: false,
            reason: fj('batch.notEnrolled')
          }
        }
        const run = data.current_run
        const activeRun = run?.execution_kind === 'online'
          && !!run.job_status
          && isActiveJobStatus(run.job_status)
        if (!data.available) {
          const reason = data.unavailable_reason || 'unknown'
          return {
            feedback_id: item.id,
            label,
            feedbackStatus: item.status,
            regressionRevision: null,
            eligible: false,
            reason: t(unavailableReasonKey(data.unavailable_reason), { reason })
          }
        }
        if (activeRun && run?.job_status) {
          return {
            feedback_id: item.id,
            label,
            feedbackStatus: item.status,
            regressionRevision: data.revision,
            eligible: false,
            reason: fj('batch.active', { status: fj(jobStatusKey(run.job_status)) })
          }
        }
        return {
          feedback_id: item.id,
          label,
          feedbackStatus: item.status,
          regressionRevision: data.revision,
          eligible: true,
          reason: ''
        }
      } catch (err: any) {
        return {
          feedback_id: item.id,
          label,
          feedbackStatus: item.status,
          regressionRevision: null,
          eligible: false,
          reason: jobErrorMessage(err, 'batch.loadFailed')
        }
      }
    }))
    if (controller.signal.aborted || !intakeGuard.isCurrent(token)) return null
    // await 之后重新校验生命周期/选择/管理员；旧上下文的整批结果必须丢弃。
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return null
    intake.value = rows
    return rows
  } finally {
    if (intakeGuard.isCurrent(token) && intakeController === controller) {
      intakeLoading.value = false
      intakeController = null
    }
  }
}

function stopBatchPoll() {
  if (batchTimer) {
    clearTimeout(batchTimer)
    batchTimer = null
  }
  batchPollController?.abort()
  batchPollController = null
  batchPollFailures = 0
}

function scheduleBatchPoll() {
  if (batchTimer) clearTimeout(batchTimer)
  if (!batch.value || !hasActiveJobs.value) return
  batchTimer = setTimeout(() => {
    batchTimer = null
    void pollBatch()
  }, BATCH_POLL_INTERVAL_MS)
}

function applyBatch(next: FeedbackRegressionBatch) {
  batch.value = next
  if (next.counts.queued + next.counts.running > 0) {
    scheduleBatchPoll()
  } else {
    stopBatchPoll()
  }
}

async function pollBatch() {
  if (!batch.value || !hasActiveJobs.value) return
  const batchId = batch.value.batch_id
  const controller = new AbortController()
  const generation = lifecycleGeneration
  const selectionKey = selectedIdsKey.value
  const scopedAdminId = adminId.value
  batchPollController = controller
  try {
    const { data } = await feedbackRegressionJobsApi.getBatch(batchId, controller.signal)
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return
    if (batch.value?.batch_id !== batchId) return
    batchPollFailures = 0
    cancelReadBlocked.value = false
    applyBatch(data)
  } catch (err: any) {
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return
    if (batch.value?.batch_id !== batchId) return
    batchPollFailures += 1
    if (batchPollFailures >= BATCH_POLL_MAX_FAILURES) {
      batchError.value = jobErrorMessage(err, 'errors.loadFailed')
      stopBatchPoll()
    } else {
      scheduleBatchPoll()
    }
  } finally {
    if (batchPollController === controller) batchPollController = null
  }
}

async function refreshBatch(options: { silent?: boolean } = {}): Promise<boolean> {
  if (!batch.value) return false
  const token = batchGuard.begin()
  batchController?.abort()
  const controller = new AbortController()
  batchController = controller
  const batchId = batch.value.batch_id
  const generation = lifecycleGeneration
  const selectionKey = selectedIdsKey.value
  const scopedAdminId = adminId.value
  if (!options.silent) batchLoading.value = true
  batchError.value = ''
  try {
    const { data } = await feedbackRegressionJobsApi.getBatch(batchId, controller.signal)
    if (!batchGuard.isCurrent(token)) return false
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return false
    if (batch.value?.batch_id !== batchId) return false
    // 只有真实成功的重读才能解除旧 revision 的取消封锁。
    cancelReadBlocked.value = false
    applyBatch(data)
    return true
  } catch (err: any) {
    if (!batchGuard.isCurrent(token)) return false
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return false
    if (batch.value?.batch_id !== batchId) return false
    batchError.value = jobErrorMessage(err, 'errors.loadFailed')
    return false
  } finally {
    if (batchGuard.isCurrent(token) && batchController === controller) {
      batchLoading.value = false
      batchController = null
    }
  }
}

async function submitBatch() {
  if (!canSubmit.value || submitting.value) return
  const expectedSelection = selectedIdsKey.value
  const rows = await refreshIntake()
  if (!rows || expectedSelection !== selectedIdsKey.value) return
  if (rows.length !== props.selected.length || rows.some(row => !row.eligible)) {
    submitError.value = fj('batch.blocked')
    return
  }
  const result = buildBatchCreatePayload(rows.map(row => ({
    feedback_id: row.feedback_id,
    expected_revision: row.regressionRevision as number
  })))
  if (!result.payload) {
    submitError.value = fj(result.errorKey || 'errors.unexpected', { max: formatNumber(MAX_BATCH_ITEMS) })
    return
  }
  const requestId = result.payload.request_id
  const controller = new AbortController()
  submitController?.abort()
  submitController = controller
  const generation = lifecycleGeneration
  const selectionKey = selectedIdsKey.value
  const scopedAdminId = adminId.value
  submitting.value = true
  submitError.value = ''
  recoveryError.value = ''
  recoveryNotice.value = ''
  unconfirmedRequestId.value = ''
  savePendingRequest(storage, origin, scopedAdminId, {
    request_id: requestId,
    kind: 'batch',
    feedback_ids: rows.map(row => row.feedback_id),
    created_at: Date.now()
  })
  try {
    const { data } = await feedbackRegressionJobsApi.createBatch(result.payload, controller.signal)
    if (controller.signal.aborted) return
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) {
      // 写入可能已成功但上下文已切换：保留原 request id，只允许只读恢复。
      if (adminId.value === scopedAdminId) unconfirmedRequestId.value = requestId
      return
    }
    removePendingRequest(storage, origin, scopedAdminId, requestId)
    intake.value = []
    submitError.value = ''
    ElMessage.success(fj('batch.success'))
    applyBatch(data)
    emit('clear-selection')
    emit('batch-created')
  } catch (err: any) {
    if (controller.signal.aborted) return
    if (err?.response?.status === 409) {
      if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return
      removePendingRequest(storage, origin, scopedAdminId, requestId)
      submitError.value = fj('batch.atomicConflict')
      // 整批零写入；清空缓存 revision 并重新读取每条当前状态。
      intake.value = []
      await refreshIntake()
    } else if (isAmbiguousWriteFailure(err)) {
      if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) {
        if (adminId.value === scopedAdminId) unconfirmedRequestId.value = requestId
        return
      }
      if (err?.code === 'ERR_NETWORK' || err?.code === 'ECONNABORTED' || err?.code === 'ETIMEDOUT') {
        submitError.value = fj('errors.network')
      } else {
        submitError.value = jobErrorMessage(err, 'errors.createFailed')
      }
      await recoverRequest(requestId)
    } else {
      // 明确失败（非未知语义）才可移除原 request id。
      removePendingRequest(storage, origin, scopedAdminId, requestId)
      if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return
      submitError.value = jobErrorMessage(err, 'errors.createFailed')
      intake.value = []
      await refreshIntake()
    }
  } finally {
    if (submitController === controller) {
      submitController = null
      submitting.value = false
    }
  }
}

function pendingBatch(): string {
  const entries = loadPendingRequests(storage, origin, adminId.value)
  const found = entries.find(entry => entry.kind === 'batch')
  return found?.request_id || ''
}

async function recoverRequest(requestId: string) {
  if (!requestId || recovering.value) return
  const controller = new AbortController()
  recoveryController?.abort()
  recoveryController = controller
  const generation = lifecycleGeneration
  const selectionKey = selectedIdsKey.value
  const scopedAdminId = adminId.value
  recovering.value = true
  recoveryError.value = ''
  try {
    const { data } = await feedbackRegressionJobsApi.lookupRequest(requestId, controller.signal)
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return
    if (data.kind !== 'batch' || !data.batch) {
      unconfirmedRequestId.value = requestId
      recoveryError.value = fj('recoveryMismatch')
      return
    }
    removePendingRequest(storage, origin, scopedAdminId, requestId)
    unconfirmedRequestId.value = ''
    recoveryNotice.value = fj('recovered')
    applyBatch(data.batch)
    emit('batch-created')
  } catch (err: any) {
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return
    unconfirmedRequestId.value = requestId
    recoveryError.value = err?.response?.status === 404
      ? fj('errors.requestNotFound')
      : jobErrorMessage(err, 'recoveryFailed')
  } finally {
    if (recoveryController === controller) {
      recoveryController = null
      recovering.value = false
    }
  }
}

async function cancelBatch() {
  const current = batch.value
  if (!current || !hasActiveJobs.value || cancelling.value || cancelReadBlocked.value) return
  const result = buildCancelRequest(current.revision)
  if (!result.payload) {
    cancelError.value = fj(result.errorKey || 'errors.invalidRevision')
    return
  }
  const controller = new AbortController()
  cancelController?.abort()
  cancelController = controller
  const batchId = current.batch_id
  const generation = lifecycleGeneration
  const selectionKey = selectedIdsKey.value
  const scopedAdminId = adminId.value
  cancelling.value = true
  cancelError.value = ''
  try {
    const { data } = await feedbackRegressionJobsApi.cancelBatch(
      batchId,
      result.payload,
      controller.signal
    )
    if (controller.signal.aborted) return
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) {
      cancelReadBlocked.value = true
      return
    }
    if (batch.value?.batch_id !== batchId) return
    cancelReadBlocked.value = false
    applyBatch(data)
    ElMessage.success(fj('batch.cancelSuccess'))
  } catch (err: any) {
    if (controller.signal.aborted) return
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return
    if (batch.value?.batch_id !== batchId) return
    if (err?.response?.status === 409) {
      // 旧 revision 立即失效，重读成功前禁止再次发送取消。
      cancelReadBlocked.value = true
      cancelError.value = fj('batch.cancelConflict')
      await refreshBatch({ silent: true })
    } else if (isAmbiguousWriteFailure(err)) {
      cancelReadBlocked.value = true
      cancelError.value = fj('batch.cancelUnconfirmed')
      await refreshBatch({ silent: true })
    } else {
      cancelError.value = jobErrorMessage(err, 'cancelFailed')
    }
  } finally {
    if (cancelController === controller) {
      cancelController = null
      cancelling.value = false
    }
  }
}

async function retryCancelRead() {
  const current = batch.value
  if (!current || cancelReadRetrying.value) return
  const controller = new AbortController()
  cancelReadController?.abort()
  cancelReadController = controller
  const batchId = current.batch_id
  const generation = lifecycleGeneration
  const selectionKey = selectedIdsKey.value
  const scopedAdminId = adminId.value
  cancelReadRetrying.value = true
  cancelError.value = ''
  try {
    const { data } = await feedbackRegressionJobsApi.getBatch(batchId, controller.signal)
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return
    if (batch.value?.batch_id !== batchId) return
    // 真实重读成功才解除封锁。
    cancelReadBlocked.value = false
    batchError.value = ''
    applyBatch(data)
  } catch (err: any) {
    if (!batchOperationIsCurrent(generation, selectionKey, scopedAdminId, controller)) return
    cancelError.value = jobErrorMessage(err, 'errors.loadFailed')
  } finally {
    if (cancelReadController === controller) {
      cancelReadController = null
      cancelReadRetrying.value = false
    }
  }
}

function openComparison(job: FeedbackRegressionJob) {
  if (!isReviewableJob(job)) return
  comparisonJob.value = job
  comparisonVisible.value = true
}

function onComparisonUpdated() {
  void refreshBatch({ silent: true })
}

function onComparisonFailed() {
  void refreshBatch({ silent: true })
}

function clearSelection() {
  if (submitting.value) return
  emit('clear-selection')
}

function closeBatchResult() {
  stopBatchPoll()
  cancelController?.abort()
  cancelController = null
  cancelling.value = false
  cancelReadController?.abort()
  cancelReadController = null
  cancelReadRetrying.value = false
  cancelReadBlocked.value = false
  batch.value = null
  batchError.value = ''
  cancelError.value = ''
  recoveryNotice.value = ''
  comparisonVisible.value = false
  comparisonJob.value = null
}

function resetContextReads() {
  invalidateLifecycle()
  intakeGuard.begin()
  intakeController?.abort()
  intakeController = null
  intakeLoading.value = false
  batchGuard.begin()
  batchController?.abort()
  batchController = null
  batchLoading.value = false
  // 取消请求已发出但结果未知时，旧 revision 立即失效，重读成功前禁止再写。
  if (cancelController) cancelReadBlocked.value = true
  cancelController?.abort()
  cancelController = null
  cancelling.value = false
  recoveryController?.abort()
  recoveryController = null
  recovering.value = false
  cancelReadController?.abort()
  cancelReadController = null
  cancelReadRetrying.value = false
  stopBatchPoll()
  comparisonVisible.value = false
  comparisonJob.value = null
  intake.value = []
  submitError.value = ''
  recoveryError.value = ''
  recoveryNotice.value = ''
  batchError.value = ''
  cancelError.value = ''
  // 批次结果仍在展示时按新上下文重读一次，成功后才解除取消封锁并恢复轮询。
  if (batch.value) void refreshBatch({ silent: true })
}

function abortAll() {
  intakeController?.abort()
  intakeController = null
  intakeLoading.value = false
  submitController?.abort()
  submitController = null
  submitting.value = false
  batchController?.abort()
  batchController = null
  batchLoading.value = false
  cancelController?.abort()
  cancelController = null
  cancelling.value = false
  recoveryController?.abort()
  recoveryController = null
  recovering.value = false
  cancelReadController?.abort()
  cancelReadController = null
  cancelReadRetrying.value = false
  stopBatchPoll()
}

watch(
  () => props.selected.map(item => item.id).join(','),
  () => {
    resetContextReads()
    if (submitting.value) return
    if (props.selected.length) void refreshIntake()
  }
)

watch(
  () => props.resetToken,
  () => {
    resetContextReads()
    if (props.selected.length && !submitting.value) void refreshIntake()
  }
)

watch(submitting, value => emit('busy-change', value))

// 刷新后用户资料可能晚于列表恢复；adminId 就绪时再按当前作用域查一次待恢复批次。
watch(adminId, (value, previous) => {
  if (!value || value === previous || recovering.value || batch.value || unconfirmedRequestId.value) return
  const pending = pendingBatch()
  if (!pending) return
  unconfirmedRequestId.value = pending
  void recoverRequest(pending)
})

onBeforeUnmount(() => {
  invalidateLifecycle()
  intakeGuard.dispose()
  batchGuard.dispose()
  abortAll()
  emit('busy-change', false)
})

// 首次挂载：先按原 request id 做只读恢复，再等用户选择反馈。
const initialPending = pendingBatch()
if (initialPending) {
  unconfirmedRequestId.value = initialPending
  void recoverRequest(initialPending)
}
</script>

<style scoped>
.batch-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  margin-bottom: 14px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
}

.batch-head,
.section-head,
.batch-job-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 12px;
}

.section-head {
  align-items: baseline;
}

h4,
h5 {
  margin: 0 0 3px;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
}

.jobs-title {
  margin-top: 4px;
}

.hint {
  margin: 0;
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.6;
}

.batch-head-actions,
.result-actions,
.intake-actions,
.batch-actions,
.batch-job-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.intake,
.batch-result,
.recovery {
  display: flex;
  flex-direction: column;
  gap: 9px;
  min-width: 0;
  padding: 11px 13px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}

.recovery {
  border-color: var(--warning-border, var(--line));
  background: var(--warning-soft, var(--surface-soft));
}

.section-title {
  color: var(--text);
  font-size: 12.5px;
  font-weight: 600;
}

.intake-list,
.batch-jobs {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.intake-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 4px 12px;
  min-width: 0;
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--surface-soft);
}

.intake-item.blocked {
  border-color: var(--danger-border, var(--danger));
}

.intake-main {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 10px;
  min-width: 0;
}

.intake-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text-2);
  font-size: 11.5px;
  overflow-wrap: anywhere;
}

.intake-status .ok {
  color: var(--success, #2f9e44);
}

.blocked-icon {
  color: var(--danger);
}

.intake-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 12px;
  color: var(--text-3);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.blocked-hint {
  margin: 0;
  color: var(--danger);
  font-size: 11.5px;
}

.mono {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 11px;
}

.break {
  overflow-wrap: anywhere;
  word-break: break-word;
}

.counts {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 8px 12px;
  margin: 0;
}

.counts dt {
  margin: 0 0 1px;
  color: var(--text-3);
  font-size: 10.5px;
}

.counts dd {
  margin: 0;
  color: var(--text);
  font-size: 14px;
  font-variant-numeric: tabular-nums;
}

.batch-job {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  padding: 9px 11px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--surface-soft);
}

.status-line {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text);
  font-size: 12px;
}

.status-succeeded {
  color: var(--success, #2f9e44);
}

.status-failed {
  color: var(--danger);
}

.job-outcome {
  margin: 0;
  color: var(--text-2);
  font-size: 11.5px;
}

@media (max-width: 700px) {
  .counts {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
</style>
