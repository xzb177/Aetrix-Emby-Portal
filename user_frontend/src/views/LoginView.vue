<script setup lang="ts">
/**
 * 登录 / 注册 — 全页认证视图
 *
 * 统一后端 JWT 认证：
 * - POST /api/user/auth/login     登录（access + refresh token）
 * - POST /api/user/auth/register  注册（自动生成自建 Emby 凭据）
 *
 * 登录成功后自动跳转 ?redirect= 指定的受保护页面。
 */
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'
import { User, Lock, Mail, Eye, EyeOff, Clapperboard, Gift } from 'lucide-vue-next'
// 人机验证挂件（能力：人机验证）：管理员未开启时该组件自己什么都不渲染
import CaptchaChallenge from '@/components/ui/CaptchaChallenge.vue'
// 站名来自「站点与品牌」能力（未配置时用默认值）
import { branding } from '@/composables/useBranding'
import { getRegisterConfig } from '@/api/user'
import { tgApi } from '@/api/tg'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()
const toast = useToast()

// 注册页开关（管理端可配）：注册模式 + 邀请码开关
// registration_mode: open=开放注册 / closed=关闭注册（code 模式已下线）
const registrationMode = ref<'open' | 'closed'>('open')
const invitationEnabled = ref(true)
const showInviteField = computed(() => invitationEnabled.value)
const registrationClosed = computed(() => registrationMode.value === 'closed')

const mode = ref<'login' | 'register'>('login')

const loginForm = reactive({ username: '', password: '' })
// 邀请码：此前只有从带参链接进来（?invite=）才拿得到，表单里根本没有输入框，
// 现在能手填，链接进来自动预填。
const registerForm = reactive({
  username: '', password: '', confirmPassword: '', email: '',
  inviteCode: '',
})

const showLoginPassword = ref(false)
const showRegisterPassword = ref(false)
const loading = ref(false)
const error = ref('')

// 人机验证令牌：由挂件回调写入；未开启保护时一直是空串（后端也不校验）
const captchaToken = ref('')
const captchaRef = ref<InstanceType<typeof CaptchaChallenge> | null>(null)

const passwordStrength = computed(() => {
  const pwd = registerForm.password
  if (!pwd) return 0
  let score = 0
  if (pwd.length >= 6) score++
  if (pwd.length >= 10) score++
  if (/[a-z]/.test(pwd) && /[A-Z]/.test(pwd)) score++
  if (/\d/.test(pwd)) score++
  if (/[^a-zA-Z0-9]/.test(pwd)) score++
  return Math.min(score, 3)
})

function toggleMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
  error.value = ''
}

function friendlyError(err: any, fallback: string): string {
  const detail = err?.response?.data?.detail || err?.message || ''
  if (typeof detail === 'string' && detail) return detail
  return fallback
}

async function handleLogin() {
  if (!loginForm.username || !loginForm.password) {
    error.value = '请输入用户名和密码'
    return
  }
  error.value = ''
  loading.value = true
  try {
    await userStore.login(loginForm.username.trim(), loginForm.password, captchaToken.value)
    toast.success('登录成功')
    router.push((route.query.redirect as string) || '/')
  } catch (err) {
    error.value = friendlyError(err, '登录失败，请稍后重试')
    // 令牌是一次性的：失败后换一个新的，否则第二次点击会因「已使用」而失败
    captchaRef.value?.reset()
  } finally {
    loading.value = false
  }
}

async function handleRegister() {
  if (registrationClosed.value) {
    error.value = '当前未开放注册'
    return
  }
  const f = registerForm
  if (!f.username || !f.password || !f.confirmPassword) {
    error.value = '请完整填写注册信息'
    return
  }
  if (f.username.length < 3 || f.username.length > 32 || !/^[a-zA-Z0-9_]+$/.test(f.username)) {
    error.value = '用户名需 3-32 位，仅限字母、数字、下划线'
    return
  }
  if (f.password.length < 6) {
    error.value = '密码至少 6 位'
    return
  }
  if (f.password !== f.confirmPassword) {
    error.value = '两次输入的密码不一致'
    return
  }
  error.value = ''
  loading.value = true
  try {
    // 表单里填的优先；没填时沿用链接带来的
    const inviteCode = f.inviteCode.trim() || (route.query.invite as string) || ''
    await userStore.register(
      f.username.trim(), f.password, f.email || undefined,
      inviteCode || undefined, captchaToken.value,
    )
    toast.success('注册成功，已自动开通观影账号')
    // 注册后 TG 绑定引导：仅注册流程触发，普通登录不动。
    // 状态接口失败时按默认放行进首页，绝不锁死用户。
    const redirect = (route.query.redirect as string) || '/'
    try {
      const st = await tgApi.status()
      if (st.required && !st.bound && st.guide_enabled) {
        router.push({ path: '/tg-bind', query: route.query.redirect ? { redirect: route.query.redirect as string } : {} })
        return
      }
    } catch {
      /* 拿不到绑定状态就按默认放行 */
    }
    router.push(redirect)
  } catch (err) {
    error.value = friendlyError(err, '注册失败，请稍后重试')
    captchaRef.value?.reset()
  } finally {
    loading.value = false
  }
}

