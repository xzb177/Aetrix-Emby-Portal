<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { ServerCardItem, ServerWatchStrip } from '@/composables/useServerCards'
import './ticket-card.css'

const COPY = {
  freeBadge: '免费开放',
  notOpen: '未开通',
  expired: '已过期',
  tagPaid: '付费服',
  tagFree: '公益服',
  watchTitle: '观影数据',
  openAll: '开通后全库畅看',
  noCheckin: '未开启签到',
  vitalityNone: '体力 —',
  vitalityLow: '体力偏低',
  vitalityOk: '还能稳住',
} as const

const props = withDefaults(
  defineProps<{
    loading: boolean
    servers: ServerCardItem[]
    mode: 'cards' | 'overview'
    watchStrip?: ServerWatchStrip
    error?: boolean
    skeletonCount?: number
  }>(),
  {
    servers: () => [],
    error: false,
    skeletonCount: 2,
  },
)

defineEmits(['retry'])

const FALLBACK_STRIP: ServerWatchStrip = {
  watchTime: '—',
  totalPlays: '—',
  watchedItems: '—',
}

const strip = computed<ServerWatchStrip>(() => props.watchStrip ?? FALLBACK_STRIP)

function clampProgress(value: number | null): number {
  if (value === null || !Number.isFinite(value)) return 0
  return Math.min(100, Math.max(0, value))
}

