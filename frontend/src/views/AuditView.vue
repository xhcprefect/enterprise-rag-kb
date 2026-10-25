<template>
  <div class="page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">AUDIT TRAIL</span>
        <h2>审计日志</h2>
        <p>记录登录、知识库与文档变更、问答等关键操作</p>
      </div>
      <div class="page-actions">
        <el-select
          v-model="actionFilter"
          placeholder="全部操作"
          clearable
          class="filter"
          aria-label="按操作类型筛选"
          @change="onFilter"
        >
          <el-option v-for="(label, key) in ACTION_TEXT" :key="key" :label="label" :value="key" />
        </el-select>
        <el-input
          v-model="usernameFilter"
          placeholder="按用户名筛选"
          clearable
          class="filter user-filter"
          :prefix-icon="Search"
          aria-label="按用户名筛选"
          @keyup.enter="onFilter"
          @clear="onFilter"
        />
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>刷新
        </el-button>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          操作记录
          <span class="count-badge">{{ total }}</span>
        </div>
        <span class="muted">按时间倒序排列</span>
      </div>

      <el-table v-loading="loading" :data="logs">
        <el-table-column label="用户" width="180">
          <template #default="{ row }">
            <div class="actor">
              <el-avatar :size="26" class="actor-avatar">
                {{ (row.username || '?').charAt(0).toUpperCase() }}
              </el-avatar>
              <span class="actor-name">{{ row.username || '匿名' }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="actionType(row.action)" effect="light">
              {{ actionText(row.action) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="对象" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ 'is-empty': !row.target }">{{ row.target || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="ip" label="IP" width="140">
          <template #default="{ row }">
            <code class="ip">{{ row.ip || '—' }}</code>
          </template>
        </el-table-column>

        <el-table-column label="详情" min-width="220">
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

        <el-table-column label="时间" width="170">
          <template #default="{ row }">
            <span class="time">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无审计记录" :image-size="80" />
        </template>
      </el-table>

      <div class="panel-foot">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="load"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Search } from '@element-plus/icons-vue'
import client from '@/api/client'

interface LogItem {
  id: string
  username: string
  action: string
  target: string
  detail: Record<string, unknown> | null
  ip: string
  created_at: string
}

const logs = ref<LogItem[]>([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const actionFilter = ref('')
const usernameFilter = ref('')

const ACTION_TEXT: Record<string, string> = {
  login_success: '登录成功',
  login_failed: '登录失败',
  logout: '退出登录',
  user_create: '创建用户',
  user_update: '更新用户',
  user_delete: '删除用户',
  user_import: '批量导入用户',
  password_change: '修改密码',
  kb_create: '创建知识库',
  kb_update: '更新知识库',
  kb_delete: '删除知识库',
  document_upload: '上传文档',
  document_delete: '删除文档',
  chat_query: '知识问答'
}

function actionText(action: string) {
  return ACTION_TEXT[action] || action
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

function formatTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function onFilter() {
  page.value = 1
  load()
}

async function load() {
  loading.value = true
  try {
    const { data } = await client.get<{ total: number; items: LogItem[] }>('/audit-logs', {
      params: {
        page: page.value,
        page_size: pageSize.value,
        action: actionFilter.value || undefined,
        username: usernameFilter.value || undefined
      }
    })
    logs.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.filter {
  width: 156px;
}

.user-filter {
  width: 184px;
}

.actor {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.actor-avatar {
  flex-shrink: 0;
  border: 1px solid #dbe7d9;
  background: #edf2e8;
  color: #486246;
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
  outline: 2px solid rgb(36 122 97 / 35%);
  outline-offset: 2px;
}

.time {
  color: var(--text-3);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}

:global(.audit-detail-tip) {
  max-width: min(420px, calc(100vw - 32px));
  padding: 9px 12px;
  color: #f3f7f4;
  font-size: 12px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 900px) {
  .filter {
    width: 132px;
  }

  .user-filter {
    width: 160px;
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
