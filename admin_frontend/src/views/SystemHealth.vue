<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Activity, AlertTriangle, Bell, Cpu, Database, Film, HardDrive, KeyRound, Radar, RefreshCw, Radio, ScanLine, Sparkles, Users, Zap } from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import { fetchEmbyOverview, fetchMounts, fetchPanelHealth, fetchSessions } from '@/api/admin'
import { fetchBackupConfig, saveBackupConfig, runBackupNow } from '@/api/admin'
import {
  fetchAlertRules,
  updateAlertRule,
  fetchAlertSettings,
  saveAlertSettings,
  fetchAlerts,
  ackAlert,
  sendTestAlert,
} from '@/api/admin'
import type { EmbySessionRow, StorageMount } from '@/types'
import type { PanelHealth } from '@/api/admin'
import type { BackupConfig } from '@/api/admin'
import type {
  AlertChannel,
  AlertRecord,
  AlertRule,
  AlertSeverity,
  AlertSettings,
  AlertStatus,
} from '@/api/admin'

const health = ref<PanelHealth | null>(null)
const sessions = ref<EmbySessionRow[]>([])
const mounts = ref<StorageMount[]>([])
const overview = ref<{ total_items: number; total_libraries: number; active_sessions: number; total_users: number } | null>(null)
const loading = ref(false)
const lastChecked = ref('')
/** 面板健康接口失败（其余子查询各自兜底，不算失败） */
const loadError = ref('')

const backup = ref<BackupConfig | null>(null)
const backupSaving = ref(false)
const backupRunning = ref(false)

// ---------- 健康灯（原「健康总览」页并入，Batch E：健康体系合并） ----------
// 数据源与本页共用一次 GET /api/health：observability 字段由后端开关 OBSERVABILITY_ENABLED 控制。
interface LampMeta {
  key: string
  name: string
  icon: unknown
}
const LAMPS: LampMeta[] = [
  { key: 'database', name: '数据库', icon: Database },
  { key: 'redis', name: '缓存', icon: Zap },
  { key: 'disk', name: '磁盘', icon: HardDrive },
  { key: 'workers', name: '后台线程', icon: Cpu },
  // 三套词汇统一（任务 / 健康灯 / 体检）：与「任务总览」任务卡标题对齐
  { key: 'probe', name: '媒体探测', icon: Radar },
  { key: 'enrich', name: '元数据刮削', icon: Sparkles },
  { key: 'scan', name: '媒体库扫描', icon: ScanLine },
  { key: 'sa_quota', name: 'Google 子账号', icon: KeyRound },
  { key: 'api', name: '接口指标', icon: Activity },
]
/** 状态 → 中文结论（卡片右上角，原来是裸英文 key） */
const LAMP_STATUS_TEXT: Record<string, string> = { ok: '正常', warn: '警告', down: '故障' }

const observability = computed(() => health.value?.observability ?? null)

function lampFmtNum(n: unknown): string {
  if (n === null || n === undefined || n === '') return '—'
  const v = Number(n)
  if (Number.isNaN(v)) return '—'
  return v.toLocaleString('en-US')
}

function lampMetricOf(key: string, name: string): string {
  const m = observability.value?.subsystems?.[key]?.metrics || {}
  const v = m[name]
  if (v === null || v === undefined || v === '') return '—'
  if (Array.isArray(v)) return lampFmtNum(v.length)
  if (typeof v === 'boolean') return v ? '是' : '否'
  return lampFmtNum(v)
}

