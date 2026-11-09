/**
 * 文档入库质量的纯展示逻辑（不依赖 Vue/axios，可直接在 Node 测试中运行）。
 *
 * 冻结契约：issue code 只有下列已知值可映射成具体文案；未知 code 必须回退到
 * 通用文案，绝不能把后端原始键名直接展示给用户。
 */

/** 分块预览固定每页 20 条（服务端 limit 上限 50）。 */
export const QUALITY_CHUNK_PAGE_SIZE = 20

export const QUALITY_ISSUE_CODES = [
  'file_missing',
  'document_failed',
  'document_not_ready',
  'parse_incomplete',
  'no_chunks',
  'vector_incomplete',
  'empty_chunks',
  'short_chunks',
  'duplicate_chunks',
  'replacement_chars',
  'control_chars',
  'oversized_chunks',
  'missing_page_metadata',
  'chunk_index_gaps',
  'sample_truncated'
] as const

export type QualityIssueCode = (typeof QUALITY_ISSUE_CODES)[number]

const KNOWN_ISSUE_CODES = new Set<string>(QUALITY_ISSUE_CODES)

/** issue code → i18n key；未知/空 code 回退到通用「其他问题」。 */
export function qualityIssueMessageKey(code: string | null | undefined): string {
  const normalized = typeof code === 'string' ? code.trim() : ''
  return KNOWN_ISSUE_CODES.has(normalized)
    ? `documents.quality.issues.codes.${normalized}`
    : 'documents.quality.issues.unknown'
}

export type QualityIssueTagType = 'info' | 'warning' | 'danger'

/** severity → Element Plus tag 颜色；未知 severity 按信息级处理。 */
export function qualityIssueTagType(severity: string | null | undefined): QualityIssueTagType {
  if (severity === 'error') return 'danger'
  if (severity === 'warning') return 'warning'
  return 'info'
}

/** 分块总页数；空文档为 0 页。 */
export function qualityChunkPageCount(total: number, pageSize = QUALITY_CHUNK_PAGE_SIZE): number {
  if (!Number.isFinite(total) || total <= 0 || pageSize <= 0) return 0
  return Math.ceil(total / pageSize)
}

/** 1-based 页码 → 服务端 offset。非法页码按第 1 页处理。 */
export function qualityChunkOffset(page: number, pageSize = QUALITY_CHUNK_PAGE_SIZE): number {
  const safePage = Number.isInteger(page) && page > 0 ? page : 1
  return (safePage - 1) * pageSize
}

/** 把页码夹到 [1, 总页数]；空文档固定返回 1（调用方据 total 决定是否展示分页）。 */
export function clampQualityChunkPage(
  page: number,
  total: number,
  pageSize = QUALITY_CHUNK_PAGE_SIZE
): number {
  const pages = qualityChunkPageCount(total, pageSize)
  if (pages === 0) return 1
  const value = Number.isInteger(page) ? page : 1
  return Math.min(Math.max(value, 1), pages)
}
