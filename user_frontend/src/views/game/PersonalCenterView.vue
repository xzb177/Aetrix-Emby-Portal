<template>
  <div class="v4-scope">
  <div class="v4-page pc-page">
    <div class="pc-wrap">
      <!-- 身份卡 -->
      <div class="v4-card identity">
        <span class="id-avatar" aria-hidden="true">{{ avatarInitial }}</span>
        <div class="id-main">
          <div class="id-name">{{ displayName }}</div>
          <div v-if="whitelistIdentity" class="id-badges">
            <span class="wl-chip">白名单</span>
            <span class="wl-title">{{ whitelistIdentity.title }}</span>
            <span class="wl-serial">{{ whitelistIdentity.label }}</span>
          </div>
        </div>
        <span v-if="identityBadge" class="realm-pill">{{ identityBadge }}</span>
      </div>

      <!-- 状态总览（格子全部由现有接口数据驱动，拿不到的格子不渲染） -->
      <div class="v4-card pc-ov">
        <div class="ov-grid">
          <button type="button" class="ov-cell" @click="router.push('/game/account/member')">
            <span class="ov-v" :class="{ teal: headCellActive }">{{ headCell.value }}</span>
            <span class="ov-l">{{ headCell.label }}</span>
          </button>
          <button v-if="viewCell" type="button" class="ov-cell" @click="router.push('/game/account/member')">
            <span class="ov-v">{{ viewCell.value }}</span>
            <span class="ov-l">{{ viewCell.label }}</span>
          </button>
          <button v-if="points != null" type="button" class="ov-cell" @click="router.push('/shop')">
            <span class="ov-v">{{ points }}</span>
            <span class="ov-l">能量</span>
          </button>
          <button v-if="vitality" type="button" class="ov-cell" @click="router.push('/game/checkin')">
            <span class="ov-v">{{ vitality.vitality }}/{{ vitality.max }}</span>
            <span class="ov-l">活力 · 今日</span>
          </button>
        </div>
      </div>

      <div class="pc-cols">
        <div class="pc-col">
          <!-- 我的权益 -->
          <section class="pc-group">
            <div class="pc-gtitle">我的权益</div>
            <div class="v4-card pc-rows-card">
              <PcEntryRow :icon="Crown" name="我的会员" :meta="memberMeta" to="/game/account/member" />
              <PcEntryRow :icon="Wallet" name="我的钱包" :meta="points != null ? `能量 ${points}` : ''" to="/shop" />
              <PcEntryRow :icon="List" name="能量账本" to="/game/ledger" />
            </div>
          </section>

          <!-- 我的片子 -->
          <section class="pc-group">
            <div class="pc-gtitle">我的片子</div>
            <div class="v4-card pc-rows-card">
              <PcEntryRow :icon="Heart" name="想看" :meta="wantMeta" to="/want" />
              <PcEntryRow :icon="Search" name="我的求片" :meta="requestMeta" to="/store-requests" />
              <PcEntryRow :icon="Star" name="我的收藏" to="/favorites" />
              <PcEntryRow
                v-if="followVisible"
                :icon="MonitorPlay"
                name="追剧订阅"
                :meta="followMeta"
                expandable
                :expanded="followOpen"
                @click="followOpen = !followOpen"
              />
            </div>
            <!-- 面板常驻挂载（v-show 只控显隐）：挂载即加载并回报 summary，
                 接口 404 时父级把整行藏掉 -->
            <div v-show="followVisible && followOpen" class="pc-expand-below">
              <SeriesFollowsPanel @summary="onFollowSummary" />
            </div>
          </section>
        </div>

        <div class="pc-col">
          <!-- 成长 -->
          <section class="pc-group">
            <div class="pc-gtitle">成长</div>
            <div class="v4-card pc-rows-card">
              <PcEntryRow :icon="Mountain" name="等级" :meta="entryLevelText" to="/shop" />
              <PcEntryRow :icon="Trophy" name="成就" :meta="entryAchvText" to="/collector" />
              <PcEntryRow :icon="Sparkles" name="馆灵" :meta="entryPetText" to="/collector" />
              <PcEntryRow :icon="CalendarCheck" name="连签" :meta="entryStreakText" to="/game/checkin" />
              <PcEntryRow :icon="Bird" name="信鸽" meta="站内消息都在这里" to="/game/pigeon" />
            </div>
          </section>

          <!-- 我的服务 -->
          <section class="pc-group">
            <div class="pc-gtitle">我的服务</div>
            <div class="v4-card pc-rows-card">
              <PcEntryRow
                v-if="driveRowVisible"
                :icon="HardDrive"
                name="我的网盘"
                :meta="driveMeta"
                :to="{ path: '/game/account/settings', hash: '#my-drive' }"
              />
              <PcEntryRow
                v-if="myLines.length"
                :icon="Route"
                name="我的专属线路"
                :meta="`${myLines.length} 条可用`"
                expandable
                :expanded="linesOpen"
                @click="linesOpen = !linesOpen"
              />
              <PcEntryRow
                v-if="agentProfile"
                :icon="Briefcase"
                name="代理中心"
                :meta="`进货余额 ¥${agentProfile.balance.toFixed(2)}`"
                to="/game/agent"
              />
            </div>
            <div v-if="myLines.length && linesOpen" class="v4-card pc-lines-card">
              <div v-for="line in myLines" :key="line.entry_url" class="line-row">
                <span class="line-main">
                  <span class="line-name">{{ line.name }}</span>
                  <span class="line-url">{{ line.entry_url }}</span>
                </span>
                <button type="button" class="v4-mini-btn" @click="copyLineUrl(line.entry_url)">复制地址</button>
              </div>
              <p class="line-note">在播放器里添加服务器时填这些地址，就走你的专属线路进服。</p>
            </div>
          </section>

          <!-- 账号 -->
          <section class="pc-group">
            <div class="pc-gtitle">账号</div>
            <div class="v4-card pc-rows-card">
              <PcEntryRow :icon="Settings" name="账号设置" meta="密码 · 设备 · 绑定" to="/game/account/settings" />
              <PcEntryRow :icon="LogOut" name="退出登录" danger @click="handleLogout" />
            </div>
          </section>
        </div>
      </div>
    </div>
  </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 「我的」个人中心（/game/account）：身份卡 + 状态总览 + 分组入口行，
 * 全页只串入口与现状摘要，不留说明书段落。设置类功能在账号设置页
 * （/game/account/settings），会员管理在子页（/game/account/member）。
 * 所有摘要只用现有接口拿得到的数据（如设备页那套口径）：等级/成就/
 * 馆灵/连签来自 memberApi/checkinApi/panelApi.collector，能量来自签到
 * 状态（兜底账号卡 points_balance），想看/求片计数来自商店两口，网盘
 * 摘要来自 storageBindingApi；任一拿不到就留空或整行不渲染，不编造。
 * 旧深链 #my-drive 直达设置页的绑定组。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Bird, Briefcase, CalendarCheck, Crown, HardDrive, Heart, List, LogOut,
  MonitorPlay, Mountain, Route, Search, Settings, Sparkles, Star, Trophy, Wallet,
} from 'lucide-vue-next'
import PcEntryRow from '@/components/profile/PcEntryRow.vue'
import SeriesFollowsPanel from '@/components/profile/SeriesFollowsPanel.vue'
import {
  embyApi, storeApi, storeRequestsApi, subscriptionApi,
  type AccountCard, type MySubscription,
} from '@/api'
import {
  checkinApi, memberApi, storageBindingApi, vitalityApi, welfareApi,
  type VitalityStatus,
} from '@/api/economy'
import { panelApi } from '@/composables/usePanelApi'
import { getMyExclusiveLines, type MyExclusiveLine } from '@/api/user'
import { getAgentMe, type AgentProfile } from '@/api/agent'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const userStore = useUserStore()

