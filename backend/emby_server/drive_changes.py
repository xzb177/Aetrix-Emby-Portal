"""Google Drive Changes API 增量发现

调研结论（见 workspace/reports/scan-research-20261007.md）：
- Drive Changes API 是云盘增量发现的"标准答案"：
  首次 changes.getStartPageToken() → 存 token → 全量扫一次
  之后 changes.list(pageToken=上次token) → 只返回变化的文件
- 复杂度 O(变化量) 而不是 O(总量)
- 100 个子账号轮询，配额不是问题

设计：
- SA 凭证：/sa-accounts/sa/*.json，轮询使用
- JWT 断言换 access_token（cryptography RS256，不依赖 googleapiclient）
- page token 按 drive_id 存 SystemConfig
- 变化 → 库映射：changes 给 fileId/name/parents，walk parents 建 Drive 路径，
  与库的 Drive 子目录前缀匹配
- 守护线程每 5 分钟 poll 一次，只触发变化目录的定向扫描
"""

from __future__ import annotations

import base64
import configparser
import json
import logging
import os
import random as _random
import threading
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

#: SA 根目录（容器内挂载），下面有 sa/*.json
SA_DIR = os.getenv("RCLONE_SA_DIR", "/sa-accounts")
#: rclone 配置（容器内路径），用于找 Drive remote → team_drive 的映射
RCLONE_CONF = os.getenv("MOUNT_RCLONE_CONF", "/config/rclone/rclone.conf")
#: 轮询间隔（秒），默认 5 分钟
POLL_INTERVAL = max(60, int(os.getenv("DRIVE_CHANGES_INTERVAL", "300") or 300))
#: 轮询间隔抖动比例（反风控：请求间隔随机化，避免机械的固定节拍被识别为机器行为）。
#: 每次等待按 POLL_INTERVAL * uniform(1-ratio, 1+ratio) 重新计算，默认 ±20%。
#: 设为 0 可关闭抖动（便于测试）。钳制在 [0, 0.5]。
POLL_JITTER_RATIO = max(0.0, min(0.5, float(os.getenv("DRIVE_CHANGES_JITTER_RATIO", "0.2") or 0.2)))
#: page token 在 SystemConfig 里的 key 前缀
TOKEN_KEY_PREFIX = "drive_changes_page_token_"
#: 开关
ENABLED = (os.getenv("DRIVE_CHANGES_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
#: 共享盘 404（ID 无效/被删/SA 无权）后的退避秒数，默认 24 小时，最小 1 小时。
#: 404 是永久性配置问题，每 5 分钟重试毫无意义，退避期内直接跳过该盘。
DEAD_DRIVE_RETRY_SEC = max(3600, int(os.getenv("DRIVE_CHANGES_DEAD_RETRY_SEC", "86400") or 86400))

#: drive_id -> 标记为 dead 的时间戳（time.time()）。内存态，重启后清零。
_dead_drives: dict[str, float] = {}
_dead_lock = threading.Lock()


class DriveNotFoundError(Exception):
    """Drive API 返回 404：drive_id 无效（共享盘被删 / ID 写错 / SA 无权访问）。

    属于永久性配置问题，调用方不应每轮重试，而应标记 dead 并退避。
    """

    def __init__(self, drive_id: str):
        super().__init__(f"drive_id 无效（404）: {drive_id}")
        self.drive_id = drive_id


def _is_dead_drive(drive_id: str) -> bool:
    """该盘是否在 404 退避期内。过期则清除标记并返回 False（允许再试一次）。"""
    with _dead_lock:
        ts = _dead_drives.get(drive_id)
        if ts is None:
            return False
        if time.time() - ts >= DEAD_DRIVE_RETRY_SEC:
            del _dead_drives[drive_id]
            return False
        return True


def _mark_dead_drive(drive_id: str) -> None:
    with _dead_lock:
        _dead_drives[drive_id] = time.time()

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
TOKEN_URL = "https://oauth2.googleapis.com/token"
#: 注意：方法名叫 getStartPageToken，但 REST 路径是 changes/startPageToken
#:（见 Drive API discovery 文档）。之前写成 changes/getStartPageToken，
#: Google 返回的是 HTML 404，被 _api_get 误判成 DriveNotFoundError，
#: 导致所有盘被标记 dead、增量发现永远跑不起来（2026-10-10 生产实证）。
CHANGES_TOKEN_URL = "https://www.googleapis.com/drive/v3/changes/startPageToken"
CHANGES_LIST_URL = "https://www.googleapis.com/drive/v3/changes"
FILES_GET_URL = "https://www.googleapis.com/drive/v3/files/{file_id}"

# 视频扩展名（本地判型用，不额外 import scanner）
_VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m2ts",
    ".ts", ".mpg", ".mpeg", ".rmvb", ".rm", ".asf", ".3gp", ".f4v",
}

# ---------------------------------------------------------------------------
# SA 凭证轮询
# ---------------------------------------------------------------------------

_sa_files: list[str] | None = None
_sa_index = 0
_sa_lock = threading.Lock()

#: access_token 缓存：sa_path -> (token, expire_at)
_token_cache: dict[str, tuple[str, float]] = {}


def _collect_json_files(dirs: list[str]) -> list[str]:
    """从给定目录（含其子目录 sa/）收集 *.json，按 realpath 去重并排序"""
    candidates = []
    for base in dirs:
        for d in (base, os.path.join(base, "sa")):
            if not os.path.isdir(d):
                continue
            try:
                entries = sorted(os.listdir(d))
            except OSError as e:
                logger.warning("Drive Changes: 列出目录 %s 失败: %s", d, e)
                continue
            for fn in entries:
                if not fn.endswith(".json"):
                    continue
                p = os.path.join(d, fn)
                if os.path.isfile(p) and os.access(p, os.R_OK):
                    candidates.append(p)
    seen = set()
    uniq = []
    for p in candidates:
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    uniq.sort()
    return uniq


def _sa_files_from_rclone_conf() -> list[str]:
    """解析 rclone.conf，取所有 type=drive remote 的 service_account_file"""
    if not os.path.isfile(RCLONE_CONF):
        return []
    cp = configparser.ConfigParser(interpolation=None)
    paths: list[str] = []
    try:
        cp.read(RCLONE_CONF, encoding="utf-8")
        for section in cp.sections():
            if cp.get(section, "type", fallback="").strip().lower() != "drive":
                continue
            sa = cp.get(section, "service_account_file", fallback="").strip()
            if not sa or not sa.endswith(".json"):
                continue
            if not os.path.isfile(sa) or not os.access(sa, os.R_OK):
                continue
            paths.append(sa)
    except Exception as e:
        logger.warning("Drive Changes: 解析 rclone.conf 失败: %s", e)
        return []
    seen = set()
    uniq = []
    for p in paths:
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    uniq.sort()
    return uniq


def _discover_sa_files() -> list[str]:
    """找到所有 SA json 文件

    兜底链（任一命中即成功，日志打 INFO 说明用了哪条路径）：
    ① $RCLONE_SA_DIR（默认 /sa-accounts）；② rclone.conf 里各 drive remote 的
    service_account_file；③ /opt/rclone-sa。

    生产实证：生产 100 个 SA 在宿主机 /opt/rclone-sa/，而生产 .env 未设
    RCLONE_SA_DIR，导致旧实现（只看 ①）永远发现 0 个 SA，增量发现从未启动。
    """
    global _sa_files
    if _sa_files is not None:
        return _sa_files
    files = _collect_json_files([SA_DIR])
    if files:
        logger.info("Drive Changes: 从 SA 目录发现 %d 个 service account", len(files))
    else:
        files = _sa_files_from_rclone_conf()
        if files:
            logger.info("Drive Changes: 从 rclone.conf service_account_file 兜底发现 %d 个 service account", len(files))
        else:
            files = _collect_json_files(["/opt/rclone-sa"])
            if files:
                logger.info("Drive Changes: 从 /opt/rclone-sa 兜底发现 %d 个 service account", len(files))
            else:
                logger.info("Drive Changes: 发现 0 个 service account")
    _sa_files = files
    return _sa_files


def _sa_rotation_start(n: int) -> int:
    """取一个带随机抖动的 SA 轮换起点，并推进轮换计数器。

    反风控：严格的 +1 轮询是完全规律的循环模式，容易被识别为机器行为；
    步长取 1..n 的随机值，打破固定循环。全项目 SA 轮换只此一处实现，
    drive_auth 复用它，不各自维护计数器。
    """
    global _sa_index
    with _sa_lock:
        start = _sa_index % n
        _sa_index += _random.randint(1, max(1, n))
    return start

def _next_sa() -> dict | None:
    """轮询取下一个 SA 的凭证内容"""
    files = _discover_sa_files()
    if not files:
        return None
    path = files[_sa_rotation_start(len(files))]
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Drive Changes: 读取 SA 失败 %s: %s", path, exc)
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sa_access_token(sa: dict) -> str | None:
    """用 JWT 断言换 access_token（带缓存）"""
    client_email = sa.get("client_email", "")
    cache_key = client_email or sa.get("private_key_id", "")
    now = time.time()
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > now + 60:
        return cached[0]

    private_key_pem = sa.get("private_key", "")
    if not private_key_pem or not client_email:
        return None
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None)
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": client_email,
            "scope": DRIVE_SCOPE,
            "aud": TOKEN_URL,
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "." + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        ).encode("ascii")
        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        assertion = signing_input.decode("ascii") + "." + _b64url(sig)

        resp = httpx.post(TOKEN_URL, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token", "")
        expires_in = int(body.get("expires_in", 3600) or 3600)
        if token:
            _token_cache[cache_key] = (token, now + expires_in)
            return token
    except Exception as exc:
        logger.warning("Drive Changes: SA 换 token 失败 %s: %s", client_email, exc)
    return None


def _get_token() -> str | None:
    """拿一个可用的 access_token（轮换 SA）"""
    files = _discover_sa_files()
    for _ in range(min(3, len(files) or 1)):
        sa = _next_sa()
        if not sa:
            continue
        token = _sa_access_token(sa)
        if token:
            return token
    return None


# ---------------------------------------------------------------------------
# Changes API
# ---------------------------------------------------------------------------

def _api_get(url: str, token: str, params: dict) -> dict:
    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=60,
    )
    if resp.status_code == 401:
        raise PermissionError("Drive token 无效或过期")
    if resp.status_code == 404:
        # drive_id 永久性无效（共享盘被删 / ID 写错 / SA 无权访问，
        # Drive API 对这三种情况都返回 404），抛专用异常让调用方退避，
        # 不要当成普通错误每 5 分钟重试。
        drive_id = (params or {}).get("driveId", "")
        raise DriveNotFoundError(drive_id)
    resp.raise_for_status()
    return resp.json()


