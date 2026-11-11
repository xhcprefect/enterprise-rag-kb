import assert from 'node:assert/strict'
import test from 'node:test'
import type { FeedbackRegressionJobUsage } from '../src/api/feedbackRegressionJobs.ts'
import {
  MAX_BATCH_ITEMS,
  MAX_PENDING_REQUESTS,
  MAX_REVIEW_NOTES,
  PENDING_TTL_MS,
  buildBatchCreatePayload,
  buildCancelRequest,
  buildJobCreatePayload,
  buildReviewPayload,
  extractReasonCode,
  formatCostAmount,
  isActiveJobStatus,
  isAmbiguousWriteFailure,
  isCancelableJob,
  isJobStatus,
  isReviewStatus,
  isReviewableJob,
  isTerminalJobStatus,
  isUuid,
  jobOutcomeKey,
  jobOutcomeTone,
  jobStatusKey,
  loadPendingRequests,
  pendingStorageKey,
  removePendingRequest,
  reviewNotesHaveControlChars,
  reviewNotesTooLong,
  reviewStatusKey,
  savePendingRequest,
  summarizeUsage,
  type PendingRegressionRequest,
  type RecoveryStorage
} from '../src/utils/feedbackRegressionJobs.ts'
import {
  feedbackJobsHasReason,
  feedbackRegressionJobsMessages
} from '../src/i18n/messages/feedbackRegressionJobs.ts'
import { messages } from '../src/i18n/messages/index.ts'

const FEEDBACK_A = '11111111-1111-4111-8111-111111111111'
const FEEDBACK_B = '22222222-2222-4222-8222-222222222222'
const REQUEST_ID = '33333333-3333-4333-8333-333333333333'
const RUN_ID = '44444444-4444-4444-8444-444444444444'
const RESULT_SHA = 'a'.repeat(64)

function usage(overrides: Partial<FeedbackRegressionJobUsage> = {}): FeedbackRegressionJobUsage {
  return {
    attempts: 0,
    succeeded_attempts: 0,
    failed_attempts: 0,
    cancelled_attempts: 0,
    interrupted_attempts: 0,
    started_attempts: 0,
    other_attempts: 0,
    input_tokens: null,
    output_tokens: null,
    cached_input_tokens: null,
    total_tokens: null,
    known_input_tokens: null,
    known_output_tokens: null,
    usage_known_attempts: 0,
    usage_missing_attempts: 0,
    priced_attempts: 0,
    unpriced_attempts: 0,
    unfinished_attempts: 0,
    currency: null,
    currency_mismatch: false,
    known_cost_subtotal: null,
    total_cost: null,
    ...overrides
  }
}

function pending(
  requestId: string = REQUEST_ID,
  overrides: Partial<PendingRegressionRequest> = {}
): PendingRegressionRequest {
  return {
    request_id: requestId,
    kind: 'job',
    feedback_ids: [FEEDBACK_A],
    created_at: 1000,
    ...overrides
  }
}

class MemoryStorage implements RecoveryStorage {
  readonly values = new Map<string, string>()
  failReads = false

  getItem(key: string): string | null {
    if (this.failReads) throw new Error('storage disabled')
    return this.values.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }

  removeItem(key: string): void {
    this.values.delete(key)
  }
}

function flatten(value: Record<string, unknown>, prefix = ''): Record<string, string> {
  const entries: Record<string, string> = {}
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (typeof item === 'string') entries[path] = item
    else if (item && typeof item === 'object' && !Array.isArray(item)) {
      Object.assign(entries, flatten(item as Record<string, unknown>, path))
    } else {
      assert.fail(`Unexpected message value at ${path}`)
    }
  }
  return entries
}

test('lazy job catalog keeps zh/en parity and never enters the global entry catalog', () => {
  const zh = flatten(feedbackRegressionJobsMessages['zh-CN'])
  const en = flatten(feedbackRegressionJobsMessages['en-US'])
  assert.deepEqual(Object.keys(zh).sort(), Object.keys(en).sort())
  for (const [key, value] of [...Object.entries(zh), ...Object.entries(en)]) {
    assert.ok(value.trim(), `Empty translation: ${key}`)
  }
  assert.ok(Object.keys(zh).some(key => key.startsWith('feedbackJobs.')))
  const globalZh = flatten(messages['zh-CN'] as Record<string, unknown>)
  const globalEn = flatten(messages['en-US'] as Record<string, unknown>)
  assert.equal(globalZh['feedbackJobs.title'], undefined)
  assert.equal(globalEn['feedbackJobs.title'], undefined)
  assert.equal(feedbackRegressionJobsMessages['zh-CN'].feedbackJobs.batch.selectAria.includes('{id}'), true)
})

