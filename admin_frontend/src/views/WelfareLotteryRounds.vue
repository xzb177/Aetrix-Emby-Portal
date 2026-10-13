<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Gift, History, Plus, RefreshCw, Trophy, Settings2, Delete } from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import { useBreakpoint } from '@/composables/useBreakpoint'
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

const { isPhone } = useBreakpoint()

const loading = ref(false)
const loadError = ref('')
const rows = ref<LotteryRoundRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20

const columns = computed<DataColumn[]>(() => [
  { key: 'title', label: '标题', mobile: 'title' },
  { key: 'chat_id', label: '群ID', width: 140 },
  { key: 'lottery_type_label', label: '类型', width: 100 },
  { key: 'status', label: '状态', width: 100 },
  { key: 'participant_count', label: '参与人数', width: 100 },
  { key: 'prize_count', label: '奖品数', width: 90 },
  { key: 'draw_at', label: '开奖时间', width: 170 },
  { key: 'actions', label: '操作', width: 220, fixed: 'right', align: 'right' }
])

const load = async () => {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetchLotteryRounds({ page: page.value, page_size: pageSize })
    rows.value = (res.items ?? []).map((r: any) => ({
      ...r,
      lottery_type_label: r.lottery_type === 'password' ? '口令抽奖' : '按钮抽奖',
    }))
    total.value = res.total ?? 0
  } catch (e) {
    // 错误态交给表格（带「重试」），不再只弹一条转瞬即逝的提示
    loadError.value = String(errDetail(e))
  } finally {
    loading.value = false
  }
}

// 新建活动
const dlgVisible = ref(false)
const form = ref({ title: '', chat_id: '', draw_at: null as string | Date | null, max_participants: 0, lottery_type: 'button', password_keyword: '' })
const prizes = ref<{ name: string; type: string; value: number; quantity: number }[]>([])

const openCreate = () => {
  form.value = { title: '', chat_id: '', draw_at: null, max_participants: 0, lottery_type: 'button', password_keyword: '' }
  prizes.value = [{ name: '', type: 'days', value: 0, quantity: 1 }]
  dlgVisible.value = true
}
const addPrize = () => {
  prizes.value.push({ name: '', type: 'days', value: 0, quantity: 1 })
}
const creating = ref(false)
const submitCreate = async () => {
  if (creating.value) return
  if (!form.value.title.trim()) { ElMessage.warning('请填写活动标题'); return }
  const chatId = Number(form.value.chat_id.trim())
  if (!form.value.chat_id.trim() || !Number.isInteger(chatId) || chatId === 0) { ElMessage.warning('请填写有效的群ID（整数）'); return }
  if (prizes.value.length < 1) { ElMessage.warning('至少添加 1 个奖品'); return }
  if (prizes.value.some(p => !p.name.trim())) { ElMessage.warning('每个奖品名称不能为空'); return }
  if (form.value.lottery_type === 'password' && !form.value.password_keyword.trim()) { ElMessage.warning('口令抽奖必须填写口令'); return }
  const payload: any = {
    title: form.value.title.trim(),
    chat_id: chatId,
    draw_at: form.value.draw_at instanceof Date ? form.value.draw_at.toISOString() : (form.value.draw_at || null),
    max_participants: form.value.max_participants > 0 ? form.value.max_participants : null,
    lottery_type: form.value.lottery_type,
    password_keyword: form.value.lottery_type === 'password' ? form.value.password_keyword.trim() : null,
    prizes: prizes.value.map(p => ({ name: p.name.trim(), type: p.type, value: Number(p.value), quantity: Number(p.quantity) }))
  }
  creating.value = true
  try {
    await createLotteryRound(payload)
    ElMessage.success('已创建')
    dlgVisible.value = false
    load()
  } catch {
    /* 写操作失败由请求拦截器统一弹错，这里不再重复提示 */
  } finally {
    creating.value = false
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
const detailLoading = ref(false)
const detailError = ref('')
const detailId = ref<number | null>(null)

const loadDetail = async (id: number) => {
  detailLoading.value = true
  detailError.value = ''
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
    detailError.value = String(errDetail(e))
  } finally {
    detailLoading.value = false
  }
}
const openDetail = (row: LotteryRoundRow) => {
  detail.value = null
  detailId.value = row.id
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
  } catch {
    /* 写操作失败由请求拦截器统一弹错，这里不再重复提示 */
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
  } catch {
    /* 写操作失败由请求拦截器统一弹错，这里不再重复提示 */
  }
}

