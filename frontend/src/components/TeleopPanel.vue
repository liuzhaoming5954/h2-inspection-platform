<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { RobotState } from '../api'

type Direction = 'forward' | 'backward' | 'turn_left' | 'turn_right'
type Action = Direction | 'stop' | 'heartbeat' | 'release'
type Phase = 'idle' | 'acquiring' | 'active'
type Hold = { kind: 'pointer'; id: number; target: HTMLButtonElement } | { kind: 'key'; key: string }

interface Reply {
  type: 'lease' | 'ack' | 'error'
  accepted: boolean
  code: string
  message: string
  sequence?: number
}

const props = defineProps<{ disabledReason: string; robot: RobotState | null; telemetryAvailable: boolean }>()
const emit = defineEmits<{ sessionActive: [active: boolean] }>()
const pad = ref<HTMLDivElement>()
const panel = ref<HTMLElement>()
const phase = ref<Phase>('idle')
const direction = ref<Direction | null>(null)
const feedback = ref('')
const warning = ref(false)

const directionButtons = [
  { action: 'forward', label: '前进', arrow: '↑' },
  { action: 'turn_left', label: '左转', arrow: '←' },
  { action: 'turn_right', label: '右转', arrow: '→' },
  { action: 'backward', label: '后退', arrow: '↓' },
] as const
const directionKeys: Record<string, Direction> = { ArrowUp: 'forward', ArrowDown: 'backward', ArrowLeft: 'turn_left', ArrowRight: 'turn_right' }
const motionLabels: Record<NonNullable<RobotState['motion']>, string> = {
  stop: '已停步', forward: '前进', backward: '后退', turn_left: '左转', turn_right: '右转',
}

const active = computed(() => phase.value === 'active')
const occupied = computed(() => props.telemetryAvailable && (props.robot?.teleop_active || props.robot?.status === 'teleop'))
const sessionLabel = computed(() => active.value ? '本页持权' : phase.value === 'acquiring' ? '正在申请' : occupied.value ? '控制权已占用' : '未申请')
const robotMotion = computed(() => props.telemetryAvailable && props.robot?.motion ? motionLabels[props.robot.motion] ?? '未收到反馈' : '未收到反馈')
const moving = computed(() => props.telemetryAvailable && !!props.robot?.motion && props.robot.motion !== 'stop')
const pose = computed(() => props.telemetryAvailable ? props.robot?.pose ?? props.robot?.position : undefined)
const note = computed(() => feedback.value || props.disabledReason || (active.value ? '按住方向按钮，或聚焦此区域后使用方向键。松开即停步。' : '申请控制权后，按住方向按钮控制模拟机器人。'))

// Keep only one ordinary command awaiting an acknowledgement; stop and release
// bypass that slot so a slow response cannot delay the operator's stop request.
const MOVEMENT_INTERVAL_MS = 100
const HEARTBEAT_INTERVAL_MS = 400
const ACK_TIMEOUT_MS = 500
const MAX_TIMER_GAP_MS = 350
let socket: WebSocket | undefined
let sequence = 0
let pending: { sequence: number; action: Action; at: number } | undefined
let held: Hold | undefined
let lastSentAt = 0
let lastTickAt = 0
let timer: ReturnType<typeof setInterval> | undefined
let acquireTimer: ReturnType<typeof setTimeout> | undefined

function transmit(transport: WebSocket, action: Action): number {
  const frameSequence = ++sequence
  transport.send(JSON.stringify({ action, sequence: frameSequence, sent_at_ms: Date.now() }))
  return frameSequence
}

function clearHold() {
  const previous = held
  held = undefined
  direction.value = null
  if (previous?.kind === 'pointer' && previous.target.hasPointerCapture(previous.id)) {
    try { previous.target.releasePointerCapture(previous.id) } catch { /* The pointer may already have ended. */ }
  }
}

function endSession(message: string, isWarning = false) {
  const transport = socket
  const hadLease = active.value
  socket = undefined
  clearInterval(timer)
  clearTimeout(acquireTimer)
  clearHold()
  pending = undefined
  if (transport) {
    if (hadLease && transport.readyState === WebSocket.OPEN) {
      try {
        transmit(transport, 'stop')
        transmit(transport, 'release')
      } catch { /* The gateway also expires the lease after a lost connection. */ }
    }
    transport.close()
  }
  phase.value = 'idle'
  feedback.value = message
  warning.value = isWarning
}