def _is_personal_drive_key(drive_id: str) -> bool:
    """个人盘 key 形如 "myDrive:<remote名>"，此时 Drive Changes API 不能传 driveId。"""
    return drive_id.startswith("myDrive:")


def get_start_page_token(drive_id: str) -> str | None:
    """首次：拿初始 page token（调用方存下来，下次从它开始 poll）。

    个人盘不传 driveId（Drive API 会报 400），改传 spaces=drive。
    """
    token = _get_token()
    if not token:
        logger.error("Drive Changes: 拿不到 SA token")
        return None
    if _is_personal_drive_key(drive_id):
        params = {
            "spaces": "drive",
            "supportsAllDrives": "true",
        }
    else:
        params = {
            "driveId": drive_id,
            "supportsAllDrives": "true",
        }
    try:
        body = _api_get(CHANGES_TOKEN_URL, token, params)
        return body.get("startPageToken")
    except DriveNotFoundError:
        raise
    except Exception as exc:
        logger.error("Drive Changes: getStartPageToken 失败 drive=%s: %s", drive_id, exc)
        return None


def list_changes(page_token: str, drive_id: str) -> tuple[list[dict], str | None]:
    """轮询变化。返回 (changes, newStartPageToken)。

    changes 每项：{"fileId": ..., "removed": bool,
                   "file": {"name":..., "parents": [...], "mimeType":...,
                            "trashed": bool}}
    个人盘不传 driveId 和 includeItemsFromAllDrives，改传 spaces=drive。
    """
    token = _get_token()
    if not token:
        return [], None
    all_changes: list[dict] = []
    new_start = None
    next_token: str | None = page_token
    fields = ("changes(fileId,removed,file(id,name,parents,mimeType,trashed)),"
              "newStartPageToken,nextPageToken")
    try:
        while next_token:
            if _is_personal_drive_key(drive_id):
                params = {
                    "pageToken": next_token,
                    "spaces": "drive",
                    "supportsAllDrives": "true",
                    "includeRemoved": "true",
                    "pageSize": 1000,
                    "fields": fields,
                }
            else:
                params = {
                    "pageToken": next_token,
                    "driveId": drive_id,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    "includeRemoved": "true",
                    "pageSize": 1000,
                    "fields": fields,
                }
            body = _api_get(CHANGES_LIST_URL, token, params)
            all_changes.extend(body.get("changes", []))
            new_start = body.get("newStartPageToken") or new_start
            next_token = body.get("nextPageToken")
        return all_changes, new_start
    except DriveNotFoundError:
        raise
    except Exception as exc:
        logger.error("Drive Changes: list 失败 drive=%s: %s", drive_id, exc)
        return [], None