// 功能开关
const config = ref({ lottery_enabled: '1', lottery_group_ids: '', lottery_auto_draw_enabled: '1', lottery_draw_interval_sec: '60', lottery_notify_winners: '1', lottery_require_group_member: '1', lottery_min_account_age_days: '0', lottery_max_joins_per_day: '0' })
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
// 三重门·参赛资格：参赛者必须在 TG 群里
const requireGroupMember = computed({
  get: () => config.value.lottery_require_group_member !== '0',
  set: (v: boolean) => { config.value.lottery_require_group_member = v ? '1' : '0' }
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
      lottery_notify_winners: data.lottery_notify_winners === '0' ? '0' : '1',
      lottery_require_group_member: data.lottery_require_group_member === '0' ? '0' : '1',
      lottery_min_account_age_days: data.lottery_min_account_age_days ?? '0',
      lottery_max_joins_per_day: data.lottery_max_joins_per_day ?? '0'
    }
  } catch (e) {
    ElMessage.error(errDetail(e))
  }
}
const savingConfig = ref(false)
const saveConfig = async () => {
  if (savingConfig.value) return
  const interval = Number(config.value.lottery_draw_interval_sec)
  if (!Number.isInteger(interval) || interval < 30) { ElMessage.warning('扫描间隔必须是 >= 30 的整数（秒）'); return }
  const minAge = Number(config.value.lottery_min_account_age_days)
  if (!Number.isInteger(minAge) || minAge < 0) { ElMessage.warning('新号限制天数必须 >= 0 的整数'); return }
  const maxJoins = Number(config.value.lottery_max_joins_per_day)
  if (!Number.isInteger(maxJoins) || maxJoins < 0) { ElMessage.warning('每日参加上限必须 >= 0 的整数'); return }
  savingConfig.value = true
  try {
    await saveWelfareConfig({
      lottery_enabled: config.value.lottery_enabled,
      lottery_group_ids: config.value.lottery_group_ids,
      lottery_auto_draw_enabled: config.value.lottery_auto_draw_enabled,
      lottery_draw_interval_sec: String(interval),
      lottery_notify_winners: config.value.lottery_notify_winners,
      lottery_require_group_member: config.value.lottery_require_group_member,
      lottery_min_account_age_days: String(minAge),
      lottery_max_joins_per_day: String(maxJoins)
    })
    ElMessage.success('已保存')
  } catch {
    /* 写操作失败由请求拦截器统一弹错，这里不再重复提示 */
  } finally {
    savingConfig.value = false
  }
}

onMounted(() => {
  load()
  loadConfig()
})
</script>

