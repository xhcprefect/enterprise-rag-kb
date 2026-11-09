<template>
  <el-dialog
    :model-value="modelValue"
    :title="t('knowledge.diagnostics.title')"
    width="min(900px, 96vw)"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
    @update:model-value="onVisibilityChange"
    @closed="onClosed"
  >
    <p class="diag-intro">{{ t('knowledge.diagnostics.intro') }}</p>
    <el-alert
      class="diag-warning"
      type="warning"
      :title="t('knowledge.diagnostics.costWarning')"
      :closable="false"
      show-icon
    />

    <el-form label-position="top" class="diag-form">
      <el-form-item :label="t('knowledge.diagnostics.question')">
        <el-input
          v-model="question"
          type="textarea"
          :rows="3"
          resize="none"
          :maxlength="QUESTION_MAX"
          show-word-limit
          :placeholder="t('knowledge.diagnostics.questionPlaceholder')"
          :disabled="running"
        />
      </el-form-item>
      <el-form-item :label="t('knowledge.diagnostics.documents')">
        <el-select
          v-model="selectedDocIds"
          multiple
          filterable
          :multiple-limit="MAX_DOCUMENTS"
          :placeholder="t('knowledge.diagnostics.documentsPlaceholder')"
          :disabled="running || loadingDocuments"
          :loading="loadingDocuments"
          class="diag-docs"
        >
          <el-option
            v-for="doc in documents"
            :key="doc.id"
            :label="doc.filename"
            :value="doc.id"
          />
        </el-select>
        <span v-if="loadingDocuments" class="diag-hint">
          {{ t('knowledge.diagnostics.documentsLoading') }}
        </span>
        <p v-else-if="documentsError" class="diag-inline-error" role="alert">
          {{ documentsError }}
          <el-button link type="primary" size="small" @click="retryDocuments">
            {{ t('knowledge.diagnostics.retry') }}
          </el-button>
        </p>
        <span v-else-if="!documents.length" class="diag-hint">
          {{ t('knowledge.diagnostics.documentsEmpty') }}
        </span>
        <el-alert
          v-if="documentsTruncated"
          class="diag-truncated"
          type="info"
          :title="
            t('knowledge.diagnostics.documentsTruncated', {
              limit: DOCUMENT_LIMIT,
              total: formatNumber(documentsTotal)
            })
          "
          :closable="false"
        />
      </el-form-item>
    </el-form>

    <p v-if="formError" class="diag-inline-error" role="alert">{{ formError }}</p>
    <el-alert
      v-if="runError"
      class="diag-alert"
      type="error"
      :title="runError"
      :closable="false"
      show-icon
    />

    <div v-if="result" class="diag-result">
      <div class="diag-result-head">
        <h3>{{ t('knowledge.diagnostics.resultTitle') }}</h3>
        <el-tag
          :type="result.status === 'ok' ? 'success' : 'danger'"
          size="small"
          effect="light"
        >
          {{
            result.status === 'ok'
              ? t('knowledge.diagnostics.status.ok')
              : t('knowledge.diagnostics.status.error')
          }}
        </el-tag>
      </div>

      <el-alert
        v-if="result.status === 'error'"
        class="diag-alert"
        type="error"
        :title="t('knowledge.diagnostics.status.error')"
        :closable="false"
        show-icon
      >
        <p v-if="result.error_type" class="diag-error-type">
          {{ t('knowledge.diagnostics.errorType') }}: {{ result.error_type }}
        </p>
      </el-alert>

      <el-alert
        class="diag-answer-note"
        type="info"
        :title="t('knowledge.diagnostics.noAnswer')"
        :closable="false"
        show-icon
      />

      <p class="diag-question">
        {{ t('knowledge.diagnostics.questionEcho', { question: result.question }) }}
      </p>
      <p class="diag-meta">
        {{ t('knowledge.diagnostics.collection', { name: result.collection_name }) }}
      </p>
      <p class="diag-meta">
        {{
          t('knowledge.diagnostics.kbStats', {
            total: formatNumber(result.kb.total_documents),
            ready: formatNumber(result.kb.ready_documents),
            retrievable: formatNumber(result.kb.retrievable_documents)
          })
        }}
      </p>
      <div class="diag-scope">
        <span class="diag-label">{{ t('knowledge.diagnostics.scopeTitle') }}</span>
        <span v-if="!result.scope.length">{{ t('knowledge.diagnostics.scopeAll') }}</span>
        <span v-else class="diag-scope-list">
          <el-tag
            v-for="item in result.scope"
            :key="item.document_id"
            size="small"
            effect="plain"
          >
            {{
              t('knowledge.diagnostics.scopeItem', {
                name: item.filename,
                version: item.index_version
              })
            }}
          </el-tag>
        </span>
      </div>

      <section class="diag-section">
        <h3>{{ t('knowledge.diagnostics.config.title') }}</h3>
        <dl class="diag-config">
          <div>
            <dt>{{ t('knowledge.diagnostics.config.topK') }}</dt>
            <dd>{{ result.config.top_k }}</dd>
          </div>
          <div>
            <dt>{{ t('knowledge.diagnostics.config.minSimilarity') }}</dt>
            <dd>{{ result.config.min_similarity }}</dd>
          </div>
          <div>
            <dt>{{ t('knowledge.diagnostics.config.maxPerDoc') }}</dt>
            <dd>{{ result.config.max_per_doc }}</dd>
          </div>
          <div>
            <dt>{{ t('knowledge.diagnostics.config.hybrid') }}</dt>
            <dd>{{ enabledLabel(result.config.hybrid_retrieval_enabled) }}</dd>
          </div>
          <div>
            <dt>{{ t('knowledge.diagnostics.config.keywordCandidates') }}</dt>
            <dd>{{ result.config.keyword_candidates }}</dd>
          </div>
          <div>
            <dt>{{ t('knowledge.diagnostics.config.rerank') }}</dt>
            <dd>{{ enabledLabel(result.config.rerank_enabled) }}</dd>
          </div>
          <div>
            <dt>{{ t('knowledge.diagnostics.config.neighbors') }}</dt>
            <dd>{{ result.config.neighbor_chunks }}</dd>
          </div>
          <div>
            <dt>{{ t('knowledge.diagnostics.totalMs') }}</dt>
            <dd>{{ t('knowledge.diagnostics.stages.ms', { count: result.total_ms }) }}</dd>
          </div>
        </dl>
      </section>

      <section class="diag-section">
        <h3>{{ t('knowledge.diagnostics.stages.title') }}</h3>
        <div class="diag-stage-head">
          <span>{{ t('knowledge.diagnostics.stages.name') }}</span>
          <span>{{ t('knowledge.diagnostics.stages.status') }}</span>
          <span>{{ t('knowledge.diagnostics.stages.msLabel') }}</span>
          <span>{{ t('knowledge.diagnostics.stages.candidates') }}</span>
        </div>
        <div v-for="stage in result.stages" :key="stage.name" class="diag-stage-row">
          <span class="diag-stage-name">{{ t(diagnosticsStageNameKey(stage.name)) }}</span>
          <el-tag :type="diagnosticsStageTagType(stage.status)" size="small" effect="light">
            {{ t(diagnosticsStageStatusKey(stage.status)) }}
          </el-tag>
          <span class="diag-num">
            {{ t('knowledge.diagnostics.stages.ms', { count: stage.ms }) }}
          </span>
          <span class="diag-num">{{ formatNumber(stage.candidates) }}</span>
          <p v-if="stage.error_type || stage.reason" class="diag-stage-note">
            <span v-if="stage.error_type">
              {{ t('knowledge.diagnostics.stages.errorType') }}: {{ stage.error_type }}
            </span>
            <span v-if="stage.reason">
              {{ t(diagnosticsStageReasonKey(stage.reason)) }}
            </span>
          </p>
        </div>
      </section>

      <section class="diag-section">
        <h3>{{ t('knowledge.diagnostics.candidates.title') }}</h3>
        <p v-if="noCandidates" class="diag-empty">
          {{ t('knowledge.diagnostics.candidates.empty') }}
        </p>
        <ul v-else class="diag-candidate-list">
          <li
            v-for="candidate in result.candidates"
            :key="`${candidate.rank}-${candidate.document_id ?? ''}-${candidate.chunk_index ?? ''}`"
            class="diag-candidate"
          >
            <header class="diag-candidate-head">
              <span class="diag-rank">
                {{ t('knowledge.diagnostics.candidates.rank', { rank: candidate.rank }) }}
              </span>
              <span class="diag-filename">
                {{ candidate.filename || t('knowledge.diagnostics.candidates.unknownFilename') }}
              </span>
              <el-tag
                :type="diagnosticsMethodTagType(candidate.retrieval_method)"
                size="small"
                effect="light"
              >
                {{ t(diagnosticsMethodKey(candidate.retrieval_method)) }}
              </el-tag>
            </header>
            <p class="diag-candidate-meta">
              <span v-if="candidateChunkLabel(candidate)">{{ candidateChunkLabel(candidate) }}</span>
              <span>{{ candidateLocation(candidate) }}</span>
              <span v-if="candidate.index_version !== null">
                {{
                  t('knowledge.diagnostics.candidates.indexVersion', {
                    version: candidate.index_version
                  })
                }}
              </span>
            </p>
            <p class="diag-candidate-scores">
              <span
                v-for="score in diagnosticsScoreEntries(candidate.scores)"
                :key="score.key"
                class="diag-score"
              >
                {{ score.labelKey ? t(score.labelKey) : score.key }}:
                {{ diagnosticsScoreText(score.value) }}
              </span>
            </p>
            <p class="diag-candidate-state">
              <span>
                {{ t('knowledge.diagnostics.candidates.lifecycle') }}:
                {{ t(diagnosticsLifecycleKey(candidate.lifecycle_state)) }}
              </span>
              <span>
                {{ t('knowledge.diagnostics.candidates.retrievable') }}:
                {{ t(diagnosticsRetrievableKey(candidate.retrievable)) }}
              </span>
            </p>
            <pre>{{ candidate.excerpt }}</pre>
            <p v-if="candidate.excerpt_truncated" class="diag-hint">
              {{
                t('knowledge.diagnostics.candidates.truncated', {
                  count: formatNumber(candidate.excerpt_chars)
                })
              }}
            </p>
          </li>
        </ul>
      </section>
    </div>

    <template #footer>
      <div class="diag-footer">
        <el-button :disabled="running" @click="close">{{ t('common.close') }}</el-button>
        <el-button type="primary" :loading="running" :disabled="!canRun" @click="run">
          {{ running ? t('knowledge.diagnostics.running') : t('knowledge.diagnostics.run') }}
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import {
  diagnosticsApi,
  docApi,
  type DocumentItem,
  type KnowledgeBase,
  type RetrievalDiagnosticsCandidate,
  type RetrievalDiagnosticsResult
} from '@/api'
import { formatNumber } from '@/i18n/format'
import { sourceLocationLabels } from '@/utils/sourceLocation'
import {
  DIAGNOSTICS_DOCUMENT_LIMIT,
  DIAGNOSTICS_MAX_DOCUMENTS,
  DIAGNOSTICS_QUESTION_MAX,
  buildDiagnosticsRequest,
  diagnosticsChunkRange,
  diagnosticsLifecycleKey,
  diagnosticsMethodKey,
  diagnosticsMethodTagType,
  diagnosticsRetrievableKey,
  diagnosticsScoreEntries,
  diagnosticsScoreText,
  diagnosticsStageNameKey,
  diagnosticsStageReasonKey,
  diagnosticsStageStatusKey,
  diagnosticsStageTagType
} from '@/utils/retrievalDiagnostics'

