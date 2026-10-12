<script setup lang="ts">
import { Crown, TriangleAlert } from 'lucide-vue-next'
import EmptyState from '@/components/ui/EmptyState.vue'
import { useRouter } from 'vue-router'

export interface SubscriptionItem {
  id: number | string
  plan_name: string
  realm_name?: string
  start_date: string
  end_date: string
  status: string
  days_left?: number
}

const props = defineProps<{
  activeSub?: SubscriptionItem | null
  subscriptions: SubscriptionItem[]
  expiringSoon?: boolean
  isFreeRealm?: boolean
  realmNote?: string
  cancellingSub?: number | string | null
}>()

const emit = defineEmits<{
  (e: 'cancel', sub: any): void
}>()

const router = useRouter()
</script>

<template>
  <section class="pane">
    <header class="pane-head">
      <h2 class="pane-title">
        <Crown :size="17" />
        我的订阅
      </h2>
    </header>

    <div v-if="activeSub" class="sub-active" :class="{ warn: expiringSoon }">
      <div class="sub-info">
        <div class="sub-plan">{{ activeSub.plan_name }}</div>
        <div class="sub-end">{{ activeSub.end_date.slice(0, 10) }} 到期 · 剩余 {{ activeSub.days_left }} 天</div>
        <p v-if="expiringSoon" class="sub-warn">
          <TriangleAlert :size="13" />
          即将到期，前往「钱包」续费后可无缝接续
        </p>
      </div>
      <span class="badge" :class="expiringSoon ? 'warn' : 'ok'">
        {{ expiringSoon ? '即将到期' : '生效中' }}
      </span>
    </div>
    <div v-else-if="isFreeRealm" class="sub-free">
      <span class="badge free">公益服 · 免费开放</span>
      <p class="pane-empty">{{ realmNote }}</p>
    </div>
    <EmptyState
      v-else
      size="compact"
      title="还没有订阅"
      description="了解可选方案"
    >
      <template #action>
        <button class="btn primary" @click="router.push('/store')">查看订阅方案</button>
      </template>
    </EmptyState>

    <ul v-if="subscriptions.length > 1" class="sub-history">
      <li v-for="s in subscriptions.slice(0, 4)" :key="s.id" class="sub-history-item">
        <span>{{ s.plan_name }}<em v-if="s.realm_name" class="sub-realm">（{{ s.realm_name }}）</em></span>
        <span class="sub-history-date">{{ s.start_date.slice(0, 10) }} ~ {{ s.end_date.slice(0, 10) }}</span>
        <span class="badge" :class="s.status === 'active' ? 'ok' : 'off'">
          {{ s.status === 'active' ? '生效中' : '已结束' }}
        </span>
        <button
          v-if="s.status === 'active'"
          type="button"
          class="text-btn danger"
          :disabled="cancellingSub === s.id"
          @click="emit('cancel', s)"
        >{{ cancellingSub === s.id ? '终止中…' : '终止' }}</button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.pane { content-visibility: auto; contain-intrinsic-size: auto 200px; }
.pane-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.pane-title { display: flex; align-items: center; gap: 8px; font-size: 16px; margin: 0; }
.sub-active { display: flex; justify-content: space-between; align-items: center; padding: 12px; border-radius: 8px; background: var(--au-surface); }
.sub-active.warn { border: 1px solid var(--au-warning); }
.sub-plan { font-weight: 600; }
.sub-end { font-size: 13px; color: var(--au-text-3); }
.sub-warn { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--au-warning); margin: 6px 0 0; }
.badge { padding: 2px 8px; border-radius: 999px; font-size: 12px; }
.badge.ok { background: var(--au-success-soft); color: var(--au-success); }
.badge.warn { background: var(--au-warning-soft); color: var(--au-warning); }
.badge.free { background: var(--au-primary-soft); color: var(--au-primary); }
.badge.off { background: var(--au-surface-2); color: var(--au-text-3); }
.sub-history { list-style: none; margin: 12px 0 0; padding: 0; }
.sub-history-item { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-top: 1px solid var(--au-border); font-size: 13px; }
.sub-history-date { color: var(--au-text-3); margin-left: auto; }
.text-btn.danger { color: var(--au-danger); background: none; border: none; cursor: pointer; }
</style>
