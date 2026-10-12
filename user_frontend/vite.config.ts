import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 生产环境禁用 DevTools
const isProduction = process.env.NODE_ENV === 'production'
export default defineConfig({
  plugins: [
    vue(),
  ].filter(Boolean),
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    },
  },
  build: {
    // 目标浏览器
    target: 'es2015',
    // 启用 CSS 代码分割
    cssCodeSplit: true,
    // chunk 大小警告限制提高到 100KB
    chunkSizeWarningLimit: 100,
    // 使用 esbuild 压缩（生产环境移除 console/debugger）
    minify: 'esbuild',
    // Rollup 配置
    rollupOptions: {
      output: {
        // 手动分包策略
        manualChunks: (id) => {
          // node_modules 包
          if (id.includes('node_modules')) {
            // Vue 核心
            if (id.includes('vue/') || id.includes('@vue/') || id.includes('pinia/') || id.includes('vue-router/')) {
              return 'vue-vendor'
            }
            // UI 组件库
            if (id.includes('element-plus') || id.includes('@element-plus')) {
              return 'element-plus'
            }
            // 图标库
            if (id.includes('lucide-vue-next')) {
              return 'icons'
            }
            // 播放器（hls.js，独立分包避免阻塞首屏）
            if (id.includes('hls.js') || id.includes('hls.js/dist')) {
              return 'hls'
            }
            // 工具库
            if (id.includes('axios') || id.includes('lodash-es')) {
              return 'utils'
            }
            // 其他第三方包
            return 'vendor'
          }
        },
        // chunk 文件命名
        chunkFileNames: 'assets/js/[name]-[hash].js',
        entryFileNames: 'assets/js/[name]-[hash].js',
        assetFileNames: (assetInfo) => {
          const name = assetInfo.name || ''
          if (name.endsWith('.css')) {
            return 'assets/css/[name]-[hash][extname]'
          }
          if (/\.(png|jpe?g|gif|svg|webp|ico)$/.test(name)) {
            return 'assets/images/[name]-[hash][extname]'
          }
          if (/\.(woff2?|eot|ttf|otf)$/.test(name)) {
            return 'assets/fonts/[name]-[hash][extname]'
          }
          return 'assets/[name]-[hash][extname]'
        },
      },
    },
    // 禁用源码映射
    sourcemap: false,
    // 构建时报告
    reportCompressedSize: true,
  },
  // CSS 配置
  css: {
    devSourcemap: false,
    // CSS 预处理器选项
    preprocessorOptions: {
      scss: {
        additionalData: ``
      }
    }
  },
  // 优化依赖预构建
  optimizeDeps: {
    include: ['vue', 'vue-router', 'pinia', 'lucide-vue-next'],
    exclude: [],
  },
  // 服务器配置
  server: {
    port: 5173,
    host: true,
  },
})
