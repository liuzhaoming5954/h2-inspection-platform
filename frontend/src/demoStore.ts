import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage, request, type DemoRoute, type DemoState, type DemoStep, type DemoStepType } from './api'

/**
 * 巡检演示的共享状态层：侧栏控制组和主区状态条可能同时挂载，
 * 两处必须看到同一条路线选择、同一次轮询，否则按钮状态和进度显示会各说各话。
 * 状态轮询按引用计数启停，最后一个使用方卸载时停止。
 */

export interface DraftStep { type: DemoStepType; value: number; task: 'qr' | 'led' | 'person' | null }

const routes = ref<DemoRoute[]>([])
const selectedId = ref('')
const state = ref<DemoState | null>(null)
const busy = ref('')
const message = ref('')
const editing = ref(false)
const draftName = ref('')
const draftSteps = ref<DraftStep[]>([])

export { routes, selectedId, state, busy, message, editing, draftName, draftSteps }
export const selected = computed(() => routes.value.find(route => route.id === selectedId.value) ?? null)

// 状态带快捷按钮固定绑定的演示路线；按存储名精确匹配，找不到时按钮置灰。
export const QUICK_ROUTE_NAME = '多点测试-12步'
export const quickRoute = computed(() => routes.value.find(route => route.name === QUICK_ROUTE_NAME) ?? null)
export const phase = computed(() => state.value?.phase ?? 'idle')
// 「已就绪」不算忙：准备成功的瞬间必须亮出巡检演示按钮（2026-11-21 现场
// 反馈的缺陷：prepared 误入 busy 集合导致按钮在应亮起时反而消失）
export const demoBusy = computed(() => ['preparing', 'starting', 'armed', 'running'].includes(phase.value))
export const prepared = computed(() => phase.value === 'prepared')

export const phaseNames: Record<string, string> = {
  idle: '未启动', preparing: '准备中', prepared: '已就绪', prep_failed: '准备失败',
  starting: '演示开始', armed: '待命听令', running: '执行中', done: '已完成', aborted: '已中止',
}
// 就绪检查项（与后端 CHECK_LABELS 对应）
export const checkLabels: Record<string, string> = {
  mic: '麦克风数据流', asr: '语音识别', pose: '建图位姿', imu: 'IMU 转角', vision: '相机画面', teleop: '运控就绪',
}

async function pollState() {
  try {
    state.value = await request<DemoState>('/api/demo/state', { cache: 'no-store' })
  } catch { /* 状态查询失败沿用上一帧 */ }
}

export async function loadRoutes() {
  try {
    const list = await request<DemoRoute[]>('/api/demo/routes')
    routes.value = list
    if (!list.some(route => route.id === selectedId.value)) {
      selectedId.value = list[0]?.id ?? ''
    }
  } catch (error) {
    message.value = `路线加载失败：${errorMessage(error)}`
  }
}

export function stepText(step: DemoStep): string {
  if (step.type === 'forward') return `前进 ${step.value} m`
  if (step.type === 'turn') return `${step.value >= 0 ? '左转' : '右转'} ${Math.abs(step.value)}°`
  const task = step.task ?? (step.read_aloud ? 'qr' : null)
  return `停留 ${step.value} s${task ? ' · ' + ({ qr: '朗读二维码', led: '播报信号灯', person: '播报人数' } as Record<string,string>)[task] : ''}`
}

// 演示开关关闭后保留终态（done/aborted）供操作员查看原因，不自动清零
watch(phase, (next, previous) => {
  if (previous && previous !== 'idle' && next === 'idle') message.value = ''
})

let stateTimer: number | undefined
let users = 0

/** 启动共享轮询：多个使用方（侧栏控制组 + 主区状态条）只保留一个定时器。 */
export function startDemoPolling() {
  users += 1
  if (stateTimer === undefined) {
    void pollState()
    void loadRoutes()
    stateTimer = window.setInterval(() => { void pollState() }, 2000)
  }
}

export function stopDemoPolling() {
  users = Math.max(0, users - 1)
  if (users === 0 && stateTimer !== undefined) {
    window.clearInterval(stateTimer)
    stateTimer = undefined
  }
}

