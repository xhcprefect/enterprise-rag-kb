<template>
  <div class="login-page">
    <div class="login-shell">
      <section class="intro" aria-label="企业知识库介绍">
        <div class="brand">
          <span class="brand-mark"><el-icon :size="23"><Reading /></el-icon></span>
          <span class="brand-copy"><strong>企业知识库<span>.</span></strong><span>KNOWLEDGE WORKSPACE</span></span>
        </div>

        <div class="intro-copy">
          <span class="intro-eyebrow">让知识，触手可及</span>
          <h1>把分散的知识，<br />变成清晰的答案。</h1>
          <p>连接企业文档与日常问题，在一个工作空间里<br class="desktop-break" />管理资料、查找信息、追溯回答来源。</p>
        </div>

        <div class="knowledge-illustration" aria-hidden="true">
          <div class="source-stack">
            <div class="source-item"><span class="source-icon"><el-icon><Document /></el-icon></span><span>产品资料<span class="source-line"></span></span><span class="file-type">PDF</span></div>
            <div class="source-item"><span class="source-icon"><el-icon><DocumentCopy /></el-icon></span><span>操作流程<span class="source-line short"></span></span><span class="file-type">DOC</span></div>
            <div class="source-item"><span class="source-icon"><el-icon><Files /></el-icon></span><span>团队规范<span class="source-line"></span></span><span class="file-type">MD</span></div>
          </div>
          <div class="connect-path"><span></span><el-icon><ArrowRight /></el-icon></div>
          <div class="answer-card">
            <span class="answer-icon"><el-icon :size="20"><MagicStick /></el-icon></span>
            <strong>从资料中发现答案</strong>
            <span class="answer-line"></span><span class="answer-line medium"></span><span class="answer-line short"></span>
            <span class="answer-source"><el-icon><DocumentCopy /></el-icon>关联引用来源</span>
          </div>
        </div>

        <div class="intro-foot">
          <span><span class="step-num">01</span>文档入库</span>
          <span class="step-divider"></span>
          <span><span class="step-num">02</span>语义检索</span>
          <span class="step-divider"></span>
          <span><span class="step-num">03</span>引用核对</span>
        </div>
      </section>

      <section class="login-form-panel">
        <div class="form-content">
          <div class="card-head">
            <span class="form-eyebrow">欢迎来到你的知识工作空间</span>
            <h2>欢迎回来</h2>
            <p>登录账户，继续探索企业知识。</p>
          </div>

          <el-form ref="formRef" :model="form" :rules="rules" size="large" label-position="top" @submit.prevent="onSubmit">
            <el-form-item label="用户名" prop="username">
              <el-input v-model="form.username" placeholder="请输入用户名" autocomplete="username" clearable :disabled="loading">
                <template #prefix><el-icon><User /></el-icon></template>
              </el-input>
            </el-form-item>
            <el-form-item label="密码" prop="password">
              <el-input v-model="form.password" type="password" placeholder="请输入密码" autocomplete="current-password" show-password :disabled="loading">
                <template #prefix><el-icon><Lock /></el-icon></template>
              </el-input>
            </el-form-item>
            <div class="account-note"><el-icon><InfoFilled /></el-icon><span>请使用管理员为你分配的账户登录</span></div>
            <el-button type="primary" native-type="submit" :loading="loading" class="submit-btn">
              <span>登录工作空间</span><el-icon v-if="!loading"><ArrowRight /></el-icon>
            </el-button>
          </el-form>

          <div class="card-foot">
            <span class="help-icon"><el-icon :size="16"><Lock /></el-icon></span>
            <span><strong>需要登录帮助？</strong><span>忘记密码或需要开通账户，请联系管理员。</span></span>
          </div>
        </div>
        <span class="form-bottom">企业知识库 · 专注每一次知识探索</span>
      </section>
    </div>
    <div class="login-caption">让文档有序，让信息易寻。</div>
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
  if (!formRef.value || loading.value) return
  loading.value = true
  try {
    const valid = await formRef.value.validate().catch(() => false)
    if (!valid) return
    await userStore.login(form.username, form.password)
    ElMessage.success('登录成功')
    const redirect = (route.query.redirect as string) || '/chat'
    router.push(redirect)
  } catch {
    /* 错误提示由拦截器统一处理。 */
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; min-height: 100dvh; padding: 44px 32px 28px; background: #f2f5ef; }
.login-shell { display: grid; grid-template-columns: 1.08fr 1fr; width: min(1120px, 100%); min-height: 660px; border: 1px solid #dfe6db; border-radius: 24px; background: #fff; box-shadow: 0 24px 80px rgb(32 58 35 / 7%); overflow: hidden; }
.intro { position: relative; display: flex; flex-direction: column; padding: 42px 46px 32px; background: #193c2f; color: #edf2e7; overflow: hidden; }
.intro::after { position: absolute; width: 350px; height: 350px; right: -260px; top: -165px; border: 1px solid #3c5b42; border-radius: 50%; box-shadow: 0 0 0 40px #234331, 0 0 0 41px #3c5b42, 0 0 0 80px #234331, 0 0 0 81px #3c5b42; content: ''; pointer-events: none; opacity: .6; }
.brand { position: relative; z-index: 1; display: inline-flex; align-items: center; gap: 12px; }
.brand-mark { display: grid; place-items: center; width: 44px; height: 46px; border: 1px solid #6e8c64; border-radius: 12px; color: #d3e5ab; background: #2b503a; }
.brand-copy { display: flex; flex-direction: column; gap: 8px; }
.brand-copy strong { font-size: 19px; font-weight: 600; letter-spacing: .02em; }
.brand-copy strong > span { color: #d1e4a7; }
.brand-copy > span { color: #9bb59d; font-size: 8px; letter-spacing: .18em; }
.intro-copy { position: relative; z-index: 1; margin-top: 58px; }
.intro-eyebrow { color: #b6ce9f; font-size: 11px; letter-spacing: .16em; }
.intro h1 { margin: 16px 0 17px; font-size: 36px; line-height: 1.45; font-weight: 550; letter-spacing: -.035em; }
.intro-copy > p { margin: 0; color: #a5bca8; font-size: 12px; line-height: 1.95; }
.knowledge-illustration { display: flex; align-items: center; gap: 0; margin: 39px 0 35px; }
.source-stack { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 9px; }
.source-item { display: flex; align-items: center; gap: 10px; height: 47px; padding: 9px 12px; border: 1px solid #466348; border-radius: 8px; background: #284a35; }
.source-icon { display: grid; place-items: center; width: 27px; height: 30px; border: 1px solid #5c7551; border-radius: 5px; background: #34573c; color: #c7d5aa; font-size: 14px; }
.source-item > span:nth-child(2) { flex: 1; min-width: 0; color: #d0debf; font-size: 10px; }
.source-line { display: block; width: 50px; height: 3px; margin-top: 5px; border-radius: 2px; background: #5e7852; }
.source-line.short { width: 34px; }
.file-type { color: #90ab88; font-size: 7px; letter-spacing: .07em; }
.connect-path { display: flex; align-items: center; width: 37px; flex-shrink: 0; color: #9fb87a; }
.connect-path > span { flex: 1; height: 1px; background: #829a66; }
.connect-path > .el-icon { margin-left: -4px; font-size: 11px; }
.answer-card { display: flex; flex-direction: column; align-items: flex-start; width: 172px; flex-shrink: 0; padding: 16px 18px 13px; border: 1px solid #cdd8b5; border-radius: 11px; background: #eef1dc; transform: rotate(2deg); box-shadow: 0 8px 20px rgb(12 32 18 / 12%); }
.answer-icon { display: grid; place-items: center; width: 31px; height: 31px; margin-bottom: 12px; border-radius: 9px; background: #dce6c5; color: #4e704a; }
.answer-card > strong { margin-bottom: 13px; color: #3a5438; font-size: 11px; font-weight: 600; }
.answer-line { width: 100%; height: 4px; margin-bottom: 7px; border-radius: 2px; background: #c6d2b3; }
.answer-line.medium { width: 86%; }
.answer-line.short { width: 62%; }
.answer-source { display: flex; align-items: center; gap: 5px; margin-top: 7px; color: #657e52; font-size: 9px; }
.intro-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: auto; padding-top: 19px; border-top: 1px solid #395640; }
.intro-foot > span:not(.step-divider) { display: inline-flex; align-items: center; gap: 7px; color: #b4c7a6; font-size: 10px; white-space: nowrap; }
.step-num { color: #839f70; font-size: 9px; font-variant-numeric: tabular-nums; }
.step-divider { width: 13px; height: 1px; background: #5b7651; }
.login-form-panel { position: relative; display: flex; align-items: center; justify-content: center; padding: 64px 56px 82px; }
.form-content { width: 100%; max-width: 350px; }
.card-head { margin-bottom: 37px; }
.form-eyebrow { color: var(--text-3); font-size: 11px; letter-spacing: .04em; }
.card-head h2 { margin: 14px 0 10px; font-size: 30px; font-weight: 650; letter-spacing: -.04em; }
.card-head p { margin: 0; color: var(--text-3); font-size: 13px; line-height: 1.7; }
.login-form-panel :deep(.el-form-item) { margin-bottom: 24px; }
.login-form-panel :deep(.el-form-item__label) { padding-bottom: 9px; color: var(--text); font-size: 12px; font-weight: 550; }
.login-form-panel :deep(.el-input__wrapper) { min-height: 46px; padding-inline: 14px; background: #fcfdfb; }
.login-form-panel :deep(.el-input__inner) { font-size: 13px; }
.login-form-panel :deep(.el-input__prefix) { margin-right: 4px; color: #8d9c8e; }
.account-note { display: flex; align-items: center; gap: 6px; margin: -1px 0 24px; color: var(--text-3); font-size: 11px; }
.account-note .el-icon { color: #91a48d; font-size: 12px; }
.submit-btn { width: 100%; height: 46px; font-size: 13px; }
.submit-btn :deep(> span) { display: flex; align-items: center; justify-content: center; gap: 13px; }
.card-foot { display: flex; align-items: center; gap: 11px; margin-top: 35px; padding: 19px 0 0; border-top: 1px solid var(--line); }
.help-icon { display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid #e7ece2; border-radius: 10px; color: #7f9177; background: #f7f9f3; flex-shrink: 0; }
.card-foot > span:last-child { display: flex; flex-direction: column; gap: 5px; }
.card-foot strong { color: var(--text-2); font-size: 11px; font-weight: 550; }
.card-foot > span:last-child > span { color: var(--text-3); font-size: 10px; line-height: 1.7; }
.form-bottom { position: absolute; bottom: 28px; color: #8e9b8e; font-size: 10px; letter-spacing: .06em; }
.login-caption { margin-top: 24px; color: #8a9887; font-size: 10px; letter-spacing: .12em; }
@media (min-width: 1500px) { .login-shell { min-height: 710px; } .intro { padding: 48px 52px 36px; } .intro-copy { margin-top: 65px; } .knowledge-illustration { margin-top: 46px; } }
@media (max-width: 1050px) { .intro { padding: 36px 32px 28px; } .intro h1 { font-size: 31px; } .login-form-panel { padding-inline: 36px; } .answer-card { width: 145px; padding-inline: 14px; } .source-item { padding-inline: 8px; gap: 7px; } .file-type { display: none; } }
@media (max-width: 760px) {
  .login-page { padding: 24px 20px; }
  .login-shell { grid-template-columns: 1fr; max-width: 460px; min-height: auto; border-radius: 18px; }
  .intro { padding: 27px 30px 26px; }
  .brand-mark { width: 35px; height: 38px; border-radius: 10px; }
  .brand-copy strong { font-size: 17px; }
  .brand-copy > span { font-size: 7px; }
  .intro-copy { margin-top: 25px; }
  .intro-eyebrow, .intro-copy > p, .knowledge-illustration, .intro-foot { display: none; }
  .intro h1 { margin: 0; font-size: 25px; line-height: 1.55; }
  .login-form-panel { padding: 31px 30px 65px; }
  .login-form-panel :deep(.el-input__inner) { font-size: 16px; }
  .card-head { margin-bottom: 28px; }
  .form-eyebrow { display: none; }
  .card-head h2 { margin-top: 0; font-size: 25px; }
  .card-head p { font-size: 12px; }
  .card-foot { margin-top: 27px; }
  .form-bottom { bottom: 23px; font-size: 9px; }
  .login-caption { margin-top: 20px; font-size: 9px; }
}
@media (max-width: 380px) { .login-page { padding: 16px 12px; } .intro, .login-form-panel { padding-inline: 23px; } }
</style>
