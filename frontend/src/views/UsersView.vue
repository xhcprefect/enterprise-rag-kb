<template>
  <div class="page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">{{ t('users.eyebrow') }}</span>
        <h2>{{ t('users.title') }}</h2>
        <p>{{ t('users.subtitle') }}</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="keyword"
          :placeholder="t('users.searchPlaceholder')"
          clearable
          class="search"
          :prefix-icon="Search"
          :aria-label="t('users.searchAria')"
          @keyup.enter="onSearch"
          @clear="onSearch"
        />
        <el-button @click="openImport">
          <el-icon><Upload /></el-icon>{{ t('users.importButton') }}
        </el-button>
        <el-button type="primary" @click="openCreate">
          <el-icon><Plus /></el-icon>{{ t('users.createButton') }}
        </el-button>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          {{ t('users.listTitle') }}
          <span class="count-badge">{{ total }}</span>
        </div>
        <span class="muted">{{ t('users.listHint') }}</span>
      </div>

      <el-table v-loading="loading" :data="users">
        <el-table-column :label="t('users.colUser')" min-width="200">
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

        <el-table-column :label="t('users.colEmail')" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ 'is-empty': !row.email }">{{ row.email || t('common.notProvided') }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('users.colRole')" width="120">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'primary' : 'info'" size="small" effect="light">
              {{ row.role === 'admin' ? t('users.roleAdmin') : t('users.roleUser') }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column :label="t('users.colStatus')" width="110">
          <template #default="{ row }">
            <span class="status-cell">
              <span class="status-dot" :class="row.is_active ? 'on' : 'off'"></span>
              {{ row.is_active ? t('users.statusActive') : t('users.statusDisabled') }}
            </span>
          </template>
        </el-table-column>

        <el-table-column :label="t('users.colCreatedAt')" width="170">
          <template #default="{ row }">
            <span class="time">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="t('users.colActions')" width="210" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button link type="primary" size="small" @click="openEdit(row)">{{ t('common.edit') }}</el-button>
              <el-button link type="warning" size="small" @click="resetPassword(row)">{{ t('users.resetPassword') }}</el-button>
              <el-button
                link
                type="danger"
                size="small"
                :disabled="row.id === userStore.profile?.id"
                @click="remove(row)"
              >
                {{ t('common.delete') }}
              </el-button>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty :description="keyword ? t('users.emptySearch') : t('users.empty')" :image-size="80" />
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

    <el-dialog
      v-model="dialogVisible"
      :title="editing ? t('users.dialogEditTitle') : t('users.dialogCreateTitle')"
      width="520px"
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        :validate-on-rule-change="false"
        label-position="top"
      >
        <div class="form-grid">
          <el-form-item :label="t('common.username')" prop="username">
            <el-input
              v-model="form.username"
              :disabled="!!editing"
              :placeholder="t('users.usernamePlaceholder')"
            />
          </el-form-item>
          <el-form-item v-if="!editing" :label="t('users.initialPassword')" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              show-password
              :placeholder="t('users.initialPasswordPlaceholder')"
            />
          </el-form-item>
          <el-form-item :label="t('common.displayName')">
            <el-input v-model="form.display_name" :placeholder="t('users.displayNamePlaceholder')" />
          </el-form-item>
          <el-form-item :label="t('common.email')">
            <el-input v-model="form.email" :placeholder="t('users.emailPlaceholder')" />
          </el-form-item>
        </div>

        <el-form-item :label="t('common.role')">
          <el-radio-group v-model="form.role">
            <el-radio value="user">{{ t('users.roleUser') }}</el-radio>
            <el-radio value="admin">{{ t('users.roleAdmin') }}</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="editing" :label="t('users.accountStatus')">
          <div class="switch-row">
            <el-switch v-model="form.is_active" />
            <span>{{ form.is_active ? t('users.statusOnHint') : t('users.statusOffHint') }}</span>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 批量导入 -->
    <el-dialog
      v-model="importVisible"
      :title="t('users.import.title')"
      width="680px"
      :close-on-click-modal="false"
      :before-close="handleDialogClose"
      @closed="resetImport"
    >
      <template v-if="!importResult">
        <el-alert type="info" :closable="false" show-icon class="import-tip">
          <template #title>
            <i18n-t keypath="users.import.securityTip" tag="span">
              <template #strong>
                <strong>{{ t('users.import.randomPassword') }}</strong>
              </template>
            </i18n-t>
          </template>
        </el-alert>

        <div class="import-guide">
          <span>{{ t('users.import.guide') }}</span>
          <el-button link type="primary" size="small" @click="downloadTemplate">
            {{ t('users.import.downloadTemplate') }}
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
              <i18n-t keypath="users.import.dropHint" tag="span">
                <template #action>
                  <em>{{ t('users.import.chooseFile') }}</em>
                </template>
              </i18n-t>
            </div>
            <div class="drop-limit">
              {{ t('users.import.dropLimit', { rows: formatNumber(IMPORT_MAX_ROWS) }) }}
            </div>
          </div>
        </el-upload>

        <!-- 上传到 100% 只是传输完成，服务端还要逐行建号（500 行约 30 秒），
             所以这里不置 success，避免"进度条满了却没结果"的错觉 -->
        <div v-if="importing" class="import-progress">
          <el-progress :percentage="importPercent" :show-text="false" />
          <span class="progress-text">
            {{
              importPercent < 100
                ? t('users.import.uploading', { percent: importPercent })
                : t('users.import.processing')
            }}
          </span>
        </div>
      </template>

      <template v-else>
        <div class="import-summary">
          <div class="summary-item ok">
            <span class="summary-num">{{ formatNumber(importResult.created.length) }}</span>
            <span class="summary-label">{{ t('users.import.summaryCreated') }}</span>
          </div>
          <div class="summary-item bad">
            <span class="summary-num">{{ formatNumber(importResult.failed.length) }}</span>
            <span class="summary-label">{{ t('users.import.summaryFailed') }}</span>
          </div>
          <div class="summary-item">
            <span class="summary-num">{{ formatNumber(importResult.total) }}</span>
            <span class="summary-label">{{ t('users.import.summaryRows') }}</span>
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
            <i18n-t keypath="users.import.receiptWarning" tag="span">
              <template #strong>
                <strong>{{ t('users.import.receiptStrong') }}</strong>
              </template>
            </i18n-t>
          </template>
          <el-button type="primary" size="small" class="receipt-btn" @click="downloadReceipt">
            {{
              t('users.import.downloadReceiptCount', {
                total: formatNumber(importResult.created.length)
              })
            }}
          </el-button>
        </el-alert>

        <el-table
          v-if="importResult.failed.length"
          :data="importResult.failed"
          size="small"
          max-height="260"
          class="fail-table"
        >
          <el-table-column prop="row" :label="t('users.import.colRow')" width="70" />
          <el-table-column prop="username" :label="t('users.import.colUsername')" min-width="130">
            <template #default="{ row }">
              <span :class="{ 'is-empty': !row.username }">{{ row.username || t('users.import.emptyUsername') }}</span>
            </template>
          </el-table-column>
          <el-table-column
            prop="reason"
            :label="t('users.import.colReason')"
            min-width="240"
            show-overflow-tooltip
          />
        </el-table>
      </template>

      <template #footer>
        <template v-if="!importResult">
          <el-button :disabled="importing" @click="importVisible = false">{{ t('common.cancel') }}</el-button>
          <el-button
            type="primary"
            :disabled="!importFile"
            :loading="importing"
            @click="submitImport"
          >
            {{ importing ? t('users.import.importing') : t('users.import.startImport') }}
          </el-button>
        </template>
        <template v-else>
          <el-button type="primary" @click="downloadReceipt" :disabled="!importResult.created.length">
            {{ t('users.import.downloadReceipt') }}
          </el-button>
          <el-button @click="finishImport">{{ t('users.import.done') }}</el-button>
        </template>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
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
import { formatDateTime, formatNumber } from '@/i18n/format'
import { useUserStore } from '@/stores/user'

const { t, locale } = useI18n()
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

const rules = computed<FormRules>(() => ({
  username: [
    { required: true, message: t('users.validation.usernameRequired'), trigger: 'blur' },
    {
      pattern: /^[A-Za-z0-9_.-]+$/,
      message: t('users.validation.usernamePattern'),
      trigger: 'blur'
    }
  ],
  password: [
    { required: true, message: t('users.validation.passwordRequired'), trigger: 'blur' },
    { min: 6, message: t('users.validation.passwordMin'), trigger: 'blur' }
  ]
}))

// 切换语言只重建校验文案，不重新校验、也不清空用户正在编辑的输入
watch(locale, () => {
  formRef.value?.clearValidate()
})

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
      ElMessage.success(t('common.updated'))
    } else {
      await userApi.create({ ...form, email: form.email || undefined })
      ElMessage.success(t('common.created'))
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
    const res = await ElMessageBox.prompt(
      t('users.resetPrompt', { username: row.username }),
      t('users.resetPassword'),
      {
        inputType: 'password',
        inputPattern: /^.{6,128}$/,
        inputErrorMessage: t('users.validation.passwordMin'),
        inputPlaceholder: t('users.resetPlaceholder'),
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel')
      }
    )
    value = res.value
  } catch {
    return
  }
  await userApi.update(row.id, { password: value })
  ElMessage.success(t('users.resetDone'))
}