function send(action: Action) {
  const transport = socket
  if (!active.value || !transport || transport.readyState !== WebSocket.OPEN) {
    endSession('遥控连接已断开，请重新申请控制权。', true)
    return
  }
  try {
    const now = performance.now()
    pending = { sequence: transmit(transport, action), action, at: now }
    lastSentAt = now
  } catch {
    endSession('指令发送失败，遥控会话已结束。', true)
  }
}

function stopMotion(force = false) {
  const wasHeld = !!held
  clearHold()
  if (!active.value || (!force && !wasHeld) || pending?.action === 'stop') return
  send('stop')
}

function tick() {
  if (!active.value) return
  const now = performance.now()
  if (now - lastTickAt >= MAX_TIMER_GAP_MS || (pending && now - pending.at >= ACK_TIMEOUT_MS)) {
    endSession('遥控响应超时，会话已结束。请重新申请控制权。', true)
    return
  }
  lastTickAt = now
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    endSession('遥控连接已断开，请重新申请控制权。', true)
    return
  }
  if (pending || socket.bufferedAmount > 0) return
  const interval = direction.value ? MOVEMENT_INTERVAL_MS : HEARTBEAT_INTERVAL_MS
  if (now - lastSentAt >= interval) send(direction.value ?? 'heartbeat')
}

function parseReply(raw: unknown): Reply | undefined {
  if (typeof raw !== 'string') return
  try {
    const item: unknown = JSON.parse(raw)
    if (!item || typeof item !== 'object') return
    const candidate = item as Record<string, unknown>
    if (!['lease', 'ack', 'error'].includes(String(candidate.type)) || typeof candidate.accepted !== 'boolean' || typeof candidate.code !== 'string' || typeof candidate.message !== 'string') return
    return candidate as unknown as Reply
  } catch { return undefined }
}

function rejected(reply: Reply): string {
  const code = reply.code.toLowerCase()
  if (code.includes('busy') || code.includes('occupied') || code.includes('conflict')) return '机器人正在执行巡检，或控制权已被占用。请稍后重新申请。'
  if (code.includes('stale') || code.includes('future')) return '指令时间超出有效范围，遥控会话已结束。'
  if (code.includes('lease') || code.includes('expired')) return '遥控控制权已失效，请重新申请。'
  return `${reply.type === 'lease' ? '未取得控制权' : '遥控指令未被接受'}${reply.message ? `：${reply.message}` : '。'}`
}

function acquire() {
  if (phase.value !== 'idle' || props.disabledReason || document.hidden) return
  feedback.value = ''
  warning.value = false
  sequence = 0
  phase.value = 'acquiring'
  const endpoint = new URL('/api/teleop/ws', window.location.href)
  endpoint.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  let transport: WebSocket
  try { transport = new WebSocket(endpoint) }
  catch { endSession('无法建立遥控连接，请检查服务后重试。', true); return }
  socket = transport
  acquireTimer = setTimeout(() => {
    if (socket === transport && phase.value === 'acquiring') endSession('申请控制权超时，请检查连接后重试。', true)
  }, 4000)
  transport.addEventListener('message', event => {
    if (socket !== transport) return
    const reply = parseReply(event.data)
    if (!reply) { endSession('收到无法识别的遥控反馈，会话已结束。', true); return }
    if (!reply.accepted || reply.type === 'error') { endSession(rejected(reply), true); return }
    if (reply.type === 'lease' && phase.value === 'acquiring') {
      clearTimeout(acquireTimer)
      if (props.disabledReason || document.hidden) {
        // The gateway has granted the lease; explicitly return it.
        phase.value = 'active'
        endSession('遥控条件已变化，会话已结束。', true)
        return
      }
      phase.value = 'active'
      lastTickAt = lastSentAt = performance.now()
      timer = setInterval(tick, 25)
      void nextTick(() => { if (active.value) pad.value?.focus({ preventScroll: true }) })
    } else if (reply.type === 'ack' && reply.sequence === pending?.sequence) {
      pending = undefined
    }
  })
  const connectionLost = () => {
    if (socket === transport) endSession('遥控连接已断开，请重新申请控制权。', true)
  }
  transport.addEventListener('error', connectionLost)
  transport.addEventListener('close', connectionLost)
}

function beginHold(value: Direction, source: Hold) {
  if (!active.value || held) return
  feedback.value = ''
  warning.value = false
  held = source
  direction.value = value
  lastSentAt = 0
  tick()
}