test('status guards and outcome tones keep execution failure distinct from failed assertions', () => {
  assert.equal(isJobStatus('queued'), true)
  assert.equal(isJobStatus('done'), false)
  assert.equal(isReviewStatus('unknown'), true)
  assert.equal(isReviewStatus('passed'), false)
  assert.equal(isActiveJobStatus('running'), true)
  assert.equal(isTerminalJobStatus('cancelled'), true)
  assert.equal(isTerminalJobStatus('running'), false)
  assert.equal(isCancelableJob({ status: 'queued' }), true)
  assert.equal(isCancelableJob({ status: 'succeeded' }), false)
  assert.equal(jobStatusKey('failed'), 'status.failed')
  assert.equal(reviewStatusKey('verified'), 'reviewStatus.verified')

  assert.equal(jobOutcomeTone({ status: 'succeeded', result: null }), 'succeededUnknown')
  assert.equal(
    jobOutcomeTone({ status: 'succeeded', result: { passed: true } as never }),
    'succeededPassed'
  )
  assert.equal(
    jobOutcomeTone({ status: 'succeeded', result: { passed: false } as never }),
    'succeededAssertionsFailed'
  )
  assert.equal(jobOutcomeTone({ status: 'failed', result: null }), 'executionFailed')
  assert.equal(jobOutcomeTone({ status: 'cancelled', result: null }), 'cancelled')
  assert.equal(jobOutcomeKey({ status: 'failed', result: null }), 'outcome.executionFailed')
  assert.equal(
    jobOutcomeKey({ status: 'succeeded', result: { passed: false } as never }),
    'outcome.succeededAssertionsFailed'
  )
  assert.equal(isReviewableJob({ status: 'succeeded', result: { result_sha256: RESULT_SHA } as never }), true)
  assert.equal(isReviewableJob({ status: 'failed', result: { result_sha256: RESULT_SHA } as never }), false)
  assert.equal(isReviewableJob({ status: 'succeeded', result: { result_sha256: null } as never }), false)
})

test('create, batch, and cancel payload builders reject invalid or unbounded input', () => {
  const created = buildJobCreatePayload(FEEDBACK_A, 3, REQUEST_ID)
  assert.deepEqual(created, {
    payload: { feedback_id: FEEDBACK_A, expected_revision: 3, request_id: REQUEST_ID },
    errorKey: null
  })
  assert.equal(buildJobCreatePayload('bad', 3, REQUEST_ID).errorKey, 'errors.invalidFeedback')
  assert.equal(buildJobCreatePayload(FEEDBACK_A, 0, REQUEST_ID).errorKey, 'errors.invalidRevision')
  assert.equal(buildJobCreatePayload(FEEDBACK_A, 3, 'bad').errorKey, 'errors.invalidRequestId')

  const batch = buildBatchCreatePayload(
    [
      { feedback_id: FEEDBACK_A, expected_revision: 1 },
      { feedback_id: FEEDBACK_B, expected_revision: 2 }
    ],
    REQUEST_ID
  )
  assert.deepEqual(batch.payload, {
    request_id: REQUEST_ID,
    items: [
      { feedback_id: FEEDBACK_A, expected_revision: 1 },
      { feedback_id: FEEDBACK_B, expected_revision: 2 }
    ]
  })
  assert.equal(buildBatchCreatePayload([], REQUEST_ID).errorKey, 'errors.batchEmpty')
  assert.equal(
    buildBatchCreatePayload([{ feedback_id: FEEDBACK_A, expected_revision: 1 }, { feedback_id: FEEDBACK_A, expected_revision: 1 }], REQUEST_ID).errorKey,
    'errors.batchDuplicate'
  )
  const tooMany = Array.from({ length: MAX_BATCH_ITEMS + 1 }, (_, index) => ({
    feedback_id: `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`,
    expected_revision: 1
  }))
  assert.equal(buildBatchCreatePayload(tooMany, REQUEST_ID).errorKey, 'errors.batchTooLarge')
  assert.equal(
    buildBatchCreatePayload([{ feedback_id: FEEDBACK_A, expected_revision: 0 }], REQUEST_ID).errorKey,
    'errors.batchInvalidItem'
  )

  assert.deepEqual(buildCancelRequest(4), { payload: { expected_revision: 4 }, errorKey: null })
  assert.equal(buildCancelRequest(1.5).errorKey, 'errors.invalidRevision')
})

