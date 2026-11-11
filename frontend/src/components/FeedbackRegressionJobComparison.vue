<template>
  <section class="job-comparison" data-testid="regression-job-comparison">
    <div v-if="loading" class="comparison-loading" role="status">
      {{ fj('loading') }}
    </div>

    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      show-icon
      data-testid="comparison-error"
    />
    <div v-if="error" class="comparison-error-actions">
      <p class="hint">{{ fj('comparison.hiddenAfterFailure') }}</p>
      <el-button size="small" plain :loading="loading" data-testid="comparison-reload" @click="load">
        <el-icon><Refresh /></el-icon>{{ fj('comparison.reload') }}
      </el-button>
    </div>

    <template v-if="data">
      <section class="binding-block">
        <div class="section-head">
          <h4>{{ fj('binding.title') }}</h4>
          <el-tag :type="reviewTagType" size="small" effect="light" data-testid="comparison-review-status">
            {{ fj(reviewStatusKey(data.job.review.status)) }}
          </el-tag>
        </div>
        <dl class="binding-grid">
          <div class="binding-cell">
            <dt>{{ fj('binding.jobRevision') }}</dt>
            <dd class="mono" data-testid="binding-job-revision">{{ formatNumber(data.job.revision) }}</dd>
          </div>
          <div class="binding-cell">
            <dt>{{ fj('binding.runId') }}</dt>
            <dd class="mono break" data-testid="binding-run-id">{{ data.run.run_id }}</dd>
          </div>
          <div class="binding-cell binding-wide">
            <dt>{{ fj('binding.resultSha') }}</dt>
            <dd class="mono break" data-testid="binding-result-sha">{{ data.new.result_sha256 }}</dd>
          </div>
          <div class="binding-cell binding-wide">
            <dt>{{ fj('binding.machineSha') }}</dt>
            <dd class="mono break">{{ data.new.machine_sha256 || '—' }}</dd>
          </div>
          <div class="binding-cell binding-wide">
            <dt>{{ fj('binding.bindingSha') }}</dt>
            <dd class="mono break">{{ data.binding.binding_sha256 }}</dd>
          </div>
          <div class="binding-cell">
            <dt>{{ fj('binding.datasetSha') }}</dt>
            <dd class="mono break">{{ data.binding.dataset_sha256 }}</dd>
          </div>
          <div class="binding-cell">
            <dt>{{ fj('binding.caseSha') }}</dt>
            <dd class="mono break">{{ data.binding.case_sha256 }}</dd>
          </div>
          <div class="binding-cell">
            <dt>{{ fj('binding.rootSha') }}</dt>
            <dd class="mono break">{{ data.binding.root_sha256 }}</dd>
          </div>
        </dl>
      </section>

      <section class="machine-block" data-testid="comparison-machine">
        <div class="section-head">
          <h4>{{ fj('machine.title') }}</h4>
          <span class="machine-flag" :class="data.new.passed ? 'pass' : 'fail'">
            <el-icon>
              <CircleCheck v-if="data.new.passed" />
              <CircleClose v-else />
            </el-icon>
            {{ data.new.passed ? fj('machine.passed') : fj('machine.failed') }}
          </span>
        </div>
        <dl class="machine-grid">
          <div>
            <dt>{{ fj('machine.outcome') }}</dt>
            <dd>{{ data.new.outcome || '—' }}</dd>
          </div>
          <div>
            <dt>{{ fj('machine.httpStatus') }}</dt>
            <dd>{{ data.new.http_status != null ? formatNumber(data.new.http_status) : '—' }}</dd>
          </div>
          <div>
            <dt>{{ fj('machine.citations') }}</dt>
            <dd>{{ formatNumber(data.new.citation_count) }}</dd>
          </div>
        </dl>
        <div v-if="data.new.failures.length" class="failure-list" data-testid="comparison-failures">
          <span class="field-label">{{ fj('machine.failures') }}</span>
          <code v-for="(code, index) in data.new.failures" :key="`${code}-${index}`">{{ code }}</code>
        </div>
        <p v-else class="hint">{{ fj('machine.noFailures') }}</p>
      </section>

      <div class="answer-columns">
        <section class="answer-block" data-testid="comparison-old">
          <h4>{{ fj('comparison.oldTitle') }}</h4>
          <p class="answer-meta">
            {{ fj('comparison.oldMeta', { messageId: data.old.message_id, conversationId: data.old.conversation_id }) }}
          </p>
          <p class="answer-meta break">
            <span>{{ fj('binding.resultSha') }}:</span>
            <code>{{ data.old.result_sha256 }}</code>
          </p>
          <pre class="answer-body" data-testid="comparison-old-answer">{{ data.old.answer }}</pre>
          <div class="citation-section" data-testid="comparison-old-citations">
            <h5>{{ fj('machine.citations') }}</h5>
            <p v-if="!data.old.citations.length" class="hint">{{ fj('comparison.noCitations') }}</p>
            <ul v-else class="citation-list">
              <li
                v-for="(citation, index) in data.old.citations"
                :key="`old-${index}`"
                class="citation-item"
              >
                <div class="citation-head">
                  <strong class="break">{{ citationTitle(citation, index) }}</strong>
                  <span v-if="chunkLabel(citation)" class="citation-chip">{{ chunkLabel(citation) }}</span>
                </div>
                <dl v-if="citationEntries(citation).length" class="citation-meta">
                  <div v-for="entry in citationEntries(citation)" :key="entry.label">
                    <dt>{{ entry.label }}</dt>
                    <dd class="break">{{ entry.value }}</dd>
                  </div>
                </dl>
                <p v-if="citationSnippet(citation)" class="citation-snippet">{{ citationSnippet(citation) }}</p>
              </li>
            </ul>
          </div>
        </section>

        <section class="answer-block" data-testid="comparison-new">
          <h4>{{ fj('comparison.newTitle') }}</h4>
          <p class="answer-meta">
            {{ fj('comparison.newMeta', { count: formatNumber(data.new.citation_count) }) }}
          </p>
          <pre class="answer-body" data-testid="comparison-new-answer">{{ data.new.answer }}</pre>
          <div class="citation-section" data-testid="comparison-new-citations">
            <h5>{{ fj('machine.citations') }}</h5>
            <p v-if="!data.new.citations.length" class="hint">{{ fj('comparison.noCitations') }}</p>
            <ul v-else class="citation-list">
              <li
                v-for="(citation, index) in data.new.citations"
                :key="`new-${index}`"
                class="citation-item"
              >
                <div class="citation-head">
                  <strong class="break">{{ citationTitle(citation, index) }}</strong>
                  <span v-if="chunkLabel(citation)" class="citation-chip">{{ chunkLabel(citation) }}</span>
                </div>
                <dl v-if="citationEntries(citation).length" class="citation-meta">
                  <div v-for="entry in citationEntries(citation)" :key="entry.label">
                    <dt>{{ entry.label }}</dt>
                    <dd class="break">{{ entry.value }}</dd>
                  </div>
                </dl>
                <p v-if="citationSnippet(citation)" class="citation-snippet">{{ citationSnippet(citation) }}</p>
              </li>
            </ul>
          </div>
        </section>
      </div>

      <p class="hint trace-note">{{ fj('comparison.traceNotShown') }}</p>

      <section class="review-block" data-testid="regression-job-review">
        <h4>{{ fj('review.title') }}</h4>
        <p class="hint">{{ fj('review.hint') }}</p>

        <dl class="current-review">
          <div>
            <dt>{{ fj('review.currentReview') }}</dt>
            <dd>{{ fj(reviewStatusKey(data.job.review.status)) }}</dd>
          </div>
          <div>
            <dt>{{ fj('review.reviewer') }}</dt>
            <dd>{{ data.job.review.reviewed_by_name || '—' }}</dd>
          </div>
          <div>
            <dt>{{ fj('review.reviewedAt') }}</dt>
            <dd>{{ data.job.review.reviewed_at ? formatDateTime(data.job.review.reviewed_at) : '—' }}</dd>
          </div>
          <div>
            <dt>{{ fj('review.boundResult') }}</dt>
            <dd class="mono break">
              {{ data.job.review.result_sha256 || fj('review.unknownBound') }}
            </dd>
          </div>
        </dl>
        <p v-if="data.job.review.notes" class="review-notes">{{ data.job.review.notes }}</p>

        <el-alert
          v-if="reviewError"
          :title="reviewError"
          type="error"
          :closable="false"
          show-icon
          data-testid="review-error"
        />
        <el-alert
          v-if="reviewMessage"
          :title="reviewMessage"
          type="success"
          :closable="false"
          show-icon
          data-testid="review-success"
        />

        <div class="review-field">
          <span class="field-label">{{ fj('review.statusLabel') }}</span>
          <el-radio-group v-model="reviewDecision" :disabled="reviewSubmitting" data-testid="review-decision">
            <el-radio value="verified">{{ fj('reviewStatus.verified') }}</el-radio>
            <el-radio value="rejected">{{ fj('reviewStatus.rejected') }}</el-radio>
            <el-radio value="unknown">{{ fj('reviewStatus.unknown') }}</el-radio>
          </el-radio-group>
        </div>
        <div class="review-field">
          <label class="field-label" for="regression-review-notes">{{ fj('review.notesLabel') }}</label>
          <el-input
            id="regression-review-notes"
            v-model="reviewNotes"
            type="textarea"
            :rows="3"
            resize="none"
            :maxlength="MAX_REVIEW_NOTES"
            show-word-limit
            :disabled="reviewSubmitting"
            :placeholder="fj('review.notesPlaceholder')"
            data-testid="review-notes"
          />
          <span class="hint">{{ fj('review.maxLength', { max: formatNumber(MAX_REVIEW_NOTES) }) }}</span>
        </div>
        <div class="review-actions">
          <el-button
            type="primary"
            :loading="reviewSubmitting"
            :disabled="reviewSubmitting || !canReview"
            data-testid="review-submit"
            @click="submitReview"
          >
            {{ reviewSubmitting ? fj('review.submitting') : fj('review.submit') }}
          </el-button>
        </div>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheck, CircleClose, Refresh } from '@element-plus/icons-vue'