/** 每个子系统展示 2 个关键指标（label, metrics 字段名，单位后缀） */
function lampKeyMetrics(key: string): Array<{ label: string; field: string; suffix: string }> {
  switch (key) {
    case 'disk':
      return [
        { label: '磁盘剩余', field: 'disk_free_percent', suffix: '%' },
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
function lampMetricValue(key: string, field: string, suffix: string): string {
  if (key === 'api' && field === 'error_ratio_pct') {
    const m = observability.value?.subsystems?.api?.metrics || {}
    const v = m['global_error_ratio']
    if (v === null || v === undefined || v === '') return '—'
    // R40：后端给非数字（如 "N/A"）时不显示 NaN%
    const pct = Number(v) * 100
    if (!Number.isFinite(pct)) return '—'
    return `${pct.toFixed(2)}${suffix}`
  }
  return `${lampMetricOf(key, field)}${suffix}`
}

const lampOverallLevel = computed(() => observability.value?.level || 'ok')
const lampOverallText = computed(() =>
  lampOverallLevel.value === 'down' ? '有故障' : lampOverallLevel.value === 'warn' ? '有警告' : '正常',
)

const lampCards = computed(() =>
  LAMPS.map((meta) => {
    const probe = observability.value?.subsystems?.[meta.key]
    const status = probe?.status || 'warn'
    return {
      ...meta,
      status,
      statusText: LAMP_STATUS_TEXT[status] ?? status,
      message: probe?.message || '暂无数据',
      metrics: lampKeyMetrics(meta.key),
    }
  }),
)

/** 「看告警」：同页滚动到下方的告警通知区块 */
function scrollToAlerts() {
  document.getElementById('alerts')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// ---------- 告警通知 ----------
const alertSettings = ref<AlertSettings | null>(null)
const alertSettingsSaving = ref(false)
/** 设置表单（与展示分离，避免未保存污染） */
const alertForm = ref({ enabled: false, check_interval_sec: 600, webhook_url: '' })
/** 高级设置折叠：默认收起（el-collapse v-model 为空数组即全收；配置瘦身第 3b 批） */
const alertAdvOpen = ref<string[]>([])
/** webhook 密钥：只用于提交，后端不回显 */
const alertWebhookSecret = ref('')
const alertTestChannel = ref<AlertChannel>('in_app')
const alertTestSending = ref(false)

const alertRules = ref<AlertRule[]>([])
const alertAllowedChannels = ref<string[]>([])
const alertRulesLoading = ref(false)
const alertRuleDialogVisible = ref(false)
const alertRuleSaving = ref(false)
const alertEditingRule = ref<AlertRule | null>(null)
/** 收件人配置：对话框里不展示编辑控件，打开时原样带过来、保存时原样回传，避免静默覆盖 */
const alertRuleRecipients = ref<Record<string, unknown>>({ staff: true })
const alertRuleForm = ref({
  name: '',
  threshold: 0,
  severity: 'warning' as AlertSeverity,
  channels: [] as string[],
  cooldown_sec: 3600,
  is_active: true,
})

const alertTab = ref<AlertStatus>('open')
const alertList = ref<AlertRecord[]>([])
const alertListLoading = ref(false)
const alertAckingId = ref<number | null>(null)

/** 严重度中文 */
const ALERT_SEVERITY_LABEL: Record<string, string> = { info: '提示', warning: '警告', critical: '严重' }
/** 渠道中文 */
const ALERT_CHANNEL_LABEL: Record<string, string> = { in_app: '站内', email: '邮件', telegram: 'Telegram', webhook: 'Webhook' }
/** 事件类型中文（后端 event_type 枚举不直接上界面） */
const ALERT_EVENT_TYPE_LABEL: Record<string, string> = {
  probe_queue_high: '探测队列堆积',
  enrich_stalled: '补全停滞',
  disk_full: '磁盘空间不足',
  sa_quota_low: 'Google 子账号配额告警',
  scan_failed: '扫描失败',
  api_error_high: 'API 错误率过高',
}

function alertSeverityLabel(s: string | null | undefined): string {
  return ALERT_SEVERITY_LABEL[String(s ?? '')] ?? String(s ?? '未知')
}
function alertEventTypeLabel(t: string | null | undefined): string {
  const k = String(t ?? '')
  return ALERT_EVENT_TYPE_LABEL[k] ?? (k || '—')
}
function alertChannelLabels(channels: unknown): string {
  if (!Array.isArray(channels) || !channels.length) return '—'
  return channels.map((c) => ALERT_CHANNEL_LABEL[String(c)] ?? String(c)).join('、')
}
function alertCooldownLabel(sec: unknown): string {
  const n = Number(sec)
  if (!Number.isFinite(n) || n <= 0) return '—'
  if (n < 3600) return `${Math.round(n / 60)} 分钟`
  const h = n / 3600
  return `${Number.isInteger(h) ? h : h.toFixed(1)} 小时`
}
function alertTimeLabel(v: string | null | undefined): string {
  if (!v) return '—'
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString()
}
const alertEnabled = computed(() => !!alertSettings.value?.enabled)

function fmtSize(bytes: number): string {
  // 非数字/无穷大（比如后端没返回）直接给占位，避免 "NaN MB"
  if (!Number.isFinite(bytes)) return '—'
  if (bytes <= 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

const panelOk = computed(() => health.value?.status === 'healthy' || health.value?.status === 'degraded')
const panelDetail = computed(() => {
  if (health.value?.status === 'healthy') return 'API 正常响应'
  if (health.value?.status === 'degraded' || health.value?.status === 'unhealthy') {
    const issues = health.value?.health_issues?.map((i) => i.message).join('；')
    if (health.value?.status === 'unhealthy') {
      return issues ? `异常：${issues}` : '异常：原因未知，请查看后端日志'
    }
    return issues ? `有警告：${issues}` : '有警告，API 正常响应'
  }
  return '无法确认状态'
})

const checks = computed(() => [
  { label: 'EM 面板', detail: panelDetail.value, ok: panelOk.value, icon: Activity },
  { label: '共享数据库', detail: health.value?.database || '未返回数据库信息', ok: !!health.value, icon: Database },
  { label: '媒体网关', detail: overview.value ? `${overview.value.total_items} 个条目可用` : '等待媒体数据', ok: !!overview.value, icon: Film },
  { label: '存储来源', detail: `${mounts.value.filter((m) => m.is_enabled).length} 个挂载已启用`, ok: mounts.value.length === 0 || mounts.value.every((m) => m.last_check_ok !== false), icon: HardDrive },
])

async function load() {
  // 并发守卫：加载中重复触发时直接忽略，避免重复请求
  if (loading.value) return
  loading.value = true
  loadError.value = ''
  try {
    const [h, o, s, m, b] = await Promise.all([
      fetchPanelHealth(),
      fetchEmbyOverview().catch(() => null),
      fetchSessions().catch(() => ({ sessions: [] as EmbySessionRow[] })),
      fetchMounts().catch(() => ({ mounts: [], mount_types: [] })),
      fetchBackupConfig().catch(() => null),
    ])
    health.value = h
    overview.value = o
    sessions.value = s.sessions
    mounts.value = m.mounts
    backup.value = b
    lastChecked.value = new Date().toLocaleTimeString()
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '请求失败'
    ElMessage.error('健康检查失败，请确认 EM 服务仍在运行')
  } finally {
    loading.value = false
  }
}

async function saveBackup() {
  if (!backup.value) return
  if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(backup.value.time.trim())) {
    ElMessage.error('时间格式应为 HH:MM（24 小时制）')
    return
  }
  backupSaving.value = true
  try {
    backup.value = await saveBackupConfig(
      backup.value.enabled,
      backup.value.time.trim(),
      Math.max(1, Math.min(30, Math.round(backup.value.keep_days) || 7)),
    )
    ElMessage.success('备份配置已保存，立即生效')
  } catch {
    ElMessage.error('保存失败，请检查时间格式与保留天数')
  } finally {
    backupSaving.value = false
  }
}

async function manualBackup() {
  backupRunning.value = true
  try {
    const res = await runBackupNow()
    backup.value = res
    ElMessage.success(`备份完成：${res.backup.name}`)
  } catch {
    ElMessage.error('备份失败，请稍后重试')
  } finally {
    backupRunning.value = false
  }
}

function fmtDate(value: string | null): string {
  return value ? value.slice(0, 16).replace('T', ' ') : '—'
}

// ---------- 告警通知 ----------
async function loadAlertSettings() {
  try {
    const s = await fetchAlertSettings()
    alertSettings.value = s
    alertForm.value = {
      enabled: !!s.enabled,
      check_interval_sec: s.check_interval_sec ?? 600,
      webhook_url: s.webhook_url ?? '',
    }
    alertWebhookSecret.value = ''
  } catch {
    alertSettings.value = null
  }
}

async function loadAlertRules() {
  alertRulesLoading.value = true
  try {
    const res = await fetchAlertRules()
    alertRules.value = Array.isArray(res.rules) ? res.rules : []
    alertAllowedChannels.value = Array.isArray(res.allowed_channels) ? res.allowed_channels : []
  } catch {
    alertRules.value = []
  } finally {
    alertRulesLoading.value = false
  }
}

async function loadAlerts() {
  alertListLoading.value = true
  try {
    const res = await fetchAlerts(alertTab.value, 50)
    alertList.value = Array.isArray(res.alerts) ? res.alerts : []
  } catch {
    alertList.value = []
  } finally {
    alertListLoading.value = false
  }
}

/** 保存告警设置：只提交变更字段（secret 为空不传，避免清空） */
async function onSaveAlertSettings() {
  const s = alertSettings.value
  const payload: { enabled?: boolean; check_interval_sec?: number; webhook_url?: string; webhook_secret?: string } = {}
  if (!s || alertForm.value.enabled !== !!s.enabled) payload.enabled = alertForm.value.enabled
  const iv = Math.max(60, Math.min(86400, Math.round(alertForm.value.check_interval_sec) || 600))
  if (!s || iv !== s.check_interval_sec) payload.check_interval_sec = iv
  if (!s || alertForm.value.webhook_url !== (s.webhook_url ?? '')) payload.webhook_url = alertForm.value.webhook_url.trim()
  if (alertWebhookSecret.value) payload.webhook_secret = alertWebhookSecret.value
  if (!Object.keys(payload).length) {
    ElMessage.info('没有改动')
    return
  }
  alertSettingsSaving.value = true
  try {
    await saveAlertSettings(payload)
    ElMessage.success('告警设置已保存，立即生效')
    await loadAlertSettings()
  } catch {
    // 拦截器已提示
  } finally {
    alertSettingsSaving.value = false
  }
}

async function onSendTestAlert() {
  alertTestSending.value = true
  try {
    const res = await sendTestAlert(alertTestChannel.value)
    if (res.ok) ElMessage.success(`测试告警已发送（${alertChannelLabels([res.channel])}）`)
    else ElMessage.warning('发送失败，请检查渠道配置')
  } catch {
    // 拦截器已提示
  } finally {
    alertTestSending.value = false
  }
}

function openAlertRuleDialog(rule: AlertRule) {
  alertEditingRule.value = rule
  alertRuleRecipients.value = rule.recipients ?? { staff: true }
  alertRuleForm.value = {
    name: rule.name ?? '',
    threshold: rule.threshold ?? 0,
    severity: (['info', 'warning', 'critical'].includes(rule.severity) ? rule.severity : 'warning') as AlertSeverity,
    channels: Array.isArray(rule.channels) ? [...rule.channels] : [],
    cooldown_sec: rule.cooldown_sec ?? 3600,
    is_active: !!rule.is_active,
  }
  alertRuleDialogVisible.value = true
}

async function onSaveAlertRule() {
  const rule = alertEditingRule.value
  if (!rule) return
  alertRuleSaving.value = true
  try {
    await updateAlertRule(rule.id, {
      name: alertRuleForm.value.name.trim(),
      threshold: Math.max(0, Number(alertRuleForm.value.threshold) || 0),
      severity: alertRuleForm.value.severity,
      channels: alertRuleForm.value.channels,
      // 深拷贝一次再发，避免引用污染表单状态
      recipients: JSON.parse(JSON.stringify(alertRuleRecipients.value)) as Record<string, unknown>,
      cooldown_sec: Math.max(60, Math.min(86400 * 7, Math.round(alertRuleForm.value.cooldown_sec) || 3600)),
      is_active: alertRuleForm.value.is_active,
    })
    ElMessage.success('规则已更新')
    alertRuleDialogVisible.value = false
    await loadAlertRules()
  } catch {
    // 拦截器已提示
  } finally {
    alertRuleSaving.value = false
  }
}

async function onAckAlert(id: number) {
  alertAckingId.value = id
  try {
    await ackAlert(id)
    ElMessage.success('已确认')
    await loadAlerts()
  } catch {
    // 拦截器已提示
  } finally {
    alertAckingId.value = null
  }
}

function onAlertTabChange() {
  loadAlerts()
}

onMounted(() => {
  load()
  // 告警数据独立加载：失败不影响健康页主体
  loadAlertSettings()
  loadAlertRules()
  loadAlerts()
})
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="系统与审计"
      title="系统健康"
      description="十个子系统的实时状态灯（绿灯正常、黄灯警告、红灯故障，只展示不诊断），以及服务运行态、实时会话、数据库备份与告警通知。"
    >
      <template #actions>
        <span class="checked-at">最近检查 {{ lastChecked || '—' }}</span>
        <el-button :loading="loading" type="primary" @click="load" :icon="RefreshCw">
          重新检查
        </el-button>
      </template>
    </PageHeader>

    <!-- 健康灯：十个子系统同屏（原「健康总览」页并入 Batch E，黄/红灯直接挂深链） -->
    <SectionCard
      v-if="observability?.enabled"
      title="健康灯"
      :icon="Activity"
      :meta="lastChecked ? `最后刷新 ${lastChecked}` : ''"
      class="lamp-card"
    >
      <div class="lamp-overall" :class="`is-${lampOverallLevel}`">
        <span class="lamp-dot" :class="`is-${lampOverallLevel}`" />
        <span class="lamp-overall-text">整体：{{ lampOverallText }}</span>
      </div>
      <div class="lamp-grid">
        <div
          v-for="card in lampCards"
          :key="card.key"
          class="lamp"
          :class="`is-${card.status}`"
        >
          <div class="lamp-head">
            <span class="lamp-dot" :class="`is-${card.status}`" />
            <component :is="card.icon" :size="15" class="lamp-icon" />
            <span class="lamp-name">{{ card.name }}</span>
            <span class="lamp-status" :class="`is-${card.status}`">{{ card.statusText }}</span>
          </div>
          <p class="lamp-msg">{{ card.message }}</p>
          <div v-if="card.metrics.length" class="lamp-metrics">
            <div
              v-for="m in card.metrics"
              :key="m.field"
              class="lamp-metric"
            >
              <span class="lamp-metric-label">{{ m.label }}</span>
              <span class="lamp-metric-value">{{ lampMetricValue(card.key, m.field, m.suffix) }}</span>
            </div>
          </div>
          <!-- 黄/红灯深链：去体检找原因、看任务看进度、看告警看记录 -->
          <div v-if="card.status !== 'ok'" class="lamp-links">
            <router-link :to="{ name: 'Diagnosis' }" class="lamp-link">去体检</router-link>
            <router-link :to="{ name: 'TaskOverview' }" class="lamp-link">看任务</router-link>
            <button type="button" class="lamp-link is-btn" @click="scrollToAlerts">看告警</button>
          </div>
          <details class="lamp-raw">
            <summary>原始探针输出</summary>
            <code>{{ card.key }} · {{ card.message }}</code>
          </details>
        </div>
      </div>
    </SectionCard>
    <SectionCard
      v-else-if="health && !observability?.enabled"
      title="健康灯"
      :icon="Activity"
      class="lamp-card"
    >
      <EmptyState
        compact
        title="可观测未启用"
        description="后端开关 OBSERVABILITY_ENABLED 未开启，暂无子系统状态灯。开启后这里会显示十个子系统的实时状态灯。"
      />
    </SectionCard>

    <SectionCard v-if="loadError && !health" tone="accent">
      <EmptyState
        compact
        :icon="AlertTriangle"
        title="健康检查失败"
        :description="`请确认 EM 服务仍在运行（${loadError}）`"
      >
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>
    </SectionCard>

    <div class="health-grid" :aria-busy="loading">
      <StatTile
        v-for="item in checks"
        :key="item.label"
        layout="icon-left"
        :icon="item.icon"
        :label="item.label"
        :value="!health && loading ? '检查中' : item.ok ? '正常' : '异常'"
        :tone="!health && loading ? 'plain' : item.ok ? 'ok' : 'danger'"
        :hint="item.detail"
        :title="item.detail"
        class="health-tile"
      />
    </div>

    <SectionCard title="运行态" :icon="Radio" :meta="health?.emby_server || 'Aetrix Media Server'">
      <dl class="runtime-grid">
        <div><dt>在线用户</dt><dd>{{ health?.online_users ?? 0 }}</dd></div>
        <div><dt>在线会话</dt><dd>{{ overview?.active_sessions ?? sessions.length }}</dd></div>
        <div><dt>媒体条目</dt><dd>{{ overview?.total_items ?? 0 }}</dd></div>
        <div><dt>媒体库</dt><dd>{{ overview?.total_libraries ?? 0 }}</dd></div>
        <div><dt>管理员看到的挂载</dt><dd>{{ mounts.length }}</dd></div>
      </dl>
    </SectionCard>

    <div class="ops-grid">
      <SectionCard
        title="实时会话"
        :icon="Users"
        :meta="sessions.length > 8 ? `前 8 / 共 ${sessions.length}` : sessions.length ? `${sessions.length} 个` : ''"
      >
        <ul v-if="sessions.length" class="status-list">
          <li v-for="session in sessions.slice(0, 8)" :key="session.session_key" class="status-row">
            <span class="status-main">{{ session.username }} · {{ session.item }}</span>
            <span class="muted mono">{{ fmtDate(session.started_at) }}</span>
          </li>
        </ul>
        <EmptyState v-else compact :icon="Users" title="当前没有播放会话" />
      </SectionCard>

      <SectionCard title="数据库备份" :icon="Database" :meta="`上次执行 ${backup?.last_run || '—'}`">
        <div v-if="backup" class="backup-form">
          <div class="backup-row">
            <el-switch v-model="backup.enabled" active-text="定时备份" />
            <el-time-picker
              v-model="backup.time"
              format="HH:mm"
              value-format="HH:mm"
              placeholder="执行时间"
              class="w-time"
              :clearable="false"
            />
            <span class="keep">
              <span class="muted">保留</span>
              <el-input-number v-model="backup.keep_days" :min="1" :max="30" :controls="false" class="w-days" />
              <span class="muted">天</span>
            </span>
          </div>
          <div class="backup-row backup-actions">
            <el-button :loading="backupRunning" @click="manualBackup">立即备份</el-button>
            <el-button type="primary" :loading="backupSaving" @click="saveBackup">保存配置</el-button>
          </div>
          <ul v-if="backup.backups.length" class="status-list backup-list">
            <li v-for="f in backup.backups.slice(0, 7)" :key="f.name" class="status-row">
              <span class="status-main mono">{{ f.name }}</span>
              <span class="muted">{{ fmtSize(f.size) }} · {{ f.created_at }}</span>
            </li>
          </ul>
          <EmptyState v-else compact title="暂无备份文件" description="开启定时备份，或点「立即备份」生成第一份。" />
        </div>
        <div v-else-if="loading" class="backup-skeleton">
          <span class="au-skeleton" /><span class="au-skeleton" /><span class="au-skeleton is-short" />
        </div>
        <EmptyState v-else compact :icon="AlertTriangle" title="备份信息加载失败" description="不影响页面其他内容，可点「重新检查」再试。" />
      </SectionCard>
    </div>

    <!-- 告警通知 -->
    <SectionCard id="alerts" title="告警通知" :icon="Bell" :meta="alertEnabled ? '已启用' : '已关闭'" class="alert-card">
      <!-- 设置 -->
      <div class="alert-block">
        <div class="alert-block-title">设置</div>
        <div class="alert-settings-row">
          <el-switch v-model="alertForm.enabled" active-text="总开关" />
        </div>
        <!-- 高级设置：检查间隔（一辈子不用碰的低频项，默认收起；配置瘦身第 3b 批） -->
        <el-collapse v-model="alertAdvOpen" class="alert-advanced">
          <el-collapse-item name="advanced" title="高级设置（1）">
            <div class="alert-settings-row">
              <span class="alert-field">
                <span class="muted">检查间隔</span>
                <el-input-number v-model="alertForm.check_interval_sec" :min="60" :max="86400" :controls="false" class="w-seconds" />
                <span class="muted">秒</span>
              </span>
              <span class="hint">60~86400，默认 600，保存后立即生效</span>
            </div>
          </el-collapse-item>
        </el-collapse>
        <div class="alert-settings-row">
          <el-input v-model="alertForm.webhook_url" placeholder="Webhook 通知 URL（可选）" clearable class="w-webhook" />
          <el-input v-model="alertWebhookSecret" type="password" show-password placeholder="签名密钥（为空则不修改）" class="w-webhook" />
        </div>
        <div v-if="alertSettings" class="alert-secret-hint muted">
          密钥状态：{{ alertSettings.webhook_secret_set ? '已设置' : '未设置' }}
          <span v-if="alertSettings.enabled_source === 'env'"> · 总开关由环境变量控制，页面开关不生效</span>
        </div>
        <div class="alert-actions">
          <el-select v-model="alertTestChannel" class="w-channel">
            <el-option label="站内" value="in_app" />
            <el-option label="邮件" value="email" />
            <el-option label="Telegram" value="telegram" />
            <el-option label="Webhook" value="webhook" />
          </el-select>
          <el-button :loading="alertTestSending" @click="onSendTestAlert">发送测试</el-button>
          <el-button type="primary" :loading="alertSettingsSaving" @click="onSaveAlertSettings">保存设置</el-button>
        </div>
      </div>

      <!-- 规则 -->
      <div class="alert-block">
        <div class="alert-block-title">告警规则</div>
        <el-table v-if="alertRules.length" :data="alertRules" size="small" class="alert-table">
          <el-table-column prop="event_type" label="事件" min-width="140">
            <template #default="{ row }">{{ alertEventTypeLabel(row.event_type) }}</template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="120" />
          <el-table-column label="阈值" width="90">
            <template #default="{ row }">{{ row.threshold }}</template>
          </el-table-column>
          <el-table-column label="严重度" width="80">
            <template #default="{ row }">
              <span :class="['sev-tag', `sev-tag--${row.severity}`]">{{ alertSeverityLabel(row.severity) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="渠道" min-width="120">
            <template #default="{ row }">{{ alertChannelLabels(row.channels) }}</template>
          </el-table-column>
          <el-table-column label="冷却" width="90">
            <template #default="{ row }">{{ alertCooldownLabel(row.cooldown_sec) }}</template>
          </el-table-column>
          <el-table-column label="启用" width="70">
            <template #default="{ row }">{{ row.is_active ? '开' : '关' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openAlertRuleDialog(row)">编辑</el-button>
            </template>
          </el-table-column>
        </el-table>
        <EmptyState v-else-if="!alertRulesLoading" compact :icon="Bell" title="暂无告警规则" description="系统会自动生成一套默认告警规则，点右上角「重新检查」就能看到。" />
      </div>

      <!-- 记录 -->
      <div class="alert-block">
        <div class="alert-block-title">告警记录</div>
        <el-tabs v-model="alertTab" @tab-change="onAlertTabChange">
          <el-tab-pane label="待处理" name="open" />
          <el-tab-pane label="已确认" name="acknowledged" />
          <el-tab-pane label="已恢复" name="resolved" />
        </el-tabs>
        <ul v-if="alertList.length" class="status-list">
          <li v-for="a in alertList" :key="a.id" class="status-row alert-row">
            <div class="alert-main">
              <span :class="['sev-tag', `sev-tag--${a.severity}`]">{{ alertSeverityLabel(a.severity) }}</span>
              <span class="status-main">{{ a.title }}</span>
              <span v-if="!a.notified_ok" class="send-fail">发送失败</span>
            </div>
            <div class="alert-sub muted">
              <span>{{ alertEventTypeLabel(a.event_type) }}</span>
              <span>{{ alertTimeLabel(a.created_at) }}</span>
              <el-button
                v-if="a.status === 'open'"
                link
                type="primary"
                :loading="alertAckingId === a.id"
                @click="onAckAlert(a.id)"
              >
                确认
              </el-button>
            </div>
          </li>
        </ul>
        <EmptyState v-else-if="!alertListLoading" compact :icon="Bell" title="暂无告警记录" description="系统正常时这里是空的。" />
      </div>
    </SectionCard>

    <!-- 规则编辑弹窗 -->
    <el-dialog v-model="alertRuleDialogVisible" title="编辑告警规则" width="440px">
      <el-form label-width="90px" @submit.prevent>
        <el-form-item label="名称">
          <el-input v-model="alertRuleForm.name" maxlength="100" clearable />
        </el-form-item>
        <el-form-item label="阈值">
          <el-input-number v-model="alertRuleForm.threshold" :min="0" :controls="false" class="w-full" />
        </el-form-item>
        <el-form-item label="严重度">
          <el-select v-model="alertRuleForm.severity" class="w-full">
            <el-option label="提示" value="info" />
            <el-option label="警告" value="warning" />
            <el-option label="严重" value="critical" />
          </el-select>
        </el-form-item>
        <el-form-item label="渠道">
          <el-checkbox-group v-model="alertRuleForm.channels">
            <el-checkbox v-for="c in alertAllowedChannels" :key="c" :label="c">
              {{ ALERT_CHANNEL_LABEL[c] ?? c }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="冷却">
          <div class="alert-field">
            <el-input-number v-model="alertRuleForm.cooldown_sec" :min="60" :max="604800" :controls="false" class="w-seconds" />
            <span class="muted">秒</span>
          </div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="alertRuleForm.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="alertRuleDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="alertRuleSaving" @click="onSaveAlertRule">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.checked-at { font-size: 12px; color: var(--au-text-4); }

.health-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
/* 脚注只放一行（悬停看全文），四块高度对齐 */
.health-tile :deep(.au-stat__hint) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.health-tile :deep(.au-stat__value) { font-size: 1.25rem; }

.runtime-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
  margin: 0;
}
.runtime-grid div {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-surface-2);
}
.runtime-grid dt { font-size: 12px; color: var(--au-text-3); }
.runtime-grid dd {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--au-text);
}

.ops-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }

.status-list { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
.status-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 0;
  border-bottom: 1px solid var(--au-border);
  font-size: 13px;
  color: var(--au-text-2);
}
.status-row:last-child { border-bottom: 0; }
.status-main { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-row .muted { flex-shrink: 0; }
.muted { color: var(--au-text-3); font-size: 12px; }

.backup-form { display: flex; flex-direction: column; gap: 12px; }
.backup-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.backup-actions { justify-content: flex-end; }
.keep { display: inline-flex; align-items: center; gap: 6px; }
.w-time { width: 130px; }
.w-days { width: 70px; }
.backup-list { max-height: 220px; overflow-y: auto; border-top: 1px solid var(--au-border); }
.backup-skeleton { display: flex; flex-direction: column; gap: 10px; }
.backup-skeleton .au-skeleton { display: block; height: 14px; border-radius: var(--au-r-sm); }
.backup-skeleton .is-short { width: 60%; }

@media (max-width: 1024px) {
  .health-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .runtime-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .ops-grid { grid-template-columns: 1fr; }
}

@media (max-width: 640px) {
  .health-grid { grid-template-columns: 1fr; gap: 10px; }
  .runtime-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .status-row { flex-direction: column; gap: 2px; }
  .backup-actions { justify-content: stretch; }
  .backup-actions :deep(.el-button) { flex: 1; margin-left: 0; }
}

/* ---------- 健康灯（原「健康总览」页并入，Batch E） ---------- */
.lamp-card { margin-bottom: 16px; }

.lamp-overall {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  padding: 12px 16px;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-left: 3px solid var(--au-success);
  border-radius: var(--au-r-md);
}

.lamp-overall.is-warn { border-left-color: var(--au-warning); }
.lamp-overall.is-down { border-left-color: var(--au-danger); }

.lamp-dot {
  width: 12px;
  height: 12px;
  flex: 0 0 12px;
  border-radius: 50%;
  background: var(--au-success);
}

.lamp-dot.is-warn { background: var(--au-warning); }
.lamp-dot.is-down { background: var(--au-danger); }

.lamp-overall-text {
  font-size: 14px;
  font-weight: 600;
  color: var(--au-text);
}

.lamp-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.lamp {
  padding: 14px 16px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-left: 3px solid var(--au-success);
  border-radius: var(--au-r-md);
  min-width: 0;
}

.lamp.is-warn { border-left-color: var(--au-warning); }
.lamp.is-down { border-left-color: var(--au-danger); }

.lamp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.lamp-icon {
  color: var(--au-text-3);
  flex-shrink: 0;
}

.lamp-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--au-text);
}

.lamp-status {
  margin-left: auto;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-success-border);
  color: var(--au-success);
  white-space: nowrap;
}

.lamp-status.is-warn {
  border-color: var(--au-warning-border);
  color: var(--au-warning);
}

.lamp-status.is-down {
  border-color: var(--au-danger-border);
  color: var(--au-danger);
}

.lamp-msg {
  margin: 0 0 10px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--au-text-2);
}

.lamp-metrics {
  display: flex;
  gap: 20px;
  padding-top: 10px;
  border-top: 1px solid var(--au-border);
}

.lamp-metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.lamp-metric-label {
  font-size: 11px;
  color: var(--au-text-3);
  letter-spacing: 0.04em;
}

.lamp-metric-value {
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/** 黄/红灯深链：去体检找原因、看任务看进度、看告警看记录 */
.lamp-links {
  display: flex;
  gap: 8px;
  padding-top: 10px;
  margin-top: 10px;
  border-top: 1px dashed var(--au-border);
}

.lamp-link {
  font-size: 12px;
  padding: 4px 12px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-full);
  color: var(--au-primary);
  background: transparent;
  cursor: pointer;
  text-decoration: none;
  white-space: nowrap;
}

.lamp-link:hover {
  border-color: var(--au-primary);
}

.lamp-link.is-btn {
  font-family: inherit;
}

/** 原始探针输出：英文 key 与探针原文收进折叠，默认不打扰 */
.lamp-raw {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--au-border);
  font-size: 11px;
  color: var(--au-text-4);
}

