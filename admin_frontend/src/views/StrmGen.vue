<script setup lang="ts">
/**
 * .strm 生成器
 *
 * 把 Google Drive 视频批量生成 .strm 文件的能力收进管理后台：
 * - 生成状态与进度（轮询）
 * - 手动触发增量 / 全量生成
 * - 配置：总开关 / Drive 源目录 / 每天执行时刻 / 指定 Drive / 过期清理
 * - 缺集报告：上次生成的完整性校验结果
 *
 * 版式与其它页一致：PageHeader + StatTile + SectionCard，按钮统一 el-button，
 * 颜色只用 --au-* 令牌。数据做防御校验（后端返回格式异常时不白屏）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Clapperboard, Film, Play, RefreshCw, RotateCcw, Trash2, TriangleAlert } from 'lucide-vue-next'
import { PageHeader, SectionCard, StatTile, EmptyState } from '@/components/ui'
import {
  fetchStrmGenConfig,
  saveStrmGenConfig,
  triggerStrmGen,
  fetchStrmGenProgress,
  fetchStrmGenDrives,
  fetchStrmGenMissing,
  type StrmGenConfig,
  type StrmGenProgress,
  type StrmGenDrive,
  type StrmGenIncomplete,
} from '@/api/admin'

const loading = ref(false)
const loadError = ref('')
const config = ref<StrmGenConfig | null>(null)
const progress = ref<StrmGenProgress | null>(null)
const drives = ref<StrmGenDrive[]>([])
const missing = ref<{
  hasData: boolean
  total: number
  complete: number
  incomplete: number
  list: StrmGenIncomplete[]
}>({ hasData: false, total: 0, complete: 0, incomplete: 0, list: [] })

const triggering = ref(false)
const saving = ref(false)

// 配置表单（与 config 分开，避免未保存就污染展示）
const form = ref({ enabled: true, source_dir: '', schedule: '', drive_id: '', prune: false, interval_minutes: 60, source_type: 'drive', local_root: '/media' })

let pollTimer: ReturnType<typeof setInterval> | null = null

/** 进度百分比 */
const progressPct = computed(() => {
  const p = progress.value
  if (!p || !p.total || p.total <= 0) return 0
  return Math.min(100, Math.round((p.done / p.total) * 100))
})

/** 阶段文案 */
const phaseLabel = computed(() => {
  const m: Record<string, string> = {
    idle: '空闲',
    listing: '列举 Drive 文件…',
    generating: '生成 .strm…',
    verifying: '完整性校验…',
    done: '已完成',
    error: '出错',
  }
  return m[progress.value?.phase || ''] || progress.value?.phase || '未知'
})

/** 状态徽标类型 */
const statusTone = computed(() => {
  if (!progress.value) return 'info'
  if (progress.value.running) return 'warn'
  if (progress.value.phase === 'error') return 'danger'
  if (progress.value.phase === 'done') return 'ok'
  return 'info'
})

/** 错误列表（防御：非数组时置空） */
const errorList = computed<string[]>(() => {
  const errs = progress.value?.errors
  return Array.isArray(errs) ? errs.filter((e) => typeof e === 'string') : []
})

/** 缺集列表（防御） */
const missingList = computed<StrmGenIncomplete[]>(() => missing.value.list)

async function load(silent = false) {
  if (!silent) {
    loading.value = true
    loadError.value = ''
  }
  try {
    const [cfgRes, progRes, drivesRes, missRes] = await Promise.all([
      fetchStrmGenConfig(),
      fetchStrmGenProgress(),
      fetchStrmGenDrives().catch(() => ({ success: false as const, drives: [] })),
      fetchStrmGenMissing(100).catch(() => ({ ok: false as const })),
    ])
    config.value = cfgRes.config
    progress.value = progRes.progress
    // progress 接口也带回 config，以它为准（更新 last_run）
    if (progRes.config) config.value = progRes.config
    form.value = {
      enabled: !!config.value?.enabled,
      source_dir: config.value?.source_dir ?? '',
      schedule: config.value?.schedule ?? '',
      drive_id: config.value?.drive_id ?? '',
      prune: !!config.value?.prune,
      interval_minutes: config.value?.interval_minutes ?? 60,
      source_type: config.value?.source_type ?? 'drive',
      local_root: config.value?.local_root ?? '/media',
    }
    const ds = (drivesRes as { success: boolean; drives?: StrmGenDrive[] }).drives
    drives.value = Array.isArray(ds) ? ds : []
    if ((missRes as { ok: boolean }).ok) {
      const m = missRes as { has_data?: boolean; total_series?: number; complete_series?: number; incomplete_series?: number; incomplete?: StrmGenIncomplete[] }
      const list = Array.isArray(m.incomplete) ? m.incomplete : []
      missing.value = {
        hasData: !!m.has_data,
        total: m.total_series ?? 0,
        complete: m.complete_series ?? 0,
        incomplete: m.incomplete_series ?? 0,
        list,
      }
    }
  } catch (e) {
    if (!silent) loadError.value = e instanceof Error ? e.message : '请求失败'
  } finally {
    if (!silent) loading.value = false
  }
}

