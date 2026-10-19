<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <div class="shop-head">
        <span class="shop-stamp">补给站 · 今日开张</span>
        <h1><span aria-hidden="true">🎒</span> 探险补给站</h1>
        <p>出发前，先备好干粮 —— 能量是路上的盘缠，会员是手里的通行证。</p>
        <div v-if="loading" class="v4-skeleton" style="height:64px;margin-top:12px"></div>
        <div v-else class="purse">
          <div class="coin">
            <span class="ic" aria-hidden="true">⚡</span>
            <div><div class="n">{{ pointsText }}</div><div class="t">能量</div></div>
            <!-- R85：能量余额加载失败时给重试入口，不再永久挂「–」 -->
            <button v-if="pointsFailed && !loading" class="v4-retry" style="margin-left:auto;font-size:12px" @click="loadAll">重试</button>
          </div>
          <div class="coin">
            <span class="ic" aria-hidden="true">📜</span>
            <div><div class="n">{{ memberLevelText }}</div><div class="t">{{ memberLevelName }} · 探险家</div></div>
          </div>
        </div>
      </div>

      <!-- ⚡ 能量补给 -->
      <section class="shop-sec">
        <div class="v4-sec-title"><span aria-hidden="true">⚡</span> 能量补给</div>
        <!-- R37 P3-1：比例接口失败时副标题诚实兜底，不再假装「加载中」 -->
        <div class="v4-sec-sub">
          <template v-if="ratioFailed">充能量 = 补充能量 —— 点一罐看看</template>
          <template v-else>充能量 = 补充能量 · {{ ratioText }} —— 点一罐看看</template>
        </div>
        <div v-if="loading" class="v4-skeleton" style="height:220px"></div>
        <div v-else-if="pkgsFailed" class="v4-err">能量罐加载失败，稍后再试<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else-if="!rechargeEnabled" class="v4-empty">能量充值暂未开启，稍后再来看看<br><button class="v4-retry" @click="loadAll">刷新看看</button></div>
        <div v-else-if="packages.length === 0" class="v4-empty">能量罐暂未上架，稍后再来看看<br><button class="v4-retry" @click="loadAll">刷新看看</button></div>
        <template v-else>
          <!-- R82A-1：支付方式未就绪不再整体替换空态，改为货架上方一条瘦横幅提示；浏览照常，充能类按钮靠已有 !methodsReady 禁用 -->
          <!-- 口径：错误态给"重试"，配置态给"刷新看看"（页面被 KeepAlive 缓存，配置态也需要站内刷新入口） -->
          <div v-if="!methodsReady" class="methods-note" role="status"><template v-if="methodsFailed">支付方式加载失败，请检查网络后重试<button class="v4-retry" @click="loadAll">重试</button></template><template v-else>暂无可用支付方式，请联系服主<button class="v4-retry" @click="loadAll">刷新看看</button></template></div>
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
              <span class="bolt" aria-hidden="true">{{ pkg.icon || (pkg.total_points >= 60 ? '🛢️' : '🔋') }}</span>
              <div class="amt">{{ fmtNum(pkg.total_points) }} <small>能量</small></div>
              <div class="price">¥ {{ fmtNum(pkg.price) }}</div>
              <div v-if="pkg.bonus > 0" class="per">多送 {{ fmtNum(pkg.bonus) }}</div>
            </div>
          </div>
          <div class="meter" role="progressbar" :aria-valuenow="Math.round(meterScale * 100)" aria-valuemin="0" aria-valuemax="100" :aria-label="meterText">
            <div class="fill" :style="{ '--meter-p': meterScale }"></div>
            <div class="txt">{{ meterText }}</div>
          </div>
          <div v-if="couponApplied && quoteOf('recharge', selectedPkg?.id || 0)" class="coupon-strip">
            <span aria-hidden="true">🎫</span> 券后 ¥ {{ fmtNum(quoteOf('recharge', selectedPkg?.id || 0)!.paid_amount) }}
            <s>¥ {{ fmtNum(quoteOf('recharge', selectedPkg?.id || 0)!.list_price) }}</s>
          </div>
          <button class="charge" :class="{ 'is-busy': orderBusy }" :disabled="orderBusy || !methodsReady" :aria-busy="orderBusy && orderBusyTag === 'recharge'" @click="doRecharge"><span v-if="!(orderBusy && orderBusyTag === 'recharge')" aria-hidden="true">⚡ </span>{{ orderBusy && orderBusyTag === 'recharge' ? '支付中…' : '充能' }}</button>
          <!-- 自定义金额 -->
          <div class="custom">
            <div class="custom-row">
              <span class="cur">¥</span>
              <input
                v-model.number="customAmount"
                type="number"
                min="1"
                max="100000"
                placeholder="任意金额"
                class="custom-input"
              />
              <!-- R85：支付中给忙时文案 + aria-busy，与充能/领取按钮口径一致（此前 orderBusyTag='custom' 无任何读取方） -->
              <button class="custom-btn" :disabled="!canCustomRecharge || orderBusy || !methodsReady" :aria-busy="orderBusy && orderBusyTag === 'custom'" @click="handleCustomRecharge">
                <span class="lbl">{{ orderBusy && orderBusyTag === 'custom' ? '支付中…' : '按这个金额充' }}</span>
              </button>
            </div>
            <div v-if="quickAmounts.length" class="quick">
              <button
                v-for="q in quickAmounts"
                :key="q"
                class="quick-btn"
                @click="customAmount = q"
              ><span class="lbl">¥{{ q }}</span></button>
            </div>
            <!-- R37 P3-1：比例未知时不静默错算，标注「比例待确认」 -->
            <!-- R85：小数手输时按钮静默禁用无提示，先给人话；与 canCustomRecharge 的整数门一致 -->
            <small v-if="customAmount && !Number.isInteger(customAmount)" class="custom-note" style="color:var(--red-ink)">金额需为整数</small>
            <small v-else-if="customAmount != null && customAmount > 100000" class="custom-note" style="color:var(--red-ink)">最多充 100000，减一点再下单</small>
            <small v-else-if="customAmount && customAmount >= 1" class="custom-note">
              <template v-if="rechargeRatio == null">≈ 比例待确认</template>
              <template v-else>≈ {{ fmtNum(customPointsPreview) }} 能量</template>
            </small>
          </div>
          <!-- 支付方式 -->
          <div v-if="methods.length > 1" class="payways">
            <span class="payways-label">支付方式</span>
            <button
              v-for="m in methods"
              :key="m.id"
              class="payway"
              :class="{ on: payMethod === m.id }"
              :aria-pressed="payMethod === m.id"
              @click="payMethod = m.id"
            ><!-- R85：name 为空时不再把内部 id 暴露给用户 --><span class="lbl">{{ m.name || '在线支付' }}</span></button>
          </div>
          <!-- R28：只有一种支付方式时展示只读标签，不隐藏 -->
          <div v-else-if="methods.length === 1" class="payways">
            <span class="payways-label">支付方式</span>
            <span class="payway on" aria-current="true"><!-- R85：同上，内部 id 不暴露 --><span class="lbl">{{ methods[0].name || '在线支付' }}</span></span>
          </div>
          <div class="fineprint">能量可用于续命体力、兑换会员、发红包 —— 不过期</div>
        </template>
      </section>

      <!-- 📜 探险执照 -->
      <section class="shop-sec">
        <div class="v4-sec-title"><span aria-hidden="true">📜</span> 探险执照</div>
        <div class="v4-sec-sub">买会员 = 领取执照 —— 凭执照解锁全片库，点开看细则</div>
        <div v-if="loading" class="v4-skeleton" style="height:280px"></div>
        <div v-else-if="plansFailed" class="v4-err">执照加载失败，稍后再试<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else-if="isFreeRealm" class="v4-empty">当前为公益服，全片库免费开放，无需领取执照 <span aria-hidden="true">🎉</span><br v-if="realmNote" /><span v-if="realmNote" style="font-size:11px">{{ realmNote }}</span></div>
        <div v-else-if="!plansEnabled" class="v4-empty">执照领取暂未开启，稍后再来看看<br><button class="v4-retry" @click="loadAll">刷新看看</button></div>
        <div v-else-if="plans.length === 0" class="v4-empty">执照暂未上架，稍后再来看看<br><button class="v4-retry" @click="loadAll">刷新看看</button></div>
        <template v-else>
          <div class="cur-toggle">
            <button :class="{ on: currency === 'y' }" :aria-pressed="currency === 'y'" @click="currency = 'y'">¥ 人民币</button>
            <button :class="{ on: currency === 'e' }" :aria-pressed="currency === 'e'" @click="currency = 'e'"><span aria-hidden="true">⚡</span> 能量抵扣</button>
          </div>
          <div
            v-for="plan in sortedPlans"
            :key="plan.id"
            class="license"
            :class="{ hot: plan.is_popular, open: openPlanId === plan.id }"
          >
            <div class="ribbon" v-if="plan.is_popular"><span>最多人拿</span></div>
            <div
              class="face"
              role="button"
              tabindex="0"
              :aria-expanded="openPlanId === plan.id"
              :aria-controls="`plan-detail-${plan.id}`"
              @click="togglePlan(plan.id)"
              @keydown.enter="togglePlan(plan.id)"
              @keydown.space.prevent="togglePlan(plan.id)"
            >
              <div class="wax">{{ waxText(plan) }}</div>
              <div class="info">
                <div class="nm">{{ licenseName(plan) }}</div>
                <div class="ds">{{ plan.description || (plan.duration_days != null ? `${plan.duration_days} 天全片库通行` : '时长以购买页为准') }}</div>
              </div>
              <div class="pr">
                <div class="p">{{ planPriceText(plan) }}</div>
                <div v-if="couponApplied && currency === 'y' && quoteOf('subscription', plan.id)" class="coupon-strip sm">
                  <span aria-hidden="true">🎫</span> 券后 {{ couponPlanPriceText(plan, quoteOf('subscription', plan.id)!) }}
                </div>
              </div>
            </div>
            <div class="detail" :id="`plan-detail-${plan.id}`">
              <div class="kv"><span>有效期</span><b>{{ plan.duration_days != null ? `${plan.duration_days} 天` : '以购买页为准' }}</b></div>
              <div class="kv" v-for="(f, i) in plan.features || []" :key="i"><span>{{ f }}</span><span></span></div>
              <div v-if="memberDiscountPct > 0" class="kv"><span>{{ memberLevelName }}折扣</span><b>已享 {{ (100 - memberDiscountPct) / 10 }} 折</b></div>
              <div class="kv"><span>到期后</span><span>自动失效；可在「我的会员」开启能量自动续订</span></div>
              <button class="buy" :class="{ 'is-busy': orderBusy }" :disabled="orderBusy || !methodsReady" :aria-busy="orderBusy && orderBusyTag === 'buy'" @click.stop="doBuyPlan(plan)" @keydown.enter.stop @keydown.space.stop>{{ orderBusy && orderBusyTag === 'buy' ? '领取中…' : '领取执照 →' }}</button>
              <!-- R85：支付通道没准备好时按钮静默禁用，给一行人话说明（口径与能量区 methods-note 对齐） -->
              <div v-if="!methodsReady" class="methods-note" role="status" style="margin:8px 0 0"><template v-if="methodsFailed">支付方式加载失败，通道恢复后可领<button class="v4-retry" @click="loadAll">重试</button></template><template v-else>支付通道准备中，稍后就能领<button class="v4-retry" @click="loadAll">刷新看看</button></template></div>
            </div>
          </div>
          <div class="fineprint">人民币 / 能量二选一，价格以页面显示为准</div>
        </template>
      </section>

      <!-- ⛰️ 探险家等级 -->
      <section class="shop-sec">
        <div class="v4-sec-title"><span aria-hidden="true">⛰️</span> 探险家等级</div>
        <div class="v4-sec-sub">Lv.1 → Lv.6 登山路线 —— 等级越高，执照越便宜，点开看特权</div>
        <div v-if="loading" class="v4-skeleton" style="height:260px"></div>
        <div v-else-if="memberFailed" class="v4-err">等级数据加载失败<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else-if="!member" class="v4-empty">暂无等级记录，默认从 Lv.1 开始</div>
        <!-- 「暂未开启/暂未上架」是配置态（非故障），不挂"重试"按钮（重试只给加载失败类错误态）；
             但页面被 KeepAlive 缓存、无其他站内刷新入口，所以配置态给一个叫"刷新看看"的按钮，与错误态区分 -->
        <div v-else-if="!trailLevels.length" class="v4-empty">等级路线暂未开启，稍后再来看看<br><button class="v4-retry" @click="loadAll">刷新看看</button></div>
        <div v-else class="trail">
          <div
            v-for="lv in trailLevels"
            :key="lv.level"
            class="camp"
            :class="campClass(lv)"
            role="button"
            tabindex="0"
            :aria-expanded="openCampLevel === lv.level"
            :aria-controls="`camp-perks-${lv.level}`"
            :id="`camp-${lv.level}`"
            @click="toggleCamp(lv.level)"
            @keydown.enter="toggleCamp(lv.level)"
            @keydown.space.prevent="toggleCamp(lv.level)"
          >
            <span class="flag" aria-hidden="true">{{ campFlag(lv) }}</span>
            <div class="card">
              <div class="lv">Lv.{{ lv.level }} <small>{{ lv.name }}</small></div>
              <div v-if="lv.discount_pct > 0" class="off">执照 {{ (100 - lv.discount_pct) / 10 }} 折</div>
              <div v-else class="xp">经验 {{ lv.xp_threshold }}</div>
            </div>
            <!-- R85：展开面板移出点击冒泡区（点文本不再误收起），并与 camp 的 aria-controls 配对 -->
            <div class="perks" v-if="openCampLevel === lv.level" :id="`camp-perks-${lv.level}`" @click.stop>
              <template v-for="(b, i) in lv.benefits" :key="i"><i aria-hidden="true">★</i> {{ b }}<br /></template>
              <span style="font-size:12px">经验 {{ lv.xp_threshold }}<span v-if="lv.level === member.level && member.next_level && member.xp_to_next != null"> → 下一级 Lv.{{ member.next_level }} 还差 {{ member.xp_to_next }}</span></span>
              <div v-if="lv.level === member.level && member.progress_pct != null" style="background:var(--paper2);border-radius:6px;height:8px;margin-top:6px;overflow:hidden">
                <div :style="{ width: member.progress_pct + '%', height: '100%', background: 'var(--red)' }"></div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 🤝 带上同伴 -->
      <section v-if="loading || inviteEnabled || inviteFailed" class="shop-sec" style="padding-bottom:24px">
        <div class="v4-sec-title"><span aria-hidden="true">🤝</span> 带上同伴</div>
        <div class="v4-sec-sub">邀请一位探险家，双方都得能量</div>
        <div v-if="loading" class="v4-skeleton" style="height:120px"></div>
        <div v-else-if="inviteFailed" class="v4-err">组队暗号加载失败{{ inviteErrMsg ? '：' + inviteErrMsg : '' }}<br><button class="v4-retry" @click="loadAll">重试</button></div>
        <div v-else-if="inviteEnabled" class="invite">
          <h3><span aria-hidden="true">🧭</span> 结伴远行，都有能量拿</h3>
          <p v-if="inviteReward > 0 || inviteeReward > 0">同伴用你的组队暗号注册并领取执照，你得 {{ inviteReward }} 能量，他得 {{ inviteeReward }} 能量 —— 上不封顶。</p>
          <!-- R30：奖励为 0 时"上不封顶"自相矛盾，改诚实文案 -->
          <p v-else>当前邀请奖励未开启。</p>
          <div class="code">
            <!-- R27：code 为空不再显示占位 '----'，给生成中态 + 重试 -->
            <div v-if="inviteCode" class="c">{{ inviteCode }}</div>
            <div v-else class="c generating">组队暗号生成中</div>
            <button @click="copyInviteCode" :disabled="!inviteCode">复制</button>
          </div>
          <button v-if="!inviteCode" class="v4-retry" style="margin-top:10px" @click="loadAll">重试</button>
        </div>
      </section>

      <!-- 🎟️ 核销中心 -->
      <section id="shop-redeem" class="shop-sec" style="padding-bottom:24px">
        <div class="v4-sec-title"><span aria-hidden="true">🎟️</span> 核销中心</div>
        <div class="v4-sec-sub">会员卡码 / 兑换码 / 优惠券 —— 自动识别，输完点"使用"</div>
        <div v-if="loading" class="v4-skeleton" style="height:140px"></div>
        <div v-else class="redeem">
          <form class="redeem-form" @submit.prevent="handleRedeem">
            <input
              v-model="redeemCode"
              placeholder="输入会员卡码 / 兑换码 / 优惠券"
              aria-label="会员卡码 / 兑换码 / 优惠券"
              maxlength="64"
              autocomplete="off"
              :readonly="!!(codePreview || exchangePreview)"
              class="redeem-input"
              @input="resetCodeFeedback"
            />
            <button type="submit" class="redeem-btn" :class="{ 'is-loading': redeemLoading || couponApplying }" :disabled="redeemLoading || couponApplying || !redeemCode.trim()" :aria-busy="redeemLoading || couponApplying">
              <!-- R85：验券（applyCoupon）期间 redeemLoading 同为 true，原顺序下「验券中…」永不可达——先判验券 -->
              <span class="lbl">{{ couponApplying ? '验券中…' : redeemLoading ? '核销中…' : '使用' }}</span>
            </button>
          </form>
          <div v-if="codePreview" class="redeem-preview">
            <span><span aria-hidden="true">✅</span> {{ codePreview.type_name }} · <b>{{ codePreview.days_text }}</b></span>
            <span v-if="codePreview.realm_name"> · 开「{{ codePreview.realm_name }}」的会员</span>
            <span v-if="codePreview.is_named"> · 限指定账号</span>
            <button type="button" class="redeem-confirm" :disabled="redeemLoading" @click="confirmCodeRedeem">
              <span class="lbl">确认开通</span>
            </button>
            <button type="button" class="redeem-clear" aria-label="取消，重新输入" @click="resetCodeFeedback">✕</button>
          </div>
          <div v-else-if="exchangePreview" class="redeem-preview">
            <span><span aria-hidden="true">✅</span> {{ exchangePreview.type_name || '兑换码' }} · <b>{{ exchangePreview.days_text || exchangePreview.message || '确认后立即兑换' }}</b></span>
            <button type="button" class="redeem-confirm" :disabled="redeemLoading" @click="confirmExchangeRedeem">
              <span class="lbl">确认兑换</span>
            </button>
            <button type="button" class="redeem-clear" aria-label="取消，重新输入" @click="resetCodeFeedback">✕</button>
          </div>
          <div v-else-if="couponApplied" class="redeem-coupon">
            <span><span aria-hidden="true">🎫</span> 优惠券 <b>{{ couponApplied }}</b> 已应用，下单自动抵扣</span>
            <button type="button" class="redeem-clear" aria-label="移除优惠券" @click="clearCoupon">✕</button>
          </div>
          <div v-else-if="couponHint" class="redeem-coupon">
            <span><span aria-hidden="true">🎫</span> 这张券可用，点「使用」应用抵扣</span>
          </div>
          <p class="redeem-note" :class="{ warn: !!redeemNotice }">{{ redeemNotice || '会员时长 · 能量 · 会员 · 优惠券，自动识别' }}</p>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'