async function remove(row: User) {
  try {
    await ElMessageBox.confirm(
      t('users.deleteConfirm', { username: row.username }),
      t('common.tip'),
      {
        type: 'warning',
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel')
      }
    )
  } catch {
    return
  }
  await userApi.remove(row.id)
  ElMessage.success(t('common.deleted'))
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
    ElMessage.warning(t('users.import.invalidType'))
    return
  }
  if (raw.size > IMPORT_MAX_BYTES) {
    ElMessage.warning(t('users.import.tooLarge', { size: formatSize(IMPORT_MAX_BYTES) }))
    return
  }
  if (!raw.size) {
    ElMessage.warning(t('users.import.emptyFile'))
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
      ElMessage.success(
        t('users.import.createdSuccess', { count: formatNumber(data.created.length) })
      )
    } else {
      ElMessage.warning(t('users.import.noneCreated'))
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
  ElMessage.success(t('users.import.receiptDownloaded'))
}

/**
 * 关闭前的拦截。点 X、按 ESC 和点「完成」都会走到这里——
 * 只要还有没下载的回执就再问一次，密码关掉窗口就真的没了。
 */
async function handleDialogClose(done: () => void) {
  if (importing.value) {
    ElMessage.warning(t('users.import.inProgress'))
    return
  }
  if (importResult.value?.created.length && !receiptSaved.value) {
    try {
      await ElMessageBox.confirm(
        t('users.import.closeConfirm'),
        t('users.import.closeTitle'),
        {
          type: 'warning',
          confirmButtonText: t('users.import.stillClose'),
          cancelButtonText: t('users.import.backToDownload')
        }
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
    saveBlob(data, t('users.import.templateFileName'))
  } catch {
    // 拦截器已提示
  }
}

onMounted(load)
</script>

<style scoped>
.search {
  width: 240px;
}

.user-cell {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}

.user-avatar {
  flex-shrink: 0;
  border: 1px solid var(--line);
  background: var(--slate-soft);
  color: var(--slate-text);
  font-size: 13px;
  font-weight: 600;
}

.user-avatar.admin {
  border-color: var(--brand);
  background: var(--brand);
  color: var(--on-brand);
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

.is-empty {
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
  box-shadow: 0 0 0 3px var(--success-soft);
}

.status-dot.off {
  background: var(--line-strong);
}

.time {
  color: var(--text-3);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}

.row-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 10px;
}

.row-actions :deep(.el-button + .el-button) {
  margin-left: 0;
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
  flex-wrap: wrap;
  gap: 8px 12px;
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
  color: var(--text-3);
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
  gap: 10px;
  margin-bottom: 16px;
}

.summary-item {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 15px 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-soft);
}

.summary-item.ok {
  border-color: var(--success-border);
  background: var(--success-soft);
}

.summary-item.bad {
  border-color: var(--danger-border);
  background: var(--danger-soft);
}

.summary-num {
  color: var(--text);
  font-size: 23px;
  font-weight: 650;
  line-height: 1.2;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}

.summary-item.ok .summary-num {
  color: var(--success);
}

.summary-item.bad .summary-num {
  color: var(--danger);
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

  .import-summary {
    gap: 8px;
  }
}
</style>
