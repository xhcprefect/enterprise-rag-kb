<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="min(1100px, 96vw)"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
    class="cmp-dialog"
    @update:model-value="onVisibilityChange"
    @closed="onClosed"
  >
    <template v-if="doc">
      <el-alert
        v-if="readOnly"
        class="cmp-alert"
        type="info"
        :title="t('documents.comparison.confirm.supersededReadOnly')"
        :closable="false"
        show-icon
      />

      <p class="cmp-intro">{{ t('documents.comparison.intro') }}</p>

      <!-- 候选版本 -->
      <section class="cmp-section" :aria-label="t('documents.comparison.candidate.label')">
        <h3 class="cmp-section-title">{{ t('documents.comparison.candidate.label') }}</h3>
        <el-input
          v-model="candidateSearchInput"
          :placeholder="t('documents.comparison.candidate.searchPlaceholder')"
          :aria-label="t('documents.comparison.candidate.searchAria')"
          clearable
          :disabled="submitting"
          class="cmp-search"
          @input="onCandidateSearchInput"
          @clear="onCandidateSearchInput"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>

        <div class="cmp-candidate-meta">
          <span>
            {{ t('documents.comparison.candidate.total', { total: formatNumber(candidatesTotal) }) }}
          </span>
          <span>{{ t('documents.comparison.candidate.selfExcluded') }}</span>
        </div>

        <p v-if="candidatesNotice" class="cmp-hint cmp-hint-warn" role="status">
          {{ candidatesNotice }}
        </p>

        <div v-if="loadingCandidates && !candidates.length" class="cmp-status" role="status">
          {{ t('documents.comparison.candidate.loading') }}
        </div>
        <el-alert
          v-else-if="candidatesError"
          class="cmp-alert"
          type="error"
          :title="candidatesError"
          :closable="false"
          show-icon
        >
          <el-button size="small" @click="retryCandidates">
            {{ t('documents.comparison.candidate.retry') }}
          </el-button>
        </el-alert>
        <p v-else-if="!candidates.length" class="cmp-empty">
          {{ t('documents.comparison.candidate.empty') }}
        </p>
        <el-radio-group
          v-else
          v-model="selectedReplacementId"
          class="cmp-candidate-list"
          :disabled="submitting"
        >
          <el-radio
            v-for="candidate in candidates"
            :key="candidate.id"
            :value="candidate.id"
            border
            class="cmp-candidate"
          >
            <span class="cmp-candidate-name">{{ candidate.filename }}</span>
            <span class="cmp-candidate-info">
              {{
                t('documents.comparison.candidate.indexVersion', {
                  version: candidate.index_version
                })
              }}
              ·
              {{
                t('documents.comparison.candidate.chunks', {
                  count: formatNumber(candidate.chunk_count)
                })
              }}
            </span>
          </el-radio>
        </el-radio-group>

        <div v-if="candidatePages > 1" class="cmp-pager">
          <el-pagination
            small
            layout="prev, pager, next"
            :total="candidatesTotal"
            :page-size="CANDIDATE_PAGE_SIZE"
            :current-page="candidatePage"
            :disabled="loadingCandidates || submitting"
            @current-change="onCandidatePageChange"
          />
        </div>
        <el-alert
          v-if="candidatesTotal > CANDIDATE_PAGE_SIZE"
          class="cmp-alert"
          type="info"
          :title="
            t('documents.comparison.candidate.tooMany', { limit: CANDIDATE_PAGE_SIZE })
          "
          :closable="false"
          show-icon
        />
      </section>

      <div class="cmp-compare-action">
        <el-button
          type="primary"
          :loading="loadingComparison"
          :disabled="!selectedReplacementId || submitting"
          @click="startComparison"
        >
          {{
            loadingComparison
              ? t('documents.comparison.comparing')
              : t('documents.comparison.start')
          }}
        </el-button>
        <span v-if="loadingComparison" class="cmp-hint">
          {{ t('documents.comparison.comparing') }}
        </span>
      </div>

      <p v-if="comparisonError" class="cmp-error" role="alert">{{ comparisonError }}</p>

      <template v-if="preview">
        <section class="cmp-section" :aria-label="t('documents.comparison.summary.title')">
          <div class="cmp-doc-grid">
            <div class="cmp-doc-card">
              <h4>{{ t('documents.comparison.originalTitle') }}</h4>
              <p class="cmp-doc-name">{{ preview.original.filename }}</p>
              <p class="cmp-doc-meta">
                {{
                  t('documents.comparison.document.indexVersion', {
                    version: preview.original.index_version
                  })
                }}
                ·
                {{
                  t('documents.comparison.document.chunks', {
                    count: formatNumber(preview.original.chunk_count)
                  })
                }}
                ·
                {{
                  t('documents.comparison.document.chars', {
                    count: formatNumber(preview.original.char_count)
                  })
                }}
              </p>
              <el-tag
                :type="lifecycleType(preview.original.lifecycle_state)"
                size="small"
                effect="light"
              >
                {{ lifecycleText(preview.original.lifecycle_state) }}
              </el-tag>
            </div>
            <div class="cmp-doc-card">
              <h4>{{ t('documents.comparison.replacementTitle') }}</h4>
              <p class="cmp-doc-name">{{ preview.replacement.filename }}</p>
              <p class="cmp-doc-meta">
                {{
                  t('documents.comparison.document.indexVersion', {
                    version: preview.replacement.index_version
                  })
                }}
                ·
                {{
                  t('documents.comparison.document.chunks', {
                    count: formatNumber(preview.replacement.chunk_count)
                  })
                }}
                ·
                {{
                  t('documents.comparison.document.chars', {
                    count: formatNumber(preview.replacement.char_count)
                  })
                }}
              </p>
              <el-tag
                :type="lifecycleType(preview.replacement.lifecycle_state)"
                size="small"
                effect="light"
              >
                {{ lifecycleText(preview.replacement.lifecycle_state) }}
              </el-tag>
            </div>
          </div>
        </section>

        <template v-if="isComparable">
          <section class="cmp-section" :aria-label="t('documents.comparison.summary.title')">
            <div class="cmp-summary-head">
              <h3 class="cmp-section-title">{{ t('documents.comparison.summary.title') }}</h3>
              <span class="cmp-summary-total">
                {{ t('documents.comparison.rows.total', { total: formatNumber(rowsTotal) }) }}
              </span>
            </div>
            <div v-if="preview.summary" class="cmp-summary">
              <span class="cmp-count is-added">
                {{
                  t('documents.comparison.summary.added', {
                    count: formatNumber(preview.summary.added)
                  })
                }}
              </span>
              <span class="cmp-count is-removed">
                {{
                  t('documents.comparison.summary.removed', {
                    count: formatNumber(preview.summary.removed)
                  })
                }}
              </span>
              <span class="cmp-count is-modified">
                {{
                  t('documents.comparison.summary.modified', {
                    count: formatNumber(preview.summary.modified)
                  })
                }}
              </span>
              <span class="cmp-count is-unchanged">
                {{
                  t('documents.comparison.summary.unchanged', {
                    count: formatNumber(preview.summary.unchanged)
                  })
                }}
              </span>
            </div>

            <p v-if="!rowsPresent" class="cmp-empty">
              {{ t('documents.comparison.rows.empty') }}
            </p>
            <template v-else>
              <el-alert
                v-if="!hasDifferences"
                class="cmp-alert"
                type="info"
                :title="t('documents.comparison.noDifferences')"
                :closable="false"
                show-icon
              />
              <div class="cmp-preview-tools">
                <el-switch
                  v-model="onlyChanges"
                  :active-text="t('documents.comparison.onlyChanges')"
                  :disabled="submitting"
                />
                <span class="cmp-hint">
                  {{ t('documents.comparison.rows.total', { total: formatNumber(filteredRows.length) }) }}
                </span>
              </div>

              <p v-if="!filteredRows.length" class="cmp-empty">
                {{ t('documents.comparison.onlyChangesEmpty') }}
              </p>
              <ul v-else class="cmp-rows">
                <li
                  v-for="(row, index) in pagedRows"
                  :key="comparisonRowKey(row, rowOffset + index)"
                  :class="['cmp-row', `is-${row.kind}`]"
                >
                  <div class="cmp-row-head">
                    <el-tag
                      :type="comparisonRowTagType(row.kind)"
                      size="small"
                      effect="light"
                    >
                      {{ t(comparisonRowKindKey(row.kind)) }}
                    </el-tag>
                  </div>
                  <div class="cmp-row-body">
                    <div class="cmp-cell cmp-cell-original">
                      <p v-if="row.original" class="cmp-cell-meta">
                        {{ unitLabel(row.original) }}
                      </p>
                      <pre v-if="row.original">{{ row.original.text }}</pre>
                      <p v-else class="cmp-absent">
                        {{ t('documents.comparison.rows.absent') }}
                      </p>
                    </div>
                    <div class="cmp-cell cmp-cell-replacement">
                      <p v-if="row.replacement" class="cmp-cell-meta">
                        {{ unitLabel(row.replacement) }}
                      </p>
                      <pre v-if="row.replacement">{{ row.replacement.text }}</pre>
                      <p v-else class="cmp-absent">
                        {{ t('documents.comparison.rows.absent') }}
                      </p>
                    </div>
                  </div>
                </li>
              </ul>

              <div v-if="rowPages > 1" class="cmp-pager">
                <el-button
                  size="small"
                  :disabled="previewPage <= 1"
                  @click="previewPage -= 1"
                >
                  {{ t('documents.comparison.rows.prev') }}
                </el-button>
                <span>
                  {{
                    t('documents.comparison.rows.pageOf', {
                      page: previewPage,
                      pages: rowPages
                    })
                  }}
                </span>
                <el-button
                  size="small"
                  :disabled="previewPage >= rowPages"
                  @click="previewPage += 1"
                >
                  {{ t('documents.comparison.rows.next') }}
                </el-button>
              </div>
            </template>
          </section>

          <section class="cmp-section cmp-confirm" :aria-label="t('documents.comparison.confirm.submit')">
            <el-alert
              class="cmp-alert"
              type="warning"
              :title="t('documents.comparison.confirm.warning')"
              :closable="false"
              show-icon
            />
            <el-alert
              v-if="!fingerprintValid"
              class="cmp-alert"
              type="error"
              :title="t('documents.comparison.confirm.fingerprintMissing')"
              :closable="false"
              show-icon
            />
            <el-checkbox
              v-model="acknowledged"
              class="cmp-acknowledge"
              :disabled="readOnly || submitting"
            >
              {{
                t('documents.comparison.confirm.checkbox', {
                  original: doc.filename,
                  replacement: replacementFilename
                })
              }}
            </el-checkbox>
            <p v-if="submitError" class="cmp-error" role="alert">{{ submitError }}</p>
          </section>
        </template>
        <el-alert
          v-else
          class="cmp-alert"
          type="warning"
          :title="t('documents.comparison.notComparable')"
          :closable="false"
          show-icon
        >
          <p class="cmp-reason">{{ t(comparisonReasonKey(preview.reason)) }}</p>
        </el-alert>
      </template>

      <p v-else-if="!loadingComparison" class="cmp-empty">
        {{ t('documents.comparison.previewEmpty') }}
      </p>
    </template>

    <template #footer>
      <div class="cmp-footer">
        <el-button :disabled="submitting" @click="close">
          {{ t('common.cancel') }}
        </el-button>
        <el-button
          type="warning"
          :loading="submitting"
          :disabled="!canSubmit"
          @click="submit"
        >
          {{
            submitting
              ? t('documents.comparison.confirm.submitting')
              : t('documents.comparison.confirm.submit')
          }}
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
  docApi,
  type ComparisonUnit,
  type DocumentComparison,
  type DocumentItem
} from '@/api'
import { formatNumber } from '@/i18n/format'
import {
  createSupersedeController,
  governanceWriteLock,
  isGovernanceConflict,
  isSuperseded,
  lifecycleTagType,
  normalizeLifecycleState,
  toGovernanceSnapshot
} from '@/utils/documentGovernance'
import { sourceLocationLabels } from '@/utils/sourceLocation'
import {
  COMPARISON_CANDIDATE_PAGE_SIZE,
  COMPARISON_PAGE_SIZE,
  availableCandidates,
  buildComparisonSupersedeBody,
  canConfirmComparison,
  comparisonHasDifferences,
  comparisonPageCount,
  comparisonPageSlice,
  comparisonReasonKey,
  comparisonRowKey,
  comparisonRowKindKey,
  comparisonRowTagType,
  filterComparisonRows,
  hasValidComparisonFingerprint,
  invalidateComparison,
  isComparisonCurrent
} from '@/utils/documentComparison'

