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
.plan-item {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-surface);
  content-visibility: auto;
  contain-intrinsic-size: auto 220px;
}
.plan-item.popular { border-color: var(--au-primary); }
.plan-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.plan-name { font-size: 16px; }
.plan-badge {
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 11px;
  background: var(--au-primary-soft);
  color: var(--au-primary);
}
.plan-price { font-size: 14px; }
.plan-now { font-size: 22px; font-weight: 700; }
.plan-desc, .plan-member-hint { font-size: 12px; color: var(--au-text-3); }
.plan-features { margin: 0; padding-left: 18px; font-size: 13px; }
.plan-buy {
  width: 100%;
  padding: 8px 12px;
  border: none;
  border-radius: var(--au-r-sm);
  background: var(--au-primary);
  color: #fff;
  cursor: pointer;
}
.plan-buy:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
