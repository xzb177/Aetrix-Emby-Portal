<script setup lang="ts">
/**
 * 设备风控（v2.6.0）
 *
 * 用户第三方客户端（Infuse / Forward / SenPlayer 等）登录即登记设备。
 * 这里做跨用户审查：查设备、封禁（同时吊销令牌）与移除（踢下线）。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { RefreshCw, Ban, CircleCheck, LogOut, Smartphone, Activity, Gauge } from 'lucide-vue-next'
import { PageHeader, SectionCard, StatTile } from '@/components/ui'
import { fetchDeviceStats, fetchDevices, removeDevice, setDeviceBlocked } from '@/api/admin'
import { parseServerTime } from '@/utils/serverTime'
import type { DeviceRow, DeviceStats } from '@/types'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import FilterBar from '@/components/FilterBar.vue'
import type { AppliedFilter } from '@/components/FilterBar.vue'

/** 手机卡片：用户为标题，设备/客户端/IP/首次/最近活跃做键值行 */
const columns: DataColumn[] = [
  { key: 'username', label: '用户', width: 150, mobile: 'title' },
  { key: 'name', label: '设备', minWidth: 170 },
  { key: 'client', label: '客户端', width: 150 },
  { key: 'ip', label: 'IP', width: 130 },
  { key: 'first_seen_at', label: '首次', width: 150, mobile: 'hide' },
  { key: 'last_seen_at', label: '最近活跃', width: 130 },
  { key: 'is_blocked', label: '状态', width: 90 },
  // 封禁 / 移除收进详情弹窗：行里只留一个入口，也顺便把设备 ID / 版本 / 首末次时间看全
  { key: 'actions', label: '操作', width: 100, fixed: 'right', align: 'right' },
]

const devices = ref<DeviceRow[]>([])
const stats = ref<DeviceStats | null>(null)
const loading = ref(false)
const loadError = ref('')
const total = ref(0)
const draft = ref({ keyword: '', only_blocked: false })
const committed = ref({ keyword: '', only_blocked: false })

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    // 统计读不到只隐藏顶部瓦片，不连累设备清单
    const [list, stat] = await Promise.all([
      fetchDevices({
        keyword: committed.value.keyword || undefined,
        only_blocked: committed.value.only_blocked || undefined,
        limit: 300,
      }),
      fetchDeviceStats().catch(() => null),
    ])
    devices.value = list.devices
    total.value = list.total
    if (stat) stats.value = stat
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

function commitQuery() {
  const next = {
    keyword: draft.value.keyword.trim(),
    only_blocked: draft.value.only_blocked,
  }

  // 先标准化再比对，并在请求前保存快照：弱网下连点查询也不会重复请求。
  if (
    next.keyword === committed.value.keyword &&
    next.only_blocked === committed.value.only_blocked
  ) {
    return
  }

  committed.value = next
  load()
}

/** v2.59 空状态"清除筛选"：与 FilterBar 的清除逻辑一致 */
function clearFilters() {
  draft.value.keyword = ''
  draft.value.only_blocked = false
  commitQuery()
}

const committedHasFilter = computed(() =>
  Boolean(committed.value.keyword || committed.value.only_blocked),
)

// 标签只描述已提交的筛选，不拿草稿冒充当前结果，也不触发请求。
const appliedFilters = computed<AppliedFilter[]>(() => {
  const items: AppliedFilter[] = []
  if (committed.value.keyword) {
    items.push({ key: 'keyword', label: '关键词', text: committed.value.keyword })
  }
  if (committed.value.only_blocked) {
    items.push({ key: 'only_blocked', label: '', text: '只看已封禁' })
  }
  return items
})

// ==================== 设备详情弹窗（v2.29.0） ====================
// 以前封禁 / 移除是行里两个按钮，点下去只有一句确认框，看不到设备 ID、客户端版本、
// 首末次时间；现在一行一个「详情」按钮，处置动作也在弹窗里完成（理由说明就在按钮旁）。
const detail = ref({ visible: false, row: null as DeviceRow | null })

function openDetail(row: DeviceRow) {
  detail.value = { visible: true, row }
}

/** 行内处置动作（封禁 / 踢下线）进行中：用 userId:deviceId 做 key */
const rowBusyKey = ref<string | null>(null)

/** 动作做完刷新列表，并把弹窗里的行换成最新快照（状态徽标就地变化） */
async function afterAction(row: DeviceRow) {
  await load()
  const fresh = devices.value.find(
    (d) => d.device_id === row.device_id && d.user_id === row.user_id,
  )
  if (fresh) {
    detail.value.row = fresh
  } else {
    // 被移除了：弹窗没有可描述的对象，关掉并说明
    detail.value.visible = false
    ElMessage.info('设备已移除，客户端需重新登录')
  }
}

