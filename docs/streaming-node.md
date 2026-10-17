# 流节点（分离架构）：高宽带大盘 + CF 隐藏 IP + 域名统一入口

> 通用能力：单机部署不受任何影响（`stream_nodes` 无配置时行为逐字节一致）；
> 需要时一键加节点，不用改代码。

## 什么时候需要

- 单机 VPS 带宽跑满（多人同时看 4K 卡顿）
- Google Drive 单文件下载配额被热门剧触发 403
- 想要源站 IP 不暴露、防攻击

## 架构

```
用户 ──HTTPS──▶ Cloudflare（橙云，隐藏源站 IP）
                  │
                  ├─▶ 主服务域名（emby.example.com）：API/鉴权/播放 URL 签发
                  │
                  └─▶ 流节点域名（stream.example.com）：只出视频流
                        │
                        ▼
                   高宽带大盘机器：EA 出流 + rclone FUSE + 大盘 VFS 缓存 ──▶ Drive
```

1. 用户在主服务鉴权，`PlaybackInfo` 返回的播放 URL 已自动改写到流节点域名
   （短期签名 `uid/exp/sign` 原样保留，流节点用同一 `SECRET_KEY` 验签）；
2. 流节点读主库（只读）做用户/订阅校验，从自己的 rclone 挂载读文件；
3. 热门内容常驻流节点大盘 VFS 缓存，Drive 配额压力骤降；
4. 多流节点按权重分流，某台挂了自动摘除（30 秒健康检查）。

## 一键部署（推荐）

在**流节点机器**（高宽带大盘，Debian/Ubuntu，root）上：

```bash
curl -fsSL https://raw.githubusercontent.com/xzb177/Aetrix-Portal/main/scripts/deploy-streaming-node.sh -o deploy.sh
chmod +x deploy.sh
sudo ./deploy.sh
```

脚本会问 4 个问题（主服务地址、面板密钥、流节点域名、节点名），然后自动：
拉配置包 → 落盘 rclone.conf/SA → 按磁盘自动放大 VFS 缓存（>500GB 给 70% 空闲）
→ 挂载每个远端 → 起 `aetrix-stream` 容器 → 回主服务注册。

## Cloudflare 隐藏 IP（5 步）

1. CF → DNS → 给 `stream.example.com` 加 A 记录指向流节点公网 IP，**开橙云**（代理）；
2. SSL/TLS → 加密模式选**完全（严格）**；
3. 规则 → 缓存规则：`stream.example.com/emby/Videos/*/stream*`，边缘 TTL
   按需（注意：CF 免费版单文件上限 512MB，大文件主要靠节点本地大盘缓存，
   CF 负责隐藏 IP + 挡攻击 + 缓小文件/图片）；
4. 流节点防火墙：`8001` 端口**只放行 CF 回源 IP 段**
   （https://www.cloudflare.com/ips/）和主服务内网 IP；
5. 验证：`curl -I https://stream.example.com/api/health` 通，且
   `curl -I http://<源站IP>:8001/emby/System/Info` 从外网不通。

域名强制已内置：`ENFORCE_DOMAIN=stream.example.com` 时，直接用 IP 访问一律 403
（`/api/health` 内网健康检查除外）；`TRUST_CF_IP=true` 时日志/限流用
`CF-Connecting-IP` 还原的真人 IP。

## 手动部署

1. 复制 `docker-compose.stream-node.yml`，按注释填挂载点；
2. 写 `.env`（敏感值，`chmod 600`）：

```bash
SECRET_KEY=与主服务相同
DATABASE_URL=postgresql://aetrix:密码@主服务内网IP:5432/aetrix
DATABASE_TYPE=postgresql
AETRIX_ROLE=stream
ENFORCE_DOMAIN=stream.example.com
TRUST_CF_IP=true
STREAM_NODE_PUBLIC_URL=https://stream.example.com
```

3. 主机上 rclone 挂载（路径与主服务 `file_path` 一致，如 `/mnt/mp`）；
4. `docker compose up -d`；
5. 主服务注册节点（二选一）：
   - 后台「播放节点」页添加（规划中），或
   - `curl -X POST https://主服务/api/admin/stream-nodes/register \
       -H "X-Panel-Key: $NODE_KEY" -H "Content-Type: application/json" \
       -d '{"url":"https://stream.example.com","name":"流节点-1","weight":100}'`
     （`$NODE_KEY` 是**节点密钥**，在主服务上 `python -m backend.node_auth` 查看；
     自 S3 安全修复起不再接受 `SECRET_KEY` 原文）

## 大盘缓存

- 磁盘总量 ≥500GB：VFS 缓存自动给到空闲的 70%（`playback_tune.auto_vfs_cache_size`）；
- 热门预热：`docker exec aetrix-stream python scripts/preheat-hot-cache.py --top 50`
 （按全站播放次数 Top N，每文件读 64MB 进缓存；部署脚本可选自动跑）；
- 淘汰：rclone VFS 自带类 LRU（`--vfs-cache-max-age 168h`），冷数据自动腾地方。

## 常见问题

- **只加了一台流节点，本机还出流吗？** 会。`stream_nodes` 里同时保留本机
  （公网地址）+ 流节点，按权重分流；想全切到流节点就把本机权重调小或删掉。
- **流节点需要 Redis 吗？** 不需要。播放验签只依赖 `SECRET_KEY` + 主库只读。
- **主服务挂了流节点还能播吗？** 不能——鉴权 URL 是主服务签发的，
  且流节点要读主库验用户。这是设计取舍：无状态可扩展，但鉴权中心化。
