/**
 * 有界并发批量执行器。
 *
 * 管理后台的批量操作（批量禁用用户、批量退款订单…）都要走这里：
 * 并发默认 2（避免把后端 API 打爆）、单项失败不重试（逐项 try/catch，失败项继续下一项），
 * 最后返回汇总，失败的行键保留给调用方（可继续留在选中态里重试）。
 */

/** 单项执行结果：失败时附上错误文案 */
export interface BulkItemResult<K> {
  key: K
  ok: boolean
  error?: string
}

/** 批量执行汇总：failedKeys 保持调用方传入的顺序 */
export interface BulkSummary<K> {
  total: number
  succeeded: number
  failed: number
  failedKeys: K[]
}

/** 单次批量操作的建议上限：超过时调用方应先提示用户拆分 */
export const BULK_LIMIT = 100

/** 并发上限：Math.min(concurrency, keys.length) 个 worker 抢下标 */
export async function runBulkOperation<K>(
  keys: K[],
  fn: (key: K) => Promise<unknown>,
  opts?: {
    /** 并发数，默认 2；<1 或非整数会抛错 */
    concurrency?: number
    /** 每完成一项回调（done 1..total） */
    onProgress?: (done: number, total: number) => void
  },
): Promise<BulkSummary<K>> {
  const concurrency = opts?.concurrency ?? 2
  if (!Number.isInteger(concurrency) || concurrency < 1) {
    throw new RangeError('concurrency 必须为正整数')
  }

  const total = keys.length
  // 按输入顺序预建结果槽位，避免完成顺序打乱 failedKeys 的顺序
  const results: BulkItemResult<K>[] = keys.map((key) => ({ key, ok: false }))
  let nextIndex = 0
  let done = 0

  async function worker(): Promise<void> {
    while (nextIndex < total) {
      // 取下标后立即推进：同一项只被一个 worker 领到
      const index = nextIndex
      nextIndex += 1
      const result = results[index]
      if (!result) return
      try {
        await fn(result.key)
        result.ok = true
      } catch (e: unknown) {
        // 单项失败只记录，不重试，不影响其他项
        result.error = (e as Error)?.message
      }
      done += 1
      opts?.onProgress?.(done, total)
    }
  }

  // 不用 Promise.all 直接起 N 个请求：N=100 时会把后端打爆；worker 池天然限流
  await Promise.all(
    Array.from({ length: Math.min(concurrency, total) }, () => worker()),
  )

  const failedKeys = results.filter((r) => !r.ok).map((r) => r.key)
  return {
    total,
    succeeded: total - failedKeys.length,
    failed: failedKeys.length,
    failedKeys,
  }
}

/** 批量汇总文案：verb 是「禁用」「退款」这类动词 */
export function bulkSummaryText(
  s: { succeeded: number; failed: number; total: number },
  verb: string,
): string {
  if (s.total === 0) return `没有可${verb}的项`
  if (s.failed === 0) return `已${verb} ${s.total} 项`
  if (s.succeeded === 0) return `${s.total} 项${verb}失败，请稍后重试`
  return `已${verb} ${s.succeeded} 项，${s.failed} 项未完成`
}