def _file_meta(file_id: str, token: str) -> dict | None:
    """取文件的 name/parents（建路径用，有内存缓存）"""
    cached = _parent_cache.get(file_id)
    if cached is not None:
        return cached
    try:
        body = _api_get(FILES_GET_URL.format(file_id=file_id), token, {
            "fields": "id,name,parents,mimeType",
            "supportsAllDrives": "true",
        })
        _parent_cache[file_id] = body
        return body
    except Exception:
        return None


#: 文件元数据内存缓存：file_id -> {name, parents}
_parent_cache: dict[str, dict] = {}


def drive_path_of(file_id: str, name: str, parents: list[str],
                  drive_id: str, token: str) -> str | None:
    """把 file 建成 Drive 内的完整路径（/a/b/c.mkv）。"""
    parts = [name]
    seen = {file_id}
    cur_parents = parents or []
    depth = 0
    while cur_parents and depth < 32:
        depth += 1
        pid = cur_parents[0]
        if pid in seen or pid == drive_id or pid == "root":
            break
        seen.add(pid)
        meta = _file_meta(pid, token)
        if not meta:
            return None
        parts.append(meta.get("name", ""))
        cur_parents = meta.get("parents", []) or []
    parts.reverse()
    return "/" + "/".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Drive 发现：rclone.conf → {drive_id: [remote 名]}