/** 「巡检准备」：一键拉起全部服务并做功能性就绪检测，全绿后播报就绪。 */
export async function prepareDemo() {
  if (busy.value || prepared.value) return
  busy.value = 'prepare'
  message.value = ''
  try {
    // 冷启动建图最长约 30 秒，准备总时限后端 150 秒，前端放宽
    await request<{ ok: boolean }>('/api/demo/prepare', {
      method: 'POST', signal: AbortSignal.timeout(180_000),
    })
    void pollState()
  } catch (error) {
    message.value = `失败：${errorMessage(error)}`
  } finally {
    busy.value = ''
  }
}

/** 打开演示开关：要求先「巡检准备」就绪，后端播问候语并武装语音触发。
 *  routeId 缺省时用侧栏选中的路线；状态带快捷按钮显式传入固定路线，互不影响侧栏选择。 */
export async function startDemo(routeId?: string) {
  const route = routeId ? routes.value.find(r => r.id === routeId) : selected.value
  if (!route || busy.value) return
  busy.value = 'start'
  message.value = ''
  try {
    await request<{ ok: boolean }>('/api/demo/start', {
      method: 'POST', body: JSON.stringify({ route_id: route.id }),
    })
    message.value = '演示已开启：机器人播报问候语后等待「开始」口令'
    void pollState()
  } catch (error) {
    message.value = `失败：${errorMessage(error)}`
  } finally {
    busy.value = ''
  }
}

export async function stopDemo() {
  if (busy.value) return
  busy.value = 'stop'
  try {
    await request<{ ok: boolean }>('/api/demo/stop', { method: 'POST' })
    void pollState()
  } catch (error) {
    message.value = `失败：${errorMessage(error)}`
  } finally {
    busy.value = ''
  }
}

/** 调试路径：不武装语音、不拉服务直接执行选中路线（模拟干跑同入口）。 */
export async function runManual() {
  if (!selected.value || busy.value || demoBusy.value) return
  busy.value = 'run'
  message.value = ''
  try {
    await request<{ ok: boolean }>(`/api/demo/routes/${selected.value.id}/run`, { method: 'POST' })
    void pollState()
  } catch (error) {
    message.value = `失败：${errorMessage(error)}`
  } finally {
    busy.value = ''
  }
}

export function beginEdit() {
  const source = selected.value
  draftName.value = source ? `${source.name}副本` : '新路线'
  draftSteps.value = source
    ? source.steps.map(step => ({ type: step.type, value: step.value, task: step.task ?? (step.read_aloud ? 'qr' : null) }))
    : [{ type: 'forward', value: 0.3, task: null }]
  editing.value = true
}

export function toggleEdit() {
  if (editing.value) {
    editing.value = false
    return
  }
  beginEdit()
}

export function addStep() {
  draftSteps.value.push({ type: 'forward', value: 0.3, task: null })
}

export function removeStep(index: number) {
  draftSteps.value.splice(index, 1)
}

export function moveStep(index: number, offset: -1 | 1) {
  const target = index + offset
  if (target < 0 || target >= draftSteps.value.length) return
  const steps = draftSteps.value
  ;[steps[index], steps[target]] = [steps[target], steps[index]]
}

export async function saveRoute() {
  if (busy.value) return
  if (!draftName.value.trim()) {
    message.value = '请填写路线名称'
    return
  }
  busy.value = 'save'
  try {
    const created = await request<DemoRoute>('/api/demo/routes', {
      method: 'POST',
      body: JSON.stringify({
        name: draftName.value.trim(),
        steps: draftSteps.value.map(step => ({
          type: step.type, value: Number(step.value),
          ...(step.type === 'wait' && step.task ? { task: step.task } : {}),
        })),
      }),
    })
    editing.value = false
    await loadRoutes()
    selectedId.value = created.id
    ElMessage.success(`路线「${created.name}」已保存`)
  } catch (error) {
    message.value = `保存失败：${errorMessage(error)}`
  } finally {
    busy.value = ''
  }
}

export async function deleteRoute() {
  if (!selected.value || busy.value || demoBusy.value) return
  busy.value = 'delete'
  try {
    await request<{ ok: boolean }>(`/api/demo/routes/${selected.value.id}`, { method: 'DELETE' })
    await loadRoutes()
  } catch (error) {
    message.value = `删除失败：${errorMessage(error)}`
  } finally {
    busy.value = ''
  }
}
