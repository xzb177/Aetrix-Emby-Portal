<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { Send, RefreshCw, Check, X, Copy } from 'lucide-vue-next'
import { tgApi } from '@/api/tg'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'bound'): void
}>()

const toast = useToast()

const loading = ref(false)
const verifying = ref(false)
const unbinding = ref(false)
const code = ref('')
const botUsername = ref('')
const expiresIn = ref(0)
const countdown = ref(0)
const verifyError = ref('')
const success = ref(false)
const copied = ref(false)
// 已绑定状态：弹窗打开时先查 /status，避免已绑定用户仍看到绑定码对话框
const alreadyBound = ref(false)
const boundTelegramId = ref<number | null>(null)

let timer: ReturnType<typeof setInterval> | null = null
let closeTimer: ReturnType<typeof setTimeout> | null = null

function rawErrMsg(e: unknown): string {
  const anyErr = e as { response?: { data?: unknown }; message?: string }
  const data = anyErr?.response?.data
  if (data && typeof data === 'object') {
    // FastAPI HTTPException → { detail }；业务返回 → { message }
    const d = data as { message?: unknown; detail?: unknown }
    if (typeof d.message === 'string') return d.message
    if (typeof d.detail === 'string') return d.detail
  }
  if (typeof data === 'string') return data
  return anyErr?.message || '网络错误，请稍后重试'
}

/** 后端原文含中文保留原文，否则人话化 */
function errMsg(e: unknown): string {
  const msg = rawErrMsg(e)
  return /[\u4e00-\u9fff]/.test(msg) ? msg : humanizeError(e)
}

function stopCountdown() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

function startCountdown(seconds: number) {
  stopCountdown()
  countdown.value = seconds
  timer = setInterval(() => {
    countdown.value -= 1
    if (countdown.value <= 0) {
      countdown.value = 0
      stopCountdown()
    }
  }, 1000)
}

async function loadCode() {
  loading.value = true
  verifyError.value = ''
  success.value = false
  try {
    const res = await tgApi.bindCode()
    code.value = res.code
    botUsername.value = res.bot_username
    expiresIn.value = res.expires_in
    startCountdown(res.expires_in)
  } catch (e) {
    code.value = ''
    toast.error(errMsg(e))
  } finally {
    loading.value = false
  }
}

async function refreshCode() {
  await loadCode()
}

async function onVerify() {
  if (verifying.value || !code.value || countdown.value <= 0) return
  verifying.value = true
  verifyError.value = ''
  try {
    const res = await tgApi.verify()
    if (res.success) {
      success.value = true
      emit('bound')
      toast.success('Telegram 绑定成功')
      closeTimer = setTimeout(close, 3000)
    } else {
      verifyError.value = res.message || '验证失败，请确认已给 Bot 发送正确验证码后重试'
      // 绑定码因全站异常尝试过多被作废：倒计时归零，引导用户重新生成
      if (res.code_expired) countdown.value = 0
    }
  } catch (e) {
    verifyError.value = errMsg(e)
  } finally {
    verifying.value = false
  }
}

async function copyCode() {
  if (!code.value) return
  try {
    await navigator.clipboard.writeText(code.value)
    copied.value = true
    toast.success('已复制到剪贴板')
    setTimeout(() => {
      copied.value = false
    }, 1500)
  } catch {
    toast.error('复制失败，请手动复制')
  }
}

async function onUnbind() {
  if (unbinding.value) return
  if (!window.confirm('确定要解绑 Telegram 吗？解绑后公益服功能（签到/积分/红包/抽奖）将受限。')) return
  unbinding.value = true
  try {
    const res = await tgApi.unbind()
    if (res && res.success) {
      alreadyBound.value = false
      boundTelegramId.value = null
      emit('bound')
      toast.success('已解绑 Telegram')
      // 切回绑定码流程，方便用户直接重新绑定
      await loadCode()
    } else {
      toast.error('解绑失败，请稍后重试')
    }
  } catch (e) {
    toast.error(errMsg(e))
  } finally {
    unbinding.value = false
  }
}

function close() {
  emit('update:modelValue', false)
}

/** 弹窗打开：先查绑定状态，已绑定则展示已绑定面板，不再生成新码 */
async function onOpen() {
  loading.value = true
  verifyError.value = ''
  success.value = false
  alreadyBound.value = false
  boundTelegramId.value = null
  try {
    const st = await tgApi.status()
    if (!props.modelValue) return // 弹窗已关闭：丢弃过期响应
    if (st && st.bound) {
      alreadyBound.value = true
      boundTelegramId.value = st.telegram_id
      emit('bound') // 刷新父组件徽章（Bot 端绑定后网页徽章可能过期）
      loading.value = false
      return
    }
  } catch {
    // 状态查询失败：降级走原有绑定码流程
  }
  await loadCode()
}

watch(
  () => props.modelValue,
  (v) => {
    if (v) {
      onOpen()
    } else {
      stopCountdown()
      alreadyBound.value = false
      boundTelegramId.value = null
      if (closeTimer) {
        clearTimeout(closeTimer)
        closeTimer = null
      }
    }
  }
)

onBeforeUnmount(() => {
  stopCountdown()
  if (closeTimer) clearTimeout(closeTimer)
})
</script>