// ── 身份卡 ──
const displayName = computed(() => userStore.user?.username || '我的')
const avatarInitial = computed(() => (userStore.user?.username || '账').slice(0, 1).toUpperCase())
const isFreeRealm = computed(() => userStore.isFreeRealm)
const identityBadge = computed(() => {
  if (isFreeRealm.value) return '公益服'
  if (userStore.isVIP) return '会员'
  return ''
})
const whitelistIdentity = computed(() => userStore.user?.whitelist_identity ?? null)

// ── 状态总览 ──
const subscriptions = ref<MySubscription[]>([])
const subsLoaded = ref(false)
const welfare = ref<{ is_welfare: boolean; expires_at: string | null } | null>(null)
const accountCard = ref<AccountCard | null>(null)
const points = ref<number | null>(null)
const vitality = ref<VitalityStatus | null>(null)

const activeSub = computed(
  () => subscriptions.value.find(s => s.status === 'active' && s.days_left > 0) || null,
)
const headCell = computed(() => {
  if (isFreeRealm.value) {
    if (!welfare.value) return { value: '—', label: '公益资格' }
    return {
      value: welfare.value.is_welfare ? '生效中' : '未开通',
      label: welfare.value.is_welfare && welfare.value.expires_at
        ? `公益资格 · 到期 ${welfare.value.expires_at.slice(0, 10)}`
        : '公益资格',
    }
  }
  if (!subsLoaded.value) return { value: '—', label: '我的会员' }
  if (!activeSub.value) return { value: '未开通', label: '我的会员' }
  return {
    value: '生效中',
    label: `${activeSub.value.plan_name} · ${activeSub.value.end_date.slice(0, 10)} 到期`,
  }
})
/** 总览首格数值深青（效果图口径）：权益生效中时染深青，其余墨色 */
const headCellActive = computed(() =>
  isFreeRealm.value ? !!welfare.value?.is_welfare : !!activeSub.value,
)
const viewCell = computed(() => {
  const vp = accountCard.value?.view_permission
  if (!vp) return null
  if (!vp.granted) return { value: '未解锁', label: '查看凭证' }
  if (!vp.expires_at) return { value: '已解锁', label: '查看凭证 · 永久' }
  const expired = new Date(vp.expires_at).getTime() <= Date.now()
  return {
    value: expired ? '已到期' : '已解锁',
    label: `查看凭证 · 到期 ${vp.expires_at.slice(0, 10)}`,
  }
})
const memberMeta = computed(() => {
  if (isFreeRealm.value) return '公益服 · 免费开放'
  if (!subsLoaded.value) return ''
  return activeSub.value
    ? `${activeSub.value.plan_name} · 剩余 ${activeSub.value.days_left} 天`
    : '还没有会员'
})

