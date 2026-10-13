<script setup lang="ts">
/**
 * 防共享（v2.43.0）：跨城市行为轨迹 + 同播检测
 *
 * 这一页回答两个问题，顺序就是运营看它的顺序：
 *
 * 1. **现在开着没有、开到哪一档**（策略卡）。四档：关闭 / 只记录 / 记录并告警 /
 *    记录、告警并处置。**默认关闭**，「处置」档会停用账号、拦下多余播放会话，
 *    只能由超级管理员显式打开——判错一次就是误伤正常用户。
 * 2. **判出来过什么**（事件流水）。两种 kind 共用一张时间轴：跨城市轨迹与
 *    同播检测是同一件事（账号被多人共用）的两面，翻一个页签就能看全。
 *
 * **没配「IP 与地理位置」能力时跨城市检测不判定**：查不到城市就不知道人在哪，
 * 这里照实显示「未配置地理能力」，而不是假装检测开着。
 *
 * 判定与处置逻辑在后端 `backend/share_guard.py`，本页只做配置与展示。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CalendarClock, Eraser, History, MapPin, MonitorPlay, RefreshCw, Save, ShieldAlert, ShieldCheck,
  SlidersHorizontal, TriangleAlert, Undo2,
} from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard, StatTile } from '@/components/ui'
import {
  fetchShareGuard,
  purgeShareGuardEvents,
  saveShareGuardPolicy,
  type ShareGuardAction,
  type ShareGuardPolicy,
  type ShareGuardResponse,
} from '@/api/admin'
import { useAuthStore } from '@/stores/auth'
import { confirmIrreversible } from '@/composables/useDangerOps'
import DataTable from '@/components/DataTable.vue'
import type { DataColumn } from '@/components/DataTable.vue'

/** 手机卡片：用户为标题，城市/处置/详情/时间做键值行（事件类型与并发数在标题里） */
const columns: DataColumn[] = [
  { key: 'username', label: '用户', width: 130, mobile: 'title' },
  { key: 'created_at', label: '时间', width: 170 },
  { key: 'kind', label: '检测', width: 120 },
  { key: 'action', label: '处置', width: 130 },
  { key: 'region', label: '城市', width: 170 },
  { key: 'detail', label: '说明', minWidth: 260 },
]

const data = ref<ShareGuardResponse | null>(null)
const loading = ref(false)
/** 首次加载失败：给出可重试的错误态 */
const loadError = ref(false)
const saving = ref(false)
const kindFilter = ref('')

/** 本地草稿：改完点「保存」才写后端，不动后端就不算改 */
const draft = ref<ShareGuardPolicy | null>(null)
const dirty = computed(() => {
  const cur = data.value?.policy
  const d = draft.value
  if (!cur || !d) return false
  return (
    cur.travel_action !== d.travel_action ||
    cur.travel_window_minutes !== d.travel_window_minutes ||
    cur.concurrent_action !== d.concurrent_action ||
    cur.concurrent_limit !== d.concurrent_limit ||
    cur.retention_days !== d.retention_days
  )
})

const auth = useAuthStore()
/** 与导航锁标记、危险操作页同一口径：只有超级管理员能改这档配置 */
const canWrite = computed(() => auth.admin?.is_super !== false)
const WRITE_HINT = '防共享的「处置」档会停用账号、拦下播放会话，需要超级管理员才能打开'

/** 清理复用危险操作那一份确认（手打「清理」），与登录日志页同一份实现 */

const ACTION_OPTIONS = [
  { value: 'off', label: '关闭', hint: '不判定、不写库、不通知' },
  { value: 'record', label: '只记录', hint: '写事件流水，后台可查，不打扰任何人' },
  { value: 'alert', label: '记录并告警', hint: '额外给管理员发站内信，不影响用户' },
  { value: 'enforce', label: '记录、告警并处置', hint: '停用账号 / 拦下多余会话（会误伤，请先只记录观察几天）' },
]

