<template>
  <div class="page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">{{ t('audit.eyebrow') }}</span>
        <h2>{{ t('audit.title') }}</h2>
        <p>{{ t('audit.subtitle') }}</p>
      </div>
      <div class="page-actions">
        <el-select
          v-model="actionFilter"
          :placeholder="t('audit.allActions')"
          clearable
          class="filter"
          :aria-label="t('audit.actionFilterAria')"
          @change="onFilter"
        >
          <el-option v-for="(label, key) in ACTION_TEXT" :key="key" :label="label" :value="key" />
        </el-select>
        <el-input
          v-model="usernameFilter"
          :placeholder="t('audit.usernamePlaceholder')"
          clearable
          class="filter user-filter"
          :prefix-icon="Search"
          :aria-label="t('audit.usernameAria')"
          @keyup.enter="onFilter"
          @clear="onFilter"
        />
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>{{ t('common.refresh') }}
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="listError"
      :title="t('errors.request')"
      type="error"
      :closable="false"
      show-icon
      role="alert"
      data-testid="audit-load-error"
    />

    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          {{ t('audit.listTitle') }}
          <span class="count-badge">{{ total }}</span>
        </div>
        <span class="muted">{{ t('audit.listHint') }}</span>
      </div>

      <el-table v-loading="loading" :data="logs">
        <el-table-column :label="t('audit.colUser')" width="180">
          <template #default="{ row }">
            <div class="actor">
              <el-avatar :size="26" class="actor-avatar">
                {{ (row.username || '?').charAt(0).toUpperCase() }}
              </el-avatar>
              <span class="actor-name">{{ row.username || t('audit.anonymous') }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="t('audit.colAction')" width="200">
          <template #default="{ row }">
            <el-tag size="small" :type="actionType(row.action)" effect="light">
              {{ actionText(row.action) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('audit.colTarget')" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ 'is-empty': !row.target }">{{ row.target || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="ip" :label="t('audit.colIp')" width="140">
          <template #default="{ row }">
            <code class="ip">{{ row.ip || '—' }}</code>
          </template>
        </el-table-column>

        <el-table-column :label="t('audit.colDetail')" min-width="220">
          <template #default="{ row }">
            <span v-if="!row.detail" class="muted">—</span>
            <el-tooltip
              v-else
              :content="detailText(row.detail)"
              placement="top"
              :trigger="['hover', 'focus']"
              :show-after="180"
              popper-class="audit-detail-tip"
            >
              <span class="detail-preview" tabindex="0">{{ summarize(row.detail) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column :label="t('audit.colTime')" width="170">
          <template #default="{ row }">
            <span class="time">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty :description="t('audit.empty')" :image-size="80" />
        </template>
      </el-table>

      <div class="panel-foot">
        <el-pagination
          :current-page="page"
          :page-size="pageSize"
          :total="total"
          :disabled="loading"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @update:current-page="onPageChange"
          @update:page-size="onPageSizeChange"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Search } from '@element-plus/icons-vue'
import client from '@/api/client'
import { formatDateTime } from '@/i18n/format'
import { useAuditActionsI18n } from '@/i18n/messages/auditActions'
import { createAdminListLoader } from '@/utils/adminList'

interface LogItem {
  id: string
  username: string
  action: string
  target: string
  detail: Record<string, unknown> | null
  ip: string
  created_at: string
}

const { t } = useI18n()
const { aa } = useAuditActionsI18n()

const logs = ref<LogItem[]>([])
const loading = ref(false)
const listError = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const actionFilter = ref('')
const usernameFilter = ref('')

const ACTION_TEXT = computed<Record<string, string>>(() => ({
  login_success: t('audit.actions.loginSuccess'),
  login_failed: t('audit.actions.loginFailed'),
  logout: t('audit.actions.logout'),
  user_create: t('audit.actions.userCreate'),
  user_update: t('audit.actions.userUpdate'),
  user_delete: t('audit.actions.userDelete'),
  user_import: t('audit.actions.userImport'),
  password_change: t('audit.actions.passwordChange'),
  kb_create: t('audit.actions.kbCreate'),
  kb_update: t('audit.actions.kbUpdate'),
  kb_delete: t('audit.actions.kbDelete'),
  kb_member_update: aa('kbMemberUpdate'),
  kb_member_remove: aa('kbMemberRemove'),
  document_upload: t('audit.actions.documentUpload'),
  document_delete: t('audit.actions.documentDelete'),
  chat_query: t('audit.actions.chatQuery'),
  conversation_metadata: t('audit.actions.conversationMetadata'),
  project_kb_delete: t('audit.actions.projectKbDelete'),
  document_governance_update: t('audit.actions.documentGovernanceUpdate'),
  document_supersede: t('audit.actions.documentSupersede'),
  document_reenable: t('audit.actions.documentReenable'),
  model_price_update: t('audit.actions.modelPriceUpdate'),
  feedback_export: t('audit.actions.feedbackExport'),
  feedback_review: t('audit.actions.feedbackReview'),
  feedback_view_detail: t('audit.actions.feedbackViewDetail'),
  feedback_regression_enroll: t('audit.actions.feedbackRegressionEnroll'),
  feedback_regression_prepare: t('audit.actions.feedbackRegressionPrepare'),
  feedback_regression_export: t('audit.actions.feedbackRegressionExport'),
  feedback_regression_import: t('audit.actions.feedbackRegressionImport'),
  feedback_regression_job_create: aa('feedbackRegressionJobCreate'),
  feedback_regression_batch_create: aa('feedbackRegressionBatchCreate'),
  feedback_regression_job_cancel: aa('feedbackRegressionJobCancel'),
  feedback_regression_job_review: aa('feedbackRegressionJobReview'),
  feedback_regression_batch_cancel: aa('feedbackRegressionBatchCancel')
}))

function actionText(action: string) {
  return Object.prototype.hasOwnProperty.call(ACTION_TEXT.value, action)
    ? ACTION_TEXT.value[action]
    : action
}

function actionType(action: string) {
  if (action.includes('failed') || action.includes('delete')) return 'danger'
  if (action.includes('login') || action.includes('logout')) return 'info'
  return 'success'
}

/** 未知值转成可读文本，对象用 JSON，避免出现 [object Object]。 */
function detailValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (Array.isArray(value)) {
    return value.length ? value.map(detailValue).join(', ') : '—'
  }
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch {
      return '—'
    }
  }
  return String(value)
}

/** 详情对象压成一行摘要，完整内容通过纯文本 tooltip 查看。 */
function summarize(detail: Record<string, unknown> | null) {
  if (typeof detail !== 'object' || detail === null) return detailValue(detail)
  const entries = Object.entries(detail)
  if (!entries.length) return '—'
  return entries.map(([key, value]) => `${key}: ${detailValue(value)}`).join(' · ')
}

/** 与摘要同源，仅供 tooltip 使用，始终按纯文本渲染。 */
function detailText(detail: Record<string, unknown> | null) {
  return summarize(detail)
}

const listLoader = createAdminListLoader(
  { items: logs, total, loading, error: listError },
  async (params: { page: number; page_size: number; action?: string; username?: string }, signal) => {
    const { data } = await client.get<{ total: number; items: LogItem[] }>('/audit-logs', { params, signal })
    return data
  }
)

function load() {
  return listLoader.load({
    page: page.value,
    page_size: pageSize.value,
    action: actionFilter.value || undefined,
    username: usernameFilter.value || undefined
  })
}

function onFilter() {
  page.value = 1
  void load()
}

function onPageChange(value: number) {
  // 清空 total 会触发组件自动改页；在途反馈不能反过来覆盖请求的页码快照。
  if (loading.value || page.value === value) return
  page.value = value
  void load()
}

function onPageSizeChange(value: number) {
  if (loading.value || pageSize.value === value) return
  pageSize.value = value
  page.value = 1
  void load()
}

// 响应后再收敛页码，不能让加载时的临时 total=0 把页码改回第一页。
watch(loading, busy => {
  if (busy || listError.value) return
  const lastPage = Math.max(1, Math.ceil(total.value / pageSize.value))
  if (page.value > lastPage) onPageChange(lastPage)
})

onMounted(load)
onBeforeUnmount(listLoader.dispose)
</script>

<style scoped>
.filter {
  width: 180px;
}

.user-filter {
  width: 220px;
}

.actor {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.actor-avatar {
  flex-shrink: 0;
  border: 1px solid var(--line);
  background: var(--slate-soft);
  color: var(--slate-text);
  font-size: 11px;
  font-weight: 600;
}

.actor-name {
  overflow: hidden;
  color: var(--text);
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.is-empty {
  color: var(--text-3);
}

.ip {
  display: inline-block;
  padding: 2px 7px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--surface-soft);
  color: var(--text-2);
  font-family: 'Cascadia Code', Consolas, Monaco, monospace;
  font-size: 11.5px;
  line-height: 1.5;
}

.detail-preview {
  display: block;
  max-width: 100%;
  padding: 2px 0;
  overflow: hidden;
  border-radius: 4px;
  color: var(--text-3);
  font-size: 12.5px;
  line-height: 1.55;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: help;
}

.detail-preview:hover {
  color: var(--text-2);
}

.detail-preview:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: 2px;
}

.time {
  color: var(--text-3);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}

:global(.el-popper.is-dark.audit-detail-tip) {
  max-width: min(420px, calc(100vw - 32px));
  padding: 9px 12px;
  border: 1px solid var(--line-strong);
  background: var(--text);
  color: var(--surface);
  font-size: 12px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 900px) {
  .filter {
    width: 150px;
  }

  .user-filter {
    width: 190px;
  }
}

@media (max-width: 640px) {
  .filter,
  .user-filter {
    flex: 1;
    width: auto;
    min-width: 130px;
  }
}
</style>
