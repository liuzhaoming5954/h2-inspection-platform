<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import RoomScene from './components/RoomScene.vue'
import MapPanel from './components/MapPanel.vue'
import EnvironmentChart from './components/EnvironmentChart.vue'
import TeleopPanel from './components/TeleopPanel.vue'
import DemoPanel from './components/DemoPanel.vue'
import { errorMessage, request, type Report, type RobotState, type RoutePoint, type ServiceHealth, type Task } from './api'
import { busy as demoBusyFlag, demoBusy, phase as demoPhase, prepareDemo, quickRoute, startDemo, stopDemo, QUICK_ROUTE_NAME } from './demoStore'

const defaultPoints: RoutePoint[] = [
  { id: 'p01', label: 'A01机柜', x: -3, y: -1.8 },
  { id: 'p02', label: 'A02机柜', x: 0, y: -1.8 },
  { id: 'p03', label: 'B01机柜', x: 3, y: 1.8 },
]
const robot = ref<RobotState | null>(null)
const tasks = ref<Task[]>([])
const selectedTask = ref<Task | null>(null)
const serviceAvailable = ref(false)
const robotAvailable = ref(false)
const hardwareConfigured = ref<boolean | null>(null)
const hardwareMode = computed(() => hardwareConfigured.value === true || robot.value?.simulated === false)
const modeKnown = computed(() => hardwareConfigured.value !== null || typeof robot.value?.simulated === 'boolean')
const connected = computed(() => serviceAvailable.value && robotAvailable.value
  && (!hardwareMode.value || robot.value?.simulated === false))
const lastSync = ref('')
const serviceError = ref('')
const robotError = ref('')
const connectionError = computed(() => serviceError.value || robotError.value)
const serviceLoading = ref(true)
const robotLoading = ref(true)
const loading = computed(() => serviceLoading.value || robotLoading.value)
const teleopSession = ref(false)
// 演示编排占用运动租约时（拉起/武装/执行）禁用手动遥控：单一租约互斥的界面提示。
// 状态来自 demoStore，侧栏控制组与主区状态条共用同一份数据。
const demoActive = demoBusy
// 侧栏抽屉：手动呼出/收起，不做鼠标移开自动隐藏。
// 遥控会话或演示执行期间禁止收起——方向键消失时机器人可能仍在运动。
const sidebarOpen = ref(false)
const sidebarToggleDisabled = computed(() => teleopSession.value || demoBusy.value)
function toggleSidebar() {
  if (sidebarOpen.value && sidebarToggleDisabled.value) return
  sidebarOpen.value = !sidebarOpen.value
}
// 演示开始（准备/武装/执行）时自动呼出侧栏：停止演示按钮与执行进度必须可见可达
watch(demoBusy, busy => { if (busy) sidebarOpen.value = true }, { immediate: true })
const submitting = ref(false)
const stopping = ref(false)
const reporting = ref(false)
const reviewOpen = ref(false)
const reviewNote = ref('')
const savingReview = ref(false)
const taskName = ref('机房环境验证巡检')
const pendingRequestId = ref<string | null>(null)
const clock = ref(new Date())
let pollTimer: ReturnType<typeof setTimeout> | undefined
let clockTimer: ReturnType<typeof setInterval> | undefined
let robotPollTimer: ReturnType<typeof setTimeout> | undefined
let robotRequest: AbortController | undefined
let disposed = false

const terminalStates = new Set(['completed', 'stopped', 'interrupted', 'failed', 'rejected', 'expired', 'dispatch_expired'])
const active = computed(() => Boolean(selectedTask.value && !terminalStates.has(selectedTask.value.status)))
const robotBusy = computed(() => ['running', 'accepted', 'stopping'].includes(robot.value?.status ?? ''))
const remoteTeleop = computed(() => !!(robot.value?.teleop_active || robot.value?.status === 'teleop'))
const teleopLocked = computed(() => teleopSession.value || remoteTeleop.value)
const teleopDisabledReason = computed(() => {
  if (hardwareMode.value) {
    if (hardwareConfigured.value !== true || robot.value?.simulated === true) return '等待真机遥控模式确认'
    // 遥控网关在授予控制权和处理每次请求时检查实时就绪状态。
    // 状态轮询延迟时，仍允许正常的遥控会话继续运行。
    if (teleopSession.value) return ''
    if (demoActive.value) return '巡检演示执行中，结束后可申请遥控'
    if (!connected.value || !robot.value) return '等待服务和机器人连接'
    if (robot.value.status === 'fault') return '真机遥控故障，请检查网关和机器人'
    return robot.value.teleop_ready === true ? '' : '等待机器人进入可遥控的高层运动状态'
  }
  if (!connected.value) return '等待服务和机器人连接'
  return active.value || robotBusy.value || submitting.value || stopping.value || demoActive.value ? '巡检任务或演示执行中，结束后可申请遥控' : ''
})
const progress = computed(() => Math.max(0, Math.min(100, selectedTask.value?.progress ?? 0)))
const points = computed(() => selectedTask.value?.points?.length ? selectedTask.value.points : defaultPoints)
const samples = computed(() => selectedTask.value?.samples ?? [])
const latestSample = computed(() => samples.value.at(-1))
const reports = computed(() => selectedTask.value?.reports ?? [])
const battery = computed(() => robot.value?.battery_percent ?? robot.value?.battery)
// 链路管理区用指示灯显示各环节状态。
// 每个按钮在操作员点击后执行对应操作，连接恢复也由操作员触发。
interface FieldStatus { pc2_agent: boolean; vision_ready?: boolean; map_ready?: boolean; asr_ready?: boolean; forwards: Record<string, boolean>; gateway: boolean; valves?: { vision?: boolean; mapping?: boolean; asr?: boolean } }
const fieldStatus = ref<FieldStatus | null>(null)
const fieldBusy = ref('')
const fieldMessage = ref('')

// 数据接入阀门：开=确保上游程序在跑并接入数据；关=只断数据，程序保持运行。
// 状态以后端为准，状态轮询纠偏，点击立即本地生效。语音默认关（主动使用型）。
const visionValve = ref(true)
const mappingValve = ref(true)
const asrValve = ref(false)

async function pollField() {
  if (disposed || hardwareConfigured.value !== true) return
  try {
    const status = await request<FieldStatus>('/api/field/status', { cache: 'no-store' })
    if (!disposed && hardwareConfigured.value === true) {
      fieldStatus.value = status
      if (status.valves) {
        visionValve.value = status.valves.vision !== false
        mappingValve.value = status.valves.mapping !== false
        asrValve.value = status.valves.asr === true
      }
    }
  } catch { /* 查询失败时沿用上一次收到的状态。 */ }
}

