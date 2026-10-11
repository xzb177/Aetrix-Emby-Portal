<script setup lang="ts">
/**
 * 网页播放页
 *
 * - 优先直连 stream（浏览器原生支持 MP4/WebM，Range 分段）
 * - 直连不可用时回退 HLS 转码（hls.js；Safari 原生支持）
 * - 播放进度节流上报（Sessions/Playing/Progress），与 Emby 客户端互通续播
 * - 自动从上次进度续播；播完（>=95%）自动标记已看
 * - 剧集播完自动连播下一集（同季优先，其次下一季第一集），可取消
 * - 手势：双击左/右快退/快进 10s；左侧上下滑动调亮度，右侧调音量
 * - 字幕轨可切换；音轨切换待后端转码支持（TODO）
 */
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
// P2#12：hls.js（594KB）改动态 import，只在非 Safari 需要 HLS 转码时才加载，
// 首屏/路由分包不再背这个体积
type HlsClass = typeof import('hls.js').default
import {
  embyApi, posterUrl, ticksToSeconds, type EmbyItem, type EmbyMediaSource,
} from '@/api/emby'
import { useToast } from '@/composables/useToast'
import { useUserStore } from '@/stores/user'
import { pageTitle } from '@/composables/useBranding'
import {
  Play, Pause, Volume2, VolumeX, Maximize, ChevronLeft, Film,
  Crown, Sparkles, CalendarCheck, Wallet, ListVideo, Captions,
  X, Check, Loader2, Sun,
} from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const userStore = useUserStore()

const videoRef = ref<HTMLVideoElement | null>(null)
const containerRef = ref<HTMLDivElement | null>(null)
const seekBarRef = ref<HTMLDivElement | null>(null)

const item = ref<EmbyItem | null>(null)
const loading = ref(true)
/** 直连探测中：避免 8s 黑屏无反馈 */
const probing = ref(false)
const playError = ref('')
const playMethod = ref<'DirectStream' | 'HLS'>('DirectStream')
// 付费墙拦截：需要订阅才能播放
const paywalled = ref(false)
const paywallMessage = ref('')

/** 字幕轨：全部可投递文本字幕；-1 = 关闭字幕 */
const subtitleTracks = ref<{ label: string; url: string }[]>([])
const selectedSub = ref(-1)
const activeSub = computed(() =>
  selectedSub.value >= 0 ? subtitleTracks.value[selectedSub.value] || null : null,
)
/** 音轨：直连为单文件，HTML5 无法切换；仅展示预留，待后端转码支持 */
// TODO(backend): 音轨切换需要服务端转码（/Videos/{id}/master.m3u8 多音轨）支持
const audioTracks = ref<{ label: string }[]>([])

// 播放器状态
const isPlaying = ref(false)
const isPaused = ref(true)
const muted = ref(false)
const currentTime = ref(0)
const duration = ref(0)
const volume = ref(1)
const brightness = ref(1)
const showControls = ref(true)

let hls: import('hls.js').default | null = null
let hlsCtor: HlsClass | null = null

/** 按需加载 hls.js，失败返回 null（走错误提示） */
async function ensureHls(): Promise<HlsClass | null> {
  if (!hlsCtor) {
    try {
      hlsCtor = (await import('hls.js')).default
    } catch {
      return null
    }
  }
  return hlsCtor
}
let controlsTimer: ReturnType<typeof setTimeout> | null = null
let progressTimer: ReturnType<typeof setInterval> | null = null
let reportedStart = false

const itemId = computed(() => route.params.id as string)
const poster = computed(() => (item.value ? posterUrl(item.value, 800) : ''))
const progressPct = computed(() =>
  duration.value > 0 ? (currentTime.value / duration.value) * 100 : 0,
)
/** 是否为剧集单集（可连播/选集） */
const isEpisode = computed(
  () => item.value?.Type === 'Episode' && !!item.value?.SeriesId,
)

const fmt = (s: number) => {
  if (!s || s < 0) s = 0
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60)
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  return `${h > 0 ? h + ':' : ''}${mm}:${String(sec).padStart(2, '0')}`
}
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v))

// ==================== 播放源解析 ====================

function destroyHls() {
  if (hls) {
    hls.destroy()
    hls = null
  }
}

