<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import {
  addStep, busy, checkLabels, deleteRoute, demoBusy, draftName, draftSteps, editing, message, moveStep,
  phase, phaseNames, prepared, removeStep, routes, runManual, saveRoute, selected, selectedId, startDemo,
  startDemoPolling, state, stepText, stopDemo, stopDemoPolling, toggleEdit, prepareDemo,
} from '../demoStore'

/**
 * 巡检演示面板：两种形态共用 demoStore 的同一份状态与轮询——
 * controls：侧栏卡片，控制（路线选择、操作按钮）与进度（检查灯、步骤列表、报错）合并展示，
 *           编辑路线走弹窗；
 * full：模拟模式的完整面板（干跑入口，编辑器内联，保持原布局）。
 * 演示占用运动租约时由 store 的 demoBusy 驱动父组件禁用手动遥控。
 */
const props = defineProps<{ hardware: boolean; section?: 'controls' | 'full' }>()
const mode = computed(() => props.section ?? 'full')

onMounted(() => { startDemoPolling() })
onBeforeUnmount(() => { stopDemoPolling() })
</script>

<template>
  <!-- 侧栏控制组：控制与进度合并为一张卡，阶段徽章只在此处显示 -->
  <div v-if="mode === 'controls'" class="demo-controls">
    <div class="demo-controls-head">
      <span class="demo-controls-title">巡检演示</span>
      <span :class="['demo-phase-badge', phase]">{{ phaseNames[phase] ?? phase }}</span>
    </div>
    <select v-model="selectedId" class="demo-controls-select" data-testid="demo-route-select" aria-label="选择路线" :disabled="demoBusy || editing">
      <option v-for="route in routes" :key="route.id" :value="route.id">{{ route.name }}（{{ route.steps.length }} 步）</option>
    </select>
    <div class="demo-controls-buttons">
      <button v-if="props.hardware && !demoBusy" class="demo-prepare" type="button" data-testid="demo-prepare"
              :disabled="!!busy" @click="prepareDemo">⚙ 准备</button>
      <button v-if="props.hardware && !demoBusy" class="demo-start" type="button" data-testid="demo-start"
              :disabled="!selected || !!busy || !prepared" :title="prepared ? '' : '请先点击「巡检准备」并等待就绪播报'"
              @click="startDemo()">⏻ 演示</button>
      <button v-if="props.hardware && demoBusy" class="demo-stop" type="button" data-testid="demo-stop"
              :disabled="!!busy" @click="stopDemo">■ 停止演示</button>
      <button class="demo-run" type="button" data-testid="demo-run"
              :disabled="!selected || !!busy || demoBusy || editing" @click="runManual">▶ 手动执行</button>
      <button class="demo-edit" type="button" data-testid="demo-edit" :disabled="demoBusy" @click="toggleEdit">{{ editing ? '收起编辑' : '✎ 编辑路线' }}</button>
    </div>
    <button v-if="selected && !demoBusy" type="button" class="demo-delete" data-testid="demo-delete"
            :disabled="!!busy || editing" @click="deleteRoute">删除该路线</button>
    <p v-if="message" class="demo-controls-message" role="status">{{ message }}</p>

    <!-- 执行进度：就绪检查灯与步骤列表跟随状态出现，主区不再重复展示 -->
    <div v-if="props.hardware && Object.keys(state?.checks ?? {}).length" class="demo-checks" data-testid="demo-checks">
      <span v-for="(ok, key) in state?.checks" :key="key" :class="['demo-check', { on: ok }]">
        <i></i>{{ checkLabels[key] ?? key }}
      </span>
    </div>
    <div v-if="selected" class="demo-progress demo-controls-progress" data-testid="demo-progress">
      <div v-if="state?.detail || (state?.route && state.route.name !== selected.name)" class="demo-phase">
        <span v-if="state?.route && state.route.name !== selected.name" class="demo-phase-route">执行中：{{ state.route.name }}</span>
        <span v-if="state?.detail" class="demo-phase-detail">{{ state.detail }}</span>
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

    <el-dialog v-model="editing" title="编辑路线" width="560px" :close-on-click-modal="false" append-to-body>
      <label class="demo-field">名称 <input v-model="draftName" class="demo-input" maxlength="100" placeholder="路线名称" /></label>
      <div v-for="(step, index) in draftSteps" :key="index" class="demo-step-row">
        <select v-model="step.type" class="demo-select demo-step-type" :aria-label="`第 ${index + 1} 步类型`">
          <option value="forward">前进 m</option>
          <option value="turn">转向 °（左正右负）</option>
          <option value="wait">停留 s</option>
        </select>
        <input v-model.number="step.value" class="demo-input demo-step-value" type="number" step="0.1" :aria-label="`第 ${index + 1} 步数值`" />
        <select v-if="step.type === 'wait'" v-model="step.task" class="demo-select demo-step-task">
          <option :value="null">无任务</option>
          <option value="qr">朗读二维码</option>
          <option value="led">播报信号灯</option>
          <option value="person">播报人数</option>
        </select>
        <span class="demo-step-ops">
          <button type="button" :disabled="index === 0" @click="moveStep(index, -1)">↑</button>
          <button type="button" :disabled="index === draftSteps.length - 1" @click="moveStep(index, 1)">↓</button>
          <button type="button" @click="removeStep(index)">✕</button>
        </span>
      </div>
      <div class="demo-editor-ops">
        <button type="button" @click="addStep">+ 添加步骤</button>
      </div>
      <p class="demo-hint">约束：前进 0.05–5 m · 转向 ±180°（左转为正）· 停留 0.5–60 s；保存为新路线，不影响原路线。</p>
      <template #footer>
        <el-button @click="editing = false">取消</el-button>
        <el-button type="primary" :disabled="!!busy || !draftSteps.length" @click="saveRoute">保存为新路线</el-button>
      </template>
    </el-dialog>
  </div>

  <!-- 模拟模式完整面板：保持原有内联布局与干跑入口 -->
  <div v-else class="demo-panel">
    <div class="demo-toolbar">
      <select v-model="selectedId" class="demo-select" data-testid="demo-route-select" aria-label="选择路线" :disabled="demoBusy || editing">
        <option v-for="route in routes" :key="route.id" :value="route.id">{{ route.name }}（{{ route.steps.length }} 步）</option>
      </select>
      <button v-if="props.hardware && !demoBusy" class="demo-prepare" type="button" data-testid="demo-prepare"
              :disabled="!!busy" @click="prepareDemo">⚙ 巡检准备</button>
      <button v-if="props.hardware && !demoBusy" class="data-valve" type="button" data-testid="demo-start"
              :disabled="!selected || !!busy || !prepared" :title="prepared ? '' : '请先点击「巡检准备」并等待就绪播报'"
              @click="startDemo()">⏻ 巡检演示</button>
      <button v-if="props.hardware && demoBusy" class="demo-stop" type="button" data-testid="demo-stop"
              :disabled="!!busy" @click="stopDemo">■ 停止演示</button>
      <button class="demo-run" type="button" data-testid="demo-run"
              :disabled="!selected || !!busy || demoBusy || editing" @click="runManual">手动执行</button>
      <button class="demo-edit" type="button" data-testid="demo-edit" :disabled="demoBusy" @click="toggleEdit">{{ editing ? '收起编辑' : '编辑路线' }}</button>
    </div>

    <div v-if="props.hardware && Object.keys(state?.checks ?? {}).length" class="demo-checks" data-testid="demo-checks">
      <span v-for="(ok, key) in state?.checks" :key="key" :class="['demo-check', { on: ok }]">
        <i></i>{{ checkLabels[key] ?? key }}
      </span>
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
        <select v-if="step.type === 'wait'" v-model="step.task" class="demo-select demo-step-task">
          <option :value="null">无任务</option>
          <option value="qr">朗读二维码</option>
          <option value="led">播报信号灯</option>
          <option value="person">播报人数</option>
        </select>
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
.demo-prepare { border: 1px solid #c9a227; color: #8a6d1a; font-weight: 600; border-radius: 6px; padding: 5px 10px; font-size: 12px; background: #fff; cursor: pointer; }
/* 就绪检查灯：准备阶段实时显示六项功能判据 */
.demo-checks { display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 4px 2px; }
.demo-check { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: #93a7b4; }
.demo-check i { width: 8px; height: 8px; border-radius: 50%; background: #c3d0d9; }
.demo-check.on { color: #1d7a5f; }
.demo-check.on i { background: #35a18c; box-shadow: 0 0 4px rgba(53, 161, 140, .8); }
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
.demo-phase-badge.preparing { background: #fdf3e3; color: #9a6b0f; }
.demo-phase-badge.prepared { background: #e7f6ef; color: #1d7a5f; }
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

/* 侧栏控制组：跟随侧栏的深色主题，按钮两列排布 */
.demo-controls { display: flex; flex-direction: column; gap: 9px; padding: 12px 13px; }
.demo-controls-head { display: flex; align-items: center; justify-content: space-between; }
.demo-controls-title { color: #e6eef5; font-size: 13px; font-weight: 600; }
.demo-controls-head .demo-phase-badge { background: rgba(255, 255, 255, .08); color: #a9bccb; }
.demo-controls-head .demo-phase-badge.prepared,
.demo-controls-head .demo-phase-badge.done { background: rgba(53, 161, 140, .2); color: #7fe0d2; }
.demo-controls-head .demo-phase-badge.preparing,
.demo-controls-head .demo-phase-badge.armed { background: rgba(201, 162, 39, .22); color: #e5c368; }
.demo-controls-head .demo-phase-badge.running { background: rgba(47, 111, 143, .3); color: #8ecbe6; }
.demo-controls-head .demo-phase-badge.aborted { background: rgba(192, 57, 43, .25); color: #e8a49b; }
.demo-controls-select { width: 100%; max-width: none; border: 1px solid rgba(255, 255, 255, .14); border-radius: 6px; padding: 6px 8px; font-size: 12px; background: rgba(255, 255, 255, .05); color: #dfe9f2; }
.demo-controls-select:disabled { opacity: .5; cursor: not-allowed; }
.demo-controls-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
.demo-controls-buttons button { border-radius: 6px; padding: 7px 8px; font-size: 12px; cursor: pointer; background: rgba(255, 255, 255, .05); border: 1px solid rgba(255, 255, 255, .14); color: #cfe0ea; white-space: nowrap; }
.demo-controls-buttons .demo-prepare { border-color: rgba(201, 162, 39, .55); color: #e5c368; font-weight: 600; }
.demo-controls-buttons .demo-start { border-color: rgba(53, 161, 140, .6); color: #7fe0d2; font-weight: 600; }
.demo-controls-buttons .demo-stop { border-color: rgba(192, 57, 43, .65); color: #f0a99f; font-weight: 600; }
.demo-controls-buttons .demo-run { border-color: rgba(105, 180, 176, .5); color: #9fd8d4; }
.demo-controls .demo-delete { align-self: flex-start; border: 0; background: none; color: #c98a7e; font-size: 11px; padding: 2px 0; cursor: pointer; }
.demo-controls-message { margin: 0; color: #9fd8d4; font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
.demo-controls .demo-editor { background: rgba(255, 255, 255, .04); border-color: rgba(255, 255, 255, .2); }

/* 侧栏内的执行进度：检查灯、步骤列表与报错沿用深色配色 */
.demo-controls .demo-checks { flex-wrap: wrap; gap: 5px 12px; padding: 2px 0; border-top: 1px solid rgba(255, 255, 255, .08); margin-top: 2px; padding-top: 8px; }
.demo-controls .demo-check { color: #7d92a3; }
.demo-controls .demo-check.on { color: #7fe0d2; }
.demo-controls .demo-check i { background: rgba(255, 255, 255, .18); }
.demo-controls .demo-check.on i { background: #35a18c; box-shadow: 0 0 4px rgba(53, 161, 140, .8); }
.demo-controls-progress { gap: 5px; }
.demo-controls-progress .demo-phase-route { color: #dfe9f2; }
.demo-controls-progress .demo-phase-detail { color: #8ba1b3; }
.demo-controls .demo-steps { max-height: 120px; }
.demo-controls .demo-steps li { color: #9fb3c4; }
.demo-controls .demo-steps li.done { color: #7fe0d2; }
.demo-controls .demo-steps li.current { background: rgba(47, 111, 143, .3); color: #bfe0ee; }
.demo-controls .step-number { border-color: rgba(255, 255, 255, .22); color: #cfe0ea; }
.demo-controls .demo-steps li.done .step-number { border-color: #35a18c; background: #35a18c; color: #fff; }
.demo-controls .demo-error { color: #f0a99f; }
</style>
