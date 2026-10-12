<script setup lang="ts">
import { Coins, CircleAlert, Sparkles, ExternalLink } from 'lucide-vue-next'
import EmptyState from '@/components/EmptyState.vue'

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
  wasPrice: (type: string, id: any) => any
  paidPrice: (type: string, id: any, price: any) => any
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
.store-card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
.pkg-list { display: flex; flex-direction: column; gap: 8px; }
.pkg-row { display: flex; align-items: center; gap: 12px; padding: 10px; border: 1px solid var(--au-border); border-radius: 8px; background: transparent; cursor: pointer; text-align: left; }
.pkg-row.popular { border-color: var(--au-primary); }
.pkg-info { flex: 1; display: flex; flex-direction: column; gap: 2px; }
.pkg-price { text-align: right; }
.pkg-now { font-size: 18px; font-weight: 700; }
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
