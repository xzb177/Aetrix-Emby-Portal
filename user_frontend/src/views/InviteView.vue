<script setup lang="ts">
/**
 * 邀请返利 — 我的邀请码、邀请记录、返利台账
 */
import { ref, computed, onMounted } from 'vue'
import {
  Gift, Users, Coins, Percent, Copy, Check, Share2, UserPlus, Link2, Handshake, ChevronDown,
  RefreshCw, Sparkles,
} from 'lucide-vue-next'
import {
  inviteApi,
  type MyInviteInfo, type InvitationRecordRow, type RebateRow,
  type MyPromotions, type PromotionRewardRow,
} from '@/api/economy'
import { useClipboard } from '@/composables/useClipboard'
import EmptyState from '@/components/ui/EmptyState.vue'

const { copy } = useClipboard()

const loading = ref(true)
const failed = ref(false)
const info = ref<MyInviteInfo | null>(null)
const records = ref<InvitationRecordRow[]>([])
const rebates = ref<RebateRow[]>([])
const totalRebate = ref(0)
const promotions = ref<PromotionRewardRow[]>([])
const promoConfig = ref<MyPromotions['config'] | null>(null)
const copied = ref('')

const inviteLink = computed(() => {
  if (!info.value?.code) return ''
  const base = window.location.origin
  return `${base}/login?invite=${info.value.code}`
})

const copiedField = ref('')

/**
 * 复制邀请码 / 邀请链接。
 *
 * 真正的复制与反馈都交给 useClipboard（HTTP 等非安全上下文下
 * navigator.clipboard 不存在，它会降级到 execCommand）；
 * 这里只负责成功后把图标换成勾，以及 1.8 秒后换回去。
 */
async function copyText(text: string, field: string) {
  const ok = await copy(text, field === 'link' ? '邀请链接' : '邀请码')
  if (!ok) return
  copiedField.value = field
  setTimeout(() => { if (copiedField.value === field) copiedField.value = '' }, 1800)
}

function fmtTime(iso?: string | null) {
  if (!iso) return '—'
  return iso.slice(0, 16).replace('T', ' ')
}

/**
 * 邀请是否由管理员开启（来自 /my-code 的 config）。
 * 关掉时后端仍会生成邀请码、但 apply_invitation 直接返回未应用——
 * 页面上必须说清楚，否则用户会拿着一个看着能用、实际不发奖励的码去邀请朋友。
 */
const disabled = computed(() => info.value?.config?.enabled === false)

/** 推广奖励开关（来自 /promotions 的 config）：决定空态怎么解释 */
const promoEnabled = computed(() => promoConfig.value?.enabled === true)

