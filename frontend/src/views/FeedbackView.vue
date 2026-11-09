<template>
  <div class="page feedback-page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">{{ t('feedback.admin.eyebrow') }}</span>
        <h2>{{ t('feedback.admin.title') }}</h2>
        <p>{{ t('feedback.admin.subtitle') }}</p>
      </div>
      <div class="page-actions">
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>{{ t('feedback.admin.refresh') }}
        </el-button>
        <el-button type="primary" plain :loading="exportingAll" :disabled="exportingAll" @click="exportAll">
          <el-icon><Download /></el-icon>{{ exportingAll ? t('feedback.admin.exporting') : t('feedback.admin.exportAll') }}
        </el-button>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          {{ t('feedback.admin.listTitle') }}
          <span class="count-badge">{{ formatNumber(total) }}</span>
        </div>
        <span class="muted">{{ t('feedback.admin.listHint') }}</span>
      </div>

      <div class="filters">
        <el-select
          v-model="statusFilter"
          class="filter-select"
          :placeholder="t('feedback.admin.allStatus')"
          :aria-label="t('feedback.admin.filterStatus')"
          @change="onFilterChange"
        >
          <el-option value="" :label="t('feedback.admin.allStatus')" />
          <el-option v-for="status in STATUSES" :key="status" :value="status" :label="t(feedbackStatusKey(status))" />
        </el-select>
        <el-select
          v-model="categoryFilter"
          class="filter-select"
          :placeholder="t('feedback.admin.allCategory')"
          :aria-label="t('feedback.admin.filterCategory')"
          @change="onFilterChange"
        >
          <el-option value="" :label="t('feedback.admin.allCategory')" />
          <el-option v-for="category in CATEGORIES" :key="category" :value="category" :label="t(feedbackCategoryKey(category))" />
        </el-select>
      </div>

      <el-table v-loading="loading" :data="items">
        <el-table-column :label="t('feedback.admin.colId')" width="120">
          <template #default="{ row }">
            <span class="mono" :title="row.id">{{ shortId(row.id) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('feedback.admin.colCategory')" min-width="150">
          <template #default="{ row }">
            <el-tag size="small" effect="light">{{ t(feedbackCategoryKey(row.category)) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('feedback.admin.colStatus')" width="120">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small" effect="light">{{ t(feedbackStatusKey(row.status)) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('feedback.admin.colNote')" width="100">
          <template #default="{ row }">
            <span :class="{ 'is-empty': !row.note_present }">
              {{ row.note_present ? t('feedback.admin.notePresent') : t('feedback.admin.noteAbsent') }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('feedback.admin.colRevision')" width="90">
          <template #default="{ row }">
            <span class="mono">{{ formatNumber(row.revision) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('feedback.admin.colCreatedAt')" min-width="170">
          <template #default="{ row }">
            <span class="time">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('feedback.admin.colReviewedAt')" min-width="170">
          <template #default="{ row }">
            <span :class="row.reviewed_at ? 'time' : 'is-empty'">{{ row.reviewed_at ? formatDateTime(row.reviewed_at) : '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('feedback.admin.colReviewer')" min-width="130">
          <template #default="{ row }">
            <span :class="{ 'is-empty': !row.reviewer_name }">{{ row.reviewer_name || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">{{ t('feedback.admin.detail') }}</el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty :description="t('feedback.admin.empty')" :image-size="80" />
        </template>
      </el-table>

      <div class="panel-foot">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="load"
        />
      </div>
    </div>

    <el-drawer
      v-model="drawerVisible"
      :title="t('feedback.admin.detailTitle')"
      size="min(720px, 96vw)"
      append-to-body
      destroy-on-close
    >
      <div v-loading="detailLoading" class="detail">
        <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon />
        <template v-if="detail">
          <div class="detail-meta">
            <el-tag size="small" effect="light">{{ t(feedbackCategoryKey(detail.category)) }}</el-tag>
            <el-tag :type="statusTagType(detail.status)" size="small" effect="light">{{ t(feedbackStatusKey(detail.status)) }}</el-tag>
            <span class="meta-item">{{ t('feedback.admin.revision') }} {{ formatNumber(detail.revision) }}</span>
            <span class="meta-item">{{ t('feedback.admin.colCreatedAt') }} {{ formatDateTime(detail.created_at) }}</span>
          </div>

          <el-alert
            v-if="detail.result_stale"
            class="stale-alert"
            :title="t('feedback.admin.staleWarning')"
            type="warning"
            :closable="false"
            show-icon
          />

          <section class="detail-section">
            <h4>{{ t('feedback.admin.question') }}</h4>
            <p class="detail-text">{{ detail.question || t('common.notProvided') }}</p>
          </section>

          <section class="detail-section">
            <h4>{{ t('feedback.admin.answer') }}</h4>
            <pre class="detail-answer">{{ detail.answer }}</pre>
          </section>

          <section class="detail-section">
            <h4>{{ t('feedback.admin.note') }}</h4>
            <p class="detail-text">{{ detail.note || t('common.notProvided') }}</p>
          </section>

          <section class="detail-section">
            <h4>{{ t('feedback.admin.citations') }}</h4>
            <p v-if="!detail.citations.length" class="is-empty">{{ t('feedback.admin.noCitations') }}</p>
            <ul v-else class="citation-list">
              <li v-for="(citation, index) in detail.citations" :key="index" class="citation-item" :class="{ invalid: isInvalidSource(citation) }">
                <div class="citation-head">
                  <span class="citation-name">{{ sourceLabel(citation) }}</span>
                  <span v-if="citation.chunk_index != null" class="meta-item">
                    {{ t('feedback.admin.chunkLabel', { index: formatNumber(citation.chunk_index + 1) }) }}
                  </span>
                  <span v-if="citation.document_id" class="mono citation-doc">{{ citation.document_id }}</span>
                  <el-tag v-if="isInvalidSource(citation)" type="danger" size="small">{{ t('feedback.admin.invalidSource') }}</el-tag>
                </div>
                <p v-if="citation.snippet" class="citation-snippet">{{ citation.snippet }}</p>
              </li>
            </ul>
            <p v-if="detail.invalid_source_ids.length" class="invalid-ids">
              <strong>{{ t('feedback.admin.invalidSources') }}:</strong>
              <span class="mono">{{ detail.invalid_source_ids.join(', ') }}</span>
            </p>
          </section>

          <el-divider />

          <section class="review">
            <h4>{{ t('feedback.admin.reviewTitle') }}</h4>

            <template v-if="canEdit">
              <div class="review-field">
                <span class="field-label">{{ t('feedback.admin.decisionLabel') }}</span>
                <el-radio-group v-model="reviewForm.decision" :disabled="reviewSubmitting">
                  <el-radio value="verified">{{ t('feedback.admin.decisionVerified') }}</el-radio>
                  <el-radio value="rejected">{{ t('feedback.admin.decisionRejected') }}</el-radio>
                </el-radio-group>
              </div>

              <template v-if="reviewForm.decision === 'verified'">
                <div class="review-field">
                  <span class="field-label">{{ t('feedback.admin.outcomeLabel') }}</span>
                  <el-select v-model="reviewForm.correctedOutcome" :placeholder="t('feedback.admin.outcomeLabel')" :disabled="reviewSubmitting" class="review-select">
                    <el-option v-for="outcome in OUTCOMES" :key="outcome" :value="outcome" :label="t(feedbackOutcomeKey(outcome))" />
                  </el-select>
                </div>

                <div class="review-field">
                  <span class="field-label">{{ t('feedback.admin.sourcesLabel') }}</span>
                  <p class="field-hint">{{ t('feedback.admin.sourcesHint') }}</p>
                  <p v-if="!detail.citations.length" class="is-empty">{{ t('feedback.admin.noCitations') }}</p>
                  <el-checkbox-group v-else v-model="reviewForm.selectedCitationIndexes" class="source-options" :disabled="reviewSubmitting">
                    <div v-for="(citation, index) in detail.citations" :key="index" class="source-option" :class="{ invalid: isInvalidSource(citation) }">
                      <el-checkbox :value="index" :disabled="reviewSubmitting">
                        <span class="source-option-name">{{ sourceLabel(citation) }}</span>
                      </el-checkbox>
                      <span v-if="citation.chunk_index != null" class="meta-item">
                        {{ t('feedback.admin.chunkLabel', { index: formatNumber(citation.chunk_index + 1) }) }}
                      </span>
                      <el-tag v-if="isInvalidSource(citation)" type="danger" size="small">{{ t('feedback.admin.invalidSource') }}</el-tag>
                    </div>
                  </el-checkbox-group>
                </div>

                <div class="review-field">
                  <label class="field-label" for="feedback-extra-sources">{{ t('feedback.admin.extraSourcesLabel') }}</label>
                  <el-input
                    id="feedback-extra-sources"
                    v-model="reviewForm.extraSources"
                    type="textarea"
                    :rows="2"
                    resize="none"
                    :disabled="reviewSubmitting"
                    :placeholder="t('feedback.admin.extraSourcesPlaceholder')"
                  />
                </div>

                <div class="review-grid">
                  <div class="review-field">
                    <label class="field-label" for="feedback-required-phrases">{{ t('feedback.admin.requiredPhrasesLabel') }}</label>
                    <el-input
                      id="feedback-required-phrases"
                      v-model="reviewForm.requiredPhrases"
                      type="textarea"
                      :rows="3"
                      resize="none"
                      :disabled="reviewSubmitting"
                      :placeholder="t('feedback.admin.requiredPhrasesPlaceholder')"
                    />
                  </div>
                  <div class="review-field">
                    <label class="field-label" for="feedback-forbidden-phrases">{{ t('feedback.admin.forbiddenPhrasesLabel') }}</label>
                    <el-input
                      id="feedback-forbidden-phrases"
                      v-model="reviewForm.forbiddenPhrases"
                      type="textarea"
                      :rows="3"
                      resize="none"
                      :disabled="reviewSubmitting"
                      :placeholder="t('feedback.admin.forbiddenPhrasesPlaceholder')"
                    />
                  </div>
                </div>

                <div class="review-field">
                  <label class="field-label" for="feedback-unknowns">{{ t('feedback.admin.unknownsLabel') }}</label>
                  <el-input
                    id="feedback-unknowns"
                    v-model="reviewForm.unknowns"
                    type="textarea"
                    :rows="2"
                    resize="none"
                    :disabled="reviewSubmitting"
                    :placeholder="t('feedback.admin.unknownsPlaceholder')"
                  />
                </div>

                <div class="review-field">
                  <label class="field-label" for="feedback-evidence">{{ t('feedback.admin.evidenceLabel') }}</label>
                  <el-input
                    id="feedback-evidence"
                    v-model="reviewForm.evidenceNote"
                    type="textarea"
                    :rows="2"
                    resize="none"
                    :disabled="reviewSubmitting"
                    :placeholder="t('feedback.admin.evidencePlaceholder')"
                  />
                </div>
              </template>

              <div class="review-field">
                <label class="field-label" for="feedback-reviewer-note">{{ t('feedback.admin.reviewerNoteLabel') }}</label>
                <el-input
                  id="feedback-reviewer-note"
                  v-model="reviewForm.reviewerNote"
                  type="textarea"
                  :rows="2"
                  resize="none"
                  :disabled="reviewSubmitting"
                  :placeholder="reviewForm.decision === 'rejected' ? t('feedback.admin.noteRequired') : t('feedback.admin.reviewerNotePlaceholder')"
                />
              </div>

              <el-alert v-if="reviewError" :title="reviewError" type="error" :closable="false" show-icon />

              <div class="review-actions">
                <span v-if="blockingText" class="blocking-hint" role="status">{{ blockingText }}</span>
                <el-button
                  type="primary"
                  :loading="reviewSubmitting"
                  :disabled="reviewSubmitting || !!blockingReason"
                  @click="submitReview"
                >
                  {{ reviewSubmitting ? t('feedback.admin.reviewSubmitting') : t('feedback.admin.reviewSubmit') }}
                </el-button>
              </div>
            </template>

            <template v-else>
              <el-alert :title="t('feedback.admin.alreadyReviewed')" type="info" :closable="false" show-icon />
              <dl class="reviewed-meta">
                <dt>{{ t('feedback.admin.reviewer') }}</dt>
                <dd>{{ detail.reviewer_name || '—' }}</dd>
                <dt>{{ t('feedback.admin.reviewedAt') }}</dt>
                <dd>{{ detail.reviewed_at ? formatDateTime(detail.reviewed_at) : '—' }}</dd>
                <template v-if="detail.corrected_outcome">
                  <dt>{{ t('feedback.admin.outcomeLabel') }}</dt>
                  <dd>{{ outcomeLabel(detail.corrected_outcome) }}</dd>
                </template>
                <template v-if="detail.evidence_note">
                  <dt>{{ t('feedback.admin.evidenceLabel') }}</dt>
                  <dd class="detail-text">{{ detail.evidence_note }}</dd>
                </template>
                <template v-if="detail.reviewer_note">
                  <dt>{{ t('feedback.admin.reviewerNoteLabel') }}</dt>
                  <dd class="detail-text">{{ detail.reviewer_note }}</dd>
                </template>
                <template v-if="detail.expected_sources.length">
                  <dt>{{ t('feedback.admin.savedExpectedSources') }}</dt>
                  <dd>
                    <span v-for="(source, index) in detail.expected_sources" :key="index" class="saved-source">
                      {{ source.filename || source.document_id || t('feedback.admin.untitledSource') }}
                    </span>
                  </dd>
                </template>
              </dl>
            </template>

            <div v-if="isExportableStatus(detail.status)" class="export-row">
              <el-button type="primary" plain :loading="exportingOne" :disabled="exportingOne" @click="exportOne">
                <el-icon><Download /></el-icon>{{ exportingOne ? t('feedback.admin.exporting') : t('feedback.admin.exportOne') }}
              </el-button>
            </div>
          </section>

          <el-divider />

          <FeedbackRegressionPanel
            :feedback-id="detail.id"
            :revision="detail.revision"
            :status="detail.status"
            @changed="onRegressionChanged"
          />
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { Download, Refresh } from '@element-plus/icons-vue'
import FeedbackRegressionPanel from '@/components/FeedbackRegressionPanel.vue'
import {
  feedbackApi,
  type AnswerFeedbackAdminDetail,
  type AnswerFeedbackAdminItem,
  type FeedbackCategory,
  type FeedbackOutcome,
  type FeedbackStatus
} from '@/api'
import { formatDateTime, formatNumber } from '@/i18n/format'
import { createLatestGuard } from '@/utils/usage'
import {
  buildReviewPayload,
  canVerify,
  downloadJsonl,
  feedbackCategoryKey,
  feedbackOutcomeKey,
  feedbackStatusKey,
  isExportableStatus,
  type FeedbackReviewForm
} from '@/utils/feedback'

const { t } = useI18n()

const STATUSES: FeedbackStatus[] = ['pending', 'verified', 'rejected']
const CATEGORIES: FeedbackCategory[] = ['inaccurate_answer', 'citation_error', 'missing_material']
const OUTCOMES: FeedbackOutcome[] = ['answer', 'refuse', 'clarify']

const items = ref<AnswerFeedbackAdminItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const statusFilter = ref<FeedbackStatus | ''>('')
const categoryFilter = ref<FeedbackCategory | ''>('')
const loading = ref(false)
const listError = ref('')
const listGuard = createLatestGuard()
let listController: AbortController | null = null

const drawerVisible = ref(false)
const detail = ref<AnswerFeedbackAdminDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const detailGuard = createLatestGuard()
let detailController: AbortController | null = null

const reviewForm = reactive<FeedbackReviewForm>({
  decision: 'verified',
  correctedOutcome: null,
  selectedCitationIndexes: [],
  extraSources: '',
  requiredPhrases: '',
  forbiddenPhrases: '',
  unknowns: '',
  evidenceNote: '',
  reviewerNote: ''
})
const reviewSubmitting = ref(false)
const reviewError = ref('')
const exportingOne = ref(false)
const exportingAll = ref(false)

const canEdit = computed(() => detail.value?.status === 'pending')
const blockingReason = computed(() => {
  const current = detail.value
  if (!current || current.status !== 'pending') return null
  return canVerify(current, reviewForm)
})
const blockingText = computed(() => blockingReason.value ? t(blockingReason.value) : '')

function outcomeLabel(outcome: FeedbackOutcome | null) {
  return outcome ? t(feedbackOutcomeKey(outcome)) : ''
}

function shortId(id: string) {
  return id.slice(0, 8)
}

function statusTagType(status: FeedbackStatus): 'success' | 'warning' | 'danger' {
  if (status === 'verified') return 'success'
  return status === 'rejected' ? 'danger' : 'warning'
}

function sourceLabel(citation: { document_id?: string | null; filename?: string | null }) {
  return citation.filename || citation.document_id || t('feedback.admin.untitledSource')
}

function isInvalidSource(citation: { document_id?: string | null }) {
  const current = detail.value
  return !!citation.document_id && !!current?.invalid_source_ids.includes(citation.document_id)
}

function resetReviewForm(current: AnswerFeedbackAdminDetail) {
  reviewForm.decision = 'verified'
  reviewForm.correctedOutcome = current.corrected_outcome ?? null
  // 明确从空选择开始：绝不预选原引用，管理员必须逐条确认。
  reviewForm.selectedCitationIndexes = []
  reviewForm.extraSources = ''
  reviewForm.requiredPhrases = current.required_phrases.join('\n')
  reviewForm.forbiddenPhrases = current.forbidden_phrases.join('\n')
  reviewForm.unknowns = current.unknowns.join('\n')
  reviewForm.evidenceNote = current.evidence_note || ''
  reviewForm.reviewerNote = current.reviewer_note || ''
  reviewError.value = ''
}

async function load() {
  const token = listGuard.begin()
  listController?.abort()
  const current = new AbortController()
  listController = current
  loading.value = true
  listError.value = ''
  try {
    const { data } = await feedbackApi.adminList({
      page: page.value,
      page_size: pageSize.value,
      status: statusFilter.value || undefined,
      category: categoryFilter.value || undefined,
      signal: current.signal
    })
    if (current.signal.aborted || !listGuard.isCurrent(token)) return
    items.value = Array.isArray(data?.items) ? data.items : []
    total.value = typeof data?.total === 'number' && data.total >= 0 ? data.total : items.value.length
  } catch (err: any) {
    if (current.signal.aborted || !listGuard.isCurrent(token)) return
    listError.value = err?.message || t('feedback.admin.loadFailed')
    ElMessage.error(listError.value)
  } finally {
    // 只有最新请求允许结束 loading；旧筛选的迟到响应不能覆盖新筛选的状态。
    if (listGuard.isCurrent(token)) loading.value = false
  }
}

function onFilterChange() {
  page.value = 1
  void load()
}

async function loadDetail(id: string) {
  const token = detailGuard.begin()
  detailController?.abort()
  const current = new AbortController()
  detailController = current
  detailLoading.value = true
  detailError.value = ''
  try {
    const { data } = await feedbackApi.adminDetail(id, current.signal)
    if (current.signal.aborted || !detailGuard.isCurrent(token)) return
    detail.value = data
    resetReviewForm(data)
  } catch (err: any) {
    if (current.signal.aborted || !detailGuard.isCurrent(token)) return
    detailError.value = err?.message || t('feedback.admin.detailLoadFailed')
    ElMessage.error(detailError.value)
  } finally {
    if (detailGuard.isCurrent(token)) detailLoading.value = false
  }
}

function openDetail(row: AnswerFeedbackAdminItem) {
  drawerVisible.value = true
  detail.value = null
  detailError.value = ''
  void loadDetail(row.id)
}

async function submitReview() {
  const current = detail.value
  if (!current || current.status !== 'pending' || reviewSubmitting.value || blockingReason.value) return
  reviewSubmitting.value = true
  reviewError.value = ''
  try {
    await feedbackApi.review(current.id, buildReviewPayload(current, reviewForm))
    ElMessage.success(t('feedback.admin.reviewSuccess'))
    await loadDetail(current.id)
    await load()
  } catch (err: any) {
    if (err?.response?.status === 409) {
      // 先重载最新详情（会重置表单），再把冲突提示放回去，避免被重置清掉。
      await loadDetail(current.id)
      reviewError.value = t('feedback.admin.revisionConflict')
      ElMessage.error(reviewError.value)
    } else {
      const serverMessage = typeof err?.response?.data?.message === 'string'
        ? err.response.data.message.trim()
        : ''
      reviewError.value = serverMessage || t('feedback.admin.reviewFailed')
      ElMessage.error(reviewError.value)
    }
  } finally {
    reviewSubmitting.value = false
  }
}

async function download(feedbackId?: string) {
  const { data } = await feedbackApi.export(feedbackId)
  if (!downloadJsonl(data.filename, data.jsonl)) throw new Error(t('feedback.admin.exportFailed'))
  ElMessage.success(t('feedback.admin.exported', { count: formatNumber(data.case_count) }, data.case_count))
}

async function exportOne() {
  const current = detail.value
  if (!current || !isExportableStatus(current.status) || exportingOne.value) return
  exportingOne.value = true
  try {
    await download(current.id)
  } catch {
    ElMessage.error(t('feedback.admin.exportFailed'))
  } finally {
    exportingOne.value = false
  }
}

async function exportAll() {
  if (exportingAll.value) return
  exportingAll.value = true
  try {
    await download()
  } catch {
    ElMessage.error(t('feedback.admin.exportFailed'))
  } finally {
    exportingAll.value = false
  }
}

/**
 * 回归纳入/准备/导入不会修改反馈详情字段（只推进回归版本与轮次）。
 * 因此不调用 loadDetail：它会 resetReviewForm，抹掉管理员正在编辑的核验内容。
 * 只刷新列表；回归面板自己已按接口响应重新加载。
 */
function onRegressionChanged() {
  void load()
}

onMounted(() => {
  void load()
})
onBeforeUnmount(() => {
  listGuard.dispose()
  detailGuard.dispose()
  listController?.abort()
  listController = null
  detailController?.abort()
  detailController = null
})
</script>

<style scoped>
.feedback-page {
  min-width: 0;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  /* 与 .panel-head 的 18px 24px 对齐；不加这段筛选框会贴住面板的上边和左边。 */
  padding: 16px 24px 0;
  margin-bottom: 14px;
}

.filter-select {
  width: 160px;
}

.mono {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  font-size: 12px;
  color: var(--text-2);
}

.is-empty {
  color: var(--text-3);
}

.time {
  color: var(--text-3);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}

.detail {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 160px;
}

.detail-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px 14px;
}

.meta-item {
  color: var(--text-3);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.stale-alert {
  margin-top: 2px;
}

.detail-section h4,
.review h4 {
  margin: 0 0 8px;
  color: var(--text);
  font-size: 13.5px;
  font-weight: 600;
}

.detail-text {
  margin: 0;
  color: var(--text-2);
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.detail-answer {
  margin: 0;
  padding: 12px 14px;
  max-height: 280px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
  color: var(--text-2);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.citation-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.citation-item {
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
}

.citation-item.invalid {
  border-color: var(--danger-border);
  background: var(--danger-soft);
}

.citation-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 12px;
}

.citation-name {
  color: var(--text);
  font-size: 12.5px;
  font-weight: 550;
}

.citation-doc {
  overflow-wrap: anywhere;
}

.citation-snippet {
  margin: 6px 0 0;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
}

.invalid-ids {
  margin: 10px 0 0;
  color: var(--danger);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.review {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.review-field {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.field-label {
  color: var(--text-2);
  font-size: 12.5px;
  font-weight: 550;
}

.field-hint {
  margin: 0;
  color: var(--text-3);
  font-size: 11.5px;
}

.review-select {
  width: min(240px, 100%);
}

.source-options {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.source-option {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
}

.source-option.invalid {
  border-color: var(--danger-border);
}

.source-option-name {
  font-size: 12.5px;
  color: var(--text);
  overflow-wrap: anywhere;
}

.review-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.review-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 10px;
}

.blocking-hint {
  color: var(--warning);
  font-size: 12px;
}

.reviewed-meta {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 7px 14px;
  margin: 0;
  font-size: 12.5px;
}

.reviewed-meta dt {
  color: var(--text-3);
}

.reviewed-meta dd {
  margin: 0;
  color: var(--text-2);
}

.saved-source {
  display: inline-block;
  margin-right: 10px;
  overflow-wrap: anywhere;
}

.export-row {
  display: flex;
  justify-content: flex-end;
  padding-top: 4px;
}

@media (max-width: 700px) {
  .review-grid {
    grid-template-columns: 1fr;
  }

  .filters {
    /* 与移动端 .panel-head 的 16px 内边距一致。 */
    padding: 14px 16px 0;
  }

  .filter-select {
    width: 100%;
  }
}
</style>
