/**
 * 追新日历 API（门户后端 GET /api/user/emby/calendar）
 *
 * ## 为什么不是 Emby 协议端点
 * 「按天分组 + 媒体库维度」是门户自己的展示口径，Emby 协议里没有对应的东西；
 * 用 /emby/Users/me/Items 逼近要按天发 N 次请求，还得在前端把时间戳重新分桶，
 * 分桶口径一旦和后端对不上就会出现「差一天的片子」。
 * 这里一次请求换回按天分好的数据，区间 / 库 / 类型筛选都在后端做。
 *
 * ## 条目字段
 * `items` 里是**原样的 Emby DTO**（Id / Name / Type / ImageTags / SeriesName …）
 * 外加一个 `LibraryName`，所以海报地址直接复用 api/emby.ts 的 posterUrl()，
 * 详情页深链用 `Id` 拼 /media/:id，不再维护第二套下划线结构。
 *
 * ## 口径
 * - 按 `date_added`（入库时间）分组，不是 `date_modified`（会被补全刷新）；
 * - `start` / `end` 都是**含当天**的日历日；
 * - 单日条目详情有上限，但 `count` 永远是当天真实总数（前端显示「+N」靠它）。
 */
import api from '@/api'
import type { EmbyItem } from '@/api/emby'

/** 日历支持的条目类型（与后端 CALENDAR_ITEM_TYPES 一致；不含 season） */
export type CalendarItemType = 'movie' | 'series' | 'episode'

/**
 * 日历里的条目 = Emby DTO + 所属媒体库名。
 *
 * 后端只**追加**这一个字段（``LibraryName``），不动 ``_item_dto`` 的协议字段——
 * 那些字段第三方客户端在用。取交集而不是新建一套下划线结构，是为了让
 * 海报地址直接复用 posterUrl()、详情页深链直接用 ``Id``。
 */
export type CalendarItem = EmbyItem & { LibraryName?: string | null }

export interface CalendarDay {
  /** YYYY-MM-DD（服务器本地日） */
  date: string
  /** 当天真实条数（可能大于 items.length：单日详情有上限） */
  count: number
  items: CalendarItem[]
}

export interface CalendarResponse {
  start: string
  end: string
  total: number
  days: CalendarDay[]
  /** 各类型在当前区间 / 筛选下的条数，用于筛选按钮上的角标 */
  types: Partial<Record<CalendarItemType, number>>
  /**
   * 保密新大陆占位卡（后台可配的揭晓位，见 backend/emby_server/teaser.py）。
   * - null = 后台关了这张卡，前端不渲染；
   * - secret = 未到揭晓日：只有揭晓前文案 + 倒计时，**不含**揭晓后内容；
   * - revealed = 已到日子：标题 / 描述 / 可选跳转链接。
   * 状态由服务端时间判定，不信任客户端时区。
   * undefined = 旧后端（无此字段），视图层回退到静态悬念卡。
   */
  teaser?: CalendarTeaser | null
}

/** 未到揭晓日：只有保密文案。countdown_days 为 null = 没配揭晓时间（一直保密） */
export interface CalendarTeaserSecret {
  status: 'secret'
  title: string
  subtitle: string
  countdown_days: number | null
}

/** 已到日子：揭晓后内容。link 为 null = 卡片不可点 */
export interface CalendarTeaserRevealed {
  status: 'revealed'
  title: string
  description: string
  link: string | null
}

export type CalendarTeaser = CalendarTeaserSecret | CalendarTeaserRevealed

export interface CalendarQuery {
  /** YYYY-MM-DD，含当天 */
  start: string
  /** YYYY-MM-DD，含当天 */
  end: string
  /** 媒体库 id：直接用 /emby/Users/me/Views 返回的 Id（guid），后端两种都收 */
  libraryId?: string
  itemTypes?: CalendarItemType[]
}

/**
 * 请求序号守卫：后发的请求让先发的响应作废。调用方 catch 到 StaleResponseError
 * 时直接 return（旧响应丢弃，不报错、不闪错误态）。
 */
export class StaleResponseError extends Error {
  constructor() { super('stale calendar response'); this.name = 'StaleResponseError' }
}
let fetchSeq = 0

/**
 * 拉一个区间的追新日历。
 *
 * 筛选值会让区间失效，所以不做缓存——上个月翻回去时拿到的必须是当时的库状态。
 */
export async function fetchCalendar(query: CalendarQuery): Promise<CalendarResponse> {
  const mySeq = ++fetchSeq
  const params: Record<string, string> = {
    start: query.start,
    end: query.end,
  }
  if (query.libraryId) params.library_id = query.libraryId
  if (query.itemTypes?.length) params.item_type = query.itemTypes.join(',')

  const res = await api.get<never, Partial<CalendarResponse>>('/api/user/emby/calendar', { params })
  // 发起之后又有新请求：这个响应已过期，丢弃
  if (mySeq !== fetchSeq) throw new StaleResponseError()
  return {
    start: res.start || query.start,
    end: res.end || query.end,
    total: res.total ?? 0,
    days: res.days || [],
    types: res.types || {},
    // 旧后端没有这个字段时保持 undefined，视图层回退到静态悬念卡
    teaser: res.teaser,
  }
}