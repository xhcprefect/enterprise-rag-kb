<template>
  <div class="login-page">
    <div class="login-preferences"><AppearanceControls /></div>
    <div class="login-shell">
      <section class="intro" :aria-label="t('login.introLabel')">
        <div class="brand"><span class="brand-mark"><el-icon :size="23"><Reading /></el-icon></span><span class="brand-copy"><strong>{{ t('common.appName') }}<span>.</span></strong><span>KNOWLEDGE WORKSPACE</span></span></div>
        <div class="intro-copy">
          <span class="intro-eyebrow">{{ t('login.tagline') }}</span>
          <h1>{{ t('login.titleLine1') }}<br />{{ t('login.titleLine2') }}</h1>
          <p>{{ t('login.descriptionLine1') }}<br class="desktop-break" />{{ t('login.descriptionLine2') }}</p>
        </div>
        <div class="knowledge-illustration" aria-hidden="true">
          <div class="source-stack">
            <div class="source-item"><span class="source-icon"><el-icon><Document /></el-icon></span><span>{{ t('login.productDocs') }}<span class="source-line"></span></span><span class="file-type">PDF</span></div>
            <div class="source-item"><span class="source-icon"><el-icon><DocumentCopy /></el-icon></span><span>{{ t('login.workflows') }}<span class="source-line short"></span></span><span class="file-type">DOC</span></div>
            <div class="source-item"><span class="source-icon"><el-icon><Files /></el-icon></span><span>{{ t('login.teamGuides') }}<span class="source-line"></span></span><span class="file-type">MD</span></div>
          </div>
          <div class="connect-path"><span></span><el-icon><ArrowRight /></el-icon></div>
          <div class="answer-card">
            <span class="answer-icon"><el-icon :size="20"><MagicStick /></el-icon></span><strong>{{ t('login.discover') }}</strong>
            <span class="answer-line"></span><span class="answer-line medium"></span><span class="answer-line short"></span>
            <span class="answer-source"><el-icon><DocumentCopy /></el-icon>{{ t('login.sources') }}</span>
          </div>
        </div>
        <div class="intro-foot">
          <span><span class="step-num">01</span>{{ t('login.step1') }}</span><span class="step-divider"></span>
          <span><span class="step-num">02</span>{{ t('login.step2') }}</span><span class="step-divider"></span>
          <span><span class="step-num">03</span>{{ t('login.step3') }}</span>
        </div>
      </section>
      <section class="login-form-panel">
        <div class="form-content">
          <div class="card-head"><span class="form-eyebrow">{{ t('login.eyebrow') }}</span><h2>{{ t('login.welcome') }}</h2><p>{{ t('login.subtitle') }}</p></div>
          <el-form ref="formRef" :model="form" :rules="rules" :validate-on-rule-change="false" size="large" label-position="top" @submit.prevent="onSubmit">
            <el-form-item :label="t('common.username')" prop="username">
              <el-input v-model="form.username" :placeholder="t('login.usernamePlaceholder')" autocomplete="username" clearable :disabled="loading"><template #prefix><el-icon><User /></el-icon></template></el-input>
            </el-form-item>
            <el-form-item :label="t('common.password')" prop="password">
              <el-input v-model="form.password" type="password" :placeholder="t('login.passwordPlaceholder')" autocomplete="current-password" show-password :disabled="loading"><template #prefix><el-icon><Lock /></el-icon></template></el-input>
            </el-form-item>
            <div class="account-note"><el-icon><InfoFilled /></el-icon><span>{{ t('login.accountNote') }}</span></div>
            <el-button type="primary" native-type="submit" :loading="loading" class="submit-btn"><span>{{ t('login.submit') }}</span><el-icon v-if="!loading"><ArrowRight /></el-icon></el-button>
          </el-form>
          <div class="card-foot"><span class="help-icon"><el-icon :size="16"><Lock /></el-icon></span><span><strong>{{ t('login.help') }}</strong><span>{{ t('login.helpNote') }}</span></span></div>
        </div>
        <span class="form-bottom">{{ t('login.footer') }}</span>
      </section>
    </div>
    <div class="login-caption">{{ t('login.caption') }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import AppearanceControls from '@/components/AppearanceControls.vue'
import { useUserStore } from '@/stores/user'
import { getLoginRedirect } from '@/router/guards'

const router = useRouter()
const route = useRoute()
const { t, locale } = useI18n()
const userStore = useUserStore()
const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '' })
const rules = computed<FormRules>(() => ({
  username: [{ required: true, message: t('login.usernamePlaceholder'), trigger: 'blur' }],
  password: [{ required: true, message: t('login.passwordPlaceholder'), trigger: 'blur' }]
}))
watch(locale, () => formRef.value?.clearValidate())

