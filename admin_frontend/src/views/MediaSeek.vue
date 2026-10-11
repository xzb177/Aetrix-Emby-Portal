<script setup lang="ts">
/** 求片管理：审核批准/拒绝/标记完成，联动用户通知 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, CloudDownload, Download, MessageSquareDashed, RefreshCw, X } from 'lucide-vue-next'
import { PageHeader, SectionCard } from '@/components/ui'
import {
  fetchMediaSeeks, fetchServersSummary, getMediaSeekUserQuota, markMediaSeekInLibrary, pushMediaSeek, updateMediaSeek,
} from '@/api/admin'
import type { MediaSeekMonthlyQuota } from '@/api/admin'
import type { MediaSeekRow, ServerKind } from '@/types'
import { useQueryFilter } from '@/composables/useQueryFilter'
import { useRealmStore } from '@/stores/realm'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import FilterBar from '@/components/FilterBar.vue'
import type { AppliedFilter, FilterConfig } from '@/components/FilterBar.vue'

const realm = useRealmStore()
/** 统计范围：当前服（默认）或全部服 */
const scope = ref<'realm' | 'all'>('realm')

/** 手机卡片只留片名 / 类型 / 用户 / 状态 / 时间，管理备注在桌面表格里看 */
const columns = computed<DataColumn[]>(() => [
  { key: 'movie_name', label: '片名', minWidth: 200, mobile: 'title' },
  { key: 'type', label: '类型', width: 80 },
  { key: 'user_name', label: '用户', width: 110 },
  // 求片是「给哪个服求的」：跨服汇总时才需要这一列
  ...(scope.value === 'all'
    ? [{ key: 'realm_name', label: '求给', minWidth: 110 } as DataColumn]
    : []),
  { key: 'status', label: '状态', width: 100 },
  // v2 附议数：热度排序时一眼看出哪部片呼声最高
  { key: 'vote_count', label: '附议', width: 80, align: 'center' },
  { key: 'push', label: '转交外部服务', width: 170 },
  { key: 'admin_note', label: '管理备注', minWidth: 140, mobile: 'hide' },
  { key: 'created_at', label: '提交时间', width: 150 },
  // 所有动作都收进「处理」弹窗：行里只留一个入口，不再挤成一排按钮
  { key: 'actions', label: '操作', width: 110, fixed: 'right', align: 'right' },
])

const list = ref<MediaSeekRow[]>([])
const loading = ref(false)
const loadError = ref('')
/** 下拉框草稿仍用同一个 ref，深链才能直接写入并展示 URL 中的状态 */
const statusFilter = ref('')
const committedStatus = ref('')

const filters = computed<FilterConfig[]>(() => [
  {
    key: 'status',
    label: '状态',
    type: 'select',
    value: statusFilter.value,
    placeholder: '全部状态',
    options: [
      { label: '全部状态', value: '' },
      { label: '待审核', value: 'pending' },
      { label: '已批准', value: 'approved' },
      { label: '已入库', value: 'completed' },
      { label: '已拒绝', value: 'rejected' },
      { label: '已撤回', value: 'withdrawn' },
    ],
  },
])

/** chips 只展示已提交快照，不监听展示变化发请求，弱网下也不会多打一轮 */
const appliedFilters = computed<AppliedFilter[]>(() => committedStatus.value
  ? [{ key: 'status', label: '状态', text: statusLabel(committedStatus.value) }]
  : [],
)

// v2 排序：latest=按提交时间，hot=按附议数
const orderBy = ref<'latest' | 'hot'>('latest')
// 深链：仪表盘「待审求片」/ 命令面板跳过来时带的就是这个筛选（Phase 5）
// URL 是外部权威入口：watcher 比较的是上次 URL，不是提交快照，不能被草稿碰巧相等挡掉。
// 原来 changed 后 load 一次；现在先同步快照再 load 一次，不额外发请求。
useQueryFilter(statusFilter, 'status', () => {
  committedStatus.value = statusFilter.value
  load()
})
const busyId = ref<number | null>(null)
/** 哪几类外部服务已经接好（用于决定显示哪些推送按钮） */
const pushReady = ref<ServerKind[]>([])
/** 可转交目标查过一次之后才显示「还没有可接收求片的服务」，免得进页时先闪一下 */
const pushChecked = ref(false)