async function toggleBlock(row: DeviceRow) {
  const action = row.is_blocked ? '解封' : '封禁'
  try {
    await ElMessageBox.confirm(
      row.is_blocked
        ? `确认解封 ${row.username} 的设备「${row.name || row.device_id}」？`
        : `封禁后该设备已签发的令牌会被吊销，且无法再次登录。确认封禁 ${row.username} 的设备「${row.name || row.device_id}」？`,
      `${action}设备`,
      { type: 'warning' },
    )
  } catch {
    return
  }
  const key = `${row.user_id}:${row.device_id}`
  rowBusyKey.value = key
  try {
    const res = await setDeviceBlocked(row.user_id, row.device_id, !row.is_blocked)
    ElMessage.success(res.message)
    await afterAction(row)
  } catch {
    // 拦截器已提示
  } finally {
    rowBusyKey.value = null
  }
}

async function kick(row: DeviceRow) {
  try {
    await ElMessageBox.confirm(
      `移除后该设备令牌立即失效，客户端需重新登录。确认移除 ${row.username} 的设备？`,
      '移除设备',
      { type: 'warning' },
    )
  } catch {
    return
  }
  const key = `${row.user_id}:${row.device_id}`
  rowBusyKey.value = key
  try {
    const res = await removeDevice(row.user_id, row.device_id)
    ElMessage.success(res.message)
    await afterAction(row)
  } catch {
    // 拦截器已提示
  } finally {
    rowBusyKey.value = null
  }
}

function fmt(s: string | null): string {
  if (!s) return '—'
  return s.slice(0, 16).replace('T', ' ')
}