const props = defineProps<{
  modelValue: boolean
  kbId: string
  /** 被对比的当前文档；superseded_by_id 存在时预选原替代者。 */
  doc: DocumentItem | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  superseded: [doc: DocumentItem]
}>()

const { t } = useI18n()

const CANDIDATE_PAGE_SIZE = COMPARISON_CANDIDATE_PAGE_SIZE
const PAGE_SIZE = COMPARISON_PAGE_SIZE
const SEARCH_DEBOUNCE_MS = 300

const candidates = ref<DocumentItem[]>([])
const candidatesTotal = ref(0)
const candidatePage = ref(1)
const candidateSearchInput = ref('')
const appliedQuery = ref('')
const loadingCandidates = ref(false)
const candidatesError = ref('')
const candidatesNotice = ref('')
const selectedReplacementId = ref('')

const preview = ref<DocumentComparison | null>(null)
const loadingComparison = ref(false)
const comparisonError = ref('')
const onlyChanges = ref(false)
const previewPage = ref(1)
const acknowledged = ref(false)
const submitError = ref('')
// 由 createSupersedeController 回调驱动的真实 busy：提交期间禁用确认与关闭。
const submitting = ref(false)

// 打开会话代次：关闭/切 KB/切文档/重新打开都会 +1，迟到的响应一律丢弃。
let generation = 0
// 候选/选择变化的对比代次：换候选、换搜索页、重新对比都会作废在途 GET。
let comparisonEpoch = 0
let candidateController: AbortController | null = null
let comparisonController: AbortController | null = null
let searchTimer: number | undefined
let preselectedId = ''
let disposed = false

