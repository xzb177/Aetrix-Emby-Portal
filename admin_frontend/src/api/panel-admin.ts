/**
 * 面板 v4 · 成就定义与馆灵阶段管理（r48 遗留 ⑨）
 *
 * 后端：`/api/admin/panel/achievements*`（成就定义表）、`/api/admin/panel/pet-stages`
 * （system_configs 键 panel_pet_stages）。管理 axios 实例 baseURL 是 `/api/admin`。
 */
import { get, put } from '@/utils/request'

export interface AchievementDef {
  key: string
  name: string
  desc: string
  icon: string
  sort: number
  enabled: boolean
}

export interface PetStage {
  name: string
  icon: string
  hours: number
}

export const fetchAchievements = async (): Promise<AchievementDef[]> =>
  (await get<{ achievements: AchievementDef[] }>('/panel/achievements')).achievements

export const updateAchievement = async (
  key: string,
  body: Omit<AchievementDef, 'key'>,
): Promise<AchievementDef> =>
  (await put<{ achievement: AchievementDef }>(`/panel/achievements/${key}`, body)).achievement

export const fetchPetStages = async (): Promise<PetStage[]> =>
  (await get<{ stages: PetStage[] }>('/panel/pet-stages')).stages

export const savePetStages = async (stages: PetStage[]): Promise<PetStage[]> =>
  (await put<{ stages: PetStage[] }>('/panel/pet-stages', { stages })).stages
