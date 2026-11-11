import client from './client.ts'

export type WorkerRoleName = 'ingest' | 'summary' | 'project' | 'regression'
export type WorkerState = 'disabled' | 'not_started' | 'running' | 'stale' | 'stopped' | 'unknown'

export interface WorkerFailure {
  updated_at: string
  code: string
}

export interface WorkerRoleStatus {
  role: WorkerRoleName
  enabled: boolean
  state: WorkerState
  alive_count: number | null
  last_heartbeat_at: string | null
  pending: number | null
  processing: number | null
  stale_tasks: number | null
  oldest_pending_age_seconds: number | null
  last_failure: WorkerFailure | null
}

export interface WorkerStatusSnapshot {
  generated_at: string
  stale_after_seconds: number
  degraded: boolean
  roles: WorkerRoleStatus[]
}

/** The shared client unwraps the standard API envelope before returning data. */
export async function getWorkerStatus(signal?: AbortSignal): Promise<WorkerStatusSnapshot> {
  const { data } = await client.get<WorkerStatusSnapshot>('/operations/workers', { signal })
  return data
}
