<template>
  <div v-if="open" class="acct-mask" @click.self="close" @keydown="onKeydown">
    <div class="acct-modal" role="dialog" aria-modal="true" aria-label="修改登录密码">
      <h3 class="acct-modal-title">修改登录密码</h3>
      <div class="acct-field">
        <label class="acct-label" for="pwd-old">当前密码</label>
        <input id="pwd-old" ref="oldPwdInput" v-model="form.oldPassword" type="password" autocomplete="current-password" />
      </div>
      <div class="acct-field">
        <label class="acct-label" for="pwd-new">新密码</label>
        <input id="pwd-new" v-model="form.newPassword" type="password" autocomplete="new-password" />
      </div>
      <div class="acct-field">
        <label class="acct-label" for="pwd-confirm">确认新密码</label>
        <input id="pwd-confirm" v-model="form.confirmPassword" type="password" autocomplete="new-password" />
      </div>
      <p v-if="error" class="acct-error" role="alert">{{ error }}</p>
      <div class="acct-modal-actions">
        <button type="button" class="acct-btn ghost" :disabled="loading" @click="close">取消</button>
        <button type="button" class="acct-btn primary" :disabled="loading" @click="submit">
          {{ loading ? '提交中' : '确认修改' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 修改登录密码弹窗（自旧 AccountSettingsView 原样抽出，行为口径不变）：
 * authApi.changePassword 全仓唯一改密入口；焦点陷阱 / Esc / 关闭后焦点
 * 回触发按钮 / 提交在途不许关，全部沿用原实现。
 */
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { authApi } from '@/api'
import { useToast } from '@/composables/useToast'
import { toastCopy } from '@/constants/toastMessages'
import { toUserMessage } from '@/utils/toUserMessage'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', v: boolean): void }>()

const toast = useToast()
const form = ref({ oldPassword: '', newPassword: '', confirmPassword: '' })
const loading = ref(false)
const error = ref('')
const oldPwdInput = ref<HTMLInputElement | null>(null)
/** 关闭后焦点回到触发按钮 */
const lastTrigger = ref<HTMLElement | null>(null)

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Tab' || !props.open) return
  const card = (e.currentTarget as HTMLElement).querySelector('.acct-modal')
  if (!card) return
  const els = Array.from(
    card.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'),
  ).filter((el) => el.offsetParent !== null)
  if (els.length === 0) return
  const first = els[0]
  const last = els[els.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}
function onEsc(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.open) close()
}
function openModal() {
  error.value = ''
  lastTrigger.value = document.activeElement as HTMLElement | null
  document.body.style.overflow = 'hidden'
  window.addEventListener('keydown', onEsc)
  nextTick(() => oldPwdInput.value?.focus())
}
function close() {
  // 提交在途时不允许关闭，否则在途请求的失败原因会被清空后静默丢失
  if (loading.value) return
  emit('update:open', false)
}
function teardown() {
  document.body.style.overflow = ''
  form.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
  window.removeEventListener('keydown', onEsc)
  lastTrigger.value?.focus?.()
}
watch(
  () => props.open,
  (v) => {
    if (v) openModal()
    else teardown()
  },
)
onUnmounted(() => {
  window.removeEventListener('keydown', onEsc)
  document.body.style.overflow = ''
})

async function submit() {
  const { oldPassword, newPassword, confirmPassword } = form.value
  if (!oldPassword || !newPassword || !confirmPassword) {
    error.value = '请填写完整'
    return
  }
  if (newPassword.length < 6) {
    error.value = '新密码至少 6 位'
    return
  }
  if (newPassword !== confirmPassword) {
    error.value = '两次输入的新密码不一致'
    return
  }
  error.value = ''
  loading.value = true
  try {
    await authApi.changePassword({ old_password: oldPassword, new_password: newPassword })
    toast.success(toastCopy.passwordChanged())
    emit('update:open', false)
    form.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
  } catch (err: any) {
    error.value = toUserMessage(err, 'settingFailed')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* 改密码弹窗（纸色）：自旧 AccountSettingsView 原样迁入 */
.acct-mask {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 24px;
  padding: max(24px, env(safe-area-inset-top)) max(24px, env(safe-area-inset-right)) max(24px, env(safe-area-inset-bottom)) max(24px, env(safe-area-inset-left));
  overflow-y: auto;
  background: var(--scrim);
  z-index: 100;
}
.acct-modal {
  width: 100%;
  max-width: 340px;
  margin: auto;
  max-height: calc(100% - 48px);
  max-height: calc(100dvh - 48px);
  overflow-y: auto;
  padding: 20px;
  background: var(--paper-card);
  border: 1.5px solid var(--ink);
  border-radius: 14px;
  box-shadow: 4px 4px 0 rgba(42, 38, 32, 0.15);
}
.acct-modal-title {
  margin: 0 0 14px;
  font-size: 17px;
  font-weight: 600;
  color: var(--ink);
}
.acct-field { margin-bottom: 12px; }
.acct-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--ink2);
}
.acct-field input {
  width: 100%;
  height: 44px;
  padding: 0 12px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-sm, 8px);
  color: var(--ink);
  font-size: 16px;
  outline: none;
  box-sizing: border-box;
}
.acct-field input:focus { border-color: var(--ink); }
.acct-field input:focus-visible { outline: 2px solid var(--ink); outline-offset: 1px; }
.acct-error {
  margin: 0 0 10px;
  padding: 8px 12px;
  background: var(--au-danger-soft);
  border-radius: var(--paper-r-sm, 8px);
  color: var(--red);
  font-size: 13px;
}
.acct-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 16px;
}
.acct-btn {
  height: 44px;
  padding: 0 16px;
  border-radius: var(--paper-r-md, 10px);
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}
.acct-btn.ghost {
  background: var(--paper2);
  border: 1px solid var(--line);
  color: var(--ink2);
}
.acct-btn.primary {
  background: var(--ink);
  border: 1px solid var(--ink);
  color: var(--on-dark);
  font-weight: 600;
}
.acct-btn:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
