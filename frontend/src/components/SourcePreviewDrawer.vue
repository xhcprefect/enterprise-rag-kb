<template>
  <el-drawer :model-value="modelValue" :title="t('chat.source.title')" size="min(760px, 95vw)" @update:model-value="emit('update:modelValue', $event)">
    <div v-if="loading" class="source-status" role="status">{{ t('chat.source.loading') }}</div>
    <el-alert v-else-if="error" :title="error" type="error" :closable="false" show-icon />
    <template v-else-if="source">
      <h2 class="source-filename">{{ source.filename }}</h2>
      <p class="source-position">{{ position.join(' · ') || t('chat.source.noPosition') }}</p>
      <p class="source-version">{{ t('chat.source.version', { version: source.index_version }) }}</p>
      <el-button v-if="isPdf" :loading="pdfLoading" @click="loadPdf">{{ targetPage ? t('chat.source.previewPdfAtPage', { page: targetPage }) : t('chat.source.previewPdf') }}</el-button>
      <p v-if="isPdf" class="source-position">{{ targetPage ? t('chat.source.pdfTarget', { page: targetPage }) : t('chat.source.pdfPageUnknown') }}</p>
      <el-alert v-if="pdfError" class="source-pdf-error" :title="pdfError" type="warning" :closable="false" />
      <iframe v-if="pdfUrl" class="source-pdf" :src="pdfSourceUrl(pdfUrl, targetPage)" :title="source.filename" referrerpolicy="no-referrer"></iframe>
      <div v-for="chunk in source.chunks" :key="chunk.chunk_index" class="source-chunk">
        <div class="source-chunk-head">{{ t('chat.citations.chunk', { index: chunk.chunk_index + 1 }) }}<span>{{ sourceLocationLabels(chunk.location, t).join(' · ') }}</span></div>
        <pre>{{ chunk.content }}</pre>
      </div>
      <p v-if="source.truncated" class="source-position">{{ t('chat.source.truncated') }}</p>
    </template>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { docApi, type Citation, type SourcePreview } from '@/api'
import { pdfSourceUrl, sourceLocationLabels, sourceTargetPage } from '@/utils/sourceLocation'

const props = defineProps<{ modelValue: boolean; kbId: string; citation: Citation | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const { t } = useI18n()
const source = ref<SourcePreview | null>(null)
const loading = ref(false)
const error = ref('')
const pdfLoading = ref(false)
const pdfError = ref('')
const pdfUrl = ref('')
let generation = 0
const position = computed(() => source.value ? sourceLocationLabels(source.value.location, t) : [])
const isPdf = computed(() => source.value?.file_ext.toLowerCase() === '.pdf')
const targetPage = computed(() => source.value ? sourceTargetPage(source.value) : null)

function clearPdf() {
  if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
  pdfUrl.value = ''
  pdfLoading.value = false
  pdfError.value = ''
}

watch([() => props.modelValue, () => props.citation, () => props.kbId], async () => {
  const current = ++generation
  clearPdf()
  source.value = null
  error.value = ''
  loading.value = false
  if (!props.modelValue || !props.citation || !props.kbId) return
  const citation = props.citation
  if (!citation.document_id || citation.chunk_index == null) {
    error.value = t('chat.source.unavailable')
    return
  }
  loading.value = true
  try {
    const { data } = await docApi.source(props.kbId, citation)
    if (current === generation) source.value = data
  } catch (err: any) {
    if (current === generation) error.value = err?.message || t('chat.source.unavailable')
  } finally {
    if (current === generation) loading.value = false
  }
}, { immediate: true })

async function loadPdf() {
  if (!source.value || pdfLoading.value || pdfUrl.value) return
  const current = generation
  const selected = source.value
  pdfLoading.value = true
  pdfError.value = ''
  try {
    const { data } = await docApi.pdf(props.kbId, selected.document_id, selected.index_version)
    if (current === generation) pdfUrl.value = URL.createObjectURL(data)
  } catch (err: any) {
    if (current === generation) pdfError.value = err?.message || t('chat.source.unavailable')
  } finally {
    if (current === generation) pdfLoading.value = false
  }
}

onBeforeUnmount(() => { generation += 1; clearPdf() })
</script>

<style scoped>
.source-status, .source-position, .source-version { color: var(--text-3); font-size: 12px; line-height: 1.8; }
.source-filename { color: var(--text); font-size: 17px; overflow-wrap: anywhere; }
.source-pdf { width: 100%; height: 55vh; margin-top: 16px; border: 1px solid var(--line); border-radius: 8px; }
.source-pdf-error { margin-top: 12px; }
.source-chunk { margin-top: 18px; padding: 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-soft); }
.source-chunk-head { display: flex; flex-wrap: wrap; gap: 10px; color: var(--text-3); font-size: 12px; }
.source-chunk pre { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); font-family: inherit; font-size: 13px; line-height: 1.8; }
</style>