# ---------------------------------------------------------------------------

def discover_drives() -> dict[str, list[str]]:
    """解析 rclone.conf，找出所有 Drive remote 及其 team_drive"""
    result: dict[str, list[str]] = {}
    if not os.path.isfile(RCLONE_CONF):
        logger.warning("Drive Changes: rclone.conf 不存在: %s", RCLONE_CONF)
        return result
    try:
        cp = configparser.ConfigParser()
        cp.read(RCLONE_CONF, encoding="utf-8")
        for section in cp.sections():
            try:
                if cp.get(section, "type", fallback="").strip().lower() != "drive":
                    continue
                team_drive = cp.get(section, "team_drive", fallback="").strip()
                if team_drive:
                    result.setdefault(team_drive, []).append(section)
                else:
                    # 个人盘（type=drive 但无 team_drive）：key 用 myDrive:<remote名>。
                    # Changes API 调个人盘时不能传 driveId（见 _is_personal_drive_key）。
                    result.setdefault(f"myDrive:{section}", []).append(section)
            except Exception:
                continue
    except Exception as exc:
        logger.warning("Drive Changes: 解析 rclone.conf 失败: %s", exc)
    return result


# ---------------------------------------------------------------------------
# page token 持久化（SystemConfig）
# ---------------------------------------------------------------------------

def _get_page_token(db, drive_id: str) -> str | None:
    from backend import models as base_models
    row = db.query(base_models.SystemConfig).filter(
        base_models.SystemConfig.key == TOKEN_KEY_PREFIX + drive_id).first()
    return row.value if row and row.value else None


