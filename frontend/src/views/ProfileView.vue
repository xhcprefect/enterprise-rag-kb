<template>
  <div class="page">
    <div class="page-head">
      <div>
        <span class="page-eyebrow">{{ t('profile.eyebrow') }}</span>
        <h2>{{ t('profile.title') }}</h2>
        <p>{{ t('profile.subtitle') }}</p>
      </div>
    </div>

    <div class="profile-grid">
      <!-- 账号概览 -->
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">{{ t('profile.infoTitle') }}</div>
          <span class="muted">{{ t('profile.infoHint') }}</span>
        </div>

        <div class="profile-hero">
          <el-avatar :size="64" class="hero-avatar">
            {{ (profile?.display_name || profile?.username || '?').charAt(0).toUpperCase() }}
          </el-avatar>
          <div class="hero-text">
            <h3>{{ profile?.display_name || profile?.username }}</h3>
            <span class="hero-account">@{{ profile?.username }}</span>
          </div>
          <el-tag :type="profile?.role === 'admin' ? 'primary' : 'info'" effect="light" round>
            {{ profile?.role === 'admin' ? t('profile.roleAdmin') : t('profile.roleUser') }}
          </el-tag>
        </div>

        <div class="info-list">
          <div class="info-row">
            <span class="info-label">{{ t('common.username') }}</span>
            <span class="info-value">{{ profile?.username || '—' }}</span>
          </div>
          <div class="info-row">
            <span class="info-label">{{ t('common.displayName') }}</span>
            <span class="info-value">{{ profile?.display_name || '—' }}</span>
          </div>
          <div class="info-row">
            <span class="info-label">{{ t('common.email') }}</span>
            <span class="info-value" :class="{ 'is-empty': !profile?.email }">
              {{ profile?.email || t('common.notProvided') }}
            </span>
          </div>
          <div class="info-row">
            <span class="info-label">{{ t('profile.accountStatus') }}</span>
            <span class="info-value status">
              <span class="status-dot" :class="profile?.is_active ? 'on' : 'off'"></span>
              {{ profile?.is_active ? t('common.enabled') : t('common.disabled') }}
            </span>
          </div>
        </div>
      </section>

      <!-- 修改密码 -->
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">{{ t('profile.changePassword') }}</div>
          <span class="muted">{{ t('profile.changePasswordHint') }}</span>
        </div>

        <div class="pwd-body">
          <el-form
            ref="formRef"
            :model="form"
            :rules="rules"
            :validate-on-rule-change="false"
            label-position="top"
          >
            <el-form-item :label="t('profile.oldPassword')" prop="old_password">
              <el-input
                v-model="form.old_password"
                type="password"
                show-password
                autocomplete="current-password"
                :placeholder="t('profile.oldPasswordPlaceholder')"
              />
            </el-form-item>
            <el-form-item :label="t('profile.newPassword')" prop="new_password">
              <el-input
                v-model="form.new_password"
                type="password"
                show-password
                autocomplete="new-password"
                :placeholder="t('profile.newPasswordPlaceholder')"
              />
            </el-form-item>
            <el-form-item :label="t('profile.confirmPassword')" prop="confirm">
              <el-input
                v-model="form.confirm"
                type="password"
                show-password
                autocomplete="new-password"
                :placeholder="t('profile.confirmPlaceholder')"
              />
            </el-form-item>
          </el-form>
        </div>

        <div class="panel-foot pwd-foot">
          <span class="pwd-hint">{{ t('profile.hint') }}</span>
          <el-button type="primary" :loading="submitting" @click="submit">{{ t('profile.savePassword') }}</el-button>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { userApi } from '@/api'
import { useUserStore } from '@/stores/user'

const { t, locale } = useI18n()
const userStore = useUserStore()
const profile = computed(() => userStore.profile)

const formRef = ref<FormInstance>()
const submitting = ref(false)
const form = reactive({ old_password: '', new_password: '', confirm: '' })

const rules = computed<FormRules>(() => ({
  old_password: [{ required: true, message: t('profile.validation.oldRequired'), trigger: 'blur' }],
  new_password: [
    { required: true, min: 6, message: t('profile.validation.newMin'), trigger: 'blur' }
  ],
  confirm: [
    {
      validator: (_rule, value, callback) => {
        if (value !== form.new_password) callback(new Error(t('profile.validation.mismatch')))
        else callback()
      },
      trigger: 'blur'
    }
  ]
}))

// 切换语言只重建校验文案，不清空输入，也不立即触发校验
watch(locale, () => {
  formRef.value?.clearValidate()
})

async function submit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    await userApi.changePassword(form.old_password, form.new_password)
    ElMessage.success(t('profile.passwordUpdated'))
    await userStore.logout(false)
    location.assign('/login')
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
  align-items: start;
  gap: 18px;
  max-width: 1080px;
}

/* 账号概览 */
.profile-hero {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
  padding: 22px;
  background: var(--brand-tint);
  border-bottom: 1px solid var(--line);
}

.hero-avatar {
  flex-shrink: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: var(--brand-soft);
  color: var(--brand-dark);
  font-size: 24px;
  font-weight: 650;
}

.hero-text {
  flex: 1;
  min-width: 140px;
}

.hero-text h3 {
  margin: 0 0 3px;
  overflow: hidden;
  color: var(--text);
  font-size: 17px;
  font-weight: 650;
  letter-spacing: -0.01em;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hero-account {
  color: var(--text-3);
  font-size: 12.5px;
}

.info-list {
  padding: 6px 22px 16px;
}

.info-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 13px 0;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
}

.info-row:last-child {
  border-bottom: none;
}

.info-label {
  flex-shrink: 0;
  color: var(--text-3);
}

.info-value {
  overflow: hidden;
  color: var(--text);
  font-weight: 500;
  text-align: right;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.info-value.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-2);
  font-weight: 400;
}

.info-value.is-empty {
  color: var(--text-3);
  font-weight: 400;
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

/* 修改密码 */
.pwd-body {
  padding: 22px 24px 4px;
}

.pwd-body :deep(.el-form-item:last-child) {
  margin-bottom: 18px;
}

.pwd-foot {
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px 20px;
  padding: 15px 24px 20px;
  background: var(--surface);
}

.pwd-hint {
  color: var(--text-3);
  font-size: 12px;
  line-height: 1.6;
}

@media (max-width: 1000px) {
  .profile-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .profile-hero {
    padding: 18px;
  }

  .info-list {
    padding-inline: 18px;
  }

  .pwd-body {
    padding: 18px 18px 2px;
  }

  .pwd-foot {
    flex-direction: column;
    align-items: stretch;
    padding: 14px 18px 18px;
  }

  .pwd-foot .el-button {
    width: 100%;
  }
}
</style>
