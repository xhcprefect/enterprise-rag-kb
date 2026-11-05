<template>
  <div v-if="visible && conversationId" class="memory-bar" role="status" :aria-label="t('chat.memory.label')">
    <span class="memory-dot" :class="dotClass"></span>
    <span class="memory-text">{{ statusText }}</span>
    <el-button v-if="viewable && !busy" link type="primary" size="small" @click="openView">
      {{ t('chat.memory.view') }}
    </el-button>
    <el-button v-if="showRefresh" link type="primary" size="small" @click="forceRefresh">
      {{ t('chat.memory.refresh') }}
    </el-button>
    <el-button v-if="canResetNow" link type="danger" size="small" :loading="resetting" :disabled="busy || resetting || confirming" @click="confirmReset">
      {{ t('chat.memory.reset') }}
    </el-button>

    <el-dialog :model-value="viewOpen" :title="t('chat.memory.viewTitle')" width="min(560px, 92vw)"
      @update:model-value="viewOpen = $event">
      <template v-if="status">
        <dl class="memory-meta">
          <div><dt>{{ t('chat.memory.covered') }}</dt><dd>{{ covered }}</dd></div>
          <div><dt>{{ t('chat.memory.lag') }}</dt><dd>{{ lagLabel }}</dd></div>
          <div><dt>{{ t('chat.memory.truncated') }}</dt><dd>{{ status.truncated ? t('chat.memory.yes') : t('chat.memory.no') }}</dd></div>
          <div v-if="status.version != null"><dt>{{ t('chat.memory.version') }}</dt><dd>{{ status.version }}</dd></div>
          <div v-if="status.updated_at"><dt>{{ t('chat.memory.updatedAt') }}</dt><dd>{{ formatTime(status.updated_at) }}</dd></div>
        </dl>
        <el-alert class="memory-lossy" :title="t('chat.memory.nonLossless')" type="warning" :closable="false" show-icon />
        <p v-if="status.structured && status.structured.text" class="memory-structured-head">{{ t('chat.memory.structuredText') }}</p>
        <pre v-if="status.structured && status.structured.text" class="memory-structured">{{ status.structured.text }}</pre>
        <pre v-else-if="status.structured" class="memory-structured">{{ prettyStructured }}</pre>
      </template>
      <p v-else>{{ t('chat.memory.loading') }}</p>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { memoryApi, type MemoryStatus } from '@/api'
import { canViewSummary, deriveMemoryDisplay, type MemoryDisplay } from '@/utils/memoryStatus'
import { MemoryController, type MemoryView } from '@/utils/memoryController'

const props = defineProps<{
  /** 当前打开的会话；null 表示新建会话/无会话。 */
  conversationId: string | null
  /** chat 正在生成或恢复中时禁止重置。 */
  busy: boolean
  /** 是否展示（mode=chat 且有会话时）。 */
  visible: boolean
  /** 父级显式刷新信号（如 SSE meta/done 里的 memory 快照）；递增一次就重新读取。 */
  refreshKey?: number
}>()

const { t } = useI18n()
const status = ref<MemoryStatus | null>(null)
const viewOpen = ref(false)
const loading = ref(false)
const resetting = ref(false)
const confirming = ref(false)
const pollExpired = ref(false)
const display = ref<MemoryDisplay>({ kind: 'disabled' })

function applyStatus(view: MemoryView) {
  status.value = view as MemoryStatus
  display.value = deriveMemoryDisplay({
    enabled: view.enabled,
    state: view.state,
    covered_turn_seq: view.covered_turn_seq,
    version: view.version,
    lag: view.lag,
    truncated: view.truncated,
    error_message: view.error_message
  })
}

const controller = new MemoryController({
  load: (conversationId, signal) => memoryApi.get(conversationId, signal)
    .then((response) => (response.status === 200 && response.data ? response.data as MemoryView : null))
    .catch(() => null),
  reset: (conversationId, expectedVersion) => memoryApi.reset(conversationId, expectedVersion)
    .then((response) => (response.status === 200 ? 'ok' as const : response.status === 409 ? 'conflict' as const : 'error' as const))
    .catch(() => 'error' as const),
  onView: (view) => applyStatus(view),
  onClear: () => {
    status.value = null
    display.value = { kind: 'disabled' }
  },
  onLoading: (value) => { loading.value = value },
  onPollExpired: () => { pollExpired.value = true }
})
controller.setBusy(props.busy)

onBeforeUnmount(() => { controller.dispose() })

// 切会话 / 开关显示：整段重新读取（作废旧代次），并清掉旧会话的轮询超时标记。
watch(
  () => [props.visible, !!props.conversationId, props.conversationId] as const,
  () => {
    pollExpired.value = false
    controller.onContext(props.visible && !!props.conversationId, props.conversationId)
  },
  { immediate: true }
)

// 本轮 chat 生成/恢复结束（busy true→false）后，摘要可能刚发布，须重新读取。
watch(() => props.busy, (busy, prev) => {
  controller.setBusy(busy)
  if (!busy && prev) {
    // 生成结束也清掉超时标记，因为将要重新读取。
    pollExpired.value = false
    controller.onBusyEnd()
  }
})

