<script setup lang="ts">
/**
 * .strm 生成器
 *
 * 把 Google Drive 视频批量生成 .strm 文件的能力收进管理后台：
 * - 生成状态与进度（轮询）
 * - 手动触发增量 / 全量生成
 * - 配置：总开关 / Drive 源目录 / 增量间隔 / 指定 Drive / 过期清理
 * - 缺集报告：上次生成的完整性校验结果
 *
 * 版式与其它页一致：PageHeader + StatTile + SectionCard，按钮统一 el-button，
 * 颜色只用 --au-* 令牌。数据做防御校验（后端返回格式异常时不白屏）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Clapperboard, Film, Play, RefreshCw, RotateCcw, Trash2, TriangleAlert } from 'lucide-vue-next'
import { AdvancedSettings, PageHeader, SectionCard, StatTile, EmptyState } from '@/components/ui'
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
  verifyMode: string
  total: number
  complete: number
  incomplete: number
  list: StrmGenIncomplete[]
}>({ hasData: false, verifyMode: 'full', total: 0, complete: 0, incomplete: 0, list: [] })

const triggeringIncremental = ref(false)
const triggeringFull = ref(false)
const saving = ref(false)

// 配置表单（与 config 分开，避免未保存就污染展示）
// 配置瘦身第 3c 批：调优项全部给入口（值走 _read_config 回显，默认与后端一致）
const form = ref({ enabled: true, source_dir: '', drive_id: '', prune: false, dedupe_same_size: true, interval_minutes: 60, lock_timeout_minutes: 180, source_type: 'drive', local_root: '/media', jitter_ratio: 0.2, subtree_listing: true, drive_concurrency: 3, api_qps: 8.0, prune_dry_run: true, prune_max_ratio: 0.2, prune_max_absolute: 50, verify_new_ids: true, orphan_check_enabled: true, orphan_check_interval_days: 7, orphan_check_batch: 2000, orphan_check_qps: 2.0 })

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
      // 用 source_dir_raw 回显：多目录配置不被截断（回退 source_dir 兼容旧后端）
      source_dir: config.value?.source_dir_raw ?? config.value?.source_dir ?? '',
      drive_id: config.value?.drive_id ?? '',
      prune: !!config.value?.prune,
      // 后端无此键（老版本）时回显为开，与默认行为一致
      dedupe_same_size: config.value?.dedupe_same_size ?? true,
      interval_minutes: config.value?.interval_minutes ?? 60,
      lock_timeout_minutes: config.value?.lock_timeout_minutes ?? 180,
      source_type: config.value?.source_type ?? 'drive',
      local_root: config.value?.local_root ?? '/media',
      // 后端无此键（老版本）时回显为默认行为
      jitter_ratio: config.value?.jitter_ratio ?? 0.2,
      subtree_listing: config.value?.subtree_listing ?? true,
      // 配置瘦身第 3c 批：调优项回显（老后端无此键时回显后端默认值）
      drive_concurrency: config.value?.drive_concurrency ?? 3,
      api_qps: config.value?.api_qps ?? 8.0,
      prune_dry_run: config.value?.prune_dry_run ?? true,
      prune_max_ratio: config.value?.prune_max_ratio ?? 0.2,
      prune_max_absolute: config.value?.prune_max_absolute ?? 50,
      verify_new_ids: config.value?.verify_new_ids ?? true,
      orphan_check_enabled: config.value?.orphan_check_enabled ?? true,
      orphan_check_interval_days: config.value?.orphan_check_interval_days ?? 7,
      orphan_check_batch: config.value?.orphan_check_batch ?? 2000,
      orphan_check_qps: config.value?.orphan_check_qps ?? 2.0,
    }
    const ds = (drivesRes as { success: boolean; drives?: StrmGenDrive[] }).drives
    drives.value = Array.isArray(ds) ? ds : []
    if ((missRes as { ok: boolean }).ok) {
      const m = missRes as { has_data?: boolean; verify_mode?: string; total_series?: number; complete_series?: number; incomplete_series?: number; incomplete?: StrmGenIncomplete[] }
      const list = Array.isArray(m.incomplete) ? m.incomplete : []
      missing.value = {
        hasData: !!m.has_data,
        verifyMode: m.verify_mode ?? 'full',
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
        { confirmButtonText: '开始全量重建', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }
  // 两个按钮各用独立的 loading，避免"点一个、两个一起转"的误导
  const triggering = full ? triggeringFull : triggeringIncremental
  triggering.value = true
  try {
    const res = await triggerStrmGen(full)
    if (res.success) {
      ElMessage.success(res.message || (full ? '已开始全量重建，进度会在本页实时更新' : '已开始增量生成，进度会在本页实时更新'))
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
    ElMessage.success('配置已保存，立即生效')
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
      description="把 Google Drive 视频批量生成 .strm 文件：定时自动跑，也可手动触发。生成后，「自动发现新增文件」会把新增的 .strm 扫进媒体库。扫描认不认 .strm 的开关在「媒体库」页（定时与刮削标签），别和这里搞混。"
    >
      <template #actions>
        <el-button :loading="loading" :icon="RefreshCw" @click="load()">刷新</el-button>
        <el-button
          type="primary"
          :icon="Play"
          :loading="triggeringIncremental"
          :disabled="!!progress?.running"
          @click="onTrigger(false)"
        >
          触发增量生成
        </el-button>
        <el-button
          :icon="RotateCcw"
          :loading="triggeringFull"
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
          <el-input v-model="form.source_dir" placeholder="如 MoviePilot/ 或 MoviePilot/,nastool/" clearable style="max-width: 320px" />
          <span class="hint">相对网盘根的目录，逗号分隔可配多个</span>
        </el-form-item>
        <el-form-item label="增量间隔">
          <el-input-number v-model="form.interval_minutes" :min="0" :max="1440" :step="5" style="max-width: 160px" />
          <span class="hint">分钟，0=关闭。实际间隔加 ±20% 随机抖动防风控</span>
        </el-form-item>
        <el-form-item label="运行锁超时">
          <el-input-number v-model="form.lock_timeout_minutes" :min="0" :max="1440" :step="30" style="max-width: 160px" />
          <span class="hint">分钟。锁被持有超此时长视为僵死可抢占，0=关闭自愈</span>
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
            <el-option label="全部发现的盘" value="all" />
            <el-option
              v-for="d in drives"
              :key="d.drive_id"
              :label="driveLabel(d)"
              :value="d.drive_id"
            />
          </el-select>
          <span class="hint">多共享盘时可指定只扫某一个；选"全部发现的盘"则含个人盘</span>
        </el-form-item>
        <el-form-item label="过期清理">
          <el-switch v-model="form.prune" />
          <span class="hint danger-hint">删除 Drive 上已不存在的 .strm 文件及状态行，不可逆，默认只上报不删</span>
        </el-form-item>
        <el-form-item label="同名同大小去重">
          <el-switch v-model="form.dedupe_same_size" />
          <span class="hint">开启后，输出文件名相同且文件大小也相同的只生成一份；大小不同的是不同版本，照常保留。关闭则撞名全部改 -2/-3 后缀保留</span>
        </el-form-item>
        <el-form-item label="上次执行">
          <span class="muted">{{ config?.last_run || '—' }}</span>
        </el-form-item>
        <!-- 配置瘦身第 3 批：低频调优项收进高级折叠（默认收起） -->
        <AdvancedSettings
          count="12 项"
          description="并发 / 限速 / 删除保护 / 反向校验的微调：一般不用动，默认即可。"
        >
          <el-form-item label="调度抖动比例">
            <el-input-number v-model="form.jitter_ratio" :min="0" :max="0.5" :step="0.05" style="max-width: 160px" />
            <span class="hint">增量等待按 ± 该比例随机化，防机械节拍（范围 0–0.5，默认 0.2；0=关闭）</span>
          </el-form-item>
          <el-form-item label="子树限定列举">
            <el-switch v-model="form.subtree_listing" />
            <span class="hint">开 = 全量回退时只拉源目录子树的条目；关 = 回退旧扁平全量拉取（应急回滚口）</span>
          </el-form-item>
          <!-- 配置瘦身第 3c 批：并发 / 限速 / 删除保护 / 反向校验调优项补入口 -->
          <el-form-item label="多盘并行列举并发数">
            <el-input-number v-model="form.drive_concurrency" :min="1" :max="10" :step="1" style="max-width: 160px" />
            <span class="hint">ThreadPoolExecutor 并发数，范围 1–10，默认 3（保守防风控）</span>
          </el-form-item>
          <el-form-item label="Drive API 速率上限">
            <el-input-number v-model="form.api_qps" :min="1.0" :max="20.0" :step="0.5" style="max-width: 160px" />
            <span class="hint">QPS，范围 1.0–20.0，默认 8.0（Drive per-user 配额约 10）</span>
          </el-form-item>
          <el-form-item label="删除保护演习">
            <el-switch v-model="form.prune_dry_run" />
            <span class="hint">开 = 只记 would-delete 不真删；确认清单无误后关闭才真正 armed</span>
          </el-form-item>
          <el-form-item label="删除比例上限">
            <el-input-number v-model="form.prune_max_ratio" :min="0.01" :max="1" :step="0.05" style="max-width: 160px" />
            <span class="hint">单轮删除比例上限，范围 (0, 1]，默认 0.2</span>
          </el-form-item>
          <el-form-item label="删除绝对上限">
            <el-input-number v-model="form.prune_max_absolute" :min="1" :step="10" style="max-width: 160px" />
            <span class="hint">单轮删除绝对上限，≥1，默认 50</span>
          </el-form-item>
          <el-form-item label="写盘前验 file_id">
            <el-switch v-model="form.verify_new_ids" />
            <span class="hint">增量中新文件写盘前校验一次 file_id（默认开）</span>
          </el-form-item>
          <el-form-item label="反向僵尸校验">
            <el-switch v-model="form.orphan_check_enabled" />
            <span class="hint">反向僵尸校验任务开关（默认开）</span>
          </el-form-item>
          <el-form-item label="僵尸校验周期">
            <el-input-number v-model="form.orphan_check_interval_days" :min="1" :step="1" style="max-width: 160px" />
            <span class="hint">反向僵尸校验周期（天），≥1，默认 7</span>
          </el-form-item>
          <el-form-item label="僵尸校验批量">
            <el-input-number v-model="form.orphan_check_batch" :min="100" :max="10000" :step="100" style="max-width: 160px" />
            <span class="hint">每轮最多校验数，范围 100–10000，默认 2000</span>
          </el-form-item>
          <el-form-item label="僵尸校验 QPS">
            <el-input-number v-model="form.orphan_check_qps" :min="0.5" :max="10" :step="0.5" style="max-width: 160px" />
            <span class="hint">反向校验 per-SA QPS 上限，范围 0.5–10，默认 2.0</span>
          </el-form-item>
        </AdvancedSettings>
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
        <div v-if="missing.verifyMode === 'light'" class="muted small" style="margin: 4px 0 8px;">
          增量轮轻量校验：仅覆盖本轮有变动的目录（全量轮才做全库校验）。
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
  border-radius: var(--radius-sm, 8px);
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
  border-radius: var(--radius-sm, 8px);
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
