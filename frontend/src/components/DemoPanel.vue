<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { errorMessage, request, type DemoRoute, type DemoState, type DemoStep, type DemoStepType } from '../api'

/**
 * 巡检演示面板：路线编辑 + 演示开关（语音触发，仅真机）+ 手动执行 + 进度。
 * 自轮询 /api/demo/state，与 MapPanel 一样自包含；演示占用运动租约时
 * 通知父组件禁用手动遥控（单一租约互斥的界面侧提示）。
 */
const props = defineProps<{ hardware: boolean }>()
const emit = defineEmits<{ activeChange: [active: boolean] }>()

interface DraftStep { type: DemoStepType; value: number; read_aloud: boolean }

const routes = ref<DemoRoute[]>([])
const selectedId = ref('')
const state = ref<DemoState | null>(null)
const busy = ref('')
const message = ref('')
const editing = ref(false)
const draftName = ref('')
const draftSteps = ref<DraftStep[]>([])

const selected = computed(() => routes.value.find(route => route.id === selectedId.value) ?? null)
const phase = computed(() => state.value?.phase ?? 'idle')
const demoBusy = computed(() => ['starting', 'armed', 'running'].includes(phase.value))
const phaseNames: Record<string, string> = {
  idle: '未启动', starting: '拉起服务', armed: '待命听令', running: '执行中', done: '已完成', aborted: '已中止',
}

watch(demoBusy, active => emit('activeChange', active))
watch(phase, (next, previous) => {
  // 演示开关关闭后保留终态（done/aborted）供操作员查看原因，不自动清零
  if (previous && previous !== 'idle' && next === 'idle') message.value = ''
})

let stateTimer: number | undefined

async function pollState() {
  try {
    state.value = await request<DemoState>('/api/demo/state', { cache: 'no-store' })
  } catch { /* 状态查询失败沿用上一帧 */ }
}