import {
  feedbackRegressionJobsApi,
  type FeedbackRegressionJob,
  type FeedbackRegressionJobComparison,
  type FeedbackRegressionReviewStatus
} from '@/api/feedbackRegressionJobs'
import {
  feedbackJobsHasReason,
  useFeedbackRegressionJobsI18n
} from '@/i18n/messages/feedbackRegressionJobs'
import { formatDateTime, formatNumber } from '@/i18n/format'
import {
  MAX_REVIEW_NOTES,
  buildReviewPayload,
  extractReasonCode,
  isReviewableJob
} from '@/utils/feedbackRegressionJobs'

const props = defineProps<{ jobId: string }>()

const emit = defineEmits<{
  (event: 'updated', job: FeedbackRegressionJob): void
  (event: 'failed'): void
}>()

const { fj } = useFeedbackRegressionJobsI18n()

const data = ref<FeedbackRegressionJobComparison | null>(null)
const loading = ref(false)
const error = ref('')
const comparisonGuard = (() => {
  let current = 0
  let disposed = false
  return {
    begin: () => ++current,
    isCurrent: (token: number) => !disposed && token === current,
    dispose: () => {
      disposed = true
      current += 1
    }
  }
})()
let comparisonController: AbortController | null = null

const reviewDecision = ref<FeedbackRegressionReviewStatus>('unknown')
const reviewNotes = ref('')
const reviewSubmitting = ref(false)
const reviewError = ref('')
const reviewMessage = ref('')
let reviewController: AbortController | null = null

