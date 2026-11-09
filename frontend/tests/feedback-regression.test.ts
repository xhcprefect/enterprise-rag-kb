import assert from 'node:assert/strict'
import test from 'node:test'
import type { FeedbackRegressionRun } from '../src/api/index.ts'
import {
  MAX_REGRESSION_NOTE_LENGTH,
  MAX_REPORT_BYTES,
  REGRESSION_CAUSES,
  REGRESSION_MODES,
  buildEnrollPayload,
  buildPreparePayload,
  canImport,
  describeRunOutcome,
  failureLabel,
  isRegressionCause,
  isRegressionMode,
  isTerminalRun,
  newPrepareRequestId,
  regressionCauseKey,
  regressionModeKey,
  runStateKey,
  unavailableReasonKey,
  validateReportFile,
  validationLevelKey
} from '../src/utils/feedbackRegression.ts'

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

function run(overrides: Partial<FeedbackRegressionRun> = {}): FeedbackRegressionRun {
  return {
    run_id: '11111111-1111-4111-8111-111111111111',
    attempt_no: 1,
    is_current: true,
    state: 'awaiting_report',
    requested_mode: 'offline',
    protocol_version: 1,
    case_sha256: 'a'.repeat(64),
    dataset_sha256: 'b'.repeat(64),
    binding_sha256: 'c'.repeat(64),
    prepared_by_name: 'admin',
    created_at: '2026-01-01T00:00:00Z',
    imported_at: null,
    imported_by_name: null,
    report_mode: null,
    validation_level: null,
    passed: null,
    failures: [],
    outcome: null,
    http_status: null,
    citation_count: null,
    dataset_report_sha256: null,
    report_sha256: null,
    result_sha256: null,
    machine_sha256: null,
    ...overrides
  }
}

test('cause/mode constants and guards only accept the frozen enumerations', () => {
  assert.deepEqual([...REGRESSION_CAUSES], [
    'parsing',
    'retrieval',
    'document_version',
    'generation',
    'scope_context',
    'other'
  ])
  assert.deepEqual([...REGRESSION_MODES], ['offline', 'live'])

  for (const cause of REGRESSION_CAUSES) assert.equal(isRegressionCause(cause), true)
  assert.equal(isRegressionCause('bogus'), false)
  assert.equal(isRegressionCause(''), false)
  assert.equal(isRegressionCause(null), false)
  assert.equal(isRegressionCause(42), false)

  for (const mode of REGRESSION_MODES) assert.equal(isRegressionMode(mode), true)
  assert.equal(isRegressionMode('replay'), false)
  assert.equal(isRegressionMode(undefined), false)
})

test('buildEnrollPayload trims the note and rejects empty, overlong and control-character notes', () => {
  const valid = buildEnrollPayload(4, 'retrieval', '  检索未命中正确分块  ')
  assert.deepEqual(valid, {
    payload: {
      expected_feedback_revision: 4,
      cause: 'retrieval',
      cause_note: '检索未命中正确分块'
    },
    errorKey: null
  })
  assert.deepEqual(Object.keys(valid.payload as object).sort(), [
    'cause',
    'cause_note',
    'expected_feedback_revision'
  ])

  const multiline = buildEnrollPayload(1, 'other', '第一行\n第二行\t结束')
  assert.equal(multiline.errorKey, null)
  assert.equal(multiline.payload?.cause_note, '第一行\n第二行\t结束')

  assert.equal(
    buildEnrollPayload(1, 'other', '   ').errorKey,
    'feedback.regression.errors.noteRequired'
  )
  assert.equal(
    buildEnrollPayload(1, 'other', 'x'.repeat(MAX_REGRESSION_NOTE_LENGTH + 1)).errorKey,
    'feedback.regression.errors.noteTooLong'
  )
  assert.equal(
    buildEnrollPayload(1, 'other', `  ${'x'.repeat(MAX_REGRESSION_NOTE_LENGTH)}  `).errorKey,
    null
  )
  assert.equal(
    buildEnrollPayload(1, 'other', 'a\u0000b').errorKey,
    'feedback.regression.errors.noteControlChars'
  )
  assert.equal(
    buildEnrollPayload(1, 'other', 'a\rb').errorKey,
    'feedback.regression.errors.noteControlChars'
  )
  assert.equal(
    buildEnrollPayload(1, 'other', 'a\u0085b').errorKey,
    'feedback.regression.errors.noteControlChars'
  )
  assert.equal(
    buildEnrollPayload(1, 'bogus', 'note').errorKey,
    'feedback.regression.errors.causeRequired'
  )
  assert.equal(
    buildEnrollPayload(0, 'parsing', 'note').errorKey,
    'feedback.regression.errors.revisionInvalid'
  )
})

test('newPrepareRequestId emits unique RFC 4122 v4 identifiers', () => {
  const seen = new Set<string>()
  for (let index = 0; index < 50; index += 1) {
    const value = newPrepareRequestId()
    assert.match(value, UUID_V4)
    seen.add(value)
  }
  assert.equal(seen.size, 50)
})

