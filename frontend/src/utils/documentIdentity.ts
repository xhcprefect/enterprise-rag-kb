/**
 * 文档身份相关的纯逻辑：复用判定与续传文件匹配。
 *
 * 服务端以真实内容摘要决定是否复用已有文档；前端只负责把结果翻译成
 * 用户能理解的提示。复用一份入库失败/取消的旧文档时，状态被原样保留，
 * 前端必须提示用户显式"重建索引"，不能假装成功、也不能静默重试。
 */

export type ReuseOutcome = 'created' | 'reused' | 'reused-failed' | 'reused-cancelled'

/** 是否复用了已有文档，以及复用记录当前处于什么状态。 */
export function reuseOutcome(deduplicated: boolean, status: string): ReuseOutcome {
  if (!deduplicated) return 'created'
  if (status === 'failed') return 'reused-failed'
  if (status === 'cancelled') return 'reused-cancelled'
  return 'reused'
}

/** 复用失败/取消的旧文档时，必须由用户显式重建索引。 */
export function needsExplicitReindex(outcome: ReuseOutcome): boolean {
  return outcome === 'reused-failed' || outcome === 'reused-cancelled'
}

/** 上传完成后应提示的 i18n key；未复用不提示。 */
export function reuseMessageKey(outcome: ReuseOutcome): string | null {
  switch (outcome) {
    case 'reused':
      return 'documents.toast.reused'
    case 'reused-failed':
      return 'documents.toast.reusedFailed'
    case 'reused-cancelled':
      return 'documents.toast.reusedCancelled'
    default:
      return null
  }
}

export interface ResumeIdentity {
  filename: string
  fileSize: number
}

export interface FileIdentity {
  name: string
  size: number
}

/**
 * 未完成会话只接受同名同大小的文件；内容摘要仍由服务端最终校验。
 */
export function matchesResume(file: FileIdentity, resume: ResumeIdentity | null | undefined): boolean {
  return !!resume && file.name === resume.filename && file.size === resume.fileSize
}

/**
 * 从一次多选里挑出能续传的文件：匹配项优先，其余视为不匹配。
 * 没有未完成会话时全部接受。
 */
export function splitResumeMatches<T extends FileIdentity>(
  files: T[],
  resume: ResumeIdentity | null | undefined
): { matched: T[]; mismatched: T[] } {
  if (!resume) return { matched: [...files], mismatched: [] }
  const matched: T[] = []
  const mismatched: T[] = []
  for (const file of files) {
    if (matchesResume(file, resume)) matched.push(file)
    else mismatched.push(file)
  }
  return { matched, mismatched }
}

/**
 * 选择文件时是否用未完成会话限制本次选择。
 *
 * 只有"刷新后恢复的未完成会话、且当前没有任何任务"才需要限制：此时
 * 用户是来续传那个会话的。上传过程中 resumeInfo 也指向当前会话，若同样
 * 限制，用户就无法把后续文件追加进等待队列。
 */
export function shouldRestrictToResume(
  resume: ResumeIdentity | null | undefined,
  hasCurrent: boolean
): boolean {
  return !!resume && !hasCurrent
}

/** 按是否续传场景给出本次选择可接受的文件。 */
export function selectPickedFiles<T extends FileIdentity>(
  files: T[],
  resume: ResumeIdentity | null | undefined,
  hasCurrent: boolean
): { accepted: T[]; mismatched: T[] } {
  if (!shouldRestrictToResume(resume, hasCurrent)) {
    return { accepted: [...files], mismatched: [] }
  }
  const { matched, mismatched } = splitResumeMatches(files, resume)
  return { accepted: matched, mismatched }
}
