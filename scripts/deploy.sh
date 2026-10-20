#!/usr/bin/env bash
# Aetrix Portal Docker 一键部署脚本
#
# 首次部署或用当前工作区重新构建：
#   检查环境 / .env / Git 工作区
#   → docker compose config --quiet
#   → docker compose build --pull
#   → docker compose up -d --remove-orphans
#   → 等待 aetrix-api 容器健康检查通过
#
# 已经部署过、想从远端拉取新版本时，请使用 scripts/update.sh；本脚本不执行 git pull。
#
# 注意：本脚本只管理默认编排文件（docker-compose.yml）里的后端 5 个容器，
# 不含网页前端（docker-compose.frontend.yml 的 aetrix-web）。两条路线不要混用：
# 脚本的 up 带 --remove-orphans，会摘掉不在默认编排文件里的前端容器。
# 完整口径见 docs/deploy-docker.md 第 3 节。
#
# 用法：
#   bash scripts/deploy.sh
#   bash scripts/deploy.sh --dry-run

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$ROOT_DIR"

DRY_RUN=0

usage() {
  cat <<'EOF'
用法：bash scripts/deploy.sh [选项]

选项：
  --dry-run       只打印将要执行的命令，不构建、启动或等待服务
  -h, --help      显示帮助

首次部署前请先在项目根目录创建 .env：
  cp env.example .env
并填好固定的 SECRET_KEY。已有部署需要拉取远端更新时，请运行：
  bash scripts/update.sh
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知选项：$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if ((DRY_RUN)); then
    printf '  + '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

command -v git >/dev/null 2>&1 || die "找不到 git，请先安装 Git。"
if (( ! DRY_RUN )); then
  command -v docker >/dev/null 2>&1 || die "找不到 docker，请先安装 Docker。"
  docker compose version >/dev/null 2>&1 || die "找不到 Docker Compose v2（docker compose）。"
fi

git rev-parse --show-toplevel >/dev/null 2>&1 || die "当前目录不是 Git 仓库：$ROOT_DIR"
repo_root="$(git rev-parse --show-toplevel)"
[[ "$repo_root" == "$ROOT_DIR" ]] || die "脚本必须在项目根目录对应的仓库中运行。"

if [[ ! -f .env ]]; then
  die "项目根目录缺少 .env，请先执行：cp env.example .env，并填好 SECRET_KEY。"
fi

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  warn "工作区存在未提交改动，已停止部署，避免把未确认的代码直接发布："
  git status --short >&2
  die "请先提交、暂存或移走这些改动后再运行。"
fi

compose=(docker compose)
log "校验 Compose 配置"
run "${compose[@]}" config --quiet

log "构建最新镜像"
run "${compose[@]}" build --pull

log "启动或重建 Compose 服务"
run "${compose[@]}" up -d --remove-orphans

if ((DRY_RUN)); then
  log "dry-run 完成，未实际构建、启动或等待服务。"
  exit 0
fi

log "等待 aetrix-api 容器健康检查通过"
healthy=0
for _ in $(seq 1 60); do
  status="$(docker inspect -f '{{.State.Status}}' aetrix-api 2>/dev/null || true)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' aetrix-api 2>/dev/null || true)"
  if [[ "$status" == "running" && "$health" == "healthy" ]]; then
    healthy=1
    break
  fi
  if [[ "$status" == "exited" || "$status" == "dead" ]]; then
    "${compose[@]}" logs --tail 80 aetrix-api >&2 || true
    die "aetrix-api 容器启动失败（状态：$status）。"
  fi
  sleep 2
done

if (( ! healthy )); then
  "${compose[@]}" ps >&2 || true
  die "aetrix-api 在 120 秒内没有通过健康检查。"
fi

log "查看服务状态"
"${compose[@]}" ps
log "部署完成：aetrix-api 容器已通过健康检查"
log "建议随后检查：curl http://127.0.0.1:8000/api/health"
