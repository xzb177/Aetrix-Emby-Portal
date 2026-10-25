"""BYOS P3：按绑定选源扫描入库。

只做一件事：**按绑定选源**——一次扫描只认一个用户存储绑定（P1/P2 建
好的行），用该绑定自己的凭据列举文件，交给现行扫描体（scanner 的
定向模式）入库。入库走已有的身份锚与条目模型（Drive 写
``provider_file_id``、115 留空，见 scanner 写库循环）。

地盘红线（违反打回）：
- 只管选源扫描。认亲（content_hash 批量查条目）是 #13 PR-3 的地盘：
  本模块只把列举到的 ``content_hash`` 随 MountFile 原样透传给扫描体
  （扫描体 #13 PR-2 已有的写库列），绝不拿它查库、去重、合并；
- 不碰播放、115 sidecar、139、阶段 8 的文件；
- 不碰管理后台：本模块没有任何管理页，管理端接线由管理后台专线
  另做（等本 PR 落地后再做）；
- 绝不跨用户复用凭据：绑定行只认本人行，凭据只取该行自己的
  （Drive 走 P2 的绑定级令牌口、115 走 P1 的绑定级 Cookie 口），
  失败响亮报错、不回落共享 SA、不回落共享 Cookie、不回落最早绑定。

运行形态：
- 总开关 ``BYOS_SCAN_ENABLED``（默认关）：关时 ``scan_binding`` 直接
  拒绝、不碰任何上游、不写任何条目；关掉即回到今天的行为；
- 定向扫描：快照置 ``targeted=True``，整库清理阶段跳过——绑定线只做
  新增/更新，绝不因“本轮只看见一个绑定”而删掉库里别的来源的条目。
  绑定下文件消失的清理由 ``prune_binding_items`` 在扫描成功后定向做
  （只删本绑定、本库的未见行，带主线同款空清单拒绝与缺失宽限），
  不走整库清理路径；
- 入库路径命名空间 ``byos://<binding_id>/<rel>``：同一文件被两个用户
  绑定 = 两条条目（guid 由该路径派生，天然分账）；同一绑定重扫 =
  同一 guid，走更新。Drive 的改名识别另靠 ``drive_file_id``（扫描体
  现行逻辑），路径只是 guid 锚；
- 凭据纪律：本模块经手的令牌/Cookie 只传给当次列举/探测的调用，
  绝不记日志、不进返回值、不进扫描统计；
- NFO 与外挂字幕：绑定线复用扫描体主线口（``scanner._find_nfos`` /
  ``_side_info`` 的绑定线分支 + ``subtitle_match`` 的 byos 路径前缀），
  不另写语义；NFO 文本落库走主线 ``apply_nfo`` 同款分支（见未决项：
  主线 NFO 优先分支并非严格只填空缺，本模块不擅自改语义）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace as _dataclass_replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterator, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import scanner
from backend.emby_server import soft_delete as _soft_delete
from backend.emby_server import user_bindings as ub

logger = logging.getLogger(__name__)

#: BYOS P3 总开关（默认关；开时才接受绑定扫描调用）。
SCAN_ENABLED_ENV = "BYOS_SCAN_ENABLED"

#: 绑定扫描入库路径前缀（scanner 的 _dir_key_of / _series_guid_of 按
#: 此前缀认绑定命名空间，见两处注释）。
BYOS_PATH_PREFIX = "byos://"

#: Drive files.list（用户令牌直调，不走 SA 池）。
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_FILES_FIELDS = (
    "nextPageToken,files(id,name,mimeType,size,md5Checksum,"
    "modifiedTime,etag,headRevisionId,version)"
)


class BindingScanError(RuntimeError):
    """绑定扫描失败（带可分类的 kind）。

    kind 取值：disabled（总开关关着）、not_found（绑定/库不存在或
    非本人）、invalid（绑定已失效：凭据错/被限）、expired（登录态
    过期）、unsupported（该 provider 不支持绑定扫描）、transient
    （一过性失败，可重试；不改绑定状态）。
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def scan_enabled() -> bool:
    """BYOS 绑定扫描总开关（默认关；非法值按关处理，不猜开）"""
    return (os.getenv(SCAN_ENABLED_ENV, "0") or "0").strip().lower() not in {
        "0", "false", "no", "off", ""}


