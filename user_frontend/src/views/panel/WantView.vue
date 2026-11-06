<template>
  <div class="v4-scope">
    <div class="v4-page ww-page">
      <div class="v4-loghead">
        <span class="v4-stamp">想看 · 今日营业</span>
        <h1>想看</h1>
        <p>库里有就转存到你的盘，库里没有就先求片。</p>
      </div>

      <!-- 商店未开启：总闸关时各口都 404，这里只说这一句，不假装能用 -->
      <section v-if="storeOff" class="v4-sec">
        <div class="v4-empty">想看未开启，稍后再来看看<br><button class="v4-retry" @click="reloadAll">刷新看看</button></div>
      </section>

      <template v-else>
        <div v-if="needLogin" class="v4-sec">
          <div class="v4-empty">想看要先登录<RouterLink to="/login" class="v4-retry">去登录</RouterLink></div>
        </div>
        <div v-if="needMember" class="v4-sec">
          <div class="v4-empty">想看需要有效的会员订阅<RouterLink to="/shop" class="v4-retry">去补给站办会员</RouterLink></div>
        </div>

        <div class="ww-grid">
          <div class="ww-col">
            <!-- 找片：全页唯一主路——输片名，库里有就想看，没有就点候选求片 -->
            <section class="v4-sec">
              <div class="v4-sec-title">找片</div>
              <div class="v4-sec-sub">输入片名，先看库里有没有。</div>
              <div class="ww-searchbar">
                <input
                  v-model="query"
                  type="text"
                  placeholder="输入片名，例如 蝙蝠侠"
                  class="ww-input"
                  maxlength="100"
                  @keydown.enter="runSearch"
                />
                <button class="ww-btn-pri" style="min-height:40px" :disabled="searching || !query.trim()" @click="runSearch">
                  {{ searching ? '搜着呢…' : '搜索' }}
                </button>
              </div>
              <div v-if="searching" class="v4-skeleton" style="height:64px;margin-top:12px"></div>
              <div v-else-if="searchErr" class="v4-err" style="margin-top:12px">{{ searchErr }}</div>
              <template v-else-if="searched">
                <div v-if="degraded" class="ww-note">搜索结果可能不全</div>

                <!-- 搜到了：库里有的候选行，想看直接转存 -->
                <div v-if="inLibCands.length" style="margin-top:12px">
                  <div v-for="c in inLibCands" :key="`lib-${c.kind}-${c.tmdb_id}`" class="ww-row">
                    <img v-if="c.poster_url" :src="imageAuthUrl(c.poster_url)" :alt="c.name" class="ww-poster big" />
                    <div v-else class="ww-poster big ww-poster-ph"><span>{{ c.name }}</span></div>
                    <div class="ww-grow">
                      <div class="ww-name">{{ c.name }}</div>
                      <div class="ww-meta">{{ candMeta(c) }}</div>
                    </div>
                    <button
                      v-if="!wantedIds.has(c.tmdb_id)"
                      class="ww-btn-pri ww-btn-sm"
                      :disabled="wanting === c.tmdb_id"
                      @click="doWant(c.tmdb_id)"
                    >{{ wanting === c.tmdb_id ? '想看中…' : '想看' }}</button>
                    <span v-else class="ww-done">已想看</span>
                  </div>
                </div>

                <!-- TMDB 未配置时的回退：本地库按片名命中（无编号，只能标在库） -->
                <div v-if="lookupItems.length" style="margin-top:12px">
                  <div v-for="it in lookupItems" :key="`lk-${it.id}`" class="ww-row">
                    <img v-if="it.poster_url" :src="imageAuthUrl(it.poster_url)" :alt="it.name" class="ww-poster big" />
                    <div v-else class="ww-poster big ww-poster-ph"><span>{{ it.name }}</span></div>
                    <div class="ww-grow">
                      <div class="ww-name">{{ it.name }}</div>
                      <div class="ww-meta">{{ lookupMeta(it) }}</div>
                    </div>
                    <span class="ww-badge b-ready">已在库</span>
                  </div>
                </div>

                <!-- 没搜到：库里还没有；有候选时点中哪条就登记哪条 -->
                <div v-if="notFound" class="v4-card ww-notfound">
                  <div class="ww-grow">
                    <div class="ww-name">库里还没有这部片</div>
                    <div class="ww-meta">{{ notLibCands.length ? '下面这些就是它，点中哪条就登记哪条。' : '换个片名试试，或者看看求片榜里有没有人求过。' }}</div>
                  </div>
                  <button v-if="notLibCands.length" class="ww-btn-pri ww-btn-sm" @click="scrollToCands">求这部片</button>
                </div>
                <div v-if="notLibCands.length" ref="candAnchor">
                  <div class="ww-divider">可以求片</div>
                  <div v-for="c in notLibCands" :key="`req-${c.kind}-${c.tmdb_id}`" class="ww-row">
                    <img v-if="c.poster_url" :src="imageAuthUrl(c.poster_url)" :alt="c.name" class="ww-poster big" />
                    <div v-else class="ww-poster big ww-poster-ph"><span>{{ c.name }}</span></div>
                    <div class="ww-grow">
                      <div class="ww-name">{{ c.name }}</div>
                      <div class="ww-meta">{{ candMeta(c) }}</div>
                    </div>
                    <button
                      v-if="!registeredIds.has(c.tmdb_id)"
                      class="ww-btn-pri ww-btn-sm"
                      :disabled="registering === c.tmdb_id"
                      @click="doRegister(c)"
                    >{{ registering === c.tmdb_id ? '登记中…' : '求片' }}</button>
                    <span v-else class="ww-done">已登记</span>
                  </div>
                </div>
              </template>
            </section>
          </div>

          <div class="ww-col">
            <!-- 求片榜：处理中按想看人数排榜；点「我也想看」即同片再登记计票 -->
            <section class="v4-sec">
              <div class="v4-sec-title">求片榜</div>
              <div class="v4-sec-sub">想看的人越多，排得越靠前。</div>
              <div v-if="boardLoading" class="v4-skeleton" style="height:64px"></div>
              <div v-else-if="boardErr" class="v4-err">{{ boardErr }}<br><button class="v4-retry" @click="loadBoard">重试</button></div>
              <div v-else-if="board.length === 0" class="v4-empty">还没人求片，上面搜一部登记试试</div>
              <div v-else>
                <div v-for="(r, i) in board" :key="r.request_id ?? r.tmdb_id" class="ww-row">
                  <span class="ww-rank">{{ i + 1 }}</span>
                  <div class="ww-grow">
                    <div class="ww-name">{{ r.movie_name || r.tmdb_id }} <span class="ww-badge" :class="reqBadgeClass(r.state)">{{ reqStateLabel(r.state) }}</span></div>
                    <div class="ww-meta">{{ r.vote_count }} 人想看</div>
                  </div>
                  <div class="ww-stack">
                    <button
                      v-if="r.state === 'stored' || r.in_library"
                      class="ww-btn-pri ww-btn-sm"
                      :disabled="wanting === r.tmdb_id"
                      @click="doWant(r.tmdb_id)"
                    >{{ wanting === r.tmdb_id ? '想看中…' : '想看' }}</button>
                    <button
                      v-else-if="r.request_id !== null && !r.voted"
                      class="ww-btn-ghost ww-btn-sm"
                      :disabled="voting === r.tmdb_id"
                      @click="vote(r)"
                    >{{ voting === r.tmdb_id ? '投着呢…' : '我也想看' }}</button>
                    <span v-else-if="r.voted" class="ww-done">已想看</span>
                    <div class="ww-links">
                      <RouterLink
                        v-if="r.state === 'seeking'"
                        :to="{ path: '/source-discovery', query: { title: r.movie_name || '', tmdb_id: r.tmdb_id } }"
                        class="ww-txtlink"
                      >找源</RouterLink>
                      <a
                        v-if="welfareRealm && r.request_id !== null && r.state !== 'stored' && !r.in_library && !reportedIds.has(r.request_id)"
                        href="#"
                        class="ww-txtlink"
                        @click.prevent="openReport(r.request_id)"
                      >举报</a>
                      <span v-else-if="welfareRealm && r.request_id !== null && r.state !== 'stored' && !r.in_library && reportedIds.has(r.request_id)" class="ww-done">已举报</span>
                    </div>
                  </div>
                  <div v-if="reportingId === r.request_id" class="ww-report">
                    <select v-model="reportReason" class="ww-input" aria-label="举报原因">
                      <option value="spam">垃圾内容</option>
                      <option value="duplicate">重复求片</option>
                      <option value="violation">违规内容</option>
                    </select>
                    <input
                      v-model="reportDetail"
                      type="text"
                      placeholder="补充说明（可选）"
                      class="ww-input"
                      maxlength="500"
                    />
                    <button class="ww-btn-pri ww-btn-sm" :disabled="reportBusy" @click="submitReport(r.request_id)">{{ reportBusy ? '提交中…' : '提交举报' }}</button>
                    <button class="ww-btn-ghost ww-btn-sm" :disabled="reportBusy" @click="reportingId = null">取消</button>
                    <div v-if="reportErr" class="v4-err" style="flex:1 1 100%">{{ reportErr }}</div>
                  </div>
                </div>
              </div>
            </section>

            <!-- 我的求片：创建的 + 投过的；已入库的可直接想看 -->
            <section class="v4-sec">
              <div class="v4-sec-title">我的求片</div>
              <div v-if="mineLoading" class="v4-skeleton" style="height:64px"></div>
              <div v-else-if="mineErr" class="v4-err">{{ mineErr }}<br><button class="v4-retry" @click="loadMine">重试</button></div>
              <div v-else-if="mine.length === 0" class="v4-empty">你还没求过片</div>
              <div v-else>
                <div v-for="r in mine" :key="r.request_id ?? r.tmdb_id" class="ww-row">
                  <div class="ww-grow">
                    <div class="ww-name">{{ r.movie_name || r.tmdb_id }} <span class="ww-badge" :class="reqBadgeClass(r.state)">{{ reqStateLabel(r.state) }}</span></div>
                    <div class="ww-meta">{{ r.vote_count }} 人想看<template v-if="r.state === 'stored' || r.in_library"> · 已进库</template></div>
                  </div>
                  <button
                    v-if="r.state === 'stored' || r.in_library"
                    class="ww-btn-pri ww-btn-sm"
                    :disabled="wanting === r.tmdb_id"
                    @click="doWant(r.tmdb_id)"
                  >{{ wanting === r.tmdb_id ? '想看中…' : '想看' }}</button>
                  <RouterLink
                    v-else-if="r.state === 'seeking'"
                    :to="{ path: '/source-discovery', query: { title: r.movie_name || '', tmdb_id: r.tmdb_id } }"
                    class="ww-txtlink"
                  >找源</RouterLink>
                </div>
              </div>
            </section>

            <!-- 我的想看：本人想看转存任务行，状态徽章与后端 message 同口径 -->
            <section class="v4-sec">
              <div class="v4-sec-title">我的想看</div>
              <div v-if="wantsLoading" class="v4-skeleton" style="height:64px"></div>
              <div v-else-if="wantsErr" class="v4-err">{{ wantsErr }}<br><button class="v4-retry" @click="loadWants">重试</button></div>
              <div v-else-if="wants.length === 0" class="v4-empty">还没想看过，上面搜一片点想看试试</div>
              <div v-else>
                <div v-for="w in wants" :key="w.job_id ?? w.tmdb_id" class="ww-row">
                  <img v-if="w.poster_url" :src="imageAuthUrl(w.poster_url)" :alt="w.title || w.tmdb_id" class="ww-poster mini" />
                  <div v-else class="ww-poster mini ww-poster-ph"><span>{{ w.title || '🎬' }}</span></div>
                  <div class="ww-grow">
                    <div class="ww-name">{{ w.title || `TMDB ${w.tmdb_id}` }} <span class="ww-badge" :class="wantBadgeClass(w.state)">{{ wantBadgeLabel(w.state) }}</span></div>
                    <div class="ww-meta">{{ w.message }}</div>
                  </div>
                </div>
              </div>
            </section>

            <!-- 找源：紧凑保留一行入口；有来源线索时去确认，确认后转存到自己的盘 -->
            <section class="v4-sec">
              <RouterLink to="/source-discovery" class="v4-card ww-entry">
                <span class="ww-entry-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.6-4.6"/></svg></span>
                <span class="ww-grow">
                  <span class="ww-name" style="font-size:15px;display:block">有来源线索？</span>
                  <span class="ww-meta" style="display:block">用找源确认后，转存到你自己的盘</span>
                </span>
                <span class="ww-arrow" aria-hidden="true">→</span>
              </RouterLink>
            </section>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  mediaSeekApi,
  storeApi,
  storeRequestsApi,
  type MediaLookupItem,
  type MediaSeekCandidate,
  type StoreRequestRecord,
  type StoreRequestState,
  type StoreWantRecord,
  type StoreWantState,
} from '@/api'
import { imageAuthUrl } from '@/api/emby'
import { governanceApi, type GovernanceReportReason } from '@/api/governance'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const route = useRoute()
const userStore = useUserStore()
/** 举报入口仅公益服：与补给站同款 userStore.isFreeRealm 信号，拿不到按非公益服处理 */
const welfareRealm = computed(() => userStore.isFreeRealm)

