<script setup lang="ts">
/**
 * 公益服·积分与公益配置：SystemConfig 表单
 *
 * v2.55（暗房影院统一）：PageHeader + 分组 SectionCard。
 * PR #437：签到积分三项已移至「系统设置 → 每日签到」，本页移除。
 * P0 统一货币体系：新增货币体系（充值比例）与红包规则配置组。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeftRight, Bot, Coins, Crown, Dices, Gift, KeyRound, RefreshCw, Save, Send, Settings2, Wallet } from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import { useBreakpoint } from '@/composables/useBreakpoint'
import { fetchWelfareConfig, saveWelfareConfig } from '@/api/welfare'
import { fetchViewUnlockSettings, updateViewUnlockSettings } from '@/api/admin'
import { createGiftBatch, fetchGiftBatches, previewGiftAll, type GiftAllPreview, type GiftAllScope, type GiftBatchRow } from '@/api/economy'
import { addPermanentEntry, fetchPermanentEntries, removePermanentEntry, savePermanentConfig, searchPermanentUsers, type PermanentEntry, type PermanentSearchUser } from '@/api/permanentWhitelist'

const { isPhone } = useBreakpoint()
/** 窄屏标签放到输入框上方，120px 的左侧标签会把输入框挤出屏幕 */
const labelPosition = computed(() => (isPhone.value ? 'top' : 'right'))

const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
/** 首次加载成功前不允许保存：否则会把一张空表单（全是默认值）写回服务端 */
const loaded = ref(false)
const form = ref<Record<string, string>>({})

const currencyFields = [
  { key: 'recharge_ratio', label: '充值比例', hint: '1 元人民币兑换多少积分，默认 1.2', suffix: '积分/元' },
  { key: 'recharge_quick_amounts', label: '快捷金额', hint: '自定义充值快捷金额，逗号分隔、最多 8 个，默认 10,30,50,100,200', suffix: '元' },
]

const redpacketFields = [
  { key: 'redpacket_fee_pct', label: '红包手续费', hint: '按发送金额比例收取，默认 5，填 0 不收', suffix: '%' },
  { key: 'redpacket_send_limit_7d', label: '发送频率限制', hint: '7 天内最多发送次数，默认 20，填 0 不限', suffix: '次' },
  { key: 'redpacket_recv_limit_7d', label: '领取频率限制', hint: '7 天内最多领取次数，默认 10，填 0 不限（管理员发的不计）', suffix: '次' },
  { key: 'redpacket_refund_interval_sec', label: '退款扫描间隔', hint: '每隔多少秒扫描一次过期红包，默认 300，最小 60', suffix: '秒' },
]

const transferFields = [
  { key: 'points_transfer_fee_pct', label: '手续费', hint: '按转账金额比例收取，默认 5，填 0 不收', suffix: '%' },
  { key: 'points_transfer_min', label: '单笔最小', hint: '默认 1', suffix: '积分' },
  { key: 'points_transfer_max', label: '单笔最大', hint: '默认 0 表示不限', suffix: '积分' },
  { key: 'points_transfer_daily_cap', label: '每日转出上限', hint: '默认 0 表示不限', suffix: '积分/天' },
]

const earnRedeemFields = [
  { key: 'points_chat_per_msg', label: '发言每条奖励', hint: '默认 0.1，支持两位小数（如 0.1 / 0.2），小数按条累计、满 1 结算，填 0 关闭发言奖励', suffix: '积分/条', inputmode: 'decimal' },
  { key: 'points_chat_daily_cap', label: '发言每日上限', hint: '默认 20', suffix: '积分/天', inputmode: 'numeric' },
  { key: 'points_redeem_7d', label: '兑换 7 天', hint: '默认 100', suffix: '积分', inputmode: 'numeric' },
  { key: 'points_redeem_30d', label: '兑换 30 天', hint: '默认 300', suffix: '积分', inputmode: 'numeric' },
  { key: 'welfare_redeem_min_account_age_days', label: '兑换号龄门槛', hint: '自助兑换需注册满的天数，默认 3，填 0 关闭', suffix: '天', inputmode: 'numeric' },
  { key: 'welfare_redeem_min_active_days', label: '兑换活跃门槛', hint: '自助兑换需近 7 天活跃天数（签到/播放算活跃，发言最多计 1 天），默认 2，填 0 关闭', suffix: '天', inputmode: 'numeric' },
]

