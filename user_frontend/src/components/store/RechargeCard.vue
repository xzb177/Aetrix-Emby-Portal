<script setup lang="ts">
import { Coins, CircleAlert, Sparkles, ExternalLink } from 'lucide-vue-next'
import EmptyState from '@/components/ui/EmptyState.vue'

defineProps<{
  rechargeEnabled: boolean
  loading?: boolean
  packages: any[]
  rechargeRatio: number
  quickAmounts: number[]
  customAmount: number
  canCustomRecharge?: boolean
  orderLoading?: number | string | null
  methods: { id: string; name: string }[]
  payMethod?: string
  customPointsPreview?: string | number
  wasPrice: (type: 'recharge' | 'subscription', id: number) => string
  paidPrice: (type: 'recharge' | 'subscription', id: number, price: number) => string
}>()

const emit = defineEmits<{
  (e: 'order', type: string, id: any): void
  (e: 'custom'): void
  (e: 'update:customAmount', v: number): void
  (e: 'update:payMethod', v: string): void
}>()
</script>

<template>
  <section id="card-recharge" class="store-card au-anim-up">
    <header class="store-card-head">
      <Coins class="store-card-icon" />
      <h2>充积分</h2>
      <small>用人民币购买积分</small>
      <span class="rate-badge"><span class="nowrap">1&nbsp;元</span><span class="rate-eq">=</span><span class="nowrap">{{ rechargeRatio }}&nbsp;积分</span></span>
    </header>

    <div v-if="!rechargeEnabled" class="store-empty">
      <CircleAlert />
      <p>充值通道暂未开启，可先通过签到、邀请或兑换获取积分</p>
    </div>
    <div v-else-if="loading && !packages.length" class="store-skeleton">
      <div v-for="i in 3" :key="i" class="store-skel-row">
        <span class="store-skel-points"></span>
        <span class="store-skel-price"></span>
      </div>
    </div>
    <EmptyState v-else-if="!packages.length" title="暂时没有充值套餐" description="可购买的充值套餐会显示在这里" />

    <template v-else>
      <div class="pkg-list">
        <button
          v-for="p in packages"
          :key="p.id"
          type="button"
          class="pkg-row"
          :class="{ popular: p.is_popular }"
          :disabled="orderLoading === p.id || !methods.length"
          @click="emit('order', 'recharge', p.id)"
        >
          <span class="pkg-info">
            <span class="nowrap"><strong>{{ p.total_points }}</strong> 积分</span>
            <span class="pkg-name">{{ p.name }}</span>
            <span v-if="p.bonus > 0" class="pkg-bonus"><Sparkles /> 含赠送 <span class="nowrap">{{ p.bonus }} 积分</span></span>
          </span>
          <span class="pkg-price">
            <del v-if="wasPrice('recharge', p.id)"><span class="nowrap">¥{{ wasPrice('recharge', p.id) }}</span></del>
            <span class="pkg-now"><span class="nowrap">¥{{ paidPrice('recharge', p.id, p.price) }}</span></span>
            <em v-if="p.is_popular" class="pkg-badge">超值</em>
          </span>
          <span class="pkg-action">
            <span v-if="orderLoading === p.id" class="spinner"></span>
            <template v-else>购买 <ExternalLink /></template>
          </span>
        </button>
      </div>

      <div class="recharge-amount">
        <span class="sec-label">充值金额</span>
        <div v-if="quickAmounts.length" class="store-quick">
          <div class="store-quick-chips">
            <button
              v-for="amt in quickAmounts"
              :key="amt"
              type="button"
              class="quick-chip"
              :class="{ active: customAmount === amt }"
              @click="emit('update:customAmount', amt)"
            ><span class="nowrap">¥{{ amt }}</span></button>
          </div>
        </div>
        <div class="store-custom">
          <div class="store-custom-row">
            <span class="store-custom-currency">¥</span>
            <input
              :value="customAmount"
              type="number"
              min="1"
              max="100000"
              placeholder="输入金额"
              class="store-custom-input"
              @input="emit('update:customAmount', Number(($event.target as HTMLInputElement).value))"
            />
            <button
              type="button"
              class="store-custom-btn"
              :disabled="!canCustomRecharge || orderLoading === -1 || !methods.length"
              @click="emit('custom')"
            >
              <span v-if="orderLoading === -1" class="spinner"></span>
              <template v-else>充值<span class="nowrap">{{ customPointsPreview }}积分</span></template>
            </button>
          </div>
          <small class="store-custom-note">按当前比例 <span class="nowrap">{{ rechargeRatio }}</span> 兑换</small>
        </div>
      </div>

      <div v-if="methods.length" class="store-pay">
        <span class="sec-label">支付方式</span>
        <div class="store-pay-group">
          <button
            v-for="m in methods"
            :key="m.id"
            type="button"
            class="store-pay-btn"
            :class="{ active: payMethod === m.id }"
            @click="emit('update:payMethod', m.id)"
          >{{ m.name }}</button>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.store-card { content-visibility: auto; contain-intrinsic-size: auto 320px; padding: 16px; border: 1px solid var(--au-border); border-radius: var(--au-r-md); background: var(--au-surface); }
