<script setup lang="ts">
/**
 * 贡献池管理（goal-33 PR-4）
 *
 * 成员生命周期运营面：授予候选资格、审核（批准/拒绝）、暂停/恢复、
 * 清退（可勾违规，置位后该成员违规清退期不结激励）、单盘上限覆盖，
 * 外加按条目查池副本与手动下线。池配置在「系统设置 · 公共贡献池」卡。
 * 版式与其它页一致：PageHeader + StatTile + SectionCard，颜色只用
 * --au-* 令牌。
 */
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  AlertTriangle, Coins, Database, Flag, HardDrive, Play, RefreshCw, Users,
} from 'lucide-vue-next'
import { PageHeader, SectionCard, StatTile, EmptyState } from '@/components/ui'
import {
  approvePoolMember, clearPoolMemberViolation, ejectPoolMember,
  fetchPoolCopies, fetchPoolMembers, fetchPoolOverview,
  fetchPoolRewardConfig, fetchPoolRewardDetails, fetchPoolRewardPreview,
  fetchPoolRewardSettlements, grantPoolCandidate, patchPoolMember,
  rejectPoolMember, releasePoolCopy, releasePoolRewardDetail,
  resumePoolMember, savePoolRewardConfig, settlePoolRewards,
  suspendPoolMember,
  type PoolCopy, type PoolMember, type PoolOverview,
  type PoolRewardConfig, type PoolRewardDetail, type PoolRewardPreview,
  type PoolRewardSettlement,
} from '@/api/pool'

const loading = ref(false)
const loadError = ref('')
const overview = ref<PoolOverview | null>(null)
const members = ref<PoolMember[]>([])
const actingId = ref<number | null>(null)

const grantUserId = ref<number | null>(null)
const granting = ref(false)

const copiesItemId = ref<number | null>(null)
const copies = ref<PoolCopy[]>([])
const copiesLoading = ref(false)
const copiesQueried = ref(false)

const ejectTarget = ref<PoolMember | null>(null)
const ejectVisible = ref(false)
const ejectViolation = ref(false)
const ejectReason = ref('')
const ejecting = ref(false)

const STATUS_LABELS: Record<string, string> = {
  candidate: '候选',
  pending: '待审',
  active: '在池',
  suspended: '已暂停',
  exited: '已退出',
  rejected: '已拒绝',
  ejected: '已清退',
}

const REASON_LABELS: Record<string, string> = {
  manual: '管理员暂停',
  breaker: '熔断',
  binding_invalid: '绑定失效',
  contributor_exit: '贡献者退出',
  admin_eject: '管理员清退',
}

function fmtBytes(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}

function fmtTime(s: string | null): string {
  return s ? s.replace('T', ' ').slice(0, 16) : '—'
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [ov, list] = await Promise.all([
      fetchPoolOverview(), fetchPoolMembers(),
    ])
    overview.value = ov
    members.value = list.members
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '请求失败'
  } finally {
    loading.value = false
  }
}

async function runAction(id: number, fn: () => Promise<unknown>, okText: string) {
  actingId.value = id
  try {
    await fn()
    ElMessage.success(okText)
    await load()
  } catch {
    // request 层已弹错误提示
  } finally {
    actingId.value = null
  }
}

async function doGrant() {
  if (!grantUserId.value) {
    ElMessage.warning('先填站内用户 ID')
    return
  }
  granting.value = true
  try {
    await grantPoolCandidate(grantUserId.value)
    ElMessage.success('候选资格已授予')
    grantUserId.value = null
    await load()
  } catch {
    // request 层已弹错误提示
  } finally {
    granting.value = false
  }
}

async function confirmDo(
  title: string, text: string, fn: () => Promise<unknown>, okText: string,
  id: number,
) {
  try {
    await ElMessageBox.confirm(text, title, {
      confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning',
    })
  } catch {
    return
  }
  await runAction(id, fn, okText)
}

