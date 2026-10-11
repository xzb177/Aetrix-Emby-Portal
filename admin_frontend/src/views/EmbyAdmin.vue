<script setup lang="ts">
/**
 * 媒体库管理：库列表/创建/扫描/删除 + 刮削策略 + 平台虚拟媒体库 + 图片修复队列
 * + 停止全部转码
 *
 * v2.6.11：会话表改用 DataTable（手机卡片）；媒体库卡片的「挂载 / 刮削策略 / 115 账号」
 * 在窄屏改为「标签在上、控件在下」，不再把中文标签挤成竖排两行；页面里的硬编码灰度
 * 全部换成主题令牌。
 *
 * v2.6.20：多服 / 多机部署——每个库都能指定「归属服」与「归属播放节点」（未指定 = 所有服、
 * 所有节点可见，由面板扫描）；已分配的库只有那台 EA 向客户端展示、也只有它会扫描。
 *
 * v2.42.9：**本页不再承担跳板职责**。「元数据与刮削」卡片里的 TMDB 密钥填写框、
 * 「去存储来源页管理」与「去元数据来源页」两个跳转按钮、以及文内的链接全部移除：
 * 密钥只在「元数据来源」页填（单一入口），本页只保留 TMDB 状态读数与按库的扫描配置。
 * 侧边栏已经是这两个页面的入口，页内再堆按钮只会让人以为「这里管不了，得去别处」。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import {
  Delete, Film, FolderPlus, HardDrive, History, ImagePlus, RefreshCcw, RefreshCw, ScanSearch,
  CalendarClock, Info, Link, ListChecks, Search, Server, ShieldAlert,
  Settings2, Square, Wand2, X,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import {
  cancelQueuedScan,
  createLibrary,
  deleteLibrary,
  fetchLibraries,
  fetchLibraryCover,
  fetchLibraryScans,
  fetchMounts,
  fetchPan115Accounts,
  fetchReachability,
  fetchRepairQueue,
  fetchScanQueue,
  fetchFsWatchStatus,
  fetchServers,
  fetchAutoScan,
  fetchChaseNew,
  fetchStrmConfig,
  fetchTmdbKeys,
  fetchTmdbLanguage,
  fetchEnrichEpisodeNight,
  generateVirtualLibraries,
  previewLibraryCover,
  regenerateLibraryCover,
  removeLibraryCover,
  renderLibraryCover,
  rescrapeLibrary,
  runRepairQueue,
  scanAllLibraries,
  scanLibrary,
  saveAutoScan,
  saveChaseNew,
  saveStrmConfig,
  saveTmdbLanguage,
  saveEnrichEpisodeNight,
  stopAllTranscodes,
  updateLibrary,
  uploadLibraryCover,
} from '@/api/admin'
import type { AutoScanConfig, ChaseNewConfig, StrmConfig, TmdbKeysStatus, TmdbLanguageStatus, EnrichEpisodeNightConfig } from '@/api/admin'
import { TMDB_LANGUAGE_LABELS } from '@/api/admin'
import type {
  EmbyLibrary,
  EmbyPlaybackReachability,
  EmbyReachabilityReport,
  EmbyScanQueue,
  EmbyScanResult,
  EmbyScanRun,
  EmbyScanSource,
  EmbyScanStatus,
  EmbyScanTask,
  Pan115Account,
  RemoteServerRow,
  StorageMount,
  StorageBackend,
  LibraryPathEntry,
} from '@/types'
import { useRealmStore } from '@/stores/realm'
import DataTable from '@/components/DataTable.vue'
import LibraryPathManager from '@/components/LibraryPathManager.vue'
import './EmbyAdmin.css'
import type { DataColumn } from '@/components/DataTable.vue'

const realm = useRealmStore()
const libraries = ref<EmbyLibrary[]>([])
const panAccounts = ref<Pan115Account[]>([])
const mounts = ref<StorageMount[]>([])
/** 可分配的播放节点（面板里 kind=ea 的服务器）：一个服可以有多台 */
const nodes = ref<RemoteServerRow[]>([])
const loading = ref(false)
const repairCount = ref(0)
const virtualLoading = ref(false)
const coverUrls = ref<Record<number, string>>({})
const coverUploading = ref<Record<number, boolean>>({})
let coverLoadVersion = 0

// 媒体库配置表单的可见性与状态统一在下面的「单页分组表单」一节里（新建 / 编辑共用）

// 扫描流水（最近若干轮）：抽屉里看「是不是每轮都在失败」
const scanDrawer = ref(false)
const scanLoading = ref(false)
const scanTarget = ref<EmbyLibrary | null>(null)
const scanRuns = ref<EmbyScanRun[]>([])
const scanKeep = ref(0)

// 扫描队列（v2.27.0）：同一远程挂载同时只跑一个扫描，其它库排队。
// 只靠「每 1.5 秒刷一次媒体库列表」看不出排队原因（在等哪个挂载、排第几位），
// 所以队列单独轮询一份快照。
const scanQueue = ref<EmbyScanQueue | null>(null)
let queueTimer: number | undefined
let queueWasBusy = false
/** 扫描后「过一会儿再刷列表」的延时任务：离开页面时一并清掉，别在卸载后还发请求 */
const pendingReloads = new Set<number>()
function reloadSoon(ms: number) {
  const id = window.setTimeout(() => {
    pendingReloads.delete(id)
    void load()
  }, ms)
  pendingReloads.add(id)
}

/** 标签页切到后台就停掉队列轮询，回到前台立刻补拉一次再恢复（3 秒一次的请求没人看就是浪费） */
function startQueuePolling() {
  if (queueTimer) return
  queueTimer = window.setInterval(pollQueue, 3000)
}
function stopQueuePolling() {
  if (queueTimer) window.clearInterval(queueTimer)
  queueTimer = undefined
}
function onVisibilityChange() {
  if (document.hidden) {
    stopQueuePolling()
  } else {
    void pollQueue()
    startQueuePolling()
  }
}

// 播放可达性（v2.28.0）：面板扫描正常 ≠ 出流的机器拿得到内容。
// 分离部署（EM 控制面 + EA 数据面 + 共享存储）下这是最该先看的一页：
// 本机路径 / local 挂载的库在 EA 出流时读不到，客户端只会看到条目的 404。
const reachability = ref<EmbyReachabilityReport | null>(null)

/** 归属服选项：默认服 + 已建的其他服 */
const realmOptions = computed(() => realm.realms)

function nodeLabel(n: RemoteServerRow): string {
  const state = n.last_check_ok === true ? '在线' : n.last_check_ok === false ? '未通过' : '未体检'
  return `${n.name}（${state}）`
}

// ==================== 媒体库配置：新建与编辑共用一张分组表单 ====================
// 借鉴 Emby Manager：一个媒体库的全部配置在一张单页表单里改完（分组 + 每项一句人话
// 说明 + 推荐项标注），而不是「新建一个弹窗 + 设置一个抽屉」两处各改一半：以前
// 路径在设置里只能看不能改、启用状态和追新监听根本没入口。
// 表单状态只有这一份，所以「浏览」的追加 / 去重口径两边完全一致。

interface LibFormState {
  /** null = 新建 */
  id: number | null
  name: string
  collection_type: string
  is_enabled: boolean
  realm_id: number | null
  node_id: number | null
  scrape_policy: string
  account_115_id: number | null
  /** 媒体路径（简化形式）：路径与存储后端分开，不出现 mount:// 前缀 */
  path_entries: LibraryPathEntry[]
  /** 是否纳入「追新」轮询监听（仅编辑可用；追新本身是全局开关） */
  chase: boolean
}

function emptyLibForm(): LibFormState {
  return {
    id: null,
    name: '',
    collection_type: 'movies',
    is_enabled: true,
    realm_id: null,
    node_id: null,
    scrape_policy: 'missing_only',
    account_115_id: null,
    path_entries: [],
    chase: false,
  }
}

const libForm = reactive<LibFormState>(emptyLibForm())
const libFormVisible = ref(false)
const libFormSaving = ref(false)
/** 追新监听是即时保存的（另一个接口），单独一个 loading，不跟表单保存共用 */
const chaseLibSaving = ref(false)
/** 编辑中的库对象（封面操作要用）；新建时为 null */
const libFormTarget = ref<EmbyLibrary | null>(null)
/**
 * 老数据里「库级挂载」的 id（v2.46.0 起不再能编辑，只读展示）。
 * 它不进表单、也不回传：后端对省略 = 不修改，所以老库挂着的挂载原封不动。
 * 只做两件事：校验时让「只有挂载没路径」的老库还能保存 + 在界面上说清它还在生效。
 */
const legacyMountIds = computed<number[]>(() => libFormTarget.value?.mount_ids || [])
/** 挂载 id → 名字（挂载已删时回落成 ``#id``，不能变成一片空白） */
function mountNamesOf(ids: number[]): string {
  return ids.map((id) => mounts.value.find((m) => m.id === id)?.name || `#${id}`).join('、')
}
const legacyMountNames = computed<string>(() => mountNamesOf(legacyMountIds.value))
/** 打开时的表单指纹：算「有没有改动」+ 还原用（存指纹而不是对象引用） */
const libFormFingerprintAtOpen = ref('')

// ---- 封面自动生成（样式 + 标题变量） ----

/** 样式选项：每项写清「长什么样」，别让管理员对着预览猜 */
const COVER_TEMPLATES: LibOption[] = [
  { value: 'poster', label: '海报拼贴', hint: '最新入库的多张海报排在一起，下面压标题。适合剧集库，一眼看出最近更新了什么' },
  { value: 'visual', label: '主视觉', hint: '单张海报铺满整幅，底部渐变压标题。适合电影库，干净大气' },
  { value: 'filmstrip', label: '胶片带', hint: '三张海报横排成带状，标题压在下方。适合片库少的分类' },
]

const coverTemplate = ref<'' | 'poster' | 'visual' | 'filmstrip'>('')
const coverTitle = ref('')
const coverSubtitle = ref('')
/** “新片入库后自动重生成封面”开关：跟着表单一起存（保存走主表单的提交） */
const coverAutoRegen = ref(false)
const coverPreviewUrl = ref('')
const coverPreviewLoading = ref(false)
const coverPreviewError = ref('')
const coverSaving = ref(false)
const coverRegenerating = ref(false)

/** 预览的 blob URL，用完必须 revoke，否则每点一次泄漏一份 */
let coverPreviewObjectUrl = ''

function releaseCoverPreview() {
  if (coverPreviewObjectUrl) {
    URL.revokeObjectURL(coverPreviewObjectUrl)
    coverPreviewObjectUrl = ''
  }
}

/** 预览需要已保存的库 id：渲染要从库里挑海报，新建库还没入库没有海报可选 */
function canPreviewCover(): boolean {
  return libFormTarget.value !== null
}

async function loadCoverPreview() {
  const lib = libFormTarget.value
  if (!lib || !coverTemplate.value) {
    coverPreviewUrl.value = ''
    coverPreviewError.value = coverTemplate.value ? '新建的库还没有条目，保存后再生成封面' : ''
    return
  }
  coverPreviewLoading.value = true
  coverPreviewError.value = ''
  try {
    const blob = await previewLibraryCover(lib.id, {
      template: coverTemplate.value,
      title: coverTitle.value,
      subtitle: coverSubtitle.value,
    })
    releaseCoverPreview()
    coverPreviewObjectUrl = URL.createObjectURL(blob)
    coverPreviewUrl.value = coverPreviewObjectUrl
    coverPreviewError.value = ''
  } catch (e) {
    // 生成不出来是常态（库还没刮削出图），给一句话说明而不是弹错
    coverPreviewUrl.value = ''
    coverPreviewError.value = e instanceof Error ? e.message : '预览失败'
  } finally {
    coverPreviewLoading.value = false
  }
}

async function saveCoverConfig() {
  const lib = libFormTarget.value
  if (!lib || !coverTemplate.value) return
  coverSaving.value = true
  try {
    await renderLibraryCover(lib.id, {
      template: coverTemplate.value,
      title: coverTitle.value,
      subtitle: coverSubtitle.value,
    })
    ElMessage.success('封面已重新生成并保存')
    await load()
    await loadCoverPreview()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '封面生成失败')
  } finally {
    coverSaving.value = false
  }
}

async function regenerateCover() {
  const lib = libFormTarget.value
  if (!lib) return
  coverRegenerating.value = true
  try {
    await regenerateLibraryCover(lib.id)
    ElMessage.success('已按最新入库的海报重新生成')
    await load()
    await loadCoverPreview()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '重新生成失败')
  } finally {
    coverRegenerating.value = false
  }
}

/** 关表单时顺手回收 blob URL */
watch(coverTemplate, () => { if (coverTemplate.value) loadCoverPreview() })

interface LibOption { value: string; label: string; hint: string; recommended?: boolean }

/** 内容类型：每一项都写清「选了会发生什么」 */
const COLLECTION_TYPES: LibOption[] = [
  { value: 'movies', label: '电影', hint: '一个文件一部片：客户端只给「播放」，没有季 / 集' },
  { value: 'tvshows', label: '剧集', hint: '按「剧名/季/集」目录组织：客户端能选季选集，刮削按剧集算' },
  { value: 'music', label: '音乐', hint: '按「歌手/专辑」组织，刮削走音乐元数据（TMDB 音乐条目少）' },
  { value: 'mixed', label: '混合', hint: '电影剧集放一起：能扫进来，但客户端分不出季集，只适合临时合并' },
]

/** 刮削策略：决定一轮扫描要不要回头重刮「已经刮过」的条目（直接影响 TMDB 配额） */
const POLICIES: LibOption[] = [
  { value: 'missing_only', label: '仅缺失时刮削', hint: '只补缺图 / 缺简介的条目，刮过的不再请求 TMDB —— 最省配额', recommended: true },
  { value: '3m', label: '3 个月重刮', hint: '顺手修正三个月前刮错的信息（换主图、补 IMDb / 别名）' },
  { value: '6m', label: '半年重刮', hint: '半年回头重刮一次；条目多时配额开销明显' },
  { value: '1y', label: '一年重刮', hint: '一年一次大修：适合整站换源 / 换刮削源之后' },
  { value: 'all', label: '全部重刮', hint: '每轮扫描把所有条目重刮一遍，最干净也最耗配额，别常开' },
]

function optionOf(list: LibOption[], value: string | null | undefined): LibOption | undefined {
  return list.find((o) => o.value === value)
}

/** 下拉收起时也能看见「（推荐）」，不然推荐项藏在面板里没人知道 */
function optionLabel(list: LibOption[], value: string | null | undefined): string {
  const hit = optionOf(list, value)
  return hit ? (hit.recommended ? `${hit.label}（推荐）` : hit.label) : ''
}

