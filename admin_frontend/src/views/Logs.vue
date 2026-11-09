<script setup lang="ts">
/**
 * 操作日志（外壳）：两页并一页
 *
 * - 操作日志：管理员操作审计流水（原操作日志页，OperationLogs.vue）；
 * - 双盘审计：个人盘 + 贡献池全程留痕只读查询（DualDriveAudit.vue，
 *   原独立页 /admin/audit 应用户布局纪律并入本页，旧地址跳到本标签）。
 *
 * 两个标签页各自是一整页、按需加载；当前标签与 ?tab= 同步，刷新不丢
 * （与 Governance.vue / Welfare.vue 同一写法）。
 */
import { computed, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const OperationLogs = defineAsyncComponent(() => import('./OperationLogs.vue'))
const DualDriveAudit = defineAsyncComponent(() => import('./DualDriveAudit.vue'))

type LogsTab = 'logs' | 'audit'

const route = useRoute()
const router = useRouter()

const activeTab = computed<LogsTab>({
  get: () => (route.query.tab === 'audit' ? 'audit' : 'logs'),
  set: (tab) => {
    if (tab === activeTab.value) return
    router.replace({ query: { ...route.query, tab } })
  },
})
</script>

<template>
  <!-- 两个子页各自带 PageHeader，这里只放标签条 -->
  <div class="admin-page logs-shell">
    <div class="tab-bar" role="tablist" aria-label="操作日志">
      <button
        id="logs-tab-logs"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'logs' }"
        :aria-selected="activeTab === 'logs'"
        aria-controls="logs-panel"
        @click="activeTab = 'logs'"
      >
        操作日志
        <span class="tab-hint">管理员操作审计流水</span>
      </button>
      <button
        id="logs-tab-audit"
        type="button"
        role="tab"
        class="tab-btn"
        :class="{ active: activeTab === 'audit' }"
        :aria-selected="activeTab === 'audit'"
        aria-controls="logs-panel"
        @click="activeTab = 'audit'"
      >
        双盘审计
        <span class="tab-hint">个人盘 + 贡献池留痕 · 只读查询</span>
      </button>
    </div>

    <div
      id="logs-panel"
      class="tab-panel"
      role="tabpanel"
      :aria-labelledby="activeTab === 'logs' ? 'logs-tab-logs' : 'logs-tab-audit'"
    >
      <!-- KeepAlive：来回切标签不重拉数据、不丢筛选 -->
      <KeepAlive>
        <OperationLogs v-if="activeTab === 'logs'" />
        <DualDriveAudit v-else />
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
