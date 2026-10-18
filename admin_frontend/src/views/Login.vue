<script setup lang="ts">
/**
 * 管理员登录页：JWT 登录 → 恢复 ?redirect 或进入概览
 *
 * 暗房影院：与用户端 LoginView 同一张「认证卡」——暖黑底 + 胶片颗粒、实色卡片 + 发丝线、
 * 琥珀放映机标、衬线大写拉字距的站名、带图标的输入框（聚焦琥珀光环）、琥珀实心提交按钮。
 * 只多一枚「管理控制台」眉题，让管理员一眼分清自己在哪一端。
 */
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Clapperboard, Loader2, Lock, ShieldCheck, User } from 'lucide-vue-next'
import { login } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'
// 人机验证（能力：人机验证）：管理员开了「保护管理后台登录」时才会渲染
import CaptchaChallenge from '@/components/CaptchaChallenge.vue'
// 站名与 Logo 来自「站点与品牌」能力（未配置时用默认值）
import { branding, initBranding } from '@/composables/branding'

void initBranding()

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const form = reactive({ username: '', password: '' })
const loading = ref(false)
const error = ref('')
const captchaToken = ref('')
const captchaRef = ref<InstanceType<typeof CaptchaChallenge> | null>(null)

async function submit() {
  if (!form.username || !form.password) {
    error.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const res = await login({
      username: form.username,
      password: form.password,
      captcha_token: captchaToken.value || undefined,
    })
    auth.setSession(res.access_token, res.user)
    const redirect = (route.query.redirect as string) || '/'
    router.push(redirect)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '登录失败'
    // 令牌一次性：失败后换一个新的，否则第二次点击会因「已使用」而失败
    captchaRef.value?.reset()
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <!-- 暗房影院：背景只有暖黑底 + 全局胶片颗粒（base.css），不再有光斑 -->
    <div class="auth-card">
      <div class="auth-brand">
        <div class="brand-mark">
          <img v-if="branding.logo_url" :src="branding.logo_url" :alt="branding.site_name" />
          <Clapperboard v-else :size="22" />
        </div>
        <p class="au-eyebrow brand-eyebrow">管理控制台</p>
        <h1 class="brand-title">{{ branding.site_name }}</h1>
        <p class="brand-subtitle">运营管理后台 · 仅限管理员账号登录</p>
      </div>

      <!-- 首次运行向导刚完成：告诉用户用刚建的账号登录 -->
      <p v-if="route.query.initialized === '1'" class="auth-hint">
        初始化完成，管理员账号已创建，请登录
      </p>

      <!-- 门户（用户端）已登录时的免登结果：能自动进就直接进，不能进也要说明原因 -->
      <p v-if="auth.ssoNotice" class="auth-hint">{{ auth.ssoNotice }}</p>

      <form class="auth-form" @submit.prevent="submit">
        <label class="field">
          <span class="field-label">用户名</span>
          <span class="field-box">
            <User :size="16" class="field-icon" />
            <input v-model="form.username" type="text" name="username" autocomplete="username" placeholder="管理员用户名" />
          </span>
        </label>
        <label class="field">
          <span class="field-label">密码</span>
          <span class="field-box">
            <Lock :size="16" class="field-icon" />
            <input v-model="form.password" type="password" name="password" autocomplete="current-password" placeholder="登录密码" />
          </span>
        </label>

        <CaptchaChallenge ref="captchaRef" @update:token="captchaToken = $event" />

        <p v-if="error" class="form-error" role="alert">{{ error }}</p>

        <button class="submit-btn" type="submit" :disabled="loading">
          <Loader2 v-if="loading" :size="16" class="spinning" />
          <span>{{ loading ? '登录中…' : '登录' }}</span>
        </button>
      </form>

      <p class="auth-foot"><ShieldCheck :size="13" /> 登录状态保存在本地，操作会记入管理日志</p>
    </div>
  </div>
</template>

<style scoped>
.auth-page {
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.5rem;
  background: var(--au-bg);
}

.auth-card {
  width: 100%;
  max-width: 400px;
  background: var(--au-surface);
  /* ui/beautify-20261018：顶部暖光叠加（ radial 琥珀光，不抢琥珀主色） */
  background-image: radial-gradient(ellipse 120% 90% at 50% 0%, rgba(232, 168, 74, 0.07), transparent 60%);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  padding: 2rem 1.75rem 1.5rem;
  box-shadow: var(--au-card-highlight), var(--au-shadow-2);
  animation: au-fade-up var(--au-fade-in) var(--au-ease) both;
}

.auth-brand {
  text-align: center;
  margin-bottom: 1.5rem;
}

.brand-mark {
  width: 46px;
  height: 46px;
  margin: 0 auto 0.75rem;
  border-radius: var(--au-r-md);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--au-primary);
  /* ui/beautify-20261018：琥珀淡底 + 琥珀发丝描边 */
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
}

.brand-mark img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: inherit;
}

