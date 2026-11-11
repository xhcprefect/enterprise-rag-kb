import assert from 'node:assert/strict'
import test from 'node:test'
import type { WorkerState } from '../src/api/operations.ts'
import {
  createRequestGate, failureLabelKey, formatAge, formatCount, formatWorkerTimestamp,
  normalizeWorkerState, statusTone, workerLabelKey
} from '../src/utils/operations.ts'

const knownStates: WorkerState[] = ['disabled', 'not_started', 'running', 'stale', 'stopped', 'unknown']

test('worker state mapping is allow-listed, readable, and never promotes disabled roles', () => {
  for (const state of knownStates) {
    assert.equal(normalizeWorkerState({ enabled: true, state }), state)
    assert.equal(workerLabelKey(state), `operations.states.${state}`)
    assert.ok(statusTone(state))
  }
  assert.equal(normalizeWorkerState({ enabled: true, state: 'future_state' as WorkerState }), 'unknown')
  assert.equal(normalizeWorkerState({ enabled: false, state: 'running' }), 'disabled')
})

test('nullable counts and ages stay unknown rather than becoming zero', () => {
  assert.equal(formatCount(null, 'en-US', 'Unknown'), 'Unknown')
  assert.equal(formatCount(undefined, 'en-US', 'Unknown'), 'Unknown')
  assert.equal(formatCount(0, 'en-US', 'Unknown'), '0')
  assert.equal(formatAge(null, 'en-US', 'Unknown'), 'Unknown')
  assert.equal(formatAge(-1, 'en-US', 'Unknown'), 'Unknown')
  assert.equal(formatAge(0, 'en-US', 'Unknown'), '0s')
  assert.equal(formatAge(62, 'en-US', 'Unknown'), '1m 2s')
  assert.equal(formatAge(62, 'zh-CN', '未知'), '1分2秒')
})

test('timestamps format valid ISO values and use an explicit fallback for missing or invalid values', () => {
  const formatted = formatWorkerTimestamp('2026-06-07T12:34:56Z', 'en-US', 'Not recorded')
  assert.match(formatted, /2026/)
  assert.notEqual(formatted, '2026-06-07T12:34:56Z')
  assert.equal(formatWorkerTimestamp(null, 'en-US', 'Not recorded'), 'Not recorded')
  assert.equal(formatWorkerTimestamp('not-a-date', 'en-US', 'Not recorded'), 'Not recorded')
})

test('failure labels expose only safe fixed categories', () => {
  assert.equal(failureLabelKey('task_failed'), 'operations.failureCodes.taskFailed')
  assert.equal(failureLabelKey('worker_unavailable'), 'operations.failureCodes.workerUnavailable')
  assert.equal(failureLabelKey('secret/path/user@example.com'), 'operations.failureCodes.generic')
  assert.equal(failureLabelKey('<script>alert(1)</script>'), 'operations.failureCodes.generic')
})

test('a late response cannot replace the latest refresh result', async () => {
  const gate = createRequestGate()
  let releaseOld!: (value: string) => void
  let releaseNew!: (value: string) => void
  const oldResponse = new Promise<string>(resolve => { releaseOld = resolve })
  const newResponse = new Promise<string>(resolve => { releaseNew = resolve })
  let displayed = ''
  const apply = async (requestId: number, response: Promise<string>) => {
    const result = await response
    if (gate.isCurrent(requestId)) displayed = result
  }

  const oldId = gate.begin()
  const oldApply = apply(oldId, oldResponse)
  const newId = gate.begin()
  const newApply = apply(newId, newResponse)
  releaseNew('new snapshot')
  await newApply
  releaseOld('old snapshot')
  await oldApply
  assert.equal(displayed, 'new snapshot')

  const pendingId = gate.begin()
  gate.invalidate()
  assert.equal(gate.isCurrent(pendingId), false)
})
