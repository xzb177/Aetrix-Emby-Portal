/**
 * Emby 协议 API 客户端
 *
 * 直接复用后端内置的 Emby 兼容协议端点（/emby/*）：
 * - 浏览：Views / Items / Resume / Latest / NextUp / Favorites
 * - 详情：Items/{id} / Shows/{id}/Seasons / Shows/{id}/Episodes
 * - 互动：Rating（收藏）/ PlayedItems（已看）
 *
 * 2026-10-13 旧页面清理：播放相关 wrapper（getPlaybackInfo / reportPlaying /
 * reportProgress / reportStopped）随 WatchView 删除；网页播放走第三方客户端。
 *
 * 鉴权：全部携带门户 JWT（后端 /emby/* 已支持 JWT 回退）。
 * 复用 api/index.ts 的 axios 实例：自动附 Authorization 头 + 401 自动刷新。
 */
import { shallowRef } from 'vue'
import api from '@/api'

/**
 * 分离部署时，门户 EM 与 Emby EA 是两个地址：
 * - /api/* 仍走当前门户域名；
 * - /emby/* 必须走账号卡里的 EA base_url。
 *
 * 以前这里无条件用同源 api，网页端就把 /emby/Users/me/Views 发给了 EM；
 * EM 已关闭 Emby 网关，全部 404，结果就是网页媒体库空白，而 iOS 直连 EA 正常。
 */
const EMBY_BASE_CACHE_KEY = 'emby_base_url'
/** EA 地址缓存有效期：1 小时。过期后下次请求会重新拉账号卡校验，避免
 *  8001→8002 这类服务端换地址后用户端一直拿旧地址连不上的问题。 */
const EMBY_BASE_TTL_MS = 60 * 60 * 1000

let embyBasePromise: Promise<string> | null = null

/** 读缓存：兼容旧版纯字符串格式（旧格式视为已过期，强制重新校验一次） */
function readCachedBase(): { url: string; ts: number } {
  try {
    const raw = localStorage.getItem(EMBY_BASE_CACHE_KEY) || ''
    if (!raw) return { url: '', ts: 0 }
    if (raw.startsWith('{')) {
      const parsed = JSON.parse(raw) as { u?: string; t?: number }
      return { url: (parsed.u || '').replace(/\/+$/, ''), ts: parsed.t || 0 }
    }
    return { url: raw.replace(/\/+$/, ''), ts: 0 }
  } catch {
    return { url: '', ts: 0 }
  }
}

const _cachedBase = readCachedBase()

/**
 * 同步可读的 EA 地址。图片地址是同步函数、模板里直接用，拿不到 Promise，
 * 所以解析后同时写内存和 localStorage，刷新页面也不会先闪一版错地址。
 */
let embyBaseCache: string = _cachedBase.url
let embyBaseTs: number = _cachedBase.ts
/**
 * 同一个 EA 地址的**响应式**副本：图片地址是模板 / computed 里同步拼的，
 * 第一次渲染时地址可能还没拉到（刚登录：登出时缓存已清空，App 启动时又没有 token
 * 跳过了预取）。以前拼出来的是同源 /emby/...，分离部署下 EM 不提供图片 → 整排 404，
 * 而且 computed 不会因为普通变量变化而重算，KeepAlive 的首页就一直是占位卡。
 * imageUrl 读它，地址一到，依赖它的 computed 自动重算。
 */
const embyBaseRef = shallowRef<string>(_cachedBase.url)

function setEmbyBase(base: string): string {
  embyBaseCache = (base || '').replace(/\/+$/, '')
  embyBaseRef.value = embyBaseCache
  embyBaseTs = Date.now()
  try {
    if (embyBaseCache) localStorage.setItem(EMBY_BASE_CACHE_KEY, JSON.stringify({ u: embyBaseCache, t: embyBaseTs }))
    else localStorage.removeItem(EMBY_BASE_CACHE_KEY)
  } catch {
    /* 隐私模式下写不进去无所谓，内存里还有 */
  }
  return embyBaseCache
}