import {
  paymentApi,
  currencyApi,
  inviteApi,
  memberApi,
  pointsApi,
  exchangeApi,
  membershipApi,
  couponApi,
  type SubscriptionPlan,
  type RechargePackage,
  type MyMemberInfo,
  type CodePreview,
  type CouponQuote,
  type PaymentMethod,
} from '@/api/economy'

const toast = useToast()

const loading = ref(true)
const pkgsFailed = ref(false)
const plansFailed = ref(false)
const methodsFailed = ref(false)
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
const memberFailed = ref(false)
const inviteCode = ref('')
const inviteReward = ref(0)
const inviteeReward = ref(0)
const points = ref<number | null>(null)
// R85：能量余额请求失败的标记（此前失败静默吞掉，页头永久显示占位「–」且无重试入口）
const pointsFailed = ref(false)
const rechargeRatio = ref<number | null>(null)
/** R37 P3-1：充值比例接口失败标记 —— 失败时副标题走诚实兜底，不再永久「加载中」 */
const ratioFailed = ref(false)
const payMethod = ref('alipay')
/* R28：支付方式接口就绪标记——未就绪时禁用下单按钮，避免用默认值 'alipay' 下单 */
const methodsReady = ref(false)

const selectedPkg = ref<RechargePackage | null>(null)
const currency = ref<'y' | 'e'>('y')
const openPlanId = ref<number | null>(null)
const openCampLevel = ref<number | null>(null)
const orderBusy = ref(false)
/** R38 P3-3：orderBusy 被三个下单动作共用 —— 用 tag 区分当前是哪一个在忙，
 *  忙碌文案只出现在真正动作的按钮上，不串到别的按钮 */