test('review payload binds the current job revision, run and result fingerprint', () => {
  const job = {
    revision: 7,
    run_id: RUN_ID,
    result: { result_sha256: RESULT_SHA }
  }
  assert.deepEqual(buildReviewPayload(job, 'verified', '  checked  '), {
    payload: {
      expected_revision: 7,
      run_id: RUN_ID,
      result_sha256: RESULT_SHA,
      status: 'verified',
      notes: 'checked'
    },
    errorKey: null
  })
  assert.equal(buildReviewPayload(job, 'unknown', '').payload?.status, 'unknown')
  assert.equal(buildReviewPayload({ ...job, revision: 0 }, 'verified', '').errorKey, 'errors.invalidRevision')
  assert.equal(buildReviewPayload({ ...job, run_id: 'bad' }, 'verified', '').errorKey, 'errors.invalidRun')
  assert.equal(buildReviewPayload({ ...job, result: null }, 'verified', '').errorKey, 'errors.resultUnavailable')
  assert.equal(buildReviewPayload({ ...job, result: { result_sha256: 'x' as never } }, 'verified', '').errorKey, 'errors.resultUnavailable')
  assert.equal(buildReviewPayload(job, 'accepted', '').errorKey, 'errors.invalidReviewStatus')
  assert.equal(reviewNotesTooLong('a'.repeat(MAX_REVIEW_NOTES)), false)
  assert.equal(reviewNotesTooLong('a'.repeat(MAX_REVIEW_NOTES + 1)), true)
  assert.equal(reviewNotesHaveControlChars('ok\n\tok'), false)
  assert.equal(reviewNotesHaveControlChars('bad\rline'), true)
  assert.equal(buildReviewPayload(job, 'verified', 'a'.repeat(MAX_REVIEW_NOTES + 1)).errorKey, 'errors.notesTooLong')
  assert.equal(buildReviewPayload(job, 'verified', 'bad\u0000').errorKey, 'errors.notesControlChars')
})

test('usage summary preserves unknown totals instead of fabricating zero', () => {
  const measured = summarizeUsage(usage({
    attempts: 2,
    succeeded_attempts: 2,
    input_tokens: 100,
    output_tokens: 50,
    total_tokens: 150,
    cached_input_tokens: 10,
    known_input_tokens: 100,
    known_output_tokens: 50,
    usage_known_attempts: 2,
    usage_missing_attempts: 0,
    priced_attempts: 2,
    currency: 'USD',
    known_cost_subtotal: '0.012300',
    total_cost: '0.012300'
  }))
  assert.equal(measured.tokensMeasured, true)
  assert.equal(measured.totalTokens, 150)
  assert.equal(measured.costComplete, true)
  assert.equal(measured.totalCost, '0.012300')

  const partial = summarizeUsage(usage({
    attempts: 2,
    succeeded_attempts: 2,
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    known_input_tokens: 40,
    known_output_tokens: 20,
    usage_known_attempts: 1,
    usage_missing_attempts: 1,
    priced_attempts: 1,
    unpriced_attempts: 1,
    currency_mismatch: true,
    known_cost_subtotal: '0.010',
    total_cost: null
  }))
  assert.equal(partial.tokensMeasured, false)
  assert.equal(partial.totalTokens, null)
  assert.equal(partial.knownInputTokens, 40)
  assert.equal(partial.knownOutputTokens, 20)
  assert.equal(partial.missingAttempts, 1)
  assert.equal(partial.costComplete, false)
  assert.equal(partial.totalCost, null)
  assert.equal(partial.knownCostSubtotal, '0.010')
  assert.equal(partial.currencyMismatch, true)

  const empty = summarizeUsage(usage())
  assert.equal(empty.hasCalls, false)
  assert.equal(empty.tokensMeasured, false)
  assert.equal(empty.totalTokens, null)

  const malformed = summarizeUsage(usage({
    attempts: 1,
    input_tokens: 10,
    output_tokens: null,
    total_tokens: 10,
    usage_missing_attempts: 0
  }))
  assert.equal(malformed.tokensMeasured, false)
  assert.equal(malformed.outputTokens, null)
})

test('cost formatting trims decimal strings without float conversion', () => {
  assert.equal(formatCostAmount('0.0100'), '0.01')
  assert.equal(formatCostAmount('001.20'), '1.2')
  assert.equal(formatCostAmount('1.000'), '1')
  assert.equal(formatCostAmount('-0.000'), '0')
  assert.equal(formatCostAmount('-1.500'), '-1.5')
  assert.equal(formatCostAmount('0'), '0')
  assert.equal(formatCostAmount(''), '')
  assert.equal(formatCostAmount('1e-7'), '')
  assert.equal(formatCostAmount(null), '')
})

