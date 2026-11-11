<template>
  <section v-loading="loading" class="regression" data-testid="feedback-regression">
    <div class="regression-head">
      <div class="regression-heading">
        <h4>{{ t('feedback.regression.title') }}</h4>
        <p class="regression-desc">{{ t('feedback.regression.description') }}</p>
      </div>
      <el-button
        link
        size="small"
        :loading="loading"
        :disabled="loading || busy"
        data-testid="regression-refresh"
        @click="refresh"
      >
        <el-icon><Refresh /></el-icon>{{ t('feedback.regression.refresh') }}
      </el-button>
    </div>

    <el-alert
      v-if="loadError"
      :title="loadError"
      type="error"
      :closable="false"
      show-icon
      data-testid="regression-load-error"
    />

    <el-alert
      v-if="unavailableText"
      :title="unavailableText"
      type="warning"
      :closable="false"
      show-icon
      data-testid="regression-unavailable"
    />

    <div v-if="!loaded && loading" class="regression-loading" role="status">
      {{ t('feedback.regression.loading') }}
    </div>

    <template v-if="loaded">
      <!-- 未纳入：原因 + 说明 + 纳入 -->
      <div v-if="!regression" class="enroll" data-testid="regression-enroll">
        <p class="regression-status">{{ t('feedback.regression.notEnrolled') }}</p>
        <div class="field">
          <label class="field-label" for="regression-cause">{{ t('feedback.regression.causeLabel') }}</label>
          <el-select
            id="regression-cause"
            v-model="enrollCause"
            class="cause-select"
            :disabled="!canEnroll"
            :placeholder="t('feedback.regression.causePlaceholder')"
            data-testid="regression-cause"
          >
            <el-option
              v-for="cause in REGRESSION_CAUSES"
              :key="cause"
              :value="cause"
              :label="t(regressionCauseKey(cause))"
            />
          </el-select>
        </div>
        <div class="field">
          <label class="field-label" for="regression-cause-note">{{ t('feedback.regression.causeNoteLabel') }}</label>
          <el-input
            id="regression-cause-note"
            v-model="enrollNote"
            type="textarea"
            :rows="3"
            resize="none"
            :maxlength="MAX_REGRESSION_NOTE_LENGTH"
            show-word-limit
            :disabled="!canEnroll"
            :placeholder="t('feedback.regression.causeNotePlaceholder')"
          />
        </div>
        <el-alert
          v-if="enrollError"
          :title="enrollError"
          type="error"
          :closable="false"
          show-icon
          data-testid="regression-enroll-error"
        />
        <div class="actions">
          <el-button
            type="primary"
            :loading="enrolling"
            :disabled="!canEnroll"
            data-testid="regression-enroll-submit"
            @click="enroll"
          >
            {{ enrolling ? t('feedback.regression.enrolling') : t('feedback.regression.enroll') }}
          </el-button>
        </div>
      </div>

      <!-- 已纳入：元数据、准备、导入、历史 -->
      <template v-else>
        <dl class="regression-meta">
          <div class="meta-cell">
            <dt>{{ t('feedback.regression.causeLabel') }}</dt>
            <dd>{{ t(regressionCauseKey(regression.cause)) }}</dd>
          </div>
          <div class="meta-cell">
            <dt>{{ t('feedback.regression.revisionLabel') }}</dt>
            <dd class="mono">{{ formatNumber(regression.revision) }}</dd>
          </div>
          <div class="meta-cell">
            <dt>{{ t('feedback.regression.currentAttemptLabel') }}</dt>
            <dd class="mono">{{ regression.current_attempt_no ? formatNumber(regression.current_attempt_no) : '—' }}</dd>
          </div>
          <div class="meta-cell">
            <dt>{{ t('feedback.regression.enrolledByLabel') }}</dt>
            <dd>{{ regression.created_by_name || '—' }}</dd>
          </div>
          <div class="meta-cell">
            <dt>{{ t('feedback.regression.enrolledAtLabel') }}</dt>
            <dd>{{ formatDateTime(regression.created_at) }}</dd>
          </div>
          <div class="meta-cell meta-wide">
            <dt>{{ t('feedback.regression.causeNoteLabel') }}</dt>
            <dd class="pre-wrap">{{ regression.cause_note }}</dd>
          </div>
        </dl>

        <FeedbackRegressionJobsPanel
          ref="jobsPanel"
          :feedback-id="feedbackId"
          :regression-revision="regression.revision"
          :available="regression.available"
          :unavailable-reason="regression.unavailable_reason"
          @active-change="onOnlineActiveChange"
          @changed="onOnlineChanged"
        />

        <section class="prepare">
          <h5>{{ t('feedback.regression.prepareTitle') }}</h5>
          <p class="hint">{{ t('feedback.regression.prepareHint') }}</p>
          <p v-if="onlineActive" class="hint online-block" data-testid="regression-prepare-blocked">
            {{ fj('prepareBlockedOnline') }}
          </p>
          <div class="prepare-row">
            <span class="field-label">{{ t('feedback.regression.prepareModeLabel') }}</span>
            <el-radio-group v-model="prepareMode" :disabled="!canPrepare" data-testid="regression-mode">
              <el-radio v-for="mode in REGRESSION_MODES" :key="mode" :value="mode">
                {{ t(regressionModeKey(mode)) }}
              </el-radio>
            </el-radio-group>
        <el-button
          type="primary"
          plain
          :loading="preparing"
          :disabled="!canPrepare"
          data-testid="regression-prepare"
          @click="prepare"
        >
          <el-icon><Download /></el-icon>
          {{ preparing ? t('feedback.regression.preparing') : t('feedback.regression.prepare') }}
        </el-button>
          </div>
          <el-alert
            v-if="prepareError"
            :title="prepareError"
            type="error"
            :closable="false"
            show-icon
            data-testid="regression-prepare-error"
          />
          <div v-if="cliHint" class="cli-hint" data-testid="regression-cli-hint">
            <span class="field-label">{{ t('feedback.regression.cliHintLabel') }}</span>
            <pre>{{ cliHint }}</pre>
            <p v-if="preparedFilename" class="hint">
              {{ t('feedback.regression.preparedFile', { filename: preparedFilename }) }}
            </p>
          </div>
        </section>

        <section v-if="importReady" class="import" data-testid="regression-import">
          <h5>{{ t('feedback.regression.importTitle') }}</h5>
          <p class="hint">{{ t('feedback.regression.importHint') }}</p>
          <input
            ref="fileInput"
            class="file-input"
            type="file"
            accept=".json,application/json"
            data-testid="regression-import-input"
            @change="onFileChange"
          />
          <div class="import-row">
            <el-button :disabled="importing || preparing" @click="chooseReport">
              {{ t('feedback.regression.importSelect') }}
            </el-button>
            <div v-if="selectedFile" class="selected-file" data-testid="regression-selected-file">
              <span class="file-name">{{ selectedFile.name }}</span>
              <span class="meta-item">{{ formatFileSize(selectedFile.size) }}</span>
            </div>
            <el-button
              type="primary"
              :loading="importing"
              :disabled="!selectedFile || importing || preparing || !!unavailableReason"
              data-testid="regression-import-confirm"
              @click="confirmImport"
            >
              {{ importing ? t('feedback.regression.importing') : t('feedback.regression.importConfirm') }}
            </el-button>
            <el-button v-if="importing" data-testid="regression-import-cancel" @click="cancelImport">
              {{ t('feedback.regression.importCancel') }}
            </el-button>
          </div>
          <p v-if="fileError" class="field-error" data-testid="regression-file-error">{{ fileError }}</p>
          <el-alert
            v-if="importError"
            :title="importError"
            type="error"
            :closable="false"
            show-icon
            data-testid="regression-import-error"
          />
        </section>
        <p v-else-if="currentRun" class="hint" data-testid="regression-import-unavailable">
          {{ currentRun.execution_kind === 'online'
            ? fj('runImportUnavailableOnline')
            : t('feedback.regression.importNotReady') }}
        </p>

        <section class="history">
          <div class="history-head">
            <h5>{{ t('feedback.regression.historyTitle') }}</h5>
            <el-pagination
              v-if="historyTotal > 0"
              v-model:current-page="historyPage"
              :page-size="HISTORY_PAGE_SIZE"
              :total="historyTotal"
              layout="total, prev, pager, next"
              :disabled="historyLoading"
              @current-change="onHistoryPageChange"
            />
          </div>
          <p class="hint">{{ t('feedback.regression.historyHint') }}</p>
          <el-alert
            v-if="historyError"
            :title="historyError"
            type="error"
            :closable="false"
            show-icon
            data-testid="regression-history-error"
          />
          <p v-if="!historyLoading && !historyItems.length" class="is-empty" data-testid="regression-history-empty">
            {{ t('feedback.regression.historyEmpty') }}
          </p>
          <ul v-else class="run-list" data-testid="regression-history">
            <li
              v-for="run in historyItems"
              :key="run.run_id"
              class="run-item"
              :class="{ current: run.is_current }"
              :data-testid="`regression-run-${run.attempt_no}`"
            >
              <div class="run-top">
                <span class="run-attempt">
                  {{ t('feedback.regression.history.attempt') }} #{{ formatNumber(run.attempt_no) }}
                </span>
                <el-tag v-if="run.is_current" size="small" type="primary" effect="plain">
                  {{ t('feedback.regression.history.current') }}
                </el-tag>
                <span class="run-time">{{ formatDateTime(run.created_at) }}</span>
                <el-button
                  v-if="run.is_current && run.execution_kind !== 'online'"
                  link
                  type="primary"
                  size="small"
                  :title="t('feedback.regression.history.actions')"
                  :loading="exportingRunId === run.run_id"
                  :disabled="!!exportingRunId"
                  @click="downloadRun(run)"
                >
                  {{ t('feedback.regression.downloadAgain') }}
                </el-button>
              </div>
              <div class="run-id">
                <em>{{ t('feedback.regression.history.runId') }}</em>
                <code class="run-id-value">{{ run.run_id }}</code>
              </div>
              <div class="run-facts">
                <span>
                  <em>{{ t('feedback.regression.history.mode') }}</em>
                  {{ t(regressionModeKey(run.requested_mode)) }}
                </span>
                <span>
                  <em>{{ t('feedback.regression.history.state') }}</em>
                  <el-tag size="small" effect="light" :type="stateTagType(run)">{{ t(runStateKey(run.state)) }}</el-tag>
                </span>
                <span data-testid="regression-run-execution-kind">
                  <em>{{ fj('executionKindLabel') }}</em>
                  {{ t(executionKindKey(run.execution_kind || 'manual')) }}
                </span>
                <span>
                  <em>{{ t('feedback.regression.history.createdAt') }}</em>
                  {{ formatDateTime(run.created_at) }}
                </span>
                <span>
                  <em>{{ t('feedback.regression.history.validation') }}</em>
                  {{ run.validation_level ? t(validationLevelKey(run.validation_level)) : '—' }}
                </span>
                <span>
                  <em>{{ t('feedback.regression.history.result') }}</em>
                  {{ runResult(run) }}
                </span>
                <span v-if="run.imported_at">
                  <em>{{ t('feedback.regression.history.importedAt') }}</em>
                  {{ formatDateTime(run.imported_at) }}
                </span>
              </div>
              <p class="run-outcome">{{ t(describeRunOutcome(run)) }}</p>
              <div v-if="run.failures && run.failures.length" class="run-failures">
                <span>{{ t('feedback.regression.history.failures') }}:</span>
                <code v-for="(code, index) in run.failures" :key="`${run.run_id}-${index}`">
                  {{ failureLabel(code) }}
                </code>
              </div>
            </li>
          </ul>
        </section>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { Download, Refresh } from '@element-plus/icons-vue'