.lamp-raw summary {
  cursor: pointer;
  user-select: none;
}

.lamp-raw code {
  display: block;
  margin-top: 6px;
  font-family: ui-monospace, monospace;
  word-break: break-all;
  color: var(--au-text-3);
}

@media (max-width: 1024px) {
  .lamp-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 768px) {
  .lamp-grid {
    grid-template-columns: 1fr;
  }
}

/* ---------- 告警通知 ---------- */
.alert-card { margin-top: 16px; }
.alert-block { margin-bottom: 20px; }
.alert-block:last-child { margin-bottom: 0; }
.alert-block-title { font-size: 14px; font-weight: 600; color: var(--au-text); margin-bottom: 10px; }
/* 高级设置折叠：与 RegistrationCodes 的 gen-advanced 同口径（默认收起） */
.alert-advanced { margin-top: 2px; }
.alert-advanced .el-collapse-item__header { font-size: var(--font-size-xs, 12px); color: var(--au-text-3); border-bottom: none; }
.alert-settings-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.alert-field { display: inline-flex; align-items: center; gap: 6px; }
.alert-secret-hint { font-size: 12px; margin-bottom: 10px; }
.alert-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
.w-seconds { width: 110px; }
.w-webhook { max-width: 320px; }
.w-channel { width: 120px; }
.w-full { width: 100%; }
.alert-table { margin-top: 4px; }
.alert-row { flex-direction: column; align-items: stretch; gap: 4px; }
.alert-main { display: flex; align-items: center; gap: 8px; min-width: 0; }
.alert-sub { display: flex; align-items: center; gap: 10px; }
.sev-tag {
  flex-shrink: 0;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--au-r-md);
  border: 1px solid var(--au-border);
  color: var(--au-text-2);
}
.sev-tag--info { color: var(--au-text-2); }
.sev-tag--warning { color: var(--au-primary); border-color: color-mix(in srgb, var(--au-primary) 40%, transparent); }
.sev-tag--critical { color: var(--au-danger); border-color: color-mix(in srgb, var(--au-danger) 40%, transparent); }
.send-fail { font-size: 11px; color: var(--au-danger); }
.hint { font-size: 12px; color: var(--au-text-2); }

@media (max-width: 640px) {
  .alert-settings-row { flex-direction: column; align-items: stretch; }
  .w-webhook { max-width: none; }
  .alert-actions { justify-content: stretch; }
  .alert-actions :deep(.el-button),
  .alert-actions :deep(.el-select) { flex: 1; margin-left: 0; }
}
</style>
