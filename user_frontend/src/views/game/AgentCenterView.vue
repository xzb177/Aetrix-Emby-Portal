<template>
  <div class="v4-scope">
    <div class="v4-page">
      <header class="v4-loghead">
        <h1>代理中心</h1>
        <p>用进货余额按你的等级折扣进货卡码，卖出后销售自动记在你名下。</p>
      </header>

      <div v-if="loading" class="v4-skeleton" style="height:160px;border-radius:14px"></div>
      <div v-else-if="error" class="v4-err">
        {{ error }}<br>
        <button class="v4-retry" @click="reload">重试</button>
      </div>

      <template v-else-if="profile">
        <!-- 概览 -->
        <section class="v4-sec" aria-label="代理概览">
          <div class="v4-card agent-cards">
            <div class="agent-stat">
              <div class="agent-stat-lbl">我的等级</div>
              <div class="agent-stat-num">{{ profile.level_name }}</div>
              <div class="agent-stat-sub">{{ rateText }}进货</div>
            </div>
            <div class="agent-stat">
              <div class="agent-stat-lbl">进货余额</div>
              <div class="agent-stat-num">¥{{ money(profile.balance) }}</div>
              <div class="agent-stat-sub">余额不足请联系管理员补充</div>
            </div>
            <div class="agent-stat">
              <div class="agent-stat-lbl">累计销售</div>
              <div class="agent-stat-num">{{ stats?.summary.sales_count ?? 0 }} 单</div>
              <div class="agent-stat-sub">面值 ¥{{ money(stats?.summary.sales_face_total ?? 0) }}</div>
            </div>
            <div class="agent-stat">
              <div class="agent-stat-lbl">累计进货</div>
              <div class="agent-stat-num">{{ stats?.summary.purchase_quantity ?? 0 }} 张</div>
              <div class="agent-stat-sub">花费 ¥{{ money(stats?.summary.purchase_cost_total ?? 0) }}</div>
            </div>
          </div>
        </section>

        <!-- 进货 -->
        <section class="v4-sec" aria-label="进货">
          <div class="v4-sec-title">进货</div>
          <div class="v4-sec-sub">选套餐和数量，生成后卡码归你所有；用户核销时自动算作你的销售。</div>
          <div v-if="!products.length" class="v4-empty">暂无可进货的套餐</div>
          <div v-else class="v4-card">
            <div v-for="p in products" :key="p.plan_id" class="acct-row">
              <span class="acct-row-main">
                <span class="acct-row-text">{{ p.name }}（{{ p.duration_days }} 天）</span>
                <span class="acct-row-meta">面值 ¥{{ money(p.face_value) }} · 你的进货价 ¥{{ money(p.unit_cost) }}</span>
              </span>
              <button type="button" class="v4-mini-btn" @click="openBuy(p)">进货</button>
            </div>
          </div>
        </section>

        <!-- 我的卡码 -->
        <section class="v4-sec" aria-label="我的卡码">
          <div class="v4-sec-title">我的卡码</div>
          <div v-if="!codes.length" class="v4-empty">还没有进过货</div>
          <div v-else class="v4-card">
            <div v-for="c in codes" :key="c.id" class="acct-row">
              <span class="acct-row-main">
                <span class="acct-row-text mono">{{ c.code }}</span>
                <span class="acct-row-meta">{{ c.code_type_name }} · {{ c.days_text }} · {{ stateText(c.state) }}</span>
              </span>
              <button type="button" class="v4-mini-btn" @click="copyCode(c.code)">复制</button>
            </div>
          </div>
        </section>

        <!-- 销售统计 -->
        <section class="v4-sec" aria-label="销售统计">
          <div class="v4-sec-title">销售统计</div>
          <div v-if="!sales.length" class="v4-empty">还没有销售记录</div>
          <div v-else class="v4-card">
            <div v-for="s in sales" :key="s.id" class="acct-row">
              <span class="acct-row-main">
                <span class="acct-row-text mono">{{ s.code }}</span>
                <span class="acct-row-meta">{{ s.buyer_username }} 购买 · {{ fmtTime(s.created_at) }}</span>
              </span>
              <span class="acct-row-meta">面值 ¥{{ money(s.face_value) }} · 毛利 ¥{{ money(s.face_value - s.unit_cost) }}</span>
            </div>
          </div>
          <div v-if="stats && stats.monthly.some(m => m.count > 0)" class="v4-card" style="margin-top:10px">
            <div v-for="m in [...stats.monthly].reverse()" :key="m.period" class="acct-row">
              <span class="acct-row-main">
                <span class="acct-row-text">{{ m.period }}</span>
                <span class="acct-row-meta">{{ m.count }} 单</span>
              </span>
              <span class="acct-row-meta">¥{{ money(m.face_total) }}</span>
            </div>
          </div>
        </section>

        <!-- 余额流水 -->
        <section class="v4-sec" aria-label="余额流水">
          <div class="v4-sec-title">余额流水</div>
          <div v-if="!logs.length" class="v4-empty">还没有流水</div>
          <div v-else class="v4-card">
            <div v-for="l in logs" :key="l.id" class="acct-row">
              <span class="acct-row-main">
                <span class="acct-row-text">{{ l.kind === 'purchase' ? '进货扣款' : '管理员调整' }}{{ l.note ? ` · ${l.note}` : '' }}</span>
                <span class="acct-row-meta">{{ fmtTime(l.created_at) }} · 变动后余额 ¥{{ money(l.balance_after) }}</span>
              </span>
              <span class="acct-row-meta">{{ l.amount >= 0 ? '+' : '' }}¥{{ money(l.amount) }}</span>
            </div>
          </div>
        </section>
      </template>

      <!-- 进货弹窗 -->
      <div v-if="buyTarget" class="agent-modal-mask" @click.self="buyTarget = null">
        <div class="agent-modal" role="dialog" aria-modal="true" aria-label="确认进货">
          <div class="v4-sec-title">进货：{{ buyTarget.name }}</div>
          <div class="v4-sec-sub">
            面值 ¥{{ money(buyTarget.face_value) }} · 进货价 ¥{{ money(buyTarget.unit_cost) }} / 张
          </div>
          <label class="agent-field">
            <span>卡码类型</span>
            <select v-model.number="buyForm.code_type">
              <option :value="1">注册码（给新用户开通）</option>
              <option :value="2">续期码（给老用户续期）</option>
            </select>
          </label>
          <label class="agent-field">
            <span>数量</span>
            <input v-model.number="buyForm.quantity" type="number" min="1" max="100">
          </label>
          <div class="agent-total">
            合计扣款 ¥{{ money(buyTarget.unit_cost * (buyForm.quantity || 0)) }}
            （余额 ¥{{ money(profile?.balance ?? 0) }}）
          </div>
          <div v-if="buyError" class="v4-err">{{ buyError }}</div>
          <div v-if="boughtCodes.length" class="bought">
            <div class="v4-sec-sub">已生成，点任意一张复制：</div>
            <button
              v-for="c in boughtCodes"
              :key="c.id"
              type="button"
              class="bought-code mono"
              @click="copyCode(c.code)"
            >{{ c.code }}</button>
          </div>
          <div class="agent-modal-actions">
            <button type="button" class="v4-mini-btn" @click="buyTarget = null">关闭</button>
            <button
              type="button"
              class="v4-mini-btn primary"
              :disabled="buying || !!boughtCodes.length"
              @click="doBuy"
            >{{ buying ? '生成中…' : '确认进货' }}</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useToast } from '@/composables/useToast'