async function toggleDataValve(kind: 'vision' | 'mapping' | 'asr') {
  if (hardwareConfigured.value !== true || fieldBusy.value) return
  const enabled = kind === 'vision' ? !visionValve.value : kind === 'mapping' ? !mappingValve.value : !asrValve.value
  fieldBusy.value = `data-${kind}`
  fieldMessage.value = ''
  try {
    // 开阀可能触发程序拉起（建图最长约 25 秒），单独放宽超时
    const result = await request<{ ok: boolean; enabled?: boolean; already?: boolean; error?: string }>(
      `/api/field/data/${kind}`,
      { method: 'POST', body: JSON.stringify({ enabled }), signal: AbortSignal.timeout(45_000) })
    if (kind === 'vision') visionValve.value = result.enabled !== false
    else if (kind === 'mapping') mappingValve.value = result.enabled !== false
    else asrValve.value = result.enabled !== false
    if (!result.ok) fieldMessage.value = `失败：${result.error ?? '上游未就绪'}`
    else if (result.enabled === false) {
      const label = kind === 'vision' ? '相机数据' : kind === 'mapping' ? '雷达数据' : '语音识别'
      fieldMessage.value = `${label}已关闭（服务保持运行）`
    } else {
      fieldMessage.value = kind === 'asr'
        ? (result.already ? '服务已在运行，已恢复采音' : '服务已拉起，开始采音')
        : (result.already ? '程序已在运行，数据已接入' : '程序已拉起，数据已接入')
    }
  } catch (error) {
    fieldMessage.value = `失败：${errorMessage(error)}`
  } finally {
    fieldBusy.value = ''
    void pollField()
  }
}

// 视觉识别：PC2 头部相机程序的结果经后端转发。该程序约 3Hz 输出，
// 与温湿度一致按 8 秒采样年龄判断新鲜度，超龄即提示视觉程序未运行。
interface VisionLed { color: string; distance_mm: number; x: number; y: number }
interface VisionStatus {
  available: boolean
  age_ms?: number
  person?: number
  led?: number
  red?: number
  green?: number
  qr?: string | null
  center_mm?: number
  leds?: VisionLed[]
  frame_ts?: string
  error?: string
}
const visionStatus = ref<VisionStatus | null>(null)
const visionFreshnessMs = 8000
const visionLive = computed(() => visionStatus.value?.available === true
  && (visionStatus.value.age_ms ?? Number.POSITIVE_INFINITY) <= visionFreshnessMs)

async function pollVision() {
  if (disposed || hardwareConfigured.value !== true || !visionValve.value) return
  try {
    const status = await request<VisionStatus>('/api/field/vision', { cache: 'no-store' })
    if (!disposed && hardwareConfigured.value === true) visionStatus.value = status
  } catch { /* 网络闪断时保留上一次结果。 */ }
}

const visionPerson = computed(() => visionLive.value ? visionStatus.value?.person ?? 0 : null)
const visionLedTotal = computed(() => visionLive.value ? visionStatus.value?.led ?? 0 : null)
const visionQr = computed(() => visionLive.value ? visionStatus.value?.qr ?? null : null)
// 卡片脚注宽度有限，未运行状态用紧凑相对时间，完整信息在 title 提示里。
function visionAgeText(ageMs: number): string {
  const seconds = Math.round(ageMs / 1000)
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.round(minutes / 60)
  return hours < 24 ? `${hours} 小时前` : `${Math.round(hours / 24)} 天前`
}
const visionNote = computed(() => {
  if (!visionValve.value) return '数据接入已关闭'
  const status = visionStatus.value
  if (status === null) return '无法获取视觉数据'
  if (status.available !== true) return 'PC2 视觉接口不可用'
  if (!visionLive.value) return `视觉程序未运行 · ${visionAgeText(status.age_ms ?? 0)}`
  const parts = [`红 ${status.red ?? 0} · 绿 ${status.green ?? 0}`]
  parts.push(visionQr.value ? `二维码「${visionQr.value}」` : '无二维码')
  if (status.center_mm) parts.push(`中心 ${(status.center_mm / 1000).toFixed(2)} m`)
  return parts.join(' · ')
})

// 图像显示模块：轮询 PC2 的预览帧（带检测框 JPEG）。用对象 URL 换图，
// 避免 <img> 的 URL 缓存；采样年龄来自 PC2 写入的响应头，无需跨机对时。
const visionFrameUrl = ref<string | null>(null)
const visionFrameAgeMs = ref<number | null>(null)
let visionFrameObjectUrl: string | undefined

async function pollVisionFrame() {
  if (disposed || hardwareConfigured.value !== true || !visionValve.value) return
  try {
    const response = await fetch('/api/field/vision/frame', { cache: 'no-store' })
    if (!response.ok) return  // 来源暂不可用时保留上一帧，状态由年龄提示。
    const ageHeader = Number(response.headers.get('X-Frame-Age-Ms'))
    const blob = await response.blob()
    if (disposed) return
    const next = URL.createObjectURL(blob)
    const previous = visionFrameObjectUrl
    visionFrameObjectUrl = next
    visionFrameUrl.value = next
    visionFrameAgeMs.value = Number.isFinite(ageHeader) && ageHeader >= 0 ? ageHeader : null
    if (previous) URL.revokeObjectURL(previous)
  } catch { /* 网络闪断时保留上一帧。 */ }
}

const visionFrameNote = computed(() => {
  if (!visionValve.value) return '数据接入已关闭'
  const age = visionFrameAgeMs.value
  if (visionFrameUrl.value === null) return '等待相机画面接入'
  if (age == null) return '机器人摄像头'
  return age <= visionFreshnessMs
    ? `机器人摄像头 · ${(age / 1000).toFixed(1)} 秒前`
    : `画面停滞 · ${visionAgeText(age)}`
})

// 语音识别：阀门开时增量拉取转写文本（机器人麦克风 → PC3 本地转写）
interface AsrItem { id: number; ts: number; text: string }
const asrItems = ref<AsrItem[]>([])
let asrLastId = 0
const asrLogEl = ref<HTMLDivElement>()

async function pollAsrText() {
  if (disposed || hardwareConfigured.value !== true || !asrValve.value) return
  try {
    let payload = await request<{ items: AsrItem[]; last_id: number }>(
      `/api/field/asr/text?since=${asrLastId}`, { cache: 'no-store' })
    if (disposed || !asrValve.value) return
    // ASR 服务重启后 id 从 1 重新计数：检测到回卷就从头拉一遍，避免新文本被旧 since 滤掉
    if (payload.last_id < asrLastId) {
      asrLastId = 0
      payload = await request(`/api/field/asr/text?since=0`, { cache: 'no-store' })
    }
    if (payload.items.length) {
      asrItems.value = [...asrItems.value, ...payload.items].slice(-50)
      asrLastId = payload.last_id
      void nextTick(() => {
        const el = asrLogEl.value
        if (el) el.scrollTop = el.scrollHeight
      })
    } else {
      asrLastId = Math.max(asrLastId, payload.last_id)
    }
  } catch { /* 服务未就绪时保留已收文本 */ }
}

const asrNote = computed(() => {
  if (!asrValve.value) return '数据接入已关闭'
  if (fieldStatus.value?.asr_ready === false) return '服务未就绪，等待模型加载…'
  return `机器人麦克风 · 实时转写 · 已 ${asrItems.value.length} 条`
})

watch(hardwareConfigured, (configured, _, onCleanup) => {
  fieldStatus.value = null
  visionStatus.value = null
  if (configured !== true) return
  void pollField()
  void pollVision()
  void pollVisionFrame()
  const timer = setInterval(() => { if (!fieldBusy.value) void pollField() }, 5000)
  // 视觉数据变化快于链路状态，单独用更短的轮询周期。
  const visionTimer = setInterval(() => { void pollVision() }, 2000)
  // 预览帧由视觉程序按 500ms 落盘，轮询略慢于产出即可。
  const visionFrameTimer = setInterval(() => { void pollVisionFrame() }, 700)
  // 语音转写按增量拉取，断句粒度约秒级
  const asrTimer = setInterval(() => { void pollAsrText() }, 1500)
  onCleanup(() => {
    clearInterval(timer)
    clearInterval(visionTimer)
    clearInterval(visionFrameTimer)
    clearInterval(asrTimer)
  })
})

