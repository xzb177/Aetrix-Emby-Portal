/**
 * 全站导航的唯一一份定义
 *
 * 用户端此前是三套并行导航：桌面顶栏写着一组文字链接，移动端底部坞又是另一组
 * 条目（名字、顺序都不同），顶栏还另有一个 ☰ 抽屉，合起来就是「一个像 App 的
 * 底部栏 + 一个像网页的顶栏」，看着像两个导航、两套产品。这里收敛成一份：
 *
 *   primaryNav    主导航（三个高频目的地）：全断点在顶栏渲染——
 *                 ≥900px 横排，≤900px 是第二行的横向滑动选项卡。
 *   menuSections  长尾入口（低频功能）：全断点统一收进顶栏头像菜单，
 *                 顶栏不再有第二个抽屉，也不再有底部导航坞。
 *
 * v2.10.0（方案 A）：收藏与观看记录原本是两个一级入口，占掉主导航 3/5 个位置，
 * 而它们本质是**媒体库的两种视图**（本站自己也保留了一份历史数据，但播放器
 * 同时承担着这些功能）。现在降级为媒体库页内的分段标签（`/media?tab=`），
 * 主导航只留 首页 / 媒体库 / 我的 三项，旧地址重定向过来，书签不失效。
 *
 * v2.10.1：搜索升为一级入口（首页 / 媒体库 / 搜索 / 我的）。此前它只是右上角
 * 一个放大镜图标、头像菜单里又写了一条同样的「搜索片名」——同一件事两个入口，
 * 窄屏更只有一个图标可点。搜索是「找片」这个主线上的一步，跟着媒体库排。
 * 一个语义只留一个入口：菜单里的重复条目与顶栏那个图标一并撤掉。
 *
 * 改导航只改这里：顶栏、头像菜单、底部坞三处同时生效，不会再出现
 * 「新页面只加进了一处」这种漏入口。
 *
 * v2.42.4（纸片人口径定稿）：主导航收敛为 首页 / 商店 / 我的 三项——
 * 商店 = 商城 + 钱包一体（积分、余额、充值、套餐、消费记录都在 /wallet），
 * 参考纸片人的「总览 / Emby / 商店」三分法：导航只留高频目的地，媒体库与
 * 搜索退到头像菜单与页内入口（页面本身保留，不在主导航占位）。
 *
 * v2.11.0：商店 / 钱包拆分——商店（/store）只管花钱/花积分（充积分/买会员/
 * 花积分/核销），钱包（/wallet）只管看资产（余额/订单/流水/会员等级）。
 * 主导航变为 首页 / 商店 / 钱包 / 我的 四项。
 */

import type { Component } from 'vue'
import {
  ArrowDownLeft, Bot, CalendarCheck, CalendarDays, Clapperboard, Gift,
  Inbox, MessageSquareDashed, Receipt, ShoppingBag, Ticket, User, Wallet,
  Tent,
} from 'lucide-vue-next'

export interface NavItem {
  /** 显示名（顶栏与底部坞共用） */
  name: string
  /** 路由内路径，必须已在 src/router/index.ts 声明 */
  path: string
  icon: Component
}

export interface NavGroup {
  title: string
  items: NavItem[]
}

/** 主导航：全断点共用同一份（宽屏横排、窄屏底部坞）。
 *  四项定稿（v2.11.0）：商店（/store，只管花钱/花积分）与钱包（/wallet，
 *  只管看资产：余额/订单/流水/会员等级）拆分；此前商店 = /wallet 一体。
 *  媒体库与搜索仍可从头像菜单 / 首页帮助区 / 顶栏资产 pill 上下文进入，
 *  不在主导航占位 */
export const primaryNav: NavItem[] = [
  { name: '首页', path: '/', icon: Clapperboard },
  // v4 游戏化统一入口：探险日志 Hub（/game），直接放主导航（用户要求，不再收进头像菜单）
  { name: '探险日志', path: '/game', icon: Tent },
  { name: '商店', path: '/store', icon: ShoppingBag },
  { name: '钱包', path: '/wallet', icon: Wallet },
  { name: '我的', path: '/profile', icon: User },
]

/** 长尾入口：头像菜单里的分组，桌面与移动端一致。
 *  v2.42.4：钱包已是主导航的「商店」（一个语义只留一处，钱包条目从这里移除）。
 *  v2.42.5：媒体库与搜索页删除——看片在第三方客户端完成，站内只保留
 *  首页观影数据卡与详情 / 播放深链，不再提供站内媒体浏览入口。
 *  v2.42.8：新增「看片」分组放追新日历（按入库时间看每天新上了什么）。 */
export const menuSections: NavGroup[] = [
  {
    title: '看片',
    items: [
      // v2.42.8：站内不再提供媒体浏览（统一走第三方客户端），
      // 但「最近入了什么新」是门户自己能给的，所以单独做了日历页
      { name: '追新日历', path: '/calendar', icon: CalendarDays },
    ],
  },
  {
    title: '账号与经济',
    items: [
      // 订单与流水原来只能进钱包再点分页；直接深链到对应分页，少一步寻找
      { name: '我的订单', path: '/wallet?tab=orders', icon: Receipt },
      { name: '积分流水', path: '/wallet?tab=log', icon: ArrowDownLeft },
      { name: '每日签到', path: '/checkin', icon: CalendarCheck },
      { name: '邀请返利', path: '/invite', icon: Gift },
    ],
  },
  {
    title: '互动与支持',
    items: [
      { name: '求片中心', path: '/request', icon: MessageSquareDashed },
      // AI 助手（v2.19.0）：是否真的能用由管理员的后台配置决定，页面里会说清楚
      { name: 'AI 助手', path: '/assistant', icon: Bot },
      { name: '工单支持', path: '/tickets', icon: Ticket },
      { name: '消息中心', path: '/messages', icon: Inbox },
    ],
  },
]