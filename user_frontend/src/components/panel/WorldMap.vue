<template>
  <div class="world-root" :class="[`dp-${daypart}`, { 'is-game': variant === 'game' }]">
    <div class="game-goalbar" v-if="variant === 'game'">
      <div class="m-goal-text">今日探险 <b>{{ questDoneCount }}/{{ quests.length }}</b><template v-if="questNextLabel"> · 还差：{{ questNextLabel }}</template><template v-else> · 全部完成 ✓</template></div>
      <div class="m-goal-bar"><i :style="{ width: questPercent + '%' }"></i></div>
    </div>
    <div class="world">
      <svg class="scene" viewBox="0 0 1440 900" fill="none" preserveAspectRatio="xMidYMid slice">
        <g class="scene-roads" stroke="#d9cba6" stroke-width="1.4" opacity=".55">
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
        <g class="scene-trails" stroke="#a06c24" stroke-width="2.6" stroke-dasharray="2 9" stroke-linecap="round" opacity=".75">
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

      <div class="m-hud" aria-label="今日状态">
        <div class="m-hello">{{ greetingText }}，{{ nickname }}</div>
        <div class="m-stats">
          <template v-if="streak > 0">连签 <b>{{ streak }}</b> 天 · </template>
          <template v-if="memberInfo"><b>Lv.{{ memberInfo.level }}</b> {{ memberInfo.level_name }}</template>
        </div>
      </div>

      <div class="hello">
        <div class="d">{{ dateLine }}</div>
        <h2>{{ greetingText }}，{{ nickname }}</h2>
        <p v-if="remaining > 0">世界里有 <b>{{ remaining }} 个包裹</b> 等你拆<template v-if="hasLetter">，邮局里有一封昨天的信。</template><template v-else>。</template></p>
        <p v-else-if="hasLetter">邮局里有一封昨天的信，点邮局拆开看看。</p>
        <p v-else>今天片库有更新，去影院看看新到的片子。</p>
      </div>

      <button type="button" class="classic" v-if="variant === 'home'" @click="toClassic">经典视图 ⇄</button>

      <div class="m-zone" aria-hidden="true">🏘 建筑区</div>
      <div class="m-zone-line" aria-hidden="true"></div>
      <div class="m-zone m-zone-2" aria-hidden="true">⛲ 中央广场</div>
      <div class="m-scroll-hint" aria-hidden="true"><span>↓ 上滑继续逛 · 中央广场在下面</span></div>

      <button type="button" class="mark m-cinema" :style="{ left: '48.61%', top: '31.67%' }" @click="openCinema" aria-label="梦幻影院">
        <div class="pin">🎬</div>
        <span class="name">梦幻影院</span>
        <span class="sub">{{ cinemaSub }}</span>
      </button>

      <button type="button" class="mark m-parcels" :style="{ left: '20.83%', top: '46.67%' }" @click="openParcels" :aria-label="remaining > 0 ? `封存驿站，有 ${remaining} 个包裹待拆` : '封存驿站'">
        <span class="dot" v-if="remaining > 0">{{ remaining }}</span>
        <div class="pin">📦</div>
        <span class="name">封存驿站</span>
        <span class="sub">{{ remaining > 0 ? `${remaining} 个包裹待拆` : '今天的包裹都拆完啦' }}</span>
      </button>

      <button type="button" class="mark lighthouse m-lighthouse" :style="{ left: '80.21%', top: '50%' }" @click="goServer" aria-label="传送灯塔">
        <div class="pin">🗼</div>
        <span class="name">传送灯塔</span>
        <span class="sub">{{ lighthouseSub }}</span>
      </button>

      <button type="button" class="mark m-shop" :style="{ left: '35.07%', top: '54.44%' }" @click="goShop" aria-label="补给站">
        <div class="pin">🧰</div>
        <span class="name">补给站</span>
        <span class="sub">商店 · 充积分买会员</span>
      </button>

      <button type="button" class="mark m-collector" :style="{ left: '68.4%', top: '58.33%' }" @click="goCollector" aria-label="收藏家小屋">
        <div class="pin">🏅</div>
        <span class="name">收藏家小屋</span>
        <span class="sub">{{ collectorSub }}</span>
      </button>

      <button type="button" class="mark m-post" :style="{ left: '33.33%', top: '33.89%' }" @click="openLetter" :aria-label="hasLetter && !letterRead ? '邮局，有一封未读来信' : '邮局'">
        <span class="dot" v-if="hasLetter && !letterRead">1</span>
        <div class="pin">✉️</div>
        <span class="name">邮局</span>
        <span class="sub">{{ postSub }}</span>
      </button>

      <button type="button" class="mark m-gamehall" :style="{ left: '68.4%', top: '36.67%' }" @click="goGame" aria-label="游戏大厅">
        <div class="pin">🎪</div>
        <span class="name">游戏大厅</span>
        <span class="sub">签到 · 许愿池 · 追新历</span>
      </button>

      <button type="button" class="mark m-stele" :style="{ left: '15.28%', top: '33.33%' }" @click="openStele" aria-label="观影碑">
        <div class="pin">🏆</div>
        <span class="name">观影碑</span>
        <span class="sub">{{ steleSub }}</span>
      </button>

      <button type="button" class="mark m-board" :style="{ left: '28%', top: '25%' }" @click="openBoard" aria-label="服主告示牌">
        <div class="pin">📢</div>
        <span class="name">告示牌</span>
        <span class="sub">{{ boardSub }}</span>
      </button>

      <button type="button" class="mark m-wall" :style="{ left: '58%', top: '77%' }" @click="openWall" aria-label="到此一游留言墙">
        <div class="pin">💬</div>
        <span class="name">留言墙</span>
        <span class="sub">{{ wallSub }}</span>
      </button>

      <button type="button" class="mark m-fortune" :class="{ 'fortune-pending': !fortuneDrew }" :style="{ left: '79%', top: '74%' }" @click="openFortune" aria-label="每日抽一签">
        <div class="pin">🎋</div>
        <span class="name">抽签亭</span>
        <span class="sub">{{ fortuneSub }}</span>
      </button>

      <div class="player m-player" :style="{ left: '50%', top: '73.33%' }">
        <div class="tok">{{ avatarChar }}</div>
        <span class="who">你在这里</span>
      </div>

      <div class="m-feed" v-if="feedItems.length > 0" aria-hidden="true"><i>●</i> {{ feedItems[0].time }} {{ feedItems[0].text }}</div>

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
        <span class="stamp-done" v-if="questPercent >= 100">今日探险完成 ✓</span>
        <button type="button" class="q" :class="{ done: q.done }" v-for="q in quests" :key="q.key" @click="onQuest(q.key)">
          <span class="box">{{ q.done ? '✓' : '' }}</span>{{ q.label }}<span class="rw" v-if="q.rw">{{ q.rw }}</span>
        </button>
        <div class="bar"><i :style="{ width: questPercent + '%' }"></i></div>
      </div>
    </div>

    <div class="w-overlay" v-if="overlay !== 'none'" @click.self="closeOverlay">
      <div class="w-sheet" role="dialog" aria-modal="true" tabindex="-1" ref="sheetEl"
           @touchstart="onSheetTouchStart" @touchend="onSheetTouchEnd">
        <div class="w-grip" aria-hidden="true"></div>
        <div class="w-sheet-head">
          <span class="w-sheet-title">{{ sheetTitle }}</span>
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
        <template v-else-if="overlay === 'stele'">
          <div class="w-tabs">
            <button type="button" class="w-tab" :class="{ on: lbBoard === 'hot' }" @click="lbBoard = 'hot'">热播榜</button>
            <button type="button" class="w-tab" :class="{ on: lbBoard === 'duration' }" @click="lbBoard = 'duration'">观影时长榜</button>
          </div>
          <div class="w-subtabs">
            <button type="button" class="w-subtab" :class="{ on: lbPeriod === 'day' }" @click="lbPeriod = 'day'">日榜</button>
            <button type="button" class="w-subtab" :class="{ on: lbPeriod === 'week' }" @click="lbPeriod = 'week'">周榜</button>
          </div>
          <p class="w-sheet-empty" v-if="lbLoading && lbRows.length === 0">正在抄榜…</p>
          <p class="w-sheet-empty" v-else-if="lbRows.length === 0">还没有人上榜，快去看片抢头名 🏆</p>
          <ul class="w-lb-list" v-else>
            <li v-for="(r, i) in lbRows" :key="String(r.item_id ?? r.user_id ?? i)" class="w-lb-row" :class="{ me: lbBoard === 'duration' && String(r.nickname) === nickname }">
              <span class="w-lb-rank" :class="{ top: i < 3 }">{{ i + 1 }}</span>
              <span class="w-lb-main">
                <b>{{ lbBoard === 'hot' ? String(r.name ?? '') : String(r.nickname ?? '') }}</b>
                <small v-if="lbBoard === 'hot'">{{ String(r.item_type ?? '') }}</small>
              </span>
              <span class="w-lb-num">{{ lbBoard === 'hot' ? `${Number(r.count ?? 0)} 次` : `${fmtHours(r.seconds)}` }}</span>
            </li>
          </ul>
          <p class="w-sheet-foot" v-if="lbMyRank">我的排名：第 {{ lbMyRank }} 名</p>
          <p class="w-sheet-foot">{{ lbScopeText }} · 时长榜只显示昵称，可在「我的账号 → 设置」里关掉自己上榜</p>
        </template>
        <template v-else-if="overlay === 'board'">
          <div v-if="boardData.visible === true" class="w-board">
            <p class="w-board-text">{{ String(boardData.text ?? '') }}</p>
            <p class="w-board-sig">—— {{ String(boardData.signature ?? '服主') }}</p>
          </div>
          <p class="w-sheet-empty" v-else>服主还没留言，改天再来看看 📢</p>
        </template>
        <template v-else-if="overlay === 'wall'">
          <p class="w-sheet-empty" v-if="wallLoading && wallPosts.length === 0">正在翻墙…</p>
          <ul class="w-wall-list" v-else-if="wallPosts.length > 0">
            <li v-for="p in wallPosts" :key="String(p.id)" class="w-wall-row">
              <span class="w-wall-nick">{{ String(p.nickname ?? '') }}</span>
              <span class="w-wall-time">{{ fmtWallTime(p.created_at) }}</span>
              <p class="w-wall-text">{{ String(p.content ?? '') }}</p>
            </li>
          </ul>
          <p class="w-sheet-empty" v-else>墙上还空着，来留下第一句话吧 💬</p>
          <div class="w-wall-form">
            <input v-model="wallInput" maxlength="60" placeholder="到此一游，留句话吧（60字内）" @keyup.enter="submitWall" aria-label="留言内容" />
            <button type="button" class="w-wall-btn" :disabled="wallPosting || !wallInput.trim()" @click="submitWall">
              {{ wallPosting ? '贴上…' : '贴上墙' }}
            </button>
          </div>
          <p class="w-sheet-foot">每人每 10 分钟可留 1 条</p>
        </template>
        <template v-else-if="overlay === 'fortune'">
          <div v-if="fortuneLoading && !fortune" class="w-sheet-empty">正在摇签筒…</div>
          <div v-else-if="fortuneDrew" class="w-fortune">
            <div class="w-fortune-level">{{ String(fortune?.level ?? '') }}</div>
            <p class="w-fortune-text">{{ String(fortune?.text ?? '') }}</p>
            <p class="w-fortune-yiji"><b>宜</b> {{ String(fortune?.yi ?? '') }}　<b>忌</b> {{ String(fortune?.ji ?? '') }}</p>
            <p class="w-sheet-foot">每天 1 次 · 0 点刷新</p>
          </div>
          <div v-else class="w-fortune-draw">
            <div class="w-fortune-pot">🎋</div>
            <p class="w-sheet-empty">心诚则灵，点一下抽今日运势</p>
            <button type="button" class="w-fortune-btn" :disabled="fortuneDrawing" @click="drawFortune">
              {{ fortuneDrawing ? '摇签中…' : '抽一签' }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { humanizeError, panelApi } from '@/composables/usePanelApi'
import type { ParcelItem, ParcelsData } from '@/composables/usePanelApi'
import { checkinApi, memberApi } from '@/api/economy'
import { useHomeViewMode } from '@/composables/useHomeViewMode'
import { ensureEmbyBase } from '@/api/emby'
import { useToast } from '@/composables/useToast'
import ParcelBox from '@/components/panel/ParcelBox.vue'
import WaxLetter from '@/components/panel/WaxLetter.vue'

/** 世界地图本体：首页（variant=home）与探险日志 /game（variant=game）共用同一份数据与弹层 */
withDefaults(defineProps<{ variant?: 'home' | 'game' }>(), { variant: 'home' })

const router = useRouter()
const panel = usePanelStore()
const user = useUserStore()
const { setMode } = useHomeViewMode()

const { today, parcels, pulse, letter, board, collector } = storeToRefs(panel)

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

const overlay = ref<'none' | 'parcels' | 'letter' | 'stele' | 'board' | 'wall' | 'fortune'>('none')
const unsealingIds = ref(new Set<string>())
const sheetEl = ref<HTMLElement | null>(null)
let lastTrigger: HTMLElement | null = null

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

const cinemaSub = computed(() => {
  if (parcelCount.value <= 0) return '片库 · 随时开场'
  let base = `片库 · 今日新到 ${parcelCount.value} 部`
  const first = parcelList.value[0]
  const raw = String(first?.title || first?.name || '').trim()
  if (raw) {
    const name = raw.length > 7 ? `${raw.slice(0, 7)}…` : raw
    base += `，含《${name}》`
  }
  return base
})

const postSub = computed(() => {
  if (!hasLetter.value) return '暂无新信'
  if (letterRead.value) return '昨天的信到了'
  const teaser = typeof letterData.value.teaser === 'string' ? letterData.value.teaser.trim() : ''
  if (teaser) {
    const t = teaser.length > 12 ? `${teaser.slice(0, 12)}…` : teaser
    return `信里说：『${t}』`
  }
  const rec = typeof letterData.value.recommendation === 'string' ? letterData.value.recommendation.trim() : ''
  if (rec) {
    const r = rec.length > 12 ? `${rec.slice(0, 12)}…` : rec
    return `今日推荐：『${r}』`
  }
  return '昨天的信到了'
})

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
    const rec = d as Record<string, unknown>
    const list = rec.achievements
    if (Array.isArray(list) && list.length > 0) {
      const lit = list.filter(
        (a) => a && typeof a === 'object' && (a as Record<string, unknown>).unlocked === true,
      ).length
      if (list.length > lit) {
        if (list.length - lit === 1) return `徽章墙 · ${lit}/${list.length} 枚 · 还差 1 枚`
        return `徽章墙 · 已点亮 ${lit}/${list.length} 枚`
      }
    }
    const n = rec.achievement_count
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

const questDoneCount = computed(() => quests.value.filter((q) => q.done).length)
const questNextLabel = computed(() => quests.value.find((q) => !q.done)?.label ?? '')

/* ---------- 弹层 ---------- */
function openParcels() {
  lastTrigger = document.activeElement as HTMLElement | null
  overlay.value = 'parcels'
  nextTick(() => {
    try { sheetEl.value?.focus() } catch { /* 静默 */ }
  })
}

function openLetter() {
  if (!hasLetter.value) return
  markLetterRead()
  lastTrigger = document.activeElement as HTMLElement | null
  overlay.value = 'letter'
  nextTick(() => {
    try { sheetEl.value?.focus() } catch { /* 静默 */ }
  })
}

function closeOverlay() {
  overlay.value = 'none'
  nextTick(() => {
    if (lastTrigger && document.contains(lastTrigger)) {
      lastTrigger.focus()
    }
    lastTrigger = null
  })
}

/* 抽屉下滑关闭（仅手机端：无 touch 的桌面端不受影响） */
const sheetTouchY = ref<number | null>(null)
function onSheetTouchStart(e: TouchEvent) {
  if (e.touches.length === 1) sheetTouchY.value = e.touches[0].clientY
}
function onSheetTouchEnd(e: TouchEvent) {
  if (sheetTouchY.value === null) return
  const dy = e.changedTouches[0].clientY - sheetTouchY.value
  sheetTouchY.value = null
  if (dy > 90 && window.matchMedia('(max-width: 1023px)').matches) closeOverlay()
}

function openOverlaySheet(kind: 'stele' | 'board' | 'wall' | 'fortune') {
  lastTrigger = document.activeElement as HTMLElement | null
  overlay.value = kind
  nextTick(() => {
    try { sheetEl.value?.focus() } catch { /* 静默 */ }
  })
}

/* ---------- 观影碑 · 排行榜 ---------- */
const lbBoard = ref<'hot' | 'duration'>('hot')
const lbPeriod = ref<'day' | 'week'>('day')
const lbData = ref<{ items: Array<Record<string, unknown>>; my_rank: number | null } | null>(null)
const lbLoading = ref(false)

async function loadLeaderboard() {
  lbLoading.value = true
  try {
    const d = await panelApi.leaderboard(lbBoard.value, lbPeriod.value)
    lbData.value = { items: d.items ?? [], my_rank: d.my_rank ?? null }
  } catch {
    lbData.value = null
  } finally {
    lbLoading.value = false
  }
}

function openStele() {
  void loadLeaderboard()
  openOverlaySheet('stele')
}

watch([lbBoard, lbPeriod], () => { void loadLeaderboard() })

const lbRows = computed(() => lbData.value?.items ?? [])
const lbMyRank = computed(() => lbData.value?.my_rank ?? null)
const lbScopeText = computed(() =>
  lbPeriod.value === 'day' ? '今日（自然日）· 全服' : '本周（自然周）· 全服',
)
const lbUnitText = computed(() =>
  lbBoard.value === 'hot'
    ? (lbPeriod.value === 'day' ? '今日播放' : '本周播放')
    : (lbPeriod.value === 'day' ? '今日观影' : '本周观影'),
)

const steleSub = computed(() => {
  const first = lbRows.value[0] as Record<string, unknown> | undefined
  if (!first) return '虚位以待，等你开播'
  const name = String(first.name ?? first.nickname ?? '')
  const n = lbBoard.value === 'hot' ? Number(first.count ?? 0) : Math.round(Number(first.seconds ?? 0) / 3600 * 10) / 10
  const unit = lbBoard.value === 'hot' ? '次' : '小时'
  const when = lbPeriod.value === 'day' ? '今日' : '本周'
  return `${when}最热 · ${name} ${n}${unit}`
})

/* ---------- 服主告示牌 ---------- */
const boardData = computed<Record<string, unknown>>(
  () => (board.value.data ?? {}) as Record<string, unknown>,
)

const boardSub = computed(() => {
  if (boardData.value.visible === true) {
    const sig = String(boardData.value.signature ?? '服主')
    const text = typeof boardData.value.text === 'string' ? boardData.value.text.trim() : ''
    if (!text) return `服主留言 · ${sig}`
    const hook = text.length <= 12 ? text : `${text.slice(0, 12)}…`
    return `『${hook}』——${sig}`
  }
  return '等服主留言'
})

function openBoard() {
  openOverlaySheet('board')
}

/* ---------- 到此一游 · 留言墙 ---------- */
const wallPosts = ref<Array<Record<string, unknown>>>([])
const wallLoading = ref(false)
const wallInput = ref('')
const wallPosting = ref(false)

async function loadWall() {
  wallLoading.value = true
  try {
    const d = await panelApi.wallList()
    wallPosts.value = Array.isArray(d.posts) ? d.posts : []
  } catch {
    wallPosts.value = []
  } finally {
    wallLoading.value = false
  }
}

async function submitWall() {
  const c = wallInput.value.trim()
  if (!c || wallPosting.value) return
  if (c.length > 60) {
    useToast().warning('留言最多 60 字')
    return
  }
  wallPosting.value = true
  try {
    await panelApi.wallPost(c)
    wallInput.value = ''
    await loadWall()
  } catch (e) {
    useToast().error(humanizeError(e))
  } finally {
    wallPosting.value = false
  }
}

function parseWallTime(v: unknown): number {
  if (v === null || v === undefined || v === '') return NaN
  if (typeof v === 'number') return v < 1e12 ? v * 1000 : v
  if (typeof v === 'string') {
    const t = Date.parse(v)
    return Number.isNaN(t) ? NaN : t
  }
  return NaN
}

const wallSub = computed(() => {
  const posts = wallPosts.value
  if (posts.length === 0) return '来留下第一句话吧'
  let sub = `已有 ${posts.length} 条足迹`
  let latest = -Infinity
  for (const p of posts) {
    const t = parseWallTime(p?.created_at)
    if (!Number.isNaN(t) && t > latest) latest = t
  }
  if (latest !== -Infinity) {
    const diff = Date.now() - latest
    if (diff >= 0) {
      const MIN = 60 * 1000
      const HOUR = 60 * MIN
      if (diff < HOUR) sub += ` · 最新 ${Math.max(1, Math.floor(diff / MIN))} 分钟前`
      else if (diff < 24 * HOUR) sub += ` · 最新 ${Math.floor(diff / HOUR)} 小时前`
    }
  }
  return sub
})

function openWall() {
  void loadWall()
  openOverlaySheet('wall')
}

/* ---------- 每日抽一签 ---------- */
const fortune = ref<Record<string, unknown> | null>(null)
const fortuneLoading = ref(false)
const fortuneDrawing = ref(false)

async function loadFortune() {
  fortuneLoading.value = true
  try {
    fortune.value = await panelApi.fortuneToday()
  } catch {
    fortune.value = null
  } finally {
    fortuneLoading.value = false
  }
}

async function drawFortune() {
  if (fortuneDrawing.value) return
  fortuneDrawing.value = true
  try {
    fortune.value = await panelApi.fortuneDraw()
  } catch (e) {
    useToast().error(humanizeError(e))
  } finally {
    fortuneDrawing.value = false
  }
}

const fortuneDrew = computed(() => fortune.value?.drew === true)

const fortuneSub = computed(() =>
  fortuneDrew.value ? `今日：${String(fortune.value?.level ?? '')}` : '每日一签，试试手气',
)

function openFortune() {
  void loadFortune()
  openOverlaySheet('fortune')
}

/* ---------- 昼夜 ---------- */
const daypart = computed(() => {
  const h = new Date().getHours()
  if (h >= 5 && h < 9) return 'dawn'
  if (h >= 9 && h < 17) return 'day'
  if (h >= 17 && h < 19) return 'dusk'
  return 'night'
})

/* ---------- 弹层小工具 ---------- */
const sheetTitle = computed(() => {
  switch (overlay.value) {
    case 'parcels': return '📦 今日封存'
    case 'letter': return '✉️ 每日一封'
    case 'stele': return '🏆 观影碑'
    case 'board': return '📢 服主告示牌'
    case 'wall': return '💬 到此一游'
    case 'fortune': return '🎋 每日一签'
    default: return ''
  }
})

function fmtHours(v: unknown): string {
  const s = Number(v ?? 0)
  if (!Number.isFinite(s) || s <= 0) return '0 小时'
  const h = Math.round((s / 3600) * 10) / 10
  return `${h} 小时`
}

function fmtWallTime(v: unknown): string {
  if (typeof v !== 'string' || !v) return ''
  const dt = new Date(v)
  if (Number.isNaN(dt.getTime())) return ''
  const now = new Date()
  const sameDay = dt.toDateString() === now.toDateString()
  const mm = String(dt.getMonth() + 1).padStart(2, '0')
  const dd = String(dt.getDate()).padStart(2, '0')
  const hh = String(dt.getHours()).padStart(2, '0')
  const mi = String(dt.getMinutes()).padStart(2, '0')
  return sameDay ? `${hh}:${mi}` : `${mm}-${dd} ${hh}:${mi}`
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
  // 用户手势内同步占位新窗口，避免 await 后被弹窗拦截
  const placeholder = window.open('about:blank', '_blank')
  if (!placeholder) {
    useToast().warning('浏览器拦截了新标签页，请允许本站弹窗后重试')
    void router.push('/game/operator')
    return
  }
  try {
    const base = await ensureEmbyBase()
    if (base) {
      placeholder.location.href = base.replace(/\/+$/, '') + '/web/index.html'
      return
    }
  } catch {
    /* 落到兜底 */
  }
  placeholder.close()
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
  void loadLeaderboard()
  void loadWall()
  void loadFortune()
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

.quest .stamp-done { position: absolute; top: -11px; right: 12px; transform: rotate(-6deg); border: 2px solid var(--red); color: var(--red); background: rgba(255,253,246,.92); border-radius: 6px; padding: 2px 9px; font-size: 11px; font-weight: 800; letter-spacing: 1px; }
.mark.fortune-pending .pin { animation: fortunePulse 2.2s ease-in-out infinite; }
@keyframes fortunePulse { 0% { box-shadow: 0 0 0 0 rgba(242,192,99,.55); } 70% { box-shadow: 0 0 0 14px rgba(242,192,99,0); } 100% { box-shadow: 0 0 0 0 rgba(242,192,99,0); } }
.m-feed { background: rgba(255,253,246,.94); border: 1px solid var(--line); border-radius: 99px; padding: 7px 14px; font-size: 12px; color: var(--ink2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.m-feed i { color: var(--green); font-style: normal; margin-right: 4px; }

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

/* ---------- 昼夜天色 ---------- */
.dp-dawn .world {
  background: linear-gradient(180deg, #f8e2bd 0%, #F6F1E7 55%, #f2e0bd 100%);
}
.dp-dusk .world {
  background: linear-gradient(180deg, #f3cf9e 0%, #efdfc2 55%, #e9d0a8 100%);
}
.dp-night .world {
  background: linear-gradient(180deg, #2e3a5c 0%, #3d4a68 55%, #333a52 100%);
}
.dp-dawn .scene { filter: brightness(.97) sepia(.14); }
.dp-day .scene { filter: none; }
.dp-dusk .scene { filter: brightness(.9) sepia(.22); }
.dp-night .scene { filter: brightness(.58) saturate(.82); }
.dp-night .world::after {
  /* 星空点缀（纯 CSS，不新增图片） */
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(circle at 12% 14%, rgba(255, 246, 220, .95) 0 2px, transparent 3px),
    radial-gradient(circle at 28% 8%, rgba(255, 246, 220, .8) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 45% 18%, rgba(255, 246, 220, .9) 0 2px, transparent 3px),
    radial-gradient(circle at 62% 10%, rgba(255, 246, 220, .7) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 78% 22%, rgba(255, 246, 220, .85) 0 2px, transparent 3px),
    radial-gradient(circle at 92% 8%, rgba(255, 246, 220, .9) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 8% 32%, rgba(255, 246, 220, .6) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 55% 30%, rgba(255, 246, 220, .55) 0 1.5px, transparent 2.5px);
}
.dp-night .world::before {
  /* 夜色罩：地图整体压暗，纸色建筑依然可辨 */
  content: "";
  position: absolute;
  inset: 0;
  z-index: 20;
  pointer-events: none;
  background: rgba(20, 28, 52, .12);
}
.dp-night .lighthouse .pin {
  /* 夜晚灯塔亮灯 */
  box-shadow: 0 0 22px 8px rgba(255, 214, 120, .8);
  border-color: #f2c063;
}

/* ---------- 新弹层内容：排行榜 / 告示牌 / 留言墙 / 抽签 ---------- */
.w-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
.w-tab {
  flex: 1;
  padding: 8px 0;
  border-radius: 10px;
  border: 1px solid var(--line);
  background: var(--paper-card);
  font-size: 14px;
  font-weight: 700;
  color: var(--ink2);
  cursor: pointer;
}
.w-tab.on {
  background: #a06c24;
  border-color: #a06c24;
  color: #FFFDF6;
}
.w-subtabs {
  display: flex;
  gap: 6px;
  margin-bottom: 12px;
}
.w-subtab {
  padding: 5px 14px;
  border-radius: 99px;
  border: 1px solid var(--line);
  background: transparent;
  font-size: 12.5px;
  color: var(--ink2);
  cursor: pointer;
}
.w-subtab.on {
  background: var(--ink);
  border-color: var(--ink);
  color: #FFFDF6;
}
.w-lb-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.w-lb-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--paper-card);
}
.w-lb-row.me {
  border-color: #a06c24;
  box-shadow: 0 0 0 1px #a06c24;
}
.w-lb-rank {
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: #efe7d6;
  font-size: 13px;
  font-weight: 800;
  color: var(--ink2);
}
.w-lb-rank.top {
  background: #a06c24;
  color: #FFFDF6;
}
.w-lb-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.w-lb-main b {
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.w-lb-main small {
  font-size: 11.5px;
  color: var(--ink2);
}
.w-lb-num {
  font-size: 13px;
  font-weight: 700;
  color: #a06c24;
  white-space: nowrap;
}
.w-sheet-foot {
  margin: 12px 0 0;
  font-size: 12px;
  color: var(--ink2);
  text-align: center;
  line-height: 1.7;
}
.w-board-text {
  font-size: 15px;
  line-height: 1.9;
  white-space: pre-wrap;
  margin: 4px 0 10px;
}
.w-board-sig {
  text-align: right;
  font-size: 13px;
  color: var(--ink2);
}
.w-wall-list {
  list-style: none;
  margin: 0 0 12px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 40dvh;
  overflow-y: auto;
}
.w-wall-row {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 8px 12px;
  background: var(--paper-card);
}
.w-wall-nick {
  font-size: 13px;
  font-weight: 800;
  color: #a06c24;
}
.w-wall-time {
  font-size: 11px;
  color: var(--ink2);
  margin-left: 8px;
}
.w-wall-text {
  margin: 4px 0 0;
  font-size: 14px;
  line-height: 1.7;
}
.w-wall-form {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}
.w-wall-form input {
  flex: 1;
  min-width: 0;
  padding: 9px 12px;
  border-radius: 10px;
  border: 1px solid var(--line);
  background: #FFFDF6;
  font-size: 14px;
  color: var(--ink);
}
.w-wall-btn, .w-fortune-btn {
  padding: 9px 18px;
  border-radius: 10px;
  border: none;
  background: #a06c24;
  color: #FFFDF6;
  font-size: 14px;
  font-weight: 800;
  cursor: pointer;
  white-space: nowrap;
}
.w-wall-btn:disabled, .w-fortune-btn:disabled {
  opacity: .5;
  cursor: default;
}
.w-fortune {
  text-align: center;
  padding: 10px 0 4px;
}
.w-fortune-level {
  display: inline-block;
  font-size: 22px;
  font-weight: 900;
  color: #D9482B;
  border: 2px solid #D9482B;
  border-radius: 12px;
  padding: 6px 22px;
  margin-bottom: 12px;
  transform: rotate(-3deg);
}
.w-fortune-text {
  font-size: 16px;
  line-height: 1.9;
  margin: 0 0 10px;
}
.w-fortune-yiji {
  font-size: 14px;
  color: var(--ink2);
}
.w-fortune-yiji b {
  color: var(--green);
}
.w-fortune-draw {
  text-align: center;
  padding: 16px 0 6px;
}
.w-fortune-pot {
  font-size: 52px;
  margin-bottom: 6px;
}

/* /game 页内嵌：不定高，留呼吸边距（手机端另有布局） */
.world-root.is-game { height: auto; padding: 16px 0; }

/* ---------- 手机端 /game：竖屏长地图 + 底部抽屉（≤1023px，仅 is-game 生效） ---------- */
@media (max-width: 1023px) {
  .world-root.is-game { display: block; height: auto; }
  .world-root.is-game .world {
    width: 100%;
    aspect-ratio: 390 / 1560;
    overflow: hidden;
  }
  /* 竖图切片后桌面道路/小径与手机地标坐标对不上，手机端不画这两组线 */
  .world-root.is-game .scene .scene-roads, .world-root.is-game .scene .scene-trails { display: none; }
  /* 地标放大：64px 图标 + 名牌 + 状态小字（照手机图纸） */
  .world-root.is-game .mark .pin { width: 64px; height: 64px; font-size: 30px; border-radius: 20px; }
  .world-root.is-game .mark .pin::after { width: 13px; height: 13px; bottom: -7px; }
  .world-root.is-game .mark .name { font-size: 13.5px; padding: 4px 11px; border-radius: 8px; margin-top: 13px; }
  .world-root.is-game .mark .sub { font-size: 11px; padding: 2px 8px; border-radius: 6px; margin-top: 4px; width: max-content; max-width: 56vw; margin-left: auto; margin-right: auto; }
  .world-root.is-game .mark .dot { min-width: 22px; height: 22px; font-size: 12px; top: -7px; right: calc(50% - 40px); padding: 0 6px; }
  .world-root.is-game .mark:hover .pin { transform: none; }
  /* 手机坐标（!important 覆盖行内桌面坐标；照手机图纸百分比） */
  .world-root.is-game .m-lighthouse { left: 72% !important; top: 11% !important; }
  .world-root.is-game .m-cinema { left: 38% !important; top: 15% !important; }
  .world-root.is-game .m-gamehall { left: 70% !important; top: 24% !important; }
  .world-root.is-game .m-post { left: 30% !important; top: 27% !important; }
  .world-root.is-game .m-parcels { left: 62% !important; top: 35% !important; }
  .world-root.is-game .m-shop { left: 26% !important; top: 42% !important; }
  .world-root.is-game .m-collector { left: 68% !important; top: 46% !important; }
  .world-root.is-game .m-board { left: 26% !important; top: 64% !important; }
  .world-root.is-game .m-stele { left: 72% !important; top: 66% !important; }
  .world-root.is-game .m-wall { left: 30% !important; top: 79% !important; }
  .world-root.is-game .m-fortune { left: 68% !important; top: 81% !important; }
  .world-root.is-game .m-player { left: 50% !important; top: 93% !important; }
  .world-root.is-game .m-player .tok { width: 44px; height: 44px; font-size: 16px; border-width: 3px; }
  .world-root.is-game .m-player .who { font-size: 12px; padding: 2px 10px; }
  /* 分区牌 */
  .world-root.is-game .m-zone { position: absolute; z-index: 26; font-size: 13px; font-weight: 800; color: #a06c24; background: rgba(255,253,246,.9); border: 1.5px dashed #a06c24; border-radius: 10px; padding: 4px 12px; letter-spacing: 1px; left: 10px; top: 118px; }
  .world-root.is-game .m-zone-2 { top: 52.5%; }
  .world-root.is-game .m-zone-line { position: absolute; z-index: 4; left: 14px; right: 14px; top: 56.4%; border-top: 2px dashed rgba(160,108,36,.5); }
  /* 滑动提示 */
  .world-root.is-game .m-scroll-hint { position: absolute; left: 0; right: 0; top: 49.5%; bottom: auto; z-index: 35; pointer-events: none; background: none; padding: 10px 0; text-align: center; }
  .world-root.is-game .m-scroll-hint span { display: inline-block; background: #2A2620; color: #FFFDF6; border-radius: 999px; padding: 7px 18px; font-size: 13px; font-weight: 700; }
  /* 紧凑 HUD：问候 + 连签/积分/Lv 两行；桌面 hello 卡与 hud 在手机端隐藏 */
  .world-root.is-game .hello, .world-root.is-game .hud { display: none; }
  .world-root.is-game .m-hud { display: block; position: absolute; top: 0; left: 0; right: 0; z-index: 40; padding: 12px 14px 18px; background: linear-gradient(180deg, rgba(246,241,231,.97), rgba(246,241,231,.7) 70%, transparent); }
  .world-root.is-game .game-goalbar { margin: 0 0 10px; background: rgba(255,253,246,.96); border: 1px solid var(--line); border-radius: 12px; padding: 8px 12px; box-shadow: 0 2px 4px rgba(42,38,32,.08); }
  .world-root.is-game .m-goal-text { font-size: 12px; font-weight: 700; }
  .world-root.is-game .m-goal-text b { color: #a06c24; }
  .world-root.is-game .m-goal-bar { height: 6px; background: #EFE7D6; border-radius: 99px; margin-top: 6px; overflow: hidden; }
  .world-root.is-game .m-goal-bar i { display: block; height: 100%; background: var(--paper-primary); border-radius: 99px; transition: width .3s ease; }
  .world-root.is-game .m-feed { position: absolute; left: 14px; right: 14px; top: 85.5%; z-index: 30; }
  .world-root.is-game .m-hello { font-size: 14px; font-weight: 800; }
  .world-root.is-game .m-stats { font-size: 11.5px; color: #6B6257; margin-top: 2px; }
  .world-root.is-game .m-stats b { color: #a06c24; }
  /* 世界动态/今日任务两块浮动卡：手机端不占地图，隐藏（数据不变，只是排版） */
  .world-root.is-game .feed, .world-root.is-game .quest { display: none; }
  /* 底部抽屉：占屏高约 70%，顶部圆角+下拉把手 */
  .world-root.is-game .w-overlay { align-items: flex-end; padding: 0; }
  .world-root.is-game .w-sheet { width: 100%; max-height: 72dvh; border-radius: 24px 24px 0 0; border-bottom: none; padding: 8px 20px 24px; }
  .world-root.is-game .w-grip { width: 44px; height: 5px; border-radius: 99px; background: #D8CDB8; margin: 6px auto 8px; }
  .world-root.is-game .w-sheet-head { margin-bottom: 10px; }
}
/* 桌面端隐藏手机专用元素 */
.m-zone, .m-zone-line, .m-scroll-hint, .w-grip, .m-hud, .game-goalbar, .m-feed { display: none; }
@media (max-width: 1023px) {
  .world-root.is-game .m-zone, .world-root.is-game .m-zone-line,
  .world-root.is-game .m-scroll-hint, .world-root.is-game .w-grip,
  .world-root.is-game .m-hud, .world-root.is-game .game-goalbar,
  .world-root.is-game .m-feed { display: block; }
}

@media (prefers-reduced-motion: reduce) {
  .mark .pin {
    transition: none;
  }
  .mark.fortune-pending .pin {
    animation: none;
  }
  .mark:hover .pin {
    transform: none;
  }
  .quest .bar i {
    transition: none;
  }
}
</style>
