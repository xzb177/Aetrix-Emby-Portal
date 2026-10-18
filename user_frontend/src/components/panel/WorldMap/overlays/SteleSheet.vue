<template>
  <div class="sheet-stele">
          <div class="lb-head">
            <div class="lb-arch"><svg width="150" height="30" viewBox="0 0 150 30" fill="none" aria-hidden="true"><path d="M6 27 Q75 -14 144 27" stroke="#c97e2c" stroke-width="1.6"/><path d="M24 27 Q75 2 126 27" stroke="#d9cba6" stroke-width="1.3"/><circle cx="75" cy="7" r="10.5" fill="#FCF8EC"/><path d="M75 3 l2.5 5.4 5.9.7 -4.4 4 1.2 5.8 -5.2-3 -5.2 3 1.2-5.8 -4.4-4 5.9-.7 z" fill="#e8a84a" stroke="#c97e2c" stroke-width="1.1" stroke-linejoin="round"/></svg></div>
            <div class="lb-title">观影碑</div>
            <div class="lb-sub">排行榜 · <b>{{ lbPeriod === 'day' ? '今日' : '本周' }}</b> {{ lbPeriod === 'day' ? '00:00 起' : '周一 00:00 起' }}</div>
            <button type="button" class="w-sheet-close lb-close" @click="$emit('close')" aria-label="关闭">✕</button>
          </div>
          <div class="lb-tabs">
            <button type="button" class="lb-tab" :class="{ on: lbBoard === 'hot' }" @click="lbBoard = 'hot'"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="8" cy="8" r="6.6" stroke="currentColor" stroke-width="1.5"/><path d="M6.4 5.2 L11 8 L6.4 10.8 Z" fill="currentColor"/></svg>热播榜</button>
            <button type="button" class="lb-tab" :class="{ on: lbBoard === 'duration' }" @click="lbBoard = 'duration'"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4.5 2.5 h7 M4.5 13.5 h7 M5.5 2.5 c0 3.4 5 3.4 5 5.5 c0 2.1 -5 2.1 -5 5.5 M10.5 2.5 c0 3.4 -5 3.4 -5 5.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>观影时长榜</button>
          </div>
          <div class="lb-periods">
            <span class="lb-plabel">碑刻周期</span>
            <button type="button" class="lb-period" :class="{ on: lbPeriod === 'day' }" @click="lbPeriod = 'day'">日榜</button>
            <button type="button" class="lb-period" :class="{ on: lbPeriod === 'week' }" @click="lbPeriod = 'week'">周榜</button>
          </div>
          <p class="w-sheet-empty" v-if="lbLoading && lbRows.length === 0">正在抄榜…</p>
          <p class="w-sheet-empty" v-else-if="lbRows.length === 0">还没有人上榜，快去看片抢头名 🏆</p>
          <template v-else>
            <div class="lb-podium">
              <div v-for="slot in lbPodium" :key="slot.rank" class="lb-pod" :class="`r${slot.rank}`">
                <div class="lb-medal"><svg class="lb-crown" v-if="slot.rank === 1" width="26" height="16" viewBox="0 0 26 16" fill="none" aria-hidden="true"><path d="M2 14 L4 4 L9.5 9 L13 2.5 L16.5 9 L22 4 L24 14 Z" fill="#e8a84a" stroke="#c97e2c" stroke-width="1.3" stroke-linejoin="round"/></svg>{{ slot.rank }}<span class="lb-sealdot" v-if="slot.rank === 1">冠</span></div>
                <div class="lb-pname">{{ slot.title }}</div>
                <div class="lb-ptype" v-if="lbBoard === 'hot'">{{ slot.type }}</div>
                <div class="lb-pnum">{{ slot.num }} <small>{{ slot.unit }}</small></div>
                <div class="lb-plinth"></div>
              </div>
            </div>
            <div class="lb-divider"><i></i></div>
            <ul class="lb-rows">
              <li v-for="(r, i) in lbRest" :key="String(r.item_id ?? r.user_id ?? i)" class="lb-row" :class="{ me: lbIsMe(r) }">
                <span class="lb-rk">{{ i + 4 }}</span>
                <span class="lb-rname">{{ lbTitle(r) }}</span>
                <span class="lb-tag" v-if="lbBoard === 'hot'">{{ String(r.item_type ?? '') }}</span>
                <span class="lb-metag" v-if="lbIsMe(r)">我</span>
                <span class="lb-rnum">{{ lbNumText(r) }} <small>{{ lbUnitText() }}</small></span>
              </li>
            </ul>
          </template>
          <div class="lb-myrank">
            <div class="lb-stamp"><b>{{ lbStampTop }}</b><span>{{ lbStampBottom }}</span></div>
            <div class="lb-mtxt">{{ lbMyLead }}<b v-if="lbMyStrong">{{ lbMyStrong }}</b><template v-if="lbMyGap">，距上一名只差 <span class="gap">{{ lbMyGap }}</span></template>{{ lbMyTail }}</div>
          </div>
          <p class="lb-scope">{{ lbScopeLine }}</p>
          </div>
