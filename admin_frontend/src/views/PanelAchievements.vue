<script setup lang="ts">
/**
 * 成就与馆灵：面板 v4 玩法配置（r48 遗留 ⑨ 补的管理入口）
 *
 * - 成就定义：只改已有 key 的名称 / 描述 / 图标 / 排序 / 开关。
 *   判定逻辑在后端 evaluate() 按 key 硬编码，不许新增/删除 key（新增的拿不到、删了的悬空）。
 * - 馆灵阶段：第一阶段 hours 必须为 0，且阈值不能倒挂（后端同口径校验）。
 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import type { FormInstance, FormRules } from 'element-plus'
import { Medal, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import {
  fetchAchievements, updateAchievement, fetchPetStages, savePetStages,
  type AchievementDef, type PetStage,
} from '@/api/panel-admin'

const loading = ref(false)
const loadError = ref('')
const achievements = ref<AchievementDef[]>([])
const activeTab = ref('achievements')

const achColumns: DataColumn[] = [
  { key: 'icon', label: '图标', width: 70 },
  { key: 'name', label: '名称', width: 150, mobile: 'title' },
  { key: 'desc', label: '描述', minWidth: 220, mobile: 'hide' },
  { key: 'key', label: 'key', width: 170, mobile: 'hide' },
  { key: 'sort', label: '排序', width: 80 },
  { key: 'enabled', label: '开关', width: 90 },
  { key: 'actions', label: '操作', width: 110, fixed: 'right', align: 'right' },
]

// ===== 成就编辑表单 =====
const achVisible = ref(false)
const achEditing = ref<AchievementDef | null>(null)
const achFormRef = ref<FormInstance>()
const achForm = ref({ name: '', desc: '', icon: '🏅', sort: 0, enabled: true })
const achRules: FormRules = {
  name: [
    { required: true, message: '请输入成就名称', trigger: 'blur' },
    { max: 50, message: '名称最多 50 个字', trigger: 'blur' },
  ],
  desc: [
    { required: true, message: '请输入成就描述', trigger: 'blur' },
    { max: 200, message: '描述最多 200 个字', trigger: 'blur' },
  ],
  icon: [{ max: 20, message: '图标最多 20 个字符', trigger: 'blur' }],
}

function openAchEdit(row: AchievementDef) {
  achEditing.value = row
  achForm.value = { name: row.name, desc: row.desc, icon: row.icon, sort: row.sort, enabled: row.enabled }
  achVisible.value = true
}

async function saveAch() {
  const valid = await achFormRef.value?.validate().catch(() => false)
  if (!valid || !achEditing.value) return
  try {
    const updated = await updateAchievement(achEditing.value.key, { ...achForm.value })
    const i = achievements.value.findIndex((a) => a.key === updated.key)
    if (i >= 0) achievements.value[i] = updated
    achievements.value.sort((a, b) => a.sort - b.sort)
    achVisible.value = false
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(toAdminError(e, '保存失败'))
  }
}

// ===== 馆灵阶段 =====
const stages = ref<PetStage[]>([])
const stagesLoaded = ref(false)
const savingStages = ref(false)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [achs, sts] = await Promise.all([fetchAchievements(), fetchPetStages()])
    achievements.value = [...achs].sort((a, b) => a.sort - b.sort)
    stages.value = sts.map((s) => ({ ...s }))
    stagesLoaded.value = true
  } catch (e) {
    loadError.value = toAdminError(e, '加载失败')
  } finally {
    loading.value = false
  }
}

function addStage() {
  const last = stages.value[stages.value.length - 1]
  stages.value.push({ name: '', icon: '', hours: last ? last.hours + 100 : 0 })
}

function removeStage(i: number) {
  stages.value.splice(i, 1)
}

async function saveStages() {
  if (!stagesLoaded.value) return
  // 前端先拦一遍 obvious 的错：后端同口径再拦一次
  if (!stages.value.length) {
    ElMessage.error('至少保留一个阶段')
    return
  }
  for (let i = 0; i < stages.value.length; i++) {
    const s = stages.value[i]
    if (!s.name.trim()) {
      ElMessage.error(`第 ${i + 1} 个阶段名称不能为空`)
      return
    }
    if (i === 0 && s.hours !== 0) {
      ElMessage.error('第一阶段 hours 必须为 0（从 0 小时开始）')
      return
    }
    if (i > 0 && s.hours < stages.value[i - 1].hours) {
      ElMessage.error(`第 ${i + 1} 个阶段 hours 不能小于前一阶段`)
      return
    }
  }
  try {
    await ElMessageBox.confirm('保存后立即对全站生效，确认吗？', '保存馆灵阶段', { type: 'warning' })
  } catch {
    return
  }
  savingStages.value = true
  try {
    stages.value = await savePetStages(stages.value.map((s) => ({ ...s, name: s.name.trim() })))
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(toAdminError(e, '保存失败'))
  } finally {
    savingStages.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader
      title="成就与馆灵"
      description="面板 v4 玩法配置：成就只改文案与开关（判定逻辑不动），馆灵阶段改累计观影小时阈值。"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="成就定义" name="achievements">
        <SectionCard title="成就定义" :icon="Medal" :meta="loading ? '' : `${achievements.length} 个`" flush>
          <DataTable
            :rows="achievements"
            :columns="achColumns"
            :loading="loading"
            :error="loadError"
          >
            <template #cell-icon="{ row }"><span class="text-xl">{{ row.icon }}</span></template>
            <template #cell-sort="{ row }"><span class="mono">{{ row.sort }}</span></template>
            <template #cell-enabled="{ row }">
              <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
                {{ row.enabled ? '启用' : '停用' }}
              </el-tag>
            </template>
            <template #cell-actions="{ row }">
              <el-button size="small" @click="openAchEdit(row)">编辑</el-button>
            </template>
          </DataTable>
          <p class="field-help">
            key 是判定逻辑的锚点，不可改；新增/删除成就需要改后端判定逻辑，请走需求流程。
          </p>
        </SectionCard>
      </el-tab-pane>

      <el-tab-pane label="馆灵阶段" name="stages">
        <SectionCard title="馆灵阶段" :icon="Medal" flush>
          <template #actions>
            <el-button :icon="Plus" @click="addStage">新增阶段</el-button>
            <el-button type="primary" :loading="savingStages" :disabled="!stagesLoaded" @click="saveStages">
              保存
            </el-button>
          </template>
          <div v-for="(s, i) in stages" :key="i" class="stage-row">
            <span class="mono stage-idx">{{ i + 1 }}</span>
            <el-input v-model="s.icon" placeholder="图标" class="stage-icon" maxlength="20" />
            <el-input v-model="s.name" placeholder="阶段名称" class="stage-name" maxlength="30" />
            <el-input-number
              v-model="s.hours"
              :min="0"
              :disabled="i === 0"
              placeholder="累计小时"
              class="stage-hours"
            />
            <span class="field-help">小时</span>
            <el-button
              size="small" type="danger" :icon="Trash2" circle
              :disabled="stages.length <= 1 || i === 0"
              @click="removeStage(i)"
              aria-label="删除该阶段"
            />
          </div>
          <p class="field-help">
            第一阶段 hours 固定为 0（从 0 小时开始，不可改）；后续阶段阈值不能倒挂。保存后立即对全站生效。
          </p>
        </SectionCard>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="achVisible" title="编辑成就" width="min(520px, 92vw)">
      <el-form ref="achFormRef" :model="achForm" :rules="achRules" label-position="top">
        <el-form-item label="key（判定锚点，不可改）">
          <el-input :value="achEditing?.key" disabled />
        </el-form-item>
        <el-form-item label="名称" prop="name"><el-input v-model="achForm.name" maxlength="50" /></el-form-item>
        <el-form-item label="描述" prop="desc">
          <el-input v-model="achForm.desc" type="textarea" :rows="2" maxlength="200" show-word-limit />
        </el-form-item>
        <div class="form-grid">
          <el-form-item label="图标" prop="icon"><el-input v-model="achForm.icon" maxlength="20" /></el-form-item>
          <el-form-item label="排序" prop="sort"><el-input-number v-model="achForm.sort" :min="0" class="w-full" /></el-form-item>
        </div>
        <el-form-item label="开关">
          <el-checkbox v-model="achForm.enabled">启用（停用后不再向新用户发放，已解锁的不受影响）</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="achVisible = false">取消</el-button>
        <el-button type="primary" @click="saveAch">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.stage-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.stage-idx { width: 24px; color: var(--el-text-color-secondary); }
.stage-icon { width: 90px; }
.stage-name { flex: 1; min-width: 120px; }
.stage-hours { width: 150px; }
</style>