async function onSubmit() {
  if (!formRef.value || loading.value) return
  loading.value = true
  try {
    const valid = await formRef.value.validate().catch(() => false)
    if (!valid) return
    await userStore.login(form.username, form.password)
    ElMessage.success(t('login.success'))
    await router.replace(getLoginRedirect(route.query.redirect))
  } catch {
    /* 错误提示由拦截器统一处理。 */
  } finally { loading.value = false }
}
</script>

<style scoped>
.login-page { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; min-height: 100dvh; padding: 28px 32px; background: var(--bg); }
.login-preferences { display: flex; justify-content: flex-end; width: min(1120px, 100%); margin-bottom: 14px; }
.login-shell { display: grid; grid-template-columns: 1.08fr 1fr; width: min(1120px, 100%); min-height: 660px; border: 1px solid var(--line); border-radius: 24px; background: var(--surface); box-shadow: var(--shadow-lg); overflow: hidden; }
.intro { position: relative; display: flex; flex-direction: column; padding: 42px 46px 32px; background: var(--login-intro-bg, #193c2f); color: #edf2e7; overflow: hidden; }
.intro::after { position: absolute; width: 350px; height: 350px; right: -260px; top: -165px; border: 1px solid #3c5b42; border-radius: 50%; box-shadow: 0 0 0 40px #234331, 0 0 0 41px #3c5b42, 0 0 0 80px #234331, 0 0 0 81px #3c5b42; content: ''; pointer-events: none; opacity: .6; }
.brand { position: relative; z-index: 1; display: inline-flex; align-items: center; gap: 12px; }
.brand-mark { display: grid; place-items: center; width: 44px; height: 46px; border: 1px solid #6e8c64; border-radius: 12px; color: #d3e5ab; background: #2b503a; flex-shrink: 0; }
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
.source-item { display: flex; align-items: center; gap: 10px; min-height: 47px; padding: 9px 12px; border: 1px solid #466348; border-radius: 8px; background: #284a35; }
.source-icon { display: grid; place-items: center; flex-shrink: 0; width: 27px; height: 30px; border: 1px solid #5c7551; border-radius: 5px; background: #34573c; color: #c7d5aa; font-size: 14px; }
.source-item > span:nth-child(2) { flex: 1; min-width: 0; color: #d0debf; font-size: 10px; }
.source-line { display: block; width: 50px; max-width: 100%; height: 3px; margin-top: 5px; border-radius: 2px; background: #5e7852; }
.source-line.short { width: 34px; }
.file-type { color: #90ab88; font-size: 7px; letter-spacing: .07em; }
.connect-path { display: flex; align-items: center; width: 37px; flex-shrink: 0; color: #9fb87a; }
.connect-path > span { flex: 1; height: 1px; background: #829a66; }
.connect-path > .el-icon { margin-left: -4px; font-size: 11px; }
.answer-card { display: flex; flex-direction: column; align-items: flex-start; width: 172px; flex-shrink: 0; padding: 16px 18px 13px; border: 1px solid var(--accent-border); border-radius: 11px; background: var(--brand-soft); transform: rotate(2deg); box-shadow: 0 8px 20px rgb(12 32 18 / 12%); }
.answer-icon { display: grid; place-items: center; width: 31px; height: 31px; margin-bottom: 12px; border-radius: 9px; background: var(--brand-tint); color: var(--brand); }
.answer-card > strong { margin-bottom: 13px; color: var(--brand-dark); font-size: 11px; font-weight: 600; }
.answer-line { width: 100%; height: 4px; margin-bottom: 7px; border-radius: 2px; background: var(--accent-border); }
.answer-line.medium { width: 86%; }
.answer-line.short { width: 62%; }
.answer-source { display: flex; align-items: center; gap: 5px; margin-top: 7px; color: var(--text-3); font-size: 9px; }
.intro-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: auto; padding-top: 19px; border-top: 1px solid #395640; }
.intro-foot > span:not(.step-divider) { display: inline-flex; align-items: center; gap: 7px; color: #b4c7a6; font-size: 10px; }
.step-num { color: #839f70; font-size: 9px; font-variant-numeric: tabular-nums; }
.step-divider { width: 13px; height: 1px; flex-shrink: 0; background: #5b7651; }
.login-form-panel { position: relative; display: flex; align-items: center; justify-content: center; padding: 64px 56px 82px; }
.form-content { width: 100%; max-width: 350px; }
.card-head { margin-bottom: 37px; }
.form-eyebrow { color: var(--text-3); font-size: 11px; letter-spacing: .04em; }
.card-head h2 { margin: 14px 0 10px; font-size: 30px; font-weight: 650; letter-spacing: -.04em; }
.card-head p { margin: 0; color: var(--text-3); font-size: 13px; line-height: 1.7; }
.login-form-panel :deep(.el-form-item) { margin-bottom: 24px; }
.login-form-panel :deep(.el-form-item__label) { padding-bottom: 9px; color: var(--text); font-size: 12px; font-weight: 550; }
.login-form-panel :deep(.el-input__wrapper) { min-height: 46px; padding-inline: 14px; background: var(--surface-soft); }
.login-form-panel :deep(.el-input__inner) { font-size: 13px; }
.login-form-panel :deep(.el-input__prefix) { margin-right: 4px; color: var(--text-3); }
.account-note { display: flex; align-items: center; gap: 6px; margin: -1px 0 24px; color: var(--text-3); font-size: 11px; line-height: 1.7; }
.account-note .el-icon { flex-shrink: 0; color: var(--text-3); font-size: 12px; }
.submit-btn { width: 100%; height: 46px; font-size: 13px; }
.submit-btn :deep(> span) { display: flex; align-items: center; justify-content: center; gap: 13px; }
.card-foot { display: flex; align-items: center; gap: 11px; margin-top: 35px; padding: 19px 0 0; border-top: 1px solid var(--line); }
.help-icon { display: grid; place-items: center; width: 34px; height: 34px; border: 1px solid var(--line); border-radius: 10px; color: var(--text-3); background: var(--surface-soft); flex-shrink: 0; }
.card-foot > span:last-child { display: flex; flex-direction: column; gap: 5px; }
.card-foot strong { color: var(--text-2); font-size: 11px; font-weight: 550; }
.card-foot > span:last-child > span { color: var(--text-3); font-size: 10px; line-height: 1.7; }
.form-bottom { position: absolute; bottom: 28px; left: 20px; right: 20px; color: var(--text-3); font-size: 10px; letter-spacing: .04em; text-align: center; }
.login-caption { margin-top: 24px; color: var(--text-3); font-size: 10px; letter-spacing: .08em; text-align: center; }
@media (min-width: 1500px) { .login-shell { min-height: 710px; } .intro { padding: 48px 52px 36px; } .intro-copy { margin-top: 65px; } .knowledge-illustration { margin-top: 46px; } }
@media (max-width: 1050px) { .intro { padding: 36px 32px 28px; } .intro h1 { font-size: 31px; } .login-form-panel { padding-inline: 36px; } .answer-card { width: 145px; padding-inline: 14px; } .source-item { padding-inline: 8px; gap: 7px; } .file-type { display: none; } }
@media (max-width: 760px) {
  .login-page { padding: 20px; }
  .login-preferences { max-width: 460px; }
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
  .form-bottom { bottom: 20px; font-size: 9px; line-height: 1.6; }
  .login-caption { margin-top: 20px; font-size: 9px; }
}
@media (max-width: 380px) { .login-page { padding: 16px 12px; } .intro, .login-form-panel { padding-inline: 23px; } }
</style>
