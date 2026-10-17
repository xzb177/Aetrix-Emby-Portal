<template>
  <div class="v4-scope">
    <div class="v4-page">
      <div class="sr-only" aria-live="polite">{{ liveMsg }}</div>
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">营地门口 · 组队处</span>
        <h1>🤝 结伴同行</h1>
        <p>在营地门口举起牌子 —— 组队探险啦！</p>
        <div v-if="info && !disabled" class="v4-fire">
          🎒 已组队 <b>{{ info.invited_count ?? 0 }}</b> 人
          <span v-if="totalRebate > 0"> · 累计带路费 +{{ totalRebate }}⚡</span>
        </div>
        <div v-else-if="loading" class="v4-skeleton" aria-hidden="true" style="height:32px;width:60%;margin-top:10px"></div>
      </header>

      <!-- 加载失败 -->
      <section v-if="failed" class="v4-sec">
        <div class="v4-err">
          组队暗号加载失败，可能是网络波动<br>
          <button class="v4-retry" @click="load">重试</button>
        </div>
      </section>

      <template v-if="!failed">
        <!-- 举牌子：组队暗号 -->
        <section class="v4-sec" :aria-busy="loading">
          <div v-if="loading" class="v4-skeleton" aria-hidden="true" style="height:190px"></div>
          <template v-else-if="disabled">
            <div class="board off">
              <div class="shout">📣 组 队 暗 号</div>
              <p class="off-note">
                结伴同行当前未开启。管理员开启后，这里会显示你的专属组队暗号。
              </p>
            </div>
          </template>
          <template v-else>
            <div class="board">
              <div class="shout">📣 组 队 暗 号</div>
              <div class="code">{{ info?.code || '···' }}</div>
              <button
                class="copybtn"
                :class="{ done: copiedField === 'code' }"
                :disabled="!info?.code"
                @click="copyText(info?.code || '', 'code')"
              >{{ copiedField === 'code' ? '✓ 已复制！' : '📋 一键复制组队暗号' }}</button>
              <div class="pole"></div>
            </div>
            <div class="linkrow">
              <input class="linkinput" :value="inviteLink" readonly aria-label="组队链接">
              <button class="linkbtn" :disabled="!inviteLink" @click="copyText(inviteLink, 'link')">
                {{ copiedField === 'link' ? '✓ 已复制' : '复制链接' }}
              </button>
            </div>
          </template>
        </section>
      </template>

      <!-- 组队规则：数值全部来自后端 config -->
        <section v-if="!loading && info?.config && !disabled" class="v4-sec">
          <div class="rule">
            📜 <b>组队规则</b><br>
            朋友用你的组队暗号加入营地 —— 他得 <b>+{{ info.config.invitee_reward_points }}⚡</b> 见面礼，
            你得 <b>+{{ info.config.reward_points }}⚡</b> 带路费；
            他每次充能量，你再拿 <b>{{ info.config.rebate_percent }}%</b> 回扣。<br>
            组队暗号终身有效，多带多得。
          </div>
        </section>

        <!-- 已组队的伙伴 -->
        <section v-if="!loading" class="v4-sec" :aria-busy="recordsLoading">
          <h2 class="v4-sec-title">🎒 已组队<template v-if="!recordsErr"> · {{ recordsTotal }} 人</template></h2>
          <div class="v4-sec-sub">跟着你一起出发的伙伴</div>
          <div v-if="recordsErr" class="v4-err">
            伙伴名单加载失败，可能是网络波动<br>
            <button class="v4-retry" @click="loadRecords" :disabled="recordsLoading">{{ recordsLoading ? '重试中…' : '重试' }}</button>
          </div>
          <div v-else-if="recordsLoading" class="v4-skeleton" aria-hidden="true" style="height:120px"></div>
          <div v-else-if="!records.length" class="v4-empty">
            {{ disabled ? '结伴同行当前未开启，开启后组队的伙伴会出现在这里' : '还没有伙伴同行' }}<template v-if="!disabled"><br>把组队暗号喊出去，第一个朋友在等你</template>
          </div>
          <div v-else class="mates">
            <div v-for="r in records" :key="r.id" class="mate">
              <div class="av" aria-hidden="true">{{ avatarOf(r.invitee_username) }}</div>
              <div class="mid">
                <div class="nm">{{ r.invitee_username }}</div>
                <div class="dt">{{ fmtDate(r.created_at) }} 加入营地</div>
              </div>
              <div class="rw">+{{ r.reward_points }} ⚡</div>
            </div>
          </div>
          <div v-if="!recordsErr && recordsTotal > 50" class="v4-sec-sub">仅显示最近 50 条</div>
        </section>

        <!-- 带路费账本 -->
        <section v-if="!loading" class="v4-sec" :aria-busy="rebatesLoading">
          <h2 class="v4-sec-title">💰 带路费账本</h2>
          <div class="v4-sec-sub">伙伴充能量，你的回扣自动记在这里<template v-if="!rebatesErr"> · 累计 +{{ totalRebate }}⚡</template></div>
          <div v-if="rebatesErr" class="v4-err">
            带路费账本加载失败，可能是网络波动<br>
            <button class="v4-retry" @click="loadRebates" :disabled="rebatesLoading">{{ rebatesLoading ? '重试中…' : '重试' }}</button>
          </div>
          <div v-else-if="rebatesLoading" class="v4-skeleton" aria-hidden="true" style="height:120px"></div>
          <div v-else-if="!rebates.length" class="v4-empty">
            {{ disabled ? '结伴同行当前未开启，开启后伙伴充能量的回扣会记在这里' : '账本还是空的' }}<template v-if="!disabled"><br>伙伴充能量后，回扣会自动记一笔</template>
          </div>
          <div v-else class="mates">
            <div v-for="r in rebates" :key="r.id" class="mate">
              <div class="av dot" aria-hidden="true"></div>
              <div class="mid">
                <div class="nm">{{ r.description || '充能量回扣' }}</div>
                <div class="dt">{{ fmtTime(r.created_at) }}</div>
              </div>
              <div class="rw">+{{ r.amount }} ⚡</div>
            </div>
          </div>
        </section>

        <!-- 见面礼记录 -->
        <section v-if="!loading" class="v4-sec" :aria-busy="promotionsLoading">
          <h2 class="v4-sec-title">🎁 见面礼记录</h2>
          <div class="v4-sec-sub">每带一位伙伴，营地当场发的谢礼</div>
          <div v-if="promotionsErr" class="v4-err">
            见面礼记录加载失败，可能是网络波动<br>
            <button class="v4-retry" @click="loadPromotions" :disabled="promotionsLoading">{{ promotionsLoading ? '重试中…' : '重试' }}</button>
          </div>
          <div v-else-if="promotionsLoading" class="v4-skeleton" aria-hidden="true" style="height:120px"></div>
          <div v-else-if="!promotions.length" class="v4-empty">
            {{ disabled ? '结伴同行当前未开启，开启后带人成功的谢礼会记在这里' : promoEnabled ? '带上第一位伙伴，谢礼会记在这里' : '见面礼当前未开启；开启后，带人成功会在这里记一笔' }}
          </div>
          <div v-else class="mates">
            <div v-for="r in promotions" :key="r.id" class="mate">
              <div class="av dot gold" aria-hidden="true"></div>
              <div class="mid">
                <div class="nm">带上了 {{ r.invitee_username }}</div>
                <div class="dt">{{ r.reward_type_label }} · {{ fmtTime(r.created_at) }}</div>
              </div>
              <div class="rw">+{{ r.reward_value }}{{ r.reward_type === 'days' ? ' 天' : ' ⚡' }}</div>
            </div>
          </div>
        </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  inviteApi,
  type MyInviteInfo, type InvitationRecordRow, type RebateRow,
  type MyPromotions, type PromotionRewardRow,
} from '@/api/economy'
import { useClipboard } from '@/composables/useClipboard'