const supersedeController = createSupersedeController((busy) => {
  submitting.value = busy
})

const title = computed(() =>
  t('documents.comparison.title', { name: props.doc?.filename || '' })
)

const readOnly = computed(() => isSuperseded(props.doc))

const candidatePages = computed(() =>
  comparisonPageCount(candidatesTotal.value, CANDIDATE_PAGE_SIZE)
)

const replacementFilename = computed(() => {
  if (preview.value?.replacement?.filename) return preview.value.replacement.filename
  return (
    candidates.value.find((candidate) => candidate.id === selectedReplacementId.value)
      ?.filename || ''
  )
})

const isComparable = computed(
  () => preview.value?.comparable === true && preview.value.reason === 'ok'
)

const rowsTotal = computed(() => preview.value?.rows?.length ?? 0)
const rowsPresent = computed(() => rowsTotal.value > 0)
const hasDifferences = computed(() =>
  comparisonHasDifferences(preview.value?.rows)
)
const filteredRows = computed(() =>
  filterComparisonRows(preview.value?.rows, onlyChanges.value)
)
const rowPages = computed(() =>
  comparisonPageCount(filteredRows.value.length, PAGE_SIZE)
)
const pagedRows = computed(() =>
  comparisonPageSlice(filteredRows.value, previewPage.value, PAGE_SIZE)
)
const rowOffset = computed(() => (previewPage.value - 1) * PAGE_SIZE)
const fingerprintValid = computed(() =>
  hasValidComparisonFingerprint(preview.value?.comparison_fingerprint)
)
const canSubmit = computed(
  () =>
    !readOnly.value &&
    canConfirmComparison(preview.value, acknowledged.value, submitting.value)
)

