<template>
  <div class="world-root">
    <div class="world">
      <svg class="scene" viewBox="0 0 1440 900" fill="none" preserveAspectRatio="xMidYMid slice">
        <g stroke="#d9cba6" stroke-width="1.4" opacity=".55">
          <path d="M-20 210 Q 300 130 620 190 T 1460 170"/>
          <path d="M-20 250 Q 320 175 640 232 T 1460 214"/>
          <path d="M-20 660 Q 380 590 760 650 T 1460 620"/>
          <path d="M-20 706 Q 400 636 780 696 T 1460 668"/>
          <path d="M1050 -20 Q 1010 300 1090 560"/>
          <path d="M1105 -20 Q 1068 300 1148 560"/>
        </g>
        <g>
          <path d="M0 240 L130 130 L230 215 L330 105 L470 240 Z" fill="#e7dcc0"/>
          <path d="M330 105 L368 148 L330 168 L296 146 Z" fill="#fbf7ec"/>
          <path d="M1120 235 L1260 120 L1360 200 L1440 140 L1440 245 Z" fill="#e7dcc0"/>
          <path d="M1260 120 L1292 152 L1260 170 L1232 150 Z" fill="#fbf7ec"/>
        </g>
        <path d="M0 900 L0 560 Q 260 470 520 560 Q 700 620 900 560 Q 1200 480 1440 570 L1440 900 Z" fill="#e9dfc6"/>
        <path d="M0 900 L0 700 Q 330 620 700 700 Q 1080 780 1440 690 L1440 900 Z" fill="#e2d3ae"/>
        <path d="M1180 -10 Q 1120 220 1210 420 Q 1280 580 1190 760 Q 1150 840 1180 910" stroke="#a8c6dd" stroke-width="26" stroke-linecap="round" opacity=".8"/>
        <path d="M1180 -10 Q 1120 220 1210 420 Q 1280 580 1190 760 Q 1150 840 1180 910" stroke="#c3d9ea" stroke-width="10" stroke-linecap="round" opacity=".8"/>
        <ellipse cx="305" cy="655" rx="105" ry="34" fill="#b9d2e4" opacity=".85"/>
        <ellipse cx="305" cy="650" rx="80" ry="24" fill="#c9deed" opacity=".9"/>
        <g stroke="#a06c24" stroke-width="2.6" stroke-dasharray="2 9" stroke-linecap="round" opacity=".75">
          <path d="M720 640 Q 640 520 505 470 Q 380 425 300 400"/>
          <path d="M720 640 Q 830 540 985 505 Q 1090 482 1155 430"/>
          <path d="M720 640 Q 700 470 660 380 Q 640 330 700 265"/>
          <path d="M505 470 Q 560 380 700 265"/>
          <path d="M985 505 Q 900 400 700 265"/>
          <path d="M300 400 Q 380 320 480 285"/>
        </g>
        <g fill="#7d9b76">
          <circle cx="180" cy="480" r="17"/><circle cx="215" cy="462" r="13"/><circle cx="150" cy="455" r="12"/>
          <circle cx="420" cy="600" r="15"/><circle cx="452" cy="582" r="11"/><circle cx="395" cy="578" r="10"/>
          <circle cx="1010" cy="640" r="16"/><circle cx="1045" cy="620" r="12"/><circle cx="985" cy="615" r="10"/>
          <circle cx="880" cy="300" r="14"/><circle cx="912" cy="282" r="10"/>
          <circle cx="240" cy="300" r="13"/><circle cx="268" cy="284" r="9"/>
        </g>
        <g fill="#93ad8b">
          <circle cx="196" cy="492" r="12"/><circle cx="436" cy="610" r="10"/><circle cx="1026" cy="650" r="11"/><circle cx="894" cy="310" r="9"/>
        </g>
        <g stroke="#8a6d3b" stroke-width="4"><path d="M1152 428 L1236 452"/><path d="M1156 416 L1240 440"/></g>
        <g transform="translate(1330,790)" opacity=".8">
          <circle r="34" fill="#FBF7EC" stroke="#c9b98f" stroke-width="1.5"/>
          <path d="M0 -26 L7 0 L0 26 L-7 0 Z" fill="#D9482B"/>
          <path d="M-26 0 L0 -7 L26 0 L0 7 Z" fill="#a06c24"/>
          <text y="-42" text-anchor="middle" font-size="14" font-weight="700" fill="#6B6257">N</text>
        </g>
        <circle cx="90" cy="120" r="30" fill="#f2c063" opacity=".85"/>
        <circle cx="90" cy="120" r="42" fill="#f2c063" opacity=".25"/>
      </svg>

      <div class="hud">
        <span class="brand">Aetrix<b>.</b></span>
        <span class="worldname">🗺 探险世界</span>
        <div class="right">
          <span class="pill" v-if="streak > 0">🔥 连签 {{ streak }} 天</span>
          <button type="button" class="pill amber" v-if="pointsText" @click="goLedger">🪙 {{ pointsText }} 积分</button>
          <span class="pill" v-if="memberInfo"><span class="lv">Lv.{{ memberInfo.level }}</span> {{ memberInfo.level_name }}</span>
          <button type="button" class="avatar" @click="goAccount" aria-label="我的账号">{{ avatarChar }}</button>
        </div>
      </div>

      <div class="hello">
        <div class="d">{{ dateLine }}</div>
        <h2>{{ greetingText }}，{{ nickname }}</h2>
        <p v-if="remaining > 0">世界里有 <b>{{ remaining }} 个包裹</b> 等你拆<template v-if="hasLetter">，邮局里有一封昨天的信。</template><template v-else>。</template></p>
        <p v-else-if="hasLetter">邮局里有一封昨天的信，点邮局拆开看看。</p>
        <p v-else>今天片库有更新，去影院看看新到的片子。</p>
      </div>

      <button type="button" class="classic" @click="toClassic">经典视图 ⇄</button>

      <button type="button" class="mark" :style="{ left: '48.61%', top: '31.67%' }" @click="openCinema" aria-label="梦幻影院">
        <div class="pin">🎬</div>
        <span class="name">梦幻影院</span>
        <span class="sub">{{ cinemaSub }}</span>
      </button>

      <button type="button" class="mark" :style="{ left: '20.83%', top: '46.67%' }" @click="openParcels" aria-label="封存驿站">
        <span class="dot" v-if="remaining > 0">{{ remaining }}</span>
        <div class="pin">📦</div>
        <span class="name">封存驿站</span>
        <span class="sub">{{ remaining > 0 ? `${remaining} 个包裹待拆` : '今天的包裹都拆完啦' }}</span>
      </button>

      <button type="button" class="mark" :style="{ left: '80.21%', top: '50%' }" @click="goServer" aria-label="传送灯塔">
        <div class="pin">🗼</div>
        <span class="name">传送灯塔</span>
        <span class="sub">{{ lighthouseSub }}</span>
      </button>

      <button type="button" class="mark" :style="{ left: '35.07%', top: '54.44%' }" @click="goShop" aria-label="补给站">
        <div class="pin">🧰</div>
        <span class="name">补给站</span>
        <span class="sub">商店 · 充积分买会员</span>
      </button>

      <button type="button" class="mark" :style="{ left: '68.4%', top: '58.33%' }" @click="goCollector" aria-label="收藏家小屋">
        <div class="pin">🏅</div>
        <span class="name">收藏家小屋</span>
        <span class="sub">{{ collectorSub }}</span>
      </button>

      <button type="button" class="mark" :style="{ left: '33.33%', top: '33.89%' }" @click="openLetter" aria-label="邮局">
        <span class="dot" v-if="hasLetter && !letterRead">1</span>
        <div class="pin">✉️</div>
        <span class="name">邮局</span>
        <span class="sub">{{ hasLetter ? '昨天的信到了' : '暂无新信' }}</span>
      </button>

      <button type="button" class="mark" :style="{ left: '68.4%', top: '36.67%' }" @click="goGame" aria-label="游戏大厅">
        <div class="pin">🎪</div>
        <span class="name">游戏大厅</span>
        <span class="sub">签到 · 许愿池 · 追新历</span>
      </button>

      <div class="player" :style="{ left: '50%', top: '73.33%' }">
        <div class="tok">{{ avatarChar }}</div>
        <span class="who">你在这里</span>
      </div>

      <div class="feed">
        <h4><i>●</i> 世界动态 · 服务器在忙什么</h4>
        <ul>
          <li v-if="feedState === 'loading'">正在接收世界动态…</li>
          <li v-else-if="feedState === 'error'">动态没加载出来，点上面的建筑照样能玩</li>
          <template v-else>
            <li v-for="(it, idx) in feedItems" :key="idx"><b v-if="it.time">{{ it.time }}</b> {{ it.text }}</li>
            <li v-if="feedItems.length === 0">暂无动态</li>
          </template>
        </ul>
      </div>

      <div class="quest">
        <h4>📋 今日任务</h4>
        <button type="button" class="q" :class="{ done: q.done }" v-for="q in quests" :key="q.key" @click="onQuest(q.key)">
          <span class="box">{{ q.done ? '✓' : '' }}</span>{{ q.label }}<span class="rw" v-if="q.rw">{{ q.rw }}</span>
        </button>
        <div class="bar"><i :style="{ width: questPercent + '%' }"></i></div>
      </div>
    </div>

    <div class="w-overlay" v-if="overlay !== 'none'" @click.self="closeOverlay">
      <div class="w-sheet" role="dialog" aria-modal="true">
        <div class="w-sheet-head">
          <span class="w-sheet-title">{{ overlay === 'parcels' ? '📦 今日封存' : '✉️ 每日一封' }}</span>
          <button type="button" class="w-sheet-close" @click="closeOverlay" aria-label="关闭">✕</button>
        </div>
        <template v-if="overlay === 'parcels'">
          <p class="w-sheet-empty" v-if="parcelsLoading && parcelList.length === 0">正在搬包裹…</p>
          <p class="w-sheet-empty" v-else-if="parcelList.length === 0">今天还没有新封存，明天再来拆 📦</p>
          <div class="w-parcel-grid" v-else>
            <ParcelBox
              v-for="p in parcelList"
              :key="String(p.id)"
              :parcel="p"
              :busy="unsealingIds.has(String(p.id))"
              @unseal="onUnseal"
            />
          </div>
        </template>
        <template v-else-if="overlay === 'letter'">
          <WaxLetter :letter="letterData" />
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { humanizeError } from '@/composables/usePanelApi'
import type { ParcelItem, ParcelsData } from '@/composables/usePanelApi'
import { checkinApi, memberApi } from '@/api/economy'
import { useHomeViewMode } from '@/composables/useHomeViewMode'
import { ensureEmbyBase } from '@/api/emby'
import { useToast } from '@/composables/useToast'
import ParcelBox from '@/components/panel/ParcelBox.vue'
import WaxLetter from '@/components/panel/WaxLetter.vue'

