import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

/** 窄屏侧栏：断点同步、键盘关闭、焦点约束与关闭后的焦点恢复。 */
export function useResponsiveDrawer(query = '(max-width: 900px)') {
  const isNarrow = ref(window.matchMedia(query).matches)
  const isOpen = ref(false)
  const panelRef = ref<HTMLElement>()
  let media: MediaQueryList | undefined
  let previousFocus: HTMLElement | null = null
  let focusFrame = 0

  const focusableElements = () => Array.from(
    panelRef.value?.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
    ) || []
  ).filter((element) => element.getClientRects().length > 0 && !element.closest('[inert]'))

  function close() { isOpen.value = false }
  function toggle() { isOpen.value = !isOpen.value }
  function onBreakpointChange(event: MediaQueryListEvent) {
    isNarrow.value = event.matches
    if (!event.matches) close()
  }

  function onKeydown(event: KeyboardEvent) {
    if (!isNarrow.value || !isOpen.value) return
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      close()
      return
    }
    if (event.key !== 'Tab') return
    const elements = focusableElements()
    const first = elements[0]
    const last = elements[elements.length - 1]
    if (!first || !last) {
      event.preventDefault()
      return
    }
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  watch(isOpen, async (open) => {
    cancelAnimationFrame(focusFrame)
    if (open) {
      previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
      await nextTick()
      // 等待窄屏侧栏的可见性与背景 inert 状态生效，再移动焦点。
      focusFrame = requestAnimationFrame(() => {
        if (isOpen.value && isNarrow.value) focusableElements()[0]?.focus({ preventScroll: true })
      })
    } else {
      await nextTick()
      if (previousFocus?.isConnected && !previousFocus.closest('[inert]')) previousFocus.focus({ preventScroll: true })
      previousFocus = null
    }
  })

  onMounted(() => {
    media = window.matchMedia(query)
    isNarrow.value = media.matches
    media.addEventListener('change', onBreakpointChange)
  })
  onBeforeUnmount(() => {
    media?.removeEventListener('change', onBreakpointChange)
    cancelAnimationFrame(focusFrame)
  })

  return { isNarrow, isOpen, panelRef, close, toggle, onKeydown }
}
