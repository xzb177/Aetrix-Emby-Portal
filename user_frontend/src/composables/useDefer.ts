/** 把非关键工作推迟到浏览器空闲时执行，避免抢首屏连接/主线程 */
export function deferIdle(fn: () => void, timeout = 1500): void {
  if (typeof requestIdleCallback !== 'undefined') {
    requestIdleCallback(() => fn(), { timeout })
  } else {
    setTimeout(fn, Math.min(timeout, 300))
  }
}