const router = useRouter()
const panel = usePanelStore()
const user = useUserStore()
const { setMode } = useHomeViewMode()

const { today, parcels, pulse, letter, collector } = storeToRefs(panel)

/* ---------- 本地状态 ---------- */
type CheckinStatus = {
  enabled?: boolean
  checked_today?: boolean
  streak?: number
  base_points?: number
  streak_bonus?: number
  streak_max_bonus?: number
  points?: number
}

const checkin = ref<CheckinStatus | null>(null)
const memberRaw = ref<Record<string, unknown> | null>(null)

const overlay = ref<'none' | 'parcels' | 'letter'>('none')
const unsealingIds = ref(new Set<string>())

/* ---------- 基础数据 ---------- */
const todayD = computed<Record<string, unknown>>(
  () => (today.value.data ?? {}) as Record<string, unknown>,
)

const userRaw = computed(
  () => (user.user ?? null) as unknown as Record<string, unknown> | null,
)

const userId = computed(() => {
  const id = userRaw.value?.id
  return id === undefined || id === null || id === '' ? 'guest' : String(id)
})

const nickname = computed(() => {
  const n = todayD.value.nickname
  if (typeof n === 'string' && n.trim()) return n
  const u = userRaw.value
  const un = u?.nickname
  if (typeof un === 'string' && un.trim()) return un
  const name = u?.username
  if (typeof name === 'string' && name.trim()) return name
  return '探险家'
})

