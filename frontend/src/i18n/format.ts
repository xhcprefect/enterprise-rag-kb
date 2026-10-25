import { i18n } from './index'

/** 在调用处读取响应式语言；模板与 computed 会自动跟随语言切换。 */
export function formatDateTime(value: string | number | Date, options?: Intl.DateTimeFormatOptions) {
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat(i18n.global.locale.value, options || {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
  }).format(date)
}

export function formatNumber(value: number, options?: Intl.NumberFormatOptions) {
  return new Intl.NumberFormat(i18n.global.locale.value, options).format(value)
}