/** 登出时清掉，避免下个登录用户拿到上一个人的 EA 地址 */
export function resetEmbyBaseUrl(): void {
  embyBasePromise = null
  setEmbyBase('')
}

function fetchEmbyBase(): Promise<string> {
  if (!embyBasePromise) {
    embyBasePromise = api
      .get<never, { base_url?: string }>('/api/user/emby/server')
      .then((card) => setEmbyBase(card?.base_url || ''))
      .catch((error) => {
        embyBasePromise = null
        throw error
      })
  }
  return embyBasePromise
}

async function embyBaseUrl(): Promise<string> {
  // 缓存未过期直接用；过期（或没有）则重新拉账号卡，并发去重
  if (embyBaseCache && Date.now() - embyBaseTs <= EMBY_BASE_TTL_MS) return embyBaseCache
  return fetchEmbyBase()
}

/**
 * App 启动时调用：后台重新校验一次 EA 地址，变了就更新（不阻塞首屏）。
 * 未登录时跳过，省一次 401。
 */
export function refreshEmbyBaseUrl(): void {
  try {
    if (!localStorage.getItem('access_token')) return
  } catch {
    return
  }
  fetchEmbyBase().catch(() => {})
}

/**
 * 确保 EA 地址已就绪（图片地址依赖它）；拉不到（未开通 / 网络错误）时返回当前值（可能为空），不抛错。
 */
export async function ensureEmbyBase(): Promise<string> {
  try {
    return await embyBaseUrl()
  } catch {
    return embyBaseCache
  }
}

async function embyGet<T>(path: string, config?: object): Promise<T> {
  return api.get<never, T>(`${await embyBaseUrl()}${path}`, config)
}

async function embyPost<T>(path: string, data?: unknown): Promise<T> {
  return api.post<never, T>(`${await embyBaseUrl()}${path}`, data)
}

async function embyDelete<T>(path: string): Promise<T> {
  return api.delete<never, T>(`${await embyBaseUrl()}${path}`)
}

// ==================== 类型 ====================

export interface EmbyUserData {
  PlaybackPositionTicks: number
  PlayCount: number
  Played: boolean
  IsFavorite: boolean
  LastPlayedDate?: string | null
  /** 剧集未看集数（后端批量计算，电影/单集不看这个值） */
  UnplayedItemCount?: number | null
}

export interface EmbyItem {
  Id: string
  Name: string
  Type: 'Movie' | 'Series' | 'Season' | 'Episode' | string
  IsFolder: boolean
  ChildCount?: number | null
  ProductionYear?: number | null
  CommunityRating?: number | null
  OfficialRating?: string | null
  Overview?: string | null
  Genres?: string[]
  Tags?: string[]
  Studios?: string[]
  PremiereDate?: string | null
  DateCreated?: string | null
  SeriesId?: string | null
  SeriesName?: string | null
  /**
   * 外部 id 来源（Tmdb / Imdb / Douban / Bangumi…），后端 `_item_dto` 按有值的才放。
   * 追新日历的 Rex deep link 靠它拿 TMDB id；没刮削过的条目这里是空对象。
   */
  ProviderIds?: Record<string, string> | null
  SeasonId?: string | null
  ParentIndexNumber?: number | null
  IndexNumber?: number | null
  ImageTags?: Record<string, string>
  BackdropImageTags?: string[]
  UserData: EmbyUserData
  RunTimeTicks?: number | null
  /** 媒体库类型：movies / tvshows / mixed …（Views 接口返回） */
  CollectionType?: string | null
  MediaSources?: EmbyMediaSource[]
  /** 画质徽标用（后端 _item_dto 下发） */
  Width?: number | null
  Height?: number | null
  IsHD?: boolean
  /** 多版本（后端 _item_dto 下发；同目录同名 movie 才有） */
  Versions?: EmbyItemVersion[]
}

