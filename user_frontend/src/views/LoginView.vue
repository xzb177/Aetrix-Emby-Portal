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
import { toUserMessage } from '@/utils/toUserMessage'
import { toastFallback } from '@/constants/toastMessages'
import { User, Lock, Mail, Eye, EyeOff, Clapperboard, Gift, Send } from 'lucide-vue-next'
// 人机验证挂件（能力：人机验证）：管理员未开启时该组件自己什么都不渲染
import CaptchaChallenge from '@/components/ui/CaptchaChallenge.vue'
// 站名来自「站点与品牌」能力（未配置时用默认值）
import { branding } from '@/composables/useBranding'
import { getRegisterConfig } from '@/api/user'
import { tgApi } from '@/api/tg'
import { fetchLoginPosters, fetchTgBotUsername, type LoginPoster } from '@/api/site'
import PosterCollage from '@/components/ui/PosterCollage.vue'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()
const toast = useToast()

// 注册页开关（管理端可配）：注册模式 + 邀请码开关
// registration_mode: open=开放注册 / closed=关闭注册（code 模式已下线）
const registrationMode = ref<'open' | 'closed'>('open')
const invitationEnabled = ref(true)
const registrationClosed = computed(() => registrationMode.value === 'closed')
 // 注册配置拉取失败标记：失败时注册区显示错误态+重试，不让用户填完表才被拒
 const configError = ref(false)

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
const loginLoading = ref(false)
const registerLoading = ref(false)
const error = ref('')

// 人机验证令牌：由挂件回调写入；未开启保护时一直是空串（后端也不校验）
const captchaToken = ref('')
const captchaRef = ref<InstanceType<typeof CaptchaChallenge> | null>(null)

// 强度文案映射 ['弱','弱','中','强'][score]：score=0（1–5 位密码）时旧写法 ['弱','中','强'][score-1] 会取到 undefined 显示空文案
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

function toggleMode(next: 'login' | 'register') {
  mode.value = next
  error.value = ''
  // 人机验证令牌显式清空：即使挂件会随表单重建，也防止时序上复用旧令牌
  captchaToken.value = ''
}

function onTabKey(e: KeyboardEvent, current: 'login' | 'register') {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
  e.preventDefault()
  const next = current === 'login' ? 'register' : 'login'
  toggleMode(next)
  document.getElementById(next === 'login' ? 'tab-login' : 'tab-register')?.focus()
}

function friendlyError(err: any, fallback: string): string {
  // 认证表单（登录/注册）中 401/403 表示的是凭据错误（如密码错误），
  // 而不是已登录后接口调用时的“会话过期”。因此这里必须先于 toUserMessage 的
  // 全局“状态码优先”规则单独处理，避免把“密码错了”误报成“登录已过期”。
  const status = err?.response?.status
  const detail = err?.response?.data?.detail
  if ((status === 401 || status === 403) && typeof detail === 'string' && detail.trim() !== '') {
    const trimmed = detail.trim()
    const isMachineCode = /^[A-Z][A-Z0-9_]{2,}$/.test(trimmed)
    const hasLeakKeyword = /数据库|sql|连接失败|超时|异常|堆栈|traceback/i.test(trimmed)
    const isServerErrorLeak = /internal server error|服务器内部错误/i.test(trimmed)
    if (!isMachineCode && !hasLeakKeyword && !isServerErrorLeak) {
      return trimmed
    }
  }

  // 统一走 toUserMessage 解析；只有落到通用兜底时才用调用方的场景文案
  const msg = toUserMessage(err, 'saveFailed')
  return msg === toastFallback.saveFailed ? fallback : msg
}

/** query 参数取首个字符串值（多值 query 取第一个，防止数组被直接提交） */
function firstQueryValue(v: unknown): string {
  if (Array.isArray(v)) return String(v[0] ?? '')
  return String(v ?? '')
}

/** redirect 白名单：只允许站内路径，防止开放重定向 */
function safeRedirect(v: unknown): string {
  const s = firstQueryValue(v)
  if (s.startsWith('/') && !s.startsWith('//') && !s.includes('://')) return s
  return '/'
}

