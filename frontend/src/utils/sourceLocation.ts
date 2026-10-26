import type { SourceLocation, SourcePreview } from '../api/index.ts'

type Translate = (key: string, values?: Record<string, string | number>) => string

/** 位置来自已验证索引，缺失时保持缺失，不用片段编号猜测页码。 */
export function sourceLocationLabels(location: SourceLocation, translate: Translate): string[] {
  const labels: string[] = []
  if (location.section) labels.push(location.section)
  if (location.sheet) labels.push(translate('chat.source.sheet', { name: location.sheet }))
  for (const [start, end, single, range] of [
    [location.page_start, location.page_end, 'chat.source.page', 'chat.source.pages'],
    [location.row_start, location.row_end, 'chat.source.row', 'chat.source.rows'],
    [location.line_start, location.line_end, 'chat.source.line', 'chat.source.lines']
  ] as const) {
    if (!Number.isInteger(start) || (start ?? 0) < 1) continue
    const last = Number.isInteger(end) && end! >= start! ? end! : start!
    labels.push(last === start ? translate(single, { index: start! }) : translate(range, { start: start!, end: last }))
  }
  return labels
}

type PdfSource = Pick<SourcePreview, 'chunk_index' | 'chunks' | 'location'>

/** 合并相邻片段后仍跳到真正命中的片段，而不是范围内的第一页。 */
export function sourceTargetPage(source: PdfSource): number | null {
  const anchor = source.chunks.find(chunk => chunk.chunk_index === source.chunk_index)
  // 命中片段存在但没有可核验页码时，不能拿相邻片段的位置代替。
  const page = anchor ? anchor.location.page_start : source.location.page_start
  return Number.isInteger(page) && page! >= 1 ? page! : null
}

export function pdfSourceUrl(blobUrl: string, page?: number | null): string {
  return Number.isInteger(page) && page! >= 1 ? `${blobUrl}#page=${page}` : blobUrl
}
