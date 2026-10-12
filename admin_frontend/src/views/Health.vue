<script setup lang="ts">
/**
 * 健康总览（可观测底座的上层 UI，v2.56.0）
 *
 * - 数据源：GET /api/health 的 observability 字段（后端开关 OBSERVABILITY_ENABLED 控制）
 * - 十个子系统同屏：红黄绿灯只展示状态，不做诊断（诊断是一键体检的事）
 * - 品牌约束：只用 --au-* 变量，零新增色值；暗房影院深色主题
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { RefreshCw } from 'lucide-vue-next'
import {
  Activity,
  Cpu,
  Database,
  FileText,
  HardDrive,
  KeyRound,
  Radar,
  ScanLine,
  Sparkles,
  Zap,
} from 'lucide-vue-next'
import { EmptyState, PageHeader } from '@/components/ui'
import { fetchPanelHealth } from '@/api/admin'

interface SubsystemProbe {
  key: string
  status: 'ok' | 'warn' | 'down'
  message: string
  metrics: Record<string, any>
}

interface ObservabilitySnapshot {
  enabled: boolean
  level?: 'ok' | 'warn' | 'down'
  subsystems?: Record<string, SubsystemProbe>
  thresholds?: Record<string, number>
}

interface SubsystemMeta {
  key: string
  name: string
  icon: unknown
}

const SUBSYSTEMS: SubsystemMeta[] = [
  { key: 'database', name: '数据库', icon: Database },
  { key: 'redis', name: '缓存', icon: Zap },
  { key: 'disk', name: '磁盘', icon: HardDrive },
  { key: 'workers', name: '后台线程', icon: Cpu },
  { key: 'probe', name: '探测队列', icon: Radar },
  { key: 'enrich', name: '元数据补全', icon: Sparkles },
  { key: 'scan', name: '媒体扫描', icon: ScanLine },
  { key: 'strm', name: '.strm 生成', icon: FileText },
  { key: 'sa_quota', name: 'SA 账号', icon: KeyRound },
  { key: 'api', name: '接口指标', icon: Activity },
]

const snapshot = ref<ObservabilitySnapshot | null>(null)
const loading = ref(false)
const loadError = ref('')
const lastChecked = ref('')

function fmtNum(n: unknown): string {
  if (n === null || n === undefined || n === '') return '—'
  const v = Number(n)
  if (Number.isNaN(v)) return '—'
  return v.toLocaleString('en-US')
}

function metricOf(key: string, name: string): string {
  const m = snapshot.value?.subsystems?.[key]?.metrics || {}
  const v = m[name]
  if (v === null || v === undefined || v === '') return '—'
  if (Array.isArray(v)) return fmtNum(v.length)
  if (typeof v === 'boolean') return v ? '是' : '否'
  return fmtNum(v)
}

/** 每个子系统展示 2 个关键指标（label, metrics 字段名，单位后缀） */
function keyMetrics(key: string): Array<{ label: string; field: string; suffix: string }> {
  switch (key) {
    case 'disk':
      return [
        { label: '磁盘剩余', field: 'disk_free_percent', suffix: '%' },
        { label: '转码中', field: 'transcode_sessions', suffix: '' },
      ]
    case 'workers':
      return [
        { label: '存活', field: 'total', suffix: '' },
        { label: '死亡', field: 'dead', suffix: '' },
      ]
    case 'probe':
      return [
        { label: '待探测', field: 'pending_ready', suffix: '' },
        { label: '重试中', field: 'retrying', suffix: '' },
      ]
    case 'enrich':
      return [
        { label: '积压', field: 'backlog', suffix: '' },
        { label: '已完成', field: 'done', suffix: '' },
      ]
    case 'scan':
      return [
        { label: '扫描中', field: 'scanning', suffix: '' },
        { label: '失败', field: 'failed', suffix: '' },
      ]
    case 'strm':
      return [
        { label: '距上次', field: 'lag_hours', suffix: ' 小时' },
        { label: '上次生成', field: 'last_generated', suffix: '' },
      ]
    case 'sa_quota':
      return [{ label: 'SA 数量', field: 'sa_count', suffix: '' }]
    case 'api':
      return [
        { label: 'P99', field: 'global_p99_ms', suffix: 'ms' },
        { label: '错误率', field: 'error_ratio_pct', suffix: '%' },
      ]
    default:
      return []
  }
}

/** api 的错误率是小数，转成百分比展示 */
function metricValue(key: string, field: string, suffix: string): string {
  if (key === 'api' && field === 'error_ratio_pct') {
    const m = snapshot.value?.subsystems?.api?.metrics || {}
    const v = m['global_error_ratio']
    if (v === null || v === undefined || v === '') return '—'
    return `${(Number(v) * 100).toFixed(2)}${suffix}`
  }
  if (key === 'strm' && field === 'lag_hours') {
    const m = snapshot.value?.subsystems?.strm?.metrics || {}
    const v = m['lag_hours']
    if (v === null || v === undefined || v === '') return '—'
    return `${Number(v).toFixed(1)}${suffix}`
  }
  return `${metricOf(key, field)}${suffix}`
}

