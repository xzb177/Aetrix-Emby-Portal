<template>
  <div class="v4-scope">
    <div class="v4-page">
      <div class="shop-head">
        <span class="shop-stamp">活动提案</span>
        <h1>活动提案</h1>
        <p>提案需管理员批准才开奖；直接发起无需批准、事后审计。提案与发起不产生经验，月限与等级门槛以服务端为准。</p>
      </div>

      <section v-if="!welfareRealm" class="shop-sec">
        <div class="v4-empty">活动提案仅公益服开放</div>
      </section>

      <template v-else>
        <section class="shop-sec">
          <div class="v4-sec-title">我的等级</div>
          <div v-if="memberLoading" class="v4-skeleton" style="height:48px"></div>
          <div v-else-if="memberFailed" class="v4-err">等级数据加载失败<br><button class="v4-retry" @click="loadAll">重试</button></div>
          <div v-else-if="member" class="req-line">
            <span>Lv.{{ member.level }} {{ member.level_name }}</span>
            <span>经验 {{ member.xp }}</span>
            <span v-if="member.next_level && member.xp_to_next != null">下一级还差 {{ member.xp_to_next }}</span>
          </div>
          <div v-else class="v4-empty">暂无等级记录，默认从 Lv.1 开始</div>
        </section>

        <!-- 门槛明示：不够格不画饼，表单直接不出现 -->
        <section v-if="member && member.level < 2" class="shop-sec">
          <div class="v4-empty">
            活动提案需要 Lv.2 及以上，你现在是 Lv.{{ member.level }}<span v-if="xpToLv2 != null">，还差 {{ xpToLv2 }} 经验到 Lv.2</span> —— 先去签到、观影、攒邀请攒经验
          </div>
        </section>

        <template v-else-if="member">
          <section class="shop-sec">
            <div class="v4-sec-title">{{ mode === 'direct' ? '直接发起活动' : '提交活动提案' }}</div>
            <div class="v4-sec-sub">
              <template v-if="mode === 'proposal'">
                本月已提案 {{ proposalsThisMonth }} / 上限 {{ proposalCap }} 次（Lv.{{ member.level }}）；超限时服务端会明示
              </template>
              <template v-else>
                直接发起月限 Lv.5 每月 1 次、Lv.6 每月 2 次；剩余额度暂无查询口，超限时服务端会明示
              </template>
            </div>
            <div class="req-form">
              <button class="v4-retry" :class="{ on: mode === 'proposal' }" @click="mode = 'proposal'">提案（需批准）</button>
              <button
                v-if="member.level >= 5"
                class="v4-retry"
                :class="{ on: mode === 'direct' }"
                @click="mode = 'direct'"
              >直接发起</button>
              <span v-else class="v4-empty empty-compact"><span class="lbl">直接发起需要 Lv.5 及以上</span></span>
            </div>
            <div class="req-form">
              <input v-model="title" type="text" placeholder="活动标题" class="req-input" maxlength="200" />
              <select v-model="lotteryType" class="req-input" aria-label="抽奖类型">
                <option value="button">按钮抽奖</option>
                <option value="password">口令抽奖</option>
              </select>
              <input
                v-if="lotteryType === 'password'"
                v-model="passwordKeyword"
                type="text"
                placeholder="口令（口令抽奖必填）"
                class="req-input"
                maxlength="100"
              />
              <input v-model="chatId" type="text" inputmode="numeric" placeholder="群 ID（留空则服务端自动解析唯一允许群）" class="req-input" />
              <input v-model="drawAt" type="datetime-local" class="req-input" aria-label="开奖时间（可选）" />
              <input v-model="maxParticipants" type="text" inputmode="numeric" placeholder="最大参与人数（留空不限）" class="req-input" />
            </div>
            <div class="v4-sec-sub" style="margin-top:12px">奖品（至少 1 个）</div>
            <div v-for="(p, i) in prizes" :key="i" class="req-form">
              <input v-model="p.name" type="text" placeholder="奖品名" class="req-input" maxlength="100" />
              <select v-model="p.type" class="req-input" aria-label="奖品类型">
                <option value="days">查看天数</option>
                <option value="points">能量</option>
                <option value="whitelist">白名单</option>
              </select>
              <input v-model.number="p.value" type="text" inputmode="numeric" placeholder="价值（整数）" class="req-input" />
              <input v-model.number="p.quantity" type="text" inputmode="numeric" placeholder="数量（≥1）" class="req-input" />
              <button v-if="prizes.length > 1" class="v4-retry" @click="prizes.splice(i, 1)">删</button>
            </div>
            <div class="req-form">
              <button class="v4-retry" @click="addPrize">＋奖品</button>
              <button class="v4-retry" :disabled="submitting" @click="submit">
                {{ submitting ? '提交中…' : (mode === 'direct' ? '直接发起' : '提交提案') }}
              </button>
            </div>
            <div v-if="submitErr" class="v4-err">{{ submitErr }}</div>
            <div v-if="submitOk" class="req-result">{{ submitOk }}</div>
          </section>

          <section class="shop-sec">
            <div class="v4-sec-title">我的提案</div>
            <div v-if="mineLoading" class="v4-skeleton" style="height:48px"></div>
            <div v-else-if="mineErr" class="v4-err">{{ mineErr }}<br><button class="v4-retry" @click="loadMine">重试</button></div>
            <div v-else-if="mine.length === 0" class="v4-empty">你还没提交过活动提案</div>
            <div v-else class="req-list">
              <div v-for="p in mine" :key="p.id" class="req-row">
                <span class="req-name">{{ p.title }}</span>
                <span class="req-state">{{ proposalStatusLabel(p.status) }}</span>
                <span class="req-votes">Lv.{{ p.level_snapshot }} 提交<span v-if="p.round_id"> · 已建轮次 #{{ p.round_id }}</span></span>
              </div>
            </div>
          </section>
        </template>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { memberApi, type MyMemberInfo } from '@/api/economy'