async function handleLogin() {
  // 回车双提交防护：表单 @submit 与输入框回车各触发一次，此处用 loading 状态做守卫兜底
  if (loginLoading.value) return
  if (!loginForm.username || !loginForm.password) {
    error.value = '请输入用户名和密码'
    return
  }
  error.value = ''
  loginLoading.value = true
  try {
    await userStore.login(loginForm.username.trim(), loginForm.password, captchaToken.value)
    toast.success('登录成功')
    router.push(safeRedirect(route.query.redirect))
  } catch (err) {
    error.value = friendlyError(err, '登录失败，请稍后重试')
    // 令牌是一次性的：失败后换一个新的，否则第二次点击会因「已使用」而失败
    captchaRef.value?.reset()
  } finally {
    loginLoading.value = false
  }
}

async function handleRegister() {
  // 回车双提交防护（同 handleLogin）
  if (registerLoading.value) return
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
  registerLoading.value = true
  try {
    // 表单里填的优先；没填时沿用链接带来的（多值 query 取第一个）
    const inviteCode = f.inviteCode.trim() || firstQueryValue(route.query.invite)
    await userStore.register(
      f.username.trim(), f.password, f.email || undefined,
      inviteCode || undefined, captchaToken.value,
    )
    toast.success('注册成功，已自动开通观影账号')
    // 注册后 TG 绑定引导：仅注册流程触发，普通登录不动。
    // 状态接口失败时按默认放行进首页，绝不锁死用户。
    const redirect = safeRedirect(route.query.redirect)
    try {
      const st = await tgApi.status()
      if (st.required && !st.bound && st.guide_enabled) {
        router.push({ path: '/tg-bind', query: route.query.redirect ? { redirect: safeRedirect(route.query.redirect) } : {} })
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
    registerLoading.value = false
  }
}

// 登录页跟随用户主题（用户决策 2026-10-14 21:14，取代 2026-10-11 的强制深色口径）：
// 不再自己动 data-theme，路由守卫 afterEach 已按 localStorage 'aetrix-theme' 恢复用户偏好。

// 海报拼贴背景：延迟到浏览器空闲时拉取，不阻塞表单首屏渲染
const loginPosters = ref<LoginPoster[]>([])
// EA 海报直链基地址（后端下发；为空则不渲染拼贴，避免相对路径被反代到错误后端）
const loginPostersBaseUrl = ref('')

// TG 登录入口：站点机器人用户名（公开接口下发）；为空=未配置机器人，不渲染入口
const tgBotUsername = ref('')

let postersIdleHandle: number | null = null
let postersTimeout: ReturnType<typeof setTimeout> | null = null

function loadRegisterConfig() {
  configError.value = false
  // 拉取注册页开关（公开接口）：按管理端配置显示/隐藏邀请码输入框、是否关闭注册
  getRegisterConfig().then((cfg) => {
    // code 模式已下线：后端/此处统一归一为 open
    registrationMode.value = cfg.registration_mode === 'closed' ? 'closed' : 'open'
    invitationEnabled.value = cfg.invitation_enabled
    // 注册已关闭时忽略 ?mode=register / ?invite= 参数，强制留在登录页
    if (registrationMode.value === 'closed') mode.value = 'login'
  }).catch(() => { configError.value = true /* 拉取失败时注册区显示错误态+重试，不锁死 */ })
}

onMounted(() => {
  // 登录页不再强制深色：跟随用户偏好（见文件顶部注释）。

  loadRegisterConfig()
  // TG 登录入口与注册配置并行拉取，失败保持空串（不渲染入口、不阻塞页面）
  fetchTgBotUsername().then(name => { tgBotUsername.value = name }).catch(() => { tgBotUsername.value = '' })
  if (route.query.mode === 'register') mode.value = 'register'
  // 邀请链接 ?invite=CODE（多值 query 取第一个）：自动切到注册页、预填邀请码并提示
  if (route.query.invite) {
    mode.value = 'register'
    registerForm.inviteCode = firstQueryValue(route.query.invite)
    toast.info(`已收到好友邀请码，注册成功后双方都可获得积分奖励`, 5000)
  }

  // 海报拼贴背景：requestIdleCallback 延迟拉取（降级 setTimeout 800ms），
  // 失败时静默降级（loginPosters 为空或基地址为空 → 不渲染拼贴，回退现有背景）
  const loadPosters = () => {
    fetchLoginPosters().then(({ items, embyBaseUrl }) => {
      if (items.length > 0 && embyBaseUrl) {
        loginPosters.value = items
        loginPostersBaseUrl.value = embyBaseUrl
      }
    }).catch(() => { /* 静默降级 */ })
  }
  const ric = (window as unknown as { requestIdleCallback?: (cb: () => void) => number }).requestIdleCallback
  if (typeof ric === 'function') postersIdleHandle = ric(loadPosters)
  else postersTimeout = setTimeout(loadPosters, 800)
})

onUnmounted(() => {
  // 页面卸载时取消海报延迟拉取，避免回调里写已卸载组件的状态
  if (postersIdleHandle !== null) (window as unknown as { cancelIdleCallback?: (h: number) => void }).cancelIdleCallback?.(postersIdleHandle)
  if (postersTimeout !== null) clearTimeout(postersTimeout)
})
</script>

<template>
  <div class="auth-page">
    <PosterCollage v-if="loginPosters.length > 0 && loginPostersBaseUrl" :posters="loginPosters" :emby-base-url="loginPostersBaseUrl" />
    <div class="auth-grid" aria-hidden="true"></div>

    <div class="auth-column">
      <div class="auth-brand">
        <div class="brand-mark">
          <img decoding="async" loading="lazy" v-if="branding.logo_url" :src="branding.logo_url" :alt="branding.site_name" />
          <Clapperboard v-else :size="22" aria-hidden="true" />
        </div>
        <h1 class="brand-title">{{ branding.site_name }}</h1>
        <p class="brand-subtitle">自建 Emby 影视服务 · 一个账号，手机/电视/电脑都能看</p>
      </div>

      <!-- 模式切换 -->
      <div class="mode-switch" role="tablist" aria-label="登录或注册">
        <button
          id="tab-login"
          class="mode-btn"
          :class="{ active: mode === 'login' }"
          role="tab"
          :aria-selected="mode === 'login'"
          aria-controls="panel-login"
          @click="toggleMode('login')"
          @keydown="onTabKey($event, 'login')"
        >
          <span class="mode-btn-label">登录</span>
        </button>
        <button
          id="tab-register"
          class="mode-btn"
          :class="{ active: mode === 'register' }"
          role="tab"
          :aria-selected="mode === 'register'"
          aria-controls="panel-register"
          @click="toggleMode('register')"
          @keydown="onTabKey($event, 'register')"
        >
          <span class="mode-btn-label">注册</span>
        </button>
        <div class="mode-indicator" :class="{ right: mode === 'register' }" aria-hidden="true"></div>
      </div>

      <!-- 登录表单 -->
      <form v-if="mode === 'login'" id="panel-login" class="auth-form" role="tabpanel" aria-labelledby="tab-login" tabindex="0" @submit.prevent="handleLogin">
        <label class="field">
          <span class="field-label">用户名</span>
          <div class="field-box">
            <User :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="loginForm.username"
              type="text"
              name="username"
              autocomplete="username"
              placeholder="输入用户名"
              :aria-invalid="!!error"
              aria-describedby="login-form-error"
            />
          </div>
        </label>

        <label class="field">
          <span class="field-label">密码</span>
          <div class="field-box">
            <Lock :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="loginForm.password"
              :type="showLoginPassword ? 'text' : 'password'"
              name="password"
              autocomplete="current-password"
              placeholder="输入密码"
              :aria-invalid="!!error"
              aria-describedby="login-form-error"
            />
            <button type="button" class="eye-btn" :aria-label="showLoginPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showLoginPassword" @click="showLoginPassword = !showLoginPassword">
              <EyeOff v-if="showLoginPassword" :size="15" aria-hidden="true" />
              <Eye v-else :size="15" aria-hidden="true" />
            </button>
          </div>
        </label>

        <p v-if="error" id="login-form-error" class="form-error" role="alert">{{ error }}</p>

        <CaptchaChallenge ref="captchaRef" action="login" @update:token="captchaToken = $event" />

        <button type="submit" class="submit-btn" :disabled="loginLoading || !loginForm.username.trim() || !loginForm.password">
          {{ loginLoading ? '登录中…' : '登 录' }}
        </button>

        <!-- TG 一键登录入口：跳到站点机器人私聊，菜单首项即免密登录（后端未配置机器人时不渲染） -->
        <div v-if="tgBotUsername" class="tg-login">
          <div class="tg-login-divider" aria-hidden="true"><span>或</span></div>
          <a class="tg-login-btn" :href="`https://t.me/${tgBotUsername}`" target="_blank" rel="noopener noreferrer">
            <Send :size="16" aria-hidden="true" /> 用 Telegram 登录
          </a>
          <p class="tg-login-hint">在机器人私聊里点「一键免密进入控制面板」即可免密登录（需先绑定过 Telegram）</p>
        </div>
      </form>

      <!-- 注册表单 -->
      <form v-else id="panel-register" class="auth-form" role="tabpanel" aria-labelledby="tab-register" tabindex="0" @submit.prevent="handleRegister">
        <label class="field">
          <span class="field-label">用户名</span>
          <div class="field-box">
            <User :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.username"
              type="text"
              name="username"
              autocomplete="username"
              placeholder="3-32 位字母、数字、下划线"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
          </div>
        </label>

        <label class="field">
          <span class="field-label">密码</span>
          <div class="field-box">
            <Lock :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.password"
              :type="showRegisterPassword ? 'text' : 'password'"
              name="new-password"
              autocomplete="new-password"
              placeholder="至少 6 位"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
            <button type="button" class="eye-btn" :aria-label="showRegisterPassword ? '隐藏密码' : '显示密码'" :aria-pressed="showRegisterPassword" @click="showRegisterPassword = !showRegisterPassword">
              <EyeOff v-if="showRegisterPassword" :size="15" aria-hidden="true" />
              <Eye v-else :size="15" aria-hidden="true" />
            </button>
          </div>
          <div v-if="registerForm.password" class="strength">
            <div class="strength-track">
              <div class="strength-fill" :class="`lv-${passwordStrength}`"></div>
            </div>
            <span class="strength-text" aria-live="polite">{{
              ['弱', '弱', '中', '强'][passwordStrength]
            }}</span>
          </div>
        </label>

        <label class="field">
          <span class="field-label">确认密码</span>
          <div class="field-box">
            <Lock :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.confirmPassword"
              type="password"
              name="confirm-password"
              autocomplete="new-password"
              placeholder="再次输入密码"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
          </div>
        </label>

        <label class="field">
          <span class="field-label">邮箱 <em class="optional">选填</em></span>
          <div class="field-box">
            <Mail :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.email"
              type="email"
              name="email"
              autocomplete="email"
              placeholder="用于找回密码"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
          </div>
        </label>

        <label v-if="invitationEnabled" class="field">
          <span class="field-label">邀请码 <em class="optional">选填，双方得能量</em></span>
          <div class="field-box">
            <Gift :size="16" class="field-icon" aria-hidden="true" />
            <input
              v-model="registerForm.inviteCode"
              type="text"
              name="invite-code"
              placeholder="好友的邀请码"
              :aria-invalid="!!error"
              aria-describedby="register-form-error"
            />
          </div>
        </label>

        <p v-if="registrationClosed" class="closed-notice">当前未开放注册</p>
        <p v-if="configError" class="closed-notice">注册配置拉取失败，<button type="button" class="retry-link" @click="loadRegisterConfig">点重试</button></p>
        <p v-if="error" id="register-form-error" class="form-error" role="alert">{{ error }}</p>

        <CaptchaChallenge ref="captchaRef" action="register" @update:token="captchaToken = $event" />

        <button type="submit" class="submit-btn" :disabled="registerLoading || registrationClosed || !registerForm.username.trim() || !registerForm.password || !registerForm.confirmPassword">
          {{ registerLoading ? '注册中…' : '注 册' }}
        </button>
      </form>

      <p class="auth-footnote">
        {{ mode === 'login' ? '密码加密存储' : '密码加密存储 · 注册成功自动开通观影账号' }}
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
  background: var(--paper-bg);
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
      linear-gradient(to right, color-mix(in srgb, var(--paper-line) 70%, transparent) 1px, transparent 1px),
      linear-gradient(to bottom, color-mix(in srgb, var(--paper-line) 70%, transparent) 1px, transparent 1px);
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
  border-radius: var(--paper-r-md);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--paper-ink-2);
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
  text-indent: 0.2em; /* 补偿末字符后的字距，居中标题视觉不偏左 */
  text-transform: uppercase;
  color: var(--paper-ink);
  margin: 0 0 0.625rem;
  line-height: 1.3;
  overflow-wrap: anywhere;
}

