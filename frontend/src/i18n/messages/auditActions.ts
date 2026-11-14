import { useI18n } from 'vue-i18n'
import { i18n } from '../../i18n/index.ts'

/** 仅由懒加载审计页引用，新增事件文案不进入全局入口目录。 */
export const auditActionsMessages = {
  'zh-CN': {
    auditActions: {
      kbMemberUpdate: '更新知识库成员',
      kbMemberRemove: '移除知识库成员',
      feedbackRegressionJobCreate: '创建在线回归作业',
      feedbackRegressionBatchCreate: '创建在线回归批次',
      feedbackRegressionJobCancel: '取消在线回归作业',
      feedbackRegressionJobReview: '核验在线回归结果',
      feedbackRegressionBatchCancel: '取消在线回归批次'
    }
  },
  'en-US': {
    auditActions: {
      kbMemberUpdate: 'Knowledge base member updated',
      kbMemberRemove: 'Knowledge base member removed',
      feedbackRegressionJobCreate: 'Online regression job created',
      feedbackRegressionBatchCreate: 'Online regression batch created',
      feedbackRegressionJobCancel: 'Online regression job cancelled',
      feedbackRegressionJobReview: 'Online regression job reviewed',
      feedbackRegressionBatchCancel: 'Online regression batch cancelled'
    }
  }
} as const

let merged = false

export function ensureAuditActionsMessages(): void {
  if (merged) return
  i18n.global.mergeLocaleMessage('zh-CN', auditActionsMessages['zh-CN'])
  i18n.global.mergeLocaleMessage('en-US', auditActionsMessages['en-US'])
  merged = true
}

/** 沿用反馈页的局部翻译入口，懒加载键不属于全局静态目录。 */
export function useAuditActionsI18n() {
  ensureAuditActionsMessages()
  const { t } = useI18n()
  function aa(key: string): string {
    return t(`auditActions.${key}`)
  }
  return { aa }
}
