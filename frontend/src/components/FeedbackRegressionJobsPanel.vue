<template>
  <section class="online-jobs" data-testid="regression-jobs">
    <div class="jobs-head">
      <div class="jobs-heading">
        <h5>{{ fj('title') }}</h5>
        <p class="hint">{{ fj('description') }}</p>
      </div>
      <el-button
        link
        size="small"
        :loading="listLoading"
        :disabled="listLoading"
        data-testid="regression-jobs-refresh"
        @click="refresh"
      >
        <el-icon><Refresh /></el-icon>{{ fj('refresh') }}
      </el-button>
    </div>

    <el-alert
      v-if="!available"
      :title="unavailableText"
      type="warning"
      :closable="false"
      show-icon
      data-testid="regression-job-unavailable"
    />

    <template v-if="available">
      <div class="callout" data-testid="regression-job-new-call">
        <div class="callout-title">
          <el-icon><WarningFilled /></el-icon>
          <strong>{{ fj('newCallTitle') }}</strong>
        </div>
        <p class="hint">{{ fj('newCallHint') }}</p>
        <el-checkbox
          v-model="acknowledged"
          :disabled="creating || !!activeJobId"
          data-testid="regression-job-ack"
        >
          {{ fj('acknowledge') }}
        </el-checkbox>
        <div class="submit-row">
          <el-button
            type="primary"
            :loading="creating"
            :disabled="!canSubmit"
            data-testid="regression-job-submit"
            @click="createJob"
          >
            {{ creating ? fj('submitting') : fj('submit') }}
          </el-button>
          <span v-if="activeJobId" class="hint" data-testid="regression-job-active-note">
            {{ fj('activeNotice') }}
          </span>
        </div>
      </div>

      <el-alert
        v-if="createError"
        :title="createError"
        type="error"
        :closable="false"
        show-icon
        data-testid="regression-job-create-error"
      />
      <el-alert
        v-if="cancelError"
        :title="cancelError"
        type="warning"
        :closable="false"
        show-icon
        data-testid="regression-job-cancel-error"
      />
      <el-button
        v-if="cancelReadBlockedJobId"
        size="small"
        plain
        :loading="cancelReadRetrying"
        :disabled="cancelReadRetrying"
        data-testid="regression-job-cancel-read"
        @click="retryCancelRead"
      >
        <el-icon><Refresh /></el-icon>{{ fj('actions.retryRead') }}
      </el-button>
      <el-alert
        v-if="recoveryNotice"
        :title="recoveryNotice"
        type="success"
        :closable="false"
        show-icon
        data-testid="regression-job-recovered"
      />

      <div
        v-if="unconfirmedRequestId"
        class="recovery"
        data-testid="regression-job-unconfirmed"
      >
        <div class="section-title">{{ fj('unconfirmedTitle') }}</div>
        <p class="hint">
          {{ fj('unconfirmedHint', { requestId: unconfirmedRequestId }) }}
        </p>
        <p class="mono break">{{ fj('pendingRequestLabel') }}: {{ unconfirmedRequestId }}</p>
        <el-button
          size="small"
          plain
          :loading="recovering"
          data-testid="regression-job-recover"
          @click="recoverRequest(unconfirmedRequestId)"
        >
          <el-icon><Refresh /></el-icon>{{ fj('actions.retryRead') }}
        </el-button>
      </div>
      <p v-if="recovering && !unconfirmedRequestId" class="hint" data-testid="regression-job-recovering">
        {{ fj('recoveryReading') }}
      </p>

      <div v-if="activeJob" class="active-job" data-testid="regression-job-active">
        <div class="active-head">
          <el-icon class="spin"><Loading /></el-icon>
          <strong>{{ fj('activeJobTitle') }}</strong>
          <span class="mono">{{ activeJob.job_id }}</span>
          <el-tag :type="statusTagType(activeJob.status)" size="small" effect="light">
            {{ fj(jobStatusKey(activeJob.status)) }}
          </el-tag>
        </div>
        <p class="hint">{{ fj('polling') }}</p>
        <div class="active-actions">
          <el-button
            v-if="canCancelJob(activeJob)"
            size="small"
            plain
            :loading="cancellingId === activeJob.job_id"
            :disabled="!!cancellingId"
            data-testid="regression-job-active-cancel"
            @click="cancelJob(activeJob)"
          >
            <el-icon><VideoPause /></el-icon>{{ fj('actions.cancel') }}
          </el-button>
        </div>
      </div>
    </template>

    <section class="jobs-history">
      <div class="section-head">
        <h5>{{ fj('historyTitle') }}</h5>
        <span class="hint">{{ fj('historyHint') }}</span>
      </div>

      <div v-if="listLoading" class="hint" role="status">{{ fj('loading') }}</div>
      <el-alert
        v-if="listError"
        :title="listError"
        type="error"
        :closable="false"
        show-icon
        data-testid="regression-jobs-error"
      />
      <p
        v-if="!listLoading && !jobs.length"
        class="hint"
        data-testid="regression-jobs-empty"
      >
        {{ fj('historyEmpty') }}
      </p>

      <ul v-else class="job-list" data-testid="regression-jobs-list">
        <li
          v-for="job in jobs"
          :key="job.job_id"
          class="job-item"
          :data-testid="`regression-job-${job.job_id}`"
        >
          <div class="job-top">
            <span class="status-line" :class="`status-${job.status}`">
              <el-icon>
                <component :is="statusIcon(job.status)" />
              </el-icon>
              <strong>{{ fj(jobStatusKey(job.status)) }}</strong>
            </span>
            <span class="job-attempt">
              {{ fj('attemptLabel') }} #{{ formatNumber(job.run_attempt_no) }}
            </span>
            <el-tag :type="reviewTagType(job.review.status)" size="small" effect="light">
              {{ fj(reviewStatusKey(job.review.status)) }}
            </el-tag>
            <span class="hint job-created">{{ fj('createdLabel') }} {{ formatDateTime(job.created_at) }}</span>
          </div>

          <p class="job-outcome" data-testid="regression-job-outcome">
            {{ fj(jobOutcomeKey(job)) }}
          </p>

          <dl class="job-meta">
            <div>
              <dt>{{ fj('jobLabel') }}</dt>
              <dd class="mono break">{{ job.job_id }}</dd>
            </div>
            <div>
              <dt>{{ fj('runLabel') }}</dt>
              <dd class="mono break">{{ job.run_id }}</dd>
            </div>
            <div>
              <dt>{{ fj('revisionLabel') }}</dt>
              <dd class="mono">{{ formatNumber(job.revision) }}</dd>
            </div>
            <div v-if="job.batch_id">
              <dt>{{ fj('batchLabel') }}</dt>
              <dd class="mono break">{{ job.batch_id }}</dd>
            </div>
            <div>
              <dt>{{ fj('startedLabel') }}</dt>
              <dd>{{ job.started_at ? formatDateTime(job.started_at) : '—' }}</dd>
            </div>
            <div>
              <dt>{{ fj('completedLabel') }}</dt>
              <dd>{{ job.completed_at ? formatDateTime(job.completed_at) : '—' }}</dd>
            </div>
            <div v-if="job.cancelled_at">
              <dt>{{ fj('cancelledLabel') }}</dt>
              <dd>{{ formatDateTime(job.cancelled_at) }}</dd>
            </div>
            <div v-if="job.error_code">
              <dt>{{ fj('errorCodeLabel') }}</dt>
              <dd class="mono error-code" data-testid="regression-job-error-code">
                {{ job.error_code }}
              </dd>
            </div>
          </dl>

          <FeedbackRegressionJobUsage :usage="job.usage" />

          <div class="job-actions">
            <el-button
              v-if="isReviewableJob(job)"
              size="small"
              type="primary"
              plain
              :data-testid="`regression-job-compare-${job.job_id}`"
              @click="openComparison(job)"
            >
              {{ fj('actions.compare') }}
            </el-button>
            <el-button
              v-if="canCancelJob(job)"
              size="small"
              plain
              :loading="cancellingId === job.job_id"
              :disabled="!!cancellingId"
              :data-testid="`regression-job-cancel-${job.job_id}`"
              @click="cancelJob(job)"
            >
              <el-icon><VideoPause /></el-icon>{{ fj('actions.cancel') }}
            </el-button>
          </div>
        </li>
      </ul>

      <el-pagination
        v-if="total > PAGE_SIZE"
        v-model:current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="total, prev, pager, next"
        :disabled="listLoading"
        data-testid="regression-jobs-pagination"
        @current-change="onPageChange"
      />
    </section>

    <el-dialog
      v-model="comparisonVisible"
      :title="fj('comparison.title')"
      width="min(1080px, 96vw)"
      append-to-body
      destroy-on-close
      data-testid="regression-job-comparison-dialog"
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
  feedbackRegressionJobsApi,
  type FeedbackRegressionJob,
  type FeedbackRegressionJobStatus
} from '@/api/feedbackRegressionJobs'
import {
  feedbackJobsHasReason,
  useFeedbackRegressionJobsI18n
} from '@/i18n/messages/feedbackRegressionJobs'
import { formatDateTime, formatNumber } from '@/i18n/format'
import { useUserStore } from '@/stores/user'
import { createLatestGuard } from '@/utils/usage'
import { unavailableReasonKey } from '@/utils/feedbackRegression'
import {
  buildJobCreatePayload,
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
  savePendingRequest,
  type PendingRegressionRequest
} from '@/utils/feedbackRegressionJobs'
import FeedbackRegressionJobComparison from '@/components/FeedbackRegressionJobComparison.vue'
import FeedbackRegressionJobUsage from '@/components/FeedbackRegressionJobUsage.vue'

