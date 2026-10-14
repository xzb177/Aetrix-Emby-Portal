/**
 * 后台错误 → 人话文案（Batch G）
 *
 * 与 `user_frontend/src/utils/toUserMessage.ts` 同源口径的后台对等实现：
 * 同一套「错误码 → 人话」映射，文案逐字对齐，不另起矛盾映射。
 * **同步纪律**：改动映射（新增错误码 / 改文案）必须同时改 user 端那份，两端逐字对齐。
 *
 * 用法：`ElMessage.error(toAdminError(err, '保存失败'))`
 *
 * 解析优先级（与 user 端一致）：
 * 1. 无响应（断网 / 超时）→ "网络暂时连不上，请检查连接后重试"
 * 2. 后端 detail：字符串机器码走 CODE_MAP、中文人话透传（纯英文不透传）；
 *    FastAPI 422 数组取首条人话（纯英文走校验文案）；
 *    对象 detail：机器码走 CODE_MAP；非标准码含中文透传、纯英文走兜底
 * 3. HTTP 状态码兜底（仅 detail 没给出可用信息时）：
 *    401 → 登录过期；403 → 无权限；429 → 太频繁；422 → 参数有误；500 → 服务器开小差
 * 4. 非 axios Error 的 message 场景映射（已知英文技术信息按场景映射，
 *    中文人话透传，纯英文走兜底）
 * 5. 兜底：调用方传入的 fallback 字符串（默认 "请求失败，请稍后重试"）
 *
 * 配套铁律：
 * - 禁用原始错误码 / 英文技术信息上屏（"Request failed with status code 500"、
 *   "panel api error" 等按场景映射）
 * - 后端 detail 本来就是中文人话（如"用户不存在"），透传不改写
 */
const ADMIN_CODE_MAP: Record<string, string> = {
  RATE_LIMITED: '请求有些频繁，请稍后再试',
  TOO_MANY_REQUESTS: '请求有些频繁，请稍后再试',
  BALANCE_INSUFFICIENT: '余额不足，请先充值或选择其他支付方式',
  INSUFFICIENT_BALANCE: '余额不足，请先充值或选择其他支付方式',
  PERMISSION_DENIED: '当前账号没有执行此操作的权限',
  FORBIDDEN: '当前账号没有执行此操作的权限',
  UNAUTHORIZED: '登录已过期，请重新登录后继续',
  SESSION_EXPIRED: '登录已过期，请重新登录后继续',
  TOKEN_EXPIRED: '登录已过期，请重新登录后继续',
  PAY_FAILED: '这次支付未成功，请查看订单状态后再试',
  PAYMENT_FAILED: '这次支付未成功，请查看订单状态后再试',
  PAY_UNKNOWN: '暂时无法确认支付结果，请先查看订单状态',
  PAYMENT_UNKNOWN: '暂时无法确认支付结果，请先查看订单状态',
  PAYMENT_PENDING: '暂时无法确认支付结果，请先查看订单状态',
}

const NETWORK_ERROR_COPY = '网络暂时连不上，请检查连接后重试'
const SERVER_ERROR_COPY = '服务器开小差了，请稍后重试'
const VALIDATION_ERROR_COPY = '提交的内容有误，请检查后重试'
const SESSION_EXPIRED_COPY = '登录已过期，请重新登录后继续'
const PERMISSION_DENIED_COPY = '当前账号没有执行此操作的权限'
const RATE_LIMITED_COPY = '请求有些频繁，请稍后再试'

/** 全大写下划线风格的机器码，如 RATE_LIMITED / SOME_ERROR_2 */
const MACHINE_CODE_RE = /^[A-Z][A-Z0-9_]{2,}$/