<template>
  <div v-if="modelValue" class="modal-mask" @click.self="close">
    <div class="modal-box tg-bind-modal">
      <button class="tg-close" type="button" aria-label="关闭" @click="close">
        <X :size="18" />
      </button>

      <div v-if="success" class="tg-success">
        <div class="tg-success-icon">
          <Check :size="30" />
        </div>
        <h3>绑定成功</h3>
        <p>Telegram 账号已成功绑定，3 秒后自动关闭</p>
      </div>

      <div v-else-if="alreadyBound" class="tg-success">
        <div class="tg-success-icon">
          <Check :size="30" />
        </div>
        <h3>Telegram 已绑定</h3>
        <p>Telegram ID：{{ boundTelegramId }}</p>
        <p class="tg-hint">如需更换账号，可先解绑再重新绑定。</p>
        <div class="tg-actions">
          <button class="au-btn" type="button" :disabled="unbinding" @click="onUnbind">
            {{ unbinding ? '解绑中…' : '解绑' }}
          </button>
        </div>
      </div>

      <div v-else-if="loading" class="tg-loading" role="status" aria-label="正在生成绑定码">
        <div class="au-skeleton tg-code-skeleton"></div>
        <p>正在生成绑定码…</p>
      </div>

      <div v-else class="tg-body">
        <h3>绑定 Telegram</h3>
        <p v-if="!botUsername" class="tg-error">站点尚未配置 Telegram Bot，请联系管理员</p>
        <p v-else class="tg-hint">
          在 Telegram 中给 <b>@{{ botUsername }}</b> 发送下面的 6 位数字
        </p>

        <div class="tg-code-row">
          <div class="tg-code">{{ code || '------' }}</div>
          <button class="tg-copy" type="button" title="复制验证码" @click="copyCode">
            <Copy :size="16" />
            <span>{{ copied ? '已复制' : '复制' }}</span>
          </button>
        </div>

        <div class="tg-countdown" :class="{ expired: countdown <= 0 }">
          {{ countdown > 0 ? `${countdown} 秒后失效` : '验证码已过期，请换一个码' }}
        </div>

        <div v-if="verifyError" class="tg-error">{{ verifyError }}</div>

        <div class="tg-actions">
          <button
            class="au-btn au-btn-primary"
            type="button"
            :disabled="verifying || countdown <= 0 || !botUsername"
            @click="onVerify"
          >
            <Send :size="16" />
            {{ verifying ? '验证中…' : '我已发送，验证' }}
          </button>
          <button class="au-btn" type="button" :disabled="loading" @click="refreshCode">
            <RefreshCw :size="16" />
            换一个码
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(4px);
}

.modal-box {
  position: relative;
  width: min(420px, calc(100vw - 32px));
  padding: 28px 24px 24px;
  border-radius: 16px;
  background: var(--au-surface, #1a1a22);
  border: 1px solid var(--au-border, rgba(255, 255, 255, 0.08));
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.5);
  color: var(--text-primary, #ececf1);
}

.tg-close {
  position: absolute;
  top: 12px;
  right: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--text-secondary, #9a9ab0);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.tg-close:hover {
  background: var(--au-surface-2, rgba(255, 255, 255, 0.06));
  color: var(--text-primary, #ececf1);
}

.tg-body h3,
.tg-success h3 {
  margin: 0 0 8px;
  font-size: 18px;
  font-weight: 600;
}

.tg-hint {
  margin: 0 0 18px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--text-secondary, #9a9ab0);
}

.tg-hint b {
  color: var(--au-primary, #7c5cff);
  font-weight: 600;
}

.tg-code-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.tg-code {
  flex: 1;
  padding: 14px 16px;
  border-radius: 12px;
  background: var(--au-bg, rgba(0, 0, 0, 0.3));
  border: 1px dashed var(--au-border, rgba(255, 255, 255, 0.12));
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  font-size: 30px;
  font-weight: 700;
  letter-spacing: 8px;
  text-align: center;
  color: var(--au-primary, #7c5cff);
}

.tg-copy {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--au-border, rgba(255, 255, 255, 0.1));
  border-radius: 10px;
  background: transparent;
  color: var(--text-secondary, #9a9ab0);
  font-size: 13px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}

.tg-copy:hover {
  color: var(--text-primary, #ececf1);
  border-color: var(--au-primary, #7c5cff);
}

.tg-countdown {
  margin-bottom: 16px;
  font-size: 13px;
  color: var(--text-secondary, #9a9ab0);
}

.tg-countdown.expired {
  color: var(--au-danger, #ff5c6c);
}

.tg-error {
  margin-bottom: 16px;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(255, 92, 108, 0.1);
  border: 1px solid rgba(255, 92, 108, 0.3);
  font-size: 13px;
  line-height: 1.5;
  color: var(--au-danger, #ff5c6c);
}

.tg-actions {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.tg-loading,
.tg-success {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 24px 0 8px;
  text-align: center;
}

.tg-loading p,
.tg-success p {
  margin: 0;
  font-size: 14px;
  color: var(--text-secondary, #9a9ab0);
}

.tg-success-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: rgba(61, 220, 151, 0.12);
  color: var(--au-success, #3ddc97);
}

.tg-code-skeleton {
  height: 48px;
  width: 100%;
}
</style>
