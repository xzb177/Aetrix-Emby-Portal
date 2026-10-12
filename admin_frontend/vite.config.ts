import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import AutoImport from 'unplugin-auto-import/vite'
import { visualizer } from 'rollup-plugin-visualizer'

const analyze = process.env.ANALYZE === '1'

export default defineConfig({
  base: '/admin/',
  plugins: [
    vue(),
    analyze && visualizer({ gzipSize: true, brotliSize: true, open: false, filename: 'dist/stats.html' }),
    // element-plus 按需引入：只打包实际用到的组件（原来全量引入 1MB+）。
    // <el-*> 模板标签自动解析；ElMessage/ElMessageBox 等函数式调用自动引入（含样式）。
    Components({
      resolvers: [ElementPlusResolver()],
      dts: 'src/components.d.ts',
    }),
    AutoImport({
      resolvers: [ElementPlusResolver()],
      dts: 'src/auto-imports.d.ts',
    }),
  ].filter(Boolean),
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    },
  },
  server: {
    port: 5173,
    host: '0.0.0.0',
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
    chunkSizeWarningLimit: 1000,
    // 禁用 CSS 代码分割，避免懒加载路由的 CSS 预加载失败
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        manualChunks: {
          // vue 核心单独成块；element-plus 按需引入后只含用到的组件，单独成块利于缓存
          'vue-vendor': ['vue', 'vue-router', 'pinia'],
          'element-plus': ['element-plus'],
        },
      },
    },
  },
})
