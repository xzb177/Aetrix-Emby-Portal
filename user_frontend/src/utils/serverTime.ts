/**
 * PA-047：服务端时间解析口（user_frontend 唯一入口）。
 *
 * 后端下发的时间串是 UTC 时刻的 naive ISO：后端用 datetime.now()（生产容器
 * 为 UTC）写库、isoformat() 下发，无时区后缀，如 '2026-11-01T18:30:00'。
 * 浏览器 new Date(naive) 会按本地时区解释，在 Asia/Shanghai 下把 UTC 时刻
 * 当成北京时间，显示早 8 小时（北京 02:30 签到会落到前一天）。
 *
 * 规则：
 * - 带时区后缀（Z / ±HH:MM）的串：原样交给 new Date，不动；
 * - 无后缀的日期时间串：按 UTC 解析（补 Z），再由调用方的本地 getter
 *  （getDate/getHours/toLocaleString）转成本地时间展示；
 * - 纯日期串（YYYY-MM-DD）：那是日期不是时刻，保持 new Date 原样，不换算。
 */
const NAIVE_DATETIME_RE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)$/

export function parseServerTime(iso: string | null | undefined): Date {
  if (!iso) return new Date(NaN)
  const m = NAIVE_DATETIME_RE.exec(iso.trim())
  if (m) return new Date(`${m[1]}T${m[2]}Z`)
  return new Date(iso)
}