// ---- 扫描策略开关（v2.44.0）----
/** 增量扫描：走目录/文件指纹，没变动的目录不重扫 */
const incrementalScan = ref(true)
/** 本机目录实时监听：新片入库自动触发增量扫描，不用等定时扫描 */
const fsWatch = ref(true)
/** 实时监听健康状况（降级原因要让人看见：默默不工作比报错更糟） */
const fsWatchStatus = ref<Awaited<ReturnType<typeof fetchFsWatchStatus>> | null>(null)
const fsWatchForThisLib = computed(() => {
  const id = libForm.id
  if (id == null || !fsWatchStatus.value) return null
  if (!fsWatchStatus.value.available) return '未安装监听组件，已改用定时扫描'
  const reason = fsWatchStatus.value.degraded[String(id)]
  return reason ? `${reason}（已改用定时扫描）` : null
})

/** 后端标签：优先用后端下发的（本地文件 / Rclone / 115 网盘），没下发时回落到标识本身 */
function backendLabelOf(backend: string): string {
  return storageBackends.value.find((b) => b.value === backend)?.label || backend
}

/** 后端下发的存储后端清单（老后端没有这个字段时用固定的三项） */
const storageBackends = ref<StorageBackend[]>([
  { value: 'local', label: '本地文件' },
  { value: 'rclone', label: 'Rclone' },
  { value: '115', label: '115 网盘' },
])

/** 单机单服时隐藏「归属服」：只有一个选项时下拉框只会传递「没必要选」的信号。
 *  注意：「归属播放节点」不隐藏——即使只有一个节点，用户也需要能显式分配，
 *  否则库会一直处于「未分配」状态（node_id 为空 vs 明确指向该节点是两种语义）。 */
const isSingleRealm = computed(() => realmOptions.value.length <= 1)

/** 这个库是不是真的需要 115？只看「有没有 115 网盘来源」——
 *  纯本机目录的库也摆个 115 账号选择器，只会让人以为要用 115（§十一 ⑨）。
 *  新建库还没加路径时不显示；等真加了一条 115 路径它自己会出现。 */
const libraryNeeds115 = computed(() => (
  libForm.path_entries.some((e) => e.backend === '115')
))

/** 库里的路径条目（后端已把 mount:// 拆成「挂载内路径 + 存储后端」；老后端则按前缀现推） */
function pathEntriesOf(l: EmbyLibrary): LibraryPathEntry[] {
  if (l.path_entries?.length) return l.path_entries
  return (l.paths || []).map((raw) => {
    const backend = raw.startsWith('115:/') ? '115' : raw.startsWith('rclone:') ? 'rclone' : 'local'
    return {
      path: raw,
      backend,
      backend_label: backendLabelOf(backend),
      mount_id: null,
      mount_name: '',
      source: raw.startsWith('/') ? 'local' : 'prefix',
      raw,
    }
  })
}

// ---- 打开 / 还原 / 保存 ----

/** 表单指纹：只认「规范化后」的差异（路径去空去重），空改空格不算改动 */
function formFingerprint(): string {
  return JSON.stringify({
    name: libForm.name.trim(),
    collection_type: libForm.collection_type,
    is_enabled: libForm.is_enabled,
    realm_id: libForm.realm_id,
    node_id: libForm.node_id,
    scrape_policy: libForm.scrape_policy,
    account_115_id: libForm.account_115_id,
    paths: libForm.path_entries.map((e) => `${e.backend}|${e.mount_id ?? ''}|${e.path}`),
    // 封面“自动更新”开关没有单独的保存按钮，是跟着表单存的 —— 不进指纹的话
    // 只改它时 libFormDirty 为 false，“保存”一直灰着，开关就永远存不进去
    cover_auto_regen: coverAutoRegen.value,
    incremental_scan: incrementalScan.value,
    fs_watch: fsWatch.value,
    chase: libForm.chase,
  })
}

const libFormDirty = computed(
  () => libFormVisible.value && formFingerprint() !== libFormFingerprintAtOpen.value,
)

/** 路径改没改：改了就必须重新扫描一次才生效（扫描任务用配置快照，见 update_library） */
const libFormPathsChanged = computed(() => {
  if (!libFormDirty.value) return false
  const before = JSON.parse(libFormFingerprintAtOpen.value || '{}')
  return JSON.stringify(before.paths) !== JSON.stringify(
    libForm.path_entries.map((e) => `${e.backend}|${e.mount_id ?? ''}|${e.path}`))
})

function openCreate() {
  libFormTarget.value = null
  Object.assign(libForm, emptyLibForm())
  // 封面几项也要清干净：新建库不该继承上一个库的样式/标题/自动更新开关
  coverTemplate.value = ''
  coverTitle.value = ''
  coverSubtitle.value = ''
  coverAutoRegen.value = false
  coverPreviewUrl.value = ''
  coverPreviewError.value = ''
  libFormVisible.value = true
  libFormFingerprintAtOpen.value = formFingerprint()
}

function openSettings(l: EmbyLibrary) {
  libFormTarget.value = l
  // 封面：先回填已存配置，再拉一张预览（走 blob，不占 cover_path）
  coverTemplate.value = (l.cover_template || '') as typeof coverTemplate.value
  coverTitle.value = l.cover_title || ''
  coverSubtitle.value = l.cover_subtitle || ''
coverAutoRegen.value = l.cover_auto_regen === true
  incrementalScan.value = l.incremental_scan !== false
  fsWatch.value = l.fs_watch !== false
  if (!fsWatchStatus.value) void fetchFsWatchStatus().then((s) => { fsWatchStatus.value = s }).catch(() => undefined)
  if (coverTemplate.value) void loadCoverPreview()
  else { coverPreviewUrl.value = ''; coverPreviewError.value = '' }
  Object.assign(libForm, {
    id: l.id,
    name: l.name,
    collection_type: l.collection_type || 'movies',
    is_enabled: l.is_enabled !== false,
    realm_id: l.realm_id ?? null,
    node_id: l.node_id ?? null,
    scrape_policy: l.scrape_policy || 'missing_only',
    account_115_id: l.account_115_id ?? null,
    path_entries: pathEntriesOf(l).map((e) => ({ ...e })),
    chase: chaseExcluded(l.id),
  })
  libFormVisible.value = true
  libFormFingerprintAtOpen.value = formFingerprint()
}

/** 还原：回到打开时的样子（追新监听是即时保存的，不在这份还原范围内） */
function revertForm() {
  if (libFormTarget.value) openSettings(libFormTarget.value)
  else openCreate()
  ElMessage.info('已还原为打开时的配置')
}

/** 抽屉里还有没保存的改动时拦一下：关抽屉不等于想丢掉刚才填的 */
function beforeCloseForm(done: () => void) {
  if (!libFormDirty.value) {
    done()
    return
  }
  ElMessageBox.confirm('表单里有还没保存的改动，放弃吗？', '放弃改动', {
    type: 'warning',
    confirmButtonText: '放弃改动',
    cancelButtonText: '继续编辑',
  }).then(() => done()).catch(() => undefined)
}

/** 「取消」按钮走同一条拦截（直接改 v-model 不会触发 before-close） */
function cancelForm() {
  beforeCloseForm(() => {
    libFormVisible.value = false
    libFormTarget.value = null
  })
}

/** 追新开关：即时代理到全局清单，失败弹回原状态（toggleChase 里处理） */
function onChaseSwitch(v: unknown) {
  // 开关现在是「监听 / 排除」，而排除清单是反着存的：开关为真 = 监听 = 不排除
  toggleChase(!v)
}

/**
 * 保存：新建走 POST、编辑走 PUT（同一个鉴权接口，一次提交全部字段）。
 * 路径类改动后端会回 rescan_required —— 扫描任务用的是配置快照，不重扫就还是老路径。
 *
 * 注意**不下发 mount_ids**（v2.46.0）：挂载已经写在每条 path_entry 上了，再单传一份
 * 只会两处说法打架。后端对省略 = 不修改，所以老库原来挂着的挂载不会被清掉。
 */
async function saveForm(thenScan = false) {
  const editingId = libForm.id
  const name = libForm.name.trim()
  const pathEntries = libForm.path_entries.map((e) => ({
    path: e.path,
    backend: e.backend,
    mount_id: e.mount_id ?? null,
  }))
  const virtual = !!libFormTarget.value?.is_virtual
  if (!name) {
    ElMessage.warning('请填写媒体库名称')
    return
  }
  if (!virtual && !pathEntries.length && !legacyMountIds.value.length) {
    ElMessage.warning('请至少添加一个媒体路径，否则扫不到任何内容')
    return
  }
  libFormSaving.value = true
  try {
    let savedId: number | null = null
    if (editingId == null) {
      const res = await createLibrary({
        name,
        collection_type: libForm.collection_type,
        paths: [],
        path_entries: pathEntries,
        scrape_policy: libForm.scrape_policy,
        account_115_id: libForm.account_115_id ?? undefined,
        realm_id: libForm.realm_id ?? undefined,
        node_id: libForm.node_id ?? undefined,
        cover_template: coverTemplate.value || null,
        cover_title: coverTitle.value || null,
        cover_subtitle: coverSubtitle.value || null,
        cover_auto_regen: coverAutoRegen.value,
        incremental_scan: incrementalScan.value,
        fs_watch: fsWatch.value,
      })
      savedId = res.id
      const hasSource = pathEntries.length > 0
      ElMessage.success(`媒体库「${name}」已创建${hasSource ? '，可以扫一次了' : ''}`)
    } else {
      // 虚拟库没有自己的目录与挂载（也不归属某台节点），这几个字段干脆不传：
      // 传空数组等于「清空来源」，没必要为看不出来的库担这个风险
      const res = await updateLibrary(editingId, {
        name,
        collection_type: libForm.collection_type,
        is_enabled: libForm.is_enabled,
        scrape_policy: libForm.scrape_policy,
        // 传 null 表示解绑（回退默认账号）；省略会被 axios 丢掉，等于不改
        account_115_id: libForm.account_115_id ?? null,
        realm_id: libForm.realm_id ?? null,
        ...(virtual
          ? {}
          : { path_entries: pathEntries, node_id: libForm.node_id ?? null }),
        cover_template: coverTemplate.value || null,
        cover_title: coverTitle.value || null,
        cover_subtitle: coverSubtitle.value || null,
        cover_auto_regen: coverAutoRegen.value,
        incremental_scan: incrementalScan.value,
        fs_watch: fsWatch.value,
      })
      savedId = editingId
      ElMessage.success(res?.rescan_required
        ? `「${name}」配置已保存（路径 / 归属类改动要重新扫描后才对扫描生效）`
        : `「${name}」配置已保存`)
    }
    libFormVisible.value = false
    libFormTarget.value = null
    await load()
    if (thenScan && savedId != null) {
      const target = libraries.value.find((x) => x.id === savedId)
      if (target) await scan(target)
    }
  } catch {
    // 失败提示由全局拦截器给出（带着后端的 detail，如「目录不存在或不可读」）；
    // 这里不重复弹，表单内容原样留着让管理员改完再存
  } finally {
    libFormSaving.value = false
  }
}

// ---- 目录变更监听（追新）：全局开关 + 每库一个「纳不纳入」 ----

/** 追新**排除**清单的库 id（v2.45.0 起）。空 = 全部启用库都监听 */
function chaseExcludedIds(): number[] {
  return (chaseNew.value?.excluded || '')
    .split(/[,，\n]/)
    .map((x) => Number(x.trim()))
    .filter((x) => Number.isInteger(x) && x > 0)
}

/** 这个库是否被排除在追新之外 */
function chaseExcluded(id: number | null): boolean {
  return id != null && chaseExcludedIds().includes(id)
}

/**
 * 把当前库挪进 / 挪出**排除清单**（改的是全局配置里的一行，保存即生效）
 *
 * 语义是「排除」而不是「纳入」：以前想关掉一个库，得先去别的库上打开开关、让包含
 * 清单被写出来，再回来把它删掉。现在直接加进排除清单即可，不需要任何前置步骤。
 */
async function toggleChase(on: boolean) {
  const cfg = chaseNew.value
  if (!cfg || libForm.id == null) return
  const ids = new Set(chaseExcludedIds())
  if (on) ids.add(libForm.id)
  else ids.delete(libForm.id)
  chaseLibSaving.value = true
  try {
    const res = await saveChaseNew(cfg.enabled, cfg.interval, [...ids].sort((a, b) => a - b).join(','), cfg.strm_enabled)
    chaseNew.value = normalizeChase(res)
    libForm.chase = on
    // 追新是即时保存的，不算「未保存的改动」——只把指纹里的这一项对齐，
    // 其余字段的改动状态要原样留着
    const before = JSON.parse(libFormFingerprintAtOpen.value || '{}')
    before.chase = on
    libFormFingerprintAtOpen.value = JSON.stringify(before)
    ElMessage.success(on
      ? `「${libFormTarget.value?.name || '本库'}」已排除追新，不再自动扫描新文件`
      : `已重新纳入追新监听（每 ${res.interval} 分钟检查一次新文件）`)
  } catch {
    // 拦截器已提示；开关弹回去，别留一个「看起来生效了」的假状态
    libForm.chase = !on
  } finally {
    chaseLibSaving.value = false
  }
}

// ---- 卡片上的一句话策略摘要（不用点进设置就知道这个库怎么跑的） ----

function policyLabel(value: string | null | undefined): string {
  return optionOf(POLICIES, value)?.label || '仅缺失时刮削'
}

/** 这个库在不在追新轮询里；不在也要说清是「追新没开」还是「就它没参与」 */
function chaseFact(l: EmbyLibrary): string {
  const cfg = chaseNew.value
  // 配置没读到时不能说「追新关」——那是在编一个结论
  if (!cfg) return '追新状态未知'
  if (!cfg.enabled) return '追新关'
  const covered = !chaseExcluded(l.id)
  // 措辞跟着新语义走：不在排除清单 = 在听；在清单里 = 已排除追新
  return covered ? `追新每 ${cfg.interval} 分钟` : '已排除追新'
}

/** 老库上的库级挂载拼成一句话（卡片与悬停全文共用的口径） */
function legacyMountNamesFor(l: EmbyLibrary): string {
  return mountNamesOf(l.mount_ids || [])
}

function libSummary(l: EmbyLibrary): string {
  if (l.is_virtual) return `虚拟库 · ${l.platform || '按平台聚合'} · 刮削 ${policyLabel(l.scrape_policy)}`
  const entries = pathEntriesOf(l)
  const pathBit = entries.length
    ? `${entries.length} 个路径${entries[0] ? `（${entries[0].path}）` : ''}`
    : (l.mount_ids?.length ? '无本机路径 · 走挂载' : '未配路径')
  return [pathBit, chaseFact(l), `刮削 ${policyLabel(l.scrape_policy)}`].join(' · ')
}