const orderBusyTag = ref<'recharge' | 'custom' | 'buy' | null>(null)

/* ---------- 支付方式 ---------- */
const methods = ref<PaymentMethod[]>([])

/* ---------- 自定义金额充值 ---------- */
const customAmount = ref<number | null>(null)
const quickAmounts = ref<number[]>([])
const canCustomRecharge = computed(() => {
  const amt = customAmount.value ?? 0
  // R85：上限 100000 与后端 economy.py Field(ge=1, le=100000) 对齐；非整数（手输小数）也不放行
  return Number.isInteger(amt) && amt >= 1 && amt <= 100000
})
const customPointsPreview = computed(() => {
  const amt = customAmount.value ?? 0
  const ratio = rechargeRatio.value
  // R53B-12：比例未拿到时不要用 1 兜底算出一个假数字，返回 null 让 fmtNum 显示占位
  if (ratio == null) return null
  return Math.floor(amt * ratio)
})

/* ---------- 核销中心 ---------- */
const redeemCode = ref('')
const redeemLoading = ref(false)
const codePreview = ref<CodePreview | null>(null)
/* R28：兑换码也走两步确认（预览兑换内容 → 确认兑换），与会员卡码交互一致 */
const exchangePreview = ref<CodePreview | null>(null)
/* R53B-19：记录预览时的兑换码，确认时校验输入未被改动 */
const previewedCode = ref('')
const codeNotice = ref('')
const exchangeEnabled = ref(true)
const couponEnabled = ref(true)
const couponApplied = ref('')
const couponError = ref('')
const couponQuotes = ref<Record<string, CouponQuote>>({})
/* R28：静默预检只做"检测"，不自动应用改价；用户点「使用」才真正验券应用 */
const couponHint = ref('')
const redeemNotice = computed(() => couponError.value || codeNotice.value)