function openEject(member: PoolMember) {
  ejectTarget.value = member
  ejectViolation.value = false
  ejectReason.value = ''
  ejectVisible.value = true
}

async function doEject() {
  if (!ejectTarget.value) return
  const target = ejectTarget.value
  ejecting.value = true
  try {
    await ejectPoolMember(target.id, {
      violation: ejectViolation.value,
      violation_reason: ejectViolation.value ? ejectReason.value || null : null,
    })
    ElMessage.success('已清退')
    ejectTarget.value = null
    ejectVisible.value = false
    await load()
  } catch {
    // request 层已弹错误提示
  } finally {
    ejecting.value = false
  }
}

async function editCapacity(member: PoolMember) {
  let value: string
  try {
    const res = await ElMessageBox.prompt(
      '填这个盘的池占用上限（字节）。清空 = 回落全局配置。',
      `单盘上限 · ${member.username ?? member.user_id}`,
      {
        confirmButtonText: '保存',
        cancelButtonText: '取消',
        inputValue: member.capacity_cap_bytes != null
          ? String(member.capacity_cap_bytes) : '',
        inputPattern: /^\d*$/,
        inputErrorMessage: '只能填非负整数（字节）',
      },
    )
    value = res.value
  } catch {
    return
  }
  const cap = value.trim() === '' ? null : Number(value)
  await runAction(
    member.id,
    () => patchPoolMember(member.id, { capacity_cap_bytes: cap }),
    '单盘上限已更新',
  )
}

async function loadCopies() {
  if (!copiesItemId.value) {
    ElMessage.warning('先填条目 ID')
    return
  }
  copiesLoading.value = true
  try {
    const res = await fetchPoolCopies(copiesItemId.value)
    copies.value = res.copies
    copiesQueried.value = true
  } catch {
    // request 层已弹错误提示
  } finally {
    copiesLoading.value = false
  }
}

