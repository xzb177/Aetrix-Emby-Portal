<script setup lang="ts">
/**
 * 人机验证挂件（能力：人机验证）
 *
 * 支持 Cloudflare Turnstile / Google reCAPTCHA / hCaptcha —— 用哪家由管理员在
 * 后台「系统设置 → 人机验证」里选，站点密钥与脚本地址一起下发，这里不写死任何提供方。
 *
 * 行为约定（与后端一致）：
 * - **未配置或该动作没开保护 → 整个组件不渲染**，不会出现「一个空的验证框」；
 * - 脚本加载失败时明确提示，并把令牌留空（后端会拒绝，提示语比静默失败清楚得多）；
 * - 令牌是一次性的：登录/注册失败后由父组件调 ``reset()`` 重新取一个，
 *   否则用户第二次点击会因为「令牌已被使用」而莫名失败。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { AlertTriangle, ShieldCheck } from 'lucide-vue-next'
import { authApi, type CaptchaInfo } from '@/api'

const props = defineProps<{ action: 'login' | 'register' }>()
const emit = defineEmits<{ (e: 'update:token', token: string): void }>()

const info = ref<CaptchaInfo | null>(null)
const boxRef = ref<HTMLElement | null>(null)
const failed = ref(false)
const ready = ref(false) // 挂件渲染完成（加载中的占位骨架依据）
const widgetId = ref<string | number | null>(null)
let scriptEl: HTMLScriptElement | null = null

/** 挂件全局对象：三家提供方的 SDK 名字不同 */
function providerGlobal(): any {
  const name = info.value?.provider
  if (name === 'turnstile') return (window as any).turnstile
  if (name === 'recaptcha') return (window as any).grecaptcha
  if (name === 'hcaptcha') return (window as any).hcaptcha
  return null
}

function loadScript(url: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${url}"]`)
    if (existing) {
      existing.addEventListener('load', () => resolve())
      if (providerGlobal()) resolve()
      else existing.addEventListener('error', () => reject(new Error('script error')))
      return
    }
    const el = document.createElement('script')
    el.src = url
    el.async = true
    el.defer = true
    el.onload = () => resolve()
    el.onerror = () => reject(new Error('script error'))
    scriptEl = el
    document.head.appendChild(el)
  })
}

/** 等 SDK 自己挂好全局对象（脚本 onload 与全局可用之间有极短的空档） */
async function waitForGlobal(timeoutMs = 6000): Promise<any> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const g = providerGlobal()
    if (g) return g
    await new Promise((r) => setTimeout(r, 80))
  }
  return null
}

function tokenOf(response: unknown): string {
  if (typeof response === 'string') return response
  if (response && typeof response === 'object' && 'token' in response) {
    return String((response as { token?: string }).token || '')
  }
  return ''
}

async function render() {
  const current = info.value
  if (!current?.enabled || !current.actions[props.action]) return
  const global = await waitForGlobal()
  if (!global || !boxRef.value) {
    failed.value = true
    return
  }
  try {
    const options = {
      sitekey: current.site_key,
      theme: 'dark' as const,
      callback: (response: unknown) => emit('update:token', tokenOf(response)),
      'expired-callback': () => emit('update:token', ''),
      'error-callback': () => emit('update:token', ''),
    }
    if (current.provider === 'recaptcha' && typeof global.ready === 'function') {
      await new Promise<void>((resolve) => global.ready(() => resolve()))
    }
    widgetId.value = global.render(boxRef.value, options)
    ready.value = true
  } catch {
    failed.value = true
  }
}

/** 令牌一次性：每次提交失败后都要换一个新的（父组件调用） */
function reset() {
  const global = providerGlobal()
  emit('update:token', '')
  if (global && widgetId.value !== null) {
    try {
      global.reset(widgetId.value)
    } catch {
      /* 提供方拒绝重置时忽略：用户也可以手动重试 */
    }
  }
}

defineExpose({ reset })

onMounted(async () => {
  try {
    info.value = await authApi.captcha()
  } catch {
    return // 后端拿不到就不显示挂件，登录流程本身不受影响
  }
  if (!info.value.enabled || !info.value.actions[props.action]) return
  try {
    await loadScript(info.value.script_url)
  } catch {
    failed.value = true
    return
  }
  await render()
})

onBeforeUnmount(() => {
  // 只负责清掉这次插入的脚本标签；SDK 全局对象留着给页面上的其他挂件复用
  if (scriptEl && scriptEl.parentNode) scriptEl.parentNode.removeChild(scriptEl)
})
</script>

<template>
  <div v-if="info?.enabled && info.actions[action]" class="captcha-block">
    <div v-if="!ready && !failed" class="au-skeleton" style="height:48px" aria-hidden="true"></div>
    <div ref="boxRef" class="captcha-box"></div>
    <p v-if="failed" class="captcha-error">
      <AlertTriangle :size="13" />
      {{ info.label }} 脚本加载失败（可能是网络或域名未在提供方登记），请刷新重试
    </p>
    <p v-else class="captcha-hint">
      <ShieldCheck :size="13" />
      点一下证明你不是机器人
    </p>
  </div>
</template>

<style scoped>
.captcha-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 2px 0 4px;
}

.captcha-hint,
.captcha-error {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--font-size-small);
  line-height: 1.5;
  margin: 0;
  color: var(--text-muted);
}

.captcha-hint :deep(svg),
.captcha-error :deep(svg) { flex-shrink: 0; }

.captcha-error { color: var(--au-danger); }
</style>