/** 当前打开会话是否仍指向同一 KB/文档（代次 + 打开状态 + 未卸载）。 */
function isSessionCurrent(session: number, kbId: string, docId: string): boolean {
  return (
    !disposed &&
    generation === session &&
    props.modelValue &&
    props.kbId === kbId &&
    props.doc?.id === docId
  )
}

/** 对比 GET 是否仍属于当前会话、当前候选与当前参数。 */
function isComparisonRequestCurrent(
  session: number,
  epoch: number,
  controller: AbortController,
  kbId: string,
  docId: string,
  replacementId: string
): boolean {
  return (
    !controller.signal.aborted &&
    comparisonController === controller &&
    comparisonEpoch === epoch &&
    isSessionCurrent(session, kbId, docId) &&
    selectedReplacementId.value === replacementId
  )
}

function cancelSearchTimer() {
  if (searchTimer !== undefined) {
    window.clearTimeout(searchTimer)
    searchTimer = undefined
  }
}

/** 清空旧预览、指纹与勾选（候选/搜索/分页变化时立即执行）。 */
function clearPreview() {
  comparisonEpoch += 1
  comparisonController?.abort()
  comparisonController = null
  loadingComparison.value = false
  preview.value = invalidateComparison(preview.value)
  acknowledged.value = false
  comparisonError.value = ''
  submitError.value = ''
  onlyChanges.value = false
  previewPage.value = 1
}

