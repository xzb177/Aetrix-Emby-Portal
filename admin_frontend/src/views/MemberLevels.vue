<script setup lang="ts">
/**
 * 会员等级管理（P1 统一货币体系）
 *
 * 暗房影院统一风格：PageHeader + SectionCard + DataTable。
 * 等级折扣：订阅购买时按等级 discount_pct 打折，仅付费服生效，公益服不打折。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Crown, RefreshCw } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import { useBreakpoint } from '@/composables/useBreakpoint'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import {
  fetchMemberLevels,
  updateMemberLevel,
  toggleMemberLevel,
  deleteMemberLevel,
  type MemberLevelRow,
} from '@/api/economy'

const { isPhone } = useBreakpoint()
const levels = ref<MemberLevelRow[]>([])
const loading = ref(false)
const loadError = ref('')

const columns = computed<DataColumn[]>(() => [
  { key: 'level', label: '等级', width: 80 },
  { key: 'badge', label: '徽章', width: 150 },
  { key: 'name', label: '名称', width: 140, mobile: 'title' },
  { key: 'xp_threshold', label: '经验阈值', width: 110 },
  { key: 'discount_pct', label: '订阅折扣%', width: 110 },
  { key: 'benefits', label: '权益' },
  { key: 'is_active', label: '启用', width: 90 },
  { key: 'actions', label: '操作', width: 150, fixed: 'right', align: 'right' },
])

async function loadLevels() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetchMemberLevels()
    levels.value = res.levels ?? []
  } catch (e) {
    // GET 走 silent：拦截器不提示，错误态交给表格（带重试）
    loadError.value = e instanceof Error ? e.message : '加载等级失败'
  } finally {
    loading.value = false
  }
}

// ===== 编辑弹窗 =====
const dlg = ref({
  visible: false,
  editing: null as MemberLevelRow | null,
  form: {
    name: '',
    xp_threshold: 0,
    discount_pct: 0,
    badge_icon: '',
    badge_color: '',
    benefitsText: '',
  },
})

function openEdit(row: MemberLevelRow) {
  dlg.value = {
    visible: true,
    editing: { ...row },
    form: {
      name: row.name,
      xp_threshold: row.xp_threshold,
      discount_pct: row.discount_pct || 0,
      badge_icon: row.badge_icon,
      badge_color: row.badge_color,
      benefitsText: row.benefits.join('\n'),
    },
  }
}

const saving = ref(false)
async function doSave() {
  if (!dlg.value.editing || saving.value) return
  if (!dlg.value.form.name.trim()) {
    ElMessage.warning('请填写等级名称')
    return
  }
  const benefits = dlg.value.form.benefitsText
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean)
  saving.value = true
  try {
    await updateMemberLevel(dlg.value.editing.id, {
      name: dlg.value.form.name.trim(),
      xp_threshold: dlg.value.form.xp_threshold,
      discount_pct: dlg.value.form.discount_pct,
      badge_icon: dlg.value.form.badge_icon,
      badge_color: dlg.value.form.badge_color,
      benefits,
    })
    ElMessage.success('已保存')
    dlg.value.visible = false
    loadLevels()
  } catch {
    /* 写操作失败由请求拦截器统一弹错 */
  } finally {
    saving.value = false
  }
}

async function doToggle(row: MemberLevelRow) {
  try {
    await toggleMemberLevel(row.id)
  } catch {
    /* 拦截器已弹出具体原因（如「至少保留 1 个启用等级」），这里只负责刷新回真实状态 */
  } finally {
    loadLevels()
  }
}

async function doDelete(row: MemberLevelRow) {
  try {
    await ElMessageBox.confirm(`确定删除「Lv.${row.level} ${row.name}」吗？至少保留 1 个启用等级。`, '删除等级', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 点了取消 / 关闭：不是错误
  }
  try {
    await deleteMemberLevel(row.id)
    ElMessage.success('已删除')
    loadLevels()
  } catch {
    /* 写操作失败由请求拦截器统一弹错 */
  }
}

onMounted(() => { loadLevels() })
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="运营中心"
      title="会员等级"
      description="管理 Lv1-Lv6 会员等级：经验阈值、徽章、公开权益、订阅折扣（仅付费服）"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="loadLevels">刷新</el-button>
      </template>
    </PageHeader>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="经验来源：真实充值 1 元 = 1 经验、有效邀请 +10；兑换码 / 红包 / 签到不加经验。订阅折扣仅付费服生效。"
    />

    <SectionCard title="等级列表" :icon="Crown" :meta="`${levels.length} 个等级`" flush>
      <DataTable
        :columns="columns"
        :rows="levels"
        :loading="loading"
        :error="loadError"
        empty="还没有会员等级"
        empty-description="等级由服务端初始化（Lv1–Lv6）；为空时请检查数据库迁移。"
        @retry="loadLevels"
      >
        <template #cell-level="{ row }">
          <span class="font-semibold">Lv.{{ row.level }}</span>
        </template>

        <template #cell-badge="{ row }">
          <div class="flex items-center gap-2">
            <span
              class="inline-flex items-center justify-center w-8 h-8 rounded-full text-sm font-semibold text-white shrink-0"
              :style="{ background: row.badge_color }"
            >
              {{ row.level }}
            </span>
            <span class="text-xs opacity-60">{{ row.badge_icon }}</span>
          </div>
        </template>

        <template #cell-benefits="{ row }">
          <div class="flex flex-wrap gap-1">
            <el-tag v-for="(b, i) in row.benefits" :key="i" size="small">{{ b }}</el-tag>
          </div>
        </template>

        <template #cell-is_active="{ row }">
          <el-switch :model-value="row.is_active" @change="doToggle(row)" />
        </template>

        <template #cell-actions="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="doDelete(row)">删除</el-button>
        </template>
      </DataTable>
    </SectionCard>

    <el-dialog v-model="dlg.visible" title="编辑等级" width="min(480px, 92vw)">
      <el-form :label-position="isPhone ? 'top' : 'right'" label-width="90px">
        <el-form-item label="名称">
          <el-input v-model="dlg.form.name" />
        </el-form-item>
        <el-form-item label="经验阈值">
          <el-input-number v-model="dlg.form.xp_threshold" :min="0" />
        </el-form-item>
        <el-form-item label="订阅折扣%">
          <el-input-number v-model="dlg.form.discount_pct" :min="0" :max="100" />
          <span class="form-hint">填 90 就是九折（100=不打折）；仅付费服生效，公益服不打折</span>
        </el-form-item>
        <el-form-item label="徽章图标">
          <el-input v-model="dlg.form.badge_icon" placeholder="lucide 图标名，如 Crown" />
        </el-form-item>
        <el-form-item label="徽章颜色">
          <el-color-picker v-model="dlg.form.badge_color" />
        </el-form-item>
        <el-form-item label="权益">
          <el-input
            v-model="dlg.form.benefitsText"
            type="textarea"
            :rows="4"
            placeholder="每行一个权益"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="doSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
:deep(.el-form-item__content) { flex-wrap: wrap; row-gap: 4px; }
.form-hint {
  flex-basis: 100%;
  color: var(--au-text-3);
  font-size: 12px;
  line-height: 1.5;
}
</style>