async function fieldAction(kind: 'reconnect-forwards' | 'start-pc2' | 'start-pc3') {
  if (hardwareConfigured.value !== true || fieldBusy.value) return
  fieldBusy.value = kind
  fieldMessage.value = ''
  try {
    const result = await request<{ ok: boolean; already?: boolean; error?: string }>(`/api/field/${kind}`, { method: 'POST' })
    fieldMessage.value = result.ok ? (result.already ? '已在运行，未重复启动' : '完成') : `失败：${result.error ?? '未知原因'}`
  } catch (error) {
    fieldMessage.value = `失败：${errorMessage(error)}`
  } finally {
    fieldBusy.value = ''
    void pollField()
  }
}
const stateNames: Record<string, string> = { idle: '待命', ready: '待命', pending_dispatch: '等待派发', accepted: '任务已接收', running: '巡检中', completed: '已完成', stopped: '已停止', interrupted: '执行中断', failed: '执行失败', rejected: '未接受', expired: '已过期', dispatch_expired: '派发已过期', stopping: '正在停止', offline: '离线', teleop: '手动遥控' }
const hardwareStateNames: Record<string, string> = { ready: '可遥控', not_ready: '未就绪', offline: '离线', teleop: '遥控会话中', fault: '故障' }
const hardwareState = computed(() => robotAvailable.value && robot.value?.simulated === false && robot.value.status
  ? hardwareStateNames[robot.value.status] ?? '状态未知'
  : '未连接')
const environmentFreshnessMs = 8000
const environmentWatermarks = new Map<string, { sequence: number; expiresAt: number }>()
const freshEnvironment = ref<Pick<RobotState, 'temperature_c' | 'humidity_percent'> | null>(null)
let environmentInstance: string | undefined
let environmentTimer: ReturnType<typeof setTimeout> | undefined
const hardwareEnvironment = computed(() => connected.value && robot.value?.simulated === false
  && robot.value.status !== 'offline' ? freshEnvironment.value : null)
const environmentTemperature = computed(() => hardwareMode.value ? hardwareEnvironment.value?.temperature_c ?? null : latestSample.value?.temperature_c ?? null)
const environmentHumidity = computed(() => hardwareMode.value ? hardwareEnvironment.value?.humidity_percent ?? null : latestSample.value?.humidity_percent ?? null)
const environmentNote = computed(() => hardwareMode.value
  ? hardwareEnvironment.value ? '机身传感器 · 实测' : '等待传感器数据'
  : latestSample.value ? `${latestSample.value.label ?? latestSample.value.point_id} · 合成采样` : '任务执行后显示采集结果')
const taskState = computed(() => selectedTask.value ? stateNames[selectedTask.value.status] ?? selectedTask.value.status : '等待下发')
const stateTone = computed(() => selectedTask.value?.status === 'completed' ? 'success' : active.value ? 'primary' : selectedTask.value ? 'warning' : 'info')

function displayTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function updateHardwareEnvironment(state: RobotState, requestStartedAt: number) {
  clearTimeout(environmentTimer)
  const sampleId = typeof state.environment_sample_id === 'string'
    ? /^([0-9a-f]{32}):([1-9][0-9]{0,15})$/.exec(state.environment_sample_id) : null
  const remaining = state.environment_remaining_ms
  if (state.simulated !== false || state.status === 'offline' || !sampleId || typeof remaining !== 'number'
      || !Number.isSafeInteger(remaining) || remaining < 0 || remaining > environmentFreshnessMs
      || !Number.isFinite(state.temperature_c) || !Number.isFinite(state.humidity_percent)) {
    freshEnvironment.value = null
    return
  }
  const instance = sampleId[1]!
  const sequence = Number(sampleId[2])
  const previous = environmentWatermarks.get(instance)
  if (!Number.isSafeInteger(sequence) || (previous && (instance !== environmentInstance || sequence < previous.sequence))) {
    freshEnvironment.value = null
    return
  }
  // 从请求发出时开始计时，将整个 HTTP 往返耗时计入样本年龄。
  // 重复收到同一条样本时，截止时间只会提前或保持原值。
  const expiresAt = previous?.sequence === sequence
    ? Math.min(previous.expiresAt, requestStartedAt + remaining) : requestStartedAt + remaining
  environmentWatermarks.set(instance, { sequence, expiresAt })
  environmentInstance = instance
  const timeLeft = expiresAt - performance.now()
  if (timeLeft <= 0) {
    freshEnvironment.value = null
    return
  }
  freshEnvironment.value = { temperature_c: state.temperature_c, humidity_percent: state.humidity_percent }
  environmentTimer = setTimeout(() => { freshEnvironment.value = null }, Math.ceil(timeLeft))
}

async function sync() {
  try {
    const health = await request<ServiceHealth>('/api/health')
    if (disposed) return
    hardwareConfigured.value = health.hardware_teleop === true
    serviceAvailable.value = health.status !== 'error'
    serviceError.value = ''
    if (hardwareMode.value) {
      reviewOpen.value = false
      return
    }
    const rawTasks = await request<Task[] | { tasks: Task[] }>('/api/tasks')
    if (disposed || hardwareMode.value) return
    tasks.value = Array.isArray(rawTasks) ? rawTasks : rawTasks.tasks ?? []
    if (!selectedTask.value && tasks.value.length) selectedTask.value = tasks.value[0]!
    if (selectedTask.value) {
      const detail = await request<Task>(`/api/tasks/${encodeURIComponent(selectedTask.value.id)}`)
      if (!disposed && detail.id === selectedTask.value?.id) selectedTask.value = detail
    }
  } catch (error) {
    if (!disposed) {
      serviceAvailable.value = false
      serviceError.value = errorMessage(error)
    }
  } finally {
    serviceLoading.value = false
  }
}

// 页面位姿只由串行执行的机器人状态轮询更新。
// 任务和健康检查独立请求，较慢的响应不会覆盖新位姿。
async function pollRobot() {
  if (disposed || robotRequest) return
  clearTimeout(robotPollTimer)
  const controller = new AbortController()
  robotRequest = controller
  try {
    const requestedAt = performance.now()
    const raw = await request<RobotState & { state?: RobotState }>('/api/robot', {
      cache: 'no-store',
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(1600)]),
    })
    if (disposed) return
    robot.value = raw.state ?? raw
    robotAvailable.value = robot.value.connected !== false && robot.value.connection_status !== 'offline'
    updateHardwareEnvironment(robot.value, requestedAt)
    robotError.value = robotAvailable.value ? '' : '机器人未连接。'
    lastSync.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (error) {
    if (!disposed) {
      robotAvailable.value = false
      robotError.value = errorMessage(error)
    }
  } finally {
    robotRequest = undefined
    robotLoading.value = false
    if (!disposed) robotPollTimer = setTimeout(pollRobot, teleopLocked.value ? 150 : 1000)
  }
}

