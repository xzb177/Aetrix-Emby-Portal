<script setup lang="ts">
import { computed } from 'vue'
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
  auto_renew?: boolean
}

/** 查看凭证（取自账号卡 view_permission，价格与天数均由服务端给，前端不写死） */
export interface ViewUnlockInfo {
  granted: boolean
  /** 已解锁的到期时间（ISO，未解锁/永久为 null） */
  expiresAt: string | null
  unlockPoints: number
  unlockDays: number
}

const props = defineProps<{
  activeSub?: SubscriptionItem | null
  subscriptions: SubscriptionItem[]
  expiringSoon?: boolean
  isFreeRealm?: boolean
  realmNote?: string
  cancellingSub?: number | string | null
  togglingRenew?: number | string | null
  /** 公益资格是否生效（取自登录态 is_welfare） */
  welfareIsWelfare?: boolean
  /** 公益资格到期日（ISO；由福利状态口返回，续期成功后以服务端返回为准） */
  welfareExpiresAt?: string | null
  /** 福利状态口是否已成功返回（未返回时不显示到期／不过期后缀，避免闪现） */
  welfareStatusLoaded?: boolean
  /** 当前能量（取自账号卡 view_permission.points_balance，未取到为 null） */
  pointsBalance?: number | null
  /** 是否已绑 TG（null = 未知，按钮不拦、以服务端为准） */
  tgBound?: boolean | null
  viewUnlock?: ViewUnlockInfo | null
  /** 正在续期的公益天数档（7 或 30），无则 null */
  renewingWelfare?: number | null
  renewingView?: boolean
}>()

const emit = defineEmits<{
  (e: 'cancel', sub: any): void
  (e: 'toggle-renew', sub: any): void
  (e: 'renew-welfare', days: 7 | 30): void
  (e: 'renew-view'): void
}>()

const router = useRouter()

/** 查看凭证状态：unknown（无数据不展示）/ locked（未解锁）/ permanent（永久）/ valid（有效期内）/ expired（已到期） */
const viewState = computed(() => {
  const v = props.viewUnlock
  if (!v) return 'unknown'
  if (!v.granted) return 'locked'
  if (v.expiresAt === null) return 'permanent'
  return new Date(v.expiresAt).getTime() > Date.now() ? 'valid' : 'expired'
})

/** 查看到期日（yyyy-mm-dd）与续费价目（模板里直接取 prop 会被判可能为空，收到 computed 里收窄） */
const viewExpiresDate = computed(() => {
  const v = props.viewUnlock
  if (!v || v.expiresAt === null) return null
  return v.expiresAt.slice(0, 10)
})
const viewRenewInfo = computed(() => {
  const v = props.viewUnlock
  if (!v) return null
  return { points: v.unlockPoints, days: v.unlockDays }
})

