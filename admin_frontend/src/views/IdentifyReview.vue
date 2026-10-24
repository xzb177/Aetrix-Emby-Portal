<script setup lang="ts">
/**
 * 识别审查 —— 低置信识别的人工处置队列（批二 #15 的管理端页面）
 *
 * 自动刮削在 TMDB 真搜过、有候选但无一过置信线时，条目会连同当时的候选
 * 证据排进审查队列（后端 emby_identify_review_tasks，入队开关
 * IDENTIFY_REVIEW_QUEUE_ENABLED）。这里逐条看证据做处置：
 * - 绑定：点候选的「绑定这个」（或手动填 TMDB / IMDb ID），后端复用手动
 *   绑定的唯一实现 bind_tmdb_id，绑定成功审查行落 bound；
 * - 忽略：条目保持现状，审查行落 ignored 留痕；
 * - 重跑：条目重新排入补全队列，审查行落 requeued。
 * 重复处置后端返回 409，错误文案原样提示（toAdminError 透传中文 detail），
 * 不静默吞；处置成功后列表即时刷新，pending 视图里该行随之消失。
 */
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import { ListChecks, RefreshCw } from 'lucide-vue-next'
import {
  bindIdentifyReviewTask,
  fetchIdentifyReviewTask,
  fetchIdentifyReviewTasks,
  ignoreIdentifyReviewTask,
  rerunIdentifyReviewTask,
} from '@/api/admin'
import type {
  IdentifyReviewCandidate,
  IdentifyReviewTask,
} from '@/api/admin'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import './IdentifyReview.css'

const PAGE_SIZE = 20

// ==================== 队列列表 ====================

const STATUS_OPTIONS = [
  { value: 'pending', label: '待处置' },
  { value: 'bound', label: '已绑定' },
  { value: 'ignored', label: '已忽略' },
  { value: 'requeued', label: '已重跑' },
  { value: '', label: '全部状态' },
]

const statusFilter = ref('pending')
const page = ref(1)
const tasks = ref<IdentifyReviewTask[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref(false)

async function loadTasks() {
  loading.value = true
  try {
    const res = await fetchIdentifyReviewTasks(
      statusFilter.value || undefined, page.value, PAGE_SIZE)
    tasks.value = res.items
    total.value = res.total
    loadError.value = false
    // 处置完最后一页的最后一条时回退一页，别停在空页上
    if (!res.items.length && res.total > 0 && page.value > 1) {
      page.value -= 1
      return loadTasks()
    }
  } catch (e: any) {
    loadError.value = true
    ElMessage.error(toAdminError(e, '读取审查队列失败'))
  } finally {
    loading.value = false
  }
}

function onStatusChange() {
  page.value = 1
  loadTasks().catch(() => undefined)
}

function onPageChange(p: number) {
  page.value = p
  loadTasks().catch(() => undefined)
}

// ==================== 展示口径 ====================

function statusLabel(status: string) {
  return STATUS_OPTIONS.find((o) => o.value === status)?.label || status
}

function statusBadgeClass(status: string) {
  if (status === 'bound') return 'ok'
  if (status === 'pending') return 'warn'
  return 'info'
}

function reasonLabel(reason: string) {
  // 后端当前只有这一种入队原因（识别枚举与 DB 约束同源，新原因出现时在此补文案）
  if (reason === 'no_confident_match') return '无高置信命中'
  return reason
}

function typeLabel(itemType: string | null) {
  if (itemType === 'series') return '剧集'
  if (itemType === 'movie') return '电影'
  return itemType || ''
}

function fmtTime(iso: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false })
}

/** 候选的分数文案：置信档 + 档内排序分，有哪个报哪个 */
function scoreText(c: IdentifyReviewCandidate) {
  const parts: string[] = []
  if (c.tier !== null && c.tier !== undefined) parts.push(`置信档 ${c.tier}`)
  if (c.rank !== null && c.rank !== undefined) parts.push(`排序分 ${c.rank}`)
  return parts.length ? parts.join(' · ') : '无分数'
}

// ==================== 详情抽屉 ====================

const drawerVisible = ref(false)
const detail = ref<IdentifyReviewTask | null>(null)
const detailLoading = ref(false)
const manualTmdbId = ref('')