const storeOff = ref(false)
const needLogin = ref(false)
const needMember = ref(false)

// ---------- 找片（唯一主路） ----------
const query = ref('')
const searching = ref(false)
const searched = ref(false)
const searchErr = ref('')
const degraded = ref(false)
const inLibCands = ref<MediaSeekCandidate[]>([])
const notLibCands = ref<MediaSeekCandidate[]>([])
const lookupItems = ref<MediaLookupItem[]>([])
const notFound = ref(false)
const candAnchor = ref<HTMLElement | null>(null)

const wanting = ref('')
const registering = ref('')

/** 已想看集合：我的想看清单 + 本页刚点过的（清单刷新前先本地记下，防重复点） */
const wantedIds = ref(new Set<string>())
/** 已登记集合：我的求片 + 本页刚登记的候选 */
const registeredIds = ref(new Set<string>())

function statusOf(err: unknown): number | null {
  const s = (err as { response?: { status?: number } })?.response?.status
  return typeof s === 'number' ? s : null
}

function detailOf(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : ''
}

// 各口共用同一套闸：401 登录、403 会员、404 商店未开启；其余按调用方各自报错
function gateOf(err: unknown): boolean {
  if (statusOf(err) === 401) {
    needLogin.value = true
    return true
  }
  if (statusOf(err) === 403) {
    needMember.value = true
    return true
  }
  if (statusOf(err) === 404 && detailOf(err) === '想看商店未开启') {
    storeOff.value = true
    return true
  }
  return false
}

