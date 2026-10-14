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
        <!-- R37 P3-C4：标题口径「我的会员」，与账号设置「我的会员」统一 -->
        我的会员
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
        <!-- P1-2：改用全局 .btn-primary（styles/index.css:160，琥珀实心 + --au-on-primary 文，
             与旧 ProfileView .btn.primary 语义一致；.btn 基类自带 font-weight:600） -->
        <button class="btn btn-primary" @click="router.push('/shop')">查看订阅方案</button>
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
      <!-- R32：历史记录只显示最近 4 条，达到上限时诚实告知（SupplyLogView 先例） -->
      <li v-if="subscriptions.length > 4" class="sub-history-foot">· 仅显示最近 4 条，还有 {{ subscriptions.length - 4 }} 条 ·</li>
    </ul>
  </section>
</template>

<style scoped>
/* 卡片容器：旧 ProfileView.vue .pane 全文（min-width/padding/background/border/radius/animation），
   合并保留 content-visibility 性能行；@keyframes pane-in 一并迁入（旧文件第 1023-1026 行），
   全局样式无此 keyframes，不迁则 animation 失效 */
.pane {
  min-width: 0;
  padding: 1.25rem;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  animation: pane-in 0.45s var(--au-ease) both;
  content-visibility: auto;
  contain-intrinsic-size: auto 200px;
}
@keyframes pane-in {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: none; }
}
.pane-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.pane-title { display: flex; align-items: center; gap: 8px; font-size: 16px; margin: 0; }
/* P2-25：旧 ProfileView.vue 第 1407-1436 行全文——生效中琥珀底高亮、临期整块换警示底色 */
.sub-active {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  border-radius: var(--au-r-md);
}
/* 临期状态：整块底色换成警示色，避免"还剩 3 天"淹没在常规配色里 */
.sub-active.warn {
  background: var(--au-warning-soft);
  border-color: var(--au-warning-soft);
}
/* P3-13：等宽数字（倒计时不抖动） */
.sub-plan { font-weight: 600; color: var(--au-text); font-variant-numeric: tabular-nums; }
.sub-end { margin-top: 0.125rem; font-size: 0.8125rem; color: var(--au-text-3); font-variant-numeric: tabular-nums; white-space: nowrap; }
.sub-warn { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--au-warning); margin: 6px 0 0; }
.badge { padding: 2px 8px; border-radius: 999px; font-size: 12px; }
.badge.ok { background: var(--au-success-soft); color: var(--au-success); }
.badge.warn { background: var(--au-warning-soft); color: var(--au-warning); }
.badge.free { background: var(--au-primary-soft); color: var(--au-primary); }
.badge.off { background: var(--au-surface-2); color: var(--au-text-3); }
.sub-history { list-style: none; margin: 12px 0 0; padding: 0; }
.sub-history-item { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-top: 1px solid var(--au-border); font-size: 13px; }
/* R32：历史记录截断脚注（SupplyLogView .rc-foot 先例） */
.sub-history-foot { padding: 8px 0 0; border-top: 1px solid var(--au-border); text-align: center; font-size: 11px; color: var(--au-text-3); letter-spacing: 1px; }
.sub-history-date { color: var(--au-text-3); margin-left: auto; }
.text-btn.danger { color: var(--au-danger); background: none; border: none; cursor: pointer; }
/* P2-26：公益服状态块三条旧规则全文（ProfileView.vue 第 1087 / 1050 / 1469-1473 行） */
.sub-free { display: flex; flex-direction: column; gap: 0.375rem; align-items: flex-start; }
.pane-empty {
  margin: 0;
  font-size: 0.8125rem;
  line-height: 1.6;
  color: var(--au-text-3);
}
.sub-realm {
  font-style: normal;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

/* R27：减少动态偏好下装饰动画/过渡停掉（本地守卫） */
@media (prefers-reduced-motion: reduce) {
  .pane { animation: none; transition: none; }
}
</style>
