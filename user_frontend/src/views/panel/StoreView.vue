<template>
  <div class="v4-scope">
    <div class="v4-page">
      <div class="shop-head">
        <span class="shop-stamp">想看商店</span>
        <h1>想看商店</h1>
        <p>库里已有的片子才能想看，转存到你自己的盘里播；库里没有的不能想看、也不能直接开播。</p>
      </div>

      <!-- 商店未开启：总闸关时四个口都 404，这里只说这一句，不假装能用 -->
      <section v-if="storeOff" class="shop-sec">
        <div class="v4-empty">想看商店未开启，稍后再来看看<br><button class="v4-retry" @click="loadWants">刷新看看</button></div>
      </section>

      <template v-else>
        <!-- 查片：输 tmdb_id 查库内映射；在库内才给想看按钮 -->
        <section class="shop-sec">
          <div class="v4-sec-title">查片想看</div>
          <div class="v4-sec-sub">输入 TMDB 编号查一下，库里有才能点想看</div>
          <div class="want-lookup">
            <input
              v-model="tmdbInput"
              type="text"
              inputmode="numeric"
              placeholder="例如 603"
              class="want-input"
              @keydown.enter="lookup"
            />
            <button class="v4-retry" :disabled="lookingUp || !tmdbInput.trim()" @click="lookup">
              {{ lookingUp ? '查着呢…' : '查一下' }}
            </button>
          </div>
          <div v-if="lookupErr" class="v4-err">{{ lookupErr }}</div>
          <div v-if="lookupItem" class="want-card">
            <div class="want-line"><span class="k">TMDB</span><span>{{ lookupItem.tmdb_id }}</span></div>
            <div v-if="lookupItem.in_library" class="want-line">
              <span class="k">库内</span><span>已有对应条目，可以想看</span>
            </div>
            <div v-else class="v4-empty">片库里还没有这部片子，不能转入想看，也不能直接开播。</div>
            <button
              v-if="lookupItem.in_library"
              class="want-btn"
              :disabled="wanting"
              @click="doWant"
            >{{ wanting ? '想看中…' : '想看，转存到我的盘' }}</button>
          </div>
          <div v-if="wantResult" class="want-result" :class="`st-${wantResult.state}`">
            {{ wantResult.message || stateLabel(wantResult.state) }}
          </div>
          <div v-if="wantNeedLogin" class="v4-empty">
            想看要先登录<RouterLink to="/login" class="v4-retry">去登录</RouterLink>
          </div>
          <div v-if="wantNeedMember" class="v4-empty">
            想看需要有效的会员订阅<RouterLink to="/shop" class="v4-retry">去补给站办会员</RouterLink>
          </div>
        </section>

        <!-- 我的想看清单：本人 trigger=store_want 的任务行 -->
        <section class="shop-sec">
          <div class="v4-sec-title">我的想看</div>
          <div v-if="wantsLoading" class="v4-skeleton" style="height:64px"></div>
          <div v-else-if="wantsNeedLogin" class="v4-empty">
            想看清单要先登录<RouterLink to="/login" class="v4-retry">去登录</RouterLink>
          </div>
          <div v-else-if="wantsNeedMember" class="v4-empty">
            想看清单需要有效的会员订阅<RouterLink to="/shop" class="v4-retry">去补给站办会员</RouterLink>
          </div>
          <div v-else-if="wantsErr" class="v4-err">{{ wantsErr }}<br><button class="v4-retry" @click="loadWants">重试</button></div>
          <div v-else-if="wants.length === 0" class="v4-empty">还没想看过，上面查一片点想看试试</div>
          <div v-else class="want-list">
            <div v-for="w in wants" :key="w.job_id ?? w.tmdb_id" class="want-row">
              <span class="want-tmdb">{{ w.tmdb_id }}</span>
              <span class="want-state" :class="`st-${w.state}`">{{ stateLabel(w.state) }}</span>
              <span class="want-msg">{{ w.message }}</span>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { storeApi, type StoreItem, type StoreWantRecord, type StoreWantState } from '@/api'
import { useToast } from '@/composables/useToast'

const toast = useToast()

const tmdbInput = ref('')
const lookingUp = ref(false)
const lookupErr = ref('')
const lookupItem = ref<StoreItem | null>(null)
const wanting = ref(false)
const wantResult = ref<StoreWantRecord | null>(null)
const wantNeedLogin = ref(false)
const wantNeedMember = ref(false)

