"""perfection-r44: env.example 与代码 env 键清单对齐的防漂移测试。

r44 专项：env.example 缺失约 177 个代码实际读取的环境变量键（r42 遗留观察项），
本轮全部补录；另清理 11 个全仓零引用的死键文档残留。

本测试保证以后新增 env 键时文档不掉队：
1. 静态可检测的读取（os.getenv / os.environ.get / env_int / env_float /
   _env_* / _env_flag / _cpu_workers / *_ENV = "NAME" 常量）必须被 env.example
   收录，或列入下面的显式 allowlist（间接读取/部署专用，有据）。
2. allowlist 里的键必须在代码里仍有引用（防改名后 allowlist 变成僵尸）。
3. env.example 里收录的键必须仍有出处（代码读取/间接读取/部署专用），
   防止 CHECKIN_BASE_POINTS 那类"文档有、代码无"的死键残留复发。
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO / "env.example"
# scripts 也纳入：一次性运维/E2E 脚本的 env 键同样值得文档（归入"脚本与排障"段）
CODE_ROOTS = [REPO / "backend", REPO / "emby_api", REPO / "scripts"]

# 静态扫描器抓不到、但亲验确认在代码里被读取的键（元组轮询等动态读取）
# 格式：键名 -> 读取证据（文件:行 或 读取方式说明）
INDIRECT_READS = {
    "NODE_KEY": "backend/emby_server/nodes.py:53 NODE_KEY_ENVS 元组轮询 os.environ.get",
    "EMBY_NODE_KEY": "backend/emby_server/nodes.py:53 NODE_KEY_ENVS 元组轮询 os.environ.get",
    "ALL_PROXY": "backend/integrations/proxy.py:39 ENV_KEYS 元组 + :90 轮询写入/读取",
    "HTTP_PROXY": "backend/integrations/proxy.py:39 ENV_KEYS 元组 + :90 轮询写入/读取",
    "HTTPS_PROXY": "backend/integrations/proxy.py:39 ENV_KEYS 元组 + :90 轮询写入/读取",
    # 变量名 ENV_EDGE_CACHE_MODE 不以 _ENV 结尾，静态模式抓不到
    "EDGE_CACHE_MODE": "backend/emby_server/edge_cache.py:27 ENV_EDGE_CACHE_MODE 常量 + :45 os.environ.get",
    # DIAG_* 一键诊断阈值：经 THRESHOLD_DEFS / FIX_THRESHOLD_DEFS 元组轮询、
    # get_threshold / get_fix_threshold 以变量传键读 _env_float（r1907 逐键亲验）
    "DIAG_DISK_WARN_PCT": "backend/diagnosis.py:68 THRESHOLD_DEFS 元组 + 变量传键 _env_float",
    "DIAG_DISK_CRIT_PCT": "backend/diagnosis.py:69 THRESHOLD_DEFS 元组 + 变量传键 _env_float",
    "DIAG_PROBE_QUEUE_WARN": "backend/diagnosis.py:70 THRESHOLD_DEFS 元组 + 变量传键 _env_float",
    "DIAG_ENRICH_BACKLOG_FLOOR": "backend/diagnosis.py:71 THRESHOLD_DEFS 元组 + 变量传键 _env_float",
    "DIAG_ENRICH_IDLE_SEC": "backend/diagnosis.py:72 THRESHOLD_DEFS 元组 + 变量传键 _env_float",
    "DIAG_API_ERROR_RATIO": "backend/diagnosis.py:73 THRESHOLD_DEFS 元组 + 变量传键 _env_float",
    "DIAG_FIX_PROBE_TTL_SEC": "backend/diagnosis_fix.py:58 FIX_THRESHOLD_DEFS 元组 + 变量传键 _env_float",
    "DIAG_FIX_SUBTITLE_CACHE": "backend/diagnosis_fix.py:60 FIX_THRESHOLD_DEFS 元组 + 变量传键 _env_float",
}

# 部署/编排专用键：代码不直接读，但 docker-compose / 部署脚本 / 授权版部署消费
DEPLOY_ONLY = {
    "AETRIX_IMAGE_TAG",
    "AETRIX_PORT",
    "EA_INTERNAL_PORT",
    "MEDIA_ROOT",
    "PORT",
    "POSTGRES_DB",
    "POSTGRES_PASSWORD",
    "POSTGRES_USER",
    "RCLONE_RCD_GOMEMLIMIT",
    "RCLONE_RC_PASS",
    "RCLONE_RC_USER",
    "RCLONE_VFS_CACHE_MAX_SIZE",
    "RCLONE_VFS_READ_CHUNK_LIMIT",
    "STRM_CONTAINER_PATH",
    "STRM_MOUNT_DIR",
    "WORKER_RESTART_BASE",
    "WORKER_RESTART_MAX",
    "WORKER_STABLE_SECONDS",
}

_PATTERNS = [
    # 直接读取：os.getenv / os.environ.get / env_int / env_float / _env_* 系列 helper
    # （_env_[a-z_]+ 覆盖 _env_int_or / _env_flag 等全部本地 helper 变体）
    re.compile(r'\b(?:os\.getenv|os\.environ\.get|getenv|env_int|env_float|_env_[a-z_]+|_cpu_workers)\(\s*["\']([A-Z][A-Z0-9_]*)["\']'),
    re.compile(r'\bos\.environ\[\s*["\']([A-Z][A-Z0-9_]*)["\']'),
    # 间接读取：FOO_ENV = "FOO" / _FOO_ENV = "FOO" 常量（允许前导下划线）
    re.compile(r'\b[A-Za-z_][A-Za-z0-9_]*_ENV\b\s*=\s*["\']([A-Z][A-Z0-9_]*)["\']'),
]


def documented_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r'\s*#?\s*([A-Z][A-Z0-9_]*)\s*=', line)
        if m:
            keys.add(m.group(1))
    return keys


def statically_detected_keys() -> set[str]:
    keys = set()
    for root in CODE_ROOTS:
        for path in root.rglob("*.py"):
            if "test" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for pat in _PATTERNS:
                keys.update(pat.findall(text))
    return keys


def test_env_example_exists():
    assert ENV_EXAMPLE.exists(), "env.example 缺失"


def test_static_detected_keys_documented():
    """静态可检测的 env 键必须被 env.example 收录（或在 allowlist）。"""
    detected = statically_detected_keys()
    documented = documented_keys()
    missing = detected - documented - set(INDIRECT_READS) - DEPLOY_ONLY
    assert not missing, (
        "以下 env 键在代码里被读取但 env.example 未收录：\n"
        + "\n".join(sorted(f"  {k}" for k in missing))
    )


def test_allowlisted_keys_still_referenced():
    """allowlist 里的键必须仍在仓库里有引用（防改名后变成僵尸条目）。

    INDIRECT_READS 查 backend/emby_api 的 py；DEPLOY_ONLY 还可能出现在
    docker-compose / scripts / docs（部署编排层消费）。
    """
    stale = []
    search_roots = [REPO / "backend", REPO / "emby_api", REPO / "scripts"]
    for key in list(INDIRECT_READS) + sorted(DEPLOY_ONLY):
        hits = [
            p for root in search_roots
            for p in root.rglob("*.py")
            if key in p.read_text(encoding="utf-8", errors="ignore")
        ]
        hits += [
            p for p in REPO.glob("docker-compose*.yml")
            if key in p.read_text(encoding="utf-8", errors="ignore")
        ]
        if not hits:
            stale.append(key)
    assert not stale, f"allowlist 僵尸键（仓库已无引用）：{stale}"


def test_documented_keys_live():
    """env.example 收录的键必须有出处：代码读取 / 间接读取 / 部署专用。

    防止 CHECKIN_BASE_POINTS 那类"文档有、代码无"的死键残留复发。
    """
    detected = statically_detected_keys()
    documented = documented_keys()
    dead = documented - detected - set(INDIRECT_READS) - DEPLOY_ONLY
    assert not dead, (
        "以下键在 env.example 有文档但全仓无出处（疑似死键残留）：\n"
        + "\n".join(sorted(f"  {k}" for k in dead))
    )