const avatarChar = computed(() => Array.from(nickname.value)[0] ?? '探')

const greetingText = computed(() => {
  const g = todayD.value.greeting
  if (typeof g === 'string' && g.trim()) return g
  const h = new Date().getHours()
  if (h < 5) return '夜深了'
  if (h < 9) return '早上好'
  if (h < 12) return '上午好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
})

const dateLine = computed(() => {
  const raw = todayD.value.date_label
  let m: number
  let d: number
  if (typeof raw === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    // 手动拆分，避免 new Date 解析带来的时区偏移
    const parts = raw.split('-')
    m = Number(parts[1])
    d = Number(parts[2])
  } else {
    const now = new Date()
    m = now.getMonth() + 1
    d = now.getDate()
  }
  let wd = todayD.value.weekday
  if (typeof wd !== 'string' || !wd) {
    wd = ['日', '一', '二', '三', '四', '五', '六'][new Date().getDay()] ?? ''
  }
  return `探险日志 · ${m} 月 ${d} 日 星期${wd}`
})

/* ---------- 包裹 ---------- */
const parcelList = computed<ParcelItem[]>(() => {
  const d = parcels.value.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return []
  const arr = (d as ParcelsData).parcels
  return Array.isArray(arr) ? (arr as ParcelItem[]) : []
})