const canReview = computed(() => !!data.value && isReviewableJob(data.value.job))

const reviewTagType = computed<'success' | 'warning' | 'info' | 'danger'>(() => {
  const status = data.value?.job.review.status
  if (status === 'verified') return 'success'
  if (status === 'rejected') return 'danger'
  return 'info'
})

function reviewStatusKey(status: FeedbackRegressionReviewStatus): string {
  return `reviewStatus.${status}`
}

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

function resetReviewForm(current: FeedbackRegressionJobComparison) {
  reviewDecision.value = current.job.review.status
  reviewNotes.value = current.job.review.notes || ''
  reviewError.value = ''
}

function abortComparison() {
  comparisonController?.abort()
  comparisonController = null
}

function resetState() {
  abortComparison()
  reviewController?.abort()
  reviewController = null
  data.value = null
  loading.value = false
  error.value = ''
  reviewDecision.value = 'unknown'
  reviewNotes.value = ''
  reviewSubmitting.value = false
  reviewError.value = ''
  reviewMessage.value = ''
}

async function load(options: { preserveNotice?: boolean } = {}) {
  if (!props.jobId) return
  const token = comparisonGuard.begin()
  abortComparison()
  const controller = new AbortController()
  comparisonController = controller
  // 重新读取前先清空旧正文与核验动作：绝不把缓存成功当作当前授权结果。
  data.value = null
  error.value = ''
  reviewError.value = ''
  if (!options.preserveNotice) reviewMessage.value = ''
  loading.value = true
  try {
    const { data: comparison } = await feedbackRegressionJobsApi.comparison(
      props.jobId,
      controller.signal
    )
    if (controller.signal.aborted || !comparisonGuard.isCurrent(token)) return
    data.value = comparison
    resetReviewForm(comparison)
  } catch (err: any) {
    if (controller.signal.aborted || !comparisonGuard.isCurrent(token)) return
    data.value = null
    error.value = jobErrorMessage(err, 'errors.comparisonFailed')
    emit('failed')
  } finally {
    if (comparisonGuard.isCurrent(token)) {
      loading.value = false
      if (comparisonController === controller) comparisonController = null
    }
  }
}