/** 电影多版本条目 */
export interface EmbyItemVersion {
  Id: string
  Name: string
  Width?: number | null
  Height?: number | null
  SizeBytes?: number | null
  Container?: string | null
  VersionLabel: string
  IsPrimary: boolean
}

export interface EmbyMediaSource {
  Id: string
  Name?: string
  Container?: string | null
  Size?: number | null
  SupportsDirectPlay?: boolean
  SupportsDirectStream?: boolean
  DirectStreamUrl?: string
  MediaStreams?: Array<{
    Index: number
    Type: string
    Codec?: string
    Language?: string
    DisplayTitle?: string
    IsExternal?: boolean
    IsTextSubtitleStream?: boolean
    IsDefault?: boolean
    /** 字幕轨的可直取地址（服务端已附短期播放签名） */
    DeliveryUrl?: string
  }>
  DefaultSubtitleStreamIndex?: number | null
}

export interface EmbyQuery {
  parentId?: string
  includeTypes?: string[]
  /** 按条目 Id 批量取（逗号分隔传给后端 Ids） */
  ids?: string[]
  searchTerm?: string
  genres?: string[]
  years?: number[]
  officialRatings?: string[]
  tags?: string[]
  /** 观看状态：IsFavorite / IsPlayed / IsUnplayed / IsResumable */
  filters?: string[]
  sortBy?: string
  sortOrder?: 'Ascending' | 'Descending'
  startIndex?: number
  limit?: number
  recursive?: boolean
  /**
   * 无限滚动的海报墙不需要总数：false 时后端跳过 COUNT(*)，用 HasMore 告诉
   * 前端有没有下一页（TotalRecordCount 此时为本页数量，Emby 官方从不返回 -1）。
   * 默认 true（第三方客户端行为不变）。
   */
  enableTotalRecordCount?: boolean
  /** 请求取消信号（筛选/排序快速切换时丢弃过期响应，P1 #8） */
  signal?: AbortSignal
}

/** 筛选菜单的可选值（后端 /Items/Filters 给出，取值来自全库） */
export interface EmbyFilters {
  Genres: string[]
  Tags: string[]
  OfficialRatings: string[]
  Years: number[]
}

// 1 tick = 100ns；1000 万 tick = 1 秒
export const TICKS_PER_SECOND = 10_000_000

export function ticksToSeconds(ticks?: number | null): number {
  return Math.round((ticks || 0) / TICKS_PER_SECOND)
}

export function secondsToTicks(seconds: number): number {
  return Math.round(seconds * TICKS_PER_SECOND)
}

export function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return '--'
  const h = Math.floor(seconds / 3600)
  const m = Math.round((seconds % 3600) / 60)
  return h > 0 ? `${h}小时${m}分钟` : `${m}分钟`
}

// ==================== 端点封装 ====================

const USER_ITEMS = '/emby/Users/me/Items'

/** Views 5 分钟内存缓存：媒体库首页和列表页各拉一次纯属浪费 */
let viewsCache: { items: EmbyItem[]; ts: number } | null = null
const VIEWS_TTL_MS = 5 * 60 * 1000

/** 条目详情共享内存缓存：详情页 / 播放页不再重复拉同一条 */
const itemCache = new Map<string, { item: EmbyItem; ts: number }>()
const ITEM_TTL_MS = 2 * 60 * 1000
const ITEM_CACHE_MAX = 200

function cacheItem(item: EmbyItem): void {
  itemCache.set(item.Id, { item, ts: Date.now() })
  if (itemCache.size > ITEM_CACHE_MAX) {
    const oldest = itemCache.keys().next().value
    if (oldest) itemCache.delete(oldest)
  }
}

