<script setup lang="ts">
/**
 * 采样哈希进度 —— 专线 B 第二阶段，只读。
 * GET /api/admin/sample-hash/progress：只计数展示，不触发轮次、不写库。
 * worker 为 null = 后端无该工人条目，原样显示“无工人记录”，不编状态。
 */
import { onMounted, ref } from 'vue'
import { RefreshCw, Hash, Clock3, Hourglass, CheckCircle2 } from 'lucide-vue-next'
import { fetchSampleHashProgress } from '@/api/admin'
import type { SampleHashProgress } from '@/types'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'

const data = ref<SampleHashProgress | null>(null)
const loading = ref(false)
const loadError = ref('')

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await fetchSampleHashProgress()
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function fmtSec(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  return `${v} 秒`
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="系统与审计"
      title="采样哈希进度"
      description="采样哈希队列的计数快照（只读）：待补多少、已采多少、工人状态；本页不触发轮次、不写库。"
    >
      <template #actions>
        <el-button :loading="loading" type="primary" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <div v-if="loadError && !data" class="error-card">
      <EmptyState title="进度加载失败" :description="loadError">
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>
    </div>

    <div v-else-if="loading && !data" class="stat-row" aria-hidden="true">
      <span v-for="n in 4" :key="n" class="au-skeleton stat-skeleton" />
    </div>

    <template v-else-if="data">
      <div class="stat-row">
        <StatTile label="采样开关" :value="data.enabled ? '已启用' : '未启用'" :icon="Hash" :tone="data.enabled ? 'ok' : 'plain'" hint="后端采样哈希开关" />
        <StatTile label="待补条目" :value="data.pending" :icon="Hourglass" hint="内容哈希为空的影片 / 剧集" />
        <StatTile label="已采样条目" :value="data.sampled" :icon="CheckCircle2" hint="已落采样哈希（sample-sha256）" />
        <StatTile label="每轮上限 / 间隔" :value="data.max_files_per_round" :icon="Clock3" :hint="`每轮最多 ${data.max_files_per_round} 个文件，每 ${data.interval_sec} 秒一轮`" />
      </div>

      <SectionCard title="工人状态" :icon="Clock3" :meta="data.worker ? `状态：${data.worker.status}` : '无工人记录'">
        <div v-if="data.worker" class="kv-grid">
          <div><dt>存活</dt><dd>{{ data.worker.alive ? '是' : '否' }}</dd></div>
          <div><dt>状态</dt><dd>{{ data.worker.status }}</dd></div>
          <div><dt>运行时长</dt><dd>{{ fmtSec(data.worker.uptime_sec) }}</dd></div>
          <div><dt>空闲时长</dt><dd>{{ fmtSec(data.worker.idle_sec) }}</dd></div>
          <div><dt>重启次数</dt><dd>{{ data.worker.restarts }}</dd></div>
        </div>
        <EmptyState v-else compact title="无工人记录" description="后端 worker 注册表里没有 sample_hash 条目，计数本身不受影响。" />
      </SectionCard>

      <SectionCard title="口径说明" :icon="Hash">
        <p class="note">
          待补 = 影片 / 剧集、有文件路径、非 strm、非 mount://、内容哈希为空的行数；
          已采样 = 内容哈希算法为 sample-sha256 的行数。本页只读计数，刷新也只是重新 GET，不会启动一轮采样。
        </p>
      </SectionCard>
    </template>
  </div>
</template>

<style scoped>
.stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }
.stat-skeleton { display: block; height: 96px; border-radius: var(--au-r-md); }
.error-card { margin-bottom: 16px; }
.kv-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin: 0; }
.kv-grid div { display: flex; flex-direction: column; gap: 5px; padding: 12px 14px; border: 1px solid var(--au-border); border-radius: var(--au-r-md); background: var(--au-surface-2); }
.kv-grid dt { font-size: 12px; color: var(--au-text-3); }
.kv-grid dd { margin: 0; font-size: 18px; font-weight: 700; color: var(--au-text); }
.note { margin: 0; font-size: 13px; line-height: 1.6; color: var(--au-text-2); }
</style>
