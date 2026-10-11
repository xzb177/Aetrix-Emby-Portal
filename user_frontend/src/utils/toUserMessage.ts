/**
 * 业务错误 → 人话文案（装修第二期 U4）
 *
 * 用法：`toast.error(toUserMessage(err, 'checkinFailed'))`
 *
 * 解析优先级：
 * 1. 无响应（断网 / 超时）→ "网络暂时连不上，请检查连接后重试"
 * 2. HTTP 状态码：401 → 登录过期；403 → 无权限；429 → 太频繁
 * 3. 后端 detail：人话字符串直接透传；全大写错误码走 CODE_MAP；未知码走兜底
 * 4. 兜底：调用方传入的 fallbackKey（默认 "暂时没能保存，请稍后重试"）
 *
 * 配套铁律：
 * - 禁用原始错误码进 Toast；一次失败只弹一条（去重由 useToast 的 800ms 去重保证）
 * - 后端 detail 本来就是中文人话（如"用户不存在"），透传不改写
 */
import { toastFallback, toastSystem, type ToastFallbackKey } from '@/constants/toastMessages'

/** 已知的后端结构化错误码 → 系统文案（目前后端只有一个结构码，其余为中文人话） */
const CODE_MAP: Record<string, string> = {
  RATE_LIMITED: toastSystem.rateLimited,
  TOO_MANY_REQUESTS: toastSystem.rateLimited,
  BALANCE_INSUFFICIENT: toastSystem.balanceInsufficient,
  INSUFFICIENT_BALANCE: toastSystem.balanceInsufficient,
  PERMISSION_DENIED: toastSystem.permissionDenied,
  FORBIDDEN: toastSystem.permissionDenied,
  UNAUTHORIZED: toastSystem.sessionExpired,
  SESSION_EXPIRED: toastSystem.sessionExpired,
  TOKEN_EXPIRED: toastSystem.sessionExpired,
  PAY_FAILED: toastSystem.payFailed,
  PAYMENT_FAILED: toastSystem.payFailed,
  PAY_UNKNOWN: toastSystem.payUnknown,
  PAYMENT_UNKNOWN: toastSystem.payUnknown,
  PAYMENT_PENDING: toastSystem.payUnknown,
}

/** 全大写下划线风格的机器码，如 RATE_LIMITED / SOME_ERROR_2 */
const MACHINE_CODE_RE = /^[A-Z][A-Z0-9_]{2,}$/

function isNetworkError(err: any): boolean {
  if (!err || err.response) return false
  // axios 网络错误：code 为 ERR_NETWORK / ECONNABORTED，或经典 "Network Error"
  const code = err.code as string | undefined
  const message = err.message as string | undefined
  return (
    code === 'ERR_NETWORK' ||
    code === 'ECONNABORTED' ||
    code === 'ETIMEDOUT' ||
    (typeof message === 'string' &&
      (/network error/i.test(message) || /timeout/i.test(message) || /请求超时/.test(message)))
  )
}

function mapCode(code: string): string | null {
  const hit = CODE_MAP[code]
  return typeof hit === 'string' ? hit : null
}

export function toUserMessage(err: unknown, fallbackKey: ToastFallbackKey = 'saveFailed'): string {
  const fallback = toastFallback[fallbackKey] ?? toastFallback.saveFailed
  const e: any = err

  // 1. 无响应：断网 / 超时
  if (isNetworkError(e)) {
    return toastSystem.networkError
  }

  const status: number | undefined = e?.response?.status
  const detail: unknown = e?.response?.data?.detail

  // 2. 状态码映射（优先于 detail，因为 401/403/429 的语义是确定的）
  if (status === 401) return toastSystem.sessionExpired
  if (status === 403) return toastSystem.permissionDenied
  if (status === 429) return toastSystem.rateLimited

  // 3. detail 解析
  if (typeof detail === 'string' && detail.trim()) {
    const text = detail.trim()
    // 机器码 → 查表；查不到按兜底（不把码直接展示给用户）
    if (MACHINE_CODE_RE.test(text)) {
      return mapCode(text) ?? fallback
    }
    // 后端返回的人话（如"用户不存在"）直接透传
    return text
  }
  if (detail && typeof detail === 'object') {
    const code = (detail as { code?: unknown }).code
    const message = (detail as { message?: unknown }).message
    if (typeof code === 'string' && code) {
      const hit = mapCode(code)
      if (hit) return hit
      // TG_NOT_BOUND 等已有全局事件处理的码，不在这里弹 Toast
      if (code === 'TG_NOT_BOUND') return fallback
      if (MACHINE_CODE_RE.test(code)) return fallback
      return code
    }
    if (typeof message === 'string' && message.trim()) {
      return message.trim()
    }
  }

  // 非 axios 错误兜底：Error message 若是人话可透传，机器味则用 fallback
  if (e instanceof Error && e.message && !MACHINE_CODE_RE.test(e.message.trim())) {
    // 避免把 "Network Error" 这类英文技术信息直接展示
    if (/^network error$/i.test(e.message.trim())) return toastSystem.networkError
    return e.message
  }

  // 4. 兜底
  return fallback
}