async function load() {
  loading.value = true
  loadError.value = false
  try {
    data.value = await fetchShareGuard({ kind: kindFilter.value || undefined, limit: 300 })
    draft.value = { ...data.value.policy }
  } catch {
    /* 拦截器已提示；没有数据时显示错误态 */
    loadError.value = !data.value
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function save() {
  const d = draft.value
  if (!d) return
  // 「处置」档会停用账号、拦下会话：只在**打开**它时多问一句（别的改动不受影响，
  // 已经开着时调参数也不打扰）。确认框三件套：标题=动作、后果说清、按钮动词+对象。
  const cur = data.value?.policy
  const opening: string[] = []
  if (cur) {
    if (d.travel_action === 'enforce' && cur.travel_action !== 'enforce') opening.push('跨城市行为轨迹')
    if (d.concurrent_action === 'enforce' && cur.concurrent_action !== 'enforce') opening.push('同播检测')
  }
  if (opening.length) {
    try {
      await ElMessageBox.confirm(
        `打开后，${opening.join('、')}检测会自动停用被判定共享的账号、拦下多余的播放会话；判错一次就是误伤正在看的用户。建议先只用「只记录」观察几天，确认误判少再打开。`,
        '打开「处置」档',
        {
          type: 'warning',
          confirmButtonText: '打开「处置」档',
          cancelButtonText: '取消',
          confirmButtonClass: 'el-button--danger',
        },
      )
    } catch {
      return
    }
  }
  saving.value = true
  try {
    const res = await saveShareGuardPolicy({
      travel_action: d.travel_action,
      travel_window_minutes: d.travel_window_minutes,
      concurrent_action: d.concurrent_action,
      concurrent_limit: d.concurrent_limit,
      retention_days: d.retention_days,
    })
    data.value = { ...(data.value as ShareGuardResponse), policy: res.policy }
    // 以后端返回的**真实生效值**回填，不自己猜（非法值后端会保持原值不动）
    draft.value = { ...res.policy }
    ElMessage.success('防共享配置已保存并立即生效')
  } catch {
    /* 拦截器已提示 */
  } finally {
    saving.value = false
  }
}

function revert() {
  if (data.value) draft.value = { ...data.value.policy }
}

/** 清空事件：走危险操作那一份（手打「清理」），不留第二个确认口径 */
const purging = ref(false)
const pruning = ref(false)

/** 按保留天数删掉过期的那部分（保留天数 = 0 时无事可做，按钮置灰） */
async function pruneOld() {
  // 按**已保存**的保留天数执行，不拿草稿值（否则会出现「按还没保存的天数删了」）
  const days = data.value?.policy.retention_days ?? 0
  if (days <= 0) return
  const ok = await confirmIrreversible(
    `将删除 ${days} 天前的判定记录（基线轨迹与账号状态不受影响）。`,
    '清理',
    '清理过期防共享事件',
  )
  if (!ok) return
  pruning.value = true
  try {
    const res = await purgeShareGuardEvents(days)
    ElMessage.success(res.message)
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    pruning.value = false
  }
}

async function purge() {
  const ok = await confirmIrreversible(
    '清空后这些判定记录就查不到了（账号状态与已生效的处置不受影响）。',
    '清理',
    '清理防共享事件',
  )
  if (!ok) return
  purging.value = true
  try {
    const res = await purgeShareGuardEvents(0)
    ElMessage.success(res.message)
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    purging.value = false
  }
}

function fmt(ts: string | null): string {
  return ts ? ts.slice(0, 19).replace('T', ' ') : '—'
}

/** 空态要说清「为什么空」：两个开关全关时，压根不会有判定发生 */
const emptyText = computed(() => {
  const p = data.value?.policy
  if (!p) return '暂无防共享事件'
  const allOff = p.travel_action === 'off' && p.concurrent_action === 'off'
  return allOff
    ? '防共享当前全部关闭，判定不会发生；开启后命中的事件会出现在这里'
    : '还没有命中过判定：检测开着但没人触发'
})
</script>

<template>
  <div class="admin-page share-guard">
    <PageHeader
      eyebrow="安全与准入"
      title="防共享"
      description="跨城市行为轨迹与同播检测。默认全部关闭；「处置」档会停用账号、拦下多余会话。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <RefreshCw :size="14" class="btn-ico" />刷新
        </el-button>
      </template>
    </PageHeader>

    <!-- 首屏骨架 -->
    <div v-if="loading && !data" class="sg-skeleton" aria-busy="true" aria-label="加载中">
      <div v-for="n in 4" :key="n" class="au-skeleton sk-tile" />
      <div class="au-skeleton sk-wide" />
    </div>

    <el-alert
      v-else-if="loadError"
      type="error"
      show-icon
      :closable="false"
      title="防共享数据加载失败"
      description="可能是网络或后端暂时不可用。点右上角「刷新」重试。"
    />

    <section v-if="data" class="stat-grid" aria-label="近 24 小时">
      <StatTile
        label="事件总数"
        :icon="ShieldAlert"
        :value="data.summary.total"
        hint="跨城市 + 同播，一条时间轴"
      />
      <StatTile
        label="24h 跨城市"
        :icon="MapPin"
        :value="data.summary.travel_24h"
        :tone="data.summary.travel_24h > 0 ? 'warn' : 'plain'"
        hint="窗口内换城市的判定"
      />
      <StatTile
        label="24h 同播"
        :icon="MonitorPlay"
        :value="data.summary.concurrent_24h"
        :tone="data.summary.concurrent_24h > 0 ? 'warn' : 'plain'"
        hint="并发路数超出上限的判定"
      />
      <StatTile
        label="24h 已处置"
        :icon="TriangleAlert"
        :value="data.summary.enforced_24h"
        :tone="data.summary.enforced_24h > 0 ? 'danger' : 'plain'"
        hint="停用了账号或拦下了会话"
      />
    </section>

    <!-- 策略 -->
    <SectionCard
      v-if="draft"
      title="检测策略"
      :icon="SlidersHorizontal"
      description="两项检测各自选档位；建议先「只记录」观察几天，再考虑告警或处置。"
      :tone="dirty ? 'accent' : 'default'"
    >
      <template v-if="dirty" #actions>
        <span class="au-badge au-badge-amber">有未保存的改动</span>
      </template>

      <el-alert
        v-if="data && !data.policy.geo_ready"
        type="info"
        :closable="false"
        show-icon
        class="sg-alert"
      >
        <strong>跨城市检测现在不会判定</strong>：城市来自「系统设置 → 能力与服务 → IP 与地理位置」，
        没配提供方就查不到人在哪。此时下面第一项选了任何档位都只会空转
        （不判定、不写库）。先把那个能力配上，再回来开启跨城市检测。
      </el-alert>

      <el-alert
        v-if="!canWrite"
        type="warning"
        :closable="false"
        show-icon
        class="sg-alert"
      >
        {{ WRITE_HINT }}。下面的按钮已置灰，但内容仍然可读。
      </el-alert>

      <div class="sg-grid">
        <!-- 跨城市 -->
        <div class="sg-block">
          <h4 class="sg-block-title"><MapPin :size="15" />跨城市行为轨迹</h4>
          <p class="sg-hint">
            登录与开始播放时记下「IP → 城市」。同一个账号在
            <strong>时间窗口内</strong>出现在两个城市，物理上几乎不可能，判定为异常。
            没配地理能力时这一项不会判定（见上面的提示）。
          </p>
          <el-form label-position="top" class="sg-form">
            <el-form-item label="命中后怎么办">
              <el-select v-model="draft.travel_action" :disabled="!canWrite" class="sg-full">
                <el-option
                  v-for="opt in ACTION_OPTIONS"
                  :key="opt.value"
                  :value="opt.value as ShareGuardAction"
                  :label="opt.label"
                >
                  <div class="sg-option">
                    <span>{{ opt.label }}</span>
                    <span class="sg-option-hint">{{ opt.hint }}</span>
                  </div>
                </el-option>
              </el-select>
            </el-form-item>
            <el-form-item label="时间窗口（分钟）">
              <div class="sg-field">
                <el-input-number
                  v-model="draft.travel_window_minutes"
                  :min="1"
                  :max="1440"
                  :step="5"
                  :disabled="!canWrite"
                  controls-position="right"
                  class="sg-num"
                />
                <span class="sg-hint">
                  窗口内换城市才算异常。出差、回家、换网络都是正常行为，窗口调太小会大量误报。
                </span>
              </div>
            </el-form-item>
          </el-form>
        </div>

        <!-- 同播 -->
        <div class="sg-block">
          <h4 class="sg-block-title"><MonitorPlay :size="15" />同播检测</h4>
          <p class="sg-hint">
            同一个账号同时有多路播放会话时判定为异常。「上限 N」= 允许 N 路并发，
            第 N+1 路才算超。一家人一台机各看各的不算超，所以别把上限设成 1。
          </p>
          <el-form label-position="top" class="sg-form">
            <el-form-item label="命中后怎么办">
              <el-select v-model="draft.concurrent_action" :disabled="!canWrite" class="sg-full">
                <el-option
                  v-for="opt in ACTION_OPTIONS"
                  :key="opt.value"
                  :value="opt.value as ShareGuardAction"
                  :label="opt.label"
                >
                  <div class="sg-option">
                    <span>{{ opt.label }}</span>
                    <span class="sg-option-hint">{{ opt.hint }}</span>
                  </div>
                </el-option>
              </el-select>
            </el-form-item>
            <el-form-item label="同时播放数上限">
              <div class="sg-field">
                <el-input-number
                  v-model="draft.concurrent_limit"
                  :min="1"
                  :max="20"
                  :disabled="!canWrite"
                  controls-position="right"
                  class="sg-num"
                />
                <span class="sg-hint">
                  当前策略：上限 {{ draft.concurrent_limit }} 路。
                  超出部分在「处置」档会被拦下并给客户端一个明确的提示。
                </span>
              </div>
            </el-form-item>
          </el-form>
        </div>
      </div>

      <!-- 保留天数：自己就是入口，不再指向另一个页面 -->
      <div class="sg-keep">
        <span class="sg-keep-label">记录保留</span>
        <el-input-number
          v-model="draft.retention_days"
          :min="0"
          :max="3650"
          :step="10"
          :disabled="!canWrite"
          controls-position="right"
          size="small"
          class="sg-num-sm"
        />
        <span class="sg-hint sg-hint-inline">天。0 = 永不清理；「判定记录」里的「清理过期」按已保存的天数立即执行一次（不影响账号状态）。</span>
      </div>

      <template #footer>
        <div class="sg-actions">
          <span class="sg-hint sg-hint-inline">保存后立即生效。</span>
          <div class="sg-actions-btns">
            <el-button :disabled="!canWrite || !dirty" @click="revert">
              <Undo2 :size="14" class="btn-ico" />还原
            </el-button>
            <el-button
              type="primary"
              :disabled="!canWrite || !dirty"
              :loading="saving"
              @click="save"
            >
              <Save :size="14" class="btn-ico" />保存
            </el-button>
          </div>
        </div>
      </template>
    </SectionCard>

    <!-- 事件流水 -->
    <SectionCard
      title="判定记录"
      :icon="History"
      :meta="data ? `${data.events.length} 条（最多显示 300）` : ''"
      flush
    >
      <div class="sg-toolbar">
        <div class="sg-toolbar-filters">
          <el-select v-model="kindFilter" class="sg-kind" aria-label="检测类型" @change="load">
            <el-option
              v-for="k in data?.kinds || [{ value: '', label: '全部' }]"
              :key="k.value"
              :value="k.value"
              :label="k.label"
            />
          </el-select>
        </div>
        <div class="sg-toolbar-actions">
          <el-button
            :loading="pruning"
            :disabled="!data || data.policy.retention_days <= 0"
            @click="pruneOld"
          >
            <CalendarClock :size="14" class="btn-ico" />清理过期
          </el-button>
          <el-button type="danger" plain :loading="purging" @click="purge">
            <Eraser :size="14" class="btn-ico" />清空事件
          </el-button>
        </div>
      </div>
      <p class="sg-hint sg-table-hint">
        这里只列<strong>判定</strong>：行里的「甲城 → 乙城」是跨城市异常，「同时播放 N 路」是同播。
        「用户还在原地」这类基线记录不列在这里（判定靠它记住上一次的城市）。
      </p>
      <DataTable
        :rows="data?.events || []"
        :columns="columns"
        :loading="loading"
        :empty="emptyText"
      >
        <template #empty>
          <EmptyState compact :icon="ShieldCheck" title="暂无防共享事件" :description="emptyText" />
        </template>

        <template #cell-username="{ row }">
          <span class="user-name">{{ row.username || '—' }}</span>
        </template>

        <template #cell-created_at="{ row }"><span class="au-num">{{ fmt(row.created_at) }}</span></template>

        <template #cell-kind="{ row }">
          <span class="au-badge" :class="row.kind === 'travel' ? 'au-badge-info' : 'au-badge-amber'">
            {{ row.kind_label }}
          </span>
        </template>

        <template #cell-action="{ row }">
          <span
            class="au-badge"
            :class="row.action === 'enforce' ? 'au-badge-rose' : row.action === 'alert' ? 'au-badge-amber' : 'au-badge-muted'"
          >
            {{ row.action_label }}
          </span>
        </template>

        <template #cell-region="{ row }">
          <span v-if="row.prev_region" class="sg-region">
            {{ row.prev_region }} → {{ row.region || '未知' }}
          </span>
          <span v-else-if="row.region" class="sg-region">{{ row.region }}</span>
          <span v-else class="sg-muted">—</span>
          <span v-if="row.ip" class="sg-ip">{{ row.ip }}</span>
        </template>

        <template #cell-detail="{ row }">
          <span v-if="!row.detail" class="sg-muted">—</span>
          <span v-else>{{ row.detail }}</span>
        </template>
      </DataTable>
    </SectionCard>
  </div>
</template>

<style scoped>
.share-guard { gap: 16px; }
.btn-ico { margin-right: 4px; }

/* ===== 骨架 / 网格 ===== */
.sg-skeleton,
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 12px;
}
.sk-tile { height: 96px; border-radius: var(--au-r-lg); }
.sk-wide { grid-column: 1 / -1; height: 300px; border-radius: var(--au-r-lg); }

.sg-alert { margin: 0 0 16px; }

/* ===== 策略 ===== */
.sg-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 16px;
}
.sg-block {
  min-width: 0;
  padding: 14px 16px 4px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-bg-soft);
}
.sg-block-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 6px;
  font-family: var(--au-font-serif);
  font-size: 15px;
  font-weight: 600;
  color: var(--au-text);
}
.sg-block-title svg { color: var(--au-primary); }
.sg-hint {
  display: block;
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--au-text-3);
  line-height: 1.8;
}
.sg-hint-inline { margin: 0; }
.sg-full { width: 100%; }
.sg-num { width: 160px; }
.sg-num-sm { width: 130px; }
.sg-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.sg-field .sg-hint { margin: 0; }
.sg-form :deep(.el-form-item) { margin-bottom: 12px; }
.sg-keep {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 16px;
}
.sg-keep-label {
  font-size: 13px;
  color: var(--au-text-2);
  white-space: nowrap;
}
.sg-keep .sg-hint { flex: 1 1 260px; }
.sg-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.sg-actions-btns { display: flex; gap: 8px; }