// ── 我的片子摘要 ──
const wantMeta = ref('')
const requestMeta = ref('')
async function loadWantMeta() {
  try {
    const res = await storeApi.getWants()
    const items = res.items || []
    if (!items.length) return
    const preparing = items.filter(i => ['wanted', 'resolving', 'transferring'].includes(i.state)).length
    wantMeta.value = `${items.length} 部` + (preparing ? ` · ${preparing} 部准备中` : '')
  } catch { /* 商店未开启/未登录会员时不显示摘要 */ }
}
async function loadRequestMeta() {
  try {
    const res = await storeRequestsApi.getMine()
    const active = (res.items || []).filter(i => i.state === 'registered' || i.state === 'seeking').length
    if (active) requestMeta.value = `${active} 条进行中`
  } catch { /* 拿不到就不显示 */ }
}

// ── 追剧订阅（面板回报可见性与订阅数） ──
const followVisible = ref(false)
const followMeta = ref('')
const followOpen = ref(false)
function onFollowSummary(payload: { visible: boolean; activeCount: number }) {
  followVisible.value = payload.visible
  followMeta.value = payload.activeCount > 0 ? `订阅 ${payload.activeCount} 部` : ''
}

// ── 成长摘要（与旧页 loadEntrySummaries 同源同口径） ──
const entryLevelText = ref('')
const entryAchvText = ref('')
const entryPetText = ref('')
const entryStreakText = ref('')
async function loadEntrySummaries() {
  try {
    const m = await memberApi.info()
    if (m && typeof m.level === 'number') {
      entryLevelText.value = `Lv.${m.level}${m.level_name ? ` · ${m.level_name}` : ''}`
    }
  } catch { /* 摘要拿不到就不显示 */ }
  try {
    const c = await panelApi.collector()
    if (typeof c?.achievement_count === 'number') {
      entryAchvText.value = `已点亮 ${c.achievement_count} 枚`
    }
    const p = c?.pet
    if (p && Array.isArray(p.stages) && p.stages.length) {
      const stageName = p.stages[p.stage]?.name
      entryPetText.value = stageName ? `${p.name} · ${stageName}` : p.name
    }
  } catch { /* 摘要拿不到就不显示 */ }
}