const props = defineProps<{
  modelValue: boolean
  kb: KnowledgeBase | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const { t } = useI18n()

const MAX_DOCUMENTS = DIAGNOSTICS_MAX_DOCUMENTS
const QUESTION_MAX = DIAGNOSTICS_QUESTION_MAX
const DOCUMENT_LIMIT = DIAGNOSTICS_DOCUMENT_LIMIT

const question = ref('')
const selectedDocIds = ref<string[]>([])
const documents = ref<DocumentItem[]>([])
const documentsTotal = ref(0)
const loadingDocuments = ref(false)
const documentsError = ref('')
const running = ref(false)
const result = ref<RetrievalDiagnosticsResult | null>(null)
const formError = ref('')
const runError = ref('')

// 打开会话代次：关闭/切 KB/重开都会 +1，迟到的文档列表或诊断结果一律丢弃。
let generation = 0
let runController: AbortController | null = null
let disposed = false

const documentsTruncated = computed(() => documentsTotal.value > DOCUMENT_LIMIT)
const canRun = computed(
  () =>
    !running.value &&
    question.value.trim().length > 0 &&
    question.value.trim().length <= QUESTION_MAX
)
const noCandidates = computed(
  () =>
    result.value?.status === 'ok' &&
    result.value.candidates.length === 0
)

function isCurrent(session: number, kbId: string): boolean {
  return (
    !disposed &&
    generation === session &&
    props.modelValue &&
    props.kb?.id === kbId
  )
}

function invalidate() {
  generation += 1
  runController?.abort()
  runController = null
  running.value = false
  loadingDocuments.value = false
}

async function loadDocuments(session = generation, kbId = props.kb?.id) {
  if (!kbId) return
  loadingDocuments.value = true
  documentsError.value = ''
  try {
    const { data } = await docApi.list(kbId, {
      page: 1,
      page_size: DOCUMENT_LIMIT,
      status: 'ready',
      retrievable_only: true
    })
    if (!isCurrent(session, kbId)) return
    documents.value = data.items
    documentsTotal.value = data.total
    // 只保留仍在当前列表中的选择：超出前 100 份的旧选择无法核对。
    const available = new Set(data.items.map((doc) => doc.id))
    selectedDocIds.value = selectedDocIds.value.filter((id) => available.has(id))
  } catch (err: any) {
    if (isCurrent(session, kbId)) {
      documentsError.value = err?.message || t('knowledge.diagnostics.documentsFailed')
    }
  } finally {
    if (isCurrent(session, kbId)) loadingDocuments.value = false
  }
}

function retryDocuments() {
  if (!props.kb || loadingDocuments.value) return
  void loadDocuments(generation, props.kb.id)
}

function openSession() {
  invalidate()
  if (!props.modelValue || !props.kb) return
  const kbId = props.kb.id
  const session = generation
  question.value = ''
  selectedDocIds.value = []
  documents.value = []
  documentsTotal.value = 0
  documentsError.value = ''
  result.value = null
  formError.value = ''
  runError.value = ''
  void loadDocuments(session, kbId)
}

function enabledLabel(value: boolean): string {
  return value
    ? t('knowledge.diagnostics.config.enabled')
    : t('knowledge.diagnostics.config.disabled')
}

function candidateChunkLabel(candidate: RetrievalDiagnosticsCandidate): string {
  const range = diagnosticsChunkRange(candidate.chunk_start, candidate.chunk_end)
  if (!range) return ''
  return range.start === range.end
    ? t('knowledge.diagnostics.candidates.chunkSingle', { start: range.start })
    : t('knowledge.diagnostics.candidates.chunkRange', {
        start: range.start,
        end: range.end
      })
}

function candidateLocation(candidate: RetrievalDiagnosticsCandidate): string {
  const labels = sourceLocationLabels(candidate.location || {}, t)
  return labels.length
    ? labels.join(' · ')
    : t('knowledge.diagnostics.candidates.locationUnknown')
}

async function run() {
  if (!props.kb || running.value) return
  formError.value = ''
  const plan = buildDiagnosticsRequest(question.value, selectedDocIds.value)
  if (plan.status === 'questionEmpty') {
    formError.value = t('knowledge.diagnostics.questionRequired')
    return
  }
  if (plan.status === 'questionTooLong') {
    formError.value = t('knowledge.diagnostics.questionTooLong', { max: QUESTION_MAX })
    return
  }
  if (plan.status === 'tooManyDocuments') {
    formError.value = t('knowledge.diagnostics.documentLimit', { max: MAX_DOCUMENTS })
    return
  }

  const kbId = props.kb.id
  const session = generation
  const controller = new AbortController()
  runController = controller
  running.value = true
  result.value = null
  runError.value = ''
  try {
    const { data } = await diagnosticsApi.run(kbId, plan.body, controller.signal)
    if (!isCurrent(session, kbId) || controller.signal.aborted) return
    if (data.kb_id && data.kb_id !== kbId) {
      runError.value = t('knowledge.diagnostics.runFailed')
      return
    }
    result.value = data
  } catch (err: any) {
    if (isCurrent(session, kbId) && !controller.signal.aborted) {
      runError.value = err?.message || t('knowledge.diagnostics.runFailed')
    }
  } finally {
    if (isCurrent(session, kbId) && runController === controller) {
      running.value = false
      runController = null
    }
  }
}

function close() {
  if (running.value) return
  invalidate()
  emit('update:modelValue', false)
}

function beforeClose(done: () => void) {
  if (running.value) {
    ElMessage.warning(t('knowledge.diagnostics.closeBlocked'))
    return
  }
  invalidate()
  done()
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
  [() => props.modelValue, () => props.kb?.id],
  () => {
    if (disposed) return
    if (props.modelValue && props.kb) openSession()
    else invalidate()
  },
  { immediate: true, flush: 'sync' }
)

// 用户修改输入后，旧的校验错误不再适用。
watch([question, selectedDocIds], () => {
  formError.value = ''
}, { deep: true })

onBeforeUnmount(() => {
  disposed = true
  invalidate()
})
</script>

<style scoped>
.diag-intro {
  margin: 0 0 8px;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.7;
}

.diag-warning {
  margin-bottom: 14px;
}

.diag-form {
  margin: 0;
}

.diag-form :deep(.el-form-item) {
  margin-bottom: 14px;
}

.diag-docs {
  width: 100%;
}

.diag-hint {
  display: block;
  margin-top: 4px;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.6;
}

.diag-inline-error {
  margin: 6px 0 0;
  color: var(--danger);
  font-size: 12px;
  line-height: 1.6;
}

.diag-truncated {
  margin-top: 8px;
}

.diag-alert {
  margin: 8px 0;
}

.diag-answer-note {
  margin: 8px 0 12px;
}

.diag-error-type {
  margin: 4px 0 0;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.diag-result {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--line);
}

.diag-result-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.diag-result-head h3 {
  margin: 0;
  color: var(--text);
  font-size: 14px;
  font-weight: 650;
}

.diag-question {
  margin: 8px 0 4px;
  color: var(--text);
  font-size: 13px;
  font-weight: 550;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.diag-meta {
  margin: 2px 0;
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.6;
  font-variant-numeric: tabular-nums;
}

.diag-scope {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 8px;
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.8;
}

.diag-label {
  flex-shrink: 0;
  color: var(--text-2);
  font-weight: 550;
}

.diag-scope-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}

.diag-section {
  margin-top: 18px;
}

.diag-section h3 {
  margin: 0 0 8px;
  color: var(--text);
  font-size: 13px;
  font-weight: 650;
}

.diag-config {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 8px;
  margin: 0;
}

.diag-config > div {
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--surface-soft);
}

