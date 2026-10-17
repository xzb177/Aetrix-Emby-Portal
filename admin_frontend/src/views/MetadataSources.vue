<script setup lang="ts">
/**
 * 元数据来源 —— 条目这一层的元数据从哪来、怎么纠偏、补全跑得怎么样
 *
 * 迁移说明（Phase 6）：这三块以前塞在「媒体库」页的「元数据与刮削」卡片里，和「按库配置」
 * 混在一起。但它们回答的是**条目级**的问题（这一条的图/简介/IMDb 哪来的、补全队列到哪了），
 * 与「这个库扫什么目录、按什么策略刮」不是同一层。位置迁到这里，逻辑与文案原样搬迁。
 *
 * 这里仍然是**执行视角**：不新增任何状态、不改后端接口，三块功能与迁移前逐字一致
 * （接口、参数、提示文案、确认弹窗都没有动）。
 *
 * 按库的东西仍在「媒体库」页：整库重刮、定时扫描、目录变更监听。
 * **TMDB 密钥也只在这里填**（媒体库页的填写框已移除）：一把钥匙只该有一个地方能改。
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  Gauge,
  KeyRound,
  Layers,
  ListOrdered,
  Lock,
  LockOpen,
  PlugZap,
  RefreshCw,
  BookOpen,
  Search,
  Wand2,
} from 'lucide-vue-next'
import {
  addMetaSourceKey,
  addTmdbKey,
  bindTmdb,
  deleteMetaSourceKey,
  deleteTmdbKey,
  fetchAltmetaRates,
  fetchDoubanConfig,
  fetchEnrichProgress,
  fetchMetaSources,
  fetchTmdbKeys,
  fetchTmdbMirror,
  fetchDoubanBanDowngrade,
  fetchTmdbLanguage,
  fetchTmdbPosterLanguage,
  fetchTmdbKeyCooldowns,
  lockItemMetadata,
  previewTmdb,
  probeMetaSources,
  searchItemsForBind,
  searchTmdbCandidates,
  resetMetaSourceCooldown,
  resetTmdbKeyCooldown,
  saveAltmetaRates,
  saveDoubanConfig,
  saveMetaSources,
  saveTmdbMirror,
  saveDoubanBanDowngrade,
  saveTmdbLanguage,
  saveTmdbPosterLanguage,
  saveTmdbKeyCooldowns,
  testMetaSourceKeys,
  testTmdbKeys,
  unlockItemMetadata,
  TMDB_LANGUAGE_LABELS,
} from '@/api/admin'
import type {
  ItemSearchResult,
  EnrichProgress,
  MetaSourceKeyRow,
  MetaSourceOutcome,
  MetaSourceProbe,
  MetaSourceRow,
  MetaSourcesConfig,
  TmdbBindResult,
  TmdbCandidate,
  TmdbKeyPoolRow,
  TmdbKeysStatus,
  TmdbMirror,
  TmdbPreview,
  TmdbTestResult,
  AltmetaRates,
  DoubanConfig,
  DoubanBanDowngradeConfig,
  TmdbLanguageStatus,
  TmdbPosterLanguageStatus,
  TmdbKeyCooldowns,
} from '@/api/admin'
import { AdvancedSettings, EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import './MetadataSources.css'

// ==================== 手动绑定 TMDB ====================

// P1 入口前移：媒体库列表的「识别」按钮跳过来时会带 ?item_id=xxx
const route = useRoute()

// --- 手动绑定卡片折叠（默认收起，状态存 localStorage） ---
const bindCardCollapsed = ref(localStorage.getItem('aetrix_bind_tmdb_collapsed') !== '0')
function toggleBindCard() {
  bindCardCollapsed.value = !bindCardCollapsed.value
  localStorage.setItem('aetrix_bind_tmdb_collapsed', bindCardCollapsed.value ? '1' : '0')
}

// --- 剧名+年份搜索（替代手输条目 ID） ---
const bindSearchQ = ref('')
const bindSearchYear = ref('')
const bindSearchResults = ref<ItemSearchResult[]>([])
const bindSearchLoading = ref(false)
const bindSelectedItem = ref<ItemSearchResult | null>(null)

async function doSearchBindItems() {
  const q = bindSearchQ.value.trim()
  if (!q) { ElMessage.warning('请输入剧名关键字'); return }
  const yearStr = bindSearchYear.value.trim()
  let year: number | null = null
  if (yearStr) {
    year = Number(yearStr)
    if (!Number.isInteger(year) || year <= 0) { ElMessage.warning('年份必须是正整数'); return }
  }
  bindSearchLoading.value = true
  bindSearchResults.value = []
  try {
    const res = await searchItemsForBind(q, year)
    bindSearchResults.value = res.items
    if (!res.items.length) ElMessage.info('没搜到匹配的条目，换个关键字试试')
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '搜索失败'))
  } finally {
    bindSearchLoading.value = false
  }
}

function selectBindItem(item: ItemSearchResult) {
  bindSelectedItem.value = item
  bindItemId.value = String(item.id)
  bindPreview.value = null
  bindNotes.value = []
  // 第二步默认填条目名 + 年份，管理员直接点「搜索 TMDB」即可
  tmdbSearchQ.value = item.name || ''
  tmdbSearchYear.value = item.year ? String(item.year) : ''
  tmdbCandidates.value = []
  tmdbSearched.value = false
}

function clearBindSelection() {
  bindSelectedItem.value = null
  bindItemId.value = ''
  bindPreview.value = null
  tmdbCandidates.value = []
  tmdbSearched.value = false
}

// ==================== 元数据锁定（P3，Emby 式手动识别） =============// 锁定 = 自动补全（enrich）不再碰这条，防止自动刷新覆盖手动整理成果。
// 手动绑定/手动重刮不受锁定影响——锁定防的是「自动」，手动永远优先。
const lockLoadingId = ref<number | null>(null)

async function toggleItemLock(item: ItemSearchResult) {
  lockLoadingId.value = item.id
  try {
    const res = item.metadata_locked
      ? await unlockItemMetadata(item.id)
      : await lockItemMetadata(item.id)
    item.metadata_locked = res.item.metadata_locked
    ElMessage.success(
      item.metadata_locked
        ? `「${item.name}」已锁定：自动补全不再覆盖它`
        : `「${item.name}」已解锁：恢复自动补全`,
    )
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '操作失败'))
  } finally {
    lockLoadingId.value = null
  }
}
// TMDB 对中文剧集/综艺收录偏少，自动刮削搜不到的条目在这里手动指定 ID。
// 流程：填条目 ID → 填 TMDB ID → 预览确认是哪部片 → 绑定（或解绑）。
// --- 第二步：TMDB 候选搜索 + 一键绑定（Emby 式手动识别） ---
const tmdbSearchQ = ref('')
const tmdbSearchYear = ref('')
const tmdbCandidates = ref<TmdbCandidate[]>([])
const tmdbSearchLoading = ref(false)
const tmdbSearched = ref(false)
const tmdbBindLoading = ref<number | null>(null)
/** 手填 TMDB ID 入口默认折叠：候选墙能解决绝大多数情况 */
const manualIdCollapsed = ref(true)

async function doSearchTmdbCandidates() {
  const item = bindSelectedItem.value
  if (!item) { ElMessage.warning('请先搜索并选择条目'); return }
  const q = tmdbSearchQ.value.trim()
  if (!q) { ElMessage.warning('请输入剧名'); return }
  const yearStr = tmdbSearchYear.value.trim()
  let year: number | null = null
  if (yearStr) {
    year = Number(yearStr)
    if (!Number.isInteger(year) || year <= 0) { ElMessage.warning('年份必须是正整数'); return }
  }
  tmdbSearchLoading.value = true
  tmdbCandidates.value = []
  try {
    const kind = item.item_type === 'movie' ? 'movie' : 'series'
    const res = await searchTmdbCandidates(q, year, kind)
    tmdbCandidates.value = res.candidates
    tmdbSearched.value = true
    if (!res.candidates.length) ElMessage.info('TMDB 没找到匹配的候选，换个剧名或年份试试')
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '搜索 TMDB 失败'))
  } finally {
    tmdbSearchLoading.value = false
  }
}

async function doBindCandidate(c: TmdbCandidate) {
  const item = bindSelectedItem.value
  if (!item) { ElMessage.warning('请先搜索并选择条目'); return }
  try {
    await ElMessageBox.confirm(
      `把「${item.name}」绑定到 TMDB「${c.title}${c.year ? `（${c.year}）` : ''}」吗？绑定后自动补全缺失的图 / 简介 / IMDb / 别名。`,
      '确认绑定',
      { type: 'warning' },
    )
  } catch { return }
  tmdbBindLoading.value = c.tmdb_id
  try {
    const res: TmdbBindResult = await bindTmdb(item.id, String(c.tmdb_id), true)
    bindNotes.value = [`「${res.item.name}」：${res.notes.join('；')}`]
    bindPreview.value = null
    if (res.item.tmdb_id != null) item.tmdb_id = res.item.tmdb_id
    ElMessage.success('已绑定并补全元数据')
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '绑定失败'))
  } finally {
    tmdbBindLoading.value = null
  }
}
// TMDB 对中文剧集/综艺收录偏少，自动刮削搜不到的条目在这里手动识别。
// 流程：第一步搜库内条目并选中 → 第二步搜 TMDB 候选、一键绑定（或展开「手动输入 TMDB ID」兜底）。
const bindItemId = ref('')
const bindTmdbId = ref('')
const bindPreview = ref<TmdbPreview | null>(null)
const bindPreviewLoading = ref(false)
const bindLoading = ref(false)
const bindNotes = ref<string[]>([])

async function doPreviewTmdb() {
  const id = Number(bindItemId.value)
  const tid = bindTmdbId.value.trim()
  if (!id) { ElMessage.warning('请先搜索并选择条目'); return }
  if (!tid) { ElMessage.warning('请填写 TMDB ID（或 IMDb ID）'); return }
  bindPreviewLoading.value = true
  bindPreview.value = null
  try {
    bindPreview.value = await previewTmdb(id, tid)
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '预览失败'))
  } finally {
    bindPreviewLoading.value = false
  }
}

