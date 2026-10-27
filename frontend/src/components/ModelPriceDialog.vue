<template>
  <el-dialog
    :model-value="modelValue"
    :title="t('usage.priceDialog.title')"
    width="min(920px, 96vw)"
    append-to-body
    :close-on-click-modal="false"
    @update:model-value="onVisibilityChange"
  >
    <p class="price-intro">{{ t('usage.priceDialog.subtitle') }}</p>

    <div v-if="loading && !items.length" class="price-state" role="status">
      <el-skeleton :rows="5" animated />
    </div>

    <template v-else-if="items.length">
      <el-alert
        v-if="loadError"
        class="list-alert"
        :title="loadError"
        type="error"
        :closable="false"
        show-icon
      >
        <el-button class="retry" size="small" @click="load(true, conflict)">{{ t('usage.priceDialog.retry') }}</el-button>
      </el-alert>

      <div class="price-layout">
      <nav class="price-list" :aria-label="t('usage.priceDialog.listAria')">
        <button
          v-for="item in items"
          :key="priceKey(item)"
          type="button"
          class="price-item"
          :class="{ selected: priceKey(item) === selectedKey }"
          :aria-current="priceKey(item) === selectedKey ? 'true' : undefined"
          :aria-label="t('usage.priceDialog.selectAria', { model: item.model, provider: item.provider })"
          @click="selectItem(item)"
        >
          <span class="price-item-top">
            <strong :title="item.model">{{ item.model }}</strong>
            <el-tag size="small" effect="plain" :type="item.kind === 'chat' ? 'success' : 'info'">
              {{ t(`usage.kinds.${item.kind}`) }}
            </el-tag>
          </span>
          <span class="price-item-provider" :title="item.provider">{{ item.provider }}</span>
          <span class="price-item-rates">
            <span>{{ formatRate(item.input_per_million) }} / {{ formatRate(item.output_per_million) }}</span>
            <span class="price-item-state" :class="{ configured: isPriceConfigured(item) }">
              {{ isPriceConfigured(item) ? t('usage.priceDialog.configured') : t('usage.priceDialog.unset') }}
            </span>
          </span>
        </button>
      </nav>

      <section v-if="selected" class="price-editor" :aria-label="t('usage.priceDialog.editorAria')">
        <header class="editor-head">
          <div class="editor-identity">
            <strong :title="selected.model">{{ selected.model }}</strong>
            <span>{{ selected.provider }} · {{ t(`usage.kinds.${selected.kind}`) }}</span>
          </div>
          <div class="editor-meta">
            <span class="currency">{{ t('usage.priceDialog.currency') }} {{ selected.currency }}</span>
            <span class="revision">
              {{ selected.revision > 0 ? t('usage.priceDialog.revision', { revision: selected.revision }) : t('usage.priceDialog.virtual') }}
            </span>
          </div>
        </header>

        <el-alert
          v-if="conflict"
          class="editor-alert"
          :title="t('usage.priceDialog.conflict')"
          type="warning"
          :closable="false"
          show-icon
        />
        <el-alert
          v-else-if="saveError"
          class="editor-alert"
          :title="saveError"
          type="error"
          :closable="false"
          show-icon
        />
        <el-alert
          v-else-if="errorText"
          class="editor-alert"
          :title="errorText"
          type="error"
          :closable="false"
          show-icon
        />

        <div class="switch-row">
          <el-switch v-model="form.enabled" :aria-label="t('usage.priceDialog.enabled')" />
          <span>
            <strong>{{ t('usage.priceDialog.enabled') }}</strong>
            <small>{{ form.enabled ? t('usage.priceDialog.enabledHint') : t('usage.priceDialog.disabledHint') }}</small>
          </span>
        </div>

        <div class="rate-grid">
          <label class="rate-field" :class="{ invalid: validation?.field === 'input' }">
            <span>{{ t('usage.priceDialog.input') }}</span>
            <el-input
              v-model="form.input"
              :disabled="!form.enabled"
              inputmode="decimal"
              :placeholder="t('usage.priceDialog.ratePlaceholder')"
              :aria-label="t('usage.priceDialog.input')"
            />
          </label>

          <label class="rate-field" :class="{ invalid: validation?.field === 'output' }">
            <span>{{ t('usage.priceDialog.output') }}</span>
            <el-input
              v-if="selected.kind === 'chat'"
              v-model="form.output"
              :disabled="!form.enabled"
              inputmode="decimal"
              :placeholder="t('usage.priceDialog.ratePlaceholder')"
              :aria-label="t('usage.priceDialog.output')"
            />
            <el-input
              v-else
              :model-value="'0'"
              disabled
              :aria-label="t('usage.priceDialog.output')"
            />
            <small v-if="selected.kind !== 'chat'" class="field-hint">
              {{ t('usage.priceDialog.outputZeroHint') }}
            </small>
          </label>

          <label class="rate-field wide" :class="{ invalid: validation?.field === 'cached' }">
            <span>{{ t('usage.priceDialog.cached') }}</span>
            <el-input
              v-model="form.cached"
              :disabled="!form.enabled"
              inputmode="decimal"
              :placeholder="t('usage.priceDialog.cachedPlaceholder')"
              :aria-label="t('usage.priceDialog.cached')"
            />
            <small class="field-hint">{{ t('usage.priceDialog.cachedHint') }}</small>
          </label>
        </div>

        <div class="editor-notes">
          <p>{{ t('usage.priceDialog.historyHint') }}</p>
          <p>{{ t('usage.priceDialog.freeHint') }}</p>
        </div>

        <div class="editor-actions">
          <el-button
            v-if="selected.id !== null && isPriceConfigured(selected)"
            :disabled="saving"
            @click="clearPrice"
          >{{ t('usage.priceDialog.clear') }}</el-button>
          <span class="save-spacer"></span>
          <el-button type="primary" :loading="saving" :disabled="!canSave" @click="save">
            {{ t('usage.priceDialog.save') }}
          </el-button>
        </div>
        </section>
      </div>
    </template>

    <div v-else-if="loadError" class="price-state">
      <el-alert :title="loadError" type="error" :closable="false" show-icon />
      <el-button class="retry" size="small" @click="load(false)">{{ t('usage.priceDialog.retry') }}</el-button>
    </div>

    <div v-else class="price-state">
      <el-empty :description="t('usage.priceDialog.empty')" :image-size="72" />
    </div>

    <template #footer>
      <el-button @click="close">{{ t('common.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { usageApi, type ModelPrice } from '@/api/usage'
import {
  buildPricePayload,
  createDialogRequestGate,
  draftSignature,
  formatRate,
  isEditorUntouched,
  isPriceConfigured,
  isRevisionConflict,
  priceKey,
  sortPrices,
  upsertPrice,
  validatePriceDraft,
  type UsagePriceDraft,
  type UsagePriceValidation
} from '@/utils/usage'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ (event: 'update:modelValue', value: boolean): void }>()

const { t } = useI18n()
const gate = createDialogRequestGate()

const items = ref<ModelPrice[]>([])
const selectedKey = ref('')
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const saveError = ref('')
const conflict = ref(false)
const validation = ref<UsagePriceValidation | null>(null)

const form = reactive<UsagePriceDraft>({ enabled: false, input: '', output: '', cached: '' })

const selected = computed(() => items.value.find(item => priceKey(item) === selectedKey.value) || null)

const errorText = computed(() => {
  const code = validation.value?.code
  return code ? t(`usage.priceDialog.errors.${code}`) : ''
})

const canSave = computed(() => {
  if (!selected.value || saving.value) return false
  // 虚拟模型本来就没有价格，关闭计价时没有可清空的内容
  if (!form.enabled) return selected.value.id !== null && isPriceConfigured(selected.value)
  return true
})

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : t('errors.request')
}

function resetForm() {
  form.enabled = false
  form.input = ''
  form.output = ''
  form.cached = ''
  validation.value = null
}

function selectItem(item: ModelPrice, resetConflict = true) {
  selectedKey.value = priceKey(item)
  form.enabled = isPriceConfigured(item)
  form.input = item.input_per_million ?? ''
  form.output = item.kind === 'chat' ? item.output_per_million ?? '' : '0'
  form.cached = item.cached_input_per_million ?? ''
  validation.value = null
  saveError.value = ''
  if (resetConflict) conflict.value = false
}

watch(() => form.enabled, (enabled) => {
  if (enabled && selected.value && selected.value.kind !== 'chat') form.output = '0'
})

watch(() => props.modelValue, (open) => {
  // 关闭或重新打开都会淘汰上一轮在途请求，旧结果不能写回新会话
  gate.invalidate()
  loading.value = false
  saving.value = false
  if (!open) return
  selectedKey.value = ''
  conflict.value = false
  saveError.value = ''
  resetForm()
  void load(false)
})

async function load(preserve: boolean, keepConflict = false) {
  const ticket = gate.beginLoad()
  const before = { key: selectedKey.value, draft: draftSignature(form) }
  loading.value = true
  saving.value = false
  loadError.value = ''
  const wanted = preserve ? selectedKey.value : ''
  try {
    const { data } = await usageApi.prices()
    if (!gate.isLoadCurrent(ticket)) return
    items.value = sortPrices(Array.isArray(data.items) ? data.items : [])
    const target = items.value.find(item => priceKey(item) === wanted) || items.value[0]
    if (!target) {
      selectedKey.value = ''
      resetForm()
      return
    }
    // 加载期间切换了模型或编辑了草稿：只更新列表，不回填迟到的加载结果。
    const untouched = isEditorUntouched(before, {
      key: selectedKey.value,
      draft: draftSignature(form)
    })
    const stillListed = items.value.some(item => priceKey(item) === selectedKey.value)
    if (untouched || !stillListed) selectItem(target, !keepConflict)
  } catch (error) {
    if (!gate.isLoadCurrent(ticket)) return
    loadError.value = messageOf(error)
  } finally {
    if (gate.isLoadCurrent(ticket)) loading.value = false
  }
}

async function save() {
  const item = selected.value
  if (!item) return
  const result = validatePriceDraft(item.kind, form)
  if (!result.ok) {
    validation.value = result
    return
  }
  validation.value = null
  saveError.value = ''
  const before = { key: selectedKey.value, draft: draftSignature(form) }
  const ticket = gate.beginSave()
  saving.value = true
  try {
    const { data } = await usageApi.updatePrice(buildPricePayload(item, form))
    if (!gate.isSaveCurrent(ticket)) return
    items.value = upsertPrice(items.value, data)
    conflict.value = false
    // 保存期间切换了模型或继续改草稿时，不能用服务器结果覆盖当前草稿
    if (isEditorUntouched(before, { key: selectedKey.value, draft: draftSignature(form) })) {
      selectItem(data, false)
    }
    ElMessage.success(t('usage.priceDialog.saved'))
  } catch (error) {
    if (!gate.isSaveCurrent(ticket)) return
    if (isRevisionConflict(error)) {
      conflict.value = true
      saving.value = false
      // 刷新到最新 revision，但保留冲突提示，等管理员确认后再重试
      await load(true, true)
      return
    }
    saveError.value = messageOf(error)
  } finally {
    if (gate.isSaveCurrent(ticket)) saving.value = false
  }
}

async function clearPrice() {
  form.enabled = false
  await save()
}

function onVisibilityChange(value: boolean) {
  if (!value) close()
}

function close() {
  gate.invalidate()
  loading.value = false
  saving.value = false
  emit('update:modelValue', false)
}

onBeforeUnmount(() => {
  gate.invalidate()
  loading.value = false
  saving.value = false
})
</script>

<style scoped>
.price-intro {
  margin: 0 0 16px;
  color: var(--text-2);
  font-size: 12.5px;
  line-height: 1.75;
}

.price-state {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 12px;
  min-height: 160px;
}

.retry {
  margin-top: 2px;
}

.list-alert {
  margin-bottom: 12px;
}

.price-layout {
  display: grid;
  grid-template-columns: minmax(220px, 300px) minmax(0, 1fr);
  gap: 16px;
  min-height: 320px;
}

.price-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 440px;
  padding: 6px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
}

