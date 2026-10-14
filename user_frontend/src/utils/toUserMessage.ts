/**
 * 业务错误 → 人话文案（装修第二期 U4；Batch G 起为两端唯一错误呈现入口）
 *
 * 用法：`toast.error(toUserMessage(err, 'checkinFailed'))`
 *
 * 解析优先级：
 * 1. 无响应（断网 / 超时）→ "网络暂时连不上，请检查连接后重试"
 * 2. HTTP 状态码：401 → 登录过期；403 → 无权限；429 → 太频繁；
 *    422 → 参数有误；500 → 服务器开小差
 * 3. 后端 detail：人话字符串直接透传；FastAPI 422 数组取首条人话；
 *    全大写错误码走 CODE_MAP；未知码走兜底
 * 4. 兜底：调用方传入的 fallbackKey（默认 "暂时没能保存，请稍后重试"）
 *
 * 配套铁律：
 * - 禁用原始错误码进 Toast；一次失败只弹一条（去重由 useToast 的 800ms 去重保证）
 * - 后端 detail 本来就是中文人话（如"用户不存在"），透传不改写
 * - 已知英文技术信息（"Request failed with status code 500"、"panel api error" 等）
 *   按场景映射，永不直接上屏
 * - R42 架构师拍板（r40 遗留"是否与管理端统一为 detail 优先"的结论）：
 *   用户端保持"状态码优先于 detail"，与管理端 toAdminError（detail 优先）有意不一致 ——
 *   401/429/500 语义确定，curated 文案带行动指引（重新登录/慢一点/稍后重试），
 *   用户不需要 500 的诊断细节；两端受众不同，不一致是设计不是漂移。
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

/** R41：中文技术细节关键词——命中即不透传，落到兜底（不把技术细节甩给用户） */
const TECH_LEAK_KEYWORDS = ['数据库', 'sql', '连接失败', '超时', '异常', '堆栈', 'traceback']

/** R42：5xx 只认"技术语境"才算泄漏 —— "Internal Server Error / 服务器内部错误 /
 *  status 5xx / 5xx error" 这类才吞掉；"积分数必须是 500 的整数倍"这类正常数字不误杀。
 *  （R41 的裸 '500' 子串会误杀所有含 500/1500/5000 的数字：实证 backend/api/economy.py:556
 *  vitality_point_cost 可配置，取 500 时人话指引被吞成通用兜底；且裸子串拦不住真正的泄漏形态
 *  "Internal Server Error"（无数字），已收紧） */
const SERVER_ERROR_LEAK_RE =
  /internal server error|服务器内部错误|\bstatus\b.{0,12}\b5\d\d\b|\b5\d\d\b.{0,12}(错误|error|失败|exception)/i

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

  // 2. 状态码映射（优先于 detail，因为 401/403/429/500 的语义是确定的）
  if (status === 401) return toastSystem.sessionExpired
  if (status === 403) return toastSystem.permissionDenied
  if (status === 429) return toastSystem.rateLimited
  if (status === 500) return toastSystem.serverError

  // 3. detail 解析
  if (typeof detail === 'string' && detail.trim()) {
    const text = detail.trim()
    // 机器码 → 查表；查不到按兜底（不把码直接展示给用户）
    if (MACHINE_CODE_RE.test(text)) {
      return mapCode(text) ?? fallback
    }
    // R41：中文里夹带技术细节（数据库/SQL/超时/堆栈…）时不透传，落到兜底
    // R42：5xx 另走 SERVER_ERROR_LEAK_RE（技术语境才算泄漏），裸数字不误杀
    if (
      TECH_LEAK_KEYWORDS.some((k) => text.toLowerCase().includes(k)) ||
      SERVER_ERROR_LEAK_RE.test(text)
    ) {
      return fallback
    }
    // 后端返回的人话（如"用户不存在"）直接透传
    return text
  }
  if (Array.isArray(detail) && detail.length) {
    // FastAPI 422 校验错误是数组：取首条的人话 msg
    // R40：detail=[null] 之类畸形数组里 first 为空时给 ''，防止 msg.trim() 抛错
    const first = detail[0] as { msg?: unknown; message?: unknown } | undefined
    const msg: string =
      first && typeof first.msg === 'string'
        ? first.msg
        : first && typeof first.message === 'string'
          ? first.message
          : ''
    if (msg.trim()) {
      return /^[\w\s.,:'"()[\]-]+$/.test(msg.trim()) && !/[\u4e00-\u9fff]/.test(msg)
        ? toastSystem.validationError
        : msg.trim()
    }
    return toastSystem.validationError
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
  // 422：detail 没给出可用信息时，用通用校验文案
  if (status === 422) return toastSystem.validationError

  // 非 axios 错误兜底：Error message 若是人话可透传，机器味则按场景映射
  if (e instanceof Error && e.message && !MACHINE_CODE_RE.test(e.message.trim())) {
    const text = e.message.trim()
    // 已知英文技术信息 → 按场景映射，永不直接上屏（Batch G）
    if (/^network error$/i.test(text)) return toastSystem.networkError
    if (/panel api error/i.test(text)) return toastSystem.serverError
    const statusMatch = /^request failed with status code (\d+)$/i.exec(text)
    if (statusMatch) {
      const code = Number(statusMatch[1])
      if (code === 401) return toastSystem.sessionExpired
      if (code === 403) return toastSystem.permissionDenied
      if (code === 429) return toastSystem.rateLimited
      if (code === 422) return toastSystem.validationError
      if (code >= 500) return toastSystem.serverError
      return fallback
    }
    return text
  }

  // 4. 兜底
  return fallback
}