const overallLevel = computed(() => snapshot.value?.level || 'ok')
const overallText = computed(() =>
  overallLevel.value === 'down' ? '有故障' : overallLevel.value === 'warn' ? '有警告' : '正常',
)

const cards = computed(() =>
  SUBSYSTEMS.map((meta) => {
    const probe = snapshot.value?.subsystems?.[meta.key]
    return {
      ...meta,
      status: probe?.status || 'warn',
      message: probe?.message || '暂无数据',
      metrics: keyMetrics(meta.key),
    }
  }),
)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const h = await fetchPanelHealth()
    snapshot.value = h.observability || { enabled: false }
    lastChecked.value = new Date().toLocaleTimeString()
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '请求失败'
    ElMessage.error('健康数据加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="au-health">
    <PageHeader
      eyebrow="系统与审计"
      title="健康总览"
      description="十个子系统的实时状态：绿灯正常、黄灯警告、红灯故障。只展示不诊断。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <el-icon><RefreshCw :size="14" /></el-icon>
          刷新
        </el-button>
      </template>
    </PageHeader>

    <el-skeleton v-if="loading && !snapshot" :rows="6" animated />

    <EmptyState
      v-else-if="loadError"
      title="加载失败"
      :description="loadError"
    />

    <!-- 开关未开 -->
    <EmptyState
      v-else-if="snapshot && !snapshot.enabled"
      title="可观测未启用"
      description="后端开关 OBSERVABILITY_ENABLED 未开启，暂无子系统状态数据。开启后这里会显示十个子系统的实时状态灯。"
    />

    <template v-else-if="snapshot?.enabled">
      <!-- 总体状态条 -->
      <div class="au-health__overall" :class="`is-${overallLevel}`">
        <span class="au-health__dot" :class="`is-${overallLevel}`" />
        <span class="au-health__overall-text">整体：{{ overallText }}</span>
        <span v-if="lastChecked" class="au-health__overall-time">最后刷新 {{ lastChecked }}</span>
      </div>

      <!-- 子系统卡片网格 -->
      <div class="au-health__grid">
        <div
          v-for="card in cards"
          :key="card.key"
          class="au-health__card"
          :class="`is-${card.status}`"
        >
          <div class="au-health__card-head">
            <span class="au-health__dot" :class="`is-${card.status}`" />
            <component :is="card.icon" :size="15" class="au-health__card-icon" />
            <span class="au-health__card-name">{{ card.name }}</span>
            <span class="au-health__card-key">{{ card.key }}</span>
          </div>
          <p class="au-health__card-msg">{{ card.message }}</p>
          <div v-if="card.metrics.length" class="au-health__card-metrics">
            <div
              v-for="m in card.metrics"
              :key="m.field"
              class="au-health__metric"
            >
              <span class="au-health__metric-label">{{ m.label }}</span>
              <span class="au-health__metric-value">{{ metricValue(card.key, m.field, m.suffix) }}</span>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.au-health {
  padding: 0;
}

.au-health__overall {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 20px;
  padding: 14px 18px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-left: 3px solid var(--au-success);
  border-radius: var(--au-r-lg);
}

.au-health__overall.is-warn { border-left-color: var(--au-warning); }
.au-health__overall.is-down { border-left-color: var(--au-danger); }

.au-health__dot {
  width: 12px;
  height: 12px;
  flex: 0 0 12px;
  border-radius: 50%;
  background: var(--au-success);
}

.au-health__dot.is-warn { background: var(--au-warning); }
.au-health__dot.is-down { background: var(--au-danger); }

.au-health__overall-text {
  font-size: 15px;
  font-weight: 600;
  color: var(--au-text);
}

.au-health__overall-time {
  margin-left: auto;
  font-size: 12px;
  color: var(--au-text-3);
}

.au-health__grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}

.au-health__card {
  padding: 16px 18px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-left: 3px solid var(--au-success);
  border-radius: var(--au-r-lg);
  min-width: 0;
}

.au-health__card.is-warn { border-left-color: var(--au-warning); }
.au-health__card.is-down { border-left-color: var(--au-danger); }

.au-health__card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.au-health__card-icon {
  color: var(--au-text-3);
  flex-shrink: 0;
}

.au-health__card-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--au-text);
}

.au-health__card-key {
  margin-left: auto;
  font-size: 11px;
  color: var(--au-text-4);
  font-family: ui-monospace, monospace;
}

.au-health__card-msg {
  margin: 0 0 12px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--au-text-2);
}

.au-health__card-metrics {
  display: flex;
  gap: 20px;
  padding-top: 12px;
  border-top: 1px solid var(--au-border);
}

.au-health__metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.au-health__metric-label {
  font-size: 11px;
  color: var(--au-text-3);
  letter-spacing: 0.04em;
}

.au-health__metric-value {
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

@media (max-width: 1024px) {
  .au-health__grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 768px) {
  .au-health__grid {
    grid-template-columns: 1fr;
  }

  .au-health__overall-time {
    display: none;
  }
}
</style>