const { copy } = useClipboard()

const loading = ref(true)
const failed = ref(false)
const info = ref<MyInviteInfo | null>(null)
const records = ref<InvitationRecordRow[]>([])
const recordsTotal = ref(0)
const rebates = ref<RebateRow[]>([])
const totalRebate = ref(0)
const promotions = ref<PromotionRewardRow[]>([])
const promoConfig = ref<MyPromotions['config'] | null>(null)
/* R27：三小节各自独立失败标记（原 .catch(() => 空数据) 把失败吞成"真空"文案） */
const recordsErr = ref(false)
const rebatesErr = ref(false)
const promotionsErr = ref(false)
/* R48B-4：各路独立小 loading（重试只重拉自己那一路，不整页塌骨架） */
const recordsLoading = ref(false)
const rebatesLoading = ref(false)
const promotionsLoading = ref(false)
/* F4：三路加载器请求序号守卫——慢的旧响应/旧失败不许覆盖新结果 */
let recordsSeq = 0
let rebatesSeq = 0
let promotionsSeq = 0
const copiedField = ref('')
/* R48B-5：复制成功的读屏播报 */
const liveMsg = ref('')

const inviteLink = computed(() => {
  if (!info.value?.code) return ''
  return `${window.location.origin}/login?invite=${encodeURIComponent(info.value.code)}`
})