async function openDetail(task: IdentifyReviewTask) {
  drawerVisible.value = true
  detail.value = null
  manualTmdbId.value = ''
  detailLoading.value = true
  try {
    detail.value = await fetchIdentifyReviewTask(task.id)
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '读取审查详情失败'))
    drawerVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

// ==================== 处置动作 ====================

/** 正在处置的任务 id（行按钮与抽屉按钮共用一个 loading 口径） */
const actingId = ref<number | null>(null)
/** 正在绑定的候选（tmdb_id 字符串，含手填的那次） */
const bindingKey = ref<string | null>(null)

async function doBind(task: IdentifyReviewTask, tmdbId: string, candidateTitle?: string) {
  const tid = tmdbId.trim()
  if (!tid) { ElMessage.warning('请填写 TMDB ID（或 IMDb ID）'); return }
  const name = task.item_name || `条目 ${task.item_id}`
  try {
    await ElMessageBox.confirm(
      candidateTitle
        ? `把「${name}」绑定到 TMDB「${candidateTitle}」吗？绑定后自动补全缺失的图 / 简介 / IMDb / 别名。`
        : `把「${name}」绑定到 TMDB ID ${tid} 吗？绑定后自动补全缺失的图 / 简介 / IMDb / 别名。`,
      '确认绑定',
      { type: 'warning' },
    )
  } catch { return }
  actingId.value = task.id
  bindingKey.value = tid
  try {
    const res = await bindIdentifyReviewTask(task.id, tid)
    const notes = res.bind?.notes?.length ? `：${res.bind.notes.join('；')}` : ''
    ElMessage.success(`「${name}」已绑定${notes}`)
    drawerVisible.value = false
    await loadTasks()
  } catch (e: any) {
    // 409（已处置）等后端文案原样提示，并刷新列表与后端对齐
    ElMessage.error(toAdminError(e, '绑定失败'))
    await loadTasks()
    if (drawerVisible.value && detail.value?.id === task.id) {
      detail.value = await fetchIdentifyReviewTask(task.id).catch(() => detail.value)
    }
  } finally {
    actingId.value = null
    bindingKey.value = null
  }
}

async function doIgnore(task: IdentifyReviewTask) {
  const name = task.item_name || `条目 ${task.item_id}`
  try {
    await ElMessageBox.confirm(
      `忽略「${name}」这条审查记录吗？条目保持现状（仍未识别），这条记录会标记为已忽略留痕。`,
      '确认忽略',
      { type: 'warning' },
    )
  } catch { return }
  actingId.value = task.id
  try {
    await ignoreIdentifyReviewTask(task.id)
    ElMessage.success(`「${name}」已忽略`)
    drawerVisible.value = false
    await loadTasks()
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '忽略失败'))
    await loadTasks()
  } finally {
    actingId.value = null
  }
}

async function doRerun(task: IdentifyReviewTask) {
  const name = task.item_name || `条目 ${task.item_id}`
  try {
    await ElMessageBox.confirm(
      `把「${name}」送回重新识别吗？条目会重新排入补全队列，自动刮削再试一遍。`,
      '确认重跑',
      { type: 'warning' },
    )
  } catch { return }
  actingId.value = task.id
  try {
    await rerunIdentifyReviewTask(task.id)
    ElMessage.success(`「${name}」已重新排入补全队列`)
    drawerVisible.value = false
    await loadTasks()
  } catch (e: any) {
    ElMessage.error(toAdminError(e, '重跑失败'))
    await loadTasks()
  } finally {
    actingId.value = null
  }
}

