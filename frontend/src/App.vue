<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import RoomScene from './components/RoomScene.vue'
import EnvironmentChart from './components/EnvironmentChart.vue'
import TeleopPanel from './components/TeleopPanel.vue'
import { errorMessage, request, type Report, type RobotState, type RoutePoint, type Task } from './api'

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
const connected = computed(() => serviceAvailable.value && robotAvailable.value)
const lastSync = ref('')
const serviceError = ref('')
const robotError = ref('')
const connectionError = computed(() => serviceError.value || robotError.value)
const serviceLoading = ref(true)
const robotLoading = ref(true)
const loading = computed(() => serviceLoading.value || robotLoading.value)
const teleopSession = ref(false)
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
const teleopDisabledReason = computed(() => !connected.value ? '等待服务和机器人连接' : active.value || robotBusy.value || submitting.value || stopping.value ? '巡检任务处理中，结束后可申请遥控' : '')
const progress = computed(() => Math.max(0, Math.min(100, selectedTask.value?.progress ?? 0)))
const points = computed(() => selectedTask.value?.points?.length ? selectedTask.value.points : defaultPoints)
const samples = computed(() => selectedTask.value?.samples ?? [])
const latestSample = computed(() => samples.value.at(-1))
const reports = computed(() => selectedTask.value?.reports ?? [])
const battery = computed(() => robot.value?.battery_percent ?? robot.value?.battery)
const stateNames: Record<string, string> = { idle: '待命', ready: '待命', pending_dispatch: '等待派发', accepted: '任务已接收', running: '巡检中', completed: '已完成', stopped: '已停止', interrupted: '执行中断', failed: '执行失败', rejected: '未接受', expired: '已过期', dispatch_expired: '派发已过期', stopping: '正在停止', offline: '离线', teleop: '手动遥控' }
const taskState = computed(() => selectedTask.value ? stateNames[selectedTask.value.status] ?? selectedTask.value.status : '等待下发')
const stateTone = computed(() => selectedTask.value?.status === 'completed' ? 'success' : active.value ? 'primary' : selectedTask.value ? 'warning' : 'info')

function displayTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

