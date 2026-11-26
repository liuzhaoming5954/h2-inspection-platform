<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { errorMessage, request } from '../api'

/**
 * 真机模式登录门：全屏密码登录页。只负责收集密码并向后端换取令牌，
 * 校验、错误计数与锁定均由后端裁定，这里原样展示后端给出的原因。
 * 模拟模式不经过此页。
 */
const emit = defineEmits<{ loggedIn: [token: string] }>()
// 系统标题由上位机页面传入，与浏览器标签页标题保持同源
defineProps<{ title: string }>()

const password = ref('')
const error = ref('')
const submitting = ref(false)
const passwordInput = ref<HTMLInputElement>()

onMounted(() => { passwordInput.value?.focus() })

async function submit() {
  // 连点提交或空密码不发请求；锁定期间后端会返回剩余等待时间。
  if (submitting.value || !password.value) return
  submitting.value = true
  error.value = ''
  try {
    const result = await request<{ ok: boolean; token: string }>('/api/auth/verify', {
      method: 'POST', body: JSON.stringify({ password: password.value }),
    })
    password.value = ''
    emit('loggedIn', result.token)
  } catch (cause) {
    error.value = errorMessage(cause)
    // 密码错误后保留输入焦点，便于直接重输。
    password.value = ''
    passwordInput.value?.focus()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-gate">
    <div class="login-card">
      <div class="login-brand">
        <span class="brand-icon">H<span>2</span></span>
        <span class="login-brand-name">H2巡检平台</span>
      </div>
      <h1 class="login-title">{{ title }}</h1>
      <p class="login-subtitle">请输入登录密码进入调度系统</p>
      <form class="login-form" novalidate @submit.prevent="submit">
        <label class="login-field">
          <svg class="login-lock" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="5" y="10.5" width="14" height="9.5" rx="2" />
            <path d="M8.2 10.5V8a3.8 3.8 0 0 1 7.6 0v2.5" />
          </svg>
          <input ref="passwordInput" v-model="password" type="password" autocomplete="current-password"
                 placeholder="登录密码" aria-label="登录密码" data-testid="login-password" :disabled="submitting" />
        </label>
        <p class="login-error" role="alert" data-testid="login-error">{{ error }}</p>
        <button class="login-submit" type="submit" data-testid="login-submit" :disabled="submitting || !password">
          {{ submitting ? '正在登录…' : '登 录' }}
        </button>
      </form>
    </div>
    <p class="login-foot">本系统仅限授权操作人员使用</p>
  </div>
</template>

<style scoped>
/* 深色底与侧栏同色系：细网格呼应地图面板，青色柔光点出品牌色 */
.login-gate {
  position: fixed;
  inset: 0;
  display: grid;
  place-items: center;
  overflow: auto;
  padding: 24px;
  background-color: #0f1b28;
  background-image:
    radial-gradient(ellipse 60% 50% at 50% 38%, rgba(22, 136, 139, .16), transparent 70%),
    linear-gradient(rgba(255, 255, 255, .028) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, .028) 1px, transparent 1px);
  background-size: auto, 28px 28px, 28px 28px;
  color: #dfe9f2;
}
/* 卡片宽度需容下整行系统标题（约 370px），避免标题折行后末行只剩一个字 */
.login-card {
  width: 100%;
  max-width: 472px;
  padding: 38px 40px 34px;
  border: 1px solid rgba(255, 255, 255, .1);
  border-top: 3px solid rgba(105, 180, 176, .8);
  border-radius: 14px;
  background: rgba(255, 255, 255, .04);
  box-shadow: 0 24px 60px rgba(3, 10, 18, .55);
}
.login-brand { display: flex; align-items: center; justify-content: center; gap: 11px; }
.login-brand-name { font-size: 15px; font-weight: 650; letter-spacing: .6px; color: #cfe0ea; }
.login-title {
  margin: 26px 0 0;
  text-align: center;
  color: #eef5fa;
  font-size: 20px;
  font-weight: 650;
  line-height: 1.55;
  letter-spacing: .5px;
  /* 放不下时均匀折成两行，不出现单字末行 */
  text-wrap: balance;
}
.login-subtitle { margin: 10px 0 28px; text-align: center; color: #8ba1b3; font-size: 12px; }
.login-form { display: flex; flex-direction: column; }
.login-field {
  display: flex;
  align-items: center;
  height: 46px;
  padding: 0 14px;
  border: 1px solid rgba(255, 255, 255, .14);
  border-radius: 8px;
  background: rgba(255, 255, 255, .06);
  transition: border-color .15s ease, box-shadow .15s ease;
}
.login-field:focus-within { border-color: #35a18c; box-shadow: 0 0 0 3px rgba(53, 161, 140, .18); }
.login-lock { flex-shrink: 0; width: 18px; height: 18px; margin-right: 10px; fill: none; stroke: #7d92a3; stroke-width: 1.6; }
.login-field:focus-within .login-lock { stroke: #7fe0d2; }
.login-field input {
  flex: 1;
  min-width: 0;
  height: 100%;
  border: 0;
  outline: 0;
  background: transparent;
  color: #eef5fa;
  font-size: 16px;
  letter-spacing: 3px;
}
.login-field input::placeholder { color: #5f7488; letter-spacing: 0; }
.login-field input:disabled { opacity: .6; }
/* 错误行预留高度，提示出现时按钮不跳动 */
.login-error { min-height: 30px; margin: 10px 0 6px; padding-left: 2px; color: #f0a99f; font-size: 12px; line-height: 1.5; }
.login-submit {
  height: 46px;
  border: 0;
  border-radius: 8px;
  background: linear-gradient(180deg, #1a9a9c, #16888b);
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 6px;
  text-indent: 6px;
  cursor: pointer;
  box-shadow: 0 8px 20px rgba(22, 136, 139, .28);
  transition: filter .15s ease, transform .05s ease;
}
.login-submit:hover:not(:disabled) { filter: brightness(1.08); }
.login-submit:active:not(:disabled) { transform: translateY(1px); }
.login-submit:disabled { cursor: not-allowed; filter: saturate(.4) brightness(.8); box-shadow: none; }
.login-foot { margin: 22px 0 0; color: #5d7185; font-size: 11px; letter-spacing: .5px; }
</style>