/** 纯英文技术信息（无中文），用于判定是否该走场景映射/兜底而非透传 */
const ENGLISH_ONLY_RE = /^[\w\s.,:'"()[\]-]+$/

/** 纯英文无中文 → 不可透传上屏（SOME ENGLISH ERROR 之类不能给用户看） */
function isEnglishOnly(text: string): boolean {
  return ENGLISH_ONLY_RE.test(text) && !/[\u4e00-\u9fff]/.test(text)
}

function isNetworkError(err: any): boolean {
  if (!err || err.response) return false
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
  const hit = ADMIN_CODE_MAP[code]
  return typeof hit === 'string' ? hit : null
}

/**
 * 把任意错误翻译成人话。fallback 为调用方原来的兜底文案（如"保存失败"），
 * 只在确实翻不出人话时用——调用方原有行为不变。
 */
export function toAdminError(err: unknown, fallback = '请求失败，请稍后重试'): string {
  const e: any = err

  // 1. 无响应：断网 / 超时
  if (isNetworkError(e)) {
    return NETWORK_ERROR_COPY
  }

  const status: number | undefined = e?.response?.status
  const detail: unknown = e?.response?.data?.detail

  // 2. detail 解析（优先于状态码：带 detail 的业务错误语义更精确，
  //    比如 401 + "RC 密码错误" 必须显示密码错，而不是"登录已过期"）
  if (typeof detail === 'string' && detail.trim()) {
    const text = detail.trim()
    if (MACHINE_CODE_RE.test(text)) {
      return mapCode(text) ?? fallback
    }
    // P3-1：纯英文技术信息不透传（"SOME ENGLISH ERROR" 不能上屏）
    if (isEnglishOnly(text)) return fallback
    return text
  }
  if (Array.isArray(detail) && detail.length) {
    // FastAPI 422 校验错误是数组：取首条的人话 msg
    const first = detail[0] as { msg?: unknown; message?: unknown } | undefined
    const msg: string =
      first && typeof first.msg === 'string'
        ? first.msg
        : first && typeof first.message === 'string'
          ? first.message
          : ''
    if (msg.trim()) {
      return isEnglishOnly(msg.trim()) ? VALIDATION_ERROR_COPY : msg.trim()
    }
    return VALIDATION_ERROR_COPY
  }
  if (detail && typeof detail === 'object') {
    const code = (detail as { code?: unknown }).code
    const message = (detail as { message?: unknown }).message
    if (typeof code === 'string' && code) {
      const hit = mapCode(code)
      if (hit) return hit
      if (MACHINE_CODE_RE.test(code)) return fallback
      // P3-2：非标准机器码（如小写 user_not_found）→ 含中文透传、纯英文走兜底
      const c = code.trim()
      if (!c || isEnglishOnly(c)) return fallback
      return c
    }
    if (typeof message === 'string' && message.trim()) {
      const msg = message.trim()
      if (isEnglishOnly(msg)) return fallback
      return msg
    }
  }

  // 3. 状态码兜底：仅当 detail 没给出可用信息时
  if (status === 401) return SESSION_EXPIRED_COPY
  if (status === 403) return PERMISSION_DENIED_COPY
  if (status === 429) return RATE_LIMITED_COPY
  if (status === 422) return VALIDATION_ERROR_COPY
  if (status === 500) return SERVER_ERROR_COPY

  // 4. 非 axios 错误兜底：Error message 若是人话可透传，机器味则按场景映射
  if (e instanceof Error && e.message && !MACHINE_CODE_RE.test(e.message.trim())) {
    const text = e.message.trim()
    if (/^network error$/i.test(text)) return NETWORK_ERROR_COPY
    if (/panel api error/i.test(text)) return SERVER_ERROR_COPY
    const statusMatch = /^request failed with status code (\d+)$/i.exec(text)
    if (statusMatch) {
      const code = Number(statusMatch[1])
      if (code === 401) return SESSION_EXPIRED_COPY
      if (code === 403) return PERMISSION_DENIED_COPY
      if (code === 429) return RATE_LIMITED_COPY
      if (code === 422) return VALIDATION_ERROR_COPY
      if (code >= 500) return SERVER_ERROR_COPY
      return fallback
    }
    // P3-3：纯英文技术信息走兜底，中文人话才透传
    if (isEnglishOnly(text)) return fallback
    return text
  }

  // 5. 兜底
  return fallback
}
