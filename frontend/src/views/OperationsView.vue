<template>
  <div class="page operations-page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">{{ t('operations.eyebrow') }}</span>
        <h2>{{ t('operations.title') }}</h2>
        <p>{{ t('operations.subtitle') }}</p>
      </div>
      <div class="page-actions">
        <el-button :loading="loading" :aria-label="t('operations.refresh')" @click="load">
          <el-icon aria-hidden="true"><Refresh /></el-icon>{{ t('common.refresh') }}
        </el-button>
      </div>
    </div>

    <section class="panel" :aria-busy="loading" aria-labelledby="operations-panel-title">
      <div class="panel-head">
        <div>
          <h3 id="operations-panel-title" class="panel-title">{{ t('operations.panelTitle') }}</h3>
          <p class="panel-hint">{{ t('operations.panelHint') }}</p>
        </div>
        <span v-if="snapshot" class="generated-at">
          {{ t('operations.generatedAt', { time: formatTime(snapshot.generated_at) }) }}
        </span>
      </div>

      <div v-if="loadFailed" class="notice notice--error" role="alert" data-testid="operations-load-error">
        <el-icon aria-hidden="true"><Warning /></el-icon>
        <span>{{ t('operations.loadFailed') }}</span>
      </div>
      <div v-if="snapshot?.degraded" class="notice notice--degraded" role="status">
        <el-icon aria-hidden="true"><Warning /></el-icon>
        <span>{{ t('operations.degraded') }}</span>
      </div>

      <div v-if="loading" class="load-state" role="status" aria-live="polite">
        <el-icon class="loading-icon" aria-hidden="true"><Loading /></el-icon>
        <span>{{ t('operations.loading') }}</span>
      </div>
      <el-empty
        v-else-if="snapshot && snapshot.roles.length === 0"
        :description="t('operations.empty')"
        :image-size="76"
      />
      <div v-else-if="snapshot" class="role-grid" role="list" :aria-label="t('operations.panelTitle')">
        <article
          v-for="role in snapshot.roles"
          :key="role.role"
          class="role-card"
          role="listitem"
          :aria-labelledby="`worker-${role.role}-title`"
        >
          <div class="role-head">
            <div class="role-heading">
              <h3 :id="`worker-${role.role}-title`">{{ t(`operations.roles.${role.role}`) }}</h3>
              <p>{{ t('operations.role') }}</p>
            </div>
            <div class="state-label" :class="`tone-${statusTone(stateOf(role))}`">
              <el-icon aria-hidden="true"><component :is="stateIcon(stateOf(role))" /></el-icon>
              <span>{{ t(workerLabelKey(stateOf(role))) }}</span>
            </div>
          </div>

          <dl class="role-details">
            <div class="detail-row">
              <dt>{{ t('operations.enabledStatus') }}</dt>
              <dd>
                <span>{{ t(role.enabled ? 'operations.enabled' : 'operations.notEnabled') }}</span>
                <small v-if="!role.enabled">{{ t('operations.notEnabledExplanation') }}</small>
              </dd>
            </div>
            <div class="detail-row">
              <dt>{{ t('operations.aliveCount') }}</dt>
              <dd>{{ formatCount(role.alive_count) }}</dd>
            </div>
            <div class="detail-row">
              <dt>{{ t('operations.heartbeat') }}</dt>
              <dd>{{ heartbeatText(role) }}</dd>
            </div>
            <div class="detail-row">
              <dt>{{ t('operations.pending') }}</dt>
              <dd>{{ formatCount(role.pending) }}</dd>
            </div>
            <div class="detail-row">
              <dt>{{ t('operations.processing') }}</dt>
              <dd>{{ formatCount(role.processing) }}</dd>
            </div>
            <div class="detail-row">
              <dt>{{ t('operations.staleTasks') }}</dt>
              <dd>{{ formatCount(role.stale_tasks) }}</dd>
            </div>
            <div class="detail-row">
              <dt>{{ t('operations.oldestPending') }}</dt>
              <dd>{{ formatAge(role.oldest_pending_age_seconds) }}</dd>
            </div>
            <div class="detail-row">
              <dt>{{ t('operations.lastFailure') }}</dt>
              <dd>
                <template v-if="role.last_failure">
                  <span>{{ t(failureLabelKey(role.last_failure.code)) }}</span>
                  <small>{{ t('operations.failureUpdatedAt', { time: formatTime(role.last_failure.updated_at) }) }}</small>
                </template>
                <span v-else-if="snapshot.degraded">{{ t('operations.unknownValue') }}</span>
                <span v-else>{{ t('operations.noFailure') }}</span>
              </dd>
            </div>
          </dl>
        </article>
      </div>

      <aside class="security-note" :aria-label="t('operations.securityTitle')">
        <el-icon aria-hidden="true"><InfoFilled /></el-icon>
        <div>
          <strong>{{ t('operations.securityTitle') }}</strong>
          <p>{{ t('operations.securityNote') }}</p>
        </div>
      </aside>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { Component } from 'vue'
