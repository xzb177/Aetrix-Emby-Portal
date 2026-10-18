<template>
  <WorldMap variant="home" />
</template>

<script setup lang="ts">
/**
 * 首页探险世界：地图本体已拆分为 WorldMap/ 目录（与 /game 探险日志页共用），
 * 这里只剩一层壳，保证 DiscoveryView 的引用与行为不变。
 */
import WorldMap from './WorldMap/index.vue'
</script>
