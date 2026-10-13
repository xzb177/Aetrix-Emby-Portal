<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ facilityStamp('wishlist') }}</span>
        <h1>🏮 许愿池</h1>
        <p>营地中央的古井 —— 把心愿写在纸上，折成小船，投进去。</p>
      </header>

      <!-- 古井 -->
      <section class="v4-sec">
        <div class="well-wrap">
          <div ref="wellRef" class="well" aria-hidden="true">
            <div class="moon">🌙</div>
            <div class="ripple r1"></div>
            <div class="ripple r2"></div>
            <div class="ripple r3"></div>
            <div class="boat-float" style="left:52px;top:88px">⛵</div>
            <div class="boat-float" style="left:130px;top:120px;animation-delay:1.3s;font-size:20px">⛵</div>
          </div>
          <div class="well-note">
            池里已有 <b>{{ totalWishes }}</b> 条心愿在漂流
          </div>
        </div>
        <button class="wish-btn" @click="openModal">🏮 投下心愿</button>
      </section>

      <!-- 我的许愿 -->
      <section class="v4-sec">
        <div class="v4-sec-title">📜 我的许愿</div>
        <div class="v4-sec-sub">漂流中的心愿，营地正在想办法捞起来</div>
        <div v-if="myLoading" class="v4-skeleton" style="height:80px"></div>
        <div v-else-if="myError" class="v4-err">许愿加载失败：{{ myError }}<br><button class="v4-retry" @click="loadMine">重试</button></div>
        <div v-else-if="myWishes.length === 0" class="v4-empty">池子里还没有你的心愿，投一条下去吧 🏮</div>
        <div v-else>
          <div
            v-for="w in myWishes"
            :key="w.id"
            class="wish"
            :class="{ new: w.id === justAddedId }"
          >
            <div>
              <b>《{{ w.movie_name }}》</b>
              <div class="wmeta">
                {{ shortDate(w.created_at) }} 投下
                <span v-if="w.status === 'completed' && w.emby_item_id"> · 营地已上架，去播放器看</span>
                <span v-else-if="w.vote_count"> · 已有 {{ w.vote_count }} 人附议</span>
              </div>
            </div>
            <span class="chip" :class="statusChip(w.status)">{{ statusText(w.status) }}</span>
          </div>
        </div>
      </section>

      <!-- 众人许愿 -->
      <section class="v4-sec" style="padding-bottom:20px">
        <div class="v4-sec-title">👥 众人许愿</div>
        <div class="v4-sec-sub">附议多的心愿，优先被捞起来</div>
        <div v-if="hotLoading" class="v4-skeleton" style="height:80px"></div>
        <div v-else-if="hotError" class="v4-err">众人许愿加载失败：{{ hotError }}<br><button class="v4-retry" @click="loadHot">重试</button></div>
        <div v-else-if="hotWishes.length === 0" class="v4-empty">大家的心愿都在漂流，暂时没有公开的</div>
        <div v-else>
          <div v-for="w in hotWishes" :key="w.id" class="crowd">
            <div class="row1">
              <b>《{{ w.movie_name }}》</b>
              <span class="chip blue">{{ w.vote_count > 0 ? w.vote_count + ' 人也想看' : '还没有人附议' }}</span>
            </div>
            <div class="want">{{ shortDate(w.created_at) }} 投入池中</div>
            <button
              class="upvote"
              :class="{ done: w.voted }"
              :disabled="w.voted || voting === w.id"
              @click="upvote(w)"
            >
              <span class="lbl">{{ w.voted ? '✓ 已附议，坐等打捞' : voting === w.id ? '附议中' : '+1 附议' }}</span>
            </button>
          </div>
        </div>
      </section>
    </div>
    <PanelTabbar />

    <!-- 许愿弹窗 -->
    <div class="modal" :class="{ show: modalOpen }" role="dialog" aria-modal="true" aria-label="许愿" @click.self="closeModal" @keydown="onModalKeydown">
      <div class="mcard">
        <h3>✍️ 写下你的心愿</h3>
        <p>写下片名，纸条会折成小船，噗通一声投进池子</p>
        <input
          ref="wishInput"
          v-model="wishText"
          placeholder="比如：《深海之间》"
          maxlength="30"
          @keyup.enter="throwWish"
          @input="emptyWarn = false"
        >
        <div v-if="emptyWarn" class="wish-warn">先写下心愿，再投进池子吧 ✍️</div>
        <div class="mrow">
          <button class="mbtn cancel" @click="closeModal">再想想</button>
          <button class="mbtn go" :disabled="throwing" @click="throwWish">
            {{ throwing ? '投放中' : '⛵ 投进池子' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { facilityStamp } from '@/config/game'
import { mediaSeekApi, type MediaSeekRequest, type HotMediaSeekRequest } from '@/api'
import { useToast } from '@/composables/useToast'
import { humanizeError } from '@/composables/usePanelApi'

const myWishes = ref<MediaSeekRequest[]>([])
const hotWishes = ref<HotMediaSeekRequest[]>([])
const myLoading = ref(true)
const hotLoading = ref(true)
const myError = ref('')
const hotError = ref('')

const modalOpen = ref(false)
const wishText = ref('')
const emptyWarn = ref(false)
const throwing = ref(false)
const voting = ref<number | null>(null)
const justAddedId = ref<number | null>(null)
const wellRef = ref<HTMLElement | null>(null)
const wishInput = ref<HTMLInputElement | null>(null)

// hot 接口返回"所有人 pending（含自己）"，与 myWishes 按 id 去重，避免自己的心愿被算两次
const totalWishes = computed(() => {
  const ids = new Set<string | number>()
  myWishes.value.forEach((w) => ids.add(w.id))
  hotWishes.value.forEach((w) => ids.add(w.id))
  return ids.size
})

function statusText(s: string): string {
  if (s === 'completed') return '已捞起'
  if (s === 'approved') return '打捞中'
  if (s === 'rejected') return '已退回'
  return '漂流中'
}
function statusChip(s: string): string {
  if (s === 'completed') return 'green'
  if (s === 'rejected') return 'gray'
  return 'blue'
}
function shortDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${d.getMonth() + 1}-${d.getDate()}`
}
function onEsc(e: KeyboardEvent) {
  if (e.key === 'Escape' && modalOpen.value) closeModal()
}
/** R27：简易 focus trap —— Tab 在对话框内循环，不跑出对话框（已有 Esc + 自动聚焦） */
function onModalKeydown(e: KeyboardEvent) {
  if (e.key !== 'Tab' || !modalOpen.value) return
  const card = (e.currentTarget as HTMLElement).querySelector('.mcard')
  if (!card) return
  const els = Array.from(
    card.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'),
  ).filter((el) => el.offsetParent !== null)
  if (els.length === 0) return
  const first = els[0]
  const last = els[els.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}
function openModal() {
  modalOpen.value = true
  window.addEventListener('keydown', onEsc)
  nextTick(() => wishInput.value?.focus())
}
function closeModal() {
  modalOpen.value = false
  wishText.value = ''
  emptyWarn.value = false
  window.removeEventListener('keydown', onEsc)
}
onUnmounted(() => window.removeEventListener('keydown', onEsc))

async function loadMine() {
  myLoading.value = true
  myError.value = ''
  try {
    const r = await mediaSeekApi.getMyRequests()
    myWishes.value = r.requests || []
  } catch (e) {
    myError.value = humanizeError(e)
  } finally {
    myLoading.value = false
  }
}
async function loadHot() {
  hotLoading.value = true
  hotError.value = ''
  try {
    const r = await mediaSeekApi.hot()
    hotWishes.value = r.requests || []
  } catch (e) {
    hotError.value = humanizeError(e)
  } finally {
    hotLoading.value = false
  }
}

async function throwWish() {
  const name = wishText.value.trim()
  if (!name) { emptyWarn.value = true; return }
  if (throwing.value) return
  throwing.value = true
  try {
    const r = await mediaSeekApi.create({ movie_name: name })
    closeModal()
    flyBoat(() => {
      // 动画结束后刷新列表并高亮新愿望
      justAddedId.value = r.request_id
      loadMine()
      window.setTimeout(() => { justAddedId.value = null }, 1700)
    })
  } catch (e) {
    // 投放失败走 toast，不污染"我的心愿"列表的 myError
    useToast().error(humanizeError(e))
  } finally {
    throwing.value = false
  }
}

/** 纸船飞入古井动画（设计稿原交互） */
function flyBoat(done: () => void) {
  // 减少动态偏好：跳过飞行动画，直接完成
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { done(); return }
  const well = wellRef.value
  if (!well) { done(); return }
  const wr = well.getBoundingClientRect()
  const boat = document.createElement('div')
  boat.className = 'flyboat'
  boat.textContent = '⛵'
  boat.style.left = `${wr.left + wr.width / 2 - 17}px`
  boat.style.top = `${wr.top - 70}px`
  document.body.appendChild(boat)
  requestAnimationFrame(() => {
    boat.style.transition = 'transform .85s cubic-bezier(.5,-.1,.7,.5)'
    boat.style.transform = `translateY(${wr.height / 2 + 53}px)`
  })
  window.setTimeout(() => {
    boat.style.transition = 'opacity .25s'
    boat.style.opacity = '0'
    const sp = document.createElement('div')
    sp.className = 'splash'
    sp.textContent = '噗通！'
    sp.style.left = `${wr.left + wr.width / 2 - 22}px`
    sp.style.top = `${wr.top + wr.height / 2 - 40}px`
    document.body.appendChild(sp)
    window.setTimeout(() => { sp.remove(); boat.remove(); done() }, 1150)
  }, 900)
}

async function upvote(w: HotMediaSeekRequest) {
  if (w.voted || voting.value) return
  voting.value = w.id
  try {
    const r = await mediaSeekApi.vote(w.id)
    w.voted = r.voted
    w.vote_count = r.vote_count
  } catch {
    // 附议失败不再静默：toast 提醒，按钮保持可点
    useToast().error('附议失败，再试一次')
  } finally {
    voting.value = null
  }
}

onMounted(() => {
  loadMine()
  loadHot()
})
</script>

<style scoped>
/* 古井：设计稿 game-wishlist.html 原值 */
.well-wrap { text-align: center; margin-bottom: 14px; }
.well {
  width: 220px;
  height: 220px;
  margin: 0 auto;
  position: relative;
  border-radius: 50%;
  background: radial-gradient(circle, #7FA8C9 0%, #4A7CA8 55%, #2C5378 100%);
  border: 14px solid #B9A87E;
  outline: 3px solid var(--ink);
  box-shadow: inset 0 0 40px rgba(0, 0, 0, 0.35), 0 8px 20px rgba(0, 0, 0, 0.15);
  overflow: hidden;
}
.well::before {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background: radial-gradient(circle at 35% 30%, rgba(255, 255, 255, 0.35), transparent 45%);
}
.moon {
  position: absolute;
  top: 34px;
  left: 0;
  right: 0;
  margin: 0 auto;
  width: fit-content;
  font-size: 26px;
  opacity: 0.85;
  filter: drop-shadow(0 0 8px #FFE9A8);
}
.ripple {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 200px;
  height: 200px;
  border: 2px solid rgba(255, 255, 255, 0.55);
  border-radius: 50%;
  transform: translate(-50%, -50%) scale(0.2);
  animation: rip 3.2s infinite;
  pointer-events: none;
}
/* 减少动态偏好：涟漪纯属装饰，直接隐藏（基态 transform 会被全局置空导致错位） */
@media (prefers-reduced-motion: reduce) {
  .ripple { display: none; }
}
.r2 { animation-delay: 1.05s; }
.r3 { animation-delay: 2.1s; }
/* 只动 transform/opacity：不刷 layout，60fps */
@keyframes rip {
  0% { transform: translate(-50%, -50%) scale(0.2); opacity: 0.9; }
  100% { transform: translate(-50%, -50%) scale(1); opacity: 0; }
}
.well .boat-float {
  position: absolute;
  font-size: 24px;
  animation: float 4s ease-in-out infinite;
}
@keyframes float {
  0%, 100% { transform: translateY(0) rotate(-6deg); }
  50% { transform: translateY(-7px) rotate(6deg); }
}
.well-note { font-size: 12px; color: var(--ink2); margin-top: 10px; }
.well-note b { color: var(--red); }

/* 大按钮：设计稿原值 */
.wish-btn {
  display: block;
  width: 100%;
  background: var(--red);
  color: var(--paper-card);
  border: none;
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 3px;
  padding: 15px;
  border-radius: 14px;
  cursor: pointer;
  box-shadow: 0 4px 0 #a33319;
  transition: transform 0.12s;
}
.wish-btn:active { transform: translateY(3px); box-shadow: 0 1px 0 #a33319; }

/* 纸船飞 + 噗通 */
.flyboat {
  position: fixed;
  font-size: 34px;
  z-index: 99;
  pointer-events: none;
  filter: drop-shadow(0 4px 6px rgba(0, 0, 0, 0.3));
}
.splash {
  position: fixed;
  font-size: 20px;
  font-weight: 800;
  color: var(--red);
  z-index: 99;
  pointer-events: none;
  animation: pop 1.2s forwards;
}
@keyframes pop {
  0% { transform: scale(0.4); opacity: 0; }
  25% { transform: scale(1.25); opacity: 1; }
  100% { transform: scale(1) translateY(-18px); opacity: 0; }
}

/* 愿望列表 */
.wish {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 12px;
  padding: 13px 14px;
  margin-bottom: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.wish.new { animation: glow 1.6s; border-color: var(--gold); }
@keyframes glow {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}
.wish > div:first-child { min-width: 0; flex: 1; }
.wish b { font-size: 14px; display: block; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wmeta { font-size: 11px; color: var(--ink2); margin-top: 4px; }
.chip {
  font-size: 11px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 20px;
  white-space: nowrap;
}
.chip.blue { background: #E3ECF8; color: var(--blue); border: 1px solid var(--blue); }
.chip.green { background: #E8F0E9; color: var(--green); border: 1px solid var(--green); }
.chip.gray { background: var(--paper2); color: var(--ink2); border: 1px solid var(--line); }
.crowd {
  background: var(--paper2);
  border: 1.5px dashed var(--ink2);
  border-radius: 12px;
  padding: 13px 14px;
  margin-bottom: 10px;
}
.crowd .row1 { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.crowd .row1 > :first-child { min-width: 0; flex: 1; }
.crowd b { font-size: 14px; display: block; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.crowd .want { font-size: 11px; color: var(--ink2); margin-top: 6px; }
.upvote {
  margin-top: 10px;
  width: 100%;
  position: relative;
  /* 透明 padding + 负 margin：视觉按钮不变，点击区纵向扩大到 44px */
  background: transparent;
  border: none;
  color: var(--paper-card);
  font-size: 13px;
  font-weight: 700;
  padding: 14px 9px;
  margin: 5px 0 -5px;
  cursor: pointer;
}
.upvote::before {
  content: "";
  position: absolute;
  top: 5px; right: 0; bottom: 5px; left: 0;
  background: var(--ink);
  border-radius: 10px;
}
.upvote .lbl { position: relative; }
.upvote.done::before { background: var(--green); }
.upvote:disabled { cursor: default; }

/* 弹窗 */
.modal {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: none;
  align-items: center;
  justify-content: center;
  background: rgba(42, 38, 32, 0.45);
  padding: 0 24px;
}
.modal.show { display: flex; }
.mcard {
  background: var(--paper-card);
  border: 2px solid var(--ink);
  border-radius: 14px;
  padding: 22px 20px;
  width: 100%;
  animation: up 0.25s;
}
@keyframes up { from { transform: translateY(24px); opacity: 0; } }
.mcard h3 { font-size: 17px; font-weight: 800; margin: 0 0 4px; }
.mcard p { font-size: 12px; color: var(--ink2); margin: 0 0 14px; }
.mcard input {
  width: 100%;
  font-size: 16px;
  padding: 12px;
  border: 1.5px solid var(--line);
  border-radius: 10px;
  background: var(--paper);
  outline: none;
  color: var(--ink);
}
.mcard input:focus { border-color: var(--gold); }
.wish-warn { font-size: 12px; color: var(--red); margin-top: 8px; }
.mrow { display: flex; gap: 10px; margin-top: 16px; }
.mbtn {
  flex: 1;
  min-height: 44px;
  padding: 12px;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 800;
  cursor: pointer;
  border: none;
}
.mbtn.cancel { background: var(--paper2); color: var(--ink2); }
.mbtn.go { background: var(--red); color: #fff; box-shadow: 0 3px 0 #a33319; }
.mbtn.go:disabled { background: var(--ink2); box-shadow: none; }

/* R27：减少动态偏好下装饰动画停掉（本地守卫） */
@media (prefers-reduced-motion: reduce) {
  .well .boat-float { animation: none; }
}
</style>
