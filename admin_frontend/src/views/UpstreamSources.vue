<script setup lang="ts">
/**
 * 上游源 —— FMBY 学习 #25 的 25-1（只做源管理）。
 * 这里只管「源建得进、管得住」：类型、地址、凭据、启用开关、健康状态占位。
 * 目录聚合是 25-2、探活是 25-3、播放接入是 25-4，本页不拉目录、不探活、
 * 不拼播放地址；新建的源恒为关闭，启用后才会被后续步骤读取。
 * 凭据只存密封值：本页只发凭据原文一次（新建/轮换时），列表与详情
 * 永远只回 has_secret，编辑时留空 = 凭据不动，清空 = 改匿名访问。
 */
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import {
  createUpstreamSource,
  deleteUpstreamSource,
  disableUpstreamSource,
  enableUpstreamSource,
  fetchUpstreamSources,
  updateUpstreamSource,
} from '@/api/admin'
import type { UpstreamSource, UpstreamSourceInput } from '@/api/admin'
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

const showForm = ref(false)
const editingId = ref<number | null>(null)
const form = ref({
  name: '',
  source_type: 'emby' as 'emby' | 'jellyfin',
  base_url: '',
  auth_method: 'password' as 'password' | 'api_key',
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

onMounted(loadSources)
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
        <el-table-column label="操作" width="280">
          <template #default="{ row }">
            <el-button size="small" @click="toggleEnabled(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </el-button>
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button v-if="row.has_secret" size="small" @click="clearSecret(row)">
              清凭据
            </el-button>
            <el-button size="small" type="danger" @click="removeSource(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <EmptyState v-if="!loading && loadError" title="列表加载失败" description="点刷新重试" />
      <EmptyState
        v-if="!loading && !loadError && sources.length === 0"
        title="还没有上游源"
        description="点右上角新建：Emby / Jellyfin 型先行，苹果 CMS 型本步不做"
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
          </el-select>
        </el-form-item>
        <el-form-item label="地址" required>
          <el-input v-model="form.base_url" placeholder="如：https://example.com:8096/emby（只许公网地址）" />
        </el-form-item>
        <el-form-item label="认证方式" required>
          <el-select v-model="form.auth_method">
            <el-option label="账号密码" value="password" />
            <el-option label="api_key" value="api_key" />
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
          <el-input v-model="form.note" />
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

@media (max-width: 640px) {
  .tab-btn { padding: 8px 12px; font-size: 14px; }
  .tab-hint { font-size: 11px; }
}
</style>
