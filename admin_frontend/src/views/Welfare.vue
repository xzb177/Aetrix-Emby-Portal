<script setup lang="ts">
/**
 * 公益服（重新布局批 2）：两页并一页
 *
 * 群抽奖活动 + 积分配置通过标签页切换，统一入口管理公益服。
 * 两个标签页各自是一整页：按需加载，只看群抽奖时不用把积分配置页也打进首屏。
 * 配置写入口随入口统一：两页写的键集合不重叠（抽奖页只写 lottery_*，积分页写
 * 货币/红包/转账/发言/机器人/解锁配置），并页本身就是写入口唯一化，不再各占导航。
 */
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'

// 两个标签页各自是一整页：按需加载
const WelfareLotteryRounds = defineAsyncComponent(() => import('./WelfareLotteryRounds.vue'))
const WelfarePoints = defineAsyncComponent(() => import('./WelfarePoints.vue'))

type WelfareTab = 'lottery' | 'points'

const route = useRoute()
const router = useRouter()

/**
 * 当前标签与 ?tab= 同步：旧地址 /welfare-points 跳过来能落到积分配置标签，刷新也不丢。
 */
const activeTab = computed<WelfareTab>({
  get: () => (route.query.tab === 'points' ? 'points' : 'lottery'),
  set: (tab) => {
    if (tab === activeTab.value) return
    router.replace({ query: { ...route.query, tab } })
  },
})
</script>

<template>
  <!-- 两个子页各自带 PageHeader（眉题「内容」），这里只放标签条，不再叠第二个页头 -->
  <div class="admin-page welfare-page">
    <div class="tab-bar" role="tablist" aria-label="公益服">
      <button
        id="welfare-tab-lottery"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'lottery' }"
        :aria-selected="activeTab === 'lottery'"
        aria-controls="welfare-panel"
        @click="activeTab = 'lottery'"
      >
        群抽奖
        <span class="tab-hint">活动创建 / 开奖 / 发放</span>
      </button>
      <button
        id="welfare-tab-points"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'points' }"
        :aria-selected="activeTab === 'points'"
        aria-controls="welfare-panel"
        @click="activeTab = 'points'"
      >
        积分配置
        <span class="tab-hint">货币 / 红包 / 转账 / 机器人</span>
      </button>
    </div>

    <div
      id="welfare-panel"
      class="tab-panel"
      role="tabpanel"
      :aria-labelledby="activeTab === 'lottery' ? 'welfare-tab-lottery' : 'welfare-tab-points'"
    >
      <!-- KeepAlive：来回切标签不重拉数据、不丢筛选 -->
      <KeepAlive>
        <WelfareLotteryRounds v-if="activeTab === 'lottery'" />
        <WelfarePoints v-else />
      </KeepAlive>
    </div>
  </div>
</template>

<style scoped>
/* 外层间距交给 .admin-content（不再自带 padding / max-width，否则手机上左右各多出 16px） */
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
  .tab-btn { padding: 8px 12px; font-size: 14px; }
  .tab-hint { font-size: 11px; }
}
</style>