async function doReleaseCopy(copy: PoolCopy) {
  try {
    await ElMessageBox.confirm(
      '下线后这份副本立即停发，盘内文件会尝试删除。确定下线吗？',
      '手动下线副本',
      { confirmButtonText: '下线', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await releasePoolCopy(copy.id)
    ElMessage.success('已下线')
    await loadCopies()
    await load()
  } catch {
    // request 层已弹错误提示
  }
}

// ------------------------------------------------------------ 激励结算（PR-6）

const rewardCfg = ref<PoolRewardConfig | null>(null)
const rewardCfgSaving = ref(false)
const rewardPeriod = ref('')
const preview = ref<PoolRewardPreview | null>(null)
const previewLoading = ref(false)
const settling = ref(false)
const settlements = ref<PoolRewardSettlement[]>([])
const rewardDetails = ref<PoolRewardDetail[]>([])
const detailSettlementId = ref<number | null>(null)
const detailsLoading = ref(false)
const releasingId = ref<number | null>(null)

const REWARD_STATUS_LABELS: Record<string, string> = {
  shadow: '影子（只算账）',
  pending: '待发',
  frozen: '冻结待审',
  paid: '已发放',
  skipped: '不结',
}

const REWARD_REASON_LABELS: Record<string, string> = {
  below_threshold: '门槛不达',
  ejected_violation: '违规清退',
  daily_play_freeze: '单日激增冻结',
  user_missing: '账号不存在',
}

async function loadRewards() {
  try {
    const [cfg, list] = await Promise.all([
      fetchPoolRewardConfig(), fetchPoolRewardSettlements(),
    ])
    rewardCfg.value = cfg
    settlements.value = list.settlements
  } catch {
    // 激励区加载失败不拖垮成员区；request 层已弹提示
  }
}

async function doSaveRewardCfg() {
  if (!rewardCfg.value) return
  rewardCfgSaving.value = true
  try {
    rewardCfg.value = await savePoolRewardConfig({
      pool_reward_enabled: rewardCfg.value.pool_reward_enabled,
      pool_reward_payout_enabled: rewardCfg.value.pool_reward_payout_enabled,
      pool_reward_points_per_play: rewardCfg.value.pool_reward_points_per_play,
      pool_reward_points_per_active_day:
        rewardCfg.value.pool_reward_points_per_active_day,
      pool_reward_points_cap: rewardCfg.value.pool_reward_points_cap,
      pool_reward_min_active_days: rewardCfg.value.pool_reward_min_active_days,
      pool_reward_min_counted_plays:
        rewardCfg.value.pool_reward_min_counted_plays,
    })
    ElMessage.success('激励配置已保存')
  } catch {
    // request 层已弹错误提示
  } finally {
    rewardCfgSaving.value = false
  }
}

async function doPreview() {
  previewLoading.value = true
  try {
    preview.value = await fetchPoolRewardPreview(
      rewardPeriod.value.trim() || undefined)
  } catch {
    // request 层已弹错误提示
  } finally {
    previewLoading.value = false
  }
}

async function doSettle() {
  try {
    await ElMessageBox.confirm(
      '按当前配置执行这一期结算。真发开关开着时会真的发积分/经验；' +
      '关着时只落影子行对账。重复执行不会重复发钱。',
      '执行激励结算',
      { confirmButtonText: '执行结算', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  settling.value = true
  try {
    const res = await settlePoolRewards(rewardPeriod.value.trim() || undefined)
    ElMessage.success(`结算完成：${res.settlement.period_key}`)
    detailSettlementId.value = null
    rewardDetails.value = []
    await loadRewards()
  } catch {
    // request 层已弹错误提示
  } finally {
    settling.value = false
  }
}

async function viewDetails(settlement: PoolRewardSettlement) {
  detailsLoading.value = true
  try {
    const res = await fetchPoolRewardDetails(settlement.id)
    rewardDetails.value = res.details
    detailSettlementId.value = settlement.id
  } catch {
    // request 层已弹错误提示
  } finally {
    detailsLoading.value = false
  }
}

async function doReleaseDetail(detail: PoolRewardDetail) {
  try {
    await ElMessageBox.confirm(
      '放行后按冻结时已计（封顶后）的数发放；真发开关关着时先落影子行，' +
      '开闸重跑后补发。',
      '放行冻结明细',
      { confirmButtonText: '放行', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  releasingId.value = detail.id
  try {
    const updated = await releasePoolRewardDetail(detail.id)
    ElMessage.success('已放行')
    const idx = rewardDetails.value.findIndex(d => d.id === updated.id)
    if (idx >= 0) rewardDetails.value[idx] = updated
    await loadRewards()
  } catch {
    // request 层已弹错误提示
  } finally {
    releasingId.value = null
  }
}

onMounted(() => {
  void load()
  void loadRewards()
})
</script>

<template>
  <div class="pool-page">
    <PageHeader
      eyebrow="媒体"
      title="贡献池"
      description="公共贡献池成员管理：候选资格、审核、暂停/恢复、清退与副本下线。池开关与限额在「系统设置 · 公共贡献池」里改。"
    >
      <template #actions>
        <el-button :loading="loading" :icon="RefreshCw" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <SectionCard v-if="loadError && !overview">
      <EmptyState compact :icon="AlertTriangle" title="读取贡献池状态失败" :description="loadError">
        <template #actions><el-button :loading="loading" @click="load">重试</el-button></template>
      </EmptyState>
    </SectionCard>

    <template v-else-if="overview">
      <div class="pool-stats">
        <StatTile label="在池成员" :value="overview.members_active" :icon="Users" />
        <StatTile
          label="待审 / 候选"
          :value="overview.members_pending + overview.members_candidate"
          :hint="`待审 ${overview.members_pending} · 候选 ${overview.members_candidate}`"
          :icon="Users"
        />
        <StatTile
          label="池副本"
          :value="overview.copies_total"
          :hint="`占用 ${fmtBytes(overview.copies_bytes)}`"
          :icon="Database"
        />
        <StatTile
          label="今日起播"
          :value="overview.today_play_starts"
          :hint="`今日转存 ${overview.today_transfer_jobs} 单 · ${fmtBytes(overview.today_transfer_bytes)}`"
          :icon="Play"
        />
        <StatTile
          label="熔断中"
          :value="overview.members_breaker"
          :hint="`已暂停共 ${overview.members_suspended} 个`"
          :tone="overview.members_breaker ? 'warn' : 'plain'"
          :icon="AlertTriangle"
        />
      </div>

      <SectionCard title="授予候选资格" :icon="Flag" description="给某个站内账号预批入池资格：他绑盘时走池成员上限（默认 10 个），绑定后申请会挂到这行上。">
        <div class="pool-grant">
          <el-input-number
            v-model="grantUserId"
            :min="1"
            :controls="false"
            placeholder="站内用户 ID"
            class="pool-grant-input"
          />
          <el-button type="primary" :loading="granting" @click="doGrant">授予候选资格</el-button>
        </div>
      </SectionCard>

      <SectionCard title="成员" :icon="Users" :meta="`${members.length} 行`" flush>
        <el-table :data="members" stripe class="pool-table">
          <el-table-column label="贡献者" min-width="130">
            <template #default="{ row }">
              <div class="pool-cell-main">{{ row.username ?? '—' }}</div>
              <div class="pool-cell-sub">#{{ row.user_id }}</div>
            </template>
          </el-table-column>
          <el-table-column label="盘" min-width="140">
            <template #default="{ row }">
              <div class="pool-cell-main">{{ row.account_label ?? '（未绑定）' }}</div>
              <div class="pool-cell-sub">
                {{ row.provider ?? '—' }}
                <template v-if="row.binding_status"> · 绑定{{ row.binding_status === 'active' ? '正常' : '已失效' }}</template>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="状态" min-width="120">
            <template #default="{ row }">
              <span class="pool-status" :class="`is-${row.status}`">
                {{ STATUS_LABELS[row.status] ?? row.status }}
              </span>
              <div v-if="row.suspended_reason" class="pool-cell-sub">
                {{ REASON_LABELS[row.suspended_reason] ?? row.suspended_reason }}
              </div>
              <div v-if="row.violation_flagged" class="pool-viol">
                违规 · {{ row.violation_reason ?? '未写原因' }}
              </div>
            </template>
          </el-table-column>
          <el-table-column label="池占用 / 上限" min-width="130">
            <template #default="{ row }">
              <div class="pool-cell-main">{{ fmtBytes(row.copies_bytes) }}</div>
              <div class="pool-cell-sub">
                上限 {{ fmtBytes(row.effective_capacity_bytes) }}{{ row.capacity_cap_bytes != null ? '（单盘覆盖）' : '' }}
              </div>
            </template>
          </el-table-column>
          <el-table-column label="副本" width="70" align="center">
            <template #default="{ row }">{{ row.copies_count }}</template>
          </el-table-column>
          <el-table-column label="今日" min-width="120">
            <template #default="{ row }">
              <div class="pool-cell-sub">转存 {{ row.today_transfer_jobs }} 单 · {{ fmtBytes(row.today_transfer_bytes) }}</div>
              <div class="pool-cell-sub">起播 {{ row.today_play_starts }} · 额度命中 {{ row.today_quota_hits }}</div>
            </template>
          </el-table-column>
          <el-table-column label="加入" min-width="110">
            <template #default="{ row }">{{ fmtTime(row.joined_at ?? row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" min-width="230" fixed="right">
            <template #default="{ row }">
              <div class="pool-actions">
                <el-button
                  v-if="row.status === 'pending'"
                  size="small" type="primary" :loading="actingId === row.id"
                  @click="runAction(row.id, () => approvePoolMember(row.id), '已批准')"
                >批准</el-button>
                <el-button
                  v-if="row.status === 'pending' || row.status === 'candidate'"
                  size="small" :loading="actingId === row.id"
                  @click="confirmDo('拒绝申请', '拒绝后这行落为已拒绝，贡献者可以重新申请。', () => rejectPoolMember(row.id), '已拒绝', row.id)"
                >拒绝</el-button>
                <el-button
                  v-if="row.status === 'active'"
                  size="small" :loading="actingId === row.id"
                  @click="confirmDo('暂停成员', '暂停后这块盘不再接新单，它的副本暂停下发（有第二副本时观众走第二副本）。', () => suspendPoolMember(row.id), '已暂停', row.id)"
                >暂停</el-button>
                <el-button
                  v-if="row.status === 'suspended'"
                  size="small" type="primary" :loading="actingId === row.id"
                  @click="runAction(row.id, () => resumePoolMember(row.id), '已恢复')"
                >恢复</el-button>
                <el-button
                  v-if="!['exited', 'rejected', 'ejected'].includes(row.status)"
                  size="small" :loading="actingId === row.id"
                  @click="editCapacity(row)"
                >上限</el-button>
                <el-button
                  v-if="!['exited', 'rejected', 'ejected'].includes(row.status)"
                  size="small" type="danger" plain :loading="actingId === row.id"
                  @click="openEject(row)"
                >清退</el-button>
                <el-button
                  v-if="row.violation_flagged"
                  size="small" :loading="actingId === row.id"
                  @click="runAction(row.id, () => clearPoolMemberViolation(row.id), '违规标志已清除')"
                >清除违规</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <EmptyState
          v-if="!members.length"
          compact :icon="Users" title="暂无成员"
          description="先在上方授予候选资格，或等贡献者从用户端申请。"
        />
      </SectionCard>

      <SectionCard title="激励结算" :icon="Coins" description="贡献者激励按周期结算：先影子跑对账（只算账落行、不发钱），核对无误再开真发开关。计奖口径：起播 1 分 + 有效在线日 5 分（默认，可改），门槛＝在线 ≥7 天且起播 ≥10，单期上限 900 分。">
        <template v-if="rewardCfg">
          <div class="pool-grant">
            <el-switch v-model="rewardCfg.pool_reward_enabled" />
            <span>激励总开关</span>
            <el-switch v-model="rewardCfg.pool_reward_payout_enabled" />
            <span>真发开关（关＝影子跑）</span>
            <el-button type="primary" :loading="rewardCfgSaving" @click="doSaveRewardCfg">保存激励配置</el-button>
          </div>
          <div class="pool-grant" style="margin-top: 10px">
            <span class="pool-cell-sub">每起播积分</span>
            <el-input-number v-model="rewardCfg.pool_reward_points_per_play" :min="0" :controls="false" class="pool-num-input" />
            <span class="pool-cell-sub">每在线日积分</span>
            <el-input-number v-model="rewardCfg.pool_reward_points_per_active_day" :min="0" :controls="false" class="pool-num-input" />
            <span class="pool-cell-sub">单期上限</span>
            <el-input-number v-model="rewardCfg.pool_reward_points_cap" :min="0" :controls="false" class="pool-num-input" />
            <span class="pool-cell-sub">门槛在线天</span>
            <el-input-number v-model="rewardCfg.pool_reward_min_active_days" :min="0" :controls="false" class="pool-num-input" />
            <span class="pool-cell-sub">门槛起播</span>
            <el-input-number v-model="rewardCfg.pool_reward_min_counted_plays" :min="0" :controls="false" class="pool-num-input" />
          </div>
        </template>
        <div class="pool-grant" style="margin-top: 14px">
          <el-input
            v-model="rewardPeriod"
            placeholder="周期键，如 2026-11（留空＝上一完整周期）"
            class="pool-grant-input"
            clearable
          />
          <el-button :loading="previewLoading" @click="doPreview">结算预览</el-button>
          <el-button type="primary" :loading="settling" @click="doSettle">执行结算</el-button>
        </div>
        <template v-if="preview">
          <p class="pool-dialog-text" style="margin-top: 12px">
            周期 {{ preview.period_key }}（{{ preview.period_start.slice(0, 10) }} 至 {{ preview.period_end.slice(0, 10) }}）；
            激励总开关{{ preview.reward_enabled ? '开' : '关' }}、真发{{ preview.payout_enabled ? '开' : '关（影子）' }}。
            预览只算账、不落行。
          </p>
          <el-table :data="preview.rows" stripe class="pool-table">
            <el-table-column label="贡献者" min-width="120">
              <template #default="{ row }">{{ row.username ?? `#${row.user_id}` }}</template>
            </el-table-column>
            <el-table-column label="计奖起播 / 原始" min-width="120">
              <template #default="{ row }">{{ row.counted_plays }} / {{ row.raw_plays }}</template>
            </el-table-column>
            <el-table-column label="有效在线天" width="100">
              <template #default="{ row }">{{ row.active_days }}</template>
            </el-table-column>
            <el-table-column label="应发积分" width="100">
              <template #default="{ row }">{{ row.points }}</template>
            </el-table-column>
            <el-table-column label="预计走向" min-width="130">
              <template #default="{ row }">
                {{ REWARD_STATUS_LABELS[row.would_status] ?? row.would_status }}
                <span v-if="row.reason" class="pool-cell-sub">（{{ REWARD_REASON_LABELS[row.reason] ?? row.reason }}）</span>
              </template>
            </el-table-column>
          </el-table>
          <EmptyState
            v-if="!preview.rows.length"
            compact :icon="Coins" title="这一期没有可结算的成员"
            description="还没有成员真正进过池，或本期没有任何起播计量。"
          />
        </template>
        <el-table v-if="settlements.length" :data="settlements" stripe class="pool-table" style="margin-top: 14px">
          <el-table-column label="周期" width="110">
            <template #default="{ row }">{{ row.period_key }}</template>
          </el-table-column>
          <el-table-column label="批次状态" width="110">
            <template #default="{ row }">
              {{ row.status }}{{ row.shadow ? '（影子建批）' : '' }}
            </template>
          </el-table-column>
          <el-table-column label="成员 / 已发 / 冻结 / 不结" min-width="170">
            <template #default="{ row }">
              {{ row.members }} / {{ row.paid }} / {{ row.frozen }} / {{ row.skipped }}
            </template>
          </el-table-column>
          <el-table-column label="已发积分" width="100">
            <template #default="{ row }">{{ row.paid_points_total }}</template>
          </el-table-column>
          <el-table-column label="建批时间" min-width="120">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <el-button size="small" :loading="detailsLoading" @click="viewDetails(row)">明细</el-button>
            </template>
          </el-table-column>
        </el-table>
        <template v-if="detailSettlementId != null">
          <p class="pool-dialog-text" style="margin-top: 12px">
            批次 #{{ detailSettlementId }} 明细：冻结行核对后可放行（按已计封顶数发）。
          </p>
          <el-table :data="rewardDetails" stripe class="pool-table">
            <el-table-column label="贡献者" min-width="120">
              <template #default="{ row }">{{ row.username ?? `#${row.user_id}` }}</template>
            </el-table-column>
            <el-table-column label="计奖起播" width="90">
              <template #default="{ row }">{{ row.counted_plays }}</template>
            </el-table-column>
            <el-table-column label="在线天" width="80">
              <template #default="{ row }">{{ row.active_days }}</template>
            </el-table-column>
            <el-table-column label="积分 / 经验" width="110">
              <template #default="{ row }">{{ row.points }} / {{ row.xp }}</template>
            </el-table-column>
            <el-table-column label="转存（对账）" width="110">
              <template #default="{ row }">{{ fmtBytes(row.transfer_bytes) }}</template>
            </el-table-column>
            <el-table-column label="状态" min-width="140">
              <template #default="{ row }">
                {{ REWARD_STATUS_LABELS[row.status] ?? row.status }}
                <span v-if="row.reason" class="pool-cell-sub">（{{ REWARD_REASON_LABELS[row.reason] ?? row.reason }}）</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100">
              <template #default="{ row }">
                <el-button
                  v-if="row.status === 'frozen'"
                  size="small" type="primary" :loading="releasingId === row.id"
                  @click="doReleaseDetail(row)"
                >放行</el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </SectionCard>

      <SectionCard title="按条目查副本" :icon="HardDrive" description="看某个条目的池副本都在哪些盘上；可单份手动下线（停发 + 尝试删文件）。">
        <div class="pool-grant">
          <el-input-number
            v-model="copiesItemId"
            :min="1"
            :controls="false"
            placeholder="条目 ID"
            class="pool-grant-input"
          />
          <el-button :loading="copiesLoading" @click="loadCopies">查询</el-button>
        </div>
        <el-table v-if="copiesQueried" :data="copies" stripe class="pool-table">
          <el-table-column label="副本 ID" width="90">
            <template #default="{ row }">#{{ row.id }}</template>
          </el-table-column>
          <el-table-column label="条目" min-width="160">
            <template #default="{ row }">{{ row.item_name ?? `#${row.item_id}` }}</template>
          </el-table-column>
          <el-table-column label="成员" width="90">
            <template #default="{ row }">{{ row.member_id ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">{{ row.status }}</template>
          </el-table-column>
          <el-table-column label="大小" width="110">
            <template #default="{ row }">{{ fmtBytes(row.size_bytes) }}</template>
          </el-table-column>
          <el-table-column label="最近播放" min-width="120">
            <template #default="{ row }">{{ fmtTime(row.last_played_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <el-button
                v-if="row.status === 'ready' || row.status === 'orphan'"
                size="small" type="danger" plain
                @click="doReleaseCopy(row)"
              >下线</el-button>
            </template>
          </el-table-column>
        </el-table>
        <EmptyState
          v-if="copiesQueried && !copies.length"
          compact :icon="HardDrive" title="这个条目没有池副本"
          description="还没人通过公共池看过它，或副本已全部下线。"
        />
      </SectionCard>
    </template>

    <el-dialog v-model="ejectVisible" title="清退成员" width="440px" :close-on-click-modal="false">
      <template v-if="ejectTarget">
        <p class="pool-dialog-text">
          清退 {{ ejectTarget.username ?? ejectTarget.user_id }} 后：成员落终态，
          它的池副本会停发并尝试删除。清退不可恢复（重新入池要重新走申请）。
        </p>
        <div class="pool-eject-form">
          <el-switch v-model="ejectViolation" />
          <span>这次清退属于违规清退</span>
        </div>
        <p v-if="ejectViolation" class="pool-dialog-text">
          勾上违规：该成员本期激励不结（违规清退口径）；普通清退不勾，历史贡献照常结算。
        </p>
        <el-input
          v-if="ejectViolation"
          v-model="ejectReason"
          placeholder="违规原因（留痕备查）"
          maxlength="200"
        />
      </template>
      <template #footer>
        <el-button @click="ejectVisible = false">取消</el-button>
        <el-button type="danger" :loading="ejecting" @click="doEject">确定清退</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.pool-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.pool-grant {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.pool-grant-input {
  width: 200px;
}
.pool-num-input {
  width: 90px;
}
.pool-table {
  width: 100%;
}
.pool-cell-main {
  font-weight: 600;
  color: var(--au-text);
}
.pool-cell-sub {
  font-size: 12px;
  color: var(--au-text-3);
}
.pool-status {
  font-weight: 600;
}
.pool-status.is-active {
  color: var(--au-ok, #2e9e5b);
}
.pool-status.is-pending,
.pool-status.is-candidate {
  color: var(--au-warn, #c07f13);
}
.pool-status.is-suspended,
.pool-status.is-ejected {
  color: var(--au-danger, #c0392b);
}
.pool-viol {
  font-size: 12px;
  color: var(--au-danger, #c0392b);
}
.pool-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.pool-dialog-text {
  color: var(--au-text-2);
  line-height: 1.6;
  margin: 0 0 12px;
}
.pool-eject-form {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
</style>
