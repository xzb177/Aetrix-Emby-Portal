#!/usr/bin/env bash
# Aetrix 发行打包（乙路线）：产出 dist/aetrix-release-<版本号>.tar.gz
#
# 包内含：后端源码（backend/ emby_api/ serve.py VERSION）、两个前端的
# 已构建产物（frontend_dist/user、frontend_dist/admin）、买家版
# docker-compose（授权校验默认开）、干净的 env.example、买家版
# 《安装说明.md》。前端在打包机上构建，买家只需要 Docker。
#
# 干净断言（fail-closed，任一命中不许出包）：
#   1. 包内不得有 .env 真文件（只有 env.example）；
#   2. 不得出现自家生产 IP（旧 VPS / AWS / 甲骨文）；
#   3. 自家域名 135505.autos 只许以 license.135505.autos 出现
#      （授权服务地址与联系方式语境），其余子域名一律不许；
#   4. 不得有私钥块 / 常见密钥令牌样式串；
#   5. 不得带 .git、node_modules、数据库文件、测试数据。
#
# 用法：scripts/build_release.sh <版本号>（如 2.54.0）
set -euo pipefail

VERSION="${1:?用法：scripts/build_release.sh <版本号>，如 2.54.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"
PKG_NAME="aetrix-release-$VERSION"
STAGE="$DIST/$PKG_NAME"
TARBALL="$DIST/$PKG_NAME.tar.gz"

fail() { echo "❌ 出包中止：$*" >&2; exit 1; }

echo "==> 构建 user_frontend"
(cd "$ROOT/user_frontend" && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npm run build-only >/dev/null)
echo "==> 构建 admin_frontend"
(cd "$ROOT/admin_frontend" && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npm run build >/dev/null)
[ -f "$ROOT/user_frontend/dist/index.html" ] || fail "user_frontend 构建产物缺失"
[ -f "$ROOT/admin_frontend/dist/index.html" ] || fail "admin_frontend 构建产物缺失"

echo "==> 组装发行包 $PKG_NAME"
rm -rf "$STAGE"
mkdir -p "$STAGE/frontend_dist"
cp -r "$ROOT/backend" "$STAGE/backend"
cp -r "$ROOT/emby_api" "$STAGE/emby_api"
cp -r "$ROOT/serve.py" "$ROOT/VERSION" "$ROOT/Dockerfile.backend" "$STAGE/"
cp -r "$ROOT/env.example" "$STAGE/env.example"
cp -r "$ROOT/docker" "$STAGE/docker"
cp -r "$ROOT/user_frontend/dist" "$STAGE/frontend_dist/user"
cp -r "$ROOT/admin_frontend/dist" "$STAGE/frontend_dist/admin"
cp -r "$ROOT/scripts/release/docker-compose.yml" "$STAGE/docker-compose.yml"
cp -r "$ROOT/scripts/release/Dockerfile.web" "$STAGE/Dockerfile.web"
cp -r "$ROOT/scripts/release/安装说明.md" "$STAGE/安装说明.md"
# 瘦身：解释器缓存、依赖目录、数据库文件一律不带
find "$STAGE" -type d \( -name __pycache__ -o -name node_modules -o -name .git \) -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type f \( -name "*.pyc" -o -name "*.db" -o -name "*.sqlite" -o -name "*.sqlite3" \) -delete

echo "==> 干净断言"
# 1. .env 真文件（任意层级）
if find "$STAGE" -type f -name ".env" | grep -q .; then
  fail "包内出现 .env 真文件"
fi
if find "$STAGE" -type f -name ".env.*" ! -name ".env.example" | grep -q .; then
  fail "包内出现 .env 变体真文件"
fi
# 2. 生产 IP
if grep -rInE "15\.204\.95\.101|3\.107\.49\.179|144\.24\.114\.219" "$STAGE" 2>/dev/null; then
  fail "包内出现自家生产 IP"
fi
# 3. 自家域名：剥掉 license.135505.autos 后不许再有 135505.autos
if grep -rIn "135505\.autos" "$STAGE" 2>/dev/null | grep -v "license\.135505\.autos" ; then
  fail "包内出现授权服务地址/联系方式之外的自家域名"
fi
# 4. 私钥块与常见密钥/令牌样式
if grep -rInE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" "$STAGE" 2>/dev/null; then
  fail "包内出现私钥块"
fi
if grep -rInE "sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}" "$STAGE" 2>/dev/null; then
  fail "包内出现密钥/令牌样式串"
fi
# 5. .git / 测试目录
[ ! -e "$STAGE/.git" ] || fail "包内出现 .git"
[ ! -e "$STAGE/tests" ] || fail "包内出现 tests 目录"
if find "$STAGE" -type d -name node_modules | grep -q .; then
  fail "包内出现 node_modules"
fi
echo "    断言全过"

echo "==> 打包 $TARBALL"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$DIST" "$PKG_NAME"
echo "✅ 完成：$TARBALL"
du -h "$TARBALL" | cut -f1