def binding_path(binding_id: int, rel: str) -> str:
    """绑定扫描的入库路径：``byos://<binding_id>/<rel>``"""
    clean = "/" + (rel or "").lstrip("/")
    return f"{BYOS_PATH_PREFIX}{binding_id}{clean}"


def is_byos_path(path: str | None) -> bool:
    """该路径是否为绑定扫描命名空间（供调用方分支，不含凭据信息）"""
    return bool(path) and str(path).startswith(BYOS_PATH_PREFIX)


def _parse_mod_ts(value) -> float:
    """Drive modifiedTime（ISO 8601）→ Unix 秒；解析失败记 0.0（不猜）"""
    if not value:
        return 0.0
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _default_drive_http_get(url: str, token: str, params: dict) -> dict:
    """Drive API 默认 GET（仅 byos_scan 的用户令牌列举用）。

    401/403 → MountAuthError（调用方按凭据失效处理）；其余 4xx/5xx →
    MountError（一过性/上游错，不改绑定状态）。令牌只放 Authorization
    头，绝不拼进 URL（URL 会进日志与统计）。
    """
    import httpx

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                url, params=params,
                headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:  # noqa: BLE001 — 网络失败 = 一过性
        raise mount_lib.MountError(f"Drive 列举网络失败：{exc}") from exc
    if resp.status_code in (401, 403):
        raise mount_lib.MountAuthError(
            f"Drive 令牌失效（HTTP {resp.status_code}），请重新绑定。")
    if resp.status_code >= 400:
        raise mount_lib.MountError(
            f"Drive 列举失败（HTTP {resp.status_code}）。")
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — 非 JSON = 上游错
        raise mount_lib.MountError("Drive 列举返回非 JSON。") from exc