async function submitReview() {
  const current = data.value
  if (!current || reviewSubmitting.value || !canReview.value) return
  const result = buildReviewPayload(current.job, reviewDecision.value, reviewNotes.value)
  if (!result.payload) {
    reviewError.value = result.errorKey === 'errors.notesTooLong' || result.errorKey === 'errors.notesControlChars'
      ? fj(result.errorKey, { max: formatNumber(MAX_REVIEW_NOTES) })
      : fj(result.errorKey || 'errors.reviewFailed')
    return
  }
  const controller = new AbortController()
  reviewController?.abort()
  reviewController = controller
  reviewSubmitting.value = true
  reviewError.value = ''
  reviewMessage.value = ''
  try {
    const { data: job } = await feedbackRegressionJobsApi.reviewJob(
      current.job.job_id,
      result.payload,
      controller.signal
    )
    if (controller.signal.aborted) return
    emit('updated', job)
    reviewMessage.value = fj('review.success')
    ElMessage.success(reviewMessage.value)
    // 核验成功后 revision 已推进；重新读取比较结果会再次核对当前权限与来源。
    await load({ preserveNotice: true })
  } catch (err: any) {
    if (controller.signal.aborted) return
    // 核验失败（含 409/网络未知）立即清正文与核验动作，绝不自动重放，也不回退缓存。
    data.value = null
    reviewError.value = err?.response?.status === 409
      ? fj('review.conflict')
      : jobErrorMessage(err, 'review.failed')
    // data 清空后 review 区块不再渲染，必须把失败放到顶层错误区，避免静默失败。
    error.value = reviewError.value
    emit('failed')
  } finally {
    if (reviewController === controller) reviewController = null
    reviewSubmitting.value = false
  }
}

function citationRecord(citation: Record<string, unknown>): Record<string, unknown> {
  return citation && typeof citation === 'object' ? citation : {}
}

function citationTitle(citation: Record<string, unknown>, index: number): string {
  const item = citationRecord(citation)
  const filename = typeof item.filename === 'string' ? item.filename : ''
  const path = typeof item.relative_path === 'string' ? item.relative_path : ''
  const documentId = typeof item.document_id === 'string' ? item.document_id : ''
  return filename || path || documentId || fj('comparison.citationLabel', { index: formatNumber(index + 1) })
}

function chunkLabel(citation: Record<string, unknown>): string {
  const value = citationRecord(citation).chunk_index
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
    ? fj('comparison.chunk', { index: formatNumber(value + 1) })
    : ''
}

function citationSnippet(citation: Record<string, unknown>): string {
  const value = citationRecord(citation).snippet
  return typeof value === 'string' ? value : ''
}

