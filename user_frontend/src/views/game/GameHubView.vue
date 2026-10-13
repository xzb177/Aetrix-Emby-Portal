<template>
  <div class="v4-scope">
    <div class="v4-page">
      <!-- 日志头 -->
      <header class="v4-loghead">
        <span class="v4-stamp">{{ hub.stamp }}</span>
        <h1>{{ hub.title }}</h1>
        <p>{{ hub.subtitle }}</p>
      </header>

      <!-- 体力条：无条件渲染，可见性由 StaminaBar 内部 v-if="visible" 控制；
           不可见时根节点不渲染，不留幽灵间隙 -->
      <StaminaBar />

      <!-- 设施导航：全部来自 @/config/game，不 hardcode -->
      <section class="v4-sec">
        <div class="v4-sec-title">🏕️ 营地设施</div>
        <div class="v4-sec-sub">共 {{ facilities.length }} 处，点卡片直接进</div>
        <div class="hub-grid">
          <router-link
            v-for="f in facilities"
            :key="f.key"
            :to="f.path"
            class="v4-card hub-card"
            :aria-label="f.name"
          >
            <div class="hub-icon">{{ f.icon }}</div>
            <div class="hub-body">
              <div class="hub-name">{{ f.name }}</div>
              <div class="hub-desc">{{ f.desc }}</div>
            </div>
            <div class="hub-go">›</div>
          </router-link>
        </div>
      </section>
    </div>
    <PanelTabbar />
  </div>
</template>

<script setup lang="ts">
import PanelTabbar from '@/components/panel/PanelTabbar.vue'
import { gameHubConfig as hub, gameFacilities as facilities } from '@/config/game'
import StaminaBar from '@/components/game/StaminaBar.vue'
</script>

<style scoped>
.hub-grid {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.hub-card {
  display: flex;
  align-items: center;
  gap: 12px;
  text-decoration: none;
  color: inherit;
  transition: transform 0.12s ease;
}
.hub-card:active {
  transform: scale(0.98);
}
.hub-icon {
  font-size: 28px;
  line-height: 1;
  flex-shrink: 0;
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--paper2);
  border: 1.5px solid var(--line);
  border-radius: 10px;
}
.hub-body {
  flex: 1;
  min-width: 0;
}
.hub-name {
  font-size: 15px;
  font-weight: 800;
  letter-spacing: 1px;
}
.hub-desc {
  font-size: 12px;
  color: var(--ink2);
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.hub-go {
  font-size: 22px;
  color: var(--ink2);
  flex-shrink: 0;
}

.hub-card:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}
</style>
