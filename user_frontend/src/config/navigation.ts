/**
 * 全站导航的唯一一份定义
 *
 * 用户端此前是三套并行导航：桌面顶栏写着一组文字链接，移动端底部坞又是另一组
 * 条目（名字、顺序都不同），顶栏还另有一个 ☰ 抽屉，合起来就是「一个像 App 的
 * 底部栏 + 一个像网页的顶栏」，看着像两个导航、两套产品。这里收敛成一份：
 *
 *   primaryNav    主导航：全断点同一份定义 —— 桌面在顶栏渲染（≥900px 横排，
 *                 769~900px 顶栏第二行滑动选项卡），手机在底部坞渲染。
 *                 顶栏不再有第二个抽屉；手机不再有第二套底部坞。
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
 * v2.42.4（纸片人口径定稿）：主导航收敛为 首页 / 补给站 / 我的 三项——
 * 补给站 = 商城 + 钱包一体（能量、余额、充值、套餐、消费记录都在 /wallet），
 * 参考纸片人的「总览 / Emby / 补给站」三分法：导航只留高频目的地，媒体库与
 * 搜索退到头像菜单与页内入口（页面本身保留，不在主导航占位）。
 *
 * v2.11.0：补给站 / 钱包拆分——补给站（/store）只管花钱/花能量（充能量/买会员/
 * 花能量/核销），钱包（/wallet）只管看资产（余额/订单/流水/探险家等级）。
 * 主导航变为 首页 / 补给站 / 钱包 / 我的 四项。
 *
 * v4.1：头像菜单改为「探险设施直达」——直接列出 @/config/game.ts 的 10 个
 * 游戏设施（图标+名称+描述），点卡片直达对应 /game/xxx 页面。设施的
 * 名称/路径/图标/描述唯一数据源是 game.ts，这里不再重复定义。
 * 原来的 12 个通用菜单项（探险预告、探险日志、我的订单、能量账本、
 * 每日报到、邀请返利、许愿池中心、AI 助手、求救信号支持、信鸽、外观、
 * 管理后台）已从头像菜单移除，对应页面路由保留。
 */

import type { Component } from 'vue'
import {
  Clapperboard, Globe, Server, ShoppingBag, User,
} from 'lucide-vue-next'

export interface NavItem {
  /** 显示名（顶栏与底部坞共用） */
  name: string
  /** 路由内路径，必须已在 src/router/index.ts 声明 */
  path: string
  icon: Component
}

/** 主导航：全断点共用同一份（桌面顶栏横排、手机底部坞）。
 *  五项定稿（2026-10-17 用户拍板，DESIGN.md v1.1）：
 *  发现（/） / 世界（/game，含全部 /game/** 设施） / 服务器（/server） /
 *  补给站（/shop） / 我的（/game/account 账号设置）。
 *  叫法统一「发现」（废「首页」）；收藏家（/collector）页面保留，
 *  但退出主导航、入口收进发现页。
 *  媒体库与搜索仍可从头像菜单 / 首页帮助区 / 顶栏资产 pill 上下文进入，
 *  不在主导航占位 */
export const primaryNav: NavItem[] = [
  { name: '发现', path: '/', icon: Clapperboard },
  { name: '世界', path: '/game', icon: Globe },
  { name: '服务器', path: '/server', icon: Server },
  { name: '补给站', path: '/shop', icon: ShoppingBag },
  { name: '我的', path: '/game/account', icon: User },
]

/**
 * 判定当前路由命中的主导航项（最长前缀匹配）：
 * '/' 只精确命中；其余项 path 相等或 routePath 以 "path/" 开头即命中，
 * 取命中项中 path 最长的 —— 解决 /game 与 /game/account 的包含关系
 *（/game/account 命中「我的」而不是「世界」）。
 * 无命中返回 ''（如 /tg-bind、404 等非导航路由）。
 */
export function resolveActiveNavPath(routePath: string): string {
  let best = ''
  for (const item of primaryNav) {
    const p = item.path
    const matched =
      p === '/' ? routePath === '/' : routePath === p || routePath.startsWith(`${p}/`)
    if (matched && p.length > best.length) best = p
  }
  return best
}