</template>


<script setup lang="ts">
import { computed } from 'vue'
import { useSteleTop } from '../composables/useSteleTop'

/**
 * 观影碑弹层：排行榜逻辑随 sheet 搬家。
 * index 经 useSteleTop 持有同一份 ref，sheet 内解包为模板绑定（读写直通，不复制状态）。
 */
const props = defineProps<{ stele: ReturnType<typeof useSteleTop> }>()
defineEmits<{ close: [] }>()

const s = props.stele
const lbBoard = computed({ get: () => s.lbBoard.value, set: (v) => { s.lbBoard.value = v } })
const lbPeriod = computed({ get: () => s.lbPeriod.value, set: (v) => { s.lbPeriod.value = v } })
const lbLoading = computed(() => s.lbLoading.value)
const lbRows = computed(() => s.lbRows.value)
const lbPodium = computed(() => s.lbPodium.value)
const lbRest = computed(() => s.lbRest.value)
const lbStampTop = computed(() => s.lbStampTop.value)
const lbStampBottom = computed(() => s.lbStampBottom.value)
const lbMyLead = computed(() => s.lbMyLead.value)
const lbMyStrong = computed(() => s.lbMyStrong.value)
const lbMyGap = computed(() => s.lbMyGap.value)
const lbMyTail = computed(() => s.lbMyTail.value)
const lbScopeLine = computed(() => s.lbScopeLine.value)
const lbIsMe = (r: Record<string, unknown>) => s.lbIsMe(r)
const lbTitle = (r: Record<string, unknown>) => s.lbTitle(r)
const lbNumText = (r: Record<string, unknown>) => s.lbNumText(r)
const lbUnitText = () => s.lbUnitText()
</script>

<style scoped>
.w-sheet-empty { font-size: 13.5px; color: var(--paper-ink-soft); padding: 12px 0; }
.w-sheet-foot { margin-top: 12px; font-size: 11px; color: var(--paper-ink-soft); text-align: center; letter-spacing: .08em; }

.lb-head { text-align: center; margin-bottom: 14px; position: relative; }

.lb-arch { display: flex; justify-content: center; margin-bottom: 4px; }

.lb-title {
  font-family: "Songti SC", "STSong", "SimSun", serif;
  font-size: 25px; font-weight: 600; letter-spacing: .34em; text-indent: .34em; color: var(--paper-ink-deep);
}
.lb-sub { margin-top: 4px; font-size: 12px; color: var(--paper-ink-soft); letter-spacing: .22em; }

.lb-sub b { color: var(--paper-teal); font-weight: 600; }

.lb-close { position: absolute; top: -4px; right: -2px; display: flex; align-items: center; justify-content: center; }

.lb-tabs { display: flex; gap: 10px; justify-content: center; margin-bottom: 10px; }