async function doBindTmdb() {
  const id = Number(bindItemId.value)
  const tid = bindTmdbId.value.trim()
  if (!id || !tid) { bindConfirmVisible.value = false; return }
  bindConfirmVisible.value = false
  bindLoading.value = true
  try {
    const res: TmdbBindResult = await bindTmdb(id, tid, true, bindMode.value)
    bindNotes.value = [`「${res.item.name}」：${res.notes.join('；')}`]
    bindPreview.value = null
    ElMessage.success('已绑定并补全元数据')
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '绑定失败'))
  } finally {
    bindLoading.value = false
  }
}

/** 绑定确认弹窗：确认前选补全范围（默认仅补缺失） */
const bindConfirmVisible = ref(false)
const bindMode = ref<'missing' | 'all'>('missing')

const bindConfirmText = computed(() => {
  const id = Number(bindItemId.value)
  const tid = bindTmdbId.value.trim()
  if (bindPreview.value) {
    const year = bindPreview.value.year ? `（${bindPreview.value.year}）` : ''
    return `把「${bindPreview.value.current_name}」绑定到 TMDB「${bindPreview.value.title}${year}」吗？`
  }
  const kindLabel = /^tt\d+$/.test(tid) ? 'IMDb' : 'TMDB'
  return `把条目 ${id} 绑定到 ${kindLabel} ID ${tid} 吗？（未预览，建议先点「预览」确认）`
})

/** 点「绑定」先弹确认框（选补全范围），确认后再真正调接口 */
function openBindConfirm() {
  const id = Number(bindItemId.value)
  const tid = bindTmdbId.value.trim()
  if (!id) { ElMessage.warning('请先搜索并选择条目'); return }
  if (!tid) { ElMessage.warning('请填写 TMDB ID（或 IMDb ID）'); return }
  // 全量刷新会清空字段：每次打开都回到默认的「仅补缺失」，防止上次的选项被顺手沿用
  bindMode.value = 'missing'
  bindConfirmVisible.value = true
}

async function doUnbindTmdb() {
  const id = Number(bindItemId.value)
  if (!id) { ElMessage.warning('请先搜索并选择条目'); return }
  try {
    await ElMessageBox.confirm(
      `解绑条目 ${id} 的 TMDB ID？解绑后会重新排入补全队列。`,
      '确认解绑',
      { type: 'warning' },
    )
  } catch { return }
  bindLoading.value = true
  try {
    const res: TmdbBindResult = await bindTmdb(id, '', true)
    bindNotes.value = [`「${res.item.name}」：${res.notes.join('；')}`]
    bindPreview.value = null
    ElMessage.success('已解绑')
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '解绑失败'))
  } finally {
    bindLoading.value = false
  }
}

// ==================== 补全进度 ====================

const enrichProgress = ref<EnrichProgress | null>(null)
const enrichProgressLoading = ref(false)

/**
 * 阶段用时分解（v2.42.9）：按**累计耗时**降序——排最上面的就是最费时的那一段。
 * 状态计数只说「还有多少」，这一段回答「每条卡在哪」，是后面几批刮削优化的验收窗口。
 */
const enrichStageRows = computed(() => {
  const stages = enrichProgress.value?.stages || {}
  return Object.entries(stages)
    .map(([name, s]) => ({
      name,
      label: s.label || name,
      count: s.count || 0,
      avgMs: s.avg_ms || 0,
      totalMs: s.ms || 0,
    }))
    .filter((r) => r.count > 0)
    .sort((a, b) => b.totalMs - a.totalMs)
})

/** 速率窗口（秒 → 分钟，用于显示文案「近 N 分钟」） */
async function loadEnrichProgress() {
  enrichProgressLoading.value = true
  try {
    enrichProgress.value = await fetchEnrichProgress()
  } catch {
    enrichProgress.value = null // 出错不挡页面其它内容
  } finally {
    enrichProgressLoading.value = false
  }
}

// ==================== TMDB 密钥池与镜像（Phase 6a） ====================

const tmdbKeys = ref<TmdbKeysStatus | null>(null)
const tmdbKeysLoading = ref(false)
const newKey = ref('')
const keyAdding = ref(false)
const keyBusyIndex = ref<number | null>(null)
const keyTests = ref<TmdbTestResult[]>([])
const keyTesting = ref(false)
const mirror = ref<TmdbMirror | null>(null)
const mirrorForm = reactive({ api_base: '', image_base: '' })
const mirrorSaving = ref(false)

async function loadTmdbKeys() {
  tmdbKeysLoading.value = true
  try {
    tmdbKeys.value = await fetchTmdbKeys()
  } catch {
    tmdbKeys.value = null
  } finally {
    tmdbKeysLoading.value = false
  }
}

/** 密钥池每一行：序号 + 掩码 + 状态（冷却中的写出“还要 xx 秒”与原因） */
const keyRows = computed<TmdbKeyPoolRow[]>(() => tmdbKeys.value?.pool || [])

function keyStatus(row: TmdbKeyPoolRow): { text: string; cls: string } {
  if (row.cooling) return { text: `冷却中 · 还剩 ${row.cooldown_remaining}s`, cls: 'warn' }
  if (row.current) return { text: '正在使用', cls: 'ok' }
  return { text: '待命', cls: 'muted' }
}

/** 冷却原因 + 命中次数（悬停才看，避免表格过宽） */
function keyStatusTitle(row: TmdbKeyPoolRow): string {
  if (!row.reason) return ''
  return `${row.reason}（本进程命中 ${row.hits} 次）`
}

async function addKey() {
  const value = newKey.value.trim()
  if (!value) {
    ElMessage.warning('请填写 TMDB API Key')
    return
  }
  keyAdding.value = true
  try {
    const res = await addTmdbKey(value)
    newKey.value = ''
    if (res.effective) ElMessage.success(`已添加，密钥池现有 ${res.count} 把`)
    else ElMessage.warning(res.note || '已写入，但暂不生效')
    keyTests.value = []
    await loadTmdbKeys()
  } catch {
    /* 拦截器已提示（如 409：已经在池子里） */
  } finally {
    keyAdding.value = false
  }
}

async function removeKey(row: TmdbKeyPoolRow) {
  try {
    await ElMessageBox.confirm(
      `删除这把 TMDB Key（${row.masked ?? ''}）？删除后该 Key 不再参与轮询，可重新添加。`,
      '删除 TMDB Key',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  keyBusyIndex.value = row.index
  try {
    const res = await deleteTmdbKey(row.index)
    ElMessage.success(`已删除 ${res.removed}，还剩 ${res.count} 把`)
    keyTests.value = keyTests.value.filter((t) => t.index !== row.index)
    await loadTmdbKeys()
  } catch {
    /* 拦截器已提示 */
  } finally {
    keyBusyIndex.value = null
  }
}

/** 一键测试全部：测不通的那几把会被后端直接放进冷却 */
async function testAllKeys() {
  keyTesting.value = true
  try {
    const res = await testTmdbKeys()
    keyTests.value = res.results
    const okCount = res.results.filter((r) => r.ok).length
    if (!res.results.length) ElMessage.warning('密钥池是空的：先添加一把')
    else if (okCount === res.results.length) ElMessage.success(`${okCount} 把全部可用`)
    else ElMessage.warning(`${okCount}/${res.results.length} 把可用，其余已转入冷却`)
    await loadTmdbKeys()
  } catch {
    /* 拦截器已提示 */
  } finally {
    keyTesting.value = false
  }
}

async function resetCooldowns() {
  try {
    const res = await resetTmdbKeyCooldown()
    ElMessage.success(res.cleared ? `已清除 ${res.cleared} 把的冷却` : '当前没有密钥在冷却')
    await loadTmdbKeys()
  } catch {
    /* 拦截器已提示 */
  }
}

async function loadMirror() {
  try {
    mirror.value = await fetchTmdbMirror()
    mirrorForm.api_base = mirror.value.api_base
    mirrorForm.image_base = mirror.value.image_base
  } catch {
    mirror.value = null
  }
}

async function saveMirror() {
  mirrorSaving.value = true
  try {
    const res = await saveTmdbMirror({
      api_base: mirrorForm.api_base.trim(),
      image_base: mirrorForm.image_base.trim(),
    })
    ElMessage.success('已保存并生效（图片地址下一次刮削就用镜像）')
    mirrorForm.api_base = res.api_base
    mirrorForm.image_base = res.image_base
    await Promise.all([loadMirror(), loadTmdbKeys()])
  } catch {
    /* 拦截器已提示（400 会带上具体哪个地址不合法） */
  } finally {
    mirrorSaving.value = false
  }
}

/** 恢复官方地址：两个输入框清空后保存即可 */
function restoreDefaultMirror() {
  mirrorForm.api_base = ''
  mirrorForm.image_base = ''
}

// ==================== 多源元数据（Phase 6b） ====================

const meta = ref<MetaSourcesConfig | null>(null)
const metaLoading = ref(false)
const metaSaving = ref(false)
/** 本地草稿：开关、顺序、限速在改完点「保存」之前不动后端 */
const metaDraft = reactive({
  enabled: false,
  prefer_chinese: true,
  tmdb_miss_fallback: false,
  order: [] as string[],
  toggles: {} as Record<string, boolean>,
  rates: {} as Record<string, number>,
})
/** 哪个源的密钥框是张开的（默认收起，否则页面太长） */
const keyPanelOpen = ref('')
const keyInput = ref('')
const keyBusy = ref('')
const sourceKeyTests = ref<Record<string, Array<{ index: number; masked: string; ok: boolean; message: string }>>>({})
// ==================== 豆瓣（可选） ====================
const doubanCfg = ref<DoubanConfig | null>(null)
const doubanLoading = ref(false)
const doubanSaving = ref(false)
const doubanDraft = reactive({ enabled: false, min_interval: 1.0 })

const doubanDirty = computed(() => {
  if (!doubanCfg.value) return false
  return (
    doubanCfg.value.enabled !== doubanDraft.enabled ||
    Number(doubanCfg.value.min_interval) !== Number(doubanDraft.min_interval)
  )
})

async function loadDouban() {
  doubanLoading.value = true
  try {
    const cfg = await fetchDoubanConfig()
    doubanCfg.value = cfg
    doubanDraft.enabled = cfg.enabled
    doubanDraft.min_interval = Number(cfg.min_interval) || 1.0
  } catch {
    doubanCfg.value = null
  } finally {
    doubanLoading.value = false
  }
}

async function saveDouban() {
  doubanSaving.value = true
  try {
    const cfg = await saveDoubanConfig(doubanDraft.enabled, Number(doubanDraft.min_interval) || 1.0)
    doubanCfg.value = cfg
    doubanDraft.enabled = cfg.enabled
    doubanDraft.min_interval = Number(cfg.min_interval) || 1.0
    ElMessage.success('豆瓣配置已保存')
  } catch {
    ElMessage.error('保存失败，请重试')
  } finally {
    doubanSaving.value = false
  }
}

// ==================== 豆瓣限流降级（Batch D 入口收敛：从「媒体库 → 定时与刮削」收拢到此） ====================
const banDowngrade = ref<DoubanBanDowngradeConfig | null>(null)
const banDowngradeSaving = ref(false)
const banDowngradeError = ref(false)

async function loadBanDowngrade() {
  banDowngradeError.value = false
  try {
    banDowngrade.value = await fetchDoubanBanDowngrade()
  } catch {
    banDowngrade.value = null
    banDowngradeError.value = true
  }
}

async function saveBanDowngradeAction() {
  if (!banDowngrade.value) return
  banDowngradeSaving.value = true
  try {
    banDowngrade.value = await saveDoubanBanDowngrade(banDowngrade.value.enabled)
    ElMessage.success('豆瓣限流降级已保存，60 秒内生效')
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '保存失败'))
  } finally {
    banDowngradeSaving.value = false
  }
}