function resetCodeFeedback() {
  codePreview.value = null
  exchangePreview.value = null
  previewedCode.value = ''
  couponHint.value = ''
  codeNotice.value = ''
  // R85：上一张失败券的红色错误停在用户真正改输入的那一刻，不再赖着不走
  couponError.value = ''
}

// 优惠券即时验券：防抖定时器句柄与预检竞态序号
let couponPreviewTimer: ReturnType<typeof setTimeout> | null = null
let couponPreviewToken = 0
// R53B-10：应用优惠券的竞态序号与忙标记（与预检同款，防止旧响应覆盖新结果）
let couponApplyToken = 0
const couponApplying = ref(false)

async function previewCoupon(code: string) {
  if (code === couponApplied.value) return
  const token = ++couponPreviewToken
  try {
    const preview = await membershipApi.preview(code)
    if (token !== couponPreviewToken) return
    // R85：预检在途时用户可能已点「使用」触发验券——验券中/验券失败/已应用时，不让旧预检把「这张券可用」与失败提示同屏写出
    if (couponApplying.value || couponError.value || couponApplied.value) return
    if (preview.kind === 'coupon' && preview.valid && couponEnabled.value) {
      // R28：只提示"这张券可用"，不自动改价；点「使用」才应用
      couponHint.value = code
    }
  } catch {
    // 网络异常静默忽略
  }
}

watch(redeemCode, (code) => {
  couponPreviewToken++
  couponHint.value = ''
  if (couponPreviewTimer) {
    clearTimeout(couponPreviewTimer)
    couponPreviewTimer = null
  }
  if (!code || code.length < 4) return
  couponPreviewTimer = setTimeout(() => {
    couponPreviewTimer = null
    if (redeemLoading.value) return
    void previewCoupon(code)
  }, 800)
})

onBeforeUnmount(() => {
  if (couponPreviewTimer) {
    clearTimeout(couponPreviewTimer)
    couponPreviewTimer = null
  }
})

function couponTargets(kind: 'recharge' | 'subscription') {
  if (kind === 'recharge') {
    return packages.value.map((p) => ({ kind: 'recharge' as const, id: p.id }))
  }
  return plans.value.map((p) => ({ kind: 'subscription' as const, id: p.id }))
}

/** 应用优惠券：用户主动点「使用」时调用（R28：静默预检不再自动应用） */
async function applyCoupon(code: string) {
  // R53B-10：只允许最新一次验券写入 couponApplied/couponQuotes/couponError
  const token = ++couponApplyToken
  couponApplying.value = true
  couponError.value = ''
  try {
    const rows = await Promise.all(
      [...couponTargets('recharge'), ...couponTargets('subscription')].map(async (t) => {
        try {
          const quote = await couponApi.quote({ code, kind: t.kind, item_id: t.id })
          return { key: `${t.kind}:${t.id}`, quote } as { key: string; quote: CouponQuote }
        } catch {
          return null
        }
      }),
    )
    if (token !== couponApplyToken) return
    const ok = rows.filter((r): r is { key: string; quote: CouponQuote } => !!r && r.quote.valid)
    if (ok.length === 0) {
      // R45A-1：全部请求都抛异常 = 网络/验券服务挂了，说"验券失败"；否则才是券本身用不上
      const allFailed = rows.length > 0 && rows.every((r) => r === null)
      // R53B-11：packages/plans 都为空（加载失败/无在售项）时没有可下单条目，不能归因到券上
      if (rows.length === 0) couponError.value = '暂时没有可用的充值/订阅项，请稍后重试'
      else if (allFailed) couponError.value = '验券失败，稍后再试'
      // R85：某类商品因加载失败/未开启没参与验券时，不能归罪于券——说清楚是商品没加载全
      else if (pkgsFailed.value || plansFailed.value || !plansEnabled.value) couponError.value = '部分商品没加载出来，这张券可能用得上，点页面上的「重试 / 刷新看看」再试'
      else couponError.value = '这张券用不上，换一张试试'
      return
    }
    const quotes: Record<string, CouponQuote> = {}
    for (const r of ok) quotes[r.key] = r.quote
    couponQuotes.value = quotes
    couponApplied.value = code
    toast.success('优惠券已应用，下单自动抵扣')
  } catch {
    if (token === couponApplyToken) couponError.value = '验券失败，稍后再试'
  } finally {
    if (token === couponApplyToken) couponApplying.value = false
  }
}

function clearCoupon() {
  couponApplied.value = ''
  couponError.value = ''
  couponQuotes.value = {}
}