def _save_page_token(db, drive_id: str, token: str) -> None:
    from backend import models as base_models
    key = TOKEN_KEY_PREFIX + drive_id
    row = db.query(base_models.SystemConfig).filter(
        base_models.SystemConfig.key == key).first()
    if row:
        row.value = token
    else:
        db.add(base_models.SystemConfig(
            key=key, value=token,
            description="Drive Changes API page token（增量发现用）"))
    db.commit()


# ---------------------------------------------------------------------------
# 变化 → 库映射
# ---------------------------------------------------------------------------

def _library_drive_prefixes(db) -> list[tuple[int, str, str]]:
    """返回 [(library_id, drive_id, drive子目录前缀)]。

    库路径形如 /mnt/mp/nastool/剧集 → 拆出 rclone remote → team_drive，
    前缀 = remote 下的子目录（Drive 内的相对路径）。
    """
    from backend.emby_server import models as em

    # remote 名 -> drive_id
    remote_drive: dict[str, str] = {}
    for drive_id, remotes in discover_drives().items():
        for r in remotes:
            remote_drive[r] = drive_id

    # 本机 FUSE 挂载点 -> rclone remote
    fuse_map = {"/mnt/mp": "MP:", "/mnt/paul": "paul_emby:"}

    out: list[tuple[int, str, str]] = []
    try:
        libs = db.query(em.Library).filter(em.Library.is_enabled == True).all()  # noqa: E712
    except Exception:
        return out
    for lib in libs:
        if getattr(lib, "is_virtual", False):
            continue
        for raw in (getattr(lib, "paths", "") or "").split(","):
            path = raw.strip()
            if not path:
                continue
            # mount://<id>/<子目录>
            if path.startswith("mount://"):
                try:
                    from backend.emby_server import mounts as mount_lib
                    parsed = mount_lib.parse_mount_path(path)
                    if not parsed:
                        continue
                    mount_id, subdir = parsed
                    m = db.query(em.StorageMount).filter(
                        em.StorageMount.id == mount_id).first()
                    if not m:
                        continue
                    mpath = (getattr(m, "path", "") or "").strip()
                    remote = None
                    if mpath.startswith("rclone:"):
                        remote = mpath[len("rclone:"):].split("/", 1)[0]
                        remote = remote.rstrip(":")
                    drive_id = remote_drive.get(remote) if remote else None
                    if drive_id:
                        out.append((lib.id, drive_id, "/" + subdir.strip("/")))
                except Exception:
                    continue
            else:
                # 本机 FUSE 路径：/mnt/mp/nastool/剧集
                for mp, remote in fuse_map.items():
                    if path == mp or path.startswith(mp + "/"):
                        drive_id = remote_drive.get(remote.rstrip(":"))
                        if drive_id:
                            sub = path[len(mp):] or "/"
                            out.append((lib.id, drive_id, sub))
                        break
    return out


def _is_video_name(name: str) -> bool:
    import os as _os
    return _os.path.splitext(name)[1].lower() in _VIDEO_EXTS