// ── 我的服务 ──
const myLines = ref<MyExclusiveLine[]>([])
const linesOpen = ref(false)
async function copyLineUrl(url: string) {
  try {
    await navigator.clipboard.writeText(url)
    toast.success('线路地址已复制，去播放器里添加服务器时粘贴它')
  } catch {
    toast.error('复制失败，请长按地址手动复制')
  }
}
const agentProfile = ref<AgentProfile | null>(null)
const driveRowVisible = ref(false)
const driveMeta = ref('')
async function loadDriveMeta() {
  try {
    const res = await storageBindingApi.providers()
    if (!(res.providers || []).includes('drive')) return
    driveRowVisible.value = true
    const list = await storageBindingApi.list()
    const drive = (list.bindings || []).filter(b => b.provider === 'drive')
    // 小字照效果图口径只报状态（细节在账号设置页的绑定管理里）
    if (!drive.length) driveMeta.value = '谷歌盘 · 未绑定'
    else if (drive.some(b => b.status === 'active')) driveMeta.value = '谷歌盘 · 已绑定'
    else driveMeta.value = '谷歌盘 · 不可用'
  } catch { /* 名单拿不到整行不渲染（与设置页门控一致） */ }
}

// ── 总览数据 ──
async function loadOverview() {
  try {
    subscriptions.value = await subscriptionApi.getMine()
    subsLoaded.value = true
  } catch { /* 摘要拿不到就留空 */ }
  try {
    const st = await welfareApi.status()
    welfare.value = { is_welfare: st.is_welfare, expires_at: st.welfare_expires_at }
    if (userStore.user) userStore.user.is_welfare = st.is_welfare
  } catch { /* 状态口失败不阻断页面 */ }
  try {
    accountCard.value = await embyApi.getAccountCard()
    if (points.value == null) {
      const bal = accountCard.value?.view_permission?.points_balance
      if (typeof bal === 'number') points.value = bal
    }
  } catch { /* 账号卡失败不阻断页面 */ }
  try {
    const st = await checkinApi.status()
    if (st && typeof st.points === 'number') points.value = st.points
    if (st && st.streak > 0) entryStreakText.value = `已连报 ${st.streak} 天`
  } catch { /* 摘要拿不到就不显示 */ }
  try {
    vitality.value = await vitalityApi.status()
  } catch { /* 非公益服用户注定失败，格子不渲染 */ }
}

/** 退出登录：与设置页同款动作 */
async function handleLogout() {
  await userStore.logout()
  router.push('/login')
}

onMounted(() => {
  // 旧深链（播放器方案弹窗的「去绑盘」）直达设置页绑定组
  if (route.hash === '#my-drive') {
    void router.replace({ path: '/game/account/settings', hash: '#my-drive' })
    return
  }
  void loadOverview()
  void loadEntrySummaries()
  void loadWantMeta()
  void loadRequestMeta()
  void loadDriveMeta()
  void getMyExclusiveLines().then(lines => { myLines.value = lines }).catch(() => {})
  void getAgentMe().then(me => { agentProfile.value = me.is_agent ? me.agent : null }).catch(() => {})
})
</script>