onMounted(() => {
  loadTasks().catch(() => undefined)
})
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="媒体与交付"
      title="识别审查"
      description="自动刮削认不准的条目会带着当时搜到的候选排进这里：TMDB 搜到了一些候选，但没有一个够格自动绑定。逐条看证据，绑对的、忽略，或者送回去重新识别。"
    >
      <template #actions>
        <RouterLink to="/metadata-sources" class="au-btn au-btn-ghost au-btn-sm">去元数据来源</RouterLink>
        <el-button size="small" :loading="loading" @click="loadTasks">
          <RefreshCw :size="14" style="margin-right: 4px" />刷新
        </el-button>
      </template>
    </PageHeader>

    <SectionCard class="ir-card" title="审查队列" :icon="ListChecks">
      <div class="ir-actions">
        <el-select v-model="statusFilter" style="width: 150px" @change="onStatusChange">
          <el-option
            v-for="o in STATUS_OPTIONS"
            :key="o.value"
            :label="o.label"
            :value="o.value"
          />
        </el-select>
        <el-button size="small" :loading="loading" @click="loadTasks">
          刷新
        </el-button>
        <span class="ir-hint">共 {{ total }} 条</span>
      </div>

      <div v-if="loading && !tasks.length && !loadError" class="ir-skeleton" aria-busy="true">
        <div class="au-skeleton" /><div class="au-skeleton" /><div class="au-skeleton" />
      </div>

      <EmptyState
        v-else-if="loadError"
        compact
        :icon="ListChecks"
        title="读取审查队列失败"
        description="可能是网络或后端暂时不可用。"
      >
        <template #actions>
          <el-button size="small" :loading="loading" @click="loadTasks">重试</el-button>
        </template>
      </EmptyState>

      <div v-else-if="tasks.length" class="ir-results">
        <div v-for="task in tasks" :key="task.id" class="ir-result">
          <span class="ir-result-name">{{ task.item_name || '（无名快照）' }}</span>
          <span v-if="task.item_year" class="ir-hint">（{{ task.item_year }}）</span>
          <span v-if="typeLabel(task.item_type)" class="ir-hint">{{ typeLabel(task.item_type) }}</span>
          <span class="mini-badge warn">{{ reasonLabel(task.reason) }}</span>
          <span class="mini-badge" :class="statusBadgeClass(task.status)">
            {{ statusLabel(task.status) }}
          </span>
          <span v-if="!task.item_exists" class="mini-badge danger">条目已删除</span>
          <span class="ir-hint">入队 {{ fmtTime(task.created_at) }}</span>
          <span v-if="task.status === 'bound' && task.resolved_tmdb_id" class="ir-hint">
            已绑 TMDB {{ task.resolved_tmdb_id }}
          </span>
          <span v-else-if="task.item_tmdb_id" class="ir-hint">
            条目当前已绑 TMDB {{ task.item_tmdb_id }}
          </span>
          <span class="ir-row-actions">
            <el-button size="small" @click="openDetail(task)">
              查看证据
            </el-button>
            <template v-if="task.status === 'pending'">
              <el-button
                size="small"
                :loading="actingId === task.id"
                @click="doIgnore(task)"
              >
                忽略
              </el-button>
              <el-button
                size="small"
                :loading="actingId === task.id"
                @click="doRerun(task)"
              >
                重跑识别
              </el-button>
            </template>
          </span>
        </div>
      </div>

      <EmptyState
        v-else
        compact
        :icon="ListChecks"
        :title="statusFilter === 'pending' ? '没有待处置的条目' : '这个状态下没有记录'"
        :description="statusFilter === 'pending'
          ? '队列是空的。自动刮削认不准新片时，会带着当时的候选证据排到这里。'
          : '换个状态筛选看看，或者等新记录产生。'"
      />

      <el-pagination
        v-if="total > PAGE_SIZE"
        style="margin-top: 12px"
        layout="prev, pager, next"
        :total="total"
        :page-size="PAGE_SIZE"
        :current-page="page"
        @current-change="onPageChange"
      />
    </SectionCard>

    <!-- 详情抽屉：证据候选 + 处置 -->
    <el-drawer v-model="drawerVisible" title="识别审查详情" size="min(560px, 94vw)">
      <div v-if="detailLoading" class="ir-skeleton" aria-busy="true">
        <div class="au-skeleton" /><div class="au-skeleton" />
      </div>
      <template v-else-if="detail">
        <div class="ir-detail-head">
          <div class="ir-detail-name">
            {{ detail.item_name || '（无名快照）' }}
            <span v-if="detail.item_year" class="ir-hint">（{{ detail.item_year }}）</span>
          </div>
          <div class="ir-detail-facts">
            <span v-if="typeLabel(detail.item_type)">{{ typeLabel(detail.item_type) }}</span>
            <span>入队原因：{{ reasonLabel(detail.reason) }}</span>
            <span>入队时间：{{ fmtTime(detail.created_at) }}</span>
            <span>状态：{{ statusLabel(detail.status) }}</span>
          </div>
          <p class="ir-hint">
            <template v-if="!detail.item_exists">
              原条目已不存在，只能忽略这条记录。
            </template>
            <template v-else-if="detail.item_tmdb_id">
              条目当前已绑 TMDB {{ detail.item_tmdb_id }}（可能已在别处处理过）。
            </template>
            <template v-else>
              条目当前仍未绑定 TMDB。
            </template>
          </p>
          <p v-if="detail.status !== 'pending'" class="ir-hint">
            处置结果：{{ statusLabel(detail.status) }}
            <span v-if="detail.resolved_tmdb_id">（TMDB {{ detail.resolved_tmdb_id }}）</span>
            · {{ fmtTime(detail.resolved_at) }}
          </p>
        </div>

        <div class="ir-step">入队时的候选证据</div>
        <p v-if="detail.evidence?.captured_at" class="ir-hint">
          当时按「{{ detail.evidence.query || detail.item_name }}」
          <span v-if="detail.evidence.searched_year">（{{ detail.evidence.searched_year }}）</span>
          搜索，证据抓取于 {{ fmtTime(detail.evidence.captured_at) }}。这些候选都没过自动绑定的置信线。
        </p>
        <div v-if="detail.evidence?.candidates?.length" class="ir-candidates">
          <div
            v-for="c in detail.evidence.candidates"
            :key="String(c.tmdb_id)"
            class="ir-candidate"
          >
            <div class="ir-poster">
              <img
                v-if="c.poster_path"
                :src="`https://image.tmdb.org/t/p/w200${c.poster_path}`"
                :alt="c.title"
                loading="lazy"
              />
              <div v-else class="ir-poster-empty">暂无海报</div>
            </div>
            <div class="ir-candidate-info">
              <div class="ir-candidate-title">
                {{ c.title }}<span v-if="c.year">（{{ c.year }}）</span>
              </div>
              <div v-if="c.original_title && c.original_title !== c.title" class="ir-hint">
                原名：{{ c.original_title }}
              </div>
              <div class="ir-candidate-meta">
                TMDB {{ c.tmdb_id ?? '—' }} · {{ scoreText(c) }}
                <span v-if="c.popularity !== null && c.popularity !== undefined">
                  · 热度 {{ c.popularity }}
                </span>
              </div>
              <el-button
                v-if="detail.status === 'pending' && c.tmdb_id !== null && c.tmdb_id !== undefined"
                type="primary"
                size="small"
                :loading="bindingKey === String(c.tmdb_id)"
                @click="doBind(detail, String(c.tmdb_id), c.title)"
              >
                绑定这个
              </el-button>
            </div>
          </div>
        </div>
        <p v-else class="ir-hint">
          这次入队没有留下候选证据（抓取证据时 TMDB 没有返回候选）。可以用下方手动指定 ID 绑定，或者送回去重跑识别。
        </p>

        <template v-if="detail.status === 'pending'">
          <div class="ir-step">手动指定 TMDB ID</div>
          <div class="ir-actions">
            <el-input
              v-model="manualTmdbId"
              placeholder="TMDB ID，或 tt 开头的 IMDb ID"
              style="width: 230px"
              clearable
              @keyup.enter="doBind(detail, manualTmdbId)"
            />
            <el-button
              type="primary"
              size="small"
              :loading="bindingKey !== null && bindingKey === manualTmdbId.trim()"
              @click="doBind(detail, manualTmdbId)"
            >
              绑定
            </el-button>
          </div>

          <div class="ir-step">其它处置</div>
          <div class="ir-actions">
            <el-button
              size="small"
              :loading="actingId === detail.id"
              @click="doIgnore(detail)"
            >
              忽略这条
            </el-button>
            <el-button
              size="small"
              :loading="actingId === detail.id"
              @click="doRerun(detail)"
            >
              重跑识别
            </el-button>
          </div>
        </template>
      </template>
    </el-drawer>
  </div>
</template>
