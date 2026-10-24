<script setup lang="ts">
/**
 * 一键体检（只读诊断 v2.57.0；阶段二加自动修复）
 *
 * - 数据源：GET /api/admin/diagnosis/run（后端只读规则引擎，不写库不改配置）
 * - 9 条规则逐项给结论：正常也说一句，报告是一份完整清单
 * - 可修复的规则带「一键修复」按钮：点击 → 二次确认弹窗 →
 *   POST /api/admin/diagnosis/fix（confirm=true）→ 展示结果 → 重新体检
 * - 品牌约束：只用 --au-* 变量，零新增色值；暗房影院深色主题
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Play, RefreshCw, Stethoscope, Wrench } from 'lucide-vue-next'
import { EmptyState, PageHeader } from '@/components/ui'
import {
  fetchDiagnosisRules,
  fixDiagnosisRule,
  runDiagnosis,
  type DiagnosisFinding,
  type DiagnosisFixInfo,
  type DiagnosisReport,
} from '@/api/admin'

const report = ref<DiagnosisReport | null>(null)
const ruleNames = ref<Record<string, string>>({})
const fixes = ref<Record<string, DiagnosisFixInfo>>({})
const loading = ref(false)
const loadError = ref('')
const lastChecked = ref('')
const fixingRule = ref('')

const LEVEL_ICON = {
  info: 'is-ok',
  warn: 'is-warn',
  critical: 'is-down',
} as const

const LEVEL_ORDER = { critical: 0, warn: 1, info: 2 } as const

const sortedFindings = computed(() =>
  [...(report.value?.findings ?? [])].sort((a, b) => LEVEL_ORDER[a.level] - LEVEL_ORDER[b.level]),
)

const summary = computed(() => report.value?.summary || { total: 0, info: 0, warn: 0, critical: 0 })

const overallClass = computed(() => {
  if (summary.value.critical > 0) return 'is-down'
  if (summary.value.warn > 0) return 'is-warn'
  return 'is-ok'
})

const overallText = computed(() => {
  if (!report.value) return '还没体检过'
  if (summary.value.critical > 0) return `${summary.value.critical} 项严重，先看这几项`
  if (summary.value.warn > 0) return `${summary.value.warn} 项需要注意`
  return '全部正常'
})

/** 证据字段中文（后端英文字段名不上界面，未收录的原样兜底） */
const EVIDENCE_KEY_LABEL: Record<string, string> = {
  status: '状态',
  disk_free_percent: '磁盘剩余%',
  disk_free_percent_raw: '磁盘剩余（原始值）',
  pending_ready: '待探测',
  retrying: '重试中',
  running: '运行中',
  paused: '已暂停',
  rate_per_min: '每分钟处理',
  eta_hours: '预计完成（小时）',
  backlog: '积压',
  idle_sec: '空闲秒数',
  done_per_min: '每分钟完成',
  open_breakers: '熔断中的挂载',
  failed: '失败库数',
  last_failed: '最近失败的库',
  scanning: '正在扫描的库数',
  sa_count: 'Google 子账号数',
  global_error_ratio: '错误率',
  global_error_ratio_raw: '错误率（原始值）',
  global_p99_ms: 'P99 延迟（毫秒）',
  total: '总数',
  dead: '已死亡',
}

function evidenceItemText(v: unknown): string {
  if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
    const o = v as Record<string, unknown>
    if (typeof o.name === 'string' && o.name) return o.name
    return JSON.stringify(v)
  }
  return String(v)
}

function evidenceValue(v: unknown): string {
  if (Array.isArray(v)) {
    if (!v.length) return '0 个'
    const names = v.slice(0, 3).map(evidenceItemText).join('、')
    return `${v.length} 个（${names}${v.length > 3 ? ' 等' : ''}）`
  }
  if (typeof v === 'boolean') return v ? '是' : '否'
  return String(v)
}

function evidenceText(f: DiagnosisFinding): string {
  const entries = Object.entries(f.evidence || {}).filter(([, v]) => v !== null && v !== undefined && v !== '')
  if (!entries.length) return ''
  return entries
    .slice(0, 4)
    .map(([k, v]) => `${EVIDENCE_KEY_LABEL[k] ?? k}=${evidenceValue(v)}`)
    .join(' · ')
}

async function loadRules() {
  try {
    const r = await fetchDiagnosisRules()
    const m: Record<string, string> = {}
    for (const item of r.rules || []) m[item.rule_id] = item.name
    ruleNames.value = m
    fixes.value = r.fixes || {}
  } catch {
    // 规则清单拿不到不影响体检，只是不显示"本次检查项"和修复按钮
  }
}

