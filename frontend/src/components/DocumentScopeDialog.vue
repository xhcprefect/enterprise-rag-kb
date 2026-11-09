<template>
  <el-dialog
    :model-value="modelValue"
    :title="t('chat.scope.dialog.title')"
    width="min(620px, 94vw)"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
    @update:model-value="onVisibilityChange"
    @closed="onClosed"
  >
    <p class="scope-description">{{ t('chat.scope.dialog.description', { max }) }}</p>

    <section class="scope-section" :aria-label="t('chat.scope.dialog.selectedTitle')">
      <div class="scope-section-head">
        <strong>{{ t('chat.scope.dialog.selectedTitle') }}</strong>
        <span>{{ t('chat.scope.dialog.selectedCount', { count: selection.length, max }) }}</span>
      </div>
      <p v-if="!selection.length" class="scope-selected-empty">{{ t('chat.scope.dialog.selectedEmpty') }}</p>
      <ul v-else class="scope-picked-list">
        <li v-for="item in selection" :key="item.document_id" class="scope-picked">
          <span class="scope-picked-name" :title="item.filename">{{ item.filename }}</span>
          <button
            type="button"
            class="scope-remove"
            :disabled="saving || confirming"
            :aria-label="t('chat.scope.removeAria', { name: item.filename })"
            @click="removeSelected(item.document_id)"
          >
            <span aria-hidden="true">×</span>
          </button>
        </li>
      </ul>
    </section>

    <section class="scope-section" :aria-label="t('chat.scope.dialog.availableTitle')">
      <div class="scope-section-head">
        <strong>{{ t('chat.scope.dialog.availableTitle') }}</strong>
        <span v-if="total">{{ t('chat.scope.dialog.loadedCount', { loaded: readyDocs.length, total }) }}</span>
      </div>

      <div v-if="loading && !readyDocs.length" class="scope-loading" role="status">{{ t('chat.scope.dialog.loading') }}</div>
      <p v-else-if="loadError" class="scope-load-error">
        <el-alert :title="loadError" type="error" :closable="false" show-icon />
        <el-button size="small" @click="retryLoad">{{ t('chat.scope.dialog.retry') }}</el-button>
      </p>
      <p v-else-if="!readyDocs.length" class="scope-empty">{{ t('chat.scope.dialog.empty') }}</p>
      <ul v-else class="scope-ready-list">
        <li v-for="doc in readyDocs" :key="doc.id">
          <el-checkbox
            class="scope-checkbox"
            :model-value="isSelected(doc.id)"
            :disabled="saving || confirming || (selection.length >= max && !isSelected(doc.id))"
            @change="toggleDoc(doc)"
          >
            <span :title="doc.filename">{{ doc.filename }}</span>
          </el-checkbox>
        </li>
      </ul>

      <p v-if="limitWarning" class="scope-limit" role="alert">{{ t('chat.scope.dialog.limit', { max }) }}</p>
      <div v-if="hasMore" class="scope-load-more">
        <span>{{ t('chat.scope.dialog.loadedCount', { loaded: readyDocs.length, total }) }}</span>
        <el-button size="small" :loading="loading" :disabled="saving || confirming" @click="loadMore">{{ t('chat.scope.dialog.loadMore') }}</el-button>
      </div>
    </section>

    <el-alert v-if="error" class="scope-alert" :title="error" type="error" :closable="false" show-icon />

    <template #footer>
      <div class="scope-dialog-footer">
        <span class="scope-dialog-count">{{ t('chat.scope.dialog.selectedCount', { count: selection.length, max }) }}</span>
        <div class="scope-dialog-buttons">
          <el-button :disabled="saving" @click="close">{{ t('chat.scope.dialog.cancel') }}</el-button>
          <el-button type="primary" :loading="saving || confirming" :disabled="confirming" @click="confirm">{{ t('chat.scope.dialog.confirm') }}</el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { docApi, type DocumentItem } from '@/api'
import {
  MAX_SCOPE_DOCUMENTS,
  scopeItemFromDocument,
  toggleDocumentId,
  type DocumentScopeItem
} from '@/utils/documentScope'
import {
  createScopeConfirmController,
  createScopeDetailFetcher,
  createScopeLoadGuard,
  hasMoreScopePages,
  mergeScopeListPage,
  resolveSelectedIndexVersion,
  type ScopeConfirmOutcome
} from '@/utils/documentList'

const props = defineProps<{
  modelValue: boolean
  kbId: string
  selected: DocumentScopeItem[]
  max: number
  saving: boolean
  error: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: [items: DocumentScopeItem[]]
}>()

