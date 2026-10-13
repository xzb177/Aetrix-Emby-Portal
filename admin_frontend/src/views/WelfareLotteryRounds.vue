<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Gift, History, Plus, RefreshCw, Trophy, Settings2, Delete } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import {
  fetchLotteryRounds,
  createLotteryRound,
  fetchLotteryRound,
  cancelLotteryRound,
  drawLotteryRound,
  fetchWelfareConfig,
  saveWelfareConfig,
  type LotteryRoundRow,
  type LotteryRoundDetail,
} from '@/api/welfare'

const statusMap: Record<string, string> = { open: '进行中', drawing: '开奖中', done: '已结束', cancelled: '已取消' }
const prizeTypeMap: Record<string, string> = { days: '公益天数', points: '积分', whitelist: '白名单' }
const statusTagType = (s?: string): 'success' | 'warning' | 'info' | 'danger' =>
  (({ open: 'success', drawing: 'warning', done: 'info', cancelled: 'danger' } as Record<string, 'success' | 'warning' | 'info' | 'danger'>)[s ?? ''] ?? 'info')

const fmtTime = (v?: string | null) => (v ? String(v).replace('T', ' ').slice(0, 19) : '—')
const errDetail = (e: any) => e?.response?.data?.detail ?? e?.message ?? '操作失败'

const loading = ref(false)
const rows = ref<LotteryRoundRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20

const columns = computed<DataColumn[]>(() => [
  { key: 'title', label: '标题', mobile: 'title' },
  { key: 'chat_id', label: '群ID', width: 140 },
  { key: 'status', label: '状态', width: 100 },
  { key: 'participant_count', label: '参与人数', width: 100 },
  { key: 'prize_count', label: '奖品数', width: 90 },
  { key: 'draw_at', label: '开奖时间', width: 170 },
  { key: 'actions', label: '操作', width: 220, fixed: 'right', align: 'right' }
])

const load = async () => {
  loading.value = true
  try {
    const res = await fetchLotteryRounds({ page: page.value, page_size: pageSize })
    rows.value = res.items ?? []
    total.value = res.total ?? 0
  } catch (e) {
    ElMessage.error(errDetail(e))
  } finally {
    loading.value = false
  }
}

// 新建活动
const dlgVisible = ref(false)
const form = ref({ title: '', chat_id: '', draw_at: null as string | Date | null, max_participants: 0 })
const prizes = ref<{ name: string; type: string; value: number; quantity: number }[]>([])

const openCreate = () => {
  form.value = { title: '', chat_id: '', draw_at: null, max_participants: 0 }
  prizes.value = [{ name: '', type: 'days', value: 0, quantity: 1 }]
  dlgVisible.value = true
}
const addPrize = () => {
  prizes.value.push({ name: '', type: 'days', value: 0, quantity: 1 })
}
const submitCreate = async () => {
  if (!form.value.title.trim()) { ElMessage.warning('请填写活动标题'); return }
  const chatId = Number(form.value.chat_id.trim())
  if (!form.value.chat_id.trim() || !Number.isInteger(chatId) || chatId === 0) { ElMessage.warning('请填写有效的群ID（整数）'); return }
  if (prizes.value.length < 1) { ElMessage.warning('至少添加 1 个奖品'); return }
  if (prizes.value.some(p => !p.name.trim())) { ElMessage.warning('每个奖品名称不能为空'); return }
  const payload: any = {
    title: form.value.title.trim(),
    chat_id: chatId,
    draw_at: form.value.draw_at instanceof Date ? form.value.draw_at.toISOString() : (form.value.draw_at || null),
    max_participants: form.value.max_participants > 0 ? form.value.max_participants : null,
    prizes: prizes.value.map(p => ({ name: p.name.trim(), type: p.type, value: Number(p.value), quantity: Number(p.quantity) }))
  }
  try {
    await createLotteryRound(payload)
    ElMessage.success('已创建')
    dlgVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(errDetail(e))
  }
}

