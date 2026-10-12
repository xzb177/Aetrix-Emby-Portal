<script setup lang="ts">
/**
 * 一键体检（只读诊断，v2.57.0）
 *
 * - 数据源：GET /api/admin/diagnosis/run（后端只读规则引擎，不写库不改配置）
 * - 9 条规则逐项给结论：正常也说一句，报告是一份完整清单
 * - 品牌约束：只用 --au-* 变量，零新增色值；暗房影院深色主题
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Play, RefreshCw, Stethoscope } from 'lucide-vue-next'
import { EmptyState, PageHeader } from '@/components/ui'
import {
  fetchDiagnosisRules,
  runDiagnosis,
  type DiagnosisFinding,
  type DiagnosisReport,
} from '@/api/admin'

const report = ref<DiagnosisReport | null>(null)
const ruleNames = ref<Record<string, string>>({})
const loading = ref(false)
const loadError = ref('')
const lastChecked = ref('')

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

function evidenceText(f: DiagnosisFinding): string {
  const entries = Object.entries(f.evidence || {}).filter(([, v]) => v !== null && v !== undefined && v !== '')
  if (!entries.length) return ''
  return entries
    .slice(0, 4)
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.length : v}`)
    .join(' · ')
}

async function loadRules() {
  try {
    const r = await fetchDiagnosisRules()
    const m: Record<string, string> = {}
    for (const item of r.rules || []) m[item.rule_id] = item.name
    ruleNames.value = m
  } catch {
    // 规则清单拿不到不影响体检，只是不显示"本次检查项"
  }
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
      description="点右上角「开始体检」，9 项检查一次跑完。自动修复是下一阶段的功能，这次只诊断、不动手。"
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