async function resolveAndPlay() {
  const video = videoRef.value
  if (!video) return
  playError.value = ''

  try {
    const info = await embyApi.getPlaybackInfo(itemId.value)
    const source: EmbyMediaSource | undefined = info.MediaSources?.[0]

    // 字幕：收集全部可投递文本字幕，默认选中服务端默认轨
    const subs = (source?.MediaStreams || []).filter(
      (s) => s.Type === 'Subtitle' && s.IsTextSubtitleStream && s.DeliveryUrl,
    )
    subtitleTracks.value = subs.map((s) => ({
      label: s.DisplayTitle || s.Language || '字幕',
      url: s.DeliveryUrl as string,
    }))
    const defIdx = subs.findIndex((s) => s.IsDefault)
    selectedSub.value = defIdx >= 0 ? defIdx : subs.length ? 0 : -1

    // 音轨：仅展示（直连单文件无法切换）
    audioTracks.value = (source?.MediaStreams || [])
      .filter((s) => s.Type === 'Audio')
      .map((a) => ({ label: a.DisplayTitle || a.Language || a.Codec || '音轨' }))

    // 1) 直连优先（服务器地址与页面同源，地址已附短期播放签名；JWT 不进 URL）
    const directUrl = source?.DirectStreamUrl
    probing.value = true
    let directOk = false
    try {
      directOk = directUrl ? await tryDirect(directUrl) : false
    } finally {
      probing.value = false
    }
    if (directOk) {
      playMethod.value = 'DirectStream'
      return
    }

    // 2) HLS 回退
    const hlsUrl = source?.TranscodingUrl
    if (hlsUrl) {
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        // Safari 原生
        video.src = hlsUrl
        playMethod.value = 'HLS'
        await video.play().catch(() => {})
        return
      }
      const Hls = await ensureHls()
      if (Hls && Hls.isSupported()) {
        destroyHls()
        hls = new Hls({ enableWorker: true, lowLatencyMode: false, backBufferLength: 60 })
        hls.loadSource(hlsUrl)
        hls.attachMedia(video)
        hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}))
        hls.on(Hls.Events.ERROR, (_e, data) => {
          if (data.fatal) {
            playError.value = '转码流加载失败，请稍后重试或使用外部播放器'
          }
        })
        playMethod.value = 'HLS'
        return
      }
    }

    playError.value = '没有可用的播放方式（直连与转码均不可用）'
  } catch (err: any) {
    // 403 = 付费墙拦截（后端返回可读文案）
    if (err?.response?.status === 403) {
      paywalled.value = true
      paywallMessage.value = err?.response?.data?.detail || '需要有效的会员订阅才能播放'
      return
    }
    playError.value = err?.response?.data?.detail || '获取播放信息失败'
  }
}

/** 探测直连是否可用（HEAD 不被 Range 服务支持时用 GET Range 小段） */
function tryDirect(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = new XMLHttpRequest()
    probe.open('GET', url, true)
    probe.setRequestHeader('Range', 'bytes=0-1')
    probe.timeout = 8000
    probe.onload = () => {
      if (probe.status >= 200 && probe.status < 300) {
        const video = videoRef.value
        if (video) video.src = url
        resolve(true)
      } else {
        resolve(false)
      }
    }
    probe.onerror = () => resolve(false)
    probe.ontimeout = () => resolve(false)
    probe.send()
  })
}

// ==================== 进度上报 ====================

function reportProgress() {
  const video = videoRef.value
  if (!video) return
  embyApi.reportProgress(itemId.value, video.currentTime, video.paused, playMethod.value).catch(() => {})
}

function startProgressLoop() {
  if (progressTimer) return
  // 每 10 秒上报一次进度
  progressTimer = setInterval(reportProgress, 10_000)
}

function stopProgressLoop() {
  if (progressTimer) {
    clearInterval(progressTimer)
    progressTimer = null
  }
}

/**
 * P2#11：切后台/锁屏时暂停每 10 秒的进度上报，省请求；
 * 回来时立即上报一次再恢复定时器，避免丢进度。
 */
function onVisibilityChange() {
  if (document.hidden) {
    stopProgressLoop()
  } else {
    reportProgress()
    startProgressLoop()
  }
}

// ==================== 连播下一集 ====================

const nextEpisode = ref<EmbyItem | null>(null)
const nextCountdown = ref(0)
let nextTimer: ReturnType<typeof setInterval> | null = null

const nextEpLabel = computed(() => {
  const e = nextEpisode.value
  if (!e) return ''
  const num = e.IndexNumber != null ? `第${e.IndexNumber}集 ` : ''
  return `${num}${e.Name || ''}`.trim()
})

/** 找下一集：同季按集号，其次下一季第一集 */
async function findNextEpisode(): Promise<EmbyItem | null> {
  const it = item.value
  if (!it || it.Type !== 'Episode' || !it.SeriesId) return null
  try {
    const eps = await embyApi.getEpisodes(it.SeriesId, it.SeasonId || undefined)
    const sorted = [...eps].sort((a, b) => (a.IndexNumber ?? 0) - (b.IndexNumber ?? 0))
    const idx = sorted.findIndex((e) => e.Id === it.Id)
    if (idx >= 0 && idx + 1 < sorted.length) return sorted[idx + 1]
    // 本季播完：下一季第一集
    const seasons = await embyApi.getSeasons(it.SeriesId)
    const sIdx = seasons.findIndex((s) => s.Id === it.SeasonId)
    if (sIdx >= 0 && sIdx + 1 < seasons.length) {
      const neps = await embyApi.getEpisodes(it.SeriesId, seasons[sIdx + 1].Id)
      const nsorted = [...neps].sort((a, b) => (a.IndexNumber ?? 0) - (b.IndexNumber ?? 0))
      return nsorted[0] || null
    }
  } catch {
    /* 连播失败就停住，不打扰用户 */
  }
  return null
}

function goNext() {
  if (nextTimer) {
    clearInterval(nextTimer)
    nextTimer = null
  }
  const id = nextEpisode.value?.Id
  nextEpisode.value = null
  if (id) router.push(`/watch/${id}`)
}

function cancelNext() {
  if (nextTimer) {
    clearInterval(nextTimer)
    nextTimer = null
  }
  nextEpisode.value = null
  toast.success('播放完成，已标记为看过')
}

// ==================== 播放器事件 ====================

