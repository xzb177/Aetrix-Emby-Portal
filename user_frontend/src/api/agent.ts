/**
 * 代理中心 API（门户后端 /api/user/agent/*，2026-11-06 代理分销）。
 *
 * 只有被管理员指定为代理的用户才有数据：getAgentMe 对普通用户回
 * is_agent=false（入口据此不渲染）；其余接口对非代理回 403。
 */
import api from '@/api'

export interface AgentProfile {
  id: number
  level_id: number
  level_name: string
  discount_rate: number
  balance: number
  is_active: boolean
}

export interface AgentProduct {
  plan_id: number
  name: string
  description: string
  duration_days: number
  realm_id: number | null
  face_value: number
  unit_cost: number
  discount_rate: number
}

export interface AgentCodeRow {
  id: number
  code: string
  code_type: number
  code_type_name: string
  days: number
  days_text: string
  state: string
  use_count: number
  max_uses: number
  note: string | null
  created_at: string | null
  expires_at: string | null
}

export interface AgentLogRow {
  id: number
  kind: string
  amount: number
  balance_after: number
  note: string
  created_at: string | null
}

export interface AgentSaleRow {
  id: number
  code: string
  buyer_username: string
  face_value: number
  unit_cost: number
  created_at: string | null
}

export interface AgentStats {
  summary: {
    purchase_quantity: number
    purchase_cost_total: number
    sales_count: number
    sales_face_total: number
    sales_cost_total: number
  }
  daily: { period: string; count: number; face_total: number }[]
  monthly: { period: string; count: number; face_total: number }[]
}

export async function getAgentMe(): Promise<{ is_agent: boolean; agent: AgentProfile | null }> {
  try {
    return await api.get<never, { is_agent: boolean; agent: AgentProfile | null }>(
      '/api/user/agent/me')
  } catch {
    return { is_agent: false, agent: null }
  }
}

export async function getAgentProducts(): Promise<AgentProduct[]> {
  const res = await api.get<never, { products: AgentProduct[] }>('/api/user/agent/products')
  return res?.products ?? []
}

export async function purchaseAgentCodes(data: {
  plan_id: number
  code_type: number
  quantity: number
}): Promise<{ purchase: { total_cost: number }; codes: { id: number; code: string; days_text: string }[] }> {
  return api.post('/api/user/agent/purchase', data)
}

export async function getAgentCodes(limit = 50, offset = 0): Promise<{ total: number; codes: AgentCodeRow[] }> {
  return api.get<never, { total: number; codes: AgentCodeRow[] }>('/api/user/agent/codes', {
    params: { limit, offset },
  })
}

export async function getAgentLedger(limit = 50, offset = 0): Promise<{ total: number; logs: AgentLogRow[] }> {
  return api.get<never, { total: number; logs: AgentLogRow[] }>('/api/user/agent/ledger', {
    params: { limit, offset },
  })
}

export async function getAgentSales(limit = 50, offset = 0): Promise<{ total: number; sales: AgentSaleRow[] }> {
  return api.get<never, { total: number; sales: AgentSaleRow[] }>('/api/user/agent/sales', {
    params: { limit, offset },
  })
}

export async function getAgentStats(): Promise<AgentStats> {
  return api.get<never, AgentStats>('/api/user/agent/stats')
}
