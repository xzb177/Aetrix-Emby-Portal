<!-- 桌面发现页中栏 · 继续观看（C档第2批）
     数据源：GET /api/user/emby/resume（门户「继续观看」口径，
     backend/emby_server/portal.py get_resume_list）——有进度、未播完、
     最近播放的在前；单集按剧聚合。字段映射：
     标题=series_name ?? name；副标题=第N集·还剩X / 已看X（由
     position_ticks/duration_ticks 算出）；进度条=progress（0–100）。
     封面用字排封面（本站不展示海报，用户 2026-10-14 口径）。
     点击卡片：Emby 网页深链（与包裹卡「去看」同口径，新开标签页）；
     拉不到 EA 地址时卡片不可点，不造死链。 -->
<template>
  <section id="disc-watch" aria-label="继续观看">
    <div class="d3-sec-head">
      <div>
        <span class="d3-k">CONTINUE</span>
        <div class="d3-sec-title">继续观看</div>
        <div class="d3-sec-sub">从上次停的地方接着看</div>
      </div>
    </div>
    <div v-if="loading" class="d3-watch-grid" aria-hidden="true">
      <div v-for="n in 4" :key="n" class="d3-skel" style="height:190px"></div>
    </div>
    <div v-else-if="error" class="d3-err">
      继续观看没加载出来：{{ error }}<button type="button" class="d3-retry" @click="load(true)">重试</button>
    </div>
    <div v-else-if="items.length === 0" class="d3-empty">最近没有看到一半的内容，去片库逛逛吧</div>
    <div v-else class="d3-watch-grid">
      <button
        v-for="w in items"
        :key="w.id"
        type="button"
        class="d3-watch d3-hover"
        :aria-label="`继续看${watchTitle(w)}`"
        @click="openItem(w)"
      >
        <div class="d3-cover" :class="toneOf(w)">
          <div class="d3-cov-k">{{ kickerOf(w) }}</div>
          <div class="d3-cov-t">{{ watchTitle(w) }}</div>
        </div>
        <div class="d3-bar" aria-hidden="true"><i :style="{ width: pctOf(w) + '%' }"></i></div>
        <div class="d3-watch-body">
          <div class="d3-watch-t">{{ watchTitle(w) }}</div>
          <div class="d3-watch-s">{{ subOf(w) }}</div>
        </div>
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { embyApi, type PortalResumeItem } from '@/api'
import { ensureEmbyBase, TICKS_PER_SECOND } from '@/api/emby'
import { useUserStore } from '@/stores/user'
import { humanizeError } from '@/composables/usePanelApi'

const user = useUserStore()
const items = ref<PortalResumeItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

async function load(force = false) {
  if (!force && items.value.length > 0) return
  loading.value = true
  error.value = null
  try {
    const d = await embyApi.getResume(8)
    items.value = Array.isArray(d?.items) ? d.items : []
  } catch (e) {
    error.value = humanizeError(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (user.isLoggedIn) void load()
  else loading.value = false
})

/** 双色调按 id 稳定散列（与货架同算法） */
function toneOf(w: PortalResumeItem): string {
  const s = String(w.id ?? '')
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return `d3-tone-${'abcd'[h % 4]}`
}
/** 封面 kicker：只映射真实 type，不编造细分 */
function kickerOf(w: PortalResumeItem): string {
  return w.type === 'series' ? 'TV SERIES' : 'FILM'
}
function watchTitle(w: PortalResumeItem): string {
  return w.series_name || w.name || '未命名'
}
function pctOf(w: PortalResumeItem): number {
  const p = Number(w.progress ?? 0)
  return Number.isFinite(p) ? Math.max(0, Math.min(100, p)) : 0
}
function fmtMins(ticks?: number | null): string {
  const s = Math.max(0, Math.round((ticks ?? 0) / TICKS_PER_SECOND))
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分钟`
  const h = Math.floor(m / 60)
  return `${h} 小时${m % 60 ? `${m % 60} 分` : ''}`
}
function subOf(w: PortalResumeItem): string {
  const pos = w.position_ticks ?? 0
  const dur = w.duration_ticks ?? 0
  if (w.type === 'series' && typeof w.episode_number === 'number') {
    const rest = dur > pos ? ` · 还剩${fmtMins(dur - pos)}` : ''
    return `第${w.episode_number}集${rest}`
  }
  if (dur > 0) return `已看${fmtMins(pos)}`
  return '上次看到这里'
}
/** 点击进 Emby 网页深链（包裹卡同口径）；无 EA 地址时不跳转 */
async function openItem(w: PortalResumeItem) {
  const base = await ensureEmbyBase()
  if (!base) return
  const id = w.episode_id || w.id
  window.open(`${base}/web/index.html#!/item?id=${encodeURIComponent(String(id))}`, '_blank', 'noopener')
}
</script>