test('ambiguous write detection only covers lost responses, not explicit 4xx or user aborts', () => {
  assert.equal(isAmbiguousWriteFailure({ code: 'ERR_NETWORK' }), true)
  assert.equal(isAmbiguousWriteFailure({ code: 'ECONNABORTED' }), true)
  assert.equal(isAmbiguousWriteFailure({ code: 'ETIMEDOUT' }), true)
  assert.equal(isAmbiguousWriteFailure({ response: { status: 503 } }), true)
  assert.equal(isAmbiguousWriteFailure({ response: { status: 409 } }), false)
  assert.equal(isAmbiguousWriteFailure({ code: 'ERR_CANCELED', response: { status: 500 } }), false)
  assert.equal(isAmbiguousWriteFailure(null), false)
  assert.equal(isAmbiguousWriteFailure({ message: 'plain' }), false)
})

test('stable reason extraction reads only the trailing parenthesized code', () => {
  assert.equal(extractReasonCode('冲突（revision_conflict）'), 'revision_conflict')
  assert.equal(extractReasonCode('Conflict (job_active)'), 'job_active')
  assert.equal(extractReasonCode('something else'), '')
  assert.equal(extractReasonCode('bad（Revision-Conflict）'), '')
  assert.equal(extractReasonCode(''), '')
  assert.equal(feedbackJobsHasReason('revision_conflict'), true)
  assert.equal(feedbackJobsHasReason('not_a_reason'), false)
})

test('recovery metadata is origin/admin scoped, bounded, TTL filtered, and body-free', () => {
  const storage = new MemoryStorage()
  const now = PENDING_TTL_MS + 10_000_000
  savePendingRequest(storage, 'https://a.example', 'admin-a', pending(REQUEST_ID, { created_at: now }), now)
  assert.equal(loadPendingRequests(storage, 'https://a.example', 'admin-a', now).length, 1)
  assert.equal(loadPendingRequests(storage, 'https://b.example', 'admin-a', now).length, 0)
  assert.equal(loadPendingRequests(storage, 'https://a.example', 'admin-b', now).length, 0)

  const key = pendingStorageKey('https://a.example', 'admin-a')
  const raw = storage.getItem(key) || ''
  assert.ok(raw.includes(REQUEST_ID))
  assert.ok(!raw.includes('answer'))
  assert.ok(!raw.includes('question'))

  const withExtra = {
    ...pending('55555555-5555-4555-8555-555555555555', {
      created_at: now,
      feedback_ids: [FEEDBACK_A, FEEDBACK_A, 'bad', FEEDBACK_B]
    }),
    answer: 'must not persist',
    question: 'must not persist'
  } as unknown as PendingRegressionRequest
  savePendingRequest(storage, 'https://a.example', 'admin-a', withExtra, now)
  const stored = loadPendingRequests(storage, 'https://a.example', 'admin-a', now)
    .find(entry => entry.request_id === '55555555-5555-4555-8555-555555555555')
  assert.deepEqual(stored?.feedback_ids, [FEEDBACK_A, FEEDBACK_B])
  assert.ok(!(storage.getItem(key) || '').includes('must not persist'))

  const expired: PendingRegressionRequest = {
    ...pending('66666666-6666-4666-8666-666666666666'),
    created_at: now - PENDING_TTL_MS - 1
  }
  savePendingRequest(storage, 'https://a.example', 'admin-a', expired, now)
  assert.equal(
    loadPendingRequests(storage, 'https://a.example', 'admin-a', now)
      .some(entry => entry.request_id === expired.request_id),
    false
  )

  const bulk = new MemoryStorage()
  for (let index = 0; index < MAX_PENDING_REQUESTS + 5; index += 1) {
    const id = `77777777-7777-4777-8777-${String(index).padStart(12, '0')}`
    savePendingRequest(bulk, 'o', 'a', pending(id, { created_at: now + index }), now + index)
  }
  const loaded = loadPendingRequests(bulk, 'o', 'a', now + MAX_PENDING_REQUESTS + 5)
  assert.equal(loaded.length, MAX_PENDING_REQUESTS)
  assert.equal(loaded[0].created_at, now + MAX_PENDING_REQUESTS + 4)

  removePendingRequest(bulk, 'o', 'a', loaded[0].request_id, now + MAX_PENDING_REQUESTS + 5)
  assert.equal(
    loadPendingRequests(bulk, 'o', 'a', now + MAX_PENDING_REQUESTS + 5)
      .some(entry => entry.request_id === loaded[0].request_id),
    false
  )

  const disabled = new MemoryStorage()
  disabled.failReads = true
  assert.deepEqual(loadPendingRequests(disabled, 'o', 'a', now), [])
  assert.equal(isUuid(REQUEST_ID), true)
  assert.equal(isUuid('not-a-uuid'), false)
})