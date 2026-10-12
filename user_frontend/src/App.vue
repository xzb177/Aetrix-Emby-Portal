<script setup lang="ts">
import { RouterView, useRoute, useRouter } from 'vue-router'
import { onMounted, onUnmounted, ref, watch } from 'vue'
import Toast from '@/components/Toast.vue'
import TgBindModal from '@/components/TgBindModal.vue'
import { useToast } from '@/composables/useToast'
import { useUserStore } from '@/stores/user'
import { tgApi } from '@/api/tg'

const { messages, remove, warning } = useToast()
const userStore = useUserStore()
const route = useRoute()
const router = useRouter()

let lastTgNotify = 0
function onTgNotBound(e: Event) {
  // 节流：多接口同时 403 时只处理一次
  const now = Date.now()
  if (now - lastTgNotify < 3000) return
  lastTgNotify = now
  const message = (e as CustomEvent)?.detail?.message || '公益服功能需要绑定 Telegram，防小号，1 分钟搞定'
  warning(message + '，正在前往绑定…')
  setTimeout(() => router.push('/tg-bind'), 600)
}

// ===== 全局 TG 绑定自动弹窗 =====
// 新用户注册后（或任何未绑定用户进入应用时），自动弹出绑定弹窗。
// 这是 LoginView → /tg-bind 页面跳转之外的第二道保险：即使跳转被绕过，
// 用户依然会看到弹窗。已绑定用户永远不会弹。
const showGlobalTgBind = ref(false)
const TG_GUIDE_SHOWN_KEY = 'tg_bind_guide_shown'

async function checkTgBindGuide() {
  // 未登录不查
  if (!userStore.isLoggedIn) return
  // 登录页、/tg-bind 引导页不打扰
  if (route.name === 'login' || route.path === '/tg-bind') return
  // 每个标签页会话只自动弹一次，避免刷新反复打扰
  if (sessionStorage.getItem(TG_GUIDE_SHOWN_KEY)) return
  try {
    const st = await tgApi.status()
    // 已绑定、或后端没要求绑定、或引导被管理员关闭 → 不弹
    if (!st.required || st.bound || !st.guide_enabled) return
    sessionStorage.setItem(TG_GUIDE_SHOWN_KEY, '1')
    showGlobalTgBind.value = true
  } catch {
    // 状态拉取失败：静默，不锁死用户
  }
}

function handleGlobalTgBound() {
  // 绑定成功后：标记已展示，下次不再自动弹（st.bound=true 也不会再弹，双保险）
  sessionStorage.setItem(TG_GUIDE_SHOWN_KEY, '1')
}

onMounted(() => {
  window.addEventListener('tg-not-bound', onTgNotBound)
  userStore.init()
  // 有登录 token 时后台刷新一次用户信息：401 时拦截器会自动用 refresh token 续期。
  if (userStore.isLoggedIn) {
    userStore.fetchUser().catch(() => {})
  }
  // TG 绑定引导：延迟 1.5s 等首屏稳定后检查，避免与首屏加载抢资源
  setTimeout(() => { checkTgBindGuide() }, 1500)
})
onUnmounted(() => {
  window.removeEventListener('tg-not-bound', onTgNotBound)
})

// 登录状态变化（注册/登录成功）时检查一次：覆盖 LoginView 跳转被绕过的场景
watch(
  () => userStore.isLoggedIn,
  (loggedIn) => {
    if (loggedIn) {
      setTimeout(() => { checkTgBindGuide() }, 1500)
    } else {
      // 登出后清除标记，下次登录重新评估
      sessionStorage.removeItem(TG_GUIDE_SHOWN_KEY)
      showGlobalTgBind.value = false
    }
  },
)
// 路由切换时也检查一次（比如注册后直接落在首页的场景）
watch(
  () => route.path,
  () => {
    // 延迟到路由稳定后
    setTimeout(() => { checkTgBindGuide() }, 800)
  },
)
</script>

<template>
  <div class="app-shell">
    <RouterView v-slot="{ Component }">
      <Transition name="page">
        <component :is="Component" />
      </Transition>
    </RouterView>
    <Toast :messages="messages" @remove="remove" />
    <!-- 全局 TG 绑定自动弹窗：新用户注册后自动弹出，已绑定不弹 -->
    <TgBindModal v-model="showGlobalTgBind" @bound="handleGlobalTgBound" />
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  min-height: 100dvh;   /* 移动端地址栏收放时高度不跳 */
  background: var(--au-bg);
}

/* ===== 页面转场：新页面立即挂载、180ms 淡入（只动 opacity）；旧页面无淡出，导航零等待 ===== */
.page-enter-active {
  transition: opacity var(--au-dur-fast, 180ms) var(--au-ease, ease);
}
.page-enter-from {
  opacity: 0;
}
@media (prefers-reduced-motion: reduce) {
  .page-enter-active {
    transition: none;
  }
}
</style>