/** 邀请是否由管理员开启（来自 /my-code 的 config） */
const disabled = computed(() => info.value?.config?.enabled === false)
/** 见面礼开关（来自 /promotions 的 config） */
const promoEnabled = computed(() => promoConfig.value?.enabled === true)

const AVATARS = ['🦊', '🐼', '🐸', '🦄', '🐙', '🦁', '🐯', '🦉']
function avatarOf(name?: string | null): string {
  if (!name) return '🦊'
  let h = 0
  for (const c of name) h = (h * 31 + c.codePointAt(0)!) % 997
  return AVATARS[h % AVATARS.length]
}

function fmtDate(iso?: string | null): string {
  if (!iso) return '—'
  // R38 P3-10：不再硬切片 ISO —— new Date 解析 + NaN 回退原串
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const pad = (n: number) => (n < 10 ? '0' + n : String(n))
  // F6：跨年记录保留年份，避免 MM-DD 歧义；当年仍显示 MM-DD
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  return d.getFullYear() === new Date().getFullYear() ? md : `${d.getFullYear()}-${md}`
}
function fmtTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const pad = (n: number) => (n < 10 ? '0' + n : String(n))
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

let copyTimer: number | undefined
let liveTimer: number | undefined
async function copyText(text: string, field: string) {
  const ok = await copy(text, field === 'link' ? '组队链接' : '组队暗号')
  if (!ok) return
  copiedField.value = field
  liveMsg.value = field === 'link' ? '组队链接已复制' : '组队暗号已复制'
  window.clearTimeout(liveTimer)
  liveTimer = window.setTimeout(() => { liveMsg.value = '' }, 1800)
  window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => { if (copiedField.value === field) copiedField.value = '' }, 1800)
}
onUnmounted(() => {
  window.clearTimeout(copyTimer)
  window.clearTimeout(liveTimer)
})

/* R48B-4：空数据回退（失败时不吞成"真空"文案，由各自 Err 标记驱动错误态） */
const emptyRecords = { total: 0, records: [] as InvitationRecordRow[] }
const emptyRebates = { total_rebate: 0, rebates: [] as RebateRow[] }
const emptyPromotions: MyPromotions = {
  total: 0,
  config: {
    enabled: false, reward_type: 'balance', amount: 0, days: 0,
    reward_types: [], reward_type_labels: {},
  },
  rewards: [],
}

/** R48B-4：只拉组队暗号一路；首屏骨架由它关闭 */
async function loadMy() {
  loading.value = true
  failed.value = false
  try {
    info.value = await inviteApi.myCode()
  } catch {
    failed.value = true
  } finally {
    loading.value = false
  }
}