.diag-config dt {
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.6;
}

.diag-config dd {
  margin: 2px 0 0;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.diag-stage-head,
.diag-stage-row {
  display: grid;
  grid-template-columns: minmax(110px, 1.2fr) 80px 90px 80px;
  align-items: center;
  gap: 8px;
}

.diag-stage-head {
  padding: 0 4px 6px;
  color: var(--text-3);
  font-size: 10.5px;
  border-bottom: 1px solid var(--line);
}

.diag-stage-row {
  padding: 8px 4px;
  border-bottom: 1px solid var(--line);
  color: var(--text-2);
  font-size: 12px;
}

.diag-stage-name {
  color: var(--text);
  font-weight: 500;
}

.diag-num {
  font-variant-numeric: tabular-nums;
}

.diag-stage-note {
  grid-column: 1 / -1;
  margin: 2px 0 0;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.diag-empty {
  margin: 0;
  padding: 12px;
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.7;
}

.diag-candidate-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.diag-candidate {
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
}

.diag-candidate-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.diag-rank {
  color: var(--brand-dark);
  font-size: 12px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}

.diag-filename {
  min-width: 0;
  flex: 1;
  color: var(--text);
  font-size: 12.5px;
  font-weight: 550;
  overflow-wrap: anywhere;
}

.diag-candidate-meta,
.diag-candidate-state {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin: 6px 0 0;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.6;
  font-variant-numeric: tabular-nums;
}

.diag-candidate-scores {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin: 6px 0 0;
  color: var(--text-2);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.diag-score {
  padding: 1px 7px;
  border: 1px solid var(--accent-border);
  border-radius: 5px;
  background: var(--brand-tint);
}

.diag-candidate pre {
  margin: 8px 0 0;
  color: var(--text);
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.diag-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

@media (max-width: 640px) {
  .diag-stage-head,
  .diag-stage-row {
    grid-template-columns: minmax(90px, 1.2fr) 72px 80px 64px;
    font-size: 11px;
  }

  .diag-footer .el-button {
    flex: 1;
    margin-left: 0;
  }
}
</style>