import { lotteryGovernanceApi, type LotteryPrizeInput, type LotteryProposal } from '@/api/governance'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const userStore = useUserStore()
const welfareRealm = computed(() => userStore.isFreeRealm)

const member = ref<MyMemberInfo | null>(null)
const memberLoading = ref(false)
const memberFailed = ref(false)

const mode = ref<'proposal' | 'direct'>('proposal')
const title = ref('')
const lotteryType = ref<'button' | 'password'>('button')
const passwordKeyword = ref('')
const chatId = ref('')
const drawAt = ref('')
const maxParticipants = ref('')
const prizes = ref<LotteryPrizeInput[]>([{ name: '', type: 'days', value: 7, quantity: 1 }])
const submitting = ref(false)
const submitErr = ref('')
const submitOk = ref('')

const mine = ref<LotteryProposal[]>([])
const mineLoading = ref(false)
const mineErr = ref('')

function statusOf(err: unknown): number | null {
  const s = (err as { response?: { status?: number } })?.response?.status
  return typeof s === 'number' ? s : null
}

function detailOf(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : ''
}

/** 到 Lv.2 还差多少经验（等级表取自 memberApi，与服务端同源） */
const xpToLv2 = computed(() => {
  if (!member.value) return null
  const lv2 = member.value.levels.find((l) => l.level === 2)
  if (!lv2) return null
  return Math.max(0, lv2.xp_threshold - member.value.xp)
})

/** 提案月限设计默认值（Lv.2/3 每月 1 次、Lv.4+ 每月 2 次）；服务端配置可改，以服务端 enforced 为准 */
const proposalCap = computed(() => {
  if (!member.value) return 1
  return member.value.level >= 4 ? 2 : 1
})