class UserDriveEnumerator:
    """用户 Drive 令牌的目录列举器（duck-type 提供者，供扫描体用）。

    只认调用方给的这一个用户的 access_token：列举、探测取段、旁挂
    文本全走它，不读 SA 池、不读 rclone.conf、不读 scope=source 行。
    令牌过期续跑不在本类做——长列举跨过有效期时由调用方重建本类
    （新令牌经 drive_oauth 绑定级口取），本类不静默换别人的令牌。

    扫描体用到的口：``local_root``（恒 None：远程源）、``list_dir``、
    ``walk_media``、``resolve_final``（探测取段）、``read_text``
    （旁挂文本：扫描体 NFO 发现经 ``read_sidecar_text_from`` 走本口）。
    """

    def __init__(self, access_token: str, http_get=None,
                 root_id: str = "root"):
        self._token = access_token
        self._http_get = http_get or _default_drive_http_get
        self._root_id = root_id
        self.local_root = None
        self._dir_id_cache: dict[str, str] = {"/": root_id, "": root_id}
        self._file_cache: dict[str, dict] = {}

    def _api(self, params: dict) -> dict:
        return self._http_get(DRIVE_FILES_URL, self._token, params)

    def _list_children(self, folder_id: str) -> list[dict]:
        """一目录的全部子项（分页拉完；trashed 不进扫描）"""
        out: list[dict] = []
        page_token: Optional[str] = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": DRIVE_FILES_FIELDS,
                "pageSize": 100,
                "spaces": "drive",
            }
            if page_token:
                params["pageToken"] = page_token
            body = self._api(params)
            out.extend(body.get("files") or [])
            page_token = body.get("nextPageToken")
            if not page_token:
                return out

    def _folder_id(self, rel: str) -> str:
        """相对路径 → 目录 id（逐级列举，缺失抛 MountError，不猜）"""
        clean = "/" + (rel or "").lstrip("/")
        clean = clean.rstrip("/") or "/"
        if clean in self._dir_id_cache:
            return self._dir_id_cache[clean]
        parent_rel, _, name = clean.rpartition("/")
        parent_id = self._folder_id(parent_rel or "/")
        for item in self._list_children(parent_id):
            if (item.get("name") == name
                    and item.get("mimeType") == DRIVE_FOLDER_MIME):
                fid = str(item.get("id") or "")
                self._dir_id_cache[clean] = fid
                return fid
        raise mount_lib.MountError(f"Drive 上找不到目录: {clean}")

    def _locate_file(self, rel: str) -> dict:
        """相对路径 → 文件项（父目录列举里按名找；目录本身不算文件）"""
        clean = "/" + (rel or "").lstrip("/")
        if clean in self._file_cache:
            return self._file_cache[clean]
        parent_rel, _, name = clean.rpartition("/")
        parent_id = self._folder_id(parent_rel or "/")
        for item in self._list_children(parent_id):
            if (item.get("name") == name
                    and item.get("mimeType") != DRIVE_FOLDER_MIME):
                self._file_cache[clean] = item
                return item
        raise mount_lib.MountError(f"Drive 上找不到文件: {clean}")

    def list_dir(self, rel: str = "/") -> list[mount_lib.MountEntry]:
        """该目录的直系子项（扫描体目录指纹与 NFO 发现用）"""
        folder_id = self._folder_id(rel)
        prefix = ("/" + (rel or "").lstrip("/")).rstrip("/")
        entries = []
        for item in self._list_children(folder_id):
            name = item.get("name") or ""
            is_dir = item.get("mimeType") == DRIVE_FOLDER_MIME
            entries.append(mount_lib.MountEntry(
                name=name, rel=f"{prefix}/{name}", is_dir=is_dir,
                size=int(item.get("size") or 0) if not is_dir else 0,
                entry_id=str(item.get("id") or ""),
                file_id="" if is_dir else str(item.get("id") or ""),
                mod_ts=_parse_mod_ts(item.get("modifiedTime")),
            ))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def walk_media(self, root: str = "/") -> Iterator[mount_lib.MountFile]:
        """整棵（或 root 子树）媒体文件 BFS 列举。

        原盘结构目录整棵跳过（与现行扫描口径一致）；媒体扩展名只认
        mounts.REMOTE_MEDIA_EXTS；无 file_id 的文件跳过并记日志（没
        有稳定身份的文件入不了身份锚，见 DriveMount.walk_media 同款
        防御）。
        """
        stack: list[tuple[str, str]] = [(root, self._folder_id(root))]
        seen_folders: set[str] = {stack[0][1]}
        while stack:
            rel, folder_id = stack.pop()
            for item in self._list_children(folder_id):
                name = item.get("name") or ""
                child_rel = f"{rel.rstrip('/')}/{name}"
                if item.get("mimeType") == DRIVE_FOLDER_MIME:
                    if mount_lib.disc_filter.is_disc_subtree_dir(name):
                        continue
                    child_id = str(item.get("id") or "")
                    if not child_id or child_id in seen_folders:
                        continue
                    seen_folders.add(child_id)
                    stack.append((child_rel, child_id))
                    continue
                if os.path.splitext(name)[1].lower() not in mount_lib.REMOTE_MEDIA_EXTS:
                    continue
                file_id = str(item.get("id") or "")
                if not file_id:
                    logger.warning("BYOS Drive 遍历跳过无 file_id 文件：%s", child_rel)
                    continue
                md5 = str(item.get("md5Checksum") or "")
                yield mount_lib.MountFile(
                    rel=child_rel, name=name,
                    size=int(item.get("size") or 0),
                    is_strm=mount_lib._is_strm_name(name),
                    file_id=file_id,
                    mod_ts=_parse_mod_ts(item.get("modifiedTime")),
                    # PR-2 透传：md5 是列举元数据，写库落 content_hash 列；
                    # 拿它查库/去重是 PR-3 的活，本模块绝不做。
                    content_hash=md5,
                    content_hash_algo="md5" if md5 else "",
                    etag=str(item.get("etag") or ""),
                    revision=str(item.get("headRevisionId")
                               or item.get("version") or ""),
                )

    def resolve_final(self, rel: str) -> mount_lib.PlayTarget:
        """探测取段用目标：Drive alt=media + 该用户令牌头（不下发客户端）"""
        item = self._locate_file(rel)
        file_id = str(item.get("id") or "")
        return mount_lib.PlayTarget(
            kind="url",
            value=f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
            headers={"Authorization": f"Bearer {self._token}"})

    def resolve_play_target(self, rel: str) -> mount_lib.PlayTarget:
        """换链口别名（与 resolve_final 同目标；播放选路不在 P3 范围）"""
        return self.resolve_final(rel)

    def read_text(self, rel: str) -> str:
        """旁挂文本（.nfo 等）：按段下载全文解码；失败抛 MountError"""
        item = self._locate_file(rel)
        file_id = str(item.get("id") or "")
        import httpx

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
                    headers={"Authorization": f"Bearer {self._token}"})
        except Exception as exc:  # noqa: BLE001 — 网络失败 = 一过性
            raise mount_lib.MountError(f"Drive 旁挂读取网络失败：{exc}") from exc
        if resp.status_code in (401, 403):
            raise mount_lib.MountAuthError(
                f"Drive 令牌失效（HTTP {resp.status_code}），请重新绑定。")
        if resp.status_code >= 400:
            raise mount_lib.MountError(
                f"Drive 旁挂读取失败（HTTP {resp.status_code}）。")
        return resp.content.decode("utf-8-sig", errors="ignore")


