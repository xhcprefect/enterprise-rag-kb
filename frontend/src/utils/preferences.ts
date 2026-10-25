export const THEME_KEY = 'rag_theme'
export const LOCALE_KEY = 'rag_locale'
export type ThemePreference = 'light' | 'dark' | 'system'
export type AppLocale = 'zh-CN' | 'en-US'
export interface Preferences { theme: ThemePreference; locale: AppLocale }
export interface PreferenceStorage { getItem(key: string): string | null }

export function normalizeTheme(value: unknown): ThemePreference {
  return value === 'light' || value === 'dark' ? value : 'system'
}

export function normalizeLocale(value: unknown, browserLanguage = 'zh-CN'): AppLocale {
  if (value === 'zh-CN' || value === 'en-US') return value
  return browserLanguage.toLowerCase().startsWith('en') ? 'en-US' : 'zh-CN'
}

export function resolveTheme(preference: ThemePreference, systemDark: boolean): 'light' | 'dark' {
  return preference === 'system' ? (systemDark ? 'dark' : 'light') : preference
}

export function readBrowserPreferences(): Preferences {
  let storage: PreferenceStorage | undefined
  try { storage = window.localStorage } catch { /* 禁用存储时使用默认值。 */ }
  return readPreferences(storage, typeof navigator === 'undefined' ? 'zh-CN' : navigator.language)
}

export function readPreferences(storage?: PreferenceStorage, browserLanguage = 'zh-CN'): Preferences {
  const read = (key: string) => {
    try { return storage?.getItem(key) } catch { return null }
  }
  return {
    theme: normalizeTheme(read(THEME_KEY)),
    locale: normalizeLocale(read(LOCALE_KEY), browserLanguage)
  }
}