// 兑换来源限定开关：word 风格 true/false（后端 economy._get_bool_config 只认 'true'）
const redeemSelfEarnedOnly = computed({
  get: () => (form.value['welfare_redeem_self_earned_only'] ?? 'true') === 'true',
  set: (v: boolean) => { form.value['welfare_redeem_self_earned_only'] = v ? 'true' : 'false' },
})

// 能量老虎机总开关：word 风格 true/false（后端 economy._get_bool_config 只认 'true'）
const slotsEnabled = computed({
  get: () => (form.value['slots_enabled'] ?? 'true') === 'true',
  set: (v: boolean) => { form.value['slots_enabled'] = v ? 'true' : 'false' },
})

// 老虎机群里可玩开关：word 风格 true/false（同总开关口径），默认开
const slotsGroupEnabled = computed({
  get: () => (form.value['slots_group_enabled'] ?? 'true') === 'true',
  set: (v: boolean) => { form.value['slots_group_enabled'] = v ? 'true' : 'false' },
})

const slotsFields = [
  { key: 'slots_bet_tiers', label: '注额档位', hint: '逗号分隔的能量数，默认 5,10,20', suffix: '能量' },
  { key: 'slots_pay_777', label: '777 倍数', hint: '777 三连赔付倍数，默认 20（另加抱走头奖池）', suffix: '倍' },
  { key: 'slots_pay_bar', label: 'BAR 三连倍数', hint: '默认 10', suffix: '倍' },
  { key: 'slots_pay_fruit', label: '水果三连倍数', hint: '葡萄/柠檬三连赔付倍数，默认 5', suffix: '倍' },
  { key: 'slots_pay_two7', label: '双 7 倍数', hint: '恰好两个 7 的赔付倍数，默认 1（返注）', suffix: '倍' },
  { key: 'slots_pool_rake_pct', label: '头奖池抽水', hint: '每注抽入头奖池的比例，默认 5', suffix: '%' },
  { key: 'slots_pool_seed', label: '头奖池种子', hint: '池首次创建与 777 抱池后的重置额，默认 100', suffix: '能量' },
  { key: 'slots_daily_bet_cap', label: '每日下注封顶', hint: '每人每日下注总额上限，默认 200，填 0 不限', suffix: '能量/天' },
  { key: 'slots_daily_win_cap', label: '每日赢分封顶', hint: '只计基础赔付（头奖池部分不计），默认 1000，填 0 不限', suffix: '能量/天' },
]

const tgBindEnabled = computed({
  get: () => (form.value['welfare_require_tg_bind'] ?? '1') === '1',
  set: (v: boolean) => { form.value['welfare_require_tg_bind'] = v ? '1' : '0' },
})

// 连签保资格总开关：word 风格 true/false（后端 economy._get_bool_config 只认 'true'）
const streakRenewEnabled = computed({
  get: () => (form.value['welfare_streak_renew_enabled'] ?? 'true') === 'true',
  set: (v: boolean) => { form.value['welfare_streak_renew_enabled'] = v ? 'true' : 'false' },
})

