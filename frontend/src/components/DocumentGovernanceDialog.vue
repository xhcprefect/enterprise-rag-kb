<template>
  <el-dialog
    :model-value="modelValue"
    :title="t('documents.governance.dialog.title', { name: doc?.filename || '' })"
    width="min(560px, 94vw)"
    append-to-body
    :close-on-click-modal="false"
    :before-close="beforeClose"
    @update:model-value="onVisibilityChange"
    @closed="onClosed"
  >
    <template v-if="doc">
      <el-alert
        v-if="conflict"
        class="gov-alert"
        type="warning"
        :title="t('documents.governance.dialog.conflict')"
        :closable="false"
        show-icon
      />
      <p v-if="snapshot?.revision_known" class="gov-revision">
        {{ t('documents.governance.dialog.revisionLabel') }}
        <strong>{{ snapshot.governance_revision }}</strong>
      </p>
      <el-alert
        v-else-if="snapshot"
        class="gov-alert"
        type="info"
        :title="t('documents.governance.dialog.revisionUnknown')"
        :closable="false"
        show-icon
      />

      <el-form label-position="top" class="gov-form">
        <el-form-item :label="t('documents.governance.dialog.maintainer')">
          <el-input
            v-model="form.maintainer"
            :placeholder="t('documents.governance.dialog.maintainerPlaceholder')"
            :disabled="saving"
            maxlength="200"
            show-word-limit
          />
          <span class="gov-hint">{{ t('documents.governance.dialog.maintainerHint') }}</span>
        </el-form-item>

        <el-form-item :label="t('documents.governance.dialog.lastReviewed')">
          <input
            v-model="reviewLocal"
            type="datetime-local"
            step="1"
            class="gov-input"
            :disabled="saving"
            :aria-label="t('documents.governance.dialog.lastReviewed')"
          />
          <span v-if="unparseableDate(form.last_reviewed_at)" class="gov-hint gov-hint-bad">
            {{ unparseableDate(form.last_reviewed_at) }} ·
            {{ t('documents.governance.dialog.dateUnparseable') }}
          </span>
          <el-button
            v-if="form.last_reviewed_at"
            link
            size="small"
            :disabled="saving"
            @click="clearDate('last_reviewed_at')"
          >
            {{ t('documents.governance.dialog.clearDate') }}
          </el-button>
        </el-form-item>

        <div class="gov-window">
          <el-form-item :label="t('documents.governance.dialog.validFrom')">
            <input
              v-model="fromLocal"
              type="datetime-local"
              step="1"
              class="gov-input"
              :disabled="saving"
              :aria-label="t('documents.governance.dialog.validFrom')"
            />
            <span v-if="unparseableDate(form.valid_from)" class="gov-hint gov-hint-bad">
              {{ unparseableDate(form.valid_from) }} · {{ t('documents.governance.dialog.dateUnparseable') }}
            </span>
            <el-button
              v-if="form.valid_from"
              link
              size="small"
              :disabled="saving"
              @click="clearDate('valid_from')"
            >
              {{ t('documents.governance.dialog.clearDate') }}
            </el-button>
          </el-form-item>
          <el-form-item :label="t('documents.governance.dialog.validUntil')">
            <input
              v-model="untilLocal"
              type="datetime-local"
              step="1"
              class="gov-input"
              :disabled="saving"
              :aria-label="t('documents.governance.dialog.validUntil')"
            />
            <span v-if="unparseableDate(form.valid_until)" class="gov-hint gov-hint-bad">
              {{ unparseableDate(form.valid_until) }} · {{ t('documents.governance.dialog.dateUnparseable') }}
            </span>
            <el-button
              v-if="form.valid_until"
              link
              size="small"
              :disabled="saving"
              @click="clearDate('valid_until')"
            >
              {{ t('documents.governance.dialog.clearDate') }}
            </el-button>
          </el-form-item>
        </div>

        <el-form-item :label="t('documents.governance.dialog.enabled')">
          <el-switch
            :model-value="form.enabled === true"
            :disabled="saving || isSupersededDoc"
            @update:model-value="onEnabledChange"
          />
          <span class="gov-hint gov-hint-switch">
            <template v-if="form.enabled === null">{{ t('documents.governance.dialog.enabledUnknown') }}</template>
            <template v-else-if="isSupersededDoc">{{ t('documents.governance.dialog.supersededLockHint') }}</template>
          </span>
        </el-form-item>
      </el-form>

      <p v-if="formError" class="gov-error" role="alert">{{ formError }}</p>
    </template>

    <template #footer>
      <div class="gov-dialog-footer">
        <div class="gov-extra-actions">
          <el-button
            size="small"
            type="warning"
            plain
            :disabled="saving"
            @click="askReplacement"
          >
            {{ t('documents.comparison.action') }}
          </el-button>
          <el-button
            v-if="isSupersededDoc"
            size="small"
            type="primary"
            plain
            :disabled="saving"
            @click="doReenable"
          >
            {{ t('documents.governance.dialog.reenable') }}
          </el-button>
        </div>
        <div class="gov-buttons">
          <el-button :disabled="saving" @click="close">{{ t('common.cancel') }}</el-button>
          <el-button type="primary" :loading="saving" @click="save">
            {{ t('documents.governance.dialog.save') }}
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { docApi, type DocumentItem } from '@/api'
import {
  belongsToGovernanceSession,
  createGovernanceWriteController,
  formatLocalGovernanceInput,
  governanceSessionState,
  governanceWriteLock,
  invalidateGovernanceRevision,
  isGovernanceSessionCurrent,
  isSuperseded,
  parseGovernanceDate,
  planGovernancePatch,
  sameGovernanceInputInstant,
  toGovernanceSnapshot,
  type GovernanceDateField,
  type GovernanceFormState,
  type GovernanceRequestIdentity,
  type GovernanceSessionState,
  type GovernanceSnapshot
} from '@/utils/documentGovernance'

