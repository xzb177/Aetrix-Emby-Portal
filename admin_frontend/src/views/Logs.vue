<script setup lang="ts">
/** 操作日志：管理员操作审计流水 */
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, ScrollText, Search } from 'lucide-vue-next'
import { fetchLogs } from '@/api/admin'
import { PageHeader, SectionCard } from '@/components/ui'
import type { AdminLogRow } from '@/types'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'

/** 手机卡片以「操作」为标题，时间/目标/IP 作为键值行；详情本来就是长文本 */
const columns: DataColumn[] = [
  { key: 'action', label: '操作', width: 140, mobile: 'title' },
  { key: 'admin_name', label: '操作人', width: 120 },
  { key: 'target_type', label: '目标', width: 130, mobile: 'hide' },
  { key: 'detail', label: '详情', minWidth: 260 },
  { key: 'ip_address', label: 'IP', width: 130, mobile: 'hide' },
  { key: 'created_at', label: '时间', width: 170 },
]

const logs = ref<AdminLogRow[]>([])
const loading = ref(false)
const loadError = ref('')
const actionFilter = ref('')
const limit = ref(100)
const keyword = ref('')

/** 前端筛选：按操作人 / 目标 / 详情关键字（后端只提供 action 过滤） */
const visibleLogs = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return logs.value
  return logs.value.filter((l) => {
    const haystack = [l.admin_name, l.action, l.target_type, l.target_id, l.ip_address, JSON.stringify(l.details ?? '')]
      .join(' ')
      .toLowerCase()
    return haystack.includes(kw)
  })
})

const ACTION_LABELS: Record<string, string> = {
  admin_change_password: '管理员改密',
  update_user: '更新用户',
  reset_user_password: '重置用户密码',
  send_user_message: '发送用户消息',
  broadcast_message: '全站广播',
  create_registration_codes: '生成注册码',
  update_registration_code: '更新注册码',
  set_registration_mode: '设置注册模式',
  create_announcement: '发布公告',
  update_announcement: '更新公告',
  delete_announcement: '删除公告',
  update_ticket: '更新工单',
  reply_ticket: '回复工单',
  update_media_seek: '审核求片',
  grant_subscription: '授予订阅',
  extend_subscription: '延长订阅',
  economy_create_plan: '新建套餐',
  economy_update_plan: '修改套餐',
  economy_delete_plan: '删除套餐',
  economy_create_package: '新建充值包',
  economy_update_package: '修改充值包',
  economy_delete_package: '删除充值包',
  economy_create_exchange_codes: '生成兑换码',
  economy_update_exchange_code: '修改兑换码',
  economy_mark_order_paid: '人工补单',
  economy_adjust_points: '调整积分',
  economy_update_settings: '修改经济设置',
}

/** 高频操作作为快捷筛选项，其余可在下拉中搜索 */
const QUICK_ACTIONS = [
  'economy_mark_order_paid',
  'economy_adjust_points',
  'grant_subscription',
  'update_user',
]

/** 目标类型中文（user#123 这类裸英文不上界面） */
const TARGET_TYPE_LABEL: Record<string, string> = {
  user: '用户',
  admin: '管理员',
  subscription: '会员',
  plan: '套餐',
  package: '充值包',
  order: '订单',
  code: '码',
  registration_code: '注册码',
  invitation_code: '邀请码',
  exchange_code: '兑换码',
  invitation: '邀请',
  coupon: '优惠券',
  announcement: '公告',
  ticket: '工单',
  media_seek: '求片',
  media_item: '媒体条目',
  media_request: '媒体请求',
  device: '设备',
  user_device: '用户设备',
  realm: '服',
  server: '服务器',
  library: '媒体库',
  member_level: '会员等级',
  system: '系统',
  system_config: '系统配置',
  config: '配置',
  gdrive_sa: 'Google 子账号',
  emby: 'Emby',
  all_users: '全部用户',
  alert: '告警',
  alert_rule: '告警规则',
  api_key: 'API Key',
  client: '客户端',
  share_guard_event: '防共享事件',
  lottery_round: '抽奖轮次',
  lottery_blacklist: '抽奖黑名单',
  login_log: '登录日志',
  capability: '功能开关',
  self: '自己',
}

