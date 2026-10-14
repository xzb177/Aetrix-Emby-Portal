<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { Crown, Zap, Clapperboard } from 'lucide-vue-next'
import type { MemberCardInfo, EnergyCardInfo, WatchStripInfo } from '@/composables/useTicketCard'
import './ticket-card.css'

const props = defineProps<{
  loading: boolean
  memberCard?: MemberCardInfo
  energyCard?: EnergyCardInfo
  watchStrip?: WatchStripInfo
}>()

const defaultMemberCard: MemberCardInfo = {
  value: '—',
  unit: '',
  progress: null,
  planName: '',
  expiryDate: '',
  desc: '—',
  badge: null,
  hot: false,
  expiringSoon: false,
  footer: '查看套餐',
  to: '/store',
  state: 'unsubscribed',
}

const defaultEnergyCard: EnergyCardInfo = {
  balance: '—',
  vitality: null,
  vitalityMax: null,
  vitalityText: '—',
  vitalityPct: 0,
  isVitalityLow: false,
  hasVitality: false,
  badge: null,
  hot: false,
  checkinEnabled: true,
  checkedToday: false,
  streak: null,
  cardTo: '/game/ledger',
  buttonText: '去补给站',
  buttonTo: '/shop',
}

const defaultWatchStrip: WatchStripInfo = {
  watchValue: '—',
  watchUnit: '',
  watchTime: '—',
  totalPlays: '—',
  watchedItems: '—',
}

const mc = computed(() => props.memberCard ?? defaultMemberCard)
const ec = computed(() => props.energyCard ?? defaultEnergyCard)
const ws = computed(() => props.watchStrip ?? defaultWatchStrip)

/** 进度钳位到 0~100 */
function clampProgress(p: number | null): number {
  if (p === null || !Number.isFinite(p)) return 0
  return Math.min(100, Math.max(0, p))
}
</script>

<template>
  <!-- 加载中：纸色骨架屏 -->
  <section v-if="loading" class="hc-wrap" aria-hidden="true">
    <!-- 会员卡骨架 -->
    <div class="hc-card hc-sk-card">
      <div class="hc-header">
        <span class="v4-skeleton hc-sk-title"></span>
        <span class="v4-skeleton hc-sk-badge"></span>
      </div>
      <div class="hc-main-row">
        <span class="v4-skeleton hc-sk-val"></span>
        <span class="v4-skeleton hc-sk-btn"></span>
      </div>
      <span class="v4-skeleton hc-sk-progress"></span>
      <span class="v4-skeleton hc-sk-desc"></span>
    </div>

    <!-- 能量卡骨架 -->
    <div class="hc-card hc-sk-card">
      <div class="hc-header">
        <span class="v4-skeleton hc-sk-title"></span>
        <span class="v4-skeleton hc-sk-badge"></span>
      </div>
      <div class="hc-main-row">
        <span class="v4-skeleton hc-sk-val"></span>
        <span class="v4-skeleton hc-sk-btn"></span>
      </div>
      <div class="hc-vitality">
        <span class="v4-skeleton hc-sk-vit-head"></span>
        <span class="v4-skeleton hc-sk-progress"></span>
      </div>
    </div>

    <!-- 观影数据小条骨架 -->
    <div class="hc-watch-strip">
      <span class="v4-skeleton hc-sk-watch-line"></span>
    </div>
  </section>

  <!-- 真实内容：两张独立卡片 + 观影数据小条 -->
  <section v-else class="hc-wrap" aria-label="首页概览卡片">
    <!-- 1. 会员卡 -->
    <div class="hc-card" :class="{ 'is-warning': mc.expiringSoon }">
      <div class="hc-header">
        <div class="hc-title">
          <Crown :size="15" class="hc-title-icon" />
          <span>会员</span>
        </div>
        <span
          v-if="mc.badge"
          class="hc-badge"
          :class="{ 'is-warning': mc.expiringSoon }"
        >
          {{ mc.badge }}
        </span>
      </div>

      <div class="hc-main-row">
        <div class="hc-val-group">
          <span class="hc-val">{{ mc.value }}</span>
          <span v-if="mc.unit" class="hc-unit">{{ mc.unit }}</span>
        </div>
        <RouterLink :to="mc.to" class="hc-btn">
          {{ mc.footer }}
        </RouterLink>
      </div>

      <!-- 会员周期进度条（仅会员有效且有进度时展示） -->
      <div
        v-if="mc.progress !== null"
        class="hc-progress-track"
        role="progressbar"
        :aria-valuenow="clampProgress(mc.progress)"
        :aria-label="`会员周期已过 ${clampProgress(mc.progress)}%`"
      >
        <span
          class="hc-progress-bar"
          :class="{ 'is-warning': mc.expiringSoon }"
          :style="{ transform: `scaleX(${clampProgress(mc.progress) / 100})` }"
        ></span>
      </div>

      <!-- 套餐描述 / 到期日 / 公益服文案 / 未开通引导 -->
      <div class="hc-desc">{{ mc.desc }}</div>
    </div>

    <!-- 2. 能量卡 -->
    <div class="hc-card">
      <div class="hc-header">
        <RouterLink :to="ec.cardTo" class="hc-title hc-title-link">
          <Zap :size="15" class="hc-title-icon" />
          <span>能量</span>
        </RouterLink>
        <span
          v-if="ec.badge"
          class="hc-badge"
          :class="{ hot: ec.hot }"
        >
          {{ ec.badge }}
        </span>
      </div>

      <div class="hc-main-row">
        <RouterLink :to="ec.cardTo" class="hc-val-group hc-val-link">
          <span class="hc-val">{{ ec.balance }}</span>
        </RouterLink>
        <RouterLink :to="ec.buttonTo" class="hc-btn">
          {{ ec.buttonText }}
        </RouterLink>
      </div>

      <!-- 体力模块：体力值 + 体力条 -->
      <div class="hc-vitality">
        <div class="hc-vit-meta" :class="{ 'is-low': ec.isVitalityLow }">
          <span>体力</span>
          <span class="hc-vit-val-wrap">
            <span class="hc-vit-nums">{{ ec.vitalityText }}</span>
            <span v-if="ec.isVitalityLow" class="hc-vit-warn">体力低</span>
          </span>
        </div>
        <div
          class="hc-progress-track"
          role="progressbar"
          :aria-valuenow="clampProgress(ec.vitalityPct)"
          :aria-label="`体力 ${clampProgress(ec.vitalityPct)}%`"
        >
          <span
            class="hc-progress-bar"
            :class="{ 'is-warning': ec.isVitalityLow }"
            :style="{ transform: `scaleX(${clampProgress(ec.vitalityPct) / 100})` }"
          ></span>
        </div>
      </div>
    </div>

    <!-- 3. 观影数据小条（独立虚线边框小条） -->
    <div class="hc-watch-strip">
      <Clapperboard :size="13" class="hc-watch-icon" />
      <span class="hc-watch-text">
        累计观影 <b>{{ ws.watchTime }}</b> · 播放 <b>{{ ws.totalPlays }}</b> 次 · 看过 <b>{{ ws.watchedItems }}</b> 部
      </span>
    </div>
  </section>
</template>