const { t } = useI18n()

/** 每页加载数量；分页是为了不悄悄只展示第一页。 */
const PAGE_SIZE = 20

const selection = ref<DocumentScopeItem[]>([])
// 打开时继承、且用户尚未移除的旧选择：沿用原代次；移除后再选才绑定当前代次
const keptSelectionIds = ref<Set<string>>(new Set())
const readyDocs = ref<DocumentItem[]>([])
const page = ref(0)
const total = ref(0)
const lastPageSize = ref(0)
const loading = ref(false)
const loadError = ref('')
const limitWarning = ref(false)
let loadGeneration = 0
// 卸载后所有在途响应都必须作废（不能写入已卸载组件的 ref）
let disposed = false
// 确认前详情复核的代次：关闭/切 KB 后迟到的详情响应不能影响新一次确认
let confirmGeneration = 0
// 本次确认发起的 KB：详情必须属于同一个 KB，切换后迟到的响应一律作废
let confirmKbId = ''
const confirming = ref(false)
// 列表会话身份：重开/切 KB/关闭/卸载时换 token，迟到的旧列表响应作废
const loadGuard = createScopeLoadGuard()
// 详情取数器：绑定发起确认时的 KB，请求前后都校验"仍打开 + 同一 KB"
const fetchScopeDetail = createScopeDetailFetcher({
  kbId: () => confirmKbId,
  isKbCurrent: (kbId: string) =>
    !disposed && props.modelValue && kbId === props.kbId && confirmKbId === kbId,
  fetch: async (kbId: string, documentId: string) => {
    const { data } = await docApi.get(kbId, documentId)
    return data
  }
})
// confirm 控制器：一次只允许一个在途确认，关闭即 invalidate
const confirmController = createScopeConfirmController({ getDetail: fetchScopeDetail })

/** 生效上限：后端契约固定 5；props.max 非法或更大都不放宽。 */
const effectiveMax = computed(() =>
  typeof props.max === 'number' && Number.isInteger(props.max) && props.max >= 0
    ? Math.min(props.max, MAX_SCOPE_DOCUMENTS)
    : MAX_SCOPE_DOCUMENTS
)

const hasMore = computed(() =>
  hasMoreScopePages(lastPageSize.value, readyDocs.value.length, total.value, PAGE_SIZE, loading.value)
)

function resetState() {
  loadGeneration += 1
  confirmGeneration += 1
  confirmKbId = ''
  confirmController.invalidate()
  loadGuard.reset()
  selection.value = props.selected.map((item) => ({ ...item }))
  keptSelectionIds.value = new Set(props.selected.map((item) => item.document_id))
  readyDocs.value = []
  page.value = 0
  total.value = 0
  lastPageSize.value = 0
  loading.value = false
  loadError.value = ''
  limitWarning.value = false
  confirming.value = false
}

async function loadPage(reset: boolean) {
  if (!props.kbId) return
  if (loading.value) return
  const generation = reset ? ++loadGeneration : loadGeneration
  const token = loadGuard.begin(reset)
  const kb = props.kbId
  const nextPage = reset ? 1 : page.value + 1
  loading.value = true
  loadError.value = ''
  try {
    // 服务端已用治理谓词过滤（retrievable_only=true 在 count/limit 前生效），
    // total 与页长都是服务端真实值，不用客户端估算或静默截断。
    const { data } = await docApi.list(kb, {
      page: nextPage,
      page_size: PAGE_SIZE,
      status: 'ready',
      retrievable_only: true
    })
    // 绑定发起时的 KB 与会话代次：关闭/切 KB/卸载后的迟到响应直接丢弃
    if (disposed || !loadGuard.isCurrent(token) || kb !== props.kbId) return
    // 统一走纯合并：原始页长决定分页，本地过滤只影响展示
    const merged = mergeScopeListPage(reset ? [] : readyDocs.value, data)
    readyDocs.value = merged.docs as unknown as DocumentItem[]
    page.value = nextPage
    lastPageSize.value = merged.lastPageSize
    total.value = merged.total
  } catch (err: any) {
    if (!disposed && loadGuard.isCurrent(token) && kb === props.kbId) {
      loadError.value = err?.message || t('chat.scope.dialog.loadFailed')
    }
  } finally {
    // 旧会话的 finally 不能清掉新会话的 busy
    if (!disposed && loadGuard.isCurrent(token) && kb === props.kbId) loading.value = false
  }
}

function openReadyList() {
  resetState()
  void loadPage(true)
}

function isSelected(documentId: string) {
  return selection.value.some((item) => item.document_id === documentId)
}