def load_binding_for_scan(db: Session, user_id: int,
                          binding_id: int) -> em.UserStorageBinding:
    """扫描用绑定行：本人行才认；非本人/不存在统一按 not_found 处理
    （不泄露他人绑定存在性），未 active 按状态分 invalid/expired，
    绝不回落到该用户的其他绑定——回落就是跨绑定复用凭据。"""
    binding = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.id == binding_id,
                em.UserStorageBinding.user_id == user_id)
        .first()
    )
    if binding is None:
        raise BindingScanError("not_found", "绑定不存在或不属于当前用户。")
    if binding.status != "active":
        kind = "expired" if binding.status == "expired" else "invalid"
        raise BindingScanError(
            kind, f"绑定当前状态为 {binding.status}，请先重新验证绑定。")
    return binding


def binding_provider(db: Session, binding: em.UserStorageBinding,
                     http_get=None, http_post=None):
    """绑定 → 列举提供者（凭据只取该绑定自己的行）。

    - drive：P2 绑定级令牌口（不做“最早 active 绑定”查找）；
    - pan115：P1 绑定级 Cookie 口 + cookie_override 实例（P1 已保证
      该实例永不读写共享目录缓存，见 mounts cached_listing 的
      _byos_no_cache 分支）；
    - 其余 provider：unsupported（不猜、不回落）。
    """
    if binding.provider == ub.PROVIDER_DRIVE:
        from backend.emby_server import drive_oauth

        try:
            token = drive_oauth.get_binding_drive_access_token(
                db, binding, http_post=http_post)
        except drive_oauth.DriveOAuthError as exc:
            raise BindingScanError(exc.kind, str(exc)) from exc
        return UserDriveEnumerator(token, http_get=http_get)
    if binding.provider == ub.PROVIDER_PAN115:
        try:
            cookie = ub.binding_115_cookie(db, binding.user_id, binding.id)
        except ub.BindingError as exc:
            raise BindingScanError(exc.kind, exc.message) from exc
        # 桩挂载行：Pan115Mount 只读其 config（cid，缺省根目录 "0"）；
        # 凭据全靠 cookie_override，不读挂载配置/账号档/服务器级 Cookie。
        stub_mount = SimpleNamespace(config="{}")
        return mount_lib.Pan115Mount(stub_mount, db=None,
                                     cookie_override=cookie)
    raise BindingScanError(
        "unsupported", f"该 provider 不支持绑定扫描：{binding.provider}")


