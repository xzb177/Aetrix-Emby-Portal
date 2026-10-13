<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <div class="shop-head">
        <span class="shop-stamp">补给站 · 今日开张</span>
        <h1>🎒 探险补给站</h1>
        <p>出发前，先备好干粮 —— 积分是路上的能量，会员是手里的执照。</p>
        <div v-if="loading" class="v4-skeleton" style="height:64px;margin-top:12px"></div>
        <div v-else class="purse">
          <div class="coin">
            <span class="ic">⚡</span>
            <div><div class="n">{{ pointsText }}</div><div class="t">能量（积分）</div></div>
          </div>
          <div class="coin">
            <span class="ic">📜</span>
            <div><div class="n">{{ memberLevelText }}</div><div class="t">{{ memberLevelName }} · 探险家</div></div>
          </div>
        </div>
      </div>

      <!-- ⚡ 能量补给 -->
      <section class="shop-sec">
        <div class="v4-sec-title">⚡ 能量补给</div>
        <div class="v4-sec-sub">充积分 = 补充能量 · {{ ratioText }} —— 点一罐看看</div>
        <div v-if="loading" class="v4-skeleton" style="height:220px"></div>
        <div v-else-if="pkgsFailed" class="v4-err">能量罐加载失败，稍后再试<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else-if="!rechargeEnabled" class="v4-empty">能量充值暂未开启<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else-if="packages.length === 0" class="v4-empty">补给站暂未上架能量罐<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <template v-else>
          <div class="shelf">
            <div
              v-for="pkg in packages"
              :key="pkg.id"
              class="can"
              :class="{ sel: selectedPkg?.id === pkg.id }"
              role="button"
              tabindex="0"
              :aria-pressed="selectedPkg?.id === pkg.id"
              @click="selectedPkg = pkg"
              @keydown.enter="selectedPkg = pkg"
              @keydown.space.prevent="selectedPkg = pkg"
            >
              <span v-if="pkg.is_popular" class="tag">最划算</span>
              <span class="bolt">{{ pkg.total_points >= 60 ? '🛢️' : '🔋' }}</span>
              <div class="amt">{{ fmtNum(pkg.total_points) }} <small>能量</small></div>
              <div class="price">¥ {{ fmtNum(pkg.price) }}</div>
              <div v-if="pkg.bonus > 0" class="per">多送 {{ fmtNum(pkg.bonus) }}</div>
            </div>
          </div>
          <div class="meter">
            <div class="fill" :style="{ '--meter-p': meterScale }"></div>
            <div class="txt">{{ meterText }}</div>
          </div>
          <button class="charge" :disabled="orderBusy" @click="doRecharge">⚡ 充能</button>
          <div class="fineprint">能量可用于续命活力值、兑换订阅、发红包 —— 不过期</div>
        </template>
      </section>

      <!-- 📜 探险执照 -->
      <section class="shop-sec">
        <div class="v4-sec-title">📜 探险执照</div>
        <div class="v4-sec-sub">买会员 = 领取执照 —— 凭执照解锁全片库，点开看细则</div>
        <div v-if="loading" class="v4-skeleton" style="height:280px"></div>
        <div v-else-if="plansFailed" class="v4-err">执照加载失败，稍后再试<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else-if="isFreeRealm" class="v4-empty">当前为公益服，全片库免费开放，无需领取执照 🎉<br v-if="realmNote" /><span v-if="realmNote" style="font-size:11px">{{ realmNote }}</span></div>
        <div v-else-if="!plansEnabled" class="v4-empty">执照领取暂未开启<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else-if="plans.length === 0" class="v4-empty">补给站暂未上架执照<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <template v-else>
          <div class="cur-toggle">
            <button :class="{ on: currency === 'y' }" @click="currency = 'y'">¥ 人民币</button>
            <button :class="{ on: currency === 'e' }" @click="currency = 'e'">⚡ 能量抵扣</button>
          </div>
          <div
            v-for="plan in sortedPlans"
            :key="plan.id"
            class="license"
            :class="{ hot: plan.is_popular, open: openPlanId === plan.id }"
            role="button"
            tabindex="0"
            :aria-expanded="openPlanId === plan.id"
            @click="togglePlan(plan.id)"
            @keydown.enter="togglePlan(plan.id)"
            @keydown.space.prevent="togglePlan(plan.id)"
          >
            <div class="ribbon" v-if="plan.is_popular"><span>最多人拿</span></div>
            <div class="face">
              <div class="wax">{{ waxText(plan) }}</div>
              <div class="info">
                <div class="nm">{{ licenseName(plan) }}</div>
                <div class="ds">{{ plan.description || `${plan.duration_days} 天全片库通行` }}</div>
              </div>
              <div class="pr">
                <div class="p">{{ planPriceText(plan) }}</div>
              </div>
            </div>
            <div class="detail">
              <div class="kv"><span>有效期</span><b>{{ plan.duration_days }} 天</b></div>
              <div class="kv" v-for="(f, i) in plan.features || []" :key="i"><span>{{ f }}</span><span></span></div>
              <div v-if="memberDiscountPct > 0" class="kv"><span>{{ memberLevelName }}折扣</span><b>已享 {{ (100 - memberDiscountPct) / 10 }} 折</b></div>
              <div class="kv"><span>到期后</span><span>自动失效，不自动续费</span></div>
              <button class="buy" :disabled="orderBusy" @click.stop="doBuyPlan(plan)" @keydown.enter.stop @keydown.space.stop>领取执照 →</button>
            </div>
          </div>
          <div class="fineprint">人民币 / 能量二选一，价格由服主在后台配置</div>
        </template>
      </section>

      <!-- ⛰️ 探险家等级 -->
      <section class="shop-sec">
        <div class="v4-sec-title">⛰️ 探险家等级</div>
        <div class="v4-sec-sub">Lv.1 → Lv.6 登山路线 —— 等级越高，执照越便宜，点开看特权</div>
        <div v-if="loading" class="v4-skeleton" style="height:260px"></div>
        <div v-else-if="!member" class="v4-err">等级数据加载失败<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else class="trail">
          <div
            v-for="lv in trailLevels"
            :key="lv.level"
            class="camp"
            :class="campClass(lv)"
            role="button"
            tabindex="0"
            @click="toggleCamp(lv.level)"
            @keydown.enter="toggleCamp(lv.level)"
            @keydown.space.prevent="toggleCamp(lv.level)"
          >
            <span class="flag">{{ campFlag(lv) }}</span>
            <div class="card">
              <div class="lv">Lv.{{ lv.level }} <small>{{ lv.name }}{{ lv.level === member.level ? ' · 你在这里' : '' }}</small></div>
              <div v-if="lv.discount_pct > 0" class="off">执照 {{ (100 - lv.discount_pct) / 10 }} 折</div>
              <div v-else class="xp">经验 {{ lv.xp_threshold }}</div>
            </div>
            <div class="perks" v-if="openCampLevel === lv.level">
              <template v-for="(b, i) in lv.benefits" :key="i"><i>★</i> {{ b }}<br /></template>
              <span style="font-size:11px">经验 {{ lv.xp_threshold }}<span v-if="lv.level === member.level && member.next_level"> → 下一级 Lv.{{ member.next_level }} 还差 {{ member.xp_to_next }}</span></span>
              <div v-if="lv.level === member.level && member.progress_pct != null" style="background:var(--paper2);border-radius:6px;height:8px;margin-top:6px;overflow:hidden">
                <div :style="{ width: member.progress_pct + '%', height: '100%', background: 'var(--red)' }"></div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 🤝 带上同伴 -->
      <section v-if="loading || inviteEnabled || inviteFailed" class="shop-sec" style="padding-bottom:24px">
        <div class="v4-sec-title">🤝 带上同伴</div>
        <div class="v4-sec-sub">邀请一位探险家，双方都得能量</div>
        <div v-if="loading" class="v4-skeleton" style="height:120px"></div>
        <div v-else-if="inviteFailed" class="v4-err">邀请码加载失败{{ inviteErrMsg ? '：' + inviteErrMsg : '' }}<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else-if="inviteEnabled" class="invite">
          <h3>🧭 结伴远行，能量翻倍</h3>
          <p>同伴用你的暗号注册并领取执照，你得 {{ inviteReward }} 能量，他得 {{ inviteeReward }} 能量 —— 上不封顶。</p>
          <div class="code">
            <!-- R27：code 为空不再显示占位 '----'，给生成中态 + 重试 -->
            <div v-if="inviteCode" class="c">{{ inviteCode }}</div>
            <div v-else class="c generating">邀请码生成中…</div>
            <button @click="copyInviteCode" :disabled="!inviteCode">复制</button>
          </div>
          <button v-if="!inviteCode" class="v4-retry" style="margin-top:10px" @click="loadAll">重试</button>
        </div>
      </section>
    </div>
    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'
