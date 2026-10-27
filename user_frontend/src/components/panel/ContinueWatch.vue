<!-- 发现页中栏 · 继续观看（C档第2批；桌面与手机都渲染，手机 2 列）
     数据源：GET /api/user/emby/resume（门户「继续观看」口径，
     backend/emby_server/portal.py get_resume_list）——有进度、未播完、
     最近播放的在前；单集按剧聚合。字段映射：
     标题=series_name ?? name；副标题=第N集·还剩X / 已看X（由
     position_ticks/duration_ticks 算出）；进度条=progress（0–100）。
     封面用字排封面（本站不展示海报，用户 2026-10-14 口径）。
     点击卡片：播放器选择（Rex / SenPlayer / VidHub，与包裹「去看」同口径），
     不打开 Emby 网页。 -->
<template>
  <section id="disc-watch" aria-label="继续观看">
    <div class="d3-sec-head">
      <div>
        <span class="d3-k">CONTINUE</span>
        <div class="d3-sec-title">继续观看</div>
        <div class="d3-sec-sub">从上次停的地方接着看</div>
      </div>
    </div>
    <div v-if="!provided && loading" class="d3-watch-grid" aria-hidden="true">
      <div v-for="n in 4" :key="n" class="d3-skel" style="height:190px"></div>
    </div>
    <div v-else-if="!provided && error" class="d3-err">
      继续观看没加载出来：{{ error }}<button type="button" class="d3-retry" @click="load(true)">重试</button>
    </div>
    <div v-else-if="shown.length === 0" class="d3-empty">最近没有看到一半的内容，去片库逛逛吧</div>
    <div v-else class="d3-watch-grid">
      <!-- 藏按钮不能嵌在卡片 button 里，包一层 wrapper 放卡片外 -->
      <div v-for="w in shown" :key="w.id" class="d3-watch-wrap">
        <button
          type="button"
          class="d3-watch d3-hover"
          :aria-label="`继续看${watchTitle(w)}`"
          @click="openPicker(w)"
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
        <button
          type="button"
          class="d3-watch-hide"
          :aria-label="`不再显示${watchTitle(w)}`"
          title="不再显示"
          @click="hideItem(w)"
        >×</button>
      </div>
    </div>
    <!-- 已藏清单（L9）：懒加载，点开才取；恢复后自有模式重拉续看，注册表模式通知父级重拉分区 -->
    <div class="d3-hidden-toggle">
      <button type="button" @click="toggleHidden">{{ hiddenLabel }}</button>
    </div>
    <div v-if="showHidden" class="d3-hidden-list">
      <div v-if="hiddenLoading" class="d3-skel" style="height:60px" aria-hidden="true"></div>
      <div v-else-if="hiddenItems.length === 0" class="d3-empty">没有藏起来的内容</div>
      <div v-else>
        <div v-for="h in hiddenItems" :key="h.item_guid" class="d3-hidden-row">
          <span>{{ h.display_name }}<template v-if="typeof h.episode_number === 'number'"> · 第{{ h.episode_number }}集</template></span>
          <button type="button" @click="unhideItem(h)">恢复</button>
        </div>
      </div>
    </div>
    <PlayerSchemeSheet v-model:open="pickerOpen" :title="pickerTitle" />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { embyApi, type HiddenResumeItem, type PortalResumeItem } from '@/api'
import { TICKS_PER_SECOND } from '@/api/emby'
import { useUserStore } from '@/stores/user'
import { humanizeError } from '@/composables/usePanelApi'
import PlayerSchemeSheet from '@/components/panel/PlayerSchemeSheet.vue'

/** items 由父级（首页分区）传入时走注册表模式：不自取、不显示加载/错误态，
    藏/恢复后通知父级（父级改分区数据或重拉）；不传时走旧自取模式。 */
const props = defineProps<{ items?: PortalResumeItem[] | null }>()
const emit = defineEmits<{ hidden: [guid: string]; unhidden: [guid: string] }>()

