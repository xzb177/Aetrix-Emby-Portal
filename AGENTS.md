# 协作与交付流程

本仓库默认采用 **PR 合并**，不直接向 `main` 推送（包括管理员与自动化代理）。

## 默认流程

1. 从最新的 `origin/main` 切出短生命周期分支（如 `fix/...`、`feat/...`、`chore/...`）。
2. 在分支上完成改动，并按仓库既有命令本地验证：
   - 后端：`pytest tests/ -q` 与相关 `scripts/check_*.py`
   - 管理端：`cd admin_frontend && npm run type-check && npm run build`
   - 用户端：`cd user_frontend && npm run type-check && npm run build`
3. 推送分支，创建面向 `main` 的 PR。
4. 等待下列状态检查全部通过后再合并：
   - 前端 · user_frontend
   - 前端 · admin_frontend
   - 后端 · 冒烟测试
   - 后端 · 部署自检（真起服务 + 真发 HTTP）
   - 后端 · PG 兼容冒烟
5. 合并后删除分支，并把 `main` 同步回本地。

## 例外

只有在用户明确要求、且确认不会绕过必需检查时，才允许直接推送 `main`。
不得使用强推、`--force`、历史重写或绕过保护规则的命令。
