<script setup lang="ts">
/**
 * 举报与停权（等级公益服活动治理 Step 2 管理端）：两页并一页
 *
 * - 举报终审：复核达标升级上来的举报（escalated）由管理员受理 / 驳回；
 * - 治理停权：恶意举报自动停权 + 管理员手动停权 / 撤销。
 *
 * 两个标签页各自是一整页、按需加载；当前标签与 ?tab= 同步，刷新不丢（与 Welfare.vue 同一写法）。
 * 后端：backend/api/governance_report.py 的 admin_router（/api/admin/governance）。
 */
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const GovernanceReports = defineAsyncComponent(() => import('./GovernanceReports.vue'))
const GovernanceSuspensions = defineAsyncComponent(() => import('./GovernanceSuspensions.vue'))

type GovernanceTab = 'reports' | 'suspensions'

const route = useRoute()
const router = useRouter()

const activeTab = computed<GovernanceTab>({
  get: () => (route.query.tab === 'suspensions' ? 'suspensions' : 'reports'),
  set: (tab) => {
    if (tab === activeTab.value) return
    router.replace({ query: { ...route.query, tab } })
  },
})
</script>

<template>
  <!-- 两个子页各自带 PageHeader，这里只放标签条 -->
  <div class="admin-page governance-page">
    <div class="tab-bar" role="tablist" aria-label="举报与停权">
      <button
        id="governance-tab-reports"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'reports' }"
        :aria-selected="activeTab === 'reports'"
        aria-controls="governance-panel"
        @click="activeTab = 'reports'"
      >
        举报终审
        <span class="tab-hint">终审队列 / 受理 / 驳回</span>
      </button>
      <button
        id="governance-tab-suspensions"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'suspensions' }"
        :aria-selected="activeTab === 'suspensions'"
        aria-controls="governance-panel"
        @click="activeTab = 'suspensions'"
      >
        治理停权
        <span class="tab-hint">停权列表 / 手动停权 / 撤销</span>
      </button>
    </div>

    <div
      id="governance-panel"
      class="tab-panel"
      role="tabpanel"
      :aria-labelledby="activeTab === 'reports' ? 'governance-tab-reports' : 'governance-tab-suspensions'"
    >
      <!-- KeepAlive：来回切标签不重拉数据、不丢筛选 -->
      <KeepAlive>
        <GovernanceReports v-if="activeTab === 'reports'" />
        <GovernanceSuspensions v-else />
      </KeepAlive>
    </div>
  </div>
</template>

<style scoped>
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