function humanDate(value: string | number | Date | null | undefined): string {
  if (value === null || value === undefined || value === '') return ''
  if (value instanceof Date) {
    if (Number.isNaN(value.getTime())) return ''
    return `${value.getMonth() + 1}月${value.getDate()}日`
  }
  const matched = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(String(value))
  if (matched) {
    const mo = Number(matched[2])
    const da = Number(matched[3])
    if (mo < 1 || mo > 12 || da < 1 || da > 31) return ''
    return `${mo}月${da}日`
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return `${date.getMonth() + 1}月${date.getDate()}日`
}

function planLine(item: ServerCardItem): string {
  if (item.state === 'unsubscribed') return ''
  const end = humanDate(item.endDate)
  return item.planName && end ? `${item.planName} · ${end}到期` : ''
}

function vitalityLine(item: ServerCardItem): string {
  if (!item.hasVitality) return COPY.vitalityNone
  return `体力 ${item.vitalityText} · ${item.isVitalityLow ? COPY.vitalityLow : COPY.vitalityOk}`
}

function badgeText(item: ServerCardItem): string {
  if (item.accessMode === 'free') return COPY.freeBadge
  return item.badge ?? COPY.notOpen
}

function badgeClass(item: ServerCardItem): string {
  if (item.accessMode === 'free') return 'hc-badge'
  if (item.badge === '生效中') return 'hc-badge is-amber'
  if (item.badge === '临期') return 'hc-badge is-warning'
  if (item.badge === '已过期') return 'hc-badge is-warning'
  return 'hc-badge'
}

function tagText(item: ServerCardItem): string {
  return item.accessMode === 'paid' ? COPY.tagPaid : COPY.tagFree
}

/** 标签与服务器名相同时隐藏，避免「付费服 付费服」叠字 */
function showTag(item: ServerCardItem): boolean {
  return tagText(item) !== item.name
}

function overviewInfo(item: ServerCardItem): string {
  if (item.accessMode === 'free') {
    return `能量 ${item.balance} · ${vitalityLine(item)} · ${
      item.checkinEnabled ? (item.checkedToday ? '今日已报到' : '今日未报到') : COPY.noCheckin
    }`
  }
  if (item.state === 'unsubscribed') return COPY.openAll
  if (item.state === 'expired') {
    const plan = planLine(item)
    return plan ? `已过期 · ${plan}` : COPY.expired
  }
  const plan = planLine(item)
  if (item.daysLeft === null || item.daysLeft === undefined) return plan || '生效中'
  return plan ? `剩 ${item.daysLeft} 天 · ${plan}` : `剩 ${item.daysLeft} 天`
}
</script>

<template>
  <section class="hc-wrap" aria-label="服务器卡片">
    <template v-if="loading">
      <template v-if="mode === 'cards'">
        <article v-for="n in skeletonCount" :key="`sk-card-${n}`" class="hc-card hc-sk-card" aria-hidden="true">
          <div class="hc-header">
            <div class="v4-skeleton hc-sk-title"></div>
            <div class="v4-skeleton hc-sk-badge"></div>
          </div>
          <div class="hc-main-row">
            <div class="v4-skeleton hc-sk-val"></div>
          </div>
          <div class="v4-skeleton hc-sk-progress"></div>
          <div class="v4-skeleton hc-sk-desc"></div>
        </article>
      </template>
      <article v-else class="hc-card hc-ov hc-sk-card" aria-hidden="true">
        <div class="hc-header">
          <div class="v4-skeleton hc-sk-title"></div>
        </div>
        <div v-for="n in 3" :key="`sk-row-${n}`" class="hc-row">
          <div class="v4-skeleton hc-sk-desc"></div>
        </div>
      </article>
      <div class="hc-watch-strip" aria-hidden="true">
        <div class="v4-skeleton hc-sk-watch-line"></div>
      </div>
    </template>

    <template v-else>
      <template v-if="servers.length > 0">
        <template v-if="mode === 'cards'">
          <article v-for="s in servers" :key="s.id" class="hc-card">
            <template v-if="s.accessMode === 'paid'">
              <div class="hc-header">
                <span class="hc-title">{{ s.name }}<span v-if="showTag(s)" class="hc-tag">{{ tagText(s) }}</span></span>
                <span :class="badgeClass(s)">{{ badgeText(s) }}</span>
              </div>
              <div class="hc-main-row">
                <div class="hc-val-group">
                  <span v-if="s.state === 'unsubscribed'" class="hc-val">未开通</span>
                  <span v-else-if="s.state === 'expired'" class="hc-val">已过期</span>
                  <template v-else>
                    <span class="hc-val">{{ s.daysLeft }}</span>
                    <span class="hc-unit">天剩余</span>
                  </template>
                </div>
              </div>
              <div
                v-if="s.progress !== null"
                class="hc-progress-track"
                role="progressbar"
                :aria-label="`会员剩余 ${100 - clampProgress(s.progress)}%`"
                :aria-valuenow="Math.round(100 - clampProgress(s.progress))"
                aria-valuemin="0"
                aria-valuemax="100"
              >
                <div class="hc-progress-bar" :style="{ width: Math.round(100 - clampProgress(s.progress)) + '%' }"></div>
              </div>
              <div class="hc-footer">
                <span class="hc-desc">{{ s.desc }}</span>
                <RouterLink class="hc-link" :to="s.to">{{ s.footer }} ›</RouterLink>
              </div>
            </template>

            <template v-else>
              <div class="hc-header">
                <span class="hc-title">{{ s.name }}</span>
                <span :class="badgeClass(s)">{{ badgeText(s) }}</span>
              </div>
              <div class="hc-main-row">
                <span class="hc-val-group">
                  <span class="hc-val">{{ s.balance }}</span>
                  <span class="hc-unit">能量</span>
                </span>
                <span
                  class="hc-vit"
                  :class="{ 'hc-vit-low': s.hasVitality && s.isVitalityLow }"
                >{{ vitalityLine(s) }}</span>
              </div>
              <div v-if="s.checkinEnabled" class="hc-chips">
                <span class="hc-chip" :class="{ 'hc-chip-danger': !s.checkedToday }">{{
                  s.checkedToday ? '今日已报到' : '今日未报到'
                }}</span>
                <span v-if="s.streak !== null" class="hc-chip">连签 {{ s.streak }} 天</span>
                <span class="hc-chip">无需会员看全库</span>
              </div>
              <div class="hc-footer">
                <span class="hc-desc">{{ s.desc }}</span>
                <RouterLink class="hc-link" :to="s.to">{{ s.footer }} ›</RouterLink>
              </div>
            </template>
          </article>
        </template>

        <article v-else class="hc-card hc-ov">
          <div class="hc-header">
            <span class="hc-title">我的服务器</span>
            <span class="hc-unit">共 {{ servers.length }} 个</span>
          </div>
          <div v-for="s in servers" :key="s.id" class="hc-row">
            <div class="hc-header">
              <span class="hc-title">{{ s.name }}<span v-if="showTag(s)" class="hc-tag">{{ tagText(s) }}</span></span>
              <span :class="badgeClass(s)">{{ badgeText(s) }}</span>
            </div>
            <div class="hc-footer">
              <span class="hc-row-info">{{ overviewInfo(s) }}</span>
              <RouterLink class="hc-link" :to="s.to">{{ s.footer }} ›</RouterLink>
            </div>
          </div>
        </article>
      </template>
      <div v-else-if="error" class="hc-err">服务器卡片没加载出来<button class="hc-retry" @click="$emit('retry')">重试</button></div>
      <div v-else class="hc-empty">暂无可用服务器</div>

      <div class="hc-watch-strip">
        <span class="hc-watch-title">观影数据</span>
        <span class="hc-watch-data">{{ strip.watchTime }} · 播放 {{ strip.totalPlays }} 次 · 看过 {{ strip.watchedItems }} 部</span>
      </div>
    </template>
  </section>
</template>
