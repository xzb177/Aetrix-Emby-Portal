/**
 * v4 游戏化「探险日志」入口配置 —— 唯一数据源
 *
 * 所有游戏设施的名称、路径、图标、描述都在这里定义，不在组件里 hardcode。
 * 新增游戏页面时：1) 在 router/index.ts 加路由；2) 在这里加一条 facilities 记录。
 * 后续如需走后端配置（管理后台可配），把这里换成接口拉取即可，组件层不用改。
 */

export interface GameFacility {
  /** 唯一 key */
  key: string
  /** 显示名（卡片标题） */
  name: string
  /** 路由路径，必须已在 src/router/index.ts 声明 */
  path: string
  /** 表情图标（与各游戏页 h1 的 emoji 保持一致） */
  icon: string
  /** 一句话介绍 */
  desc: string
  /** 印章文字（v4-stamp 风格） */
  stamp: string
}

/** 探险日志 Hub 页本身的文案（也可配） */
export const gameHubConfig = {
  /** Hub 路由 */
  path: '/game',
  /** 导航入口显示名 */
  navName: '探险日志',
  /** Hub 页印章 */
  stamp: '探险家 · 大本营',
  /** Hub 页标题 */
  title: '🏕️ 探险日志',
  /** Hub 页副标题 */
  subtitle: '营地的每一处设施都在这里了，挨个逛逛吧。',
} as const

/** 全部游戏设施（营地设施） */
export const gameFacilities: GameFacility[] = [
  {
    key: 'checkin',
    name: '每日报到',
    path: '/game/checkin',
    icon: '📋',
    desc: '每天到营地报到盖章，领能量，连报有加成。',
    stamp: '营地 · 报到处',
  },
  {
    key: 'calendar',
    name: '探险预告',
    path: '/game/calendar',
    icon: '🗺️',
    desc: '看看接下来有什么探险活动，别错过。',
    stamp: '营地 · 探险地图',
  },
  {
    key: 'wishlist',
    name: '许愿池',
    path: '/game/wishlist',
    icon: '🏮',
    desc: '把心愿投进池子里，说不定哪天就实现了。',
    stamp: '心愿 · 待实现',
  },
  {
    key: 'signal',
    name: '求救信号',
    path: '/game/signal',
    icon: '📡',
    desc: '发出求救信号，等好心人来搭把手。',
    stamp: '求救 · 已发出',
  },
  {
    key: 'pigeon',
    name: '信鸽',
    path: '/game/pigeon',
    icon: '🕊️',
    desc: '信鸽替你送信，看看有没有回音。',
    stamp: '信鸽 · 传书中',
  },
  {
    key: 'ledger',
    name: '能量账本',
    path: '/game/ledger',
    icon: '📒',
    desc: '每一笔能量的来龙去脉，都记在这本账上。',
    stamp: '探险日志 · 营地设施',
  },
  {
    key: 'supply',
    name: '补给记录',
    path: '/game/supply',
    icon: '🧾',
    desc: '在补给站拿过的东西，这里都有小票。',
    stamp: '探险日志 · 营地设施',
  },
  {
    key: 'companion',
    name: '结伴同行',
    path: '/game/companion',
    icon: '🤝',
    desc: '一个人探险太孤单，找个搭子一起走。',
    stamp: '营地门口 · 组队处',
  },
  {
    key: 'guide',
    name: '向导',
    path: '/game/guide',
    icon: '🧭',
    desc: '营地一角的老向导，有事就问他。',
    stamp: '营地一角 · 老向导',
  },
  {
    key: 'outfit',
    name: '换装',
    path: '/game/outfit',
    icon: '👕',
    desc: '换套行头，以全新的面貌出发。',
    stamp: '衣帽间 · 试衣中',
  },
]
