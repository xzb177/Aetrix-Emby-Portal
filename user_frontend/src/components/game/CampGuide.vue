<template>
  <!-- 新手引导卡：localStorage['aetrix_camp_onboarded_v1'] 为空且已登录才展示。
       跳过 / 跟逛都会写 key，此后不再出现。 -->
  <section v-if="visible" class="v4-card guide-card" aria-label="营地新手引导">
    <div class="guide-head">
      <span class="guide-char" aria-hidden="true">🧭</span>
      <div class="guide-titles">
        <div class="guide-title">向导带你逛营地</div>
        <div class="guide-sub">第一次来？三步就上手</div>
      </div>
    </div>
    <ol class="guide-steps">
      <li><b>先去报到</b>——{{ checkinTip }}</li>
      <li><b>领体力</b>——报到顺手把今天的体力拿上，看片不心慌。</li>
      <li><b>逛设施</b>——地图上的小帐篷都能点进去看看。</li>
    </ol>
    <div class="guide-btns">
      <button class="guide-go" @click="followGuide" aria-label="跟着向导逛逛，先去报到">
        跟着向导逛逛
      </button>
      <button class="guide-skip" @click="skipGuide" aria-label="跳过新手引导，我自己逛">
        我自己逛
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { gameFacilities } from '@/config/game'

/** 带版本号的 key：以后想重置引导，升版本号就行 */
const ONBOARDED_KEY = 'aetrix_camp_onboarded_v1'

const router = useRouter()
const userStore = useUserStore()

const visible = ref(false)

/** 报到设施的小白版介绍（guideTip），没配就 fallback desc */
const checkinTip = gameFacilities.find((f) => f.key === 'checkin')?.guideTip
  ?? gameFacilities.find((f) => f.key === 'checkin')?.desc
  ?? '每天来盖个章，顺手把体力领了。'

function markOnboarded() {
  try {
    localStorage.setItem(ONBOARDED_KEY, '1')
  } catch {
    /* 隐私模式写不进就每次都看一遍，不打扰功能 */
  }
}

function followGuide() {
  markOnboarded()
  visible.value = false
  router.push('/game/checkin')
}

function skipGuide() {
  markOnboarded()
  visible.value = false
}

onMounted(() => {
  let done = false
  try {
    done = !!localStorage.getItem(ONBOARDED_KEY)
  } catch {
    done = false
  }
  visible.value = !done && userStore.isLoggedIn
})
</script>

<style scoped>
.guide-card {
  margin: 14px 0 0;
  padding: 16px;
  border-style: dashed;
  border-width: 2px;
}
.guide-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.guide-char {
  font-size: 40px;
  line-height: 1;
  flex-shrink: 0;
  filter: drop-shadow(1px 2px 0 rgba(0, 0, 0, 0.12));
}
.guide-title {
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 1px;
}
.guide-sub {
  font-size: 12px;
  color: var(--ink2);
  margin-top: 2px;
}
.guide-steps {
  margin: 0 0 14px;
  padding-left: 22px;
  font-size: 13px;
  line-height: 2;
  color: var(--ink);
}
.guide-steps b {
  color: var(--red);
}
.guide-btns {
  display: flex;
  gap: 10px;
}
.guide-go,
.guide-skip {
  flex: 1;
  font-size: 14px;
  font-weight: 800;
  padding: 11px 0;
  border-radius: 12px;
  cursor: pointer;
  letter-spacing: 1px;
}
.guide-go {
  background: var(--red);
  color: #fff;
  border: none;
  box-shadow: 0 3px 0 #a33319;
}
.guide-go:active {
  transform: translateY(2px);
  box-shadow: none;
}
.guide-skip {
  background: var(--paper-card);
  color: var(--ink);
  border: 1.5px dashed var(--line);
}
.guide-go:focus-visible,
.guide-skip:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}
</style>