function citationEntries(citation: Record<string, unknown>): Array<{ label: string; value: string }> {
  const item = citationRecord(citation)
  const entries: Array<{ label: string; value: string }> = []
  if (typeof item.source_kind === 'string' && item.source_kind) {
    entries.push({ label: fj('comparison.sourceKind'), value: item.source_kind })
  }
  if (typeof item.filename === 'string' && item.filename) {
    entries.push({ label: fj('comparison.filename'), value: item.filename })
  }
  if (typeof item.relative_path === 'string' && item.relative_path) {
    entries.push({ label: fj('comparison.path'), value: item.relative_path })
  }
  if (typeof item.document_id === 'string' && item.document_id) {
    entries.push({ label: fj('comparison.documentId'), value: item.document_id })
  }
  if (typeof item.score === 'number' && Number.isFinite(item.score)) {
    entries.push({ label: fj('comparison.scoreLabel'), value: String(item.score) })
  }
  return entries
}

watch(
  () => props.jobId,
  () => {
    resetState()
    if (props.jobId) void load()
  },
  { immediate: true }
)

onBeforeUnmount(() => {
  comparisonGuard.dispose()
  abortComparison()
  reviewController?.abort()
  reviewController = null
  data.value = null
})
</script>

<style scoped>
.job-comparison {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.comparison-loading {
  color: var(--text-3);
  font-size: 12px;
}

.comparison-error-actions {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

h4,
h5 {
  margin: 0;
  color: var(--text);
  font-size: 13.5px;
  font-weight: 600;
}

h5 {
  font-size: 12.5px;
  margin-bottom: 6px;
}

.hint {
  margin: 0;
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.6;
}

.binding-block,
.machine-block,
.answer-block,
.review-block {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
}

.binding-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px 14px;
  margin: 0;
}

.binding-cell {
  min-width: 0;
}

.binding-wide {
  grid-column: span 2;
}

.binding-cell dt,
.machine-grid dt,
.current-review dt,
.citation-meta dt {
  margin: 0 0 2px;
  color: var(--text-3);
  font-size: 11px;
}

.binding-cell dd,
.machine-grid dd,
.current-review dd,
.citation-meta dd {
  margin: 0;
  color: var(--text-2);
  font-size: 12px;
}

.mono {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 11px;
}

.break {
  overflow-wrap: anywhere;
  word-break: break-word;
}

.machine-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px 14px;
  margin: 0;
}

.machine-flag {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 600;
}

.machine-flag.pass {
  color: var(--success, #2f9e44);
}

.machine-flag.fail {
  color: var(--danger);
}

.failure-list {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px 8px;
}

.failure-list code {
  padding: 1px 5px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface);
  color: var(--danger);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 11px;
  overflow-wrap: anywhere;
}

.field-label {
  color: var(--text-2);
  font-size: 12.5px;
  font-weight: 550;
}

.answer-columns {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
  min-width: 0;
}

.answer-meta {
  margin: 0;
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.6;
}

.answer-meta code {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 11px;
}

.answer-body {
  margin: 0;
  padding: 10px 12px;
  max-height: 320px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text-2);
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.citation-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.citation-item {
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}

.citation-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 8px;
  color: var(--text);
  font-size: 12px;
}

.citation-chip {
  padding: 0 5px;
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--text-3);
  font-size: 10.5px;
}

.citation-meta {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 3px 10px;
  margin: 6px 0 0;
  padding: 0;
}

.citation-meta > div {
  display: contents;
}

.citation-snippet {
  margin: 6px 0 0;
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.trace-note {
  padding: 0 2px;
}

.review-block .current-review {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px 14px;
  margin: 0;
}

.review-notes {
  margin: 0;
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.review-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.review-actions {
  display: flex;
  justify-content: flex-end;
}

@media (max-width: 900px) {
  .answer-columns {
    grid-template-columns: minmax(0, 1fr);
  }

  .binding-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .review-block .current-review {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .binding-grid,
  .machine-grid,
  .review-block .current-review {
    grid-template-columns: minmax(0, 1fr);
  }

  .binding-wide {
    grid-column: auto;
  }
}
</style>