/* 下拉里的选项：标题 + 一行「点下去会发生什么」 */
.sg-option { display: flex; flex-direction: column; gap: 2px; }
.sg-option-hint {
  font-size: 11.5px;
  color: var(--au-text-3);
  line-height: 1.5;
}

/* ===== 判定记录：筛选在左、操作在右 ===== */
.sg-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 12px 20px 0;
}
.sg-toolbar-filters,
.sg-toolbar-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sg-kind { width: 160px; }
.sg-table-hint { padding: 8px 20px 4px; margin: 0; }
.sg-region {
  display: block;
  font-size: 12px;
  color: var(--au-text-2);
}
.sg-ip {
  display: block;
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--au-text-3);
}
.sg-muted { color: var(--au-text-4); }
.user-name { font-weight: 600; color: var(--au-text); }

/* flush 卡片里的手机卡片列表：DataTable 本身不留边距，这里补回左右内距 */
.share-guard :deep(.dt-cards) { padding: 0 12px 12px; }

@media (max-width: 768px) {
  .sg-grid { grid-template-columns: 1fr; gap: 12px; }
  .sg-toolbar { padding: 10px 16px 0; }
  .sg-table-hint { padding: 8px 16px 4px; }
  .sg-toolbar-filters,
  .sg-toolbar-actions { flex: 1 1 100%; }
  .sg-kind { flex: 1 1 auto; width: auto; }
  .sg-toolbar-actions .el-button { flex: 1 1 0; margin-left: 0; }
  .sg-actions-btns { flex: 1 1 100%; }
  .sg-actions-btns .el-button { flex: 1 1 0; }
}
</style>