import {
  paymentApi,
  currencyApi,
  inviteApi,
  memberApi,
  pointsApi,
  type SubscriptionPlan,
  type RechargePackage,
  type MyMemberInfo,
} from '@/api/economy'

const toast = useToast()

const loading = ref(true)
const pkgsFailed = ref(false)
const plansFailed = ref(false)
const packages = ref<RechargePackage[]>([])
const plans = ref<SubscriptionPlan[]>([])
const plansEnabled = ref(true)
const isFreeRealm = ref(false)
const realmNote = ref('')
const rechargeEnabled = ref(true)
const inviteEnabled = ref(true)
const inviteFailed = ref(false)
const inviteErrMsg = ref('')
const member = ref<MyMemberInfo | null>(null)
const inviteCode = ref('')
const inviteReward = ref(0)
const inviteeReward = ref(0)
const points = ref<number | null>(null)
const rechargeRatio = ref<number | null>(null)
const payMethod = ref('alipay')

const selectedPkg = ref<RechargePackage | null>(null)
const currency = ref<'y' | 'e'>('y')
const openPlanId = ref<number | null>(null)
const openCampLevel = ref<number | null>(null)
const orderBusy = ref(false)

onMounted(() => {
  void loadAll()
})

async function loadAll() {
  loading.value = true
  pkgsFailed.value = false
  plansFailed.value = false
  inviteFailed.value = false
  inviteErrMsg.value = ''
  const [pkgs, pls, mem, inv, pts, cur, methods] = await Promise.allSettled([
    paymentApi.packages(),
    paymentApi.plans(),
    memberApi.info(),
    inviteApi.myCode(),
    pointsApi.log({ limit: 1 }),
    currencyApi.info(),
    paymentApi.methods(),
  ])
  if (pkgs.status === 'fulfilled') {
    packages.value = pkgs.value.packages || []
    rechargeEnabled.value = pkgs.value.enabled !== false
    // 默认选中 is_popular 的那一罐，没有就选中间那罐
    selectedPkg.value =
      pkgs.value.packages.find((p) => p.is_popular) ||
      pkgs.value.packages[Math.floor(pkgs.value.packages.length / 2)] ||
      pkgs.value.packages[0] ||
      null
  } else {
    pkgsFailed.value = true
  }
  if (pls.status === 'fulfilled') {
    plans.value = pls.value.plans || []
    plansEnabled.value = pls.value.enabled !== false
    // 公益服（免费服）：后端下发 is_free / access_mode，套餐不卖、只展示免费开放
    isFreeRealm.value = pls.value.is_free === true || pls.value.access_mode === 'free'
    realmNote.value = pls.value.access_note || ''
  } else {
    plansFailed.value = true
  }
  if (mem.status === 'fulfilled') member.value = mem.value
  if (inv.status === 'fulfilled') {
    inviteCode.value = inv.value.code || ''
    inviteEnabled.value = inv.value.config?.enabled !== false
    inviteReward.value = inv.value.config?.reward_points ?? 0
    inviteeReward.value = inv.value.config?.invitee_reward_points ?? 0
  } else {
    // 邀请接口挂了：不再渲染"----"/0 能量的半成品，走错误态 + 重试
    inviteFailed.value = true
    inviteErrMsg.value = humanizeError(inv.reason)
  }
  if (pts.status === 'fulfilled') points.value = pts.value.balance ?? null
  if (cur.status === 'fulfilled') rechargeRatio.value = cur.value.recharge_ratio ?? null
  if (methods.status === 'fulfilled' && methods.value.length > 0) {
    payMethod.value = methods.value[0].id
  }
  loading.value = false
}

