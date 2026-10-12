<script setup lang="ts">
import { Percent, CircleCheck } from 'lucide-vue-next'

export interface PlanItem {
  id: number | string
  name: string
  is_popular?: boolean
  realm_name?: string
  price: number
  duration_days: number
  description?: string
  features?: string[]
}

defineProps<{
  plan: PlanItem
  wasPrice?: number | string | null
  paidPrice: number | string
  discountPct?: number
  orderLoading?: boolean
  methodsAvailable?: boolean
}>()

const emit = defineEmits<{
  (e: 'order', id: number | string): void
}>()
</script>

<template>
  <article class="plan-item" :class="{ popular: plan.is_popular }">
    <header class="plan-head">
      <strong class="plan-name">{{ plan.name }}</strong>
      <em v-if="plan.is_popular" class="plan-badge">推荐</em>
      <span v-if="plan.realm_name" class="plan-realm">{{ plan.realm_name }}</span>
    </header>
    <div class="plan-price">
      <del v-if="wasPrice"><span class="nowrap">¥{{ wasPrice }}</span></del>
      <span class="plan-now"><span class="nowrap">¥{{ paidPrice }}</span></span> / <span class="nowrap">{{ plan.duration_days }} 天</span>
    </div>
    <p v-if="plan.description" class="plan-desc">{{ plan.description }}</p>
    <p v-if="(discountPct || 0) > 0" class="plan-member-hint">
      <Percent :size="12" /> 会员 {{ (100 - (discountPct || 0)) / 10 }} 折，下单自动抵扣
    </p>
    <ul v-if="plan.features && plan.features.length" class="plan-features">
      <li v-for="(f, i) in plan.features" :key="i"><CircleCheck /> {{ f }}</li>
    </ul>
    <div class="plan-actions">
      <button
        type="button"
        class="plan-buy"
        :disabled="orderLoading || !methodsAvailable"
        @click="emit('order', plan.id)"
      >
        <span v-if="orderLoading" class="spinner"></span>
        <template v-else><span class="nowrap">¥{{ paidPrice }}</span> 开通</template>
      </button>
    </div>
  </article>
</template>

<style scoped>
/* P2-28：套餐卡交互组——hover 上浮 -2px / popular 琥珀辉光 / transition（旧第 1580-1606 行全文，背景 surface-2） */
.plan-item {
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  padding: 20px;
  background: var(--au-surface-2);
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  position: relative;
  transition:
    transform 0.12s cubic-bezier(0.34, 1.56, 0.64, 1),
    border-color var(--au-fast) var(--au-ease),
    box-shadow 0.15s ease-out;
  content-visibility: auto;
  contain-intrinsic-size: auto 220px;
}
.plan-item:hover {
  border-color: var(--au-border-strong);
  transform: translateY(-2px);
}
.plan-item.popular {
  border: 1px solid var(--au-primary-border);
  box-shadow: 0 0 12px var(--au-primary-border);
}
.plan-item.popular:hover {
  box-shadow: 0 0 12px var(--au-primary-border);
}
/* P3-6：header 默认外边距清零（旧第 1607-1612 行） */
.plan-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 0; }
/* P3-5：650 字重 + 单行省略（旧第 1614-1621 行） */
.plan-name {
  font-size: 16px;
  font-weight: 650;
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
/* P3-4：「推荐」徽章——正体大写字母间距（旧第 1623-1632 行；em 默认斜体必须 font-style: normal） */
.plan-badge {
  font-style: normal;
  font-size: 11px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--au-primary-soft);
  color: var(--au-primary);
  white-space: nowrap;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.plan-realm { font-size: 12px; color: var(--au-text-3); white-space: nowrap; }
.plan-price { font-size: 14px; }
/* P3-3：衬线大字价格（旧第 1653-1660 行） */
.plan-now {
  font-family: var(--au-font-serif);
  font-size: 28px;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  letter-spacing: 0.01em;
}
.plan-desc, .plan-member-hint { font-size: 12px; color: var(--au-text-3); }
.plan-features { margin: 0; padding-left: 18px; font-size: 13px; }
.nowrap { white-space: nowrap; }
/* P2-29 + P3-2：购买主按钮——文字色走 --au-on-primary、44px 最小触控高、
   hover 加深 + 辉光、active 按压回弹（旧第 1720-1746 行） */
.plan-buy {
  width: 100%;
  padding: 8px 12px;
  border: none;
  border-radius: var(--au-r-sm);
  background: var(--au-primary);
  color: var(--au-on-primary);
  min-height: 44px;
  cursor: pointer;
}
.plan-buy:hover {
  background: var(--au-primary-strong);
  box-shadow: 0 0 12px var(--au-primary-border);
}
.plan-buy:active { transform: scale(0.97); }
.plan-buy:disabled { opacity: 0.6; cursor: not-allowed; }
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
</style>
