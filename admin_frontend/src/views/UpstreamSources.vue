<script setup lang="ts">
/**
 * 上游源 —— FMBY 学习 #25 的 25-1（源管理）+ 25-2 手动聚合触发。
 * 源管理只管「源建得进、管得住」：类型、地址、凭据、启用开关、健康状态。
 * 目录聚合（25-2）只做手动触发：行上的「聚合」按钮入队后轮询任务。
 * 聚合经常超过 axios 的 30 秒，不再干等同步口，避免超时被说成「网络连不上」。
 * 后台按库拉上游目录、配对本地条目、写路径开关开时才写映射；不做自动调度、
 * 不探活、不拼播放地址、不做自动播放兜底；新建的源恒为关闭，启用后才会被后续步骤读取。
 * 苹果 CMS 型源不走 Emby 目录聚合口，本页不给它聚合按钮（后端同样会 400 拒）。
 * 凭据只存密封值：本页只发凭据原文一次（新建/轮换时），列表与详情
 * 永远只回 has_secret，编辑时留空 = 凭据不动，清空 = 改匿名访问。
 */
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import {
  aggregateUpstreamSource,
  createUpstreamSource,
  deleteUpstreamSource,
  disableUpstreamSource,
  enableUpstreamSource,
  fetchUpstreamAggregateTask,
  fetchUpstreamCategoryLibraryMap,
  fetchUpstreamDetailLineConfig,
  fetchUpstreamSources,
  updateUpstreamCategoryLibraryMap,
  updateUpstreamDetailLineConfig,
  updateUpstreamSource,
} from '@/api/admin'
import type {
  UpstreamAggregateResult,
  UpstreamAggregateTask,
  UpstreamSource,
  UpstreamSourceInput,
} from '@/api/admin'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'

/**
 * 重新布局批 3：存储绑定 / 上游映射 / 115 账号池 / sidecar 四个只读观测页
 * 降为本页标签（沿用 Codes.vue 的 ?tab= 同步 + KeepAlive 模式）。
 * 源列表面板用 v-show：来回切标签不卸载表单与列表状态；观测面板按需加载、
 * 进过一次后 KeepAlive 缓存，不重拉数据。
 */
const StorageBindings = defineAsyncComponent(() => import('./StorageBindings.vue'))
const UpstreamMappings = defineAsyncComponent(() => import('./UpstreamMappings.vue'))
const Yun139Pools = defineAsyncComponent(() => import('./Yun139Pools.vue'))
const ProviderSidecar = defineAsyncComponent(() => import('./ProviderSidecar.vue'))

type UpstreamTab = 'sources' | 'bindings' | 'mappings' | 'pools' | 'sidecar'

const route = useRoute()
const router = useRouter()

const activeTab = computed<UpstreamTab>({
  get: () => {
    const t = route.query.tab
    return t === 'bindings' || t === 'mappings' || t === 'pools' || t === 'sidecar' ? t : 'sources'
  },
  set: (tab) => {
    if (tab === activeTab.value) return
    router.replace({ query: { ...route.query, tab } })
  },
})

const sources = ref<UpstreamSource[]>([])
const loading = ref(false)
const loadError = ref(false)
const saving = ref(false)
const aggregatingIds = ref<number[]>([])
const aggregateResult = ref<{ sourceName: string; result: UpstreamAggregateResult } | null>(null)
/** 进行中任务的最新快照（O-02 进度条）：轮询每拍刷新，终态即清。 */
const aggregateProgress = ref<{ sourceName: string; task: UpstreamAggregateTask } | null>(null)
/** 进度按已完成库占总库数算；总库数还没发现出来（0）时按 0 显示，
 *  文案会说明还在发现库，不假装有百分比。 */
const aggregateProgressPercent = computed(() => {
  const task = aggregateProgress.value?.task
  if (!task || task.libraries_total <= 0) return 0
  return Math.min(100, Math.round((task.libraries_done / task.libraries_total) * 100))
})

/** 离开页面就停轮询，不再弹已经看不见的结果。 */
let aggregatePageToken = 0
/** 聚合一轮要跑数分钟，进度 3 秒一拍足够看，不密打查询口。 */
const AGGREGATE_POLL_MS = 3000
const AGGREGATE_POLL_RETRIES = 3

/** 终态：成功 / 失败 / 心跳过期被标中断——都停轮询，不再空转。 */
function isAggregateTerminal(status: UpstreamAggregateTask['status']): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'interrupted'
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

