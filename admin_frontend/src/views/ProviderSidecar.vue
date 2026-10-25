<script setup lang="ts">
/**
 * sidecar 状态 —— 专线 B 第二阶段，只读。
 * GET /api/admin/providers/sidecar：不传 provider 查 p115 + yun139 两个，传了只查该个。
 * 某端没起来时该项 available=false + error，整页仍正常显示，不炸成错误页。
 */
import { onMounted, ref } from 'vue'
import { RefreshCw, Plug } from 'lucide-vue-next'
import { fetchSidecarStatus } from '@/api/admin'
import type { SidecarProviderStatus } from '@/types'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'

const PROVIDER_LABELS: Record<string, string> = { p115: '115（p115）', yun139: '139（yun139）' }

const providers = ref<SidecarProviderStatus[]>([])
const selected = ref('')
const loading = ref(false)
const loadError = ref('')

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await fetchSidecarStatus(selected.value || undefined)
    providers.value = res.providers
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function label(p: string): string {
  return PROVIDER_LABELS[p] ?? p
}

function jsonText(v: unknown): string {
  if (v === null || v === undefined) return '—'
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

function errorText(p: SidecarProviderStatus): string {
  if (!p.error) return '—'
  return p.error.message || `${p.error.kind}（${p.error.code}）`
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="媒体与交付"
      title="sidecar 状态"
      description="115 与 139 两个 sidecar 运行时的 manifest 与健康状态（只读）；一端没起来时只标该端不可用，不影响另一端显示。"
    >
      <template #actions>
        <el-select v-model="selected" placeholder="全部服务商" clearable class="w-provider" @change="load">
          <el-option label="全部（p115 + yun139）" value="" />
          <el-option label="115（p115）" value="p115" />
          <el-option label="139（yun139）" value="yun139" />
        </el-select>
        <el-button :loading="loading" type="primary" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <div v-if="loadError && !providers.length" class="error-card">
      <EmptyState title="状态加载失败" :description="loadError">
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>
    </div>

    <div v-else-if="loading && !providers.length" class="stat-row" aria-hidden="true">
      <span v-for="n in 2" :key="n" class="au-skeleton stat-skeleton" />
    </div>

    <template v-else>
      <div class="stat-row">
        <StatTile
          v-for="p in providers"
          :key="p.provider"
          :label="label(p.provider)"
          :value="p.available ? '可用' : '不可用'"
          :icon="Plug"
          :tone="p.available ? 'ok' : 'danger'"
          :hint="p.available ? p.socket_path : errorText(p)"
        />
      </div>

      <div v-if="!providers.length">
        <EmptyState title="暂无 sidecar 状态" description="后端没有返回任何服务商状态，刷新后再试。" />
      </div>

      <SectionCard
        v-for="p in providers"
        :key="p.provider"
        :title="label(p.provider)"
        :icon="Plug"
        :meta="p.available ? '可用' : '不可用'"
        :tone="p.available ? 'default' : 'accent'"
      >
        <dl class="kv-list">
          <div><dt>服务商</dt><dd class="mono">{{ p.provider }}</dd></div>
          <div><dt>套接字路径</dt><dd class="mono">{{ p.socket_path }}</dd></div>
          <div><dt>可用</dt><dd>{{ p.available ? '是' : '否' }}</dd></div>
          <div><dt>错误</dt><dd>{{ errorText(p) }}</dd></div>
        </dl>
        <details class="raw">
          <summary>manifest（对端原样）</summary>
          <pre>{{ jsonText(p.manifest) }}</pre>
        </details>
        <details class="raw">
          <summary>health（对端原样）</summary>
          <pre>{{ jsonText(p.health) }}</pre>
        </details>
      </SectionCard>
    </template>
  </div>
</template>

<style scoped>
.w-provider { width: 200px; }
.stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px; }
.stat-skeleton { display: block; height: 96px; border-radius: var(--au-r-md); }
.error-card { margin-bottom: 16px; }
.kv-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin: 0 0 12px; }
.kv-list div { display: flex; flex-direction: column; gap: 4px; padding: 10px 12px; border: 1px solid var(--au-border); border-radius: var(--au-r-md); background: var(--au-surface-2); }
.kv-list dt { font-size: 12px; color: var(--au-text-3); }
.kv-list dd { margin: 0; font-size: 13px; color: var(--au-text); word-break: break-all; }
.mono { font-family: ui-monospace, monospace; }
.raw { margin-top: 10px; font-size: 12px; color: var(--au-text-3); }
.raw summary { cursor: pointer; user-select: none; }
.raw pre { margin: 6px 0 0; padding: 10px 12px; overflow: auto; border: 1px solid var(--au-border); border-radius: var(--au-r-md); background: var(--au-surface-2); color: var(--au-text-2); font-family: ui-monospace, monospace; }
</style>