import { useOperationsI18n } from '@/i18n/messages/operations'
import {
  CircleCheck, CircleClose, Clock, InfoFilled, Loading, QuestionFilled, Refresh, Remove, Warning
} from '@element-plus/icons-vue'
import { getWorkerStatus, type WorkerRoleStatus, type WorkerState, type WorkerStatusSnapshot } from '@/api/operations'
import {
  createRequestGate, failureLabelKey, formatAge as formatWorkerAge, formatCount as formatWorkerCount,
  formatWorkerTimestamp, isCancelledRequest, normalizeWorkerState, statusTone, workerLabelKey
} from '@/utils/operations'

const { t, locale } = useOperationsI18n()
const snapshot = ref<WorkerStatusSnapshot | null>(null)
const loading = ref(true)
const loadFailed = ref(false)
const requestGate = createRequestGate()
let activeController: AbortController | null = null

const stateIcons: Record<WorkerState, Component> = {
  disabled: Remove,
  not_started: Clock,
  running: CircleCheck,
  stale: Warning,
  stopped: CircleClose,
  unknown: QuestionFilled
}

function stateOf(role: WorkerRoleStatus): WorkerState {
  return normalizeWorkerState(role)
}

function stateIcon(state: WorkerState): Component {
  return stateIcons[state]
}

function formatCount(value: number | null): string {
  return formatWorkerCount(value, locale.value, t('operations.unknownValue'))
}

function formatAge(value: number | null): string {
  return formatWorkerAge(value, locale.value, t('operations.unknownValue'))
}

function formatTime(value: string | null): string {
  return formatWorkerTimestamp(value, locale.value, t('operations.notRecorded'))
}

/** Degraded snapshots could not query heartbeats; absence is unknown, not "not recorded". */
function heartbeatText(role: WorkerRoleStatus): string {
  if (snapshot.value?.degraded && !role.last_heartbeat_at) return t('operations.unknownValue')
  return formatTime(role.last_heartbeat_at)
}

async function load() {
  const requestId = requestGate.begin()
  activeController?.abort()
  const controller = new AbortController()
  activeController = controller
  snapshot.value = null
  loadFailed.value = false
  loading.value = true

  try {
    const result = await getWorkerStatus(controller.signal)
    if (requestGate.isCurrent(requestId)) snapshot.value = result
  } catch (error) {
    if (requestGate.isCurrent(requestId) && !isCancelledRequest(error)) loadFailed.value = true
  } finally {
    if (requestGate.isCurrent(requestId)) {
      loading.value = false
      activeController = null
    }
  }
}

onMounted(load)
onBeforeUnmount(() => {
  requestGate.invalidate()
  activeController?.abort()
  activeController = null
})
</script>

<style scoped>
.operations-page {
  max-width: 1440px;
}

.panel-head {
  align-items: flex-start;
  gap: 16px;
}

.panel-title {
  margin: 0;
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
}

.panel-hint {
  margin: 6px 0 0;
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.6;
}

.generated-at {
  flex-shrink: 0;
  color: var(--text-3);
  font-size: 11px;
  line-height: 1.6;
  text-align: right;
}

.notice,
.security-note,
.load-state {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 9px;
  color: var(--text-2);
  background: var(--surface-soft);
  font-size: 12px;
  line-height: 1.7;
}

.notice + .notice {
  margin-top: 10px;
}

.notice .el-icon,
.security-note > .el-icon,
.load-state .el-icon {
  flex-shrink: 0;
  margin-top: 2px;
}

.load-state {
  align-items: center;
  justify-content: center;
  min-height: 150px;
  color: var(--text-2);
}

.loading-icon {
  animation: spin 1s linear infinite;
}

.role-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
  gap: 14px;
}

.role-card {
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
}

.role-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 13px;
  border-bottom: 1px solid var(--line);
}

.role-heading {
  min-width: 0;
}

.role-heading h3 {
  margin: 0;
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.role-heading p {
  margin: 3px 0 0;
  color: var(--text-3);
  font-size: 11px;
}

.state-label {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  gap: 6px;
  color: var(--text-2);
  font-size: 11px;
  font-weight: 550;
  line-height: 1.5;
}

.tone-success {
  color: var(--brand);
}

.tone-warning,
.tone-danger {
  color: var(--text-2);
}

.tone-info,
.tone-muted {
  color: var(--text-3);
}

.role-details {
  display: grid;
  gap: 0;
  margin: 5px 0 0;
}

.detail-row {
  display: grid;
  grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
  gap: 12px;
  padding: 9px 0;
  border-bottom: 1px solid var(--line);
}

.detail-row:last-child {
  border-bottom: 0;
}

.detail-row dt,
.detail-row dd {
  min-width: 0;
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.detail-row dt {
  color: var(--text-3);
}

.detail-row dd {
  color: var(--text-2);
  font-variant-numeric: tabular-nums;
}

.detail-row dd small {
  display: block;
  margin-top: 3px;
  color: var(--text-3);
  font-size: 10.5px;
  font-variant-numeric: normal;
}

.security-note {
  margin-top: 14px;
}

.security-note strong {
  color: var(--text);
  font-size: 12px;
  font-weight: 600;
}

.security-note p {
  margin: 3px 0 0;
  color: var(--text-3);
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 640px) {
  .panel-head {
    flex-direction: column;
  }

  .generated-at {
    text-align: left;
  }

  .role-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .role-card {
    padding: 13px;
  }

  .detail-row {
    grid-template-columns: minmax(0, 0.85fr) minmax(0, 1.15fr);
    gap: 10px;
  }
}
</style>