<template>
  <div class="admin-page">
    <PageHeader eyebrow="公益服" title="群抽奖活动" description="创建与管理 Telegram 群抽奖活动，开奖与发放">
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建活动</el-button>
      </template>
    </PageHeader>

    <SectionCard title="活动列表" :icon="Trophy" :meta="`共 ${total} 个活动`" flush>
      <DataTable
        :columns="columns"
        :rows="rows"
        :loading="loading"
        :error="loadError"
        empty="还没有抽奖活动"
        empty-description="点右上角「新建活动」在指定群里发起一次抽奖。"
        @retry="load"
        :pagination="{ total, page, pageSize }"
        @page-change="(p: number) => { page = p; load() }"
      >
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
    </SectionCard>

    <SectionCard title="功能开关" :icon="Settings2" meta="总开关与允许群ID 两行配置">
      <el-form :label-position="isPhone ? 'top' : 'right'" label-width="220px">
        <el-form-item label="群抽奖总开关（关闭后不可新建活动）">
          <el-switch v-model="lotteryEnabled" />
        </el-form-item>
        <el-form-item label="允许群ID">
          <el-input v-model="config.lottery_group_ids" placeholder="逗号分隔的群 chat_id，空=不限制" class="field-md" />
        </el-form-item>
        <el-form-item label="自动开奖">
          <el-switch v-model="autoDrawEnabled" />
          <span class="field-hint">关闭后到期活动不再自动开奖，只能手动开奖</span>
        </el-form-item>
        <el-form-item label="扫描间隔">
          <el-input v-model="config.lottery_draw_interval_sec" class="field-sm" />
          <span class="field-suffix">秒</span>
          <span class="field-hint">每隔多少秒扫描一次到期活动，默认 60，最小 30</span>
        </el-form-item>
        <el-form-item label="开奖通知">
          <el-switch v-model="notifyWinners" />
          <span class="field-hint">开奖后在群里公布中奖名单并私聊通知中奖者</span>
        </el-form-item>
        <el-form-item label="参赛资格：必须在群里">
          <el-switch v-model="requireGroupMember" />
          <span class="field-hint">参赛者必须在 TG 群成员列表里，防群外薅奖</span>
        </el-form-item>
        <el-form-item label="参赛资格：新号限制">
          <el-input v-model="config.lottery_min_account_age_days" class="field-sm" />
          <span class="field-suffix">天</span>
          <span class="field-hint">账号注册满多少天才能参加，0=不限制</span>
        </el-form-item>
        <el-form-item label="参赛资格：每日上限">
          <el-input v-model="config.lottery_max_joins_per_day" class="field-sm" />
          <span class="field-suffix">次</span>
          <span class="field-hint">每人每天最多参加次数，0=不限制</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="savingConfig" @click="saveConfig">保存</el-button>
        </el-form-item>
      </el-form>
    </SectionCard>

    <el-dialog v-model="dlgVisible" title="新建活动" width="min(560px, 92vw)">
      <el-form :label-position="isPhone ? 'top' : 'right'" label-width="80px">
        <el-form-item label="标题">
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="抽奖类型">
          <el-select v-model="form.lottery_type" style="width: 100%">
            <el-option label="按钮抽奖" value="button" />
            <el-option label="口令抽奖" value="password" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.lottery_type === 'password'" label="口令">
          <el-input v-model="form.password_keyword" placeholder="用户在群里发送此口令参加，如：我要抽奖" />
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
          <div v-for="(p, i) in prizes" :key="i" class="prize-row">
            <el-input v-model="p.name" placeholder="奖品名" class="prize-name" />
            <el-select v-model="p.type" class="prize-type">
              <el-option label="公益天数" value="days" />
              <el-option label="积分" value="points" />
              <el-option label="白名单" value="whitelist" />
            </el-select>
            <div class="prize-field">
              <span class="prize-label">{{ p.type === 'days' ? '天数' : p.type === 'points' ? '积分' : '奖品值' }}</span>
              <el-input-number v-model="p.value" :min="0" controls-position="right" class="prize-num" aria-label="奖品值" />
            </div>
            <div class="prize-field">
              <span class="prize-label">份数</span>
              <el-input-number v-model="p.quantity" :min="1" controls-position="right" class="prize-num" aria-label="份数" />
            </div>
            <el-button link type="danger" :icon="Delete" aria-label="删除奖品" @click="prizes.splice(i, 1)" />
          </div>
          <el-button @click="addPrize">添加奖品</el-button>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlgVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">确定</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="detailVisible" title="活动详情" :size="isPhone ? '100%' : '640px'">
      <div v-if="detailLoading && !detail" v-loading="true" class="detail-loading" />
      <EmptyState
        v-else-if="detailError"
        compact
        title="详情加载失败"
        :description="detailError"
      >
        <template #actions>
          <el-button @click="detailId != null && loadDetail(detailId)">重试</el-button>
        </template>
      </EmptyState>
      <template v-else-if="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="标题">{{ detail.title }}</el-descriptions-item>
          <el-descriptions-item label="群ID">{{ detail.chat_id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusTagType(detail.status)" size="small">{{ statusMap[detail.status] ?? detail.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="人数上限">{{ detail.max_participants ? detail.max_participants : '不限' }}</el-descriptions-item>
          <el-descriptions-item label="开奖时间">{{ fmtTime(detail.draw_at) }}</el-descriptions-item>
          <el-descriptions-item label="seed_hash 公示">
            <span class="mono">{{ (detail.seed_hash ?? '').slice(0, 16) }}…</span>
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ fmtTime(detail.created_at) }}</el-descriptions-item>
        </el-descriptions>

        <div class="detail-title">
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

        <div class="detail-title"><Trophy :size="14" /> 中奖名单</div>
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

        <div class="detail-title">
          <History :size="14" /> 参与者（前 100）
        </div>
        <div v-for="(u, i) in (detail.entries ?? []).slice(0, 100)" :key="i" class="entry-row">
          {{ u.telegram_id ?? u.user_id }} <span class="entry-time">· {{ fmtTime(u.joined_at) }}</span>
        </div>
        <EmptyState v-if="!(detail.entries ?? []).length" compact title="暂无参与者" />
      </template>
      <template #footer>
        <el-button v-if="detail?.status === 'open'" type="primary" @click="handleDraw(detail)">手动开奖</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.au-pagination { margin-top: 12px; justify-content: flex-end; padding: 0 16px 16px; flex-wrap: wrap; }
.field-md { width: 100%; max-width: 360px; }
.field-sm { width: 100%; max-width: 160px; }
.prize-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; width: 100%; }
.prize-name { flex: 1 1 160px; min-width: 0; }
.prize-type { width: 120px; }
.prize-num { width: 110px; }
.prize-field { display: inline-flex; align-items: center; gap: 6px; flex: none; }
.prize-label { font-size: 12px; line-height: 1; color: var(--au-text-2); white-space: nowrap; user-select: none; }
.detail-loading { min-height: 160px; }
.detail-title { display: flex; align-items: center; gap: 6px; margin: 16px 0 8px; font-weight: 600; color: var(--au-text); }
.detail-title svg { color: var(--au-primary); }
.mono { font-family: var(--font-mono); font-size: 12px; }
.entry-row { padding: 2px 0; font-size: 13px; color: var(--au-text-2); }
.entry-time { color: var(--au-text-3); }
.field-suffix {
  margin-left: 8px;
  color: var(--au-text-2);
  white-space: nowrap;
}
.field-hint {
  flex-basis: 100%;
  color: var(--au-text-3);
  font-size: 12px;
  line-height: 1.5;
}
:deep(.el-form-item__content) { flex-wrap: wrap; row-gap: 4px; }
</style>