function candMeta(c: MediaSeekCandidate): string {
  const kind = c.kind === 'movie' ? '电影' : '剧集'
  return c.year ? `${c.year} · ${kind}` : kind
}

function lookupMeta(it: MediaLookupItem): string {
  const kind = { movie: '电影', series: '剧集', season: '季', episode: '单集' }[it.type] ?? it.type
  return it.year ? `${it.year} · ${kind}` : kind
}

async function runSearch() {
  const q = query.value.trim()
  if (!q || searching.value) return
  searching.value = true
  searchErr.value = ''
  degraded.value = false
  inLibCands.value = []
  notLibCands.value = []
  lookupItems.value = []
  notFound.value = false
  try {
    const res = await mediaSeekApi.search(q)
    if (!res.configured) {
      // TMDB 未配置：回退按片名查本地库（命中即在库，无编号可点想看，只标在库）
      const lk = await mediaSeekApi.lookup(q)
      lookupItems.value = lk.items
      notFound.value = lk.items.length === 0
    } else {
      degraded.value = res.degraded
      inLibCands.value = res.results.filter((c) => c.in_library)
      notLibCands.value = res.results.filter((c) => !c.in_library)
      notFound.value = inLibCands.value.length === 0
    }
    searched.value = true
  } catch (err) {
    if (!gateOf(err)) {
      searchErr.value = detailOf(err) || '搜索失败，稍后再试'
    }
  } finally {
    searching.value = false
  }
}

