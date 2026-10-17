<!-- 桌面发现页底部 · 媒体库速览带（C档第2批）
     数据源：/api/panel/server 的 eco（与服务器页片库生态同口径）——
     真实库名 + 真实条数（movie/series 口径）。eco 只含非空库；
     条数为 0 的库显示「去看看 →」，不编数。 -->
<template>
  <section v-if="libs.length > 0" id="disc-libs" class="d3-band" aria-label="媒体库速览">
    <div class="d3-sec-head">
      <div>
        <span class="d3-k">LIBRARY</span>
        <div class="d3-sec-title">媒体库速览</div>
        <div class="d3-sec-sub">{{ libs.length }} 个库 · 全部直挂可播</div>
      </div>
      <RouterLink to="/server" class="d3-sec-link">服务器页 <span aria-hidden="true">→</span></RouterLink>
    </div>
    <div class="d3-lib-grid">
      <div v-for="lib in libs" :key="lib.library_id" class="d3-card d3-lib-card d3-hover">
        <div class="ln">{{ lib.name }}</div>
        <div class="lc">
          <template v-if="lib.count > 0">{{ lib.count.toLocaleString('zh-CN') }} 条</template>
          <template v-else>去看看 <span aria-hidden="true">→</span></template>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { RouterLink } from 'vue-router'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import type { EcoItem } from '@/composables/usePanelApi'

const panel = usePanelStore()
const user = useUserStore()
const { server } = storeToRefs(panel)

const libs = computed((): EcoItem[] => {
  const d = (server.value.data || {}) as Record<string, unknown>
  const eco = d.eco as EcoItem[] | undefined
  return Array.isArray(eco) ? eco : []
})

onMounted(() => {
  if (user.isLoggedIn) void panel.loadServer()
})
</script>