def iter_binding_scan_files(provider, binding_id: int,
                            root: str = "/") -> Iterator[scanner.ScanFile]:
    """提供者列举 → 扫描体 ScanFile（只做形态转换，不做认亲/去重）。

    - stored_path：``byos://<binding_id>/<rel>``（分账命名空间）；
    - file_id / content_hash / etag / revision：MountFile 原样透传，
      扫描体写库循环按现行口径落列（Drive 写 provider_file_id、115
      留空、content_hash 落列但不查库）；
    - source_row_id：None——没有 library_sources 行，不确定的锚留空
      绝不猜（扫描体只在非 None 时写 source_id）；
    - .strm：容器按现行 _remote_strm_container 口径从直链推断，推断
      不出按 strm 记，播放时再解析。
    """
    for mount_file in provider.walk_media(root=root):
        rel = mount_file.rel
        dir_rel = os.path.dirname(rel) or "/"
        container = os.path.splitext(mount_file.name)[1].lstrip(".").lower()
        if container == "strm":
            try:
                target = provider.resolve_final(rel)
                container = mount_lib.strm_container(target.value)
            except Exception:  # noqa: BLE001 — 直链失效不阻断入库
                container = "strm"
        yield scanner.ScanFile(
            stored_path=binding_path(binding_id, rel),
            name=mount_file.name, local_dir=None, dir_rel=dir_rel,
            size=mount_file.size, container=container,
            mount_id=None, rel=rel, provider=provider,
            file_id=getattr(mount_file, "file_id", "") or "",
            source_row_id=None,
            remote_mod_ts=getattr(mount_file, "mod_ts", 0.0) or 0.0,
            content_hash=getattr(mount_file, "content_hash", "") or "",
            content_hash_algo=getattr(mount_file, "content_hash_algo", "") or "",
            etag=getattr(mount_file, "etag", "") or "",
            revision=getattr(mount_file, "revision", "") or "",
        )