function toggleDoc(doc: DocumentItem) {
  if (props.saving || confirming.value) return
  // 上限用对话框契约的 max（≤5）；确认控制器里再做一次严格校验兜底
  const { ids, rejected } = toggleDocumentId(
    selection.value.map((item) => item.document_id),
    doc.id,
    effectiveMax.value
  )
  if (rejected) {
    limitWarning.value = true
    return
  }
  limitWarning.value = false
  if (!ids.includes(doc.id)) {
    // 主动移除：之后再选同一篇才允许绑定当前代次（旧选择不静默升级）
    selection.value = selection.value.filter((item) => item.document_id !== doc.id)
    keptSelectionIds.value = new Set([...keptSelectionIds.value].filter((id) => id !== doc.id))
    return
  }
  // 保留的旧选择沿用其原代次（即使 props.selected 缺省为 0 也不升级）；
  // 只有从列表新选（或移除后重选）才用列表返回的当前 index_version。
  // 任一侧代次非法：拒绝这次选择，不退化成 0。
  const prior = props.selected.find((entry) => entry.document_id === doc.id)
  const version = resolveSelectedIndexVersion(
    keptSelectionIds.value.has(doc.id),
    prior?.index_version,
    doc.index_version
  )
  const item = version === null ? null : scopeItemFromDocument(doc.id, doc.filename, version)
  if (item) selection.value = [...selection.value, item]
}

function removeSelected(documentId: string) {
  if (props.saving || confirming.value) return
  limitWarning.value = false
  selection.value = selection.value.filter((item) => item.document_id !== documentId)
  // 从"已选列表"移除也算主动移除：再选同一篇必须绑定当前代次
  keptSelectionIds.value = new Set([...keptSelectionIds.value].filter((id) => id !== documentId))
}

function loadMore() {
  if (confirming.value) return
  void loadPage(false)
}

function retryLoad() {
  void loadPage(readyDocs.value.length === 0)
}

/** 确认前逐项重取详情：检查 retrievable 与选中时的 index_version。 */
async function confirm() {
  if (!props.modelValue) return
  if (props.saving || confirming.value) return
  if (!selection.value.length) {
    emit('confirm', [])
    return
  }
  confirming.value = true
  const generation = ++confirmGeneration
  const kb = props.kbId
  confirmKbId = kb
  const selected = selection.value.map((item) => ({ ...item }))
  let outcome: ScopeConfirmOutcome | null = null
  try {
    outcome = await confirmController.confirm(selected, effectiveMax.value)
  } catch {
    outcome = null
  }
  // 迟到的结果只有在"同一代次 + 仍打开 + 仍是同一个 KB + 未卸载"时才允许落地
  if (generation !== confirmGeneration || !props.modelValue || kb !== props.kbId || disposed) return
  try {
    if (outcome?.status === 'confirmed') {
      loadError.value = ''
      limitWarning.value = false
      emit('confirm', outcome.items)
    } else if (outcome?.status === 'notRetrievable' && outcome.documentId) {
      limitWarning.value = false
      loadError.value = t('chat.scope.dialog.freshNotRetrievable')
    } else if (outcome?.status === 'versionChanged') {
      limitWarning.value = false
      loadError.value = t('chat.scope.dialog.freshVersionChanged')
    } else if (outcome?.status === 'insufficientChecks') {
      // 不能只核验一部分就确认：明确提示不可用，让用户重试/减少选择
      limitWarning.value = false
      loadError.value = t('chat.scope.dialog.freshChecksIncomplete')
    } else if (outcome?.status === 'invalidSelection') {
      limitWarning.value = false
      loadError.value = t('chat.scope.dialog.freshUnavailable')
    } else if (outcome === null || outcome?.status === 'detailMissing' || outcome?.status === 'invalidated') {
      // 请求失败/确认被关闭作废：不 emit，提示用户重试
      limitWarning.value = false
      loadError.value = outcome?.status === 'invalidated'
        ? ''
        : t('chat.scope.dialog.freshUnavailable')
    }
  } finally {
    // 仅当前代次允许清 busy；关闭/重开后旧 finally 不能动新会话
    if (generation === confirmGeneration && !disposed) confirming.value = false
  }
}

function close() {
  if (props.saving) return
  // 关闭优先于确认：先作废在途请求，再通知父级关窗
  invalidateAll()
  emit('update:modelValue', false)
}

function beforeClose(done: () => void) {
  if (props.saving) return
  // 允许关闭，但必须先作废在途列表/确认请求
  invalidateAll()
  done()
}

