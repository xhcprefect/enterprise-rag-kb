<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>用户管理</h2>
        <p>管理员可创建账号、分配角色、重置密码</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="keyword"
          placeholder="搜索用户名/姓名"
          clearable
          class="search"
          :prefix-icon="Search"
          @keyup.enter="onSearch"
          @clear="onSearch"
        />
        <el-button @click="openImport">
          <el-icon><Upload /></el-icon>批量导入
        </el-button>
        <el-button type="primary" @click="openCreate">
          <el-icon><Plus /></el-icon>新建用户
        </el-button>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          账号列表
          <span class="count-badge">{{ total }}</span>
        </div>
      </div>

      <el-table v-loading="loading" :data="users">
        <el-table-column label="用户" min-width="200">
          <template #default="{ row }">
            <div class="user-cell">
              <el-avatar :size="32" class="user-avatar" :class="row.role">
                {{ (row.display_name || row.username).charAt(0).toUpperCase() }}
              </el-avatar>
              <div class="user-names">
                <span class="user-display">{{ row.display_name || row.username }}</span>
                <span class="user-account">@{{ row.username }}</span>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="邮箱" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ muted: !row.email }">{{ row.email || '未填写' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="角色" width="110">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'primary' : 'info'" size="small" effect="light">
              {{ row.role === 'admin' ? '管理员' : '普通用户' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <span class="status-cell">
              <span class="status-dot" :class="row.is_active ? 'on' : 'off'"></span>
              {{ row.is_active ? '启用' : '禁用' }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">
            <span class="time">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="warning" size="small" @click="resetPassword(row)">重置密码</el-button>
            <el-button
              link
              type="danger"
              size="small"
              :disabled="row.id === userStore.profile?.id"
              @click="remove(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty :description="keyword ? '没有匹配的用户' : '暂无用户'" :image-size="80" />
        </template>
      </el-table>

      <div class="panel-foot">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="load"
        />
      </div>
    </div>

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑用户' : '新建用户'" width="520px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <div class="form-grid">
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model="form.username"
              :disabled="!!editing"
              placeholder="字母、数字、下划线"
            />
          </el-form-item>
          <el-form-item v-if="!editing" label="初始密码" prop="password">
            <el-input v-model="form.password" type="password" show-password placeholder="至少 6 位" />
          </el-form-item>
          <el-form-item label="显示名称">
            <el-input v-model="form.display_name" placeholder="例如：张三" />
          </el-form-item>
          <el-form-item label="邮箱">
            <el-input v-model="form.email" placeholder="可选" />
          </el-form-item>
        </div>

        <el-form-item label="角色">
          <el-radio-group v-model="form.role">
            <el-radio value="user">普通用户</el-radio>
            <el-radio value="admin">管理员</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="editing" label="账号状态">
          <div class="switch-row">
            <el-switch v-model="form.is_active" />
            <span>{{ form.is_active ? '启用：可以正常登录' : '禁用：无法登录系统' }}</span>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 批量导入 -->
    <el-dialog
      v-model="importVisible"
      title="批量导入用户"
      width="680px"
      :close-on-click-modal="false"
      :before-close="handleDialogClose"
      @closed="resetImport"
    >
      <template v-if="!importResult">
        <el-alert type="info" :closable="false" show-icon class="import-tip">
          <template #title>
            从 Excel / CSV 批量建号。每人生成一个<strong>随机初始密码</strong>，导入完成后
            下载回执查看（密码不存明文，事后无法再导出）。
          </template>
        </el-alert>

        <div class="import-guide">
          <span>表格需包含「用户名」列，可选「姓名 / 邮箱 / 角色」。表头支持中英文，
            角色留空为普通用户。</span>
          <el-button link type="primary" size="small" @click="downloadTemplate">
            下载模板
          </el-button>
        </div>

        <el-upload
          drag
          :auto-upload="false"
          :show-file-list="false"
          accept=".xlsx,.csv"
          :on-change="onFileChange"
          class="import-drop"
        >
          <div class="drop-body">
            <el-icon class="drop-icon"><UploadFilled /></el-icon>
            <div v-if="importFile" class="drop-file">
              <span class="drop-name">{{ importFile.name }}</span>
              <span class="drop-size">{{ formatSize(importFile.size) }}</span>
            </div>
            <div v-else class="drop-hint">
              把 .xlsx 或 .csv 拖到这里，或<em>点击选择文件</em>
            </div>
            <div class="drop-limit">单次最多 {{ IMPORT_MAX_ROWS }} 行，文件不超过 5MB</div>
          </div>
        </el-upload>

        <!-- 上传到 100% 只是传输完成，服务端还要逐行建号（500 行约 30 秒），
             所以这里不置 success，避免"进度条满了却没结果"的错觉 -->
        <div v-if="importing" class="import-progress">
          <el-progress :percentage="importPercent" :show-text="false" />
          <span class="progress-text">
            {{ importPercent < 100 ? `正在上传 ${importPercent}%` : '上传完成，正在创建账号…' }}
          </span>
        </div>
      </template>

      <template v-else>
        <div class="import-summary">
          <div class="summary-item ok">
            <span class="summary-num">{{ importResult.created.length }}</span>
            <span class="summary-label">成功创建</span>
          </div>
          <div class="summary-item bad">
            <span class="summary-num">{{ importResult.failed.length }}</span>
            <span class="summary-label">未导入</span>
          </div>
          <div class="summary-item">
            <span class="summary-num">{{ importResult.total }}</span>
            <span class="summary-label">数据行</span>
          </div>
        </div>

        <el-alert
          v-if="importResult.created.length"
          type="warning"
          :closable="false"
          show-icon
          class="receipt-alert"
        >
          <template #title>
            关闭本窗口后就<strong>再也拿不到</strong>这些初始密码了，请先下载回执。
          </template>
          <el-button type="primary" size="small" class="receipt-btn" @click="downloadReceipt">
            下载回执（{{ importResult.created.length }} 个密码）
          </el-button>
        </el-alert>

        <el-table
          v-if="importResult.failed.length"
          :data="importResult.failed"
          size="small"
          max-height="260"
          class="fail-table"
        >
          <el-table-column prop="row" label="行号" width="70" />
          <el-table-column prop="username" label="用户名" min-width="130">
            <template #default="{ row }">
              <span :class="{ muted: !row.username }">{{ row.username || '（空）' }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="原因" min-width="240" show-overflow-tooltip />
        </el-table>
      </template>

      <template #footer>
        <template v-if="!importResult">
          <el-button :disabled="importing" @click="importVisible = false">取消</el-button>
          <el-button
            type="primary"
            :disabled="!importFile"
            :loading="importing"
            @click="submitImport"
          >
            {{ importing ? '导入中…' : '开始导入' }}
          </el-button>
        </template>
        <template v-else>
          <el-button type="primary" @click="downloadReceipt" :disabled="!importResult.created.length">
            下载回执
          </el-button>
          <el-button @click="finishImport">完成</el-button>
        </template>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import {
  ElMessage,
  ElMessageBox,
  type FormInstance,
  type FormRules,
  type UploadFile
} from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import {
  saveBase64File,
  saveBlob,
  userApi,
  type ImportResult,
  type User
} from '@/api'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()

const users = ref<User[]>([])
const loading = ref(false)
const keyword = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)

const dialogVisible = ref(false)
const submitting = ref(false)
const editing = ref<User | null>(null)
const formRef = ref<FormInstance>()

const form = reactive({
  username: '',
  password: '',
  display_name: '',
  email: '',
  role: 'user' as 'admin' | 'user',
  is_active: true
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: /^[A-Za-z0-9_.-]+$/, message: '只能包含字母、数字、下划线、点、横线', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入初始密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' }
  ]
}

function formatTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function load() {
  loading.value = true
  try {
    const { data } = await userApi.list({
      page: page.value,
      page_size: pageSize.value,
      keyword: keyword.value
    })
    users.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function onSearch() {
  page.value = 1
  load()
}

function openCreate() {
  editing.value = null
  Object.assign(form, {
    username: '',
    password: '',
    display_name: '',
    email: '',
    role: 'user',
    is_active: true
  })
  dialogVisible.value = true
}

function openEdit(row: User) {
  editing.value = row
  Object.assign(form, {
    username: row.username,
    password: '',
    display_name: row.display_name,
    email: row.email || '',
    role: row.role,
    is_active: row.is_active
  })
  dialogVisible.value = true
}

async function submit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    if (editing.value) {
      await userApi.update(editing.value.id, {
        display_name: form.display_name,
        email: form.email || null,
        role: form.role,
        is_active: form.is_active
      })
      ElMessage.success('已更新')
    } else {
      await userApi.create({ ...form, email: form.email || undefined })
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await load()
  } finally {
    submitting.value = false
  }
}

async function resetPassword(row: User) {
  let value: string
  try {
    const res = await ElMessageBox.prompt(`为「${row.username}」设置新密码`, '重置密码', {
      inputType: 'password',
      inputPattern: /^.{6,128}$/,
      inputErrorMessage: '密码至少 6 位',
      confirmButtonText: '确定'
    })
    value = res.value
  } catch {
    return
  }
  await userApi.update(row.id, { password: value })
  ElMessage.success('密码已重置')
}

async function remove(row: User) {
  try {
    await ElMessageBox.confirm(`确定删除用户「${row.username}」吗？`, '提示', { type: 'warning' })
  } catch {
    return
  }
  await userApi.remove(row.id)
  ElMessage.success('已删除')
  await load()
}

// ---------- 批量导入 ----------
// 与后端 MAX_IMPORT_ROWS 保持一致，仅用于界面提示；真正的上限以服务端为准
const IMPORT_MAX_ROWS = 500
const IMPORT_MAX_BYTES = 5 * 1024 * 1024

const importVisible = ref(false)
const importing = ref(false)
const importPercent = ref(0)
const importFile = ref<File | null>(null)
const importResult = ref<ImportResult | null>(null)
// 回执下载过一次就不再提醒，避免"下载了但提示还在"的错觉
const receiptSaved = ref(false)

function openImport() {
  resetImport()
  importVisible.value = true
}

function resetImport() {
  importFile.value = null
  importResult.value = null
  importing.value = false
  importPercent.value = 0
  receiptSaved.value = false
}

function onFileChange(upload: UploadFile) {
  const raw = upload.raw
  if (!raw) return
  const name = raw.name.toLowerCase()
  if (!name.endsWith('.xlsx') && !name.endsWith('.csv')) {
    ElMessage.warning('只支持 .xlsx 和 .csv；旧版 .xls 请先另存为 .xlsx')
    return
  }
  if (raw.size > IMPORT_MAX_BYTES) {
    ElMessage.warning(`文件超过 ${formatSize(IMPORT_MAX_BYTES)}，请精简后重试`)
    return
  }
  if (!raw.size) {
    ElMessage.warning('文件是空的')
    return
  }
  importFile.value = raw
  importResult.value = null
  importPercent.value = 0
}

async function submitImport() {
  if (!importFile.value || importing.value) return
  importing.value = true
  importPercent.value = 0
  try {
    const { data } = await userApi.importUsers(importFile.value, (p) => {
      importPercent.value = p
    })
    importResult.value = data
    if (data.created.length) {
      ElMessage.success(`成功创建 ${data.created.length} 个账号，请下载回执查看初始密码`)
    } else {
      ElMessage.warning('没有任何账号被创建，请查看下方原因')
    }
    // 列表可能有新增（也可能只有失败，不刷也无妨，统一刷一次更省心）
    if (data.created.length) {
      page.value = 1
      await load()
    }
  } catch {
    // 文件级错误（格式/表头/超行数）已由 axios 拦截器提示
  } finally {
    importing.value = false
  }
}

async function downloadReceipt() {
  const result = importResult.value
  if (!result) return
  saveBase64File(
    result.receipt_base64,
    result.receipt_filename,
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  )
  receiptSaved.value = true
  ElMessage.success('回执已下载，请妥善保管')
}

/**
 * 关闭前的拦截。点 X、按 ESC 和点「完成」都会走到这里——
 * 只要还有没下载的回执就再问一次，密码关掉窗口就真的没了。
 */
async function handleDialogClose(done: () => void) {
  if (importing.value) {
    ElMessage.warning('正在导入，请等待完成')
    return
  }
  if (importResult.value?.created.length && !receiptSaved.value) {
    try {
      await ElMessageBox.confirm(
        '还没有下载回执。关闭后这些初始密码将无法再次获取，确定关闭吗？',
        '确认关闭',
        { type: 'warning', confirmButtonText: '仍然关闭', cancelButtonText: '返回下载' }
      )
    } catch {
      return
    }
  }
  done()
}

function finishImport() {
  // 直接改 v-model 不会触发 before-close，这里显式走同一套拦截
  handleDialogClose(() => {
    importVisible.value = false
  })
}

async function downloadTemplate() {
  try {
    const { data } = await userApi.downloadTemplate()
    saveBlob(data, '用户导入模板.xlsx')
  } catch {
    // 拦截器已提示
  }
}

onMounted(load)
</script>

<style scoped>
.search {
  width: 220px;
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

.user-cell {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}

.user-avatar {
  flex-shrink: 0;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  background: linear-gradient(135deg, #9aa6bc, #7b8698);
}

.user-avatar.admin {
  background: linear-gradient(135deg, #6b8cff, #4c6ef5);
}

.user-names {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.user-display {
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-account {
  font-size: 11.5px;
  color: var(--text-3);
}

.status-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-2);
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.status-dot.on {
  background: var(--success);
  box-shadow: 0 0 0 3px rgba(18, 183, 106, 0.14);
}

.status-dot.off {
  background: #c3cad6;
}

.time {
  font-size: 12.5px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
}

.switch-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--text-3);
}

/* ---------- 批量导入 ---------- */
.import-tip {
  margin-bottom: 14px;
}

.import-guide {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  font-size: 12.5px;
  color: var(--text-3);
}

.import-drop :deep(.el-upload-dragger) {
  padding: 26px 20px;
  border-radius: 10px;
}

.drop-body {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.drop-icon {
  font-size: 34px;
  color: var(--text-3, #98a2b3);
}

.drop-hint {
  font-size: 13.5px;
  color: var(--text-2);
}

.drop-hint em {
  color: var(--brand);
  font-style: normal;
  font-weight: 500;
}

.drop-file {
  display: flex;
  align-items: baseline;
  gap: 8px;
  max-width: 100%;
}

.drop-name {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.drop-size {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}

.drop-limit {
  font-size: 12px;
  color: var(--text-3);
}

.import-progress {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
}

.import-progress :deep(.el-progress) {
  flex: 1;
}

.progress-text {
  flex-shrink: 0;
  font-size: 12.5px;
  color: var(--text-3);
}

.import-summary {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}

.summary-item {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 14px 8px;
  border-radius: 10px;
  background: var(--surface-2, #f5f7fa);
}

.summary-item.ok {
  background: rgba(18, 183, 106, 0.08);
}

.summary-item.bad {
  background: rgba(240, 68, 56, 0.08);
}

.summary-num {
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--text);
}

.summary-item.ok .summary-num {
  color: var(--success);
}

.summary-item.bad .summary-num {
  color: var(--danger, #f04438);
}

.summary-label {
  font-size: 12px;
  color: var(--text-3);
}

.receipt-alert {
  margin-bottom: 14px;
}

.receipt-btn {
  margin-top: 8px;
}

.fail-table {
  border-radius: 8px;
}

@media (max-width: 700px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .search {
    width: 150px;
  }

  .import-summary {
    gap: 8px;
  }
}
</style>