/** 本月已提案数：按服务端自然月（created_at 的 YYYY-MM）计，含已拒绝行（与服务端口径一致） */
const proposalsThisMonth = computed(() => {
  const ym = new Date().toISOString().slice(0, 7)
  return mine.value.filter((p) => (p.created_at || '').slice(0, 7) === ym).length
})

function proposalStatusLabel(status: string): string {
  return (
    {
      pending: '待审批',
      approved: '已批准',
      rejected: '已驳回',
    }[status] ?? status
  )
}

function addPrize() {
  prizes.value.push({ name: '', type: 'days', value: 7, quantity: 1 })
}

/** 客户端校验镜像后端 validate_round_payload 的报错文案，拦住无用请求；最终以服务端为准 */
function validate(): string {
  if (!title.value.trim()) return '活动标题不能为空'
  if (prizes.value.length === 0) return '至少需要配置 1 个奖品'
  for (const p of prizes.value) {
    if (p.type !== 'days' && p.type !== 'points' && p.type !== 'whitelist') {
      return '奖品类型只能是 days/points/whitelist'
    }
    if (!Number.isInteger(p.value)) return '奖品价值必须是整数'
    if ((p.type === 'days' || p.type === 'points') && p.value <= 0) {
      return `${p.type === 'points' ? '积分' : '天数'}奖品的价值必须大于 0`
    }
    if (p.type === 'whitelist' && p.value < 0) return '奖品价值不能为负数'
    if (!Number.isInteger(p.quantity) || p.quantity < 1) return '奖品数量至少为 1'
  }
  if (lotteryType.value === 'password' && !passwordKeyword.value.trim()) {
    return '口令抽奖需要设置口令'
  }
  if (chatId.value.trim() && !/^-?\d+$/.test(chatId.value.trim())) return 'chat_id 必须是整数'
  if (maxParticipants.value.trim()) {
    const n = Number(maxParticipants.value.trim())
    if (!Number.isInteger(n) || n < 1) return '最大参与人数至少为 1'
  }
  return ''
}

async function submit() {
  submitErr.value = ''
  submitOk.value = ''
  const v = validate()
  if (v) {
    submitErr.value = v
    return
  }
  submitting.value = true
  try {
    const body = {
      title: title.value.trim(),
      chat_id: chatId.value.trim() ? Number(chatId.value.trim()) : null,
      prizes: prizes.value.map((p) => ({ ...p })),
      draw_at: drawAt.value.trim() || null,
      max_participants: maxParticipants.value.trim() ? Number(maxParticipants.value.trim()) : null,
      lottery_type: lotteryType.value,
      password_keyword: lotteryType.value === 'password' ? passwordKeyword.value.trim() : null,
    }
    if (mode.value === 'direct') {
      await lotteryGovernanceApi.directLaunch(body)
      submitOk.value = '直接发起成功，轮次已建立（事后审计有据可查）'
    } else {
      await lotteryGovernanceApi.submitProposal(body)
      submitOk.value = '提案已提交，待管理员批准'
    }
    toast.success(submitOk.value)
    title.value = ''
    await loadMine()
  } catch (err) {
    // 后端文案原样回：403 停权/等级不足/群未允许、400 月限/载荷/群解析
    if (statusOf(err) === 401) {
      submitErr.value = '请先登录'
    } else {
      submitErr.value = detailOf(err) || '提交失败，稍后再试'
    }
  } finally {
    submitting.value = false
  }
}

async function loadMine() {
  mineLoading.value = true
  mineErr.value = ''
  try {
    const res = await lotteryGovernanceApi.listMyProposals()
    mine.value = res.items
  } catch (err) {
    mineErr.value = detailOf(err) || '我的提案加载失败，稍后再试'
  } finally {
    mineLoading.value = false
  }
}

async function loadAll() {
  memberLoading.value = true
  memberFailed.value = false
  try {
    member.value = await memberApi.info()
  } catch {
    memberFailed.value = true
  } finally {
    memberLoading.value = false
  }
  if (welfareRealm.value) await loadMine()
}

onMounted(() => {
  loadAll()
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
</style>