const props = defineProps<{
  feedbackId: string
  regressionRevision: number
  available: boolean
  unavailableReason: string | null
}>()

const emit = defineEmits<{
  (event: 'active-change', active: boolean): void
  (event: 'changed'): void
}>()

const { t } = useI18n()
const { fj } = useFeedbackRegressionJobsI18n()
const userStore = useUserStore()
const storage = browserRecoveryStorage()
const origin = currentOrigin()

const PAGE_SIZE = 10
const POLL_INTERVAL_MS = 2000
const POLL_MAX_FAILURES = 3

const jobs = ref<FeedbackRegressionJob[]>([])
const total = ref(0)
const page = ref(1)
const listLoading = ref(false)
const listError = ref('')
const listGuard = createLatestGuard()
let listController: AbortController | null = null

const acknowledged = ref(false)
const creating = ref(false)
const createError = ref('')
const cancelError = ref('')
const cancellingId = ref('')
let createController: AbortController | null = null
let cancelController: AbortController | null = null

const recovering = ref(false)
const unconfirmedRequestId = ref('')
const recoveryNotice = ref('')
let recoveryController: AbortController | null = null

const activeJobId = ref<string | null>(null)
const activeJob = ref<FeedbackRegressionJob | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollController: AbortController | null = null
let pollFailures = 0
let lastActiveEmitted = false