async function sync() {
  try {
    const [health, rawTasks] = await Promise.all([
      request<Record<string, unknown>>('/api/health'),
      request<Task[] | { tasks: Task[] }>('/api/tasks'),
    ])
    if (disposed) return
    tasks.value = Array.isArray(rawTasks) ? rawTasks : rawTasks.tasks ?? []
    serviceAvailable.value = health.status !== 'error'
    serviceError.value = ''
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

// One serialized robot poller owns the displayed pose, independent of task
// requests. A slower task/health response can never overwrite a new pose.
async function pollRobot() {
  if (disposed || robotRequest) return
  clearTimeout(robotPollTimer)
  const controller = new AbortController()
  robotRequest = controller
  try {
    const raw = await request<RobotState & { state?: RobotState }>('/api/robot', {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(1600)]),
    })
    if (disposed) return
    robot.value = raw.state ?? raw
    robotAvailable.value = robot.value.connected !== false
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
  if (!connected.value || active.value || robotBusy.value || teleopLocked.value || submitting.value) return
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
  clearInterval(clockTimer)
  robotRequest?.abort()
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="/" aria-label="H2巡检技术验证首页"><span class="brand-icon">H<span>2</span></span><span>巡检运维<span class="brand-sub">INSPECTION CONSOLE</span></span></a>
      <div class="workspace-label">验证工作区</div>
      <div class="nav-item active"><span class="nav-symbol">▦</span> 巡检验证台 <span class="nav-selected"></span></div>
      <div class="sidebar-section">验证链路</div>
      <div class="chain-item"><span class="chain-num">01</span><span>任务编排与下发</span></div>
      <div class="chain-item"><span class="chain-num">02</span><span>机器人执行反馈</span></div>
      <div class="chain-item"><span class="chain-num">03</span><span>采集数据与复核</span></div>
      <div class="chain-item"><span class="chain-num">04</span><span>报告生成与导出</span></div>
      <div class="sidebar-foot"><span class="platform-pill">x86_64 / Ubuntu</span><p>本地技术验证环境</p><small>SIMULATED ROBOT · v0.1</small></div>
    </aside>

    <main class="main-content">
      <header class="topbar"><div class="breadcrumb">巡检管理 <span>/</span> 技术验证台</div><div class="topbar-right"><span class="env-tag">开发验证</span><time>{{ clock.toLocaleString('zh-CN', { hour12: false }) }}</time><span class="avatar">验</span></div></header>
      <div class="page-content">
        <div class="page-heading"><div><div class="eyebrow">ROBOT INSPECTION / PROOF OF CONCEPT</div><h1>巡检技术验证台</h1><p>连接任务、机器人与巡检证据，验证完整业务链路。</p></div><button class="api-link" type="button" @click="refreshAll"><span>↻</span> 刷新状态</button></div>
        <div class="validation-banner"><span class="banner-icon">i</span><div><strong>模拟机器人 · 合成数据</strong><span>当前用于软件技术验证，巡检数据不代表真实机房状态。</span></div><span class="banner-label">TECHNICAL VALIDATION</span></div>
        <div v-if="connectionError" class="connection-alert" role="alert"><strong>服务连接异常</strong><span>{{ connectionError }}</span><button @click="refreshAll">重新连接</button></div>

        <section class="metrics" aria-label="机器人与任务概况">
          <div class="metric-card"><div class="metric-top"><span>机器人连接</span><span class="metric-icon">⌁</span></div><div class="metric-value connection-value"><span :class="['status-dot', connected ? 'online' : 'offline']"></span>{{ loading ? '连接中' : connected ? '已连接' : '未连接' }}</div><div class="metric-foot">{{ connected ? (robot?.robot_id ?? 'H2 模拟机器人') : '等待业务服务和网关' }}</div></div>
          <div class="metric-card"><div class="metric-top"><span>机器人电量</span><span class="metric-icon battery-icon">▱</span></div><div class="metric-value">{{ battery === undefined ? '—' : battery.toFixed(0) }}<span class="metric-unit">%</span><span class="small-status">{{ robot?.status ? stateNames[robot.status] ?? robot.status : '状态未知' }}</span></div><div class="battery-track"><span :style="{width: `${battery ?? 0}%`}"></span></div></div>
          <div class="metric-card"><div class="metric-top"><span>任务完成进度</span><span class="metric-icon">◎</span></div><div class="metric-value">{{ progress.toFixed(0) }}<span class="metric-unit">%</span><el-tag size="small" :type="stateTone" effect="light">{{ taskState }}</el-tag></div><div class="metric-foot">已采集 {{ samples.length }} / {{ points.length }} 个巡检点</div></div>
          <div class="metric-card"><div class="metric-top"><span>最近环境采样</span><span class="metric-icon">♧</span></div><div class="sample-values"><strong>{{ latestSample?.temperature_c?.toFixed(1) ?? '—' }}<span>℃</span></strong><i></i><strong>{{ latestSample?.humidity_percent?.toFixed(1) ?? '—' }}<span>%RH</span></strong></div><div class="metric-foot">{{ latestSample ? `${latestSample.label ?? latestSample.point_id} · 合成采样` : '任务执行后显示采集结果' }}</div></div>
        </section>

        <div class="workspace-grid">
          <section class="panel scene-panel"><div class="panel-heading"><div><h2>机房巡检场景</h2><span class="panel-caption">三维位姿与任务路线</span></div><span class="view-badge">3D 实时视图</span></div><RoomScene :robot="robot" :points="points" :progress="progress" /><div class="scene-status"><span><i :class="['status-dot', connected ? 'online' : 'offline']"></i> {{ connected ? '网关状态同步中' : '网关未连接' }}</span><span>最近同步 {{ lastSync || '—' }} <b>·</b> 模型为验证示意</span></div></section>
          <TeleopPanel :robot="robot" :telemetry-available="robotAvailable" :disabled-reason="teleopDisabledReason" @session-active="setTeleopSession" />
        </div>

        <div class="results-grid">
          <div class="evidence-stack">
            <section class="panel results-panel"><div class="panel-heading"><div><h2>巡检数据与证据</h2><span class="panel-caption">原始采样关联任务与巡检点</span></div><div class="result-controls"><span class="count-pill">{{ samples.length }} 条记录</span><el-button size="small" :disabled="!selectedTask || !samples.length" @click="openReview">人工复核</el-button></div></div><div v-if="!samples.length" class="empty-state"><span class="empty-icon">▤</span><strong>等待巡检采集</strong><p>下发任务后，温湿度与图片证据将在这里逐点显示。</p></div><div v-else class="data-table-wrap"><table class="data-table"><thead><tr><th>巡检点</th><th>温度</th><th>湿度</th><th>图像证据</th><th>数据来源</th></tr></thead><tbody><tr v-for="sample in samples" :key="`${sample.point_id}-${sample.captured_at}`"><td><strong>{{ sample.label ?? points.find(p => p.id === sample.point_id)?.label ?? sample.point_id }}</strong><small>{{ sample.captured_at ? new Date(sample.captured_at).toLocaleTimeString('zh-CN', {hour12:false}) : sample.point_id }}</small></td><td>{{ sample.temperature_c.toFixed(1) }} <span>℃</span></td><td>{{ sample.humidity_percent.toFixed(1) }} <span>%RH</span></td><td><a v-if="sample.image_url" :href="sample.image_url" target="_blank" rel="noreferrer" class="image-evidence"><img :src="sample.image_url" :alt="`${sample.label ?? sample.point_id}合成图像`" /><span>查看图片 ↗</span></a><span v-else class="muted">暂无图片</span></td><td><span class="synthetic-tag">合成数据</span></td></tr></tbody></table></div><div v-if="selectedTask?.review_note" class="review-record"><span>✓ 人工复核</span><p>{{ selectedTask.review_note }}</p></div></section>
            <section class="panel trend-panel"><div class="panel-heading"><div><h2>环境采样趋势</h2><span class="panel-caption">当前任务 · 按巡检点</span></div></div><div v-if="!samples.length" class="empty-state chart-empty"><span class="empty-chart">╱╲╱</span><p>完成采样后显示环境趋势</p></div><EnvironmentChart v-else :samples="samples" /><div class="trend-note"><span class="mini-dot"></span> 温湿度均由模拟器生成，用于验证数据链路。</div></section>
          </div>
          <section class="panel task-panel"><div class="panel-heading"><div><h2>巡检任务</h2><span class="panel-caption">{{ selectedTask ? "新任务默认三点 · 下方为当前任务路线" : "新任务默认三点路线 · 逐点模拟采集" }}</span></div><span class="step-pill">{{ String(points.length).padStart(2, "0") }} 个点</span></div><div class="task-body"><label class="field-label" for="task-name">新任务名称</label><el-input id="task-name" v-model="taskName" maxlength="80" :disabled="active || robotBusy || submitting || teleopLocked" placeholder="输入验证任务名称" /><div class="route-title">{{ selectedTask ? "当前任务路线" : "默认验证路线" }}<span>温湿度 / 图像证据</span></div><ol class="route-list"><li v-for="(point, index) in points" :key="point.id" :class="{done: samples.some(s => s.point_id === point.id)}"><span class="route-number">{{ samples.some(s => s.point_id === point.id) ? '✓' : String(index + 1).padStart(2, '0') }}</span><div><strong>{{ point.label }}</strong><span>目标位置 {{ point.x.toFixed(1) }}, {{ point.y.toFixed(1) }} m</span></div><span class="route-check">{{ samples.some(s => s.point_id === point.id) ? '已采集' : '待巡检' }}</span></li></ol><div class="task-current"><span>当前任务</span><strong :title="selectedTask?.id">{{ selectedTask ? selectedTask.name : '尚未创建验证任务' }}</strong><div v-if="selectedTask" class="task-id">{{ selectedTask.id }}</div></div><div class="task-actions"><el-button type="primary" :loading="submitting" :disabled="!connected || active || robotBusy || teleopLocked" data-testid="start-task" @click="createTask"><span class="button-glyph">▷</span>{{ submitting ? '正在下发' : '下发默认三点巡检' }}</el-button><el-button :loading="stopping" :disabled="!connected || !(active || robotBusy)" class="stop-button" data-testid="stop-task" @click="stopTask">■ 停止任务</el-button></div><p class="task-hint">{{ teleopLocked ? "遥控控制权归还后可下发巡检。" : "停止操作等待网关确认，执行状态以机器人反馈为准。" }}</p></div></section>
        </div>

        <section class="panel report-panel"><div class="report-intro"><span class="report-icon">▥</span><div><h2>结构化巡检报告</h2><p>汇总任务、检测数据、图片证据及复核记录。</p></div></div><div class="report-actions"><a v-for="report in reports" :key="report.format" :href="report.url" class="download-link" :download="true">↓ {{ report.format.toUpperCase() }}</a><el-button type="primary" plain :disabled="!selectedTask || active || !samples.length" :loading="reporting" data-testid="generate-reports" @click="createReports">{{ reporting ? '报告生成中' : '生成验证报告' }}</el-button></div></section>

        <section v-if="tasks.length" class="panel history-panel"><div class="panel-heading"><div><h2>验证任务记录</h2><span class="panel-caption">任务状态与数据持久化</span></div><span class="count-pill">{{ tasks.length }} 项</span></div><div class="history-list"><button v-for="task in tasks.slice(0, 8)" :key="task.id" :class="['history-item', {selected: selectedTask?.id === task.id}]" @click="selectTask(task)"><span class="history-name">{{ task.name }}<small>{{ task.id }}</small></span><span>{{ displayTime(task.created_at) }}</span><span class="history-status">{{ stateNames[task.status] ?? task.status }}</span><span>查看 →</span></button></div></section>
        <footer class="page-footer"><span>H2 巡检运维 · 技术验证环境</span><span>Vue 3 <i>·</i> FastAPI <i>·</i> PostgreSQL <i>·</i> gRPC</span></footer>
      </div>
    </main>
    <el-dialog v-model="reviewOpen" title="人工复核" width="520px" :close-on-click-modal="false"><p class="review-description">为当前任务记录复核意见，原始采样数据将保留。</p><el-input v-model="reviewNote" type="textarea" :rows="5" maxlength="2000" show-word-limit placeholder="例如：已核对三个巡检点的合成采样和图片，数据关联正确。" aria-label="复核意见" /><template #footer><el-button @click="reviewOpen = false">取消</el-button><el-button type="primary" :disabled="!reviewNote.trim()" :loading="savingReview" @click="saveReview">保存复核</el-button></template></el-dialog>
  </div>
</template>