function ago(s: string | null): string {
  if (!s) return '—'
  const diff = Date.now() - parseServerTime(s).getTime()
  if (diff < 0) return '刚刚'
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="用户"
      title="设备与安全"
      description="第三方客户端（Infuse / Forward / SenPlayer 等）登录即登记设备。封禁会同时吊销令牌，移除等于踢下线。"
    >
      <template #actions>
        <el-button :loading="loading" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <div v-if="stats" class="stat-row">
      <StatTile label="设备总数" :value="stats.total" :icon="Smartphone" :hint="`来自 ${stats.users} 位用户`" />
      <StatTile
        :label="`近 ${stats.active_days} 天活跃`"
        :value="stats.active_30d"
        :icon="Activity"
        hint="长期未活跃的设备不计入上限"
      />
      <StatTile
        label="已封禁"
        :value="stats.blocked"
        :icon="Ban"
        :tone="stats.blocked > 0 ? 'danger' : 'plain'"
        hint="封禁设备无法再次登录"
      />
      <StatTile
        label="每用户上限"
        :value="stats.limit_per_user || '不限'"
        :icon="Gauge"
        :hint="stats.auto_evict ? '超限自动踢最久未用' : '超限直接拒绝新设备'"
      />
    </div>
    <div v-else-if="loading" class="stat-row" aria-hidden="true">
      <span v-for="n in 4" :key="n" class="au-skeleton stat-skeleton" />
    </div>

    <SectionCard title="设备清单" :icon="Smartphone" :meta="loading ? '' : `共 ${total} 台`" flush>
      <div class="list-bar">
        <FilterBar
          class="fb-grow"
          :search="{
            value: draft.keyword,
            placeholder: '搜索用户名 / 设备 / 客户端 / IP',
            mode: 'submit',
          }"
          :filters="[
            { key: 'only_blocked', label: '只看已封禁', type: 'checkbox', value: draft.only_blocked },
          ]"
          :applied-filters="appliedFilters"
          :loading="loading"
          @update:search="draft.keyword = $event"
          @search-submit="commitQuery"
          @query="commitQuery"
          @filter-change="(_key, v) => { draft.only_blocked = v; commitQuery() }"
          @remove-chip="(key) => {
            if (key === 'keyword') draft.keyword = '';
            if (key === 'only_blocked') draft.only_blocked = false;
            commitQuery();
          }"
          @clear="clearFilters"
        />
      </div>

      <DataTable
        :rows="devices"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        empty="暂无设备记录"
        empty-description="用户用第三方客户端登录后，设备会登记在这里。"
        :has-active-filters="committedHasFilter"
        @retry="load"
        @clear-filters="clearFilters"
      >
        <template #cell-username="{ row }">
          <span class="user-name">{{ row.username }}</span>
          <span v-if="!row.is_user_active" class="au-badge au-badge-rose badge-gap">已禁用</span>
        </template>

        <template #cell-name="{ row }">
          <div class="dev-name">{{ row.name || '未命名设备' }}</div>
          <div class="dev-id">{{ row.device_id }}</div>
        </template>

        <template #cell-client="{ row }">
          <div>{{ row.client || '—' }}</div>
          <div class="dev-id">v{{ row.app_version || '-' }}</div>
        </template>

        <template #cell-ip="{ row }">
          <span class="mono">{{ row.ip || '—' }}</span>
        </template>

        <template #cell-first_seen_at="{ row }"><span class="mono">{{ fmt(row.first_seen_at) }}</span></template>

        <template #cell-last_seen_at="{ row }">
          <span :class="{ muted: !row.is_online_recent }">{{ ago(row.last_seen_at) }}</span>
        </template>

        <template #cell-is_blocked="{ row }">
          <span class="au-badge" :class="row.is_blocked ? 'au-badge-rose' : 'au-badge-green'">
            {{ row.is_blocked ? '已封禁' : '正常' }}
          </span>
        </template>

        <template #cell-actions="{ row }">
          <!-- 一个入口：详情 + 封禁 / 解封 / 移除 都在弹窗里 -->
          <el-button size="small" @click="openDetail(row)">详情</el-button>
        </template>
      </DataTable>
    </SectionCard>

    <!-- 设备详情与处置（弹窗）：先把这台设备是什么说清楚，再动手 -->
    <el-dialog v-model="detail.visible" title="设备详情与处置" width="min(540px, 92vw)">
      <div v-if="detail.row" class="dev-detail">
        <div class="kv-list">
          <div class="kv-row"><span class="kv-key">所属用户</span>
            <span class="kv-value">
              {{ detail.row.username }}
              <span v-if="!detail.row.is_user_active" class="au-badge au-badge-rose badge-gap">账号已禁用</span>
            </span>
          </div>
          <div class="kv-row"><span class="kv-key">设备</span>
            <span class="kv-value">{{ detail.row.name || '未命名设备' }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">设备 ID</span>
            <span class="kv-value mono">{{ detail.row.device_id }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">客户端</span>
            <span class="kv-value">{{ detail.row.client || '—' }}<span class="dev-ver">v{{ detail.row.app_version || '-' }}</span></span>
          </div>
          <div class="kv-row"><span class="kv-key">IP</span>
            <span class="kv-value mono">{{ detail.row.ip || '—' }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">首次出现</span><span class="kv-value">{{ fmt(detail.row.first_seen_at) }}</span></div>
          <div class="kv-row"><span class="kv-key">最近活跃</span>
            <span class="kv-value">
              {{ fmt(detail.row.last_seen_at) }}
              <span class="dev-ago">{{ ago(detail.row.last_seen_at) }}</span>
            </span>
          </div>
          <div class="kv-row"><span class="kv-key">当前状态</span>
            <span class="kv-value">
              <span class="au-badge" :class="detail.row.is_blocked ? 'au-badge-rose' : 'au-badge-green'">
                {{ detail.row.is_blocked ? '已封禁' : '正常' }}
              </span>
            </span>
          </div>
        </div>

        <p class="dev-actions-hint">
          封禁会同时吊销该设备已签发的令牌，且它无法再次登录；移除等于踢下线，
          客户端需重新登录（两者都不影响用户的会员与订阅）。
        </p>
      </div>

      <template #footer>
        <div class="dev-footer">
          <el-button
            type="warning"
            :icon="LogOut"
            :loading="!!detail.row && rowBusyKey === `${detail.row.user_id}:${detail.row.device_id}`"
            @click="detail.row && kick(detail.row)"
          >
            移除（踢下线）
          </el-button>
          <div class="dev-footer-right">
            <el-button @click="detail.visible = false">关闭</el-button>
            <el-button
              v-if="detail.row"
              :type="detail.row.is_blocked ? 'success' : 'danger'"
              :icon="detail.row.is_blocked ? CircleCheck : Ban"
              :loading="!!detail.row && rowBusyKey === `${detail.row.user_id}:${detail.row.device_id}`"
              @click="detail.row && toggleBlock(detail.row)"
            >
              {{ detail.row.is_blocked ? '解封这台设备' : '封禁这台设备' }}
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
.stat-skeleton { display: block; height: 96px; border-radius: var(--au-r-lg); }

.list-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 12px;
  flex-wrap: wrap;
  padding: 4px 20px 12px;
}
.list-bar .fb-grow { flex: 1 1 auto; }
.list-bar .head-actions { justify-content: flex-end; }

.user-name { font-weight: 600; color: var(--au-text); }
.badge-gap { margin-left: 6px; }
.dev-name { font-size: 13px; color: var(--au-text); }
.dev-id {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--au-text-3);
  word-break: break-all;
}
.muted { color: var(--au-text-4); }

/* 设备详情弹窗：详情用全局 .kv-list，只补两处间距 */
.dev-detail { display: flex; flex-direction: column; gap: 12px; }
/* 弹窗里的键值行靠左：值与值之间会很长（设备 ID / 提示文案），右对齐读不动 */
.dev-detail .kv-row .kv-value { text-align: left; }
.dev-ver,
.dev-ago { margin-left: 6px; font-size: 12px; color: var(--au-text-3); }
.dev-actions-hint {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--au-text-2);
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  padding: 10px 12px;
}
.dev-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.dev-footer-right { display: flex; gap: 8px; }

@media (max-width: 768px) {
  .list-bar { padding: 4px 16px 12px; }
}

@media (max-width: 640px) {
  .stat-row { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .dev-footer { flex-direction: column-reverse; align-items: stretch; }
  .dev-footer-right :deep(.el-button) { flex: 1; margin-left: 0; }
}
</style>