import {
  feedbackRegressionApi,
  type FeedbackRegression,
  type FeedbackRegressionCause,
  type FeedbackRegressionMode,
  type FeedbackRegressionRun,
  type FeedbackStatus
} from '@/api'
import { formatDateTime, formatNumber } from '@/i18n/format'
import { useFeedbackRegressionJobsI18n } from '@/i18n/messages/feedbackRegressionJobs'
import { createLatestGuard } from '@/utils/usage'
import { downloadJsonl } from '@/utils/feedback'
import {
  MAX_REGRESSION_NOTE_LENGTH,
  MAX_REPORT_BYTES,
  REGRESSION_CAUSES,
  REGRESSION_MODES,
  buildEnrollPayload,
  buildPreparePayload,
  canImport,
  describeRunOutcome,
  executionKindKey,
  failureLabel,
  newPrepareRequestId,
  regressionCauseKey,
  regressionModeKey,
  runStateKey,
  unavailableReasonKey,
  validateReportFile,
  validationLevelKey
} from '@/utils/feedbackRegression'
import FeedbackRegressionJobsPanel from '@/components/FeedbackRegressionJobsPanel.vue'

const props = defineProps<{
  feedbackId: string
  revision: number
  status: FeedbackStatus
}>()

const emit = defineEmits<{
  (event: 'changed'): void
}>()