.price-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: transparent;
  color: var(--text-2);
  text-align: left;
  cursor: pointer;
  transition: background .15s, border-color .15s, color .15s;
}

.price-item:hover {
  background: var(--surface);
}

.price-item.selected {
  border-color: var(--accent-border);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
}

.price-item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.price-item-top strong {
  overflow: hidden;
  color: var(--text);
  font-size: 12.5px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.price-item-provider {
  overflow: hidden;
  color: var(--text-3);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.price-item-rates {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--text-3);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
}

.price-item-state {
  flex-shrink: 0;
  color: var(--warning);
}

.price-item-state.configured {
  color: var(--success);
}

.price-editor {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
  padding: 16px 18px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
}

.editor-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.editor-identity {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.editor-identity strong {
  overflow: hidden;
  color: var(--text);
  font-size: 14px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.editor-identity span {
  color: var(--text-3);
  font-size: 11.5px;
}

.editor-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.currency,
.revision {
  padding: 2px 7px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.6;
}

.editor-alert {
  margin: 0;
}

.switch-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  color: var(--text-2);
  font-size: 12.5px;
}

.switch-row > span {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.switch-row strong {
  color: var(--text);
  font-size: 12.5px;
  font-weight: 600;
}

.switch-row small {
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.65;
}

.rate-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 13px;
}

.rate-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  color: var(--text-2);
  font-size: 12px;
}

.rate-field.wide {
  grid-column: 1 / -1;
}

.rate-field.invalid > span {
  color: var(--danger);
}

.field-hint {
  color: var(--text-3);
  font-size: 10.5px;
  line-height: 1.6;
}

.editor-notes {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-top: 2px;
  border-top: 1px solid var(--line);
}

.editor-notes p {
  margin: 0;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.7;
}

.editor-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: auto;
}

.save-spacer {
  flex: 1;
}

.editor-actions .el-button {
  margin: 0;
}

@media (max-width: 760px) {
  .price-layout {
    grid-template-columns: 1fr;
    min-height: 0;
  }

  .price-list {
    flex-direction: row;
    max-height: none;
    overflow-x: auto;
    overflow-y: hidden;
  }

  .price-item {
    min-width: 200px;
  }

  .rate-grid {
    grid-template-columns: 1fr;
  }
}
</style>