import {
  getAgentCodes,
  getAgentLedger,
  getAgentMe,
  getAgentProducts,
  getAgentSales,
  getAgentStats,
  purchaseAgentCodes,
  type AgentCodeRow,
  type AgentLogRow,
  type AgentProduct,
  type AgentProfile,
  type AgentSaleRow,
  type AgentStats,
} from '@/api/agent'
import { toUserMessage } from '@/utils/toUserMessage'

const toast = useToast()
const loading = ref(true)
const error = ref('')
const profile = ref<AgentProfile | null>(null)
const products = ref<AgentProduct[]>([])
const codes = ref<AgentCodeRow[]>([])
const logs = ref<AgentLogRow[]>([])
const sales = ref<AgentSaleRow[]>([])
const stats = ref<AgentStats | null>(null)

const money = (n: number) => (n ?? 0).toFixed(2)
const rateText = computed(() => {
  const r = profile.value?.discount_rate ?? 0
  return `${(r * 10).toFixed(1).replace(/\.0$/, '')}折`
})

function fmtTime(v: string | null) {
  return v ? v.replace('T', ' ').slice(0, 16) : ''
}

function stateText(state: string) {
  return { active: '可售', used_up: '已售出', disabled: '已停用', expired: '已过期' }[state] ?? state
}