const comparisonVisible = ref(false)
const comparisonJob = ref<FeedbackRegressionJob | null>(null)
const cancelReadBlockedJobId = ref<string | null>(null)
const cancelReadRetrying = ref(false)
let cancelReadController: AbortController | null = null
let lifecycleGeneration = 0

const adminId = computed(() => userStore.profile?.id || '')

function invalidateLifecycle() {
  lifecycleGeneration += 1
}

function operationIsCurrent(
  generation: number,
  feedbackId: string,
  scopedAdminId: string,
  controller: AbortController
): boolean {
  return !controller.signal.aborted
    && generation === lifecycleGeneration
    && feedbackId === props.feedbackId
    && scopedAdminId === adminId.value
}

function canCancelJob(job: FeedbackRegressionJob): boolean {
  return isCancelableJob(job) && cancelReadBlockedJobId.value !== job.job_id
}

const unavailableText = computed(() => {
  const reason = props.unavailableReason || 'unknown'
  return t(unavailableReasonKey(props.unavailableReason), { reason })
})

const canSubmit = computed(() =>
  props.available
  && !!props.feedbackId
  && acknowledged.value
  && !creating.value
  && !recovering.value
  && !unconfirmedRequestId.value
  && !activeJobId.value
)

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

function statusTagType(
  status: FeedbackRegressionJobStatus
): 'success' | 'warning' | 'info' | 'danger' {
  if (status === 'succeeded') return 'success'
  if (status === 'running') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}

function reviewTagType(
  status: FeedbackRegressionJob['review']['status']
): 'success' | 'warning' | 'info' | 'danger' {
  if (status === 'verified') return 'success'
  if (status === 'rejected') return 'danger'
  return 'info'
}

function emitActive(active: boolean) {
  if (active === lastActiveEmitted) return
  lastActiveEmitted = active
  emit('active-change', active)
}

