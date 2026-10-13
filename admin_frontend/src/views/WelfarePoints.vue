<script setup lang="ts">
/**
 * 公益服·积分与公益配置：SystemConfig 表单
 *
 * v2.55（暗房影院统一）：PageHeader + 分组 SectionCard。
 * PR #437：签到积分三项已移至「系统设置 → 每日签到」，本页移除。
 * P0 统一货币体系：新增货币体系（充值比例）与红包规则配置组。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowLeftRight, Bot, Coins, Gift, MessageCircle, MessageSquareDashed, RefreshCw, Save, Send, Settings2, Wallet } from 'lucide-vue-next'
import { EmptyState, PageHeader, SectionCard } from '@/components/ui'
import { useBreakpoint } from '@/composables/useBreakpoint'
import { fetchWelfareConfig, saveWelfareConfig } from '@/api/welfare'

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

const redpacketRefundEnabled = boolSwitch('redpacket_refund_enabled')

const transferFields = [
  { key: 'points_transfer_fee_pct', label: '手续费', hint: '按转账金额比例收取，默认 5，填 0 不收', suffix: '%' },
  { key: 'points_transfer_min', label: '单笔最小', hint: '默认 1', suffix: '积分' },
  { key: 'points_transfer_max', label: '单笔最大', hint: '默认 0 表示不限', suffix: '积分' },
  { key: 'points_transfer_daily_cap', label: '每日转出上限', hint: '默认 0 表示不限', suffix: '积分/天' },
]

const transferEnabled = computed({
  get: () => (form.value['points_transfer_enabled'] ?? '1') === '1',
  set: (v: boolean) => { form.value['points_transfer_enabled'] = v ? '1' : '0' },
})

const earnRedeemFields = [
  { key: 'points_chat_daily_cap', label: '发言每日上限', hint: '默认 20', suffix: '积分/天' },
  { key: 'points_redeem_7d', label: '兑换 7 天', hint: '默认 100', suffix: '积分' },
  { key: 'points_redeem_30d', label: '兑换 30 天', hint: '默认 300', suffix: '积分' },
]

const tgBindEnabled = computed({
  get: () => (form.value['welfare_require_tg_bind'] ?? '1') === '1',
  set: (v: boolean) => { form.value['welfare_require_tg_bind'] = v ? '1' : '0' },
})

const tgGuideEnabled = computed({
  get: () => (form.value['tg_bind_guide_enabled'] ?? '1') === '1',
  set: (v: boolean) => { form.value['tg_bind_guide_enabled'] = v ? '1' : '0' },
})

// B4 TG Bot 总控：true/false 开关统一用辅助函数生成
function boolSwitch(key: string, dflt = 'true') {
  return computed({
    get: () => (form.value[key] ?? dflt) === 'true',
    set: (v: boolean) => { form.value[key] = v ? 'true' : 'false' },
  })
}

const botEnabled = boolSwitch('bot_enabled')
const botRedpacketEnabled = boolSwitch('bot_redpacket_enabled')
const botCmdSwitches = [
  { key: 'bot_cmd_checkin', label: '/checkin' },
  { key: 'bot_cmd_points', label: '/points' },
  { key: 'bot_cmd_redeem', label: '/redeem' },
  { key: 'bot_cmd_bind', label: '/bind' },
].map((s) => ({ ...s, model: boolSwitch(s.key) }))

const botFields = [
  { key: 'bot_group_ids', label: '启用群 ID', hint: '逗号分隔的 TG 群 id，空=所有群都启用 Bot', suffix: '' },
  { key: 'bot_rate_limit_seconds', label: '命令限流', hint: '每用户每命令最小间隔，默认 3', suffix: '秒' },
  { key: 'bot_group_rate_limit', label: '群限流', hint: '每群每分钟最多处理消息数，超限静默丢弃，默认 20', suffix: '条/分' },
]

const welfareFields = [
  { key: 'welfare_grace_days', label: '到期保留天数', hint: '到期后可登录但不可播放，默认 7', suffix: '天' },
  { key: 'welfare_inactive_days', label: '未活跃禁用天数', hint: '默认 30', suffix: '天' },
  { key: 'welfare_request_monthly', label: '公益求片额度', hint: '默认 3', suffix: '次/月' },
]

const chatPointsFields = [
  { key: 'chat_points_group_ids', label: '目标群 ID', hint: '逗号分隔的 TG 群 id，空=不计分', suffix: '' },
  { key: 'chat_points_per_message', label: '单条分值', hint: '默认 1', suffix: '积分/条' },
  { key: 'chat_points_min_len', label: '最小字符数', hint: '默认 2，过滤纯表情刷屏', suffix: '字' },
  { key: 'chat_points_minute_window', label: '防刷窗口', hint: '同一用户多少秒内最多计 1 条，默认 60', suffix: '秒' },
  { key: 'chat_points_daily_cap', label: '每日上限', hint: '默认 20', suffix: '积分/天' },
  { key: 'chat_points_points_per_day', label: '兑换参考比例', hint: '多少积分兑 1 天（仅展示说明），默认 100', suffix: '积分/天' },
]

const chatPointsEnabled = computed({
  get: () => form.value['chat_points_enabled'] === 'true',
  set: (v: boolean) => { form.value['chat_points_enabled'] = v ? 'true' : 'false' },
})

// v2 求片：总开关 / 附议 / 附议者通知（'1' 为开）
function seekBoolSwitch(key: string) {
  return computed({
    get: () => (form.value[key] ?? '1') === '1',
    set: (v: boolean) => { form.value[key] = v ? '1' : '0' },
  })
}
const mediaSeekEnabled = seekBoolSwitch('media_seek_enabled')
const mediaSeekVoteEnabled = seekBoolSwitch('media_seek_vote_enabled')
const mediaSeekNotifyVoters = seekBoolSwitch('media_seek_notify_voters')

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

onMounted(load)
</script>

<template>
  <div class="admin-page">
    <PageHeader
      eyebrow="公益服"
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
          <el-form-item label="过期自动退款">
            <el-switch v-model="redpacketRefundEnabled" />
            <span class="field-hint">关闭后过期红包不再自动退款；默认开启</span>
          </el-form-item>
          <el-form-item v-for="f in redpacketFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="积分转账"
        :icon="ArrowLeftRight"
        description="用户间积分转账的开关、手续费与限额"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item label="功能开关">
            <el-switch v-model="transferEnabled" />
            <span class="field-hint">关闭后用户端隐藏转账入口，后端接口拒绝</span>
          </el-form-item>
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
          <el-form-item label="引导页">
            <el-switch v-model="tgGuideEnabled" />
            <span class="field-hint">开启后，新用户注册成功将进入 TG 绑定引导页，未完成绑定前无法使用面板</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="TG Bot 总控"
        :icon="Bot"
        description="Bot 总开关、群白名单、限流与各功能开关（B4）"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item label="总开关">
            <el-switch v-model="botEnabled" />
            <span class="field-hint">关闭后 Bot 不响应任何消息（轮询继续跑）；默认开启</span>
          </el-form-item>
          <el-form-item label="红包功能">
            <el-switch v-model="botRedpacketEnabled" />
            <span class="field-hint">关闭后 /redpacket 与抢红包按钮均提示已关闭；后端红包接口不受影响</span>
          </el-form-item>
          <el-form-item v-for="s in botCmdSwitches" :key="s.key" :label="s.label">
            <el-switch v-model="s.model.value" />
            <span class="field-hint">关闭后该命令回复"该功能已关闭"</span>
          </el-form-item>
          <el-form-item v-for="f in botFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="获取与兑换"
        :icon="Coins"
        description="发言积分上限与积分兑换公益天数"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item v-for="f in earnRedeemFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="公益服规则"
        :icon="Settings2"
        description="到期保留、未活跃禁用、求片额度"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item v-for="f in welfareFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="群发言积分"
        :icon="MessageCircle"
        description="Bot 统计群发言赚积分，总开关默认关闭"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item label="总开关">
            <el-switch v-model="chatPointsEnabled" />
            <span class="field-hint">关闭时 bot 不统计任何发言，默认关闭</span>
          </el-form-item>
          <el-form-item v-for="f in chatPointsFields" :key="f.key" :label="f.label">
            <el-input v-model="form[f.key]" class="field-input" />
            <span class="field-suffix">{{ f.suffix }}</span>
            <span class="field-hint">{{ f.hint }}</span>
          </el-form-item>
        </el-form>
      </SectionCard>

      <SectionCard
        title="求片 v2"
        :icon="MessageSquareDashed"
        description="求片总开关、附议与附议者通知，默认全开"
      >
        <el-form :label-position="labelPosition" label-width="120px" class="config-form">
          <el-form-item label="求片总开关">
            <el-switch v-model="mediaSeekEnabled" />
            <span class="field-hint">关闭后用户不能提交求片（只读列表不受影响），默认开启</span>
          </el-form-item>
          <el-form-item label="附议开关">
            <el-switch v-model="mediaSeekVoteEnabled" />
            <span class="field-hint">用户可对他人的求片附议 +1，默认开启</span>
          </el-form-item>
          <el-form-item label="通知附议者">
            <el-switch v-model="mediaSeekNotifyVoters" />
            <span class="field-hint">求片入库时除求片者外也通知附议者，默认开启</span>
          </el-form-item>
        </el-form>
      </SectionCard>
    </div>
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