.brand-subtitle {
  font-size: 0.8125rem;
  color: var(--paper-ink-3);
  margin: 0;
  line-height: 1.6;
}

/* 胶囊模式切换 */
.mode-switch {
  box-sizing: border-box;
  position: relative;
  display: grid;
  grid-template-columns: 1fr 1fr;
  height: auto;
  background: var(--paper-card);
  border: 1px solid var(--paper-line);
  border-radius: var(--paper-r-full);
  padding: 4px;
  margin-bottom: 1.5rem;
}

.mode-btn {
  position: relative;
  z-index: 1;
  min-width: 0;
  min-height: 44px;
  padding: 0 0.75rem;
  border: none;
  background: transparent;
  border-radius: var(--paper-r-full);
  font: inherit;
  font-size: 0.875rem;
  font-weight: 500;
  line-height: 1;
  color: var(--paper-ink-3);
  cursor: pointer;
  transition: color 0.2s ease;
  /* 文字在胶囊内严格水平垂直居中（移动端浏览器默认样式可能偏） */
  display: flex;
  align-items: center;
  justify-content: center;
}

/* CJK 光学垂直居中：PingFang SC 等中文字形在行盒内视觉重心偏下，
   line-height: 1 收紧行盒后再微调上移，保证文字在琥珀胶囊内真正居中 */
