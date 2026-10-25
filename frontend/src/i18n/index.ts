import { createI18n } from 'vue-i18n'
import { messages } from './messages/index.ts'
import { readBrowserPreferences } from '../utils/preferences.ts'

export const i18n = createI18n({
  legacy: false,
  globalInjection: true,
  locale: readBrowserPreferences().locale,
  fallbackLocale: 'zh-CN',
  messages
})
