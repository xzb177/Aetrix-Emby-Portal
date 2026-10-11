/**
 * 首页访问记忆：「好久不见」欢迎卡的数据层（装修第二期 U2）。
 *
 * 设计要点：
 * - localStorage 只存**时间戳**，不存影片/进度/账号资料；
 * - key 按账号隔离：`aetrix:home:last-visit:v1:<accountId>`，切号不串；
 * - 先读后写：读到的是"上一次"访问时间，写的是"这一次"；
 * - localStorage 损坏/禁用/配额满 → 静默降级（不显示欢迎卡，不抛错）。
 *
 * 防负优化：纯同步读写、无网络请求；调用方在 onMounted/watch 里调用，
 * 不阻塞首屏、不推迟任何数据请求。
 */

/** 严格大于 72 小时（3 天）才算"好久不见" */
export const WELCOME_BACK_AFTER_MS = 72 * 60 * 60 * 1000

const KEY_PREFIX = 'aetrix:home:last-visit:v1'

function storageKey(accountId: number | string): string {
  return `${KEY_PREFIX}:${accountId}`
}

function readTimestamp(key: string): number | null {
  try {
    const raw = localStorage.getItem(key)
    if (raw == null) return null
    const ts = Number(raw)
    // 损坏的值（非数字/负数/0）一律视为无记忆
    if (!Number.isFinite(ts) || ts <= 0) return null
    return ts
  } catch {
    // localStorage 禁用等：静默降级
    return null
  }
}

function writeTimestamp(key: string): void {
  try {
    localStorage.setItem(key, String(Date.now()))
  } catch {
    // localStorage 禁用/配额满：静默降级
  }
}

/**
 * 评估并记录一次首页访问。
 *
 * @returns 是否应显示"好久不见"欢迎卡：
 *   - 无记忆（首次访问）→ false
 *   - ≤72h → false（不打扰）
 *   - 严格 >72h → true
 *   - accountId 为空 / localStorage 不可用 → false
 */
export function checkAndRecordHomeVisit(accountId: number | string | null | undefined): boolean {
  if (accountId == null || accountId === '') return false
  const key = storageKey(accountId)
  const last = readTimestamp(key)
  // 先读后写：无论显不显示，这次访问都要记下来
  writeTimestamp(key)
  if (last == null) return false
  return Date.now() - last > WELCOME_BACK_AFTER_MS
}
