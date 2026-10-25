import { computed, onScopeDispose, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { i18n } from '@/i18n'
import {
  LOCALE_KEY, THEME_KEY, readBrowserPreferences, resolveTheme,
  type AppLocale, type ThemePreference
} from '@/utils/preferences'

export const usePreferencesStore = defineStore('preferences', () => {
  const initial = readBrowserPreferences()
  const themePreference = ref<ThemePreference>(initial.theme)
  const locale = ref<AppLocale>(initial.locale)
  const media = window.matchMedia('(prefers-color-scheme: dark)')
  const systemDark = ref(media.matches)
  const resolvedTheme = computed(() => resolveTheme(themePreference.value, systemDark.value))
  const persist = (key: string, value: string) => {
    try { window.localStorage.setItem(key, value) } catch { /* 偏好仍在当前页面生效。 */ }
  }

  watch(resolvedTheme, (theme) => {
    const root = document.documentElement
    root.classList.toggle('dark', theme === 'dark')
    root.dataset.theme = theme
    root.style.colorScheme = theme
  }, { immediate: true })
  watch(themePreference, (value) => persist(THEME_KEY, value), { immediate: true })
  watch(locale, (value) => {
    i18n.global.locale.value = value
    document.documentElement.lang = value
    persist(LOCALE_KEY, value)
  }, { immediate: true })

  function setTheme(value: unknown) {
    if (value === 'light' || value === 'dark' || value === 'system') themePreference.value = value
  }
  function setLocale(value: unknown) {
    if (value === 'zh-CN' || value === 'en-US') locale.value = value
  }
  function onSystemThemeChange(event: MediaQueryListEvent) { systemDark.value = event.matches }
  function onStorage(event: StorageEvent) {
    if (event.key !== null && event.key !== THEME_KEY && event.key !== LOCALE_KEY) return
    const next = readBrowserPreferences()
    themePreference.value = next.theme
    locale.value = next.locale
  }
  media.addEventListener('change', onSystemThemeChange)
  window.addEventListener('storage', onStorage)
  onScopeDispose(() => {
    media.removeEventListener('change', onSystemThemeChange)
    window.removeEventListener('storage', onStorage)
  })

  return { themePreference, resolvedTheme, locale, setTheme, setLocale }
})