<style scoped>
.pc-page { padding-bottom: 34px; }
.pc-wrap { padding: 10px 16px 0; }
/* 身份卡 */
.identity {
  display: flex;
  align-items: center;
  gap: 13px;
  padding: 16px;
}
.id-avatar {
  width: 54px;
  height: 54px;
  flex: none;
  border-radius: 50%;
  background: var(--ink);
  color: var(--paper);
  font-size: 23px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}
.id-main { flex: 1; min-width: 0; }
.id-name {
  font-size: 20px;
  font-weight: 800;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.id-badges {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 5px;
  flex-wrap: wrap;
}
.wl-chip {
  padding: 2px 8px;
  border-radius: 5px;
  background: var(--au-primary, #016a71);
  color: #fff;
  font-size: 11.5px;
  font-weight: 700;
}
.wl-title { font-size: 14px; font-weight: 700; }
.wl-serial {
  font-size: 14px;
  font-weight: 800;
  color: var(--au-primary, #016a71);
  font-variant-numeric: tabular-nums;
}
.realm-pill {
  flex: none;
  padding: 4px 11px;
  border: 1.5px solid var(--au-primary, #016a71);
  border-radius: var(--paper-r-full, 999px);
  color: var(--au-primary, #016a71);
  font-size: 12.5px;
  font-weight: 700;
}
/* 状态总览 */
.pc-ov { padding: 0; overflow: hidden; margin-top: 14px; }
.ov-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
}
.ov-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  align-items: flex-start;
  padding: 13px 14px;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--ink);
  font: inherit;
  text-align: left;
  min-width: 0;
}
.ov-cell:nth-child(odd) { border-right: 1px solid var(--line); }
.ov-cell:nth-child(n + 3) { border-top: 1px solid var(--line); }
.ov-v { font-size: 17px; font-weight: 800; font-variant-numeric: tabular-nums; }
.ov-v.teal { color: var(--au-primary, #016a71); }
.ov-l {
  font-size: 11.5px;
  color: var(--ink2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}
/* 分组 */
.pc-group { margin-top: 22px; }
.pc-gtitle {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 2px 10px;
  font-size: 14.5px;
  font-weight: 700;
  color: var(--ink);
}
.pc-gtitle::after {
  content: '';
  flex: 1;
  border-top: 1px solid var(--line-deep);
}
.pc-rows-card { padding: 0; overflow: hidden; }
.pc-rows-card > * + * { border-top: 1px solid var(--line); }
.pc-expand-below { margin-top: 10px; }
.pc-lines-card { padding: 6px 14px; margin-top: 10px; }
.line-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 0;
}
.line-row + .line-row { border-top: 1px solid var(--line); }
.line-main {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  flex: 1;
}
.line-name { font-size: 14px; font-weight: 600; color: var(--ink); }
.line-url {
  font-size: 12px;
  color: var(--ink2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.line-note {
  margin: 0;
  padding: 8px 0 10px;
  border-top: 1px solid var(--line);
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink2);
}
.v4-mini-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
  padding: 8px 12px;
  background: var(--paper2);
  border: 1px solid var(--line);
  border-radius: var(--paper-r-full, 999px);
  color: var(--ink2);
  font-size: 12px;
  cursor: pointer;
}
@media (min-width: 1024px) {
  .v4-page.pc-page { max-width: min(1120px, 94vw); }
  .pc-wrap { padding: 26px 32px 48px; }
  .pc-cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0 26px;
    align-items: start;
  }
  .pc-cols .pc-group:first-child { margin-top: 22px; }
  .ov-grid { display: flex; }
  .ov-cell { flex: 1; border-top: none !important; }
  .ov-cell + .ov-cell { border-left: 1px solid var(--line); }
  .ov-cell:nth-child(odd) { border-right: none; }
}
</style>