async function loadRoutes() {
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

function stepText(step: DemoStep): string {
  if (step.type === 'forward') return `前进 ${step.value} m`
  if (step.type === 'turn') return `${step.value >= 0 ? '左转' : '右转'} ${Math.abs(step.value)}°`
  return `停留 ${step.value} s${step.read_aloud ? ' · 朗读识别' : ''}`
}

/** 打开演示开关：后端依次拉服务、播问候语并武装语音触发（仅真机模式）。 */
async function startDemo() {
  if (!selected.value || busy.value) return
  busy.value = 'start'
  message.value = ''
  try {
    await request<{ ok: boolean }>('/api/demo/start', {
      method: 'POST', body: JSON.stringify({ route_id: selected.value.id }),
    })
    message.value = '演示已开启：机器人播报问候语后等待「开始」口令'
    void pollState()
  } catch (error) {
    message.value = `失败：${errorMessage(error)}`
  } finally {
    busy.value = ''
  }
}

async function stopDemo() {
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
async function runManual() {
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

function beginEdit() {
  const source = selected.value
  draftName.value = source ? `${source.name}副本` : '新路线'
  draftSteps.value = source
    ? source.steps.map(step => ({ type: step.type, value: step.value, read_aloud: step.read_aloud === true }))
    : [{ type: 'forward', value: 0.3, read_aloud: false }]
  editing.value = true
}

function toggleEdit() {
  if (editing.value) {
    editing.value = false
    return
  }
  beginEdit()
}

function addStep() {
  draftSteps.value.push({ type: 'forward', value: 0.3, read_aloud: false })
}

function removeStep(index: number) {
  draftSteps.value.splice(index, 1)
}

function moveStep(index: number, offset: -1 | 1) {
  const target = index + offset
  if (target < 0 || target >= draftSteps.value.length) return
  const steps = draftSteps.value
  ;[steps[index], steps[target]] = [steps[target], steps[index]]
}

async function saveRoute() {
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
          ...(step.type === 'wait' && step.read_aloud ? { read_aloud: true } : {}),
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

async function deleteRoute() {
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

onMounted(() => {
  void loadRoutes()
  void pollState()
  stateTimer = window.setInterval(() => { void pollState() }, 2000)
})

onBeforeUnmount(() => {
  if (stateTimer !== undefined) window.clearInterval(stateTimer)
})
</script>

<template>
  <div class="demo-panel">
    <div class="demo-toolbar">
      <select v-model="selectedId" class="demo-select" data-testid="demo-route-select" aria-label="选择路线" :disabled="demoBusy || editing">
        <option v-for="route in routes" :key="route.id" :value="route.id">{{ route.name }}（{{ route.steps.length }} 步）</option>
      </select>
      <button v-if="props.hardware && !demoBusy" class="data-valve" type="button" data-testid="demo-start"
              :disabled="!selected || !!busy" @click="startDemo">⏻ 巡检演示</button>
      <button v-else-if="demoBusy" class="demo-stop" type="button" data-testid="demo-stop"
              :disabled="!!busy" @click="stopDemo">■ 停止演示</button>
      <button class="demo-run" type="button" data-testid="demo-run"
              :disabled="!selected || !!busy || demoBusy || editing" @click="runManual">手动执行</button>
      <button class="demo-edit" type="button" data-testid="demo-edit" :disabled="demoBusy" @click="toggleEdit">{{ editing ? '收起编辑' : '编辑路线' }}</button>
    </div>

    <div v-if="editing" class="demo-editor">
      <label class="demo-field">名称 <input v-model="draftName" class="demo-input" maxlength="100" placeholder="路线名称" /></label>
      <div v-for="(step, index) in draftSteps" :key="index" class="demo-step-row">
        <select v-model="step.type" class="demo-select demo-step-type" :aria-label="`第 ${index + 1} 步类型`">
          <option value="forward">前进 m</option>
          <option value="turn">转向 °（左正右负）</option>
          <option value="wait">停留 s</option>
        </select>
        <input v-model.number="step.value" class="demo-input demo-step-value" type="number" step="0.1" :aria-label="`第 ${index + 1} 步数值`" />
        <label v-if="step.type === 'wait'" class="demo-aloud"><input v-model="step.read_aloud" type="checkbox" />朗读</label>
        <span class="demo-step-ops">
          <button type="button" :disabled="index === 0" @click="moveStep(index, -1)">↑</button>
          <button type="button" :disabled="index === draftSteps.length - 1" @click="moveStep(index, 1)">↓</button>
          <button type="button" @click="removeStep(index)">✕</button>
        </span>
      </div>
      <div class="demo-editor-ops">
        <button type="button" @click="addStep">+ 添加步骤</button>
        <button type="button" class="demo-save" :disabled="!!busy || !draftSteps.length" @click="saveRoute">保存为新路线</button>
      </div>
      <p class="demo-hint">约束：前进 0.05–5 m · 转向 ±180°（左转为正）· 停留 0.5–60 s；保存为新路线，不影响原路线。</p>
    </div>

    <div v-if="selected" class="demo-progress" data-testid="demo-progress">
      <div class="demo-phase">
        <span :class="['demo-phase-badge', phase]">{{ phaseNames[phase] ?? phase }}</span>
        <span v-if="state?.route && state.route.name !== selected.name" class="demo-phase-route">执行中：{{ state.route.name }}</span>
        <span class="demo-phase-detail">{{ state?.detail || '编排层只描述距离/角度/时长，速度与闭环由执行器管理' }}</span>
      </div>
      <ol class="demo-steps">
        <li v-for="(step, index) in selected.steps" :key="index"
            :class="{ done: state && index < state.step_index, current: state && phase === 'running' && index === state.step_index }">
          <span class="step-number">{{ state && index < state.step_index ? '✓' : index + 1 }}</span>
          <span>{{ stepText(step) }}</span>
        </li>
        <li v-if="state && phase === 'done'" class="done"><span class="step-number">✓</span><span>全部完成</span></li>
      </ol>
      <p v-if="state?.error" class="demo-error">{{ state.error }}</p>
    </div>
    <div v-else class="demo-empty">尚无路线，点击「编辑路线」创建</div>

    <div class="demo-ops">
      <button v-if="selected && !demoBusy" type="button" class="demo-delete" data-testid="demo-delete"
              :disabled="!!busy || editing" @click="deleteRoute">删除该路线</button>
    </div>
    <p v-if="message" class="demo-message" role="status">{{ message }}</p>
    <p v-if="!props.hardware" class="demo-hint">模拟模式：语音演示编排不可用，可用「手动执行」做执行器干跑。</p>
  </div>
</template>

<style scoped>
.demo-panel { display: flex; flex-direction: column; gap: 10px; min-height: 120px; }
.demo-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.demo-select, .demo-input { border: 1px solid #c9d6de; border-radius: 6px; padding: 5px 8px; font-size: 12px; background: #fff; color: #22333f; }
.demo-select { max-width: 220px; }
.demo-run, .demo-edit, .demo-stop, .demo-save, .demo-delete, .demo-editor-ops button, .demo-step-ops button {
  border: 1px solid #c9d6de; border-radius: 6px; padding: 5px 10px; font-size: 12px; background: #fff; color: #2c3e4c; cursor: pointer;
}
.demo-stop { border-color: #c0392b; color: #c0392b; font-weight: 600; }
.demo-run { border-color: #2f6f8f; color: #2f6f8f; font-weight: 600; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
.demo-editor { border: 1px dashed #c9d6de; border-radius: 8px; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
.demo-field { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #4e6675; }
.demo-field .demo-input { flex: 1; }
.demo-step-row { display: flex; align-items: center; gap: 6px; }
.demo-step-type { flex: 1.4; }
.demo-step-value { width: 90px; }
.demo-aloud { display: inline-flex; align-items: center; gap: 3px; font-size: 12px; color: #4e6675; white-space: nowrap; }
.demo-step-ops { display: inline-flex; gap: 4px; margin-left: auto; }
.demo-step-ops button { padding: 3px 7px; }
.demo-editor-ops { display: flex; gap: 8px; }
.demo-hint, .demo-message { font-size: 11px; color: #6b7f8c; margin: 0; }
.demo-message { color: #2f6f8f; }
.demo-progress { display: flex; flex-direction: column; gap: 6px; }
.demo-phase { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.demo-phase-badge { border-radius: 10px; padding: 2px 10px; font-weight: 600; background: #edf3f6; color: #4e6675; }
.demo-phase-badge.running { background: #e8f1fb; color: #2f6f8f; }
.demo-phase-badge.aborted { background: #fbeae8; color: #c0392b; }
.demo-phase-badge.done { background: #e7f6ef; color: #1d7a5f; }
.demo-phase-badge.armed { background: #fdf3e3; color: #9a6b0f; }
.demo-phase-route { font-weight: 600; color: #22333f; }
.demo-phase-detail { color: #6b7f8c; }
.demo-steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; max-height: 180px; overflow-y: auto; }
.demo-steps li { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #4e6675; padding: 3px 6px; border-radius: 5px; }
.demo-steps li.done { color: #1d7a5f; }
.demo-steps li.current { background: #e8f1fb; color: #2f6f8f; font-weight: 600; }
.step-number { width: 18px; height: 18px; border-radius: 50%; border: 1px solid #c9d6de; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; flex: none; }
.demo-steps li.done .step-number { border-color: #1d7a5f; background: #1d7a5f; color: #fff; }
.demo-error { font-size: 12px; color: #c0392b; margin: 0; }
.demo-empty { font-size: 12px; color: #6b7f8c; padding: 12px 0; }
.demo-ops { display: flex; }
.demo-delete { color: #a03a2e; margin-left: auto; }
</style>
