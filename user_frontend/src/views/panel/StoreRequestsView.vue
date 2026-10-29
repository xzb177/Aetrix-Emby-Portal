<template>
  <div class="v4-scope">
    <div class="v4-page">
      <div class="shop-head">
        <span class="shop-stamp">求片登记</span>
        <h1>求片登记</h1>
        <p>库里没有的片子先登记求片；登记≠能播，入库后这里会标为已入库，到时再点想看（不会自动转存）。</p>
      </div>

      <!-- 商店未开启：总闸关时三个口都 404，这里只说这一句，不假装能用 -->
      <section v-if="storeOff" class="shop-sec">
        <div class="v4-empty">想看商店未开启，稍后再来看看<br><button class="v4-retry" @click="reloadAll">刷新看看</button></div>
      </section>

      <template v-else>
        <div v-if="needLogin" class="v4-empty">
          求片要先登录<RouterLink to="/login" class="v4-retry">去登录</RouterLink>
        </div>
        <div v-if="needMember" class="v4-empty">
          求片需要有效的会员订阅<RouterLink to="/shop" class="v4-retry">去补给站办会员</RouterLink>
        </div>

        <!-- 登记：库外 tmdb 登记或附议；库内不登记、指去想看 -->
        <section class="shop-sec">
          <div class="v4-sec-title">登记求片</div>
          <div class="v4-sec-sub">输入 TMDB 编号登记，片名可选填；同一部片子登记两次不重复建行，第二次计入附议人数</div>
          <div class="req-form">
            <input
              v-model="tmdbInput"
              type="text"
              inputmode="numeric"
              placeholder="TMDB 编号，例如 603"
              class="req-input"
              @keydown.enter="submit"
            />
            <input
              v-model="titleInput"
              type="text"
              placeholder="片名（可选）"
              class="req-input"
              maxlength="255"
              @keydown.enter="submit"
            />
            <button class="v4-retry" :disabled="submitting || !tmdbInput.trim()" @click="submit">
              {{ submitting ? '登记中…' : '登记求片' }}
            </button>
          </div>
          <div v-if="submitErr" class="v4-err">{{ submitErr }}</div>
          <div v-if="submitResult" class="req-result">
            <div>{{ submitResult.message || stateLabel(submitResult.state) }}</div>
            <div v-if="submitResult.in_library" class="v4-empty">
              这部片子已经在库里了，直接去想看，不用登记求片<RouterLink to="/want" class="v4-retry">去想看商店</RouterLink>
            </div>
            <div v-else class="req-line">
              <span class="k">状态</span><span>{{ stateLabel(submitResult.state) }}</span>
              <span class="k">附议</span><span>{{ submitResult.vote_count }} 人（加权 {{ submitResult.weighted_vote_count ?? submitResult.vote_count }}）</span>
            </div>
          </div>
        </section>

        <!-- 求片榜：处理中的行按附议数排榜；没投过可点附议 -->
        <section class="shop-sec">
          <div class="v4-sec-title">求片榜</div>
          <div v-if="boardLoading" class="v4-skeleton" style="height:64px"></div>
          <div v-else-if="boardErr" class="v4-err">{{ boardErr }}<br><button class="v4-retry" @click="loadBoard">重试</button></div>
          <div v-else-if="board.length === 0" class="v4-empty">还没人登记求片，上面登记一部试试</div>
          <div v-else class="req-list">
            <div v-for="r in board" :key="r.request_id ?? r.tmdb_id" class="req-row">
              <span class="req-name">{{ r.movie_name || r.tmdb_id }}</span>
              <span class="req-state">{{ stateLabel(r.state) }}</span>
              <span class="req-votes">加权 {{ r.weighted_vote_count ?? r.vote_count }} · {{ r.vote_count }} 人附议</span>
              <button
                v-if="r.request_id !== null && !r.voted"
                class="v4-retry"
                :disabled="voting === r.tmdb_id"
                @click="vote(r.tmdb_id)"
              >{{ voting === r.tmdb_id ? '附议中…' : '附议' }}</button>
              <span v-else-if="r.voted" class="req-voted">已附议</span>
              <button
                v-if="welfareRealm && r.request_id !== null && !reportedIds.has(r.request_id)"
                class="v4-retry"
                @click="openReport(r.request_id)"
              >举报</button>
              <span v-else-if="welfareRealm && r.request_id !== null && reportedIds.has(r.request_id)" class="req-voted">已举报</span>
              <div v-if="reportingId === r.request_id" class="req-report">
                <select v-model="reportReason" class="req-input" aria-label="举报原因">
                  <option value="spam">垃圾内容</option>
                  <option value="duplicate">重复求片</option>
                  <option value="violation">违规内容</option>
                </select>
                <input
                  v-model="reportDetail"
                  type="text"
                  placeholder="补充说明（可选）"
                  class="req-input"
                  maxlength="500"
                />
                <button class="v4-retry" :disabled="reportBusy" @click="submitReport(r.request_id)">{{ reportBusy ? '提交中…' : '提交举报' }}</button>
                <button class="v4-retry" :disabled="reportBusy" @click="reportingId = null">取消</button>
                <div v-if="reportErr" class="v4-err">{{ reportErr }}</div>
              </div>
            </div>
          </div>
        </section>

        <!-- 我的求片：创建的 + 附议过的；已入库的可去想看 -->
        <section class="shop-sec">
          <div class="v4-sec-title">我的求片</div>
          <div v-if="mineLoading" class="v4-skeleton" style="height:64px"></div>
          <div v-else-if="mineErr" class="v4-err">{{ mineErr }}<br><button class="v4-retry" @click="loadMine">重试</button></div>
          <div v-else-if="mine.length === 0" class="v4-empty">你还没登记或附议过求片</div>
          <div v-else class="req-list">
            <div v-for="r in mine" :key="r.request_id ?? r.tmdb_id" class="req-row">
              <span class="req-name">{{ r.movie_name || r.tmdb_id }}</span>
              <span class="req-state">{{ stateLabel(r.state) }}</span>
              <span class="req-votes">加权 {{ r.weighted_vote_count ?? r.vote_count }} · {{ r.vote_count }} 人附议</span>
              <RouterLink v-if="r.state === 'stored' || r.in_library" to="/want" class="v4-retry">去想看</RouterLink>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { storeRequestsApi, type StoreRequestRecord, type StoreRequestState } from '@/api'
