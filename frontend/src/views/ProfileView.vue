<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>个人中心</h2>
        <p>查看账号信息并修改登录密码</p>
      </div>
    </div>

    <div class="profile-grid">
      <!-- 账号概览 -->
      <section class="panel">
        <div class="profile-hero">
          <el-avatar :size="64" class="hero-avatar">
            {{ (profile?.display_name || profile?.username || '?').charAt(0).toUpperCase() }}
          </el-avatar>
          <div class="hero-text">
            <h3>{{ profile?.display_name || profile?.username }}</h3>
            <span class="hero-account">@{{ profile?.username }}</span>
          </div>
          <el-tag :type="profile?.role === 'admin' ? 'primary' : 'info'" effect="light" round>
            {{ profile?.role === 'admin' ? '管理员' : '普通用户' }}
          </el-tag>
        </div>

        <div class="info-list">
          <div class="info-row">
            <span class="info-label">用户名</span>
            <span class="info-value">{{ profile?.username || '—' }}</span>
          </div>
          <div class="info-row">
            <span class="info-label">显示名称</span>
            <span class="info-value">{{ profile?.display_name || '—' }}</span>
          </div>
          <div class="info-row">
            <span class="info-label">邮箱</span>
            <span class="info-value" :class="{ muted: !profile?.email }">
              {{ profile?.email || '未填写' }}
            </span>
          </div>
          <div class="info-row">
            <span class="info-label">账号状态</span>
            <span class="info-value status">
              <span class="status-dot" :class="profile?.is_active ? 'on' : 'off'"></span>
              {{ profile?.is_active ? '启用' : '禁用' }}
            </span>
          </div>
        </div>
      </section>

      <!-- 修改密码 -->
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">修改密码</div>
          <span class="muted">修改后请使用新密码重新登录</span>
        </div>

        <div class="pwd-body">
          <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
            <el-form-item label="原密码" prop="old_password">
              <el-input
                v-model="form.old_password"
                type="password"
                show-password
                placeholder="请输入当前密码"
              />
            </el-form-item>
            <el-form-item label="新密码" prop="new_password">
              <el-input
                v-model="form.new_password"
                type="password"
                show-password
                placeholder="至少 6 位"
              />
            </el-form-item>
            <el-form-item label="确认新密码" prop="confirm">
              <el-input
                v-model="form.confirm"
                type="password"
                show-password
                placeholder="再次输入新密码"
              />
            </el-form-item>

            <el-button type="primary" :loading="submitting" class="pwd-submit" @click="submit">
              保存新密码
            </el-button>
          </el-form>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { userApi } from '@/api'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()
const profile = computed(() => userStore.profile)

const formRef = ref<FormInstance>()
const submitting = ref(false)
const form = reactive({ old_password: '', new_password: '', confirm: '' })

const rules: FormRules = {
  old_password: [{ required: true, message: '请输入原密码', trigger: 'blur' }],
  new_password: [{ required: true, min: 6, message: '新密码至少 6 位', trigger: 'blur' }],
  confirm: [
    {
      validator: (_rule, value, callback) => {
        if (value !== form.new_password) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur'
    }
  ]
}

async function submit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    await userApi.changePassword(form.old_password, form.new_password)
    ElMessage.success('密码已更新')
    formRef.value.resetFields()
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.profile-grid {
  display: grid;
  grid-template-columns: minmax(300px, 0.9fr) minmax(340px, 1.1fr);
  gap: 16px;
  align-items: start;
  max-width: 1080px;
}

/* 账号概览 */
.profile-hero {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 22px 22px 20px;
  background: linear-gradient(135deg, #f7f9ff 0%, #ffffff 70%);
  border-bottom: 1px solid var(--line);
}

.hero-avatar {
  flex-shrink: 0;
  font-size: 24px;
  font-weight: 600;
  color: #fff;
  background: linear-gradient(135deg, #6b8cff, #4c6ef5);
  box-shadow: 0 6px 16px rgba(76, 110, 245, 0.28);
}

.hero-text {
  flex: 1;
  min-width: 0;
}

.hero-text h3 {
  margin: 0 0 3px;
  font-size: 17px;
  font-weight: 650;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hero-account {
  font-size: 12.5px;
  color: var(--text-3);
}

.info-list {
  padding: 8px 22px 20px;
}

.info-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 0;
  border-bottom: 1px dashed var(--line);
  font-size: 13px;
}

.info-row:last-child {
  border-bottom: none;
}

.info-label {
  color: var(--text-3);
  flex-shrink: 0;
}

.info-value {
  color: var(--text);
  font-weight: 500;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.info-value.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 400;
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

/* 修改密码 */
.pwd-body {
  padding: 20px 22px 22px;
}

.pwd-submit {
  width: 100%;
  margin-top: 4px;
}

@media (max-width: 900px) {
  .profile-grid {
    grid-template-columns: 1fr;
  }
}
</style>
