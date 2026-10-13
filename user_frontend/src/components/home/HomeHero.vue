<script setup lang="ts">
import { Crown, Sparkles, ChevronRight } from 'lucide-vue-next'
import { RouterLink } from 'vue-router'

defineProps<{
  heroImage?: string
  heroImageFailed?: boolean
  hasLoaded?: boolean
  heroEyebrow?: string
  heroFeatureTitle?: string
  heroMeta?: string
  heroStatus?: string
  greeting?: string
  greetingParts?: { lead: string; tail: string }
  username?: string
  isMember?: boolean
  isFreeRealm?: boolean
  heroHref?: string
}>()

const emit = defineEmits<{
  (e: 'image-error'): void
}>()
</script>

<template>
  <section class="hero" :class="{ 'is-ready': hasLoaded }">
    <img
      v-if="heroImage && !heroImageFailed"
      class="hero-backdrop"
      :src="heroImage"
      alt=""
      aria-hidden="true"
      decoding="async"
      fetchpriority="high"
      @error="emit('image-error')"
    />
    <div class="hero-shade" aria-hidden="true"></div>
    <div class="hero-grain" aria-hidden="true"></div>
    <div class="container hero-inner">
      <p class="au-eyebrow hero-eyebrow hero-enter d1">{{ heroEyebrow }}</p>
      <h1 v-if="heroFeatureTitle" class="hero-feature hero-enter d2">{{ heroFeatureTitle }}</h1>
      <h1 v-else class="hero-title hero-enter d2">{{ greeting }}，{{ username || '影迷' }}</h1>
      <p v-if="heroFeatureTitle && heroMeta" class="hero-meta hero-enter d3">{{ heroMeta }}</p>
      <p v-if="heroFeatureTitle" class="hero-whisper hero-enter d3">
        <span class="whisper-long">{{ greetingParts?.lead }}，{{ username || '影迷' }}{{ greetingParts?.tail }}</span>
        <span class="whisper-short">{{ greeting }}，{{ username || '影迷' }}</span>
      </p>
      <p class="hero-status hero-enter d4">
        <span v-if="isMember" class="hero-tag"><Crown :size="12" />会员</span>
        <span v-else-if="isFreeRealm" class="hero-tag"><Sparkles :size="12" />公益服</span>
        <span class="hero-status-text">{{ heroStatus }}</span>
      </p>

      <template v-if="!isMember && !isFreeRealm">
        <div class="hero-cta hero-enter d5">
          <RouterLink to="/shop" class="au-btn au-btn-primary">立即开通</RouterLink>
          <RouterLink to="/profile" class="hero-link">连接教程 <ChevronRight :size="14" /></RouterLink>
        </div>
        <p class="au-eyebrow steps-label hero-enter d6">入场三步</p>
        <ol class="hero-steps hero-enter d6">
          <li><span class="step-num">1</span><span class="step-body"><strong>开通会员</strong><em>解锁全库影视资源</em></span></li>
          <li><span class="step-num">2</span><span class="step-body"><strong>下载播放器</strong><em>Infuse / Forward 等 Emby 客户端</em></span></li>
          <li><span class="step-num">3</span><span class="step-body"><strong>一键导入</strong><em>在个人中心导入服务器地址与账号</em></span></li>
        </ol>
      </template>
      <template v-else>
        <div class="hero-cta hero-enter d5">
          <a v-if="heroHref" :href="heroHref" class="au-btn hero-cta-outline">在 Rex 里打开</a>
          <RouterLink v-else to="/profile" class="au-btn au-btn-primary">一键导入播放器</RouterLink>
          <RouterLink to="/profile" class="hero-link">连接教程 <ChevronRight :size="14" /></RouterLink>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.hero {
  position: relative;
  min-height: 52vh;
  display: flex;
  align-items: flex-end;
  overflow: hidden;
  color: #fff;
}
/* P3-11：背景海报取景"偏上 30%"（人物脸部优先）+ max-width 复位（旧 HomeView.vue 第 1217-1225 行） */
.hero-backdrop { position: absolute; inset: 0; width: 100%; height: 100%; max-width: none; object-fit: cover; object-position: center 30%; }
/* P2-14：背景图上叠三层（都是写死的暖黑，不跟主题）：
   暗角（影院感，先画）+ 自左向右压暗（文字区可读）+ 自上而下由透明压到 0.92（底部文字与按钮行落在近黑上） */
.hero-shade {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(ellipse 90% 75% at 50% 42%, transparent 58%, rgba(5, 4, 3, 0.42) 100%),
    linear-gradient(90deg, rgba(12, 10, 9, 0.55) 0%, rgba(12, 10, 9, 0.2) 55%, transparent 85%),
    linear-gradient(180deg, transparent 0%, rgba(12, 10, 9, 0.35) 40%, rgba(12, 10, 9, 0.92) 100%);
}
/* P2-13：胶片颗粒——<5% 的噪点是"高级感"和"塑料感"的分界线（旧第 1242-1249 行，SVG 噪点 data-URI 全文） */
.hero-grain {
  position: absolute;
  inset: 0;
  pointer-events: none;
  opacity: 0.55;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='180' height='180' filter='url(%23n)' opacity='0.06'/%3E%3C/svg%3E");
}
.hero-inner { position: relative; z-index: 1; padding: 40px 0; }
/* P3-12：衬线走 token，不 hardcode 字体栈 */
.hero-feature, .hero-title { font-family: var(--au-font-serif); font-size: clamp(28px, 5vw, 48px); margin: 8px 0; }
.hero-meta, .hero-whisper, .hero-status { margin: 6px 0; }
/* 问候低语：长短两版，默认显示长版，窄屏切短版（从 HomeView 迁移，#706 抽组件时遗漏导致两版同时显示） */
.whisper-short { display: none; }
.hero-tag { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; background: rgba(255,255,255,.15); font-size: 12px; margin-right: 8px; }
.hero-cta { display: flex; gap: 12px; margin-top: 16px; }
/* P1-3：描边 CTA（land-book 暗色 Hero 三件套）：平时只描边，hover 才填色。
   全页的主按钮只有一个琥珀实心（未开通的"立即开通"），已开通页这个描边的是唯一 CTA（旧第 1349-1373 行全文） */