function setActive(job: FeedbackRegressionJob | null) {
  if (job && isActiveJobStatus(job.status)) {
    activeJobId.value = job.job_id
    activeJob.value = job
    emitActive(true)
  } else {
    activeJobId.value = null
    activeJob.value = null
    emitActive(false)
  }
}

function applyJob(job: FeedbackRegressionJob) {
  const index = jobs.value.findIndex(item => item.job_id === job.job_id)
  if (index >= 0) jobs.value.splice(index, 1, job)
  if (activeJobId.value === job.job_id) {
    if (isActiveJobStatus(job.status)) {
      activeJob.value = job
    } else {
      setActive(null)
    }
  }
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
  pollController?.abort()
  pollController = null
  pollFailures = 0
}

function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer)
  if (!activeJobId.value) return
  const id = activeJobId.value
  pollTimer = setTimeout(() => {
    pollTimer = null
    void pollActiveJob(id)
  }, POLL_INTERVAL_MS)
}

async function pollActiveJob(id: string) {
  if (activeJobId.value !== id) return
  const generation = lifecycleGeneration
  const feedbackId = props.feedbackId
  const scopedAdminId = adminId.value
  const controller = new AbortController()
  pollController = controller
  try {
    const { data } = await feedbackRegressionJobsApi.getJob(id, controller.signal)
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)
      || activeJobId.value !== id) return
    cancelReadBlockedJobId.value = cancelReadBlockedJobId.value === id ? null : cancelReadBlockedJobId.value
    applyJob(data)
    pollFailures = 0
    if (isActiveJobStatus(data.status)) {
      schedulePoll()
    } else {
      stopPolling()
      await loadJobs({ silent: true })
      if (generation !== lifecycleGeneration || feedbackId !== props.feedbackId || scopedAdminId !== adminId.value) return
      emit('changed')
    }
  } catch (err: any) {
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)
      || activeJobId.value !== id) return
    pollFailures += 1
    if (pollFailures >= POLL_MAX_FAILURES) {
      listError.value = jobErrorMessage(err, 'errors.loadFailed')
      stopPolling()
    } else {
      schedulePoll()
    }
  } finally {
    if (pollController === controller) pollController = null
  }
}

function syncActiveFromItems(items: FeedbackRegressionJob[]) {
  const found = items.find(item => isActiveJobStatus(item.status))
  if (found) {
    setActive(found)
    if (!pollTimer) schedulePoll()
    return
  }
  // 当前页没有活动作业时，只有已有活动快照已终止才清除；不能因为翻页误判。
  if (activeJob.value && !isActiveJobStatus(activeJob.value.status)) {
    setActive(null)
  }
}