const wants = ref<StoreWantRecord[]>([])
const wantsLoading = ref(false)
const wantsErr = ref('')
const wantsNeedLogin = ref(false)
const wantsNeedMember = ref(false)
const storeOff = ref(false)

function stateLabel(state: StoreWantState): string {
  return {
    wanted: '已加入转存队列',
    resolving: '正在确认片源',
    transferring: '正在转存',
    ready: '已转存到你的盘',
    failed: '转存失败',
    missing_source: '库里还没有',
  }[state]
}

function statusOf(err: unknown): number | null {
  const s = (err as { response?: { status?: number } })?.response?.status
  return typeof s === 'number' ? s : null
}

function detailOf(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : ''
}

async function lookup() {
  const key = tmdbInput.value.trim()
  if (!key) return
  lookingUp.value = true
  lookupErr.value = ''
  lookupItem.value = null
  wantResult.value = null
  wantNeedLogin.value = false
  wantNeedMember.value = false
  try {
    lookupItem.value = await storeApi.getItem(key)
  } catch (err) {
    if (statusOf(err) === 404 && detailOf(err) === '想看商店未开启') {
      storeOff.value = true
    } else {
      lookupErr.value = '查不到，稍后再试'
    }
  } finally {
    lookingUp.value = false
  }
}

async function doWant() {
  if (!lookupItem.value) return
  wanting.value = true
  wantResult.value = null
  wantNeedLogin.value = false
  wantNeedMember.value = false
  try {
    wantResult.value = await storeApi.postWant(lookupItem.value.tmdb_id)
    toast.success(wantResult.value.message || stateLabel(wantResult.value.state))
    await loadWants()
  } catch (err) {
    if (statusOf(err) === 401) {
      wantNeedLogin.value = true
    } else if (statusOf(err) === 403) {
      wantNeedMember.value = true
    } else if (statusOf(err) === 404 && detailOf(err) === '想看商店未开启') {
      storeOff.value = true
    } else {
      toast.error('想看失败，稍后再试')
    }
  } finally {
    wanting.value = false
  }
}

async function loadWants() {
  wantsLoading.value = true
  wantsErr.value = ''
  wantsNeedLogin.value = false
  wantsNeedMember.value = false
  try {
    const res = await storeApi.getWants()
    wants.value = res.items
  } catch (err) {
    if (statusOf(err) === 401) {
      wantsNeedLogin.value = true
    } else if (statusOf(err) === 403) {
      wantsNeedMember.value = true
    } else if (statusOf(err) === 404 && detailOf(err) === '想看商店未开启') {
      storeOff.value = true
    } else {
      wantsErr.value = '想看清单加载失败，稍后再试'
    }
  } finally {
    wantsLoading.value = false
  }
}

onMounted(() => {
  // 未登录也先拉一次：401 走登录提示，不报错；总闸关则整页报未开启
  loadWants()
})
</script>

<style scoped>
/* 想看商店页：复用纸色 v4 token 与补给站头样式，只补本页的行布局 */
.shop-head {
  padding: 26px 20px 14px;
  border-bottom: 2px solid var(--ink);
}
.shop-stamp {
  display: inline-block;
  border: 1px solid var(--line);
  color: var(--ink2);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.04em;
  padding: 3px 8px;
  border-radius: 4px;
}
.want-lookup {
  display: flex;
  gap: 10px;
  margin-top: 12px;
}
.want-input {
  flex: 1;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: transparent;
  color: var(--ink);
  font-size: 14px;
}
.want-card {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
}
.want-line {
  display: flex;
  gap: 10px;
  font-size: 14px;
  padding: 2px 0;
}
.want-line .k {
  color: var(--ink2);
  min-width: 48px;
}
.want-btn {
  margin-top: 10px;
  padding: 10px 18px;
  border-radius: 10px;
  border: 1px solid var(--ink);
  background: var(--ink);
  color: var(--paper, #f5f1ea);
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
}
.want-btn:disabled {
  opacity: 0.6;
  cursor: default;
}
.want-result {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--line);
  font-size: 14px;
}
.want-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
}
.want-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  font-size: 14px;
}
.want-tmdb {
  font-weight: 700;
}
.want-state {
  color: var(--ink2);
  white-space: nowrap;
}
.want-msg {
  color: var(--ink2);
  font-size: 13px;
}
</style>
