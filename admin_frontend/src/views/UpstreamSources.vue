<script setup lang="ts">
/**
 * 上游源 —— FMBY 学习 #25 的 25-1（只做源管理）。
 * 这里只管「源建得进、管得住」：类型、地址、凭据、启用开关、健康状态占位。
 * 目录聚合是 25-2、探活是 25-3、播放接入是 25-4，本页不拉目录、不探活、
 * 不拼播放地址；新建的源恒为关闭，启用后才会被后续步骤读取。
 * 凭据只存密封值：本页只发凭据原文一次（新建/轮换时），列表与详情
 * 永远只回 has_secret，编辑时留空 = 凭据不动，清空 = 改匿名访问。
 */
import { onMounted, ref } from 'vue'
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
</template>

<style scoped>
.upstream-sources {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
</style>