const canMoviePilot = computed(() => pushReady.value.includes('moviepilot'))
const canQbittorrent = computed(() => pushReady.value.includes('qbittorrent'))
/** 一个能推的都没有：直接把入口指去「服务器」页，而不是发一个点了也不动的按钮 */
const noPushTarget = computed(() => !canMoviePilot.value && !canQbittorrent.value)

/**
 * 下拉值无需 trim；清空统一为 ''，再与提交快照做纯内存比较，不加防抖或等待。
 * 原来 change 只在值变化时 load：正常改动仍请求一次，重复事件和无变化查询直接跳过，只减不增。
 * radio 仍直接 load，读取已提交状态，排序 / 范围切换的请求次数不变。
 */
function commitQuery(force = false) {
  const nextStatus = statusFilter.value
  if (!force && nextStatus === committedStatus.value) return
  committedStatus.value = nextStatus
  return load()
}

function onFilterChange(key: string, value: any) {
  if (key !== 'status') return
  statusFilter.value = value ?? ''
  commitQuery()
}

/** 本页只有状态筛选，移除 chip 和清除全部共用一次提交，已清空时不重复请求 */
function clearStatus() {
  statusFilter.value = ''
  commitQuery()
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const params: { status_filter?: string; realm_id?: number; order?: string } = {}
    if (committedStatus.value) params.status_filter = committedStatus.value
    // v2 热度排序
    if (orderBy.value === 'hot') params.order = 'hot'
    // 求片登记的是「给哪个服求」；realm_id=0 = 全部服（后端未标注的也算进来）
    params.realm_id = scope.value === 'all' ? 0 : (realm.activeId ?? 0)
    list.value = await fetchMediaSeeks(params)
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 可转交的外部服务：服务器没接好时按钮点了也只会失败，所以如实反映当前可用目标。
 * 只在进页时拉一次——以前每次切筛选 / 排序 / 范围都跟着列表重拉一遍，而且它失败会把
 * 整张求片列表打成错误态（列表本身其实拉到了）。
 */
async function loadPushTargets() {
  try {
    const summary = await fetchServersSummary()
    pushReady.value = summary.push_ready || []
  } catch {
    pushReady.value = []
  } finally {
    pushChecked.value = true
  }
}

// ==================== 处理弹窗（v2.29.0） ====================
// 以前一行里摊着 5 个按钮（批准 / 拒绝 / 交 MoviePilot / 交给 qB / 标记上架），
// 拒绝与批准还要再弹一次输入框——既挤又看不全上下文（用户备注、转交结果都得切页找）。
// 现在行里只有一个「处理」按钮，弹窗里把求片详情 + 处理备注 + 全部动作放在一处。
const handle = ref({
  visible: false,
  row: null as MediaSeekRow | null,
  /** 处理备注（批准 / 拒绝时写库，拒绝会作为原因展示给用户） */
  note: '',
  /** qBittorrent 需要的磁力 / 种子地址（qB 自己不会找片子） */
  link: '',
})

/** 这一行还有没有可做的动作（已入库 / 已拒绝 / 已撤回只是查看） */
function isActionable(r: MediaSeekRow): boolean {
  return r.status === 'pending' || r.status === 'approved'
}

/** 处理弹窗里展示的提交用户本月额度（公益/付费区分） */
const handleQuota = ref<MediaSeekMonthlyQuota | null>(null)

function openHandle(r: MediaSeekRow) {
  handle.value = { visible: true, row: r, note: r.admin_note || '', link: '' }
  // 拉取该用户本月求片额度：审核时一眼看到还剩几次
  handleQuota.value = null
  getMediaSeekUserQuota(r.user_id).then(
    (q) => { handleQuota.value = q },
    () => { /* 拿不到就不展示，不挡审核 */ },
  )
}

/** 动作做完刷新列表，并把弹窗里的行换成最新快照（转交结果、状态就地可见） */
async function afterAction(id: number) {
  await load()
  const fresh = list.value.find((x) => x.id === id)
  if (fresh) {
    handle.value.row = fresh
  } else {
    // 被当前状态筛选挡掉了（例如筛「待审核」时批准了）：如实说明，别把旧快照留在弹窗里
    handle.value.visible = false
    ElMessage.info('已处理，该求片不再符合当前筛选条件')
  }
}

/** 批准 / 拒绝（拒绝必须写理由：用户收到的通知就是这条说明） */
async function reviewStatus(status: string) {
  const r = handle.value.row
  if (!r) return
  const note = handle.value.note.trim()
  if (status === 'rejected' && !note) {
    ElMessage.warning('拒绝求片要先在「处理备注」里填写理由')
    return
  }
  busyId.value = r.id
  try {
    await updateMediaSeek(r.id, { status, admin_note: note || undefined })
    ElMessage.success({ approved: '已批准', rejected: '已拒绝' }[status] || '已更新')
    await afterAction(r.id)
  } catch {
    /* 拦截器已提示 */
  } finally {
    busyId.value = null
  }
}

/**
 * 入库后标记已入库：后端先核验片真的进了媒体库（按 tmdb_id / 片名匹配）。
 * 匹配不到 → 409；管理员确认后可以强制标记（片改了名入库的场景）。
 * 两次调用都走 silent，错误提示由这里控制，避免先弹报错再弹确认框。
 */
async function markInLibrary() {
  const r = handle.value.row
  if (!r) return
  busyId.value = r.id
  try {
    try {
      const res = await markMediaSeekInLibrary(r.id)
      ElMessage.success(res.message || '已标记入库')
    } catch (err: any) {
      const status = err?.response?.status
      const detail = err?.response?.data?.detail
      if (status !== 409) throw err
      // 409：媒体库里没找到——让管理员确认后再强制标记
      try {
        await ElMessageBox.confirm(
          typeof detail === 'string' ? detail : '媒体库里没找到这部片',
          '强制标记已入库？',
          { type: 'warning', confirmButtonText: '强制标记', cancelButtonText: '再等等' },
        )
      } catch {
        return
      }
      const forced = await markMediaSeekInLibrary(r.id, true)
      ElMessage.warning(forced.message || '已强制标记入库')
    }
    await afterAction(r.id)
  } catch (err: any) {
    const detail = err?.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : '标记失败，请稍后重试')
  } finally {
    busyId.value = null
  }
}