export const embyApi = {
  /** 媒体库列表（Views） */
  async getViews(): Promise<EmbyItem[]> {
    if (viewsCache && Date.now() - viewsCache.ts < VIEWS_TTL_MS) return viewsCache.items
    const res = await embyGet<{ Items: EmbyItem[] }>('/emby/Users/me/Views')
    const items = res?.Items || []
    viewsCache = { items, ts: Date.now() }
    return items
  },

  /** 通用条目查询（浏览/搜索/筛选/分页） */
  async getItems(q: EmbyQuery = {}): Promise<{ Items: EmbyItem[]; TotalRecordCount: number; HasMore?: boolean }> {
    const params: Record<string, string | number> = {
      StartIndex: q.startIndex ?? 0,
      Limit: q.limit ?? 30,
      SortBy: q.sortBy || 'SortName',
      SortOrder: q.sortOrder || 'Ascending',
    }
    if (q.enableTotalRecordCount === false) params.EnableTotalRecordCount = 'false'
    if (q.parentId) params.ParentId = q.parentId
    if (q.recursive !== false) params.Recursive = 'true'
    if (q.includeTypes?.length) params.IncludeItemTypes = q.includeTypes.join(',')
    if (q.ids?.length) params.Ids = q.ids.join(',')
    if (q.searchTerm) params.SearchTerm = q.searchTerm
    if (q.genres?.length) params.Genres = q.genres.join('|')
    if (q.years?.length) params.Years = q.years.join(',')
    if (q.officialRatings?.length) params.OfficialRatings = q.officialRatings.join(',')
    if (q.tags?.length) params.Tags = q.tags.join('|')
    if (q.filters?.length) params.Filters = q.filters.join(',')
    return embyGet<{ Items: EmbyItem[]; TotalRecordCount: number; HasMore?: boolean }>(
      USER_ITEMS,
      q.signal ? { params, signal: q.signal } : { params },
    )
  },

  /** 筛选菜单的可选值（分类 / 标签 / 分级 / 年份） */
  async getFilters(): Promise<EmbyFilters> {
    const res = await embyGet<Partial<EmbyFilters>>('/emby/Items/Filters')
    return {
      Genres: res?.Genres || [],
      Tags: res?.Tags || [],
      OfficialRatings: res?.OfficialRatings || [],
      Years: res?.Years || [],
    }
  },

  /** 续看列表（有播放进度未看完） */
  async getResume(limit = 12): Promise<EmbyItem[]> {
    const res = await embyGet<{ Items: EmbyItem[] }>(`${USER_ITEMS}/Resume`, { params: { Limit: limit } })
    return res?.Items || []
  },

  /** 最新添加 */
  async getLatest(limit = 16): Promise<EmbyItem[]> {
    return embyGet<EmbyItem[]>(`${USER_ITEMS}/Latest`, { params: { Limit: limit } })
  },

  /** 剧集 NextUp */
  async getNextUp(limit = 12): Promise<EmbyItem[]> {
    const res = await embyGet<{ Items: EmbyItem[] }>('/emby/Shows/NextUp', { params: { Limit: limit } })
    return res?.Items || []
  },

  /** 收藏列表 */
  async getFavorites(): Promise<EmbyItem[]> {
    const res = await embyGet<{ Items: EmbyItem[] }>('/emby/Users/me/FavoriteItems')
    return res?.Items || []
  },

  /** 条目详情（full，含 MediaSources）：2 分钟共享缓存，详情页/播放页共用 */
  async getItem(itemId: string): Promise<EmbyItem> {
    const hit = itemCache.get(itemId)
    if (hit && Date.now() - hit.ts < ITEM_TTL_MS) return hit.item
    const item = await embyGet<EmbyItem>(`/emby/Items/${itemId}`)
    cacheItem(item)
    return item
  },

  /** 剧集的季列表 */
  async getSeasons(seriesId: string): Promise<EmbyItem[]> {
    const res = await embyGet<{ Items: EmbyItem[] }>(`/emby/Shows/${seriesId}/Seasons`)
    return res?.Items || []
  },

  /** 剧集/季的集列表 */
  async getEpisodes(seriesId: string, seasonId?: string): Promise<EmbyItem[]> {
    const res = await embyGet<{ Items: EmbyItem[] }>(`/emby/Shows/${seriesId}/Episodes`, {
      params: seasonId ? { SeasonId: seasonId } : {},
    })
    return res?.Items || []
  },

  /** 切换收藏 */
  async setFavorite(itemId: string, isFavorite: boolean): Promise<EmbyUserData> {
    const ud = await embyPost<EmbyUserData>(`/emby/Users/me/Items/${itemId}/Rating`, { IsFavorite: isFavorite })
    const hit = itemCache.get(itemId)
    if (hit) hit.item.UserData = { ...hit.item.UserData, ...ud }
    return ud
  },

  /** 标记已看 / 未看 */
  async setPlayed(itemId: string, played: boolean): Promise<EmbyUserData> {
    const ud = played
      ? await embyPost<EmbyUserData>(`/emby/Users/me/PlayedItems/${itemId}`)
      : await embyDelete<EmbyUserData>(`/emby/Users/me/PlayedItems/${itemId}`)
    const hit = itemCache.get(itemId)
    if (hit) hit.item.UserData = { ...hit.item.UserData, ...ud }
    return ud
  },

  // 2026-10-13 旧页面清理：getPlaybackInfo / reportPlaying / reportProgress / reportStopped
  // 是 WatchView 专用 wrapper（全仓唯一调用者），随页面删除一并移除。
  // 保留 setPlayed / getItem 等（仍有其他调用者）。
}