// ==================== 备用链路限速（配置瘦身第 3 批） ====================
// altmeta 是豆瓣主链路封禁/降级后的备用链路（豆瓣 subject_suggest + Bangumi 兜底）。
// 两个最小间隔秒数之前没有后台入口，这里补上，收进高级折叠（默认收起）。
const amRates = ref<AltmetaRates | null>(null)
const amLoading = ref(false)
const amSaving = ref(false)
const amDraft = reactive({ douban_rate: 1.0, bangumi_rate: 1.0 })

const amDirty = computed(() => {
  if (!amRates.value) return false
  return (
    Number(amRates.value.douban_rate) !== Number(amDraft.douban_rate) ||
    Number(amRates.value.bangumi_rate) !== Number(amDraft.bangumi_rate)
  )
})

async function loadAmRates() {
  amLoading.value = true
  try {
    const cfg = await fetchAltmetaRates()
    amRates.value = cfg
    amDraft.douban_rate = Number(cfg.douban_rate) || 0
    amDraft.bangumi_rate = Number(cfg.bangumi_rate) || 0
  } catch {
    amRates.value = null
  } finally {
    amLoading.value = false
  }
}

async function saveAmRates() {
  amSaving.value = true
  try {
    const cfg = await saveAltmetaRates({
      douban_rate: Math.max(0, Number(amDraft.douban_rate) || 0),
      bangumi_rate: Math.max(0, Number(amDraft.bangumi_rate) || 0),
    })
    amRates.value = { douban_rate: cfg.douban_rate, bangumi_rate: cfg.bangumi_rate }
    amDraft.douban_rate = cfg.douban_rate
    amDraft.bangumi_rate = cfg.bangumi_rate
    ElMessage.success('备用链路限速已保存，立即生效')
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '保存失败，请重试'))
  } finally {
    amSaving.value = false
  }
}

// ==================== TMDB 首选语言（Batch D 入口收敛：从「媒体库 → 定时与刮削」收拢到此） ====================
const tmdbLang = ref<TmdbLanguageStatus | null>(null)
const tmdbLangSaving = ref(false)
const tmdbLangError = ref(false)
const tmdbLangLabel = (v: string) => TMDB_LANGUAGE_LABELS[v] || v

async function loadTmdbLanguage() {
  tmdbLangError.value = false
  try {
    tmdbLang.value = await fetchTmdbLanguage()
  } catch {
    tmdbLang.value = null
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
    ElMessage.error(toAdminError(e, '保存失败'))
  } finally {
    tmdbLangSaving.value = false
  }
}

// ==================== TMDB 微调（配置瘦身第 3c 批：海报语言偏好 + 密钥冷却时长） ====================
const tmdbPosterLang = ref<TmdbPosterLanguageStatus | null>(null)
const tmdbPosterLangSaving = ref(false)
const tmdbPosterLangError = ref(false)
const TMDB_POSTER_LANG_LABELS: Record<string, string> = {
  system: '跟随系统', original: '原语言', 'zh-CN': '简体中文',
}
const tmdbPosterLangLabel = (v: string) => TMDB_POSTER_LANG_LABELS[v] || v

async function loadTmdbPosterLanguage() {
  tmdbPosterLangError.value = false
  try {
    tmdbPosterLang.value = await fetchTmdbPosterLanguage()
  } catch {
    tmdbPosterLang.value = null
    tmdbPosterLangError.value = true
  }
}

async function saveTmdbPosterLanguageAction() {
  if (!tmdbPosterLang.value) return
  tmdbPosterLangSaving.value = true
  try {
    const res = await saveTmdbPosterLanguage(tmdbPosterLang.value.language)
    tmdbPosterLang.value.language = res.language
    ElMessage.success('TMDB 海报语言偏好已保存，立即生效')
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '保存失败'))
  } finally {
    tmdbPosterLangSaving.value = false
  }
}

const tmdbCooldowns = ref<TmdbKeyCooldowns | null>(null)
const tmdbCooldownsSaving = ref(false)
const tmdbCooldownsError = ref(false)
const tmdbCooldownDraft = reactive({ cooldown_sec: 900, invalid_cooldown_sec: 21600 })

async function loadTmdbKeyCooldowns() {
  tmdbCooldownsError.value = false
  try {
    tmdbCooldowns.value = await fetchTmdbKeyCooldowns()
    tmdbCooldownDraft.cooldown_sec = Number(tmdbCooldowns.value.cooldown_sec) || 900
    tmdbCooldownDraft.invalid_cooldown_sec = Number(tmdbCooldowns.value.invalid_cooldown_sec) || 21600
  } catch {
    tmdbCooldowns.value = null
    tmdbCooldownsError.value = true
  }
}

async function saveTmdbKeyCooldownsAction() {
  tmdbCooldownsSaving.value = true
  try {
    const res = await saveTmdbKeyCooldowns(
      Number(tmdbCooldownDraft.cooldown_sec), Number(tmdbCooldownDraft.invalid_cooldown_sec))
    tmdbCooldowns.value = { ...(tmdbCooldowns.value as TmdbKeyCooldowns), ...res }
    ElMessage.success('TMDB 密钥冷却时长已保存，立即生效')
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '保存失败'))
  } finally {
    tmdbCooldownsSaving.value = false
  }
}

const probe = reactive({ title: '', year: '', kind: 'series' as 'series' | 'movie' })
const probeResult = ref<MetaSourceProbe | null>(null)
const probeOnly = ref('')
const probeNote = ref('')
const probing = ref(false)
const sourceTesting = ref('')

/** 按草稿顺序排好的源（顺序变了但还没保存时，页面也能看出新次序） */
const metaRows = computed<MetaSourceRow[]>(() => {
  const rows = meta.value?.sources || []
  if (!rows.length) return rows
  const byId = new Map(rows.map((r) => [r.id, r]))
  const ordered = metaDraft.order.map((id) => byId.get(id)).filter(Boolean) as MetaSourceRow[]
  rows.forEach((r) => {
    if (!metaDraft.order.includes(r.id)) ordered.push(r)
  })
  return ordered
})

const metaDirty = computed(() => {
  const cfg = meta.value
  if (!cfg) return false
  return (
    cfg.enabled !== metaDraft.enabled ||
    cfg.prefer_chinese !== metaDraft.prefer_chinese ||
    cfg.tmdb_miss_fallback !== metaDraft.tmdb_miss_fallback ||
    cfg.order.join(',') !== metaDraft.order.join(',') ||
    metaRows.value.some((r) => {
      const on = metaDraft.toggles[r.id] ?? r.enabled
      const rate = metaDraft.rates[r.id] ?? r.rate
      return on !== r.enabled || rate !== r.rate
    })
  )
})

const probeOutcomes = computed<MetaSourceOutcome[]>(() => probeResult.value?.outcomes || [])

async function loadMeta() {
  metaLoading.value = true
  try {
    const cfg = await fetchMetaSources()
    meta.value = cfg
    metaDraft.enabled = cfg.enabled
    metaDraft.prefer_chinese = cfg.prefer_chinese
    metaDraft.tmdb_miss_fallback = cfg.tmdb_miss_fallback
    metaDraft.order = [...cfg.order]
    metaDraft.toggles = {}
    metaDraft.rates = {}
  } catch {
    meta.value = null
  } finally {
    metaLoading.value = false
  }
}

function sourceToggle(row: MetaSourceRow): boolean {
  return metaDraft.toggles[row.id] ?? row.enabled
}

function sourceRate(row: MetaSourceRow): number {
  return metaDraft.rates[row.id] ?? row.rate
}

async function saveMeta() {
  metaSaving.value = true
  try {
    const cfg = await saveMetaSources({
      enabled: metaDraft.enabled,
      prefer_chinese: metaDraft.prefer_chinese,
      tmdb_miss_fallback: metaDraft.tmdb_miss_fallback,
      order: [...metaDraft.order],
      toggles: Object.fromEntries(
        metaRows.value.map((r) => [r.id, metaDraft.toggles[r.id] ?? r.enabled]),
      ),
      rates: Object.fromEntries(
        metaRows.value.map((r) => [r.id, Number(metaDraft.rates[r.id] ?? r.rate)]),
      ),
    })
    meta.value = cfg
    metaDraft.enabled = cfg.enabled
    metaDraft.prefer_chinese = cfg.prefer_chinese
    metaDraft.tmdb_miss_fallback = cfg.tmdb_miss_fallback
    metaDraft.order = [...cfg.order]
    metaDraft.toggles = {}
    metaDraft.rates = {}
    ElMessage.success(
      cfg.enabled
        ? `已保存并生效：${cfg.active_count} 个源参与采集`
        : '已保存：多源补全已关闭，补全走原来的 NFO / TMDB / 豆瓣链路',
    )
  } catch {
    /* 拦截器已提示 */
  } finally {
    metaSaving.value = false
  }
}

