import type { KnowledgeBase } from '@/api'

/**
 * 知识库维护能力只来自服务端响应。
 *
 * 不接收用户角色，也不把 admin 当作未知能力的兜底：旧响应缺少字段、字段不是
 * 明确的 true，或者当前库不是 upload 库，都必须按"不能维护"处理。
 */
export type KbCapabilityState =
  | { status: 'unknown' }
  | { status: 'known'; documents: boolean; members: boolean }

type CapabilityInput = Pick<
  KnowledgeBase,
  'kind' | 'can_manage_documents' | 'can_manage_members'
>

/**
 * 旧响应可能缺少 kind；历史上普通库都是 upload，因此只有明确 project 才排除。
 * 缺失 kind 且服务端明确 can_manage_documents=true 时仍按普通库兼容。
 */
export function isUploadKb(
  kb: Pick<KnowledgeBase, 'kind'> | null | undefined
): boolean {
  return !!kb && kb.kind !== 'project'
}

/** 只有拿到 KB 记录后能力才算已知；null 表示尚未加载或加载失败。 */
export function capabilitiesFromKb(
  kb: CapabilityInput | null | undefined
): KbCapabilityState {
  if (!kb) return { status: 'unknown' }
  return {
    status: 'known',
    documents: isUploadKb(kb) && kb.can_manage_documents === true,
    members: kb.can_manage_members === true
  }
}

export function canManageDocuments(
  kb: CapabilityInput | null | undefined
): boolean {
  const state = capabilitiesFromKb(kb)
  return state.status === 'known' && state.documents
}

export function canManageMembers(
  kb: CapabilityInput | null | undefined
): boolean {
  const state = capabilitiesFromKb(kb)
  return state.status === 'known' && state.members
}

/** 能力查询失败后必须收敛为未知，不能沿用上一次已知值。 */
export function capabilityStateAfterFailure(): KbCapabilityState {
  return { status: 'unknown' }
}

export function capabilityErrorStatus(error: unknown): number | null {
  if (!error || typeof error !== 'object') return null
  const status = (error as { response?: { status?: unknown } }).response?.status
  return typeof status === 'number' ? status : null
}

export function isCancelledKbRequest(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const candidate = error as { code?: unknown; name?: unknown }
  return (
    candidate.code === 'ERR_CANCELED' ||
    candidate.code === 'ABORT_ERR' ||
    candidate.name === 'CanceledError' ||
    candidate.name === 'AbortError'
  )
}

export type KbCapabilityErrorKind =
  | 'cancelled'
  | 'unauthenticated'
  | 'forbidden'
  | 'not_found'
  | 'conflict'
  | 'other'

export function classifyKbCapabilityError(error: unknown): KbCapabilityErrorKind {
  if (isCancelledKbRequest(error)) return 'cancelled'
  const status = capabilityErrorStatus(error)
  if (status === 401) return 'unauthenticated'
  if (status === 403) return 'forbidden'
  if (status === 404 || status === 410) return 'not_found'
  if (status === 409) return 'conflict'
  return 'other'
}

/** 401/403/404（含 410）表示权限或对象已不可用，必须停止维护动作。 */
export function revokesCapability(error: unknown): boolean {
  const kind = classifyKbCapabilityError(error)
  return kind === 'unauthenticated' || kind === 'forbidden' || kind === 'not_found'
}

/** 能力暂时未知时暂存的 complete 回执；恢复后只在同一上传代次/条目上重放。 */
export interface PendingCompletionReceipt {
  key: string
  generation: number
}

export interface CompletionTarget {
  key: string
  phase: string
}

/**
 * 能力恢复后，暂存的完成回执是否仍属于当前条目：
 * 必须是同一 key、同一上传代次、当前仍在可写阶段，且能力确实恢复。
 * 明确撤权（capabilityManage=false）一律不重放。
 */
export function completionReceiptStillApplies(
  receipt: PendingCompletionReceipt | null | undefined,
  current: CompletionTarget | null | undefined,
  currentGeneration: number,
  capabilityManage: boolean
): boolean {
  if (!capabilityManage || !receipt || !current) return false
  if (current.key !== receipt.key) return false
  if (currentGeneration !== receipt.generation) return false
  return current.phase === 'uploading' || current.phase === 'paused'
}

export type DocMissingAction = 'advance' | 'stop'

/**
 * 单文档 404 是否可按“文档已被删除”推进队列。
 * 必须先取新鲜能力：任何未知或不可维护都收敛停止，不能凭旧快照的 true 推进。
 */
export function docMissingAction(fresh: KbCapabilityState): DocMissingAction {
  if (fresh.status !== 'known') return 'stop'
  return fresh.documents ? 'advance' : 'stop'
}