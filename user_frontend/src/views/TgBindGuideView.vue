<!--
  TgBindGuideView.vue
  注册后 TG 绑定引导页
  用途：管理员要求用户绑定 Telegram 后才能使用系统功能时展示的引导页。
  流程：检查绑定状态 → 获取绑定码 → 引导用户向 Bot 发送 /bind 指令 → 轮询绑定状态 → 自动跳转。
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Send, Check } from 'lucide-vue-next'
import { tgApi, type TgBindStatus } from '@/api/tg'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const redirect = (route.query.redirect as string) || '/'

const loading = ref(true)          // 初始加载
const notConfigured = ref(false)   // bot_username 为空
const botUsername = ref('')
const bindCode = ref('')
const expiresIn = ref(0)           // 剩余秒数
const refreshing = ref(false)
const copyHint = ref('')           // 行内复制提示
let copyHintTimer: ReturnType<typeof setTimeout> | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null

const codeExpired = computed(() => expiresIn.value <= 0)
const countdownText = computed(() => {
  const s = Math.max(0, expiresIn.value)
  const m = Math.floor(s / 60)
  const ss = s % 60
  return `${m}:${String(ss).padStart(2, '0')}`
})

const commandText = computed(() => `/bind ${bindCode.value}`)

function clearTimers() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  if (tickTimer) { clearInterval(tickTimer); tickTimer = null }
}

function startTick() {
  if (tickTimer) clearInterval(tickTimer)
  tickTimer = setInterval(() => {
    if (expiresIn.value > 0) expiresIn.value -= 1
  }, 1000)
}

function startPoll() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = setInterval(async () => {
    try {
      const st: TgBindStatus = await tgApi.status()
      if (st.bound) {
        clearTimers()
        router.replace(redirect)
      }
    } catch {
      // 轮询失败不抛错、不锁死
    }
  }, 3000)
}

async function fetchBindCode() {
  const res = await tgApi.bindCode()
  bindCode.value = res.code
  expiresIn.value = res.expires_in
  if (res.bot_username) botUsername.value = res.bot_username
}

async function refreshCode() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await fetchBindCode()
    startTick()
  } finally {
    refreshing.value = false
  }
}

async function copyCommand() {
  const text = commandText.value
  try {
    await navigator.clipboard.writeText(text)
    showCopyHint('已复制到剪贴板')
  } catch {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      showCopyHint('已复制到剪贴板')
    } catch {
      showCopyHint('复制失败，请手动复制')
    }
  }
}

function showCopyHint(msg: string) {
  copyHint.value = msg
  if (copyHintTimer) clearTimeout(copyHintTimer)
  copyHintTimer = setTimeout(() => { copyHint.value = '' }, 2000)
}

function openBot() {
  window.open('https://t.me/' + botUsername.value, '_blank')
}

async function logout() {
  await userStore.logout()
  router.push('/login')
}

onMounted(async () => {
  try {
    const st = await tgApi.status()
    if (st.bound || !st.required || !st.guide_enabled) {
      router.replace(redirect)
      return
    }
    if (!st.bot_username) {
      notConfigured.value = true
      loading.value = false
      return
    }
    botUsername.value = st.bot_username
    await fetchBindCode()
    loading.value = false
    startTick()
    startPoll()
  } catch {
    // 初始状态拉取失败：不锁死用户，停掉加载态展示页头与退出登录入口
    loading.value = false
  }
})

onUnmounted(() => {
  clearTimers()
  if (copyHintTimer) clearTimeout(copyHintTimer)
})
</script>