.mode-btn-label {
  line-height: 1;
  transform: translateY(-0.05em);
}

.mode-btn.active {
  color: var(--paper-on-primary);
  font-weight: 600;
}

.mode-indicator {
  position: absolute;
  /* 4px 与 .mode-switch 的 padding: 4px 对应，改一处需同步 */
  top: 4px;
  left: 4px;
  width: calc(50% - 4px);
  height: calc(100% - 8px);
  border-radius: var(--paper-r-full);
  background: var(--paper-primary);
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
  padding: 0 24px;
  font-size: 0.8125rem;
  font-weight: 500;
  line-height: 1.5;
  color: var(--paper-ink-3);
}

.optional {
  margin-left: 0.25rem;
  font-size: 0.75rem;
  font-style: normal;
  font-weight: 400;
  color: var(--paper-ink-3);
}

.field-box {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  min-height: 52px;
  background: var(--paper-input-bg);
  border: 1px solid var(--paper-line);
  border-radius: var(--paper-r-full);
  padding: 0 24px;
  transition: box-shadow 0.2s ease;
}

.field-box:focus-within {
  border-color: var(--paper-border-focus);
  box-shadow: 0 0 0 3px var(--paper-primary-soft);
}

.field-icon {
  color: var(--paper-ink-3);
  margin-right: 0.625rem;
  flex-shrink: 0;
  transition: color 0.2s ease;
}