function fixErrorMessage(e: unknown): string {
  const d = (e as { response?: { data?: { detail?: { message?: string } } } })
    ?.response?.data?.detail
  if (d && typeof d.message === 'string' && d.message) return d.message
  return e instanceof Error ? e.message : '请求失败'
}

async function doFix(ruleId: string) {
  fixingRule.value = ruleId
  try {
    const r = await fixDiagnosisRule(ruleId)
    const detail = r.result?.detail
    await ElMessageBox.alert(
      typeof detail === 'string' && detail ? detail : '修复已执行。',
      `修复完成 · ${r.name}`,
      { confirmButtonText: '知道了' },
    )
  } catch (e) {
    await ElMessageBox.alert(fixErrorMessage(e), '修复未执行', {
      confirmButtonText: '知道了',
      type: 'warning',
    }).catch(() => {})
  } finally {
    fixingRule.value = ''
    // 修复后重跑一轮，看红灯灭了没有
    await run()
  }
}

function askFix(f: DiagnosisFinding) {
  const fix = fixes.value[f.rule_id]
  if (!fix) return
  ElMessageBox.confirm(fix.confirm_text, `一键修复 · ${fix.name}`, {
    confirmButtonText: '确认修复',
    cancelButtonText: '再想想',
    type: 'warning',
    dangerouslyUseHTMLString: false,
  })
    .then(() => doFix(f.rule_id))
    .catch(() => {
      // 用户点了"再想想"：静默取消，不打扰
    })
}