function onLoadedMetadata() {
  const video = videoRef.value
  if (!video) return
  duration.value = video.duration || 0

  // 续播：仅当接近片头（避免覆盖已有会话）且服务端有进度
  const saved = item.value?.UserData?.PlaybackPositionTicks || 0
  const savedSec = ticksToSeconds(saved)
  if (savedSec > 10 && video.currentTime < 5) {
    video.currentTime = savedSec
    toast.success(`已从 ${fmt(savedSec)} 继续播放`)
  }
}

function onPlay() {
  isPlaying.value = true
  isPaused.value = false
  startProgressLoop()
  showControlsTemporarily()
  const video = videoRef.value
  if (video && !reportedStart) {
    reportedStart = true
    embyApi.reportPlaying(itemId.value, video.currentTime, playMethod.value).catch(() => {})
  }
}

function onPause() {
  isPlaying.value = false
  isPaused.value = true
  reportProgress()
}

async function onEnded() {
  stopProgressLoop()
  reportProgress()
  // 播完自动标记已看
  embyApi.setPlayed(itemId.value, true).catch(() => {})
  // 剧集：尝试连播下一集
  const nxt = await findNextEpisode()
  if (nxt) {
    nextEpisode.value = nxt
    nextCountdown.value = 5
    nextTimer = setInterval(() => {
      nextCountdown.value -= 1
      if (nextCountdown.value <= 0) goNext()
    }, 1000)
  } else {
    toast.success('播放完成，已标记为看过')
  }
}

function onTimeUpdate() {
  const video = videoRef.value
  if (video && !seeking.value) currentTime.value = video.currentTime
}

function onVolumeChange() {
  const video = videoRef.value
  if (video) {
    volume.value = video.volume
    muted.value = video.muted
  }
}

function togglePlay() {
  const video = videoRef.value
  if (!video) return
  if (video.paused) video.play().catch(() => {})
  else video.pause()
}

function toggleMute() {
  const video = videoRef.value
  if (!video) return
  video.muted = !video.muted
}

function seekBy(sec: number) {
  const video = videoRef.value
  if (!video || !duration.value) return
  video.currentTime = clamp(video.currentTime + sec, 0, duration.value)
  currentTime.value = video.currentTime
  flashTip(sec > 0 ? `快进 ${sec} 秒` : `快退 ${-sec} 秒`)
}

// ==================== 进度条拖拽 ====================

const seeking = ref(false)

function seekToPct(clientX: number) {
  const video = videoRef.value
  const bar = seekBarRef.value
  if (!video || !bar || !duration.value) return
  const rect = bar.getBoundingClientRect()
  const pct = clamp((clientX - rect.left) / rect.width, 0, 1)
  video.currentTime = pct * duration.value
  currentTime.value = video.currentTime
}

function onSeekDown(e: PointerEvent) {
  seeking.value = true
  ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
  seekToPct(e.clientX)
  onUserActivity()
}

function onSeekMove(e: PointerEvent) {
  if (!seeking.value) return
  seekToPct(e.clientX)
}

function onSeekUp() {
  seeking.value = false
}

function setVolume(e: Event) {
  const video = videoRef.value
  if (!video) return
  video.volume = parseFloat((e.target as HTMLInputElement).value)
}

function fullscreen() {
  const el = containerRef.value
  if (!el) return
  if (document.fullscreenElement) document.exitFullscreen()
  else el.requestFullscreen().catch(() => {})
}

function onUserActivity() {
  showControlsTemporarily()
}

function showControlsTemporarily() {
  showControls.value = true
  if (controlsTimer) clearTimeout(controlsTimer)
  controlsTimer = setTimeout(() => {
    if (isPlaying.value) showControls.value = false
  }, 3000)
}

// ==================== 移动端手势 ====================
// 双击左/右区域：快退/快进 10s；双击中间：播放/暂停
// 左侧上下滑动：亮度；右侧上下滑动：音量

const gestureTip = ref('')
const gestureTipVisible = ref(false)
let gestureTipTimer: ReturnType<typeof setTimeout> | null = null
let tsX = 0
let tsY = 0
let tsT = 0
let touchOnControls = false
let gestureMode: 'none' | 'pan' | 'volume' | 'brightness' = 'none'
let gestureStartVal = 0
let lastTapT = 0
let tapTimer: ReturnType<typeof setTimeout> | null = null
let lastTouchTap = 0

function flashTip(text: string) {
  gestureTip.value = text
  gestureTipVisible.value = true
  if (gestureTipTimer) clearTimeout(gestureTipTimer)
  gestureTipTimer = setTimeout(() => {
    gestureTipVisible.value = false
  }, 900)
  onUserActivity()
}

function onTouchStart(e: TouchEvent) {
  const t = e.touches[0]
  tsX = t.clientX
  tsY = t.clientY
  tsT = Date.now()
  gestureMode = 'none'
  touchOnControls = !!(e.target as HTMLElement).closest('.controls, .back-btn, button, a, input')
  onUserActivity()
}