/* ---------- 展示 ---------- */
function fmtNum(n: number | null | undefined): string {
  if (n == null) return '–'
  return n.toLocaleString()
}
const pointsText = computed(() => (points.value == null ? '–' : fmtNum(points.value)))
const memberLevelText = computed(() => (member.value ? `Lv.${member.value.level}` : '–'))
const memberLevelName = computed(() => member.value?.level_name || '–')
const memberDiscountPct = computed(() => member.value?.discount_pct ?? 0)
const ratioText = computed(() =>
  rechargeRatio.value == null ? '充值比例加载中' : `1 元 = ${rechargeRatio.value} 能量`,
)

/* 能量条：按最大一罐做满格（0~1 进度，供 scaleX 动画） */
const meterScale = computed(() => {
  if (!selectedPkg.value || packages.value.length === 0) return 0
  const max = Math.max(...packages.value.map((p) => p.total_points))
  return Math.min(1, selectedPkg.value.total_points / max)
})
const meterText = computed(() => {
  const p = selectedPkg.value
  if (!p) return '选一罐能量'
  return `已选 ${fmtNum(p.total_points)} 能量 · ¥ ${fmtNum(p.price)}`
})

/* 执照按天数排序：月→季→年 */
const sortedPlans = computed(() =>
  [...plans.value].sort((a, b) => a.duration_days - b.duration_days),
)