async function run() {
  // 并发守卫：体检中重复点击直接忽略，避免发重复请求
  if (loading.value) return
  loading.value = true
  loadError.value = ''
  try {
    report.value = await runDiagnosis()
    lastChecked.value = new Date().toLocaleTimeString()
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '请求失败'
    ElMessage.error('体检失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

onMounted(loadRules)
</script>

<template>
  <div class="au-diagnosis">
    <PageHeader
      eyebrow="系统与审计"
      title="一键体检"
      description="点一下，9 项系统检查自动跑完，给出大白话结论和检查步骤。只读诊断，不改任何配置、不重启任何服务。"
    >
      <template #actions>
        <el-button type="primary" :loading="loading" @click="run">
          <el-icon><Play :size="14" /></el-icon>
          开始体检
        </el-button>
      </template>
    </PageHeader>

    <el-skeleton v-if="loading && !report" :rows="6" animated />

    <EmptyState
      v-else-if="loadError && !report"
      title="体检失败"
      :description="loadError"
    />

    <!-- 还没跑过：引导 + 检查项清单 -->
    <EmptyState
      v-else-if="!report"
      title="还没体检过"
      description="点右上角「开始体检」，9 项检查一次跑完。部分检查项支持一键自动修复（执行前会二次确认，每次修复都写审计日志）。"
    >
      <template v-if="Object.keys(ruleNames).length" #extra>
        <div class="au-diagnosis__rules">
          <span class="au-diagnosis__rules-label">本次会检查：</span>
          <span v-for="(name, id) in ruleNames" :key="id" class="au-diagnosis__rule-chip">
            {{ name }}
          </span>
        </div>
      </template>
    </EmptyState>

    <template v-else>
      <!-- 汇总条 -->
      <div class="au-diagnosis__overall" :class="overallClass">
        <Stethoscope :size="18" class="au-diagnosis__overall-icon" />
        <span class="au-diagnosis__overall-text">体检结论：{{ overallText }}</span>
        <span class="au-diagnosis__overall-counts">
          共 {{ summary.total }} 项 · 正常 {{ summary.info }} · 注意 {{ summary.warn }} · 严重 {{ summary.critical }}
        </span>
        <span v-if="lastChecked" class="au-diagnosis__overall-time">体检时间 {{ lastChecked }}</span>
        <el-button size="small" :loading="loading" @click="run">
          <el-icon><RefreshCw :size="12" /></el-icon>
          重新体检
        </el-button>
      </div>

      <!-- 结论卡片：严重的排前面 -->
      <div class="au-diagnosis__list">
        <div
          v-for="f in sortedFindings"
          :key="f.rule_id"
          class="au-diagnosis__card"
          :class="LEVEL_ICON[f.level]"
        >
          <div class="au-diagnosis__card-head">
            <span class="au-diagnosis__dot" :class="LEVEL_ICON[f.level]" />
            <span class="au-diagnosis__card-name">{{ f.name }}</span>
            <span class="au-diagnosis__card-level" :class="LEVEL_ICON[f.level]">{{ f.level_cn }}</span>
          </div>
          <p class="au-diagnosis__card-title">{{ f.title }}</p>
          <p class="au-diagnosis__card-desc">{{ f.description }}</p>
          <ol v-if="f.steps && f.steps.length" class="au-diagnosis__steps">
            <li v-for="(s, i) in f.steps" :key="i">{{ s }}</li>
          </ol>
          <p v-if="evidenceText(f)" class="au-diagnosis__evidence">数据：{{ evidenceText(f) }}</p>
          <div v-if="fixes[f.rule_id]" class="au-diagnosis__fixbar">
            <span class="au-diagnosis__fixdesc">{{ fixes[f.rule_id].description }}</span>
            <el-button
              size="small"
              type="primary"
              :loading="fixingRule === f.rule_id"
              @click="askFix(f)"
            >
              <el-icon><Wrench :size="12" /></el-icon>
              {{ fixes[f.rule_id].name }}
            </el-button>
          </div>
        </div>
      </div>

      <p class="au-diagnosis__note">{{ report.note }}</p>
    </template>
  </div>
</template>

<style scoped>
.au-diagnosis {
  padding: 0;
}

.au-diagnosis__overall {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 20px;
  padding: 14px 18px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-left: 3px solid var(--au-success);
  border-radius: var(--au-r-lg);
}

.au-diagnosis__overall.is-warn { border-left-color: var(--au-warning); }
.au-diagnosis__overall.is-down { border-left-color: var(--au-danger); }

.au-diagnosis__overall-icon {
  color: color-mix(in srgb, var(--au-text) 70%, transparent);
}

.au-diagnosis__overall-text {
  font-size: 15px;
  font-weight: 600;
  color: var(--au-text);
}

.au-diagnosis__overall-counts {
  font-size: 13px;
  color: color-mix(in srgb, var(--au-text) 70%, transparent);
}

.au-diagnosis__overall-time {
  margin-left: auto;
  font-size: 12px;
  color: color-mix(in srgb, var(--au-text) 50%, transparent);
}

.au-diagnosis__list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 14px;
}

@media (max-width: 1024px) {
  .au-diagnosis__list {
    grid-template-columns: 1fr;
  }
}

.au-diagnosis__card {
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-left: 3px solid var(--au-success);
  border-radius: var(--au-r-lg);
  padding: 14px 16px;
}

.au-diagnosis__card.is-warn { border-left-color: var(--au-warning); }
.au-diagnosis__card.is-down { border-left-color: var(--au-danger); }

.au-diagnosis__card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.au-diagnosis__dot {
  width: 10px;
  height: 10px;
  flex: 0 0 10px;
  border-radius: 50%;
  background: var(--au-success);
}

.au-diagnosis__dot.is-warn { background: var(--au-warning); }
.au-diagnosis__dot.is-down { background: var(--au-danger); }

.au-diagnosis__card-name {
  font-size: 12px;
  color: color-mix(in srgb, var(--au-text) 50%, transparent);
}

.au-diagnosis__card-level {
  margin-left: auto;
  font-size: 12px;
  font-weight: 600;
  color: var(--au-success);
}

.au-diagnosis__card-level.is-warn { color: var(--au-warning); }
.au-diagnosis__card-level.is-down { color: var(--au-danger); }

.au-diagnosis__card-title {
  margin: 0 0 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--au-text);
}

.au-diagnosis__card-desc {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.6;
  color: color-mix(in srgb, var(--au-text) 70%, transparent);
}

.au-diagnosis__steps {
  margin: 0 0 8px;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.7;
  color: color-mix(in srgb, var(--au-text) 70%, transparent);
}

.au-diagnosis__evidence {
  margin: 0;
  font-size: 12px;
  color: color-mix(in srgb, var(--au-text) 50%, transparent);
}

.au-diagnosis__fixbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--au-border);
}

.au-diagnosis__fixdesc {
  flex: 1;
  font-size: 12px;
  line-height: 1.6;
  color: color-mix(in srgb, var(--au-text) 60%, transparent);
}

.au-diagnosis__note {
  margin: 20px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: color-mix(in srgb, var(--au-text) 50%, transparent);
}

.au-diagnosis__rules {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}

.au-diagnosis__rules-label {
  font-size: 13px;
  color: color-mix(in srgb, var(--au-text) 70%, transparent);
}

.au-diagnosis__rule-chip {
  padding: 4px 10px;
  font-size: 12px;
  color: color-mix(in srgb, var(--au-text) 70%, transparent);
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: 999px;
}
</style>