/** R48B-4：只拉伙伴名单一路（带本区块小 loading，重试不塌整页） */
async function loadRecords() {
  /* F4：取号；旧请求的结果/失败不许覆盖新请求 */
  const seq = ++recordsSeq
  recordsLoading.value = true
  try {
    const rec = await inviteApi.records({ limit: 50 })
    if (seq !== recordsSeq) return
    /* F5：字段缺失/非数组不吞成"真空"，走 Err 分支 */
    if (!Array.isArray(rec.records)) {
      recordsErr.value = true
      records.value = emptyRecords.records
      recordsTotal.value = 0
    } else {
      records.value = rec.records
      // total 缺失时退回本次条数，避免模板出现 "undefined 人"
      recordsTotal.value = rec.total ?? rec.records.length ?? 0
      recordsErr.value = false
    }
  } catch {
    if (seq !== recordsSeq) return
    recordsErr.value = true
    records.value = emptyRecords.records
  } finally {
    /* F4：只有关最新请求才清 loading，旧请求的 finally 不许提前关新请求的骨架 */
    if (seq === recordsSeq) recordsLoading.value = false
  }
}

/** R48B-4：只拉带路费账本一路 */
async function loadRebates() {
  /* F4：取号；旧请求的结果/失败不许覆盖新请求 */
  const seq = ++rebatesSeq
  rebatesLoading.value = true
  try {
    const reb = await inviteApi.rebates({ limit: 50 })
    if (seq !== rebatesSeq) return
    /* F5：字段缺失/非数组/累计非数字不吞成"真空"，走 Err 分支 */
    if (!Array.isArray(reb.rebates) || !Number.isFinite(reb.total_rebate)) {
      rebatesErr.value = true
      rebates.value = emptyRebates.rebates
      totalRebate.value = emptyRebates.total_rebate
    } else {
      rebates.value = reb.rebates
      totalRebate.value = reb.total_rebate
      rebatesErr.value = false
    }
  } catch {
    if (seq !== rebatesSeq) return
    rebatesErr.value = true
    rebates.value = emptyRebates.rebates
    totalRebate.value = emptyRebates.total_rebate
  } finally {
    /* F4：只有关最新请求才清 loading，旧请求的 finally 不许提前关新请求的骨架 */
    if (seq === rebatesSeq) rebatesLoading.value = false
  }
}

/** R48B-4：只拉见面礼记录一路 */
async function loadPromotions() {
  /* F4：取号；旧请求的结果/失败不许覆盖新请求 */
  const seq = ++promotionsSeq
  promotionsLoading.value = true
  try {
    const pro = await inviteApi.promotions({ limit: 50 })
    if (seq !== promotionsSeq) return
    /* F5：字段缺失/非数组不吞成"真空"，走 Err 分支 */
    if (!Array.isArray(pro.rewards)) {
      promotionsErr.value = true
      promotions.value = emptyPromotions.rewards
      promoConfig.value = emptyPromotions.config
    } else {
      promotions.value = pro.rewards
      promoConfig.value = pro.config
      promotionsErr.value = false
    }
  } catch {
    if (seq !== promotionsSeq) return
    promotionsErr.value = true
    promotions.value = emptyPromotions.rewards
    promoConfig.value = emptyPromotions.config
  } finally {
    /* F4：只有关最新请求才清 loading，旧请求的 finally 不许提前关新请求的骨架 */
    if (seq === promotionsSeq) promotionsLoading.value = false
  }
}

/** R48B-4：首屏仍全量拉；各区块"重试"只走自己那一路 */
async function load() {
  loading.value = true
  failed.value = false
  await Promise.allSettled([loadMy(), loadRecords(), loadRebates(), loadPromotions()])
}

onMounted(load)
</script>

