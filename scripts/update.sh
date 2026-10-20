#!/usr/bin/env bash
# Aetrix Portal Docker 一键更新脚本
#
# 默认流程：检查工作区 → 备份数据库（尽可能）→ git pull --ff-only
#          → docker compose build --pull → docker compose up -d --remove-orphans
#
# 设计约束：
# - 有未提交改动时直接拒绝，避免 git pull 覆盖现场；
# - 更新前尽量备份容器内的 SQLite，备份失败会阻断更新；
#   注意：默认的 PostgreSQL 部署下 /data 里没有 SQLite，本脚本会告警并跳过备份，
#   等于没有备份——PG 部署更新前请先运行 scripts/backup_db.sh；
# - 任何一步失败立即退出，不继续执行后续步骤；
# - 不碰 .env、数据库卷、媒体目录和宿主机配置。
#
# 用法：
#   bash scripts/update.sh
#   bash scripts/update.sh --dry-run
#   bash scripts/update.sh --skip-backup

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$ROOT_DIR"

DRY_RUN=0
SKIP_BACKUP=0

usage() {
  cat <<'EOF'
用法：bash scripts/update.sh [选项]

选项：
  --dry-run       只打印将要执行的命令，不拉取、备份、构建或重启
  --skip-backup   跳过更新前的 SQLite 备份尝试
  -h, --help      显示帮助
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --skip-backup) SKIP_BACKUP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知选项：$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[1;36m[update]\033[0m %s\n' "$*"; }
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

# 定位「用来备份数据库」的运行中容器。
#
# 背景：v2.41 之后后端拆成 aetrix-api / aetrix-worker 两个服务，脚本里硬编码的
# 服务名 aetrix 已经不存在，于是每次更新都走进「未发现正在运行的 aetrix 容器」
# 分支 —— 看着只是 warn，实际是静默跳过了数据库备份。
#
# 这里不猜服务名，直接按下面顺序在运行中的容器里挑一个：
#   1. AETRIX_ROLE=api   （最稳，api 一定有 python 和 /data）
#   2. AETRIX_ROLE=worker
#   3. 任意能读到 /data 下 SQLite 的运行中容器（redis/rclone 会被自动排除）
# 成功时把容器 ID 打到 stdout，返回 0；找不到返回 1。
detect_backup_container() {
  local -a running=() ordered=()
  local seen=' ' cid role want

  while IFS= read -r cid; do
    [[ -n "$cid" ]] && running+=("$cid")
  done < <("${compose[@]}" ps --status running -q 2>/dev/null | sort -u)

  ((${#running[@]})) || return 1

  # 按角色排序，角色来自容器环境变量（不 exec，避免对 redis 之类跑 sh -c 报错）
  for want in api worker; do
    for cid in "${running[@]}"; do
      [[ "$seen" == *" $cid "* ]] && continue
      role="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null \
        | sed -n 's/^AETRIX_ROLE=//p' | head -n1 | tr -d '\r\n')"
      if [[ "$role" == "$want" ]]; then
        ordered+=("$cid")
        seen+="$cid "
      fi
    done
  done
  # 兜底：把没被角色命中的容器接在后面，交给下面 /data 探测淘汰
  for cid in "${running[@]}"; do
    [[ "$seen" == *" $cid "* ]] && continue
    ordered+=("$cid")
    seen+="$cid "
  done

  for cid in "${ordered[@]}"; do
    if docker exec "$cid" sh -c 'ls /data/*.db >/dev/null 2>&1' 2>/dev/null; then
      printf '%s' "$cid"
      return 0
    fi
  done
  return 1
}

command -v git >/dev/null 2>&1 || die "找不到 git，请先安装 Git。"
if (( ! DRY_RUN )); then
  command -v docker >/dev/null 2>&1 || die "找不到 docker，请先安装 Docker。"
  docker compose version >/dev/null 2>&1 || die "找不到 Docker Compose v2（docker compose）。"
fi

git rev-parse --show-toplevel >/dev/null 2>&1 || die "当前目录不是 Git 仓库：$ROOT_DIR"
repo_root="$(git rev-parse --show-toplevel)"
[[ "$repo_root" == "$ROOT_DIR" ]] || die "脚本必须在项目根目录对应的仓库中运行。"

branch="$(git symbolic-ref --quiet --short HEAD || true)"
[[ -n "$branch" ]] || die "当前处于 detached HEAD 状态，请先切换到要部署的分支。"
git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1 \
  || die "分支 $branch 没有上游分支，请先执行：git branch --set-upstream-to=origin/$branch $branch"

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  warn "工作区存在未提交改动，已停止更新，避免覆盖现场："
  git status --short >&2
  die "请先提交、暂存或移走这些改动后再运行。"
fi

compose=(docker compose)
backup_container=''
backup_note=''
if (( ! DRY_RUN )) && (( ! SKIP_BACKUP )); then
  backup_container="$(detect_backup_container || true)"
  if [[ -z "$backup_container" ]]; then
    backup_note='未找到挂载了 /data 的运行中 aetrix 容器，跳过数据库备份。'
  fi
fi

if ((DRY_RUN)); then
  log "dry-run：将在更新前尝试备份 /data 下的 SQLite 数据库。"
elif ((SKIP_BACKUP)); then
  warn "已按 --skip-backup 跳过数据库备份。"
elif [[ -n "$backup_note" ]]; then
  warn "$backup_note"
else
  log "备份容器：$(docker inspect -f '{{.Name}}' "$backup_container" 2>/dev/null || echo "$backup_container")"
  # 定位真实数据库：
  # - 优先按 DATABASE_URL 解析，避免"猜文件名"猜错；
  # - 兜底扫描 /data/*.db 时必须排除本脚本自己产出的 aetrix-update-* 历史备份。
  #   （旧实现直接取字母序第一个，而 "aetrix-update-" 里 '-' < '_'，
  #     所以它每次都在备份一份旧备份，真正的实时库从未被备份过。）
  db_path="$(docker exec "$backup_container" sh -c '
    url="${DATABASE_URL:-}"
    case "$url" in
      sqlite:///*)
        path="${url#sqlite:///}"
        [ -f "$path" ] && printf "%s" "$path" && exit 0
        ;;
    esac
    for db in /data/*.db; do
      [ -f "$db" ] || continue
      case "${db##*/}" in aetrix-update-*) continue ;; esac
      printf "%s" "$db"
      exit 0
    done
  ' | tr -d '\r\n')"
  py_bin="$(docker exec "$backup_container" sh -c 'command -v python3 || command -v python' \
    | tr -d '\r\n')"
  if [[ -z "$db_path" ]]; then
    warn "容器 /data 下没有找到 SQLite 数据库，跳过备份。"
  elif [[ "$db_path" == /data/aetrix-update-* ]]; then
    die "只找到历史备份文件、没有实时数据库，已停止更新：$db_path"
  elif [[ -z "$py_bin" ]]; then
    warn "备份容器内没有 python/python3，跳过备份。"
  else
    backup_path="/data/aetrix-update-$(date +%Y%m%d-%H%M%S).db"
    log "备份数据库：$db_path → $backup_path"
    docker exec -i "$backup_container" "$py_bin" - "$db_path" "$backup_path" <<'PY' \
      || die "数据库备份失败，已停止更新。"
import sqlite3
import sys

source_path, backup_path = sys.argv[1:]
with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as backup:
    source.backup(backup)
PY
    # 备份完立刻核对关键表行数：备份出一份空壳比不备份更危险
    if ! docker exec "$backup_container" "$py_bin" - "$db_path" "$backup_path" <<'PY'
import sqlite3
import sys

source_path, backup_path = sys.argv[1:]
with sqlite3.connect(source_path) as source:
    tables = {r[0] for r in source.execute(
        "select name from sqlite_master where type='table'")}
    counts = {}
    for table in sorted(tables):
        try:
            counts[table] = source.execute(f'select count(*) from "{table}"').fetchone()[0]
        except sqlite3.Error:
            continue
with sqlite3.connect(backup_path) as backup:
    for table, expected in counts.items():
        actual = backup.execute(f'select count(*) from "{table}"').fetchone()[0]
        if actual != expected:
            print(f'表 {table} 行数不一致：源 {expected} / 备份 {actual}', file=sys.stderr)
            sys.exit(1)
PY
    then
      die "数据库备份校验失败（行数不一致），已停止更新。"
    fi
    log "数据库备份完成并通过行数校验。"
  fi
fi

log "当前分支：$branch"
log "拉取远端更新（仅允许 fast-forward）"
run git pull --ff-only

log "重新构建 Docker 镜像"
run "${compose[@]}" build --pull

log "更新 Compose 服务"
run "${compose[@]}" up -d --remove-orphans

log "查看服务状态"
run "${compose[@]}" ps

log "更新流程完成"
if ((DRY_RUN)); then
  log "这是 dry-run，未执行任何实际更新。"
else
  log "建议随后检查：curl http://127.0.0.1:8000/api/health"
fi