const botFields = [
  { key: 'bot_group_ids', label: '启用群 ID', hint: '逗号分隔的 TG 群 id，空=所有群都启用 Bot', suffix: '' },
  { key: 'bot_rate_limit_seconds', label: '命令限流', hint: '每用户每命令最小间隔，默认 3', suffix: '秒' },
  { key: 'bot_group_rate_limit', label: '群限流', hint: '每群每分钟最多处理消息数，超限静默丢弃，默认 20', suffix: '条/分' },
  { key: 'bot_channel_whitelist', label: '频道白名单', hint: '逗号分隔的频道 id，名单内频道可在群里以频道身份发言，空=都不放行', suffix: '' },
]

// 频道马甲治理总开关：word 风格 true/false（读侧 tg_bot.policy 只认 'true'）
const channelGuardEnabled = computed({
  get: () => (form.value['bot_channel_guard_enabled'] ?? 'true') === 'true',
  set: (v: boolean) => { form.value['bot_channel_guard_enabled'] = v ? 'true' : 'false' },
})

// 退群处置：开=cancel_welfare（只取消公益资格），关=none（不处置）
const leaveCancelEnabled = computed({
  get: () => (form.value['bot_leave_action'] ?? 'cancel_welfare') === 'cancel_welfare',
  set: (v: boolean) => { form.value['bot_leave_action'] = v ? 'cancel_welfare' : 'none' },
})

const welfareFields = [
  { key: 'welfare_grace_days', label: '到期保留天数', hint: '到期后可登录但不可播放，默认 7', suffix: '天' },
  { key: 'welfare_inactive_days', label: '未活跃禁用天数', hint: '默认 30', suffix: '天' },
  { key: 'welfare_streak_renew_min', label: '连签续期门槛', hint: '资格到期时，连签达到该天数自动续期、不扣分；查看凭证不自动续，默认 30', suffix: '天' },
  { key: 'welfare_streak_renew_days', label: '连签续期天数', hint: '连签自动续期一次续多少天，默认 30，范围 1-365', suffix: '天' },
  { key: 'welfare_code_monthly_quota', label: '资格码月配额', hint: '每位管理员每月可生成的公益资格码张数，默认 30，填 0 不限', suffix: '张/月' },
  { key: 'register_gift_welfare_days', label: '新注册送公益天数', hint: '新用户注册默认赠送的公益资格天数，默认 30，填 0 为永不过期', suffix: '天' },
  { key: 'register_gift_view_days', label: '新注册送查看天数', hint: '新用户注册默认赠送的查看凭证天数，默认 30，最小 1', suffix: '天' },
  // 公益求片额度（welfare_request_monthly）已于 2026-10-23 去重 #5 移到
  // 运营参数「兑换码 · 求片」卡，与每日上限、付费月限放一起
]

// 群发言积分（chat_points_*）已于 2026-10-23 去重退役：发言积分只保留
// 「获取与兑换」卡里的 points_chat_* 一套（points.py），机器人那套已删

// 默认关闭的功能开关保留在面板（2026-10-21 服主口径：默认开的开关摘面板走 env，默认关的留面板）
// r40：查看权限解锁价格（emby_view_unlock_points / emby_view_unlock_days，
// 后端 GET/PUT /api/admin/emby/view-unlock；此前有接口无后台入口）
const viewUnlock = ref({ points: 0, days: 0 })
const viewUnlockLoaded = ref(false)
const viewUnlockSaving = ref(false)