async function loadJobs(options: { silent?: boolean; resetPage?: boolean } = {}) {
  if (!props.feedbackId) return
  if (options.resetPage) page.value = 1
  const token = listGuard.begin()
  const generation = lifecycleGeneration
  const feedbackId = props.feedbackId
  const scopedAdminId = adminId.value
  listController?.abort()
  const controller = new AbortController()
  listController = controller
  if (!options.silent) listLoading.value = true
  listError.value = ''
  try {
    const { data } = await feedbackRegressionJobsApi.listJobs(
      {
        feedback_id: props.feedbackId,
        page: page.value,
        page_size: PAGE_SIZE
      },
      controller.signal
    )
    if (controller.signal.aborted || !listGuard.isCurrent(token)
      || !operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    jobs.value = Array.isArray(data?.items) ? data.items : []
    total.value = typeof data?.total === 'number' && data.total >= 0 ? data.total : jobs.value.length
    if (jobs.value.some(job => job.job_id === cancelReadBlockedJobId.value)) {
      cancelReadBlockedJobId.value = null
    }
    syncActiveFromItems(jobs.value)
    return true
  } catch (err: any) {
    if (controller.signal.aborted || !listGuard.isCurrent(token)
      || !operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return false
    listError.value = jobErrorMessage(err, 'errors.loadFailed')
    if (!options.silent) {
      jobs.value = []
      total.value = 0
    }
    return false
  } finally {
    if (listGuard.isCurrent(token)
      && operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) {
      listLoading.value = false
      if (listController === controller) listController = null
    }
  }
}

function refresh() {
  if (listLoading.value) return
  cancelError.value = ''
  recoveryNotice.value = ''
  void loadJobs()
  if (activeJobId.value && !pollTimer) schedulePoll()
}

function onPageChange(next: number) {
  page.value = next
  void loadJobs()
}

function pendingForFeedback(): PendingRegressionRequest | null {
  const entries = loadPendingRequests(storage, origin, adminId.value)
  return entries.find(entry =>
    entry.kind === 'job' && entry.feedback_ids.includes(props.feedbackId)
  ) || null
}

async function recoverRequest(requestId: string) {
  if (!requestId || recovering.value) return
  const generation = lifecycleGeneration
  const feedbackId = props.feedbackId
  const scopedAdminId = adminId.value
  const controller = new AbortController()
  recoveryController?.abort()
  recoveryController = controller
  recovering.value = true
  createError.value = ''
  try {
    const { data } = await feedbackRegressionJobsApi.lookupRequest(requestId, controller.signal)
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    const job = data.kind === 'job' ? data.job : null
    if (!job || job.feedback_id !== feedbackId) {
      recoveryNotice.value = ''
      unconfirmedRequestId.value = requestId
      createError.value = fj('recoveryMismatch')
      return
    }
    removePendingRequest(storage, origin, adminId.value, requestId)
    unconfirmedRequestId.value = ''
    recoveryNotice.value = fj('recovered')
    createError.value = ''
    acknowledged.value = false
    page.value = 1
    await loadJobs({ resetPage: true })
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    applyJob(job)
    setActive(job)
    if (activeJobId.value) schedulePoll()
    emit('changed')
  } catch (err: any) {
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    unconfirmedRequestId.value = requestId
    if (err?.response?.status === 404) {
      recoveryNotice.value = ''
      createError.value = fj('errors.requestNotFound')
    } else {
      recoveryNotice.value = ''
      createError.value = jobErrorMessage(err, 'recoveryFailed')
    }
  } finally {
    if (recoveryController === controller) recoveryController = null
    if (operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) {
      recovering.value = false
    }
  }
}

async function createJob() {
  if (!canSubmit.value || !props.feedbackId) return
  const result = buildJobCreatePayload(props.feedbackId, props.regressionRevision)
  if (!result.payload) {
    createError.value = fj(result.errorKey || 'errors.createFailed')
    return
  }
  const requestId = result.payload.request_id
  const generation = lifecycleGeneration
  const feedbackId = props.feedbackId
  const scopedAdminId = adminId.value
  const controller = new AbortController()
  createController?.abort()
  createController = controller
  creating.value = true
  createError.value = ''
  recoveryNotice.value = ''
  unconfirmedRequestId.value = ''
  // 先持久化原 request UUID，再发写请求；响应丢失时只能据此只读找回。
  savePendingRequest(storage, origin, adminId.value, {
    request_id: requestId,
    kind: 'job',
    feedback_ids: [feedbackId],
    created_at: Date.now()
  })
  try {
    const { data } = await feedbackRegressionJobsApi.createJob(result.payload, controller.signal)
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    removePendingRequest(storage, origin, adminId.value, requestId)
    acknowledged.value = false
    ElMessage.success(fj('submitSuccess'))
    page.value = 1
    await loadJobs({ resetPage: true })
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    applyJob(data)
    setActive(data)
    if (activeJobId.value) schedulePoll()
    emit('changed')
  } catch (err: any) {
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    if (isAmbiguousWriteFailure(err)) {
      // 不重发 POST；同一 UUID 只读找回。找回失败时保留未确认状态供再次读取。
      if (err?.code === 'ERR_NETWORK' || err?.code === 'ECONNABORTED' || err?.code === 'ETIMEDOUT') {
        createError.value = fj('errors.network')
      } else {
        createError.value = jobErrorMessage(err, 'errors.createFailed')
      }
      await recoverRequest(requestId)
    } else {
      removePendingRequest(storage, origin, adminId.value, requestId)
      createError.value = jobErrorMessage(err, 'errors.createFailed')
      if (err?.response?.status === 409) {
        await loadJobs({ resetPage: true })
        if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
        emit('changed')
      }
    }
  } finally {
    if (createController === controller) createController = null
    if (operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) {
      creating.value = false
    }
  }
}

async function refreshAfterFailedWrite(
  jobId: string,
  generation: number,
  feedbackId: string,
  scopedAdminId: string
) {
  try {
    const { data } = await feedbackRegressionJobsApi.getJob(jobId)
    if (generation !== lifecycleGeneration || feedbackId !== props.feedbackId
      || scopedAdminId !== adminId.value) return
    // 只有针对该作业的真实读取成功，才解除取消写阻断并换用服务端新 revision。
    cancelReadBlockedJobId.value = cancelReadBlockedJobId.value === jobId ? null : cancelReadBlockedJobId.value
    applyJob(data)
    if (!isActiveJobStatus(data.status)) setActive(null)
  } catch {
    /* 读取失败时保留阻断：旧 CAS revision 在真实重读成功前不可再次发送。 */
  }
  const listed = await loadJobs({ silent: true })
  if (!listed || generation !== lifecycleGeneration || feedbackId !== props.feedbackId
    || scopedAdminId !== adminId.value) return
  if (!activeJobId.value) emit('changed')
}

async function retryCancelRead() {
  const jobId = cancelReadBlockedJobId.value
  if (!jobId || cancelReadRetrying.value) return
  const generation = lifecycleGeneration
  const feedbackId = props.feedbackId
  const scopedAdminId = adminId.value
  const controller = new AbortController()
  cancelReadController?.abort()
  cancelReadController = controller
  cancelReadRetrying.value = true
  try {
    const { data } = await feedbackRegressionJobsApi.getJob(jobId, controller.signal)
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    cancelReadBlockedJobId.value = null
    cancelError.value = ''
    applyJob(data)
    if (!isActiveJobStatus(data.status)) setActive(null)
    await loadJobs({ silent: true })
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    emit('changed')
  } catch (err: any) {
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    cancelError.value = jobErrorMessage(err, 'errors.loadFailed')
  } finally {
    if (cancelReadController === controller) cancelReadController = null
    if (operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) {
      cancelReadRetrying.value = false
    }
  }
}

async function cancelJob(job: FeedbackRegressionJob) {
  if (!canCancelJob(job) || cancellingId.value) return
  const result = buildCancelRequest(job.revision)
  if (!result.payload) {
    cancelError.value = fj(result.errorKey || 'errors.invalidRevision')
    return
  }
  const generation = lifecycleGeneration
  const feedbackId = props.feedbackId
  const scopedAdminId = adminId.value
  const controller = new AbortController()
  cancelController?.abort()
  cancelController = controller
  cancellingId.value = job.job_id
  cancelError.value = ''
  try {
    const { data } = await feedbackRegressionJobsApi.cancelJob(
      job.job_id,
      result.payload,
      controller.signal
    )
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    cancelReadBlockedJobId.value = null
    applyJob(data)
    if (!isActiveJobStatus(data.status)) {
      setActive(null)
      stopPolling()
    }
    ElMessage.success(fj('cancelSuccess'))
    await loadJobs({ silent: true })
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    emit('changed')
  } catch (err: any) {
    if (!operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) return
    if (err?.response?.status === 409) {
      cancelError.value = fj('cancelConflict')
      // 先立即使该作业的旧 revision 不可再写，再尝试真实重读。
      cancelReadBlockedJobId.value = job.job_id
      await refreshAfterFailedWrite(job.job_id, generation, feedbackId, scopedAdminId)
    } else if (isAmbiguousWriteFailure(err)) {
      cancelError.value = fj('cancelUnconfirmed')
      cancelReadBlockedJobId.value = job.job_id
      await refreshAfterFailedWrite(job.job_id, generation, feedbackId, scopedAdminId)
    } else {
      cancelError.value = jobErrorMessage(err, 'cancelFailed')
    }
  } finally {
    if (cancelController === controller) cancelController = null
    if (operationIsCurrent(generation, feedbackId, scopedAdminId, controller)) {
      cancellingId.value = ''
    }
  }
}

function openComparison(job: FeedbackRegressionJob) {
  if (!isReviewableJob(job)) return
  comparisonJob.value = job
  comparisonVisible.value = true
}

function onComparisonUpdated(job: FeedbackRegressionJob) {
  applyJob(job)
  emit('changed')
}

function onComparisonFailed() {
  void loadJobs({ silent: true })
}

function abortAll() {
  listController?.abort()
  listController = null
  createController?.abort()
  createController = null
  recoveryController?.abort()
  recoveryController = null
  cancelController?.abort()
  cancelController = null
  cancelReadController?.abort()
  cancelReadController = null
}

function resetState() {
  invalidateLifecycle()
  stopPolling()
  abortAll()
  jobs.value = []
  total.value = 0
  page.value = 1
  listLoading.value = false
  listError.value = ''
  acknowledged.value = false
  creating.value = false
  createError.value = ''
  cancelError.value = ''
  cancellingId.value = ''
  cancelReadBlockedJobId.value = null
  cancelReadRetrying.value = false
  recovering.value = false
  unconfirmedRequestId.value = ''
  recoveryNotice.value = ''
  activeJobId.value = null
  activeJob.value = null
  lastActiveEmitted = false
  comparisonVisible.value = false
  comparisonJob.value = null
  emit('active-change', false)
}

async function initialize() {
  if (!props.feedbackId) return
  const feedbackId = props.feedbackId
  const generation = lifecycleGeneration
  const scopedAdminId = adminId.value
  await loadJobs({ resetPage: true })
  // 关闭/unmount 可能仍保持同一 feedbackId；await 后必须同时复核生命周期与管理员。
  if (feedbackId !== props.feedbackId
    || generation !== lifecycleGeneration
    || scopedAdminId !== adminId.value) return
  const pending = pendingForFeedback()
  if (pending) {
    unconfirmedRequestId.value = pending.request_id
    await recoverRequest(pending.request_id)
  }
}

watch(
  () => props.feedbackId,
  () => {
    resetState()
    if (props.feedbackId) void initialize()
  },
  { immediate: true }
)

// 刷新后用户资料可能晚于抽屉挂载才恢复；adminId 就绪时再按当前作用域查一次待恢复 UUID。
watch(adminId, (value, previous) => {
  if (!value || value === previous || recovering.value || unconfirmedRequestId.value) return
  const pending = pendingForFeedback()
  if (!pending) return
  unconfirmedRequestId.value = pending.request_id
  void recoverRequest(pending.request_id)
})

defineExpose({
  /** 人工准备遇到 409 时由父面板触发一次真实重读，避免继续显示过期活动状态。 */
  refreshNow: () => refresh()
})

onBeforeUnmount(() => {
  invalidateLifecycle()
  listGuard.dispose()
  stopPolling()
  abortAll()
})
</script>

<style scoped>
.online-jobs {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}

.jobs-head,
.section-head,
.job-top,
.active-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 12px;
}

.section-head {
  align-items: baseline;
}

.jobs-heading h5,
.jobs-history h5 {
  margin: 0 0 3px;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
}

.hint {
  margin: 0;
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.6;
}

.callout,
.recovery,
.active-job,
.jobs-history {
  display: flex;
  flex-direction: column;
  gap: 9px;
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
}

.callout {
  border-color: var(--warning-border, var(--line));
}

.callout-title {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--warning);
  font-size: 12.5px;
}