def map_changes_to_libraries(db, drive_id: str,
                             changes: list[dict]) -> dict[int, set[str]]:
    """把 changes 映射成 {library_id: {库文件路径前缀}}。

    返回的前缀是库的**文件系统路径**前缀（如 /mnt/mp/nastool/剧集/国产剧），
    调用方直接传给扫描器的 limit_prefixes。
    """
    lib_prefixes = _library_drive_prefixes(db)
    if not lib_prefixes:
        return {}
    # drive_id -> [(lib_id, drive子目录)]
    by_drive: dict[str, list[tuple[int, str]]] = {}
    for lib_id, did, sub in lib_prefixes:
        by_drive.setdefault(did, []).append((lib_id, sub))
    candidates = by_drive.get(drive_id, [])
    if not candidates:
        return {}

    # 库文件路径前缀：需要 drive子目录 -> 文件系统路径 的反查
    # 这里复用 _library_drive_prefixes 的推导：再算一次带 fs 路径的
    fs_map = _library_fs_prefixes(db)  # {(lib_id, drive子目录): fs路径前缀}

    token = _get_token()
    if not token and changes:
        # 拿不到 token 就建不了路径：必须让调用方知道，不能静默返回空——
        # 调用方据此不推进 page token，下一轮重放这批 changes。
        raise RuntimeError("Drive Changes: 拿不到 SA token，无法映射 changes")
    affected: dict[int, set[str]] = {}
    for ch in changes:
        f = ch.get("file") or {}
        name = f.get("name", "")
        # 删除事件没有 file 详情：用 fileId 尝试查（可能已删查不到，跳过）
        if ch.get("removed") or f.get("trashed"):
            continue
        mime = f.get("mimeType", "")
        is_dir = mime == "application/vnd.google-apps.folder"
        if not is_dir and not _is_video_name(name):
            continue
        parents = f.get("parents", []) or []
        dpath = drive_path_of(ch.get("fileId", ""), name, parents, drive_id, token)
        if not dpath:
            continue
        # 变化目录：文件取父目录，目录取自身
        changed_dir = dpath if is_dir else dpath.rsplit("/", 1)[0] or "/"
        for lib_id, sub in candidates:
            if changed_dir == sub or changed_dir.startswith(sub.rstrip("/") + "/"):
                fs_prefix = fs_map.get((lib_id, sub))
                if fs_prefix:
                    # 拼出文件系统路径前缀
                    rel = changed_dir[len(sub):].strip("/")
                    full = fs_prefix.rstrip("/") + ("/" + rel if rel else "")
                    affected.setdefault(lib_id, set()).add(full)
    return affected


def _library_fs_prefixes(db) -> dict[tuple[int, str], str]:
    """{(lib_id, drive子目录): 文件系统路径前缀}"""
    from backend.emby_server import models as em
    out: dict[tuple[int, str], str] = {}
    fuse_map = {"/mnt/mp": "MP:", "/mnt/paul": "paul_emby:"}
    try:
        libs = db.query(em.Library).filter(em.Library.is_enabled == True).all()  # noqa: E712
    except Exception:
        return out
    for lib in libs:
        if getattr(lib, "is_virtual", False):
            continue
        for raw in (getattr(lib, "paths", "") or "").split(","):
            path = raw.strip()
            if not path:
                continue
            if path.startswith("mount://"):
                try:
                    from backend.emby_server import mounts as mount_lib
                    parsed = mount_lib.parse_mount_path(path)
                    if not parsed:
                        continue
                    mount_id, subdir = parsed
                    m = db.query(em.StorageMount).filter(
                        em.StorageMount.id == mount_id).first()
                    if not m:
                        continue
                    # mount 的本机挂载点：从 path 反推（rclone 挂载一般有对应的 FUSE 点）
                    # 简化：用库路径原文的 mount:// 前缀无法直接拼 fs 路径，
                    # 这里记录原文，调用方按 mount_id 解析
                    out[(lib.id, "/" + subdir.strip("/"))] = f"mount://{mount_id}/{subdir.strip('/')}"
                except Exception:
                    continue
            else:
                for mp in fuse_map:
                    if path == mp or path.startswith(mp + "/"):
                        # drive 子目录 = fs 路径去掉挂载点
                        # 需要 drive_id 才能对上；这里先按 (lib_id, sub) 存 fs 路径
                        # 调用方在 map 时用 candidates 的 sub 来查
                        drive_sub = path[len(mp):] or "/"
                        out[(lib.id, drive_sub)] = path
                        break
    return out


# ---------------------------------------------------------------------------
# 守护线程
# ---------------------------------------------------------------------------

_watcher_thread: threading.Thread | None = None
_watcher_stop = threading.Event()

#: 最近一轮 poll 的状态（管理后台展示用，P1-3）
_last_poll_at: str | None = None
_last_poll_changes: int = 0
_last_poll_libs: int = 0


def is_running() -> bool:
    """守护线程是否在跑（change_watcher 用它决定远程源是否跳过快照列举）。"""
    return bool(_watcher_thread and _watcher_thread.is_alive())


def status() -> dict:
    """管理后台展示用：drive_changes 运行状态。"""
    return {
        "running": is_running(),
        "last_poll": _last_poll_at,
        "last_changes": _last_poll_changes,
        "last_libs_triggered": _last_poll_libs,
    }