const { t } = useI18n()
const { fj } = useFeedbackRegressionJobsI18n()

const HISTORY_PAGE_SIZE = 10

const regression = ref<FeedbackRegression | null>(null)
const loaded = ref(false)
const loading = ref(false)
const loadError = ref('')
const regressionGuard = createLatestGuard()
let regressionController: AbortController | null = null
let loadGeneration = 0

const historyItems = ref<FeedbackRegressionRun[]>([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyLoading = ref(false)
const historyError = ref('')
const historyGuard = createLatestGuard()
let historyController: AbortController | null = null

const enrollCause = ref<FeedbackRegressionCause | ''>('')
const enrollNote = ref('')
const enrolling = ref(false)
const enrollError = ref('')
let enrollController: AbortController | null = null

const prepareMode = ref<FeedbackRegressionMode>('offline')
const preparing = ref(false)
const prepareError = ref('')
const cliHint = ref('')
const preparedFilename = ref('')
let pendingPrepareRequestId: string | null = null
let prepareController: AbortController | null = null

const exportingRunId = ref('')
let exportController: AbortController | null = null

const fileInput = ref<HTMLInputElement | null>(null)
const selectedFile = ref<File | null>(null)
const fileError = ref('')
const importing = ref(false)
const importError = ref('')
let importController: AbortController | null = null

const isVerified = computed(() => props.status === 'verified')
const busy = computed(() => enrolling.value || preparing.value || importing.value)
/** 在线作业活动期间后端会拒绝人工准备；面板已知时必须直接禁用。 */
const onlineActive = ref(false)
const jobsPanel = ref<InstanceType<typeof FeedbackRegressionJobsPanel> | null>(null)

/** 未纳入时后端不会返回 available，只能按反馈状态给出本地阻断原因。 */
const unavailableReason = computed<string | null>(() => {
  if (!isVerified.value) return 'feedback_not_verified'
  const current = regression.value
  if (current && !current.available) return current.unavailable_reason || 'unknown'
  return null
})

const unavailableText = computed(() => {
  const reason = unavailableReason.value
  if (!reason) return ''
  // 未知 reason 走通用回退，并原样显示稳定码，绝不空白或抛错。
  return t(unavailableReasonKey(reason), { reason })
})

const canEnroll = computed(() =>
  loaded.value
  && !regression.value
  && isVerified.value
  && !unavailableReason.value
  && !busy.value
)

const canPrepare = computed(() =>
  !!regression.value?.available && !busy.value && !onlineActive.value
)
const currentRun = computed(() => regression.value?.current_run ?? null)
const importReady = computed(() => canImport(currentRun.value))

function serverMessage(error: any): string {
  const data = error?.response?.data
  if (data && typeof data === 'object' && typeof data.message === 'string' && data.message.trim()) {
    return data.message.trim()
  }
  return typeof error?.message === 'string' ? error.message.trim() : ''
}

function isNetworkError(error: any): boolean {
  return error?.code === 'ERR_NETWORK' || error?.code === 'ECONNABORTED' || error?.code === 'ETIMEDOUT'
}

function formatFileSize(size: number): string {
  if (!Number.isFinite(size) || size < 0) return '—'
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(2)} MB`
}

/** 下载失败不能把已成功的准备/导出响应当作整体失败，调用方仍需刷新状态。 */
function tryDownload(filename: string, jsonl: string): boolean {
  try {
    return downloadJsonl(filename, jsonl)
  } catch {
    return false
  }
}

function runResult(run: FeedbackRegressionRun): string {
  if (run.execution_kind === 'online') {
    if (run.job_status === 'succeeded') {
      if (run.passed === true) return t('feedback.regression.history.passed')
      if (run.passed === false) return t('feedback.regression.history.failed')
      return '—'
    }
    if (run.job_status === 'failed') return fj('status.failed')
    if (run.job_status === 'cancelled') return fj('status.cancelled')
    return '—'
  }
  if (run.passed === true) return t('feedback.regression.history.passed')
  if (run.passed === false) return t('feedback.regression.history.failed')
  return '—'
}

function stateTagType(run: FeedbackRegressionRun): 'success' | 'warning' | 'info' | 'danger' {
  if (run.execution_kind === 'online') {
    if (run.job_status === 'succeeded') return 'success'
    if (run.job_status === 'failed') return 'danger'
    if (run.job_status === 'running') return 'warning'
    return 'info'
  }
  if (run.state === 'imported') return 'success'
  if (run.state === 'stale') return 'info'
  return 'warning'
}

function resetPanelState() {
  regression.value = null
  onlineActive.value = false
  loaded.value = false
  loading.value = false
  loadError.value = ''
  historyItems.value = []
  historyTotal.value = 0
  historyPage.value = 1
  historyLoading.value = false
  historyError.value = ''
  enrollCause.value = ''
  enrollNote.value = ''
  enrolling.value = false
  enrollError.value = ''
  prepareMode.value = 'offline'
  preparing.value = false
  prepareError.value = ''
  cliHint.value = ''
  preparedFilename.value = ''
  pendingPrepareRequestId = null
  exportingRunId.value = ''
  selectedFile.value = null
  fileError.value = ''
  importing.value = false
  importError.value = ''
  if (fileInput.value) fileInput.value.value = ''
}

function abortOperations() {
  regressionController?.abort()
  regressionController = null
  historyController?.abort()
  historyController = null
  enrollController?.abort()
  enrollController = null
  prepareController?.abort()
  prepareController = null
  exportController?.abort()
  exportController = null
  importController?.abort()
  importController = null
}

/**
 * undefined 表示请求被取消/已过期/失败，调用方不得据此清空当前状态；
 * null 才表示后端明确返回“尚未纳入回归”。
 */
async function loadRegression(): Promise<FeedbackRegression | null | undefined> {
  if (!props.feedbackId) return undefined
  const token = regressionGuard.begin()
  regressionController?.abort()
  const controller = new AbortController()
  regressionController = controller
  loading.value = true
  loadError.value = ''
  try {
    const { data } = await feedbackRegressionApi.get(props.feedbackId, controller.signal)
    if (controller.signal.aborted || !regressionGuard.isCurrent(token)) return undefined
    regression.value = data ?? null
    loaded.value = true
    return regression.value
  } catch (error: any) {
    if (controller.signal.aborted || !regressionGuard.isCurrent(token)) return undefined
    loadError.value = serverMessage(error) || t('feedback.regression.errors.loadFailed')
    return undefined
  } finally {
    if (regressionGuard.isCurrent(token)) {
      loading.value = false
      if (regressionController === controller) regressionController = null
    }
  }
}

async function loadHistory(page: number) {
  if (!props.feedbackId || !regression.value) {
    historyItems.value = []
    historyTotal.value = 0
    return
  }
  const token = historyGuard.begin()
  historyController?.abort()
  const controller = new AbortController()
  historyController = controller
  historyLoading.value = true
  historyError.value = ''
  try {
    const { data } = await feedbackRegressionApi.history(
      props.feedbackId,
      { page, page_size: HISTORY_PAGE_SIZE },
      controller.signal
    )
    if (controller.signal.aborted || !historyGuard.isCurrent(token)) return
    const items = Array.isArray(data?.items) ? data.items : []
    historyItems.value = [...items].sort((left, right) => right.attempt_no - left.attempt_no)
    historyTotal.value = typeof data?.total === 'number' && data.total >= 0
      ? data.total
      : items.length
    historyPage.value = page
  } catch (error: any) {
    if (controller.signal.aborted || !historyGuard.isCurrent(token)) return
    historyError.value = serverMessage(error) || t('feedback.regression.errors.historyFailed')
  } finally {
    if (historyGuard.isCurrent(token)) {
      historyLoading.value = false
      if (historyController === controller) historyController = null
    }
  }
}

async function loadAll(page: number = historyPage.value) {
  const id = props.feedbackId
  const generation = ++loadGeneration
  const current = await loadRegression()
  if (id !== props.feedbackId || generation !== loadGeneration || current === undefined) return
  if (!current) {
    historyItems.value = []
    historyTotal.value = 0
    historyPage.value = 1
    return
  }
  await loadHistory(page)
}

function refresh() {
  if (loading.value || busy.value) return
  jobsPanel.value?.refreshNow()
  void loadAll(historyPage.value)
}

/** 在线作业创建/结束/取消/核验都会改变当前轮次与回归 revision，需要重读。 */
function onOnlineChanged() {
  emit('changed')
  void loadAll(1)
}

function onOnlineActiveChange(active: boolean) {
  onlineActive.value = active
}

function onHistoryPageChange(page: number) {
  if (historyLoading.value) return
  void loadHistory(page)
}

async function enroll() {
  if (regression.value || enrolling.value || !isVerified.value) return
  const result = buildEnrollPayload(props.revision, enrollCause.value, enrollNote.value)
  if (!result.payload) {
    enrollError.value = result.errorKey
      ? t(result.errorKey, { max: formatNumber(MAX_REGRESSION_NOTE_LENGTH) })
      : t('feedback.regression.errors.unexpected')
    return
  }
  const id = props.feedbackId
  const controller = new AbortController()
  enrollController = controller
  enrolling.value = true
  enrollError.value = ''
  try {
    const { data } = await feedbackRegressionApi.enroll(id, result.payload, controller.signal)
    if (controller.signal.aborted || id !== props.feedbackId) return
    regression.value = data
    loaded.value = true
    enrollCause.value = ''
    enrollNote.value = ''
    ElMessage.success(t('feedback.regression.enrollSuccess'))
    await loadAll(1)
    if (id !== props.feedbackId) return
    emit('changed')
  } catch (error: any) {
    if (controller.signal.aborted || id !== props.feedbackId) return
    if (error?.response?.status === 409) {
      enrollError.value = t('feedback.regression.errors.enrollConflict')
      await loadAll(1)
    } else {
      enrollError.value = serverMessage(error) || t('feedback.regression.errors.enrollFailed')
    }
  } finally {
    if (enrollController === controller) enrollController = null
    if (id === props.feedbackId) enrolling.value = false
  }
}

async function prepare() {
  const current = regression.value
  if (!current || preparing.value || !current.available) return
  const requestId = pendingPrepareRequestId ?? newPrepareRequestId()
  const result = buildPreparePayload(current.revision, prepareMode.value, requestId)
  if (!result.payload) {
    prepareError.value = result.errorKey
      ? t(result.errorKey)
      : t('feedback.regression.errors.unexpected')
    return
  }
  pendingPrepareRequestId = result.payload.prepare_request_id
  const id = props.feedbackId
  const controller = new AbortController()
  prepareController = controller
  preparing.value = true
  prepareError.value = ''
  try {
    const { data } = await feedbackRegressionApi.prepare(id, result.payload, controller.signal)
    if (controller.signal.aborted || id !== props.feedbackId) return
    pendingPrepareRequestId = null
    cliHint.value = data.cli_hint
    preparedFilename.value = data.filename
    const downloaded = tryDownload(data.filename, data.jsonl)
    if (!downloaded) {
      prepareError.value = t('feedback.regression.errors.downloadFailed')
      ElMessage.warning(prepareError.value)
    } else {
      ElMessage.success(
        t('feedback.regression.prepareSuccess', { attempt: formatNumber(data.run.attempt_no) }, data.run.attempt_no)
      )
    }
    regression.value = {
      ...current,
      revision: data.regression_revision,
      current_attempt_no: data.run.attempt_no,
      current_run: data.run
    }
    await loadAll(1)
    if (id !== props.feedbackId) return
    emit('changed')
  } catch (error: any) {
    if (controller.signal.aborted || id !== props.feedbackId) return
    if (error?.response?.status === 409) {
      pendingPrepareRequestId = null
      prepareError.value = t('feedback.regression.errors.prepareConflict')
      jobsPanel.value?.refreshNow()
      await loadAll(historyPage.value)
    } else if (isNetworkError(error)) {
      // 网络中断时保留 prepare_request_id：重试同一请求，服务端幂等返回同一轮。
      prepareError.value = serverMessage(error) || t('feedback.regression.errors.prepareFailed')
    } else {
      pendingPrepareRequestId = null
      prepareError.value = serverMessage(error) || t('feedback.regression.errors.prepareFailed')
    }
  } finally {
    if (prepareController === controller) prepareController = null
    if (id === props.feedbackId) preparing.value = false
  }
}

async function downloadRun(run: FeedbackRegressionRun) {
  if (!run.is_current || exportingRunId.value) return
  const id = props.feedbackId
  const controller = new AbortController()
  exportController = controller
  exportingRunId.value = run.run_id
  try {
    const { data } = await feedbackRegressionApi.exportRun(id, run.run_id, controller.signal)
    if (controller.signal.aborted || id !== props.feedbackId) return
    if (!tryDownload(data.filename, data.jsonl)) {
      ElMessage.error(t('feedback.regression.errors.downloadFailed'))
      return
    }
    ElMessage.success(t('feedback.regression.exportSuccess'))
  } catch (error: any) {
    if (controller.signal.aborted || id !== props.feedbackId) return
    ElMessage.error(serverMessage(error) || t('feedback.regression.errors.exportFailed'))
  } finally {
    if (exportController === controller) exportController = null
    if (id === props.feedbackId) exportingRunId.value = ''
  }
}

function chooseReport() {
  if (importing.value || preparing.value) return
  fileInput.value?.click()
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  input.value = ''
  if (!file) return
  const errorKey = validateReportFile(file)
  if (errorKey) {
    selectedFile.value = null
    fileError.value = t(errorKey, { max: formatNumber(MAX_REPORT_BYTES) })
    return
  }
  selectedFile.value = file
  fileError.value = ''
  importError.value = ''
}

async function confirmImport() {
  const current = regression.value
  const run = current?.current_run ?? null
  const file = selectedFile.value
  if (!current || !run || !canImport(run) || !file || importing.value || unavailableReason.value) return
  const id = props.feedbackId
  const controller = new AbortController()
  importController = controller
  importing.value = true
  importError.value = ''
  fileError.value = ''
  try {
    const { data } = await feedbackRegressionApi.importReport(
      id,
      run.run_id,
      current.revision,
      file,
      controller.signal
    )
    if (controller.signal.aborted || id !== props.feedbackId) return
    selectedFile.value = null
    regression.value = {
      ...current,
      revision: data.regression_revision,
      current_run: data.run
    }
    ElMessage.success(t('feedback.regression.importSuccess'))
    await loadAll(historyPage.value)
    if (id !== props.feedbackId) return
    emit('changed')
  } catch (error: any) {
    if (controller.signal.aborted || id !== props.feedbackId) return
    const status = error?.response?.status
    if (status === 409) {
      selectedFile.value = null
      importError.value = t('feedback.regression.import.conflict')
      ElMessage.error(importError.value)
      await loadAll(historyPage.value)
    } else if (status === 413) {
      importError.value = t('feedback.regression.errors.reportTooLarge')
    } else {
      importError.value = serverMessage(error) || t('feedback.regression.errors.reportRejected')
    }
  } finally {
    if (importController === controller) importController = null
    if (id === props.feedbackId) importing.value = false
  }
}

function cancelImport() {
  if (!importing.value) return
  importController?.abort()
  importController = null
  importing.value = false
  ElMessage.info(t('feedback.regression.import.cancelled'))
}

watch(
  () => props.feedbackId,
  () => {
    abortOperations()
    resetPanelState()
    if (props.feedbackId) void loadAll(1)
  },
  { immediate: true }
)

watch(prepareMode, () => {
  pendingPrepareRequestId = null
})

onBeforeUnmount(() => {
  regressionGuard.dispose()
  historyGuard.dispose()
  abortOperations()
})
</script>

<style scoped>
.regression {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-height: 120px;
}

.regression-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.regression-heading h4,
.prepare h5,
.import h5,
.history h5 {
  margin: 0 0 4px;
  color: var(--text);
  font-size: 13.5px;
  font-weight: 600;
}

.regression-desc,
.hint {
  margin: 0;
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.6;
}

.regression-loading {
  color: var(--text-3);
  font-size: 12px;
}

.regression-status {
  margin: 0;
  color: var(--text-2);
  font-size: 12.5px;
}

.enroll,
.prepare,
.import,
.history {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-label {
  color: var(--text-2);
  font-size: 12.5px;
  font-weight: 550;
}

.cause-select {
  width: min(260px, 100%);
}

.actions,
.prepare-row,
.import-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.prepare-row {
  justify-content: flex-start;
}

.cli-hint pre {
  margin: 6px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text-2);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 11.5px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.regression-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px 14px;
  margin: 0;
}

.meta-cell dt {
  margin: 0 0 2px;
  color: var(--text-3);
  font-size: 11.5px;
}

.meta-cell dd {
  margin: 0;
  color: var(--text-2);
  font-size: 12.5px;
}

.meta-wide {
  grid-column: 1 / -1;
}

.pre-wrap {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.mono {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 12px;
}

.file-input {
  display: none;
}

.selected-file {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}

.file-name {
  color: var(--text);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.meta-item {
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.field-error {
  margin: 0;
  color: var(--danger);
  font-size: 11.5px;
}

.history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.run-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.run-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}

.run-item.current {
  border-color: var(--primary-border, var(--line));
}

.run-top {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
}

.run-attempt {
  color: var(--text);
  font-size: 12.5px;
  font-weight: 600;
}

.run-time {
  color: var(--text-3);
  font-size: 11.5px;
}

.run-id {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 8px;
  min-width: 0;
  max-width: 100%;
  color: var(--text-2);
  font-size: 11.5px;
}

.run-id em {
  flex: 0 0 auto;
  color: var(--text-3);
  font-style: normal;
}

.run-id-value {
  min-width: 0;
  max-width: 100%;
  color: var(--text-2);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 11px;
  overflow-wrap: anywhere;
  word-break: break-word;
  user-select: text;
}

.run-facts {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 14px;
  color: var(--text-2);
  font-size: 11.5px;
}

.run-facts em {
  margin-right: 3px;
  color: var(--text-3);
  font-style: normal;
}

.run-outcome {
  margin: 0;
  color: var(--text-2);
  font-size: 12px;
}

.run-failures {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 8px;
  color: var(--text-3);
  font-size: 11.5px;
}

.run-failures code {
  padding: 1px 5px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface-soft);
  color: var(--danger);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 11px;
  overflow-wrap: anywhere;
}

.is-empty {
  color: var(--text-3);
  font-size: 12px;
}

.online-block {
  padding: 6px 9px;
  border: 1px solid var(--warning-border, var(--line));
  border-radius: 8px;
  background: var(--warning-soft, var(--surface-soft));
  color: var(--warning);
}

@media (max-width: 700px) {
  .regression-meta {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