let restoreLoginTheme: (() => void) | null = null

onMounted(() => {
  // 修复1：登录页强制深色（品牌第一屏不跟随系统，用户决策 2026-10-11）。
  // 只动 DOM，不写 localStorage；离开时恢复进入前的主题，不影响用户偏好。
  const htmlEl = document.documentElement
  const hadLight = htmlEl.getAttribute('data-theme') === 'light'
  const metaEl = document.querySelector('meta[name="theme-color"]')
  const prevMeta = metaEl?.getAttribute('content') ?? null
  if (hadLight) {
    htmlEl.removeAttribute('data-theme')
    // 深色暗房工具栏色（与 useTheme.ts applyTheme 口径一致）
    metaEl?.setAttribute('content', '#0c0a09')
  }
  restoreLoginTheme = () => {
    if (hadLight) htmlEl.setAttribute('data-theme', 'light')
    if (metaEl && prevMeta !== null) metaEl.setAttribute('content', prevMeta)
  }

  // 拉取注册页开关（公开接口）：按管理端配置显示/隐藏邀请码输入框、是否关闭注册
  getRegisterConfig().then((cfg) => {
    // code 模式已下线：后端/此处统一归一为 open
    registrationMode.value = cfg.registration_mode === 'closed' ? 'closed' : 'open'
    invitationEnabled.value = cfg.invitation_enabled
  }).catch(() => { /* 拿不到时按默认值显示，不锁死 */ })
  if (route.query.mode === 'register') mode.value = 'register'
  // 邀请链接 ?invite=CODE：自动切到注册页、预填邀请码并提示
  if (route.query.invite) {
    mode.value = 'register'
    registerForm.inviteCode = String(route.query.invite)
    toast.info(`已收到好友邀请码，注册成功后双方都得积分奖励`, 5000)
  }
})

onUnmounted(() => {
  // 恢复进入登录页前的主题（修复1 的收尾）
  restoreLoginTheme?.()
  restoreLoginTheme = null
})
</script>