/** 摘要的悬停全文：路径逐条列全（卡片上只留一行，看全靠悬停） */
function libSummaryTitle(l: EmbyLibrary): string {
  const lines: string[] = []
  if (l.is_virtual) {
    lines.push(`虚拟库：按发行平台「${l.platform || '—'}」聚合，没有自己的目录`)
  } else if (pathEntriesOf(l).length) {
    lines.push(`媒体路径（${pathEntriesOf(l).length}）：`)
    lines.push(...pathEntriesOf(l).map(
      (e) => `· ${e.path}（${e.backend_label || e.backend}）`))
  } else {
    lines.push(l.mount_ids?.length
      ? '媒体路径：未配置（内容来自下方只读的挂载来源）'
      : '媒体路径：未配置本机路径')
  }
  if (l.mount_ids?.length) {
    lines.push(`挂载来源（只读，v2.46.0 起不再能编辑）：${legacyMountNamesFor(l)}`)
  }
  lines.push(`追新监听：${chaseFact(l)}`)
  lines.push(`刮削策略：${policyLabel(l.scrape_policy)}`)
  return lines.join('\n')
}


async function load() {
  loading.value = true
  try {
    // 归属服下拉要用服的清单（Layout 已加载过就不重复请求）
    if (!realm.loaded) realm.load().catch(() => undefined)
    const [l, r, a, m, srv, reach] = await Promise.all([
      fetchLibraries(),
      fetchRepairQueue().catch(() => ({ total: 0, items: [] })),
      fetchPan115Accounts().catch(() => ({ accounts: [], env_cookie_configured: false })),
      fetchMounts().catch(() => ({ mounts: [], mount_types: [] })),
      fetchServers().catch(() => null),
      fetchReachability().catch(() => null),
      loadTmdbStatus().catch(() => undefined),
      loadAutoScanConfig(),
      loadChaseNewConfig().catch(() => undefined),
      loadStrmConfig().catch(() => undefined),
      loadTmdbLanguageConfig().catch(() => undefined),
      loadEnrichEpisodeNightConfig().catch(() => undefined),
    ])
    // mount_ids 兼容旧响应（老后端没有这个字段）
    libraries.value = l.libraries.map((lib) => ({ ...lib, mount_ids: lib.mount_ids || [] }))
    if (l.storage_backends?.length) storageBackends.value = l.storage_backends
    void loadCoverImages(libraries.value)
    repairCount.value = r.total
    panAccounts.value = a.accounts
    mounts.value = m.mounts
    nodes.value = (srv?.servers || []).filter((x) => x.kind === 'ea')
    reachability.value = reach
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
  pollQueue()
  // 队列与进度都是秒级的东西：页面开着（且在前台）就轮询；空闲时请求极小，且不刷整页列表
  if (!document.hidden) startQueuePolling()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  stopQueuePolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
  pendingReloads.forEach((id) => window.clearTimeout(id))
  pendingReloads.clear()
  Object.values(coverUrls.value).forEach((url) => URL.revokeObjectURL(url))
  releaseCoverPreview()
})

async function loadCoverImages(rows: EmbyLibrary[]) {
  const version = ++coverLoadVersion
  Object.values(coverUrls.value).forEach((url) => URL.revokeObjectURL(url))
  coverUrls.value = {}
  await Promise.all(rows.filter((lib) => lib.cover_url).map(async (lib) => {
    try {
      const blob = await fetchLibraryCover(lib.id)
      if (version !== coverLoadVersion) {
        URL.revokeObjectURL(URL.createObjectURL(blob))
        return
      }
      coverUrls.value[lib.id] = URL.createObjectURL(blob)
    } catch {
      // 封面是可选增强；读取失败不影响媒体库卡片其余信息
    }
  }))
}

function replaceCoverImage(id: number, blob: Blob) {
  const old = coverUrls.value[id]
  if (old) URL.revokeObjectURL(old)
  coverUrls.value[id] = URL.createObjectURL(blob)
}

async function uploadCover(l: EmbyLibrary, file: File) {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
    ElMessage.warning('封面仅支持 JPG、PNG 或 WebP 图片')
    return
  }
  if (file.size > 8 * 1024 * 1024) {
    ElMessage.warning('封面图片不能超过 8 MB')
    return
  }
  coverUploading.value[l.id] = true
  try {
    const result = await uploadLibraryCover(l.id, file)
    replaceCoverImage(l.id, await fetchLibraryCover(l.id))
    l.cover_url = result.cover_url
    ElMessage.success('媒体库封面已更新')
  } finally {
    coverUploading.value[l.id] = false
  }
}

function requestCoverUpload(l: EmbyLibrary, options: UploadRequestOptions) {
  return uploadCover(l, options.file)
}