async function saveViewUnlock() {
  if (!viewUnlockLoaded.value || viewUnlockSaving.value) return
  const points = Math.max(0, Math.floor(Number(viewUnlock.value.points) || 0))
  const days = Math.max(0, Math.floor(Number(viewUnlock.value.days) || 0))
  viewUnlockSaving.value = true
  try {
    const res = await updateViewUnlockSettings({ points, days })
    viewUnlock.value = { points: res.points ?? points, days: res.days ?? days }
    ElMessage.success('查看权限解锁价格已保存')
  } catch {
    /* 写操作失败由请求拦截器统一弹错 */
  } finally {
    viewUnlockSaving.value = false
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    form.value = await fetchWelfareConfig()
    loaded.value = true
  } catch (e) {
    // GET 走 silent：拦截器不提示，这里给页面级错误态（带重试）
    loadError.value = e instanceof Error ? e.message : '加载配置失败'
  } finally {
    loading.value = false
  }
  // r40：查看权限解锁价格走独立 endpoint（view_unlock 家族），与福利表单分开加载，
  // 一边失败不影响另一边展示
  try {
    const vu = await fetchViewUnlockSettings()
    viewUnlock.value = { points: vu.points ?? 0, days: vu.days ?? 0 }
    viewUnlockLoaded.value = true
  } catch {
    /* 查看权限加载失败时该卡片显示错误态，不影响福利表单 */
  }
  // 全员赠送批次历史走独立 endpoint，一边失败不影响另一边
  await loadGiftBatches()
  // 永久账号白名单走独立 endpoint，一边失败不影响另一边
  await loadPermanent()
}

async function save() {
  if (!loaded.value || saving.value) return
  saving.value = true
  try {
    await saveWelfareConfig(form.value)
    ElMessage.success('已保存')
  } catch {
    /* 写操作失败由请求拦截器统一弹错 */
  } finally {
    saving.value = false
  }
}

// ==================== 全员赠送能量（批次） ====================
// 能力口径：金额/范围/备注全是建批参数，不写死；建批只落批次，发放由后端
// worker 分块执行。提交前必须先预览（人数+总量），且预览后改了参数要重预览。
const giftForm = ref<{ amount: number | null; scope: GiftAllScope; reason: string }>({
  amount: null, scope: 'all', reason: '',
})
const giftPreview = ref<GiftAllPreview | null>(null)
const giftPreviewKey = ref('')
const giftPreviewing = ref(false)
const giftCreating = ref(false)
const giftBatches = ref<GiftBatchRow[]>([])
const giftBatchesLoading = ref(false)

const giftStatusText: Record<GiftBatchRow['status'], string> = {
  pending: '待发放', running: '发放中', done: '已完成', failed: '失败待续跑',
}

function giftKey(amount: number | null, scope: GiftAllScope) {
  return `${amount ?? ''}|${scope}`
}

async function loadGiftBatches() {
  giftBatchesLoading.value = true
  try {
    const res = await fetchGiftBatches(50)
    giftBatches.value = res.batches ?? []
  } catch {
    /* 列表加载失败不影响配置表单，历史区保持空 */
  } finally {
    giftBatchesLoading.value = false
  }
}

async function doGiftPreview() {
  const amount = Math.floor(Number(giftForm.value.amount) || 0)
  if (amount <= 0) {
    ElMessage.warning('每人赠送量必须为正整数')
    return
  }
  giftPreviewing.value = true
  try {
    giftPreview.value = await previewGiftAll(amount, giftForm.value.scope)
    giftPreviewKey.value = giftKey(amount, giftForm.value.scope)
  } catch {
    /* 失败由请求拦截器统一弹错 */
  } finally {
    giftPreviewing.value = false
  }
}