.hero-cta-outline {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 44px;
  padding: 0 1.5rem;
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--au-always-light);
  text-decoration: none;
  white-space: nowrap;
  background: transparent;
  border: 1px solid rgba(243, 237, 228, 0.45);
  border-radius: var(--au-r-md);
  transition:
    background var(--au-fast) var(--au-ease),
    border-color var(--au-fast) var(--au-ease),
    color var(--au-fast) var(--au-ease);
}
.hero-cta-outline:hover {
  background: var(--au-always-light);
  border-color: var(--au-always-light);
  color: var(--au-always-dark);
}
.hero-cta-outline:active {
  transform: scale(0.97);
}
/* P2-27：「连接教程」次级文字链——44px 触控行高 + 次级色 + 悬停变琥珀（旧第 1475-1485 行全文） */
.hero-link {
  display: inline-flex;
  align-items: center;
  gap: 0.125rem;
  min-height: 44px;
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--au-text-2);
  text-decoration: none;
  transition: color var(--au-fast) var(--au-ease);
}
.hero-link:hover { color: var(--au-primary); }
.hero-steps { display: flex; gap: 16px; list-style: none; padding: 0; margin: 16px 0 0; }
.step-num { display: inline-flex; width: 22px; height: 22px; align-items: center; justify-content: center; border-radius: 50%; background: rgba(255,255,255,.2); font-size: 12px; margin-right: 6px; }
.hero-enter { opacity: 0; transform: translateY(8px); }
.hero.is-ready .hero-enter { opacity: 1; transform: none; transition: opacity .4s, transform .4s; }
.hero.is-ready .d1 { transition-delay: .05s; }
.hero.is-ready .d2 { transition-delay: .12s; }
.hero.is-ready .d3 { transition-delay: .19s; }
.hero.is-ready .d4 { transition-delay: .26s; }
.hero.is-ready .d5 { transition-delay: .33s; }
.hero.is-ready .d6 { transition-delay: .4s; }

/* 窄屏问候用短版（"晚上好"而不是"晚上好，今晚看什么？"），和票根卡 stat-short 同一套路 */
@media (--au-bp-sm) {
  .whisper-long { display: none; }
  .whisper-short { display: inline; }
}

/* 从 HomeView 迁移（#706 抽取遗漏，home-refactor 修复）：
   以下类在 HomeHero template 中使用，定义残留在 HomeView scoped（scoped 不穿透 → 样式失效）。 */
.hero-eyebrow {
  margin: 0 0 0.75rem;
  max-width: 34rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 状态行里新接上的文字（heroStatus 一直算着但模板没用过，现在接上） */
.hero-status-text {
  color: var(--au-text-3);
}

/* 入场三步的小标签：和眉题同一套语言 */
.steps-label {
  margin: 1.5rem 0 0;
}

.step-body {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
  min-width: 0;
}

.step-body strong {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--au-text);
}

.step-body em {
  font-style: normal;
  font-size: 0.8125rem;
  line-height: 1.5;
  color: var(--au-text-3);
}

.hero-link {
  display: inline-flex;
  align-items: center;
  gap: 0.125rem;
  min-height: 44px;
  font-size: 0.875rem;
  font-weight: 500;
  color: var(--au-text-2);
  text-decoration: none;
  transition: color var(--au-fast) var(--au-ease);
}

.hero-link:hover { color: var(--au-primary); }

/* 描边 CTA：平时只描边，hover 才填色。
   全页的主按钮只有一个琥珀实心（未开通的"立即开通"），已开通页这个描边的是唯一 CTA */
.hero-cta-outline {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 44px;
  padding: 0 1.5rem;
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--au-always-light);
  text-decoration: none;
  white-space: nowrap;
  background: transparent;
  border: 1px solid rgba(243, 237, 228, 0.45);
  border-radius: var(--au-r-md);
  transition:
    background var(--au-fast) var(--au-ease),
    border-color var(--au-fast) var(--au-ease),
    color var(--au-fast) var(--au-ease);
}

.hero-cta-outline:hover {
  background: var(--au-always-light);
  border-color: var(--au-always-light);
  color: var(--au-always-dark);
}

.hero-cta-outline:active {
  transform: scale(0.97);
}

/* reduced-motion：幕布拉开动画直接关掉，不消失（从 HomeView 迁移） */
@media (prefers-reduced-motion: reduce) {
  .hero-enter {
    opacity: 1;
    transform: none;
  }
  .hero.is-ready .hero-enter { transition: none; }
}
</style>