/** 详情字段中文（英文键不上界面，未收录的键原样兜底） */
const DETAIL_KEY_LABEL: Record<string, string> = {
  name: '名称',
  title: '标题',
  username: '用户名',
  target_username: '目标用户名',
  user_id: '用户ID',
  kind: '类型',
  type: '类型',
  code: '码',
  code_type: '码类型',
  is_decoy: '诱饵码',
  order_id: '订单ID',
  key_id: '密钥ID',
  days: '天数',
  add_days: '增加天数',
  duration_days: '时长（天）',
  count: '数量',
  amount: '金额',
  price: '价格',
  points_price: '积分价',
  max_uses: '最大使用次数',
  affected: '影响条数',
  ok: '成功',
  deleted: '已删除',
  matched: '命中',
  verified: '已核验',
  result: '结果',
  error: '错误',
  reason: '原因',
  status: '状态',
  closed: '已关闭',
  already_closed: '原本已关闭',
  enabled: '启用',
  is_active: '启用',
  mode: '模式',
  level: '等级',
  channel: '渠道',
  device_id: '设备ID',
  server: '服务器',
  target: '目标',
  realm_id: '服',
  plan_id: '套餐ID',
  rule_id: '规则ID',
  event_type: '事件',
  scopes: '权限范围',
  whitelist: '白名单',
  force: '强制',
  item_type: '条目类型',
  sa_file: '账号文件',
  chat_id: '群ID',
  winner_count: '中奖人数',
  lottery_type: '抽奖类型',
  distributed: '已发放',
  updated: '已更新',
  access_mode: '访问模式',
  mount_health_ok: '挂载健康',
  play_sign_ttl: '播放签名有效期',
}

function targetText(row: AdminLogRow): string {
  if (!row.target_type) return ''
  const label = TARGET_TYPE_LABEL[row.target_type] ?? row.target_type
  return row.target_id === null || row.target_id === undefined
    ? label
    : `${label} #${row.target_id}`
}

function detailValue(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const params: Record<string, unknown> = { limit: limit.value }
    if (actionFilter.value) params.action_filter = actionFilter.value
    logs.value = await fetchLogs(params)
  } catch (e) {
    // 查询失败拦截器不弹提示（见 utils/request.ts），由表格错误态兜底
    loadError.value = e instanceof Error ? e.message : '加载失败'
    logs.value = []
  } finally {
    loading.value = false
  }
}

onMounted(load)

function toggleQuick(a: string) {
  actionFilter.value = actionFilter.value === a ? '' : a
  load()
}

const metaText = computed(() =>
  keyword.value.trim() ? `筛出 ${visibleLogs.value.length} / ${logs.value.length} 条` : `最近 ${logs.value.length} 条`,
)

function fmtDate(s: string): string {
  return s.slice(0, 19).replace('T', ' ')
}