const props = defineProps<{
  modelValue: boolean
  kbId: string
  /** 打开表单那一刻的文档行；成为 expected_revision 与替代/重新启用的快照来源。 */
  doc: DocumentItem | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: [doc: DocumentItem]
  /** 409 后成功拉到的新鲜详情；父组件更新行，但不关窗。 */
  refreshed: [doc: DocumentItem]
  'supersede-request': [doc: DocumentItem]
}>()

const { t } = useI18n()

const snapshot = ref<GovernanceSnapshot | null>(null)
const form = ref<GovernanceFormState>({
  maintainer: '',
  last_reviewed_at: '',
  valid_from: '',
  valid_until: '',
  enabled: null
})
const conflict = ref(false)
const formError = ref('')
// 由控制器回调驱动的真实 busy：save/reenable 期间禁用按钮，finally 只清本次
const saving = ref(false)
// 打开代次：关闭/切 KB/切文档/重新打开都会 +1，迟到的响应按代次丢弃
let generation = 0
// 卸载后永不复位：卸载后的任何迟到结果都不得改状态或提示
let disposed = false
const controller = createGovernanceWriteController((busy) => {
  saving.value = busy
})

const isSupersededDoc = computed(() => isSuperseded(props.doc))

/** 当前会话状态投影（供纯函数守卫使用）。 */
function currentState(): GovernanceSessionState {
  return governanceSessionState(
    props.kbId,
    generation,
    props.modelValue,
    disposed,
    props.doc?.id ?? null
  )
}

/** 当前请求是否仍属于同一个打开会话、同一 KB/文档，且弹窗仍打开。 */
function aliveFor(captured: GovernanceRequestIdentity): boolean {
  return isGovernanceSessionCurrent(captured, currentState())
}

function resetFormFromSnapshot(next: GovernanceSnapshot) {
  form.value = {
    maintainer: next.maintainer,
    last_reviewed_at: next.last_reviewed_at ?? '',
    valid_from: next.valid_from ?? '',
    valid_until: next.valid_until ?? '',
    enabled: next.enabled
  }
}

// 当前表单已绑定的 KB/文档键：只有真正换了身份才重建，避免 409 刷新
// 用"同 id 的新对象"替换 props.doc 时被误判为切换文档而清掉冲突提示。
let boundKey = ''

