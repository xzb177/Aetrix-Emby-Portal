// 智能字幕用户端接口（后端 backend/api/smart_subtitles.py）。
// POST /api/user/smart-subtitles/{item_id}/match，入参 { candidates: string[] }，
// 返回 { item_id, matched: [{ name, language }] }；language 为 Emby 三字码。
// 开关关 → 403「智能字幕未开启」；不可见 → 403；不存在 → 404。
// 本模块只做调用，不碰 api/index.ts 与 router/index.ts。

import api from '@/api'

export interface MatchedSubtitle {
  name: string
  language: string
}

export interface SubtitleMatchResult {
  item_id: number
  matched: MatchedSubtitle[]
}

export const subtitleApi = {
  match(itemId: number, candidates: string[]): Promise<SubtitleMatchResult> {
    return api.post<never, SubtitleMatchResult>(
      `/api/user/smart-subtitles/${itemId}/match`,
      { candidates },
    )
  },
}
