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

  /** 签到成功：有积分信息就带上，没有就用通用文案 */
  checkinSuccess: (points?: number | null) =>
    typeof points === 'number' && points > 0
      ? `签到成功，+${points} 积分`
      : '签到成功，今天的记录已更新',

  /** 兑换成功 */
  exchangeSuccess: () => '兑换成功，可在兑换记录中查看',
  /** 卡码核销成功 */
  redeemSuccess: () => '卡码核销成功',

  /** 求片提交（不承诺处理时效） */
  requestSubmitted: () => '求片已提交，可在求片记录中查看进度',
  /** 求片撤回 */
  requestWithdrawn: () => '已撤回该求片',

  /** 工单提交（不承诺处理时效） */
  ticketSubmitted: () => '工单已提交，可在工单详情中查看回复',
  /** 工单关闭 */
  ticketClosed: () => '工单已关闭',

  /** 订阅终止 */
  subscriptionCancelled: () => '订阅已终止',
  /** 设备移除 */
  deviceRemoved: () => '设备已移除',
  /** 密码修改（改密码不掉登录，无需"请重新登录"） */
  passwordChanged: () => '密码已修改，播放密码已同步',
  /** 播放密码设置 */
  playPasswordSet: () => '播放密码已设置',

  /** 收藏 */
  favoriteAdded: () => '已加入收藏',
  favoriteRemoved: () => '已取消收藏',
  /** 标记看过 */
  markedWatched: () => '已标记为看过',
  markedUnwatched: () => '已标记为未看',

  /** 积分转账成功 */
  transferSuccess: (recipient: string, amount: number | string, fee?: number | string) =>
    `已转给 ${recipient} ${amount} 积分` +
    (Number(fee) > 0 ? `（手续费 ${fee} 积分）` : ''),

  /** 支付成功（不承诺"已到账"，到账由轮询/订单状态确认） */
  paySuccess: (itemName: string) => `「${itemName}」支付成功`,
}

/** 系统级文案（登录过期 / 网络 / 支付状态 / 权限…，供拦截器与 toUserMessage 使用） */
export const toastSystem = {
  /** 无响应：断网 / 超时 / CORS */
  networkError: '网络暂时连不上，请检查连接后重试',
  /** 401：token 刷新也失败后 */
  sessionExpired: '登录已过期，请重新登录后继续',
  /** 403 */
  permissionDenied: '当前账号没有执行此操作的权限',
  /** 429 */
  rateLimited: '请求有些频繁，请稍后再试',
  /** 余额不足 */
  balanceInsufficient: '余额不足，请先充值或选择其他支付方式',
  /** 支付已确认失败（与"未知"严格区分，不鼓励重复支付） */
  payFailed: '这次支付未成功，请查看订单状态后再试',
  /** 支付结果未知（轮询超时等，不鼓励重复支付） */
  payUnknown: '暂时无法确认支付结果，请先查看订单状态',
  /** 没有可用支付方式（配置问题，不推给管理员） */
  noPayMethod: '当前没有可用的支付方式，请稍后再试',
  /** 没拿到支付链接 */
  payLinkFailed: '没能获取到支付链接，请稍后重试',
} as const

/**
 * 失败兜底文案（纯字符串）。
 * 调用方式：`toast.error(toUserMessage(err, 'checkinFailed'))`
 * toUserMessage 会优先使用后端返回的人话 detail / 状态码映射，兜底才用这里。
 */
export const toastFallback = {
  /** 通用兜底 */
  saveFailed: '暂时没能保存，请稍后重试',
  transferFailed: '转账没能完成，请稍后重试',
  redeemFailed: '核销没能完成，请稍后重试',
  checkinFailed: '签到没能完成，请稍后重试',
  submitFailed: '提交没能完成，请稍后重试',
  withdrawFailed: '撤回没能完成，请稍后重试',
  voteFailed: '附议没能完成，请稍后重试',
  ticketCreateFailed: '工单没能创建，请稍后重试',
  ticketReplyFailed: '回复没能发送，请稍后重试',
  ticketCloseFailed: '工单没能关闭，请稍后重试',
  cancelFailed: '终止没能完成，请稍后重试',
  unlockFailed: '解锁没能完成，请稍后重试',
  removeFailed: '移除没能完成，请稍后重试',
  settingFailed: '设置没能保存，请稍后重试',
  stopFailed: '结束播放没能完成，请稍后重试',
  orderFailed: '下单没能完成，请稍后重试',
  markReadFailed: '标记没能完成，请稍后重试',
  favoriteFailed: '收藏没能更新，请稍后重试',
  markWatchedFailed: '标记没能更新，请稍后重试',
  calendarFailed: '日历暂时没能加载，请稍后重试',
  loadDetailFailed: '详情暂时没能加载，请稍后重试',
  loadEpisodesFailed: '剧集暂时没能加载，请稍后重试',
  loadEpisodeListFailed: '选集暂时没能加载，请稍后重试',
  loadMediaFailed: '影片信息暂时没能加载，请稍后重试',
} as const

export type ToastFallbackKey = keyof typeof toastFallback

/** 删除 / 关闭类确认弹窗文案：标题 + 后果说明（是否可恢复） */
export const confirmCopy = {
  /** 关闭工单：可重新打开 */
  closeTicket: '关闭这条工单？\n关闭后如有需要可以重新打开。',
  /** 终止订阅：不可恢复（ProfileView 已有等效文案，此处统一收口） */
  cancelSubscription: (planName: string) =>
    `确定要终止「${planName}」订阅吗？终止后立即失效，不可恢复。`,
} as const
