import client from './client.ts'

/**
 * 反馈回归在线作业 API（冻结的 R1b 接口）。
 *
 * 该模块只被懒加载的反馈页面引用，避免把作业 DTO/接口实现带进入口 chunk。
 * 所有写请求都不做自动重试：调用方必须在响应丢失时用原 request_id 只读找回。
 */

export type FeedbackRegressionJobStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export type FeedbackRegressionReviewStatus = 'unknown' | 'verified' | 'rejected'
export type FeedbackRegressionExecutionKind = 'manual' | 'online'
export type FeedbackRegressionRequestKind = 'job' | 'batch'

/** 单次作业实际模型调用用量；未知保持 null，绝不补 0。 */
export interface FeedbackRegressionJobUsage {
  attempts: number
  succeeded_attempts: number
  failed_attempts: number
  cancelled_attempts: number
  interrupted_attempts: number
  started_attempts: number
  other_attempts: number
  input_tokens: number | null
  output_tokens: number | null
  cached_input_tokens: number | null
  total_tokens: number | null
  known_input_tokens: number | null
  known_output_tokens: number | null
  usage_known_attempts: number
  usage_missing_attempts: number
  priced_attempts: number
  unpriced_attempts: number
  unfinished_attempts: number
  currency: string | null
  currency_mismatch: boolean
  known_cost_subtotal: string | null
  total_cost: string | null
}

export interface FeedbackRegressionJobBinding {
  root_sha256: string
  case_sha256: string
  dataset_sha256: string
  binding_sha256: string
}

export interface FeedbackRegressionJobResult {
  passed: boolean | null
  failures: string[]
  outcome: string | null
  http_status: number | null
  citation_count: number | null
  result_sha256: string | null
  machine_sha256: string | null
  captured_at: string | null
}

export interface FeedbackRegressionJobReview {
  status: FeedbackRegressionReviewStatus
  notes: string
  reviewed_by_name: string | null
  reviewed_at: string | null
  result_sha256: string | null
}

/** GET /feedback/regression/jobs 等路径的作业白名单元数据（无正文）。 */
export interface FeedbackRegressionJob {
  job_id: string
  feedback_id: string
  regression_id: string
  batch_id: string | null
  run_id: string
  run_attempt_no: number
  status: FeedbackRegressionJobStatus
  revision: number
  attempt_count: number
  execution_kind: 'online'
  requested_mode: 'live'
  can_import: false
  created_at: string
  updated_at: string
  available_at: string
  started_at: string | null
  deadline_at: string | null
  completed_at: string | null
  cancelled_at: string | null
  error_code: string | null
  binding: FeedbackRegressionJobBinding
  result: FeedbackRegressionJobResult | null
  review: FeedbackRegressionJobReview
  usage: FeedbackRegressionJobUsage
}

export interface FeedbackRegressionBatchCounts {
  total: number
  queued: number
  running: number
  succeeded: number
  failed: number
  cancelled: number
}

export interface FeedbackRegressionBatch {
  batch_id: string
  request_id: string
  revision: number
  created_at: string
  updated_at: string
  counts: FeedbackRegressionBatchCounts
  jobs: FeedbackRegressionJob[]
}

export interface FeedbackRegressionRequestLookup {
  request_id: string
  kind: FeedbackRegressionRequestKind
  job: FeedbackRegressionJob | null
  batch: FeedbackRegressionBatch | null
}

export interface FeedbackRegressionComparisonOld {
  message_id: string
  conversation_id: string
  answer: string
  citations: Record<string, unknown>[]
  result_sha256: string
}

export interface FeedbackRegressionComparisonNew {
  answer: string
  citations: Record<string, unknown>[]
  trace: Record<string, unknown>
  passed: boolean
  failures: string[]
  outcome: string | null
  http_status: number | null
  citation_count: number
  result_sha256: string
  machine_sha256: string
}

export interface FeedbackRegressionComparisonRun {
  run_id: string
  attempt_no: number
  execution_kind: 'online'
  requested_mode: 'live'
}

export interface FeedbackRegressionJobComparison {
  job: FeedbackRegressionJob
  run: FeedbackRegressionComparisonRun
  binding: FeedbackRegressionJobBinding
  old: FeedbackRegressionComparisonOld
  new: FeedbackRegressionComparisonNew
}

export interface FeedbackRegressionJobCreateRequest {
  feedback_id: string
  expected_revision: number
  request_id: string
}

export interface FeedbackRegressionBatchItem {
  feedback_id: string
  expected_revision: number
}

export interface FeedbackRegressionBatchCreateRequest {
  request_id: string
  items: FeedbackRegressionBatchItem[]
}

export interface FeedbackRegressionCancelRequest {
  expected_revision: number
}

export interface FeedbackRegressionJobReviewRequest {
  expected_revision: number
  run_id: string
  result_sha256: string
  status: FeedbackRegressionReviewStatus
  notes: string
}

export interface FeedbackRegressionJobListParams {
  feedback_id?: string
  batch_id?: string
  request_id?: string
  page?: number
  page_size?: number
}

interface PageResult<T> {
  total: number
  items: T[]
}

export const feedbackRegressionJobsApi = {
  createJob: (
    payload: FeedbackRegressionJobCreateRequest,
    signal?: AbortSignal
  ) => client.post<FeedbackRegressionJob>('/feedback/regression/jobs', payload, { signal }),
  createBatch: (
    payload: FeedbackRegressionBatchCreateRequest,
    signal?: AbortSignal
  ) => client.post<FeedbackRegressionBatch>('/feedback/regression/batches', payload, { signal }),
  listJobs: (params: FeedbackRegressionJobListParams, signal?: AbortSignal) =>
    client.get<PageResult<FeedbackRegressionJob>>('/feedback/regression/jobs', {
      params,
      signal
    }),
  getJob: (jobId: string, signal?: AbortSignal) =>
    client.get<FeedbackRegressionJob>(`/feedback/regression/jobs/${jobId}`, { signal }),
  comparison: (jobId: string, signal?: AbortSignal) =>
    client.get<FeedbackRegressionJobComparison>(
      `/feedback/regression/jobs/${jobId}/comparison`,
      { signal }
    ),
  cancelJob: (
    jobId: string,
    payload: FeedbackRegressionCancelRequest,
    signal?: AbortSignal
  ) => client.post<FeedbackRegressionJob>(`/feedback/regression/jobs/${jobId}/cancel`, payload, { signal }),
  /** 人工核验是 PUT；响应是递增 revision 后的作业元数据。 */
  reviewJob: (
    jobId: string,
    payload: FeedbackRegressionJobReviewRequest,
    signal?: AbortSignal
  ) => client.put<FeedbackRegressionJob>(`/feedback/regression/jobs/${jobId}/review`, payload, { signal }),
  getBatch: (batchId: string, signal?: AbortSignal) =>
    client.get<FeedbackRegressionBatch>(`/feedback/regression/batches/${batchId}`, { signal }),
  cancelBatch: (
    batchId: string,
    payload: FeedbackRegressionCancelRequest,
    signal?: AbortSignal
  ) => client.post<FeedbackRegressionBatch>(`/feedback/regression/batches/${batchId}/cancel`, payload, { signal }),
  /** 响应丢失后的唯一恢复路径：按原 request_id 只读找回，绝不猜最新作业。 */
  lookupRequest: (requestId: string, signal?: AbortSignal) =>
    client.get<FeedbackRegressionRequestLookup>(
      `/feedback/regression/requests/${requestId}`,
      { signal }
    )
}