def _trigger_scan(db, library_id: int, prefixes: set[str]) -> bool:
    """触发定向扫描（只扫变化的目录，不全扫）。

    返回 False 表示入队失败（调用方据此不推进 page token，下轮重放）；
    库不存在 / 没有前缀算「无事可做」，返回 True。
    """
    from backend.emby_server import models as em
    from backend.emby_server import scan_queue
    lib = db.query(em.Library).filter(em.Library.id == library_id).first()
    if not lib:
        return True
    if not prefixes:
        return True
    logger.info("Drive Changes: 库 %s(%s) 定向扫描 %d 个目录",
                getattr(lib, "name", "?"), library_id, len(prefixes))
    try:
        if hasattr(scan_queue, "enqueue_targeted"):
            scan_queue.enqueue_targeted(lib, sorted(prefixes),
                                        trigger="drive-changes")
        else:
            # 降级：整库增量扫（增量指纹会秒跳未变更文件）
            scan_queue.enqueue(lib, trigger="drive-changes")
    except Exception as exc:
        logger.warning("Drive Changes: 入队失败 库=%s: %s", library_id, exc)
        return False
    return True


def poll_once() -> dict:
    """跑一轮增量发现。返回统计。"""
    global _last_poll_at, _last_poll_changes, _last_poll_libs
    from backend.database import SessionLocal
    from backend.emby_server import change_watcher as cw  # 延迟导入，避免循环 import
    t0 = time.time()
    started = datetime.now(timezone.utc)
    stats = {"drives": 0, "changes": 0, "libraries": 0, "errors": 0}
    if not ENABLED:
        return stats
    drives = discover_drives()
    if not drives:
        logger.info("Drive Changes: 没有发现 Drive remote，跳过")
        return stats
    db = SessionLocal()
    fatal_error = ""
    try:
        for drive_id in drives:
            stats["drives"] += 1
            logger.debug("Drive Changes: 轮询 drive=%s（%s）", drive_id,
                         "个人盘" if _is_personal_drive_key(drive_id) else "共享盘")
            if _is_dead_drive(drive_id):
                # 404 退避期内：跳过，不再请求 API、不再打 error 日志
                logger.debug("Drive Changes: drive %s 在 404 退避期内，跳过", drive_id)
                continue
            try:
                page_token = _get_page_token(db, drive_id)
                if not page_token:
                    # 首次：只存 token，不触发扫描（下次从这里开始算增量）
                    token = get_start_page_token(drive_id)
                    if token:
                        _save_page_token(db, drive_id, token)
                        logger.info("Drive Changes: drive %s 初始化 page token", drive_id)
                    continue
                changes, new_token = list_changes(page_token, drive_id)
            except DriveNotFoundError:
                # drive_id 永久性无效：标记 dead 并退避，error 日志只打一次
                stats["errors"] += 1
                _mark_dead_drive(drive_id)
                logger.error(
                    "Drive Changes: 共享盘 ID 无效 drive=%s（Drive API 返回 404），"
                    "请检查 rclone.conf 中对应 remote 的 team_drive 是否写错、"
                    "共享盘是否被删除、SA 是否有访问权限；"
                    "该盘已暂停增量发现 %d 小时，之后会自动再试一次",
                    drive_id, DEAD_DRIVE_RETRY_SEC // 3600,
                )
                continue
            stats["changes"] += len(changes)
            # page token 只在这批 changes **已经映射并入队**之后才推进。
            # 以前先存 token 再映射：映射/入队任何一步失败（token 换不到、
            # DB 抖动、入队异常），这批新文件就被永久跳过——下一轮从新 token
            # 开始，Changes API 不会再把它们吐出来，只能等全量扫描兜底。
            # 单个盘的失败也不再中断后面的盘。
            try:
                ok = True
                if changes:
                    affected = map_changes_to_libraries(db, drive_id, changes)
                    for lib_id, prefixes in affected.items():
                        stats["libraries"] += 1
                        if not _trigger_scan(db, lib_id, prefixes):
                            ok = False
                if ok and new_token:
                    _save_page_token(db, drive_id, new_token)
                elif not ok:
                    stats["errors"] += 1
                    logger.warning("Drive Changes: drive %s 有库入队失败，"
                                   "page token 不推进，下轮重放", drive_id)
            except Exception as exc:
                stats["errors"] += 1
                fatal_error = str(exc)[:500]
                logger.warning("Drive Changes: drive %s 映射/入队失败，"
                               "page token 不推进，下轮重放: %s", drive_id, exc)
                try:
                    db.rollback()
                except Exception:
                    pass
            finally:
                # 清一下 parent 缓存，防止跨轮过期
                _parent_cache.clear()
    except Exception as exc:
        stats["errors"] += 1
        fatal_error = str(exc)[:500]
        logger.warning("Drive Changes: 本轮异常: %s", exc)
    finally:
        _last_poll_at = datetime.now(timezone.utc).isoformat()
        _last_poll_changes = stats["changes"]
        _last_poll_libs = stats["libraries"]
        run_status = "ok" if stats["errors"] == 0 else ("partial" if stats["changes"] else "fail")
        try:
            cw.record_chase_run(
                db, source="drive-changes", started_at=started,
                finished_at=datetime.now(timezone.utc),
                libs_checked=stats["drives"], files_listed=stats["changes"],
                new_found=stats["changes"], scans_triggered=stats["libraries"],
                status=run_status, error=fatal_error)
        except Exception:
            logger.warning("Drive Changes: 写运行历史失败", exc_info=True)
        try:
            db.close()
        except Exception:
            pass
    logger.info("[drive-changes] round done: drives=%d changes=%d libs=%d dur=%ds errors=%d",
                stats["drives"], stats["changes"], stats["libraries"],
                int(time.time() - t0), stats["errors"])
    return stats