function setTeleopSession(value: boolean) {
  teleopSession.value = value
  void pollRobot()
}

function refreshAll() {
  void pollRobot()
  void sync()
}

async function poll() {
  await sync()
  if (!disposed) pollTimer = setTimeout(poll, 1000)
}

async function createTask() {
  if (hardwareMode.value || !connected.value || active.value || robotBusy.value || teleopLocked.value || submitting.value) return
  submitting.value = true
  pendingRequestId.value ??= crypto.randomUUID()
  try {
    selectedTask.value = await request<Task>('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({ name: taskName.value.trim() || '机房环境验证巡检', request_id: pendingRequestId.value, points: defaultPoints }),
    })
    pendingRequestId.value = null
    if (selectedTask.value.status === 'rejected' || selectedTask.value.status === 'dispatch_expired') {
      ElMessage.warning(selectedTask.value.reason || `任务${stateNames[selectedTask.value.status]}，请检查机器人状态。`)
    } else {
      ElMessage.success('巡检任务已下发，等待机器人执行反馈。')
    }
    await sync()
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally { submitting.value = false }
}

async function stopTask() {
  if (hardwareMode.value) return
  const taskId = robotBusy.value && robot.value?.task_id ? robot.value.task_id : selectedTask.value?.id
  if (!taskId) return
  stopping.value = true
  try {
    const task = await request<Task>(`/api/tasks/${encodeURIComponent(taskId)}/stop`, { method: 'POST', body: '{}' })
    if (task?.id) selectedTask.value = task
    await sync()
    ElMessage.success('停止请求已处理，请查看机器人反馈状态。')
  } catch (error) { ElMessage.error(errorMessage(error)) }
  finally { stopping.value = false }
}

async function saveReview() {
  if (!selectedTask.value || !reviewNote.value.trim()) return
  savingReview.value = true
  try {
    selectedTask.value = await request<Task>(`/api/tasks/${encodeURIComponent(selectedTask.value.id)}/review`, { method: 'POST', body: JSON.stringify({ note: reviewNote.value.trim() }) })
    reviewOpen.value = false
    ElMessage.success('人工复核意见已保存。')
  } catch (error) { ElMessage.error(errorMessage(error)) }
  finally { savingReview.value = false }
}

async function createReports() {
  if (!selectedTask.value) return
  reporting.value = true
  try {
    await request<unknown>(`/api/tasks/${encodeURIComponent(selectedTask.value.id)}/reports`, { method: 'POST', body: '{}' })
    const result = await request<Report[] | { reports: Report[] }>(`/api/tasks/${encodeURIComponent(selectedTask.value.id)}/reports`)
    selectedTask.value.reports = Array.isArray(result) ? result : result.reports ?? []
    ElMessage.success('报告已生成，可下载核验。')
    await sync()
  } catch (error) { ElMessage.error(errorMessage(error)) }
  finally { reporting.value = false }
}

async function selectTask(task: Task) {
  selectedTask.value = task
  await sync()
}
function openReview() { reviewNote.value = selectedTask.value?.review_note ?? ''; reviewOpen.value = true }

onMounted(() => {
  void poll()
  void pollRobot()
  clockTimer = setInterval(() => { clock.value = new Date() }, 1000)
})
onBeforeUnmount(() => {
  disposed = true
  clearTimeout(pollTimer)
  clearTimeout(robotPollTimer)
  clearTimeout(environmentTimer)
  clearInterval(clockTimer)
  robotRequest?.abort()
  if (visionFrameObjectUrl) URL.revokeObjectURL(visionFrameObjectUrl)
})
</script>

