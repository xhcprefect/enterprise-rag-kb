export const ingestRetryMessages = {
  'zh-CN': { documents: {
    ingestRetry: {
      waiting: '等待重试（{seconds} 秒后）',
      waitingSoon: '等待重试（待领取）',
      retrying: '正在重试',
      permanent: '文件问题，已停止重试',
      authentication: '模型认证失败，已停止重试',
      authenticationTip: '请管理员修复模型凭据后重建索引。',
      exhausted: '自动重试次数已耗尽',
      failed: '自动重试已停止',
      failures: '入库已失败 {count} 次',
      waitingTip: '{failures}（{code}），将在 {seconds} 秒后可自动重试，无需重新上传。',
      waitingSoonTip: '{failures}（{code}），等待 worker 领取后自动重试。',
      retryingTip: '{failures}（{code}），worker 正在重试入库。',
      permanentTip: '无法自动恢复（{code}），请修复源文件后重新上传；缺失文件请联系管理员。',
      exhaustedTip: '{failures}（{code}），已停止自动重试；服务恢复后可手工重建索引。',
      failedTip: '入库失败，自动重试已停止；请检查失败原因后重建索引或重新上传。',
      code: {
        source_unavailable: '模型服务不可用',
        timeout: '调用超时',
        network: '网络错误',
        rate_limited: '被限流',
        storage: '存储空间或权限问题',
        database: '数据库临时冲突',
        unsupported_format: '不支持的文件格式',
        empty_content: '无可提取内容',
        corrupt_file: '文件损坏或已加密',
        file_missing: '源文件缺失',
        unknown: '未知错误',
        authentication: '模型服务认证失败'
      }
    }
  } },
  'en-US': { documents: {
    ingestRetry: {
      waiting: 'Retrying in {seconds}s',
      waitingSoon: 'Retry queued',
      retrying: 'Retrying now',
      permanent: 'File issue; retries stopped',
      authentication: 'Model authentication failed; retries stopped',
      authenticationTip: 'Ask an administrator to fix the model service credentials, then rebuild the index; no re-upload needed.',
      exhausted: 'Automatic retries exhausted',
      failed: 'Automatic retry stopped',
      failures: '{count} ingestion failure(s)',
      waitingTip: '{failures} ({code}). Automatic retry is due in {seconds}s; no re-upload needed.',
      waitingSoonTip: '{failures} ({code}). Waiting for a worker to claim the retry.',
      retryingTip: '{failures} ({code}). A worker is retrying ingestion.',
      permanentTip: 'This cannot recover automatically ({code}). Fix the source file and upload again; contact an administrator if it is missing.',
      exhaustedTip: '{failures} ({code}). Automatic retries stopped; rebuild the index manually after the service recovers.',
      failedTip: 'Ingestion failed and automatic retries stopped. Check the failure reason, then rebuild the index or upload again.',
      code: {
        source_unavailable: 'model service unavailable',
        timeout: 'request timed out',
        network: 'network error',
        rate_limited: 'rate limited',
        storage: 'storage space or permission problem',
        database: 'temporary database conflict',
        unsupported_format: 'unsupported file format',
        empty_content: 'no extractable content',
        corrupt_file: 'file is corrupt or encrypted',
        file_missing: 'source file is missing',
        unknown: 'unknown error',
        authentication: 'model service authentication failed'
      }
    }
  } },
}
