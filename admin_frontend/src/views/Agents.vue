<script setup lang="ts">
/**
 * 代理分销管理（2026-11-06 完整版）：等级管理 / 代理指定与取消 /
 * 进货余额调整 / 全量销售统计。
 *
 * 口径：等级与折扣都是可配数据（后端 agent_levels）；代理余额是独立
 * 账本，管理员手工增减必须走流水；销售记录由卡码核销自动归属。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { HandCoins, RefreshCw } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import { useBreakpoint } from '@/composables/useBreakpoint'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import {
  adjustAgentBalance,
  assignAgent,
  createAgentLevel,
  deleteAgentLevel,
  fetchAgentLedger,
  fetchAgentLevels,
  fetchAgentOverview,
  fetchAgentPurchases,
  fetchAgentSales,
  fetchAgents,
  fetchAllAgentSales,
  updateAgent,
  updateAgentLevel,
  type AgentLevelRow,
  type AgentLogRow,
  type AgentOverview,
  type AgentPurchaseRow,
  type AgentRow,
  type AgentSaleRow,
} from '@/api/agents'

const { isPhone } = useBreakpoint()
const tab = ref<'agents' | 'levels' | 'stats'>('agents')

const money = (n: number) => `¥${(n ?? 0).toFixed(2)}`
const rateText = (r: number) => `${(r * 10).toFixed(1).replace(/\.0$/, '')}折`

// ==================== 代理列表 ====================
const agents = ref<AgentRow[]>([])
const agentsLoading = ref(false)
const agentsError = ref('')

const agentColumns = computed<DataColumn[]>(() => [
  { key: 'username', label: '代理账号', width: 150, mobile: 'title' },
  { key: 'level_name', label: '等级', width: 110 },
  { key: 'discount_rate', label: '进货折扣', width: 100 },
  { key: 'balance', label: '进货余额', width: 120 },
  { key: 'purchase_quantity', label: '进货(张/额)', width: 140 },
  { key: 'sales_count', label: '销售(单/额)', width: 140 },
  { key: 'is_active', label: '状态', width: 90 },
  { key: 'actions', label: '操作', width: 230, fixed: 'right', align: 'right' },
])

async function loadAgents() {
  agentsLoading.value = true
  agentsError.value = ''
  try {
    const res = await fetchAgents({ limit: 200 })
    agents.value = res.agents ?? []
  } catch (e) {
    agentsError.value = e instanceof Error ? e.message : '加载代理失败'
  } finally {
    agentsLoading.value = false
  }
}

// ===== 指定代理 =====
const levels = ref<AgentLevelRow[]>([])
async function loadLevels() {
  try {
    const res = await fetchAgentLevels()
    levels.value = res.levels ?? []
  } catch {
    levels.value = []
  }
}

const assignDlg = ref({
  visible: false,
  saving: false,
  form: { username: '', level_id: undefined as number | undefined },
})

function openAssign() {
  assignDlg.value.form = { username: '', level_id: undefined }
  assignDlg.value.visible = true
}

async function doAssign() {
  const f = assignDlg.value.form
  if (!f.username.trim()) return ElMessage.warning('请填写代理的站内用户名')
  if (!f.level_id) return ElMessage.warning('请选择代理等级')
  assignDlg.value.saving = true
  try {
    await assignAgent({ username: f.username.trim(), level_id: f.level_id })
    ElMessage.success('已指定代理')
    assignDlg.value.visible = false
    await loadAgents()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '指定失败')
  } finally {
    assignDlg.value.saving = false
  }
}

// ===== 改等级 / 取消 =====
async function doChangeLevel(row: AgentRow, levelId: number) {
  try {
    await updateAgent(row.id, { level_id: levelId })
    ElMessage.success('等级已更新')
    await loadAgents()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '更新失败')
    await loadAgents()
  }
}

async function doToggleAgent(row: AgentRow, active: boolean) {
  try {
    await updateAgent(row.id, { is_active: active })
    ElMessage.success(active ? '代理已恢复' : '代理已取消（账目保留）')
    await loadAgents()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '操作失败')
    await loadAgents()
  }
}

// ===== 余额调整 =====
const balanceDlg = ref({
  visible: false,
  saving: false,
  agent: null as AgentRow | null,
  form: { amount: '', note: '' },
})

function openBalance(row: AgentRow) {
  balanceDlg.value.agent = row
  balanceDlg.value.form = { amount: '', note: '' }
  balanceDlg.value.visible = true
}

async function doAdjustBalance() {
  const dlg = balanceDlg.value
  if (!dlg.agent) return
  const amount = dlg.form.amount.trim()
  if (!amount || Number.isNaN(Number(amount)) || Number(amount) === 0) {
    return ElMessage.warning('请填写非 0 金额（正数增加、负数扣减）')
  }
  dlg.saving = true
  try {
    await adjustAgentBalance(dlg.agent.id, { amount, note: dlg.form.note.trim() })
    ElMessage.success('余额已调整，流水已留痕')
    dlg.visible = false
    await loadAgents()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '调整失败')
  } finally {
    dlg.saving = false
  }
}

// ===== 明细（流水 / 进货 / 销售） =====
const detailDlg = ref({
  visible: false,
  agent: null as AgentRow | null,
  tab: 'ledger' as 'ledger' | 'purchases' | 'sales',
  loading: false,
  logs: [] as AgentLogRow[],
  purchases: [] as AgentPurchaseRow[],
  sales: [] as AgentSaleRow[],
})

async function openDetail(row: AgentRow, which: 'ledger' | 'purchases' | 'sales') {
  detailDlg.value.agent = row
  detailDlg.value.tab = which
  detailDlg.value.visible = true
  await loadDetail()
}

async function loadDetail() {
  const dlg = detailDlg.value
  if (!dlg.agent) return
  dlg.loading = true
  try {
    if (dlg.tab === 'ledger') {
      dlg.logs = (await fetchAgentLedger(dlg.agent.id, { limit: 100 })).logs ?? []
    } else if (dlg.tab === 'purchases') {
      dlg.purchases = (await fetchAgentPurchases(dlg.agent.id, { limit: 100 })).purchases ?? []
    } else {
      dlg.sales = (await fetchAgentSales(dlg.agent.id, { limit: 100 })).sales ?? []
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '加载明细失败')
  } finally {
    dlg.loading = false
  }
}

// ==================== 等级管理 ====================
const levelsLoading = ref(false)
const levelColumns = computed<DataColumn[]>(() => [
  { key: 'name', label: '等级名称', width: 160, mobile: 'title' },
  { key: 'discount_rate', label: '进货折扣率', width: 130 },
  { key: 'sort_order', label: '排序', width: 80 },
  { key: 'agent_count', label: '代理数', width: 90 },
  { key: 'is_active', label: '启用', width: 90 },
  { key: 'actions', label: '操作', width: 150, fixed: 'right', align: 'right' },
])

const levelDlg = ref({
  visible: false,
  saving: false,
  editing: null as AgentLevelRow | null,
  form: { name: '', discount_rate: 0.8, sort_order: 0 },
})

function openLevelCreate() {
  levelDlg.value.editing = null
  levelDlg.value.form = { name: '', discount_rate: 0.8, sort_order: 0 }
  levelDlg.value.visible = true
}

function openLevelEdit(row: AgentLevelRow) {
  levelDlg.value.editing = row
  levelDlg.value.form = {
    name: row.name, discount_rate: row.discount_rate, sort_order: row.sort_order,
  }
  levelDlg.value.visible = true
}

async function doSaveLevel() {
  const dlg = levelDlg.value
  if (!dlg.form.name.trim()) return ElMessage.warning('请填写等级名称')
  if (!(dlg.form.discount_rate > 0 && dlg.form.discount_rate <= 1)) {
    return ElMessage.warning('折扣率须在 (0, 1] 之间，0.8 表示八折进货')
  }
  dlg.saving = true
  try {
    if (dlg.editing) {
      await updateAgentLevel(dlg.editing.id, { ...dlg.form, name: dlg.form.name.trim() })
    } else {
      await createAgentLevel({ ...dlg.form, name: dlg.form.name.trim() })
    }
    ElMessage.success('等级已保存')
    dlg.visible = false
    await loadLevels()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    dlg.saving = false
  }
}

async function doToggleLevel(row: AgentLevelRow, active: boolean) {
  try {
    await updateAgentLevel(row.id, { is_active: active })
    await loadLevels()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '操作失败')
    await loadLevels()
  }
}

async function doDeleteLevel(row: AgentLevelRow) {
  try {
    await ElMessageBox.confirm(
      `删除等级「${row.name}」？有代理在用的等级删不掉。`, '删除等级',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
  } catch { return }
  try {
    await deleteAgentLevel(row.id)
    ElMessage.success('等级已删除')
    await loadLevels()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '删除失败')
  }
}

// ==================== 全局统计 ====================
const overview = ref<AgentOverview | null>(null)
const statsLoading = ref(false)
const allSales = ref<AgentSaleRow[]>([])

async function loadStats() {
  statsLoading.value = true
  try {
    overview.value = await fetchAgentOverview()
    allSales.value = (await fetchAllAgentSales({ limit: 100 })).sales ?? []
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '加载统计失败')
  } finally {
    statsLoading.value = false
  }
}

const saleColumns = computed<DataColumn[]>(() => [
  { key: 'created_at', label: '核销时间', width: 170 },
  { key: 'code', label: '卡码', width: 210, mobile: 'title' },
  { key: 'buyer_username', label: '购买用户', width: 130 },
  { key: 'face_value', label: '面值', width: 100 },
  { key: 'unit_cost', label: '进货价', width: 100 },
  { key: 'profit', label: '毛利', width: 100 },
])

function fmtTime(v: string | null) {
  return v ? v.replace('T', ' ').slice(0, 19) : ''
}

onMounted(async () => {
  levelsLoading.value = true
  try {
    await Promise.all([loadAgents(), loadLevels()])
  } finally {
    levelsLoading.value = false
  }
})
</script>

<template>
  <div class="page">
    <PageHeader
      title="代理分销"
      description="代理分级、进货折扣、余额账本与销售统计：等级与折扣都是可配数据，余额变动全程留痕"
    >
      <template #actions>
        <el-button :icon="RefreshCw" @click="loadAgents(); loadLevels()">刷新</el-button>
        <el-button type="primary" @click="openAssign">指定代理</el-button>
      </template>
    </PageHeader>

    <el-tabs v-model="tab" @tab-change="(t: any) => { if (t === 'stats') loadStats() }">
      <!-- ===== 代理 ===== -->
      <el-tab-pane label="代理" name="agents">
        <SectionCard title="代理列表" :icon="HandCoins" :meta="`${agents.length} 个代理`" flush>
          <DataTable
            :columns="agentColumns"
            :rows="agents"
            :loading="agentsLoading"
            :error="agentsError"
            empty="还没有代理"
            empty-description="点右上角「指定代理」，把站内用户设为代理并选等级。"
            @retry="loadAgents"
          >
            <template #cell-discount_rate="{ row }">{{ rateText(row.discount_rate) }}</template>
            <template #cell-balance="{ row }">{{ money(row.balance) }}</template>
            <template #cell-purchase_quantity="{ row }">
              {{ row.purchase_quantity }} 张 / {{ money(row.purchase_cost_total) }}
            </template>
            <template #cell-sales_count="{ row }">
              {{ row.sales_count }} 单 / {{ money(row.sales_face_total) }}
            </template>
            <template #cell-is_active="{ row }">
              <el-switch
                :model-value="row.is_active"
                @change="(v: boolean) => doToggleAgent(row, v)"
              />
            </template>
            <template #cell-actions="{ row }">
              <el-button link type="primary" @click="openBalance(row)">调余额</el-button>
              <el-button link @click="openDetail(row, 'ledger')">流水</el-button>
              <el-button link @click="openDetail(row, 'purchases')">进货</el-button>
              <el-button link @click="openDetail(row, 'sales')">销售</el-button>
              <el-select
                :model-value="row.level_id"
                size="small"
                style="width: 110px; margin-left: 6px"
                @change="(v: number) => doChangeLevel(row, v)"
              >
                <el-option
                  v-for="lv in levels"
                  :key="lv.id"
                  :label="lv.name"
                  :value="lv.id"
                />
              </el-select>
            </template>
          </DataTable>
        </SectionCard>
      </el-tab-pane>

      <!-- ===== 等级 ===== -->
      <el-tab-pane label="代理等级" name="levels">
        <SectionCard title="等级列表" :meta="`${levels.length} 个等级`" flush>
          <template #actions>
            <el-button type="primary" size="small" @click="openLevelCreate">新建等级</el-button>
          </template>
          <DataTable
            :columns="levelColumns"
            :rows="levels"
            :loading="levelsLoading"
            empty="还没有代理等级"
            empty-description="先建等级（如八折、七折），再指定代理。"
          >
            <template #cell-discount_rate="{ row }">
              {{ row.discount_rate }}（{{ rateText(row.discount_rate) }}进货）
            </template>
            <template #cell-is_active="{ row }">
              <el-switch
                :model-value="row.is_active"
                @change="(v: boolean) => doToggleLevel(row, v)"
              />
            </template>
            <template #cell-actions="{ row }">
              <el-button link type="primary" @click="openLevelEdit(row)">编辑</el-button>
              <el-button link type="danger" @click="doDeleteLevel(row)">删除</el-button>
            </template>
          </DataTable>
        </SectionCard>
      </el-tab-pane>

      <!-- ===== 统计 ===== -->
      <el-tab-pane label="销售统计" name="stats">
        <div v-loading="statsLoading">
          <div v-if="overview" class="stat-grid">
            <div class="stat-card"><div class="stat-label">在册代理</div><div class="stat-value">{{ overview.agent_count }}</div></div>
            <div class="stat-card"><div class="stat-label">累计进货</div><div class="stat-value">{{ overview.purchase_quantity }} 张</div><div class="stat-sub">{{ money(overview.purchase_cost_total) }}</div></div>
            <div class="stat-card"><div class="stat-label">累计销售</div><div class="stat-value">{{ overview.sales_count }} 单</div><div class="stat-sub">面值 {{ money(overview.sales_face_total) }}</div></div>
            <div class="stat-card"><div class="stat-label">累计毛利</div><div class="stat-value">{{ money(overview.sales_face_total - overview.sales_cost_total) }}</div></div>
            <div class="stat-card"><div class="stat-label">管理员累计注入</div><div class="stat-value">{{ money(overview.topped_up_total) }}</div></div>
            <div class="stat-card"><div class="stat-label">代理余额池</div><div class="stat-value">{{ money(overview.balance_pool_total) }}</div></div>
          </div>

          <SectionCard title="最近销售记录" flush>
            <DataTable
              :columns="saleColumns"
              :rows="allSales"
              empty="还没有销售记录"
              empty-description="代理码被用户核销后会出现在这里。"
            >
              <template #cell-created_at="{ row }">{{ fmtTime(row.created_at) }}</template>
              <template #cell-face_value="{ row }">{{ money(row.face_value) }}</template>
              <template #cell-unit_cost="{ row }">{{ money(row.unit_cost) }}</template>
              <template #cell-profit="{ row }">{{ money(row.face_value - row.unit_cost) }}</template>
            </DataTable>
          </SectionCard>

          <SectionCard v-if="overview" title="按月销售（近 12 个月）" flush>
            <DataTable
              :columns="[
                { key: 'period', label: '月份', width: 140 },
                { key: 'count', label: '销售单数', width: 120 },
                { key: 'face_total', label: '销售面值' },
              ]"
              :rows="[...overview.monthly].reverse()"
              empty="暂无数据"
            >
              <template #cell-face_total="{ row }">{{ money(row.face_total) }}</template>
            </DataTable>
          </SectionCard>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- ===== 指定代理弹窗 ===== -->
    <el-dialog v-model="assignDlg.visible" title="指定代理" width="min(440px, 92vw)">
      <el-form :label-position="isPhone ? 'top' : 'right'" label-width="90px">
        <el-form-item label="站内用户名">
          <el-input v-model="assignDlg.form.username" placeholder="要设为代理的用户" />
        </el-form-item>
        <el-form-item label="代理等级">
          <el-select v-model="assignDlg.form.level_id" placeholder="选择等级" style="width: 100%">
            <el-option
              v-for="lv in levels.filter(l => l.is_active)"
              :key="lv.id"
              :label="`${lv.name}（${rateText(lv.discount_rate)}进货）`"
              :value="lv.id"
            />
          </el-select>
          <span class="form-hint">还没有等级时先到「代理等级」标签里新建。</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assignDlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="assignDlg.saving" @click="doAssign">确定</el-button>
      </template>
    </el-dialog>

    <!-- ===== 余额调整弹窗 ===== -->
    <el-dialog v-model="balanceDlg.visible" title="调整进货余额" width="min(440px, 92vw)">
      <el-form :label-position="isPhone ? 'top' : 'right'" label-width="90px">
        <el-form-item label="代理">
          <span>{{ balanceDlg.agent?.username }}（当前余额 {{ money(balanceDlg.agent?.balance ?? 0) }}）</span>
        </el-form-item>
        <el-form-item label="金额">
          <el-input v-model="balanceDlg.form.amount" placeholder="正数增加、负数扣减，如 500 或 -50" />
          <span class="form-hint">扣减不能把余额扣成负数；每次调整都会留一条流水。</span>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="balanceDlg.form.note" placeholder="如：微信收款进货款" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="balanceDlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="balanceDlg.saving" @click="doAdjustBalance">确定</el-button>
      </template>
    </el-dialog>

    <!-- ===== 明细弹窗 ===== -->
    <el-dialog
      v-model="detailDlg.visible"
      :title="`${detailDlg.agent?.username ?? ''} 的明细`"
      width="min(860px, 94vw)"
    >
      <el-tabs v-model="detailDlg.tab" @tab-change="loadDetail">
        <el-tab-pane label="余额流水" name="ledger">
          <el-table :data="detailDlg.logs" v-loading="detailDlg.loading" size="small" stripe>
            <el-table-column label="时间" width="160"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
            <el-table-column label="类型" width="110"><template #default="{ row }">{{ row.kind === 'purchase' ? '进货扣款' : '管理员调整' }}</template></el-table-column>
            <el-table-column label="金额" width="110"><template #default="{ row }">{{ money(row.amount) }}</template></el-table-column>
            <el-table-column label="变动后余额" width="120"><template #default="{ row }">{{ money(row.balance_after) }}</template></el-table-column>
            <el-table-column prop="note" label="备注" />
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="进货记录" name="purchases">
          <el-table :data="detailDlg.purchases" v-loading="detailDlg.loading" size="small" stripe>
            <el-table-column label="时间" width="160"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
            <el-table-column prop="plan_name" label="套餐" />
            <el-table-column label="数量" width="70"><template #default="{ row }">{{ row.quantity }}</template></el-table-column>
            <el-table-column label="面值" width="100"><template #default="{ row }">{{ money(row.face_value) }}</template></el-table-column>
            <el-table-column label="进货价" width="100"><template #default="{ row }">{{ money(row.unit_cost) }}</template></el-table-column>
            <el-table-column label="总扣款" width="110"><template #default="{ row }">{{ money(row.total_cost) }}</template></el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="销售记录" name="sales">
          <el-table :data="detailDlg.sales" v-loading="detailDlg.loading" size="small" stripe>
            <el-table-column label="核销时间" width="160"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
            <el-table-column prop="code" label="卡码" />
            <el-table-column prop="buyer_username" label="购买用户" width="120" />
            <el-table-column label="面值" width="100"><template #default="{ row }">{{ money(row.face_value) }}</template></el-table-column>
            <el-table-column label="进货价" width="100"><template #default="{ row }">{{ money(row.unit_cost) }}</template></el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>

    <!-- ===== 等级弹窗 ===== -->
    <el-dialog
      v-model="levelDlg.visible"
      :title="levelDlg.editing ? '编辑等级' : '新建等级'"
      width="min(440px, 92vw)"
    >
      <el-form :label-position="isPhone ? 'top' : 'right'" label-width="90px">
        <el-form-item label="等级名称">
          <el-input v-model="levelDlg.form.name" placeholder="如：金牌代理" />
        </el-form-item>
        <el-form-item label="进货折扣率">
          <el-input-number
            v-model="levelDlg.form.discount_rate"
            :min="0.01" :max="1" :step="0.05" :precision="4"
          />
          <span class="form-hint">进货价 = 面值 × 折扣率，按分四舍五入。0.8 = 八折进货。</span>
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="levelDlg.form.sort_order" :min="0" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="levelDlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="levelDlg.saving" @click="doSaveLevel">保存</el-button>
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
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.stat-card {
  background: var(--au-surface, #fff);
  border: 1px solid var(--au-border, #e5e7eb);
  border-radius: 10px;
  padding: 14px 16px;
}
.stat-label { font-size: 12px; color: var(--au-text-3); margin-bottom: 6px; }
.stat-value { font-size: 20px; font-weight: 600; }
.stat-sub { font-size: 12px; color: var(--au-text-3); margin-top: 2px; }
</style>