<template>
  <div class="auth-page">
    <div class="auth-grid" aria-hidden="true"></div>

    <div class="auth-column">
      <div class="auth-brand">
        <div class="brand-mark">
          <img decoding="async" loading="lazy" v-if="branding.logo_url" :src="branding.logo_url" :alt="branding.site_name" />
          <Clapperboard v-else :size="22" />
        </div>
        <h1 class="brand-title">{{ branding.site_name }}</h1>
        <p class="brand-subtitle">自建 Emby 影视服务 · 一个账号畅享所有设备</p>
      </div>

      <!-- 模式切换 -->
      <div class="mode-switch" role="tablist">
        <button
          class="mode-btn"
          :class="{ active: mode === 'login' }"
          role="tab"
          :aria-selected="mode === 'login'"
          @click="mode = 'login'"
        >
          登录
        </button>
        <button
          class="mode-btn"
          :class="{ active: mode === 'register' }"
          role="tab"
          :aria-selected="mode === 'register'"
          @click="mode = 'register'"
        >
          注册
        </button>
        <div class="mode-indicator" :class="{ right: mode === 'register' }" aria-hidden="true"></div>
      </div>

      <!-- 登录表单 -->
      <form v-if="mode === 'login'" class="auth-form" @submit.prevent="handleLogin">
        <label class="field">
          <span class="field-label">用户名</span>
          <div class="field-box">
            <User :size="16" class="field-icon" />
            <input
              v-model="loginForm.username"
              type="text"
              name="username"
              autocomplete="username"
              placeholder="输入用户名"
              @keyup.enter="handleLogin"
            />
          </div>
        </label>

        <label class="field">
          <span class="field-label">密码</span>
          <div class="field-box">
            <Lock :size="16" class="field-icon" />
            <input
              v-model="loginForm.password"
              :type="showLoginPassword ? 'text' : 'password'"
              name="password"
              autocomplete="current-password"
              placeholder="输入密码"
              @keyup.enter="handleLogin"
            />
            <button type="button" class="eye-btn" :aria-label="showLoginPassword ? '隐藏密码' : '显示密码'" @click="showLoginPassword = !showLoginPassword">
              <EyeOff v-if="showLoginPassword" :size="15" />
              <Eye v-else :size="15" />
            </button>
          </div>
        </label>

        <p v-if="error" class="form-error" role="alert">{{ error }}</p>

        <CaptchaChallenge ref="captchaRef" action="login" @update:token="captchaToken = $event" />

        <button type="submit" class="submit-btn" :disabled="loading || !loginForm.username || !loginForm.password">
          <span v-if="loading" class="spinner"></span>
          {{ loading ? '登录中…' : '登 录' }}
        </button>
      </form>

      <!-- 注册表单 -->
      <form v-else class="auth-form" @submit.prevent="handleRegister">
        <p v-if="registrationClosed" class="closed-notice">当前未开放注册</p>
        <label class="field">
          <span class="field-label">用户名</span>
          <div class="field-box">
            <User :size="16" class="field-icon" />
            <input
              v-model="registerForm.username"
              type="text"
              name="username"
              autocomplete="username"
              placeholder="3-32 位字母、数字、下划线"
              @keyup.enter="handleRegister"
            />
          </div>
        </label>

        <label class="field">
          <span class="field-label">密码</span>
          <div class="field-box">
            <Lock :size="16" class="field-icon" />
            <input
              v-model="registerForm.password"
              :type="showRegisterPassword ? 'text' : 'password'"
              name="new-password"
              autocomplete="new-password"
              placeholder="至少 6 位"
              @keyup.enter="handleRegister"
            />
            <button type="button" class="eye-btn" :aria-label="showRegisterPassword ? '隐藏密码' : '显示密码'" @click="showRegisterPassword = !showRegisterPassword">
              <EyeOff v-if="showRegisterPassword" :size="15" />
              <Eye v-else :size="15" />
            </button>
          </div>
          <div v-if="registerForm.password" class="strength">
            <div class="strength-track">
              <div class="strength-fill" :class="`lv-${passwordStrength}`"></div>
            </div>
            <span class="strength-text">{{ ['弱', '中', '强'][passwordStrength - 1] || '' }}</span>
          </div>
        </label>

        <label class="field">
          <span class="field-label">确认密码</span>
          <div class="field-box">
            <Lock :size="16" class="field-icon" />
            <input
              v-model="registerForm.confirmPassword"
              type="password"
              name="confirm-password"
              autocomplete="new-password"
              placeholder="再次输入密码"
              @keyup.enter="handleRegister"
            />
          </div>
        </label>

        <label class="field">
          <span class="field-label">邮箱 <em class="optional">选填</em></span>
          <div class="field-box">
            <Mail :size="16" class="field-icon" />
            <input
              v-model="registerForm.email"
              type="email"
              name="email"
              autocomplete="email"
              placeholder="用于找回密码"
              @keyup.enter="handleRegister"
            />
          </div>
        </label>

        <label v-if="showInviteField" class="field">
          <span class="field-label">邀请码 <em class="optional">选填，双方得积分</em></span>
          <div class="field-box">
            <Gift :size="16" class="field-icon" />
            <input
              v-model="registerForm.inviteCode"
              type="text"
              name="invite-code"
              placeholder="好友的邀请码"
              @keyup.enter="handleRegister"
            />
          </div>
        </label>

        <p v-if="error" class="form-error" role="alert">{{ error }}</p>

        <CaptchaChallenge ref="captchaRef" action="register" @update:token="captchaToken = $event" />

        <button type="submit" class="submit-btn" :disabled="loading || registrationClosed || !registerForm.username || !registerForm.password || !registerForm.confirmPassword">
          <span v-if="loading" class="spinner"></span>
          {{ loading ? '注册中…' : '注 册' }}
        </button>
      </form>

      <p class="auth-footnote">
        注册即代表同意服务条款 · 密码经 bcrypt 加密存储
      </p>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  box-sizing: border-box;
  min-height: 100vh;
  min-height: 100svh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  position: relative;
  isolation: isolate;
  overflow: hidden;
  background: var(--au-bg);
}

