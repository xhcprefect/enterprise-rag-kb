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
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { userApi, type User } from '@/api'
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

@media (max-width: 700px) {
  .form-grid {
    grid-template-columns: 1fr;
  }

  .search {
    width: 150px;
  }
}
</style>