function onTouchMove(e: TouchEvent) {
  if (touchOnControls) return
  const c = containerRef.value
  const v = videoRef.value
  if (!c || !v) return
  const t = e.touches[0]
  const dx = t.clientX - tsX
  const dy = t.clientY - tsY
  if (gestureMode === 'none') {
    if (Math.abs(dy) > 28 && Math.abs(dy) > Math.abs(dx) * 1.3) {
      const rect = c.getBoundingClientRect()
      const leftSide = t.clientX - rect.left < rect.width / 2
      gestureMode = leftSide ? 'brightness' : 'volume'
      gestureStartVal = leftSide ? brightness.value : v.volume
      e.preventDefault()
    } else if (Math.abs(dx) > 28 || Math.abs(dy) > 28) {
      gestureMode = 'pan'
    }
  } else if (gestureMode === 'volume' || gestureMode === 'brightness') {
    e.preventDefault()
    const nv = gestureStartVal + (tsY - t.clientY) / 220
    if (gestureMode === 'volume') {
      v.volume = clamp(nv, 0, 1)
      if (v.volume > 0 && v.muted) v.muted = false
      flashTip(`音量 ${Math.round(v.volume * 100)}%`)
    } else {
      brightness.value = clamp(nv, 0.4, 1.6)
      flashTip(`亮度 ${Math.round(brightness.value * 100)}%`)
    }
  }
}

function onTouchEnd(e: TouchEvent) {
  const t = e.changedTouches[0]
  const dx = t.clientX - tsX
  const dy = t.clientY - tsY
  const dt = Date.now() - tsT
  const wasTap = gestureMode === 'none' && !touchOnControls && Math.hypot(dx, dy) < 14 && dt < 400
  gestureMode = 'none'
  if (!wasTap) return
  lastTouchTap = Date.now()
  e.preventDefault() // 抑制合成 click，单击走下面的延迟逻辑
  const c = containerRef.value
  if (!c) return
  const rect = c.getBoundingClientRect()
  const x = t.clientX - rect.left
  const now = Date.now()
  if (now - lastTapT < 320) {
    // 双击
    if (tapTimer) {
      clearTimeout(tapTimer)
      tapTimer = null
    }
    lastTapT = 0
    if (x < rect.width * 0.35) seekBy(-10)
    else if (x > rect.width * 0.65) seekBy(10)
    else togglePlay()
  } else {
    // 单击：延迟 320ms，确认不是双击的第一击
    lastTapT = now
    tapTimer = setTimeout(() => {
      tapTimer = null
      lastTapT = 0
      togglePlay()
    }, 320)
  }
}

/** 鼠标点击：touch 已处理过的忽略 */
function onVideoClick() {
  if (Date.now() - lastTouchTap < 600) return
  togglePlay()
}

// ==================== 选集 / 字幕弹窗 ====================

const epSheetOpen = ref(false)
const sheetEpisodes = ref<EmbyItem[]>([])
const sheetLoading = ref(false)
const subSheetOpen = ref(false)

let scrollLocks = 0
function lockScroll() {
  scrollLocks++
  document.body.style.overflow = 'hidden'
}
function unlockScroll() {
  scrollLocks = Math.max(0, scrollLocks - 1)
  if (!scrollLocks) document.body.style.overflow = ''
}

async function openEpSheet() {
  const it = item.value
  if (!it || it.Type !== 'Episode' || !it.SeriesId) return
  epSheetOpen.value = true
  sheetLoading.value = true
  try {
    const eps = await embyApi.getEpisodes(it.SeriesId, it.SeasonId || undefined)
    sheetEpisodes.value = [...eps].sort((a, b) => (a.IndexNumber ?? 0) - (b.IndexNumber ?? 0))
  } catch {
    toast.error('加载选集失败')
  } finally {
    sheetLoading.value = false
  }
}

function pickSub(idx: number) {
  selectedSub.value = idx
  subSheetOpen.value = false
  toast.success(idx < 0 ? '已关闭字幕' : `字幕：${subtitleTracks.value[idx]?.label || ''}`)
}

function closeSheets() {
  epSheetOpen.value = false
  subSheetOpen.value = false
}

function onGlobalKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (subSheetOpen.value) subSheetOpen.value = false
  else if (epSheetOpen.value) epSheetOpen.value = false
}

watch(epSheetOpen, (open) => {
  if (open) lockScroll()
  else unlockScroll()
})
watch(subSheetOpen, (open) => {
  if (open) lockScroll()
  else unlockScroll()
})

// ==================== 生命周期 ====================

function resetPlayer() {
  stopProgressLoop()
  destroyHls()
  if (nextTimer) {
    clearInterval(nextTimer)
    nextTimer = null
  }
  nextEpisode.value = null
  closeSheets()
  const v = videoRef.value
  if (v) {
    v.pause()
    v.removeAttribute('src')
    v.load()
  }
  item.value = null
  playError.value = ''
  paywalled.value = false
  paywallMessage.value = ''
  subtitleTracks.value = []
  selectedSub.value = -1
  audioTracks.value = []
  currentTime.value = 0
  duration.value = 0
  isPlaying.value = false
  isPaused.value = true
  brightness.value = 1
  showControls.value = true
  reportedStart = false
  probing.value = false
  loading.value = true
}

async function initPlayback() {
  try {
    item.value = await embyApi.getItem(itemId.value)
    document.title = pageTitle(`播放 ${item.value.Name}`)
  } catch {
    toast.error('加载影片信息失败')
    loading.value = false
    return
  }
  loading.value = false
  await resolveAndPlay()
}

onMounted(() => {
  document.addEventListener('keydown', onGlobalKeydown)
  document.addEventListener('visibilitychange', onVisibilityChange)
  initPlayback()
})

// 同一组件内切集（连播/选集）：重置后重新加载
watch(itemId, (id, oldId) => {
  if (id && id !== oldId) {
    resetPlayer()
    initPlayback()
  }
})

