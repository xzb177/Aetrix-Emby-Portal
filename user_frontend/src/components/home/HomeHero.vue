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
  <section class="hero" :class="{ 'has-image': heroImage && !heroImageFailed, 'is-ready': hasLoaded }">
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
          <RouterLink to="/store" class="au-btn au-btn-primary">立即开通</RouterLink>
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
.hero-backdrop { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.hero-shade { position: absolute; inset: 0; background: linear-gradient(to top, rgba(0,0,0,.85) 0%, rgba(0,0,0,.3) 50%, transparent); }
.hero-grain { position: absolute; inset: 0; pointer-events: none; opacity: .15; }
.hero-inner { position: relative; z-index: 1; padding: 40px 0; }
.hero-feature, .hero-title { font-family: "Noto Serif SC", serif; font-size: clamp(28px, 5vw, 48px); margin: 8px 0; }
.hero-meta, .hero-whisper, .hero-status { margin: 6px 0; }
/* 问候低语：长短两版，默认显示长版，窄屏切短版（从 HomeView 迁移，#706 抽组件时遗漏导致两版同时显示） */
.whisper-short { display: none; }
.hero-tag { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; background: rgba(255,255,255,.15); font-size: 12px; margin-right: 8px; }
.hero-cta { display: flex; gap: 12px; margin-top: 16px; }
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
@media (max-width: 640px) {
  .whisper-long { display: none; }
  .whisper-short { display: inline; }
}
</style>