const user = useUserStore()
const items = ref<PortalResumeItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const pickerOpen = ref(false)
const pickerTitle = ref('')
const provided = computed(() => props.items != null)
const shown = computed(() => props.items ?? items.value)

const showHidden = ref(false)
const hiddenItems = ref<HiddenResumeItem[]>([])
const hiddenLoading = ref(false)
const hiddenCount = ref<number | null>(null)
const hiddenLabel = computed(() =>
  showHidden.value ? '收起已隐藏' : hiddenCount.value == null ? '已隐藏' : `已隐藏（${hiddenCount.value}）`,
)

async function load(force = false) {
  if (provided.value) { loading.value = false; return }
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

/** 藏：剧集卡传单集 guid（播的是那集），电影卡传电影 guid。 */
async function hideItem(w: PortalResumeItem) {
  const guid = w.episode_id ?? w.id
  if (!guid) return
  try {
    await embyApi.hideResumeItem(guid)
  } catch (e) {
    error.value = humanizeError(e)
    return
  }
  hiddenCount.value = null
  if (provided.value) emit('hidden', guid)
  else items.value = items.value.filter((it) => (it.episode_id ?? it.id) !== guid)
  if (showHidden.value) void loadHidden()
}

async function loadHidden() {
  hiddenLoading.value = true
  try {
    const d = await embyApi.getHiddenResume()
    hiddenItems.value = Array.isArray(d?.items) ? d.items : []
    hiddenCount.value = hiddenItems.value.length
  } catch (e) {
    error.value = humanizeError(e)
  } finally {
    hiddenLoading.value = false
  }
}

function toggleHidden() {
  showHidden.value = !showHidden.value
  if (showHidden.value) void loadHidden()
}

async function unhideItem(h: HiddenResumeItem) {
  try {
    await embyApi.unhideResumeItem(h.item_guid)
  } catch (e) {
    error.value = humanizeError(e)
    return
  }
  hiddenItems.value = hiddenItems.value.filter((it) => it.item_guid !== h.item_guid)
  hiddenCount.value = hiddenItems.value.length
  if (provided.value) emit('unhidden', h.item_guid)
  else void load(true)
}

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
/** 打开播放器选择。不拼 Emby 网页地址，也不把条目 id 写进 scheme。 */
function openPicker(w: PortalResumeItem) {
  pickerTitle.value = watchTitle(w)
  pickerOpen.value = true
}
</script>

<style scoped>
/* 藏按钮：桌面悬停/聚焦时出现；触屏（粗指针）下常显，触控区 44px，
   视觉 × 居中，不靠 hover 才能点到。 */
.d3-watch-wrap { position: relative; }
.d3-watch-wrap .d3-watch { width: 100%; }
.d3-watch-hide {
  position: absolute; top: 6px; right: 6px; z-index: 2;
  width: 44px; height: 44px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  border: none; background: rgba(0, 0, 0, 0.45); color: #fff;
  font-size: 14px; line-height: 1; cursor: pointer;
  opacity: 0; transition: opacity 0.15s;
}
.d3-watch-wrap:hover .d3-watch-hide,
.d3-watch-hide:focus-visible { opacity: 1; }
@media (pointer: coarse) {
  .d3-watch-hide { opacity: 1; }
}
@media (max-width: 767px) {
  /* 手机继续观看：4 列会挤成条，改 2 列紧凑列表；底部导航留白由 v4-page 统一口径承担 */
  .d3-watch-grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
}
.d3-hidden-toggle { margin-top: 10px; text-align: right; }
.d3-hidden-toggle button {
  background: none; border: none; cursor: pointer;
  font-size: 13px; opacity: 0.7;
}
.d3-hidden-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 0; border-bottom: 1px solid rgba(0, 0, 0, 0.08);
}
.d3-hidden-row button {
  background: none; border: 1px solid rgba(0, 0, 0, 0.2);
  border-radius: 999px; padding: 2px 10px; cursor: pointer; font-size: 13px;
}
</style>