onBeforeUnmount(() => {
  const video = videoRef.value
  if (video && video.currentTime > 0) {
    embyApi.reportStopped(itemId.value, video.currentTime, playMethod.value).catch(() => {})
  }
  stopProgressLoop()
  destroyHls()
  if (controlsTimer) clearTimeout(controlsTimer)
  if (nextTimer) clearInterval(nextTimer)
  if (gestureTipTimer) clearTimeout(gestureTipTimer)
  if (tapTimer) clearTimeout(tapTimer)
  document.removeEventListener('keydown', onGlobalKeydown)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  scrollLocks = 0
  document.body.style.overflow = ''
  document.title = pageTitle()
})
</script>

<template>
  <div class="watch-view" :class="{ 'hide-cursor': !showControls && isPlaying }">
    <div
      ref="containerRef"
      class="player-container"
      @mousemove="onUserActivity"
      @touchstart="onTouchStart"
      @touchmove="onTouchMove"
      @touchend="onTouchEnd"
    >
      <!-- 海报占位 -->
      <div v-if="loading" class="poster-layer" :style="poster ? { backgroundImage: `url(${poster})` } : {}">
        <div class="poster-shade">
          <Film :size="28" class="pulse" />
          <p>准备播放…</p>
        </div>
      </div>

      <video
        ref="videoRef"
        class="video"
        playsinline
        preload="metadata"
        :style="brightness !== 1 ? { filter: `brightness(${brightness})` } : {}"
        @loadedmetadata="onLoadedMetadata"
        @play="onPlay"
        @pause="onPause"
        @ended="onEnded"
        @timeupdate="onTimeUpdate"
        @volumechange="onVolumeChange"
        @click="onVideoClick"
      >
        <track
          v-if="activeSub"
          :key="activeSub.url"
          kind="subtitles"
          :src="activeSub.url"
          :label="activeSub.label"
          default
        />
      </video>

      <!-- 直连探测中：避免黑屏无反馈 -->
      <div v-if="probing" class="probe-layer">
        <Loader2 :size="26" class="spin" />
        <p>正在连接播放源…</p>
      </div>

      <!-- 手势提示 -->
      <div v-if="gestureTipVisible" class="gesture-tip">
        <Sun v-if="gestureTip.startsWith('亮度')" :size="18" />
        <Volume2 v-else-if="gestureTip.startsWith('音量')" :size="18" />
        <span>{{ gestureTip }}</span>
      </div>

      <!-- 连播下一集 -->
      <div v-if="nextEpisode" class="nextup-layer">
        <div class="nextup-card">
          <p class="nextup-kicker">即将播放下一集 · {{ nextCountdown }}s</p>
          <p class="nextup-title">{{ nextEpLabel }}</p>
          <div class="nextup-actions">
            <button class="btn primary sm" @click="goNext">
              <Play :size="14" />
              立即播放
            </button>
            <button class="btn ghost sm" @click="cancelNext">取消</button>
          </div>
        </div>
      </div>

      <!-- 付费墙：未订阅时引导开通，而不是丢一个播放错误 -->
      <div v-if="paywalled" class="paywall-layer">
        <div class="paywall-card">
          <span class="paywall-icon">
            <Crown :size="24" />
          </span>
          <h2 class="paywall-title">会员专享内容</h2>
          <p class="paywall-text">{{ paywallMessage }}</p>
          <div class="paywall-perks">
            <span><Sparkles :size="13" /> 全库影视任意观看</span>
            <span><CalendarCheck :size="13" /> 多端同步进度与收藏</span>
            <span><Wallet :size="13" /> 支持积分与在线支付</span>
          </div>
          <div class="paywall-actions">
            <RouterLink to="/store" class="btn primary">
              <Crown :size="16" />
              开通会员
            </RouterLink>
            <RouterLink :to="`/media/${itemId}`" class="btn ghost">返回详情</RouterLink>
          </div>
        </div>
      </div>

      <!-- 错误 -->
      <div v-if="playError && !paywalled" class="error-layer">
        <p>{{ playError }}</p>
        <div class="error-actions">
          <button class="btn ghost" @click="resolveAndPlay">重试</button>
          <RouterLink :to="`/media/${itemId}`" class="btn ghost">返回详情</RouterLink>
        </div>
      </div>

      <!-- 返回 -->
      <RouterLink :to="`/media/${itemId}`" class="back-btn" :class="{ visible: showControls }">
        <ChevronLeft :size="18" />
      </RouterLink>

      <!-- 控制条 -->
      <div class="controls" :class="{ visible: showControls || !isPlaying }">
        <!-- 进度条：点击 + 拖拽 -->
        <div
          ref="seekBarRef"
          class="seek"
          :class="{ dragging: seeking }"
          @pointerdown="onSeekDown"
          @pointermove="onSeekMove"
          @pointerup="onSeekUp"
          @pointercancel="onSeekUp"
        >
          <div class="seek-buffer"></div>
          <div class="seek-fill" :style="{ width: progressPct + '%' }"></div>
          <div class="seek-thumb" :style="{ left: progressPct + '%' }"></div>
        </div>

        <div class="controls-row">
          <button class="ctrl-btn" aria-label="播放/暂停" @click="togglePlay">
            <Pause v-if="isPlaying" :size="20" />
            <Play v-else :size="20" />
          </button>

          <div class="volume-wrap">
            <button class="ctrl-btn" aria-label="静音" @click="toggleMute">
              <VolumeX v-if="muted || volume === 0" :size="18" />
              <Volume2 v-else :size="18" />
            </button>
            <input
              class="volume-slider"
              type="range"
              min="0"
              max="1"
              step="0.05"
              :value="muted ? 0 : volume"
              @input="setVolume"
            />
          </div>

          <span class="time">{{ fmt(currentTime) }} / {{ fmt(duration) }}</span>

          <button v-if="isEpisode" class="ctrl-btn" aria-label="选集" @click="openEpSheet">
            <ListVideo :size="20" />
          </button>
          <button class="ctrl-btn" aria-label="字幕与音轨" @click="subSheetOpen = true">
            <Captions :size="20" />
          </button>

          <span class="method-badge">{{ playMethod === 'DirectStream' ? '直连' : '转码' }}</span>

          <button class="ctrl-btn fullscreen" aria-label="全屏" @click="fullscreen">
            <Maximize :size="18" />
          </button>
        </div>
      </div>
    </div>

    <!-- 选集弹窗 -->
    <Teleport to="body">
      <Transition name="sheet">
        <div v-if="epSheetOpen" class="sheet-mask" @click="epSheetOpen = false">
          <div class="sheet" role="dialog" aria-label="选集" @click.stop>
            <div class="sheet-head">
              <p class="sheet-title">选集</p>
              <button class="sheet-close" aria-label="关闭" @click="epSheetOpen = false">
                <X :size="18" />
              </button>
            </div>
            <p v-if="sheetLoading" class="sheet-loading">加载中…</p>
            <ul v-else class="sheet-list">
              <li v-for="ep in sheetEpisodes" :key="ep.Id">
                <button
                  type="button"
                  class="sheet-item"
                  :class="{ active: ep.Id === itemId }"
                  @click="epSheetOpen = false; router.push(`/watch/${ep.Id}`)"
                >
                  <span class="sheet-item-num">{{ ep.IndexNumber ?? '·' }}</span>
                  <span class="sheet-item-name">{{ ep.Name }}</span>
                  <Check v-if="ep.Id === itemId" :size="16" class="sheet-check" />
                </button>
              </li>
            </ul>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- 字幕与音轨弹窗 -->
    <Teleport to="body">
      <Transition name="sheet">
        <div v-if="subSheetOpen" class="sheet-mask" @click="subSheetOpen = false">
          <div class="sheet" role="dialog" aria-label="字幕与音轨" @click.stop>
            <div class="sheet-head">
              <p class="sheet-title">字幕与音轨</p>
              <button class="sheet-close" aria-label="关闭" @click="subSheetOpen = false">
                <X :size="18" />
              </button>
            </div>
            <p class="sheet-group">字幕</p>
            <ul class="sheet-list">
              <li>
                <button
                  type="button"
                  class="sheet-item"
                  :class="{ active: selectedSub === -1 }"
                  @click="pickSub(-1)"
                >
                  <span class="sheet-item-name">关闭字幕</span>
                  <Check v-if="selectedSub === -1" :size="16" class="sheet-check" />
                </button>
              </li>
              <li v-for="(s, i) in subtitleTracks" :key="s.url">
                <button
                  type="button"
                  class="sheet-item"
                  :class="{ active: selectedSub === i }"
                  @click="pickSub(i)"
                >
                  <span class="sheet-item-name">{{ s.label }}</span>
                  <Check v-if="selectedSub === i" :size="16" class="sheet-check" />
                </button>
              </li>
            </ul>
            <p v-if="!subtitleTracks.length" class="sheet-hint">未检测到可用字幕</p>
            <p class="sheet-group">音轨</p>
            <ul class="sheet-list">
              <li v-for="(a, i) in audioTracks" :key="i">
                <button type="button" class="sheet-item" disabled>
                  <span class="sheet-item-name">{{ a.label }}</span>
                  <span v-if="i === 0" class="sheet-tag">当前</span>
                </button>
              </li>
            </ul>
            <p v-if="!audioTracks.length" class="sheet-hint">未检测到多音轨</p>
            <!-- TODO(backend): 直连为单文件，音轨切换需服务端转码支持 -->
            <p class="sheet-hint">音轨切换暂不支持（需后端转码）</p>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- 影片信息 -->
    <div v-if="item" class="container below">
      <h1 class="below-title">{{ item.Name }}</h1>
      <p class="below-meta">
        <span v-if="item.ProductionYear">{{ item.ProductionYear }}</span>
        <span v-if="item.SeriesName">{{ item.SeriesName }}</span>
      </p>
      <p v-if="item.Overview" class="below-overview">{{ item.Overview }}</p>
    </div>
  </div>
