<!-- 桌面发现页右栏（C档第2批）：世界入口卡 / 观影碑 / 服主告示牌 /
     每日抽一签 / 到此一游留言墙。全部是现行设施，数据源与世界地图
     弹层同源（panelApi leaderboard/wall/fortune、panel store board），
     只搬入新版式，不增减设施、不改逻辑。 -->
<template>
  <aside class="d3-right" aria-label="世界与动态">
    <!-- 世界入口卡：地图缩略 + 进入世界（与日志头「探险世界」按钮同行为） -->
    <section id="disc-world">
      <div class="d3-sec-head">
        <div>
          <span class="d3-k">WORLD</span>
          <div class="d3-sec-title">世界</div>
          <div class="d3-sec-sub">地图、榜单、留言都在这</div>
        </div>
      </div>
      <div class="d3-world-map">
        <div class="d3-world-card">
          <div style="min-width:0">
            <div class="t">探险世界</div>
            <div class="s">全屏地图 · 纸痕测绘</div>
          </div>
          <button type="button" class="d3-btn-ghost" @click="enterWorld">进入世界</button>
        </div>
      </div>
    </section>

    <!-- 观影碑：热播榜 · 日/周榜页签 · 前五 -->
    <section class="d3-card" aria-label="观影碑">
      <div class="d3-sec-head">
        <div>
          <span class="d3-k">CHARTS</span>
          <div class="d3-sec-title">观影碑</div>
          <div class="d3-sec-sub">热播榜 · {{ period === 'day' ? '日榜' : '周榜' }}</div>
        </div>
        <div class="d3-tabs" role="tablist" aria-label="榜单周期">
          <button type="button" role="tab" :class="{ on: period === 'day' }" :aria-selected="period === 'day'" @click="period = 'day'">日榜</button>
          <button type="button" role="tab" :class="{ on: period === 'week' }" :aria-selected="period === 'week'" @click="period = 'week'">周榜</button>
        </div>
      </div>
      <div v-if="lbLoading && lbRows.length === 0" class="d3-empty">正在抄榜…</div>
      <div v-else-if="lbRows.length === 0" class="d3-empty">还没有人上榜，快去看片抢头名</div>
      <div v-else>
        <div v-for="(r, i) in lbRows.slice(0, 5)" :key="i + '-' + (r.name ?? '')" class="d3-rk">
          <i :class="{ top: i < 3 }">{{ i + 1 }}</i>
          {{ String(r.name ?? '') }}
          <span>热度 {{ Number(r.count ?? 0).toLocaleString('zh-CN') }}</span>
        </div>
      </div>
      <div style="margin-top:8px">
        <RouterLink to="/game" class="d3-sec-link">查看完整榜单 <span aria-hidden="true">→</span></RouterLink>
      </div>
    </section>

    <!-- 服主告示牌：与世界地图同源（panel store board） -->
    <section class="d3-card d3-notice" aria-label="服主告示牌">
      <div class="d3-sec-head">
        <div>
          <span class="d3-k">NOTICE</span>
          <div class="d3-sec-title">服主告示牌</div>
        </div>
      </div>
      <template v-if="boardVisible">
        <p>{{ boardText }}</p>
        <div class="from">—— {{ boardSig }}</div>
      </template>
      <div v-else class="d3-empty">服主还没留言，改天再来看看</div>
    </section>

    <!-- 每日抽一签：与世界地图同源（fortuneToday/fortuneDraw） -->
    <section class="d3-card" aria-label="每日抽一签">
      <div class="d3-sec-head" style="margin-bottom:6px">
        <div>
          <span class="d3-k">FORTUNE</span>
          <div class="d3-sec-title">每日抽一签</div>
        </div>
      </div>
      <div class="d3-fortune">
        <template v-if="fortuneLoading && !fortune">正在摇签筒…</template>
        <template v-else-if="fortuneDrew">
          <div class="big">{{ String(fortune?.level ?? '') }}</div>
          <p>{{ String(fortune?.text ?? '') }}<br>宜{{ String(fortune?.yi ?? '') }} · 忌{{ String(fortune?.ji ?? '') }}</p>
        </template>
        <template v-else>
          <p>每日一签，试试手气</p>
          <div style="margin-top:10px">
            <button type="button" class="d3-btn-ghost" :disabled="fortuneDrawing" @click="drawFortune">
              {{ fortuneDrawing ? '摇签中…' : '抽一签' }}
            </button>
          </div>
        </template>
      </div>
    </section>

    <!-- 到此一游留言墙：与世界地图同源（wallList）；最新两条，写留言去世界 -->
    <section class="d3-card" aria-label="到此一游留言墙">
      <div class="d3-sec-head">
        <div>
          <span class="d3-k">GUESTBOOK</span>
          <div class="d3-sec-title">到此一游</div>
          <div class="d3-sec-sub">世界留言墙 · 最新两条</div>
        </div>
        <RouterLink to="/game" class="d3-sec-link">写留言 <span aria-hidden="true">→</span></RouterLink>
      </div>
      <div v-if="wallLoading && wallPosts.length === 0" class="d3-empty">正在翻墙…</div>
      <div v-else-if="wallPosts.length === 0" class="d3-empty">墙上还空着，来留下第一句话吧</div>
      <template v-else>
        <div v-for="p in wallPosts.slice(0, 2)" :key="String(p.id)" class="d3-msg">
          「{{ String(p.content ?? '') }}」
          <div class="who">{{ String(p.nickname ?? '') }} · {{ fmtWallTime(p.created_at) }}</div>
        </div>
      </template>
    </section>
  </aside>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { RouterLink } from 'vue-router'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { panelApi, humanizeError } from '@/composables/usePanelApi'