.field-box:focus-within .field-icon {
  color: var(--paper-primary);
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
  color: var(--paper-ink);
  font: inherit;
  font-size: 0.9375rem;
}

.field-box input::placeholder {
  color: var(--paper-ink-3);
  opacity: 1;
}

/* 修复2：浏览器自动填充（Chrome/Safari）默认底色会盖住输入框，强制跟随主题 */
.field-box input:-webkit-autofill,
.field-box input:-webkit-autofill:hover,
.field-box input:-webkit-autofill:focus,
.field-box input:-webkit-autofill:active {
  -webkit-text-fill-color: var(--paper-ink);
  -webkit-box-shadow: 0 0 0 1000px var(--paper-input-bg) inset;
  transition: background-color 5000s ease-in-out 0s;
}

.eye-btn {
  position: relative;
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
  color: var(--paper-ink-3);
  cursor: pointer;
  border-radius: 50%;
  transition: color 0.2s ease, background-color 0.2s ease;
}

/* 点击区扩到 44px：视觉保持 32px，用 ::after 透明层扩展 */
.eye-btn::after {
  content: "";
  position: absolute;
  inset: -6px;
}

@media (hover: hover) {
  .eye-btn:hover {
    color: var(--paper-ink-2);
    background: var(--paper-card);
  }
}

