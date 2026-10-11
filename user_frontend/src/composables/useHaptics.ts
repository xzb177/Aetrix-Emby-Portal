/**
 * 触觉反馈（Haptics）
 *
 * 只在三个关键节点调用：
 * - 签到成功 / 支付成功 → success()（15ms）
 * - 删除确认 → confirm()（10ms）
 * 普通点击、导航、复制一律不振。
 *
 * 防负优化：
 * - 能力检测：不支持 navigator.vibrate 的浏览器（iOS Safari 等）静默跳过
 * - reduced-motion 用户不振动
 * - 500ms 节流，防连振
 * - try/catch 包裹，永不抛错
 */

const THROTTLE_MS = 500

let lastBuzzAt = 0

function vibrateSupported(): boolean {
  if (typeof navigator === 'undefined') return false
  try {
    return 'vibrate' in navigator && typeof navigator.vibrate === 'function'
  } catch {
    return false
  }
}

function reduceMotionPreferred(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

function buzz(pattern: number | number[]): boolean {
  const now = Date.now()
  if (now - lastBuzzAt < THROTTLE_MS) return false
  if (!vibrateSupported() || reduceMotionPreferred()) return false
  try {
    const ok = navigator.vibrate(pattern)
    if (ok) lastBuzzAt = now
    return !!ok
  } catch {
    return false
  }
}

export function useHaptics() {
  return {
    /** 成功类（签到成功 / 支付成功）：短促 15ms */
    success: () => buzz(15),
    /** 确认类（删除确认）：更轻 10ms */
    confirm: () => buzz(10),
  }
}
