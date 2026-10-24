<template>
  <div class="login-page">
    <div class="glow glow-a"></div>
    <div class="glow glow-b"></div>

    <div class="login-shell">
      <!-- 左侧品牌介绍 -->
      <section class="intro">
        <div class="brand">
          <div class="brand-mark">
            <el-icon :size="22"><Reading /></el-icon>
          </div>
          <span>企业知识库</span>
        </div>

        <h1>让企业知识<br />随问随答</h1>
        <p class="intro-desc">
          基于 RAG 的检索增强问答，回答全部来自你自己的文档，并附带可追溯的引用来源。
        </p>

        <ul class="features">
          <li>
            <el-icon><Search /></el-icon>
            <div>
              <strong>语义检索</strong>
              <span>bge-m3 向量召回，不止关键词匹配</span>
            </div>
          </li>
          <li>
            <el-icon><DocumentCopy /></el-icon>
            <div>
              <strong>多格式入库</strong>
              <span>PDF / Word / TXT / Markdown / Excel / CSV</span>
            </div>
          </li>
          <li>
            <el-icon><Lock /></el-icon>
            <div>
              <strong>权限与审计</strong>
              <span>角色隔离，关键操作全程留痕</span>
            </div>
          </li>
        </ul>
      </section>

      <!-- 右侧登录表单 -->
      <section class="card">
        <div class="card-head">
          <h2>欢迎回来</h2>
          <p>登录后开始检索与问答</p>
        </div>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          size="large"
          label-position="top"
          @keyup.enter="onSubmit"
        >
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" placeholder="请输入用户名" clearable>
              <template #prefix><el-icon><User /></el-icon></template>
            </el-input>
          </el-form-item>

          <el-form-item label="密码" prop="password">
            <el-input v-model="form.password" type="password" placeholder="请输入密码" show-password>
              <template #prefix><el-icon><Lock /></el-icon></template>
            </el-input>
          </el-form-item>

          <el-button type="primary" :loading="loading" class="submit-btn" @click="onSubmit">
            登 录
          </el-button>
        </el-form>

        <div class="card-foot">
          <el-icon :size="13"><InfoFilled /></el-icon>
          <span>账号由管理员分配；忘记密码请联系管理员重置</span>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

async function onSubmit() {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await userStore.login(form.username, form.password)
    ElMessage.success('登录成功')
    const redirect = (route.query.redirect as string) || '/chat'
    router.push(redirect)
  } catch {
    /* 错误提示由拦截器统一处理 */
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 24px;
  overflow: hidden;
  background: radial-gradient(1200px 600px at 10% 10%, #eef2ff 0%, transparent 55%),
    radial-gradient(900px 500px at 90% 85%, #e6f0ff 0%, transparent 60%), #f7f9fe;
}

/* 背景光斑 */
.glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(70px);
  pointer-events: none;
}

.glow-a {
  width: 420px;
  height: 420px;
  top: -140px;
  left: -80px;
  background: rgba(76, 110, 245, 0.16);
}

.glow-b {
  width: 460px;
  height: 460px;
  right: -160px;
  bottom: -180px;
  background: rgba(18, 183, 106, 0.12);
}

.login-shell {
  position: relative;
  z-index: 1;
  width: min(1040px, 100%);
  display: grid;
  grid-template-columns: 1.05fr 0.95fr;
  gap: 56px;
  align-items: center;
}

/* ---------- 左侧介绍 ---------- */
.intro {
  padding: 8px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 11px;
  font-size: 16px;
  font-weight: 650;
  color: var(--text);
  margin-bottom: 34px;
}

.brand-mark {
  width: 42px;
  height: 42px;
  border-radius: 13px;
  display: grid;
  place-items: center;
  color: #fff;
  background: linear-gradient(135deg, #6b8cff 0%, #4c6ef5 55%, #3b57d6 100%);
  box-shadow: 0 8px 20px rgba(76, 110, 245, 0.32);
}

.intro h1 {
  margin: 0 0 16px;
  font-size: 40px;
  line-height: 1.25;
  letter-spacing: -0.02em;
  font-weight: 700;
  color: var(--text);
}

.intro-desc {
  margin: 0 0 34px;
  max-width: 420px;
  font-size: 14.5px;
  line-height: 1.8;
  color: var(--text-2);
}

.features {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.features li {
  display: flex;
  align-items: flex-start;
  gap: 13px;
}

.features :deep(.el-icon) {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  border-radius: 10px;
  background: #fff;
  border: 1px solid var(--line);
  color: var(--brand);
  box-shadow: var(--shadow-sm);
}

.features strong {
  display: block;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 2px;
}

.features span {
  font-size: 12.5px;
  color: var(--text-3);
}

/* ---------- 右侧表单卡 ---------- */
.card {
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(12px);
  border: 1px solid var(--line);
  border-radius: 22px;
  padding: 38px 36px 28px;
  box-shadow: 0 24px 60px rgba(27, 36, 55, 0.12);
}

.card-head {
  margin-bottom: 26px;
}

.card-head h2 {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 650;
  color: var(--text);
  letter-spacing: -0.01em;
}

.card-head p {
  margin: 0;
  font-size: 13px;
  color: var(--text-3);
}

.card :deep(.el-form-item__label) {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-2);
  padding-bottom: 6px;
}

.card :deep(.el-input__wrapper) {
  padding: 4px 12px;
  border-radius: 10px;
}

.submit-btn {
  width: 100%;
  height: 44px;
  margin-top: 6px;
  font-size: 15px;
  letter-spacing: 4px;
  border-radius: 10px;
  background: linear-gradient(135deg, #5b7cff 0%, #4c6ef5 60%, #3b57d6 100%);
  border: none;
}

.submit-btn:hover {
  background: linear-gradient(135deg, #6b8cff 0%, #5578f7 60%, #4260e0 100%);
}

.card-foot {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  margin-top: 22px;
  padding-top: 18px;
  border-top: 1px dashed var(--line);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-3);
}

.card-foot :deep(.el-icon) {
  margin-top: 2px;
  flex-shrink: 0;
}

@media (max-width: 900px) {
  .login-shell {
    grid-template-columns: 1fr;
    max-width: 460px;
    gap: 28px;
  }

  .intro {
    display: none;
  }

  .card {
    padding: 30px 24px 24px;
  }
}
</style>