/** 运行时轮询进度 */
function ensurePolling() {
  const running = !!progress.value?.running
  if (running && !pollTimer) {
    pollTimer = setInterval(() => load(true), 3000)
  } else if (!running && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function onTrigger(full: boolean) {
  if (full) {
    try {
      await ElMessageBox.confirm(
        '全量重建会重新生成所有 .strm 文件（8 万+ 文件，耗时很长）。确定吗？',
        '全量重建',
        { confirmButtonText: '开始全量', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }
  triggering.value = true
  try {
    const res = await triggerStrmGen(full)
    if (res.success) {
      ElMessage.success(res.message || '已开始生成')
      await load(true)
      ensurePolling()
    } else {
      ElMessage.warning(res.error || '已有任务在运行')
      await load(true)
      ensurePolling()
    }
  } catch {
    // 拦截器已提示
  } finally {
    triggering.value = false
  }
}

async function onSave() {
  saving.value = true
  try {
    const res = await saveStrmGenConfig({ ...form.value })
    config.value = res.config
    ElMessage.success('已保存，立即生效')
  } catch {
    // 400 时拦截器会弹出后端 detail（如时刻格式不对）
  } finally {
    saving.value = false
  }
}

function driveLabel(d: StrmGenDrive): string {
  const parts = [d.drive_id]
  if (d.is_personal) parts.push('个人盘')
  if (Array.isArray(d.remotes) && d.remotes.length) parts.push(`(${d.remotes.join(', ')})`)
  return parts.join(' ')
}

onMounted(async () => {
  await load()
  ensurePolling()
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="媒体与交付"
      title=".strm 生成器"
      description="把 Google Drive 视频批量生成 .strm 文件：定时自动跑，也可手动触发。生成后追新会自动发现新增 .strm 并入库。"
    >
      <template #actions>
        <el-button :loading="loading" :icon="RefreshCw" @click="load()">刷新</el-button>
        <el-button
          type="primary"
          :icon="Play"
          :loading="triggering"
          :disabled="!!progress?.running"
          @click="onTrigger(false)"
        >
          触发增量生成
        </el-button>
        <el-button
          :icon="RotateCcw"
          :loading="triggering"
          :disabled="!!progress?.running"
          @click="onTrigger(true)"
        >
          全量重建
        </el-button>
      </template>
    </PageHeader>

    <div v-if="loadError" class="page-alert error">
      <TriangleAlert :size="16" />
      <span>{{ loadError }}</span>
    </div>

    <!-- 状态 -->
    <SectionCard title="生成状态" :icon="Clapperboard">
      <div class="stat-row">
        <StatTile label="状态" :value="phaseLabel" :tone="statusTone" />
        <StatTile label="总文件" :value="progress?.total ?? 0" />
        <StatTile label="已生成" :value="progress?.generated ?? 0" tone="ok" />
        <StatTile label="跳过" :value="progress?.skipped ?? 0" />
        <StatTile label="失败" :value="progress?.failed ?? 0" :tone="(progress?.failed ?? 0) > 0 ? 'danger' : 'plain'" />
      </div>
      <div v-if="progress?.running && progress.total > 0" class="progress-wrap">
        <el-progress :percentage="progressPct" :stroke-width="10" striped striped-flow />
        <div class="progress-meta">
          <span>{{ progress.done }} / {{ progress.total }}</span>
          <span v-if="progress.current" class="current-file">{{ progress.current }}</span>
        </div>
      </div>
      <div v-if="!progress?.running && progress?.finished_at" class="muted">
        上次完成：{{ progress.finished_at }}
      </div>
      <div v-if="errorList.length" class="error-list">
        <div class="error-list-title">最近错误（{{ errorList.length }}）</div>
        <ul>
          <li v-for="(e, i) in errorList" :key="i">{{ e }}</li>
        </ul>
      </div>
      <EmptyState v-if="!progress" title="暂无状态" description="尚未运行过生成任务。" />
    </SectionCard>

    <!-- 配置 -->
    <SectionCard title="配置" :icon="Film" description="保存即热生效，无需重启。">
      <el-form label-width="140px" class="cfg-form" @submit.prevent>
        <el-form-item label="总开关">
          <el-switch v-model="form.enabled" />
          <span class="hint">关闭后定时任务不再执行</span>
        </el-form-item>
        <el-form-item label="Drive 源目录">
          <el-input v-model="form.source_dir" placeholder="如 MoviePilot/" clearable style="max-width: 320px" />
          <span class="hint">相对网盘根的目录</span>
        </el-form-item>
        <el-form-item label="每天执行时刻">
          <el-input v-model="form.schedule" placeholder="03:00" clearable style="max-width: 160px" />
          <span class="hint">HH:MM（服务器本地时间），留空关闭定时</span>
        </el-form-item>
        <el-form-item label="增量间隔">
          <el-input-number v-model="form.interval_minutes" :min="0" :max="1440" :step="5" style="max-width: 160px" />
          <span class="hint">分钟，0=关闭间隔只跑每天定时。实际间隔加 ±20% 随机抖动防风控</span>
        </el-form-item>
        <el-form-item label="数据源类型">
          <el-select v-model="form.source_type" style="max-width: 200px">
            <el-option label="Google Drive" value="drive" />
            <el-option label="本地目录" value="local" />
          </el-select>
          <span class="hint">增量能力通用：Drive 用 Changes API，本地用文件指纹</span>
        </el-form-item>
        <el-form-item v-if="form.source_type === 'local'" label="本地源目录">
          <el-input v-model="form.local_root" placeholder="/media" clearable style="max-width: 320px" />
          <span class="hint">本地视频文件的根目录</span>
        </el-form-item>
        <el-form-item label="指定 Drive">
          <el-select v-model="form.drive_id" placeholder="自动选择" clearable style="max-width: 320px">
            <el-option label="自动选择（默认）" value="" />
            <el-option
              v-for="d in drives"
              :key="d.drive_id"
              :label="driveLabel(d)"
              :value="d.drive_id"
            />
          </el-select>
          <span class="hint">多共享盘时可指定只扫某一个</span>
        </el-form-item>
        <el-form-item label="过期清理">
          <el-switch v-model="form.prune" />
          <span class="hint danger-hint">删除 Drive 上已不存在的 .strm 文件及状态行，不可逆，默认只上报不删</span>
        </el-form-item>
        <el-form-item label="上次执行">
          <span class="muted">{{ config?.last_run || '—' }}</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="onSave">保存配置</el-button>
        </el-form-item>
      </el-form>
    </SectionCard>

    <!-- 缺集报告 -->
    <SectionCard title="缺集报告" :icon="Trash2" description="上次生成的完整性校验结果：Drive 上有但 .strm 缺失的剧集。">
      <div v-if="missing.hasData">
        <div class="stat-row">
          <StatTile label="剧集总数" :value="missing.total" />
          <StatTile label="完整" :value="missing.complete" tone="ok" />
          <StatTile label="缺集" :value="missing.incomplete" :tone="missing.incomplete > 0 ? 'danger' : 'ok'" />
        </div>
        <el-table v-if="missingList.length" :data="missingList" stripe style="width: 100%">
          <el-table-column prop="series" label="剧集" min-width="200" />
          <el-table-column prop="drive_count" label="Drive 文件数" width="120" align="right" />
          <el-table-column prop="strm_count" label=".strm 数" width="100" align="right" />
          <el-table-column prop="missing_count" label="缺失" width="80" align="right" />
          <el-table-column label="缺失示例" min-width="240">
            <template #default="{ row }">
              <span class="muted small">{{ (row.missing_sample || []).slice(0, 3).join('；') }}</span>
            </template>
          </el-table-column>
        </el-table>
        <EmptyState v-else title="全部完整" description="上次生成没有发现缺集。" />
      </div>
      <EmptyState v-else title="暂无报告" description="还没有生成过，或上次生成没有留下完整性统计。" />
    </SectionCard>
  </div>
</template>

<style scoped>
.page-alert {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-radius: 8px;
  margin-bottom: 16px;
  font-size: 13px;
}
.page-alert.error {
  background: color-mix(in srgb, var(--au-danger) 10%, transparent);
  color: var(--au-danger);
  border: 1px solid color-mix(in srgb, var(--au-danger) 30%, transparent);
}
.stat-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}
.progress-wrap {
  margin: 8px 0 12px;
}
.progress-meta {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--au-text-2);
}
.current-file {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 60%;
}
.muted {
  color: var(--au-text-2);
  font-size: 13px;
}
.small {
  font-size: 12px;
}
.hint {
  margin-left: 10px;
  font-size: 12px;
  color: var(--au-text-2);
}
.danger-hint {
  color: var(--au-danger);
}
.cfg-form {
  max-width: 720px;
}
.error-list {
  margin-top: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--au-danger) 6%, transparent);
  border: 1px solid color-mix(in srgb, var(--au-danger) 20%, transparent);
}
.error-list-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--au-danger);
  margin-bottom: 6px;
}
.error-list ul {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--au-text);
}
.error-list li {
  margin-bottom: 4px;
  word-break: break-all;
}
</style>