/** 关闭/切 KB/切文档/卸载：作废所有在途请求与提交守卫。 */
function invalidateAll() {
  cancelSearchTimer()
  generation += 1
  candidateController?.abort()
  candidateController = null
  comparisonEpoch += 1
  comparisonController?.abort()
  comparisonController = null
  supersedeController.invalidate()
  loadingCandidates.value = false
  loadingComparison.value = false
  preview.value = invalidateComparison(preview.value)
  acknowledged.value = false
  submitError.value = ''
}

/** 打开/切换文档：重置全部状态，并预选原替代者（加载候选后仍会核对存在）。 */
function openSession() {
  cancelSearchTimer()
  generation += 1
  candidateController?.abort()
  candidateController = null
  comparisonEpoch += 1
  comparisonController?.abort()
  comparisonController = null
  supersedeController.invalidate()
  candidates.value = []
  candidatesTotal.value = 0
  candidatePage.value = 1
  candidateSearchInput.value = ''
  appliedQuery.value = ''
  candidatesError.value = ''
  candidatesNotice.value = ''
  loadingCandidates.value = false
  preview.value = invalidateComparison(preview.value)
  loadingComparison.value = false
  comparisonError.value = ''
  onlyChanges.value = false
  previewPage.value = 1
  acknowledged.value = false
  submitError.value = ''
  preselectedId = props.doc?.superseded_by_id ?? ''
  selectedReplacementId.value = preselectedId
  if (!props.modelValue || !props.doc || !props.kbId) return
  void loadCandidates(generation, props.kbId, props.doc.id, 1, '')
}

async function loadCandidates(
  session: number,
  kbId: string,
  docId: string,
  page: number,
  query: string
) {
  if (!isSessionCurrent(session, kbId, docId)) return
  candidateController?.abort()
  const controller = new AbortController()
  candidateController = controller
  loadingCandidates.value = true
  candidatesError.value = ''
  try {
    const { data } = await docApi.list(kbId, {
      page,
      page_size: CANDIDATE_PAGE_SIZE,
      status: 'ready',
      retrievable_only: true,
      q: query || undefined
    })
    if (!isSessionCurrent(session, kbId, docId) || controller.signal.aborted) return
    const items = Array.isArray(data.items) ? data.items : []
    candidates.value = availableCandidates(items, docId)
    candidatesTotal.value = data.total
    // 预选核对：仅在第一页、无搜索且服务端已完整列出（total≤本页）时，
    // 才能确认原替代者确实不在候选中；分页/搜索期间不能据此清空。
    if (
      preselectedId &&
      selectedReplacementId.value === preselectedId &&
      page === 1 &&
      !query &&
      data.total <= items.length &&
      !items.some((item) => item.id === preselectedId)
    ) {
      selectedReplacementId.value = ''
      candidatesNotice.value = t('documents.comparison.candidate.preselectMissing')
    }
  } catch (err: any) {
    if (isSessionCurrent(session, kbId, docId) && !controller.signal.aborted) {
      candidatesError.value =
        err?.response?.data?.message ||
        t('documents.comparison.candidate.loadFailed')
    }
  } finally {
    if (isSessionCurrent(session, kbId, docId) && candidateController === controller) {
      loadingCandidates.value = false
      candidateController = null
    }
  }
}