/* 火漆字：从套餐名取前两个字竖排，无名则按天数推断 */
function waxText(plan: SubscriptionPlan): string {
  const name = (plan.name || '').replace(/[\s·]/g, '')
  if (name.length >= 2) return name.slice(0, 2).split('').join('\n')
  if (plan.duration_days >= 300) return '年\n照'
  if (plan.duration_days >= 80) return '季\n照'
  return '月\n照'
}
/* 执照名：去掉"订阅/会员"等现实词，保留游戏味 */
function licenseName(plan: SubscriptionPlan): string {
  return plan.name || `${plan.duration_days} 天通行证`
}
function planPriceText(plan: SubscriptionPlan): string {
  if (currency.value === 'e') {
    return plan.points_price != null ? `${fmtNum(plan.points_price)} ⚡` : '不支持能量'
  }
  return `¥ ${fmtNum(plan.price)}`
}

function togglePlan(id: number) {
  openPlanId.value = openPlanId.value === id ? null : id
}

/* 登山路线：等级从低到高（Lv.1 → Lv.6 往上爬） */
const trailLevels = computed(() => {
  const ls = member.value?.levels || []
  return [...ls].sort((a, b) => a.level - b.level)
})
function campClass(lv: { level: number }): string {
  if (!member.value) return 'locked'
  if (lv.level < member.value.level) return 'done'
  if (lv.level === member.value.level) return 'now'
  return 'locked'
}
function campFlag(lv: { level: number }): string {
  const c = campClass(lv)
  if (c === 'done') return '✓'
  if (c === 'now') return '●'
  return String(lv.level)
}
function toggleCamp(level: number) {
  openCampLevel.value = openCampLevel.value === level ? null : level
}

