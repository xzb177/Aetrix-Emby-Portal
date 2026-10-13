<template>
  <div class="au-page tg-login-page">
    <div class="au-card tg-login-card">
      <h1 class="tg-login-title">Telegram 快捷登录</h1>
      <div v-if="state === 'loading'" class="tg-login-loading" role="status" aria-label="正在为您登录">
        <div class="au-skeleton tg-login-skeleton"></div>
        <p class="tg-login-msg">正在为您登录…</p>
      </div>
      <p v-else-if="state === 'success'" class="tg-login-msg tg-login-ok">✅ 登录成功</p>
      <div v-else>
        <p class="tg-login-msg tg-login-error">登录链接无效或已过期，请在机器人中重新获取</p>
        <button class="au-btn tg-login-home" @click="goHome">返回首页</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

type LoginState = 'loading' | 'success' | 'error'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const state = ref<LoginState>('loading')

onMounted(async () => {
  // TS 严格模式下 query.token 需手动收窄
  const raw = route.query.token
  const token = typeof raw === 'string' ? raw : Array.isArray(raw) ? (raw[0] ?? '') : ''
  if (!token) {
    state.value = 'error'
    return
  }
  state.value = 'loading'
  try {
    const ok = await userStore.loginWithTgToken(token)
    // 请求已发出，清掉地址栏 token，避免泄漏
    router.replace({ query: {} })
    if (ok) {
      state.value = 'success'
      window.setTimeout(() => {
        router.replace('/game/account')
      }, 1000)
    } else {
      state.value = 'error'
    }
  } catch {
    state.value = 'error'
  }
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
  min-height: 60vh;
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
  color: var(--au-text-2, #9aa0a6);
}
.tg-login-ok {
  color: var(--au-success);
}
.tg-login-error {
  color: var(--au-danger);
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
.tg-login-home {
  margin-top: 1.25rem;
}
</style>
