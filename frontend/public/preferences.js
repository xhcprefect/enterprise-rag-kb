// 在样式与应用脚本之前应用偏好，避免已选择深色时首屏先显示浅色。
;(function () {
  function read(key) {
    try { return localStorage.getItem(key) } catch (_) { return null }
  }
  var savedTheme = read('rag_theme')
  var theme = savedTheme === 'light' || savedTheme === 'dark' ? savedTheme : 'system'
  var savedLocale = read('rag_locale')
  var locale = savedLocale === 'zh-CN' || savedLocale === 'en-US'
    ? savedLocale : ((navigator.language || '').toLowerCase().indexOf('en') === 0 ? 'en-US' : 'zh-CN')
  var dark = theme === 'dark' || (theme === 'system' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches)
  var root = document.documentElement
  root.classList.toggle('dark', Boolean(dark))
  root.style.colorScheme = dark ? 'dark' : 'light'
  root.dataset.theme = dark ? 'dark' : 'light'
  root.lang = locale
})()
