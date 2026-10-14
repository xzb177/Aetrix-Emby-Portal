<script setup lang="ts">
/**
 * 统一码管理（M1 · Batch F 码体系正名）
 *
 * 两页并一页：会员卡码（注册/续期/白名单/积分/折扣）+ 旧版兑换码（旧系统，只查旧码）
 * 通过标签页切换，统一入口管理所有码。
 * 四种码的分工说明收敛在本页顶部（唯一权威出处；Coupons.vue 与 ExchangeCodes.vue
 * 的两处旧说明已删，改为指向这里）。
 */
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'

// 两个标签页各自是一整页：按需加载，只看会员卡码时不用把兑换码页也打进首屏
const RegistrationCodes = defineAsyncComponent(() => import('./RegistrationCodes.vue'))
const ExchangeCodes = defineAsyncComponent(() => import('./ExchangeCodes.vue'))

type CodesTab = 'reg' | 'exchange'

const route = useRoute()
const router = useRouter()

/**
 * 当前标签与 ?tab= 同步：旧地址 /exchange-codes、/registration-codes 跳过来能落到对应标签，刷新也不丢。
 * tab= 取值（reg / exchange）是兼容硬约束：Dashboard 等页的 ?tab=exchange 深链必须继续有效，
 * 所以只改标签的显示名，不改 query 取值。
 */
const activeTab = computed<CodesTab>({
  get: () => (route.query.tab === 'exchange' ? 'exchange' : 'reg'),
  set: (tab) => {
    if (tab === activeTab.value) return
    router.replace({ query: { ...route.query, tab } })
  },
})
</script>

<template>
  <!-- 两个子页各自带 PageHeader（眉题「运营中心」），这里只放标签条，不再叠第二个页头 -->
  <div class="admin-page codes-page">
    <!-- 四种码分工说明：唯一权威出处。验收口径=新服主不看说明书能分清四种码 -->
    <el-alert type="info" :closable="false" show-icon class="division-alert">
      <template #title>四种码，一眼看懂</template>
      <ul class="division-list">
        <li><strong>会员卡码</strong>（本页左侧）：给人发的码——「开通 / 续期会员」「送积分 / 送折扣」。分三种用途：注册码（没会员的人用，已有会员会被拒绝）、续期码（在到期时间上叠加天数）、白名单码（长期有效）。</li>
        <li><strong>旧版兑换码</strong>（本页右侧）：老系统遗留的码，只查旧码；用户兑换后直接生成奖励（积分 / 订阅时长 / 抵扣额度）。新发放统一走「会员卡码」。</li>
        <li><strong>优惠券</strong>（<router-link to="/coupons">优惠券</router-link>页）：下单付款那一刻直接打折，钱照旧走支付网关。和兑换码的区别：兑换码是「不花钱直接拿东西」，优惠券是「付费时打折」。</li>
        <li><strong>邀请码</strong>（<router-link to="/invitations">邀请</router-link>页）：邀请好友注册用的码。</li>
      </ul>
    </el-alert>

    <div class="tab-bar" role="tablist" aria-label="码类型">
      <button
        id="codes-tab-reg"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'reg' }"
        :aria-selected="activeTab === 'reg'"
        aria-controls="codes-panel"
        @click="activeTab = 'reg'"
      >
        会员卡码
        <span class="tab-hint">注册 / 续期 / 积分 / 折扣</span>
      </button>
      <button
        id="codes-tab-exchange"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'exchange' }"
        :aria-selected="activeTab === 'exchange'"
        aria-controls="codes-panel"
        @click="activeTab = 'exchange'"
      >
        旧版兑换码
        <span class="tab-hint">只查旧码 · 新发放走会员卡码</span>
      </button>
    </div>

    <div
      id="codes-panel"
      class="tab-panel"
      role="tabpanel"
      :aria-labelledby="activeTab === 'reg' ? 'codes-tab-reg' : 'codes-tab-exchange'"
    >
      <!-- KeepAlive：来回切标签不重拉数据、不丢筛选 -->
      <KeepAlive>
        <RegistrationCodes v-if="activeTab === 'reg'" />
        <ExchangeCodes v-else />
      </KeepAlive>
    </div>
  </div>
</template>

<style scoped>
/* 外层间距交给 .admin-content（不再自带 padding / max-width，否则手机上左右各多出 16px） */
.division-alert { margin-bottom: 14px; }
.division-list { margin: 4px 0 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; line-height: 1.6; }
.division-list a { color: var(--au-primary); }
.tab-bar {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--au-border);
  overflow-x: auto;
  scrollbar-width: none;
}
.tab-btn {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  flex-shrink: 0;
  padding: 10px 18px;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--au-text-2);
  font: inherit;
  font-size: 15px;
  font-weight: 500;
  cursor: pointer;
  margin-bottom: -1px;
  transition: color var(--au-fast) var(--au-ease), border-color var(--au-fast) var(--au-ease);
}
.tab-btn:hover { color: var(--au-text); }
.tab-btn:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: -2px; }
.tab-btn.active {
  color: var(--au-primary);
  border-bottom-color: var(--au-primary);
}
.tab-hint {
  font-size: 12px;
  color: var(--au-text-3);
  font-weight: 400;
  white-space: nowrap;
}
.tab-btn.active .tab-hint { color: var(--au-primary); }
.tab-panel { min-width: 0; }

@media (max-width: 640px) {
  /* Batch F：手机端不再藏 hint（以前这里 display:none，只剩两个秃词）——hint 缩小保留 */
  .tab-btn { padding: 8px 12px; font-size: 14px; }
  .tab-hint { font-size: 11px; }
}
</style>
