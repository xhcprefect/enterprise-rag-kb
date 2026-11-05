import client from './client'
import type { PageResult } from './index'

/**
 * 本地项目文档/源码索引 API（与 backend/app/api/v1/projects.py、schemas/project.py 一致）。
 *
 * 读接口沿用知识库 ACL；写接口仅管理员。统一响应经 client 解包 ApiResponse.data。
 * 每个读请求都可带 AbortSignal，供页面切换/卸载时取消，避免迟到响应覆盖新状态。
 */

export interface ProjectSource {
  id: string
  kb_id: string
  name: string
  root_key: string
  relative_dir: string
  active_snapshot_id: string | null
  revoked_at: string | null
  created_at: string
}

export type ProjectSnapshotStatus = 'pending' | 'processing' | 'ready' | 'failed' | 'cancelled'

export interface ProjectSnapshot {
  id: string
  source_id: string
  status: ProjectSnapshotStatus
  base_snapshot_id: string | null
  progress: number
  file_count: number
  total_bytes: number
  manifest_fingerprint: string | null
  config_fingerprint: string | null
  error_code: string | null
  error_message: string | null
  cancel_requested: boolean
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export type ProjectFileParseStatus = 'pending' | 'ready' | 'failed' | 'fallback'

export interface ProjectFile {
  file_version_id: string
  source_id: string
  snapshot_id: string | null
  relative_path: string
  content_sha256: string
  size: number
  language: string | null
  parse_status: ProjectFileParseStatus
  chunk_count: number
  char_count: number
  symbol: string | null
  symbol_kind: string | null
  created_at: string | null
}

export interface ProjectSearchHit {
  source_id: string
  snapshot_id: string
  file_version_id: string
  relative_path: string
  language: string | null
  symbol: string | null
  symbol_kind: string | null
  line_start: number | null
  line_end: number | null
  chunk_index: number
  snippet: string
  score: number | null
  parse_status: string
}

export interface ProjectSearchResult {
  results: ProjectSearchHit[]
  total: number
  snapshot_id: string
}

export interface ProjectSourceChunk {
  chunk_index: number
  content: string
  line_start: number | null
  line_end: number | null
}

export interface ProjectSourcePreview {
  source_id: string
  snapshot_id: string
  file_version_id: string
  relative_path: string
  language: string | null
  parse_status: string
  chunk_start: number
  chunk_end: number
  chunks: ProjectSourceChunk[]
  truncated: boolean
}

export interface ProjectSourceCreate {
  name: string
  root_key: string
  relative_dir: string
}

const PAGE_SIZE = 100

export const projectApi = {
  /** 当前用户可见的项目源（写仅管理员，读沿用 KB ACL）。 */
  list: (signal?: AbortSignal) => client.get<ProjectSource[]>('/projects', { signal }),

  /** 可用授权 root_key（仅管理员；绝不返回绝对路径）。 */
  roots: (signal?: AbortSignal) => client.get<string[]>('/projects/roots', { signal }),

  get: (sourceId: string, signal?: AbortSignal) =>
    client.get<ProjectSource>(`/projects/${sourceId}`, { signal }),

  create: (data: ProjectSourceCreate, signal?: AbortSignal) =>
    client.post<ProjectSource>('/projects', data, { signal }),

  /** 软撤销（仅管理员）；已撤销源后续读取返回 410。 */
  revoke: (sourceId: string, signal?: AbortSignal) =>
    client.delete<null>(`/projects/${sourceId}`, { signal }),

  /** 手动同步入队（仅管理员，幂等）；返回当前快照，未就绪不代表可搜索。 */
  sync: (sourceId: string, signal?: AbortSignal) =>
    client.post<ProjectSnapshot>(`/projects/${sourceId}/sync`, undefined, { signal }),

  /** 取消活动同步（仅管理员）；无活动任务返回 409。 */
  cancelSync: (sourceId: string, signal?: AbortSignal) =>
    client.post<ProjectSnapshot>(`/projects/${sourceId}/sync/cancel`, undefined, { signal }),

  /** 同步状态/历史（最多 50 条，按时间倒序）。 */
  snapshots: (sourceId: string, signal?: AbortSignal) =>
    client.get<ProjectSnapshot[]>(`/projects/${sourceId}/snapshots`, { signal }),

  /** 某一快照视图下的文件分页（offset/limit，limit<=100）。 */
  files: (
    sourceId: string,
    params: { snapshot_id?: string; offset?: number; limit?: number; signal?: AbortSignal }
  ) =>
    client.get<PageResult<ProjectFile>>(`/projects/${sourceId}/files`, {
      params: { snapshot_id: params.snapshot_id, offset: params.offset, limit: params.limit ?? PAGE_SIZE },
      signal: params.signal
    }),

  /** 项目内容独立搜索（真实内容检索，非文件名过滤）。 */
  search: (
    sourceId: string,
    params: {
      query: string
      snapshot_id?: string
      top_k?: number
      path_prefix?: string
      signal?: AbortSignal
    }
  ) =>
    client.get<ProjectSearchResult>(`/projects/${sourceId}/search`, {
      params: {
        query: params.query,
        snapshot_id: params.snapshot_id,
        top_k: params.top_k ?? 8,
        path_prefix: params.path_prefix || undefined
      },
      signal: params.signal
    }),

  /** 项目引用原文（只凭 file_version_id，沿 KB ACL；仅文本）。 */
  preview: (
    sourceId: string,
    fileVersionId: string,
    params: { snapshot_id?: string; chunk_start?: number; chunk_end?: number; signal?: AbortSignal }
  ) =>
    client.get<ProjectSourcePreview>(`/projects/${sourceId}/files/${fileVersionId}/source`, {
      params: {
        snapshot_id: params.snapshot_id,
        chunk_start: params.chunk_start,
        chunk_end: params.chunk_end
      },
      signal: params.signal
    })
}

/** 快照是否可视为"已就绪可搜索"。pending=成功绝不等于可搜索。 */
export function isReadySnapshot(snap: { status: string } | null | undefined): boolean {
  return !!snap && snap.status === 'ready'
}

/** 尚有未结束（pending/processing）的活动同步。 */
export function isActiveSync(snap: { status: string } | null | undefined): boolean {
  return !!snap && (snap.status === 'pending' || snap.status === 'processing')
}

/** 从快照列表里取出最新的 ready 快照；没有则返回 null。 */
export function latestReadySnapshot(snaps: { status: string; id: string }[]): { id: string; status: string } | null {
  return snaps.find((s) => s.status === 'ready') ?? null
}