/* P3-15：卡头排版组——22px 加粗标题 + 副标题省略 + 18px 图标（旧 StoreView.vue 第 1089-1116 行） */
.store-card-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 12px; }
.store-card-head h2 {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 0.01em;
  color: var(--au-text);
  white-space: nowrap;
}
.store-card-head small {
  font-size: 13px;
  color: var(--au-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.store-card-icon { width: 18px; height: 18px; color: var(--au-text-2); }
/* P2-15：「1 元 = X 积分」琥珀徽章（含 hover 辉光，旧第 1118-1144 行全文） */
.rate-badge {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  border-radius: var(--au-r-full);
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  color: var(--au-primary);
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
  align-self: center;
  transition: box-shadow 0.15s ease-out;
}
.rate-badge:hover { box-shadow: 0 0 8px var(--au-primary-border); }
.rate-eq { color: var(--au-primary); font-weight: 700; }
.nowrap { white-space: nowrap; }
/* P2-17：充值通道关闭空状态（旧第 2105-2114 行） */
.store-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 32px 16px;
  color: var(--au-text-3);
  font-size: 13px;
  text-align: center;
}
.store-empty svg { width: 28px; height: 28px; opacity: .5; }
/* P2-16：骨架屏整组 + shimmer（旧第 2065-2103 行） */
.store-skeleton { display: flex; flex-direction: column; gap: 12px; }
.store-skel-row {
  display: flex;
  align-items: center;
  padding: 16px;
  border-radius: var(--au-r-lg);
  background: var(--au-surface-2);
}
.store-skel-points,
.store-skel-price {
  background: linear-gradient(90deg, var(--au-track) 25%, var(--au-primary-soft) 50%, var(--au-track) 75%);
  background-size: 200% 100%;
  animation: store-shimmer 1.2s linear infinite;
}
.store-skel-points { width: 120px; height: 22px; border-radius: 4px; }
.store-skel-price { width: 80px; height: 18px; border-radius: 4px; margin-left: auto; }
@keyframes store-shimmer {
  from { background-position: 200% 0; }
  to { background-position: -200% 0; }
}
.pkg-list { display: flex; flex-direction: column; gap: 8px; }
/* P2-19：套餐行整组伪类——hover 上浮 / 「超值」左侧金条+琥珀辉光 / 按压回弹 / disabled / font:inherit
   （旧第 1173-1221 行全文，选择器 button.pkg-row 改为 .pkg-row） */
.pkg-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 16px;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  cursor: pointer;
  text-align: left;
  font: inherit;
  transition:
    transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1),
    border-color var(--au-fast) var(--au-ease),
    box-shadow 0.15s ease-out;
}
.pkg-row:hover {
  border-color: var(--au-border-strong);
  transform: translateY(-1px);
  box-shadow: 0 0 12px var(--au-primary-border);
}
.pkg-row.popular:hover {
  box-shadow:
    inset 2px 0 0 var(--au-gold-a),
    0 0 12px var(--au-primary-border);
}
.pkg-row:not(.popular):hover {
  box-shadow: 0 0 12px var(--au-primary-border);
}
.pkg-row:active { transform: scale(0.98); }
.pkg-row:disabled { opacity: 0.55; cursor: default; }
.pkg-row.popular {
  border: 1px solid var(--au-primary-border);
  box-shadow:
    inset 2px 0 0 var(--au-gold-a),
    0 0 12px var(--au-primary-border);
}
/* P3-14：行内排版组——基线对齐 / 200px 截断 / 价格区 / 原价删除线（旧第 1225-1289 行） */
.pkg-info {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.pkg-info strong {
  font-size: 22px;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  font-family: var(--au-font-serif);
}
.pkg-name {
  font-size: 13px;
  color: var(--au-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 200px;
}
.pkg-info span.nowrap { font-size: 13px; color: var(--au-text-3); }
.pkg-price {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-shrink: 0;
}
.pkg-price del { font-size: 13px; color: var(--au-text-3); }
.pkg-now {
  font-size: 17px;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
/* P2-20：金色体系——「超值」徽章 / 含赠送 / 购买（旧第 1291-1313 行全文） */
.pkg-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: var(--au-r-full);
  color: var(--au-gold-b);
  border: 1px solid var(--au-gold-edge);
  white-space: nowrap;
}
.pkg-bonus {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--au-gold-b);
  white-space: nowrap;
}
.pkg-bonus svg { width: 11px; height: 11px; }
.pkg-action {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--au-primary);
  white-space: nowrap;
  flex-shrink: 0;
  min-width: 52px;
}
.pkg-action svg { width: 12px; height: 12px; }
/* P2-21：分区小标签（旧第 1052-1059 行，去掉 .store-page 前缀） */
.sec-label {
  display: block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--au-text-3);
  margin: 0 0 10px;
}
/* P2-18：「充值金额」分组卡片容器（旧第 1323-1330 行） */
.recharge-amount {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  background: var(--au-surface-2);
}
/* P3-1：14px 小版 spinner（旧第 2122-2131 行；scoped 覆盖全局 20px 版） */
.spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  border-radius: 50%;
  border: 2px solid var(--au-border-strong);
  border-top-color: var(--au-primary);
  animation: store-spin .7s linear infinite;
  vertical-align: -2px;
}
@keyframes store-spin {
  to { transform: rotate(360deg); }
}
.store-quick-chips { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
.quick-chip { padding: 4px 10px; border-radius: 999px; border: 1px solid var(--au-border); background: transparent; cursor: pointer; }
.quick-chip.active { border-color: var(--au-primary); color: var(--au-primary); }
.store-custom-row { display: flex; align-items: center; gap: 8px; }
.store-custom-input { flex: 1; padding: 8px; border: 1px solid var(--au-border); border-radius: 6px; }
.store-custom-btn { padding: 8px 14px; border: none; border-radius: 6px; background: var(--au-primary); color: #fff; cursor: pointer; }
.store-pay-group { display: flex; gap: 8px; flex-wrap: wrap; }
.store-pay-btn { padding: 6px 12px; border: 1px solid var(--au-border); border-radius: 6px; background: transparent; cursor: pointer; }
.store-pay-btn.active { border-color: var(--au-primary); color: var(--au-primary); }
</style>