function retryCandidates() {
  if (!props.modelValue || !props.doc || !props.kbId) return
  void loadCandidates(
    generation,
    props.kbId,
    props.doc.id,
    candidatePage.value,
    appliedQuery.value
  )
}

function onCandidateSearchInput() {
  cancelSearchTimer()
  searchTimer = window.setTimeout(() => {
    searchTimer = undefined
    if (!props.modelValue || !props.doc || !props.kbId || disposed) return
    appliedQuery.value = candidateSearchInput.value.trim()
    candidatePage.value = 1
    clearPreview()
    void loadCandidates(generation, props.kbId, props.doc.id, 1, appliedQuery.value)
  }, SEARCH_DEBOUNCE_MS)
}

function onCandidatePageChange(page: number) {
  if (!props.modelValue || !props.doc || !props.kbId || disposed) return
  candidatePage.value = page
  clearPreview()
  void loadCandidates(generation, props.kbId, props.doc.id, page, appliedQuery.value)
}

async function startComparison() {
  if (!props.modelValue || !props.doc || !props.kbId) return
  const replacementId = selectedReplacementId.value.trim()
  if (!replacementId) return
  const kbId = props.kbId
  const docId = props.doc.id
  const session = generation
  // 立即作废上一次在途对比及其预览/勾选，再开始新一轮。
  clearPreview()
  const epoch = comparisonEpoch
  const controller = new AbortController()
  comparisonController = controller
  loadingComparison.value = true
  comparisonError.value = ''
  try {
    const { data } = await docApi.comparison(kbId, docId, replacementId, controller.signal)
    if (!isComparisonRequestCurrent(session, epoch, controller, kbId, docId, replacementId)) return
    if (!isComparisonCurrent(data, { kbId, docId, replacementId })) {
      comparisonError.value = t('documents.comparison.identityMismatch')
      return
    }
    preview.value = data
    previewPage.value = 1
  } catch (err: any) {
    if (
      isComparisonRequestCurrent(session, epoch, controller, kbId, docId, replacementId) &&
      !controller.signal.aborted
    ) {
      comparisonError.value =
        err?.response?.data?.message || t('documents.comparison.loadFailed')
    }
  } finally {
    if (
      isComparisonRequestCurrent(session, epoch, controller, kbId, docId, replacementId) &&
      comparisonController === controller
    ) {
      loadingComparison.value = false
      comparisonController = null
    }
  }
}

/** 对比单元的位置说明：分块/行号 + 已验证的位置字段（缺失不猜）。 */
function unitLabel(unit: ComparisonUnit): string {
  const parts: string[] = []
  if (Number.isInteger(unit.chunk_index) && unit.chunk_index >= 0) {
    parts.push(t('documents.comparison.rows.chunk', { index: unit.chunk_index }))
  }
  if (Number.isInteger(unit.line_index) && unit.line_index >= 0) {
    parts.push(t('documents.comparison.rows.line', { index: unit.line_index }))
  }
  const labels = sourceLocationLabels(unit.location || {}, t)
  if (labels.length) parts.push(labels.join(' · '))
  return parts.join(' · ')
}

function lifecycleText(state: string | null | undefined): string {
  return t(`documents.lifecycle.${normalizeLifecycleState(state)}`)
}

function lifecycleType(state: string | null | undefined) {
  return lifecycleTagType(normalizeLifecycleState(state))
}

function planMessage(status: string): string {
  if (status === 'invalidRevision') {
    return t('documents.comparison.confirm.revisionUnknown')
  }
  if (status === 'invalidFingerprint') {
    return t('documents.comparison.confirm.invalidFingerprint')
  }
  return t('documents.comparison.confirm.invalidReplacement')
}

/**
 * 提交替代：带快照 revision 与对比指纹；成功后先核对响应身份再通知父组件。
 * 409（预览过期/CAS 冲突）清空预览与勾选，要求用户重新对比，绝不自动重试。
 */