function positionTicks() {
  return 0
}

// ==================== 图片与播放地址工具 ====================

/**
 * 海报地址：{EA}/emby/Items/{id}/Images/Primary（可直接给 <img> 使用）
 *
 * 图片端点本身不需要鉴权；**不要**再把门户 JWT 拼进 URL（安全修复 H2：URL 会进
 * 反代 / CDN 日志、浏览器历史与 Referer）。
 *
 * 必须是 EA 绝对/前缀地址。以前这里返回同源 /emby/...，在分离部署下打到 EM，
 * 图片 404 —— 就是首页那些只剩编号、没有封面的卡片的来源。
 */
export function posterUrl(item: EmbyItem, maxWidth = 320): string {
  if (!item.ImageTags?.Primary) return ''
  return imageUrl(item.Id, 'Primary', maxWidth)
}

/** 背景图地址 */
export function backdropUrl(item: EmbyItem, maxWidth = 1280): string {
  if (!item.BackdropImageTags?.length) return ''
  return imageUrl(item.Id, 'Backdrop', maxWidth)
}

/**
 * 条目图片地址。maxWidth <= 0 表示要原图（不带 maxWidth）。
 *
 * 缩略图串图（服务端缩略图缓存曾按「原图文件名」命名，外挂 poster.jpg / fanart.jpg 全库同名）
 * 已在服务端修复（缩略图按「绝对路径 + mtime + 大小」的 sha1 命名，见 image_store），
 * 所以列表 / 横滑行可以放心带 maxWidth，不必为了避开串图去拉几 MB 的原图。
 *
 * Thumb：服务端按横版背景图（Backdrop 链）出图，用作海报缺失时的兜底。
 */
export function imageUrl(itemId: string, kind: 'Primary' | 'Backdrop' | 'Thumb', maxWidth = 0): string {
  // H2 安全：URL 里不带任何凭据（JWT 会进反代 / CDN 日志、浏览器历史与 Referer）。
  // 条目图片端点本身是公开的（不校验 token / Cookie），<img> 跨域直接加载即可。
  const q = maxWidth > 0 ? `?maxWidth=${maxWidth}` : ''
  return `${embyBaseRef.value}/emby/Items/${encodeURIComponent(itemId)}/Images/${kind}${q}`
}

/** 进度条百分比 */
export function progressPercent(item: EmbyItem): number {
  const pos = item.UserData?.PlaybackPositionTicks || 0
  if (!pos || !item.RunTimeTicks) return 0
  return Math.min(100, Math.round((pos / item.RunTimeTicks) * 100))
}
