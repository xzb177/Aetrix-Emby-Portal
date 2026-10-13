<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">营地门口 · 组队处</span>
        <h1>🤝 结伴同行</h1>
        <p>在营地门口举起牌子 —— 组队探险啦！</p>
        <div v-if="info && !disabled" class="v4-fire">
          🎒 已组队 <b>{{ info.invited_count ?? 0 }}</b> 人
          <span v-if="totalRebate > 0"> · 累计带路费 +{{ totalRebate }}⚡</span>
        </div>
        <div v-else-if="loading" class="v4-skeleton" style="height:32px;width:60%;margin-top:10px"></div>
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
        <section class="v4-sec">
          <div v-if="loading" class="v4-skeleton" style="height:190px"></div>
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
              >{{ copiedField === 'code' ? '✓ 已复制！' : '📋 一键复制暗号' }}</button>
              <div class="pole"></div>
            </div>
            <div style="height:34px"></div>
            <div class="linkrow">
              <input class="linkinput" :value="inviteLink" readonly aria-label="组队链接">
              <button class="linkbtn" :disabled="!inviteLink" @click="copyText(inviteLink, 'link')">
                {{ copiedField === 'link' ? '✓ 已复制' : '复制链接' }}
              </button>
            </div>
          </template>
        </section>

        <!-- 组队规则：数值全部来自后端 config -->
        <section v-if="!loading && info?.config && !disabled" class="v4-sec">
          <div class="rule">
            📜 <b>组队规则</b><br>
            朋友用你的暗号加入营地 —— 他得 <b>+{{ info.config.invitee_reward_points }}⚡</b> 见面礼，
            你得 <b>+{{ info.config.reward_points }}⚡</b> 带路费；
            他每次充能量，你再拿 <b>{{ info.config.rebate_percent }}%</b> 回扣。<br>
            暗号终身有效，多带多得。
          </div>
        </section>

        <!-- 已组队的伙伴 -->
        <section v-if="!loading" class="v4-sec">
          <div class="v4-sec-title">🎒 已组队 · {{ records.length }} 人</div>
          <div class="v4-sec-sub">跟着你一起出发的伙伴</div>
          <div v-if="!records.length" class="v4-empty">
            还没有伙伴同行<br>把暗号喊出去，第一个朋友在等你
          </div>
          <div v-else class="mates">
            <div v-for="r in records" :key="r.id" class="mate">
              <div class="av">{{ avatarOf(r.invitee_username) }}</div>
              <div class="mid">
                <div class="nm">{{ r.invitee_username }}</div>
                <div class="dt">{{ fmtDate(r.created_at) }} 加入营地</div>
              </div>
              <div class="rw">+{{ r.reward_points }} ⚡</div>
            </div>
          </div>
        </section>

        <!-- 带路费账本 -->
        <section v-if="!loading" class="v4-sec">
          <div class="v4-sec-title">💰 带路费账本</div>
          <div class="v4-sec-sub">伙伴充能量，你的回扣自动记在这里 · 累计 +{{ totalRebate }}⚡</div>
          <div v-if="!rebates.length" class="v4-empty">
            账本还是空的<br>伙伴充能量后，回扣会自动记一笔
          </div>
          <div v-else class="mates">
            <div v-for="r in rebates" :key="r.id" class="mate">
              <div class="av dot"></div>
              <div class="mid">
                <div class="nm">{{ r.description || '充能量回扣' }}</div>
                <div class="dt">{{ fmtTime(r.created_at) }}</div>
              </div>
              <div class="rw">+{{ r.amount }} ⚡</div>
            </div>
          </div>
        </section>

        <!-- 见面礼记录 -->
        <section v-if="!loading" class="v4-sec">
          <div class="v4-sec-title">🎁 见面礼记录</div>
          <div class="v4-sec-sub">每带一位伙伴，营地当场发的谢礼</div>
          <div v-if="!promotions.length" class="v4-empty">
            {{ promoEnabled ? '带上第一位伙伴，谢礼会记在这里' : '见面礼当前未开启；开启后，带人成功会在这里记一笔' }}
          </div>
          <div v-else class="mates">
            <div v-for="r in promotions" :key="r.id" class="mate">
              <div class="av dot gold"></div>
              <div class="mid">
                <div class="nm">带上了 {{ r.invitee_username }}</div>
                <div class="dt">{{ r.reward_type_label }} · {{ fmtTime(r.created_at) }}</div>
              </div>
              <div class="rw">+{{ r.reward_value }}{{ r.reward_type === 'days' ? ' 天' : ' ⚡' }}</div>
            </div>
          </div>
        </section>
      </template>
    </div>
    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
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
const rebates = ref<RebateRow[]>([])
const totalRebate = ref(0)
const promotions = ref<PromotionRewardRow[]>([])
const promoConfig = ref<MyPromotions['config'] | null>(null)
const copiedField = ref('')