<template>
  <div class="app-shell" :class="{ 'hardware-shell': hardwareMode, 'hardware-drawer-open': hardwareMode && sidebarOpen }">
    <aside class="sidebar" :class="{ 'hardware-sidebar': hardwareMode, 'drawer-open': hardwareMode && sidebarOpen }">
      <a class="brand" href="/" aria-label="H2技术验证首页"><span class="brand-icon">H<span>2</span></span><span>{{ hardwareMode ? 'H2巡检平台' : '巡检运维' }}<span class="brand-sub">{{ hardwareMode ? 'INSPECTION PLATFORM' : 'INSPECTION CONSOLE' }}</span></span></a>
      <template v-if="!hardwareMode">
        <div class="workspace-label">验证工作区</div>
        <div class="nav-item active"><span class="nav-symbol">▦</span> 巡检验证台 <span class="nav-selected"></span></div>
      </template>
      <TeleopPanel v-if="modeKnown && hardwareMode" class="sidebar-teleop" :class="{ 'lease-held': teleopSession }" compact :robot="robot" :hardware="true" :telemetry-available="robotAvailable" :disabled-reason="teleopDisabledReason" @session-active="setTeleopSession" />
      <DemoPanel v-if="modeKnown && hardwareMode" hardware section="controls" class="sidebar-demo" />
      <!-- 链路管理：指示灯与恢复按钮移入侧栏，运行状态带仍在主区 -->
      <div v-if="modeKnown && hardwareMode" class="sidebar-field" data-testid="field-card" aria-label="链路管理">
        <div class="sidebar-field-head"><span class="sidebar-field-title">链路管理</span></div>
        <div class="field-lights">
          <span :class="['field-light', { on: fieldStatus?.pc2_agent }]"><i></i>PC2 代理</span>
          <span :class="['field-light', { on: fieldStatus?.gateway }]"><i></i>PC3 网关</span>
          <span :class="['field-light', { on: fieldStatus?.forwards?.['15051'] }]"><i></i>状态转发</span>
          <span :class="['field-light', { on: fieldStatus?.forwards?.['15052'] }]"><i></i>遥控转发</span>
          <span :class="['field-light', { on: fieldStatus?.vision_ready }]"><i></i>相机数据</span>
          <span :class="['field-light', { on: fieldStatus?.map_ready }]"><i></i>雷达数据</span>
          <span :class="['field-light', { on: fieldStatus?.asr_ready }]"><i></i>语音识别</span>
        </div>
        <div class="field-actions">
          <button class="field-button" type="button" :disabled="!!fieldBusy" data-testid="field-reconnect" @click="fieldAction('reconnect-forwards')">{{ fieldBusy === 'reconnect-forwards' ? '重连中…' : '重连转发' }}</button>
          <button class="field-button" type="button" :disabled="!!fieldBusy" data-testid="field-start-pc2" @click="fieldAction('start-pc2')">{{ fieldBusy === 'start-pc2' ? '启动中…' : '启动 PC2' }}</button>
          <button class="field-button" type="button" :disabled="!!fieldBusy" data-testid="field-start-pc3" @click="fieldAction('start-pc3')">{{ fieldBusy === 'start-pc3' ? '启动中…' : '启动 PC3' }}</button>
        </div>
        <small v-if="fieldMessage" class="field-message" role="status">{{ fieldMessage }}</small>
      </div>
      <template v-if="modeKnown && !hardwareMode">
        <div class="sidebar-section">验证链路</div>
        <div class="chain-item"><span class="chain-num">01</span><span>任务编排与下发</span></div>
        <div class="chain-item"><span class="chain-num">02</span><span>机器人执行反馈</span></div>
        <div class="chain-item"><span class="chain-num">03</span><span>采集数据与复核</span></div>
        <div class="chain-item"><span class="chain-num">04</span><span>报告生成与导出</span></div>
      </template>
      <div class="sidebar-foot"><span class="platform-pill">x86_64 / Ubuntu</span><p>本地技术验证环境</p><small v-if="modeKnown">{{ hardwareMode ? 'H2 HARDWARE' : 'SIMULATED ROBOT' }} · v0.1</small></div>
    </aside>

    <!-- 抽屉把手：悬浮在屏幕左上角；呼出后随侧栏右移停靠在侧栏边缘；
         遥控会话或演示执行期间禁用收起（方向键消失时机器人可能仍在运动） -->
    <button v-if="modeKnown && hardwareMode" class="sidebar-toggle" type="button" data-testid="sidebar-toggle"
            :class="{ open: sidebarOpen }"
            :disabled="sidebarOpen && sidebarToggleDisabled"
            :title="sidebarOpen ? (sidebarToggleDisabled ? '遥控会话或演示执行期间不能收起' : '收起控制栏') : '呼出控制栏'"
            @click="toggleSidebar">
      <span class="sidebar-toggle-icon" aria-hidden="true"><i></i><i></i><i></i></span>
    </button>

    <main class="main-content">
      <!-- 真机模式去掉顶栏（面包屑/时钟/头像均为纯展示），把手并入链路管理条；模拟模式维持原顶栏 -->
      <header v-if="!hardwareMode" class="topbar">
        <div class="topbar-left">
          <div class="breadcrumb">巡检管理 <span>/</span> 技术验证台</div>
        </div>
        <div class="topbar-right"><span class="env-tag">开发验证</span><time>{{ clock.toLocaleString('zh-CN', { hour12: false }) }}</time><span class="avatar">验</span></div>
      </header>
      <div class="page-content" :class="{ 'hardware-page': hardwareMode }">
        <!-- 真机模式不再渲染页头标题行：左上角品牌「H2巡检平台」已标识页面，
             手动刷新由状态轮询承担，省出的高度全部留给工作区；模拟模式维持原版页头。 -->
        <div v-if="!hardwareMode" class="page-heading"><div><div class="eyebrow">ROBOT INSPECTION / PROOF OF CONCEPT</div><h1>巡检技术验证台</h1><p>连接任务、机器人与巡检证据，验证完整业务链路。</p></div><button class="api-link" type="button" @click="refreshAll"><span>↻</span> 刷新状态</button></div>
        <div v-if="modeKnown && !hardwareMode" class="validation-banner"><span class="banner-icon">i</span><div><strong>模拟机器人 · 合成数据</strong><span>当前用于软件技术验证，巡检数据不代表真实机房状态。</span></div><span class="banner-label">TECHNICAL VALIDATION</span></div>
        <!-- 真机状态带：连接、运动服务、电量、温湿度和视觉识别一行内联摘要；链路管理在侧栏。
             右端固定路线（多点测试-12步）的准备/演示快捷按钮，与侧栏演示卡共享同一份 store 状态。 -->
        <section v-if="hardwareMode" class="status-strip" aria-label="真机连接与遥控状态">
          <span class="status-item"><i :class="['status-dot', connected ? 'online' : 'offline']"></i><strong>{{ loading ? '连接中' : connected ? '已连接' : '未连接' }}</strong><small>{{ connected ? (robot?.robot_id ?? 'H2 真机') : '等待业务服务和网关' }}</small></span>
          <span class="status-item"><label>运动服务</label><strong :title="`最近查询 ${lastSync || '—'}`">{{ hardwareState }}</strong></span>
          <span class="status-item"><label>电量</label><strong>{{ battery == null ? '—' : battery.toFixed(0) }}<i v-if="battery != null">%</i></strong><span class="mini-battery"><span :style="{ width: `${battery ?? 0}%` }"></span></span></span>
          <span class="status-item"><label>温湿度</label><strong>{{ environmentTemperature != null && environmentHumidity != null ? environmentTemperature.toFixed(1) + '℃ / ' + environmentHumidity.toFixed(1) + '%RH' : '—' }}</strong><small :title="environmentNote">{{ environmentNote }}</small></span>
          <span class="status-item"><label>视觉</label><strong>{{ visionPerson ?? '—' }} 人 · {{ visionLedTotal ?? '—' }} 灯</strong><small :title="visionQr ?? visionNote">{{ visionNote }}</small></span>
          <!-- 演示快捷入口：准备中/待命/执行中合并为停止按钮，其余状态为准备+演示两键 -->
          <span class="status-actions">
            <template v-if="demoBusy">
              <button class="strip-btn strip-stop" type="button" data-testid="strip-demo-stop"
                      :disabled="demoBusyFlag === 'stop'"
                      title="停止演示" @click="stopDemo">■ 停止演示</button>
            </template>
            <template v-else>
              <button class="strip-btn" type="button" data-testid="strip-prepare"
                      :disabled="!quickRoute || !!demoBusyFlag || demoPhase === 'prepared'"
                      :title="!quickRoute ? `未找到路线「${QUICK_ROUTE_NAME}」` : demoPhase === 'prepared' ? '已就绪' : ''"
                      @click="prepareDemo">
                {{ demoBusyFlag === 'prepare' ? '准备中…' : '⚙ 准备' }}
              </button>
              <button class="strip-btn strip-start" type="button" data-testid="strip-demo-start"
                      :disabled="!quickRoute || !!demoBusyFlag || demoPhase !== 'prepared'"
                      :title="!quickRoute ? `未找到路线「${QUICK_ROUTE_NAME}」` : demoPhase !== 'prepared' ? '请先点「准备」并等待就绪播报' : ''"
                      @click="quickRoute && startDemo(quickRoute.id)">⏻ 演示</button>
            </template>
          </span>
        </section>
        <div v-if="connectionError" class="connection-alert" role="alert"><strong>服务连接异常</strong><span>{{ connectionError }}</span><button @click="refreshAll">重新连接</button></div>

        <section v-if="modeKnown && !hardwareMode" class="metrics" aria-label="机器人与任务概况">
          <div class="metric-card"><div class="metric-top"><span>机器人连接</span><span class="metric-icon">⌁</span></div><div class="metric-value connection-value"><span :class="['status-dot', connected ? 'online' : 'offline']"></span>{{ loading ? '连接中' : connected ? '已连接' : '未连接' }}</div><div class="metric-foot">{{ connected ? (robot?.robot_id ?? 'H2 模拟机器人') : '等待业务服务和网关' }}</div></div>
          <div class="metric-card"><div class="metric-top"><span>机器人电量</span><span class="metric-icon battery-icon">▱</span></div><div class="metric-value">{{ battery === undefined ? '—' : battery.toFixed(0) }}<span class="metric-unit">%</span><span class="small-status">{{ robot?.status ? stateNames[robot.status] ?? robot.status : '状态未知' }}</span></div><div class="battery-track"><span :style="{width: `${battery ?? 0}%`}"></span></div></div>
          <div class="metric-card"><div class="metric-top"><span>任务完成进度</span><span class="metric-icon">◎</span></div><div class="metric-value">{{ progress.toFixed(0) }}<span class="metric-unit">%</span><el-tag size="small" :type="stateTone" effect="light">{{ taskState }}</el-tag></div><div class="metric-foot">已采集 {{ samples.length }} / {{ points.length }} 个巡检点</div></div>
          <div class="metric-card"><div class="metric-top"><span>最近环境采样</span><span class="metric-icon">♧</span></div><div class="sample-values"><strong>{{ environmentTemperature != null ? environmentTemperature.toFixed(1) : '—' }}<span>℃</span></strong><i></i><strong>{{ environmentHumidity != null ? environmentHumidity.toFixed(1) : '—' }}<span>%RH</span></strong></div><div class="metric-foot">{{ environmentNote }}</div></div>
        </section>

        <div v-if="modeKnown" class="workspace-grid" :class="{ 'hardware-workspace': hardwareMode }">
          <section v-if="hardwareMode" class="panel hardware-map" aria-labelledby="hardware-map-heading">
            <div class="panel-heading">
              <div><h2 id="hardware-map-heading">地图显示</h2><span class="panel-caption">FastLIO 实时建图 · 现场点云与机器人位姿</span></div>
              <button class="data-valve" :class="{ on: mappingValve }" type="button" :disabled="!!fieldBusy" data-testid="valve-mapping" @click="toggleDataValve('mapping')">{{ fieldBusy === 'data-mapping' ? '启动中…' : mappingValve ? '⏻ 开' : '⏻ 关' }}</button>
            </div>
            <MapPanel v-if="mappingValve" />
            <div v-else class="monitor-placeholder">雷达数据接入已关闭</div>
          </section>
          <section v-if="!hardwareMode" class="panel scene-panel"><div class="panel-heading"><div><h2>机房巡检场景</h2><span class="panel-caption">三维位姿与任务路线</span></div><span class="heading-actions"><span class="view-badge">3D 实时视图</span></span></div><RoomScene :robot="robot" :points="points" :progress="progress" /><div class="scene-status"><span><i :class="['status-dot', connected ? 'online' : 'offline']"></i> {{ connected ? '网关状态同步中' : '网关未连接' }}</span><span>最近同步 {{ lastSync || '—' }} <b>·</b> 模型为验证示意</span></div></section>
          <TeleopPanel v-if="!hardwareMode" :robot="robot" :hardware="hardwareMode" :telemetry-available="robotAvailable" :disabled-reason="teleopDisabledReason" @session-active="setTeleopSession" />
          <section v-if="modeKnown && !hardwareMode" class="panel demo-panel-sim" aria-labelledby="demo-sim-heading">
            <div class="panel-heading">
              <div><h2 id="demo-sim-heading">巡检演示（干跑）</h2><span class="panel-caption">路线编辑与执行器干跑 · 语音编排需真机模式</span></div>
            </div>
            <DemoPanel :hardware="false" />
          </section>
          <section v-if="hardwareMode" class="panel hardware-image" aria-labelledby="hardware-image-heading">
            <div class="panel-heading">
              <div><h2 id="hardware-image-heading">图像显示</h2><span class="panel-caption">{{ visionFrameNote }}</span></div>
              <button class="data-valve" :class="{ on: visionValve }" type="button" :disabled="!!fieldBusy" data-testid="valve-vision" @click="toggleDataValve('vision')">{{ fieldBusy === 'data-vision' ? '启动中…' : visionValve ? '⏻ 开' : '⏻ 关' }}</button>
            </div>
            <img v-if="visionValve && visionFrameUrl" class="camera-frame" :src="visionFrameUrl" alt="机器人摄像头实时画面（带检测框）" />
            <div v-else class="monitor-placeholder">{{ visionValve ? '等待相机画面接入' : '相机数据接入已关闭' }}</div>
          </section>
          <section v-if="hardwareMode" class="panel hardware-asr" aria-labelledby="hardware-asr-heading">
            <div class="panel-heading">
              <div><h2 id="hardware-asr-heading">语音识别</h2><span class="panel-caption">{{ asrNote }}</span></div>
              <button class="data-valve" :class="{ on: asrValve }" type="button" :disabled="!!fieldBusy" data-testid="valve-asr" @click="toggleDataValve('asr')">{{ fieldBusy === 'data-asr' ? '启动中…' : asrValve ? '⏻ 开' : '⏻ 关' }}</button>
            </div>
            <div v-if="asrValve" ref="asrLogEl" class="asr-log">
              <p v-for="(item, index) in asrItems" :key="item.id" :class="{ latest: index === asrItems.length - 1 }"><time>{{ new Date(item.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false }) }}</time>{{ item.text }}</p>
              <p v-if="!asrItems.length" class="asr-empty">等待语音输入 —— 对着机器人上的麦克风说话</p>
            </div>
            <div v-else class="monitor-placeholder">语音识别已关闭</div>
          </section>
        </div>

        <template v-if="modeKnown && !hardwareMode">
        <div class="results-grid">
          <div class="evidence-stack">
            <section class="panel results-panel"><div class="panel-heading"><div><h2>巡检数据与证据</h2><span class="panel-caption">原始采样关联任务与巡检点</span></div><div class="result-controls"><span class="count-pill">{{ samples.length }} 条记录</span><el-button size="small" :disabled="!selectedTask || !samples.length" @click="openReview">人工复核</el-button></div></div><div v-if="!samples.length" class="empty-state"><span class="empty-icon">▤</span><strong>等待巡检采集</strong><p>下发任务后，温湿度与图片证据将在这里逐点显示。</p></div><div v-else class="data-table-wrap"><table class="data-table"><thead><tr><th>巡检点</th><th>温度</th><th>湿度</th><th>图像证据</th><th>数据来源</th></tr></thead><tbody><tr v-for="sample in samples" :key="`${sample.point_id}-${sample.captured_at}`"><td><strong>{{ sample.label ?? points.find(p => p.id === sample.point_id)?.label ?? sample.point_id }}</strong><small>{{ sample.captured_at ? new Date(sample.captured_at).toLocaleTimeString('zh-CN', {hour12:false}) : sample.point_id }}</small></td><td>{{ sample.temperature_c.toFixed(1) }} <span>℃</span></td><td>{{ sample.humidity_percent.toFixed(1) }} <span>%RH</span></td><td><a v-if="sample.image_url" :href="sample.image_url" target="_blank" rel="noreferrer" class="image-evidence"><img :src="sample.image_url" :alt="`${sample.label ?? sample.point_id}合成图像`" /><span>查看图片 ↗</span></a><span v-else class="muted">暂无图片</span></td><td><span class="synthetic-tag">合成数据</span></td></tr></tbody></table></div><div v-if="selectedTask?.review_note" class="review-record"><span>✓ 人工复核</span><p>{{ selectedTask.review_note }}</p></div></section>
            <section class="panel trend-panel"><div class="panel-heading"><div><h2>环境采样趋势</h2><span class="panel-caption">当前任务 · 按巡检点</span></div></div><div v-if="!samples.length" class="empty-state chart-empty"><span class="empty-chart">╱╲╱</span><p>完成采样后显示环境趋势</p></div><EnvironmentChart v-else :samples="samples" /><div class="trend-note"><span class="mini-dot"></span> 温湿度均由模拟器生成，用于验证数据链路。</div></section>
          </div>
          <section class="panel task-panel"><div class="panel-heading"><div><h2>巡检任务</h2><span class="panel-caption">{{ selectedTask ? "新任务默认三点 · 下方为当前任务路线" : "新任务默认三点路线 · 逐点模拟采集" }}</span></div><span class="step-pill">{{ String(points.length).padStart(2, "0") }} 个点</span></div><div class="task-body"><label class="field-label" for="task-name">新任务名称</label><el-input id="task-name" v-model="taskName" maxlength="80" :disabled="active || robotBusy || submitting || teleopLocked" placeholder="输入验证任务名称" /><div class="route-title">{{ selectedTask ? "当前任务路线" : "默认验证路线" }}<span>温湿度 / 图像证据</span></div><ol class="route-list"><li v-for="(point, index) in points" :key="point.id" :class="{done: samples.some(s => s.point_id === point.id)}"><span class="route-number">{{ samples.some(s => s.point_id === point.id) ? '✓' : String(index + 1).padStart(2, '0') }}</span><div><strong>{{ point.label }}</strong><span>目标位置 {{ point.x.toFixed(1) }}, {{ point.y.toFixed(1) }} m</span></div><span class="route-check">{{ samples.some(s => s.point_id === point.id) ? '已采集' : '待巡检' }}</span></li></ol><div class="task-current"><span>当前任务</span><strong :title="selectedTask?.id">{{ selectedTask ? selectedTask.name : '尚未创建验证任务' }}</strong><div v-if="selectedTask" class="task-id">{{ selectedTask.id }}</div></div><div class="task-actions"><el-button type="primary" :loading="submitting" :disabled="!connected || active || robotBusy || teleopLocked" data-testid="start-task" @click="createTask"><span class="button-glyph">▷</span>{{ submitting ? '正在下发' : '下发默认三点巡检' }}</el-button><el-button :loading="stopping" :disabled="!connected || !(active || robotBusy)" class="stop-button" data-testid="stop-task" @click="stopTask">■ 停止任务</el-button></div><p class="task-hint">{{ teleopLocked ? "遥控控制权归还后可下发巡检。" : "停止操作等待网关确认，执行状态以机器人反馈为准。" }}</p></div></section>
        </div>

        <section class="panel report-panel"><div class="report-intro"><span class="report-icon">▥</span><div><h2>结构化巡检报告</h2><p>汇总任务、检测数据、图片证据及复核记录。</p></div></div><div class="report-actions"><a v-for="report in reports" :key="report.format" :href="report.url" class="download-link" :download="true">↓ {{ report.format.toUpperCase() }}</a><el-button type="primary" plain :disabled="!selectedTask || active || !samples.length" :loading="reporting" data-testid="generate-reports" @click="createReports">{{ reporting ? '报告生成中' : '生成验证报告' }}</el-button></div></section>

        <section v-if="tasks.length" class="panel history-panel"><div class="panel-heading"><div><h2>验证任务记录</h2><span class="panel-caption">任务状态与数据持久化</span></div><span class="count-pill">{{ tasks.length }} 项</span></div><div class="history-list"><button v-for="task in tasks.slice(0, 8)" :key="task.id" :class="['history-item', {selected: selectedTask?.id === task.id}]" @click="selectTask(task)"><span class="history-name">{{ task.name }}<small>{{ task.id }}</small></span><span>{{ displayTime(task.created_at) }}</span><span class="history-status">{{ stateNames[task.status] ?? task.status }}</span><span>查看 →</span></button></div></section>
        </template>
        <footer v-if="!hardwareMode" class="page-footer"><span>H2 巡检运维 · 技术验证环境</span><span>Vue 3 <i>·</i> FastAPI <i>·</i> PostgreSQL <i>·</i> gRPC</span></footer>
      </div>
    </main>
    <el-dialog v-if="!hardwareMode" v-model="reviewOpen" title="人工复核" width="520px" :close-on-click-modal="false"><p class="review-description">为当前任务记录复核意见，原始采样数据将保留。</p><el-input v-model="reviewNote" type="textarea" :rows="5" maxlength="2000" show-word-limit placeholder="例如：已核对三个巡检点的合成采样和图片，数据关联正确。" aria-label="复核意见" /><template #footer><el-button @click="reviewOpen = false">取消</el-button><el-button type="primary" :disabled="!reviewNote.trim()" :loading="savingReview" @click="saveReview">保存复核</el-button></template></el-dialog>
  </div>
