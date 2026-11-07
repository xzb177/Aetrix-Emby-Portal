#!/usr/bin/env bash
# Aetrix 发行打包（乙路线）：产出 dist/aetrix-release-<版本号>.tar.gz
#
# 拉取形态：发行包**不含任何源码**。程序本体走镜像仓库分发
# （.github/workflows/publish-images.yml 在主线 CI 全绿后自动构建并推送
# ghcr.io/xzb177/aetrix-api 与 ghcr.io/xzb177/aetrix-web，tag 规则：
# 版本号 / sha-<短码> / latest），买家用随授权码发放的拉取凭据
# docker login 后 pull 下来直接跑，机器上不需要 Node / Python、不做构建。
#
# 包内只有四样（都是文本，整个包应当只有几十 KB）：
#   docker-compose.yml  拉取版（scripts/release/docker-compose.yml），
#                       出包时把默认镜像版本钉成出包版本号
#   env.example         干净的配置模板，买家 cp 成 .env 改四处
#   安装说明.md          大白话安装 / 升级说明（scripts/release/安装说明.md）
#   VERSION             本发行包的版本号，与 compose 默认镜像版本一致
#
# 买家想升 / 降版不用重新拿包：改 .env 里的 AETRIX_VERSION 再 pull 即可，
# 所以 compose 里的默认版本只是「这一包出厂时钉的版本」。
#
# 干净断言（fail-closed，任一命中不许出包）：
#   1. 包内不得有 .env 真文件（只有 env.example）；
#   2. 包内不得出现任何源码目录（backend/ emby_api/ user_frontend/
#      admin_frontend/ tests/）或 .py 文件——拉取形态的立身之本；
#   3. 包内 compose 必须是拉取形态（出现 build: 段即中止），且两个
#      服务的默认镜像版本必须等于出包版本号；
#   4. 不得出现自家生产 IP（旧 VPS / AWS / 甲骨文）；
#   5. 自家域名 135505.autos 只许以 license.135505.autos 出现
#      （授权服务地址与联系方式语境），其余子域名一律不许；
#   6. 不得有私钥块 / 常见密钥令牌样式串（拉取凭据只随授权码发放，
#      绝不进发行包）；
#   7. 不得带 .git、node_modules、数据库文件。
#
# 用法：scripts/build_release.sh <版本号>（如 2.54.0）
set -euo pipefail

VERSION="${1:?用法：scripts/build_release.sh <版本号>，如 2.54.0}"
printf '%s' "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' \
  || { echo "❌ 出包中止：版本号不是 x.y.z 形态：'$VERSION'" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist"
PKG_NAME="aetrix-release-$VERSION"
STAGE="$DIST/$PKG_NAME"
TARBALL="$DIST/$PKG_NAME.tar.gz"

fail() { echo "❌ 出包中止：$*" >&2; exit 1; }

echo "==> 组装发行包 $PKG_NAME（拉取形态，无源码）"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp "$ROOT/scripts/release/docker-compose.yml" "$STAGE/docker-compose.yml"
cp "$ROOT/env.example" "$STAGE/env.example"
cp "$ROOT/scripts/release/安装说明.md" "$STAGE/安装说明.md"
# VERSION 写出包版本号而不是照抄仓库 VERSION 文件：两者正常情况下一致，
# 但出包参数才是这一包的版本真相，包内必须自洽。
printf '%s\n' "$VERSION" > "$STAGE/VERSION"
# 把 compose 的默认镜像版本钉成出包版本号：仓库里的 compose 默认值跟随
# 仓库 VERSION 文件，出包以本次参数为准，两者不一致时以参数为准。
sed -i "s/\${AETRIX_VERSION:-[0-9][0-9.]*}/\${AETRIX_VERSION:-$VERSION}/g" \
  "$STAGE/docker-compose.yml"

echo "==> 干净断言"
# 1. .env 真文件（任意层级）
if find "$STAGE" -type f -name ".env" | grep -q .; then
  fail "包内出现 .env 真文件"
fi
if find "$STAGE" -type f -name ".env.*" ! -name ".env.example" | grep -q .; then
  fail "包内出现 .env 变体真文件"
fi
# 2. 源码目录 / .py 文件（拉取形态不许带源码）
for d in backend emby_api user_frontend admin_frontend tests; do
  [ ! -e "$STAGE/$d" ] || fail "包内出现源码目录 $d/"
done
if find "$STAGE" -type f -name "*.py" | grep -q .; then
  fail "包内出现 .py 文件"
fi
# 3. compose 拉取形态 + 默认镜像版本 == 出包版本号
if grep -qE '^[[:space:]]+build:' "$STAGE/docker-compose.yml"; then
  fail "包内 compose 出现 build: 段（必须是拉取形态）"
fi
grep -qF "ghcr.io/xzb177/aetrix-api:\${AETRIX_VERSION:-$VERSION}" \
  "$STAGE/docker-compose.yml" || fail "包内 compose 的 aetrix-api 默认镜像版本不是 $VERSION"
grep -qF "ghcr.io/xzb177/aetrix-web:\${AETRIX_VERSION:-$VERSION}" \
  "$STAGE/docker-compose.yml" || fail "包内 compose 的 aetrix-web 默认镜像版本不是 $VERSION"
# 4. 生产 IP
if grep -rInE "15\.204\.95\.101|3\.107\.49\.179|144\.24\.114\.219" "$STAGE" 2>/dev/null; then
  fail "包内出现自家生产 IP"
fi
# 5. 自家域名：剥掉 license.135505.autos 后不许再有 135505.autos
if grep -rIn "135505\.autos" "$STAGE" 2>/dev/null | grep -v "license\.135505\.autos" ; then
  fail "包内出现授权服务地址/联系方式之外的自家域名"
fi
# 6. 私钥块与常见密钥/令牌样式
if grep -rInE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" "$STAGE" 2>/dev/null; then
  fail "包内出现私钥块"
fi
if grep -rInE "sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}" "$STAGE" 2>/dev/null; then
  fail "包内出现密钥/令牌样式串"
fi
# 7. .git / node_modules / 数据库文件
[ ! -e "$STAGE/.git" ] || fail "包内出现 .git"
if find "$STAGE" -type d -name node_modules | grep -q .; then
  fail "包内出现 node_modules"
fi
if find "$STAGE" -type f \( -name "*.db" -o -name "*.sqlite" -o -name "*.sqlite3" \) | grep -q .; then
  fail "包内出现数据库文件"
fi
echo "    断言全过"

echo "==> 打包 $TARBALL"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C "$DIST" "$PKG_NAME"
echo "✅ 完成：$TARBALL"
du -h "$TARBALL" | cut -f1
