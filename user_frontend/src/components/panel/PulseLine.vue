<!-- 桌面发现页左栏 · 脉搏一行摘要（C档第2批）
     数据源：/api/panel/server（与服务器页同口径）——今日入库取生长记录
     当天条数，待刮取 vitals.backlog。配额无用户端数据源，不展示。
     中栏实时动态流已随第3批上线（/api/panel/feed），摘要卡链接指回页内锚点 #feed。 -->
<template>
  <section v-if="!failed" class="d3-card d3-pulse-line" aria-label="服务器脉搏摘要">
    <div v-if="loading" class="d3-skel" style="height:20px;width:100%" aria-hidden="true"></div>
    <template v-else>
      <div class="d3-pulse-one">
        服务器脉搏 · 今日入库 <b>{{ inboundToday }}</b> · 待刮 <b>{{ backlog }}</b>
      </div>
      <a href="#feed" class="d3-sec-link">实时动态 <span aria-hidden="true">↓</span></a>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'

const panel = usePanelStore()
const user = useUserStore()
const { server } = storeToRefs(panel)

const loading = ref(true)
const failed = ref(false)

const backlog = computed(() => {
  const d = (server.value.data || {}) as Record<string, unknown>
  const v = d.vitals as Record<string, unknown> | undefined
  const b = v?.backlog
  return typeof b === 'number' ? b : 0
})
const inboundToday = computed(() => {
  const d = (server.value.data || {}) as Record<string, unknown>
  const g = d.growth as Array<{ count?: number }> | undefined
  if (Array.isArray(g) && g.length > 0) {
    const last = g[g.length - 1]
    if (typeof last?.count === 'number') return last.count
  }
  return 0
})

onMounted(async () => {
  if (!user.isLoggedIn) {
    failed.value = true
    return
  }
  try {
    await panel.loadServer()
    failed.value = !!server.value.error
  } catch {
    failed.value = true
  } finally {
    loading.value = false
  }
})
</script>
