/** 稳定的依赖分组；业务文件与未列出的依赖交给 Rollup 按实际引用关系处理。 */
export function getManualChunk(id: string): string | undefined {
  const normalized = id.replace(/\\/g, '/')
  const marker = '/node_modules/'
  const index = normalized.lastIndexOf(marker)
  if (index < 0) return undefined
  const dependency = normalized.slice(index + marker.length)
  const segments = dependency.split('/')
  const packageName = segments[0].startsWith('@') ? segments.slice(0, 2).join('/') : segments[0]

  if (packageName === 'vue' || packageName.startsWith('@vue/') || ['vue-router', 'pinia', 'vue-demi'].includes(packageName)) {
    return 'vendor-vue'
  }
  if (packageName === 'vue-i18n' || packageName.startsWith('@intlify/')) return 'vendor-i18n'
  if (packageName === 'axios') return 'vendor-http'
  if (packageName === '@element-plus/icons-vue') return 'vendor-icons'
  // Element Plus 交给自动分包，避免手动归并其总入口后保留未使用组件。
  if (packageName === 'highlight.js') return 'vendor-highlight'
  if (['markdown-it', 'linkify-it', 'mdurl', 'uc.micro', 'punycode.js', 'entities', 'argparse'].includes(packageName)) {
    return 'vendor-markdown'
  }
  // 不设置通用 vendor 包，避免把仅被异步页面使用的依赖带入首屏。
  return undefined
}
