<template>
  <div class="v4-scope">
  <div class="v4-page settings-page">
    <div class="set-wrap">
      <header class="set-head">
        <button type="button" class="set-back" aria-label="返回我的" @click="router.push('/game/account')">
          <ChevronLeft :size="19" />
        </button>
        <span class="set-title">账号设置</span>
      </header>

      <div class="set-cols">
        <div class="set-col">
          <!-- 安全 -->
          <section class="pc-group">
            <div class="pc-gtitle">安全</div>
            <p class="pc-gsub">发现不是自己登的，先改密码再清设备</p>
            <div class="v4-card pc-rows-card">
              <PcEntryRow :icon="Lock" name="修改登录密码" @click="pwdOpen = true" />
              <DevicesEntry />
              <LoginHistoryEntry />
            </div>
          </section>

          <!-- 绑定：providers 门控结论由面板回报，未确认前整组不显示（v-show 保挂载，面板要先加载才能回报） -->
          <section v-show="driveGroupVisible" id="my-drive" class="pc-group">
            <div class="pc-gtitle">绑定</div>
            <p class="pc-gsub">绑了自己的盘，播放走你自己的额度，不走共享池</p>
            <div class="v4-card pc-rows-card">
              <DriveBindingsPanel ref="drivePanel" @loaded="onDriveLoaded" />
            </div>
          </section>
        </div>

        <div class="set-col">
          <!-- 展示与隐私 -->
          <section class="pc-group">
            <div class="pc-gtitle">展示与隐私</div>
            <p class="pc-gsub">发现页和榜单怎么展示，你说了算</p>
            <div class="v4-card pc-rows-card">
              <CardPrefsEntry />
              <div class="pc-row-static">
                <span class="pc-row-ic" aria-hidden="true"><Eye :size="17" /></span>
                <span class="pc-row-name">观影碑时长榜上榜</span>
                <button
                  type="button"
                  class="srv-switch"
                  :class="{ 'is-on': showOnLeaderboard }"
                  :disabled="privacyLoading"
                  role="switch"
                  :aria-checked="showOnLeaderboard"
                  aria-label="观影碑时长榜上榜"
                  @click="toggleLeaderboard"
                ></button>
              </div>
              <p class="pref-note">打开后，你的观影时长会出现在世界观影碑的时长榜上（只显示昵称）；关掉则不上榜，但你自己仍能看到自己的排名。</p>
              <p v-if="privacyError" class="pref-err">{{ privacyError }}<button class="v4-retry" @click="loadPrivacy">重试</button></p>
            </div>
          </section>

          <!-- 账号 -->
          <section class="pc-group">
            <div class="pc-gtitle">账号</div>
            <div class="v4-card pc-rows-card">
              <PcEntryRow :icon="LogOut" name="退出登录" danger @click="handleLogout" />
            </div>
          </section>
        </div>
      </div>
    </div>

    <ChangePasswordModal v-model:open="pwdOpen" />
  </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 账号设置页（/game/account/settings）：个人中心重做后设置类功能的家。
 * 四组——安全（改密码 / 我的设备 / 登录记录）、绑定（谷歌盘管理 + 已
 * 下线绑定解绑）、展示与隐私（发现页卡片展示 / 观影碑上榜开关）、账
 * 号（退出登录）。各块逻辑都在 components/profile/ 的对应组件里（自旧
 * 账号页原样抽出，行为口径不变）；本页只留上榜开关与退出登录。
 * 带 #my-drive 锚点进来（旧深链 / 播放器方案弹窗）时，等绑定组显示后
 * 自动展开谷歌盘并滚到位。
 */
import { nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ChevronLeft, Eye, Lock, LogOut } from 'lucide-vue-next'
import PcEntryRow from '@/components/profile/PcEntryRow.vue'
import ChangePasswordModal from '@/components/profile/ChangePasswordModal.vue'
import DevicesEntry from '@/components/profile/DevicesEntry.vue'
import LoginHistoryEntry from '@/components/profile/LoginHistoryEntry.vue'
import DriveBindingsPanel from '@/components/profile/DriveBindingsPanel.vue'
import CardPrefsEntry from '@/components/profile/CardPrefsEntry.vue'
import { panelApi, humanizeError } from '@/composables/usePanelApi'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const toast = useToast()

const pwdOpen = ref(false)

