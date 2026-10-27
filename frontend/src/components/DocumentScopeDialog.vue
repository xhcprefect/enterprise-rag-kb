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
            :disabled="saving"
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
            :disabled="saving || (selection.length >= max && !isSelected(doc.id))"
            @change="toggleDoc(doc)"
          >
            <span :title="doc.filename">{{ doc.filename }}</span>
          </el-checkbox>
        </li>
      </ul>

      <p v-if="limitWarning" class="scope-limit" role="alert">{{ t('chat.scope.dialog.limit', { max }) }}</p>
      <div v-if="hasMore" class="scope-load-more">
        <span>{{ t('chat.scope.dialog.loadedCount', { loaded: readyDocs.length, total }) }}</span>
        <el-button size="small" :loading="loading" :disabled="saving" @click="loadMore">{{ t('chat.scope.dialog.loadMore') }}</el-button>
      </div>
    </section>

    <el-alert v-if="error" class="scope-alert" :title="error" type="error" :closable="false" show-icon />

    <template #footer>
      <div class="scope-dialog-footer">
        <span class="scope-dialog-count">{{ t('chat.scope.dialog.selectedCount', { count: selection.length, max }) }}</span>
        <div class="scope-dialog-buttons">
          <el-button :disabled="saving" @click="close">{{ t('chat.scope.dialog.cancel') }}</el-button>
          <el-button type="primary" :loading="saving" @click="confirm">{{ t('chat.scope.dialog.confirm') }}</el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { docApi, type DocumentItem } from '@/api'
import {
  scopeItemFromDocument,
  toggleDocumentId,
  type DocumentScopeItem
} from '@/utils/documentScope'

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
const readyDocs = ref<DocumentItem[]>([])
const page = ref(0)
const total = ref(0)
const lastPageSize = ref(0)
const loading = ref(false)
const loadError = ref('')
const limitWarning = ref(false)
let loadGeneration = 0

const hasMore = computed(() =>
  !loading.value && lastPageSize.value >= PAGE_SIZE && readyDocs.value.length < total.value
)

function resetState() {
  loadGeneration += 1
  selection.value = props.selected.map((item) => ({ ...item }))
  readyDocs.value = []
  page.value = 0
  total.value = 0
  lastPageSize.value = 0
  loading.value = false
  loadError.value = ''
  limitWarning.value = false
}

async function loadPage(reset: boolean) {
  if (!props.kbId) return
  if (loading.value) return
  const generation = reset ? ++loadGeneration : loadGeneration
  const nextPage = reset ? 1 : page.value + 1
  loading.value = true
  loadError.value = ''
  try {
    const { data } = await docApi.list(props.kbId, { page: nextPage, page_size: PAGE_SIZE, status: 'ready' })
    if (generation !== loadGeneration) return
    const items = Array.isArray(data?.items)
      ? data.items.filter((doc): doc is DocumentItem => !!doc && doc.status === 'ready' && typeof doc.id === 'string')
      : []
    const existing = reset ? [] : readyDocs.value
    const seen = new Set(existing.map((doc) => doc.id))
    readyDocs.value = [...existing, ...items.filter((doc) => !seen.has(doc.id))]
    page.value = nextPage
    lastPageSize.value = items.length
    const reported = typeof data?.total === 'number' && Number.isFinite(data.total) ? Math.trunc(data.total) : 0
    total.value = Math.max(0, reported, readyDocs.value.length)
  } catch (err: any) {
    if (generation === loadGeneration) loadError.value = err?.message || t('chat.scope.dialog.loadFailed')
  } finally {
    if (generation === loadGeneration) loading.value = false
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
  if (props.saving) return
  const { ids, rejected } = toggleDocumentId(
    selection.value.map((item) => item.document_id),
    doc.id,
    props.max
  )
  if (rejected) {
    limitWarning.value = true
    return
  }
  limitWarning.value = false
  if (!ids.includes(doc.id)) {
    selection.value = selection.value.filter((item) => item.document_id !== doc.id)
    return
  }
  const item = scopeItemFromDocument(doc.id, doc.filename, props.selected.find((entry) => entry.document_id === doc.id)?.index_version)
  if (item) selection.value = [...selection.value, item]
}

function removeSelected(documentId: string) {
  if (props.saving) return
  limitWarning.value = false
  selection.value = selection.value.filter((item) => item.document_id !== documentId)
}

function loadMore() {
  void loadPage(false)
}

function retryLoad() {
  void loadPage(readyDocs.value.length === 0)
}

function confirm() {
  if (props.saving) return
  emit('confirm', selection.value.map((item) => ({ ...item })))
}

function close() {
  if (props.saving) return
  emit('update:modelValue', false)
}

function beforeClose(done: () => void) {
  if (props.saving) return
  done()
}

function onVisibilityChange(value: boolean) {
  emit('update:modelValue', value)
}

function onClosed() {
  loadGeneration += 1
}

watch(() => props.modelValue, (open) => {
  if (open) openReadyList()
})

watch(() => props.kbId, () => {
  if (props.modelValue) openReadyList()
})

onMounted(() => {
  if (props.modelValue) openReadyList()
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