function docKey(doc: DocumentItem | null, kb: string): string {
  return `${kb}\u0000${doc?.id ?? ''}`
}

function openForDoc(doc: DocumentItem | null) {
  // 重新打开：在途请求立即作废，旧结果（包括迟到的 saved）不再被接受
  generation += 1
  controller.invalidate()
  boundKey = docKey(doc, props.kbId)
  conflict.value = false
  formError.value = ''
  if (!doc) {
    snapshot.value = null
    return
  }
  // expected_revision 快照：打开表单那一刻的治理版本，之后不再改动
  snapshot.value = toGovernanceSnapshot(doc)
  resetFormFromSnapshot(snapshot.value)
}

/**
 * 关闭/切库/切文档/卸载：立即作废在途请求（代次 +1），并同步清掉版本号，
 * 使迟到的 409 刷新结果无法被当作"仍可用旧版本"。
 * 旧 finally 因代次已变而不会清掉后续新请求的 busy。
 */
function stopWrites() {
  generation += 1
  controller.invalidate()
  conflict.value = false
  formError.value = ''
  snapshot.value = invalidateGovernanceRevision(snapshot.value)
}

/**
 * 日期控件的原始输入：控件只能表示到秒。若新文本与当前值在秒级完全一致，
 * 保留原值，避免把带毫秒的原值因控件精度而悄悄改写。
 */
function setDateField(field: GovernanceDateField, value: string) {
  // 控件精度只到秒：同一秒内的回显不算用户修改，保留原值（含毫秒）
  if (value && sameGovernanceInputInstant(value, form.value[field])) return
  form.value[field] = value
}

/**
 * 无法解析的原始输入原样留在表单里（因此绝不会被当成空值清空）；
 * datetime-local 控件无法显示它，用行内提示说明，保存时仍会报 invalidDate。
 */
function displayDate(raw: string): string {
  if (!raw) return ''
  const parsed = parseGovernanceDate(raw)
  return parsed.ok && parsed.iso !== null ? formatLocalGovernanceInput(parsed.iso) : ''
}

function unparseableDate(raw: string): string {
  if (!raw) return ''
  return parseGovernanceDate(raw).ok ? '' : raw
}

const reviewLocal = computed({
  get: () => displayDate(form.value.last_reviewed_at),
  set: (value: string) => setDateField('last_reviewed_at', value)
})
const fromLocal = computed({
  get: () => displayDate(form.value.valid_from),
  set: (value: string) => setDateField('valid_from', value)
})
const untilLocal = computed({
  get: () => displayDate(form.value.valid_until),
  set: (value: string) => setDateField('valid_until', value)
})

function clearDate(field: GovernanceDateField) {
  form.value[field] = ''
}

function onEnabledChange(value: string | number | boolean | undefined) {
  if (value === true || value === false) form.value.enabled = value
}

function close() {
  if (saving.value) return
  stopWrites()
  emit('update:modelValue', false)
}

function beforeClose(done: () => void) {
  if (saving.value) return
  // 在弹窗真正开始关闭前就作废：不等 closed 事件，避免关闭动画期间迟到的结果落地
  stopWrites()
  done()
}

function onVisibilityChange(value: boolean) {
  if (!value) stopWrites()
  emit('update:modelValue', value)
}

function onClosed() {
  // 只在确实已关闭时清零；若关闭事件迟到而新会话已打开，绝不清掉新会话状态
  if (props.modelValue) return
  stopWrites()
}

function messageForPlan(status: string): string {
  if (status === 'empty') return t('documents.governance.dialog.emptyPatch')
  if (status === 'invalidRevision') return t('documents.governance.dialog.revisionUnknown')
  if (status === 'invalidEnabled' || status === 'supersededEnabledChange') {
    return t('documents.governance.dialog.supersededLockHint')
  }
  if (status === 'invalidDate') return t('documents.governance.dialog.invalidDate')
  if (status === 'invalidWindow') return t('documents.governance.dialog.invalidWindow')
  return t('documents.governance.dialog.maintainerTooLong')
}

function errorMessage(error: unknown, fallback: string): string {
  const message = (error as { response?: { data?: { message?: unknown } } })?.response?.data?.message
  return typeof message === 'string' && message ? message : fallback
}