<template>
  <div class="tg-guide-page">
    <div class="tg-guide-card">
      <header class="guide-header">
        <div class="header-icon">
          <Send :size="32" />
        </div>
        <h1>请先绑定 Telegram</h1>
        <p class="header-sub">管理员要求所有用户绑定 Telegram 后才能使用系统功能，完成绑定后将自动进入面板</p>
      </header>

      <div v-if="loading" class="loading-box">加载中…</div>

      <div v-else-if="notConfigured" class="warn-box">
        站点尚未配置 Telegram Bot，请联系管理员
      </div>

      <template v-else>
        <section class="step">
          <div class="step-num">1</div>
          <div class="step-body">
            <h2 class="step-title">打开 Telegram Bot</h2>
            <p class="step-desc">
              <span class="at">@</span>
              <span class="mono-ellipsis">{{ botUsername }}</span>
            </p>
            <button class="btn btn-primary" @click="openBot">打开 Telegram Bot</button>
          </div>
        </section>

        <section class="step">
          <div class="step-num">2</div>
          <div class="step-body">
            <h2 class="step-title">发送绑定指令</h2>
            <div class="command-box">{{ commandText }}</div>
            <button class="btn btn-ghost" :disabled="codeExpired" @click="copyCommand">复制指令</button>
            <p v-if="copyHint" class="copy-hint">{{ copyHint }}</p>
          </div>
        </section>

        <section class="code-panel">
          <span class="code-label">绑定码</span>
          <div class="code-big">{{ bindCode }}</div>
          <div v-if="!codeExpired" class="countdown-row">
            <span class="countdown-text"><span class="nowrap">{{ countdownText }}</span> 后过期</span>
          </div>
          <div v-else class="countdown-row">
            <span class="expired-text">绑定码已过期，请刷新</span>
          </div>
          <button class="link-btn refresh-btn" :disabled="refreshing" @click="refreshCode">重新获取</button>
        </section>

        <div class="auto-hint">
          <Check :size="16" class="check-icon" />
          <span>发送指令后，将自动进入面板</span>
        </div>
      </template>

      <div class="logout-row">
        <button class="link-btn" @click="logout">退出登录</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tg-guide-page {
  min-height: 100vh;
  min-height: 100dvh; /* R27：移动端地址栏收放高度不跳 */
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-canvas);
  color: var(--text-main);
}

.tg-guide-card {
  max-width: 560px;
  width: 100%;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--au-r-lg);
  padding: 32px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.guide-header {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 12px;
}

.header-icon {
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: rgba(232, 168, 74, 0.12);
  color: var(--accent-primary);
  display: flex;
  align-items: center;
  justify-content: center;
}

.guide-header h1 {
  font-family: var(--au-font-serif);
  font-size: 24px;
  margin: 0;
}

.header-sub {
  font-size: 14px;
  color: var(--text-muted);
  margin: 0;
  line-height: 1.6;
}

.loading-box {
  text-align: center;
  color: var(--text-muted);
  padding: 32px;
}

.warn-box {
  background: var(--au-danger-soft);
  border: 1px solid var(--au-danger);
  border-radius: var(--au-r-md);
  padding: 16px;
  color: var(--au-danger);
  font-size: 14px;
}

.step {
  display: flex;
  gap: 16px;
}

.step-num {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--au-primary);
  color: var(--au-on-primary);
  font-weight: 700;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.step-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.step-title {
  font-size: 16px;
  font-weight: 700;
  margin: 0;
}

.step-desc {
  margin: 0;
  font-size: 14px;
  color: var(--text-muted);
  display: flex;
  align-items: center;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.mono-ellipsis {
  min-width: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.btn {
  min-height: 44px;
  border-radius: var(--au-r-md);
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  padding: 0 16px;
  border: 1px solid transparent;
  transition: opacity 0.15s ease;
}

.btn:focus-visible,
.link-btn:focus-visible {
  outline: 2px solid var(--accent-primary);
  outline-offset: 2px;
}

.btn-primary {
  width: 100%;
  background: var(--au-primary);
  color: var(--au-on-primary);
  border: none;
}

.btn-ghost {
  background: transparent;
  border: 1px solid var(--accent-primary);
  color: var(--accent-primary);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.command-box {
  background: var(--bg-surface-2);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 18px;
  padding: 12px 16px;
  border-radius: var(--au-r-md);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.copy-hint {
  margin: 0;
  font-size: 13px;
  color: var(--au-success);
}

.code-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 24px;
  background: var(--bg-surface-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--au-r-md);
}

.code-label {
  font-size: 12px;
  color: var(--text-muted);
}

.code-big {
  font-family: var(--au-font-serif);
  font-size: 40px;
  letter-spacing: 12px;
  padding-left: 12px;
  text-align: center;
  white-space: nowrap;
}

.countdown-row {
  font-size: 14px;
  color: var(--text-muted);
  white-space: nowrap;
}

.countdown-text .nowrap {
  white-space: nowrap;
}

.expired-text {
  color: var(--au-danger);
  white-space: nowrap;
}

.link-btn {
  min-height: 44px;
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 14px;
  padding: 0 12px;
}

.refresh-btn {
  color: var(--accent-primary);
  font-weight: 700;
}

.refresh-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.auto-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 14px;
  color: var(--text-muted);
}

.check-icon {
  color: var(--au-success);
  flex-shrink: 0;
}

.logout-row {
  text-align: center;
}

.logout-row .link-btn {
  color: var(--text-muted);
}

.logout-row .link-btn:hover {
  text-decoration: underline;
}

@media (max-width: 768px) {
  .tg-guide-page {
    padding: 16px;
  }

  .tg-guide-card {
    padding: 16px;
  }

  .guide-header h1 {
    font-size: 20px;
  }

  .code-big {
    font-size: 32px;
  }
}
</style>