.brand-eyebrow { margin: 0 0 0.375rem; }

/* 站名：衬线（base.css 的 h1）+ 大写 + 拉开字距，同用户端 .brand-title */
.brand-title {
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: 0.28em;
  text-transform: uppercase;
  color: var(--au-text);
  margin: 0 0 0.5rem;
}

.brand-subtitle {
  font-size: 0.8125rem;
  color: var(--au-text-3);
  margin: 0;
  line-height: 1.5;
}

.auth-hint {
  font-size: 0.8125rem;
  color: var(--au-text-2);
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  border-radius: var(--au-r-sm);
  padding: 0.5rem 0.75rem;
  margin: 0 0 1rem;
  line-height: 1.5;
}

.auth-form {
  display: flex;
  flex-direction: column;
  gap: 0.875rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

.field-label {
  font-size: 0.8125rem;
  font-weight: 500;
  color: var(--au-text-3);
}

.field-box {
  display: flex;
  align-items: center;
  height: 44px;
  background: var(--au-input-bg);
  border: 1px solid var(--au-input-border);
  border-radius: var(--au-r-md);
  padding: 0 0.75rem;
  transition: border-color var(--au-fast) var(--au-ease), box-shadow var(--au-fast) var(--au-ease),
    background var(--au-fast) var(--au-ease);
}

.field-box:focus-within {
  background: var(--au-input-bg-focus);
  border-color: var(--au-border-focus);
  box-shadow: 0 0 0 3px var(--au-primary-soft);
}

.field-icon {
  color: var(--au-text-3);
  margin-right: 0.5rem;
  flex-shrink: 0;
}

.field-box:focus-within .field-icon { color: var(--au-primary); }

.field-box input {
  flex: 1;
  min-width: 0;
  height: 100%;
  background: transparent;
  border: none;
  outline: none;
  color: var(--au-text);
  font-size: 0.875rem;
}

.field-box input::placeholder { color: var(--au-text-4); }

.form-error {
  margin: 0;
  padding: 0.5rem 0.75rem;
  border-radius: var(--au-r-sm);
  background: var(--au-danger-soft);
  border: 1px solid var(--au-danger-border);
  color: var(--au-danger);
  font-size: 0.8125rem;
  line-height: 1.4;
}

.submit-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  height: 46px;
  margin-top: 0.25rem;
  background: var(--au-primary);
  border: none;
  border-radius: var(--au-r-md);
  color: var(--au-on-primary);
  font-size: 0.9375rem;
  font-weight: 600;
  cursor: pointer;
  transition: filter var(--au-fast) var(--au-ease), transform var(--au-fast) var(--au-ease);
}

.submit-btn:hover:not(:disabled) { filter: brightness(1.08); }
.submit-btn:active:not(:disabled) { transform: scale(0.98); }

/* 禁用态：中性表面 + 三级字（不做「褪色琥珀」，同用户端 .au-btn-primary:disabled） */
.submit-btn:disabled {
  background: var(--au-surface-2);
  color: var(--au-text-3);
  box-shadow: inset 0 0 0 1px var(--au-border);
  cursor: not-allowed;
}

.submit-btn:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }

.spinning { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.auth-foot {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font-size: 0.75rem;
  color: var(--au-text-4);
  margin: 1.25rem 0 0;
}

/* 手机：卡片少一点内边距，键盘弹起时不被顶出屏幕 */
@media (max-width: 480px) {
  .auth-page { padding: 14px; align-items: flex-start; padding-top: max(28px, env(safe-area-inset-top)); }
  .auth-card { padding: 1.625rem 1.25rem 1.25rem; }
  .brand-title { font-size: 1.25rem; }
}
</style>