const quoteOf = (kind: 'recharge' | 'subscription', id: number) =>
  couponQuotes.value[`${kind}:${id}`] || null

async function handleRedeem() {
  // R85：入口守卫——按钮 disabled 防不住隐式提交/重复触发，与 confirm* 对齐
  if (redeemLoading.value || couponApplying.value) return
  const code = redeemCode.value.trim()
  if (!code) {
    toast.error('请输入会员卡码、兑换码或优惠券')
    return
  }
  previewedCode.value = code
  redeemLoading.value = true
  codePreview.value = null
  exchangePreview.value = null
  couponHint.value = ''
  codeNotice.value = ''
  try {
    const preview = await membershipApi.preview(code)
    if (preview.kind === 'coupon') {
      if (!preview.valid) {
        codeNotice.value = preview.message || '该优惠券已停用'
        return
      }
      if (!couponEnabled.value) {
        codeNotice.value = '优惠券功能暂未开放，有券先收好'
        return
      }
      await applyCoupon(code)
      if (couponApplied.value) redeemCode.value = ''
      return
    }
    if (preview.kind === 'exchange') {
      if (!exchangeEnabled.value) {
        codeNotice.value = '兑换功能暂未开放，有码先收好'
        return
      }
      // R28：兑换码不可逆，统一走两步确认（先预览兑换内容，再点确认兑换）
      exchangePreview.value = preview
      return
    }
    if (preview.kind === 'code' && preview.valid) {
      codePreview.value = preview
      return
    }
    codeNotice.value = preview.message || '会员卡码或兑换码不存在'
  } catch (err: unknown) {
    toast.error(humanizeError(err))
  } finally {
    redeemLoading.value = false
  }
}

/** R28：兑换码第二步——用户看清预览内容后手动确认，才真正核销 */
async function confirmExchangeRedeem() {
  const code = redeemCode.value.trim()
  if (!code) return
  if (redeemLoading.value) return
  if (code !== previewedCode.value) {
    toast.error('兑换码已变更，请重新预览后再确认')
    return
  }
  redeemLoading.value = true
  try {
    const res = await exchangeApi.redeem(code)
    toast.success(res.message || '兑换成功，能量已到账')
    redeemCode.value = ''
    exchangePreview.value = null
    await loadAll(true)
  } catch (err: unknown) {
    toast.error(humanizeError(err))
  } finally {
    redeemLoading.value = false
  }
}

async function confirmCodeRedeem() {
  const code = redeemCode.value.trim()
  if (!code) return
  if (redeemLoading.value) return
  if (code !== previewedCode.value) {
    // R85：这是会员卡码的确认分支——文案不说「兑换码」，说人话
    toast.error('内容已变更，请重新输入后再确认')
    return
  }
  redeemLoading.value = true
  try {
    const res = await membershipApi.redeem(code)
    toast.success(res.message || '开通成功，会员已生效')
    redeemCode.value = ''
    codePreview.value = null
    await loadAll(true)
  } catch (err: unknown) {
    toast.error(humanizeError(err))
  } finally {
    redeemLoading.value = false
  }
}

async function handleCustomRecharge() {
  const amt = customAmount.value ?? 0
  // R85：按钮已按 canCustomRecharge 禁用，这里是纵深防御——手输超限/小数不静默丢弃，给人话提示
  if (!Number.isInteger(amt) || amt < 1 || amt > 100000 || orderBusy.value || !methodsReady.value) {
    toast.error('金额需为 1～100000 的整数')
    return
  }
  orderBusy.value = true
  orderBusyTag.value = 'custom'
  try {
    const res = await paymentApi.createOrder({
      kind: 'recharge',
      payment_method: payMethod.value,
      custom_amount: amt,
    })
    if (res.pay_url) {
      toast.success('正在跳转支付')
      window.location.href = res.pay_url
    } else {
      toast.error('支付链接获取失败，请稍后再试')
    }
  } catch (err: unknown) {
    toast.error(humanizeError(err))
  } finally {
    orderBusy.value = false
    orderBusyTag.value = null
  }
}

onMounted(() => {
  void loadAll()
})

let loadAllPromise: Promise<void> | null = null
// R85：模板十来处 @click 直接绑 loadAll，Vue 会把事件对象当第一个参数传进来——静默仅认 === true，事件对象绝不触发静默刷新，原有重试按钮照旧走骨架
function loadAll(silent: unknown = false): Promise<void> {
  if (!loadAllPromise) {
    loadAllPromise = doLoadAll(silent === true).finally(() => {
      loadAllPromise = null
    })
  }
  return loadAllPromise
}

