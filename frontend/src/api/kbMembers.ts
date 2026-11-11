import client from './client'

export type KbMemberRole = 'reader' | 'maintainer'

export interface KbMember {
  user_id: string
  username: string
  display_name: string
  is_active: boolean
  role: KbMemberRole
}

/** GET /knowledge-bases/{kbId}/members（仅管理员，有界分页）。 */
export interface KbMemberList {
  revision: number
  total: number
  items: KbMember[]
}

/** PUT/DELETE 同一响应的 data；DELETE 的 member 固定为 null。 */
export interface KbMemberMutationResult {
  revision: number
  member: KbMember | null
}

export interface KbMemberListParams {
  page?: number
  page_size?: number
}

function encodeId(id: string): string {
  return encodeURIComponent(id)
}

const membersPath = (kbId: string) => `/knowledge-bases/${encodeId(kbId)}/members`
const memberPath = (kbId: string, userId: string) =>
  `${membersPath(kbId)}/${encodeId(userId)}`

/**
 * 成员管理接口。调用方必须确认服务端 can_manage_members === true；
 * 客户端隐藏不是安全控制，后端会独立鉴权。
 */
export const kbMembersApi = {
  list: (kbId: string, params: KbMemberListParams = {}, signal?: AbortSignal) =>
    client.get<KbMemberList>(membersPath(kbId), { params, signal }),

  setRole: (
    kbId: string,
    userId: string,
    data: { role: KbMemberRole; expected_revision: number },
    signal?: AbortSignal
  ) => client.put<KbMemberMutationResult>(memberPath(kbId, userId), data, { signal }),

  remove: (kbId: string, userId: string, expectedRevision: number, signal?: AbortSignal) =>
    client.delete<KbMemberMutationResult>(memberPath(kbId, userId), {
      params: { expected_revision: expectedRevision },
      signal
    })
}