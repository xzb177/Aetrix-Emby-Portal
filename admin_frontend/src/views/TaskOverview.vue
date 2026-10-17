<script setup lang="ts">
/**
 * 任务总览（路线 2-A）：管理后台一页看所有后台任务的状态、进度、开关。
 *
 * 只读展示为主；开关操作直接调各任务已有的保存接口（admin.ts 里现成的），
 * 不另起新接口。卡片数据来自 GET /api/admin/tasks/overview。
 */
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  BellRing, ExternalLink, Image as ImageIcon,
  Radar, RefreshCw, ScanSearch, Sparkles, TriangleAlert,
} from 'lucide-vue-next'
import { EmptyState, PageHeader } from '@/components/ui'
import {
  fetchTasksOverview,
  pauseProbe,
  resumeProbe,
  saveAutoScan,
  saveChaseNew,
  saveEnrichEpisodeNight,
  type TaskCard,
  type TaskCardStatus,
} from '@/api/admin'

const cards = ref<TaskCard[]>([])
const loading = ref(false)
const loadError = ref('')
const fetchedAt = ref('')
/** 每个任务开关的提交中状态（key → loading） */
const toggling = ref<Record<string, boolean>>({})

const CARD_ICON: Record<string, unknown> = {
  scan: ScanSearch,
  probe: Radar,
  enrich: Sparkles,
  thumbnail: ImageIcon,
  chase_new: BellRing,
}

const DOT_COLOR: Record<TaskCardStatus, string> = {
  running: 'var(--au-primary)',
  ok: 'var(--au-success)',
  warning: 'var(--au-warning)',
  error: 'var(--au-danger)',
  idle: 'var(--au-text-3)',
  disabled: 'var(--au-text-3)',
}

/** 库扫描状态小标签 */
function libChip(status: string | null): { text: string; cls: string } {
  switch (status) {
    case 'running': return { text: '扫描中', cls: 'lib-chip--running' }
    case 'success': return { text: '成功', cls: 'lib-chip--ok' }
    case 'partial': return { text: '部分成功', cls: 'lib-chip--warn' }
    case 'failed': return { text: '失败', cls: 'lib-chip--bad' }
    case 'interrupted': return { text: '已中断', cls: 'lib-chip--warn' }
    default: return { text: '没跑过', cls: '' }
  }
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

async function load(silent = false) {
  if (!silent) {
    loading.value = true
    loadError.value = ''
  }
  try {
    const res = await fetchTasksOverview()
    cards.value = Array.isArray(res.tasks) ? res.tasks : []
    fetchedAt.value = res.fetched_at ? new Date(res.fetched_at).toLocaleString() : ''
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '加载失败'
    if (!silent) cards.value = []
    // P2-12：silent 刷新失败时页面无任何提示，补一条警告（当前显示的是旧数据）
    else ElMessage.warning('数据刷新失败，显示的是旧数据')
  } finally {
    loading.value = false
  }
}

/** 开关切换：走各任务已有的保存接口，失败弹回原状态 */
async function onToggle(card: TaskCard, val: boolean) {
  const key = card.key
  const sw = card.switch
  if (!sw) return
  const keep = (sw.keep ?? {}) as Record<string, unknown>
  toggling.value[key] = true
  try {
    if (key === 'scan') {
      await saveAutoScan(val, String(keep.time ?? '03:00'))
    } else if (key === 'probe') {
      if (val) await resumeProbe()
      else await pauseProbe()
    } else if (key === 'enrich') {
      await saveEnrichEpisodeNight(
        val,
        String(keep.night_start ?? '02:00'),
        String(keep.night_end ?? '06:00'),
        String(keep.night_tz ?? 'Asia/Shanghai'),
      )
    } else if (key === 'chase_new') {
      await saveChaseNew(
        val,
        Number(keep.interval ?? 30),
        String(keep.excluded ?? ''),
      )
    }
    ElMessage.success(`${card.title}已${val ? '开启' : '关闭'}`)
    await load(true)
  } catch {
    // 拦截器已提示；开关弹回原状态
    sw.enabled = !val
  } finally {
    toggling.value[key] = false
  }
}

onMounted(() => load())
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="系统与审计"
      title="任务总览"
      description="所有后台任务一页看完：状态、进度、开关。出问题先看这里，再点「详情」去各任务自己的页面细看。"
    >
      <template #actions>
        <span class="checked-at">最近更新 {{ fetchedAt || '—' }}</span>
        <el-button :loading="loading" type="primary" :icon="RefreshCw" @click="() => load()">
          刷新
        </el-button>
      </template>
    </PageHeader>

    <EmptyState
      v-if="loadError && !cards.length"
      :icon="TriangleAlert"
      title="任务总览加载失败"
      :description="`后端接口没返回（${loadError}），点「刷新」重试。`"
    >
      <template #actions><el-button :loading="loading" @click="() => load()">重试</el-button></template>
    </EmptyState>

    <div v-else class="task-grid" :aria-busy="loading">
      <!-- 加载骨架 -->
      <template v-if="loading && !cards.length">
        <div v-for="i in 6" :key="i" class="task-card">
          <span class="au-skeleton" style="display:block;height:18px;width:40%" />
          <span class="au-skeleton" style="display:block;height:14px" />
          <span class="au-skeleton" style="display:block;height:14px;width:70%" />
        </div>
      </template>

      <section v-for="card in cards" :key="card.key" class="task-card">
        <header class="task-head">
          <span
            class="dot"
            :class="{ 'dot--pulse': card.status === 'running' }"
            :style="{ background: DOT_COLOR[card.status] ?? 'var(--au-text-3)' }"
          />
          <component :is="CARD_ICON[card.key]" :size="16" class="task-icon" />
          <h3 class="task-title">{{ card.title }}</h3>
          <span class="task-status">{{ card.status_text }}</span>
          <router-link v-if="card.link" :to="card.link" class="task-link">
            详情 <ExternalLink :size="12" />
          </router-link>
        </header>

        <p class="task-summary">{{ card.summary }}</p>

        <dl v-if="card.metrics.length" class="task-metrics">
          <div v-for="m in card.metrics" :key="m.label" class="metric-row">
            <dt>{{ m.label }}</dt>
            <dd>{{ m.value }}</dd>
          </div>
        </dl>

        <!-- 扫描卡：各库最近一轮 -->
        <ul v-if="card.key === 'scan' && card.libraries?.length" class="lib-list">
          <li v-for="lib in card.libraries" :key="lib.id" class="lib-row">
            <span class="lib-name">{{ lib.name }}</span>
            <span :class="['lib-chip', libChip(lib.last_status ?? lib.scan_status).cls]">
              {{ libChip(lib.last_status ?? lib.scan_status).text }}
            </span>
            <span class="muted mono lib-time">{{ fmtTime(lib.last_finished_at ?? lib.last_started_at) }}</span>
          </li>
        </ul>

        <footer class="task-foot">
          <span class="muted">{{ card.last_run_label }}：{{ fmtTime(card.last_run) }}</span>
          <el-switch
            v-if="card.switch"
            v-model="card.switch.enabled"
            :loading="toggling[card.key]"
            :active-text="card.switch.label"
            @change="(v: boolean) => onToggle(card, v)"
          />
        </footer>
      </section>
    </div>
  </div>
</template>

<style scoped>
.checked-at { color: var(--au-text-3); font-size: 12px; }

.task-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  align-items: start;
}