async function submit() {
  if (!props.modelValue || !props.doc || !props.kbId || !preview.value) return
  if (readOnly.value || supersedeController.busy) return
  if (!canSubmit.value) return
  const kbId = props.kbId
  const docId = props.doc.id
  const replacementId = selectedReplacementId.value.trim()
  // expected_revision 必须来自本次预览的 original 快照：父列表行数据不会因外部
  // 治理更新而刷新，用 props.doc 会把旧 revision 和新指纹拼在一起导致永久 409。
  if (preview.value.original.id !== docId) {
    submitError.value = t('documents.comparison.identityMismatch')
    return
  }
  const snapshot = toGovernanceSnapshot(preview.value.original)
  if (!snapshot.revision_known) {
    submitError.value = t('documents.comparison.confirm.revisionUnknown')
    return
  }
  const plan = buildComparisonSupersedeBody(
    snapshot.governance_revision,
    replacementId,
    preview.value.comparison_fingerprint,
    docId
  )
  if (plan.status !== 'ok') {
    submitError.value = planMessage(plan.status)
    return
  }
  // 同一文档的治理写入互斥：治理保存/重启用/另一入口替代都不并发。
  if (!governanceWriteLock.acquire(kbId, docId)) {
    submitError.value = t('documents.comparison.confirm.busy')
    return
  }
  submitError.value = ''
  const session = generation
  const requestEpoch = supersedeController.begin()
  const isCurrent = () =>
    !disposed &&
    supersedeController.epoch === requestEpoch &&
    isSessionCurrent(session, kbId, docId) &&
    selectedReplacementId.value === replacementId
  try {
    let data: DocumentItem
    try {
      ;({ data } = await docApi.supersede(kbId, docId, plan.body))
    } catch (err: any) {
      if (!isCurrent()) return
      if (isGovernanceConflict(err)) {
        // 预览过期/CAS 冲突：旧指纹立即作废，必须重新对比。
        preview.value = invalidateComparison(preview.value)
        acknowledged.value = false
        submitError.value = ''
        // 预览已清空，冲突提示必须挂在预览区之外，确保用户仍能看到。
        comparisonError.value = t('documents.comparison.confirm.conflict')
        return
      }
      submitError.value =
        err?.response?.data?.message || t('documents.comparison.confirm.failed')
      return
    }
    // 成功响应必须先核对 ID/KB：错响应不提示成功、不关窗、不通知父组件。
    if (!isCurrent() || data?.id !== docId || data?.kb_id !== kbId) return
    emit('superseded', data)
    emit('update:modelValue', false)
  } finally {
    governanceWriteLock.release(kbId, docId)
    supersedeController.finish(requestEpoch)
  }
}

function close() {
  if (submitting.value) return
  invalidateAll()
  emit('update:modelValue', false)
}

function beforeClose(done: () => void) {
  if (submitting.value) {
    ElMessage.warning(t('documents.comparison.confirm.closeBlocked'))
    return
  }
  // 弹窗真正关闭前就作废，关闭动画期间迟到的结果不得落地。
  invalidateAll()
  done()
}

function onVisibilityChange(value: boolean) {
  if (!value) invalidateAll()
  emit('update:modelValue', value)
}

function onClosed() {
  if (props.modelValue) return
  invalidateAll()
}

// 换候选：旧预览、旧指纹与勾选立即失效。
watch(selectedReplacementId, () => {
  if (disposed) return
  clearPreview()
}, { flush: 'sync' })

// “只看变化”切换：回到第一页。
watch(onlyChanges, () => {
  previewPage.value = 1
}, { flush: 'sync' })

// 打开/切 KB/切文档/重新打开：同步作废在途请求并按新会话重建。
watch(
  [() => props.modelValue, () => props.kbId, () => props.doc?.id],
  () => {
    if (disposed) return
    if (props.modelValue && props.doc && props.kbId) openSession()
    else invalidateAll()
  },
  { immediate: true, flush: 'sync' }
)

onBeforeUnmount(() => {
  disposed = true
  invalidateAll()
})
</script>

<style scoped>
.cmp-dialog :deep(.el-dialog__body) {
  max-height: 72vh;
  overflow-y: auto;
}

.cmp-intro {
  margin: 0 0 12px;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.7;
}

.cmp-alert {
  margin: 8px 0;
}

