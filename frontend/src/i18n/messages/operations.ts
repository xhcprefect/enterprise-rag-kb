import { useI18n } from 'vue-i18n'
import { i18n } from '../index.ts'

/** 仅随懒加载运维页加载，保留原有完整的双语目录。 */
export const operationsMessages = {
  'zh-CN': {
    operations: {
      eyebrow: 'OPERATIONS',
      title: '运行状态',
      subtitle: '查看入库、摘要、项目索引与回归重跑的独立进程心跳和任务队列。',
      refresh: '刷新运行状态',
      panelTitle: '独立进程与任务队列',
      panelHint: '进程存活基于独立 worker 心跳，不从任务状态推断。',
      generatedAt: '数据生成于 {time}',
      loading: '正在获取最新运行状态…',
      loadFailed: '运行状态获取失败。当前没有显示旧数据，请检查连接后重试。',
      degraded: '部分运行数据暂不可用；未知项不会按零处理。',
      empty: '当前没有可显示的角色状态。',
      securityTitle: '安全提示',
      securityNote: '页面仅显示固定的失败分类与记录更新时间，不展示异常详情、任务正文、文档路径或用户信息。',
      role: '角色',
      roles: {
        ingest: '文档入库',
        summary: '会话摘要',
        project: '项目索引',
        regression: '回归重跑'
      },
      states: {
        disabled: '未启用',
        not_started: '尚未启动',
        running: '运行中',
        stale: '心跳过期',
        stopped: '已停止',
        unknown: '状态未知'
      },
      enabledStatus: '启用状态',
      enabled: '已启用',
      notEnabled: '未启用',
      notEnabledExplanation: '当前 API 配置未启用此角色；仍会显示实际观测到的心跳。',
      aliveCount: '存活进程数',
      heartbeat: '最近心跳',
      pending: '待处理队列',
      processing: '处理中',
      staleTasks: '过期任务',
      oldestPending: '最老等待时间',
      lastFailure: '最近失败',
      failureUpdatedAt: '更新时间：{time}',
      noFailure: '暂无失败记录',
      unknownValue: '未知',
      notRecorded: '未记录',
      failureCodes: {
        taskFailed: '任务处理失败',
        workerUnavailable: '进程不可用',
        generic: '失败（仅显示安全分类）'
      }
    }
  },
  'en-US': {
    operations: {
      eyebrow: 'OPERATIONS',
      title: 'Worker status',
      subtitle: 'View independent process heartbeats and task queues for ingestion, summaries, project indexing, and regression reruns.',
      refresh: 'Refresh worker status',
      panelTitle: 'Independent processes and task queues',
      panelHint: 'Process liveness comes from independent worker heartbeats, not task state.',
      generatedAt: 'Data generated at {time}',
      loading: 'Fetching the latest worker status…',
      loadFailed: 'Could not load worker status. Old data is hidden; check the connection and retry.',
      degraded: 'Some operational data is temporarily unavailable; unknown values are not treated as zero.',
      empty: 'No role status is available to display.',
      securityTitle: 'Security note',
      securityNote: 'Only fixed failure categories and record update times are shown. Exception details, task content, document paths, and user information are not displayed.',
      role: 'Role',
      roles: {
        ingest: 'Document ingestion',
        summary: 'Conversation summaries',
        project: 'Project indexing',
        regression: 'Regression reruns'
      },
      states: {
        disabled: 'Disabled',
        not_started: 'Not started',
        running: 'Running',
        stale: 'Heartbeat stale',
        stopped: 'Stopped',
        unknown: 'Unknown'
      },
      enabledStatus: 'Configuration status',
      enabled: 'Enabled',
      notEnabled: 'Not enabled',
      notEnabledExplanation: 'This role is not enabled by the current API configuration; any observed heartbeat is still shown.',
      aliveCount: 'Live process count',
      heartbeat: 'Latest heartbeat',
      pending: 'Pending tasks',
      processing: 'Processing',
      staleTasks: 'Stale tasks',
      oldestPending: 'Oldest pending age',
      lastFailure: 'Most recent failure',
      failureUpdatedAt: 'Updated: {time}',
      noFailure: 'No failure recorded',
      unknownValue: 'Unknown',
      notRecorded: 'Not recorded',
      failureCodes: {
        taskFailed: 'Task processing failed',
        workerUnavailable: 'Worker unavailable',
        generic: 'Failure (safe category only)'
      }
    }
  }
} as const

let merged = false

/** 在运维页 setup 时合并两种语言，重复进入页面不会重复注册。 */
export function ensureOperationsMessages(): void {
  if (merged) return
  i18n.global.mergeLocaleMessage('zh-CN', operationsMessages['zh-CN'])
  i18n.global.mergeLocaleMessage('en-US', operationsMessages['en-US'])
  merged = true
}

/** 保留完整 key 与全局 locale，静态检查按 operations 命名空间校验懒加载目录。 */
export function useOperationsI18n() {
  ensureOperationsMessages()
  const { t, locale } = useI18n({ useScope: 'global' })
  return { t, locale }
}
