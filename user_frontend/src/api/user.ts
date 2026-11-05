/**
 * 用户端个人设置 API（门户后端 /api/user/*）。
 *
 * 2026-10 简化：播放只有中转一条路径，以下接口保留仅为兼容，
 * 新代码不要再调用。
 */
import api from '@/api'

/** 播放路径（已废弃）：只有 'relay' */
export type PlayLine = 'relay'

/** 设置播放路径（已废弃）：空操作，永远返回 relay */
export async function setPlayLine(_line: PlayLine): Promise<PlayLine> {
  return 'relay'
}

/** 我的专属线路（GET /api/user/exclusive-lines）：只有名称与入口
 * 地址，取流基址与入口密钥是内部配置、永不下发用户端。无资格时
 * 后端回空列表，页面据此不渲染该分组；请求失败同样按空处理。 */
export interface MyExclusiveLine {
  name: string
  entry_url: string
}

export async function getMyExclusiveLines(): Promise<MyExclusiveLine[]> {
  try {
    const res = await api.get<never, { lines: MyExclusiveLine[] }>('/api/user/exclusive-lines')
    return Array.isArray(res?.lines) ? res.lines : []
  } catch {
    return []
  }
}

/** 注册页开关状态（公开接口，未登录可调） */
export interface RegisterConfig {
  registration_mode: 'open' | 'closed'
  invitation_enabled: boolean
}

/** 获取注册页开关：注册模式 + 邀请码开关 */
export async function getRegisterConfig(): Promise<RegisterConfig> {
  try {
    const res = await api.get<never, { registration_mode: string; invitation_enabled: boolean }>('/api/user/auth/register-config')
    // code 模式已下线：历史残留值归一为 open
    const mode = res.registration_mode === 'closed' ? 'closed' : 'open'
    return {
      registration_mode: mode,
      invitation_enabled: res.invitation_enabled !== false,
    }
  } catch {
    // 拿不到时按最宽松处理：开放注册 + 邀请码显示（不把用户锁在门外）
    return { registration_mode: 'open', invitation_enabled: true }
  }
}