function scrollToCands() {
  candAnchor.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function doWant(tmdbId: string) {
  wanting.value = tmdbId
  try {
    const res = await storeApi.postWant(tmdbId)
    toast.success(res.message || wantStateLabel(res.state))
    if (res.state !== 'failed' && res.state !== 'missing_source') {
      wantedIds.value = new Set(wantedIds.value).add(tmdbId)
    }
    await loadWants()
  } catch (err) {
    if (!gateOf(err)) {
      toast.error(detailOf(err) || '想看失败，稍后再试')
    }
  } finally {
    wanting.value = ''
  }
}

// 点候选登记：编号由候选带入，用户全程不填编号；已在库的回 in_library 引导想看
async function doRegister(c: MediaSeekCandidate) {
  registering.value = c.tmdb_id
  try {
    const res = await storeRequestsApi.postRequest(c.tmdb_id, c.name)
    if (res.in_library || res.state === 'in_library') {
      toast.success('这部片已经在库里了，可以直接想看')
      inLibCands.value = [...inLibCands.value, c]
      notLibCands.value = notLibCands.value.filter((x) => x.tmdb_id !== c.tmdb_id)
      notFound.value = false
    } else {
      toast.success(res.message || reqStateLabel(res.state))
      registeredIds.value = new Set(registeredIds.value).add(c.tmdb_id)
      await Promise.all([loadBoard(), loadMine()])
    }
  } catch (err) {
    if (!gateOf(err)) {
      toast.error(detailOf(err) || '登记失败，稍后再试')
    }
  } finally {
    registering.value = ''
  }
}

// ---------- 求片榜 / 我的求片 ----------
const board = ref<StoreRequestRecord[]>([])
const boardLoading = ref(false)
const boardErr = ref('')
const voting = ref('')

const mine = ref<StoreRequestRecord[]>([])
const mineLoading = ref(false)
const mineErr = ref('')

// 与后端 R1_STATE_LABELS 同口径；未知态原样回，不编造
function reqStateLabel(state: StoreRequestState): string {
  return (
    {
      registered: '已登记',
      seeking: '寻源中',
      stored: '已入库',
      not_found: '找不到',
      withdrawn: '已撤回',
      in_library: '已入库',
    }[state] ?? state
  )
}

function reqBadgeClass(state: StoreRequestState): string {
  return (
    {
      seeking: 'b-seek',
      stored: 'b-ready',
      in_library: 'b-ready',
      registered: 'b-plain',
      not_found: 'b-plain',
      withdrawn: 'b-plain',
    }[state] ?? 'b-plain'
  )
}

// 我也想看：同一 tmdb 再登记一次即计票（后端不重复建行）
async function vote(r: StoreRequestRecord) {
  voting.value = r.tmdb_id
  try {
    const res = await storeRequestsApi.postRequest(r.tmdb_id)
    toast.success(res.message || reqStateLabel(res.state))
    await Promise.all([loadBoard(), loadMine()])
  } catch (err) {
    if (!gateOf(err)) {
      toast.error(detailOf(err) || '没投上，稍后再试')
    }
  } finally {
    voting.value = ''
  }
}

async function loadBoard() {
  boardLoading.value = true
  boardErr.value = ''
  try {
    const res = await storeRequestsApi.getBoard()
    board.value = res.items
  } catch (err) {
    if (!gateOf(err)) {
      boardErr.value = '求片榜加载失败，稍后再试'
    }
  } finally {
    boardLoading.value = false
  }
}

async function loadMine() {
  mineLoading.value = true
  mineErr.value = ''
  try {
    const res = await storeRequestsApi.getMine()
    mine.value = res.items
    registeredIds.value = new Set([
      ...registeredIds.value,
      ...res.items.map((r) => r.tmdb_id),
    ])
  } catch (err) {
    if (!gateOf(err)) {
      mineErr.value = '我的求片加载失败，稍后再试'
    }
  } finally {
    mineLoading.value = false
  }
}

// ---------- 举报（仅公益服；原因与后端 REPORT_REASONS 逐字一致） ----------
const reportingId = ref<number | null>(null)
const reportReason = ref<GovernanceReportReason>('spam')
const reportDetail = ref('')
const reportBusy = ref(false)
const reportErr = ref('')
const reportedIds = ref(new Set<number>())

function openReport(requestId: number) {
  reportingId.value = requestId
  reportReason.value = 'spam'
  reportDetail.value = ''
  reportErr.value = ''
}

async function submitReport(requestId: number) {
  reportBusy.value = true
  reportErr.value = ''
  try {
    await governanceApi.createReport(
      requestId,
      reportReason.value,
      reportDetail.value.trim() || undefined,
    )
    reportedIds.value = new Set(reportedIds.value).add(requestId)
    reportingId.value = null
    toast.success('举报已提交，待复核')
  } catch (err) {
    // 后端文案原样回：403 治理特权已被冻结、409 已举报过该求片、404 求片不存在
    reportErr.value = detailOf(err) || '举报失败，稍后再试'
  } finally {
    reportBusy.value = false
  }
}

// ---------- 我的想看 ----------
const wants = ref<StoreWantRecord[]>([])
const wantsLoading = ref(false)
const wantsErr = ref('')

function wantStateLabel(state: StoreWantState): string {
  return {
    wanted: '已加入转存队列',
    resolving: '正在确认片源',
    transferring: '正在转存',
    ready: '已转存到你的盘',
    failed: '转存失败',
    missing_source: '库里还没有',
  }[state]
}

function wantBadgeLabel(state: StoreWantState): string {
  return {
    wanted: '准备中',
    resolving: '准备中',
    transferring: '准备中',
    ready: '可以看了',
    failed: '转存失败',
    missing_source: '库里还没有',
  }[state]
}

function wantBadgeClass(state: StoreWantState): string {
  return {
    wanted: 'b-prep',
    resolving: 'b-prep',
    transferring: 'b-prep',
    ready: 'b-ready',
    failed: 'b-fail',
    missing_source: 'b-plain',
  }[state]
}

async function loadWants() {
  wantsLoading.value = true
  wantsErr.value = ''
  try {
    const res = await storeApi.getWants()
    wants.value = res.items
    wantedIds.value = new Set([
      ...wantedIds.value,
      ...res.items
        .filter((w) => w.state !== 'failed' && w.state !== 'missing_source')
        .map((w) => w.tmdb_id),
    ])
  } catch (err) {
    if (!gateOf(err)) {
      wantsErr.value = '想看清单加载失败，稍后再试'
    }
  } finally {
    wantsLoading.value = false
  }
}

function reloadAll() {
  storeOff.value = false
  loadBoard()
  loadMine()
  loadWants()
}

onMounted(() => {
  loadBoard()
  loadMine()
  loadWants()
  // 带词进入（如从别处点了「求这部片」）：预填并直接落在搜索结果态
  const q = route.query.q
  if (typeof q === 'string' && q.trim()) {
    query.value = q.trim()
    void runSearch()
  }
})
</script>

<style scoped>
/* 想看页：结构与数值照已批准效果图（want-watch-mock），令牌用 v4 纸面口径 */
.ww-searchbar {
  display: flex;
  gap: 8px;
}
.ww-input {
  flex: 1;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  background: var(--paper-card);
  color: var(--ink);
  font-size: 14px;
}
.ww-input::placeholder {
  color: var(--line-deep);
}
.ww-btn-pri {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: none;
  cursor: pointer;
  font-weight: 600;
  white-space: nowrap;
  background: #016a71;
  color: #fff;
  border-radius: var(--paper-r-lg, 12px);
  font-size: 14px;
  min-height: 44px;
  padding: 0 20px;
}
.ww-btn-pri:active {
  background: #015a60;
}
.ww-btn-pri:disabled {
  opacity: 0.6;
  cursor: default;
}
.ww-btn-sm {
  min-height: 32px;
  padding: 0 14px;
  font-size: 13px;
  border-radius: var(--paper-r-md, 10px);
}
.ww-btn-ghost {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(1, 106, 113, 0.22);
  cursor: pointer;
  font-weight: 600;
  white-space: nowrap;
  background: rgba(1, 106, 113, 0.08);
  color: #015a60;
  font-size: 14px;
  min-height: 44px;
  padding: 0 20px;
  border-radius: var(--paper-r-lg, 12px);
}
.ww-btn-ghost.ww-btn-sm {
  min-height: 32px;
  padding: 0 14px;
  font-size: 13px;
  border-radius: var(--paper-r-md, 10px);
}
.ww-btn-ghost:disabled {
  opacity: 0.6;
  cursor: default;
}
.ww-row {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  padding: 10px 12px;
  background: var(--paper-card);
  flex-wrap: wrap;
}
.ww-row + .ww-row {
  margin-top: 8px;
}
.ww-grow {
  flex: 1;
  min-width: 0;
}
.ww-name {
  font-size: 14px;
  font-weight: 600;
}
.ww-meta {
  font-size: 12px;
  color: var(--ink2);
  margin-top: 2px;
}
.ww-badge {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: var(--paper-r-full, 999px);
  border: 1px solid;
  white-space: nowrap;
  vertical-align: 1px;
}
.ww-badge.b-seek {
  background: rgba(79, 111, 138, 0.09);
  border-color: rgba(79, 111, 138, 0.24);
  color: var(--blue);
}
.ww-badge.b-ready {
  background: rgba(79, 127, 74, 0.1);
  border-color: rgba(79, 127, 74, 0.26);
  color: var(--green);
}
.ww-badge.b-prep {
  background: rgba(185, 128, 47, 0.1);
  border-color: rgba(185, 128, 47, 0.28);
  color: #b9802f;
}
.ww-badge.b-plain {
  background: var(--paper2);
  border-color: var(--line);
  color: var(--ink2);
}
.ww-badge.b-fail {
  background: rgba(217, 72, 43, 0.08);
  border-color: rgba(217, 72, 43, 0.28);
  color: var(--red-ink, #b93a22);
}
.ww-poster {
  flex: none;
  border-radius: 6px;
  border: 1px solid rgba(42, 38, 32, 0.25);
  object-fit: cover;
  background: var(--paper2);
}
.ww-poster.big {
  width: 44px;
  height: 62px;
}
.ww-poster.mini {
  width: 34px;
  height: 48px;
}
.ww-poster-ph {
  display: flex;
  align-items: flex-end;
  padding: 4px;
  color: var(--ink2);
  font-family: var(--paper-font-serif, 'Noto Serif SC', serif);
  font-size: 11px;
  line-height: 1.2;
  overflow: hidden;
}
.ww-poster-ph span {
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
}
.ww-rank {
  flex: none;
  width: 18px;
  text-align: center;
  font-family: var(--paper-font-serif, 'Noto Serif SC', serif);
  font-size: 15px;
  color: var(--gold-deep, #9a7627);
  font-weight: 700;
}
.ww-stack {
  text-align: right;
}
.ww-links {
  margin-top: 4px;
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}
.ww-txtlink {
  font-size: 12px;
  color: var(--ink2);
  text-decoration: underline;
  text-underline-offset: 2px;
  white-space: nowrap;
}
.ww-done {
  font-size: 12px;
  color: var(--ink2);
  white-space: nowrap;
}
.ww-divider {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 14px 0 10px;
  color: var(--ink2);
  font-size: 12px;
}
.ww-divider::before,
.ww-divider::after {
  content: "";
  flex: 1;
  border-top: 1px dashed var(--line-deep);
}
.ww-notfound {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  margin-top: 12px;
}
.ww-note {
  font-size: 12px;
  color: var(--ink2);
  margin-top: 10px;
}
.ww-report {
  flex: 1 1 100%;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.ww-report .ww-input {
  flex: 1 1 140px;
}
.ww-entry {
  display: flex;
  align-items: center;
  gap: 12px;
  text-decoration: none;
  color: var(--ink);
}
.ww-entry-ic {
  flex: none;
  width: 40px;
  height: 40px;
  border-radius: var(--paper-r-md, 10px);
  background: rgba(1, 106, 113, 0.08);
  border: 1px solid rgba(1, 106, 113, 0.22);
  display: flex;
  align-items: center;
  justify-content: center;
  color: #016a71;
}
.ww-entry-ic svg {
  width: 22px;
  height: 22px;
}
.ww-arrow {
  color: var(--ink2);
  font-size: 18px;
  flex: none;
}

/* 桌面：与手机设施 1:1，只排版不同（照效果图桌面版两栏） */
@media (min-width: 1024px) {
  .v4-page.ww-page {
    max-width: 1160px;
    padding-left: 32px;
    padding-right: 32px;
  }
  .ww-grid {
    display: grid;
    grid-template-columns: 7fr 5fr;
    gap: 8px 28px;
  }
  .ww-grid .v4-sec {
    padding: 20px 0 0;
  }
}
</style>