async function load() {
  loading.value = true
  failed.value = false
  try {
    const emptyRecords = { total: 0, records: [] as InvitationRecordRow[] }
    const emptyRebates = { total_rebate: 0, rebates: [] as RebateRow[] }
    const emptyPromotions: MyPromotions = {
      total: 0,
      config: {
        enabled: false, reward_type: 'balance', amount: 0, days: 0,
        reward_types: [], reward_type_labels: {},
      },
      rewards: [],
    }
    const [my, rec, reb, pro] = await Promise.all([
      inviteApi.myCode(),
      inviteApi.records({ limit: 50 }).catch(() => emptyRecords),
      inviteApi.rebates({ limit: 50 }).catch(() => emptyRebates),
      inviteApi.promotions({ limit: 50 }).catch(() => emptyPromotions),
    ])
    info.value = my
    records.value = rec.records
    rebates.value = reb.rebates
    totalRebate.value = reb.total_rebate
    promotions.value = pro.rewards
    promoConfig.value = pro.config
  } catch {
    // 邀请码拿不到（网络 / 接口异常）要跟「没邀请记录」区分开：
    // 旧实现会留一页 '···' 和 '—'，看上去就像功能没做
    failed.value = true
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="au-page invite-view">
    <!-- 邀请主卡：左文案右码框的双栏布局 -->
    <section class="invite-hero au-anim-up">
      <div class="hero-body">
        <div class="hero-left">
          <span class="hero-badge"><Gift :size="14" /> 邀请返利计划</span>
          <h1>邀请好友 <span class="au-gradient-text">双向得奖励</span></h1>
          <p class="hero-desc">
            分享你的专属邀请码，好友注册成功后：
            <template v-if="info">
              你得 <strong>+{{ info.config.reward_points }}</strong> 积分，
              好友得 <strong>+{{ info.config.invitee_reward_points }}</strong> 积分；
              好友每笔充值你还能拿 <strong>{{ info.config.rebate_percent }}%</strong> 返利。
            </template>
          </p>
        </div>

        <div class="hero-divider" aria-hidden="true" />

        <div v-if="disabled" class="code-box off">
          <div class="code-label">
            <Link2 :size="14" />
            邀请返利当前未开启
          </div>
          <p class="off-note">
            管理员可以在后台「系统设置 → 邀请与返利」开启本项目；开启后这里会显示你的专属邀请码。
          </p>
        </div>

        <div v-else class="code-box">
          <div class="code-label">
            <Link2 :size="14" />
            我的专属邀请码
          </div>
          <div class="code-row">
            <span class="code-value">{{ info?.code || '···' }}</span>
            <button
              class="au-btn au-btn-primary au-btn-sm"
              :disabled="!info?.code"
              @click="copyText(info?.code || '', 'code')"
            >
              <Check v-if="copiedField === 'code'" :size="14" />
              <Copy v-else :size="14" />
              复制
            </button>
          </div>
          <div class="code-row link-row">
            <input class="au-input link-input" :value="inviteLink" readonly>
            <button
              class="au-btn au-btn-ghost au-btn-sm"
              :disabled="!inviteLink"
              @click="copyText(inviteLink, 'link')"
            >
              <Share2 :size="14" />
              复制链接
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- 邀请信息没拿到：给一条明确的出路，而不是一页 “···” 和 “—” -->
    <section v-if="failed" class="au-card au-card-pad failed-state au-anim-up">
      <Gift :size="26" />
      <p>邀请信息暂时读取失败，可能是网络波动</p>
      <button class="au-btn au-btn-ghost au-btn-sm" :disabled="loading" @click="load">
        <RefreshCw :size="14" />
        重新加载
      </button>
    </section>

    <!-- 统计：与主卡合并为单行数据条，减少碎片卡片 -->
    <section v-if="!failed" class="stat-bar au-card au-anim-up" style="animation-delay: 70ms">
      <div class="stat-cell">
        <span class="stat-icon c1"><Users :size="17" /></span>
        <div class="stat-body">
          <span class="stat-num">{{ info?.invited_count ?? '—' }}</span>
          <span class="stat-label">邀请好友</span>
        </div>
      </div>
      <span class="stat-divider" />
      <div class="stat-cell">
        <span class="stat-icon c3"><Coins :size="17" /></span>
        <div class="stat-body">
          <span class="stat-num">{{ totalRebate }}</span>
          <span class="stat-label">累计返利</span>
        </div>
      </div>
      <span class="stat-divider" />
      <div class="stat-cell">
        <span class="stat-icon c2"><Handshake :size="17" /></span>
        <div class="stat-body">
          <span class="stat-num">{{ info?.use_count ?? '—' }}</span>
          <span class="stat-label">码使用次数</span>
        </div>
      </div>
      <span class="stat-divider hide-sm" />
      <div class="stat-cell hide-sm">
        <span class="stat-icon c4"><Percent :size="17" /></span>
        <div class="stat-body">
          <span class="stat-num">{{ info?.config.rebate_percent ?? '—' }}%</span>
          <span class="stat-label">充值返利比</span>
        </div>
      </div>
    </section>

    <!-- 邀请记录 / 返利台账 -->
    <div v-if="!failed" class="list-grid au-anim-up" style="animation-delay: 120ms">
      <section class="au-card au-card-pad">
        <header class="list-head">
          <h3><UserPlus :size="16" /> 邀请记录</h3>
          <span class="au-badge au-badge-cyan">{{ records.length }} 条</span>
        </header>
        <EmptyState
          v-if="!records.length"
          size="compact"
          title="还没有邀请记录"
          description="分享链接邀请朋友"
        >
          <template #action>
            <button class="au-btn au-btn-ghost au-btn-sm" @click="copyText(inviteLink, 'link')">复制邀请链接</button>
          </template>
        </EmptyState>
        <ul v-else class="rows">
          <li v-for="r in records" :key="r.id">
            <span class="row-avatar">{{ r.invitee_username.charAt(0).toUpperCase() }}</span>
            <span class="row-main">
              <span class="row-title">{{ r.invitee_username }}</span>
              <span class="row-sub">{{ fmtTime(r.created_at) }} 加入</span>
            </span>
            <strong class="row-amt">+{{ r.reward_points }}</strong>
          </li>
        </ul>
      </section>

      <section class="au-card au-card-pad">
        <header class="list-head">
          <h3><Coins :size="16" /> 返利台账</h3>
          <span class="au-badge au-badge-green">累计 +{{ totalRebate }}</span>
        </header>
        <div v-if="!rebates.length" class="au-empty compact">
          <Coins :size="26" />
          <p>好友充值后返利将自动到账</p>
        </div>
        <ul v-else class="rows">
          <li v-for="r in rebates" :key="r.id">
            <span class="row-dot" />
            <span class="row-main">
              <span class="row-title">{{ r.description || '充值返利' }}</span>
              <span class="row-sub">{{ fmtTime(r.created_at) }}</span>
            </span>
            <strong class="row-amt">+{{ r.amount }}</strong>
          </li>
        </ul>
      </section>
    </div>

    <!-- 推广奖励明细：邀请即发的一次性奖励（余额/有效期），与充值返利分开说 -->
    <section v-if="!failed" class="au-card au-card-pad promo-card au-anim-up" style="animation-delay: 145ms">
      <header class="list-head">
        <h3><Sparkles :size="16" /> 推广奖励</h3>
        <span class="au-badge au-badge-violet">{{ promotions.length }} 笔</span>
      </header>
      <div v-if="!promotions.length" class="au-empty compact">
        <Sparkles :size="26" />
        <p>
          {{ promoEnabled
            ? '邀请成功后，这里会记下你获得的推广奖励明细'
            : '推广奖励当前未开启；管理员开启后，邀请成功会在这里记一笔' }}
        </p>
      </div>
      <ul v-else class="rows">
        <li v-for="r in promotions" :key="r.id">
          <span class="row-dot promo-dot" />
          <span class="row-main">
            <span class="row-title">成功邀请 {{ r.invitee_username }}</span>
            <span class="row-sub">{{ r.reward_type_label }} · {{ fmtTime(r.created_at) }}</span>
          </span>
          <strong class="row-amt">+{{ r.reward_value }}{{ r.reward_type === 'days' ? ' 天' : ' 积分' }}</strong>
        </li>
      </ul>
    </section>

    <!-- 规则说明：折叠式，避免占视觉主体 -->
    <details v-if="!failed" class="rules au-card au-card-pad au-anim-up" style="animation-delay: 170ms">
      <summary>
        <Gift :size="16" />
        活动规则
        <ChevronDown :size="14" class="rules-chevron" />
      </summary>
      <ol>
        <li>好友通过你的邀请码或链接注册，双方即刻获得积分奖励。</li>
        <li>好友每次充值成功，你将按比例获得返利积分，自动入账。</li>
        <li>积分可在钱包兑换订阅时长或参与其他活动。</li>
        <li>请勿使用邀请码进行刷号等违规操作，违规将取消奖励。</li>
      </ol>
    </details>
  </div>
</template>

<style scoped>
.invite-view { display: flex; flex-direction: column; gap: 1.125rem; }

/* ===== 主卡 ===== */
.invite-hero {
  position: relative;
  overflow: hidden;
  border-radius: var(--au-r-xl);
  border: 1px solid var(--au-border);
  background: var(--au-surface);
  padding: 1.75rem 1.625rem;
}

/* 左右双栏 */
.hero-body {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 1px minmax(0, 1.05fr);
  gap: 1.75rem;
  align-items: center;
}

.hero-divider {
  background: var(--au-border);
  height: 100%;
  min-height: 96px;
}

.hero-left {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  min-width: 0;
}

.hero-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  width: fit-content;
  padding: 0;
  color: var(--au-primary);
  font-size: 0.8125rem;
  font-weight: 600;
  letter-spacing: 0.2em;
}

.hero-left h1 { margin: 0; font-size: 1.75rem; font-weight: 700; line-height: 1.3; }

.hero-desc { margin: 0; font-size: 0.875rem; color: var(--au-text-2); line-height: 1.7; }
.hero-desc strong { color: var(--au-primary); font-weight: 700; }

.code-box {
  display: flex;
  flex-direction: column;
  gap: 0.625rem;
  padding: 1.125rem;
  background: var(--au-bg-soft);
  border: 1px dashed var(--au-border-strong);
  border-radius: var(--au-r-lg);
  min-width: 0;
}

.code-label {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

.code-row { display: flex; align-items: center; gap: 0.625rem; }

.code-value {
  flex: 1;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 1.5rem;
  font-weight: 800;
  letter-spacing: 0.18em;
  color: var(--au-primary);
}

.link-row .link-input { flex: 1; font-size: 0.8125rem; color: var(--au-text-2); }

/* ===== 统计条（单卡分隔式，替代碎片 stat-card 网格） ===== */
.stat-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.875rem 1.25rem;
  flex-wrap: wrap;
}

.stat-cell {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  min-width: 0;
}

.stat-body { display: flex; flex-direction: column; min-width: 0; }

.stat-divider {
  width: 1px;
  height: 28px;
  background: var(--au-border);
  flex-shrink: 0;
}

.stat-icon {
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--au-r-md);
  flex-shrink: 0;
}
/* 暗房影院：四个统计图标同一支琥珀，不再各染一色 */
.stat-icon.c1,
.stat-icon.c2,
.stat-icon.c3,
.stat-icon.c4 { background: var(--au-surface-2); color: var(--au-primary); }

