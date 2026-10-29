# 前端性能优化 — 全力攻坚记录

## 已落地改动

### 1. 组件拆分（减少单文件体积、利于缓存与解析）
- 新增 `user_frontend/src/components/home/HomeTicketCard.vue`
  - 票根资产卡（积分/订阅/观影数据）独立，含骨架与响应式样式
- 新增 `user_frontend/src/components/home/HomeContinueWatching.vue`
  - 继续观看列表独立，图片自带 lazy + async
- `HomeView.vue` 引用上述组件，模板体积显著下降

### 2. 非关键请求延迟
- 新增 `user_frontend/src/composables/useDefer.ts`（`deferIdle`）
- `HomeView` 中 `loadRecent` / `loadResume` 通过 `deferIdle` 推迟到浏览器空闲时执行
- 核心摘要 `loadDeferred` 仍立即加载

### 3. 渲染优化
- `HomeView` 给 `.recent` / `.playing` / `.todo` / `.help` / `.resume` 加了 `content-visibility: auto`
  - 首屏以下区块延迟布局/绘制
- `EmbyAdmin.vue` 媒体库封面图增加 `loading="lazy"` + `decoding="async"`

### 4. 路由预取
- `user_frontend/src/router/index.ts`：登录后空闲时预加载商店 / 个人中心 / 钱包 chunk
  - 用户大概率会去的页面，切 tab 时更快

### 5. 打包分析（可选）
- `user_frontend` 与 `admin_frontend` 的 `vite.config.ts` 支持 `ANALYZE=1`
- 使用前：`npm i -D rollup-plugin-visualizer`
- 然后 `ANALYZE=1 npm run build`，打开 `dist/stats.html`

## 已确认原有良好实践（未改）
- 路由全懒加载 + KeepAlive
- ~~hls.js 动态 import~~（服务端转码删除后网页播放不再走 HLS，hls.js 已不在依赖里）
- Element Plus 按需引入
- MediaCard 原生懒加载 + 骨架
- Vite manualChunks
- 字体 display=swap + preconnect
- admin chunk 失效自动刷新

## 建议后续（可继续拆）
| 文件 | 建议 |
|------|------|
| `EmbyAdmin.vue`（仍 ~2800 行） | 把媒体库卡片列表、扫描队列、条目管理抽屉再拆 |
| `Servers.vue` / `MetadataSources.vue` | 列表与弹窗/抽屉分离 |
| `StoreView.vue` / `ProfileView.vue` | 套餐卡、设备列表等独立 |

## 验证
```bash
cd user_frontend && npm run type-check && npm run build
cd admin_frontend && npm run type-check && npm run build
```