const parcelsLoading = computed(() => parcels.value.loading === true)

const parcelCount = computed(() => {
  const n = todayD.value.parcel_count
  if (typeof n === 'number') return n
  return parcelList.value.length
})

const unsealedCount = computed(() => {
  const n = todayD.value.unsealed_count
  if (typeof n === 'number') return n
  return parcelList.value.filter((p) => p.unsealed).length
})

const remaining = computed(() => parcelList.value.filter((p) => !p.unsealed).length)

/* ---------- 信件 ---------- */
const letterData = computed<Record<string, unknown>>(() => {
  const d = letter.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    return d as Record<string, unknown>
  }
  return {}
})

const hasLetter = computed(() => Object.keys(letterData.value).length > 0)

const letterDateKey = computed(() => {
  const d = letterData.value.date
  if (typeof d === 'string' && d) return d
  const now = new Date()
  const mm = String(now.getMonth() + 1).padStart(2, '0')
  const dd = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${mm}-${dd}`
})

const letterReadKey = computed(
  () => `aetrix:world-letter-read:u${userId.value}:${letterDateKey.value}`,
)

const letterReadState = ref(false)

function refreshLetterRead() {
  try {
    letterReadState.value = localStorage.getItem(letterReadKey.value) === '1'
  } catch {
    letterReadState.value = false
  }
}

const letterRead = computed(() => letterReadState.value)

function markLetterRead() {
  letterReadState.value = true
  try {
    localStorage.setItem(letterReadKey.value, '1')
  } catch {
    /* 静默失败 */
  }
}

/* ---------- 积分 / 会员 / 副标题 ---------- */
const pointsText = computed<string | null>(() => {
  const c = checkin.value?.points
  if (typeof c === 'number') return Math.trunc(c).toLocaleString('en-US')
  const d = collector.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const p = (d as Record<string, unknown>).points
    if (typeof p === 'number') return Math.trunc(p).toLocaleString('en-US')
  }
  return null
})

const memberInfo = computed<{ level: number; level_name: string } | null>(() => {
  const m = memberRaw.value
  if (!m) return null
  const lvl = m.level
  if (typeof lvl !== 'number') return null
  return { level: lvl, level_name: String(m.level_name || '探险家') }
})

const streak = computed(() => {
  const s = todayD.value.streak_days
  if (typeof s === 'number') return s
  const c = checkin.value?.streak
  if (typeof c === 'number') return c
  const d = collector.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const cs = (d as Record<string, unknown>).streak
    if (typeof cs === 'number') return cs
  }
  return 0
})

const cinemaSub = computed(() =>
  parcelCount.value > 0 ? `片库 · 今日新到 ${parcelCount.value} 部` : '片库 · 随时开场',
)

const lighthouseSub = computed(() => {
  const d = pulse.value.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return '服务器'
  const health = (d as Record<string, unknown>).health
  if (health && typeof health === 'object' && !Array.isArray(health)) {
    const n = (health as Record<string, unknown>).issues_count
    if (typeof n === 'number' && n > 0) return `服务器 · 有 ${n} 件事在处理`
  }
  return '服务器 · 一切正常'
})

const collectorSub = computed(() => {
  const d = collector.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const n = (d as Record<string, unknown>).achievement_count
    if (typeof n === 'number') return `徽章墙 · 已点亮 ${n} 枚`
  }
  return '徽章墙'
})

/* ---------- 动态 ---------- */
function cleanActivityText(t: unknown): string {
  if (typeof t !== 'string') return '服务器刚才忙去了，这条动态没写清楚'
  const s = t.trim()
  if (s.startsWith('{')) {
    if (/['"]message['"]/.test(s)) {
      const m1 = s.match(/'message'\s*:\s*'((?:[^'\\]|\\.)*)'/)
      if (m1) return m1[1].replace(/\\'/g, "'")
      const m2 = s.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"/)
      if (m2) return m2[1].replace(/\\"/g, '"')
    }
    return '服务器刚才忙去了，这条动态没写清楚'
  }
  return s
}

const feedItems = computed<Array<{ time: string; text: string }>>(() => {
  const d = pulse.value.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return []
  const acts = (d as Record<string, unknown>).activities
  if (!Array.isArray(acts)) return []
  const out: Array<{ time: string; text: string }> = []
  for (const a of acts.slice(0, 3)) {
    const rec = (a ?? {}) as Record<string, unknown>
    const text = cleanActivityText(rec.text)
    if (!text) continue
    let time = ''
    const at = rec.at
    if (typeof at === 'string' || typeof at === 'number') {
      const dt = new Date(at)
      if (!Number.isNaN(dt.getTime())) {
        time = `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`
      }
    }
    out.push({ time, text })
  }
  return out
})

const feedState = computed<'loading' | 'error' | 'ok'>(() => {
  if (pulse.value.error) return 'error'
  if (pulse.value.loading && !pulse.value.data) return 'loading'
  return 'ok'
})

/* ---------- 任务 ---------- */
const quests = computed<Array<{ key: string; label: string; done: boolean; rw: string }>>(() => {
  const ck = checkin.value
  const checkinDone = ck?.checked_today === true
  const bp = ck?.base_points
  const parcelsDone = parcelCount.value > 0 && unsealedCount.value >= 1
  const read = letterRead.value
  return [
    {
      key: 'checkin',
      label: '每日签到',
      done: checkinDone,
      rw: checkinDone ? '已完成' : typeof bp === 'number' && bp > 0 ? `+${bp} 积分` : '',
    },
    {
      key: 'parcels',
      label: '拆一个封存包裹',
      done: parcelsDone,
      rw: parcelCount.value > 0 ? `${unsealedCount.value}/${parcelCount.value}` : '今日无新包裹',
    },
    {
      key: 'letter',
      label: '读昨天的信',
      done: read,
      rw: !hasLetter.value ? '暂无信件' : read ? '已读' : '0/1',
    },
  ]
})

const questPercent = computed(() => {
  const done = quests.value.filter((q) => q.done).length
  return (done / 3) * 100
})

/* ---------- 弹层 ---------- */
function openParcels() {
  overlay.value = 'parcels'
}

function openLetter() {
  if (!hasLetter.value) return
  markLetterRead()
  overlay.value = 'letter'
}

function closeOverlay() {
  overlay.value = 'none'
}

function onQuest(key: string) {
  if (key === 'checkin') {
    void router.push('/game/checkin')
    return
  }
  if (key === 'parcels') {
    openParcels()
    return
  }
  if (key === 'letter') openLetter()
}

/* ---------- 跳转 ---------- */
function toClassic() {
  setMode('classic')
}

function goLedger() {
  void router.push('/game/ledger')
}

function goAccount() {
  void router.push('/game/account')
}

function goServer() {
  void router.push('/server')
}

function goShop() {
  void router.push('/shop')
}

function goCollector() {
  void router.push('/collector')
}

function goGame() {
  void router.push('/game')
}

async function openCinema() {
  try {
    const base = await ensureEmbyBase()
    if (base) {
      window.open(base.replace(/\/+$/, '') + '/web/index.html', '_blank')
      return
    }
  } catch {
    /* 落到兜底 */
  }
  void router.push('/game/operator')
}

/* ---------- 拆包裹 ---------- */
async function onUnseal(id: string | number) {
  const key = String(id)
  if (unsealingIds.value.has(key)) return
  unsealingIds.value.add(key)
  try {
    await panel.unseal(id)
  } catch (e) {
    useToast().error(humanizeError(e))
  } finally {
    unsealingIds.value.delete(key)
  }
}

/* ---------- 拉取 ---------- */
async function loadCheckin() {
  try {
    checkin.value = (await checkinApi.status()) as unknown as CheckinStatus
  } catch {
    checkin.value = null
  }
}

async function loadMember() {
  try {
    memberRaw.value = (await memberApi.info()) as unknown as Record<string, unknown>
  } catch {
    memberRaw.value = null
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && overlay.value !== 'none') closeOverlay()
}

watch(letterDateKey, () => refreshLetterRead())
watch(userId, () => refreshLetterRead())

onMounted(() => {
  void panel.loadHome()
  void panel.loadCollector()
  void loadCheckin()
  void loadMember()
  refreshLetterRead()
  window.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.world-root {
  /* 世界根不在 .v4-scope 内，纸色 token 取不到，就地同步定义（与 v4-paper.css .v4-scope 同值，改值只改那边）；无全局同名的色值/阴影直接写字面值 */
  --paper-card: #FFFDF6;
  --ink: #2A2620;
  --ink2: #6B6257;
  --line: #D8CDB8;
  --paper-primary: #e8a84a;
  --red: #D9482B;
  --green: #4A7C59;

  height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #EFE7D6;
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  color: var(--ink);
}

.world {
  position: relative;
  width: min(100%, 160dvh);
  aspect-ratio: 1440 / 900;
  overflow: hidden;
  background: linear-gradient(180deg, #f3ead3 0%, #F6F1E7 55%, #efe4cb 100%);
  container-type: inline-size;
  flex: 0 0 auto;
}

.scene {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}

.hud {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4.17cqw;
  display: flex;
  align-items: center;
  gap: 0.97cqw;
  padding: 0 1.53cqw;
  background: linear-gradient(180deg, rgba(246, 241, 231, .95), rgba(246, 241, 231, .75) 75%, transparent);
  z-index: 30;
}

.hud .brand {
  font-weight: 800;
  font-size: 1.32cqw;
}

.hud .brand b {
  color: #a06c24;
}

.hud .worldname {
  background: var(--ink);
  color: #FFFDF6;
  border-radius: 999px;
  padding: 0.35cqw 0.97cqw;
  font-size: 0.94cqw;
  font-weight: 700;
  letter-spacing: 0.07cqw;
}

.hud .right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 0.69cqw;
}

.pill {
  background: rgba(255, 253, 246, .92);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.42cqw 0.97cqw;
  font-size: 0.94cqw;
  font-weight: 700;
  box-shadow: 0 1px 3px rgba(42, 38, 32, .08);
  font: inherit;
  color: inherit;
}

button.pill {
  cursor: pointer;
}

.pill.amber {
  background: rgba(185, 128, 47, .12);
  border-color: rgba(185, 128, 47, .35);
  color: #a06c24;
}

.pill .lv {
  color: #a06c24;
}

.avatar {
  width: 2.50cqw;
  height: 2.50cqw;
  border-radius: 50%;
  background: linear-gradient(135deg, #e8a84a, #c97e2c);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 0.94cqw;
  font-family: inherit;
  border: 2px solid #FFFDF6;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  cursor: pointer;
  padding: 0;
}

.mark {
  position: absolute;
  z-index: 20;
  transform: translate(-50%, -100%);
  text-align: center;
  cursor: pointer;
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  color: inherit;
}

.mark .pin {
  width: 4.31cqw;
  height: 4.31cqw;
  margin: 0 auto;
  border-radius: 1.39cqw;
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 2.01cqw;
  position: relative;
  transition: transform .15s ease;
}

.mark .pin::after {
  content: "";
  position: absolute;
  left: 50%;
  bottom: -0.49cqw;
  transform: translateX(-50%) rotate(45deg);
  width: 0.90cqw;
  height: 0.90cqw;
  background: var(--paper-card);
  border-right: 1.5px solid var(--line);
  border-bottom: 1.5px solid var(--line);
}

.mark .name {
  margin-top: 0.90cqw;
  display: inline-block;
  background: rgba(42, 38, 32, .88);
  color: #FFFDF6;
  font-size: 0.94cqw;
  font-weight: 700;
  border-radius: 0.56cqw;
  padding: 0.21cqw 0.69cqw;
  letter-spacing: 0.03cqw;
}

.mark .sub {
  display: block;
  margin-top: 0.28cqw;
  font-size: 0.83cqw;
  color: var(--ink2);
  background: rgba(255, 253, 246, .85);
  border-radius: 0.42cqw;
  padding: 0.07cqw 0.56cqw;
}

.mark .dot {
  position: absolute;
  top: -0.42cqw;
  right: calc(50% - 2.64cqw);
  min-width: 1.46cqw;
  height: 1.46cqw;
  border-radius: 99px;
  background: var(--red);
  color: #fff;
  font-size: 0.83cqw;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 0.42cqw;
  border: 2px solid #FFFDF6;
  z-index: 2;
}

.mark:hover .pin {
  transform: translateY(-0.21cqw);
}

.mark:focus-visible,
.pill:focus-visible,
.avatar:focus-visible,
.classic:focus-visible,
.quest .q:focus-visible,
.w-sheet-close:focus-visible {
  outline: 2px solid #a06c24;
  outline-offset: 2px;
}

.player {
  position: absolute;
  z-index: 25;
  transform: translate(-50%, -100%);
  text-align: center;
}

.player .tok {
  width: 2.78cqw;
  height: 2.78cqw;
  margin: 0 auto;
  border-radius: 50%;
  background: linear-gradient(135deg, #e8a84a, #c97e2c);
  border: 0.21cqw solid #FFFDF6;
  box-shadow: 0 4px 14px rgba(42, 38, 32, .3);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 1.11cqw;
}

.player .who {
  margin-top: 0.35cqw;
  font-size: 0.83cqw;
  font-weight: 700;
  background: rgba(255, 253, 246, .9);
  border: 1px solid var(--line);
  padding: 0.07cqw 0.63cqw;
  border-radius: 99px;
  display: inline-block;
}

.feed {
  position: absolute;
  left: 1.53cqw;
  bottom: 1.53cqw;
  width: 21.39cqw;
  background: rgba(255, 253, 246, .94);
  border: 1px solid var(--line);
  border-radius: 1.11cqw;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  padding: 0.97cqw 1.11cqw;
  z-index: 30;
}

.feed h4 {
  font-size: 0.90cqw;
  letter-spacing: 0.10cqw;
  color: var(--ink2);
  margin-bottom: 0.56cqw;
}

.feed h4 i {
  color: var(--green);
  font-style: normal;
}

.feed ul {
  margin: 0;
  padding: 0;
}

.feed li {
  list-style: none;
  font-size: 0.90cqw;
  color: var(--ink2);
  padding: 0.42cqw 0;
  border-top: 1px dashed var(--line);
}

.feed li:first-of-type {
  border-top: none;
}

.feed li b {
  color: var(--ink);
  margin-right: 0.42cqw;
}

.quest {
  position: absolute;
  right: 1.53cqw;
  bottom: 1.53cqw;
  width: 19.72cqw;
  background: rgba(255, 253, 246, .94);
  border: 1px solid var(--line);
  border-radius: 1.11cqw;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  padding: 0.97cqw 1.11cqw;
  z-index: 30;
}

.quest h4 {
  font-size: 0.90cqw;
  letter-spacing: 0.10cqw;
  color: var(--ink2);
  margin-bottom: 0.42cqw;
}

.quest .q {
  display: flex;
  align-items: center;
  gap: 0.63cqw;
  font-size: 0.94cqw;
  padding: 0.49cqw 0;
  border: none;
  border-top: 1px dashed var(--line);
  background: none;
  width: 100%;
  text-align: left;
  font-family: inherit;
  color: inherit;
  cursor: pointer;
}

.quest .q:first-of-type {
  border-top: none;
}

.quest .box {
  width: 1.32cqw;
  height: 1.32cqw;
  border-radius: 0.42cqw;
  border: 1.5px solid #b9ac8f;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.83cqw;
  color: #fff;
  flex: 0 0 auto;
}

.quest .q.done {
  color: var(--ink2);
}

.quest .q.done .box {
  background: var(--green);
  border-color: var(--green);
}

.quest .q .rw {
  margin-left: auto;
  font-size: 0.83cqw;
  color: #a06c24;
  font-weight: 700;
}

.quest .bar {
  height: 0.42cqw;
  background: #EFE7D6;
  border-radius: 99px;
  margin-top: 0.69cqw;
  overflow: hidden;
}

.quest .bar i {
  display: block;
  height: 100%;
  background: var(--paper-primary);
  border-radius: 99px;
  transition: width .3s ease;
}

.hello {
  position: absolute;
  left: 1.53cqw;
  top: 5.42cqw;
  z-index: 30;
  background: rgba(255, 253, 246, .94);
  border: 1px solid var(--line);
  border-radius: 1.11cqw;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  padding: 0.97cqw 1.25cqw;
  max-width: 22.92cqw;
}

.hello .d {
  font-size: 0.83cqw;
  letter-spacing: 0.14cqw;
  color: var(--ink2);
}

.hello h2 {
  font-size: 1.46cqw;
  margin: 0.21cqw 0 0.14cqw;
}

.hello p {
  font-size: 0.90cqw;
  color: var(--ink2);
  line-height: 1.55;
}

.hello p b {
  color: var(--red);
}

.classic {
  position: absolute;
  right: 1.53cqw;
  top: 5.42cqw;
  z-index: 30;
  background: rgba(255, 253, 246, .9);
  border: 1px solid var(--line);
  color: var(--ink2);
  border-radius: 999px;
  padding: 0.49cqw 1.04cqw;
  font-size: 0.90cqw;
  font-weight: 600;
  font-family: inherit;
  box-shadow: 0 1px 3px rgba(42, 38, 32, .08);
  cursor: pointer;
}

.w-overlay {
  position: fixed;
  inset: 0;
  background: rgba(42, 38, 32, .45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 80;
  padding: 24px;
}

.w-sheet {
  background: #FFFDF6;
  border: 1px solid #D8CDB8;
  border-radius: 16px;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  width: min(560px, calc(100vw - 48px));
  max-height: 84dvh;
  overflow-y: auto;
  padding: 20px;
  color: #2A2620;
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}

.w-sheet-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.w-sheet-title {
  font-size: 16px;
  font-weight: 800;
}

.w-sheet-close {
  background: none;
  border: 1px solid #D8CDB8;
  border-radius: 999px;
  width: 30px;
  height: 30px;
  cursor: pointer;
  color: #6B6257;
  font-size: 13px;
  line-height: 1;
}

.w-sheet-empty {
  font-size: 13.5px;
  color: #6B6257;
  padding: 12px 0;
}

.w-parcel-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

@media (prefers-reduced-motion: reduce) {
  .mark .pin {
    transition: none;
  }
  .mark:hover .pin {
    transform: none;
  }
  .quest .bar i {
    transition: none;
  }
}
</style>
