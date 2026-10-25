<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>审计日志</h2>
        <p>记录登录、知识库与文档变更、问答等关键操作</p>
      </div>
      <div class="page-actions">
        <el-select
          v-model="actionFilter"
          placeholder="全部操作"
          clearable
          class="filter"
          @change="onFilter"
        >
          <el-option v-for="(label, key) in ACTION_TEXT" :key="key" :label="label" :value="key" />
        </el-select>
        <el-input
          v-model="usernameFilter"
          placeholder="按用户名筛选"
          clearable
          class="filter"
          :prefix-icon="Search"
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
        <span class="muted">共 {{ total }} 条，按时间倒序</span>
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
            <span :class="{ muted: !row.target }">{{ row.target || '—' }}</span>
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
            <span v-else class="detail-preview">{{ summarize(row.detail) }}</span>
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

/** 详情对象压成一行摘要，完整内容悬浮查看 */
function summarize(detail: Record<string, unknown>) {
  if (typeof detail !== 'object' || detail === null) return String(detail)
  const entries = Object.entries(detail)
  if (!entries.length) return '—'
  return entries
    .map(([key, value]) => {
      const text = Array.isArray(value) ? value.join(', ') : String(value)
      return `${key}: ${text}`
    })
    .join(' · ')
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
  width: 150px;
}

.count-badge {
  display: inline-block;
  margin-left: 6px;
  padding: 0 7px;
  border-radius: 999px;
  background: var(--brand-soft);
  color: var(--brand);
  font-size: 11.5px;
  font-weight: 600;
  line-height: 18px;
  vertical-align: 1px;
}

.actor {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.actor-avatar {
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  background: linear-gradient(135deg, #9aa6bc, #7b8698);
}

.actor-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
  color: var(--text);
}

.ip {
  padding: 2px 7px;
  border-radius: 5px;
  background: #f4f6fb;
  font-size: 11.5px;
  color: var(--text-2);
  font-family: 'JetBrains Mono', Consolas, Monaco, monospace;
}

.detail-preview {
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.5;
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.time {
  font-size: 12.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}

@media (max-width: 900px) {
  .filter {
    width: 120px;
  }
}
</style>