/**
 * 409 之后（以及刷新失败之后）一律先作废版本号：旧 revision 绝不能再用于提交。
 * 只有随后成功拉到"属于本会话、且 revision 有效"的详情才会恢复。
 */
function invalidateRevisionForConflict(captured: GovernanceRequestIdentity, isCurrent: () => boolean) {
  if (!isCurrent()) return
  snapshot.value = invalidateGovernanceRevision(snapshot.value)
}

/**
 * 409 后拉取新鲜详情：更新快照与表单并要求重审，保留开窗（绝不自动重试写）。
 * 刷新失败或响应属于别的 ID/KB 时保持版本号失效，并给出明确提示。
 */
async function refreshAfterConflict(
  captured: GovernanceRequestIdentity,
  isCurrent: () => boolean
) {
  conflict.value = true
  // 先失效：即便下面的 await 被作废或刷新失败，旧版本也已被判废
  invalidateRevisionForConflict(captured, isCurrent)
  let data: DocumentItem
  try {
    ;({ data } = await docApi.get(captured.kbId, captured.docId))
  } catch {
    if (!isCurrent()) return
    formError.value = t('documents.governance.dialog.refreshFailed')
    return
  }
  // 错 ID / 错 KB 的响应不得写入，也不得恢复版本号
  if (!belongsToGovernanceSession(captured, currentState(), data)) return
  const fresh = toGovernanceSnapshot(data)
  if (!fresh.revision_known) {
    // 刷新回来的详情本身没有可用版本号：保持失效并明确告知
    snapshot.value = fresh
    resetFormFromSnapshot(fresh)
    formError.value = t('documents.governance.dialog.refreshFailed')
    return
  }
  snapshot.value = fresh
  resetFormFromSnapshot(fresh)
  // conflict 提示条已说明"已载入最新版本，请确认后重新提交"
  formError.value = ''
  emit('refreshed', data)
}

/**
 * 统一执行一次治理写入：同一文档的写入互斥（与父组件的替代共享一把锁），
 * 并在每个 await 之后用会话守卫丢弃迟到结果。
 */
async function runGovernanceWrite(
  captured: GovernanceRequestIdentity,
  write: () => Promise<DocumentItem>,
  hooks: { savedKey: string; failedKey: string }
): Promise<void> {
  if (!aliveFor(captured)) return
  if (!governanceWriteLock.acquire(captured.kbId, captured.docId)) {
    // 同一文档已有另一入口在写（如替代对话框）：不并发，也不假装成功
    formError.value = t('documents.governance.dialog.writeBusy')
    return
  }
  try {
    await controller.run<DocumentItem>({
      alive: () => aliveFor(captured),
      write,
      onSaved: (data) => {
        ElMessage.success(t(hooks.savedKey))
        emit('saved', data)
      },
      onConflict: (isCurrent) => refreshAfterConflict(captured, isCurrent),
      onError: (error) => {
        formError.value = errorMessage(error, t(hooks.failedKey))
      }
    })
  } finally {
    governanceWriteLock.release(captured.kbId, captured.docId)
  }
}

async function save() {
  if (!props.modelValue || !props.doc || !snapshot.value) return
  // 已替代文档仍可改普通元数据；enabled 变更必须走显式 reenable
  const plan = planGovernancePatch(snapshot.value, form.value, {
    superseded: isSupersededDoc.value
  })
  if (plan.status !== 'ok') {
    formError.value = messageForPlan(plan.status)
    return
  }
  formError.value = ''
  conflict.value = false
  const captured: GovernanceRequestIdentity = { kbId: props.kbId, docId: props.doc.id, epoch: generation }
  await runGovernanceWrite(
    captured,
    async () => {
      const { data } = await docApi.updateGovernance(captured.kbId, captured.docId, plan.body)
      if (data.id !== captured.docId || data.kb_id !== captured.kbId) {
        throw new Error('governance response does not belong to this document')
      }
      return data
    },
    {
      savedKey: 'documents.governance.toast.saved',
      failedKey: 'documents.governance.toast.saveFailed'
    }
  )
}

