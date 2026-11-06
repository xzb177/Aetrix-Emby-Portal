/** 代理分销 API（/api/admin/agents*，后端见 backend/api/agents_admin.py） */
import { get, post, patch, del } from '@/utils/request'

export interface AgentLevelRow {
  id: number
  name: string
  discount_rate: number
  sort_order: number
  is_active: boolean
  agent_count: number
  created_at: string | null
}

export interface AgentRow {
  id: number
  user_id: number
  username: string
  level_id: number
  level_name: string
  discount_rate: number
  balance: number
  is_active: boolean
  note: string
  purchase_quantity: number
  purchase_cost_total: number
  sales_count: number
  sales_face_total: number
  sales_cost_total: number
  created_at: string | null
}

export interface AgentLogRow {
  id: number
  agent_id: number
  kind: string
  amount: number
  balance_after: number
  note: string
  ref_id: number | null
  created_at: string | null
}

export interface AgentPurchaseRow {
  id: number
  agent_id: number
  plan_id: number
  plan_name: string
  realm_id: number | null
  code_type: number
  quantity: number
  face_value: number
  discount_rate: number
  unit_cost: number
  total_cost: number
  created_at: string | null
}

export interface AgentSaleRow {
  id: number
  agent_id: number
  code_id: number
  code: string
  purchase_id: number | null
  buyer_user_id: number
  buyer_username: string
  face_value: number
  unit_cost: number
  created_at: string | null
}

export interface AgentBucket {
  period: string
  count: number
  face_total: number
}

export interface AgentOverview {
  agent_count: number
  purchase_quantity: number
  purchase_cost_total: number
  sales_count: number
  sales_face_total: number
  sales_cost_total: number
  topped_up_total: number
  balance_pool_total: number
  daily: AgentBucket[]
  monthly: AgentBucket[]
}

// ===== 等级 =====
export const fetchAgentLevels = () =>
  get<{ levels: AgentLevelRow[] }>('/agents/levels')

export const createAgentLevel = (data: { name: string; discount_rate: number; sort_order: number }) =>
  post<{ success: boolean; level: AgentLevelRow }>('/agents/levels', data)

export const updateAgentLevel = (
  id: number,
  data: Partial<{ name: string; discount_rate: number; sort_order: number; is_active: boolean }>,
) => patch<{ success: boolean; level: AgentLevelRow }>(`/agents/levels/${id}`, data)

export const deleteAgentLevel = (id: number) =>
  del<{ success: boolean }>(`/agents/levels/${id}`)

// ===== 代理 =====
export const fetchAgents = (params: { limit?: number; offset?: number } = {}) =>
  get<{ total: number; agents: AgentRow[] }>('/agents', params)

export const assignAgent = (data: { user_id?: number; username?: string; level_id: number }) =>
  post<{ success: boolean; agent: AgentRow }>('/agents', data)

export const updateAgent = (
  id: number,
  data: Partial<{ level_id: number; is_active: boolean; note: string }>,
) => patch<{ success: boolean; agent: AgentRow }>(`/agents/${id}`, data)

export const adjustAgentBalance = (id: number, data: { amount: string; note: string }) =>
  post<{ success: boolean; agent: AgentRow }>(`/agents/${id}/balance`, data)

export const fetchAgentLedger = (id: number, params: { limit?: number; offset?: number } = {}) =>
  get<{ total: number; logs: AgentLogRow[] }>(`/agents/${id}/ledger`, params)

export const fetchAgentPurchases = (id: number, params: { limit?: number; offset?: number } = {}) =>
  get<{ total: number; purchases: AgentPurchaseRow[] }>(`/agents/${id}/purchases`, params)

export const fetchAgentSales = (id: number, params: { limit?: number; offset?: number } = {}) =>
  get<{ total: number; sales: AgentSaleRow[] }>(`/agents/${id}/sales`, params)

// ===== 全局统计 =====
export const fetchAgentOverview = () => get<AgentOverview>('/agents/overview/stats')

export const fetchAllAgentSales = (params: { limit?: number; offset?: number } = {}) =>
  get<{ total: number; sales: AgentSaleRow[] }>('/agents/sales/all', params)