function onVisibilityChange(value: boolean) {
  if (!value) invalidateAll()
  emit('update:modelValue', value)
}

function invalidateAll() {
  loadGeneration += 1
  confirmGeneration += 1
  confirmKbId = ''
  loadGuard.reset()
  confirmController.invalidate()
  confirming.value = false
  loading.value = false
}

/**
 * @closed 是异步的：close(new) → close(old) 时，新会话已经开始，
 * 旧会话迟到的 @closed 绝不能再 invalidate 一次把新会话的加载/确认打掉。
 * 只有"当前确实未打开"时才作废。
 */
function onClosed() {
  if (props.modelValue) return
  invalidateAll()
}

// 同步 flush：props.open / kbId 一变化立刻作废旧请求，不等下一个 tick。
// 关闭时保留最后一次 open 时的选择快照，不重新初始化 selection。
watch(() => props.modelValue, (open) => {
  if (open) {
    openReadyList()
  } else {
    invalidateAll()
  }
}, { flush: 'sync' })

watch(() => props.kbId, (next, previous) => {
  if (next === previous) return
  invalidateAll()
  // 未打开时只作废旧请求；打开中切库要按新库重开列表
  if (props.modelValue) openReadyList()
}, { flush: 'sync' })

// 初始打开（首次挂载即 open）走 mounted，避免 setup 阶段就发请求
onMounted(() => {
  if (props.modelValue) openReadyList()
})

// 同步在卸载前作废旧请求：onUnmounted 阶段太晚，refs 已不可安全写入
onBeforeUnmount(() => {
  disposed = true
  invalidateAll()
})
</script>

<style scoped>
.scope-description { margin: 0 0 6px; color: var(--text-3); font-size: 12px; line-height: 1.8; }
.scope-section { margin-top: 18px; }
.scope-section-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; color: var(--text-2); font-size: 12px; }
.scope-section-head strong { font-weight: 550; }
.scope-section-head span { color: var(--text-3); font-size: 11px; white-space: nowrap; }
.scope-selected-empty { margin: 0; padding: 10px 12px; border: 1px dashed var(--line-strong); border-radius: 8px; color: var(--text-3); font-size: 12px; line-height: 1.7; }
.scope-picked-list { display: flex; flex-direction: column; gap: 6px; max-height: 150px; margin: 0; padding: 0; overflow-y: auto; list-style: none; }
.scope-picked { display: flex; align-items: center; gap: 8px; padding: 7px 8px 7px 11px; border: 1px solid var(--accent-border); border-radius: 7px; background: var(--brand-soft); }
.scope-picked-name { flex: 1; min-width: 0; overflow: hidden; color: var(--text); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.scope-remove { display: grid; place-items: center; flex-shrink: 0; width: 24px; height: 24px; padding: 0; border: 0; border-radius: 5px; background: transparent; color: var(--text-3); cursor: pointer; font-size: 15px; line-height: 1; }
.scope-remove:not(:disabled):hover { background: var(--danger-soft); color: var(--danger); }
.scope-remove:disabled { cursor: not-allowed; opacity: .5; }
.scope-remove:focus-visible { outline: 2px solid var(--brand); outline-offset: 1px; }
.scope-ready-list { display: flex; flex-direction: column; gap: 2px; max-height: 34vh; min-height: 72px; margin: 0; padding: 2px; overflow-y: auto; list-style: none; }
.scope-checkbox { width: 100%; height: auto; margin-right: 0; padding: 7px 8px; border-radius: 6px; }
.scope-checkbox:hover { background: var(--surface-soft); }
.scope-checkbox :deep(.el-checkbox__label) { min-width: 0; overflow: hidden; color: var(--text-2); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.scope-loading, .scope-empty { margin: 0; padding: 20px 0; color: var(--text-3); font-size: 12px; text-align: center; }
.scope-load-error { margin: 0; }
.scope-load-error .el-button { margin-top: 8px; }
.scope-limit { margin: 8px 0 0; color: var(--warning); font-size: 12px; line-height: 1.6; }
.scope-load-more { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 10px; color: var(--text-3); font-size: 11px; }
.scope-alert { margin-top: 16px; }
.scope-dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.scope-dialog-count { color: var(--text-3); font-size: 11px; }
.scope-dialog-buttons { display: flex; gap: 8px; margin-left: auto; }
@media (max-width: 600px) {
  .scope-ready-list { max-height: 30vh; }
  .scope-dialog-footer { align-items: stretch; }
  .scope-dialog-buttons { margin-left: 0; }
  .scope-dialog-buttons .el-button { flex: 1; margin-left: 0; }
}
</style>