// R85：silent=true 时不碰 loading —— 核销成功后只定向刷新受影响数据（能量/会员），页面其余部分保持可交互、不闪回骨架
async function doLoadAll(silent = false) {
  if (!silent) loading.value = true
  pkgsFailed.value = false
  ratioFailed.value = false
  pointsFailed.value = false
  plansFailed.value = false
  inviteFailed.value = false
  inviteErrMsg.value = ''
  memberFailed.value = false
  methodsFailed.value = false
  try {
  const [pkgs, pls, mem, inv, pts, cur, methodsRes, exCfg, cpCfg] = await Promise.allSettled([
    paymentApi.packages(),
    paymentApi.plans(),
    memberApi.info(),
    inviteApi.myCode(),
    pointsApi.log({ limit: 1 }),
    currencyApi.info(),
    paymentApi.methods(),
    exchangeApi.config(),
    couponApi.config(),
  ])
  if (pkgs.status === 'fulfilled') {
    packages.value = pkgs.value.packages || []
    rechargeEnabled.value = pkgs.value.enabled !== false
    // 默认选中 is_popular 的那一罐，没有就选中间那罐
    selectedPkg.value =
      packages.value.find((p) => p.is_popular) ||
      packages.value[Math.floor(packages.value.length / 2)] ||
      packages.value[0] ||
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
  else memberFailed.value = true
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
  // R85：pointsApi.log 的 balance 字段已核实存在（api/economy.ts PointsLogResponse.balance）——成功恒有值，失败才置标记
  if (pts.status === 'fulfilled') points.value = pts.value.balance ?? null
  else pointsFailed.value = true
  if (cur.status === 'fulfilled') {
    rechargeRatio.value = cur.value.recharge_ratio ?? null
    quickAmounts.value = cur.value.quick_amounts || []
  } else {
    // R37 P3-1：充值比例失败置标记，副标题诚实兜底、预估标注「比例待确认」
    ratioFailed.value = true
  }
  if (methodsRes.status === 'fulfilled') {
    methodsFailed.value = false
    methods.value = Array.isArray(methodsRes.value) ? methodsRes.value : []
    // P3-24：空列表不算就绪 —— 下单按钮的 !methodsReady 绑定会自动禁用
    methodsReady.value = methods.value.length > 0
    if (methods.value.length > 0) {
      payMethod.value = methods.value[0].id
    }
  } else {
    methodsFailed.value = true
    // R85：methods 挂了就绪态必须落下，否则模板 37 行的「支付方式加载失败」错误态永不可达、按钮仍可用旧方式下单
    methodsReady.value = false
  }
  if (exCfg.status === 'fulfilled') exchangeEnabled.value = exCfg.value.enabled !== false
  if (cpCfg.status === 'fulfilled') couponEnabled.value = cpCfg.value.enabled !== false
  // 信鸽兑换通知跳进来：自动展开核销区（R28：锚点与 PigeonView 深链统一为 #shop-redeem）
  if (typeof window !== 'undefined' && window.location.hash === '#shop-redeem') {
    const el = document.getElementById('shop-redeem')
    if (el) {
      // R28：减少动态偏好下不用平滑滚动
      const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
      setTimeout(() => el.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' }), 300)
    }
  }
  } catch {
    // R53B-13：兜底 —— 任何意外异常都落到错误态并解除骨架，避免 loading 永久为 true
    pkgsFailed.value = true
  } finally {
    loading.value = false
  }
}

/* ---------- 展示 ---------- */
function fmtNum(n: number | null | undefined): string {
  if (n == null) return '–'
  return n.toLocaleString()
}
const pointsText = computed(() => (points.value == null ? '–' : fmtNum(points.value)))
const memberLevelText = computed(() => (member.value?.level != null ? `Lv.${member.value.level}` : '–'))
const memberLevelName = computed(() => member.value?.level_name || '–')
const memberDiscountPct = computed(() => member.value?.discount_pct ?? 0)
const ratioText = computed(() =>
  // R41：rechargeRatio 为 null 时不显示"充值比例加载中"假加载态——与 custom-note 的"比例待确认"口径一致
  rechargeRatio.value == null ? '比例待确认' : `1 元 = ${rechargeRatio.value} 能量`,
)

/* 能量条：按最大一罐做满格（0~1 进度，供 scaleX 动画） */
const meterScale = computed(() => {
  if (!selectedPkg.value || packages.value.length === 0) return 0
  const max = Math.max(...packages.value.map((p) => Number(p.total_points) || 0))
  if (!Number.isFinite(max) || max <= 0) return 0
  const scale = (Number(selectedPkg.value.total_points) || 0) / max
  return Number.isFinite(scale) ? Math.min(1, Math.max(0, scale)) : 0
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
/* 执照名：后端配置名原样展示（R28：旧注释"去掉订阅/会员等现实词"与实现不符，已按实现修正，不改行为） */
function licenseName(plan: SubscriptionPlan): string {
  // R85：与模板 154 行口径对齐——duration_days 为 null 时不拼「null 天」，说人话兜底
  return plan.name || (plan.duration_days != null ? `${plan.duration_days} 天通行证` : '时长以购买页为准')
}
function planPriceText(plan: SubscriptionPlan): string {
  if (currency.value === 'e') {
    return plan.points_price != null ? `${fmtNum(plan.points_price)} ⚡` : '不支持能量'
  }
  return `¥ ${fmtNum(plan.price)}`
}
function couponPlanPriceText(plan: SubscriptionPlan, q: CouponQuote): string {
  if (currency.value === 'e') return '' // 能量抵扣不用券
  return `¥ ${fmtNum(q.paid_amount)}`
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
  if (!pkg || orderBusy.value || !methodsReady.value) return
  orderBusy.value = true
  orderBusyTag.value = 'recharge'
  try {
    // 优惠码在试算通过的商品上才带：后端会按同一套口径再算一遍并占额度
    const quote = quoteOf('recharge', pkg.id)
    const res = await paymentApi.createOrder({
      kind: 'recharge',
      item_id: pkg.id,
      payment_method: payMethod.value,
      coupon_code: quote ? quote.code : undefined,
    })
    if (res.pay_url) {
      toast.success('正在跳转支付')
      window.location.href = res.pay_url
    } else {
      toast.error('支付链接获取失败，请稍后再试')
    }
  } catch (err: unknown) {
    toast.error(humanizeError(err))
  } finally {
    orderBusy.value = false
    orderBusyTag.value = null
  }
}

async function doBuyPlan(plan: SubscriptionPlan) {
  if (orderBusy.value || !methodsReady.value) return
  if (currency.value === 'e' && plan.points_price == null) {
    toast.error('该执照不支持能量抵扣，请切换人民币')
    return
  }
  orderBusy.value = true
  orderBusyTag.value = 'buy'
  try {
    const quote = quoteOf('subscription', plan.id)
    const res = await paymentApi.createOrder({
      kind: 'subscription',
      item_id: plan.id,
      payment_method: payMethod.value,
      pay_with_points: currency.value === 'e',
      coupon_code: quote ? quote.code : undefined,
    })
    if (res.paid_with_points) {
      // R101-1：静默刷新——刚领完执照不闪全页骨架（与 confirmExchangeRedeem 的 loadAll(true) 口径对齐）；
      // 能量抵扣即时消费优惠券（后端 coupons.consume），过期的「已应用」展示一并清掉
      clearCoupon()
      toast.success('执照领取成功，出发吧！')
      void loadAll(true)
    } else if (res.pay_url) {
      toast.success('正在跳转支付')
      window.location.href = res.pay_url
    } else {
      toast.error('支付链接获取失败，请稍后再试')
    }
  } catch (err: unknown) {
    toast.error(humanizeError(err))
  } finally {
    orderBusy.value = false
    orderBusyTag.value = null
  }
}

/* ---------- 组队暗号 ---------- */
async function copyInviteCode() {
  const code = inviteCode.value
  if (!code) {
    toast.error('组队暗号加载中，稍后再试')
    return
  }
  try {
    await navigator.clipboard.writeText(code)
    toast.success('组队暗号已复制：' + code)
  } catch {
    toast.info('组队暗号：' + code + '（请手动复制）')
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
  border-radius: var(--paper-r-lg, 12px);
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
  border-radius: var(--paper-r-lg, 12px);
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
  /* R85：原 rgba(217,72,43,0.15) 硬编码，主色一换阴影即脱钩——color-mix 跟 --red 联动，老浏览器走纯色兜底（GrowthChart 同款） */
  box-shadow: 0 0 0 2px rgba(217, 72, 43, 0.15);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--red) 15%, transparent);
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
  font-size: 11px;
  padding: 2px 8px;
  border-radius: var(--paper-r-md, 10px);
  transform: rotate(3deg);
}
.can .per {
  font-size: 11px;
  color: var(--green);
  margin-top: 4px;
  font-weight: 700;
}
.meter {
  margin: 14px 0 4px;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
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
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  font-size: 12px;
  font-weight: 700;
  z-index: 1;
  /* R82C1-2：深色小胶囊垫底，进度 0%（浅纸底）/100%（红渐变）下白字均可读 */
  color: var(--on-dark, #fff);
  background: rgba(20,16,12,.55);
  border-radius: 999px;
  padding: 3px 14px;
  white-space: nowrap;
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
  border-radius: var(--paper-r-lg, 12px);
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
  font-size: 12px;
  color: var(--ink);
  text-align: center;
  margin-top: 8px;
}
.cur-toggle {
  display: flex;
  background: var(--paper2);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-2xl, 20px);
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
  border-radius: var(--paper-r-xl, 16px);
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
  border-radius: var(--paper-r-lg, 12px);
  margin-bottom: 12px;
  overflow: hidden;
  /* R101-2：整卡不再挂 cursor:pointer——真正可点的只有 .face，detail 空白区点无反应，
     手型与按压缩放都收敛到 .face，不误导 */
  position: relative;
}
.license .face {
  padding: 16px;
  display: flex;
  gap: 14px;
  align-items: center;
  cursor: pointer;
  transition: transform 0.15s;
}
.license .face:active {
  transform: scale(0.98);
}
.license .wax {
  flex: none;
  width: 56px;
  height: 56px;
  border-radius: var(--paper-r-full, 50%);
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
  right: 0;
  background: var(--ink);
  color: var(--paper); /* R27：原 #f6f1e7 = --paper */
  font-size: 11px;
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
  border-radius: var(--paper-r-md, 10px);
  letter-spacing: 1px;
  /* R85：与 .charge 对齐——:active 缩放补 transition，按压不瞬跳 */
  transition: transform 0.12s;
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
  border-radius: var(--paper-r-full, 50%);
  background: var(--paper2);
  border: 2px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
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
  /* R85：同上，20% 版 */
  box-shadow: 0 0 0 4px rgba(217, 72, 43, 0.2);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--red) 20%, transparent);
}
.camp .card {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: var(--paper-r-md, 10px);
  padding: 10px 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.camp.now .card {
  border-color: var(--red);
  border-style: solid;
}
/* R85：.camp 是透明容器，inset 色条被不透明的 .card 整片盖住——把色条移到 .card 内侧（卡片不透明，条可见） */
.camp.locked .card {
  box-shadow: inset 3px 0 0 var(--ink2);
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
  color: var(--ink);
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
  border-radius: var(--paper-r-sm, 8px);
  text-align: center;
  font-size: 18px;
  font-weight: 800;
  letter-spacing: 4px;
  /* R85：letter-spacing 会给末字尾追加 4px 导致视觉左偏——text-indent 补回 */
  text-indent: 4px;
  padding: 10px;
  color: var(--ember);
}
/* R27：生成中态，字小一点、颜色淡一点 */
.invite .code .c.generating {
  font-size: 12px;
  letter-spacing: 1px;
  /* R85：生成中态字距只有 1px，不套用上面的 4px 居中补偿 */
  text-indent: 0;
  color: var(--line-deep);
  display: flex;
  align-items: center;
  justify-content: center;
}
/* R85：13px 白字配 --red 只有 4.27:1——换 --red-deep #A33319（约 6.9:1，达小字 AA） */
.invite .code button {
  background: var(--red-deep);
  color: var(--on-dark);
  border: none;
  border-radius: var(--paper-r-sm, 8px);
  padding: 0 18px;
  min-height: 44px;
  font-size: 13px;
  font-weight: 700;
  /* R85：补按压反馈，与主按钮口径一致 */
  transition: transform 0.12s;
}
.invite .code button:active {
  transform: scale(0.97);
}

/* 键盘可达性：role=button 的卡片获得焦点时给出可见轮廓 */
.can:focus-visible,
/* R85：.license 本身无 tabindex 永远拿不到焦点，可聚焦的是内部 .face——选择器随之修正（.can/.camp 自带 tabindex，原规则有效） */
.license .face:focus-visible,
.camp:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}

/* 尊重系统"减少动态效果"：关掉补给站的过渡与装饰动画 */
@media (prefers-reduced-motion: reduce) {
  .can,
  .can .bolt,
  .license .face,
  .meter .fill,
  .charge,
  .buy,
  /* R85：新补 transition 的五个按钮同步 */
  .invite .code button,
  .custom-btn,
  .quick-btn,
  .payway,
  .redeem-confirm {
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
  /* R30：按压缩放也关掉 */
  .can:active,
  .charge:active,
  .buy:active,
  .license .face:active,
  .redeem-btn:active,
  /* R85：上面新补的五个按压反馈同步进减少动态清单 */
  .invite .code button:active,
  .custom-btn:active,
  .quick-btn:active,
  .payway:active,
  .redeem-confirm:active {
    transform: none;
  }
}

/* ---------- R82A-1：支付方式未就绪提示横幅（瘦条，浏览不受影响） ---------- */
.methods-note {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 12px;
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.5;
  border-left: 3px solid var(--red);
  border-top: 1px solid var(--line);
  border-right: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  background: var(--paper-card);
}
.methods-note .v4-retry {
  font-size: 12px;
}
/* R85：.v4-retry 自带 margin-top:8px，在 flex 行内（purse coin / methods-note）
   会整体下沉 8px 错位；行内按钮清掉上边距，触控高度 44px 不动 */
.purse .coin .v4-retry { margin-top: 0; }
.methods-note .v4-retry { margin-top: 0; }

/* ---------- 核销中心 ---------- */
.redeem {
  background: var(--paper-card);
  border: 1.5px solid var(--ink);
  border-radius: var(--paper-r-lg, 12px);
  padding: 14px;
}
.redeem-form {
  display: flex;
  gap: 8px;
}
.redeem-input {
  flex: 1;
  border: 1.5px solid var(--ink);
  border-radius: var(--paper-r-sm, 8px);
  padding: 8px 12px;
  /* R28：iOS 输入框 <16px 聚焦会自动放大页面，提到 16px，padding 收紧补偿视觉高度 */
  font-size: 16px;
  background: var(--paper);
  color: var(--ink);
  min-width: 0;
  /* R85：R28 只解决了 iOS 聚焦缩放，触控高度 38px 未达标——补 min-height:44px */
  min-height: 44px;
}
.redeem-input:focus {
  outline: 2px solid var(--red);
  outline-offset: 1px;
}
.redeem-btn {
  position: relative;
  /* R30：透明 padding + 负 margin——视觉按钮不变，点击区纵向扩到 ≥44px（R22 LeverSwitch 同款手法） */
  background: transparent;
  border: none;
  color: var(--paper);
  padding: 15px 18px;
  margin: -5px 0;
  font-size: 14px;
  font-weight: 700;
  white-space: nowrap;
}
/* 视觉按钮画在 ::before 上，随 padding 偏移（上下各 5px），与原来像素一致 */
.redeem-btn::before {
  content: "";
  position: absolute;
  top: 5px; right: 0; bottom: 5px; left: 0;
  background: var(--ink);
  border-radius: var(--paper-r-sm, 8px);
}
.redeem-btn .lbl { position: relative; }
.redeem-btn:disabled {
  opacity: 0.4;
}
.redeem-btn:active {
  transform: scale(0.97);
}
/* R28：加载态不用 CSS 旋转圈（标准3），改用纸片风呼吸脉冲 */
/* R85：模板里 is-loading/is-busy 的按钮恒带 :disabled，原 :not(:disabled) 恒不匹配是死规则——去掉，让脉冲与禁用态并存；关键帧改为从禁用透明度(0.4)起落(0.4↔0.55) */
.redeem-btn.is-loading {
  animation: redeem-pulse 1.2s ease-in-out infinite;
}
@keyframes redeem-pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 0.55; }
}
@media (prefers-reduced-motion: reduce) {
  .redeem-btn.is-loading { animation: none; opacity: 0.65; }
}
/* R30：下单忙碌态复用 R28 呼吸脉冲，不只变灰 */
.charge.is-busy,
.buy.is-busy {
  animation: redeem-pulse 1.2s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .charge.is-busy,
  .buy.is-busy { animation: none; opacity: 0.65; }
}
.redeem-preview,
.redeem-coupon {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
  padding: 10px 12px;
  /* R85：硬编码金色收敛到 --gold-tint（与 v4-paper.css 同名 token） */
  background: var(--gold-tint);
  border: 1.5px solid var(--gold);
  border-radius: var(--paper-r-sm, 8px);
  font-size: 13px;
}
.redeem-confirm {
  position: relative;
  /* R30：透明 padding + 负 margin——视觉按钮不变，点击区纵向扩到 ≥44px（R22 LeverSwitch 同款手法） */
  background: transparent;
  border: none;
  color: var(--on-dark, #fff);
  padding: 15px 14px;
  margin: -9px 0 -9px auto;
  font-size: 13px;
  font-weight: 700;
}
/* R85：同上，13px 白字红底换 --red-deep */
.redeem-confirm::before {
  content: "";
  position: absolute;
  top: 9px; right: 0; bottom: 9px; left: 0;
  background: var(--red-deep);
  border-radius: 6px;
}
.redeem-confirm .lbl { position: relative; }
/* R85：补按压反馈 */
.redeem-confirm {
  transition: transform 0.12s;
}
.redeem-confirm:active {
  transform: scale(0.97);
}
.redeem-clear {
  background: none;
  border: none;
  font-size: 14px;
  color: var(--ink2);
  /* R85：R30 只扩了纵向——横向 padding 增至 16px + min-width 44px 兜底（✕ 约 12.6px + 32px ≥ 44px；无背景，加宽无视觉副作用） */
  margin: -10px 0 -10px auto;
  padding: 14px 16px;
  min-width: 44px;
}
.redeem-note {
  font-size: 12px;
  color: var(--ink2);
  margin: 8px 2px 0;
}
/* R85：12px 红字用 --red-ink（纸色底 5.59:1），--red 只有约 4.2:1 不达小字 AA */
.redeem-note.warn {
  color: var(--red-ink);
}
/* R28：旧 .spin 旋转圈已删除（标准3：加载态用 shimmer/脉冲，不用转圈） */

/* ---------- 自定义金额 ---------- */
.custom {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--line);
}
.custom-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.custom .cur {
  font-size: 16px;
  font-weight: 700;
}
.custom-input {
  flex: 1;
  border: 1.5px solid var(--ink);
  border-radius: var(--paper-r-sm, 8px);
  padding: 6px 10px;
  /* R28：iOS 输入框 <16px 聚焦会自动放大页面，提到 16px，padding 收紧补偿视觉高度 */
  font-size: 16px;
  background: var(--paper);
  color: var(--ink);
  min-width: 0;
  /* R85：同上，34px 未达标——补 min-height:44px */
  min-height: 44px;
}
.custom-btn {
  position: relative;
  /* R30：透明 padding + 负 margin——视觉按钮不变，点击区纵向扩到 ≥44px（R22 LeverSwitch 同款手法） */
  background: transparent;
  border: none;
  padding: 15px 14px;
  margin: -5.5px 0;
  font-size: 13px;
  font-weight: 700;
  white-space: nowrap;
}
.custom-btn::before {
  content: "";
  position: absolute;
  top: 5.5px; right: 0; bottom: 5.5px; left: 0;
  background: var(--paper-card);
  border: 1.5px solid var(--ink);
  border-radius: var(--paper-r-sm, 8px);
}
.custom-btn .lbl { position: relative; }
.custom-btn:disabled {
  opacity: 0.4;
}
/* R85：补按压反馈 */
.custom-btn {
  transition: transform 0.12s;
}
.custom-btn:active {
  transform: scale(0.97);
}
.quick {
  display: flex;
  gap: 6px;
  row-gap: 22px;
  margin-top: 8px;
  flex-wrap: wrap;
}
.quick-btn {
  position: relative;
  /* R30：透明 padding + 负 margin——视觉药丸不变，点击区纵向扩到 ≥44px（R22 LeverSwitch 同款手法） */
  background: transparent;
  border: none;
  padding: 16px 12px;
  margin: -11px 0;
  font-size: 12px;
  color: var(--ink);
}
.quick-btn::before {
  content: "";
  position: absolute;
  top: 11px; right: 0; bottom: 11px; left: 0;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: 14px;
}
.quick-btn .lbl { position: relative; }
/* R85：补按压反馈 */
.quick-btn {
  transition: transform 0.12s;
}
.quick-btn:active {
  transform: scale(0.97);
}
.custom-note {
  display: block;
  margin-top: 6px;
  font-size: 12px;
  color: var(--ink2);
}