.stat-num {
  font-family: var(--au-font-serif);
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
}
.stat-label { font-size: 0.8125rem; color: var(--au-text-3); }

/* ===== 列表 ===== */
.list-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 1.125rem;
  align-items: start;
}

.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.875rem;
}
.list-head h3 {
  display: flex;
  align-items: center;
  gap: 0.4375rem;
  margin: 0;
  font-size: 0.9375rem;
  font-weight: 700;
  color: var(--au-text);
}
.list-head svg { color: var(--au-primary); }

.rows { list-style: none; margin: 0; padding: 0; }
.rows li {
  display: flex;
  align-items: center;
  gap: 0.6875rem;
  padding: 0.625rem 0;
  border-bottom: 1px solid var(--au-border);
}
.rows li:last-child { border-bottom: none; }

.row-avatar {
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--au-surface-3);
  color: var(--au-text);
  font-size: 0.8125rem;
  font-weight: 700;
  flex-shrink: 0;
}

.row-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--au-success);
  flex-shrink: 0;
  margin: 0 11px;
}

.row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.0625rem; }
.row-title { font-size: 0.8125rem; color: var(--au-text); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-sub { font-size: 0.8125rem; color: var(--au-text-3); }

.row-amt { color: var(--au-success); font-size: 0.875rem; font-variant-numeric: tabular-nums; flex-shrink: 0; }

/* 推广奖励：行首圆点用琥珀，与邀请记录（绿）一眼区分；间距沿用 .row-dot */
.promo-dot { background: var(--au-primary); }

/* ===== 规则（折叠） ===== */
.rules summary {
  display: flex;
  align-items: center;
  gap: 0.4375rem;
  font-size: 0.9375rem;
  font-weight: 700;
  color: var(--au-text);
  cursor: pointer;
  user-select: none;
  list-style: none;
}
.rules summary::-webkit-details-marker { display: none; }
.rules summary svg { color: var(--au-primary); }
.rules-chevron {
  margin-left: auto;
  color: var(--au-text-3);
  transition: transform var(--au-fast) var(--au-ease);
}
.rules[open] .rules-chevron { transform: rotate(180deg); }
.rules ol {
  margin: 0.75rem 0 0;
  padding-left: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.4375rem;
}
.rules li { font-size: 0.8125rem; color: var(--au-text-2); line-height: 1.6; }

.au-empty.compact { padding: 2rem 1rem; }
.au-empty.compact p { margin: 0; font-size: 0.8125rem; }

@media (max-width: 720px) {
  /* 窄屏：三格等分（与个人中心的统计行同一形态），图标与竖线收起，数字居中对齐 */
  .stat-bar {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0;
    padding: 0.875rem 0.5rem;
  }
  .stat-divider,
  .stat-icon { display: none; }
  .stat-cell { justify-content: center; text-align: center; }
  .stat-cell + .stat-divider + .stat-cell { border-left: 1px solid var(--au-border); }
  .stat-body { align-items: center; }
  .hide-sm { display: none; }
  .list-grid { grid-template-columns: minmax(0, 1fr); }
  .hero-body { grid-template-columns: minmax(0, 1fr); gap: 1.25rem; }
  .hero-divider { height: 1px; width: 100%; min-height: 0; }
  .code-row { flex-wrap: wrap; }
  .code-value { font-size: 1.25rem; }
}
</style>