import { useHomeViewMode } from '@/composables/useHomeViewMode'
import { useToast } from '@/composables/useToast'

const panel = usePanelStore()
const user = useUserStore()
const { board } = storeToRefs(panel)
const { setMode } = useHomeViewMode()

function enterWorld() {
  setMode('world')
}

/* ---------- 观影碑 ---------- */
const period = ref<'day' | 'week'>('day')
const lbRows = ref<Array<Record<string, unknown>>>([])
const lbLoading = ref(false)
async function loadLeaderboard() {
  lbLoading.value = true
  try {
    const d = await panelApi.leaderboard('hot', period.value)
    lbRows.value = Array.isArray(d.items) ? d.items : []
  } catch {
    lbRows.value = []
  } finally {
    lbLoading.value = false
  }
}
watch(period, () => { void loadLeaderboard() })

/* ---------- 服主告示牌（board 已随 loadHome 到达） ---------- */
const boardData = computed(() => (board.value.data ?? {}) as Record<string, unknown>)
const boardVisible = computed(() => boardData.value.visible === true)
const boardText = computed(() => String(boardData.value.text ?? ''))
const boardSig = computed(() => String(boardData.value.signature ?? '服主'))

/* ---------- 每日抽一签 ---------- */
const fortune = ref<Record<string, unknown> | null>(null)
const fortuneLoading = ref(false)
const fortuneDrawing = ref(false)
const fortuneDrew = computed(() => fortune.value?.drew === true)
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

/* ---------- 留言墙 ---------- */
const wallPosts = ref<Array<Record<string, unknown>>>([])
const wallLoading = ref(false)
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
function fmtWallTime(v: unknown): string {
  if (v === null || v === undefined || v === '') return ''
  const t = typeof v === 'number' ? (v < 1e12 ? v * 1000 : v) : Date.parse(String(v))
  if (Number.isNaN(t)) return ''
  const d = new Date(t)
  const now = new Date()
  const mins = Math.floor((now.getTime() - t) / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  if (d.toDateString() === now.toDateString()) {
    const p = (n: number) => String(n).padStart(2, '0')
    return `今天 ${p(d.getHours())}:${p(d.getMinutes())}`
  }
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

onMounted(() => {
  if (!user.isLoggedIn) return
  void loadLeaderboard()
  void loadFortune()
  void loadWall()
})
</script>