</template>

<style scoped>
.watch-view {
  min-height: 100vh;
  background: var(--au-bg);
  color: var(--au-text);
}

.watch-view.hide-cursor {
  cursor: none;
}

.player-container {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  max-height: calc(100vh - 0px);
  /* 故意不用令牌：视频上下留边必须是纯黑，和画面本身一致，
     跟主题的蓝调背景混在一起反而会看出一条“框” */
  background: var(--au-video-black);
  touch-action: pan-x pan-y;
}

.video {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
}

.poster-layer {
  position: absolute;
  inset: 0;
  background-size: cover;
  background-position: center;
  z-index: 5;
}

.poster-shade {
  position: absolute;
  inset: 0;
  background: var(--au-scrim);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  color: var(--au-text-2);
  font-size: 0.875rem;
}

.pulse {
  animation: pulse 1.4s ease-in-out infinite;
}

@keyframes pulse {
  50% { opacity: 0.35; }
}

.spin {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 直连探测 loading */
.probe-layer {
  position: absolute;
  inset: 0;
  z-index: 6;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  background: rgba(0, 0, 0, 0.55);
  color: var(--au-text-2);
  font-size: 0.875rem;
}

.probe-layer p {
  margin: 0;
}

/* 手势提示 */
.gesture-tip {
  position: absolute;
  top: 18%;
  left: 50%;
  transform: translateX(-50%);
  z-index: 11;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0.875rem;
  background: var(--au-overlay-mid);
  border: 1px solid var(--au-border);
  border-radius: 10px;
  color: var(--au-text);
  font-size: 0.875rem;
  font-weight: 600;
  backdrop-filter: blur(6px);
  pointer-events: none;
  white-space: nowrap;
}

/* 连播下一集 */
.nextup-layer {
  position: absolute;
  inset: 0;
  z-index: 8;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.5rem;
  background: rgba(0, 0, 0, 0.55);
}

.nextup-card {
  width: 100%;
  max-width: 360px;
  padding: 1.25rem 1.25rem 1.125rem;
  text-align: center;
  background: var(--au-overlay-menu);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-xl);
  box-shadow: var(--au-shadow-2);
}