/* ---------- 支付方式 ---------- */
.payways {
  display: flex;
  align-items: center;
  gap: 8px;
  row-gap: 22px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.payways-label {
  font-size: 12px;
  color: var(--ink2);
}
.payway {
  position: relative;
  /* R30：透明 padding + 负 margin——视觉药丸不变，点击区纵向扩到 ≥44px（R22 LeverSwitch 同款手法） */
  background: transparent;
  border: none;
  padding: 15px 14px;
  margin: -8.5px 0;
  font-size: 13px;
  color: var(--ink);
}
.payway::before {
  content: "";
  position: absolute;
  top: 8.5px; right: 0; bottom: 8.5px; left: 0;
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 14px;
}
.payway .lbl { position: relative; }
/* R85：补按压反馈 */
.payway {
  transition: transform 0.12s;
}
.payway:active {
  transform: scale(0.97);
}
.payway.on {
  color: var(--paper);
  font-weight: 700;
}
.payway.on::before {
  border-color: var(--ink);
  background: var(--ink);
}

/* ---------- 优惠券折后价 ---------- */
/* R85：13px 红字同上，换 --red-ink 达标 */
.coupon-strip {
  margin-top: 8px;
  font-size: 13px;
  color: var(--red-ink);
  font-weight: 700;
}
.coupon-strip s {
  color: var(--ink2);
  font-weight: 400;
  margin-left: 4px;
}
.coupon-strip.sm {
  font-size: 12px;
  margin-top: 4px;
}
</style>