/** 公益到期日（yyyy-mm-dd）：状态口未成功返回时为 null，不展示 */
const welfareExpiresDate = computed(() => {
  if (!props.welfareStatusLoaded || !props.welfareExpiresAt) return null
  return props.welfareExpiresAt.slice(0, 10)
})
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

    <!-- 公益服下付费会员部分一律不渲染：转公益时 legacy 订阅记录保留至到期（权益不变），
         但 activeSub 仍为真时会抢在 v-else-if 的公益服块之前渲染，造成公益服下仍显示付费卡；
         此处以 isFreeRealm 设门，公益服下走下面的 sub-free 块（公益资格／查看凭证）。
         isFreeRealm 未传／非真值时按非公益服处理（fail-closed，信号缺失不误藏）。 -->
    <div v-if="activeSub && !isFreeRealm" class="sub-active" :class="{ warn: expiringSoon }">
      <div class="sub-info">
        <div class="sub-plan">{{ activeSub.plan_name }}</div>
        <div class="sub-end">{{ activeSub.end_date.slice(0, 10) }} 到期 · 剩余 {{ activeSub.days_left }} 天</div>
        <p v-if="expiringSoon" class="sub-warn">
          <TriangleAlert :size="13" />
          即将到期，去补给站续费后可无缝接续
        </p>
      </div>
      <span class="badge" :class="expiringSoon ? 'warn' : 'ok'">
        {{ expiringSoon ? '即将到期' : '生效中' }}
      </span>
    </div>
    <!-- r48-⑬：自动续订开关 —— 到期前 24 小时用能量按当前档位续订一期，
         能量不足则不续并站内信提醒；人民币自动扣款不开通
         公益服下与会员卡一并隐藏（同上设门原因） -->
    <div v-if="activeSub && !isFreeRealm" class="renew-row">
      <div class="renew-info">
        <b>到期自动续订</b>
        <small>到期前 24 小时用能量自动续订一期 · 能量不足则不续并提醒你</small>
      </div>
      <button
        type="button"
        class="renew-switch"
        :class="{ on: activeSub.auto_renew }"
        :disabled="togglingRenew === activeSub.id"
        :aria-pressed="!!activeSub.auto_renew"
        :aria-label="activeSub.auto_renew ? '关闭自动续订' : '开启自动续订'"
        @click="emit('toggle-renew', activeSub)"
      >
        <span class="knob" aria-hidden="true"></span>
      </button>
    </div>
    <div v-else-if="isFreeRealm" class="sub-free">
      <span class="badge free">公益服 · 免费开放</span>
      <p class="pane-empty">{{ realmNote }}</p>
      <!-- 自助续期：后端口子现成（公益天数 /redeem、查看 /unlock-view），此前个人中心无入口 -->
      <div class="renew-block">
        <div class="renew-block-title">公益资格</div>
        <p class="renew-line">{{ welfareIsWelfare ? '生效中' : '未开通' }}<template v-if="welfareExpiresDate"> · 到期 {{ welfareExpiresDate }}</template><template v-else-if="welfareStatusLoaded && welfareIsWelfare"> · 不过期</template><template v-if="pointsBalance != null"> · 当前能量 {{ pointsBalance }}</template></p>
        <p v-if="tgBound === false" class="renew-hint">续期需先绑定 Telegram，绑定后回来续期</p>
        <div class="renew-btns">
          <button
            v-for="days in ([7, 30] as const)"
            :key="days"
            type="button"
            class="btn btn-primary renew-btn"
            :disabled="renewingWelfare != null || tgBound === false"
            @click="emit('renew-welfare', days)"
          >{{ renewingWelfare === days ? '续期中…' : `续期 ${days} 天` }}</button>
          <button
            v-if="tgBound === false"
            type="button"
            class="btn renew-btn"
            @click="router.push('/tg-bind')"
          >去绑定 Telegram</button>
        </div>
      </div>
      <div v-if="viewRenewInfo" class="renew-block">
        <div class="renew-block-title">查看凭证</div>
        <p v-if="viewState === 'permanent'" class="renew-line">已永久解锁</p>
        <p v-else-if="viewState === 'valid'" class="renew-line">已解锁至 {{ viewExpiresDate }}</p>
        <p v-else class="renew-line">{{ viewState === 'expired' ? '已到期' : '未解锁' }}<template v-if="pointsBalance != null"> · 当前能量 {{ pointsBalance }}</template></p>
        <div v-if="viewState === 'locked' || viewState === 'expired'" class="renew-btns">
          <button
            type="button"
            class="btn btn-primary renew-btn"
            :disabled="renewingView || (pointsBalance != null && pointsBalance < viewRenewInfo.points)"
            @click="emit('renew-view')"
          >{{ renewingView ? '续费中…' : (pointsBalance != null && pointsBalance < viewRenewInfo.points ? '能量不足' : `花 ${viewRenewInfo.points} 能量续费 ${viewRenewInfo.days > 0 ? viewRenewInfo.days + ' 天' : '永久'}`) }}</button>
        </div>
      </div>
    </div>
    <EmptyState
      v-else
      size="compact"
      title="还没有会员"
      description="开通后解锁全片库"
    >
      <template #action>
        <!-- P1-2：改用全局 .btn-primary（styles/index.css:160，琥珀实心 + --paper-on-primary 文，
             与旧 ProfileView .btn.primary 语义一致；.btn 基类自带 font-weight:600） -->
        <button class="btn btn-primary" @click="router.push('/shop')">查看会员方案</button>
      </template>
    </EmptyState>

    <!-- 历史账单／取消续订入口 —— 公益服下与会员卡一并隐藏（含 active 行的“终止”按钮） -->
    <ul v-if="subscriptions.length > 1 && !isFreeRealm" class="sub-history">
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
  background: var(--paper-card);
  border: 1px solid var(--paper-line);
  border-radius: var(--paper-radius-lg);
  animation: pane-in 0.45s var(--paper-ease) both;
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
  background: var(--paper-primary-soft);
  border: 1px solid var(--paper-primary-border);
  border-radius: var(--paper-r-md);
}
/* 临期状态：整块底色换成警示色，避免"还剩 3 天"淹没在常规配色里 */
.sub-active.warn {
  background: var(--paper-warning-soft);
  border-color: var(--paper-warning-soft);
}
/* P3-13：等宽数字（倒计时不抖动） */
.sub-plan { font-weight: 600; color: var(--paper-ink); font-variant-numeric: tabular-nums; }
.sub-end { margin-top: 0.125rem; font-size: 0.8125rem; color: var(--paper-ink-3); font-variant-numeric: tabular-nums; white-space: nowrap; }
.sub-warn { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--paper-warning); margin: 6px 0 0; }
.badge { padding: 2px 8px; border-radius: 999px; font-size: 12px; }
.badge.ok { background: var(--paper-success-soft); color: var(--paper-success); }
.badge.warn { background: var(--paper-warning-soft); color: var(--paper-warning); }
.badge.free { background: var(--paper-primary-soft); color: var(--paper-primary); }
.badge.off { background: var(--paper-card-2); color: var(--paper-ink-3); }
.sub-history { list-style: none; margin: 12px 0 0; padding: 0; }
.sub-history-item { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-top: 1px solid var(--paper-line); font-size: 13px; }
/* R32：历史记录截断脚注（SupplyLogView .rc-foot 先例） */
.sub-history-foot { padding: 8px 0 0; border-top: 1px solid var(--paper-line); text-align: center; font-size: 11px; color: var(--paper-ink-3); letter-spacing: 1px; }
.sub-history-date { color: var(--paper-ink-3); margin-left: auto; }
.text-btn.danger { color: var(--paper-danger); background: none; border: none; cursor: pointer; }
/* r48-⑬：自动续订开关行 */
.renew-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-top: 0.75rem;
  padding: 0.75rem 1rem;
  background: var(--paper-card-2);
  border: 1px solid var(--paper-line);
  border-radius: var(--paper-r-md);
}
.renew-info { display: flex; flex-direction: column; gap: 2px; }
.renew-info b { font-size: 0.875rem; color: var(--paper-ink); }
.renew-info small { font-size: 0.75rem; color: var(--paper-ink-3); line-height: 1.5; }
.renew-switch {
  flex: none;
  width: 44px;
  height: 24px;
  border-radius: 999px;
  border: 1px solid var(--paper-line);
  background: var(--paper-card);
  cursor: pointer;
  position: relative;
  transition: background 0.2s var(--paper-ease, ease), border-color 0.2s;
}
.renew-switch .knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--paper-ink-3);
  transition: transform 0.2s var(--paper-ease, ease), background 0.2s;
}
.renew-switch.on { background: var(--paper-primary); border-color: var(--paper-primary); }
.renew-switch.on .knob { transform: translateX(20px); background: #fff; }
.renew-switch:disabled { opacity: 0.6; cursor: wait; }
.renew-switch:focus-visible { outline: 2px solid var(--paper-primary); outline-offset: 2px; }
/* P2-26：公益服状态块三条旧规则全文（ProfileView.vue 第 1087 / 1050 / 1469-1473 行） */
.sub-free { display: flex; flex-direction: column; gap: 0.375rem; align-items: flex-start; }
/* 自助续期：沿用 renew-row 的卡片语气（浅底 + 细边 + 圆角），与“到期自动续订”行同款 */
.renew-block {
  align-self: stretch;
  margin-top: 0.75rem;
  padding: 0.75rem 1rem;
  background: var(--paper-card-2);
  border: 1px solid var(--paper-line);
  border-radius: var(--paper-r-md);
}
.renew-block-title { font-size: 0.875rem; font-weight: 600; color: var(--paper-ink); }
.renew-line { margin: 4px 0 0; font-size: 0.8125rem; color: var(--paper-ink-3); font-variant-numeric: tabular-nums; }
.renew-hint { margin: 6px 0 0; font-size: 12px; color: var(--paper-warning); }
.renew-btns { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.renew-btn:disabled { opacity: 0.6; cursor: wait; }
.pane-empty {
  margin: 0;
  font-size: 0.8125rem;
  line-height: 1.6;
  color: var(--paper-ink-3);
}
.sub-realm {
  font-style: normal;
  font-size: 0.8125rem;
  color: var(--paper-ink-3);
}

/* R27：减少动态偏好下装饰动画/过渡停掉（本地守卫） */
@media (prefers-reduced-motion: reduce) {
  .pane { animation: none; transition: none; }
}
</style>