.section-title {
  color: var(--text);
  font-size: 12.5px;
  font-weight: 600;
}

.submit-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.recovery {
  border-color: var(--warning-border, var(--line));
  background: var(--warning-soft, var(--surface-soft));
}

.active-job {
  border-color: var(--primary-border, var(--line));
}

.active-head {
  align-items: center;
  justify-content: flex-start;
  font-size: 12.5px;
}

.spin {
  animation: spin 1.2s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.job-list {
  display: flex;
  flex-direction: column;
  gap: 9px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.job-item {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}

.status-line {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--text);
  font-size: 12.5px;
}

.status-succeeded {
  color: var(--success, #2f9e44);
}

.status-failed {
  color: var(--danger);
}

.status-running {
  color: var(--primary, var(--text));
}

.job-attempt {
  color: var(--text-2);
  font-size: 11.5px;
}

.job-created {
  margin-left: auto;
}

.job-outcome {
  margin: 0;
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.6;
}

.job-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px 12px;
  margin: 0;
}

.job-meta > div {
  min-width: 0;
}

.job-meta dt {
  margin: 0 0 1px;
  color: var(--text-3);
  font-size: 10.5px;
}

.job-meta dd {
  margin: 0;
  color: var(--text-2);
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

.error-code {
  color: var(--danger);
  overflow-wrap: anywhere;
}

.job-actions,
.active-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

@media (max-width: 700px) {
  .job-meta {
    grid-template-columns: 1fr 1fr;
  }

  .job-created {
    margin-left: 0;
  }
}
</style>