function pressPointer(event: PointerEvent, value: Direction) {
  if (!active.value || held || !event.isPrimary || event.button !== 0 || !(event.currentTarget instanceof HTMLButtonElement)) return
  event.preventDefault()
  const target = event.currentTarget
  target.focus({ preventScroll: true })
  try { target.setPointerCapture(event.pointerId) } catch { /* Window-level listeners cover a release outside the button. */ }
  beginHold(value, { kind: 'pointer', id: event.pointerId, target })
}

function releasePointer(event: PointerEvent) {
  if (held?.kind === 'pointer' && held.id === event.pointerId) stopMotion()
}

function pressKey(event: KeyboardEvent) {
  if (!active.value || event.altKey || event.ctrlKey || event.metaKey) return
  if (event.key === 'Escape') { event.preventDefault(); stopMotion(true); return }
  const value = directionKeys[event.key]
  if (!value) return
  event.preventDefault()
  if (!event.repeat) beginHold(value, { kind: 'key', key: event.key })
}

function releaseKey(event: KeyboardEvent) {
  if (held?.kind === 'key' && held.key === event.key) {
    event.preventDefault()
    stopMotion()
  }
}

function leavePanel(event: FocusEvent) {
  if (held && (!(event.relatedTarget instanceof Node) || !panel.value?.contains(event.relatedTarget))) stopMotion()
}

function blurPage() {
  if (phase.value !== 'idle') endSession('页面失去焦点，遥控会话已结束。请重新申请控制权。')
}
function hidePage() { if (document.hidden) blurPage() }

watch(phase, value => emit('sessionActive', value !== 'idle'), { flush: 'sync' })
watch(() => props.disabledReason, value => {
  if (value && phase.value !== 'idle') endSession(`${value}，遥控会话已结束。`, true)
})

onMounted(() => {
  window.addEventListener('pointerup', releasePointer)
  window.addEventListener('pointercancel', releasePointer)
  window.addEventListener('keyup', releaseKey)
  window.addEventListener('blur', blurPage)
  window.addEventListener('pagehide', blurPage)
  document.addEventListener('visibilitychange', hidePage)
})
onBeforeUnmount(() => {
  window.removeEventListener('pointerup', releasePointer)
  window.removeEventListener('pointercancel', releasePointer)
  window.removeEventListener('keyup', releaseKey)
  window.removeEventListener('blur', blurPage)
  window.removeEventListener('pagehide', blurPage)
  document.removeEventListener('visibilitychange', hidePage)
  endSession('')
})

function number(value: number | undefined, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'
}
</script>

<template>
  <section ref="panel" class="panel teleop-panel" data-testid="teleop-panel" aria-labelledby="teleop-heading" @keydown="pressKey" @focusout="leavePanel">
    <header class="panel-heading">
      <div>
        <h2 id="teleop-heading">手动遥控 <span class="teleop-sim-tag">模拟</span></h2>
        <p class="panel-caption">申请控制权后，可手动控制模拟机器人</p>
      </div>
      <span :class="['teleop-badge', { active }]" data-testid="teleop-session-status" role="status"><i></i>{{ sessionLabel }}</span>
    </header>
    <div class="teleop-body">
      <div class="teleop-session">
        <div><strong>遥控会话</strong><small>{{ active ? '当前浏览器已获得控制权' : '同一时刻仅允许一个遥控会话' }}</small></div>
        <button v-if="phase === 'idle'" class="teleop-session-button primary" type="button" :disabled="!!disabledReason" data-testid="teleop-acquire" @click="acquire">申请控制权</button>
        <button v-else class="teleop-session-button" type="button" data-testid="teleop-release" @click="endSession('本页遥控会话已结束。')">{{ active ? '释放控制权' : '取消申请' }}</button>
      </div>
      <div ref="pad" class="teleop-pad" tabindex="0" role="group" aria-label="方向控制，按住方向键移动，松开停步" data-testid="teleop-pad">
        <button v-for="item in directionButtons" :key="item.action" type="button" :class="['teleop-direction', `dir-${item.action}`, { pressed: direction === item.action }]" :aria-label="item.label" :aria-pressed="direction === item.action" :disabled="!active" :data-testid="`teleop-${item.action}`" @pointerdown="pressPointer($event, item.action)" @lostpointercapture="releasePointer" @contextmenu.prevent>
          <span aria-hidden="true">{{ item.arrow }}</span>{{ item.label }}
        </button>
        <button class="teleop-stop" type="button" :disabled="!active" data-testid="teleop-stop" @click="stopMotion(true)"><span aria-hidden="true">■</span> 停步</button>
      </div>
      <div class="teleop-telemetry">
        <div><small>运动反馈</small><strong :class="{moving}" data-testid="teleop-motion">{{ robotMotion }}</strong></div>
        <div data-testid="teleop-position"><small>模拟位置 (m)</small><strong>{{ number(pose?.x) }}, {{ number(pose?.y) }}</strong></div>
        <div><small>朝向</small><strong>{{ number(pose?.yaw === undefined ? undefined : pose.yaw * 180 / Math.PI, 0) }}°</strong></div>
      </div>
      <p :class="['teleop-note', { warning }]" role="status">{{ note }}</p>
    </div>
  </section>