/* ---------- 下单 ---------- */
async function doRecharge() {
  const pkg = selectedPkg.value
  if (!pkg || orderBusy.value) return
  orderBusy.value = true
  try {
    const res = await paymentApi.createOrder({
      kind: 'recharge',
      item_id: pkg.id,
      payment_method: payMethod.value,
    })
    if (res.pay_url) {
      toast.success('正在跳转支付…')
      window.location.href = res.pay_url
    } else {
      toast.error('支付链接获取失败，请稍后再试')
    }
  } catch (err: unknown) {
    toast.error(humanizeError(err))
  } finally {
    orderBusy.value = false
  }
}

async function doBuyPlan(plan: SubscriptionPlan) {
  if (orderBusy.value) return
  if (currency.value === 'e' && plan.points_price == null) {
    toast.error('该执照不支持能量抵扣，请切换人民币')
    return
  }
  orderBusy.value = true
  try {
    const res = await paymentApi.createOrder({
      kind: 'subscription',
      item_id: plan.id,
      payment_method: payMethod.value,
      pay_with_points: currency.value === 'e',
    })
    if (res.paid_with_points) {
      toast.success('执照领取成功，出发吧！')
      void loadAll()
    } else if (res.pay_url) {
      toast.success('正在跳转支付…')
      window.location.href = res.pay_url
    } else {
      toast.error('支付链接获取失败，请稍后再试')
    }
  } catch (err: unknown) {
    toast.error(humanizeError(err))
  } finally {
    orderBusy.value = false
  }
}

/* ---------- 邀请码 ---------- */
async function copyInviteCode() {
  const code = inviteCode.value
  if (!code) {
    toast.error('邀请码加载中，稍后再试')
    return
  }
  try {
    await navigator.clipboard.writeText(code)
    toast.success('暗号已复制：' + code)
  } catch {
    toast.info('暗号：' + code + '（请手动复制）')
  }
}
</script>