/* 氛围光已移除：海报拼贴本身提供视觉丰富度（2026-10-11，品牌收敛到琥珀）。 */

/* 单独遮罩网格，避免遮罩影响光晕、品牌或表单。 */
.auth-grid {
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background-image:
    linear-gradient(to right, rgba(128, 128, 128, 0.14) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(128, 128, 128, 0.14) 1px, transparent 1px);
  background-size: 48px 48px;
  -webkit-mask-image: radial-gradient(ellipse 80% 70% at 50% 40%, black 30%, transparent 100%);
  mask-image: radial-gradient(ellipse 80% 70% at 50% 40%, black 30%, transparent 100%);
}

@supports (color: color-mix(in srgb, black 70%, transparent)) {
  .auth-grid {
    background-image:
      linear-gradient(to right, color-mix(in srgb, var(--au-border) 70%, transparent) 1px, transparent 1px),
      linear-gradient(to bottom, color-mix(in srgb, var(--au-border) 70%, transparent) 1px, transparent 1px);
  }
}

/* 无卡片底色、边框与阴影，内容直接浮于页面背景。 */
.auth-column {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 400px;
  min-width: 0;
  animation: authColumnIn 0.4s cubic-bezier(0.22,1,0.36,1) both;
}

@keyframes authColumnIn {
  from { opacity: 0; transform: translateY(16px); }
  to { opacity: 1; transform: none; }
}

.auth-brand {
  text-align: center;
  margin-bottom: 2rem;
}

.brand-mark {
  width: 46px;
  height: 46px;
  margin: 0 auto 1rem;
  border-radius: var(--au-r-md);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--au-text-2);
}

.brand-mark img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: inherit;
}

.brand-title {
  font-size: 1.75rem;
  font-weight: 700;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--au-text);
  margin: 0 0 0.625rem;
  line-height: 1.3;
  overflow-wrap: anywhere;
}

.brand-subtitle {
  font-size: 0.8125rem;
  color: var(--au-text-3);
  margin: 0;
  line-height: 1.6;
}

/* 胶囊模式切换 */
.mode-switch {
  box-sizing: border-box;
  position: relative;
  display: grid;
  grid-template-columns: 1fr 1fr;
  height: 44px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-full);
  padding: 4px;
  margin-bottom: 1.5rem;
}

.mode-btn {
  position: relative;
  z-index: 1;
  min-width: 0;
  padding: 0 0.75rem;
  border: none;
  background: transparent;
  border-radius: var(--au-r-full);
  font: inherit;
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--au-text-3);
  cursor: pointer;
  transition: color 0.2s ease;
  /* 修复3：文字在胶囊内严格水平垂直居中（移动端 UA 默认样式可能偏） */
  display: flex;
  align-items: center;
  justify-content: center;
}

.mode-btn.active {
  color: var(--au-on-primary);
  font-weight: 600;
}

.mode-indicator {
  position: absolute;
  top: 4px;
  left: 4px;
  width: calc(50% - 4px);
  height: calc(100% - 8px);
  border-radius: var(--au-r-full);
  background: var(--au-primary);
  pointer-events: none;
  transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
}

.mode-indicator.right {
  transform: translateX(100%);
}

/* 全胶囊表单 */
.auth-form {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  min-width: 0;
}

.field-label {
  padding: 0 20px;
  font-size: 0.8125rem;
  font-weight: 500;
  line-height: 1.5;
  color: var(--au-text-3);
}

.optional {
  margin-left: 0.25rem;
  font-size: 0.75rem;
  font-style: normal;
  font-weight: 400;
  color: var(--au-text-3);
}

.field-box {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  height: 52px;
  background: var(--au-input-bg);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-full);
  padding: 0 20px;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.field-box:focus-within {
  border-color: var(--au-border-focus);
  box-shadow: 0 0 0 3px var(--au-primary-soft);
}

.field-icon {
  color: var(--au-text-3);
  margin-right: 0.625rem;
  flex-shrink: 0;
  transition: color 0.2s ease;
}

.field-box:focus-within .field-icon {
  color: var(--au-primary);
}

.field-box input {
  box-sizing: border-box;
  flex: 1;
  min-width: 0;
  height: 100%;
  padding: 0;
  background: transparent;
  border: none;
  outline: none;
  color: var(--au-text);
  font: inherit;
  font-size: 0.9375rem;
}

