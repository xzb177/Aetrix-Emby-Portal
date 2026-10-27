/**
 * 合集管理端 API（全部走 /api/admin/*，见 backend/api/media_sets_api.py）。
 *
 * 只做机制位，不做内容：合集与模板全由服主在管理页里配；不预置片单、
 * 不做豆列导入闭环（模板规则 JSON 由服主自己填）、不做拼贴封面引擎
 * （展示封面只认 cover_url；artwork_mode 只透传后端策略，前端不拼 URL）。
 */
import { del, get, patch, post } from '@/utils/request'

/** 合集行（GET /api/admin/media-sets 的 set_to_dict 项） */
export interface MediaSet {
  id: number
  name: string
  source_type: string
  visibility_status: string
  sort_order: number
  min_effective_members: number
  artwork_mode: string
  /** 已解析的封面地址；空则占位，前端不补 URL */
  cover_url?: string | null
  path_prefix: string | null
  bound_person_id: number | null
  requested_revision: number | null
  completed_revision: number | null
  refresh_status: string | null
  member_count: number
  effective_count: number
  shelf_eligible: boolean
}

/** 合集成员行（member_to_dict：只有 item_id，没有条目名） */
export interface MediaSetMember {
  id: number
  set_id: number
  item_id: number
  order_index: number
  member_source: string
}

/** 合集规则行（只读展示用；本页不做规则编辑） */
export interface MediaSetRule {
  id: number
  set_id: number
  rule_kind: string
  is_exclusion: boolean
  value_text: string | null
  value_int: number | null
  value_float: number | null
}

/** 合集详情（set_to_dict + rules + members） */
export interface MediaSetDetail extends MediaSet {
  rules: MediaSetRule[]
  members: MediaSetMember[]
}

/** 合集模板（template_to_dict） */
export interface MediaSetTemplate {
  id: number
  name: string
  rules: Record<string, unknown>[]
}

export interface MediaSetCreateBody {
  name: string
  source_type?: string
  visibility_status?: string
  sort_order?: number
  min_effective_members?: number
  artwork_mode?: string
  path_prefix?: string | null
  bound_person_id?: number | null
}

export type MediaSetPatchBody = Partial<MediaSetCreateBody>

export const SOURCE_TYPES = ['manual', 'rule', 'path', 'preset', 'douban_doulist'] as const
export const VISIBILITY_STATUSES = ['visible', 'hidden'] as const
export const ARTWORK_MODES = ['auto_collage', 'none'] as const

export const collectionsAdminApi = {
  // 合集列表（后端已按 sort_order、id 出架）
  list: () => get<MediaSet[]>('/media-sets'),

  // 合集详情（含规则与成员）
  get: (id: number) => get<MediaSetDetail>(`/media-sets/${id}`),

  // 新建合集
  create: (body: MediaSetCreateBody) => post<MediaSet>('/media-sets', body),

  // 编辑合集（只传改动的字段）
  patch: (id: number, body: MediaSetPatchBody) => patch<MediaSet>(`/media-sets/${id}`, body),

  // 删除合集
  remove: (id: number) => del(`/media-sets/${id}`),

  // 批量设序（架序）：整表 sort_order 一次提交
  order: (orders: Array<{ id: number; sort_order: number }>) =>
    post('/media-sets/order', { orders }),

  // 手工加成员（已存在则幂等更新 order_index，不重复建行）
  addMember: (setId: number, body: { item_id: number; order_index?: number }) =>
    post(`/media-sets/${setId}/members`, body),

  // 删成员
  removeMember: (setId: number, itemId: number) =>
    del(`/media-sets/${setId}/members/${itemId}`),

  // 成员设序（观影轨排序键）：整组成员 order_index 一次提交
  orderMembers: (setId: number, orders: Array<{ item_id: number; order_index: number }>) =>
    post(`/media-sets/${setId}/members/order`, { orders }),

  // 刷新合集（规则驱动合集重算成员；手工合集刷新为空操作）
  refresh: (setId: number) => post(`/media-sets/${setId}/refresh`, {}),

  // 模板：列表 / 新建（规则 JSON 由服主填）/ 用模板建合集
  listTemplates: () => get<MediaSetTemplate[]>('/media-set-templates'),
  createTemplate: (body: { name: string; rules?: Record<string, unknown>[] }) =>
    post<MediaSetTemplate>('/media-set-templates', body),
  instantiateTemplate: (templateId: number, body: { name?: string | null; min_effective_members?: number | null }) =>
    post(`/media-set-templates/${templateId}/instantiate`, body),
}