async function reload() {
  loading.value = true
  error.value = ''
  try {
    const me = await getAgentMe()
    if (!me.is_agent || !me.agent) {
      error.value = '你还不是代理，暂无权访问代理中心'
      profile.value = null
      return
    }
    profile.value = me.agent
    const [p, c, l, s, st] = await Promise.all([
      getAgentProducts(),
      getAgentCodes(50, 0),
      getAgentLedger(50, 0),
      getAgentSales(50, 0),
      getAgentStats(),
    ])
    products.value = p
    codes.value = c.codes
    logs.value = l.logs
    sales.value = s.sales
    stats.value = st
  } catch (e) {
    error.value = toUserMessage(e, 'loadFailed')
  } finally {
    loading.value = false
  }
}

async function copyCode(code: string) {
  try {
    await navigator.clipboard.writeText(code)
    toast.success('卡码已复制')
  } catch {
    toast.error('复制失败，请长按手动复制')
  }
}

// ===== 进货 =====
const buyTarget = ref<AgentProduct | null>(null)
const buyForm = ref({ code_type: 1, quantity: 1 })
const buying = ref(false)
const buyError = ref('')
const boughtCodes = ref<{ id: number; code: string }[]>([])

function openBuy(p: AgentProduct) {
  buyTarget.value = p
  buyForm.value = { code_type: 1, quantity: 1 }
  buyError.value = ''
  boughtCodes.value = []
}

async function doBuy() {
  if (!buyTarget.value) return
  buying.value = true
  buyError.value = ''
  try {
    const res = await purchaseAgentCodes({
      plan_id: buyTarget.value.plan_id,
      code_type: buyForm.value.code_type,
      quantity: buyForm.value.quantity,
    })
    boughtCodes.value = res.codes
    toast.success(`进货成功，扣款 ¥${money(res.purchase.total_cost)}`)
    // 刷新余额/卡码/流水（弹窗保持打开看新码，关掉后数据已是新的）
    const me = await getAgentMe()
    profile.value = me.agent
    const [c, l, st] = await Promise.all([
      getAgentCodes(50, 0), getAgentLedger(50, 0), getAgentStats(),
    ])
    codes.value = c.codes
    logs.value = l.logs
    stats.value = st
  } catch (e) {
    buyError.value = toUserMessage(e, 'saveFailed')
  } finally {
    buying.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.agent-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
  padding: 14px;
}
.agent-stat-lbl { font-size: 12px; opacity: .65; }
.agent-stat-num { font-size: 20px; font-weight: 700; margin: 4px 0 2px; }
.agent-stat-sub { font-size: 12px; opacity: .65; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.agent-modal-mask {
  position: fixed; inset: 0; z-index: 60;
  background: rgba(0, 0, 0, .45);
  display: flex; align-items: center; justify-content: center;
  padding: 16px;
}
.agent-modal {
  background: var(--v4-card-bg, #fff);
  border-radius: 14px;
  padding: 20px;
  width: min(440px, 94vw);
  max-height: 88vh;
  overflow: auto;
}
.agent-field { display: block; margin: 12px 0; font-size: 13px; }
.agent-field span { display: block; margin-bottom: 6px; opacity: .75; }
.agent-field select,
.agent-field input {
  width: 100%; padding: 8px 10px; border-radius: 8px;
  border: 1px solid rgba(128, 128, 128, .35);
  background: transparent; color: inherit;
}
.agent-total { font-weight: 600; margin: 10px 0; }
.agent-modal-actions {
  display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px;
}
.bought { margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }
.bought-code {
  text-align: left; padding: 8px 10px; border-radius: 8px;
  border: 1px dashed rgba(128, 128, 128, .5); background: transparent;
  color: inherit; cursor: pointer;
}
.v4-mini-btn.primary { font-weight: 700; }
</style>
