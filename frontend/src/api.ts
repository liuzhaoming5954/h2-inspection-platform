export interface RoutePoint {
  id: string
  label: string
  x: number
  y: number
}

export interface Sample {
  point_id: string
  label?: string
  temperature_c: number
  humidity_percent: number
  captured_at?: string
  image_url?: string
  simulated?: boolean
}

export interface Report {
  format: string
  url: string
  file_id?: string
}

export interface Task {
  id: string
  name: string
  status: string
  progress: number
  request_id: string
  points: RoutePoint[]
  samples: Sample[]
  review_note?: string
  created_at: string
  reports?: Report[]
  reason?: string
}

export interface RobotState {
  status?: string
  robot_id?: string
  battery_percent?: number
  battery?: number
  connected?: boolean
  connection_status?: string
  task_id?: string
  progress?: number
  sequence?: number
  start_count?: number
  x?: number
  y?: number
  pose?: { x: number; y: number; yaw: number } | null
  position?: { x?: number; y?: number; yaw?: number }
  teleop_active?: boolean
  teleop_ready?: boolean
  fsm_id?: number | null
  fsm_mode?: number | null
  temperature_c?: number | null
  humidity_percent?: number | null
  environment_captured_at?: number | null
  environment_sample_id?: string | null
  environment_remaining_ms?: number | null
  motion?: 'stop' | 'forward' | 'backward' | 'turn_left' | 'turn_right' | 'stop_requested' | 'unknown'
  simulated?: boolean
}

export interface ServiceHealth {
  status?: string
  hardware_teleop?: boolean
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const timeout = AbortSignal.timeout(path.endsWith('/reports') ? 60_000 : 8_000)
  const response = await fetch(path, {
    ...init,
    signal: init?.signal ?? timeout,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = body?.detail
    throw new Error(typeof detail === 'string' ? detail : `请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === 'TimeoutError') return '服务响应超时，请检查后端与机器人网关。'
  return error instanceof Error ? error.message : '操作失败，请重试。'
}
