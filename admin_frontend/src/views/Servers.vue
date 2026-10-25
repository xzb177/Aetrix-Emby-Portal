<script setup lang="ts">
/**
 * 服务器 · Emby 总览 —— **Emby 相关功能的唯一入口页**
 *
 * 演进：以前「填一台 EA + 填一台已有 Emby」写在「Emby 服务入口」，「接了什么、有几台能用」
 * 在「服务器」，「这台 EA 碰不碰得到存储」在「存储挂载」，「库归谁扫」在「媒体库」——
 * 想知道「这台 EA 到底在不在服务这个服、库有没有分给它」要来回跳四个页面拼。
 *
 * v2.6.22 起这一页就是**唯一入口**：旧页「Emby 服务入口」已下线（旧地址跳到这里），
 * 页面本身由 `GET /api/admin/servers/overview` 驱动，按服给出每台入口的：
 *
 * 1. **连接体检**（面板视角的连通性与最近一次结论）；
 * 2. **节点认领**：`NODE_KEY` 有没有配上、EA 自己说它属于哪个服（`live` 时真去问）；
 * 3. **挂载体检**：这台 EA 视角下哪些存储碰不到（挂载是本机相对的，面板测通不代表它能播）；
 * 4. **库归属**：归它出流/扫描的媒体库数，以及同服里还没分配的库数；
 * 5. **告警**：两边不一致（EA 自称的服 ≠ 面板登记的服、NODE_KEY 对不上、挂载不可达、
 *    连接失败…）直接列出来，不用自己去比对。
 *
 * 增删改（添加 / 编辑 / 测试 / 设为当前 / 启停 / 删除）也都在这一页完成；
 * 内容自动化（MoviePilot / qB）与出流入口分开成两段，避免混在一起看不清谁在出流。
 */