import { governanceApi, type GovernanceReportReason } from '@/api/governance'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const userStore = useUserStore()
/** 举报入口仅公益服：与补给站同款 userStore.isFreeRealm 信号，拿不到按非公益服处理 */
const welfareRealm = computed(() => userStore.isFreeRealm)

const storeOff = ref(false)
const needLogin = ref(false)
const needMember = ref(false)

const tmdbInput = ref('')
const titleInput = ref('')
const submitting = ref(false)
const submitErr = ref('')
const submitResult = ref<StoreRequestRecord | null>(null)

const board = ref<StoreRequestRecord[]>([])
const boardLoading = ref(false)
const boardErr = ref('')
const voting = ref('')

const mine = ref<StoreRequestRecord[]>([])
const mineLoading = ref(false)
const mineErr = ref('')

// 与后端 R1_STATE_LABELS 同口径；未知态原样回，不编造
function stateLabel(state: StoreRequestState): string {
  return (
    {
      registered: '已登记',
      seeking: '寻源中',
      stored: '已入库',
      not_found: '找不到',
      withdrawn: '已撤回',
      in_library: '已在库',
    }[state] ?? state
  )
}

function statusOf(err: unknown): number | null {
  const s = (err as { response?: { status?: number } })?.response?.status
  return typeof s === 'number' ? s : null
}

function detailOf(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : ''
}

// 三个口共用同一套闸：401 登录、403 会员、404 商店未开启；其余按调用方各自报错
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

async function submit() {
  const key = tmdbInput.value.trim()
  if (!key) return
  // 后端只收纯数字 tmdb_id，先在前端拦一句，不发无用请求
  if (!/^\d+$/.test(key)) {
    submitErr.value = 'tmdb_id 只要纯数字。'
    return
  }
  submitting.value = true
  submitErr.value = ''
  submitResult.value = null
  try {
    const title = titleInput.value.trim()
    submitResult.value = await storeRequestsApi.postRequest(key, title || undefined)
    toast.success(submitResult.value.message || stateLabel(submitResult.value.state))
    await Promise.all([loadBoard(), loadMine()])
  } catch (err) {
    if (!gateOf(err)) {
      submitErr.value = detailOf(err) || '登记失败，稍后再试'
    }
  } finally {
    submitting.value = false
  }
}

// 榜单附议：同一 tmdb 再登记一次即附议（后端不重复建行）
async function vote(tmdbId: string) {
  voting.value = tmdbId
  try {
    const res = await storeRequestsApi.postRequest(tmdbId)
    toast.success(res.message || stateLabel(res.state))
    await Promise.all([loadBoard(), loadMine()])
  } catch (err) {
    if (!gateOf(err)) {
      toast.error(detailOf(err) || '附议失败，稍后再试')
    }
  } finally {
    voting.value = ''
  }
}

// 举报：仅公益服入口；原因与后端 REPORT_REASONS 逐字一致；服务端文案原样回
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

async function loadBoard() {  boardLoading.value = true
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
  } catch (err) {
    if (!gateOf(err)) {
      mineErr.value = '我的求片加载失败，稍后再试'
    }
  } finally {
    mineLoading.value = false
  }
}

function reloadAll() {
  storeOff.value = false
  loadBoard()
  loadMine()
}

onMounted(() => {
  loadBoard()
  loadMine()
})
</script>

<style scoped>
.req-form {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 12px;
}
.req-input {
  flex: 1 1 160px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: transparent;
  color: var(--ink);
  font-size: 14px;
}
.req-result {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
  font-size: 14px;
}
.req-line {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-top: 8px;
  font-size: 13px;
}
.req-line .k {
  color: var(--ink2);
}
.req-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 12px;
}
.req-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
  font-size: 14px;
}
.req-name {
  flex: 1 1 140px;
  min-width: 0;
  font-weight: 600;
}
.req-state {
  color: var(--ink2);
  font-size: 13px;
}
.req-votes {
  color: var(--ink2);
  font-size: 13px;
}
.req-voted {
  color: var(--ink2);
  font-size: 13px;
}
.req-report {
  flex: 1 1 100%;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.req-report .v4-err {
  flex: 1 1 100%;
}
</style>