const inviteLink = computed(() => {
  if (!info.value?.code) return ''
  return `${window.location.origin}/login?invite=${info.value.code}`
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
  // "2026-10-10T…" → "10-10"
  return iso.slice(5, 10)
}
function fmtTime(iso?: string | null): string {
  if (!iso) return '—'
  return iso.slice(0, 16).replace('T', ' ')
}

let copyTimer: number | undefined
async function copyText(text: string, field: string) {
  const ok = await copy(text, field === 'link' ? '组队链接' : '组队暗号')
  if (!ok) return
  copiedField.value = field
  window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => { if (copiedField.value === field) copiedField.value = '' }, 1800)
}
onUnmounted(() => window.clearTimeout(copyTimer))

async function load() {
  loading.value = true
  failed.value = false
  try {
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
    const [my, rec, reb, pro] = await Promise.all([
      inviteApi.myCode(),
      inviteApi.records({ limit: 50 }).catch(() => emptyRecords),
      inviteApi.rebates({ limit: 50 }).catch(() => emptyRebates),
      inviteApi.promotions({ limit: 50 }).catch(() => emptyPromotions),
    ])
    info.value = my
    records.value = rec.records
    rebates.value = reb.rebates
    totalRebate.value = reb.total_rebate
    promotions.value = pro.rewards
    promoConfig.value = pro.config
  } catch {
    failed.value = true
  } finally {
    loading.value = false
  }
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
}
.board .shout {
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 2px;
  color: var(--red);
  margin-bottom: 6px;
}
.board .code {
  font-size: 32px;
  font-weight: 800;
  letter-spacing: 4px;
  background: #FFFDF6;
  border: 2px dashed var(--ink2);
  border-radius: 10px;
  padding: 14px 8px;
  margin: 10px 0;
  font-family: "Courier New", monospace;
  overflow-wrap: anywhere;
}
.copybtn {
  width: 100%;
  background: var(--red);
  color: #fff;
  border: none;
  font-size: 15px;
  font-weight: 800;
  padding: 13px;
  border-radius: 10px;
  cursor: pointer;
  box-shadow: 0 3px 0 #a33319;
}
.copybtn:active { transform: scale(0.96); }
.copybtn.done { background: var(--green); box-shadow: 0 3px 0 #35593f; }
.copybtn:disabled { background: var(--ink2); box-shadow: none; cursor: default; }
.board.off { padding: 24px 18px; }
.off-note { font-size: 13px; color: var(--ink2); line-height: 1.8; margin: 8px 0 0; }

.linkrow { display: flex; gap: 8px; }
.linkinput {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--ink2);
  background: #FFFDF6;
  border: 1.5px solid var(--line);
  border-radius: 10px;
  padding: 10px 12px;
}
.linkbtn {
  flex-shrink: 0;
  min-height: 44px;
  background: var(--paper2);
  border: 1.5px solid var(--ink);
  border-radius: 10px;
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}
.linkbtn:active { transform: scale(0.96); }
.linkbtn:disabled { opacity: 0.5; cursor: default; }

/* 规则卡：设计稿原值 */
.rule {
  background: #FFFDF6;
  border: 1.5px solid var(--line);
  border-radius: 10px;
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
  background: #FFFDF6;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px 14px;
}
.mate .av {
  width: 42px;
  height: 42px;
  border-radius: 50%;
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
</style>
