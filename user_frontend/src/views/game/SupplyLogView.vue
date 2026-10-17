<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ facilityStamp('supply') }}</span>
        <h1><span aria-hidden="true">🧾</span> 补给记录</h1>
        <p>补给站的采购小票，一张张钉在布告栏上 —— 点小票看明细。</p>
      </header>

      <!-- 布告栏 -->
      <section class="v4-sec">
        <div class="v4-sec-title"><span aria-hidden="true">📌</span> 布告栏</div>
        <div class="v4-sec-sub">在补给站买过的东西</div>

        <div v-if="loading" class="v4-skeleton" style="height:180px;margin-bottom:20px"></div>
        <div v-else-if="error" class="v4-err">布告栏的小票拿不到：{{ error }}<br><button class="v4-retry" @click="load">重试</button></div>
        <div v-else-if="!orders.length" class="v4-empty"><span aria-hidden="true">🧾</span><br>布告栏空空的 —— 去补给站备点干粮吧 <span aria-hidden="true">🎒</span></div>

        <div v-else class="board">
          <article
            v-for="o in orders"
            :key="o.order_id"
            class="receipt"
            :class="{ open: openId === o.order_id }"
            role="button"
            tabindex="0"
            :aria-expanded="openId === o.order_id"
            :aria-label="`补给小票：${plainText(kindIcon(o.kind))}${o.item_name}，实付${fmtMoney(o.amount)}，${plainText(seal(o.status).label)}。点击${openId === o.order_id ? '收起' : '展开'}明细`"
            @click="toggle(o.order_id)"
            @keydown.enter="toggle(o.order_id)"
            @keydown.space.prevent="toggle(o.order_id)"
          >
            <span class="pin" aria-hidden="true"></span>
            <div class="rc-body">
              <div class="rc-top">
                <div>
                  <div class="rc-item">{{ kindIcon(o.kind) }} {{ o.item_name }}</div>
                  <div class="rc-date">{{ fmtTime(o.created_at) }}</div>
                </div>
                <div class="rc-price">{{ fmtMoney(o.amount) }}</div>
              </div>
              <div class="rc-status">
                <span class="seal" :class="seal(o.status).cls">{{ seal(o.status).label }}</span>
                <span class="rc-hint">{{ openId === o.order_id ? '点小票收起 ▴' : '点小票展开明细 ▾' }}</span>
              </div>
              <div v-if="o.status === 'refunded' && o.refund_reason" class="rc-refund-reason">
                ↩️ 原因：{{ o.refund_reason }}
              </div>
              <div v-if="(o.discount_amount || 0) > 0" class="rc-coupon">
                🎟️ 优惠码 {{ o.coupon_code || '已用券' }} 已减 {{ fmtMoney(o.discount_amount || 0) }}
              </div>
              <div class="rc-detail">
                <div class="kv"><span>小票编号</span><b class="mono">{{ o.order_id }}</b></div>
                <div class="kv"><span>补给内容</span><b>{{ kindIcon(o.kind) }} {{ o.item_name }}</b></div>
                <div class="kv"><span>实付</span><b>{{ fmtMoney(o.amount) }}</b></div>
                <div v-if="(o.discount_amount || 0) > 0" class="kv">
                  <span>原价 / 已减</span>
                  <b><s class="price-was">{{ fmtMoney(o.list_price) }}</s> −{{ fmtMoney(o.discount_amount || 0) }}（{{ o.coupon_code || '已用券' }}）</b>
                </div>
                <div class="kv"><span>下单时间</span><b>{{ fmtTime(o.created_at) }}</b></div>
                <div v-if="o.paid_at" class="kv"><span>支付时间</span><b>{{ fmtTime(o.paid_at) }}</b></div>
                <div v-if="o.refunded_at" class="kv"><span>退款时间</span><b>{{ fmtTime(o.refunded_at) }}</b></div>
                <div v-if="o.status === 'refunded' && o.refund_reason" class="kv"><span>退款原因</span><b>{{ o.refund_reason }}</b></div>
              </div>
              <div class="rc-foot">· 谢谢惠顾 · 欢迎下次再来 ·</div>
            </div>
          </article>
          <!-- R30：api 暂无分页，limit 50 会静默截断，达到上限时诚实告知 -->
          <div v-if="orders.length >= 50" class="rc-foot">· 仅显示最近 50 笔 ·</div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { facilityStamp } from '@/config/game'
import { paymentApi, type OrderRow } from '@/api/economy'
import { humanizeError } from '@/composables/usePanelApi'

const orders = ref<OrderRow[]>([])
const loading = ref(true)
const error = ref('')
const openId = ref<string | null>(null)

/** kind 游戏化图标：recharge → ⚡ 能量补给，subscription → 📜 远征执照，
 *  未知 kind 不再误标"远征执照"，用 📦 通用包裹（aria-label 同步走这里） */
function kindIcon(kind: OrderRow['kind']): string {
  if (kind === 'recharge') return '⚡'
  if (kind === 'subscription') return '📜'
  return '📦'
}

/**
 * 状态章：历史口径（旧 WalletView 的 orderStatusMeta 值域），仅换游戏化展示
 * paid → ✓ 已收货（绿章），pending → 🚚 运输中（蓝章，呼吸闪烁）
 * refunded → ↩️ 已退款（灰章），closed → 已关闭（灰章）
 */