.field-box input::placeholder {
  color: var(--au-text-3);
  opacity: 1;
}

/* 修复2：浏览器自动填充（Chrome/Safari）默认底色会盖住输入框，强制跟随主题 */
.field-box input:-webkit-autofill,
.field-box input:-webkit-autofill:hover,
.field-box input:-webkit-autofill:focus,
.field-box input:-webkit-autofill:active {
  -webkit-text-fill-color: var(--au-text);
  -webkit-box-shadow: 0 0 0 1000px var(--au-input-bg) inset;
  transition: background-color 5000s ease-in-out 0s;
}

.eye-btn {
  box-sizing: border-box;
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  margin-left: 0.375rem;
  background: transparent;
  border: none;
  color: var(--au-text-3);
  cursor: pointer;
  border-radius: 50%;
  transition: color 0.2s ease, background-color 0.2s ease;
}

.eye-btn:hover {
  color: var(--au-text-2);
  background: var(--au-surface);
}

.mode-btn:focus-visible,
.eye-btn:focus-visible,
.submit-btn:focus-visible {
  outline: 2px solid var(--au-border-focus);
  outline-offset: 3px;
}

/* 密码强度 */
.strength {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  margin-top: 0.125rem;
  padding: 0 20px;
}

.strength-track {
  flex: 1;
  height: 3px;
  background: var(--au-surface-2);
  border-radius: var(--au-r-full);
  overflow: hidden;
}

.strength-fill {
  height: 100%;
  border-radius: var(--au-r-full);
  transition: width 0.3s ease, background 0.3s ease;
  width: 0;
}

.strength-fill.lv-1 { width: 33%; background: var(--au-danger); }
.strength-fill.lv-2 { width: 66%; background: var(--au-warning); }
.strength-fill.lv-3 { width: 100%; background: var(--au-primary); }

.strength-text {
  font-size: 0.8125rem;
  color: var(--au-text-3);
  min-width: 16px;
}

/* 错误与注册关闭提示保留独立语义色。 */
.form-error {
  margin: 0;
  padding: 0.75rem 20px;
  border-radius: 26px;
  background: var(--au-danger-soft);
  border: 1px solid var(--au-danger-border);
  color: var(--au-danger);
  font-size: 0.8125rem;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.closed-notice {
  margin: 0;
  padding: 0.75rem 20px;
  border-radius: 26px;
  background: var(--au-warning-soft, var(--au-danger-soft));
  border: 1px solid var(--au-warning-border, var(--au-danger-border));
  color: var(--au-warning, var(--au-danger));
  font-size: 0.875rem;
  line-height: 1.5;
  text-align: center;
}

/* 渐变胶囊提交按钮 */
.submit-btn {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  width: 100%;
  height: 52px;
  flex-shrink: 0;
  margin-top: 0.25rem;
  padding: 0 20px;
  background: var(--au-gradient);
  border: none;
  border-radius: var(--au-r-full);
  color: var(--au-on-primary);
  font: inherit;
  font-size: 0.9375rem;
  font-weight: 600;
  cursor: pointer;
  transition: transform 0.15s ease, filter 0.2s ease, color 0.2s ease;
}

.submit-btn:hover:not(:disabled) {
  filter: brightness(1.08);
}

.submit-btn:active:not(:disabled) {
  transform: scale(0.98);
}

.submit-btn:disabled {
  background: var(--au-surface-3);
  color: var(--au-text-3);
  cursor: not-allowed;
}

.spinner {
  width: 15px;
  height: 15px;
  border: 2px solid var(--au-on-image-strong);
  border-top-color: var(--au-text);
  border-radius: 50%;
  animation: au-spin 0.8s linear infinite;
}

@keyframes au-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.auth-footnote {
  margin: 1.5rem 0 0;
  text-align: center;
  font-size: 0.8125rem;
  color: var(--au-text-3);
  line-height: 1.6;
}

@media (max-width: 767px) {
  .auth-page {
    padding-top: 32px;
    padding-left: 24px;
    padding-right: 24px;
    padding-bottom: env(safe-area-inset-bottom);
  }

  .auth-column {
    max-width: 100%;
  }

  .field-box,
  .submit-btn {
    height: 52px;
  }

  .field-box input {
    font-size: 16px;
  }

  .auth-footnote {
    margin-bottom: 24px;
  }
}

@media (prefers-reduced-motion: reduce) {
  * {
    animation: none !important;
    transition: none !important;
  }
}
</style>