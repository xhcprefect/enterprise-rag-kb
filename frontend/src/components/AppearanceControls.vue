<template>
  <div class="appearance-controls">
    <el-dropdown trigger="click" @command="preferences.setTheme">
      <button class="preference-button theme-button" type="button" :title="themeLabel" :aria-label="themeLabel">
        <el-icon :size="17"><Sunny v-if="preferences.resolvedTheme === 'light'" /><Moon v-else /></el-icon>
      </button>
      <template #dropdown>
        <el-dropdown-menu :aria-label="t('preferences.theme')">
          <el-dropdown-item v-for="option in themes" :key="option.value" :command="option.value" role="menuitemradio" :aria-checked="preferences.themePreference === option.value" class="preference-option">
            <el-icon><component :is="option.icon" /></el-icon>
            <span>{{ t(`preferences.${option.value}`) }}</span>
            <el-icon v-if="preferences.themePreference === option.value" class="selected-check"><Check /></el-icon>
          </el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
    <el-dropdown trigger="click" @command="preferences.setLocale">
      <button class="preference-button language-button" type="button" :title="t('preferences.language')" :aria-label="t('preferences.currentLanguage')">
        <span>{{ locale === 'zh-CN' ? '中文' : 'EN' }}</span>
      </button>
      <template #dropdown>
        <el-dropdown-menu :aria-label="t('preferences.language')">
          <el-dropdown-item v-for="option in languages" :key="option.value" :command="option.value" role="menuitemradio" :aria-checked="locale === option.value" class="preference-option">
            <span :lang="option.value">{{ option.label }}</span>
            <el-icon v-if="locale === option.value" class="selected-check"><Check /></el-icon>
          </el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, Monitor, Moon, Sunny } from '@element-plus/icons-vue'
import { usePreferencesStore } from '@/stores/preferences'

const preferences = usePreferencesStore()
const { t, locale } = useI18n()
const themes = [{ value: 'light', icon: Sunny }, { value: 'dark', icon: Moon }, { value: 'system', icon: Monitor }]
const languages = [{ value: 'zh-CN', label: '简体中文' }, { value: 'en-US', label: 'English' }]
const themeLabel = computed(() => t('preferences.currentTheme', { theme: t(`preferences.${preferences.themePreference}`) }))
</script>

<style scoped>
.appearance-controls { display: flex; align-items: center; flex-shrink: 0; gap: 8px; }
.preference-button { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; height: 34px; padding: 0 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--text-2); cursor: pointer; transition: color .18s, background .18s, border-color .18s; }
.preference-button:hover { border-color: var(--accent-border); background: var(--brand-tint); color: var(--brand); }
.theme-button { width: 34px; padding: 0; }
.language-button { min-width: 42px; font-size: 11px; font-weight: 600; }
.preference-option { gap: 8px; min-width: 160px; }
.preference-option > span { flex: 1; }
.selected-check { margin-left: 12px; color: var(--brand); }
</style>