def prune_binding_items(db: Session, library_id: int, binding_id: int,
                        seen_paths, *, listing_complete: bool) -> dict:
    """绑定线定向清理：只删**本绑定、本库**的条目，绝不触发整库清理。

    口径照抄主线 ``scanner._remove_missing_items``，只换作用域：

    - 清单不完整（``listing_complete=False``：列举中途抛错、扫描中途
      失败、子树扫描）→ 本轮什么都不删（``skipped="listing_incomplete"``
      或调用方在子树时直接跳过——见 ``scan_binding`` 的 root 门）；
    - 空清单（本轮一个文件都没列到、但库里该绑定线还有可见条目）→
      拒绝批量删（主线“空清单禁止删库”保护的绑定线版：挂载 3 教训，
      读不到源时删条目是事故）；
    - 缺失宽限（主线批一 #6）：未见行先 ``missing_count + 1``，连续
      缺失达到 ``SCAN_MISSING_GRACE_SCANS``（默认 3）轮才下架；本轮
      又见到的行清零。单轮抖动不作数；
    - 下架走软删除（``MEDIA_SOFT_DELETE`` 关时走主线 ``_purge_items``
      硬删，与主线同一分支）；已下架行不重复处理；
    - 父行（剧/季）只清“因本轮下架而无可见子条目”的：候选只取本轮
      下架行的 ``series_id`` / ``parent_id``，且删前复查无可见子行——
      P3 已保证绑定集行不跨命名空间复用（同绑定重扫走 guid 命中），
      别的绑定/共享库的父行永远不在候选里；
    - 刻意**不做**主线的两件整库动作：不调 ``purge_expired``（它按库
      回收，会动别的绑定的过期行）；不摘 Drive 存在性正缓存（那是
      共享盘播放侧的机制，绑定线不碰）。

    ``seen_paths`` 是本轮完整列举到的入库路径集合（``byos://<id>/...``），
    由调用方在列举完整跑完的前提下传入；本函数不自己列举、不猜清单。
    返回只含 id 与计数，不含任何凭据材料。
    """
    result = {
        "binding_id": binding_id, "library_id": library_id,
        "skipped": None, "checked": 0, "resets": 0, "graced": 0,
        "removed": 0, "parents_removed": 0,
    }
    if not listing_complete:
        result["skipped"] = "listing_incomplete"
        logger.warning("绑定 #%s 清理跳过：清单不完整（本库 %s）",
                       binding_id, library_id)
        return result
    prefix = binding_path(binding_id, "/")  # "byos://<id>/"
    MI = em.MediaItem
    rows = db.execute(
        select(MI.id, MI.file_path, MI.item_type, MI.missing_count,
               MI.series_id, MI.parent_id)
        .where(MI.library_id == library_id,
               MI.file_path.like(prefix + "%"),
               MI.deleted_at.is_(None),
               MI.item_type.in_(["movie", "episode"]))
        .order_by(MI.id)
    ).all()
    # LIKE 在 SQLite 下大小写不敏感：再用精确前缀过滤一次，绝不认错绑定。
    rows = [r for r in rows if (r[1] or "").startswith(prefix)]
    result["checked"] = len(rows)
    seen = set(seen_paths or [])
    if not seen and rows:
        result["skipped"] = "empty_listing_refused"
        logger.warning(
            "绑定 #%s 清理拒绝：本轮列出 0 个文件，但库 %s 里该绑定线"
            "还有 %d 条可见条目——视为来源不可用，本轮不删",
            binding_id, library_id, len(rows))
        return result
    grace_scans = int(getattr(scanner, "MISSING_GRACE_SCANS", 3) or 3)
    reset_ids, grace_ids, doomed = [], [], []
    for item_id, file_path, item_type, missing_count, series_id, parent_id in rows:
        missing_count = missing_count or 0
        if file_path in seen:
            if missing_count > 0:
                reset_ids.append(item_id)
            continue
        if missing_count + 1 >= grace_scans:
            doomed.append((item_id, item_type, series_id, parent_id))
        else:
            grace_ids.append(item_id)
    if reset_ids:
        for i in range(0, len(reset_ids), 200):
            chunk = reset_ids[i:i + 200]
            db.execute(
                update(MI).where(MI.id.in_(chunk)).values(
                    missing_count=0, missing_since=None))
        result["resets"] = len(reset_ids)
    if grace_ids:
        now = datetime.now()
        for i in range(0, len(grace_ids), 200):
            chunk = grace_ids[i:i + 200]
            db.execute(
                update(MI).where(MI.id.in_(chunk)).values(
                    missing_count=MI.missing_count + 1,
                    missing_since=func.coalesce(MI.missing_since, now)))
        result["graced"] = len(grace_ids)
    removed = 0
    if doomed:
        doomed_ids = [d[0] for d in doomed]
        if _soft_delete.soft_delete_enabled():
            removed = _soft_delete.mark_soft_deleted(db, doomed_ids)
        else:
            scanner._purge_items(db, doomed_ids)
            removed = len(doomed_ids)
        result["removed"] = removed
        # 父行：只看本轮下架行的父行，且删前复查无可见子行。
        candidate_series = {d[2] for d in doomed if d[2]}
        candidate_seasons = {d[3] for d in doomed
                             if d[1] == "episode" and d[3]}
        season_doomed = []
        for season_id in sorted(candidate_seasons):
            has_child = db.execute(
                select(MI.id).where(
                    MI.parent_id == season_id,
                    MI.deleted_at.is_(None)).limit(1)).first()
            if not has_child:
                season_doomed.append(season_id)
                srow = db.execute(
                    select(MI.series_id).where(
                        MI.id == season_id).limit(1)).first()
                if srow and srow[0]:
                    candidate_series.add(srow[0])
        parents = season_doomed + []
        for series_id in sorted(candidate_series):
            has_child = db.execute(
                select(MI.id).where(
                    MI.series_id == series_id,
                    MI.deleted_at.is_(None)).limit(1)).first()
            if not has_child:
                parents.append(series_id)
        if parents:
            if _soft_delete.soft_delete_enabled():
                result["parents_removed"] = _soft_delete.mark_soft_deleted(
                    db, parents)
            else:
                scanner._purge_items(db, parents)
                result["parents_removed"] = len(parents)
    if reset_ids or grace_ids or doomed:
        db.commit()
    if result["removed"] or result["parents_removed"]:
        # 计数与主线清理收尾同口径：可见 movie/series 重数。
        lib = db.query(em.Library).filter(
            em.Library.id == library_id).first()
        if lib is not None:
            lib.item_count = _soft_delete.count_visible(
                db,
                MI.library_id == library_id,
                MI.item_type.in_(["movie", "series"]),
                MI.deleted_at.is_(None),
                MI.merged_into_id.is_(None),
            )
            db.commit()
    return result