async function doGiftCreate() {
  const amount = Math.floor(Number(giftForm.value.amount) || 0)
  if (amount <= 0) {
    ElMessage.warning('每人赠送量必须为正整数')
    return
  }
  if (!giftPreview.value || giftPreviewKey.value !== giftKey(amount, giftForm.value.scope)) {
    ElMessage.warning('请先点「预览」核对命中人数与总发放量，改了参数要重新预览')
    return
  }
  const p = giftPreview.value
  try {
    await ElMessageBox.confirm(
      `将给 ${p.user_count} 位用户每人赠送 ${amount} 能量，共 ${p.total_amount} 能量。批次创建后由后台分批发放，确定创建？`,
      '确认全员赠送',
      { type: 'warning', confirmButtonText: '创建批次', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  giftCreating.value = true
  try {
    await createGiftBatch({ amount, scope: giftForm.value.scope, reason: giftForm.value.reason.trim() })
    ElMessage.success('赠送批次已创建，后台发放中，可在下方历史里看进度')
    giftPreview.value = null
    giftPreviewKey.value = ''
    await loadGiftBatches()
  } catch {
    /* 失败由请求拦截器统一弹错 */
  } finally {
    giftCreating.value = false
  }
}

// ==================== 永久账号白名单 ====================
// 名单里的账号是永久账号：公益资格、查看凭证、订阅三处过期判定恒有效
// （不扣费、不提示续费、不因到期拦截）。独立 endpoint 独立保存，
// 不随上方配置表单走。总开关默认开，关后名单保留、全站回原过期口径。
const pwEnabled = ref(true)
const pwEntries = ref<PermanentEntry[]>([])
const pwLoading = ref(false)
const pwLoaded = ref(false)
const pwSavingConfig = ref(false)
const pwPick = ref<number | null>(null)
const pwOptions = ref<PermanentSearchUser[]>([])
const pwSearching = ref(false)
const pwNote = ref('')
const pwAdding = ref(false)

async function loadPermanent() {
  pwLoading.value = true
  try {
    const res = await fetchPermanentEntries()
    pwEntries.value = res.entries ?? []
    pwEnabled.value = res.enabled ?? true
    pwLoaded.value = true
  } catch {
    /* 列表加载失败只影响本卡片 */
  } finally {
    pwLoading.value = false
  }
}

async function onPwEnabledChange(v: boolean) {
  pwSavingConfig.value = true
  try {
    const res = await savePermanentConfig(v)
    pwEnabled.value = res.enabled ?? v
    ElMessage.success(v ? '永久白名单已开启' : '永久白名单已关闭（名单保留，全站回原过期口径）')
  } catch {
    pwEnabled.value = !v
  } finally {
    pwSavingConfig.value = false
  }
}

async function onPwSearch(query: string) {
  if (!query.trim()) {
    pwOptions.value = []
    return
  }
  pwSearching.value = true
  try {
    const res = await searchPermanentUsers(query.trim())
    pwOptions.value = res.users ?? []
  } catch {
    pwOptions.value = []
  } finally {
    pwSearching.value = false
  }
}

async function doPwAdd() {
  if (!pwPick.value) {
    ElMessage.warning('请先搜索并选择要加入的用户')
    return
  }
  pwAdding.value = true
  try {
    await addPermanentEntry({ user_id: pwPick.value, note: pwNote.value.trim() || undefined })
    ElMessage.success('已加入永久名单')
    pwPick.value = null
    pwNote.value = ''
    pwOptions.value = []
    await loadPermanent()
  } catch {
    /* 失败由请求拦截器统一弹错（重复添加后端回 409） */
  } finally {
    pwAdding.value = false
  }
}

async function doPwRemove(row: PermanentEntry) {
  try {
    await ElMessageBox.confirm(
      `把「${row.username ?? row.user_id}」移出永久名单？移除后该账号即刻回到原来的过期口径（资格/凭证/订阅数据本身不动）。`,
      '确认移除',
      { type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await removePermanentEntry(row.user_id)
    ElMessage.success('已移出永久名单')
    await loadPermanent()
  } catch {
    /* 失败由请求拦截器统一弹错 */
  }
}

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="内容"
      title="积分与公益配置"
      description="货币体系、红包规则、兑换比例与公益服到期策略"
    >
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Save" :loading="saving" :disabled="!loaded" @click="save">保存</el-button>
      </template>
    </PageHeader>

    <el-alert type="info" :closable="false">
      签到积分规则已统一到「系统设置 → 每日签到」（仅公益服用户获得积分）
    </el-alert>

    <SectionCard v-if="loadError && !loaded">
      <EmptyState :icon="Settings2" title="配置加载失败" :description="loadError">
        <template #actions>
          <el-button :loading="loading" @click="load">重试</el-button>
        </template>
      </EmptyState>
    </SectionCard>

    <div v-else v-loading="loading" class="form-grid">
      <SectionCard
        title="货币体系"
        :icon="Wallet"
        description="人民币充值兑换积分的比例"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item v-for="f in currencyFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="红包规则"
        :icon="Gift"
        description="红包手续费与 7 天频率限制"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item v-for="f in redpacketFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="能量老虎机"
        :icon="Dices"
        description="TG 机器人老虎机的注额、赔付与头奖池（结果由 Telegram 原生老虎机决定）"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item label="总开关">
            <el-switch v-model="slotsEnabled" />
            <span class="field-hint">关闭后机器人老虎机入口与下注全部停用，默认开</span>
          </el-form-item>
          <el-form-item label="群里可玩">
            <el-switch v-model="slotsGroupEnabled" />
            <span class="field-hint">名单群内的老虎机面板与注额按钮在群里直接可用：点按钮的人用自己的能量下注、中奖归他，公开结果按机器人消息自毁时间自动删除；关闭后群里只提示去私聊，私聊玩法不受影响。默认开</span>
          </el-form-item>
          <el-form-item v-for="f in slotsFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="积分转账"
        :icon="ArrowLeftRight"
        description="用户间积分转账的手续费与限额"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item v-for="f in transferFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="Telegram 门禁"
        :icon="Send"
        description="公益服能力（签到/积分/红包/抽奖）是否要求绑定 Telegram"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item label="强制绑定">
            <el-switch v-model="tgBindEnabled" />
            <span class="field-hint">开启后未绑定用户无法使用公益服写操作；老用户有 7 天宽限期；Bot 故障时可关闭降级</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="TG Bot 总控"
        :icon="Bot"
        description="Bot 启用群白名单与限流（各功能开关由服务端配置控制）"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item v-for="f in botFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
          <el-form-item label="频道马甲治理">
            <el-switch v-model="channelGuardEnabled" />
            <span class="field-hint">开启后群里以外部频道身份发言的消息会被删除并回说明；匿名管理员不受影响，默认开</span>
          </el-form-item>
          <el-form-item label="退群取消资格">
            <el-switch v-model="leaveCancelEnabled" />
            <span class="field-hint">开启后已绑定用户退群／被踢自动只取消公益资格（账号、能量、查看凭证不动）；关闭则退群不做处理，默认开</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="获取与兑换"
        :icon="Coins"
        description="发言积分上限与积分兑换公益天数"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item label="兑换只认自挣">
            <el-switch v-model="redeemSelfEarnedOnly" />
            <span class="field-hint">开启后自助兑换公益资格只能花自己挣的能量（签到、发言等），转入、赠送、中奖的能量不能用于兑换资格；只卡自助兑换，管理员发放与买查看权限不受影响，默认开</span>
          </el-form-item>
          <el-form-item v-for="f in earnRedeemFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" :inputmode="f.inputmode" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="公益服规则"
        :icon="Settings2"
        description="到期保留、未活跃禁用、连签续期与新注册赠送（求片额度在运营参数里统一设置）"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item label="连签自动续期">
            <el-switch v-model="streakRenewEnabled" />
            <span class="field-hint">开启后资格到期时连签达标自动续期；关闭后到期照常走保留期／取消流程，默认开</span>
          </el-form-item>
          <el-form-item v-for="f in welfareFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <!-- r40：查看权限解锁价格（emby_view_unlock_points/days）。后端接口一直存在，
           但管理后台没有入口，只能手写 DB；本轮补上独立卡片（独立 endpoint 独立保存） -->
      <SectionCard
        title="查看权限解锁"
        :icon="KeyRound"
        description="公益服查看权限（Emby 账号/线路）的积分价格与有效期"
      >
        <el-form v-if="viewUnlockLoaded" :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item label="积分价格">
            <el-input-number v-model="viewUnlock.points" :min="0" :step="10" class="field-input" />
            <span class="field-suffix">积分</span>
            <span class="field-hint">用户解锁查看权限需要支付的积分</span>
          </el-form-item>
          <el-form-item label="有效期">
            <el-input-number v-model="viewUnlock.days" :min="0" :step="1" class="field-input" />
            <span class="field-suffix">天</span>
            <span class="field-hint">解锁后有效天数，0 = 永久有效</span>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :icon="Save" :loading="viewUnlockSaving" @click="saveViewUnlock">保存解锁价格</el-button>
          </el-form-item>
        </el-form>
        <el-alert v-else title="查看权限配置加载失败，请刷新重试" type="warning" :closable="false" />
      </SectionCard>

    </div>

    <!-- 全员赠送能量：独立 endpoint 独立提交（不随上方配置表单保存） -->
    <SectionCard
      v-if="loaded"
      title="全员赠送能量"
      :icon="Gift"
      description="给全体用户（或仅公益资格用户）每人赠送能量：先预览人数与总量，确认后建批次，后台分批发放、可续跑不重发"
      style="margin-top: 16px"
    >
      <el-form :label-position="labelPosition" label-width="120px" class="config-form">
        <el-form-item label="每人赠送">
          <el-input-number v-model="giftForm.amount" :min="1" :step="10" class="field-input" />
          <span class="field-suffix">能量/人</span>
          <span class="field-hint">正整数；金额与范围都是本次批次的参数，不写死在配置里</span>
        </el-form-item>
        <el-form-item label="赠送范围">
          <el-select v-model="giftForm.scope" class="field-input">
            <el-option label="全部用户" value="all" />
            <el-option label="仅公益资格用户" value="welfare" />
          </el-select>
          <span class="field-hint">公益口径＝当前持有有效公益资格（未过期）者；发放时按实时资格判定</span>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="giftForm.reason" maxlength="255" placeholder="如：系统维护补偿" class="field-input" style="width: 260px" />
          <span class="field-hint">会写进每位用户的能量台账说明</span>
        </el-form-item>
        <el-form-item>
          <el-button :loading="giftPreviewing" @click="doGiftPreview">预览</el-button>
          <el-button type="primary" :loading="giftCreating" @click="doGiftCreate">创建赠送批次</el-button>
          <span v-if="giftPreview" class="field-hint" style="flex-basis: auto; margin-left: 12px">
            命中 {{ giftPreview.user_count }} 人，共发放 {{ giftPreview.total_amount }} 能量
          </span>
        </el-form-item>
      </el-form>

      <el-table v-loading="giftBatchesLoading" :data="giftBatches" size="small" style="margin-top: 8px">
        <el-table-column prop="id" label="批次" width="70" />
        <el-table-column label="每人" width="80">
          <template #default="{ row }">{{ row.amount }} 能量</template>
        </el-table-column>
        <el-table-column label="范围" width="90">
          <template #default="{ row }">{{ row.scope === 'welfare' ? '公益资格' : '全部用户' }}</template>
        </el-table-column>
        <el-table-column prop="reason" label="备注" min-width="140" show-overflow-tooltip />
        <el-table-column label="进度" width="110">
          <template #default="{ row }">{{ row.granted_count }} / {{ row.target_count }} 人</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            {{ giftStatusText[row.status as GiftBatchRow['status']] }}
            <span v-if="row.status === 'failed' && row.last_error" class="field-hint">{{ row.last_error }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_by_name" label="操作者" width="110" />
        <el-table-column label="创建时间" min-width="150">
          <template #default="{ row }">{{ row.created_at ? row.created_at.replace('T', ' ').slice(0, 19) : '' }}</template>
        </el-table-column>
      </el-table>
      <div style="margin-top: 8px">
        <el-button text :loading="giftBatchesLoading" @click="loadGiftBatches">刷新历史</el-button>
      </div>
    </SectionCard>

    <!-- 永久账号白名单：独立 endpoint 独立保存（不随上方配置表单走） -->
    <SectionCard
      title="永久账号白名单"
      :icon="Crown"
      description="名单里的账号是永久账号：公益资格、查看凭证、订阅都不过期，不用续费、不弹续费提醒；两个服通用"
      style="margin-top: 16px"
    >
      <el-form :label-position="labelPosition" label-width="120px" class="config-form">
        <el-form-item label="总开关">
          <el-switch :model-value="pwEnabled" :loading="pwSavingConfig" @change="onPwEnabledChange" />
          <span class="field-hint">默认开；关闭后名单保留不删，但全站回到原来的过期口径，重开即刻恢复</span>
        </el-form-item>
        <el-form-item label="添加账号">
          <el-select
            v-model="pwPick"
            filterable
            remote
            clearable
            :remote-method="onPwSearch"
            :loading="pwSearching"
            placeholder="搜索用户名 / 邮箱 / 用户 ID"
            class="field-input"
            style="width: 260px"
          >
            <el-option
              v-for="u in pwOptions"
              :key="u.id"
              :label="`${u.username}（#${u.id}）`"
              :value="u.id"
              :disabled="u.already_listed"
            >
              <span>{{ u.username }}（#{{ u.id }}）</span>
              <span v-if="u.already_listed" class="field-hint" style="flex-basis: auto; margin-left: 8px">已在名单</span>
            </el-option>
          </el-select>
          <el-input v-model="pwNote" maxlength="255" placeholder="备注（可选，如：站主小号）" class="field-input" style="width: 220px; margin-left: 8px" />
          <el-button type="primary" :loading="pwAdding" style="margin-left: 8px" @click="doPwAdd">加入名单</el-button>
          <span class="field-hint">活力值不在白名单豁免范围内：名单里的公益账号仍会被每日扣活力、活力不足仍会拦播放</span>
        </el-form-item>
      </el-form>

      <el-table v-loading="pwLoading" :data="pwEntries" size="small" style="margin-top: 8px">
        <el-table-column label="用户" min-width="160">
          <template #default="{ row }">{{ row.username ?? '（已注销）' }}（#{{ row.user_id }}）</template>
        </el-table-column>
        <el-table-column label="身份" width="90">
          <template #default="{ row }">{{ row.is_welfare ? '公益' : '普通' }}</template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="140" show-overflow-tooltip />
        <el-table-column prop="added_by_name" label="添加人" width="110" />
        <el-table-column label="添加时间" min-width="150">
          <template #default="{ row }">{{ row.created_at ? row.created_at.replace('T', ' ').slice(0, 19) : '' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button text type="danger" @click="doPwRemove(row)">移除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="pwLoaded && !pwEntries.length" class="field-hint" style="margin-top: 8px">名单是空的，还没有永久账号。</div>
      <div style="margin-top: 8px">
        <el-button text :loading="pwLoading" @click="loadPermanent">刷新名单</el-button>
      </div>
    </SectionCard>
  </div>
</template>

<style scoped>
.form-grid {
  display: grid;
  gap: 16px;
  /* min(100%, …)：360px 手机上单列铺满，不会因为 320px 的轨道下限把页面撑出横向滚动 */
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 380px), 1fr));
}
.config-form :deep(.el-form-item__content) {
  flex-wrap: wrap;
  row-gap: 4px;
}
.field-input {
  width: 160px;
  max-width: 100%;
}
.config-form :deep(.el-form-item) {
  margin-bottom: 14px;
}
.config-form :deep(.el-form-item:last-child) {
  margin-bottom: 0;
}
.field-suffix {
  margin-left: 8px;
  color: var(--au-text-2);
  white-space: nowrap;
}
/* 说明另起一行：长句子跟在输入框后面会被挤成一列竖排 */
.field-hint {
  flex-basis: 100%;
  color: var(--au-text-3);
  font-size: 12px;
  line-height: 1.5;
}
</style>
