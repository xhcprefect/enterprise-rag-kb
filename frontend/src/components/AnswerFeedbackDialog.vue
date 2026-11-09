<template>
  <el-dialog
    :model-value="modelValue"
    :title="t('feedback.dialog.title')"
    width="min(520px, 94vw)"
    append-to-body
    :close-on-click-modal="!submitting"
    :close-on-press-escape="!submitting"
    :show-close="!submitting"
    @update:model-value="onVisibilityChange"
    @closed="onClosed"
  >
    <div class="feedback-form">
      <section class="field" :aria-label="t('feedback.dialog.categoryLabel')">
        <span class="field-label">{{ t('feedback.dialog.categoryLabel') }}</span>
        <el-radio-group v-model="category" :disabled="submitting" :aria-label="t('feedback.dialog.categoryLabel')">
          <el-radio v-for="item in categories" :key="item" :value="item">
            {{ t(feedbackCategoryKey(item)) }}
          </el-radio>
        </el-radio-group>
      </section>

      <section class="field">
        <label class="field-label" for="feedback-note">{{ t('feedback.dialog.noteLabel') }}</label>
        <el-input
          id="feedback-note"
          v-model="note"
          type="textarea"
          :rows="4"
          maxlength="1000"
          show-word-limit
          resize="none"
          :disabled="submitting"
          :placeholder="t('feedback.dialog.notePlaceholder')"
          :aria-label="t('feedback.dialog.noteLabel')"
        />
      </section>

      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    </div>

    <template #footer>
      <div class="dialog-footer">
        <el-button :disabled="submitting" @click="requestClose">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="submitting" :disabled="submitting || !category" @click="submit">
          {{ submitting ? t('feedback.dialog.submitting') : t('feedback.dialog.submit') }}
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { FeedbackCategory } from '@/api'
import { feedbackCategoryKey } from '@/utils/feedback'

const props = defineProps<{
  modelValue: boolean
  submitting: boolean
  error: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  submit: [payload: { category: FeedbackCategory; note: string }]
  closed: []
}>()

const { t } = useI18n()

const categories: FeedbackCategory[] = ['inaccurate_answer', 'citation_error', 'missing_material']
const category = ref<FeedbackCategory>('inaccurate_answer')
const note = ref('')

// 每次重新打开都从干净的表单开始；提交失败时弹窗保持打开，用户输入不丢。
watch(() => props.modelValue, (open) => {
  if (!open) return
  category.value = 'inaccurate_answer'
  note.value = ''
})

function onVisibilityChange(value: boolean) {
  if (props.submitting && !value) return
  emit('update:modelValue', value)
}

function requestClose() {
  if (props.submitting) return
  emit('update:modelValue', false)
}

function onClosed() {
  emit('closed')
}

function submit() {
  if (props.submitting) return
  emit('submit', { category: category.value, note: note.value.trim() })
}
</script>

<style scoped>
.feedback-form {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 9px;
}

.field-label {
  color: var(--text-2);
  font-size: 12.5px;
  font-weight: 550;
}

.field :deep(.el-radio-group) {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
