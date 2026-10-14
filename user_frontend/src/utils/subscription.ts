/**
 * 订阅已用占比（0~100）：按订阅自己的 start_date → end_date 计算。
 * 口径统一处：useServerCards 的会员进度条。组件层做 100-x 转剩余方向。
 */
export function subscriptionUsedPercent(startDate: string, endDate: string): number {
  const start = new Date(startDate).getTime()
  const end = new Date(endDate).getTime()
  if (!start || !end || end <= start) return 0
  const total = end - start
  const used = Math.min(Math.max(Date.now() - start, 0), total)
  return Math.max(1, Math.min(100, Math.round((used / total) * 100)))
}
