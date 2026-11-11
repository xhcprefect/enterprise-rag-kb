import type { WorkerRoleStatus, WorkerState } from '@/api/operations'

const STATES: WorkerState[] = ['disabled', 'not_started', 'running', 'stale', 'stopped', 'unknown']

export function isWorkerState(value: unknown): value is WorkerState {
  return typeof value === 'string' && STATES.includes(value as WorkerState)
}

export function normalizeWorkerState(role: Pick<WorkerRoleStatus, 'enabled' | 'state'>): WorkerState {
  if (!role.enabled) return 'disabled'
  return isWorkerState(role.state) ? role.state : 'unknown'
}

export function formatCount(value: number | null | undefined, locale: string, unknownText: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return unknownText
  return new Intl.NumberFormat(locale).format(value)
}

export function formatAge(seconds: number | null | undefined, locale: string, unknownText: string): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return unknownText
  const rounded = Math.floor(seconds)
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const remainder = rounded % 60
  if (locale.toLowerCase().startsWith('zh')) {
    if (hours) return minutes ? `${hours}小时${minutes}分` : `${hours}小时`
    if (minutes) return remainder ? `${minutes}分${remainder}秒` : `${minutes}分`
    return `${remainder}秒`
  }
  if (hours) return minutes ? `${hours}h ${minutes}m` : `${hours}h`
  if (minutes) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`
  return `${remainder}s`
}

export function formatWorkerTimestamp(value: string | null | undefined, locale: string, unknownText: string): string {
  if (!value) return unknownText
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return unknownText
  return new Intl.DateTimeFormat(locale, {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
  }).format(date)
}

export function failureLabelKey(code: unknown): string {
  if (code === 'task_failed') return 'operations.failureCodes.taskFailed'
  if (code === 'worker_unavailable') return 'operations.failureCodes.workerUnavailable'
  return 'operations.failureCodes.generic'
}

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'muted'

export function statusTone(state: WorkerState): StatusTone {
  switch (state) {
    case 'running': return 'success'
    case 'stale': return 'warning'
    case 'stopped': return 'danger'
    case 'disabled': return 'muted'
    case 'not_started': return 'info'
    default: return 'warning'
  }
}

export function workerLabelKey(state: WorkerState): string {
  return `operations.states.${state}`
}

/** A monotonically increasing gate prevents an older response from winning a refresh race. */
export function createRequestGate() {
  let version = 0
  return {
    begin: () => ++version,
    isCurrent: (requestId: number) => requestId === version,
    invalidate: () => { version += 1 }
  }
}

export function isCancelledRequest(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const candidate = error as { code?: unknown; name?: unknown }
  return candidate.code === 'ERR_CANCELED' || candidate.code === 'ABORT_ERR' ||
    candidate.name === 'CanceledError' || candidate.name === 'AbortError'
}