// 父级显式刷新信号（如 SSE memory 快照）：重新读取，并清掉旧超时标记。
watch(() => props.refreshKey, (key, prev) => {
  if (typeof key === 'number' && key !== prev) {
    pollExpired.value = false
    controller.onRefresh()
  }
})

const covered = computed(() =>
  status.value && Number.isInteger(status.value.covered_turn_seq) && status.value.covered_turn_seq >= 0
    ? status.value.covered_turn_seq
    : 0)

const lagLabel = computed(() => {
  if (status.value == null || typeof status.value.lag !== 'boolean') return t('chat.memory.unknown')
  return status.value.lag ? t('chat.memory.yes') : t('chat.memory.no')
})

const statusText = computed(() => {
  if (!status.value) return loading.value ? t('chat.memory.loading') : t('chat.memory.unavailable')
  const d = display.value
  if (d.kind === 'disabled') return t('chat.memory.disabled')
  if (d.kind === 'idle') return t('chat.memory.idle')
  if (d.kind === 'pending') return t('chat.memory.pending', { covered: d.covered })
  if (d.kind === 'failed') return d.message ? d.message : t('chat.memory.failed')
  // ready
  if (d.lag === true) return t('chat.memory.readyLag', { covered: d.covered })
  if (d.lag === null) return t('chat.memory.readyUnknown', { covered: d.covered })
  return t('chat.memory.ready', { covered: d.covered })
})

const dotClass = computed(() => {
  const d = display.value
  if (d.kind === 'disabled' || d.kind === 'idle') return 'is-muted'
  if (d.kind === 'pending') return 'is-pending'
  if (d.kind === 'failed') return 'is-failed'
  return 'is-ready'
})

/** 是否有已发布的摘要可只读查看（ready 恒有；pending/failed 依历史版本号）。 */
const viewable = computed(() => {
  if (!status.value || !status.value.enabled) return false
  return canViewSummary(status.value.state, status.value.version)
})

const canResetNow = computed(() => {
  if (!status.value || !status.value.enabled || props.busy || resetting.value || confirming.value) return false
  // disabled/idle 没有已发布摘要可重置，不显示无意义的重置按钮。
  const d = display.value
  if (d.kind === 'disabled' || d.kind === 'idle') return false
  // queued/processing 后台摘要允许重置（epoch 重置作废旧 job）；生成中的拦截由 busy 负责。
  return status.value.state !== 'disabled'
})

const showRefresh = computed(() => {
  if (!props.conversationId || !props.visible) return false
  if (pollExpired.value) return true
  // 读取失败且没有数据时，给手动刷新入口。
  return !status.value && !loading.value
})

const prettyStructured = computed(() => {
  const s = status.value?.structured
  if (!s) return ''
  try { return JSON.stringify(s, null, 2) } catch { return String(s) }
})

function formatTime(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

function openView() {
  viewOpen.value = true
}

async function forceRefresh() {
  pollExpired.value = false
  controller.onRefresh()
}

async function confirmReset() {
  if (props.busy || resetting.value || confirming.value) return
  // 在确认框之前冻结重置目标并上锁：杜绝多次点击堆叠多个确认。
  const frozen = controller.freezeReset()
  if (!frozen) return
  confirming.value = true
  resetting.value = true
  try {
    let confirmed = false
    try {
      await ElMessageBox.confirm(t('chat.memory.resetConfirm'), t('chat.memory.resetTitle'), {
        type: 'warning',
        confirmButtonText: t('chat.memory.reset'),
        cancelButtonText: t('common.cancel')
      })
      confirmed = true
    } catch {
      confirmed = false
    }
    if (!confirmed) {
      controller.abortReset()
      return
    }
    const outcome = await frozen.commit()
    switch (outcome) {
      case 'stale':
        // 确认期间切了会话/卸载/开始生成：不发，避免误重置别的会话。
        return
      case 'conflict':
        ElMessage.info(t('chat.memory.resetConflict'))
        controller.onRefresh()
        return
      case 'ok':
        ElMessage.success(t('chat.memory.resetDone'))
        controller.onRefresh()
        return
      default:
        ElMessage.error(t('chat.memory.resetFailed'))
        return
    }
  } finally {
    resetting.value = false
    confirming.value = false
  }
}
</script>

<style scoped>
.memory-bar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 4px 0 8px; font-size: 12px; color: var(--text-3);
}
.memory-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-3); }
.memory-dot.is-pending { background: var(--el-color-warning); animation: blink 1.2s infinite; }
.memory-dot.is-ready { background: var(--el-color-success); }
.memory-dot.is-failed { background: var(--el-color-danger); }
@keyframes blink { 0%,100% {opacity: 1} 50% {opacity: .35} }
.memory-text { overflow-wrap: anywhere; }
.memory-meta { display: flex; flex-wrap: wrap; gap: 10px 22px; margin: 0; padding: 4px 0 10px; }
.memory-meta dt { color: var(--text-3); font-size: 12px; }
.memory-meta dd { margin: 2px 0 0; font-size: 13px; }
.memory-lossy { margin-bottom: 12px; }
.memory-structured-head { margin: 4px 0; font-size: 13px; color: var(--text); }
.memory-structured { white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; font-size: 13px; line-height: 1.7; background: var(--surface-soft); border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin: 0; max-height: 40vh; overflow: auto; }
</style>