// 详情抽屉
const detailVisible = ref(false)
/** 详情视图：后端返回 {round, prizes, entries, winners, verify}，这里把 round 字段拍平 */
interface RoundDetailView {
  id: number
  title: string
  chat_id: string
  status: string
  max_participants: number | null
  seed_hash: string | null
  seed: string | null
  draw_at: string | null
  created_at: string | null
  prizes: LotteryRoundDetail['prizes']
  entries: LotteryRoundDetail['entries']
  winners: LotteryRoundDetail['winners']
  verify: LotteryRoundDetail['verify']
}
const detail = ref<RoundDetailView | null>(null)

const loadDetail = async (id: number) => {
  try {
    const res = await fetchLotteryRound(id)
    const round = (res.round ?? {}) as Record<string, unknown>
    detail.value = {
      id: Number(round.id), title: String(round.title ?? ''), chat_id: String(round.chat_id ?? ''),
      status: String(round.status ?? ''), max_participants: round.max_participants != null ? Number(round.max_participants) : null,
      seed_hash: round.seed_hash != null ? String(round.seed_hash) : null,
      seed: round.seed != null ? String(round.seed) : null,
      draw_at: round.draw_at != null ? String(round.draw_at) : null,
      created_at: round.created_at != null ? String(round.created_at) : null,
      prizes: res.prizes, entries: res.entries, winners: res.winners, verify: res.verify,
    }
  } catch (e) {
    ElMessage.error(errDetail(e))
  }
}
const openDetail = (row: LotteryRoundRow) => {
  detail.value = null
  detailVisible.value = true
  loadDetail(row.id)
}
const isDistributed = (w: { distributed?: boolean }) => w?.distributed === true || (w as Record<string, unknown>)?.distributed === 1

