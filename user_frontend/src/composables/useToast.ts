import { ref } from 'vue'
import { toastCopy } from '@/constants/toastMessages'

export interface ToastMessage {
  id: number
  type: 'success' | 'error' | 'warning' | 'info'
  message: string
  duration?: number
  actionLabel?: string
  onAction?: () => void
}

const messages = ref<ToastMessage[]>([])
let messageId = 0

// 一次失败不弹两条：同一文案 800ms 内重复触发直接丢弃（装修第二期 U4）
// 纯前端去重，零请求、零布局影响
let lastKey = ''
let lastAt = 0
const DEDUP_WINDOW_MS = 800

/** 复制场景的专属文案（带下一步引导）；未知 label 走通用句式 */
const COPY_HINTS: Record<string, string> = {
  '邀请链接': toastCopy.inviteLinkCopied(),
  '邀请码': toastCopy.inviteCodeCopied(),
}

export function useToast() {
  const add = (message: string, type: ToastMessage['type'] = 'info', duration = 3000, opts?: Pick<ToastMessage, 'actionLabel' | 'onAction'>) => {
    const now = Date.now()
    const key = `${type}:${message}`
    if (key === lastKey && now - lastAt < DEDUP_WINDOW_MS) {
      return -1
    }
    lastKey = key
    lastAt = now

    const id = ++messageId
    messages.value.push({ id, type, message, duration, ...opts })

    // 自动关闭
    if (duration > 0) {
      setTimeout(() => {
        remove(id)
      }, duration)
    }

    return id
  }

  const remove = (id: number) => {
    const index = messages.value.findIndex(m => m.id === id)
    if (index > -1) {
      messages.value.splice(index, 1)
    }
  }

  const success = (message: string, duration?: number) => add(message, 'success', duration)
  const error = (message: string, duration?: number) => add(message, 'error', duration)
  /** 带重试的报错：不自动消失，用户点“重试”才执行并关闭本条 */
  const errorWithRetry = (message: string, onRetry: () => void) =>
    add(message, 'error', 0, { actionLabel: '重试', onAction: onRetry })
  const warning = (message: string, duration?: number) => add(message, 'warning', duration)
  const info = (message: string, duration?: number) => add(message, 'info', duration)

  // 复制成功的快捷方法：走文案目录，句式统一为"XX已复制，可以去粘贴了"
  const copySuccess = (item = '内容') => success(COPY_HINTS[item] ?? toastCopy.copied(item))

  return {
    messages,
    add,
    remove,
    success,
    error,
    errorWithRetry,
    warning,
    info,
    copySuccess
  }
}