.mode-btn:focus-visible,
.eye-btn:focus-visible,
.submit-btn:focus-visible {
  outline: 2px solid var(--paper-border-focus);
  outline-offset: 3px;
}

/* 密码强度 */
.strength {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  margin-top: 0.125rem;
  padding: 0 24px;
}

.strength-track {
  flex: 1;
  height: 3px;
  background: var(--paper-card-2);
  border-radius: var(--paper-r-full);
  overflow: hidden;
}

.strength-fill {
  height: 100%;
  width: 100%;
  border-radius: var(--paper-r-full);
  /* 用 transform: scaleX 代替 width 做进度动画，避免触发布局重排 */
  transform: scaleX(0);
  transform-origin: left center;
  transition: transform 0.3s ease, background-color 0.3s ease;
}

.strength-fill.lv-1 { transform: scaleX(0.33); background: var(--paper-danger); }
.strength-fill.lv-2 { transform: scaleX(0.66); background: var(--paper-warning); }
.strength-fill.lv-3 { transform: scaleX(1); background: var(--paper-primary); }

.strength-text {
  font-size: 0.8125rem;
  color: var(--paper-ink-3);
  min-width: 16px;
}

/* 错误与注册关闭提示保留独立语义色。 */
.form-error {
  margin: 0;
  padding: 0.75rem 24px;
  border-radius: 26px;
  background: var(--paper-danger-soft);
  border: 1px solid var(--paper-danger-border);
  color: var(--paper-danger);
  font-size: 0.8125rem;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.closed-notice {
  margin: 0;
  padding: 0.75rem 24px;
  border-radius: 26px;
  background: var(--paper-warning-soft, var(--paper-danger-soft));
  border: 1px solid var(--paper-warning-border, var(--paper-danger-border));
  color: var(--paper-warning, var(--paper-danger));
  font-size: 0.875rem;
  line-height: 1.5;
  text-align: center;
}

.retry-link {
  background: none;
  border: none;
  padding: 0 8px;
  min-height: 44px;
  color: var(--paper-primary);
  font: inherit;
  text-decoration: underline;
  cursor: pointer;
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
  padding: 0 24px;
  background: var(--paper-gradient);
  border: none;
  border-radius: var(--paper-r-full);
  color: var(--paper-on-primary);
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
  background: var(--paper-card-3);
  color: var(--paper-ink-3);
  cursor: not-allowed;
}

.auth-footnote {
  margin: 1.5rem 0 0;
  text-align: center;
  font-size: 0.8125rem;
  color: var(--paper-ink-3);
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

  .field-box {
    min-height: 52px;
  }

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

/* TG 一键登录入口（描边次级按钮风，与主提交按钮区分主次） */
.tg-login {
  margin-top: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.tg-login-divider {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  color: var(--paper-ink-3);
  font-size: 0.8125rem;
}

.tg-login-divider::before,
.tg-login-divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--paper-line);
}

.tg-login-btn {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  width: 100%;
  min-height: 52px;
  padding: 0 24px;
  background: transparent;
  border: 1.5px solid var(--paper-line-strong);
  border-radius: var(--paper-r-full);
  color: inherit;
  font-size: 0.9375rem;
  font-weight: 600;
  text-decoration: none;
  transition: filter 0.2s ease, border-color 0.2s ease;
}

.tg-login-btn:hover {
  filter: brightness(1.06);
}

.tg-login-btn:focus-visible {
  outline: 2px solid currentColor;
  outline-offset: 2px;
}

.tg-login-hint {
  margin: 0;
  font-size: 0.8125rem;
  line-height: 1.5;
  color: var(--paper-ink-3);
  text-align: center;
}

@media (prefers-reduced-motion: reduce) {
  /* 自动填充的 5000s 背景过渡是防黄色底色的 hack，不是可见动画，排除在外 */
  *:not(:-webkit-autofill) {
    animation: none !important;
    transition: none !important;
  }
}
</style>