/** Only one growing message is cached, so every token cannot accumulate a new cache entry. */
export function createStreamingMarkdown(render: (text: string) => string, now: () => number = () => performance.now()) {
  let previous = ''
  let html = ''
  let renderedAt = -Infinity
  return {
    render(text: string) {
      const interval = text.length < 8000 ? 40 : text.length < 32000 ? 100 : 200
      const current = now()
      if (text === previous) return html
      // A new message or replacement must render immediately; only append-only growth is throttled.
      if (!text.startsWith(previous) || current - renderedAt >= interval) {
        html = render(text)
        previous = text
        renderedAt = current
      }
      return html
    },
    clear() { previous = ''; html = ''; renderedAt = -Infinity }
  }
}
