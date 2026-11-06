<template>
  <div class="pc-entry">
    <PcEntryRow
      :icon="History"
      name="登录记录"
      expandable
      :expanded="open"
      @click="toggle"
    />
    <div v-if="open" class="pc-expand">
      <div v-if="lhError" class="v4-err">登录记录加载失败，请稍后重试<br><button class="v4-retry" @click="loadLoginHistory(true)">重试</button></div>
      <div v-else-if="lhLoading && lhItems.length === 0" class="v4-skeleton" style="height:96px"></div>
      <div v-else-if="lhItems.length === 0" class="lh-empty">还没有登录记录</div>
      <ul v-else class="lh-list">
        <li v-for="(it, i) in lhItems" :key="i" class="lh-row">
          <span class="lh-badge" :class="it.success ? 'ok' : 'bad'">{{ it.success ? '成功' : '失败' }}</span>
          <span class="lh-main">
            <span class="lh-line1">{{ it.reason_label }} · {{ fmtTime(it.created_at) }}</span>
            <span class="lh-line2">{{ it.ip || '未知 IP' }}<span v-if="it.region"> · {{ it.region }}</span></span>
            <span class="lh-ua" :title="it.user_agent || ''">{{ it.user_agent || '未知设备' }}</span>
          </span>
        </li>
      </ul>
      <div v-if="!lhError && lhItems.length < lhTotal" class="lh-more">
        <button type="button" class="v4-retry" :disabled="lhLoading" @click="loadLoginHistory(false)">{{ lhLoading ? '正在加载…' : '加载更多' }}</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 登录记录（自旧 AccountSettingsView 原样抽出）：只读、只查本人，
 * limit/offset 分页；首次展开才加载（数据与原页一致）。加载失败留
 * 错误态重试，不静默吞。
 */
import { ref } from 'vue'
import { History } from 'lucide-vue-next'
import PcEntryRow from './PcEntryRow.vue'
import { authApi, type LoginHistoryItem } from '@/api'
import { parseServerTime } from '@/utils/serverTime'

const LH_PAGE = 20

const open = ref(false)
const started = ref(false)
const lhItems = ref<LoginHistoryItem[]>([])
const lhTotal = ref(0)
const lhLoading = ref(false)
const lhError = ref(false)

function toggle() {
  open.value = !open.value
  if (open.value && !started.value) {
    started.value = true
    void loadLoginHistory(true)
  }
}
function fmtTime(iso: string | null): string {
  if (!iso) return '时间未知'
  // 登录记录是 UTC naive 串，须按 UTC 解析再转本地（PA-047）
  const d = parseServerTime(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}
async function loadLoginHistory(reset: boolean) {
  if (lhLoading.value) return
  lhLoading.value = true
  lhError.value = false
  try {
    const data = await authApi.getLoginHistory({
      limit: LH_PAGE,
      offset: reset ? 0 : lhItems.value.length,
    })
    lhTotal.value = data.total
    lhItems.value = reset ? data.items : [...lhItems.value, ...data.items]
  } catch {
    lhError.value = true
  } finally {
    lhLoading.value = false
  }
}
</script>

<style scoped>
.pc-expand {
  border-top: 1px dashed var(--line);
  padding: 12px 14px 14px;
}
.lh-empty { font-size: 13px; color: var(--ink2); padding: 10px 2px; }
.lh-list { list-style: none; margin: 0; padding: 0; }
.lh-row {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 9px 2px; border-top: 1px solid var(--paper2);
}
.lh-row:first-child { border-top: none; }
.lh-badge {
  flex-shrink: 0; font-size: 11px; line-height: 1;
  padding: 4px 7px; border-radius: 999px; margin-top: 1px;
  border: 1px solid var(--au-border, #e3e3e2); color: var(--ink2);
}
.lh-badge.ok { color: var(--au-primary, #016a71); border-color: var(--au-primary, #016a71); }
.lh-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.lh-line1 { font-size: 13px; color: var(--ink); }
.lh-line2 { font-size: 12px; color: var(--ink2); }
.lh-ua {
  font-size: 11px; color: var(--ink2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;
}
.lh-more { text-align: center; margin-top: 10px; }
</style>