/** 丢弃未保存的改动 */
function revertMeta() {
  const cfg = meta.value
  if (!cfg) return
  metaDraft.enabled = cfg.enabled
  metaDraft.prefer_chinese = cfg.prefer_chinese
  metaDraft.tmdb_miss_fallback = cfg.tmdb_miss_fallback
  metaDraft.order = [...cfg.order]
  metaDraft.toggles = {}
  metaDraft.rates = {}
}

/** 上移 / 下移：只改草稿，点「保存」才写库 */
function moveSource(row: MetaSourceRow, offset: number) {
  const order = metaDraft.order
  const index = order.indexOf(row.id)
  const target = index + offset
  if (index < 0 || target < 0 || target >= order.length) return
  const next = [...order]
  next.splice(index, 1)
  next.splice(target, 0, row.id)
  metaDraft.order = next
}

/** 该源在**保存后**的位次（用于 ↑↓ 按钮的禁用状态） */
function draftPosition(row: MetaSourceRow): number {
  return metaDraft.order.indexOf(row.id)
}

/** 展开密钥池的那一行（null = 都没展开） */
const keyPanelRow = computed<MetaSourceRow | null>(
  () => metaRows.value.find((r) => r.id === keyPanelOpen.value) || null,
)

/** TMDB 的密钥在下方「TMDB 密钥池与镜像」卡片里管：这里只给个跳转，不弄第二个入口 */
const tmdbPoolRef = ref<HTMLElement | null>(null)

