/**
 * 知识库成员管理的纯逻辑：分页边界、角色白名单、错误分类与迟到响应守卫。
 * 组件只根据这些结果决定是否写入界面，不缓存跨请求的成员角色。
 */

export const KB_MEMBER_ROLES = ['reader', 'maintainer'] as const
export type KbMemberRole = (typeof KB_MEMBER_ROLES)[number]

export const MEMBER_PAGE_SIZE = 20
export const MEMBER_MAX_PAGE_SIZE = 100

export function isKbMemberRole(value: unknown): value is KbMemberRole {
  return typeof value === 'string' && (KB_MEMBER_ROLES as readonly string[]).includes(value)
}

/** 页码异常时收敛到 1，不接受 0/负数/NaN 作为请求参数。 */
export function normalizeMemberPage(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed)) return 1
  return Math.max(1, Math.trunc(parsed))
}

/** 契约要求 page_size <= 100；非法值回落到默认 20。 */
export function normalizeMemberPageSize(value: unknown): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed) || parsed < 1) return MEMBER_PAGE_SIZE
  return Math.min(MEMBER_MAX_PAGE_SIZE, Math.trunc(parsed))
}

/** revision 只接受非负整数；缺失/负数/小数都视为未知（null）。 */
export function normalizeMemberRevision(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) return null
  return value
}

/** 移除成员后当前页可能为空：收敛到仍存在的最后一页。 */
export function pageAfterRemoval(
  page: unknown,
  totalAfterRemoval: unknown,
  pageSize: unknown
): number {
  const size = normalizeMemberPageSize(pageSize)
  const total =
    typeof totalAfterRemoval === 'number' && Number.isFinite(totalAfterRemoval)
      ? Math.max(0, Math.trunc(totalAfterRemoval))
      : 0
  const lastPage = Math.max(1, Math.ceil(total / size))
  return Math.min(normalizeMemberPage(page), lastPage)
}

export type MemberErrorKind =
  | 'cancelled'
  | 'conflict'
  | 'invalid'
  | 'not_found'
  | 'unauthenticated'
  | 'forbidden'
  | 'other'

export function memberErrorStatus(error: unknown): number | null {
  if (!error || typeof error !== 'object') return null
  const status = (error as { response?: { status?: unknown } }).response?.status
  return typeof status === 'number' ? status : null
}

export function isCancelledMemberRequest(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const candidate = error as { code?: unknown; name?: unknown }
  return (
    candidate.code === 'ERR_CANCELED' ||
    candidate.code === 'ABORT_ERR' ||
    candidate.name === 'CanceledError' ||
    candidate.name === 'AbortError'
  )
}

export function classifyMemberError(error: unknown): MemberErrorKind {
  if (isCancelledMemberRequest(error)) return 'cancelled'
  const status = memberErrorStatus(error)
  if (status === 409) return 'conflict'
  if (status === 401) return 'unauthenticated'
  if (status === 403) return 'forbidden'
  if (status === 404 || status === 410) return 'not_found'
  if (status === 400 || status === 422) return 'invalid'
  return 'other'
}

export function memberErrorKey(kind: MemberErrorKind): string {
  switch (kind) {
    case 'conflict':
      return 'knowledge.members.errors.conflict'
    case 'invalid':
      return 'knowledge.members.errors.invalid'
    case 'not_found':
      return 'knowledge.members.errors.notFound'
    case 'unauthenticated':
    case 'forbidden':
      return 'knowledge.members.errors.forbidden'
    case 'cancelled':
      return 'knowledge.members.errors.cancelled'
    default:
      return 'knowledge.members.errors.generic'
  }
}

/** 409 后必须刷新当前列表，但绝不能自动重放刚才的写请求。 */
export function shouldReloadAfterMemberError(kind: MemberErrorKind): boolean {
  return kind === 'conflict' || kind === 'not_found'
}

/** 400（例如停用用户）与用户不存在时，搜索结果已经过期，需要重新搜索。 */
export function shouldRefreshCandidatesAfterMemberError(kind: MemberErrorKind): boolean {
  return kind === 'invalid' || kind === 'not_found'
}

/**
 * 未知失败（网络中断/超时/5xx）：写请求可能已经提交而响应丢失，
 * 旧成员列表与旧 revision 都不能再当作事实，必须收敛为未知并等待用户安全刷新。
 * 这里只允许重新读取，绝不自动重放写请求。
 */
export function shouldInvalidateMemberState(kind: MemberErrorKind): boolean {
  return kind === 'other'
}

/** 成员管理本身不可用的错误：应关闭对话框并由上层重新核对能力。 */
export function shouldStopMemberManagement(kind: MemberErrorKind): boolean {
  return kind === 'unauthenticated' || kind === 'forbidden'
}

export type MemberTargetState = 'ready' | 'inactive' | 'existing'

export function memberTargetState(
  user: { id: string; is_active: boolean },
  existingIds: ReadonlySet<string>
): MemberTargetState {
  if (!user.is_active) return 'inactive'
  if (existingIds.has(user.id)) return 'existing'
  return 'ready'
}

export interface MemberRequestIdentity {
  session: number
  kbId: string
  requestId: number
}

export interface MemberRequestSnapshot {
  session: number
  kbId: string | null
  open: boolean
  disposed: boolean
}

/**
 * 迟到响应守卫：关闭、切库、卸载、或同一类请求已有更新的序号时一律丢弃。
 * requestId 由列表/搜索各自维护，避免慢搜索覆盖新的列表或反过来。
 */
export function isMemberRequestCurrent(
  identity: MemberRequestIdentity,
  snapshot: MemberRequestSnapshot,
  latestRequestId: number
): boolean {
  return (
    !snapshot.disposed &&
    snapshot.open &&
    snapshot.session === identity.session &&
    snapshot.kbId === identity.kbId &&
    latestRequestId === identity.requestId
  )
}

export interface CapturedRemoval {
  session: number
  kbId: string
  expectedRevision: number
}

export interface RemovalSubmissionState extends MemberRequestSnapshot {
  revision: number | null
  busy: boolean
}

/**
 * 移除确认框打开前固定的会话/KB/revision，在确认后是否仍可提交。
 *
 * 等待期间关闭并重开同一 KB 会得到新的 generation 与 revision：即使 KB id 相同，
 * 旧确认也不能借用新版本发 DELETE，否则 isAlive 无法识别。版本在等待期间被刷新
 * 同样不允许提交，交回列表重新确认（409 刷新语义）。
 */
export function canSubmitCapturedRemoval(
  captured: CapturedRemoval,
  current: RemovalSubmissionState
): boolean {
  if (current.disposed || !current.open || current.busy) return false
  if (!Number.isInteger(captured.expectedRevision) || captured.expectedRevision < 0) return false
  if (current.session !== captured.session || current.kbId !== captured.kbId) return false
  return current.revision === captured.expectedRevision
}