.lb-tab {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 8px 18px; border-radius: 999px;
  border: 1.4px solid var(--paper-hairline); background: var(--paper-card-soft);
  font-size: 14.5px; color: var(--paper-ink-soft); letter-spacing: .06em;
  font-family: inherit; cursor: pointer;
}
.lb-tab.on {
  background: linear-gradient(180deg, #FDF3D8, #FBEECB);
  border-color: var(--paper-teal); color: var(--paper-teal); font-weight: 600;
}
.lb-periods { display: flex; justify-content: flex-end; align-items: center; margin: -2px 2px 4px; }

.lb-plabel { font-size: 11.5px; color: var(--paper-ink-soft); letter-spacing: .18em; margin-right: 8px; }

.lb-period {
  font-size: 12.5px; padding: 4px 13px; color: var(--paper-ink-soft);
  border: 1.2px solid var(--paper-hairline); background: var(--paper-card-soft);
  font-family: inherit; cursor: pointer;
}
.lb-period:first-of-type { border-radius: 8px 0 0 8px; border-right: none; }

.lb-period:last-of-type { border-radius: 0 8px 8px 0; }

.lb-period.on { background: var(--paper-teal-deep); border-color: var(--paper-teal-deep); color: var(--paper-canvas); font-weight: 600; }

.lb-podium { display: flex; gap: 12px; align-items: flex-end; margin: 8px 0 2px; }

.lb-pod { flex: 1; text-align: center; min-width: 0; }

.lb-pod.r1 { order: 2; }

.lb-pod.r2 { order: 1; }

.lb-pod.r3 { order: 3; }

.lb-medal {
  position: relative; width: 52px; height: 52px; margin: 0 auto 6px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-family: "Songti SC", "STSong", serif; font-size: 21px; font-weight: 600;
  background: radial-gradient(circle at 34% 30%, var(--paper-card-soft), #F7EFDA);
}
.lb-pod.r1 .lb-medal {
  width: 62px; height: 62px; font-size: 26px; color: var(--paper-teal);
  border: 2.2px solid var(--paper-teal);
}
.lb-pod.r2 .lb-medal { color: var(--paper-ink-soft); border: 2px solid var(--paper-hairline); }

.lb-pod.r3 .lb-medal { color: var(--paper-teal-deep); border: 2px solid var(--paper-hairline); }

.lb-crown { position: absolute; top: -16px; left: 50%; transform: translateX(-50%); }

.lb-sealdot {
  position: absolute; right: -4px; bottom: -2px; width: 17px; height: 17px; border-radius: 5px;
  background: #D9482B; color: #FFF6E8; font-size: 10px; font-weight: 600;
  display: flex; align-items: center; justify-content: center; transform: rotate(-8deg);
}
.lb-pname {
  font-size: 15px; font-weight: 600; line-height: 1.3;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.lb-pod.r1 .lb-pname { font-size: 16.5px; }

.lb-ptype { font-size: 11px; color: var(--paper-ink-soft); margin-top: 2px; letter-spacing: .14em; }

.lb-pnum { margin-top: 3px; font-size: 13.5px; font-weight: 600; color: var(--paper-teal); }

.lb-pnum small { font-size: 11px; font-weight: 500; color: var(--paper-ink-soft); }

.lb-plinth {
  margin: 9px auto 0; height: 8px; border-radius: 5px 5px 2px 2px;
  background: linear-gradient(180deg, #d9cba6, #CBBd97); width: 76%;
}
.lb-pod.r1 .lb-plinth { width: 86%; height: 11px; }

.lb-divider { display: flex; align-items: center; gap: 10px; margin: 14px 4px 4px; }

.lb-divider::before, .lb-divider::after { content: ""; flex: 1; border-top: 1.4px dashed var(--paper-hairline); }

.lb-divider i { width: 7px; height: 7px; background: var(--paper-teal); transform: rotate(45deg); display: block; }

.lb-rows { list-style: none; margin: 0; padding: 0; }

.lb-row {
  display: flex; align-items: center; gap: 12px;
  padding: 9px 10px; border-bottom: 1px solid #E7DCC3;
}
.lb-row:last-child { border-bottom: none; }

.lb-rk {
  width: 26px; text-align: center; flex: none;
  font-family: "Songti SC", "STSong", serif; font-size: 16px; color: var(--paper-ink-soft); font-weight: 600;
}
.lb-rname { flex: 1; min-width: 0; font-size: 14.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.lb-tag {
  flex: none; font-size: 10.5px; color: var(--paper-ink-soft); letter-spacing: .1em;
  border: 1.1px solid var(--paper-hairline); border-radius: 999px; padding: 2px 8px; background: var(--paper-card-soft);
}
.lb-metag {
  flex: none; font-size: 10.5px; font-weight: 600; color: #fff; background: #7d9b76;
  border-radius: 5px; padding: 2px 6px; transform: rotate(-3deg);
}
.lb-rnum { flex: none; font-size: 14px; font-weight: 600; color: var(--paper-teal); }

.lb-rnum small { font-size: 11px; font-weight: 500; color: var(--paper-ink-soft); }

.lb-row.me {
  background: linear-gradient(90deg, rgba(147, 173, 139, .20), rgba(147, 173, 139, .07));
  border-radius: 10px; border-bottom-color: transparent;
}
.lb-myrank {
  margin-top: 14px; border: 1.4px dashed var(--paper-teal-deep); border-radius: 14px;
  background: linear-gradient(180deg, var(--paper-card-soft), #FBF3DC);
  padding: 11px 16px; display: flex; align-items: center; gap: 13px;
}
.lb-stamp {
  flex: none; width: 46px; height: 46px; border-radius: 50%;
  border: 1.8px solid #D9482B; color: #D9482B;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  transform: rotate(-7deg); background: rgba(217, 72, 43, .05);
}
.lb-stamp b { font-family: "Songti SC", "STSong", serif; font-size: 16px; line-height: 1; }

.lb-stamp span { font-size: 8.5px; letter-spacing: .2em; margin-top: 2px; }

.lb-mtxt { flex: 1; font-size: 13.5px; color: var(--paper-ink-soft); line-height: 1.55; }

.lb-mtxt b { color: var(--paper-ink-deep); font-size: 15px; }

.lb-mtxt .gap { color: var(--paper-teal); font-weight: 600; }

.lb-scope { margin-top: 10px; text-align: center; font-size: 11px; color: var(--paper-ink-soft); letter-spacing: .05em; }


@media (prefers-reduced-motion: reduce) {
  .sheet-stele * { animation: none !important; transition: none !important; }
}
</style>