def _jittered_interval() -> float:
    """本次轮询等待秒数：基础间隔 ± 抖动，每次调用重新随机化。

    反风控：固定 300.0 秒的机械节拍容易被识别为机器行为，
    这里按 uniform(1-ratio, 1+ratio) 打散。ratio=0 时返回精确的基础间隔。
    """
    if POLL_JITTER_RATIO <= 0:
        return float(POLL_INTERVAL)
    return POLL_INTERVAL * _random.uniform(1.0 - POLL_JITTER_RATIO, 1.0 + POLL_JITTER_RATIO)


def _watcher_loop() -> None:
    logger.info("Drive Changes: 增量发现守护线程启动（基础间隔 %d 秒，±%.0f%% 抖动）",
                POLL_INTERVAL, POLL_JITTER_RATIO * 100)
    # 启动时等 60 秒，让主流程先完成
    _watcher_stop.wait(60)
    while not _watcher_stop.is_set():
        try:
            stats = poll_once()
            if stats["changes"]:
                logger.info("Drive Changes: 本轮 changes=%d libraries=%d",
                            stats["changes"], stats["libraries"])
            # 抖动计算也在 try 内：若抛异常会被下面兜住记 warning，
            # 而不是静默杀死整个守护线程。
            wait_s = _jittered_interval()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Drive Changes: 轮询异常: %s", exc)
            wait_s = float(POLL_INTERVAL)
        _watcher_stop.wait(wait_s)


def start() -> bool:
    """启动守护线程。返回是否成功启动。"""
    global _watcher_thread
    if not ENABLED:
        logger.info("Drive Changes: 已禁用（DRIVE_CHANGES_ENABLED=0）")
        return False
    if _watcher_thread and _watcher_thread.is_alive():
        return True
    # 没有 SA 或没有 Drive remote 就不启动
    if not _discover_sa_files():
        logger.info("Drive Changes: 没有 SA 文件，不启动")
        return False
    if not discover_drives():
        logger.info("Drive Changes: 没有 Drive remote，不启动")
        return False
    _watcher_stop.clear()
    _watcher_thread = threading.Thread(
        target=_watcher_loop, daemon=True, name="drive-changes")
    _watcher_thread.start()
    return True


def stop(timeout: float = 5.0) -> None:
    _watcher_stop.set()
    global _watcher_thread
    if _watcher_thread and _watcher_thread.is_alive():
        _watcher_thread.join(timeout=timeout)
    _watcher_thread = None