async function removeCover(l: EmbyLibrary) {
  try {
    await ElMessageBox.confirm(
      `移除「${l.name}」的自定义封面？移除后显示默认封面，可随时重新上传。`,
      '移除媒体库封面',
      { type: 'warning', confirmButtonText: '确认移除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await removeLibraryCover(l.id)
  const old = coverUrls.value[l.id]
  if (old) URL.revokeObjectURL(old)
  delete coverUrls.value[l.id]
  l.cover_url = null
  ElMessage.success('媒体库封面已移除')
}

async function generateVirtual() {
  virtualLoading.value = true
  try {
    const res = await generateVirtualLibraries({ enabled: true })
    ElMessage.success(`虚拟媒体库：新建 ${res.created.length}、更新 ${res.updated.length}`)
    if (res.created.length === 0 && res.updated.length === 0) {
      ElMessage.info('当前库里还没有识别到发行平台标签（NF / DSNP / ATVP …）')
    }
    load()
  } finally {
    virtualLoading.value = false
  }
}

async function repairNow() {
  const res = await runRepairQueue()
  const merged = res.already?.length || 0
  ElMessage.success(`已把 ${res.libraries.length} 个库加入扫描队列${merged ? `（另 ${merged} 个已在队列中，已合并）` : ''}`)
  await pollQueue()
  reloadSoon(2000)
}

async function scan(l: EmbyLibrary, full = false) {
  const res = await scanLibrary(l.id, full)
  // 三件事要分清（v2.27.0）：已开扫 / 排在队列第几位（在等哪个挂载）/ 重复点击被合并
  if (res.already) ElMessage.info(`「${l.name}」${res.message || '已在扫描 / 已在队列中'}`)
  else if (res.started === false) ElMessage.warning(`「${l.name}」${res.message || '已加入扫描队列'}`)
  else ElMessage.success(`「${l.name}」${full ? '全量扫描已启动' : '扫描已启动'}`)
  await pollQueue()
  reloadSoon(1500)
}

/**
 * 手动全量扫描：无视所有指纹，把每个文件重新处理一遍。
 *
 * 平时不需要——增量扫描本来就是对的；这是给“指纹异常（改了文件但扫出来还是旧数据）”
 * 用的后门。所以二次确认里写清楚代价：条目多的大库可能要跑很久。
 */
async function scanFull(l: EmbyLibrary) {
  try {
    await ElMessageBox.confirm(
      `全量扫描会无视增量指纹，把「${l.name}」里的每个文件重新处理一遍。` +
      `条目多的大库可能要跑很久。确定吗？`,
      '全量扫描',
      { type: 'warning', confirmButtonText: '开始全量扫描', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await scan(l, true)
}

/**
 * 一键扫描全部：把所有启用的库按顺序加入扫描队列（增量扫描）。
 * 新用户挂载后点这个，不用一个个点 11 次。
 * 正在扫的库会自动跳过，不会重复加入。
 */
async function scanAll() {
  const enabledCount = libraries.value.filter(l => l.is_enabled !== false).length
  if (enabledCount === 0) {
    ElMessage.warning('没有启用的媒体库，先去创建一个吧')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将把 ${enabledCount} 个启用的库按顺序加入扫描队列（增量扫描，只处理新增/改过的文件）。` +
      `正在扫的库会自动跳过。全部扫完可能需要较长时间，确定吗？`,
      '一键扫描全部',
      { type: 'info', confirmButtonText: '开始扫描', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const res = await scanAllLibraries()
    const parts = [`已加入 ${res.queued_count} 个库的扫描队列`]
    if (res.already_count > 0) parts.push(`${res.already_count} 个正在扫/已在队列（已跳过）`)
    if (res.skipped_count > 0) parts.push(`${res.skipped_count} 个已停用/虚拟（已跳过）`)
    ElMessage.success(parts.join('，'))
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '一键扫描失败')
  }
  await pollQueue()
  reloadSoon(1500)
}

// ==================== 元数据与刮削 ====================
// 定时扫描：开关 + 每天几点扫，全部由用户在后台决定（默认关闭）
const autoScan = ref<AutoScanConfig | null>(null)
const autoScanSaving = ref(false)

/** 定时扫描配置读取失败（区分「读取中」与「读不到」） */
const autoScanError = ref(false)

async function loadAutoScanConfig() {
  try {
    const res = await fetchAutoScan()
    autoScan.value = { enabled: res.enabled, time: res.time, last_run: res.last_run }
    autoScanError.value = false
  } catch {
    autoScan.value = null // 出错不挡页面其它内容
    autoScanError.value = true
  }
}

// 追新：开关 + 轮询间隔（分钟），默认关闭
const chaseNew = ref<ChaseNewConfig | null>(null)
/** 追新配置读取失败（区分「读取中」与「读不到」，两者给的提示不一样） */
const chaseNewError = ref(false)

/** 运行记录里的 ISO 时间 → 「YYYY-MM-DD HH:MM:SS」 */
function fmtRunTime(s: string | null | undefined): string {
  return s ? s.slice(0, 19).replace('T', ' ') : '—'
}

/** 接口响应 → 页面状态：老后端没有 drive_changes / recent_runs / alerts 时补默认值（三处共用） */
function normalizeChase(res: ChaseNewConfig): ChaseNewConfig {
  return {
    enabled: res.enabled,
    interval: res.interval,
    // 老后端没有这个字段时按默认开处理——和后端的 CONFIG_STRM_ENABLED 默认 "1" 一致
    strm_enabled: res.strm_enabled ?? true,
    excluded: res.excluded,
    libraries: res.libraries,
    last_check: res.last_check,
    last_found: res.last_found,
    drive_changes: res.drive_changes || { running: false, last_poll: null, last_changes: 0, last_libs_triggered: 0 },
    recent_runs: res.recent_runs || [],
    alerts: res.alerts || [],
    total_found: res.total_found || 0,
  }
}
const chaseNewSaving = ref(false)

async function loadChaseNewConfig() {
  try {
    const res = await fetchChaseNew()
    chaseNew.value = normalizeChase(res)
    chaseNewError.value = false
  } catch {
    chaseNew.value = null
    chaseNewError.value = true
  }
}

async function saveChaseNewAction() {
  if (!chaseNew.value) return
  chaseNewSaving.value = true
  try {
    const res = await saveChaseNew(chaseNew.value.enabled, chaseNew.value.interval, chaseNew.value.excluded, chaseNew.value.strm_enabled)
    chaseNew.value = normalizeChase(res)
    ElMessage.success(res.enabled ? '追新已开启（每 ' + res.interval + ' 分钟）' : '追新已关闭')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    chaseNewSaving.value = false
  }
}

async function saveAutoScanAction() {
  if (!autoScan.value) return
  autoScanSaving.value = true
  try {
    const res = await saveAutoScan(autoScan.value.enabled, autoScan.value.time)
    autoScan.value = { enabled: res.enabled, time: res.time, last_run: res.last_run }
    ElMessage.success(`定时扫描已${res.enabled ? `开启（每天 ${res.time}）` : '关闭'}`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    autoScanSaving.value = false
  }
}

// ==================== .strm 直链目录配置 ====================
// .strm 是内容为播放直链的纯文本小文件；宿主机目录经 docker-compose 挂进容器。
// 三个配置项全部由管理员决定（默认启用=/opt/strm→/strm，与现状一致）；关闭总开关后
// 扫描不再收录 .strm 文件（已入库条目不动），保存即热生效、无需重启。
const strmConfig = ref<StrmConfig | null>(null)
const strmConfigSaving = ref(false)

async function loadStrmConfig() {
  try {
    const res = await fetchStrmConfig()
    strmConfig.value = { ...res.strm }
  } catch {
    strmConfig.value = null // 出错不挡页面其它内容
  }
}

async function saveStrmConfigAction() {
  if (!strmConfig.value) return
  strmConfigSaving.value = true
  try {
    const res = await saveStrmConfig({
      enabled: strmConfig.value.enabled,
      host_dir: strmConfig.value.host_dir,
      container_path: strmConfig.value.container_path,
    })
    strmConfig.value = { ...res.strm }
    ElMessage.success(`.strm 直链已${res.strm.enabled ? '开启' : '关闭'}`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    strmConfigSaving.value = false
  }
}

/** TMDB 密钥只读状态：密钥在「元数据来源」页填写，本页只回答「配了没」 */
const tmdbStatus = ref<TmdbKeysStatus | null>(null)

/** 手动识别入口前移（P1）：库卡片上的「识别」按钮直达元数据来源页 */
const router = useRouter()

/** 页内标签（v2.54）：媒体库 / 扫描队列 / 定时与刮削；?tab= 可深链，切换时同步回地址栏 */
const route = useRoute()
const EMBY_TABS = ['libraries', 'queue', 'schedule'] as const
type EmbyTab = (typeof EMBY_TABS)[number]
const initialTab = String(route.query.tab || '')
const activeTab = ref<EmbyTab>(
  (EMBY_TABS as readonly string[]).includes(initialTab) ? (initialTab as EmbyTab) : 'libraries',
)
function onTabChange(name: string | number) {
  const tab = String(name)
  router.replace({ query: { ...route.query, tab: tab === 'libraries' ? undefined : tab } })
}
/** 标签上的计数：正在跑 + 排队中（有数字才显示，0 不打扰） */
const queueActiveCount = computed(
  () => (scanQueue.value?.running.length || 0) + (scanQueue.value?.waiting.length || 0),
)
const enabledLibCount = computed(() => libraries.value.filter((l) => l.is_enabled).length)

/** 跳到「元数据来源」页并带上条目 ID：对方 onMounted 会自动填入并选中，跳过「搜条目」一步 */
function goIdentify(l: EmbyLibrary) {
  router.push({ name: 'MetadataSources', query: { item_id: String(l.id) } })
}

// ==================== TMDB 首选语言（P2c） ====================

/** TMDB 返回的简介/标题/别名用哪种语言：后台配置，保存即热生效 */
const tmdbLang = ref<TmdbLanguageStatus | null>(null)
const tmdbLangSaving = ref(false)

const tmdbLangLabel = (v: string) => TMDB_LANGUAGE_LABELS[v] || v

const tmdbLangError = ref(false)

async function loadTmdbLanguageConfig() {
  try {
    tmdbLang.value = await fetchTmdbLanguage()
    tmdbLangError.value = false
  } catch {
    tmdbLang.value = null // 出错不挡页面其它内容
    tmdbLangError.value = true
  }
}

async function saveTmdbLanguageAction() {
  if (!tmdbLang.value) return
  tmdbLangSaving.value = true
  try {
    const res = await saveTmdbLanguage(tmdbLang.value.language)
    tmdbLang.value.language = res.language
    ElMessage.success('TMDB 首选语言已保存，立即生效')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    tmdbLangSaving.value = false
  }
}

// ==================== 单集夜间刮削 ====================

/** 白天只刮顶层条目（电影/剧集），单集留到夜间窗口刮：后台配置，60 秒内热生效 */
const episodeNight = ref<EnrichEpisodeNightConfig | null>(null)
const episodeNightSaving = ref(false)
const episodeNightError = ref(false)

async function loadEnrichEpisodeNightConfig() {
  try {
    episodeNight.value = await fetchEnrichEpisodeNight()
    episodeNightError.value = false
  } catch {
    episodeNight.value = null // 出错不挡页面其它内容
    episodeNightError.value = true
  }
}

async function saveEnrichEpisodeNightAction() {
  if (!episodeNight.value) return
  episodeNightSaving.value = true
  try {
    const res = await saveEnrichEpisodeNight(
      episodeNight.value.enabled,
      episodeNight.value.night_start,
      episodeNight.value.night_end,
    )
    episodeNight.value = res
    ElMessage.success('单集夜间刮削已保存，60 秒内生效')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    episodeNightSaving.value = false
  }
}
const rescrapeVisible = ref(false)
const rescrapeTarget = ref<EmbyLibrary | null>(null)
const rescrapePolicy = ref<'missing_only' | 'all'>('missing_only')
const rescrapeLoading = ref(false)

const tmdbStatusText = computed(() => {
  if (!tmdbStatus.value) return '加载中…'
  if (!tmdbStatus.value.configured) return '未配置'
  return `已配置 ${tmdbStatus.value.count} 个（${tmdbStatus.value.source === 'env' ? '环境变量' : '后台填写'}）`
})

async function loadTmdbStatus() {
  try {
    tmdbStatus.value = await fetchTmdbKeys()
  } catch {
    tmdbStatus.value = null // 出错不挡页面其它内容
  }
}

function openRescrape(l: EmbyLibrary) {
  rescrapeTarget.value = l
  rescrapePolicy.value = 'missing_only'
  rescrapeVisible.value = true
}

async function confirmRescrape() {
  if (!rescrapeTarget.value) return
  rescrapeLoading.value = true
  try {
    const res = await rescrapeLibrary(rescrapeTarget.value.id, rescrapePolicy.value)
    rescrapeVisible.value = false
    if (res.already) ElMessage.info(`「${rescrapeTarget.value.name}」${res.message}`)
    else ElMessage.success(`「${rescrapeTarget.value.name}」${res.message}`)
    await pollQueue()
  } finally {
    rescrapeLoading.value = false
  }
}

/** 取消一个还在排队的扫描（正在跑的取不了：停在中途会留下半个库的状态） */
async function cancelQueued(t: EmbyScanTask) {
  await cancelQueuedScan(t.library_id)
  ElMessage.success(`已取消「${t.name}」的排队`)
  await pollQueue()
}

async function removeLib(l: EmbyLibrary) {
  try {
    await ElMessageBox.confirm(
      `删除媒体库「${l.name}」将同时移除其索引条目（不删除磁盘文件），确定吗？`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  await deleteLibrary(l.id)
  if (libFormTarget.value?.id === l.id) {
    libFormVisible.value = false
    libFormTarget.value = null
  }
  if (coverUrls.value[l.id]) {
    URL.revokeObjectURL(coverUrls.value[l.id])
    delete coverUrls.value[l.id]
  }
  ElMessage.success('已删除')
  load()
}

async function stopAll() {
  const res = await stopAllTranscodes()
  ElMessage.success(`已停止 ${res.stopped} 路转码`)
}

function fmtDate(s: string | null): string {
  if (!s) return '—'
  return s.slice(0, 16).replace('T', ' ')
}

// ---- 最近一次扫描的结果（后端已落库，刷新页面也还在）----

/** 扫描结果徽标：没扫过返回 null（不占位） */
function scanBadge(l: EmbyLibrary): { text: string; cls: string } | null {
  const s: EmbyScanResult | null | undefined = l.last_scan
  if (!s) return null
  if (s.status === 'running') return { text: '上次扫描未完成', cls: 'scanning' }
  if (s.status === 'failed') return { text: '扫描失败', cls: 'danger' }
  if (s.status === 'partial') return { text: '来源不完整', cls: 'warn' }
  return { text: '扫描正常', cls: 'ok' }
}

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

/** 部分失败 / 异常的原因（来源读不到时的 path + 原因） */
function scanError(l: EmbyLibrary): string {
  const s = l.last_scan
  if (!s || (s.status !== 'failed' && s.status !== 'partial')) return ''
  return s.error || (s.failed_roots || []).join('；')
}

const SCAN_STATUS_META: Record<EmbyScanStatus, { text: string; cls: string }> = {
  running: { text: '进行中', cls: 'scanning' },
  success: { text: '正常', cls: 'ok' },
  partial: { text: '来源不完整', cls: 'warn' },
  failed: { text: '失败', cls: 'danger' },
}

const SCAN_TRIGGER_LABELS: Record<string, string> = {
  manual: '面板',
  client: '客户端',
  node: '归属节点',
  repair: '修复队列',
}

function statusMeta(status: EmbyScanStatus): { text: string; cls: string } {
  return SCAN_STATUS_META[status] || { text: status, cls: 'muted' }
}

function triggerLabel(trigger: string | null): string {
  return trigger ? SCAN_TRIGGER_LABELS[trigger] || trigger : '—'
}

/** 流水里一轮的增量摘要（与卡片上的口径一致，失败时只显示原因） */
function runSummary(r: EmbyScanRun): string {
  const bits = [`+${r.added}`, `~${r.updated}`, `-${r.removed}`]
  // 复活只在真的发生时占位：它是「下架过的片又回来了」，没发生就不该增加噪音
  if (r.resurrected) bits.push(`↺${r.resurrected}`)
  return bits.join(' / ')
}

// ---- 按来源拆分（哪条来源扫到了什么）----

/** 悬停明细：一条来源一行（读不到 / 0 个文件 / 文件数 + 增量） */
function sourcesDetail(list: EmbyScanSource[] | undefined): string {
  return (list || []).map((s) => {
    if (s.error) return `${s.label}：读取失败 —— ${s.error}`
    if (!s.files) return `${s.label}：0 个文件`
    const bits = [`${s.files} 个文件`, `新增 ${s.added}`, `更新 ${s.updated}`]
    if (s.probed) bits.push(`探测 ${s.probed}`)
    if (s.unchanged) bits.push(`未变 ${s.unchanged}`)
    return `${s.label}：${bits.join(' / ')}`
  }).join('\n')
}

/** 来源列：条数 + 有几条一条文件都没扫到 */
function sourcesLabel(list: EmbyScanSource[] | undefined): string {
  if (!list?.length) return '—'
  const empty = emptySourceCount(list)
  return empty ? `${list.length} 条 · ${empty} 空` : `${list.length} 条`
}

/** 有没有「别的来源有文件、这条一条都没有」的来源（全空是整库为空，另当别论） */
function emptySourceCount(list: EmbyScanSource[] | undefined): number {
  const sources = list || []
  if (!sources.some((s) => s.files > 0)) return 0
  return sources.filter((s) => !s.error && !s.files).length
}

// ---- 扫描队列（v2.27.0）：排队中的、在跑的、刚跑完的 ----

/** 轮询队列快照；从「忙」变「闲」时顺手把列表刷一遍（『最近一次结果』是落库数据，不会自己变） */
async function pollQueue() {
  try {
    const q = await fetchScanQueue()
    scanQueue.value = q
    const busy = q.running.length > 0 || q.waiting.length > 0
    if (queueWasBusy && !busy) load()
    queueWasBusy = busy
  } catch {
    // 轮询失败不打扰（比如正在重新登录）：下一次接着来
  }
}

const queueBusy = computed(() => {
  const q = scanQueue.value
  return !!q && (q.running.length > 0 || q.waiting.length > 0)
})

/** 队列里还没结束的扫描：库 id → 任务（卡片直接看这个，不用刷整页） */
const liveTasks = computed(() => {
  const map = new Map<number, EmbyScanTask>()
  for (const t of [...(scanQueue.value?.running || []), ...(scanQueue.value?.waiting || [])]) {
    map.set(t.library_id, t)
  }
  return map
})

const queueHistory = computed(() => (scanQueue.value?.history || []).slice(0, 5))

/** v2.49.0: "最近完成"区域显示开关，存 localStorage */
const showScanHistory = ref(localStorage.getItem('aetrix_show_scan_history') !== '0')
watch(showScanHistory, (v) => {
  try { localStorage.setItem('aetrix_show_scan_history', v ? '1' : '0') } catch { /* ignore */ }
})

/** v2.49.0: 格式化扫描数量，如"扫描 2325 条 · 新增 50 条" */
function scanCountText(t: any): string {
  const added = Number(t.added || 0)
  const updated = Number(t.updated || 0)
  const removed = Number(t.removed || 0)
  const total = added + updated + removed
  const parts: string[] = []
  if (total > 0) parts.push(`扫描 ${total} 条`)
  if (added > 0) parts.push(`新增 ${added} 条`)
  return parts.join(' · ')
}
const queueHasContent = computed(() => !!scanQueue.value && (queueBusy.value || queueHistory.value.length > 0))

function liveFor(l: EmbyLibrary): EmbyScanTask | null {
  return liveTasks.value.get(l.id) || null
}

/** 挂载名字：面板写得出「在等谁」，不让管理员去存储来源页对 id */
function mountName(id: number): string {
  return scanQueue.value?.mount_names?.[String(id)] || `挂载 #${id}`
}

/** 一条任务的状态徽标（排队中带位置，扫描中带阶段） */
function taskBadge(t: EmbyScanTask): { text: string; cls: string } {
  if (t.state === 'queued') return { text: `排队中（第 ${t.position ?? '-'} 位）`, cls: 'muted' }
  if (t.state === 'running') return { text: `扫描中 · ${t.progress?.phase_label || '准备中'}`, cls: 'scanning' }
  if (t.state === 'canceled') return { text: '已取消', cls: 'muted' }
  if (t.state === 'failed' || t.result === 'failed') return { text: '失败', cls: 'danger' }
  if (t.result === 'partial') return { text: '完成（来源不完整）', cls: 'warn' }
  return { text: '完成', cls: 'ok' }
}

/** 为什么在等：同一远程挂载被别的库占着就写明是哪个 */
function waitingText(t: EmbyScanTask): string {
  const mounts = (t.waiting_for || []).map(mountName)
  if (mounts.length) return `在等挂载：${mounts.join('、')}`
  return (t.position ?? 1) > 1 ? '前面还有扫描在跑' : '等待调度'
}

/** 进度一行：已处理 / 已发现 / 耗时（扫到哪了一眼可见，不用看容器 CPU） */
function progressLine(t: EmbyScanTask): string {
  const p = t.progress
  if (!p) return ''
  const parts = [`已处理 ${p.processed}`]
  if (p.enumerated) parts.push(`已发现 ${p.enumerated}`)
  if (p.elapsed_ms) parts.push(fmtDuration(p.elapsed_ms))
  if (p.remote_lists) parts.push(`远程请求 ${p.remote_lists}`)
  return parts.join(' · ')
}

/** 卡片徽标：队列优先（排队中 / 扫描中 + 阶段）→ 归属节点在扫 → 上一次的结果 */
function cardBadge(l: EmbyLibrary): { text: string; cls: string } | null {
  const task = liveFor(l)
  if (task) return taskBadge(task)
  if (l.scan_live?.state === 'running') return { text: '扫描中（归属节点）', cls: 'scanning' }
  if (l.is_scanning) return { text: '扫描中…', cls: 'scanning' }
  return scanBadge(l)
}

/** 卡片的实时一行：排队原因，或「已处理 N · 当前目录」 */
function cardLiveHint(l: EmbyLibrary): string {
  const task = liveFor(l)
  if (!task) return l.scan_live?.state === 'running' ? (l.scan_live.message || '') : ''
  if (task.state === 'queued') return waitingText(task)
  return [progressLine(task), task.progress?.current].filter(Boolean).join(' · ')
}

function queuedSince(t: EmbyScanTask): string {
  return t.queued_ms ? `等了 ${fmtDuration(t.queued_ms)}` : ''
}

// ---- 播放可达性（v2.28.0）：面板扫描正常 ≠ 出流的那台机器拿得到内容 ----

/** 一条库的可达性提示（ok 不占地方，只提示 warn / bad） */
function libReach(l: EmbyLibrary): EmbyPlaybackReachability | null {
  const verdict = l.playback
  return verdict && verdict.level !== 'ok' ? verdict : null
}

const reachProblems = computed(() => (reachability.value?.libraries || []).filter((i) => i.level !== 'ok'))
/** 顶部横幅：只有真的有问题（或有待确认项）才占版面 */
const reachHasContent = computed(() => {
  const r = reachability.value
  return !!r && (r.level !== 'ok' || reachProblems.value.length > 0)
})

function reachCls(level: string): string {
  return level === 'bad' ? 'danger' : level === 'warn' ? 'warn' : 'ok'
}

function reachText(level: string): string {
  return { bad: '读不到', warn: '待确认', ok: '可达' }[level] || level
}

/** 卡片提示的悬停文案：原因 + 改法（卡片上只放一句，详情靠悬停） */
function reachTitle(l: EmbyLibrary): string {
  const verdict = libReach(l)
  return [verdict?.message, verdict?.fix].filter(Boolean).join(' ｜ ')
}

/** 横幅第一行的事实：谁在出流、面板协议面开着吗、用户该连哪个地址、EA 体检什么时候拉的 */
function reachFacts(): string[] {
  const r = reachability.value
  if (!r) return []
  const facts = [r.playback.label]
  facts.push(r.playback.gateway_enabled ? '面板协议面：开' : '面板协议面：关（分离部署）')
  facts.push(clientUrlFact(r))
  facts.push(r.ea_health_at ? `EA 体检：${fmtDate(r.ea_health_at)}` : 'EA 体检：还没拉过')
  return facts
}

/**
 * 用户该连哪个地址（v2.32.0）
 *
 * 还没配任何入口地址时（client_url_source = none）后端给的是空串：这时候用户该连的就是
 * **管理员此刻访问面板的这个地址**（面板与接口同源），所以直接报浏览器自己的 origin，
 * 而不是把写死的 localhost 摆在用户端地址上、再报一条红。
 * 配过地址（服地址 / 服务入口 / 环境变量）就照配置显示。
 */
function clientUrlFact(r: EmbyReachabilityReport): string {
  if (r.playback.client_url) return `用户端地址：${r.playback.client_url}`
  return `用户端地址：${window.location.origin}（按当前访问地址）`
}



// ---- 卡片facts：这个库由谁扫、内容从哪来、有多少条目（不用进库再点一层）----

/** 服务：归属节点（EA）在扫；没指定就是面板自己扫 */
function serviceFact(l: EmbyLibrary): { text: string; warn: boolean } {
  if (l.node_name) {
    const label = l.realm_name ? `${l.node_name}（${l.realm_name}）` : l.node_name
    return { text: label, warn: l.node_online === false }
  }
  if (l.realm_name) return { text: `面板 · ${l.realm_name}`, warn: false }
  return { text: '面板扫描', warn: false }
}

/** 来源：几条路径 + 几个挂载，后面直接跟「读不到 / 为空」 */
function sourceFact(l: EmbyLibrary): { text: string; warn: boolean } {
  const total = (l.paths?.length || 0) + (l.mount_ids?.length || 0)
  if (!total) return { text: l.is_virtual ? '虚拟库（无来源）' : '未配来源', warn: !l.is_virtual }
  const sources = l.last_scan?.sources || []
  const unavailable = sources.filter((s) => s.kind === 'unavailable').length
  const empty = emptySourceCount(sources)
  const parts = [`来源 ${total}`]
  if (unavailable) parts.push(`${unavailable} 条读不到`)
  else if (empty) parts.push(`${empty} 条为空`)
  return { text: parts.join(' · '), warn: unavailable > 0 || empty > 0 }
}

const scanColumns: DataColumn[] = [
  { key: 'started_at', label: '开始', width: 140, mobile: 'title' },
  { key: 'status', label: '结果', width: 110 },
  { key: 'trigger', label: '触发', width: 90 },
  { key: 'summary', label: '新增/更新/下架', width: 170 },
  { key: 'sources', label: '来源', width: 110 },
  { key: 'duration', label: '耗时', width: 90 },
  { key: 'error', label: '原因', minWidth: 160 },
]

async function openScans(l: EmbyLibrary) {
  scanTarget.value = l
  scanDrawer.value = true
  scanLoading.value = true
  scanRuns.value = []
  try {
    const res = await fetchLibraryScans(l.id)
    scanRuns.value = res.runs
    scanKeep.value = res.keep
  } catch {
    ElMessage.error('读取扫描记录失败')
  } finally {
    scanLoading.value = false
  }
}

function typeLabel(t: string): string {
  return { movies: '电影', tvshows: '剧集', music: '音乐', mixed: '混合' }[t] || t
}
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="媒体与交付"
      title="媒体库"
      description="点「新建媒体库」或卡片上的「设置」打开同一张配置表单：分组改完路径、归属、刮削与追新"
    >
      <template #actions>
        <el-button v-if="repairCount > 0" @click="repairNow">
          修复缺图（{{ repairCount }}）
        </el-button>
        <el-button :loading="virtualLoading" @click="generateVirtual">
          <Wand2 :size="14" style="margin-right: 4px" />生成平台虚拟库
        </el-button>
        <el-button @click="stopAll">
          <Square :size="13" style="margin-right: 4px" />停止全部转码
        </el-button>
        <el-button type="primary" @click="openCreate">
          <FolderPlus :size="15" style="margin-right: 4px" />新建媒体库
        </el-button>
        <el-button :loading="loading" aria-label="刷新" title="刷新" @click="load">
          <RefreshCw :size="15" />
        </el-button>
      </template>
    </PageHeader>

    <!--
      播放可达性（v2.28.0）：面板扫描没问题 ≠ 出流的机器拿得到内容。
      分离部署（EM 控制面 + EA 数据面 + 共享 WebDAV/rclone）下，内容只存在于面板那台机器
      上时（本机目录 / local 挂载），客户端会看得到条目却播不了；这里提前说清楚。
    -->
    <SectionCard
      v-if="reachHasContent && reachability"
      class="reach-card"
      :icon="ShieldAlert"
      :tone="reachability.level === 'ok' ? 'default' : 'accent'"
    >
      <template #title>
        播放可达性
        <span class="mini-badge" :class="reachCls(reachability.level)">
          {{ reachText(reachability.level) }}
        </span>
      </template>
      <template #actions>
        <div class="queue-facts">
          <span v-for="f in reachFacts()" :key="f" class="fact">{{ f }}</span>
        </div>
      </template>

      <!-- 用户端该连哪个地址（分离部署最容易配错的一处） -->
      <div v-if="reachability.client_endpoint.level !== 'ok'" class="reach-row">
        <span class="mini-badge" :class="reachCls(reachability.client_endpoint.level)">
          {{ reachText(reachability.client_endpoint.level) }}
        </span>
        <div class="reach-row-body">
          <div class="reach-row-title">用户端地址</div>
          <div class="queue-row-sub" :class="{ danger: reachability.client_endpoint.level === 'bad', warn: reachability.client_endpoint.level === 'warn' }">
            {{ reachability.client_endpoint.message }}
          </div>
          <div v-if="reachability.client_endpoint.fix" class="reach-row-fix">
            {{ reachability.client_endpoint.fix }}
          </div>
        </div>
      </div>

      <!-- 逐库：哪个库在出流的那台机器上拿不到内容、怎么改 -->
      <div v-for="p in reachProblems" :key="'reach-' + p.library_id" class="reach-row">
        <span class="mini-badge" :class="reachCls(p.level)">{{ reachText(p.level) }}</span>
        <div class="reach-row-body">
          <div class="reach-row-title">
            <span class="reach-name">{{ p.library_name }}</span>
            <span class="reach-node">{{ p.targets.length ? p.targets.join('、') : p.playback_label }}</span>
          </div>
          <div class="queue-row-sub" :class="{ danger: p.level === 'bad', warn: p.level === 'warn' }">
            {{ p.message }}
          </div>
          <div v-if="p.fix" class="reach-row-fix">{{ p.fix }}</div>
        </div>
      </div>
    </SectionCard>

    <!--
      v2.54：这一页原来是四张卡竖着堆（队列 / 定时刮削 / 一键扫描说明 / 库卡片），
      真正要找的库卡片被压在一屏以下。拆成三个标签：媒体库（默认）/ 扫描队列 / 定时与追新。
      ?tab= 深链可直达（如 /emby?tab=queue）。
    -->
    <el-tabs v-model="activeTab" class="emby-tabs" @tab-change="onTabChange">
      <el-tab-pane name="libraries">
        <template #label>
          <span class="tab-label">媒体库<span class="tab-count">{{ libraries.length }}</span></span>
        </template>
        <div class="lib-toolbar">
          <span class="lib-toolbar-meta">
            共 {{ libraries.length }} 个库<template v-if="enabledLibCount !== libraries.length">，启用 {{ enabledLibCount }} 个</template>
          </span>
          <div class="lib-toolbar-actions">
            <el-popover placement="bottom-end" :width="340" trigger="click">
              <template #reference>
                <el-button text>
                  <Info :size="14" style="margin-right: 4px" />扫描说明
                </el-button>
              </template>
              <div class="scan-help">
                <div>• <b>扫描</b>（每个库）：增量扫描，只处理新增/改过的文件，没变化的跳过。日常用这个。</div>
                <div>• <b>一键扫描全部</b>：把所有库各扫一次（增量）。新用户挂载后点这个。</div>
                <div>• <b>全量扫描</b>（每个库）：强制重处理该库每个文件，无视增量记录。出问题、修 bug 后才用，慢。</div>
              </div>
            </el-popover>
            <el-button
              type="primary"
              plain
              title="把所有启用的库各扫一次（增量扫描）。新用户挂载后点这个，不用一个个点。"
              @click="scanAll"
            >
              <ScanSearch :size="14" style="margin-right: 4px" />一键扫描全部
            </el-button>
          </div>
        </div>
        <div v-if="libraries.length" class="lib-grid">
          <article v-for="l in libraries" :key="l.id" class="lib-card">
            <div class="lib-cover">
              <img v-if="coverUrls[l.id]" :src="coverUrls[l.id]" :alt="`${l.name} 封面`" />
              <div v-else class="lib-cover-empty">
                <Film :size="32" />
                <span>{{ typeLabel(l.collection_type) }}库</span>
              </div>
              <div class="lib-cover-shade" />
              <div class="lib-cover-badges">
                <span v-if="l.is_virtual" class="mini-badge pin">虚拟库</span>
                <span class="mini-badge" :class="l.is_enabled ? 'ok' : 'off'">
                  {{ l.is_enabled ? '启用' : '停用' }}
                </span>
                <span v-if="cardBadge(l)" class="mini-badge" :class="cardBadge(l)?.cls">
                  {{ cardBadge(l)?.text }}
                </span>
              </div>
              <div class="lib-cover-actions">
                <el-upload
                  :accept="'image/jpeg,image/png,image/webp'"
                  :show-file-list="false"
                  :disabled="coverUploading[l.id]"
                  :http-request="(options: UploadRequestOptions) => requestCoverUpload(l, options)"
                >
                  <el-button
                    class="cover-button"
                    circle
                    text
                    :loading="coverUploading[l.id]"
                    :title="l.cover_url ? '更换封面' : '上传封面'"
                    aria-label="上传媒体库封面"
                  >
                    <ImagePlus :size="18" />
                  </el-button>
                </el-upload>
                <el-button
                  v-if="l.cover_url"
                  class="cover-button"
                  circle
                  text
                  title="移除封面"
                  aria-label="移除媒体库封面"
                  @click="removeCover(l)"
                >
                  <X :size="17" />
                </el-button>
              </div>
            </div>

            <div class="lib-body">
              <div class="lib-head">
                <span class="lib-name">{{ l.name }}</span>
                <span class="lib-meta">{{ typeLabel(l.collection_type) }}库</span>
              </div>

              <div class="lib-facts">
                <span class="fact" :class="{ warn: serviceFact(l).warn }" :title="'服务：' + serviceFact(l).text">
                  <Server :size="12" />{{ serviceFact(l).text }}
                </span>

                <span class="fact" :class="{ warn: sourceFact(l).warn }" :title="'来源：' + sourceFact(l).text">
                  <HardDrive :size="12" />{{ sourceFact(l).text }}
                </span>
                <span class="fact"><Film :size="12" />{{ l.item_count }} 个条目</span>
              </div>

              <!-- 一句话策略摘要：路径 / 轮询间隔 / 刮削策略，不点进设置也知道这个库怎么跑 -->
              <div class="lib-summary" :title="libSummaryTitle(l)">{{ libSummary(l) }}</div>

              <div class="lib-state">
                <span v-if="cardLiveHint(l)" class="lib-live" :title="cardLiveHint(l)">
                  <span class="scan-dot" :class="liveFor(l)?.state === 'queued' ? 'is-queued' : 'is-running'" />
                  {{ cardLiveHint(l) }}
                </span>
                <span v-else class="lib-time">
                  {{ l.last_scan_at ? `上次扫描 ${fmtDate(l.last_scan_at)}` : '尚未扫描' }}
                </span>
              </div>

              <div v-if="scanError(l)" class="scan-error" :title="scanError(l)">{{ scanError(l) }}</div>
              <div v-else-if="libReach(l)?.level === 'bad'" class="scan-error" :title="reachTitle(l)">
                播放风险：{{ libReach(l)?.message }}
              </div>
            </div>

            <div class="lib-foot">
              <el-button
                size="small"
                type="primary"
                plain
                title="增量扫描：只处理新增/改过的文件，没变化的跳过。日常用这个。"
                @click="scan(l)"
              >
                <ScanSearch :size="13" />扫描
              </el-button>
              <el-button
                size="small"
                plain
                title="全量扫描：强制重处理该库每个文件，无视增量记录。出问题、修 bug 后才用，慢。"
                @click="scanFull(l)"
              >
                <RefreshCcw :size="13" />全量扫描
              </el-button>
              <el-button size="small" plain title="刷新已有条目的元数据（不扫描新文件）" @click="openRescrape(l)">
                <RefreshCw :size="13" />刷新元数据
              </el-button>
              <el-button size="small" plain title="手动识别：跳到「元数据来源」页直接绑定 TMDB / IMDb ID" @click="goIdentify(l)">
                <Search :size="13" />识别
              </el-button>
              <el-button size="small" plain @click="openSettings(l)">
                <Settings2 :size="13" />设置
              </el-button>
              <el-button size="small" plain title="扫描记录" @click="openScans(l)">
                <History :size="13" />
              </el-button>
              <el-button size="small" type="danger" plain title="删除媒体库" @click="removeLib(l)">
                <Delete :size="13" />
              </el-button>
            </div>
          </article>

        </div>
        <EmptyState
          v-if="libraries.length === 0 && !loading"
          class="lib-empty"
          :icon="Film"
          title="暂无媒体库"
          description="新建一个媒体库，选好目录后扫描一次，条目就会出现在这里。"
        >
          <template #actions>
            <el-button type="primary" @click="openCreate">
              <FolderPlus :size="14" style="margin-right: 4px" />新建媒体库
            </el-button>
          </template>
        </EmptyState>
        <div v-else-if="loading && libraries.length === 0" class="lib-grid" aria-busy="true">
          <div v-for="n in 3" :key="n" class="au-skeleton lib-skeleton" />
        </div>
      </el-tab-pane>

      <el-tab-pane name="queue">
        <template #label>
          <span class="tab-label">
            扫描队列
            <span v-if="queueActiveCount" class="tab-count is-live">{{ queueActiveCount }}</span>
          </span>
        </template>
        <!-- 扫描队列：同一远程挂载同时只跑一个扫描，其它库在这里排队（不再让管理员自己控并发） -->
        <SectionCard v-if="queueHasContent" class="queue-card" title="扫描队列" :icon="ListChecks">
          <template #actions>
            <div class="queue-facts">
              <span class="fact">并发上限 {{ scanQueue?.max_parallel }}</span>
              <span class="fact">{{ scanQueue?.mount_serial ? '同一远程挂载串行' : '挂载串行已关闭' }}</span>
              <span class="fact" title="本轮真实远程请求 / 内存复用 / 在飞请求">
                远程请求 {{ scanQueue?.remote.lists }} · 复用 {{ scanQueue?.remote.reused }}
                · 在飞 {{ scanQueue?.remote.inflight }}
              </span>
              <!-- 扫描拆给 worker 执行后，本进程看不到它的内存队列：
                   这两列来自它写进库里的状态（进度按刷盘间隔更新），说清楚免得被当成实时值 -->
              <span
                v-if="scanQueue?.view === 'db'"
                class="fact"
                title="扫描由执行节点（worker）运行；「正在扫描」「最近完成」两列来自它写进数据库的状态，进度按刷盘间隔更新"
              >
                进度来自执行节点
              </span>
            </div>
          </template>

          <div class="queue-grid">
            <div class="queue-col">
              <div class="queue-col-title">正在扫描（{{ scanQueue?.running.length || 0 }}）</div>
              <div v-for="t in scanQueue?.running" :key="'run-' + t.library_id" class="queue-row">
                <div class="queue-row-head">
                  <span class="queue-name">{{ t.name }}</span>
                  <span class="mini-badge scanning">{{ t.progress?.phase_label || '准备中' }}</span>
                  <span
                    v-if="t.via === 'db'"
                    class="mini-badge muted"
                    title="本轮由执行扫描的节点运行；这里显示的是它写进数据库的进度快照"
                  >执行节点</span>
                </div>
                <div class="queue-row-sub mono">{{ progressLine(t) || '刚刚开始' }}</div>
                <div v-if="t.progress?.current" class="queue-row-sub mono" :title="t.progress.current">
                  {{ t.progress.current }}
                </div>
              </div>
              <div v-if="!scanQueue?.running.length" class="queue-empty">没有正在跑的扫描</div>
            </div>

            <div class="queue-col">
              <div class="queue-col-title">排队中（{{ scanQueue?.waiting.length || 0 }}）</div>
              <div v-for="t in scanQueue?.waiting" :key="'wait-' + t.library_id" class="queue-row">
                <div class="queue-row-head">
                  <span class="queue-name">{{ t.name }}</span>
                  <span class="mini-badge muted">第 {{ t.position ?? '-' }} 位</span>
                  <el-button
                    size="small"
                    text
                    :icon="X"
                    @click="cancelQueued(t)"
                  >取消</el-button>
                </div>
                <div class="queue-row-sub warn">{{ waitingText(t) }}</div>
                <div class="queue-row-sub mono">
                  {{ [queuedSince(t), triggerLabel(t.trigger)].filter(Boolean).join(' · ') }}
                </div>
              </div>
              <div v-if="!scanQueue?.waiting.length" class="queue-empty">没有排队的扫描</div>
            </div>

            <div class="queue-col">
              <div class="queue-col-title queue-col-title--split">
                <span>最近完成</span>
                <el-switch v-model="showScanHistory" size="small" title="显示/隐藏扫描记录" />
              </div>
              <template v-if="showScanHistory">
              <div v-for="t in queueHistory" :key="'done-' + t.library_id + t.requested_at" class="queue-row">
                <div class="queue-row-head">
                  <span class="queue-name">{{ t.name }}</span>
                  <span class="mini-badge" :class="taskBadge(t).cls">{{ taskBadge(t).text }}</span>
                </div>
                <div class="queue-row-sub mono">
                  {{ [scanCountText(t),
                     t.duration_ms != null ? `耗时 ${fmtDuration(t.duration_ms)}` : '',
                     queuedSince(t), triggerLabel(t.trigger),
                     t.request_count > 1 ? `被点 ${t.request_count} 次` : ''].filter(Boolean).join(' · ') }}
                </div>
                <div v-if="t.error" class="queue-row-sub danger" :title="t.error">{{ t.error }}</div>
              </div>
              <div v-if="!queueHistory.length" class="queue-empty">还没有跑完的扫描</div>
              </template>
            </div>
          </div>
        </SectionCard>
        <EmptyState
          v-else
          :icon="ListChecks"
          title="扫描队列是空的"
          description="没有正在跑或排队的扫描，也还没有跑完的记录。在「媒体库」标签点「扫描」或「一键扫描全部」后，进度会显示在这里。"
        />
      </el-tab-pane>

      <el-tab-pane label="定时与刮削" name="schedule">
        <!-- 元数据与刮削：按库的扫描 / 定时 / 追新收在这里（密钥不在本页填，见页面末尾的说明） -->
        <SectionCard class="scrape-card" title="元数据与刮削" :icon="CalendarClock">
          <template #actions>
            <div class="queue-facts">
              <span class="fact" :class="{ warn: tmdbStatus !== null && !tmdbStatus.configured }">
                TMDB：{{ tmdbStatusText }}
              </span>
            </div>
          </template>
          <div class="scrape-grid">
            <div class="scrape-block">
              <h3>定时扫描</h3>
              <p class="drawer-hint">
                每天到点自动扫描所有启用库（增量，没变化的跳过）
              </p>
              <div v-if="autoScan" class="scrape-actions">
                <el-switch v-model="autoScan.enabled" active-text="开启" inactive-text="关闭" />
                <el-time-picker
                  v-model="autoScan.time"
                  format="HH:mm"
                  value-format="HH:mm"
                  placeholder="每天几点"
                  style="width: 130px"
                  :disabled="!autoScan.enabled"
                />
                <el-button type="primary" size="small" :loading="autoScanSaving" @click="saveAutoScanAction">
                  保存
                </el-button>
              </div>
              <div v-else-if="autoScanError" class="drawer-hint drawer-hint--after">
                读取失败
                <el-button size="small" text @click="loadAutoScanConfig">重试</el-button>
              </div>
              <div v-else class="au-skeleton scrape-skeleton" aria-busy="true" />
              <div v-if="autoScan?.last_run" class="drawer-hint drawer-hint--after">
                上次执行：{{ autoScan.last_run }}
              </div>
              <div v-else-if="autoScan" class="drawer-hint drawer-hint--after">
                还没有执行过
              </div>
            </div>
            <div class="scrape-block">
              <h3>追新</h3>
              <p class="drawer-hint">
                每隔 N 分钟检查新文件，发现即自动扫描刮削
              </p>
              <div v-if="chaseNew" class="scrape-actions">
                <el-switch v-model="chaseNew.enabled" active-text="开启" inactive-text="关闭" />
                <el-input-number
                  v-model="chaseNew.interval"
                  :min="5" :max="120" :step="5"
                  placeholder="分钟"
                  style="width: 130px"
                  :disabled="!chaseNew.enabled"
                />
                <span class="drawer-hint">分钟</span>
                <el-button type="primary" size="small" :loading="chaseNewSaving" @click="saveChaseNewAction">
                  保存
                </el-button>
              </div>
              <div v-if="chaseNew" class="scrape-actions" style="margin-top: 8px">
                <el-switch v-model="chaseNew.strm_enabled" active-text="监听 .strm" inactive-text="忽略 .strm" />
                <span class="drawer-hint">库路径是 /strm/... 时，strm 生成器产出的新分集（.strm 文件）也会触发增量扫描 + 刮削</span>
              </div>
              <div v-else-if="chaseNewError" class="drawer-hint drawer-hint--after">
                读取失败
                <el-button size="small" text @click="loadChaseNewConfig">重试</el-button>
              </div>
              <div v-else class="au-skeleton scrape-skeleton" aria-busy="true" />

              <template v-if="chaseNew">
                <!-- 告警最先看：连续失败的源 -->
                <div v-if="chaseNew.alerts.length" class="chase-alert" role="alert">
                  <div class="chase-alert-title">
                    {{ chaseNew.alerts.length }} 个来源连续失败 ≥ 3 次，新片可能发现不了
                  </div>
                  <ul class="chase-alert-list">
                    <li v-for="a in chaseNew.alerts" :key="a.source_key">
                      <span class="mono">{{ a.source_key }}</span>
                      · 连续 {{ a.consec_failures }} 次 · {{ a.last_error || '未知错误' }}
                      <span v-if="a.last_ok_at" class="chase-muted">（上次成功 {{ fmtRunTime(a.last_ok_at) }}）</span>
                    </li>
                  </ul>
                </div>

                <dl class="chase-facts">
                  <div>
                    <dt>上次检查</dt>
                    <dd>{{ chaseNew.last_check || '还没有检查过' }}</dd>
                  </div>
                  <div>
                    <dt>上轮 / 累计发现</dt>
                    <dd>{{ chaseNew.last_found }} / {{ chaseNew.total_found || 0 }} 个新文件</dd>
                  </div>
                  <div>
                    <dt title="Google Drive Changes API：只拉变化的文件，不用每轮列目录">增量发现（Drive Changes）</dt>
                    <dd>
                      <span class="chase-state" :class="{ 'is-on': chaseNew.drive_changes.running }">
                        {{ chaseNew.drive_changes.running ? '运行中' : '未运行' }}
                      </span>
                      <template v-if="chaseNew.drive_changes.last_poll">
                        · {{ fmtRunTime(chaseNew.drive_changes.last_poll) }}
                        变化 {{ chaseNew.drive_changes.last_changes }} 个文件，触发 {{ chaseNew.drive_changes.last_libs_triggered }} 个库
                      </template>
                    </dd>
                  </div>
                </dl>
                <p class="drawer-hint drawer-hint--after">
                  有 Drive 挂载时优先走增量发现，其余库按上面的间隔轮询目录；两种来源的结果都记在下面的运行记录里。
                </p>

                <details v-if="chaseNew.recent_runs.length" class="chase-runs">
                  <summary>最近 {{ chaseNew.recent_runs.length }} 轮运行</summary>
                  <ul>
                    <li v-for="r in chaseNew.recent_runs" :key="r.id" :class="{ 'is-error': !!r.error }">
                      <span class="mono">{{ fmtRunTime(r.started_at) }}</span>
                      <span>{{ r.source === 'drive-changes' ? '增量发现' : '轮询' }}</span>
                      <span>发现 {{ r.new_found }} · 扫描 {{ r.scans_triggered }}</span>
                      <span class="chase-run-status">{{ r.status }}</span>
                      <span v-if="r.error" class="chase-run-error" :title="r.error">{{ r.error }}</span>
                    </li>
                  </ul>
                </details>
              </template>
            </div>
            <div class="scrape-block">
              <h3>TMDB 首选语言</h3>
              <p class="drawer-hint">
                TMDB 返回的简介 / 标题 / 别名用哪种语言（保存后立即生效）
              </p>
              <div v-if="tmdbLang" class="scrape-actions">
                <el-select v-model="tmdbLang.language" style="width: 160px" aria-label="TMDB 首选语言">
                  <el-option
                    v-for="v in tmdbLang.options"
                    :key="v"
                    :label="tmdbLangLabel(v)"
                    :value="v"
                  />
                </el-select>
                <el-button type="primary" size="small" :loading="tmdbLangSaving" @click="saveTmdbLanguageAction">
                  保存
                </el-button>
              </div>
              <div v-else-if="tmdbLangError" class="drawer-hint drawer-hint--after">
                读取失败
                <el-button size="small" text @click="loadTmdbLanguageConfig">重试</el-button>
              </div>
              <div v-else class="au-skeleton scrape-skeleton" aria-busy="true" />
              <div v-if="tmdbLang?.from_env" class="drawer-hint drawer-hint--after">
                环境变量 TMDB_LANGUAGE 覆盖了这里的设置（改这里不会生效）
              </div>
            </div>
            <div class="scrape-block">
              <h3>单集夜间刮削</h3>
              <p class="drawer-hint">
                打开后，白天只刮顶层条目（电影 / 剧集），单集留到夜间窗口再刮，避免单集挤占白天的刮削资源（保存后 60 秒内生效；时间为服务器本地时间，窗口支持跨午夜）
              </p>
              <div v-if="episodeNight" class="scrape-actions">
                <el-switch
                  v-model="episodeNight.enabled"
                  active-text="开启"
                  inactive-text="关闭"
                  aria-label="单集仅夜间刮削开关"
                />
                <el-time-picker
                  v-model="episodeNight.night_start"
                  format="HH:mm"
                  value-format="HH:mm"
                  placeholder="开始"
                  style="width: 110px"
                  aria-label="夜间窗口开始"
                />
                <span class="chase-muted">至</span>
                <el-time-picker
                  v-model="episodeNight.night_end"
                  format="HH:mm"
                  value-format="HH:mm"
                  placeholder="结束"
                  style="width: 110px"
                  aria-label="夜间窗口结束"
                />
                <el-button type="primary" size="small" :loading="episodeNightSaving" @click="saveEnrichEpisodeNightAction">
                  保存
                </el-button>
                <span v-if="episodeNight.enabled" class="chase-state" :class="{ 'is-on': episodeNight.in_window }">
                  {{ episodeNight.in_window ? '夜间窗口内：正在刮单集' : '白天：跳过单集' }}
                </span>
              </div>
              <div v-else-if="episodeNightError" class="drawer-hint drawer-hint--after">
                读取失败
                <el-button size="small" text @click="loadEnrichEpisodeNightConfig">重试</el-button>
              </div>
              <div v-else class="au-skeleton scrape-skeleton" aria-busy="true" />
              <div v-if="episodeNight?.from_env" class="drawer-hint drawer-hint--after">
                环境变量（ENRICH_EPISODE_NIGHT_ONLY / ENRICH_NIGHT_START / ENRICH_NIGHT_END）覆盖了这里的设置（改这里不会生效）
              </div>
            </div>
          </div>
        </SectionCard>
        <SectionCard class="strm-card" title=".strm 直链" :icon="Link">
          <template #actions>
            <div class="queue-facts">
              <span class="fact" :class="{ warn: strmConfig && !strmConfig.enabled }">
                {{ strmConfig ? (strmConfig.enabled ? '已启用' : '已关闭') : '读取中…' }}
              </span>
            </div>
          </template>
          <p class="drawer-hint">
            .strm 是内容为播放直链的纯文本小文件。宿主机目录经 docker-compose（STRM_MOUNT_DIR，默认 /opt/strm）挂到容器内挂载点（STRM_CONTAINER_PATH，默认 /strm，只读）；改这里只改"认哪个目录"，不动容器挂载本身。要换容器内挂载点：先改 .env 的 STRM_CONTAINER_PATH 并 docker compose up -d 重启，再回来保存（容器里不存在的目录会被拒绝）。关闭总开关后扫描不再收录 .strm 文件（已入库的不动），保存即生效。
          </p>
          <div v-if="strmConfig" class="scrape-actions">
            <el-switch v-model="strmConfig.enabled" active-text="开启" inactive-text="关闭" />
            <el-input v-model="strmConfig.host_dir" placeholder="/opt/strm" style="width: 210px" aria-label="宿主机 .strm 目录">
              <template #prepend>宿主机</template>
            </el-input>
            <el-input v-model="strmConfig.container_path" placeholder="/strm" style="width: 180px" aria-label="容器内挂载点">
              <template #prepend>容器内</template>
            </el-input>
            <el-button type="primary" size="small" :loading="strmConfigSaving" @click="saveStrmConfigAction">
              保存
            </el-button>
          </div>
          <div v-else class="au-skeleton scrape-skeleton" aria-busy="true" />
          <div v-if="strmConfig" class="drawer-hint drawer-hint--after">
            当前容器内挂载点：{{ strmConfig.container_path }} ｜ 建媒体库时把路径指向它（添加路径弹窗里有快捷入口）
          </div>
        </SectionCard>
      </el-tab-pane>
    </el-tabs>

    <!--
      媒体库配置：新建与编辑共用这一张单页表单（借鉴 Emby Manager）。
      以前这里是两个地方——「新建弹窗」管一半、「设置抽屉」管另一半，路径在设置里只能看
      不能改、启用状态与追新监听干脆没入口。现在一张表单分组改完，每个选项都有一句
      人话说明，推荐项直接标出来。
    -->
    <el-drawer
      v-model="libFormVisible"
      :title="libForm.id ? `媒体库设置 · ${libFormTarget?.name || ''}` : '新建媒体库'"
      size="min(680px, 96vw)"
      :before-close="beforeCloseForm"
    >
      <div class="library-settings">
        <!-- 分组一：基础信息 -->
        <div class="settings-section">
          <div class="settings-section-title">基础信息</div>
          <p class="form-hint">
            库叫什么、装什么内容、归谁管。「归属」两项决定了这个库的内容会不会出现在
            别的服 / 别的机器上，是多服多机部署里最容易配错的一处。
          </p>
          <el-form label-position="top">
            <el-form-item label="媒体库名称">
              <el-input v-model="libForm.name" placeholder="如：电影库 / 剧集库 / 动漫库" />
            </el-form-item>

            <el-form-item label="内容类型">
              <el-select
                v-model="libForm.collection_type"
                style="width: 100%"
                popper-class="lib-opt-popper"
              >
                <el-option
                  v-for="t in COLLECTION_TYPES"
                  :key="t.value"
                  :label="optionLabel(COLLECTION_TYPES, t.value)"
                  :value="t.value"
                >
                  <div class="opt">
                    <div class="opt-label">{{ t.label }}</div>
                    <div class="opt-hint">{{ t.hint }}</div>
                  </div>
                </el-option>
              </el-select>
              <p class="field-help">{{ optionOf(COLLECTION_TYPES, libForm.collection_type)?.hint }}</p>
            </el-form-item>

            <el-form-item label="启用状态">
              <el-switch v-model="libForm.is_enabled" active-text="启用" inactive-text="停用" />
              <p class="field-help">
                停用后客户端看不到这个库，它也不参与定时扫描与追新；已入库的条目留着不删，
                重新启用就回来。
              </p>
            </el-form-item>

            <el-form-item v-if="!isSingleRealm" label="归属服">
              <el-select
                v-model="libForm.realm_id"
                clearable
                placeholder="未标注（所有服可见）"
                style="width: 100%"
              >
                <el-option
                  v-for="r in realmOptions"
                  :key="r.id"
                  :label="r.id === realm.activeId ? `${r.name}（推荐 · 当前服）` : r.name"
                  :value="r.id"
                />
              </el-select>
              <p class="field-help">
                内容隔离的边界：只有这个服的 EA 会向客户端提供这个库。换服等于把内容交给另一个服，
                绑定在旧服上的挂载会跟着搬过去。
              </p>
            </el-form-item>

            <el-form-item
              v-if="!libFormTarget?.is_virtual"
              label="归属播放节点"
            >
              <el-select
                v-model="libForm.node_id"
                clearable
                placeholder="未分配（所有节点可见，由面板扫描）"
                style="width: 100%"
              >
                <el-option v-for="n in nodes" :key="n.id" :label="nodeLabel(n)" :value="n.id" />
              </el-select>
              <p class="field-help">
                决定「谁向客户端展示这个库、谁来扫描它」。内容只在那台机器上（本机目录 / 只在那里配了的
                rclone）就分配给它；留空则所有节点可见、由面板扫。
              </p>
            </el-form-item>
          </el-form>
        </div>

        <!-- 虚拟库：没有自己的目录与挂载，后面两组不适用 -->
        <div v-if="libFormTarget?.is_virtual" class="settings-paths">
          <span>虚拟媒体库</span>
          <p>
            按发行平台「{{ libFormTarget.platform || '—' }}」聚合库里已识别到的条目，条目仍归属原媒体库。
            它没有自己的目录与挂载，所以下面不列「媒体路径」与「追新监听」——要改内容来源，去改原始媒体库。
          </p>
        </div>

        <!-- 分组二：扫描性能 -->
        <div class="settings-section">
          <div class="settings-section-title">扫描性能与刮削</div>
          <p class="form-hint">
            这一组决定扫描时花多少力气：刮削策略决定要不要回头重刮「已经刮过」的条目，
            直接影响 TMDB 配额与扫描耗时。
          </p>
          <el-form label-position="top">
            <el-form-item label="增量扫描">
              <el-switch
                v-model="incrementalScan"
                active-text="开"
                inactive-text="关（每轮全量重扫）"
              />
              <p class="field-help">
                开着时，只有新增或改动过的文件会重新处理，没变动的目录直接跳过——
                没变动的库一般秒级扫完。关掉则每轮都完整处理一遍（排查指纹异常时才需要）。
              </p>
            </el-form-item>

            <el-form-item label="新片入库实时监听">
              <el-switch
                v-model="fsWatch"
                active-text="开"
                inactive-text="关"
              />
              <p class="field-help">
                对**本机目录**（如 <code>/mnt/...</code>）用 inotify 监听：文件一落地就自动触发
                一轮增量扫描，不用等定时扫描。1 秒内的多个变动会合并成一次，不会把队列灌满。
                远程挂载（115 / rclone）拿不到事件源，仍由定时扫描兜底。
              </p>
              <p v-if="fsWatchForThisLib" class="field-warn">{{ fsWatchForThisLib }}</p>
            </el-form-item>

            <el-form-item label="刮削策略">
              <el-select
                v-model="libForm.scrape_policy"
                style="width: 100%"
                popper-class="lib-opt-popper"
              >
                <el-option
                  v-for="p in POLICIES"
                  :key="p.value"
                  :label="optionLabel(POLICIES, p.value)"
                  :value="p.value"
                >
                  <div class="opt">
                    <div class="opt-label">
                      {{ p.label }}<span v-if="p.recommended" class="opt-rec">（推荐）</span>
                    </div>
                    <div class="opt-hint">{{ p.hint }}</div>
                  </div>
                </el-option>
              </el-select>
              <p class="field-help">{{ optionOf(POLICIES, libForm.scrape_policy)?.hint }}</p>
            </el-form-item>

            <el-form-item v-if="libraryNeeds115" label="115 账号">
              <el-select
                v-model="libForm.account_115_id"
                clearable
                placeholder="默认账号（面板级 PAN115_COOKIE）"
                style="width: 100%"
              >
                <el-option v-for="a in panAccounts" :key="a.id" :label="a.name" :value="a.id" />
              </el-select>
              <p class="field-help">这个库用哪个 115 配置档转存 / 下载；留空 = 用默认账号。</p>
            </el-form-item>

            <div class="settings-paths">
              <span>定时扫描（全局）</span>
              <p v-if="autoScan">
                {{ autoScan.enabled
                  ? `已开启：每天 ${autoScan.time} 把所有启用库入队扫描（增量，没变化的目录跳过）。`
                  : '未开启：只在手动点「扫描」、或追新发现新文件时扫。' }}
                上次执行：{{ autoScan.last_run || '还没有执行过' }}。
              </p>
              <p v-else>读取中…</p>
              <p>所有启用库共用一份计划，开关与时间在本页「定时与刮削」标签里改。</p>
            </div>
          </el-form>
        </div>

        <!-- 分组三：媒体库封面 -->
        <div v-if="libFormTarget" class="settings-section">
          <div class="settings-section-title">媒体库封面</div>
          <p class="form-hint">
            封面只影响列表与首页的观感，不参与刮削，扫描也不会覆盖它。支持 JPG / PNG / WebP，单张最大 8 MB。
          </p>
          <p class="form-hint">
            <strong>封面样式与标题改完要点底部「保存」才生效</strong>——页面上的预览只是预览，
            不会自动写库；已经生成过的封面在下次「生成封面」或扫描后自动更新时才被替换。
          </p>
          <div class="lib-cover-edit">
            <div class="lib-cover-thumb">
              <img
                v-if="libFormTarget.cover_url && coverUrls[libFormTarget.id]"
                :src="coverUrls[libFormTarget.id]"
                :alt="`${libFormTarget.name} 封面`"
              />
              <div v-else class="lib-cover-empty">
                <Film :size="20" />
                <span>未设置封面</span>
              </div>
            </div>
            <div class="lib-cover-ops">
              <el-upload
                :accept="'image/jpeg,image/png,image/webp'"
                :show-file-list="false"
                :disabled="!!coverUploading[libFormTarget.id]"
                :http-request="(options: UploadRequestOptions) => requestCoverUpload(libFormTarget!, options)"
              >
                <el-button :loading="!!coverUploading[libFormTarget.id]">
                  <ImagePlus :size="14" style="margin-right: 4px" />{{ libFormTarget.cover_url ? '更换封面' : '上传封面' }}
                </el-button>
              </el-upload>
              <el-button v-if="libFormTarget.cover_url" @click="removeCover(libFormTarget)">
                <X :size="14" style="margin-right: 4px" />移除封面
              </el-button>
            </div>
          </div>
  <div class="form-hint" style="margin-top: 12px">
    或者选个样式自动生成：系统会从<strong>最新入库</strong>的条目里挑海报拼一张横版封面
    （1920×1080），不用自己找图配字。刮完新片点「按最新海报重新生成」即可换封面。
  </div>

  <el-form-item label="自动生成样式" style="margin-bottom: 12px">
    <el-select
      v-model="coverTemplate"
      placeholder="留空 = 用上传的封面"
      clearable
      style="width: 100%"
    >
      <el-option
        v-for="t in COVER_TEMPLATES"
        :key="t.value"
        :label="t.label"
        :value="t.value"
      />
    </el-select>
    <p class="drawer-hint drawer-hint--tight">
      选中的样式：{{ optionOf(COVER_TEMPLATES, coverTemplate)?.hint || '不生成，仍用上传的封面' }}
    </p>
    <div class="cover-autogen">
      <el-switch
        v-model="coverAutoRegen"
        active-text="新片入库后自动更新"
        inactive-text="不自动更新"
      />
      <p class="drawer-hint" style="margin: 4px 0 0">
        打开后，这个库每扫完一轮且<b>确实有新片入库时</b>，会自动用最新入库的海报按上面选的样式
        重新拼一张封面；没有新片就不动它。渲染在<b>后台排队</b>进行，不会拖慢扫描；转场一次入库
        几百个文件也只会排一个任务，画不出来时旧封面原样保留。默认关闭。
        <template v-if="!coverTemplate">
          <br /><span class="field-warn-inline">需先选一个封面样式，否则不会生效。</span>
        </template>
      </p>
    </div>
  </el-form-item>

  <template v-if="coverTemplate">
    <el-form-item label="封面标题" style="margin-bottom: 12px">
      <el-input
        v-model="coverTitle"
        placeholder="例如：{library}"
        maxlength="100"
      />
    </el-form-item>
    <el-form-item label="封面副标题" style="margin-bottom: 12px">
      <el-input
        v-model="coverSubtitle"
        placeholder="例如：{type} · {year}"
        maxlength="100"
      />
    </el-form-item>
    <p class="drawer-hint" style="margin: -6px 0 10px">
      可用变量：<code>{library}</code> 媒体库名、<code>{type}</code> 内容类型、<code>{year}</code> 当前年份。
      只渲染纯文字，不执行 HTML 或样式；字体不可用时自动省略文字，不影响封面生成。
    </p>

    <div class="cover-preview-wrap">
      <div class="cover-preview-box">
        <img
          v-if="coverPreviewUrl"
          :src="coverPreviewUrl"
          alt="封面预览"
          class="cover-preview-img"
        />
        <el-empty
          v-else-if="coverPreviewLoading"
          description="正在渲染预览…"
          :image-size="52"
        />
        <div v-else class="cover-preview-hint">
          {{ coverPreviewError || '选好样式后自动出预览' }}
        </div>
      </div>
      <div class="cover-preview-actions">
        <el-button
          size="small"
          :loading="coverPreviewLoading"
          :disabled="!canPreviewCover()"
          @click="loadCoverPreview"
        >
          刷新预览
        </el-button>
        <el-button
          size="small"
          type="primary"
          :loading="coverSaving"
          :disabled="!canPreviewCover()"
          @click="saveCoverConfig"
        >
          生成并保存
        </el-button>
        <el-button
          v-if="libFormTarget?.cover_template"
          size="small"
          :loading="coverRegenerating"
          @click="regenerateCover"
        >
          按最新海报重新生成
        </el-button>
        <p v-if="!canPreviewCover()" class="drawer-hint">
          新建的库还没有条目，保存后扫出内容才能生成封面。
        </p>
      </div>
    </div>
  </template>
        </div>

        <!-- 分组四：媒体路径 -->
        <div v-if="!libFormTarget?.is_virtual" class="settings-section">
          <div class="settings-section-title">媒体路径</div>
          <p class="form-hint">
            决定扫描哪些目录：<strong>一行一条</strong>，路径只写目录本身（如 <code>/mnt/电影</code>），
            它存在哪种存储上由每行的标签决定（本地文件 / Rclone / 115 网盘）。
            点「按类型添加路径」选好类型再浏览目录，默认就是服务器本地硬盘。
          </p>
          <LibraryPathManager
            v-model="libForm.path_entries"
            :mounts="mounts"
            :backends="storageBackends"
          />
          <!--
            老库的「库级挂载」：v2.46.0 起了独立的下拉框去重，这里只读回显。
            不删它是因为扫描 / 追新 / 挂载健康都还按 mount_ids 走，写成「已删除」等于
            骗用户说数据没了 —— 实际它还在生效，只是不能再从这里改。
          -->
          <div v-if="legacyMountIds.length" class="legacy-mount-note">
            <b>还挂着 {{ legacyMountIds.length }} 个库级挂载</b>：{{ legacyMountNames }}
            <p>
              这是旧配置留下的，内容仍会一起扫描。为了不再两处说法打架，v2.46.0 起不再单独编辑它
              —— 要加目录请用上面的「按类型添加路径」，要解除绑定请到「服务器」页删掉对应挂载。
            </p>
          </div>
        </div>

        <!-- 分组五：目录变更监听 -->
        <div v-if="libFormTarget && !libFormTarget.is_virtual" class="settings-section">
          <div class="settings-section-title">目录变更监听</div>
          <p class="form-hint">
            「追新」每隔几分钟扫一遍目录，发现新视频文件（.strm 开关开时也含 .strm）就自动触发一次扫描 + 刮削（NFO 优先 → TMDB → 豆瓣），
            不用等手动扫描。开关与间隔是全局的，纳不纳入某个库在这里定。
          </p>
          <el-form label-position="top">
            <el-form-item label="本库是否监听追新">
              <el-switch
                :model-value="!libForm.chase"
                :loading="chaseLibSaving"
                active-text="监听"
                inactive-text="排除"
                @change="onChaseSwitch"
              />
              <p class="field-help">
                <template v-if="!chaseNew">{{ chaseNewError ? '追新配置读取失败，暂时无法切换（可到「定时与刮削」标签重试）' : '追新配置读取中…' }}</template>
                <template v-else>
                  改动即时生效：开「监听」就是正常扫新片；开「排除」就是把这个库加进
                  <strong>排除清单</strong>（当前已排除 {{ chaseExcludedIds().length }} 个库），
                  不会再自动发现新片——手动扫描与定时扫描不受影响。
                </template>
              </p>
            </el-form-item>
            <div class="settings-paths">
              <span>追新总开关（全局）</span>
              <p v-if="chaseNew">
                {{ chaseNew.enabled
                  ? `已开启 · 每 ${chaseNew.interval} 分钟检查一次`
                  : '未开启（开关与间隔在本页「定时与刮削」标签）' }}
              </p>
              <p v-if="chaseNew?.last_check">
                上次检查：{{ chaseNew.last_check }} ｜ 上轮发现 {{ chaseNew.last_found }} 个新文件
              </p>
            </div>
          </el-form>
        </div>
      </div>

      <template #footer>
        <div class="lib-form-foot">
          <span class="lib-form-dirty">
            {{ libFormDirty
              ? (libFormPathsChanged ? '路径已改：保存会连带重新扫描一次' : '有未保存的改动')
              : '' }}
          </span>
          <div class="lib-form-foot-btns">
            <el-button :disabled="!libFormDirty" @click="revertForm">还原</el-button>
            <el-button @click="cancelForm">取消</el-button>
            <!-- 路径改了就把**主**按钮变成「保存并扫描」：以前两个并列按钮摆一起，
               主按钮还是「保存」，很容易只点它、然后发现扫描没跟着跑 -->
            <el-button
              type="primary"
              :loading="libFormSaving"
              @click="saveForm(!!(libForm.id && libFormPathsChanged))"
            >
              {{ libForm.id ? (libFormPathsChanged ? '保存并扫描' : '保存') : '创建' }}
            </el-button>
          </div>
        </div>
      </template>
    </el-drawer>

    <!-- 刷新元数据：选策略；all 二次确认并提示配额消耗 -->
    <el-dialog v-model="rescrapeVisible" title="刷新元数据" width="min(420px, 92vw)">
      <p class="drawer-hint">
        对「{{ rescrapeTarget?.name }}」里**已有**的条目重新刮削一遍。不会扫描新文件（要发现新片请用「扫描」），
        策略只覆盖本轮，不改库配置。
      </p>
      <el-radio-group v-model="rescrapePolicy">
        <el-radio-button value="missing_only">仅补缺失</el-radio-button>
        <el-radio-button value="all">全量重刮</el-radio-button>
      </el-radio-group>
      <p v-if="rescrapePolicy === 'all'" class="drawer-hint text-danger" style="margin-top: 8px">
        全量重刮会对该库所有条目重新请求 TMDB，会消耗大量配额，确定要继续吗？
      </p>
      <template #footer>
        <el-button @click="rescrapeVisible = false">取消</el-button>
        <el-button type="primary" :loading="rescrapeLoading" @click="confirmRescrape">开始</el-button>
      </template>
    </el-dialog>

    <!-- 扫描记录：最近若干轮（每轮的状态 / 触发方 / 增量 / 耗时 / 原因） -->
    <el-drawer v-model="scanDrawer" :title="`扫描记录 · ${scanTarget?.name || ''}`" size="min(620px, 96vw)">
      <p class="drawer-hint">
        每轮扫描一行，最近的在最上面（每库最多保留 {{ scanKeep }} 条）。
        「每轮都失败」和「只是最近一轮失败」是两件事，这里能直接看出来。
        「来源」列把这一轮拆到每条路径 / 挂载上：悬停看每条扫到多少文件、哪条是空的。
      </p>
      <DataTable
        :rows="scanRuns"
        :columns="scanColumns"
        :loading="scanLoading"
        empty="还没有扫描记录"
        row-key="id"
      >
        <template #empty>
          <EmptyState compact :icon="History" title="还没有扫描记录" description="点库卡片上的「扫描」跑一轮后，这里会出现记录。" />
        </template>
        <template #cell-started_at="{ row }">{{ fmtDate(row.started_at) }}</template>

        <template #cell-status="{ row }">
          <span class="mini-badge" :class="statusMeta(row.status).cls">{{ statusMeta(row.status).text }}</span>
        </template>

        <template #cell-trigger="{ row }">{{ triggerLabel(row.trigger) }}</template>

        <template #cell-summary="{ row }">
          <span class="mono">{{ runSummary(row) }}</span>
        </template>

        <template #cell-sources="{ row }">
          <span
            :class="{ 'scan-hint': emptySourceCount(row.sources) }"
            :title="sourcesDetail(row.sources)"
          >{{ sourcesLabel(row.sources) }}</span>
        </template>

        <template #cell-duration="{ row }">
          {{ row.duration_ms != null ? fmtDuration(row.duration_ms) : '—' }}
        </template>

        <template #cell-error="{ row }">
          <span v-if="row.error" class="scan-error" :title="row.error">{{ row.error }}</span>
          <span v-else-if="row.failed_roots.length" class="scan-error" :title="row.failed_roots.join('；')">
            {{ row.failed_roots.join('；') }}
          </span>
          <span v-else>—</span>
        </template>
      </DataTable>
    </el-drawer>
  </div>
</template>

<style scoped>
/* v2.54 暗房影院：颜色 / 圆角只走 --au-* 令牌；卡片 = 实色表面 + 发丝线，不靠阴影。
   库卡片的封面、配置抽屉的大部分规则在 EmbyAdmin.css（下拉面板挂在 body 上，scoped 盖不到）。 */

/* ==================== 页内标签 ==================== */
.emby-tabs :deep(.el-tabs__header) { margin-bottom: 16px; }
.emby-tabs :deep(.el-tab-pane) { display: flex; flex-direction: column; gap: 16px; }
.tab-label { display: inline-flex; align-items: center; gap: 6px; }
.tab-count {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 18px; height: 18px; padding: 0 5px;
  border-radius: var(--au-r-full);
  background: var(--au-violet-soft);
  color: var(--au-text-3);
  font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums;
}
.tab-count.is-live { background: var(--au-primary-soft); color: var(--au-primary); }

/* ==================== 媒体库工具条 + 卡片墙 ==================== */
.lib-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
}
.lib-toolbar-meta { font-size: var(--font-size-sm); color: var(--au-text-3); }
.lib-toolbar-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.scan-help { display: flex; flex-direction: column; gap: 6px; font-size: var(--font-size-xs); line-height: 1.7; color: var(--au-text-2); }

.lib-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(310px, 1fr));
  gap: 16px;
}
.lib-card {
  display: flex; flex-direction: column; gap: 0; overflow: hidden; min-width: 0;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  transition: border-color var(--au-fast) var(--au-ease);
}
.lib-card:hover { border-color: var(--au-border-strong); }
.lib-skeleton { height: 320px; border-radius: var(--au-r-lg); }
.lib-empty {
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
}
.lib-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: nowrap; }
.lib-name {
  font-family: var(--au-font-serif);
  font-weight: 700; font-size: var(--font-size-lg); color: var(--au-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.lib-meta { flex-shrink: 0; font-size: var(--font-size-xs); color: var(--au-text-3); }
.lib-time { font-size: var(--font-size-xs); color: var(--au-text-4); }

.fact {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: var(--font-size-xs); color: var(--au-text-2);
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.fact.warn { color: var(--au-warning); }

.lib-foot {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; flex-wrap: wrap;
  margin-top: 4px; padding-top: 12px;
  border-top: 1px solid var(--au-border);
}

/* 老库的只读挂载回显：说清“还在生效” + “不再能改”，否则看着像残留的破 UI */
.legacy-mount-note {
  margin-top: 12px;
  padding: 10px 14px;
  border: 1px solid var(--au-warning-border);
  border-radius: var(--au-r-md);
  background: var(--au-warning-soft);
  font-size: var(--font-size-sm);
  color: var(--au-text-2);
  line-height: 1.6;
}
.legacy-mount-note b { color: var(--au-warning); }
.legacy-mount-note p { margin: 6px 0 0; font-size: var(--font-size-xs); color: var(--au-text-3); }

.drawer-hint { margin: 0 0 12px; font-size: var(--font-size-xs); color: var(--au-text-3); line-height: 1.7; }
.drawer-hint--after { margin: 6px 0 0; }
.drawer-hint--tight { margin: 4px 0 0; }
.text-danger { color: var(--au-danger); }
.field-warn-inline { color: var(--au-warning); }
.chase-alert {
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid var(--au-danger-border);
  border-radius: var(--au-r-md);
  background: var(--au-danger-soft);
  font-size: var(--font-size-xs);
  line-height: 1.7;
  color: var(--au-text-2);
}
.chase-alert-title { color: var(--au-danger); font-weight: 600; }
.chase-alert-list { margin: 4px 0 0; padding-left: 16px; overflow-wrap: anywhere; }
.chase-muted { color: var(--au-text-3); }
.chase-facts {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 10px 0 0;
  padding-top: 10px;
  border-top: 1px solid var(--au-border);
  font-size: var(--font-size-xs);
}
.chase-facts > div { display: flex; gap: 10px; min-width: 0; }
.chase-facts dt { flex: 0 0 9em; color: var(--au-text-3); }
.chase-facts dd { flex: 1; min-width: 0; margin: 0; color: var(--au-text-2); overflow-wrap: anywhere; }
.chase-state { color: var(--au-text-3); }
.chase-state.is-on { color: var(--au-success); }
.chase-runs { margin-top: 8px; font-size: var(--font-size-xs); color: var(--au-text-3); }
.chase-runs summary { cursor: pointer; color: var(--au-text-2); }
.chase-runs summary:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }
.chase-runs ul { list-style: none; margin: 6px 0 0; padding: 0; line-height: 1.8; }
.chase-runs li { display: flex; flex-wrap: wrap; gap: 0 8px; }
.chase-runs li.is-error .chase-run-status { color: var(--au-danger); }
.chase-run-error {
  flex-basis: 100%;
  color: var(--au-danger);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 640px) {
  .chase-facts > div { flex-direction: column; gap: 0; }
  .chase-facts dt { flex-basis: auto; }
}

/* ==================== 定时与刮削 ==================== */
.scrape-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 20px; }
.scrape-block {
  display: flex; flex-direction: column;
  padding: 14px 16px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-bg-soft);
}
.scrape-block h3 { margin: 0 0 6px; font-size: var(--font-size-sm); font-weight: 700; color: var(--au-text); }
.scrape-block .drawer-hint { margin-bottom: 10px; }
.scrape-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.scrape-skeleton { height: 32px; border-radius: var(--au-r-md); }

/* ==================== 扫描状态（卡片 + 记录抽屉） ==================== */
.scan-dot { width: 6px; height: 6px; border-radius: var(--au-r-full); background: var(--au-text-4); flex-shrink: 0; }
.scan-dot.is-success { background: var(--au-success); }
.scan-dot.is-partial { background: var(--au-warning); }
.scan-dot.is-failed { background: var(--au-danger); }
.scan-dot.is-running { background: var(--au-info); }
.scan-dot.is-queued { background: var(--au-text-3); }
.scan-error, .scan-hint {
  font-size: var(--font-size-xs);
  color: var(--au-warning);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 实时状态一行（排队原因 / 扫描进度）：与「最近一次结果」分层，后者是落库的历史 */
.lib-live {
  display: flex; align-items: center; gap: 6px;
  font-size: var(--font-size-xs); color: var(--au-info);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* ==================== 播放可达性 ==================== */
.reach-card :deep(.au-section__body) { display: flex; flex-direction: column; gap: 10px; }
.reach-row {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-bg-soft);
}
.reach-row-body { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.reach-row-title { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; color: var(--au-text); }
.reach-name { font-weight: 700; color: var(--au-text); }
.reach-node { font-size: var(--font-size-xs); color: var(--au-text-3); }
.reach-row-fix { font-size: var(--font-size-xs); color: var(--au-text-2); }

/* ==================== 扫描队列：正在跑 / 排队中 / 最近完成 ==================== */
.queue-facts { display: flex; flex-wrap: wrap; gap: 6px 14px; }
.queue-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
.queue-col { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.queue-col-title { font-size: var(--font-size-xs); color: var(--au-text-3); letter-spacing: 0.04em; }
.queue-col-title--split { display: flex; align-items: center; justify-content: space-between; }
.queue-row {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-bg-soft);
  min-width: 0;
}
.queue-row-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.queue-name {
  font-size: var(--font-size-sm); font-weight: 500; color: var(--au-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.queue-row-head :deep(.el-button) { margin-left: auto; padding: 0 4px; }
.queue-row-sub {
  font-size: var(--font-size-xs); color: var(--au-text-3);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.queue-row-sub.warn { color: var(--au-warning); }
.queue-row-sub.danger { color: var(--au-danger); }
.queue-empty {
  padding: 14px 10px;
  border: 1px dashed var(--au-border);
  border-radius: var(--au-r-md);
  text-align: center;
  font-size: var(--font-size-xs); color: var(--au-text-4);
}

/* ==================== 手机（≤768px） ==================== */
@media (max-width: 768px) {
  .lib-grid { grid-template-columns: minmax(0, 1fr); }
  .lib-toolbar-actions { width: 100%; }
  .lib-toolbar-actions .el-button { flex: 1; margin-left: 0; }
  .scrape-grid { grid-template-columns: 1fr; gap: 12px; }
  .emby-tabs :deep(.el-tabs__nav-wrap) { overflow-x: auto; }
}
</style>