async function doReenable() {
  if (!props.modelValue || !props.doc || !snapshot.value) return
  if (!snapshot.value.revision_known) {
    formError.value = t('documents.governance.dialog.revisionUnknown')
    return
  }
  formError.value = ''
  conflict.value = false
  const captured: GovernanceRequestIdentity = { kbId: props.kbId, docId: props.doc.id, epoch: generation }
  const revision = snapshot.value.governance_revision
  await runGovernanceWrite(
    captured,
    async () => {
      const { data } = await docApi.reenable(captured.kbId, captured.docId, {
        expected_revision: revision
      })
      if (data.id !== captured.docId || data.kb_id !== captured.kbId) {
        throw new Error('reenable response does not belong to this document')
      }
      return data
    },
    {
      savedKey: 'documents.governance.toast.reenabled',
      failedKey: 'documents.governance.toast.reenableFailed'
    }
  )
}

function askReplacement() {
  if (!props.doc || saving.value) return
  formError.value = ''
  emit('supersede-request', props.doc)
}

// flush:'sync'：关闭/切 KB/切文档必须在同一个 tick 内立即作废在途请求，
// 不能等到 pre-flush 的渲染时机，否则该间隙内迟到的响应仍会被当成有效。
watch(() => props.modelValue, (open) => {
  if (disposed) return
  if (open) openForDoc(props.doc)
  else stopWrites()
}, { flush: 'sync' })

// 切换文档/KB：先同步作废在途请求，再按新快照重建表单。
// 只看身份（KB+docId）：409 刷新会发同 id 的新对象，那不算切换。
watch(() => [props.doc?.id, props.doc?.governance_revision, props.kbId], () => {
  if (disposed) return
  const next = docKey(props.doc, props.kbId)
  if (next !== boundKey) {
    stopWrites()
    if (props.modelValue) openForDoc(props.doc)
    return
  }
  if (!props.modelValue || !props.doc || !snapshot.value) return
  // 同一文档被外部推进到新版本（如替代成功/他人编辑）：旧版本号立刻不可再提交，
  // 但保留用户当前输入，交由其确认后重新提交。
  const fresh = toGovernanceSnapshot(props.doc)
  if (!fresh.revision_known) {
    snapshot.value = invalidateGovernanceRevision(snapshot.value)
    conflict.value = true
    return
  }
  if (fresh.governance_revision === snapshot.value.governance_revision) return
  snapshot.value = {
    ...snapshot.value,
    revision_known: true,
    governance_revision: fresh.governance_revision
  }
  conflict.value = true
}, { flush: 'sync' })

onBeforeUnmount(() => {
  disposed = true
  stopWrites()
})
</script>

<style scoped>
.gov-alert { margin-bottom: 10px; }
.gov-revision { margin: 0 0 12px; color: var(--text-3); font-size: 12px; }
.gov-revision strong { color: var(--text); margin-left: 4px; font-variant-numeric: tabular-nums; }
.gov-form { margin: 0; }
.gov-form :deep(.el-form-item) { margin-bottom: 14px; }
.gov-hint { display: block; margin-top: 4px; color: var(--text-3); font-size: 11px; line-height: 1.6; }
.gov-hint-switch { display: inline-block; margin-left: 10px; }
.gov-hint-switch:empty { display: none; }
.gov-hint-bad { color: var(--danger); }
.gov-window { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.gov-input {
  width: 100%;
  padding: 5px 8px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}
.gov-input:focus-visible { outline: 2px solid var(--brand); outline-offset: 1px; }
.gov-input:disabled { cursor: not-allowed; opacity: .6; }
.gov-error { margin: 8px 0 0; color: var(--danger); font-size: 12px; line-height: 1.6; }
.gov-dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.gov-extra-actions { display: flex; gap: 8px; }
.gov-buttons { display: flex; gap: 8px; margin-left: auto; }
@media (max-width: 640px) {
  .gov-window { grid-template-columns: 1fr; }
  .gov-dialog-footer { align-items: stretch; flex-direction: column; }
  .gov-buttons { margin-left: 0; }
  .gov-buttons .el-button { flex: 1; margin-left: 0; }
}
</style>