function scrollToTmdbPool() {
  tmdbPoolRef.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function toggleKeyPanel(row: MetaSourceRow) {
  keyPanelOpen.value = keyPanelOpen.value === row.id ? '' : row.id
  keyInput.value = ''
}

async function addSourceKey(row: MetaSourceRow) {
  const value = keyInput.value.trim()
  if (!value) {
    ElMessage.warning(`请填写「${row.label}」的密钥`)
    return
  }
  keyBusy.value = row.id
  try {
    meta.value = await addMetaSourceKey(row.id, value)
    keyInput.value = ''
    ElMessage.success(`已保存并生效，「${row.label}」现有 ${row.key_count + 1} 把`)
    delete sourceKeyTests.value[row.id]
  } catch {
    /* 拦截器已提示（409：已经在池子里） */
  } finally {
    keyBusy.value = ''
  }
}

async function removeSourceKey(row: MetaSourceRow, key: MetaSourceKeyRow) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.label}」的第 ${key.index} 把密钥（${key.masked}）吗？`,
      '删除密钥',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  keyBusy.value = row.id
  try {
    meta.value = await deleteMetaSourceKey(row.id, key.index)
    ElMessage.success('已删除')
    delete sourceKeyTests.value[row.id]
  } catch {
    /* 拦截器已提示 */
  } finally {
    keyBusy.value = ''
  }
}

async function clearSourceCooldown(row: MetaSourceRow) {
  try {
    const res = await resetMetaSourceCooldown(row.id)
    ElMessage.success(res.cleared ? `已清除 ${res.cleared} 把的冷却` : '当前没有密钥在冷却')
    meta.value = res.config
  } catch {
    /* 拦截器已提示 */
  }
}

/** 逐把试这个源的密钥（拿试采集里同一个片名去问） */
async function testSourceKeys(row: MetaSourceRow) {
  const title = probe.title.trim()
  if (!title) {
    ElMessage.warning('先在上面填一个试采集用的片名')
    return
  }
  sourceTesting.value = row.id
  try {
    const res = await testMetaSourceKeys(row.id, {
      title,
      year: probe.year ? Number(probe.year) : null,
      kind: probe.kind,
    })
    sourceKeyTests.value = { ...sourceKeyTests.value, [row.id]: res.results }
    meta.value = res.config
    const okCount = res.results.filter((r) => r.ok).length
    if (okCount === res.results.length) ElMessage.success(`${okCount} 把全部可用`)
    else ElMessage.warning(`${okCount}/${res.results.length} 把可用，其余已转入冷却`)
  } catch {
    /* 拦截器已提示（没配密钥时 400） */
  } finally {
    sourceTesting.value = ''
  }
}

/** 试采集：只看不写，告诉我们“开了这个源到底有没有用” */
async function runProbe() {
  const title = probe.title.trim()
  if (!title) {
    ElMessage.warning('请填写要试的片名')
    return
  }
  probing.value = true
  try {
    const res = await probeMetaSources({
      title,
      year: probe.year ? Number(probe.year) : null,
      kind: probe.kind,
      source: probeOnly.value,
    })
    probeResult.value = res.probe
    probeNote.value = res.note
    const hits = res.probe.outcomes.filter((o) => o.hit).map((o) => o.label)
    if (!hits.length) ElMessage.warning('没有源命中这部片')
    else ElMessage.success(`命中：${hits.join('、')}`)
  } catch {
    /* 拦截器已提示 */
  } finally {
    probing.value = false
  }
}

/** 试采集结果里，最后被哪个源填上的字段（按人看的顺序，不按字典序） */
const PROBE_FIELD_LABELS: Record<string, string> = {
  title: '标题',
  original_title: '原名',
  overview: '简介',
  genres: '类型',
  rating: '评分',
  year: '年份',
  poster: '海报',
}

const probeFieldRows = computed<Array<{ name: string; label: string; value: string }>>(() => {
  const fields = probeResult.value?.fields || {}
  return Object.keys(PROBE_FIELD_LABELS)
    .filter((key) => fields[key] !== undefined && fields[key] !== null && fields[key] !== '')
    .map((key) => ({
      name: key,
      label: PROBE_FIELD_LABELS[key],
      value: Array.isArray(fields[key]) ? (fields[key] as unknown[]).join('、')
        : String(fields[key]),
    }))
})

const probeExternalIds = computed<Array<{ site: string; id: string }>>(
  () => Object.entries(probeResult.value?.external_ids || {}).map(([site, id]) => ({ site, id })),
)

function outcomeBadge(row: MetaSourceOutcome): { text: string; cls: string } {
  if (row.skipped) return { text: row.skipped, cls: 'muted' }
  if (!row.ok) return { text: row.error || '失败', cls: 'danger' }
  if (row.hit) return { text: '命中', cls: 'ok' }
  return { text: '没搜到', cls: 'warn' }
}

onMounted(() => {
  loadEnrichProgress().catch(() => undefined)
  loadTmdbKeys().catch(() => undefined)
  loadMirror().catch(() => undefined)
  loadMeta().catch(() => undefined)
  loadDouban().catch(() => undefined)
  loadBanDowngrade().catch(() => undefined)
  loadAmRates().catch(() => undefined)
  loadTmdbLanguage().catch(() => undefined)
  loadTmdbPosterLanguage().catch(() => undefined)
  loadTmdbKeyCooldowns().catch(() => undefined)
  // Batch D 入口收敛：媒体库页用 hash 深链跳进来（#card-douban / #card-tmdb）
  // R40：querySelector 直接塞 hash 会在畸形值时抛 SyntaxError；改用 getElementById
  if (route.hash) {
    nextTick(() => {
      const el = document.getElementById(route.hash.slice(1))
      el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    })
  }
  // P1 入口前移：媒体库列表的「识别」按钮跳过来时带 ?item_id=xxx，
  // 直接填入条目 ID 并选中，跳过「搜条目」一步（深链刷新页面也要生效）
  const deepItemId = Number(route.query.item_id)
  if (deepItemId) {
    bindCardCollapsed.value = false
    localStorage.setItem('aetrix_bind_tmdb_collapsed', '0')
    bindItemId.value = String(deepItemId)
    bindSelectedItem.value = {
      id: deepItemId,
      name: `条目 #${deepItemId}`,
      year: null,
      item_type: '',
      tmdb_id: null,
      metadata_locked: false,
    }
    bindPreview.value = null
    bindNotes.value = []
  }
})
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="媒体与交付"
      title="元数据来源"
      description="条目这一层的元数据从哪来、错了怎么纠、补全队列跑到哪一步。按库的扫描与刮削策略仍在「媒体库」页。"
    >
      <template #actions>
        <RouterLink to="/emby" class="au-btn au-btn-ghost au-btn-sm">去媒体库页</RouterLink>
        <el-button size="small" :loading="enrichProgressLoading" @click="loadEnrichProgress">
          <RefreshCw :size="14" style="margin-right: 4px" />刷新进度
        </el-button>
      </template>
    </PageHeader>

    <div class="ms-grid">
      <!-- 0. 多源元数据补全（Phase 6b）：总开关 / 中文优先 / 顺序 / 逐源开关与密钥池 -->
      <SectionCard class="ms-card ms-card-wide" title="多源元数据补全" :icon="Layers">
        <template #actions>
          <div class="ms-facts">
            <span v-if="meta" class="fact" :class="meta.enabled ? 'ok' : 'muted'">
              总开关：{{ meta.enabled ? '已开启' : '已关闭' }}
            </span>
            <span v-if="meta" class="fact">
              {{ meta.active_count }} / {{ meta.sources.length }} 个源参与
              <template v-if="!meta.enabled">（总开关关着，暂不参与）</template>
            </span>
            <span v-if="metaDirty" class="fact warn">有未保存的改动</span>
          </div>
        </template>
        <p class="ms-hint">
          TMDB 对中文剧集 / 综艺收录偏少。总开关和「TMDB 未命中时走多源兜底」
          同时开着时，TMDB 没搜到的中文条目会按下面的顺序
          再问一遍其它源：<strong>标题 / 简介 / 类型 / 评分逐个字段按序填充</strong>，
          某个源没有的字段自动让给下一个源；任何一个源失败（限流 / 密钥失效 / 反爬）
          都只影响它自己，不会中断整条刮削。命中的各源外部 ID 会合并存到条目上。
        </p>

        <div v-if="metaLoading && !meta" class="ms-skeleton" aria-busy="true">
          <div class="au-skeleton" /><div class="au-skeleton" /><div class="au-skeleton" />
        </div>
        <EmptyState
          v-else-if="!meta"
          compact
          :icon="Layers"
          title="读取多源配置失败"
          description="可能是网络或后端暂时不可用。"
        >
          <template #actions>
            <el-button size="small" :loading="metaLoading" @click="loadMeta">重试</el-button>
          </template>
        </EmptyState>
        <template v-else>
          <el-alert
            v-if="!meta.enabled"
            type="info"
            :closable="false"
            show-icon
            class="ms-alert"
          >
            总开关关着：补全链路与升级前完全一致（NFO → TMDB → 豆瓣 / Bangumi 兵底），
            下面的配置不会生效，但可以先配好、随时打开。
          </el-alert>

          <div class="ms-switches">
            <div class="ms-switch">
              <el-switch v-model="metaDraft.enabled" />
              <div>
                <div class="ms-switch-title">启用多源补全</div>
                <div class="ms-hint">关 = 走原来的单源链路；开 = TMDB 没命中时按顺序问其它源</div>
              </div>
            </div>
            <div class="ms-switch">
              <el-switch v-model="metaDraft.prefer_chinese" />
              <div>
                <div class="ms-switch-title">中文信息优先</div>
                <div class="ms-hint">
                  只影响<strong>标题与简介</strong>：中文源的文案压过优先级更高但给英文的源；
                  类型、评分、海报、外部 ID 仍按顺序取
                </div>
              </div>
            </div>
            <div class="ms-switch ms-switch-sub">
              <el-switch
                v-model="metaDraft.tmdb_miss_fallback"
                :disabled="!metaDraft.enabled"
              />
              <div>
                <div class="ms-switch-title">
                  TMDB 未命中时走多源兜底
                  <span class="ms-sub-badge">总开关的子项</span>
                </div>
                <div class="ms-hint">
                  现在起降级为总开关的子项：统一兜底链（一个豆瓣入口）
                  启用时由总开关统一控制，不再独立门控；保留键值用于回滚。
                  未启用时保持原语义——只对<strong>中文标题的剧 / 电影</strong>生效：
                  TMDB 搜过无命中、豆瓣也没兜上时，按上面的顺序再问其它源
                </div>
              </div>
            </div>
          </div>

          <div class="ms-sub"><ListOrdered :size="13" style="margin-right: 4px" />源优先级</div>
          <p class="ms-hint">
            越靠前越先问。默认把中文源（Bangumi / 豆瓣）放前面治中文命中率，
            TMDB 放最后兜底（它字段最全但中文收录少）。
            一次最多问 {{ meta.max_sources }} 个源、限时 {{ meta.collect_timeout_sec }} 秒。
          </p>

          <table class="ms-table ms-table-order">
            <thead>
              <tr>
                <th style="width: 46px">顺序</th>
                <th>数据源</th>
                <th style="width: 96px">参与</th>
                <th style="width: 130px">限速</th>
                <th style="width: 92px">密钥</th>
                <th style="width: 168px">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in metaRows" :key="row.id" :class="{ 'ms-off': !sourceToggle(row) }">
                <td>
                  <div class="ms-order-cell">
                    <span class="ms-pos">{{ draftPosition(row) + 1 }}</span>
                    <span class="ms-order-btns">
                      <el-button
                        size="small"
                        text
                        :disabled="draftPosition(row) <= 0"
                        title="上移（优先问这个源）"
                        @click="moveSource(row, -1)"
                      ><ArrowUp :size="13" /></el-button>
                      <el-button
                        size="small"
                        text
                        :disabled="draftPosition(row) >= metaRows.length - 1"
                        title="下移"
                        @click="moveSource(row, 1)"
                      ><ArrowDown :size="13" /></el-button>
                    </span>
                  </div>
                </td>
                <td class="ms-name-cell">
                  <div class="ms-source-name">
                    {{ row.label }}
                    <span v-if="row.lang === 'zh'" class="mini-badge ok">中文强</span>
                  </div>
                  <div class="ms-note">{{ row.note }}</div>
                  <div v-if="row.skipped_reason" class="ms-note warn">{{ row.skipped_reason }}</div>
                </td>
                <td class="ms-toggle-cell">
                  <el-switch
                    :model-value="sourceToggle(row)"
                    @update:model-value="(v: string | number | boolean) => (metaDraft.toggles[row.id] = Boolean(v))"
                  />
                </td>
                <td class="ms-rate-cell">
                  <span class="ms-cell-label">限速</span>
                  <el-input
                    :model-value="sourceRate(row)"
                    type="number"
                    size="small"
                    :min="0"
                    :step="0.5"
                    :disabled="!sourceToggle(row)"
                    @update:model-value="(v: string | number) => (metaDraft.rates[row.id] = Number(v))"
                  >
                    <template #append>秒</template>
                  </el-input>
                </td>
                <td class="ms-keys-cell">
                  <span class="ms-cell-label">密钥</span>
                  <span v-if="row.requires_key">
                    <template v-if="row.key_count">
                      {{ row.key_count }} 把<template v-if="row.cooling"> · 冷却 {{ row.cooling }}</template>
                    </template>
                    <span v-else class="mini-badge warn">未填</span>
                    <div v-if="row.keys_legacy" class="ms-note warn">
                      检测到旧版遗留的密钥配置，启动时会自动合并到下方密钥池
                    </div>
                  </span>
                  <span v-else class="muted">无需密钥</span>
                </td>
                <td class="ms-ops-cell">
                  <span class="ms-cell-label">操作</span>
                  <div class="ms-row-ops">
                    <!-- TMDB：密钥只在下方「TMDB 密钥池与镜像」里填（单一入口），
                         这里不再给第二个输入框，否则同一把钥匙会出现两个入口 -->
                    <el-button
                      v-if="row.key_entry === 'pool_card'"
                      size="small"
                      text
                      @click="scrollToTmdbPool"
                    >去密钥池</el-button>
                    <template v-else-if="row.requires_key">
                      <el-button size="small" text @click="toggleKeyPanel(row)">
                        {{ keyPanelOpen === row.id ? '收起' : '密钥' }}
                      </el-button>
                      <el-button
                        size="small"
                        text
                        :loading="sourceTesting === row.id"
                        :disabled="!row.key_count"
                        title="拿上面的片名逐把试这个源的密钥"
                        @click="testSourceKeys(row)"
                      >测试</el-button>
                    </template>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>

          <!-- 逐源密钥池：默认收起，展开后逐把增删 + 冷却 -->
          <div v-if="keyPanelOpen && keyPanelRow && keyPanelRow.key_entry !== 'pool_card'" class="ms-keypanel">
            <div class="ms-keypanel-inner">
              <div class="ms-panel-title">
                <KeyRound :size="13" style="margin-right: 4px" />{{ keyPanelRow.label }} 密钥池
                <span class="muted">（原文不会出现在接口里，只显示后 4 位）</span>
              </div>
              <p v-if="keyPanelRow.apply_hint" class="ms-note">
                {{ keyPanelRow.apply_hint }}
                <a
                  v-if="keyPanelRow.apply_url"
                  :href="keyPanelRow.apply_url"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="ms-apply-link"
                >{{ keyPanelRow.apply_url }}</a>
              </p>
              <p v-if="keyPanelRow.key_storage" class="ms-note">
                存在配置项 <code class="mono">{{ keyPanelRow.key_storage }}</code>
                （写入即生效，下一轮采集就用它，不用重启）。
              </p>
              <div v-if="!keyPanelRow.keys.length" class="ms-hint">
                还没有密钥：这个源现在<strong>不会参与采集</strong>。
              </div>
              <div v-for="key in keyPanelRow.keys" :key="key.index" class="ms-key-row">
                <span class="muted">{{ key.index }}</span>
                <span class="mono">{{ key.masked }}</span>
                <span
                  class="mini-badge"
                  :class="key.cooling ? 'warn' : 'ok'"
                  :title="key.reason ? `${key.reason}（本进程命中 ${key.hits} 次）` : ''"
                >
                  {{ key.cooling ? `冷却中 · 还剩 ${key.cooldown_remaining}s` : '待命' }}
                </span>
                <el-button
                  size="small"
                  text
                  type="danger"
                  :loading="keyBusy === keyPanelRow.id"
                  @click="removeSourceKey(keyPanelRow, key)"
                >删除</el-button>
              </div>
              <div v-if="sourceKeyTests[keyPanelRow.id]?.length" class="ms-results">
                <div v-for="t in sourceKeyTests[keyPanelRow.id]" :key="t.index" class="ms-result">
                  <span class="mini-badge" :class="t.ok ? 'ok' : 'danger'">
                    {{ t.ok ? '可用' : '不可用' }}
                  </span>
                  <span class="mono">{{ t.masked }}</span>
                  <span class="muted">{{ t.message }}</span>
                </div>
              </div>
              <div class="ms-actions">
                <el-input
                  v-model="keyInput"
                  placeholder="粘贴一把新的密钥"
                  style="width: 300px"
                  clearable
                />
                <el-button
                  size="small"
                  type="primary"
                  :loading="keyBusy === keyPanelRow.id"
                  @click="addSourceKey(keyPanelRow)"
                >
                  保存并生效
                </el-button>
                <el-button size="small" :loading="sourceTesting === keyPanelRow.id" @click="testSourceKeys(keyPanelRow)">
                  <PlugZap :size="14" style="margin-right: 4px" />测试全部
                </el-button>
                <el-button size="small" :disabled="!keyPanelRow.cooling" @click="clearSourceCooldown(keyPanelRow)">
                  清除冷却
                </el-button>
              </div>
            </div>
          </div>

          <div class="ms-actions ms-block">
            <el-button size="small" type="primary" :loading="metaSaving" @click="saveMeta">保存</el-button>
            <el-button size="small" :disabled="!metaDirty" @click="revertMeta">还原</el-button>
            <el-button size="small" text :loading="metaLoading" @click="loadMeta">
              <RefreshCw :size="14" />
            </el-button>
          </div>

          <!-- 试采集：开一个源之前先用它试一下能不能搜到这部片 -->
          <div class="ms-sub"><Search :size="13" style="margin-right: 4px" />试采集（只看不写）</div>
          <p class="ms-hint">
            用一个真实片名跑一遍，看看每个源能不能命中、最后哪个字段被哪个源填上。
            不会写入任何数据，总开关关着也能试（临时试采集）。
          </p>
          <div class="ms-actions">
            <el-input v-model="probe.title" placeholder="片名，如：落语朱音" style="width: 220px" clearable />
            <el-input v-model="probe.year" placeholder="年份（可空）" style="width: 130px" clearable />
            <el-select v-model="probe.kind" style="width: 120px">
              <el-option label="剧集" value="series" />
              <el-option label="电影" value="movie" />
            </el-select>
            <el-select v-model="probeOnly" placeholder="全部源" clearable style="width: 170px">
              <el-option v-for="row in metaRows" :key="row.id" :label="row.label" :value="row.id" />
            </el-select>
            <el-button size="small" type="primary" :loading="probing" @click="runProbe">试采集</el-button>
          </div>

          <div v-if="probeResult" class="ms-probe">
            <p class="ms-hint ms-block">
              逐源结果{{ probeNote ? `（${probeNote}）` : '' }}
              <span v-if="probeResult.primary">｜标题取自 {{ probeResult.primary }}</span>
            </p>
            <div class="ms-results">
              <div v-for="row in probeOutcomes" :key="row.source" class="ms-result">
                <span class="mini-badge" :class="outcomeBadge(row).cls">{{ outcomeBadge(row).text }}</span>
                <span>{{ row.label }}</span>
                <span class="muted">{{ row.elapsed_ms }}ms</span>
              </div>
            </div>
            <div v-if="probeFieldRows.length" class="ms-results">
              <div v-for="f in probeFieldRows" :key="f.name" class="ms-result">
                <span class="mini-badge">{{ f.label }}</span>
                <span class="ms-probe-value">{{ f.value }}</span>
              </div>
            </div>
            <div v-if="probeExternalIds.length" class="ms-results">
              <span v-for="row in probeExternalIds" :key="row.site" class="mini-badge mono">
                {{ row.site }}: {{ row.id }}
              </span>
            </div>
          </div>
        </template>
      </SectionCard>

      <!-- 0.5 豆瓣（可选）：中文兜底源，默认关闭。Batch D：限流降级从媒体库页收拢到此 -->
      <SectionCard id="card-douban" class="ms-card" title="豆瓣（可选）" :icon="BookOpen">
        <template #actions>
          <div class="ms-facts">
            <span v-if="doubanCfg" class="fact" :class="doubanCfg.enabled ? 'ok' : 'muted'">
              {{ doubanCfg.enabled ? '已开启' : '已关闭' }}
            </span>
            <span v-if="doubanDirty" class="fact warn">有未保存的改动</span>
          </div>
        </template>
        <p class="ms-hint">
          豆瓣是<strong>可选</strong>的中文兜底源，<strong>默认关闭</strong>。
          开启后，<strong>仅在 TMDB 未命中时</strong>对含中文的标题走豆瓣搜索：
          搜中则取详情（标题 / 简介 / 评分 / 海报）写库，没搜中则保持原状。
          关闭时不发出任何豆瓣请求，中文标题也直接走 TMDB。
          豆瓣限流 / 封禁期内自动跳过（零豆瓣请求），解封后由后台补刮。
          定时扫描、单集刮削等仍在「<router-link :to="{ name: 'EmbyAdmin' }" class="ms-link">媒体库 → 定时与刮削</router-link>」。
        </p>
        <div v-if="doubanLoading && !doubanCfg" class="ms-skeleton" aria-busy="true">
          <div class="au-skeleton" /><div class="au-skeleton" />
        </div>
        <EmptyState
          v-else-if="!doubanCfg"
          compact
          :icon="BookOpen"
          title="读取豆瓣配置失败"
          description="可能是网络或后端暂时不可用。"
        >
          <template #actions>
            <el-button size="small" :loading="doubanLoading" @click="loadDouban">重试</el-button>
          </template>
        </EmptyState>
        <template v-else>
          <div class="ms-switches">
            <div class="ms-switch">
              <el-switch v-model="doubanDraft.enabled" />
              <div>
                <div class="ms-switch-title">启用豆瓣中文兜底</div>
                <div class="ms-hint">关 = 不打任何豆瓣请求，中文标题也直接走 TMDB（默认）</div>
              </div>
            </div>
          </div>
          <div class="ms-field">
            <span class="ms-field-label">请求间隔（秒）</span>
            <el-input-number
              v-model="doubanDraft.min_interval"
              :min="0"
              :max="10"
              :step="0.5"
              size="small"
              style="width: 140px"
            />
            <span class="ms-hint">两次豆瓣请求之间的最小间隔，防反爬封 IP（默认 1.0）</span>
          </div>
          <div class="ms-actions">
            <el-button
              type="primary"
              size="small"
              :loading="doubanSaving"
              :disabled="!doubanDirty"
              @click="saveDouban"
            >保存</el-button>
          </div>

          <!-- Batch D 入口收敛：豆瓣限流降级从「媒体库 → 定时与刮削」收拢到此，唯一写入口 -->
          <div class="ms-sub">豆瓣限流降级</div>
          <p class="ms-hint">
            豆瓣限流 / 冷却期内，中文条目不再整条延后等解封——跳过豆瓣这一站，照常走 TMDB → 多源兜底；
            期间命中从严（标题 + 年份都对上才落库，宁可没命中也不乱认）。解封后自动恢复原顺序（保存后 60 秒内生效）。
          </p>
          <div v-if="banDowngrade" class="ms-actions">
            <el-switch v-model="banDowngrade.enabled" active-text="开启" inactive-text="关闭" aria-label="豆瓣限流降级开关" />
            <el-button type="primary" size="small" :loading="banDowngradeSaving" @click="saveBanDowngradeAction">保存</el-button>
            <span class="mini-badge" :class="banDowngrade.banned ? 'warn' : 'ok'">
              {{ banDowngrade.banned ? `限流中（剩余约 ${Math.round(banDowngrade.ban_remaining_sec)} 秒），降级${banDowngrade.enabled ? '生效中' : '未开启'}` : '豆瓣正常' }}
            </span>
          </div>
          <div v-else-if="banDowngradeError" class="ms-hint">
            读取失败 <el-button size="small" text @click="loadBanDowngrade">重试</el-button>
          </div>
          <div v-else class="ms-skeleton" aria-busy="true"><div class="au-skeleton" /></div>
        </template>
      </SectionCard>

      <!-- 0.6 备用链路限速（配置瘦身第 3 批）：altmeta 豆瓣备用 + Bangumi 兜底的请求间隔 -->
      <SectionCard id="card-altmeta" class="ms-card" title="备用链路限速" :icon="Gauge">
        <template #actions>
          <div class="ms-facts">
            <span v-if="amDirty" class="fact warn">有未保存的改动</span>
          </div>
        </template>
        <p class="ms-hint">
          豆瓣主链路被封禁/降级时，刮削走备用链路：豆瓣 subject_suggest + Bangumi 兜底。
          这里是备用链路自己的请求间隔，<strong>不是</strong>上面「豆瓣（可选）」卡里的间隔——
          两个链路各限各的。0 = 不限速（默认 1.0 秒）。
          注意：上面「豆瓣（可选）」开关关闭时，备用链路里的豆瓣请求同样不发出（只走 Bangumi）。
        </p>
        <div v-if="amLoading && !amRates" class="ms-skeleton" aria-busy="true">
          <div class="au-skeleton" /><div class="au-skeleton" />
        </div>
        <EmptyState
          v-else-if="!amRates"
          compact
          :icon="Gauge"
          title="读取备用链路配置失败"
          description="可能是网络或后端暂时不可用。"
        >
          <template #actions>
            <el-button size="small" :loading="amLoading" @click="loadAmRates">重试</el-button>
          </template>
        </EmptyState>
        <AdvancedSettings
          v-else
          count="2 项"
          description="备用链路的请求间隔：太大刮得慢，太小容易被封 IP。"
        >
          <div class="ms-field">
            <span class="ms-field-label">豆瓣备用间隔（秒）</span>
            <el-input-number
              v-model="amDraft.douban_rate"
              :min="0"
              :max="60"
              :step="0.5"
              size="small"
              style="width: 140px"
            />
            <span class="ms-hint">备用链路豆瓣请求的最小间隔（默认 1.0，0=不限速）</span>
          </div>
          <div class="ms-field">
            <span class="ms-field-label">Bangumi 兜底间隔（秒）</span>
            <el-input-number
              v-model="amDraft.bangumi_rate"
              :min="0"
              :max="60"
              :step="0.5"
              size="small"
              style="width: 140px"
            />
            <span class="ms-hint">Bangumi 兜底请求的最小间隔（默认 1.0，0=不限速）</span>
          </div>
          <div class="ms-actions">
            <el-button
              type="primary"
              size="small"
              :loading="amSaving"
              :disabled="!amDirty"
              @click="saveAmRates"
            >保存</el-button>
          </div>
        </AdvancedSettings>
      </SectionCard>

      <!-- 1. TMDB 密钥池与镜像（Phase 6a）：多把轮换、逐把增删、失效/限流自动冷却 -->
      <!-- Batch D：首选语言从「媒体库 → 定时与刮削」收拢到此 -->
      <div id="card-tmdb" ref="tmdbPoolRef" class="ms-card-wide">
      <SectionCard class="ms-card" title="TMDB 密钥池与镜像" :icon="KeyRound">
        <template #actions>
          <div class="ms-facts">
            <span v-if="tmdbKeys" class="fact">
              来源：{{ tmdbKeys.source === 'env' ? '环境变量' : tmdbKeys.source === 'db' ? '后台填写' : '未配置' }}
            </span>
            <span v-if="tmdbKeys" class="fact">共 {{ tmdbKeys.count }} 把</span>
            <span v-if="tmdbKeys?.keys_cooling" class="fact warn">
              冷却中 {{ tmdbKeys.keys_cooling }}
            </span>
            <span v-if="tmdbKeys" class="fact">实际 {{ tmdbKeys.rate ?? 0 }}/秒（上限 {{ tmdbKeys.rate_ceiling ?? 0 }}）</span>
          </div>
        </template>
        <p class="ms-hint">
          多把密钥轮着：一把被限流（429）或失效（401）会自动冷却并切到下一把，
          冷却时长走配置（当前 429 {{ mirror?.cooldown_sec ?? '—' }} 秒 / 401
          {{ mirror?.invalid_cooldown_sec ?? '—' }} 秒）。密钥原文不会出现在接口里，只显示后 4 位。
        </p>
        <el-alert
          v-if="tmdbKeys?.env_present"
          type="warning"
          :closable="false"
          show-icon
          class="ms-alert"
        >
          环境变量 <code>TMDB_API_KEYS</code> 里已经有密钥，它优先于后台填写的池子：
          下面添加的密钥会存下来但<strong>暂不生效</strong>。
        </el-alert>

        <div v-if="tmdbKeysLoading && !tmdbKeys" class="ms-skeleton" aria-busy="true">
          <div class="au-skeleton" /><div class="au-skeleton" />
        </div>
        <EmptyState
          v-else-if="!tmdbKeys"
          compact
          :icon="KeyRound"
          title="读取密钥池失败"
          description="可能是网络或后端暂时不可用。"
        >
          <template #actions>
            <el-button size="small" :loading="tmdbKeysLoading" @click="loadTmdbKeys">重试</el-button>
          </template>
        </EmptyState>
        <EmptyState
          v-else-if="!keyRows.length"
          compact
          :icon="KeyRound"
          title="还没有 TMDB 密钥"
          description="没有密钥时刮削会静默跳过。在下方输入框添一把（可多添几把轮着用），填完点「测试全部」先确认能通。"
        />
        <table v-else class="ms-table">
          <thead>
            <tr><th>#</th><th>密钥</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in keyRows" :key="row.index">
              <td class="muted">{{ row.index }}</td>
              <td class="mono">{{ row.masked }}</td>
              <td>
                <span class="mini-badge" :class="keyStatus(row).cls" :title="keyStatusTitle(row)">
                  {{ keyStatus(row).text }}
                </span>
                <div v-if="row.reason" class="ms-sub muted">{{ row.reason }}</div>
              </td>
              <td>
                <el-button
                  size="small"
                  text
                  type="danger"
                  :loading="keyBusyIndex === row.index"
                  @click="removeKey(row)"
                >删除</el-button>
              </td>
            </tr>
          </tbody>
        </table>

        <div class="ms-actions">
          <el-input
            v-model="newKey"
            placeholder="粘贴一把新的 TMDB API Key"
            style="width: 320px"
            clearable
          />
          <el-button size="small" type="primary" :loading="keyAdding" @click="addKey">添加</el-button>
          <el-button size="small" :loading="keyTesting" @click="testAllKeys">
            <PlugZap :size="14" style="margin-right: 4px" />测试全部
          </el-button>
          <el-button size="small" :disabled="!tmdbKeys?.keys_cooling" @click="resetCooldowns">
            清除冷却
          </el-button>
          <el-button size="small" text :loading="tmdbKeysLoading" @click="loadTmdbKeys">
            <RefreshCw :size="14" />
          </el-button>
        </div>
        <div v-if="keyTests.length" class="ms-results">
          <div v-for="t in keyTests" :key="t.index" class="ms-result">
            <span class="mini-badge" :class="t.ok ? 'ok' : 'danger'">{{ t.ok ? '可用' : '不可用' }}</span>
            <span class="mono">{{ t.masked }}</span>
            <span class="muted">{{ t.message }}</span>
          </div>
        </div>

        <div class="ms-sub">镜像 / 反代地址（境内直连不通时填）</div>
        <p class="ms-hint">
          API 与图片 CDN <strong>分开配</strong>：常见情况是图片走镜像、API 直连（或反过来）。
          留空 = 用官方地址。环境变量 <code>TMDB_API_BASE</code> / <code>TMDB_IMAGE_BASE</code> 优先。
        </p>
        <div class="ms-actions">
          <el-input v-model="mirrorForm.api_base" placeholder="API 地址，如 https://tmdb.example.com/3" style="width: 300px" />
          <el-input v-model="mirrorForm.image_base" placeholder="图片 CDN，如 https://img.example.com/t/p" style="width: 300px" />
          <el-button size="small" type="primary" :loading="mirrorSaving" @click="saveMirror">保存镜像</el-button>
          <el-button size="small" text @click="restoreDefaultMirror">清空（恢复官方）</el-button>
        </div>
        <p v-if="mirror" class="ms-hint">
          当前生效：API <code class="mono">{{ mirror.api_base }}</code>
          {{ mirror.api_base_from_env ? '（来自环境变量，后台保存不生效）' : '' }}
          ｜图片 <code class="mono">{{ mirror.image_base }}</code>
          {{ mirror.image_base_from_env ? '（来自环境变量）' : '' }}
        </p>

        <!-- Batch D 入口收敛：TMDB 首选语言从「媒体库 → 定时与刮削」收拢到此，唯一写入口 -->
        <div class="ms-sub">TMDB 首选语言</div>
        <p class="ms-hint">TMDB 返回的简介 / 标题 / 别名用哪种语言（保存后立即生效）</p>
        <div v-if="tmdbLang" class="ms-actions">
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
        <div v-else-if="tmdbLangError" class="ms-hint">
          读取失败 <el-button size="small" text @click="loadTmdbLanguage">重试</el-button>
        </div>
        <div v-else class="ms-skeleton" aria-busy="true"><div class="au-skeleton" /></div>
        <div v-if="tmdbLang?.from_env" class="ms-hint">
          环境变量 TMDB_LANGUAGE 覆盖了这里的设置（改这里不会生效）
        </div>

        <!-- 配置瘦身第 3c 批：TMDB 微调（海报语言偏好 + 密钥冷却时长）收进高级折叠 -->
        <AdvancedSettings
          count="3 项"
          description="海报图语言与密钥冷却策略：一般不用动，默认即可。"
        >
          <div class="ms-sub">TMDB 海报语言偏好</div>
          <p class="ms-hint">海报图请求哪种语言的版本（保存后立即生效）</p>
          <div v-if="tmdbPosterLang" class="ms-actions">
            <el-select v-model="tmdbPosterLang.language" style="width: 160px" aria-label="TMDB 海报语言偏好">
              <el-option
                v-for="v in tmdbPosterLang.options"
                :key="v"
                :label="tmdbPosterLangLabel(v)"
                :value="v"
              />
            </el-select>
            <el-button type="primary" size="small" :loading="tmdbPosterLangSaving" @click="saveTmdbPosterLanguageAction">
              保存
            </el-button>
          </div>
          <div v-else-if="tmdbPosterLangError" class="ms-hint">
            读取失败 <el-button size="small" text @click="loadTmdbPosterLanguage">重试</el-button>
          </div>
          <div v-else class="ms-skeleton" aria-busy="true"><div class="au-skeleton" /></div>

          <div class="ms-sub">TMDB 密钥冷却时长</div>
          <p class="ms-hint">
            密钥被限流（429）或失效（401）后冷却多久再试。环境变量
            <code>TMDB_KEY_COOLDOWN_SEC</code> / <code>TMDB_KEY_INVALID_COOLDOWN_SEC</code>
            优先于这里的设置。
          </p>
          <div v-if="tmdbCooldowns" class="ms-field">
            <span class="ms-field-label">限流冷却（秒）</span>
            <el-input-number
              v-model="tmdbCooldownDraft.cooldown_sec"
              :min="1"
              :step="60"
              size="small"
              style="width: 140px"
            />
            <span class="ms-hint">默认 {{ tmdbCooldowns.defaults.cooldown_sec }} 秒</span>
          </div>
          <div v-if="tmdbCooldowns" class="ms-field">
            <span class="ms-field-label">失效冷却（秒）</span>
            <el-input-number
              v-model="tmdbCooldownDraft.invalid_cooldown_sec"
              :min="1"
              :step="600"
              size="small"
              style="width: 140px"
            />
            <span class="ms-hint">默认 {{ tmdbCooldowns.defaults.invalid_cooldown_sec }} 秒（约 6 小时）</span>
          </div>
          <div v-if="tmdbCooldowns?.from_env.cooldown_sec || tmdbCooldowns?.from_env.invalid_cooldown_sec" class="ms-hint">
            环境变量覆盖了这里的设置（改这里不会生效）
          </div>
          <div v-if="tmdbCooldowns" class="ms-actions">
            <el-button type="primary" size="small" :loading="tmdbCooldownsSaving" @click="saveTmdbKeyCooldownsAction">
              保存
            </el-button>
          </div>
          <div v-else-if="tmdbCooldownsError" class="ms-hint">
            读取失败 <el-button size="small" text @click="loadTmdbKeyCooldowns">重试</el-button>
          </div>
          <div v-else class="ms-skeleton" aria-busy="true"><div class="au-skeleton" /></div>
        </AdvancedSettings>
      </SectionCard>
      </div>
      <!-- 2. 手动绑定 TMDB（默认折叠） -->
      <SectionCard class="ms-card" title="手动绑定 TMDB" :icon="Wand2">
        <template #actions>
          <el-button size="small" text :aria-expanded="!bindCardCollapsed" @click="toggleBindCard">
            <ChevronDown :size="14" class="ms-chevron" :class="{ 'is-open': !bindCardCollapsed }" />
            {{ bindCardCollapsed ? '展开' : '收起' }}
          </el-button>
        </template>
        <div v-show="!bindCardCollapsed">
        <p class="ms-hint">
          TMDB 对中文剧集 / 综艺收录偏少，自动刮削搜不到的条目在这里手动识别。
          第一步搜库内条目并选中，第二步搜 TMDB 候选、一键绑定（或手动输入 TMDB ID / tt 开头的 IMDb ID）；绑定后自动补全缺失的图 / 简介 / IMDb / 别名。
        </p>
        <div class="ms-step">第一步 · 搜库内条目</div>
        <div class="ms-actions">
          <el-input v-model="bindSearchQ" placeholder="剧名关键字，如：黑鸟" style="width: 200px" clearable @keyup.enter="doSearchBindItems" />
          <el-input v-model="bindSearchYear" placeholder="年份（可选）" style="width: 110px" clearable @keyup.enter="doSearchBindItems" />
          <el-button size="small" :loading="bindSearchLoading" @click="doSearchBindItems">
            搜索条目
          </el-button>
        </div>
        <div v-if="bindSearchResults.length" class="ms-results">
          <div
            v-for="item in bindSearchResults"
            :key="item.id"
            class="ms-result"
            :class="{ 'is-selected': bindSelectedItem?.id === item.id, 'is-locked': item.metadata_locked }"
            style="cursor: pointer"
            @click="selectBindItem(item)"
          >
            <span class="ms-result-name">
              {{ item.name }}
              <Lock v-if="item.metadata_locked" :size="13" class="lock-mark" />
            </span>
            <span v-if="item.year" class="ms-hint">（{{ item.year }}）</span>
            <span class="ms-hint">{{ item.item_type === 'series' ? '剧集' : '电影' }}</span>
            <span class="mini-badge" :class="item.tmdb_id ? 'ok' : 'warn'">
              {{ item.tmdb_id ? `已绑 ${item.tmdb_id}` : '未绑定' }}
            </span>
            <el-button
              class="lock-btn"
              size="small"
              :type="item.metadata_locked ? 'warning' : 'default'"
              :loading="lockLoadingId === item.id"
              :title="item.metadata_locked ? '已锁定：点击解锁，恢复自动补全' : '锁定：自动补全不再覆盖此条目'"
              @click.stop="toggleItemLock(item)"
            >
              <Lock v-if="item.metadata_locked" :size="14" />
              <LockOpen v-else :size="14" />
              {{ item.metadata_locked ? '已锁定' : '锁定' }}
            </el-button>
          </div>
        </div>
        <div v-if="bindSelectedItem" class="ms-results">
          <div class="ms-result">
            <span>已选：{{ bindSelectedItem.name }}<span v-if="bindSelectedItem.year">（{{ bindSelectedItem.year }}）</span></span>
            <el-button size="small" text @click="clearBindSelection">重选</el-button>
          </div>
        </div>
        <!-- 第二步：搜 TMDB 候选，一键绑定（Emby 式手动识别） -->
        <template v-if="bindSelectedItem">
          <div class="ms-step">第二步 · 搜 TMDB 候选，一键绑定</div>
          <div class="ms-actions">
            <el-input v-model="tmdbSearchQ" placeholder="剧名，如：黑鸟" style="width: 200px" clearable @keyup.enter="doSearchTmdbCandidates" />
            <el-input v-model="tmdbSearchYear" placeholder="年份（可选）" style="width: 110px" clearable @keyup.enter="doSearchTmdbCandidates" />
            <el-button type="primary" size="small" :loading="tmdbSearchLoading" @click="doSearchTmdbCandidates">
              搜索 TMDB
            </el-button>
          </div>
          <div v-if="tmdbCandidates.length" class="tmdb-candidates">
            <div v-for="c in tmdbCandidates" :key="c.tmdb_id" class="tmdb-candidate">
              <div class="tmdb-poster">
                <img
                  v-if="c.poster_path"
                  :src="`https://image.tmdb.org/t/p/w200${c.poster_path}`"
                  :alt="c.title"
                  loading="lazy"
                />
                <div v-else class="tmdb-poster-empty">暂无海报</div>
              </div>
              <div class="tmdb-info">
                <div class="tmdb-title">{{ c.title }}<span v-if="c.year">（{{ c.year }}）</span></div>
                <div class="tmdb-meta">{{ c.media_type === 'tv' ? '剧集' : '电影' }} · TMDB {{ c.tmdb_id }}</div>
                <p class="tmdb-overview" :title="c.overview">{{ c.overview || '暂无简介' }}</p>
                <el-button
                  type="primary"
                  size="small"
                  :loading="tmdbBindLoading === c.tmdb_id"
                  @click="doBindCandidate(c)"
                >
                  绑定此条
                </el-button>
              </div>
            </div>
          </div>
          <div v-else-if="tmdbSearched && !tmdbSearchLoading" class="ms-hint">
            TMDB 没找到匹配的候选，换个剧名或年份试试；或者展开下方「手动输入 TMDB ID」。
          </div>
          <!-- 手填 TMDB ID 入口（默认折叠）：候选墙搜不到时的兜底 -->
          <div class="tmdb-manual-toggle">
            <el-button size="small" text @click="manualIdCollapsed = !manualIdCollapsed">
              {{ manualIdCollapsed ? '▸' : '▾' }} 手动输入 TMDB ID
            </el-button>
          </div>
          <div v-show="!manualIdCollapsed">
            <div class="ms-actions">
              <el-input v-model="bindTmdbId" placeholder="TMDB ID（数字）" style="width: 160px" clearable />
              <el-button size="small" :loading="bindPreviewLoading" @click="doPreviewTmdb">
                预览
              </el-button>
            </div>
            <div v-if="bindPreview" class="ms-results">
              <div class="ms-result">
                <span class="mini-badge" :class="bindPreview.matches_current ? 'ok' : 'warn'">
                  {{ bindPreview.matches_current ? '片名一致' : '片名不一致，请核对' }}
                </span>
                <span>TMDB：{{ bindPreview.title }}<span v-if="bindPreview.year">（{{ bindPreview.year }}）</span></span>
                <span class="ms-hint">当前条目：{{ bindPreview.current_name }}（TMDB {{ bindPreview.current_tmdb_id ?? '未绑定' }}）</span>
              </div>
            </div>
            <div class="ms-actions">
              <el-button type="primary" size="small" :loading="bindLoading" @click="openBindConfirm">
                绑定
              </el-button>
              <el-button size="small" :loading="bindLoading" @click="doUnbindTmdb">
                解绑
              </el-button>
            </div>
          </div>
        </template>
        <!-- 绑定确认弹窗：选补全范围（默认仅补缺失） -->
        <el-dialog v-model="bindConfirmVisible" title="确认绑定" width="min(480px, 92vw)">
          <p class="bind-confirm-text">{{ bindConfirmText }}</p>
          <div class="bind-mode">
            <span class="ms-hint">补全范围：</span>
            <el-radio-group v-model="bindMode">
              <el-radio value="missing">仅补缺失</el-radio>
              <el-radio value="all">全量刷新</el-radio>
            </el-radio-group>
          </div>
          <p class="ms-hint" style="margin-top: 8px">
            {{ bindMode === 'all'
              ? '全量刷新会先清空该条目的 TMDB 来源字段（简介 / 评分 / 类型 / 别名 / 图片），再按新 ID 重写一遍；片名不动。'
              : '仅补缺失：已有的简介 / 图片 / IMDb / 别名不动，只填空着的项。' }}
          </p>
          <template #footer>
            <el-button @click="bindConfirmVisible = false">取消</el-button>
            <el-button type="primary" :loading="bindLoading" @click="doBindTmdb">确认绑定</el-button>
          </template>
        </el-dialog>

        <div v-if="bindNotes.length" class="ms-results">
          <div v-for="(n, i) in bindNotes" :key="i" class="ms-result">{{ n }}</div>
        </div>
        </div>
      </SectionCard>

      <!-- 3. 补全进度 -->
      <SectionCard
        class="ms-card ms-card-wide"
        title="补全进度"
        :icon="RefreshCw"
        description="后台补全 worker（enrich）的工作进度：待处理 / 进行中 / 已完成 / 失败 / 重试中。"
      >
        <div v-if="enrichProgressLoading && !enrichProgress" class="ms-stat-grid" aria-busy="true">
          <div v-for="n in 5" :key="n" class="au-skeleton ms-stat-skeleton" />
        </div>
        <EmptyState
          v-else-if="!enrichProgress"
          compact
          :icon="RefreshCw"
          title="暂无补全进度"
          description="读取失败或 worker 还没上报过数据。"
        >
          <template #actions>
            <el-button size="small" :loading="enrichProgressLoading" @click="loadEnrichProgress">重试</el-button>
          </template>
        </EmptyState>
        <div v-else class="ms-progress">
          <div class="ms-stat-grid">
            <StatTile label="待处理" :value="enrichProgress.enrich.pending" />
            <StatTile label="进行中" :value="enrichProgress.enrich.enriching" />
            <StatTile label="已完成" :value="enrichProgress.enrich.done" />
            <StatTile
              label="失败"
              :value="enrichProgress.enrich.failed"
              :tone="enrichProgress.enrich.failed > 0 ? 'danger' : 'plain'"
            />
            <StatTile label="重试中" :value="enrichProgress.enrich.retrying" />
          </div>
          <!-- v2.42.9：阶段用时分解 + 近 5 分钟完成速率。只报「进程内计数」——
               分母是本次进程运行时长，重启会归零，所以标签写清「本进程」。 -->
          <div v-if="enrichStageRows.length" class="ms-facts">
            <span
              v-for="row in enrichStageRows"
              :key="row.name"
              class="fact"
              :title="`${row.label}：${row.count} 次，累计 ${row.totalMs} ms`"
            >
              {{ row.label }} {{ row.avgMs }}ms
            </span>
          </div>

          <p class="ms-hint ms-block">
            Worker {{ enrichProgress.workers }} 线程 · {{ enrichProgress.enabled ? '运行中' : '已停用' }}
            <el-button size="small" text :loading="enrichProgressLoading" @click="loadEnrichProgress">
              刷新
            </el-button>
          </p>
        </div>
      </SectionCard>
    </div>
  </div>
</template>