<style scoped>
/* 补给站样式：严格抄自设计稿 shop.html（纸色 token 不许改） */
.shop-head {
  padding: 26px 20px 14px;
  border-bottom: 2px solid var(--ink);
}
.shop-stamp {
  display: inline-block;
  border: 2px solid var(--red);
  color: var(--red);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 2px;
  padding: 4px 10px;
  transform: rotate(-4deg);
  border-radius: 3px;
  margin-bottom: 10px;
}
.shop-head h1 {
  font-size: 24px;
  font-weight: 800;
  letter-spacing: 1px;
}
.shop-head p {
  color: var(--ink2);
  font-size: 13px;
  margin-top: 6px;
}
.purse {
  display: flex;
  gap: 10px;
  margin-top: 12px;
}
.purse .coin {
  flex: 1;
  background: var(--ink);
  color: var(--paper); /* R27：原 #f6f1e7 = --paper */
  border-radius: 12px;
  padding: 10px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.purse .coin .n {
  font-size: 18px;
  font-weight: 800;
}
.purse .coin .t {
  font-size: 11px;
  opacity: 0.7;
}
.purse .coin .ic {
  font-size: 22px;
}
.shop-sec {
  padding: 20px 20px 0;
}
.shelf {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.can {
  background: var(--paper-card);
  border: 1.5px dashed var(--ink2);
  border-radius: 12px;
  padding: 16px 10px;
  text-align: center;
  cursor: pointer;
  transition: transform 0.15s, border-color 0.2s;
  position: relative;
}
.can:active {
  transform: scale(0.94);
}
.can.sel {
  border-style: solid;
  border-color: var(--red);
  box-shadow: 0 0 0 2px rgba(217, 72, 43, 0.15);
}
.can .bolt {
  font-size: 30px;
  display: block;
  margin-bottom: 6px;
  filter: grayscale(1);
  transition: filter 0.2s;
}
.can.sel .bolt {
  filter: none;
}
.can .amt {
  font-size: 17px;
  font-weight: 800;
}
.can .amt small {
  font-size: 11px;
  color: var(--ink2);
  font-weight: 400;
}
.can .price {
  font-size: 12px;
  color: var(--ink2);
  margin-top: 4px;
}
.can .tag {
  position: absolute;
  top: -8px;
  right: 8px;
  background: var(--red);
  color: var(--on-dark);
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 10px;
  transform: rotate(3deg);
}
.can .per {
  font-size: 10px;
  color: var(--green);
  margin-top: 4px;
  font-weight: 700;
}
.meter {
  margin: 14px 0 4px;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: 10px;
  height: 34px;
  position: relative;
  overflow: hidden;
}
.meter .fill {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 100%;
  transform: scaleX(var(--meter-p, 0));
  transform-origin: left center;
  background: linear-gradient(90deg, var(--ember), var(--red));
  transition: transform 0.6s cubic-bezier(0.2, 0.9, 0.3, 1.2);
}
.meter .txt {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  z-index: 1;
}
.charge {
  display: block;
  width: 100%;
  margin-top: 10px;
  background: var(--red);
  color: var(--on-dark);
  border: none;
  font-size: 15px;
  font-weight: 800;
  padding: 14px;
  border-radius: 12px;
  letter-spacing: 2px;
  box-shadow: 0 3px 0 var(--red-deep);
  transition: transform 0.12s;
}
.charge:active {
  transform: scale(0.97);
}
.charge:disabled {
  opacity: 0.6;
}
.fineprint {
  font-size: 11px;
  color: var(--ink2);
  text-align: center;
  margin-top: 8px;
}
.cur-toggle {
  display: flex;
  background: var(--paper2);
  border: 1.5px solid var(--line);
  border-radius: 20px;
  padding: 3px;
  margin-bottom: 12px;
}
.cur-toggle button {
  flex: 1;
  border: none;
  background: none;
  font-size: 13px;
  font-weight: 700;
  padding: 8px;
  min-height: 44px;
  border-radius: 16px;
  color: var(--ink2);
  cursor: pointer;
}
.cur-toggle button.on {
  background: var(--ink);
  color: var(--paper); /* R27：原 #f6f1e7 = --paper */
}
.license {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 12px;
  margin-bottom: 12px;
  overflow: hidden;
  cursor: pointer;
  transition: transform 0.15s;
  position: relative;
}
.license:active {
  transform: scale(0.98);
}
.license .face {
  padding: 16px;
  display: flex;
  gap: 14px;
  align-items: center;
}
.license .wax {
  flex: none;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: var(--red);
  color: var(--on-dark);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 800;
  box-shadow: 0 3px 0 var(--red-deep);
  transform: rotate(-6deg);
  font-family: Georgia, serif;
  line-height: 1.2;
  text-align: center;
  white-space: pre-line;
}
.license.hot .wax {
  background: var(--gold);
  box-shadow: 0 3px 0 var(--gold-deep);
}
.license .info {
  flex: 1;
}
.license .info .nm {
  font-size: 15px;
  font-weight: 800;
}
.license .info .ds {
  font-size: 11px;
  color: var(--ink2);
  margin-top: 3px;
}
.license .pr {
  text-align: right;
}
.license .pr .p {
  font-size: 19px;
  font-weight: 800;
  color: var(--red);
}
.license .ribbon span {
  position: absolute;
  top: 10px;
  right: -2px;
  background: var(--ink);
  color: var(--paper); /* R27：原 #f6f1e7 = --paper */
  font-size: 10px;
  padding: 3px 10px;
  border-radius: 2px;
  transform: rotate(2deg);
}
.license .detail {
  display: none;
  border-top: 1px dashed var(--line);
  padding: 14px 16px;
  font-size: 12px;
  line-height: 2;
  background: var(--paper);
}
.license.open .detail {
  display: block;
  animation: shop-fadein 0.3s;
}
@keyframes shop-fadein {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
}
.license .detail .kv {
  display: flex;
  justify-content: space-between;
}
.license .detail .kv b {
  color: var(--red);
}
.buy {
  display: block;
  width: 100%;
  min-height: 44px;
  margin: 10px 0 2px;
  background: var(--ink);
  color: var(--paper); /* R27：原 #f6f1e7 = --paper */
  border: none;
  font-size: 14px;
  font-weight: 800;
  padding: 12px;
  border-radius: 10px;
  letter-spacing: 1px;
}
.buy:active {
  transform: scale(0.98);
}
.buy:disabled {
  opacity: 0.6;
}
.trail {
  position: relative;
  margin: 6px 0 0;
  padding-left: 26px;
}
.trail::before {
  content: '';
  position: absolute;
  left: 9px;
  top: 8px;
  bottom: 8px;
  width: 2px;
  background: repeating-linear-gradient(180deg, var(--line) 0 6px, transparent 6px 12px);
}
.camp {
  position: relative;
  padding: 10px 0;
  cursor: pointer;
}
.camp .flag {
  position: absolute;
  left: -26px;
  top: 12px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--paper2);
  border: 2px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
}
.camp.done .flag {
  background: var(--gold);
  border-color: var(--gold);
  color: var(--on-dark);
}
.camp.now .flag {
  background: var(--red);
  border-color: var(--red);
  color: var(--on-dark);
  box-shadow: 0 0 0 4px rgba(217, 72, 43, 0.2);
}
.camp .card {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.camp.now .card {
  border-color: var(--red);
  border-style: solid;
}
.camp.locked {
  opacity: 0.55;
}
.camp .lv {
  font-weight: 800;
  font-size: 14px;
}
.camp .lv small {
  font-weight: 400;
  color: var(--ink2);
  font-size: 11px;
  margin-left: 6px;
}
.camp .off {
  font-size: 11px;
  color: var(--green);
  font-weight: 700;
}
.camp .xp {
  font-size: 11px;
  color: var(--ink2);
}
.camp .perks {
  font-size: 12px;
  color: var(--ink2);
  padding: 8px 12px 2px;
  line-height: 1.9;
}
.camp .perks i {
  font-style: normal;
  color: var(--gold);
}
.invite {
  background: var(--ink);
  color: var(--paper2); /* R27：原 #ede6d6，近似 --paper2 #EFE7D6，收敛 */
  border-radius: 14px;
  padding: 18px;
  position: relative;
  overflow: hidden;
}
.invite h3 {
  font-size: 15px;
  font-weight: 800;
  letter-spacing: 1px;
}
.invite p {
  font-size: 12px;
  color: var(--line-deep);
  margin-top: 6px;
  line-height: 1.7;
}
.invite .code {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.invite .code .c {
  flex: 1;
  background: var(--scrim);
  border: 1px dashed var(--line-deep);
  border-radius: 8px;
  text-align: center;
  font-size: 18px;
  font-weight: 800;
  letter-spacing: 4px;
  padding: 10px;
  color: var(--ember);
}
/* R27：生成中态，字小一点、颜色淡一点 */
.invite .code .c.generating {
  font-size: 12px;
  letter-spacing: 1px;
  color: var(--line-deep);
  display: flex;
  align-items: center;
  justify-content: center;
}
.invite .code button {
  background: var(--red);
  color: var(--on-dark);
  border: none;
  border-radius: 8px;
  padding: 0 18px;
  min-height: 44px;
  font-size: 13px;
  font-weight: 700;
}

/* 键盘可达性：role=button 的卡片获得焦点时给出可见轮廓 */
.can:focus-visible,
.license:focus-visible,
.camp:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* 尊重系统"减少动态效果"：关掉补给站的过渡与装饰动画 */
@media (prefers-reduced-motion: reduce) {
  .can,
  .can .bolt,
  .license,
  .meter .fill,
  .charge,
  .buy {
    transition: none;
  }
  .license.open .detail {
    animation: none;
  }
  .shop-stamp,
  .license .wax,
  .license .ribbon span {
    transform: none;
  }
}
</style>
