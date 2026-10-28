/**
 * Toast 文案目录（装修第二期 U4：Toast 文案人性化）
 *
 * 文案规范：
 * - 句式 = 结果 + 必要时的下一步；成功用"已……"开头
 * - 禁用"操作失败 / 系统异常 / 未知错误"、禁用原始错误码、禁用责备语气
 * - 禁用"已到账"类超前承诺；支付"未知"与"失败"严格区分
 * - 参数化函数用于带动态信息的文案；失败兜底为纯字符串（供 toUserMessage 使用）
 *
 * 防负优化：纯文案改动，零性能影响；调用方保持 `toast.success()` / `toast.error()` 原样。
 */

/** 成功 / 信息类文案（参数化函数） */
export const toastCopy = {
  /** 通用复制成功 */
  copied: (label = '内容') => `${label}已复制，可以去粘贴了`,
  /** 邀请链接复制（带分享引导） */
  inviteLinkCopied: () => '邀请链接已复制，可以分享给朋友了',
  /** 邀请码复制（带分享引导） */
  inviteCodeCopied: () => '邀请码已复制，可以分享给朋友了',

  /** 订阅终止 */
  subscriptionCancelled: () => '订阅已终止',
  /** 设备移除 */
  deviceRemoved: () => '设备已移除',
  /** 密码修改（改密码不掉登录，无需"请重新登录"） */
  passwordChanged: () => '密码已修改，播放密码已同步',
}

/** 系统级文案（登录过期 / 网络 / 支付状态 / 权限…，供拦截器与 toUserMessage 使用） */
export const toastSystem = {
  /** 无响应：断网 / 超时 / CORS */
  networkError: '网络暂时连不上，请检查连接后重试',
  /** 401：token 刷新也失败后 */
  sessionExpired: '登录已过期，请重新登录后继续',
  /** 403 */
  permissionDenied: '这个操作需要更高权限，换个账号试试或找管理员',
  /** 429 */
  rateLimited: '请求有些频繁，请稍后再试',
  /** 余额不足 */
  balanceInsufficient: '余额不足，请先充值或选择其他支付方式',
  /** 支付已确认失败（与"未知"严格区分，不鼓励重复支付） */
  payFailed: '这次支付未成功，请查看订单状态后再试',
  /** 支付结果未知（轮询超时等，不鼓励重复支付） */
  payUnknown: '暂时无法确认支付结果，请先查看订单状态',
  /** 500：服务端故障（Batch G：拦截器四场景之一） */
  serverError: '服务器开小差了，请稍后重试',
  /** 422：请求参数校验失败（Batch G：拦截器四场景之一） */
  validationError: '提交的内容有误，请检查后重试',
} as const

/**
 * 失败兜底文案（纯字符串）。
 * 调用方式：`toast.error(toUserMessage(err, 'checkinFailed'))`
 * toUserMessage 会优先使用后端返回的人话 detail / 状态码映射，兜底才用这里。
 */
export const toastFallback = {
  /** 通用兜底 */
  saveFailed: '暂时没能保存，请稍后重试',
  cancelFailed: '终止没能完成，请稍后重试',
  toggleAutoRenewFailed: '自动续订开关没能切换，请稍后重试',
  renewFailed: '续期没能完成，请稍后重试',
  removeFailed: '移除没能完成，请稍后重试',
  settingFailed: '设置没能保存，请稍后重试',
  /** 数据加载类兜底（Batch G：humanizeError 统一收敛到 toUserMessage 时用） */
  loadFailed: '加载失败，稍后再试',
  /** 结束播放兜底（R41：结束播放失败不再借用"保存失败"文案，动作对不上） */
  stopFailed: '没能结束这次播放，请稍后重试',
} as const

export type ToastFallbackKey = keyof typeof toastFallback

/** 删除 / 关闭类确认弹窗文案：标题 + 后果说明（是否可恢复） */
export const confirmCopy = {
  /** 关闭求救信号：可重新打开 */
  closeTicket: '关闭这条求救信号？\n关闭后如有需要可以重新打开。',
  /** 终止订阅：不可恢复（ProfileView 已有等效文案，此处统一收口） */
  cancelSubscription: (planName: string) =>
    `确定要终止「${planName}」订阅吗？终止后立即失效，不可恢复。`,
} as const
