/**
 * 探险体力（活力值）文案配置 —— 唯一数据源，组件里不 hardcode 文案。
 * 后续要走后端配置时，替换本文件的数据源即可，组件不用改。
 */
export interface StaminaText {
  title: string
  unit: string
  full: string
  low: string
  empty: string
  emptyHint: string
  checkinRecovered: string
}

export const staminaText: StaminaText = {
  title: '探险体力',
  unit: '点',
  full: '体力充沛，出发！',
  low: '体力不足，明天记得早点来报到',
  empty: '体力耗尽',
  emptyHint: '今日探险额度已用完，明日报到恢复',
  checkinRecovered: '报到成功，体力已恢复，明日继续探险！',
}

/** 体力槽展示阈值：低于此比例显示不足警告样式 */
export const STAMINA_LOW_RATIO = 0.3

/** 体力上限兜底：后端没给 max 时的显示值（R41：GameHub 状态条魔法数字 `?? 14` 收敛到此） */
export const DEFAULT_STAMINA_MAX = 14
