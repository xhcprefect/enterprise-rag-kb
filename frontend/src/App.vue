<template>
  <el-config-provider :locale="elementLocale">
    <router-view />
  </el-config-provider>
</template>

<script setup lang="ts">
import { computed, watchEffect } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import en from 'element-plus/es/locale/lang/en'
import { usePreferencesStore } from '@/stores/preferences'

usePreferencesStore()
const route = useRoute()
const { t, locale } = useI18n()
const elementLocale = computed(() => locale.value === 'en-US' ? en : zhCn)
watchEffect(() => {
  const key = typeof route.meta.titleKey === 'string' ? route.meta.titleKey : 'nav.chat'
  document.title = `${t(key)} · ${t('common.appName')}`
})
</script>
