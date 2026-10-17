<template>
  <div class="au-page tg-login-page">
    <div class="au-card tg-login-card">
      <h1 class="tg-login-title">Telegram 快捷登录</h1>
      <div v-if="state === 'loading'" class="tg-login-loading" role="status">
        <div class="au-skeleton tg-login-skeleton"></div>
        <p class="tg-login-msg">正在为您登录…</p>
      </div>
      <p v-else-if="state === 'success'" class="tg-login-msg tg-login-ok" role="status"><span aria-hidden="true">✅</span> 登录成功</p>
      <div v-else>
        <p class="tg-login-msg tg-login-error" role="alert">{{ errorMsg }}</p>
        <div class="tg-login-actions">
          <button type="button" class="au-btn au-btn-primary" @click="doLogin">重试</button>
          <button type="button" class="au-btn" @click="goHome">回发现页</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { toUserMessage } from '@/utils/toUserMessage'
import { toastSystem } from '@/constants/toastMessages'

type LoginState = 'loading' | 'success' | 'error'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const state = ref<LoginState>('loading')
const errorMsg = ref('登录链接无效或已过期，请在机器人中重新获取')
let redirectTimer: ReturnType<typeof setTimeout> | null = null

async function doLogin() {
  state.value = 'loading'
  // TS 严格模式下 query.token 需手动收窄（多值 query 取第一个）
  const raw = route.query.token
  const token = typeof raw === 'string' ? raw : Array.isArray(raw) ? (raw[0] ?? '') : ''
  if (!token) {
    state.value = 'error'
    errorMsg.value = '登录链接无效或已过期，请在机器人中重新获取'
    return
  }
  try {
    const ok = await userStore.loginWithTgToken(token)
    if (ok) {
      state.value = 'success'
      redirectTimer = window.setTimeout(() => {
        router.replace('/game/account')
      }, 1000)
    } else {
      state.value = 'error'
      errorMsg.value = '登录链接无效或已过期，请在机器人中重新获取'
    }
  } catch (err) {
    state.value = 'error'
    // 网络错误给"网络连不上，请重试"，其他一律按链接无效处理
    errorMsg.value = toUserMessage(err, 'loadFailed') === toastSystem.networkError
      ? '网络连不上，请重试'
      : '登录链接无效或已过期，请在机器人中重新获取'
  } finally {
    // 无论成功失败都清掉地址栏 token，避免泄漏残留
    router.replace({ query: {} })
  }
}

onMounted(() => {
  doLogin()
})

onUnmounted(() => {
  if (redirectTimer) clearTimeout(redirectTimer)
})

function goHome() {
  router.replace('/')
}
</script>

<style scoped>
.tg-login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 60dvh;
}
.tg-login-card {
  max-width: 420px;
  width: 100%;
  text-align: center;
  padding: 2.5rem 1.5rem;
}
.tg-login-title {
  font-size: 1.25rem;
  margin-bottom: 1rem;
}
.tg-login-msg {
  color: var(--paper-ink-2, #9aa0a6);
}
.tg-login-ok {
  color: var(--paper-success);
}
.tg-login-error {
  color: var(--paper-danger);
}
.tg-login-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1rem;
}
.tg-login-skeleton {
  height: 64px;
  width: 100%;
}
.tg-login-actions {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 1.25rem;
}
</style>