</template>

<style scoped>
.teleop-panel { border-top: 3px solid #69b4b0; }
.teleop-panel .panel-heading h2 { display: flex; align-items: center; gap: 8px; }
.teleop-panel .panel-caption { margin-bottom: 0; }
.teleop-sim-tag { padding: 2px 5px; border-radius: 3px; background: #fcf6e8; color: #896e3f; font-size: 9px; font-weight: 500; }
.teleop-badge { display: inline-flex; align-items: center; gap: 6px; padding: 5px 7px; color: #718795; border: 1px solid #e7edf1; border-radius: 5px; background: #f6f8fa; white-space: nowrap; font-size: 10px; }
.teleop-badge i { width: 6px; height: 6px; border-radius: 50%; background: #9aabb6; }
.teleop-badge.active { color: #267e74; border-color: #d2e7e1; background: #eef8f4; }
.teleop-badge.active i { background: #35a18c; }
.teleop-body { padding: 16px 19px 13px; }
.teleop-session { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.teleop-session strong { display: block; font-size: 11px; color: #4e6675; }
.teleop-session small { display: block; margin-top: 4px; color: #768895; font-size: 10px; line-height: 1.5; }
.teleop-session-button { flex-shrink: 0; padding: 7px 10px; border: 1px solid #c9dedf; border-radius: 6px; background: #f4fafa; color: #277e80; cursor: pointer; font-size: 11px; }
.teleop-session-button.primary { border-color: #16888b; background: #16888b; color: #fff; }
.teleop-session-button:disabled { opacity: .53; cursor: not-allowed; }
.teleop-pad { display: grid; grid-template-columns: repeat(3, 1fr); grid-template-areas: ". forward ." "left stop right" ". backward ."; gap: 6px; width: min(100%, 262px); margin: 15px auto; border-radius: 7px; touch-action: none; }
.teleop-pad:focus-visible { outline: 2px solid #82bbb8; outline-offset: 5px; }
.teleop-direction, .teleop-stop { display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 35px; padding: 5px; border: 1px solid #d7e4eb; border-radius: 6px; background: #f3f8fa; color: #486c80; cursor: pointer; user-select: none; font-size: 11px; }
.teleop-direction span { font-size: 17px; line-height: 1; }
.teleop-direction:disabled, .teleop-stop:disabled { color: #8d9eab; background: #f7f9fb; border-color: #e8edf1; cursor: not-allowed; }
.teleop-direction:hover:not(:disabled) { border-color: #8ec4c2; }
.teleop-direction.pressed { border-color: #16888b; background: #16888b; color: #fff; }
.dir-forward { grid-area: forward; }
.dir-turn_left { grid-area: left; }
.dir-turn_right { grid-area: right; }
.dir-backward { grid-area: backward; }
.teleop-stop { grid-area: stop; border-color: #e8c9c0; background: #fff7f3; color: #9d5140; font-weight: 600; }
.teleop-stop span { font-size: 8px; }
.teleop-stop:hover:not(:disabled) { background: #f9e6de; }
.teleop-telemetry { display: grid; grid-template-columns: .9fr 1.25fr .55fr; gap: 7px; padding: 10px 9px; border: 1px solid #e8eff3; border-radius: 6px; background: #f7fafc; }
.teleop-telemetry small { display: block; margin-bottom: 5px; color: #718694; font-size: 9px; }
.teleop-telemetry strong { color: #496875; white-space: nowrap; font-size: 11px; font-variant-numeric: tabular-nums; }
.teleop-telemetry .moving { color: #16888b; }
.teleop-note { min-height: 31px; margin: 11px 0 0; color: #6f8492; font-size: 10px; line-height: 1.6; }
.teleop-note.warning { color: #9a5a26; }
</style>