import { computed, defineAsyncComponent, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import {
  AlertTriangle, CheckCircle2, ChevronDown, CloudDownload, Download, FolderOpen, HardDrive,
  Info, Library, Pencil, Plus, RefreshCw, Route as RealmIcon, Server, Trash2, Wifi,
} from 'lucide-vue-next'
import {
  activateServer,
  createServer,
  createMount,
  deleteMount,
  deleteServer,
  fetchServers,
  fetchServersOverview,
  fetchServerOps,
  fetchMounts,
  fetchServerRcloneConf,
  probeMountsHealth,
  refreshServerMounts,
  saveServerRcloneConf,
  testServer,
  testServerConfig,
  toggleServer,
  updateServer,
  testSavedMount,
  updateMount,
} from '@/api/admin'
import type {
  RemoteServerRow, ServerKind, ServerKindMeta, ServerOverview, ServerOverviewRow,
  ServerOpsSnapshot, ServerProbeResult, ServerSummary, StorageMount,
} from '@/types'
import MountSourceEditor from '@/components/MountSourceEditor.vue'
import MountBrowseDialog from '@/components/MountBrowseDialog.vue'
import type { MountTypeValue } from '@/components/MountSourceEditor.vue'
import { useRealmStore } from '@/stores/realm'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'
import NoticePanel from '@/components/NoticePanel.vue'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'

const realm = useRealmStore()
const router = useRouter()

/**
 * 重新布局批 5：115 / Google Drive 账号页降为本页「存储账号」标签
 * （沿用 Codes.vue 的 ?tab= 同步 + KeepAlive 模式；存储账号内再分两个子标签）。
 * 服务器面板用 v-show：来回切标签不卸载列表与弹窗状态；账号面板按需加载、
 * 进过一次后 KeepAlive 缓存。
 */
const Pan115Accounts = defineAsyncComponent(() => import('./Pan115Accounts.vue'))
const GDriveAccounts = defineAsyncComponent(() => import('./GDriveAccounts.vue'))

type ServerTab = 'servers' | 'storage'
type StorageSubTab = 'pan115' | 'gdrive'

const route = useRoute()

const activeTab = computed<ServerTab>({
  get: () => (route.query.tab === 'storage' ? 'storage' : 'servers'),
  set: (tab) => {
    if (tab === activeTab.value) return
    router.replace({ query: { ...route.query, tab } })
  },
})

const activeSubTab = computed<StorageSubTab>({
  get: () => (route.query.subtab === 'gdrive' ? 'gdrive' : 'pan115'),
  set: (subtab) => {
    if (subtab === activeSubTab.value) return
    router.replace({ query: { ...route.query, subtab } })
  },
})

/** 出流入口（一个服一个）与内容自动化（可全服共用）——分两段展示，别混在一起 */
const ENTRY_KINDS: ServerKind[] = ['ea', 'emby']
const CONTENT_KINDS: ServerKind[] = ['moviepilot', 'qbittorrent']

const KIND_ICONS: Record<string, unknown> = {
  ea: Server,
  emby: HardDrive,
  moviepilot: CloudDownload,
  qbittorrent: Download,
}

// ==================== 状态 ====================

const servers = ref<RemoteServerRow[]>([])
const kinds = ref<ServerKindMeta[]>([])
const summary = ref<ServerSummary | null>(null)
/** Emby 总览（按服分组的入口画像）：页面主体数据 */
const overview = ref<ServerOverview | null>(null)
const loading = ref(false)
/** 「一键体检」在跑：真去问每台 EA 的节点身份 + 重拉挂载体检 */
const liveRunning = ref(false)
/** 范围：默认看**全部服**（总览的意义就在这），也可以只看当前服 */
const scopeAll = ref(true)
const kindFilter = ref<ServerKind | ''>('')
const busyId = ref<number | null>(null)

const dialogVisible = ref(false)
const editingId = ref<number | null>(null)

/** rclone 配置并进「添加服务器」：**每台 EA 一份**，服务器级配一次就够了 */
const rcloneConf = ref('')
const rcloneRemotes = ref<string[]>([])
const rcloneConfPath = ref('')
const rcloneLoading = ref(false)
const rcloneSaving = ref(false)

async function loadRcloneConf(serverId: number) {
  rcloneLoading.value = true
  try {
    const res = await fetchServerRcloneConf(serverId)
    rcloneRemotes.value = res.remotes
    rcloneConfPath.value = res.path
    // **不回显原文**：rclone.conf 里全是 token，回显等于写进浏览器历史与前端日志。
    // 要改就重新粘一份覆盖（本来就是整份替换）。
    rcloneConf.value = ''
  } catch (e) {
    rcloneRemotes.value = []
    rcloneConfPath.value = ''
  } finally {
    rcloneLoading.value = false
  }
}

/** 挂载也并进服务器：挂到哪台 EA 上，就由它去读（每台 EA 一份 rclone.conf） */
const serverMounts = ref<StorageMount[]>([])
const mountsLoading = ref(false)
const mountForm = reactive({ name: '', type: 'local' as MountTypeValue, path: '' })
const editingMountId = ref<number | null>(null)

/** 目录浏览（从存储来源页搬过来的组件）与体检 */
const browseVisible = ref(false)
const browseTarget = ref<StorageMount | null>(null)
const checkingMounts = ref(false)

function openMountBrowse(m: StorageMount) {
  browseTarget.value = m
  browseVisible.value = true
}

/** 点目录时把它回写成挂载根（仅 115 有 cid 这类「根」的概念） */
async function onBrowsePick(rel: string) {
  const m = browseTarget.value
  if (!m || !rel || rel === '/') return
  if (!['115', 'rclone'].includes(m.mount_type)) return
  const prefix = m.mount_type === '115' ? '115:' : 'rclone:'
  const path = `${prefix}${rel}`
  await updateMount(m.id, { path })
  ElMessage.success(`挂载根已改为：${path}（请重新扫描对应媒体库）`)
  loadServerMounts()
}

async function runMountHealth() {
  checkingMounts.value = true
  try {
    const res = await probeMountsHealth()
    ElMessage.success(`本机体检完成：${res.ok_count}/${res.total} 条可达`)
    loadServerMounts()
  } finally {
    checkingMounts.value = false
  }
}

/** 页头「更多」下拉：服管理 / 存储来源 / 媒体库（原来是三颗并排按钮，行为不变） */
function onHeaderCommand(cmd: string) {
  if (cmd === 'realms') router.push({ name: 'Realms' })
  else if (cmd === 'emby') router.push({ name: 'EmbyAdmin' })
  else if (cmd === 'mounts') openMountsForServer()
}

/** 「存储来源」按钮：直接开这台服（本服已激活的 EA）的弹窗，里面就是它的挂载区 */
function openMountsForServer() {
  const row = servers.value.find((s) => s.kind === 'ea')
    && (editingId.value ? servers.value.find((s) => s.id === editingId.value)
                        : servers.value.find((s) => s.kind === 'ea'))
  if (!row) {
    ElMessage.warning('还没有添加 EA 服务器，先添加一台再配存储来源')
    openCreate('ea')
    return
  }
  openEdit(row)
}

function startEditMount(m: StorageMount) {
  editingMountId.value = m.id
  mountForm.name = m.name
  mountForm.type = (['local', '115', 'rclone', 'drive'].includes(m.mount_type)
    ? m.mount_type : 'local') as MountTypeValue
  mountForm.path = m.path
}

function cancelEditMount() {
  editingMountId.value = null
  mountForm.name = ''
  mountForm.path = ''
  mountForm.type = 'local'
}

async function loadServerMounts() {
  if (!editingId.value) return
  mountsLoading.value = true
  try {
    const res = await fetchMounts(0)
    serverMounts.value = (res.mounts || []).filter((m) => m.server_id === editingId.value)
  } finally {
    mountsLoading.value = false
  }
}

async function addMount() {
  if (!editingId.value) return
  const name = mountForm.name.trim()
  if (!name) {
    ElMessage.warning('请填写挂载名称')
    return
  }
  if (!mountForm.path.trim()) {
    ElMessage.warning('请填写路径')
    return
  }
  const row = servers.value.find((s) => s.id === editingId.value)
  try {
    if (editingMountId.value) {
      const res = await updateMount(editingMountId.value, {
        name, path: mountForm.path.trim(),
      })
      if (res.mount_type_changed) {
        ElMessage.warning('来源已切换（旧条目的读取方式随之改变），请重新扫描对应媒体库')
      } else {
        ElMessage.success('挂载已更新：重新扫描对应媒体库后生效')
      }
      cancelEditMount()
    } else {
      await createMount({
        name,
        path: mountForm.path.trim(),
        server_id: editingId.value,
        realm_id: row?.realm_id ?? undefined,
      })
      ElMessage.success('挂载已创建：到「媒体库」页把它绑定到库上即可扫描')
      mountForm.name = ''
      mountForm.path = ''
      mountForm.type = 'local'
    }
    loadServerMounts()
  } catch (e) {
    ElMessage.error(toAdminError(e, '请求失败，请稍后重试'))
  }
}

async function testOneMount(m: StorageMount) {
  const res = await testSavedMount(m.id)
  if (res.success) ElMessage.success(`${m.name}：${res.result.message}`)
  else ElMessage.error(`${m.name}：${res.result.message}`)
  loadServerMounts()
}

async function toggleMount(m: StorageMount) {
  await updateMount(m.id, { is_enabled: !m.is_enabled })
  ElMessage.success(m.is_enabled ? `「${m.name}」已停用（重扫后生效）` : `「${m.name}」已启用（重扫后生效）`)
  loadServerMounts()
}

async function removeMount(m: StorageMount) {
  try {
    await ElMessageBox.confirm(`挂载「${m.name}」删除后，引用它的媒体库会自动解绑（不再从这个挂载读文件）。`, '删除挂载', {
      type: 'warning',
      confirmButtonText: '删除这个挂载',
      cancelButtonText: '取消',
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return
  }
  await deleteMount(m.id)
  ElMessage.success('已删除')
  loadServerMounts()
}

async function saveRcloneConf() {
  if (!editingId.value) return
  if (!rcloneConf.value.trim()) {
    ElMessage.warning('先把 rclone config 生成的 INI 文本粘进来')
    return
  }
  rcloneSaving.value = true
  try {
    const res = await saveServerRcloneConf(editingId.value, rcloneConf.value)
    rcloneRemotes.value = res.remotes
    rcloneConf.value = ''
    ElMessage.success(`已保存：${res.total} 个 remote 可用（原文不再回显）`)
  } finally {
    rcloneSaving.value = false
  }
}
const saving = ref(false)
const testingUnsaved = ref(false)
const unsavedResult = ref<ServerProbeResult | null>(null)
const form = reactive({
  name: '',
  kind: 'ea' as ServerKind,
  url: '',
  remark: '',
  is_enabled: true,
  config: {} as Record<string, string>,
  /** 归属服：EA / Emby 一个服一个；MoviePilot / qB 可留空（全服共用） */
  realm_id: null as number | null,
  shared: false,
})

// ==================== 派生 ====================

const scopeParams = computed(() => (scopeAll.value ? { realm_id: 0 } : {}))
const isSharedKind = computed(() => form.kind === 'moviepilot' || form.kind === 'qbittorrent')
const currentKind = computed(() => kinds.value.find((k) => k.value === form.kind))
const kindCards = computed(() =>
  kinds.value.map((k) => ({ meta: k, stat: (overview.value?.summary ?? summary.value)?.kinds?.[k.value] }))
)
const totals = computed(() => overview.value?.totals ?? null)

/** 当前筛选下要看哪几类 */
const filterKinds = computed<ServerKind[]>(() =>
  kindFilter.value
    ? [kindFilter.value]
    : [...ENTRY_KINDS, ...CONTENT_KINDS]
)

/** 某台入口行是不是被当前筛选保留 */
function keepRow(row: ServerOverviewRow): boolean {
  return filterKinds.value.includes(row.kind)
}

/** 一段一段：每个服一张卡（按服分组才是「总览」，也避免同一台入口在页面上出现两次） */
const realmCards = computed(() => {
  const cards = overview.value?.realms ?? []
  const rows = (overview.value?.rows ?? []).filter(keepRow)
  return cards
    .map((card) => ({ card, rows: rows.filter((r) => r.realm_id === card.id) }))
    // 只筛某一类时，没有这一类的服不用占位置；看全部时保留空服，好让人一眼看出「这个服还空着」
    .filter(({ rows: list }) => list.length > 0 || kindFilter.value === '')
})

/** 内容自动化（MoviePilot / qB）：不属于某个服，多服共用 */
const contentRows = computed(() =>
  servers.value.filter((s) => filterKinds.value.includes(s.kind) && CONTENT_KINDS.includes(s.kind))
)

/** 当前筛选下要不要显示两段（出流入口 / 内容自动化） */
const showEntries = computed(() => !kindFilter.value || ENTRY_KINDS.includes(kindFilter.value))
const showContent = computed(() => !kindFilter.value || CONTENT_KINDS.includes(kindFilter.value))

const warnCount = computed(() => {
  const cards = (overview.value?.realms ?? []).reduce((sum, c) => sum + (c.warnings?.length || 0), 0)
  const rows = (overview.value?.rows ?? []).reduce((sum, r) => sum + (r.warnings?.length || 0), 0)
  return cards + rows
})

// ==================== 表格列 ====================

/** 入口表（EA / 已有 Emby）：一行的所有事实都在这几列里 */
const entryColumns: DataColumn[] = [
  { key: 'name', label: '名称', minWidth: 170, mobile: 'title' },
  { key: 'kind_label', label: '类型', width: 120 },
  { key: 'url', label: '地址', minWidth: 190 },
  { key: 'state', label: '连接', width: 170 },
  { key: 'node', label: '节点 / 归属', minWidth: 200 },
  { key: 'mounts', label: '挂载体检', minWidth: 190 },
  { key: 'libraries', label: '媒体库', width: 130 },
  // 测试 / 设为当前 / 编辑 / 停用启用 / 删除 收进「管理」弹窗：行里只留一个入口
  { key: 'actions', label: '操作', width: 100, fixed: 'right', align: 'right' },
]

const contentColumns: DataColumn[] = [
  { key: 'name', label: '名称', minWidth: 160, mobile: 'title' },
  { key: 'kind_label', label: '类型', width: 130 },
  { key: 'scope', label: '作用范围', width: 120 },
  { key: 'url', label: '地址', minWidth: 200 },
  { key: 'state', label: '状态', width: 190 },
  // 同上：一个「管理」入口，细节与动作都在弹窗里
  { key: 'actions', label: '操作', width: 100, fixed: 'right', align: 'right' },
]

// ==================== 加载 ====================

async function load() {
  loading.value = true
  try {
    if (!realm.loaded) realm.load().catch(() => undefined)
    const [list, ov] = await Promise.all([
      fetchServers(scopeParams.value),
      fetchServersOverview(scopeParams.value),
    ])
    servers.value = list.servers
    kinds.value = list.kinds
    summary.value = list.summary
    overview.value = ov
  } finally {
    loading.value = false
  }
}

/** 一键体检：真的去问每台 EA「你是谁、属于哪个服、负责哪些库」，并重拉当前出流 EA 的挂载体检 */
async function runLive() {
  liveRunning.value = true
  try {
    const ov = await fetchServersOverview({ ...scopeParams.value, live: true })
    overview.value = ov
    const list = await fetchServers(scopeParams.value)
    servers.value = list.servers
    summary.value = list.summary
    const bad = ov.totals?.warnings ?? 0
    bad
      ? ElMessage.warning(`体检完成，有 ${bad} 条需要注意（已标在下面各行）`)
      : ElMessage.success('体检完成，全部正常')
  } catch {
    /* 拦截器已提示 */
  } finally {
    liveRunning.value = false
  }
}

onMounted(load)

// ==================== 展示辅助 ====================

function fmtDate(s: string | null): string {
  return s ? s.slice(0, 16).replace('T', ' ') : '未测试'
}

function stateBadge(s: RemoteServerRow): string {
  if (!s.is_enabled) return 'off'
  if (s.last_check_ok === true) return 'ok'
  if (s.last_check_ok === false) return 'danger'
  return 'warn'
}

function stateText(s: RemoteServerRow): string {
  if (!s.is_enabled) return '已停用'
  if (s.last_check_ok === true) return '连接正常'
  if (s.last_check_ok === false) return '连接失败'
  return '未测试'
}

/** 节点列：面板登记了什么 + EA 自己说了什么（live 才有） */
function nodeText(row: ServerOverviewRow): string {
  const parts: string[] = []
  if (row.node_key) parts.push(`认领：${row.node_key}`)
  else parts.push(row.kind === 'ea' ? '未认领（未配 NODE_KEY）' : '不适用')
  const identity = row.identity || ({} as ServerOverviewRow['identity'])
  if (identity.ok) {
    if (identity.realm_slug) parts.push(`EA 自称：${identity.realm_name || identity.realm_slug}`)
    if (identity.claimed && identity.node_name) parts.push(`节点：${identity.node_name}`)
    if (typeof identity.libraries === 'number') parts.push(`EA 可见库：${identity.libraries}`)
  }
  return parts.join(' · ')
}

function mountBadge(mounts: ServerOverviewRow['mounts']): string {
  if (!mounts) return 'off'
  if (mounts.never_checked) return 'warn'
  if (!mounts.ok) return 'danger'
  return mounts.failed_count ? 'danger' : 'ok'
}

function mountText(mounts: ServerOverviewRow['mounts']): string {
  if (!mounts) return '仅 EA 有此体检'
  if (mounts.never_checked) return '还没做过（点「一键体检」）'
  if (mounts.error) return `体检失败：${mounts.error}`
  if (mounts.failed_count) return `${mounts.failed_count} / ${mounts.total} 条不可达`
  return `${mounts.total} 条全部可达`
}

// ==================== 对话框 ====================

/**
 * 切换当前服：与顶栏那个切换器同一份状态（stores/realm），切完就地刷新本页
 *
 * 「多服」在导航里不再是一个模块，所以作用域的选择就放在这一页——
 * 先选服，再看这台的服务器与线路。
 */
async function switchRealm(id: number) {
  if (!id || id === realm.activeId) return
  try {
    await realm.switchTo(id)
    ElMessage.success(`已切换到「${realm.activeName() || id}」`)
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

function resetForm(kind: ServerKind = 'ea', realmId: number | null = null) {
  editingId.value = null
  unsavedResult.value = null
  form.name = ''
  form.kind = kind
  form.url = ''
  form.remark = ''
  form.is_enabled = true
  form.config = {}
  form.realm_id = realmId ?? realm.activeId
  form.shared = false
}

/** 从某个服的卡片上添加：直接预填那个服，不用再选一次 */
function openCreate(kind: ServerKind = 'ea', realmId: number | null = null) {
  resetForm(kind, realmId)
  dialogVisible.value = true
}

function openEdit(row: RemoteServerRow) {
  resetForm(row.kind, row.realm_id)
  editingId.value = row.id
  form.name = row.name
  form.url = row.url
  form.remark = row.remark
  form.is_enabled = row.is_enabled
  form.realm_id = row.realm_id ?? realm.activeId
  form.shared = !!row.shared
  // 密钥不回明文：留空即「不改」，所以这里只回填非密钥字段
  const config: Record<string, string> = {}
  for (const [key, value] of Object.entries(row.config || {})) config[key] = value ?? ''
  form.config = config
  dialogVisible.value = true
  // rclone 配置只在「已存在」的 EA 上可读（新增时还没有 id）
  if (row.kind === 'ea') {
    loadRcloneConf(row.id)
    loadServerMounts()
  }
}

/** 换类型时旧类型的字段不再适用，清掉避免把 A 类型的配置存进 B 类型 */
function onKindChange() {
  const keep = new Set((currentKind.value?.fields || []).map((f) => f.key))
  form.config = Object.fromEntries(Object.entries(form.config).filter(([k]) => keep.has(k)))
}

async function testUnsaved() {
  if (!form.url) return ElMessage.warning('请先填写服务地址')
  testingUnsaved.value = true
  try {
    unsavedResult.value = await testServerConfig({
      kind: form.kind,
      url: form.url,
      config: form.config,
      server_id: editingId.value ?? undefined,
    })
    unsavedResult.value.ok
      ? ElMessage.success(unsavedResult.value.message || '连接成功')
      : ElMessage.warning(unsavedResult.value.message || '连接失败')
  } catch {
    /* 拦截器已提示 */
  } finally {
    testingUnsaved.value = false
  }
}

async function save() {
  if (!form.name.trim()) return ElMessage.warning('请填写一个便于识别的名称')
  if (!form.url.trim()) return ElMessage.warning('请填写服务地址')
  saving.value = true
  const payload = {
    name: form.name.trim(),
    kind: form.kind,
    url: form.url.trim(),
    config: form.config,
    is_enabled: form.is_enabled,
    remark: form.remark,
    // 一个服一个：EA / Emby 必须选一个归属服；内容自动化可以选「全服共用」
    realm_id: form.shared ? null : form.realm_id,
    shared: isSharedKind.value && form.shared,
  }
  try {
    const res = editingId.value
      ? await updateServer(editingId.value, payload)
      : await createServer(payload)
    // 新建时也能直接粘 rclone.conf（v2.46.0）：拿到服务器 ID 后顺手写入，
    // 不用先保存、再打开一次、再粘一遍。写失败只提示，不推翻服务器已建成功的事实。
    let rcloneNote = ''
    const pendingRclone = rcloneConf.value.trim()
    if (pendingRclone && res.server?.id) {
      try {
        const rc = await saveServerRcloneConf(res.server.id, pendingRclone)
        rcloneConf.value = ''
        rcloneNote = `，已同时写入 rclone.conf（${rc.total} 个 remote）`
      } catch {
        rcloneNote = '，但 rclone.conf 写入失败（服务器已建好，可重新编辑这台服务器补填）'
      }
    }
    res.probe?.ok
      ? ElMessage.success(`已保存，连接正常${rcloneNote}`)
      : ElMessage.warning(`${res.probe?.message || '已保存，但连接失败：请检查地址与凭据'}${rcloneNote}`)
    dialogVisible.value = false
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

// ==================== 行操作 ====================

// ==================== 服务器管理弹窗（v2.29.0） ====================
// 入口行原本摆着 5 个按钮（测试 / 设为当前 / 编辑 / 停用启用 / 删除），而连接结论、节点认领、
// 挂载体检、媒体库归属全挤在带 tooltip 的单元格里；现在一个入口，弹窗里把一台机器的事实
// 与能做的动作放在同一处。
const manage = ref({ visible: false, row: null as ServerOverviewRow | RemoteServerRow | null })

function isEntryRow(r: RemoteServerRow | ServerOverviewRow): r is ServerOverviewRow {
  return 'is_current_entry' in r
}

function openManage(row: ServerOverviewRow | RemoteServerRow) {
  manage.value = { visible: true, row }
}

/** 动作后刷新两段表，并把弹窗里的那台机器换成最新快照 */
async function refreshManage(id: number) {
  await load()
  const fresh =
    (overview.value?.rows ?? []).find((r) => r.id === id) || servers.value.find((s) => s.id === id)
  if (fresh) {
    manage.value.row = fresh
  } else {
    manage.value.visible = false
  }
}

/** 从弹窗进编辑：先关掉这一层，避免两个弹窗叠着 */
function editFromManage(row: RemoteServerRow) {
  manage.value.visible = false
  openEdit(row)
}

/** 作用范围：内容自动化（MoviePilot / qB）可以是「全服共用」 */
function scopeText(row: RemoteServerRow): string {
  if (row.shared) return '全服共用'
  return row.realm_name || '未标注'
}

async function runTest(row: RemoteServerRow) {
  busyId.value = row.id
  try {
    const res = await testServer(row.id)
    res.ok ? ElMessage.success(res.message || '连接成功') : ElMessage.warning(res.message || '连接失败')
    await refreshManage(row.id)
  } catch {
    /* 拦截器已提示 */
  } finally {
    busyId.value = null
  }
}

async function runActivate(row: RemoteServerRow) {
  busyId.value = row.id
  try {
    const res = await activateServer(row.id)
    if (res.success) {
      ElMessage.success(res.message || '已设为当前使用')
      if (res.mounts_health && res.mounts_health.ok === false) {
        ElMessage.warning('这台 EA 上还有挂载不可达，看本页「挂载体检」列')
      }
    } else {
      ElMessage.warning(res.message || '连接没通过，已保持原来的入口')
    }
    await refreshManage(row.id)
  } catch {
    /* 拦截器已提示 */
  } finally {
    busyId.value = null
  }
}

async function runToggle(row: RemoteServerRow) {
  busyId.value = row.id
  try {
    const res = await toggleServer(row.id)
    ElMessage.success(res.server.is_enabled ? '已启用' : '已停用')
    await refreshManage(row.id)
  } catch {
    /* 拦截器已提示 */
  } finally {
    busyId.value = null
  }
}

async function runRefreshMounts() {
  try {
    const res = await refreshServerMounts()
    res.success
      ? ElMessage.success(`已刷新 ${res.server} 上的挂载可达性`)
      : ElMessage.warning(res.health?.error || 'EA 体检未完成')
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

async function remove(row: RemoteServerRow) {
  try {
    await ElMessageBox.confirm(
      `「${row.name}」将被永久删除，配置与凭证都会被清除，不可恢复。${row.is_active ? '它是当前的 Emby 服务入口，删除后会切回面板自己出流。' : ''}`,
      '删除服务器',
      {
        type: 'warning',
        confirmButtonText: '删除这台服务器',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      }
    )
  } catch {
    return
  }
  busyId.value = row.id
  try {
    await deleteServer(row.id)
    ElMessage.success('已删除')
    await refreshManage(row.id)
  } catch {
    /* 拦截器已提示 */
  } finally {
    busyId.value = null
  }
}

// ==================== 媒体运维（服务器维度） ====================
/**
 * 把「跑」这个动作从库上移到节点上：路径、策略、挂载仍然在「媒体库」页按库改
 * （这里只读不改），而「一键扫描 + 刮削」「整服扫描历史」「任务流水」都以这台
 * 机器为单位回答。
 */
const opsVisible = ref(false)
const opsLoading = ref(false)
const opsRow = ref<ServerOverviewRow | null>(null)
const ops = ref<ServerOpsSnapshot | null>(null)
/** 把同服里还没分配节点的库也算进来（那些库由面板扫，不属于这台节点） */
const opsIncludeUnassigned = ref(false)

function openOps(row: ServerOverviewRow) {
  opsRow.value = row
  ops.value = null
  opsVisible.value = true
  loadOps()
}

async function loadOps() {
  const row = opsRow.value
  if (!row) return
  opsLoading.value = true
  try {
    ops.value = await fetchServerOps(row.id, {
      include_unassigned: opsIncludeUnassigned.value,
    })
  } catch {
    /* 拦截器已提示 */
  } finally {
    opsLoading.value = false
  }
}

function onOpsScopeChange() {
  loadOps()
}

/** 一键扫描：入队结果逐类回报（入队 / 已在队列 / 转发 / 跳过），不只弹一句「已触发」 */
/** 队列状态 → 一句人话（而不是把 state 原词丢给用户） */
function opsStateText(state: string): string {
  switch (state) {
    case 'running': return '扫描中'
    case 'queued': return '排队中'
    case 'done': return '已完成'
    case 'failed': return '失败'
    case 'canceled': return '已取消'
    default: return '空闲'
  }
}

/** 耗时：毫秒 → 人话；没跑完（null）不要报 0ms */
/** 谁触发的（后端给的是机器可读的口径） */
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="媒体"
      title="服务器与线路"
      description="接了几台后端服、几台已有 Emby、当前用哪台出流、库归谁、这台机器碰不碰得到存储 —— 都在这一页（范围在「概况」右侧切，不用先理解「多服」是个什么模块）"
    >
      <template #actions>
        <el-button :loading="liveRunning" @click="runLive">
          <Wifi :size="14" style="margin-right: 4px" />一键体检
        </el-button>
        <el-button @click="runRefreshMounts">EA 挂载体检</el-button>
        <!-- 跳转类入口收进「更多」：页头只留体检与添加，窄屏不再折成三行按钮 -->
        <el-dropdown trigger="click" @command="onHeaderCommand">
          <el-button>
            更多<ChevronDown :size="14" style="margin-left: 4px" />
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="realms">
                <RealmIcon :size="14" style="margin-right: 6px" />服管理
              </el-dropdown-item>
              <el-dropdown-item command="mounts">
                <HardDrive :size="14" style="margin-right: 6px" />存储来源
              </el-dropdown-item>
              <el-dropdown-item command="emby">
                <Server :size="14" style="margin-right: 6px" />媒体库
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button aria-label="刷新" title="刷新" :loading="loading" @click="load"><RefreshCw :size="14" /></el-button>
        <el-button type="primary" @click="openCreate()">
          <Plus :size="14" style="margin-right: 4px" />添加服务器
        </el-button>
      </template>
    </PageHeader>

    <div class="tab-bar" role="tablist" aria-label="服务器与线路">
      <button
        id="servers-tab-servers"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'servers' }"
        :aria-selected="activeTab === 'servers'"
        aria-controls="servers-panel"
        @click="activeTab = 'servers'"
      >
        服务器
        <span class="tab-hint">入口 / 挂载 / 体检</span>
      </button>
      <button
        id="servers-tab-storage"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'storage' }"
        :aria-selected="activeTab === 'storage'"
        aria-controls="servers-panel"
        @click="activeTab = 'storage'"
      >
        存储账号
        <span class="tab-hint">115 / Google Drive 凭据</span>
      </button>
    </div>

    <!-- 服务器面板：v-show 常驻，切标签回来列表与弹窗状态都在 -->
    <div v-show="activeTab === 'servers'">

    <!-- 汇总：一眼看出「接了多少、多少可用、多少要处理」 -->
    <SectionCard title="概况" :icon="Server" class="ov-card">
      <template #actions>
        <span class="ov-scope">
        <!-- 当前服与范围都在这一页选：多服不是一个要单独学的模块，只是这里的一个筛选条件 -->
        <span class="ov-scope-label">当前服</span>
        <el-select
          :model-value="realm.activeId ?? undefined"
          size="small"
          placeholder="当前服"
          class="scope-realm"
          @change="switchRealm"
        >
          <el-option v-for="r in realm.realms" :key="r.id" :label="r.name" :value="r.id" />
        </el-select>
        <span class="ov-scope-label">范围</span>
        <el-radio-group v-model="scopeAll" size="small" @change="load">
          <el-radio-button :value="true">全部服</el-radio-button>
          <el-radio-button :value="false">仅当前服</el-radio-button>
        </el-radio-group>
        </span>
      </template>
      <div class="ov-grid">
        <StatTile label="Emby 出流入口" :value="totals?.entry ?? 0" />
        <StatTile label="连接正常" :value="totals?.online ?? 0" />
        <StatTile label="媒体库" :value="totals?.libraries ?? 0" />
        <StatTile
          label="未分配节点"
          :value="totals?.libraries_unassigned ?? 0"
          :tone="(totals?.libraries_unassigned ?? 0) > 0 ? 'warn' : 'plain'"
        />
        <StatTile label="需要处理" :value="warnCount" :tone="warnCount > 0 ? 'danger' : 'ok'" />
      </div>
    </SectionCard>

    <!-- 统计卡：点一下只看那一类（内容自动化与出流入口分开看） -->
    <div class="kind-grid">
      <button
        v-for="card in kindCards"
        :key="card.meta.value"
        type="button"
        class="filter-card"
        :class="{ active: kindFilter === card.meta.value }"
        :aria-pressed="kindFilter === card.meta.value"
        @click="kindFilter = kindFilter === card.meta.value ? '' : card.meta.value"
      >
        <div class="kind-top">
          <span class="kind-icon"><component :is="KIND_ICONS[card.meta.value]" :size="17" /></span>
          <span class="kind-label">{{ card.meta.short }}</span>
          <span class="kind-count">{{ card.stat?.total ?? 0 }}</span>
        </div>
        <div class="kind-line">
          <span class="mini-badge" :class="(card.stat?.reachable ?? 0) > 0 ? 'ok' : 'off'">
            可用 {{ card.stat?.reachable ?? 0 }}
          </span>
          <span v-if="card.meta.activatable" class="kind-current">
            {{ card.stat?.active_name ? `当前：${card.stat.active_name}` : '未设置当前使用' }}
          </span>
          <span v-else class="kind-current">用于求片</span>
        </div>
      </button>
    </div>

    <NoticePanel
      title="每一类分别是干什么的"
      summary="后端服 / 已有 Emby / MoviePilot / qBittorrent 的职责"
      :icon="Info"
      storage-key="servers-kinds"
      class="guide-panel"
    >
      <div class="guide">
        <b>后端服（EA）</b>＝本项目自带的 Emby API，播放和媒体库都靠它；必须和面板用同一个数据库与
        SECRET_KEY。<b>已有 Emby 服</b>＝你已经在跑的那台 Emby / Jellyfin，接入后本项目的自建媒体库会停用。
        <b>MoviePilot</b>＝负责搜索下载与整理，求片批准后一键提交成它的订阅。
        <b>qBittorrent</b>＝下载器，拿到磁力 / 种子链接就能直接下载（它自己不会去找片子）。
        EA / Emby 可以加多台，但**同一个服同一时间只有一台是「当前使用」**；MoviePilot 与 qB 可以多台一起接。
        <b>入口只有一个</b>：以前那页「Emby 服务入口」已并入这里，加完点「设为当前」即可生效。
      </div>
    </NoticePanel>

    <!-- ==================== Emby 总览（按服） ==================== -->
    <template v-if="showEntries">
      <div v-loading="loading" class="realm-stack">
        <EmptyState
          v-if="!loading && !realmCards.length"
          :icon="RealmIcon"
          title="还没有可显示的服"
          description="先到「服管理」建一个服，再回来给它加 EA 或已有 Emby 作为出流入口。"
        >
          <template #actions>
            <RouterLink to="/realms" class="au-btn au-btn-ghost au-btn-sm">去服管理</RouterLink>
          </template>
        </EmptyState>
        <SectionCard
          v-for="{ card, rows: entryRows } in realmCards"
          :key="card.id"
          class="realm-card"
          :icon="RealmIcon"
          :tone="card.warnings?.length ? 'accent' : 'default'"
        >
          <template #title>
            <span class="realm-name">
              {{ card.name }}
              <span v-if="card.is_default" class="mini-badge muted">默认服</span>
              <span v-if="!card.is_active" class="mini-badge warn">已停用</span>
              <em class="realm-slug">{{ card.slug }}</em>
            </span>
          </template>
          <template #actions>
            <span class="realm-actions">
              <RouterLink :to="`/realms`" class="muted link">服管理</RouterLink>
              <RouterLink :to="`/emby`" class="muted link">媒体库</RouterLink>
              <el-button size="small" @click="openCreate('ea', card.id)">
                <Plus :size="13" style="margin-right: 3px" />加 EA
              </el-button>
              <el-button size="small" @click="openCreate('emby', card.id)">
                <Plus :size="13" style="margin-right: 3px" />加 Emby
              </el-button>
            </span>
          </template>
            <div class="realm-entry">
              <span class="mini-badge" :class="card.entry.mode === 'panel' ? 'warn' : 'ok'">
                当前入口：{{ card.entry.label }}
              </span>
              <span v-if="card.entry.url" class="muted realm-url">{{ card.entry.url }}</span>
              <span class="muted">
                媒体库 {{ card.libraries.total }} 个<template v-if="card.libraries.unassigned">
                  （未分配节点 {{ card.libraries.unassigned }}）</template>
              </span>
            </div>

          <div v-if="card.warnings?.length" class="warn-box">
            <span v-for="w in card.warnings" :key="w" class="warn-line">
              <AlertTriangle :size="13" />{{ w }}
            </span>
          </div>

          <DataTable
            :rows="entryRows"
            :columns="entryColumns"
            empty="这个服还没有 Emby 出流入口：点右上角「加 EA」或「加 Emby」"
          >
            <template #empty>
              <EmptyState
                compact
                :icon="Server"
                title="这个服还没有 Emby 出流入口"
                description="点右上角「加 EA」或「加 Emby」。"
              />
            </template>
            <template #cell-name="{ row }">
              <div class="name-cell">
                <span class="name">{{ row.name }}</span>
                <span class="badges">
                  <span v-if="row.is_current_entry" class="mini-badge ok">当前使用</span>
                  <span v-if="!row.is_enabled" class="mini-badge off">已停用</span>
                  <span v-if="row.realm_name && !scopeAll" class="mini-badge muted">{{ row.realm_name }}</span>
                </span>
                <span v-if="row.remark" class="muted remark">{{ row.remark }}</span>
              </div>
            </template>

            <template #cell-kind_label="{ row }">
              <span class="mini-badge local">{{ row.kind_label }}</span>
            </template>

            <template #cell-url="{ row }">
              <span class="url">{{ row.url }}</span>
            </template>

            <template #cell-state="{ row }">
              <div class="state-cell">
                <span class="mini-badge" :class="stateBadge(row)">{{ stateText(row) }}</span>
                <span class="muted sub">{{ fmtDate(row.last_checked_at) }}</span>
                <el-tooltip v-if="row.last_check_message" :content="row.last_check_message" placement="top">
                  <span class="muted sub ellipsis">{{ row.last_check_message }}</span>
                </el-tooltip>
              </div>
            </template>

            <template #cell-node="{ row }">
              <el-tooltip :content="nodeText(row)" placement="top">
              <div class="state-cell">
                <span class="mini-badge" :class="row.node_key ? 'info' : 'muted'">
                  {{ row.node_key ? `认领 ${row.node_key}` : (row.kind === 'ea' ? '未认领' : '不适用') }}
                </span>
                <span v-if="row.identity?.ok && row.identity.realm_slug" class="muted sub">
                  EA 自称：{{ row.identity.realm_name || row.identity.realm_slug }}
                </span>
                <el-tooltip v-if="row.identity?.ok === false" :content="row.identity.error || ''" placement="top">
                  <span class="muted sub ellipsis">身份探测失败</span>
                </el-tooltip>
                <span v-else-if="typeof row.identity?.libraries === 'number'" class="muted sub">
                  EA 可见库：{{ row.identity.libraries }}
                </span>
              </div>
              </el-tooltip>
            </template>

            <template #cell-mounts="{ row }">
              <el-tooltip v-if="row.mounts" :content="mountText(row.mounts)" placement="top">
                <div class="state-cell">
                  <span class="mini-badge" :class="mountBadge(row.mounts)">{{ mountText(row.mounts) }}</span>
                  <span v-if="row.mounts.unreachable?.length" class="muted sub ellipsis">
                    不可达：{{ row.mounts.unreachable.join('、') }}
                  </span>
                </div>
              </el-tooltip>
              <span v-else class="muted">—</span>
            </template>

            <template #cell-libraries="{ row }">
              <div class="state-cell">
                <span class="muted sub">归它 {{ row.libraries_assigned }} 个</span>
                <span v-if="row.kind === 'ea'" class="muted sub">未分配 {{ row.libraries_unassigned }} 个</span>
              </div>
            </template>

            <template #cell-actions="{ row }">
              <!-- 执行动作上移到服务器维度：扫描 / 刮削 / 历史 / 任务都在这台节点上（配置仍在媒体库页按库改） -->
              <el-button
                v-if="row.kind === 'ea'"
                size="small"
                plain
                @click="openOps(row)"
              >媒体运维</el-button>
              <!-- 一个入口：测试 / 设为当前 / 编辑 / 停用启用 / 删除 都在弹窗里（原来这行有 5 个按钮） -->
              <el-button size="small" plain @click="openManage(row)">管理</el-button>
            </template>

          </DataTable>

          <!-- 逐行的告警（挂载不可达 / 服不一致 / 连接失败…）：手机端在卡片下方，桌面端也能看全 -->
          <div v-if="entryRows.some((r) => r.warnings?.length)" class="row-warnings">
            <span v-for="r in entryRows.filter((x) => x.warnings?.length)" :key="r.id" class="warn-line">
              <AlertTriangle :size="13" /><b>{{ r.name }}：</b>{{ r.warnings.join('；') }}
            </span>
          </div>
        </SectionCard>
      </div>
    </template>

    <!-- ==================== 内容自动化（求片用） ==================== -->
    <template v-if="showContent">
      <SectionCard
        title="内容自动化（求片用）"
        :icon="CloudDownload"
        description="MoviePilot 负责找片、qB 负责下载；可以声明「全服共用」，多服接一套就够"
      >
        <template #actions>
          <el-button size="small" @click="openCreate('moviepilot')">
            <Plus :size="13" style="margin-right: 3px" />加 MoviePilot
          </el-button>
          <el-button size="small" @click="openCreate('qbittorrent')">
            <Plus :size="13" style="margin-right: 3px" />加 qB
          </el-button>
        </template>
        <DataTable
          :rows="contentRows"
          :columns="contentColumns"
          :loading="loading"
          empty="还没有接 MoviePilot / qBittorrent：求片批准后就没办法把片子弄进来"
        >
          <template #empty>
            <EmptyState
              compact
              :icon="CloudDownload"
              title="还没有接 MoviePilot / qBittorrent"
              description="求片批准后就没办法把片子弄进来。"
            />
          </template>
          <template #cell-name="{ row }">
            <div class="name-cell">
              <span class="name">{{ row.name }}</span>
              <span v-if="row.remark" class="muted remark">{{ row.remark }}</span>
            </div>
          </template>
          <template #cell-kind_label="{ row }">
            <span class="mini-badge remote">{{ row.kind_label }}</span>
          </template>
          <template #cell-scope="{ row }">
            <span v-if="row.shared" class="mini-badge info">全服共用</span>
            <span v-else-if="row.realm_name" class="mini-badge muted">{{ row.realm_name }}</span>
            <span v-else class="mini-badge warn">未归服</span>
          </template>
          <template #cell-url="{ row }">
            <span class="url">{{ row.url }}</span>
          </template>
          <template #cell-state="{ row }">
            <div class="state-cell">
              <span class="mini-badge" :class="stateBadge(row)">{{ stateText(row) }}</span>
              <span class="muted sub">{{ fmtDate(row.last_checked_at) }}</span>
            </div>
          </template>
          <template #cell-actions="{ row }">
            <!-- 一个入口：测试 / 编辑 / 停用启用 / 删除 都在弹窗里 -->
            <el-button size="small" plain @click="openManage(row)">管理</el-button>
          </template>
        </DataTable>
      </SectionCard>
    </template>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑服务器' : '添加服务器'"
      width="min(560px, 92vw)"
      class="server-dialog"
    >
      <el-form label-position="top">
        <el-form-item label="类型">
          <el-select v-model="form.kind" style="width: 100%" :disabled="!!editingId" @change="onKindChange">
            <el-option v-for="k in kinds" :key="k.value" :label="k.label" :value="k.value" />
          </el-select>
          <p v-if="currentKind" class="field-help">{{ currentKind.desc }}</p>
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="form.name" :placeholder="`例如 ${currentKind?.label || '服务器'} 主节点`" />
        </el-form-item>
        <el-form-item label="地址">
          <el-input v-model="form.url" placeholder="必须以 http:// 或 https:// 开头" />
        </el-form-item>
        <el-form-item label="归属服">
          <el-select
            v-model="form.realm_id"
            :disabled="isSharedKind && form.shared"
            style="width: 100%"
            placeholder="选择这台服务器属于哪个服"
          >
            <el-option v-for="r in realm.realms" :key="r.id" :label="r.name" :value="r.id" />
          </el-select>
          <p class="field-help">
            EA / Emby 是「一个服一个」的入口：用户看到的地址、能播的内容都按归属服判定。
          </p>
          <template v-if="isSharedKind">
            <el-checkbox v-model="form.shared" label="全服共用（不属于某个服）" border style="margin-top: 8px" />
            <p class="field-help">
              MoviePilot / qBittorrent 属于内容自动化：勾上以后每个服都能用它求片，
              多服运营通常只需要接一套。
            </p>
          </template>
        </el-form-item>
        <el-form-item v-for="field in currentKind?.fields || []" :key="field.key" :label="field.label">
          <el-input
            v-model="form.config[field.key]"
            :type="field.type === 'password' ? 'password' : 'text'"
            :show-password="field.type === 'password'"
            :placeholder="
              editingId && field.secret && (servers.find((s) => s.id === editingId)?.secret_keys || []).includes(field.key)
                ? '已保存，留空表示不修改'
                : field.placeholder
            "
          />
          <p v-if="field.help" class="field-help">{{ field.help }}</p>
        </el-form-item>
        <!-- rclone 配置：每台 EA 一份，在「添加/编辑服务器」里配一次（对标 oceancloud）-->
        <template v-if="form.kind === 'ea'">
          <el-divider content-position="left">rclone 配置（每台 EA 一份）</el-divider>
          <el-form-item label="rclone.conf">
            <el-input
              v-model="rcloneConf"
              type="textarea"
              :rows="7"
              placeholder="[gdrive]&#10;type = drive&#10;token = …&#10;&#10;[onedrive]&#10;type = onedrive"
              :disabled="false"
            />
            <p class="field-help">
              粘 <code>rclone config</code> 生成的 INI 文本，落盘到
              <code>{{ rcloneConfPath || 'data/rclone/rclone-&lt;服务器ID&gt;.conf' }}</code>，调 rclone 时自动带
              <code>--config</code>。面板不代管凭据，也不回显原文——要改就重新粘一份覆盖。
            </p>
            <p v-if="!editingId" class="field-help">
              现在可以直接粘：点下方「创建」时会连同服务器一起写入，不用先保存再回来。
            </p>
            <div class="rclone-row">
              <el-button
                v-if="editingId"
                :loading="rcloneSaving"
                :disabled="!rcloneConf.trim()"
                @click="saveRcloneConf"
              >
                保存 rclone.conf
              </el-button>
              <span v-if="rcloneLoading" class="field-help">读取中…</span>
              <span v-else-if="rcloneRemotes.length" class="field-help">
                已配置 {{ rcloneRemotes.length }} 个 remote：{{ rcloneRemotes.join('、') }}
              </span>
              <span v-else-if="editingId" class="field-help">还没配置 rclone.conf</span>
            </div>
          </el-form-item>
        </template>
        <!-- 存储挂载也并进服务器：建在哪台 EA 上就由它去读 -->
        <template v-if="form.kind === 'ea' && editingId">
          <el-divider content-position="left">存储挂载</el-divider>
          <p class="field-help" style="margin-bottom: 10px">
            挂载 = 把内容接进媒体库的方式。类型由路径前缀决定；建在这里就固定由这台 EA 读，
            并用这台 EA 自己的 rclone.conf。
          </p>
          <div v-if="mountsLoading" class="server-mounts" aria-busy="true">
            <div class="au-skeleton mount-skeleton" /><div class="au-skeleton mount-skeleton" />
          </div>
          <div v-else-if="serverMounts.length" class="server-mounts">
            <div v-for="m in serverMounts" :key="m.id" class="server-mount-row">
              <div class="server-mount-main">
                <span class="mini-badge">{{ m.mount_type_label }}</span>
                <code>{{ m.path }}</code>
                <span v-if="m.library_ids.length" class="field-help">
                  被 {{ m.library_ids.length }} 个媒体库使用
                </span>
                <div v-if="m.legacy_note" class="legacy-note">{{ m.legacy_note }}</div>
              </div>
              <div class="server-mount-ops">
                <el-button link :loading="busyId === m.id" title="测试挂载" aria-label="测试挂载" @click="testOneMount(m)">
                  <Wifi :size="14" />
                </el-button>
                <el-button link title="浏览目录" aria-label="浏览目录" @click="openMountBrowse(m)">
                  <FolderOpen :size="14" />
                </el-button>
                <el-button link @click="startEditMount(m)">编辑</el-button>
                <el-button link @click="toggleMount(m)">
                  {{ m.is_enabled ? '停用' : '启用' }}
                </el-button>
                <el-button link type="danger" title="删除挂载" aria-label="删除挂载" @click="removeMount(m)">
                  <Trash2 :size="14" />
                </el-button>
              </div>
            </div>
          </div>
          <EmptyState
            v-else
            compact
            :icon="HardDrive"
            title="这台服务器还没有挂载"
            description="在下方填名称与路径即可添加。"
            class="mount-empty"
          />

          <div class="add-mount">
            <el-input v-model="mountForm.name" placeholder="挂载名称，例如「主号电影」" />
            <MountSourceEditor v-model="mountForm.path" v-model:type="mountForm.type" />
            <div class="add-mount-ops">
              <el-button type="primary" plain @click="addMount">
                <Plus v-if="!editingMountId" :size="14" style="margin-right: 4px" />
                {{ editingMountId ? '保存修改' : '添加挂载' }}
              </el-button>
              <el-button v-if="editingMountId" text @click="cancelEditMount">取消</el-button>
              <el-button :loading="checkingMounts" @click="runMountHealth">
                <RefreshCw :size="14" style="margin-right: 4px" />挂载体检
              </el-button>
            </div>
          </div>
        </template>
        <el-form-item label="备注">
          <el-input v-model="form.remark" placeholder="可选，例如「主力机」「备份」" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.is_enabled" />
        </el-form-item>
      </el-form>

      <div v-if="unsavedResult" class="probe" :class="unsavedResult.ok ? 'ok' : 'bad'" role="status">
        <CheckCircle2 v-if="unsavedResult.ok" :size="14" />
        <AlertTriangle v-else :size="14" />
        {{ unsavedResult.message || (unsavedResult.ok ? '连接成功' : '连接失败') }}
      </div>

      <template #footer>
        <el-button :loading="testingUnsaved" @click="testUnsaved">
          <Wifi :size="14" style="margin-right: 4px" />测试连接
        </el-button>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!--
      服务器管理（弹窗）：一台机器的全部事实（连接、节点认领、挂载体检、媒体库归属、告警）
      与全部动作放在一处。以前这些分散在带 tooltip 的列里，左边是 5 个按钮。
      动作做完弹窗不关，弹窗里的快照就地刷新。
    -->
    <el-dialog v-model="manage.visible" :title="`管理服务器「${manage.row?.name || ''}」`" width="min(580px, 92vw)">
      <div v-if="manage.row" class="mg-body">
        <div class="kv-list">
          <div class="kv-row"><span class="kv-key">名称</span>
            <span class="kv-value">
              {{ manage.row.name }}
              <span v-if="isEntryRow(manage.row) && manage.row.is_current_entry" class="mini-badge ok mg-gap">当前使用</span>
              <span v-if="!manage.row.is_enabled" class="mini-badge off mg-gap">已停用</span>
            </span>
          </div>
          <div class="kv-row"><span class="kv-key">类型 / 归属</span>
            <span class="kv-value">{{ manage.row.kind_label }} · {{ scopeText(manage.row) }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">地址</span>
            <span class="kv-value mono">{{ manage.row.url || '未填写' }}</span>
          </div>
          <div class="kv-row"><span class="kv-key">连接</span>
            <span class="kv-value">
              <span class="mini-badge" :class="stateBadge(manage.row)">{{ stateText(manage.row) }}</span>
              <span class="mg-msg">
                {{ fmtDate(manage.row.last_checked_at) }}
                <template v-if="manage.row.last_check_message">· {{ manage.row.last_check_message }}</template>
              </span>
            </span>
          </div>
          <div v-if="isEntryRow(manage.row)" class="kv-row"><span class="kv-key">节点认领</span>
            <span class="kv-value">{{ nodeText(manage.row) }}</span>
          </div>
          <div v-if="isEntryRow(manage.row)" class="kv-row"><span class="kv-key">挂载体检（EA 视角）</span>
            <span class="kv-value">
              <span class="mini-badge" :class="mountBadge(manage.row.mounts)">{{ mountText(manage.row.mounts) }}</span>
              <span v-if="manage.row.mounts?.unreachable?.length" class="mg-msg">
                不可达：{{ manage.row.mounts.unreachable.join('、') }}
              </span>
            </span>
          </div>
          <div v-if="isEntryRow(manage.row)" class="kv-row"><span class="kv-key">媒体库归属</span>
            <span class="kv-value">
              归它 {{ manage.row.libraries_assigned }} 个
              <template v-if="manage.row.kind === 'ea'">· 未分配 {{ manage.row.libraries_unassigned }} 个</template>
            </span>
          </div>
          <div class="kv-row"><span class="kv-key">备注</span><span class="kv-value">{{ manage.row.remark || '—' }}</span></div>
        </div>

        <el-alert
          v-if="isEntryRow(manage.row) && manage.row.warnings?.length"
          type="warning"
          :closable="false"
          show-icon
        >
          <template #title>这台机器有需要处理的事项</template>
          <template #default>
            <ul class="mg-warn">
              <li v-for="w in manage.row.warnings" :key="w">{{ w }}</li>
            </ul>
          </template>
        </el-alert>

        <p class="mg-hint">
          <template v-if="isEntryRow(manage.row) && manage.row.is_entry">
            「设为当前」会让这个服的客户端改连这台（连接不通过时保持原入口）；
          </template>
          「停用」只是不再使用它，配置与凭证都保留；删除不可恢复。改地址与密钥进「编辑」。
        </p>

        <!-- 危险区：删除不跟日常操作同排，单独隔离（Batch B） -->
        <div class="mg-danger">
          <div class="mg-danger-text">
            <strong>危险区</strong>
            <p>删除这台服务器不可恢复，配置与凭证都会被清除。日常运维（测试连接、停用 / 启用、改地址）请用页脚的按钮。</p>
          </div>
          <el-button type="danger" plain :disabled="!manage.row" @click="manage.row && remove(manage.row)">
            <Trash2 :size="13" style="margin-right: 3px" />删除这台服务器
          </el-button>
        </div>
      </div>

      <template #footer>
        <div class="mg-footer">
          <div class="mg-footer-right">
            <el-button @click="manage.visible = false">关闭</el-button>
            <template v-if="manage.row">
              <el-button :loading="busyId === manage.row.id" @click="runTest(manage.row)">
                <Wifi :size="13" style="margin-right: 3px" />测试连接
              </el-button>
              <!-- 「设为当前」只给出流入口（EA / 已有 Emby）：内容自动化那两段原先也没有这个动作 -->
              <el-button
                v-if="isEntryRow(manage.row) && manage.row.is_entry && manage.row.activatable && !manage.row.is_current_entry"
                type="primary"
                plain
                :loading="busyId === manage.row.id"
                @click="runActivate(manage.row)"
              >
                <CheckCircle2 :size="13" style="margin-right: 3px" />设为当前
              </el-button>
              <el-button :loading="busyId === manage.row.id" @click="runToggle(manage.row)">
                {{ manage.row.is_enabled ? '停用' : '启用' }}
              </el-button>
              <el-button type="primary" @click="editFromManage(manage.row)">
                <Pencil :size="13" style="margin-right: 3px" />编辑
              </el-button>
            </template>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- ==================== 媒体运维（服务器维度） ==================== -->
    <el-drawer
      v-model="opsVisible"
      :title="`媒体运维 · ${opsRow?.name || ''}`"
      size="min(760px, 96vw)"
    >
      <div v-loading="opsLoading" class="ops">
        <div class="ops-head">
          <p class="ops-hint">
            节点视角：这台机器管哪些库、各自状态如何。
            <strong>配置不动</strong>——路径、刮削策略、存储挂载仍在「媒体库」页按库改；扫描控制台也在「媒体库」页。
          </p>
          <div class="ops-scope">
            <el-checkbox v-model="opsIncludeUnassigned" @change="onOpsScopeChange">
              含未分配的库（同服 {{ ops?.scope.unassigned ?? 0 }} 个，由面板扫描）
            </el-checkbox>
            <el-button @click="loadOps"><RefreshCw :size="14" style="margin-right: 4px" />刷新</el-button>
          </div>
        </div>

        <div v-if="ops?.notes?.length" class="ops-notes">
          <div v-for="(note, i) in ops.notes" :key="i" class="ops-note">
            <Info :size="13" />{{ note }}
          </div>
        </div>




        <!-- 归这台节点的库（只读：配置在媒体库页改） -->
        <SectionCard
          class="ops-card"
          title="媒体库"
          :meta="`${ops?.scope.libraries ?? 0} 个，其中 ${ops?.scope.enabled ?? 0} 个可扫`"
          :icon="Library"
        >
          <EmptyState v-if="!ops?.libraries.length" compact :icon="Library" title="这个范围里还没有媒体库" />
          <table v-else class="ops-table">
            <thead>
              <tr><th>库名</th><th>状态</th><th>条目</th><th>来源</th><th>最近扫描</th></tr>
            </thead>
            <tbody>
              <tr v-for="lib in ops!.libraries" :key="lib.id">
                <td>
                  {{ lib.name }}
                  <span v-if="lib.virtual" class="mini-badge muted">虚拟库</span>
                  <span v-else-if="!lib.enabled" class="mini-badge off">已停用</span>
                </td>
                <td>
                  <span class="mini-badge" :class="lib.state === 'running' ? 'local' : 'muted'">
                    {{ opsStateText(lib.state) }}
                  </span>
                  <div v-if="lib.live?.message" class="ops-err">{{ lib.live.message }}</div>
                </td>
                <td class="muted">{{ lib.item_count }}</td>
                <td class="muted">{{ lib.paths }} 路径<span v-if="lib.mount_ids.length"> + {{ lib.mount_ids.length }} 挂载</span></td>
                <td class="muted">{{ fmtDate(lib.last_scan_at) }}</td>
              </tr>
            </tbody>
          </table>
        </SectionCard>
      </div>

      <template #footer>
        <div class="ops-foot">
          <span class="muted">数据来自既有快照，不新建状态；点「刷新」重新读一次。</span>
          <el-button @click="opsVisible = false">关闭</el-button>
        </div>
      </template>
    </el-drawer>

    <!-- 目录浏览：从「存储来源」页搬过来的独立组件 -->
    <MountBrowseDialog
      v-model="browseVisible"
      :mount="browseTarget"
      root-key-label="目录"
      @pick="onBrowsePick"
    />
    </div>

    <!-- 存储账号面板：两个子页各自带 PageHeader，这里只放标签条，不再叠第二个页头 -->
    <div
      id="servers-panel"
      v-show="activeTab === 'storage'"
      class="tab-panel"
      role="tabpanel"
      :aria-labelledby="activeTab === 'servers' ? 'servers-tab-servers' : 'servers-tab-storage'"
    >
      <div class="tab-bar tab-bar--inner" role="tablist" aria-label="存储账号">
        <button
          id="storage-tab-pan115"
          type="button"
          role="tab"
          class="tab-btn"
          :class="{ active: activeSubTab === 'pan115' }"
          :aria-selected="activeSubTab === 'pan115'"
          aria-controls="storage-panel"
          @click="activeSubTab = 'pan115'"
        >
          115 账号
        </button>
        <button
          id="storage-tab-gdrive"
          type="button"
          role="tab"
          class="tab-btn"
          :class="{ active: activeSubTab === 'gdrive' }"
          :aria-selected="activeSubTab === 'gdrive'"
          aria-controls="storage-panel"
          @click="activeSubTab = 'gdrive'"
        >
          Google Drive
        </button>
      </div>
      <div
        id="storage-panel"
        class="tab-panel"
        role="tabpanel"
        :aria-labelledby="activeSubTab === 'pan115' ? 'storage-tab-pan115' : 'storage-tab-gdrive'"
      >
        <KeepAlive>
          <Pan115Accounts v-if="activeSubTab === 'pan115'" />
          <GDriveAccounts v-else />
        </KeepAlive>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 标签条（与 Codes.vue / Welfare.vue 同款；内层与外层隔 16px，不贴在一起） */
.tab-bar {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--au-border);
  overflow-x: auto;
  scrollbar-width: none;
}
.tab-bar--inner { margin-top: 16px; }
.tab-btn {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  flex-shrink: 0;
  padding: 10px 18px;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--au-text-2);
  font: inherit;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  margin-bottom: -1px;
  transition: color var(--au-fast) var(--au-ease), border-color var(--au-fast) var(--au-ease);
}
.tab-btn:hover { color: var(--au-text); }
.tab-btn:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: -2px; }
.tab-btn.active {
  color: var(--au-primary);
  border-bottom-color: var(--au-primary);
}
.tab-hint {
  font-size: 12px;
  color: var(--au-text-3);
  font-weight: 400;
  white-space: nowrap;
}
.tab-btn.active .tab-hint { color: var(--au-primary); }
.tab-panel { min-width: 0; }

@media (max-width: 640px) {
  .tab-btn { padding: 8px 12px; font-size: 14px; }
  .tab-hint { font-size: 11px; }
}
/* 管理弹窗：详情用全局 .kv-list，只补告警列表与页脚布局 */
.mg-body { display: flex; flex-direction: column; gap: 12px; }
.mg-body .kv-row .kv-value { text-align: left; }
.mg-gap { margin-left: 6px; }
.mg-msg { display: block; margin-top: 3px; font-size: var(--font-size-xs); color: var(--au-text-3); }
.mg-warn { margin: 0; padding-left: 18px; font-size: var(--font-size-xs); line-height: 1.8; }
.mg-hint {
  margin: 0;
  font-size: var(--font-size-xs);
  line-height: 1.8;
  color: var(--au-text-3);
  background: var(--au-bg-soft);
  border-radius: var(--au-r-md);
  padding: 10px 12px;
}
.mg-footer { display: flex; align-items: center; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
.mg-footer-right { display: flex; gap: 8px; flex-wrap: wrap; }
/* 管理弹窗 · 危险区：删除单独隔离，不跟日常操作同排 */
.mg-danger {
  display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  padding: 12px 14px; margin-top: 12px;
  border: 1px solid var(--danger-border); border-left: 3px solid var(--danger-border);
  border-radius: var(--au-r-md); background: var(--danger-bg);
}
.mg-danger-text strong { font-size: 13px; color: var(--danger); }
.mg-danger-text p { margin: 4px 0 0; font-size: 12px; color: var(--au-text-2); line-height: 1.6; }

/* ==================== 概况（StatTile + 范围切换） ==================== */
.ov-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}
.ov-scope { display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ov-scope-label { font-size: var(--font-size-xs); color: var(--au-text-3); }
.scope-realm { width: 150px; }

/* ==================== 类型卡片 ==================== */
/* 可点击的筛选卡：与 StatTile 同配方（实色表面 + 发丝线 + 14px），选中 = 琥珀描边 + 浅琥珀底 */
.kind-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.filter-card {
  display: block;
  width: 100%;
  padding: 14px 16px;
  text-align: left;
  font: inherit;
  color: inherit;
  cursor: pointer;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  transition: border-color var(--au-fast) var(--au-ease), background-color var(--au-fast) var(--au-ease);
}
.filter-card:hover { border-color: var(--au-border-strong); }
.filter-card:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }
.filter-card:active { transform: scale(0.98); }
.filter-card.active { border-color: var(--au-primary-border); background: var(--au-primary-soft); }
.kind-top { display: flex; align-items: center; gap: 8px; }
.kind-icon {
  width: 30px; height: 30px; display: grid; place-items: center;
  border-radius: var(--au-r-sm); background: var(--au-primary-soft); color: var(--au-primary);
}
.kind-label { color: var(--au-text-2); font-size: var(--font-size-sm); }
.kind-count {
  margin-left: auto; font-size: 22px; font-weight: 700; color: var(--au-text);
  font-variant-numeric: tabular-nums;
}
.kind-line { display: flex; align-items: center; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.kind-current { color: var(--au-text-3); font-size: var(--font-size-xs); }

.guide { line-height: 1.75; }

/* ==================== 按服的入口卡片（SectionCard） ==================== */
.realm-stack { display: flex; flex-direction: column; gap: 16px; min-height: 60px; }
.realm-name {
  display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap;
}
.realm-slug {
  font-family: var(--au-font-sans); font-style: normal; font-weight: 400;
  font-size: var(--font-size-xs); color: var(--au-text-4); letter-spacing: 0;
}
.realm-entry { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.realm-url { font-size: var(--font-size-xs); word-break: break-all; font-family: var(--font-mono); }
.realm-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.link { font-size: var(--font-size-xs); text-decoration: none; }
.link:hover { color: var(--au-primary); }
.link:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; border-radius: var(--au-r-sm); }

/* ==================== 告警 ==================== */
.warn-box, .row-warnings {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
}
.row-warnings { margin: 10px 0 0; }
.warn-line {
  display: flex; align-items: flex-start; gap: 6px;
  padding: 7px 10px;
  border: 1px solid var(--au-danger-border);
  border-radius: var(--au-r-md);
  background: var(--au-danger-soft);
  color: var(--au-danger);
  font-size: var(--font-size-xs);
  line-height: 1.6;
}
.warn-line svg { flex-shrink: 0; margin-top: 2px; }

/* ==================== 单元格 ==================== */
.name-cell { display: flex; flex-direction: column; gap: 3px; }
.name-cell .name { color: var(--au-text); font-weight: var(--font-weight-medium); }
.badges { display: flex; flex-wrap: wrap; gap: 4px; }
.remark { font-size: var(--font-size-xs); }
.url { color: var(--au-text-2); word-break: break-all; font-size: var(--font-size-sm); }
.state-cell { display: flex; flex-direction: column; gap: 3px; }
.sub { font-size: var(--font-size-xs); }
.ellipsis { display: block; max-width: 190px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.field-help { color: var(--au-text-3); font-size: var(--font-size-xs); margin: 5px 0 0; line-height: 1.6; }
.rclone-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
.server-mounts { display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; }
.server-mount-row {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 8px;
  padding: 8px 10px; border: 1px solid var(--au-border); border-radius: var(--au-r-md);
}
.server-mount-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.server-mount-ops { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
.server-mount-main code { font-size: var(--font-size-xs); word-break: break-all; }
.legacy-note {
  color: var(--au-danger); font-size: var(--font-size-xs); line-height: 1.6; margin-top: 2px;
}
.add-mount { display: flex; flex-direction: column; gap: 8px; }
.add-mount-ops { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mount-skeleton { height: 46px; border-radius: var(--au-r-md); }
.mount-empty {
  margin-bottom: 10px;
  border: 1px dashed var(--au-border-strong);
  border-radius: var(--au-r-md);
}
.probe {
  display: flex; align-items: center; gap: 6px;
  padding: 9px 11px; border: 1px solid transparent; border-radius: var(--au-r-md);
  font-size: var(--font-size-sm); margin-top: 4px;
}
.probe svg { flex-shrink: 0; }
.probe.ok { color: var(--au-success); background: var(--au-success-soft); border-color: var(--au-success-border); }
.probe.bad { color: var(--au-danger); background: var(--au-danger-soft); border-color: var(--au-danger-border); }

/* ==================== 媒体运维抽屉 ==================== */
.ops { display: flex; flex-direction: column; gap: 14px; }
.ops-head { display: flex; flex-direction: column; gap: 8px; }
.ops-hint { margin: 0; font-size: var(--font-size-xs); color: var(--au-text-3); line-height: 1.8; }
.ops-hint.bad { color: var(--au-danger); }
.ops-scope { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.ops-notes {
  display: flex; flex-direction: column; gap: 6px;
  padding: 10px 12px; border-radius: var(--au-r-md); background: var(--au-bg-soft);
}
.ops-note {
  display: flex; align-items: flex-start; gap: 6px;
  font-size: var(--font-size-xs); color: var(--au-text-2); line-height: 1.7;
}
.ops-note svg { flex-shrink: 0; margin-top: 3px; color: var(--au-text-3); }
.ops-card :deep(.au-section__body) { display: flex; flex-direction: column; gap: 10px; }
.ops-sub {
  font-size: var(--font-size-xs); color: var(--au-text-2); font-weight: var(--font-weight-medium);
  margin-top: 4px; padding-top: 8px; border-top: 1px solid var(--au-border);
}
.ops-table { width: 100%; border-collapse: collapse; font-size: var(--font-size-xs); }
.ops-table th {
  text-align: left; font-weight: var(--font-weight-medium); color: var(--au-text-3); letter-spacing: 0.04em;
  padding: 6px 8px; border-bottom: 1px solid var(--au-border); white-space: nowrap;
}
.ops-table td { padding: 7px 8px; border-bottom: 1px solid var(--au-border); vertical-align: top; }
.ops-table tr:last-child td { border-bottom: none; }
.ops-err { color: var(--au-danger); margin-top: 3px; }
.ops-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.ops-foot .muted { font-size: var(--font-size-xs); }

@media (max-width: 1100px) { .kind-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 768px) {
  .kind-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .filter-card { padding: 12px; }
  .kind-count { font-size: 19px; }
  .ov-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .ov-scope { width: 100%; }
  .scope-realm { flex: 1 1 120px; width: auto; }
  .realm-actions { width: 100%; }
  .server-mount-row { flex-direction: column; }
  .server-mount-ops { align-self: flex-end; }
  .mg-footer, .mg-footer-right { width: 100%; }
}
@media (max-width: 420px) {
  .kind-grid { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .ops-table { display: block; overflow-x: auto; white-space: nowrap; }
  .ops-foot { flex-direction: column; align-items: stretch; }
  .ops-foot .el-button { width: 100%; }
}

.realm-card, .server-card { content-visibility: auto; contain-intrinsic-size: auto 280px; }
</style>
