# Cloudflare 边缘缓存配置指南

> 通用能力：谁部署、按本文档配好 CF，都能用；不配也不坏（默认 `off`）。

## 适用场景

用户点播放后黑屏 10–19 秒（服务端只花 1–2 秒），起流后带宽正常。
原因是每次首字节都要回源（悉尼）→ Google Drive。开了 CF 橙云也没用，
因为每个播放 URL 的 `PlaySessionId` 都不同，CF 默认缓存 key 含完整查询串，
永远 MISS。

本指南让 CF 按"忽略查询串"做缓存 key，热门视频首字节从边缘命中。

## 前置条件

1. 域名已接入 Cloudflare 且开了橙云（代理）
2. 服务端 `EDGE_CACHE_MODE=query_ignore`（`env.example` 有说明，默认 `off`）
3. 确认 `/stream` 响应头已有 `Cache-Control: public, max-age=21600, s-maxage=21600, immutable`
   （服务端默认就发，不用改）

## 配置步骤（CF 免费版可用）

1. CF 后台 → 你的域名 → **Caching → Cache Rules → Create rule**
2. Rule name：`emby-stream-edge-cache`
3. If incoming requests match… → Custom filter expression，填：
   ```
   (http.request.uri.path wildcard r"/emby/videos/*/stream*")
   ```
   （UI 操作：字段 `URI 路径`，运算符 `通配符`，值 `/emby/videos/*/stream*`。
   注意小写 `videos`——路径匹配区分大小写。）
4. Then… → **缓存资格**：`符合缓存条件`（Eligible for cache）
5. Then… → **边缘 TTL**：`忽略缓存控制标头，使用此 TTL`，填 `8 小时`
   （或选"使用缓存控制标头"，源站已发 `s-maxage=21600`=6 小时）
6. Then… → **缓存密钥** → 添加设置 → **忽略查询字符串**：打开 ← **最关键的一步**
7. 保存，等待约 30 秒生效

### 不要配的

- ❌ 不要对 `/emby/` API 路径开缓存（`Items`、`PlaybackInfo` 是动态的）
- （`master.m3u8` 等 HLS 转码地址已随服务端转码删除，一律 404，无需单独配规则）
- ❌ 不要开 "Cache Everything" 页面规则（一刀切会缓存 API）

## 验证

1. 播同一部片两次
2. 第二次看响应头 `CF-Cache-Status: HIT`（第一次是 MISS）
3. 如开了 `EDGE_CACHE_MODE=query_ignore`，响应里还有
   `X-Edge-Cache-Mode: query-ignore`，证明服务端声明了该模式

## 安全说明（必读）

`query_ignore` 模式下，CF 缓存 key 不含 token。已登录用户 A 的播放 URL
被分享给无账号用户 B，B 在缓存 TTL 内可直接从边缘拿到视频（不回源鉴权）。

- 前提：B 必须知道 32 位 hex 的 item GUID（不可枚举，只能从 A 处获得）
- 对比现状：B 拿 A 的 URL 本来也能播（签名 15 分钟～6 小时有效期内），
  本模式只是把窗口放大到缓存 TTL（建议 ≤8 小时）
- 视频字节对所有用户相同，不存在"串片"（串的是访问权，不是内容）
- 缓存 MISS 时一律回源走完整鉴权，边缘只是"热门内容的快捷路径"

默认 `off`，管理员知情后才开；高敏感站点别开。

## 降级/回退

- CF Rule 删掉或关掉 → 回到 MISS 回源，不坏
- 服务端 `EDGE_CACHE_MODE` 改回 `off` → 即刻生效（热读，无需重启/重建）
- 紧急情况：CF 后台一键关 Rule 即可