.task-card {
  background: var(--au-bg-soft);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}

.task-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot--pulse { animation: task-dot-pulse 1.6s ease-in-out infinite; }

@keyframes task-dot-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

.task-icon { color: var(--au-text-2); flex-shrink: 0; }

.task-title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--au-text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-status {
  font-size: 12px;
  color: var(--au-text-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-sm);
  padding: 2px 8px;
  flex-shrink: 0;
}

.task-link {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 12px;
  color: var(--au-primary);
  text-decoration: none;
  flex-shrink: 0;
}

.task-summary {
  margin: 0;
  font-size: 13px;
  color: var(--au-text-2);
  line-height: 1.6;
}

.task-metrics {
  margin: 0;
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--au-border);
  padding-top: 4px;
}

.metric-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px solid var(--au-border);
  font-size: 13px;
}

.metric-row:last-child { border-bottom: 0; }

.metric-row dt { color: var(--au-text-3); }

.metric-row dd {
  margin: 0;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.lib-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  max-height: 220px;
  overflow-y: auto;
  border-top: 1px solid var(--au-border);
}

.lib-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 0;
  border-bottom: 1px solid var(--au-border);
  font-size: 12px;
}

.lib-row:last-child { border-bottom: 0; }

.lib-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--au-text-2);
}

.lib-chip {
  flex-shrink: 0;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--au-r-sm);
  border: 1px solid var(--au-border);
  color: var(--au-text-3);
}

.lib-chip--running {
  color: var(--au-primary);
  border-color: var(--au-primary-border);
  background: var(--au-primary-soft);
}

.lib-chip--ok {
  color: var(--au-success);
  border-color: var(--au-success-border);
  background: var(--au-success-soft);
}

.lib-chip--warn {
  color: var(--au-warning);
  border-color: var(--au-warning-border);
  background: var(--au-warning-soft);
}

.lib-chip--bad {
  color: var(--au-danger);
  border-color: var(--au-danger-border);
  background: var(--au-danger-soft);
}

.lib-time { flex-shrink: 0; }

.task-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border-top: 1px solid var(--au-border);
  padding-top: 10px;
  margin-top: auto;
}

.muted { color: var(--au-text-3); font-size: 12px; }
.mono { font-variant-numeric: tabular-nums; }

@media (max-width: 1200px) {
  .task-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 720px) {
  .task-grid { grid-template-columns: 1fr; }
  .task-foot { flex-direction: column; align-items: stretch; gap: 8px; }
}
</style>