</template>

<style scoped>
/* 真机模式紧凑化：侧栏收窄、顶部状态收成一行，把纵向空间让给地图/图像/语音/演示。
   语音转写为通高右栏，最新一句大字显示。 */
/* 侧栏抽屉：默认收起（滑出屏幕），把手按钮手动呼出；呼出时主区随之让出 240px。
   只在真机模式宽屏生效；visibility 延迟切换保证收起动画完成后内容不可聚焦。 */
@media (min-width: 1201px) {
  .hardware-sidebar { width: 240px; transform: translateX(-100%); visibility: hidden; transition: transform .25s ease, visibility 0s linear .25s; }
  .hardware-drawer-open .hardware-sidebar { transform: translateX(0); visibility: visible; transition: transform .25s ease; }
  .hardware-shell > .main-content { margin-left: 0; width: 100%; transition: margin-left .25s ease, width .25s ease; }
  .hardware-drawer-open > .main-content { margin-left: 240px; width: calc(100% - 240px); }
}
.hardware-page { padding-top: 12px; }
/* 侧栏链路卡：指示灯与恢复按钮从主区移入，沿用深色卡片语言 */
.sidebar-field { flex-shrink: 0; margin: 8px -6px 0; padding: 12px 13px; background: rgba(255, 255, 255, .04); border: 1px solid rgba(255, 255, 255, .1); border-top: 3px solid rgba(105, 180, 176, .55); border-radius: 8px; }
.sidebar-field-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.sidebar-field-title { color: #e6eef5; font-size: 13px; font-weight: 600; }
/* 主区状态带：连接、运动服务、电量、温湿度和视觉识别一行内联摘要。 */
.status-strip { display: flex; align-items: center; margin-bottom: 14px; padding: 8px 6px; border: 1px solid #e4eaf0; border-radius: 8px; background: #fff; box-shadow: 0 2px 4px #283e5a02; overflow: hidden; }
.status-item { display: flex; align-items: center; gap: 7px; min-width: 0; padding: 0 15px; white-space: nowrap; }
.status-item + .status-item { border-left: 1px solid #eef2f5; }
.status-item label { flex-shrink: 0; color: #7f8c98; font-size: 11px; }
.status-item strong { color: #263e4b; font-size: 14px; font-weight: 600; letter-spacing: -.2px; }
.status-item strong i { font-style: normal; font-size: 10px; color: #9ba9b3; margin-left: 1px; font-weight: 400; }
.status-item small { min-width: 0; overflow: hidden; text-overflow: ellipsis; color: #94a0aa; font-size: 10px; }
.mini-battery { flex-shrink: 0; width: 56px; height: 4px; border-radius: 3px; background: #eff3f5; overflow: hidden; }
.mini-battery span { display: block; height: 100%; border-radius: 3px; background: #7eb9a4; max-width: 100%; }
/* 状态带右端：固定路线（多点测试-12步）的准备/演示快捷按钮 */
.status-actions { margin-left: auto; flex-shrink: 0; display: inline-flex; align-items: center; gap: 8px; padding-left: 15px; border-left: 1px solid #eef2f5; }
.strip-btn { border: 1px solid #c9dedf; border-radius: 999px; background: #f4fafa; color: #277e80; padding: 5px 12px; font-size: 11px; font-weight: 600; cursor: pointer; white-space: nowrap; }
.strip-btn:hover:not(:disabled) { border-color: #16888b; }
.strip-btn:disabled { opacity: .5; cursor: not-allowed; }
.strip-start:not(:disabled) { border-color: #35a0a4; color: #0f6f72; background: #e8f6f6; }
.strip-stop { border-color: #c0392b; color: #c0392b; background: #fff; }
/* 工作区三列：地图/图像/语音转写通高并排；演示的控制与进度在侧栏，不再占主区。
   行随视口高度伸缩（min-content 下限保证最小可读尺寸）。 */
.hardware-workspace {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(280px, 320px);
  /* 行下限必须为 0：若用 min-content，转写内容会把整行（连同地图/图像面板）持续撑高。 */
  grid-template-rows: minmax(0, 1fr);
  grid-template-areas: 'map image asr';
  gap: 12px;
  margin-bottom: 12px;
  /* 76px ≈ 页首留白 12 + 状态带 38 与下边距 14、工作区下边距 12。高度定死为一屏，
     转写超出后在 .asr-log 内部滚动，页面保持无滚动。 */
  height: calc(100vh - 76px);
}
.hardware-map { grid-area: map; }
.hardware-image { grid-area: image; }
.hardware-asr { grid-area: asr; }
.hardware-workspace .panel-heading { min-height: 0; padding: 10px 16px 9px; }
/* 地图与相机画面填满所在行；数据未接入时占位块同高。 */
.hardware-map, .hardware-image, .hardware-asr { display: flex; flex-direction: column; }
.hardware-map :deep(.map-panel) { display: flex; flex-direction: column; flex: 1; }
.hardware-map :deep(.map-canvas) { flex: 1; height: auto; min-height: clamp(200px, 26vh, 420px); }
.hardware-image .camera-frame { flex: 1; height: auto; max-height: none; min-height: clamp(200px, 26vh, 420px); }
.hardware-workspace .monitor-placeholder { flex: 1; min-height: clamp(140px, 22vh, 320px); }
/* 语音转写：填满通高栏；时间列弱化，最新一句加大加粗便于现场跟读。 */
.asr-log { flex: 1; min-height: 0; overflow-y: auto; padding: 10px 14px 10px 4px; }
.asr-log p { margin: 0 0 10px; line-height: 1.55; color: #2c3f4d; font-size: 13px; }
.asr-log p.latest { color: #16303f; font-size: 16px; font-weight: 600; }
.asr-log time { color: #93a7b4; font-size: 11px; margin-right: 10px; font-variant-numeric: tabular-nums; }
.asr-empty { color: #93a7b4; }
@media (max-width: 800px) {
  /* 窄屏堆叠布局恢复自然高度，不锁一屏。 */
  .hardware-workspace { grid-template-columns: minmax(0, 1fr); grid-template-areas: 'map' 'image' 'asr'; height: auto; }
}
.hardware-sidebar .brand { padding: 0 4px 8px; font-size: 16px; letter-spacing: .4px; }
.sidebar-teleop { flex-shrink: 0; margin: 10px -6px 0; border-radius: 8px; }
.sidebar-teleop.lease-held { outline: 2px solid rgba(232, 137, 107, .7); outline-offset: 2px; }
.sidebar-demo { flex-shrink: 0; margin: 8px -6px 0; background: rgba(255, 255, 255, .04); border: 1px solid rgba(255, 255, 255, .1); border-top: 3px solid rgba(105, 180, 176, .55); border-radius: 8px; }
/* 顶栏左组：面包屑（仅模拟模式渲染顶栏） */
.topbar-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
/* 侧栏抽屉把手：悬浮在屏幕左上角的空白沟槽内；呼出后右移停靠在侧栏右缘，水平三条杠 */
.sidebar-toggle {
  position: fixed;
  top: 10px;
  left: 8px;
  z-index: 5;
  width: 26px;
  height: 30px;
  padding: 0;
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  border: 1px solid #c9d6de;
  border-radius: 6px;
  background: #fff;
  box-shadow: 0 1px 3px rgba(32, 49, 63, .12);
  cursor: pointer;
  transition: left .25s ease;
}
.sidebar-toggle.open { left: 246px; }
.sidebar-toggle i { display: block; width: 13px; height: 2px; background: #4e6675; margin: 2px 0; border-radius: 1px; }
.sidebar-toggle:hover:not(:disabled) { border-color: #8ec4c2; }
.sidebar-toggle.open { background: #16888b; border-color: #16888b; }
.sidebar-toggle.open i { background: #fff; }
.sidebar-toggle:disabled { opacity: .5; cursor: not-allowed; }
@media (max-width: 1200px) {
  /* 窄屏仍用常驻侧栏的响应式规则，抽屉把手无意义 */
  .sidebar-toggle { display: none; }
}
/* 数据阀门开关：电源式圆钮，放在各数据面板标题栏右侧 */
.data-valve { flex-shrink: 0; border: 1px solid #b7c6cf; background: #fff; color: #6a7f8d; border-radius: 999px; padding: 5px 14px; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; }
.data-valve.on { border-color: #35a0a4; color: #0f6f72; background: #e8f6f6; }
.data-valve:disabled { opacity: .5; cursor: not-allowed; }
/* 指示灯与手动恢复按钮：随链路卡移入侧栏后的深色排布 */
.field-lights { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 10px; }
.field-light { display: flex; align-items: center; gap: 6px; color: #7d92a3; font-size: 11px; white-space: nowrap; min-width: 0; }
.field-light i { flex-shrink: 0; width: 8px; height: 8px; border-radius: 50%; background: rgba(255, 255, 255, .18); }
.field-light.on { color: #cfe0ea; }
.field-light.on i { background: #35a18c; box-shadow: 0 0 5px rgba(53, 161, 140, .5); }
.field-actions { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 8px; }
.field-button { padding: 6px 9px; border: 1px solid rgba(255, 255, 255, .14); border-radius: 6px; background: rgba(255, 255, 255, .05); color: #9fd8d4; cursor: pointer; font-size: 11px; white-space: nowrap; }
.field-button:hover:not(:disabled) { border-color: rgba(53, 161, 140, .6); }
.field-button:disabled { opacity: .5; cursor: not-allowed; }
.field-message { display: block; color: #9fd8d4; font-size: 10px; overflow-wrap: anywhere; margin-top: 6px; }
.heading-actions { display: inline-flex; align-items: center; gap: 8px; }
/* 预览帧按原始比例缩放显示，深色底与检测框画面协调。 */
.camera-frame { display: block; width: 100%; max-height: 46vh; object-fit: contain; border-radius: 6px; background: #0d141a; }
.monitor-placeholder {
  display: grid;
  place-items: center;
  min-height: clamp(144px, 16vw, 248px);
  padding: 16px;
  background-color: #f2f6f8;
  color: #536b7b;
  font-size: 12px;
  text-align: center;
}
.map-placeholder {
  background-image: linear-gradient(#e4ecf1 1px, transparent 1px),
    linear-gradient(90deg, #e4ecf1 1px, transparent 1px);
  background-position: center;
  background-size: 24px 24px;
}
</style>