.nextup-kicker {
  margin: 0 0 0.375rem;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

.nextup-title {
  margin: 0 0 1rem;
  font-size: 1rem;
  font-weight: 700;
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nextup-actions {
  display: flex;
  gap: 0.625rem;
  justify-content: center;
}

.error-layer {
  position: absolute;
  inset: 0;
  z-index: 6;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1rem;
  background: var(--au-overlay);
  padding: 1.5rem;
  text-align: center;
}

.error-layer p {
  margin: 0;
  color: var(--au-danger);
  font-size: 0.9375rem;
}

.error-actions {
  display: flex;
  gap: 0.625rem;
}

.btn {
  height: 38px;
  padding: 0 1rem;
  display: inline-flex;
  align-items: center;
  border-radius: 10px;
  font-size: 0.8125rem;
  font-weight: 500;
  text-decoration: none;
  border: none;
  cursor: pointer;
}

.btn.sm {
  height: 36px;
  padding: 0 0.875rem;
}

.btn.ghost {
  background: var(--au-surface-2);
  color: var(--au-text);
  border: 1px solid var(--au-border);
}

.btn.ghost:hover {
  background: var(--au-surface-3);
}

.btn.primary {
  gap: 0.4375rem;
  background: var(--au-primary);
  color: var(--au-on-primary);
  font-weight: 700;
}

.btn.primary:hover {
  filter: brightness(1.08);
}

.btn:active {
  transform: scale(0.97);
}

/* ==================== 付费墙 ==================== */

.paywall-layer {
  position: absolute;
  inset: 0;
  z-index: 7;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.5rem;
  background: var(--au-overlay);
}

.paywall-card {
  width: 100%;
  max-width: 420px;
  padding: 1.75rem 1.5rem;
  text-align: center;
  background: var(--au-surface);
  border: 1px solid var(--au-border-strong);
  border-radius: var(--au-r-xl);
  box-shadow: var(--au-shadow-2);
}

.paywall-icon {
  width: 52px;
  height: 52px;
  margin: 0 auto 0.875rem;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  border-radius: 50%;
  color: var(--au-primary);
}

.paywall-title {
  margin: 0 0 0.5rem;
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--au-text);
}

.paywall-text {
  margin: 0 0 1rem;
  font-size: 0.8125rem;
  line-height: 1.6;
  color: var(--au-text-2);
}

.paywall-perks {
  display: grid;
  gap: 0.4375rem;
  margin-bottom: 1.25rem;
  text-align: left;
}

.paywall-perks span {
  display: flex;
  align-items: center;
  gap: 0.4375rem;
  font-size: 0.8125rem;
  color: var(--au-text-2);
}

.paywall-perks svg {
  color: var(--au-primary);
  flex-shrink: 0;
}

.paywall-actions {
  display: flex;
  gap: 0.625rem;
  justify-content: center;
  flex-wrap: wrap;
}

.back-btn {
  position: absolute;
  top: 1rem;
  left: 1rem;
  z-index: 10;
  width: 38px;
  height: 38px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--au-overlay-mid);
  border: 1px solid var(--au-border);
  border-radius: 10px;
  color: var(--au-text);
  opacity: 0;
  transition: opacity 0.2s ease;
  backdrop-filter: blur(6px);
}

.back-btn.visible {
  opacity: 1;
}

/* 控制条 */
.controls {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  padding: 1.75rem 1rem 0.75rem;
  background: linear-gradient(to top, var(--au-overlay-strong), transparent);
  opacity: 0;
  transform: translateY(8px);
  transition: all 0.25s ease;
  pointer-events: none;
}

.controls.visible {
  opacity: 1;
  transform: none;
  pointer-events: auto;
}

.seek {
  position: relative;
  height: 14px;
  display: flex;
  align-items: center;
  cursor: pointer;
  margin-bottom: 0.375rem;
  /* 扩大触摸热区，但视觉条保持细 */
  touch-action: none;
}