<style scoped>
/* 举牌子：设计稿 game-invite.html 原值 */
.board {
  margin: 4px 0 0;
  background: var(--paper2);
  border: 2px solid var(--ink);
  border-radius: 14px;
  padding: 20px;
  text-align: center;
  position: relative;
  box-shadow: 4px 4px 0 var(--ink);
}
.board .pole {
  position: absolute;
  left: 50%;
  bottom: -34px;
  width: 8px;
  height: 34px;
  background: var(--ink);
  transform: translateX(-50%);
}
/* 减少动态偏好：全局 transform:none 会让杆子右偏 4px，用 margin-left 补回居中 */
@media (prefers-reduced-motion: reduce) {
  .board .pole { margin-left: -4px; }
  .copybtn:active, .linkbtn:active { transform: none; }
}
.board .shout {
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 2px;
  /* F10：抵消行尾字距，视觉回正居中 */
  text-indent: 2px;
  color: var(--red);
  margin-bottom: 6px;
}
.board .code {
  font-size: 32px;
  font-weight: 800;
  letter-spacing: 4px;
  /* F10：抵消行尾字距，视觉回正居中 */
  text-indent: 4px;
  background: var(--paper-card);
  border: 2px dashed var(--ink2);
  border-radius: var(--paper-r-md, 10px);
  padding: 14px 8px;
  margin: 10px 0;
  font-family: "Courier New", monospace;
  overflow-wrap: anywhere;
}
.copybtn {
  width: 100%;
  /* F8：继承页面 PingFang 栈，不用系统默认按钮字体 */
  font-family: inherit;
  background: var(--red);
  color: var(--on-dark, #fff);
  border: none;
  font-size: 15px;
  font-weight: 800;
  padding: 13px;
  border-radius: var(--paper-r-md, 10px);
  cursor: pointer;
  box-shadow: 0 3px 0 var(--red-deep, #a33319);
}
.copybtn:active { transform: scale(0.96); }
.copybtn.done { background: var(--green); box-shadow: 0 3px 0 var(--green-deep, #35593f); }
.copybtn:disabled { background: var(--ink2); box-shadow: none; cursor: default; }
.board.off { padding: 24px 18px; }
.off-note { font-size: 13px; color: var(--ink2); line-height: 1.8; margin: 8px 0 0; }

/* F12：牌杆伸出 34px 的留位（原内联空 div 已删，34px=牌杆高度必须保留） */
.linkrow { display: flex; gap: 8px; margin-top: 34px; }
.linkinput {
  flex: 1;
  min-width: 0;
  font-size: 16px;
  color: var(--ink2);
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  padding: 10px 12px;
}
.linkbtn {
  flex-shrink: 0;
  /* F8：继承页面 PingFang 栈，不用系统默认按钮字体 */
  font-family: inherit;
  min-height: 44px;
  /* R48B-8：固定最小宽度，文案"复制链接"/"✓ 已复制"切换时不抖动 */
  min-width: 84px;
  /* F11：防"✓ 已复制"在个别字体下折行增高 */
  white-space: nowrap;
  text-align: center;
  background: var(--paper2);
  border: 1.5px solid var(--ink);
  border-radius: var(--paper-r-md, 10px);
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}
.linkbtn:active { transform: scale(0.96); }
.linkbtn:disabled { opacity: 0.5; cursor: default; }

/* 规则卡：设计稿原值 */
.rule {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  padding: 14px 16px;
  font-size: 13px;
  line-height: 1.9;
}
.rule b { color: var(--gold); }

/* 伙伴列表：设计稿 game-invite.html 原值 */
.mates { display: flex; flex-direction: column; gap: 10px; }
.mate {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--paper-card);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  padding: 12px 14px;
}
.mate .av {
  width: 42px;
  height: 42px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--paper2);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  flex: none;
  border: 1.5px solid var(--line);
}
.mate .av.dot { width: 10px; height: 10px; font-size: 0; background: var(--green); border: none; padding: 0; }
.mate .av.dot.gold { background: var(--gold); }
.mate .mid { flex: 1; min-width: 0; }
.mate .nm { font-size: 14px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mate .dt { font-size: 11px; color: var(--ink2); margin-top: 2px; }
.mate .rw { font-size: 14px; font-weight: 800; color: var(--green); white-space: nowrap; }

.copybtn:focus-visible,
.linkbtn:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* R48B-5：读屏播报节点（视觉隐藏） */
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
</style>