function seal(s: string): { label: string; cls: string } {
  if (s === 'paid') return { label: '✓ 已收货', cls: 'done' }
  if (s === 'pending') return { label: '🚚 运输中', cls: 'ship' }
  if (s === 'refunded') return { label: '↩️ 已退款', cls: 'grey' }
  if (s === 'closed') return { label: '已关闭', cls: 'grey' }
  return { label: '处理中', cls: 'grey' }
}

/** R27：金额可能缺失/非法时显示 '—'，不渲染 ¥NaN */
function fmtMoney(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return '—'
  return `¥${n.toFixed(2)}`
}

function fmtTime(iso?: string | null): string {
  if (!iso) return '—'
  // R41：不再硬切片 ISO —— new Date 解析 + NaN 回退原串（照抄 CompanionView R38 P3-10）
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const pad = (n: number) => (n < 10 ? '0' + n : String(n))
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function toggle(orderId: string) {
  openId.value = openId.value === orderId ? null : orderId
}

// 可访问名只保留文字：读屏会把 ⚡/🚚/✓ 念成「high voltage」「truck」「check mark」
function plainText(text: string) {
  return String(text ?? '').replace(/^[^0-9A-Za-z\u4e00-\u9fff]+/, '').trim()
}

// 连点重试时只有最新一次请求能写回，避免旧响应覆盖新数据、提前关掉 loading
let reqSeq = 0

async function load() {
  const seq = ++reqSeq
  loading.value = true
  error.value = ''
  try {
    const res = await paymentApi.orders({ limit: 50 })
    if (seq !== reqSeq) return
    orders.value = res.orders || []
  } catch (e) {
    if (seq !== reqSeq) return
    error.value = humanizeError(e)
  } finally {
    if (seq === reqSeq) loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
/* 布告栏上的小票：设计稿 game-orders.html 原值 */
.receipt {
  background: var(--paper-card);
  margin-bottom: 22px;
  border-radius: 2px;
  cursor: pointer;
  position: relative;
  min-height: 44px;
  filter: drop-shadow(0 3px 5px rgba(42, 38, 32, 0.15));
}
.receipt::before,
.receipt::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  height: 8px;
  background: linear-gradient(-45deg, transparent 8px, var(--paper-card) 0) 0 0 / 16px 16px repeat-x;
}
.receipt::before { top: -8px; transform: rotate(180deg); }
.receipt::after { bottom: -8px; }
.pin {
  position: absolute;
  top: -7px;
  left: 50%;
  transform: translateX(-50%);
  width: 18px;
  height: 18px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--red);
  box-shadow: 0 2px 0 var(--red-deep, #a33319), inset 0 2px 3px rgba(255, 255, 255, 0.4);
  z-index: 2;
}
.rc-body { padding: 24px 18px 16px; }
.rc-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 10px;
}
.rc-top > div:first-child { min-width: 0; flex: 1; }
.rc-item { font-size: 16px; font-weight: 800; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rc-date { font-size: 12px; color: var(--ink); margin-top: 4px; }
.rc-price { font-size: 18px; font-weight: 800; white-space: nowrap; }
.rc-status {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.seal {
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 2px;
  padding: 5px 12px;
  border-radius: 3px;
  transform: rotate(-3deg);
  display: inline-block;
}
.seal.done { border: 2px solid var(--green); color: var(--green); }
.seal.ship { border: 2px solid var(--blue); color: var(--blue); animation: blink 1.4s infinite; }
.seal.grey { border: 2px solid var(--ink); color: var(--ink); }
@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}
.rc-hint { font-size: 12px; color: var(--ink); }
.rc-refund-reason,
.rc-coupon {
  margin-top: 8px;
  font-size: 12px;
  color: var(--ink2);
}
.rc-detail {
  border-top: 1px dashed var(--line);
  margin-top: 12px;
  padding-top: 12px;
}
.receipt:not(.open) .rc-detail { display: none; }
.receipt.open .rc-detail { animation: fadein 0.3s; }
@keyframes fadein {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
.receipt.open .rc-hint { display: none; }
.kv {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  padding: 7px 0;
  border-bottom: 1px dotted var(--line);
}
.kv:last-child { border-bottom: none; }
.kv span:first-child { color: var(--ink); flex-shrink: 0; }
.kv b { font-weight: 700; text-align: right; min-width: 0; word-break: break-all; }
.kv .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; word-break: break-all; }
.price-was { color: var(--ink); text-decoration: line-through; font-weight: 400; margin-right: 4px; }
.rc-foot {
  margin-top: 12px;
  text-align: center;
  font-size: 11px;
  color: var(--ink);
  letter-spacing: 1px;
}

/* 键盘可达性：重试按钮与 role=button 的小票获得焦点时给出可见轮廓 */
.v4-retry:focus-visible,
.receipt:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* 减少动态偏好：停掉运输中呼吸闪烁和展开渐显；静态造型（pin 居中、锯齿边）保持原样 */
@media (prefers-reduced-motion: reduce) {
  .seal.ship { animation: none; }
  .receipt.open .rc-detail { animation: none; }
}
</style>