/** 转交外部服务：MoviePilot（提交订阅）或 qBittorrent（加种，必须带链接） */
async function pushTo(target: 'moviepilot' | 'qbittorrent') {
  const r = handle.value.row
  if (!r) return
  const link = handle.value.link.trim()
  if (target === 'qbittorrent' && !link) {
    ElMessage.warning('交给 qBittorrent 需要先填磁力 / 种子地址')
    return
  }
  busyId.value = r.id
  try {
    const res = await pushMediaSeek(r.id, target === 'qbittorrent' ? { target, link } : { target })
    res.success
      ? ElMessage.success(res.message || `已提交给 ${pushLabel(target)}`)
      : ElMessage.warning(res.message)
    handle.value.link = ''
    await afterAction(r.id)
  } catch {
    /* 拦截器已提示 */
  } finally {
    busyId.value = null
  }
}

function pushLabel(target: string | null): string {
  if (target === 'moviepilot') return 'MoviePilot'
  if (target === 'qbittorrent') return 'qBittorrent'
  return target || '—'
}

onMounted(() => {
  // immediate watch 已写入 URL 状态：先同步快照，首次仍只 load 一次，同步本身不发请求。
  committedStatus.value = statusFilter.value
  load()
  loadPushTargets()
})

function fmtDate(s: string): string {
  return s.slice(0, 16).replace('T', ' ')
}

function statusBadge(status: string): string {
  const map: Record<string, string> = {
    pending: 'au-badge-amber', approved: 'au-badge-info', completed: 'au-badge-green',
    rejected: 'au-badge-rose', withdrawn: 'au-badge-muted',
  }
  return map[status] || 'au-badge-muted'
}