.seek::before {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  height: 4px;
  background: var(--au-on-image-strong);
  border-radius: 2px;
  transition: height 0.15s ease;
}

.seek:hover::before,
.seek.dragging::before {
  height: 6px;
}

.seek-buffer {
  position: absolute;
  inset: 0;
  border-radius: 2px;
  pointer-events: none;
}

.seek-fill {
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  height: 4px;
  background: var(--au-primary);
  border-radius: 2px;
  pointer-events: none;
  transition: height 0.15s ease;
}

.seek:hover .seek-fill,
.seek.dragging .seek-fill {
  height: 6px;
}

.seek-thumb {
  position: absolute;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 14px;
  height: 14px;
  background: var(--au-primary);
  border-radius: 50%;
  box-shadow: 0 1px 4px var(--au-shadow-color);
  pointer-events: none;
}

.controls-row {
  display: flex;
  align-items: center;
  gap: 0.625rem;
}

.ctrl-btn {
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  color: var(--au-text);
  cursor: pointer;
  border-radius: 10px;
  transition: background 0.15s ease;
  flex-shrink: 0;
}

.ctrl-btn:hover {
  background: var(--au-surface-3);
}

.ctrl-btn:active {
  background: var(--au-surface-3);
  transform: scale(0.94);
}

.volume-wrap {
  display: flex;
  align-items: center;
  gap: 0.375rem;
}

.volume-slider {
  width: 80px;
  accent-color: var(--au-primary);
  cursor: pointer;
}

.time {
  font-size: 0.8125rem;
  color: var(--au-text-2);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.method-badge {
  margin-left: auto;
  padding: 0.1875rem 0.5rem;
  background: var(--au-primary-soft);
  border-radius: var(--au-r-sm);
  color: var(--au-primary);
  font-size: 0.8125rem;
  font-weight: 600;
  white-space: nowrap;
}

.fullscreen {
  margin-left: 0.25rem;
}

/* ==================== 底部弹窗（选集 / 字幕） ==================== */

.sheet-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: flex-end;
  justify-content: center;
  backdrop-filter: blur(2px);
}

.sheet {
  width: 100%;
  max-width: 560px;
  max-height: 70vh;
  overflow-y: auto;
  background: var(--au-bg-soft);
  border: 1px solid var(--au-border);
  border-bottom: none;
  border-radius: 14px 14px 0 0;
  padding: 0.5rem 0.75rem 1.5rem;
}

.sheet-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.25rem 0.25rem 0.5rem;
}

.sheet-title {
  margin: 0;
  font-size: 0.9375rem;
  font-weight: 700;
  color: var(--au-text);
}

.sheet-close {
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: 9px;
  color: var(--au-text-3);
  cursor: pointer;
}

.sheet-close:active {
  background: var(--au-surface-2);
}

.sheet-group {
  margin: 0.75rem 0 0.25rem;
  padding: 0 0.5rem;
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--au-text-3);
}

.sheet-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.sheet-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 0.625rem;
  padding: 0.75rem 0.875rem;
  background: transparent;
  border: none;
  border-radius: 10px;
  color: var(--au-text);
  font-size: 0.875rem;
  cursor: pointer;
  text-align: left;
  min-height: 44px;
}

.sheet-item:not(:disabled):active {
  background: var(--au-surface-2);
}

.sheet-item.active {
  background: var(--au-primary-soft);
  color: var(--au-primary);
  font-weight: 600;
}

.sheet-item:disabled {
  opacity: 0.55;
  cursor: default;
}

.sheet-item-num {
  flex-shrink: 0;
  width: 2rem;
  text-align: center;
  font-weight: 700;
  color: var(--au-primary);
  font-variant-numeric: tabular-nums;
}

.sheet-item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sheet-check {
  flex-shrink: 0;
}

.sheet-tag {
  flex-shrink: 0;
  padding: 0.125rem 0.5rem;
  background: var(--au-primary-soft);
  border-radius: var(--au-r-full);
  color: var(--au-primary);
  font-size: 0.8125rem;
  font-weight: 600;
}

.sheet-loading,
.sheet-hint {
  margin: 0;
  padding: 0.75rem 0.5rem;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

.sheet-loading {
  text-align: center;
  animation: pulse 1.4s ease-in-out infinite;
}

.sheet-enter-active,
.sheet-leave-active {
  transition: opacity 0.2s ease;
}

.sheet-enter-from,
.sheet-leave-to {
  opacity: 0;
}

.sheet-enter-active .sheet,
.sheet-leave-active .sheet {
  transition: transform 0.25s ease;
}

.sheet-enter-from .sheet,
.sheet-leave-to .sheet {
  transform: translateY(48px);
}

/* 播放器下方信息 */
/* 播放器下方的信息区：宽度与左右留白来自全局 .container（页面骨架），
   这里只管垂直节奏与文字层级 */
.below {
  padding-top: 1.25rem;
}

.below-title {
  margin: 0 0 0.375rem;
  font-size: 1.25rem;
  font-weight: 700;
  color: var(--au-text);
}

.below-meta {
  margin: 0 0 0.75rem;
  display: flex;
  gap: 0.875rem;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

.below-overview {
  margin: 0;
  font-size: 0.875rem;
  line-height: 1.7;
  color: var(--au-text-2);
  max-width: 720px;
  padding-bottom: 2.5rem;
}
</style>