/** 把任务失败还原成带 response.detail 的错误，走现有 toAdminError 文案。 */
function aggregateTaskError(task: UpstreamAggregateTask): Error {
  const detail = task.error?.detail?.trim() || '聚合失败'
  const err = new Error(detail) as Error & {
    response?: { status?: number; data?: { detail: string } }
  }
  err.response = { status: task.error?.status, data: { detail } }
  return err
}

async function pollAggregateTask(
  sourceId: number,
  taskId: string,
  token: number,
  onSnapshot?: (snap: UpstreamAggregateTask) => void,
): Promise<UpstreamAggregateTask> {
  let transient = 0
  for (;;) {
    if (token !== aggregatePageToken) {
      throw new Error('聚合已取消')
    }
    try {
      const snap = await fetchUpstreamAggregateTask(sourceId, taskId)
      onSnapshot?.(snap)
      if (isAggregateTerminal(snap.status)) return snap
      transient = 0
    } catch (e) {
      transient += 1
      if (transient > AGGREGATE_POLL_RETRIES || token !== aggregatePageToken) throw e
    }
    await sleep(AGGREGATE_POLL_MS)
  }
}

const showForm = ref(false)
const editingId = ref<number | null>(null)
const form = ref({
  name: '',
  source_type: 'emby' as 'emby' | 'jellyfin' | 'applecms',
  base_url: '',
  auth_method: 'password' as 'password' | 'api_key' | 'none',
  username: '',
  secret: '',
  extra_headers_text: '',
  user_agent: '',
  referer: '',
  note: '',
})

const HEALTH_LABELS: Record<string, string> = {
  unknown: '未知（未探活）',
  healthy: '健康',
  unhealthy: '异常',
}

async function loadSources() {
  loading.value = true
  try {
    const res = await fetchUpstreamSources()
    sources.value = res.items
    loadError.value = false
  } catch (e) {
    loadError.value = true
    ElMessage.error(toAdminError(e))
  } finally {
    loading.value = false
  }
}

// 详情页上游线路开关（U-02）：默认开；关后用户端详情/PlaybackInfo
// 来源列表不再出现「上游 · <源名>」线路，取流口 404。
const detailLineEnabled = ref(true)
const detailLineSaving = ref(false)

async function loadDetailLineConfig() {
  try {
    const res = await fetchUpstreamDetailLineConfig()
    detailLineEnabled.value = res.enabled
  } catch (e) {
    ElMessage.error(toAdminError(e))
  }
}

async function onDetailLineToggle(value: boolean) {
  detailLineSaving.value = true
  try {
    const res = await updateUpstreamDetailLineConfig(value)
    detailLineEnabled.value = res.enabled
    ElMessage.success(res.enabled ? '详情页上游线路已开启' : '详情页上游线路已关闭')
  } catch (e) {
    ElMessage.error(toAdminError(e))
    detailLineEnabled.value = !value
  } finally {
    detailLineSaving.value = false
  }
}

// 分类→本地库改配（备注-分类对位）：源备注「备注-分类」的分类默认
// 同名对位本地库；这里只改配对不上的个别分类。文本是 JSON 对象，
// 留空保存 = 清掉改配、全部分类回同名对位。
const categoryMapText = ref('')
const categoryMapSaving = ref(false)

async function loadCategoryLibraryMap() {
  try {
    const res = await fetchUpstreamCategoryLibraryMap()
    categoryMapText.value = res.value
  } catch (e) {
    ElMessage.error(toAdminError(e))
  }
}

async function saveCategoryLibraryMap() {
  const text = categoryMapText.value.trim()
  if (text) {
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      ElMessage.error('分类映射必须是 JSON 对象，如 {"国产剧": "华语剧场"}')
      return
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      ElMessage.error('分类映射必须是 JSON 对象，如 {"国产剧": "华语剧场"}')
      return
    }
  }
  categoryMapSaving.value = true
  try {
    const res = await updateUpstreamCategoryLibraryMap(text)
    categoryMapText.value = res.value
    ElMessage.success('分类→本地库映射已保存')
  } catch (e) {
    ElMessage.error(toAdminError(e))
  } finally {
    categoryMapSaving.value = false
  }
}