test('buildPreparePayload validates revision/mode and keeps a caller-provided idempotency key', () => {
  const generated = buildPreparePayload(3, 'offline')
  assert.equal(generated.errorKey, null)
  assert.equal(generated.payload?.expected_revision, 3)
  assert.equal(generated.payload?.requested_mode, 'offline')
  assert.match(generated.payload?.prepare_request_id ?? '', UUID_V4)

  const fixed = '00000000-0000-4000-8000-000000000000'
  const explicit = buildPreparePayload(7, 'live', fixed)
  assert.deepEqual(explicit, {
    payload: {
      expected_revision: 7,
      requested_mode: 'live',
      prepare_request_id: fixed
    },
    errorKey: null
  })

  assert.equal(
    buildPreparePayload(3, 'replay').errorKey,
    'feedback.regression.errors.modeRequired'
  )
  assert.equal(
    buildPreparePayload(0, 'offline').errorKey,
    'feedback.regression.errors.revisionInvalid'
  )
  assert.equal(
    buildPreparePayload(3, 'offline', 'not-a-uuid').errorKey,
    'feedback.regression.errors.requestInvalid'
  )
})

test('validateReportFile enforces .json extension and the 2 MiB ceiling', () => {
  assert.equal(validateReportFile({ name: 'report.json', size: 1024, type: 'application/json' }), null)
  assert.equal(validateReportFile({ name: 'REPORT.JSON', size: MAX_REPORT_BYTES }), null)
  assert.equal(
    validateReportFile({ name: 'report.json', size: MAX_REPORT_BYTES + 1 }),
    'feedback.regression.errors.fileTooLarge'
  )
  assert.equal(
    validateReportFile({ name: 'report.jsonl', size: 10 }),
    'feedback.regression.errors.fileExtension'
  )
  assert.equal(
    validateReportFile({ name: 'report', size: 10 }),
    'feedback.regression.errors.fileExtension'
  )
  assert.equal(
    validateReportFile({ name: 'report.json.txt', size: 10 }),
    'feedback.regression.errors.fileExtension'
  )
  assert.equal(validateReportFile({ name: '', size: 10 }), 'feedback.regression.errors.fileInvalid')
  assert.equal(
    validateReportFile({ name: 'report.json', size: Number.NaN }),
    'feedback.regression.errors.fileInvalid'
  )
  assert.equal(validateReportFile(null), 'feedback.regression.errors.fileInvalid')
})

test('state, validation, outcome and unavailable keys map to stable i18n namespaces', () => {
  assert.equal(regressionCauseKey('document_version'), 'feedback.regression.causes.document_version')
  assert.equal(regressionModeKey('live'), 'feedback.regression.modes.live')
  assert.equal(runStateKey('awaiting_report'), 'feedback.regression.state.awaiting_report')
  assert.equal(runStateKey('stale'), 'feedback.regression.state.stale')
  assert.equal(
    validationLevelKey('content_regraded'),
    'feedback.regression.validation.content_regraded'
  )
  assert.equal(validationLevelKey(null), '')
  assert.equal(validationLevelKey(undefined), '')

  assert.equal(
    unavailableReasonKey('sample_changed'),
    'feedback.regression.unavailable.sample_changed'
  )
  assert.equal(
    unavailableReasonKey('brand_new_code'),
    'feedback.regression.unavailable.unknown'
  )
  assert.equal(
    unavailableReasonKey(null),
    'feedback.regression.unavailable.unknown'
  )

  assert.equal(
    describeRunOutcome(run()),
    'feedback.regression.outcome.awaitingReport'
  )
  assert.equal(
    describeRunOutcome(run({ state: 'stale', passed: true })),
    'feedback.regression.outcome.stale'
  )
  assert.equal(
    describeRunOutcome(run({ state: 'imported', passed: true, validation_level: 'consistency_only' })),
    'feedback.regression.outcome.consistencyOnly'
  )
  assert.equal(
    describeRunOutcome(run({ state: 'imported', passed: true, validation_level: 'content_regraded' })),
    'feedback.regression.outcome.contentRegraded'
  )
  assert.equal(
    describeRunOutcome(run({ state: 'imported', passed: false, validation_level: 'consistency_only' })),
    'feedback.regression.outcome.failed'
  )
  assert.equal(
    describeRunOutcome(run({ state: 'imported', passed: null, validation_level: null })),
    'feedback.regression.outcome.offlineReplay'
  )
  assert.equal(
    describeRunOutcome(run({
      state: 'imported',
      passed: null,
      validation_level: null,
      requested_mode: 'live'
    })),
    'feedback.regression.outcome.imported'
  )
})

test('canImport only allows the current awaiting_report attempt', () => {
  assert.equal(canImport(run()), true)
  assert.equal(canImport(run({ is_current: false })), false)
  assert.equal(canImport(run({ state: 'imported' })), false)
  assert.equal(canImport(run({ state: 'stale' })), false)
  assert.equal(canImport(null), false)
  assert.equal(canImport(undefined), false)
})

test('isTerminalRun treats imported and stale attempts as final', () => {
  assert.equal(isTerminalRun(run()), false)
  assert.equal(isTerminalRun(run({ state: 'imported' })), true)
  assert.equal(isTerminalRun(run({ state: 'stale' })), true)
  assert.equal(isTerminalRun(null), false)
})

test('failureLabel returns the stable machine code verbatim', () => {
  for (const code of [
    'expected_sources_mismatch',
    'outcome_mismatch',
    'forbidden_phrase_present',
    'some_new_code'
  ]) {
    assert.equal(failureLabel(code), code)
  }
  assert.notEqual(failureLabel('outcome_mismatch'), '结果不一致')
})