function typeLabel(type: string | null): string {
  if (type === 'movie') return '电影'
  if (type === 'tv' || type === 'series') return '剧集'
  if (type === 'anime') return '动漫'
  return type || '—'
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    pending: '待审核', approved: '已批准', completed: '已入库', rejected: '已拒绝',
    // 用户自己撤掉的：默认不进待办清单，只能靠状态筛选查（下面有这一项）
    withdrawn: '已撤回',
  }
  return map[status] || status
}
</script>

<template>
  <div class="admin-page">
    <PageHeader eyebrow="内容与服务" title="求片管理" description="批准 / 拒绝、转交外部下载服务、标记已入库都在「处理」弹窗里；审核结果会通知提交用户。">
      <template #actions>
        <el-button :loading="loading" @click="load" :icon="RefreshCw">刷新</el-button>
      </template>
    </PageHeader>

    <!-- 没接好 MoviePilot / qB 时，求片批了也没法真的把片子弄进来：如实说明并给出入口 -->
    <el-alert v-if="pushChecked && noPushTarget && !loading && !loadError" type="warning" :closable="false" show-icon class="push-guide">
      <template #default>
        还没有可以接收求片的服务：请在<RouterLink to="/servers" class="inline-link">「服务器与线路」</RouterLink>页添加
        <b>MoviePilot</b>（搜片下载与整理）或 <b>qBittorrent</b>（下载器），测试连接通过后这里就会出现转交按钮。
      </template>
    </el-alert>

    <SectionCard title="求片列表" :icon="MessageSquareDashed" :meta="loading ? '' : `${list.length} 条`" flush>
      <div class="list-bar">
        <FilterBar
          class="fb-grow"
          :filters="filters"
          :applied-filters="appliedFilters"
          :query-button="true"
          :loading="loading"
          @filter-change="onFilterChange"
          @query="commitQuery"
          @remove-chip="clearStatus"
          @clear="clearStatus"
        />
        <div class="head-actions">
          <!-- v2 排序：最新 / 热度（附议数） -->
          <el-radio-group v-model="orderBy" aria-label="排序" @change="load">
            <el-radio-button value="latest">最新</el-radio-button>
            <el-radio-button value="hot">热度</el-radio-button>
          </el-radio-group>
          <el-radio-group v-model="scope" aria-label="统计范围" @change="load">
            <el-radio-button value="realm">当前服</el-radio-button>
            <el-radio-button value="all">全部服</el-radio-button>
          </el-radio-group>
        </div>
      </div>

      <DataTable
        :rows="list"
        :columns="columns"
        :loading="loading"
        :error="loadError"
        :empty="committedStatus ? `没有「${statusLabel(committedStatus)}」的求片` : '暂无求片记录'"
        :empty-description="committedStatus ? '清空状态筛选看看全部。' : scope === 'realm' ? '当前服还没人求片；可切到「全部服」看看。' : '用户提交的求片会出现在这里。'"
        @retry="load"
      >
        <template #cell-movie_name="{ row }">
          <span class="movie-name">《{{ row.movie_name }}》</span>
          <span v-if="row.year" class="movie-year">{{ row.year }}</span>
          <span v-if="row.season_label" class="au-badge au-badge-muted movie-season">{{ row.season_label }}</span>
          <div v-if="row.note" class="movie-note">用户备注：{{ row.note }}</div>
        </template>

        <template #cell-type="{ row }">{{ typeLabel(row.type) }}</template>

        <template #cell-user_name="{ row }">{{ row.user_name }}</template>

        <template #cell-realm_name="{ row }">
          <span class="au-badge au-badge-muted">{{ row.realm_name || '未标注' }}</span>
        </template>

        <template #cell-status="{ row }">
          <span class="au-badge" :class="statusBadge(row.status)">{{ statusLabel(row.status) }}</span>
        </template>

        <!-- v2 附议数 -->
        <template #cell-vote_count="{ row }">
          <span class="vote-count" :class="{ 'vote-hot': (row.vote_count || 0) > 0 }">👍 {{ row.vote_count || 0 }}</span>
        </template>

        <template #cell-admin_note="{ row }">
          <span v-if="!row.admin_note" class="muted">—</span>
          <span v-else>{{ row.admin_note }}</span>
        </template>

        <template #cell-push="{ row }">
          <div v-if="!row.push_target" class="muted">未转交</div>
          <div v-else class="push-cell">
            <span class="au-badge" :class="row.push_status === 'ok' ? 'au-badge-green' : 'au-badge-rose'">
              {{ pushLabel(row.push_target) }}{{ row.push_status === 'ok' ? ' 已提交' : ' 失败' }}
            </span>
            <el-tooltip v-if="row.push_message" :content="row.push_message" placement="top">
              <span class="muted push-msg">{{ row.push_message }}</span>
            </el-tooltip>
          </div>
        </template>

        <template #cell-created_at="{ row }"><span class="mono">{{ fmtDate(row.created_at) }}</span></template>

        <template #cell-actions="{ row }">
          <!-- 一个入口：详情 + 批准 / 拒绝 / 转交 / 标记已入库 都在弹窗里（原来这行有 5 个按钮） -->
          <el-button
            size="small"
            :type="isActionable(row) ? 'primary' : 'default'"
            @click="openHandle(row)"
          >
            {{ isActionable(row) ? '处理' : '查看' }}
          </el-button>
        </template>
      </DataTable>
    </SectionCard>

    <!--
      处理弹窗（v2.29.0）：求片详情 + 处理备注 + 全部动作都在这一处。
      动作做完弹窗不关——就地换成最新快照，转交结果 / 新状态直接可见，
      而且换行的动作（批准 → 标记已入库）不用再重新找到那一行。
    -->
    <el-dialog
      v-model="handle.visible"
      :title="handle.row ? `处理求片《${handle.row.movie_name}》` : '处理求片'"
      width="min(560px, 92vw)"
    >
      <div v-if="handle.row" class="handle-body">
        <div class="kv-list">
          <div class="kv-row"><span class="kv-key">片名</span><span class="kv-value">《{{ handle.row.movie_name }}》</span></div>
          <div class="kv-row"><span class="kv-key">年份 / 类型</span>
            <span class="kv-value">{{ handle.row.year || '—' }} · {{ typeLabel(handle.row.type) }}</span>
          </div>
          <div v-if="handle.row.season_label" class="kv-row"><span class="kv-key">申请范围</span>
            <span class="kv-value">{{ handle.row.season_label }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">提交用户</span><span class="kv-value">{{ handle.row.user_name }}</span></div>
          <div v-if="handleQuota" class="kv-row"><span class="kv-key">本月额度</span>
            <span class="kv-value">已用 {{ handleQuota.monthly_used }} / {{ handleQuota.monthly_limit }}（{{ handleQuota.kind === 'welfare' ? '公益服' : '付费' }}），剩余 {{ handleQuota.monthly_remaining }} 次</span>
          </div>
          <div class="kv-row"><span class="kv-key">求给</span>
            <span class="kv-value">{{ handle.row.realm_name || '未标注' }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">提交时间</span><span class="kv-value">{{ fmtDate(handle.row.created_at) }}</span></div>
          <div class="kv-row"><span class="kv-key">当前状态</span>
            <span class="kv-value">
              <span class="au-badge" :class="statusBadge(handle.row.status)">{{ statusLabel(handle.row.status) }}</span>
            </span>
          </div>
          <div class="kv-row"><span class="kv-key">用户备注</span>
            <span class="kv-value">{{ handle.row.note || '—' }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">转交外部服务</span>
            <span class="kv-value">
              <template v-if="!handle.row.push_target">未转交</template>
              <template v-else>
                {{ pushLabel(handle.row.push_target) }}{{ handle.row.push_status === 'ok' ? ' 已提交' : ' 失败' }}
                <span v-if="handle.row.push_message" class="push-msg-line">{{ handle.row.push_message }}</span>
              </template>
            </span>
          </div>
        </div>

        <el-form label-position="top" class="handle-form">
          <el-form-item label="处理备注">
            <el-input
              v-model="handle.note"
              type="textarea"
              :rows="2"
              placeholder="如：预计本周内上架 / 已有同类型资源…"
            />
            <p class="form-hint">拒绝时必填，作为理由展示给提交用户；批准时作为进度说明（可不填）。</p>
          </el-form-item>

          <!-- qBittorrent 自己不会找片子：要交给它就必须在这里把链接填上（原来是一个二次弹窗） -->
          <el-form-item v-if="canQbittorrent && isActionable(handle.row)" label="qBittorrent 下载地址">
            <el-input
              v-model="handle.link"
              type="textarea"
              :rows="2"
              placeholder="magnet:?xt=… 或 .torrent 的 http 地址（只在这条求片交给 qB 时使用）"
            />
          </el-form-item>
        </el-form>
      </div>

      <template #footer>
        <div class="handle-footer">
          <el-button @click="handle.visible = false">关闭</el-button>
          <div class="handle-actions">
            <el-button
              v-if="handle.row?.status === 'pending'"
              size="small"
              type="danger"
              :loading="busyId === handle.row.id"
              :icon="X"
              @click="reviewStatus('rejected')"
            >
              拒绝
            </el-button>
            <el-button
              v-if="handle.row?.status === 'pending'"
              size="small"
              type="primary"
              :loading="busyId === handle.row.id"
              :icon="Check"
              @click="reviewStatus('approved')"
            >
              批准
            </el-button>
            <!--
              标记已入库：走后端校验——先确认片真的在媒体库里（按 tmdb_id / 片名匹配），
              匹配不到会 409，管理员确认后再强制标记。原来只是把状态改成「已上架」，面板无从核实。
            -->
            <el-button
              v-if="handle.row?.status === 'approved' || handle.row?.status === 'pending'"
              size="small"
              :type="handle.row?.status === 'approved' ? 'primary' : 'default'"
              :loading="busyId === handle.row.id"
              @click="markInLibrary"
            >
              标记已入库
            </el-button>
            <el-button
              v-if="canMoviePilot && handle.row && isActionable(handle.row)"
              size="small"
              :loading="busyId === handle.row.id"
              :icon="CloudDownload"
              @click="pushTo('moviepilot')"
            >
              交 MoviePilot
            </el-button>
            <el-button
              v-if="canQbittorrent && handle.row && isActionable(handle.row)"
              size="small"
              :loading="busyId === handle.row.id"
              :icon="Download"
              @click="pushTo('qbittorrent')"
            >
              交给 qB
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.list-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px 12px;
  flex-wrap: wrap;
  padding: 4px 20px 12px;
}
.list-bar .fb-grow { flex: 1 1 auto; }
.list-bar .head-actions { width: auto; justify-content: flex-end; }

.movie-name { font-weight: 600; color: var(--au-text); }
.movie-year { font-size: 12px; color: var(--au-text-3); margin-left: 6px; }
.movie-season { margin-left: 6px; }
.movie-note { font-size: 12px; color: var(--au-text-3); margin-top: 3px; }
/* v2 附议数：有附议的高亮 */
.vote-count { font-size: 13px; color: var(--au-text-3); white-space: nowrap; }
.vote-count.vote-hot { color: var(--au-primary); font-weight: 600; }
.muted { color: var(--au-text-4); font-size: 12px; }

.push-guide { line-height: 1.7; }
.inline-link { color: var(--au-primary); text-decoration: underline; text-underline-offset: 2px; }
.push-cell { display: flex; flex-direction: column; gap: 3px; }
.push-cell .au-badge { align-self: flex-start; }
.push-msg { display: block; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }

/* 处理弹窗：详情用全局 .kv-list 原语，只补两处专有间距 */
.handle-body { display: flex; flex-direction: column; gap: 14px; }
/* 弹窗里的键值行靠左：值是片名 / 备注 / 报错这类长文本，右对齐读不动 */
.handle-body .kv-row .kv-value { text-align: left; }
.handle-form { margin-top: 2px; }
.handle-form :deep(.el-form-item) { margin-bottom: 12px; }
.push-msg-line { display: block; font-size: 12px; color: var(--au-text-3); margin-top: 2px; }
.handle-footer { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.handle-actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }

@media (max-width: 768px) {
  .list-bar { padding: 4px 16px 12px; }
}

@media (max-width: 640px) {
  .push-msg { max-width: 100%; }
  .handle-footer { flex-direction: column-reverse; align-items: stretch; }
  .handle-actions :deep(.el-button) { flex: 1 1 40%; margin-left: 0; }
}
</style>
