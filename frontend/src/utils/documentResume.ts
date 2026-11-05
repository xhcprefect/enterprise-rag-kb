/**
 * 未完成上传（续传）记录的持久化规则。
 *
 * 记录存在 localStorage 里、按知识库分键，因此同一 KB 的前后两个页面实例
 * 会共用同一条记录。组件卸载后，上一实例仍在途的 createSession / complete
 * 回执可能迟到：它们只能处置"自己那个 upload_id"的记录，绝不能清掉或覆盖
 * 用户重新进入后新上传写入的记录。仅靠组件内的 generation 计数器不够。
 */

export interface ResumeRecord {
  uploadId: string
  filename: string
  fileSize: number
  percent: number
}

/** 最小存储接口，便于用内存实现做 Node 测试。 */
export interface ResumeStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export const resumeKey = (kbId: string) => `rag_upload_resume_${kbId}`

/** 读取持久记录；内容损坏时视为不存在（调用方负责清理）。 */
export function readResume(storage: ResumeStorage, kbId: string): ResumeRecord | null {
  const raw = storage.getItem(resumeKey(kbId))
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Partial<ResumeRecord> | null
    if (!value || typeof value.uploadId !== 'string' || !value.uploadId) return null
    return {
      uploadId: value.uploadId,
      filename: typeof value.filename === 'string' ? value.filename : '',
      fileSize: typeof value.fileSize === 'number' ? value.fileSize : 0,
      percent: typeof value.percent === 'number' ? value.percent : 0
    }
  } catch {
    return null
  }
}

/**
 * 写入续传记录。
 *
 * `ownerUploadId` 是本次回执所属的会话；只有它和要写的记录一致、且组件
 * 仍在运行时才写。卸载后的迟到回执一律丢弃，既不覆盖界面也不写存储。
 */
export function writeResume(
  storage: ResumeStorage,
  kbId: string,
  record: ResumeRecord,
  options: { disposed: boolean; ownerUploadId?: string }
): boolean {
  if (options.disposed) return false
  if (options.ownerUploadId !== undefined && record.uploadId !== options.ownerUploadId) return false
  // 说明：仍然在运行的实例切换会话（刷新后重新上传同一文件拿到新会话）时
  // 允许覆盖旧记录；需要防的是"已卸载实例的迟到回执"，由 disposed 拦下。
  storage.setItem(resumeKey(kbId), JSON.stringify(record))
  return true
}

/**
 * 清除续传记录：只清"当前持久记录就是 uploadId 这条"的情况。
 *
 * 另一个页面实例可能已经为同一 KB 写入了新会话，旧实例的迟到 complete /
 * 用户放弃旧会话都不能把它删掉。因此 uploadId 必传，不存在无条件删除路径。
 *
 * 返回值表示是否真的移除了记录。
 */
export function clearResumeRecord(
  storage: ResumeStorage,
  kbId: string,
  uploadId: string,
  options: { disposed?: boolean } = {}
): boolean {
  if (options.disposed) return false
  const stored = readResume(storage, kbId)
  // 记录已经不存在：无需处理；记录属于别的会话：绝不能碰
  if (!stored || stored.uploadId !== uploadId) return false
  storage.removeItem(resumeKey(kbId))
  return true
}

/**
 * 刷新后恢复的迟到响应是否还能生效。
 *
 * 卸载、已有新任务、或持久记录已被另一个会话取代时都不应覆盖界面状态。
 */
export function canApplyRestoredResume(
  saved: ResumeRecord,
  stored: ResumeRecord | null,
  hasCurrent: boolean,
  disposed: boolean
): boolean {
  if (disposed) return false
  if (hasCurrent) return false
  if (!stored || stored.uploadId !== saved.uploadId) return false
  return true
}

/** 恢复会话的失败是否已确证"该会话不再可用"（只有这时才该清记录）。 */
export function isResumeGoneStatus(status: number | null | undefined): boolean {
  return status === 404 || status === 410
}

/**
 * 恢复请求失败后是否应清除记录。
 *
 * 只有确证会话已不存在（404/410）且记录仍属于这次恢复的会话、页面未卸载、
 * 用户也没开始新任务时才清。断网、超时、5xx 一律保留记录，等下次刷新再试。
 */
export function shouldClearOnResumeFailure(
  saved: ResumeRecord,
  stored: ResumeRecord | null,
  hasCurrent: boolean,
  disposed: boolean,
  status: number | null | undefined
): boolean {
  if (!isResumeGoneStatus(status)) return false
  return canApplyRestoredResume(saved, stored, hasCurrent, disposed)
}

/** 内存存储，供测试与 SSR 兜底使用。 */
export function memoryResumeStorage(initial: Record<string, string> = {}): ResumeStorage {
  const map = new Map(Object.entries(initial))
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => {
      map.set(key, value)
    },
    removeItem: (key) => {
      map.delete(key)
    }
  }
}