function detailText(log: AdminLogRow): string {
  if (!log.details) return ''
  const parts: string[] = []
  for (const [k, v] of Object.entries(log.details)) {
    parts.push(`${DETAIL_KEY_LABEL[k] ?? k}=${detailValue(v)}`)
  }
  return parts.join(' ')
}
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="系统与审计"
      title="操作日志"
      :description="`每一次管理操作都会留下审计记录；后端按操作类型筛选，关键字在已加载的最近 ${limit} 条里查。`"
    >
      <template #actions>
        <el-button :loading="loading" @click="load" :icon="RefreshCw">刷新</el-button>
      </template>
    </PageHeader>

    <SectionCard title="审计流水" :icon="ScrollText" :meta="loading ? '加载中…' : metaText" flush>
      <div class="list-bar">
        <div class="toolbar">
          <el-input v-model="keyword" placeholder="搜索操作人 / 目标 / 详情" clearable>
            <template #prefix><Search :size="14" /></template>
          </el-input>
          <el-select v-model="actionFilter" placeholder="全部操作类型" clearable filterable @change="load">
            <el-option v-for="(label, key) in ACTION_LABELS" :key="key" :label="label" :value="key" />
          </el-select>
        </div>
        <div class="head-actions">
          <el-select v-model="limit" class="w-limit" aria-label="加载条数" @change="load">
            <el-option :label="'最近 100 条'" :value="100" />
            <el-option :label="'最近 300 条'" :value="300" />
            <el-option :label="'最近 500 条'" :value="500" />
          </el-select>
        </div>
      </div>

      <div class="quick-filters" role="group" aria-label="常用操作快捷筛选">
        <span class="quick-label">常用</span>
        <button
          v-for="a in QUICK_ACTIONS"
          :key="a"
          type="button"
          class="quick-chip"
          :class="{ active: actionFilter === a }"
          :aria-pressed="actionFilter === a"
          @click="toggleQuick(a)"
        >{{ ACTION_LABELS[a] }}</button>
      </div>

      <DataTable
        :rows="visibleLogs"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        :empty="keyword || actionFilter ? '没有匹配的日志' : '暂无操作日志'"
        :empty-description="keyword || actionFilter ? '换个关键字或清空操作类型再试。' : '管理员的每次改动都会记录在这里。'"
        @retry="load"
      >
        <template #cell-action="{ row }">
          <span class="au-badge au-badge-amber">{{ ACTION_LABELS[row.action] || row.action }}</span>
        </template>

        <template #cell-admin_name="{ row }">{{ row.admin_name }}</template>

        <template #cell-target_type="{ row }">
          <span v-if="row.target_type" class="mono">{{ targetText(row) }}</span>
          <span v-else class="muted">—</span>
        </template>

        <template #cell-detail="{ row }">
          <span class="log-detail">{{ detailText(row) || '—' }}</span>
          <div v-if="row.error_hint" class="error-hint">
            <div class="error-hint-title">{{ row.error_hint.title }}</div>
            <div class="error-hint-what">{{ row.error_hint.what }}</div>
            <ol v-if="row.error_hint.steps?.length" class="error-hint-steps">
              <li v-for="(s, i) in row.error_hint.steps" :key="i">{{ s }}</li>
            </ol>
          </div>
        </template>

        <template #cell-ip_address="{ row }">
          <span class="mono">{{ row.ip_address || '—' }}</span>
        </template>

        <template #cell-created_at="{ row }"><span class="mono">{{ fmtDate(row.created_at) }}</span></template>
      </DataTable>
    </SectionCard>
  </div>
</template>

<style scoped>
.list-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 12px;
  flex-wrap: wrap;
  padding: 4px 20px 10px;
}

.list-bar .toolbar { flex: 1 1 auto; }
.list-bar .head-actions { justify-content: flex-end; }
.w-limit { width: 124px; }

.quick-filters {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 0 20px 12px;
}

.quick-label { font-size: 12px; color: var(--au-text-4); margin-right: 2px; }

.quick-chip {
  padding: 4px 12px;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-border);
  background: transparent;
  color: var(--au-text-2);
  font-size: 12px;
  cursor: pointer;
  transition: color var(--au-fast) var(--au-ease), border-color var(--au-fast) var(--au-ease),
    background var(--au-fast) var(--au-ease);
}

.quick-chip:hover { color: var(--au-text); border-color: var(--au-border-strong); }
.quick-chip:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 1px; }

.quick-chip.active {
  color: var(--au-primary);
  border-color: var(--au-primary-border);
  background: var(--au-primary-soft);
  font-weight: 600;
}

.log-detail {
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--au-text-2);
  word-break: break-all;
}

/** 错误码大白话：日志详情下的中文解释（只用已有 --au-* 令牌） */
.error-hint {
  margin-top: 6px;
  padding: 8px 10px;
  border-left: 2px solid var(--au-primary);
  background: var(--au-primary-soft);
  border-radius: 0 var(--au-r-sm) var(--au-r-sm) 0;
}

.error-hint-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--au-primary);
  margin-bottom: 2px;
}

.error-hint-what {
  font-size: 12px;
  color: var(--au-text-2);
  line-height: 1.5;
}

.error-hint-steps {
  font-size: 12px;
  color: var(--au-text-4);
  line-height: 1.5;
  margin: 2px 0 0;
  padding-left: 18px;
}

.muted { color: var(--au-text-4); }

@media (max-width: 768px) {
  .list-bar { padding: 4px 16px 10px; }
  .quick-filters { padding: 0 16px 12px; }
  .w-limit { flex: 1 1 150px; width: auto; }
}
</style>