def scan_binding(db: Session, user_id: int, binding_id: int, library_id: int,
                 *, root: str = "/", provider=None,
                 http_get=None, http_post=None) -> dict:
    """按绑定扫描入库（BYOS P3 唯一入口）。

    - 总开关关着 → disabled，什么都不做；
    - 库不存在 → not_found；绑定非本人/不存在 → not_found；
    - 绑定未 active → invalid/expired，不回落其他绑定；
    - 扫描体以定向模式跑（targeted=True）：整库清理阶段跳过——
      绑定线绝不因“本轮只看见一个绑定”而删掉库里别的来源的条目；
    - 扫描成功后按绑定定向清理（``prune_binding_items``）：只删本绑定、
      本库的未见行，且只在**整绑定列举**（``root="/"``）完整跑完时
      才做——子树扫描、列举中途抛错、扫描本身失败时一律跳过清理；
      空清单时拒绝批量删（见 prune 的 fail-closed 门）；
    - 列举中凭据失效（MountAuthError）→ 绑定落 expired 后抛 expired，
      不静默换源、不静默走中继（Drive 令牌刷新失败的落态由 P2 口
      自己负责，本处只处理列举期 401/403）。
    返回统计（只有 id 与计数，不含任何凭据材料）；``prune`` 一项是
    定向清理的结果（``skipped`` 非空 = 本轮没清理，原因见值）。
    """
    if not scan_enabled():
        raise BindingScanError(
            "disabled", "绑定扫描未开启（BYOS_SCAN_ENABLED=0），行为与今天一致。")
    library = (
        db.query(em.Library).filter(em.Library.id == library_id).first()
    )
    if library is None:
        raise BindingScanError("not_found", "媒体库不存在。")
    binding = load_binding_for_scan(db, user_id, binding_id)
    provider = provider if provider is not None else binding_provider(
        db, binding, http_get=http_get, http_post=http_post)
    label = f"BYOS 绑定 #{binding.id}（{binding.provider}）"
    # LibrarySnapshot 是 frozen dataclass：定向快照用 replace 重建，
    # 不改原快照（原快照仍是库的现行配置，调用方复用它时不受影响）。
    snap = _dataclass_replace(scanner.LibrarySnapshot.of(library),
                              targeted=True)
    # 录下本轮列到的入库路径：只有生成器完整跑完（没抛错、没被提前
    # 截断），listing_complete 才为真——半张清单绝不拿去删条目。
    seen_paths: set = set()
    listing_state = {"complete": False}

    def _recording(files):
        for scan_file in files:
            seen_paths.add(scan_file.stored_path)
            yield scan_file
        listing_state["complete"] = True

    files = _recording(iter_binding_scan_files(provider, binding.id, root=root))
    try:
        stats = scanner.scan_library_sync(
            db, library, snap, trigger="byos_binding",
            source_override=[(label, files)])
    except mount_lib.MountAuthError as exc:
        # 列举期凭据失效：落 expired + 响亮报错（P2 刷新失败已在 P2 口
        # 内落态，此处补列举期 401/403 的落态；一过性 MountError 不落态）。
        try:
            ub._mark_failed(db, binding, "expired")
            db.commit()
        except Exception:  # noqa: BLE001 — 落态失败不掩盖原错
            logger.warning("绑定 #%s 失效落态失败", binding.id, exc_info=True)
        raise BindingScanError("expired", ub.error_message("expired")) from exc
    except ub.BindingError as exc:
        raise BindingScanError(exc.kind, exc.message) from exc
    # 扫描成功才谈清理；子树扫描的清单天生不完整，绝不拿它删行。
    norm_root = "/" + (root or "").strip("/")
    if norm_root == "/":
        try:
            prune = prune_binding_items(
                db, library.id, binding.id, seen_paths,
                listing_complete=listing_state["complete"])
        except Exception as exc:  # noqa: BLE001 — 清理失败不掩盖扫描成功
            logger.warning("绑定 #%s 定向清理失败（本轮不删任何行）: %s",
                           binding.id, exc, exc_info=True)
            prune = {"binding_id": binding.id, "library_id": library.id,
                     "skipped": "error", "checked": 0, "resets": 0,
                     "graced": 0, "removed": 0, "parents_removed": 0}
    else:
        prune = {"binding_id": binding.id, "library_id": library.id,
                 "skipped": "subtree_root", "checked": 0, "resets": 0,
                 "graced": 0, "removed": 0, "parents_removed": 0}
        logger.info("绑定 #%s 子树扫描（root=%s），跳过定向清理",
                    binding.id, root)
    return {
        "binding_id": binding.id,
        "user_id": user_id,
        "provider": binding.provider,
        "library_id": library.id,
        "stats": stats,
        "prune": prune,
    }