function openCreate() {
  editingId.value = null
  form.value = {
    name: '',
    source_type: 'emby',
    base_url: '',
    auth_method: 'password',
    username: '',
    secret: '',
    extra_headers_text: '',
    user_agent: '',
    referer: '',
    note: '',
  }
  showForm.value = true
}

function openEdit(source: UpstreamSource) {
  editingId.value = source.id
  form.value = {
    name: source.name,
    source_type: source.source_type,
    base_url: source.base_url,
    auth_method: source.auth_method,
    username: source.username ?? '',
    secret: '',
    extra_headers_text: source.extra_headers
      ? JSON.stringify(source.extra_headers)
      : '',
    user_agent: source.user_agent ?? '',
    referer: source.referer ?? '',
    note: source.note ?? '',
  }
  showForm.value = true
}

function parseExtraHeaders(): Record<string, string> | null | undefined {
  const text = form.value.extra_headers_text.trim()
  if (!text) return undefined
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('附加请求头必须是 JSON 对象')
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('附加请求头必须是 JSON 对象')
  }
  return parsed as Record<string, string>
}

async function submitForm() {
  let extraHeaders: Record<string, string> | null | undefined
  try {
    extraHeaders = parseExtraHeaders()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '附加请求头格式错误')
    return
  }
  const input: UpstreamSourceInput = {
    name: form.value.name.trim(),
    source_type: form.value.source_type,
    base_url: form.value.base_url.trim(),
    auth_method: form.value.auth_method,
    username: form.value.auth_method === 'password'
      ? form.value.username.trim() || undefined
      : undefined,
    user_agent: form.value.user_agent.trim() || undefined,
    referer: form.value.referer.trim() || undefined,
    note: form.value.note.trim() || undefined,
  }
  if (extraHeaders !== undefined) input.extra_headers = extraHeaders
  if (editingId.value === null) {
    // 新建：secret 留空 = 不配凭据（匿名访问）
    if (form.value.secret) input.secret = form.value.secret
  } else if (form.value.secret) {
    // 编辑：secret 非空 = 轮换；留空 = 凭据不动（清空走下面，不在这里）
    input.secret = form.value.secret
  }
  saving.value = true
  try {
    if (editingId.value === null) {
      await createUpstreamSource(input)
      ElMessage.success('上游源已新建（默认关闭，需手动启用）')
    } else {
      await updateUpstreamSource(editingId.value, input)
      ElMessage.success('上游源已更新')
    }
    showForm.value = false
    await loadSources()
  } catch (e) {
    ElMessage.error(toAdminError(e))
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(source: UpstreamSource) {
  try {
    if (source.enabled) {
      await disableUpstreamSource(source.id)
      ElMessage.success('已停用')
    } else {
      await enableUpstreamSource(source.id)
      ElMessage.success('已启用')
    }
    await loadSources()
  } catch (e) {
    ElMessage.error(toAdminError(e))
  }
}

async function runAggregate(source: UpstreamSource) {
  if (aggregatingIds.value.includes(source.id)) return
  const token = aggregatePageToken
  aggregatingIds.value = [...aggregatingIds.value, source.id]
  try {
    try {
      await ElMessageBox.confirm(
        `给「${source.name}」跑一次目录聚合吗？会按库拉上游目录、配对本地条目，写路径开关开时才写映射；单库失败不毁已写映射，结果会在表下方展示。`,
        '跑目录聚合',
        { type: 'warning' },
      )
    } catch {
      return
    }
    let task = await aggregateUpstreamSource(source.id)
    aggregateProgress.value = { sourceName: source.name, task }
    if (!isAggregateTerminal(task.status)) {
      task = await pollAggregateTask(source.id, task.task_id, token, (snap) => {
        if (token === aggregatePageToken) {
          aggregateProgress.value = { sourceName: source.name, task: snap }
        }
      })
    }
    if (token !== aggregatePageToken) return
    if (task.status === 'interrupted') {
      throw new Error(
        '聚合被中断（执行面重启或心跳超时），已完成库的映射已保留，重新点聚合即可续跑未完成的库',
      )
    }
    if (task.status === 'failed' || !task.result) {
      throw aggregateTaskError(task)
    }
    const result = task.result
    aggregateResult.value = { sourceName: source.name, result }
    ElMessage.success(
      `聚合完成：发现 ${result.libraries_discovered} 个库，拉取 ${result.fetched} 条，配对 ${result.paired} 条，写入 ${result.written} 条，失败库 ${result.failed_libraries.length} 个`,
    )
  } catch (e) {
    if (token !== aggregatePageToken) return
    ElMessage.error(toAdminError(e))
  } finally {
    if (token === aggregatePageToken) {
      aggregatingIds.value = aggregatingIds.value.filter((id) => id !== source.id)
      aggregateProgress.value = null
    }
  }
}

async function clearSecret(source: UpstreamSource) {
  try {
    await ElMessageBox.confirm(
      `把「${source.name}」的凭据清掉、改匿名访问吗？`,
      '清掉凭据',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await updateUpstreamSource(source.id, { secret: '' })
    ElMessage.success('凭据已清掉')
    await loadSources()
  } catch (e) {
    ElMessage.error(toAdminError(e))
  }
}

async function removeSource(source: UpstreamSource) {
  try {
    await ElMessageBox.confirm(
      `删除上游源「${source.name}」吗？只删源本身，聚合与播放是后续步骤、现在没有数据依赖它。`,
      '删除上游源',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await deleteUpstreamSource(source.id)
    ElMessage.success('已删除')
    await loadSources()
  } catch (e) {
    ElMessage.error(toAdminError(e))
  }
}

onMounted(() => {
  void loadSources()
  void loadDetailLineConfig()
  void loadCategoryLibraryMap()
})
onUnmounted(() => {
  aggregatePageToken += 1
  aggregateProgress.value = null
})
</script>

<template>
  <div class="upstream-sources">
    <PageHeader
      title="上游源"
      subtitle="别人的媒体服当作源接进来（FMBY #25 第一步：只做源管理）。新建的源默认关闭，目录聚合、探活、播放接入是后续步骤，本页不做。"
    />
    <div class="tab-bar" role="tablist" aria-label="上游源">
      <button
        id="upstream-tab-sources"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'sources' }"
        :aria-selected="activeTab === 'sources'"
        aria-controls="upstream-panel"
        @click="activeTab = 'sources'"
      >
        源列表
        <span class="tab-hint">新建 / 启用 / 凭据</span>
      </button>
      <button
        id="upstream-tab-bindings"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'bindings' }"
        :aria-selected="activeTab === 'bindings'"
        aria-controls="upstream-panel"
        @click="activeTab = 'bindings'"
      >
        观测·绑定
        <span class="tab-hint">存储绑定</span>
      </button>
      <button
        id="upstream-tab-mappings"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'mappings' }"
        :aria-selected="activeTab === 'mappings'"
        aria-controls="upstream-panel"
        @click="activeTab = 'mappings'"
      >
        观测·映射
        <span class="tab-hint">上游映射</span>
      </button>
      <button
        id="upstream-tab-pools"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'pools' }"
        :aria-selected="activeTab === 'pools'"
        aria-controls="upstream-panel"
        @click="activeTab = 'pools'"
      >
        观测·账号池
        <span class="tab-hint">115 账号池</span>
      </button>
      <button
        id="upstream-tab-sidecar"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'sidecar' }"
        :aria-selected="activeTab === 'sidecar'"
        aria-controls="upstream-panel"
        @click="activeTab = 'sidecar'"
      >
        观测·sidecar
        <span class="tab-hint">供应商 sidecar</span>
      </button>
    </div>

    <!-- 源列表面板：v-show 常驻，切标签回来表单与列表状态都在 -->
    <div v-show="activeTab === 'sources'">
    <SectionCard title="用户端展示">
      <div class="detail-line-row">
        <div>
          <div>详情页上游线路</div>
          <div class="detail-line-hint">
            开启时，有上游映射的条目会在用户端详情页来源列表里多一条
            「上游 · 源名」线路，点它走上游起播；关闭后线路不出现。
          </div>
        </div>
        <el-switch
          :model-value="detailLineEnabled"
          :loading="detailLineSaving"
          active-text="开启"
          inactive-text="关闭"
          @change="onDetailLineToggle"
        />
      </div>
    </SectionCard>
    <SectionCard title="聚合对位">
      <div class="detail-line-hint">
        聚合按源备注对位：备注写成「备注-分类」（如「乐观-国产剧」），
        配对只在分类对位的本地库里进行；备注没写分类、或分类对不上
        任何本地库，该源整源跳过并在聚合结果里报原因。
        分类默认同名对位本地库；个别分类的库名对不上时，在下面用
        JSON 对象改配（分类名 → 本地库名），只写要改配的即可。
      </div>
      <el-input
        v-model="categoryMapText"
        type="textarea"
        :rows="3"
        placeholder='如 {"国产剧": "华语剧场"}，留空 = 全部分类同名对位'
      />
      <div style="margin-top: 8px">
        <el-button type="primary" :loading="categoryMapSaving" @click="saveCategoryLibraryMap">
          保存映射
        </el-button>
      </div>
    </SectionCard>
    <SectionCard title="源列表">
      <template #actions>
        <el-button type="primary" @click="openCreate">新建上游源</el-button>
        <el-button @click="loadSources">刷新</el-button>
      </template>
      <el-table v-if="!loadError" v-loading="loading" :data="sources" style="width: 100%">
        <el-table-column prop="name" label="名称" width="140" />
        <el-table-column prop="source_type" label="类型" width="90" />
        <el-table-column prop="base_url" label="地址" min-width="220" />
        <el-table-column label="认证" width="90">
          <template #default="{ row }">
            {{ row.auth_method === 'password' ? '账号密码' : 'api_key' }}
          </template>
        </el-table-column>
        <el-table-column label="凭据" width="70">
          <template #default="{ row }">
            {{ row.has_secret ? '已配' : '未配' }}
          </template>
        </el-table-column>
        <el-table-column label="启用" width="70">
          <template #default="{ row }">
            {{ row.enabled ? '开' : '关' }}
          </template>
        </el-table-column>
        <el-table-column label="健康状态" width="120">
          <template #default="{ row }">
            {{ HEALTH_LABELS[row.health_status] ?? row.health_status }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="360">
          <template #default="{ row }">
            <el-button size="small" @click="toggleEnabled(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </el-button>
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button v-if="row.has_secret" size="small" @click="clearSecret(row)">
              清凭据
            </el-button>
            <el-button
              v-if="row.source_type !== 'applecms'"
              size="small"
              type="primary"
              :loading="aggregatingIds.includes(row.id)"
              :disabled="aggregatingIds.includes(row.id)"
              @click="runAggregate(row)"
            >
              聚合
            </el-button>
            <el-button size="small" type="danger" @click="removeSource(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <SectionCard v-if="aggregateProgress" title="聚合进行中">
        <p>
          源「{{ aggregateProgress.sourceName }}」：
          <span v-if="aggregateProgress.task.status === 'queued'">排队中，等执行面认领…</span>
          <span v-else-if="aggregateProgress.task.current_library_name">
            已处理到库「{{ aggregateProgress.task.current_library_name }}」
          </span>
          <span v-else>正在拉取上游库清单…</span>
        </p>
        <el-progress :percentage="aggregateProgressPercent" />
        <p>
          已完成库 {{ aggregateProgress.task.libraries_done }}<template
            v-if="aggregateProgress.task.libraries_total > 0"
          >
            / {{ aggregateProgress.task.libraries_total }}</template
          >
          个，已拉取 {{ aggregateProgress.task.fetched }} 条，配对
          {{ aggregateProgress.task.paired }} 条，写入 {{ aggregateProgress.task.written }} 条。
        </p>
      </SectionCard>
      <SectionCard v-if="aggregateResult" title="最近一次聚合结果">
        <p v-if="aggregateResult.result.skipped">
          源「{{ aggregateResult.sourceName }}」本轮已跳过：
          {{ aggregateResult.result.skip_reason?.message }}（{{ aggregateResult.result.skip_reason?.code }}）
        </p>
        <p v-else-if="aggregateResult.result.target_library_name">
          源「{{ aggregateResult.sourceName }}」本轮对位本地库「{{ aggregateResult.result.target_library_name }}」
          （分类：{{ aggregateResult.result.category }}），配对只在该库内进行。
        </p>
        <p>
          源「{{ aggregateResult.sourceName }}」：发现 {{ aggregateResult.result.libraries_discovered }} 个库，
          拉取 {{ aggregateResult.result.fetched }} 条，聚合 {{ aggregateResult.result.aggregates }} 条，
          配对 {{ aggregateResult.result.paired }} 条，写入 {{ aggregateResult.result.written }} 条，
          写路径开关：{{ aggregateResult.result.mapping_enabled ? '开' : '关' }}。
          写进的映射去「映射」标签查看。
        </p>
        <ul>
          <li v-for="lib in aggregateResult.result.libraries" :key="lib.library_id">
            {{ lib.library_name || lib.library_id }}：拉取 {{ lib.fetched }} 条，配对 {{ lib.paired }} 条，
            写入 {{ lib.written }} 条，状态 {{ lib.status }}
            <span v-if="lib.status === 'failed'">（{{ lib.error_code }}：{{ lib.error }}）</span>
          </li>
        </ul>
        <p v-if="aggregateResult.result.failed_libraries.length">
          失败库 {{ aggregateResult.result.failed_libraries.length }} 个：
          <span v-for="f in aggregateResult.result.failed_libraries" :key="f.library_id">
            {{ f.library_name || f.library_id }}（{{ f.code }}：{{ f.message }}）；
          </span>
          已写映射不受影响，重跑即可重试失败库。
        </p>
      </SectionCard>
      <EmptyState v-if="!loading && loadError" title="列表加载失败" description="点刷新重试" />
      <EmptyState
        v-if="!loading && !loadError && sources.length === 0"
        title="还没有上游源"
        description="点右上角新建：Emby / Jellyfin / 苹果 CMS 型（苹果 CMS 须先开 APPLECMS_ENABLED 总闸，否则建源被拒）"
      />
    </SectionCard>

    <SectionCard v-if="showForm" :title="editingId === null ? '新建上游源' : '编辑上游源'">
      <el-form label-width="110px" @submit.prevent="submitForm">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="如：友服 A（全站唯一）" />
        </el-form-item>
        <el-form-item label="类型" required>
          <el-select v-model="form.source_type">
            <el-option label="Emby" value="emby" />
            <el-option label="Jellyfin" value="jellyfin" />
            <el-option label="苹果 CMS" value="applecms" />
          </el-select>
        </el-form-item>
        <el-form-item label="地址" required>
          <el-input v-model="form.base_url" placeholder="如：https://example.com:8096/emby（只许公网地址）" />
        </el-form-item>
        <el-form-item label="认证方式" required>
          <el-select v-model="form.auth_method">
            <el-option label="账号密码" value="password" />
            <el-option label="api_key" value="api_key" />
            <el-option label="匿名（none）" value="none" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.auth_method === 'password'" label="用户名" required>
          <el-input v-model="form.username" />
        </el-form-item>
        <el-form-item label="凭据">
          <el-input
            v-model="form.secret"
            type="password"
            show-password
            :placeholder="editingId === null ? '留空 = 不配凭据（匿名访问）' : '留空 = 凭据不动；填了 = 轮换'"
          />
        </el-form-item>
        <el-form-item label="附加请求头">
          <el-input
            v-model="form.extra_headers_text"
            type="textarea"
            :rows="2"
            placeholder='JSON 对象，如 {"X-Custom": "1"}，留空 = 无'
          />
        </el-form-item>
        <el-form-item label="User-Agent">
          <el-input v-model="form.user_agent" />
        </el-form-item>
        <el-form-item label="Referer">
          <el-input v-model="form.referer" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="form.note"
            placeholder="如：乐观-国产剧（聚合按「-」后的分类对位本地同名库，见上方「聚合对位」）"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="submitForm">保存</el-button>
          <el-button @click="showForm = false">取消</el-button>
        </el-form-item>
      </el-form>
    </SectionCard>
    </div>

    <!-- 观测面板：四个子页各自带 PageHeader，这里只放标签条，不再叠第二个页头 -->
    <div
      id="upstream-panel"
      class="tab-panel"
      role="tabpanel"
      :aria-labelledby="`upstream-tab-${activeTab}`"
    >
      <KeepAlive>
        <StorageBindings v-if="activeTab === 'bindings'" />
        <UpstreamMappings v-else-if="activeTab === 'mappings'" />
        <Yun139Pools v-else-if="activeTab === 'pools'" />
        <ProviderSidecar v-else-if="activeTab === 'sidecar'" />
      </KeepAlive>
    </div>
  </div>
</template>

<style scoped>
.upstream-sources {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
/* 标签条（与 Codes.vue / Welfare.vue 同款） */
.tab-bar {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--au-border);
  overflow-x: auto;
  scrollbar-width: none;
}
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
.detail-line-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.detail-line-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--au-text-3);
}

@media (max-width: 640px) {
  .tab-btn { padding: 8px 12px; font-size: 14px; }
  .tab-hint { font-size: 11px; }
}
</style>