// ── 绑定组门控（providers 结论由 DriveBindingsPanel 回报） ──
const drivePanel = ref<InstanceType<typeof DriveBindingsPanel> | null>(null)
const driveGroupVisible = ref(false)
function onDriveLoaded(payload: { visible: boolean }) {
  driveGroupVisible.value = payload.visible
  if (payload.visible && route.hash === '#my-drive') {
    void nextTick(() => {
      drivePanel.value?.expandDrive()
      document.getElementById('my-drive')?.scrollIntoView({ block: 'start' })
    })
  }
}

// ── 观影碑上榜开关（自旧页原样迁入） ──
const showOnLeaderboard = ref(true)
const privacyLoading = ref(false)
const privacyError = ref('')

async function loadPrivacy() {
  privacyLoading.value = true
  privacyError.value = ''
  try {
    const d = await panelApi.privacyGet()
    showOnLeaderboard.value = d.show_on_leaderboard !== false
  } catch (e) {
    privacyError.value = humanizeError(e)
  } finally {
    privacyLoading.value = false
  }
}
async function toggleLeaderboard() {
  if (privacyLoading.value) return
  const next = !showOnLeaderboard.value
  privacyLoading.value = true
  try {
    const d = await panelApi.privacySet(next)
    showOnLeaderboard.value = d.show_on_leaderboard !== false
    toast.success(next ? '已允许上榜' : '已关掉上榜')
  } catch (e) {
    toast.error(humanizeError(e))
  } finally {
    privacyLoading.value = false
  }
}

/** 退出登录：与顶栏头像菜单同款动作——清登录态后回登录页，不另写一套 */
async function handleLogout() {
  await userStore.logout()
  router.push('/login')
}

onMounted(() => {
  void loadPrivacy()
})
</script>

<style scoped>
.settings-page { padding-bottom: 34px; }
.set-wrap { padding: 16px 16px 0; }
.set-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 0 6px;
}
.set-back {
  width: 38px;
  height: 38px;
  border: 1.5px solid var(--ink);
  border-radius: 10px;
  background: var(--paper-card);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: var(--ink);
  flex: none;
}
.set-title { font-size: 19px; font-weight: 800; }
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
.pc-gsub {
  margin: -4px 2px 10px;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--ink2);
}
.pc-rows-card { padding: 0; overflow: hidden; }
.pc-rows-card > * + * { border-top: 1px solid var(--line); }
.pc-row-static {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12.5px 14px;
}
.pc-row-ic {
  width: 31px;
  height: 31px;
  flex: none;
  border: 1.5px solid var(--ink);
  border-radius: 8px;
  background: var(--paper);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ink);
}
.pc-row-name { font-size: 14.5px; font-weight: 600; white-space: nowrap; }
/* 观影碑开关（自旧页 .srv-switch 原样迁入） */
.srv-switch {
  position: relative;
  flex: none;
  width: 48px;
  height: 44px;
  padding: 0;
  margin-left: auto;
  border-radius: 999px;
  border: 0;
  background: none;
  cursor: pointer;
}
.srv-switch:disabled { opacity: 0.5; cursor: not-allowed; }
.srv-switch::before {
  content: '';
  position: absolute;
  left: 6px;
  top: 12px;
  width: 36px;
  height: 20px;
  border-radius: 999px;
  border: 1.5px solid var(--paper-ink);
  background: var(--paper-card);
}
.srv-switch.is-on::before { background: var(--paper-primary); }
.srv-switch::after {
  content: '';
  position: absolute;
  top: 14px;
  left: 8px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--paper-card);
  border: 1.5px solid var(--paper-ink);
}
.srv-switch.is-on::after {
  left: auto;
  right: 8px;
  background: var(--paper-ink);
}
.pref-note {
  margin: 0;
  padding: 0 14px 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--ink2);
}
.pref-err {
  margin: 0;
  padding: 10px 14px;
  border-top: 1px solid var(--line);
  font-size: 12px;
  line-height: 1.6;
  color: var(--paper-ink-2);
}
@media (min-width: 1024px) {
  .v4-page.settings-page { max-width: min(1120px, 94vw); }
  .set-wrap { padding: 26px 32px 48px; }
  /* 桌面两栏（照效果图）：左安全+绑定，右展示与隐私+账号；手机单列同序 */
  .set-cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0 26px;
    align-items: start;
  }
}
</style>