.cmp-section {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

.cmp-section-title {
  margin: 0 0 10px;
  color: var(--text);
  font-size: 13px;
  font-weight: 650;
}

.cmp-search {
  width: 100%;
}

.cmp-candidate-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-top: 6px;
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.cmp-hint {
  color: var(--text-3);
  font-size: 11.5px;
  line-height: 1.6;
}

.cmp-hint-warn {
  margin: 6px 0 0;
  color: var(--warning);
}

.cmp-status {
  padding: 20px 0;
  color: var(--text-3);
  font-size: 12px;
  text-align: center;
}

.cmp-empty {
  margin: 8px 0;
  padding: 12px;
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.7;
}

.cmp-candidate-list {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-top: 8px;
  width: 100%;
}

.cmp-candidate {
  display: flex;
  align-items: flex-start;
  margin: 0;
  width: 100%;
  height: auto;
  min-height: 38px;
  padding: 7px 12px;
}

.cmp-candidate :deep(.el-radio__label) {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
  min-width: 0;
}

.cmp-candidate-name {
  color: var(--text);
  font-size: 12.5px;
  font-weight: 550;
  overflow-wrap: anywhere;
}

.cmp-candidate-info {
  color: var(--text-3);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.cmp-pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 10px;
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.cmp-compare-action {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}

.cmp-error {
  margin: 8px 0 0;
  color: var(--danger);
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.cmp-doc-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.cmp-doc-card {
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
  min-width: 0;
}

.cmp-doc-card h4 {
  margin: 0 0 6px;
  color: var(--text-3);
  font-size: 11px;
  font-weight: 600;
}

.cmp-doc-name {
  margin: 0 0 4px;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.cmp-doc-meta {
  margin: 0 0 6px;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.6;
  font-variant-numeric: tabular-nums;
}

.cmp-summary-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.cmp-summary-total {
  color: var(--text-3);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.cmp-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 4px 0 8px;
}

.cmp-count {
  padding: 2px 9px;
  border: 1px solid var(--line);
  border-radius: 6px;
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.cmp-count.is-added {
  border-color: var(--success-border);
  background: var(--success-soft);
  color: var(--success);
}

.cmp-count.is-removed {
  border-color: var(--danger-border);
  background: var(--danger-soft);
  color: var(--danger);
}

.cmp-count.is-modified {
  border-color: var(--warning-border);
  background: var(--warning-soft);
  color: var(--warning);
}

.cmp-count.is-unchanged {
  color: var(--text-3);
}

.cmp-preview-tools {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 8px 0;
}

.cmp-rows {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 42vh;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}

.cmp-row {
  /* 列表是 flex column + 内部滚动：禁止行被压缩，否则行内容会被 overflow 裁成空条。 */
  flex: 0 0 auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  overflow: hidden;
}

.cmp-row.is-added {
  border-color: var(--success-border);
}

.cmp-row.is-removed {
  border-color: var(--danger-border);
}

.cmp-row.is-modified {
  border-color: var(--warning-border);
}

.cmp-row-head {
  padding: 5px 10px;
  border-bottom: 1px solid var(--line);
  background: var(--surface-soft);
}

.cmp-row-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--line);
}

.cmp-cell {
  padding: 8px 11px;
  background: var(--surface);
  min-width: 0;
}

.cmp-row.is-added .cmp-cell-replacement {
  background: var(--success-soft);
}

.cmp-row.is-removed .cmp-cell-original {
  background: var(--danger-soft);
}

.cmp-row.is-modified .cmp-cell {
  background: var(--warning-soft);
}

.cmp-cell-meta {
  margin: 0 0 4px;
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.5;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.cmp-cell pre {
  margin: 0;
  color: var(--text);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.cmp-absent {
  margin: 0;
  color: var(--text-3);
  font-size: 11.5px;
}

.cmp-confirm {
  padding-bottom: 4px;
}

.cmp-acknowledge {
  display: flex;
  align-items: flex-start;
  margin-top: 6px;
  height: auto;
}

.cmp-acknowledge :deep(.el-checkbox__label) {
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.7;
  white-space: normal;
}

.cmp-reason {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.6;
}

.cmp-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

@media (max-width: 720px) {
  .cmp-doc-grid,
  .cmp-row-body {
    grid-template-columns: 1fr;
  }

  .cmp-footer .el-button {
    flex: 1;
    margin-left: 0;
  }
}
</style>