// 开奖 / 取消
const handleDraw = async (row: { id: number; title: string }) => {
  try {
    await ElMessageBox.confirm(`对活动「${row.title}」执行开奖？`, '确认', { type: 'warning' })
  } catch { return }
  try {
    const res = await drawLotteryRound(row.id)
    const winners = Array.isArray(res.winners) ? res.winners : []
    ElMessage.success(`开奖完成，中奖 ${winners.length} 人`)
    load()
    if (detailVisible.value && detail.value?.id === row.id) await loadDetail(row.id)
  } catch (e) {
    ElMessage.error(errDetail(e))
  }
}
const handleCancel = async (row: { id: number; title: string }) => {
  try {
    await ElMessageBox.confirm(`取消活动「${row.title}」？`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await cancelLotteryRound(row.id)
    ElMessage.success('已取消活动')
    load()
    if (detailVisible.value && detail.value?.id === row.id) await loadDetail(row.id)
  } catch (e) {
    ElMessage.error(errDetail(e))
  }
}

// 功能开关
const config = ref({ lottery_enabled: '1', lottery_group_ids: '', lottery_auto_draw_enabled: '1', lottery_draw_interval_sec: '60', lottery_notify_winners: '1' })
const lotteryEnabled = computed({
  get: () => config.value.lottery_enabled === '1',
  set: (v: boolean) => { config.value.lottery_enabled = v ? '1' : '0' }
})
const autoDrawEnabled = computed({
  get: () => config.value.lottery_auto_draw_enabled === '1',
  set: (v: boolean) => { config.value.lottery_auto_draw_enabled = v ? '1' : '0' }
})
const notifyWinners = computed({
  get: () => config.value.lottery_notify_winners === '1',
  set: (v: boolean) => { config.value.lottery_notify_winners = v ? '1' : '0' }
})
const loadConfig = async () => {
  try {
    const res: any = await fetchWelfareConfig()
    const data = res?.data ?? res ?? {}
    config.value = {
      lottery_enabled: data.lottery_enabled === '1' ? '1' : '0',
      lottery_group_ids: data.lottery_group_ids ?? '',
      lottery_auto_draw_enabled: data.lottery_auto_draw_enabled === '0' ? '0' : '1',
      lottery_draw_interval_sec: data.lottery_draw_interval_sec ?? '60',
      lottery_notify_winners: data.lottery_notify_winners === '0' ? '0' : '1'
    }
  } catch (e) {
    ElMessage.error(errDetail(e))
  }
}
const saveConfig = async () => {
  const interval = Number(config.value.lottery_draw_interval_sec)
  if (!Number.isInteger(interval) || interval < 30) { ElMessage.warning('扫描间隔必须是 >= 30 的整数（秒）'); return }
  try {
    await saveWelfareConfig({
      lottery_enabled: config.value.lottery_enabled,
      lottery_group_ids: config.value.lottery_group_ids,
      lottery_auto_draw_enabled: config.value.lottery_auto_draw_enabled,
      lottery_draw_interval_sec: String(interval),
      lottery_notify_winners: config.value.lottery_notify_winners
    })
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(errDetail(e))
  }
}

onMounted(() => {
  load()
  loadConfig()
})
</script>

<template>
  <div>
    <PageHeader eyebrow="公益服" title="群抽奖活动" description="创建与管理 Telegram 群抽奖活动，开奖与发放">
      <template #actions>
        <el-button :icon="RefreshCw" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建活动</el-button>
      </template>
    </PageHeader>

    <SectionCard title="活动列表" :icon="Trophy" :meta="`共 ${total} 个活动`" flush>
      <DataTable :columns="columns" :rows="rows" :loading="loading">
        <template #cell-status="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small">{{ statusMap[row.status] ?? row.status }}</el-tag>
        </template>
        <template #cell-draw_at="{ row }">{{ fmtTime(row.draw_at) }}</template>
        <template #cell-actions="{ row }">
          <el-button link type="primary" @click="openDetail(row)">详情</el-button>
          <el-button v-if="row.status === 'open'" link type="warning" @click="handleDraw(row)">开奖</el-button>
          <el-button v-if="row.status === 'open'" link type="danger" @click="handleCancel(row)">取消</el-button>
        </template>
      </DataTable>
      <el-pagination v-model:current-page="page" :page-size="pageSize" :total="total" layout="total, prev, pager, next" @change="load" class="au-pagination" />
    </SectionCard>

    <div class="section-gap" />

    <SectionCard title="功能开关" :icon="Settings2" meta="总开关与允许群ID 两行配置">
      <el-form label-width="220px">
        <el-form-item label="群抽奖总开关（关闭后不可新建活动）">
          <el-switch v-model="lotteryEnabled" />
        </el-form-item>
        <el-form-item label="允许群ID">
          <el-input v-model="config.lottery_group_ids" placeholder="逗号分隔的群 chat_id，空=不限制" style="max-width: 360px" />
        </el-form-item>
        <el-form-item label="自动开奖">
          <el-switch v-model="autoDrawEnabled" />
          <span class="field-hint">关闭后到期活动不再自动开奖，只能手动开奖</span>
        </el-form-item>
        <el-form-item label="扫描间隔">
          <el-input v-model="config.lottery_draw_interval_sec" style="max-width: 160px" />
          <span class="field-suffix">秒</span>
          <span class="field-hint">每隔多少秒扫描一次到期活动，默认 60，最小 30</span>
        </el-form-item>
        <el-form-item label="开奖通知">
          <el-switch v-model="notifyWinners" />
          <span class="field-hint">开奖后在群里公布中奖名单并私聊通知中奖者</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="saveConfig">保存</el-button>
        </el-form-item>
      </el-form>
    </SectionCard>

    <el-dialog v-model="dlgVisible" title="新建活动" width="560px">
      <el-form label-width="80px">
        <el-form-item label="标题">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="群ID">
          <el-input v-model="form.chat_id" placeholder="群 chat_id，如 -1001234567890" />
        </el-form-item>
        <el-form-item label="开奖时间">
          <el-date-picker v-model="form.draw_at" type="datetime" placeholder="可选，不填表示手动开奖" style="width: 100%" />
        </el-form-item>
        <el-form-item label="人数上限">
          <el-input-number v-model="form.max_participants" :min="0" />
        </el-form-item>
        <el-form-item label="奖品">
          <div v-for="(p, i) in prizes" :key="i" style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px">
            <el-input v-model="p.name" placeholder="奖品名" style="flex: 1" />
            <el-select v-model="p.type" style="width: 120px">
              <el-option label="公益天数" value="days" />
              <el-option label="积分" value="points" />
              <el-option label="白名单" value="whitelist" />
            </el-select>
            <el-input-number v-model="p.value" :min="0" controls-position="right" style="width: 110px" />
            <el-input-number v-model="p.quantity" :min="1" controls-position="right" style="width: 100px" />
            <el-button link type="danger" :icon="Delete" @click="prizes.splice(i, 1)" />
          </div>
          <el-button @click="addPrize">添加奖品</el-button>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlgVisible = false">取消</el-button>
        <el-button type="primary" @click="submitCreate">确定</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="detailVisible" title="活动详情" size="640px">
      <template v-if="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="标题">{{ detail.title }}</el-descriptions-item>
          <el-descriptions-item label="群ID">{{ detail.chat_id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusTagType(detail.status)" size="small">{{ statusMap[detail.status] ?? detail.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="人数上限">{{ detail.max_participants ? detail.max_participants : '不限' }}</el-descriptions-item>
          <el-descriptions-item label="开奖时间">{{ fmtTime(detail.draw_at) }}</el-descriptions-item>
          <el-descriptions-item label="seed_hash 公示">
            <span style="font-family: monospace; font-size: 12px">{{ (detail.seed_hash ?? '').slice(0, 16) }}…</span>
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ fmtTime(detail.created_at) }}</el-descriptions-item>
        </el-descriptions>

        <div style="display: flex; align-items: center; gap: 6px; margin: 16px 0 8px; font-weight: 600">
          <Gift :size="14" /> 奖品列表
        </div>
        <el-table :data="detail.prizes ?? []" size="small" border>
          <el-table-column prop="name" label="名称" show-overflow-tooltip />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">{{ prizeTypeMap[row.type] ?? row.type }}</template>
          </el-table-column>
          <el-table-column prop="value" label="值" width="90" />
          <el-table-column prop="quantity" label="份数" width="90" />
        </el-table>

        <div style="display: flex; align-items: center; gap: 6px; margin: 16px 0 8px; font-weight: 600">中奖名单</div>
        <el-table :data="detail.winners ?? []" size="small" border>
          <el-table-column label="奖品名" show-overflow-tooltip>
            <template #default="{ row }">{{ row.prize_name ?? row.prize?.name }}</template>
          </el-table-column>
          <el-table-column label="中奖用户" show-overflow-tooltip>
            <template #default="{ row }">{{ row.telegram_id ?? row.user_id }}</template>
          </el-table-column>
          <el-table-column label="发放状态" width="100">
            <template #default="{ row }">
              <el-tag :type="isDistributed(row) ? 'success' : 'warning'" size="small">{{ isDistributed(row) ? '已发放' : '待发放' }}</el-tag>
            </template>
          </el-table-column>
        </el-table>

        <div style="display: flex; align-items: center; gap: 6px; margin: 16px 0 8px; font-weight: 600">
          <History :size="14" /> 参与者（前 100）
        </div>
        <div v-for="(u, i) in (detail.entries ?? []).slice(0, 100)" :key="i" style="padding: 2px 0; font-size: 13px">
          {{ u.telegram_id ?? u.user_id }} <span style="color: var(--el-text-color-secondary)">· {{ fmtTime(u.joined_at) }}</span>
        </div>
        <div v-if="!(detail.entries ?? []).length" style="color: var(--el-text-color-secondary); font-size: 13px">暂无参与者</div>
      </template>
      <template #footer>
        <el-button v-if="detail?.status === 'open'" type="primary" @click="handleDraw(detail)">手动开奖</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.section-gap { margin-top: 16px; }
.au-pagination { margin-top: 12px; justify-content: flex-end; padding: 0 16px 16px; }
.field-suffix {
  margin-left: 8px;
  color: var(--au-text-2);
  white-space: nowrap;
}
.field-hint {
  margin-left: 8px;
  color: var(--au-text-2);
  font-size: 12px;
}
</style>
