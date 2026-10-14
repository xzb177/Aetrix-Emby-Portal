<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
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
    <!-- 付费服卡骨架 -->
    <div class="hc-card hc-sk-card">
      <div class="hc-header">
        <div class="v4-skeleton hc-sk-title"></div>
        <div class="v4-skeleton hc-sk-badge"></div>
      </div>
      <div class="hc-main-row">
        <div class="v4-skeleton hc-sk-val"></div>
      </div>
      <div class="v4-skeleton hc-sk-progress"></div>
      <div class="v4-skeleton hc-sk-desc"></div>
    </div>

    <!-- 公益服卡骨架 -->
    <div class="hc-card hc-sk-card">
      <div class="hc-header">
        <span class="hc-sk-title v4-skeleton"></span>
        <span class="hc-sk-badge v4-skeleton"></span>
      </div>
      <div class="hc-main-row">
        <span class="hc-sk-val v4-skeleton"></span>
      </div>
      <div class="hc-chips">
        <span class="hc-sk-desc v4-skeleton"></span>
        <span class="hc-sk-desc v4-skeleton"></span>
        <span class="hc-sk-desc v4-skeleton"></span>
      </div>
    </div>

    <!-- 观影数据小条骨架 -->
    <div class="hc-watch-strip">
      <span class="hc-sk-watch-line v4-skeleton"></span>
    </div>
  </section>

  <!-- 真实内容：付费服卡 + 公益服卡 + 观影数据小条（效果图定案版一比一） -->
  <section v-else class="hc-wrap" aria-label="首页概览卡片">
    <!-- 1. 付费服卡 -->
    <div class="hc-card" :class="{ 'is-warning': mc.expiringSoon }">
      <div class="hc-header">
        <span class="hc-title">付费服</span>
        <span
          class="hc-badge"
          :class="{
            'is-amber': mc.badge === '生效中',
            'is-warning': mc.badge === '临期' || mc.badge === '今日未报到'
          }"
        >{{ mc.badge ?? '未开通' }}</span>
      </div>

      <div class="hc-main-row">
        <div class="hc-val-group">
          <span class="hc-val">{{ mc.value }}</span>
          <span class="hc-unit">{{ (mc.state === 'active' || mc.state === 'expiring') ? '天剩余' : mc.unit }}</span>
        </div>
        <span
          v-if="(mc.state === 'active' || mc.state === 'expiring') && mc.planName && mc.expiryDate"
          class="hc-plan"
        >{{ mc.planName }} · {{ mc.expiryDate }}到期</span>
      </div>

      <!-- 剩余天数占比进度条（仅有进度时展示） -->
      <div
        v-if="mc.progress !== null"
        class="hc-progress-track"
        role="progressbar"
        :aria-valuenow="100 - clampProgress(mc.progress)"
        aria-valuemin="0"
        aria-valuemax="100"
        :aria-label="`会员剩余 ${100 - clampProgress(mc.progress)}%`"
      >
        <div
          class="hc-progress-bar"
          :style="{ width: (100 - clampProgress(mc.progress)) + '%' }"
        ></div>
      </div>

      <div class="hc-footer">
        <span class="hc-desc">{{ mc.desc }}</span>
        <RouterLink :to="mc.to" class="hc-link">{{ mc.footer }} ›</RouterLink>
      </div>
    </div>

    <!-- 2. 公益服卡 -->
    <div class="hc-card">
      <div class="hc-header">
        <span class="hc-title">公益服</span>
        <span class="hc-badge">免费开放</span>
      </div>
      <div class="hc-main-row">
        <span class="hc-val-group">
          <span class="hc-val">{{ ec.balance }}</span>
          <span class="hc-unit">能量</span>
        </span>
        <span v-if="ec.hasVitality" :class="['hc-vit', { 'hc-vit-low': ec.isVitalityLow }]">
          体力 {{ ec.vitalityText }} · {{ ec.isVitalityLow ? '体力偏低' : '还能稳住' }}
        </span>
        <span v-else class="hc-vit">体力 —</span>
      </div>
      <div v-if="ec.checkinEnabled" class="hc-chips">
        <span :class="['hc-chip', { 'hc-chip-danger': !ec.checkedToday }]">
          {{ ec.checkedToday ? '今日已报到' : '今日未报到' }}
        </span>
        <span v-if="ec.streak !== null" class="hc-chip">连签 {{ ec.streak }} 天</span>
        <span class="hc-chip">无需会员看全库</span>
      </div>
      <div class="hc-footer">
        <span class="hc-desc">能量在补给站和账本里用</span>
        <RouterLink :to="ec.buttonTo" class="hc-link">去补给站 ›</RouterLink>
      </div>
    </div>

    <!-- 3. 观影数据小条 -->
    <div class="hc-watch-strip">
      <span class="hc-watch-title">观影数据</span>
      <span class="hc-watch-data">{{ ws.watchTime }} · 播放 {{ ws.totalPlays }} 次 · 看过 {{ ws.watchedItems }} 部</span>
    </div>
  </section>
</template>
