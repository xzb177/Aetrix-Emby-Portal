"""Google Drive 通用挂载（``drive``）：Drive API 直调列目录，不经 rclone

为什么新开一种类型，而不是继续用 rclone 挂载扫共享盘：

- rclone 的配置里绑死**一个**账号（一个 SA 文件或一份 OAuth），一轮全量扫描的
  几千次列目录请求全压在这一个账号头上——既吃它的 per-user 配额，也是最容易
  被 Google 风控盯上的机器行为模式。
- 本类型列目录走 ``drive_changes`` 的统一凭据层（横切只许一套）：
  ``get_token_for_drive`` 按盘取凭据——共享盘是 100 个服务账号轮换（带随机步长
  打破机器节拍）、个人盘 ``myDrive:<remote>`` 用该 remote 自己的 OAuth；
  ``_api_get`` 自带全局令牌桶限流（默认 8 QPS）与 429/5xx/配额 403 的
  风控安全退避。本模块不自带第二套轮换/重试/限流实现。
- 文件的 Drive id 直接透传成 ``MountEntry.file_id`` → 扫描器写
  ``item.drive_file_id`` → 播放走 302 直链分支（与 rclone 挂载的条目完全同构）。
  ``resolve()`` 只剩探测（ffprobe）与 .strm 内容读取在用。

路径与配置：

- ``path`` 形如 ``drive:<drive_id>``，挂载根 = 盘根。共享盘的 drive_id 是
  共享盘 ID 本身（其根目录 id 与盘 id 相同）；个人盘写
  ``drive:myDrive:<rclone remote 名>``，根目录 id 为 ``root``。
- ``config.drive_id`` 可覆盖路径里的盘 ID（路径写错时不用改路径也能救）。
- ``config.root_id`` 可选：把挂载根钉在某个子文件夹 id 上（默认盘根）。

失败语义（与扫描器的防误删链对齐）：

- 列目录失败一律抛 ``MountError``：根目录失败 → 来源不可用；子目录失败 →
  ``RemoteMount.walk_media`` 收集后统一抛错，扫描器记 ``failed_roots``
  并跳过本轮清理。**绝不**把「读不到」返回成空目录。
- 凭据问题（DriveAuthError / token 刷新后仍 401）抛 ``MountAuthError``，
  后台测试连接会明确提示是凭据问题而不是网络问题。

注意：Drive 允许同一目录下有同名文件/文件夹。列举展示时同名文件夹只认
第一个（与 rclone 按名寻址的行为一致）；但路径解析（``_fid_of`` 经由的
``_locate``/``resolve``/``exists``）在按首个匹配走入死胡同时（子目录名
不存在、或目标文件名不在该文件夹中），会回溯尝试其它同名文件夹——
合法路径一定能定位到。产出的 ``rel`` 若重名会撞同一条路径，扫描器侧
有 guid 去重兜底。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import FrozenSet, Optional

from backend.emby_server import drive_auth, drive_changes
from backend.emby_server import mounts as mount_lib
from backend.emby_server.mounts import (
    MOUNT_DRIVE,
    MountAuthError,
    MountEntry,
    MountError,
    PlayTarget,
    RemoteMount,
    cached_listing,
)

logger = logging.getLogger(__name__)

#: Drive 文件夹的 MIME 类型
FOLDER_MIME = "application/vnd.google-apps.folder"
#: files.list 端点（与 drive_changes 同一地址）
FILES_URL = "https://www.googleapis.com/drive/v3/files"
#: 列目录取的字段：含 modifiedTime（扫描器文件指纹要用）
_LIST_FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime,trashed)"
_PAGE_SIZE = 1000
#: 单次列举内钉住 SA 时 token 的最小剩余寿命（秒）：超大目录枚举可能耗时久，
#: 索取长寿 token；万一中途仍 401，由同 SA 刷新续跑（Google 的 pageToken
#: 与请求身份绑定，第 2 页换 SA 会 400——2026-10-17 实证）
_PIN_TOKEN_MIN_REMAINING = 1800.0


def _parse_mod_ts(value: object) -> float:
    """Drive 的 modifiedTime（RFC3339）→ Unix 秒；解析不了返回 0（指纹退化为尺寸）"""
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class _PathDeadEnd(MountError):
    """内部信号：按名逐级解析走入死胡同（某级名在已选文件夹下不存在）。

    仅供 ``_fid_of`` 的同名回溯捕获；它是 ``MountError`` 的子类，对外
    ``except MountError`` 的调用方行为不变。列举 API 本身失败抛的是普通
    ``MountError``，不会被回溯逻辑吞掉。
    """


class DriveMount(RemoteMount):
    """Google Drive 挂载：逐级按名解析目录，files.list 分页列举"""

    mount_type = MOUNT_DRIVE
    kind = "remote"
    what = "Google 云盘"

    def __init__(self, mount, db=None, library=None):
        super().__init__(mount, db, library)
        raw_path = (getattr(mount, "path", "") or "").strip()
        path_drive_id = (
            raw_path[len("drive:"):].strip() if raw_path.startswith("drive:") else ""
        )
        # config 显式给的 drive_id 优先（救路径写错的场景），都没有就当场报错
        self.drive_id = str(self.config.get("drive_id") or path_drive_id).strip()
        if not self.drive_id:
            raise MountError(
                "Google 云盘挂载缺少盘 ID：路径应为 drive:<共享盘 ID>，"
                "个人盘为 drive:myDrive:<rclone remote 名>")
        self.personal = drive_changes._is_personal_drive_key(self.drive_id)
        root_override = str(self.config.get("root_id") or "").strip()
        # 共享盘根目录 id == 盘 id；个人盘根目录是固定的 "root"
        self.root_id = root_override or ("root" if self.personal else self.drive_id)
        # rel → folder id（只在本次提供者实例内；list_dir 的共享 TTL 缓存另在基类层）
        self._fid_cache: dict[str, str] = {"/": self.root_id}

    # ---- 凭据与列举 ----

    def _token(self, force_refresh: bool = False) -> str:
        """按盘取凭据（共享盘 SA 轮换 / 个人盘 OAuth），取不到按凭据错误报"""
        try:
            token = drive_changes.get_token_for_drive(
                self.drive_id, force_refresh=force_refresh)
        except drive_changes.DriveAuthError as exc:
            raise MountAuthError(str(exc)) from exc
        if not token:
            raise MountAuthError(
                "无可用 Drive 凭据：服务账号池为空，或个人盘授权已失效")
        return token

    def _pin_listing_cred(self) -> tuple[Optional[str], Optional[dict]]:
        """单次列举（``_list_all`` 一次调用）内钉住的凭据。

        - 个人盘：固定 OAuth，返回 ``(token, None)``——不轮换，本来就钉住了。
        - 共享盘：轮换出一个 SA 并按其索取长寿 token，返回 ``(token, sa)``；
          后续每页复用，Google 分页 pageToken 与 SA 身份绑定，钉住才能翻页。
        凭据不可用 → MountAuthError（与 ``_token`` 同款）。
        """
        if self.personal:
            return self._token(), None
        sa = drive_changes.next_shared_sa()
        if not sa:
            raise MountAuthError(
                "无可用 Drive 凭据：服务账号池为空，或个人盘授权已失效")
        token = drive_changes._sa_access_token(
            sa, min_remaining=_PIN_TOKEN_MIN_REMAINING)
        if not token:
            raise MountAuthError(
                f"服务账号取 token 失败: {sa.get('client_email', '')}")
        return token, sa

    def _list_page(self, folder_id: str, page_token: Optional[str],
                   token: Optional[str] = None,
                   sa: Optional[dict] = None) -> tuple[dict, str]:
        """一页 files.list。401 时刷新凭据重试一次；其余失败翻译成 MountError，
        绝不吞成空页。

        - ``sa`` 给出（单次列举钉住模式）：401 时对**同一 SA**强制刷新续跑，
          返回新 token（后续页沿用，pageToken 不失效）；
        - ``sa`` 为空（其他调用方）：沿用原 force_refresh 语义（共享盘再轮换
          一个 SA、个人盘刷新 OAuth），行为不变。

        返回 ``(body, 实际用到的 token)``。
        """
        params: dict = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "pageSize": _PAGE_SIZE,
            "fields": _LIST_FIELDS,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        if self.personal:
            params["spaces"] = "drive"
        else:
            params["driveId"] = self.drive_id
            params["corpora"] = "drive"
        if token is None:
            token = self._token()
        try:
            return drive_changes._api_get(FILES_URL, token, params), token
        except PermissionError:
            # token 中途过期（长遍历跨过有效期）：刷新一次续跑
            if sa is not None:
                fresh = drive_changes.refresh_sa_token(
                    sa, min_remaining=_PIN_TOKEN_MIN_REMAINING)
                if not fresh:
                    raise MountAuthError(
                        "Drive 凭据已失效（同 SA 刷新失败），请检查服务账号池"
                    )
                try:
                    return (drive_changes._api_get(FILES_URL, fresh, params),
                            fresh)
                except PermissionError as exc:
                    raise MountAuthError(
                        "Drive 凭据已失效（刷新后仍被拒绝），请检查服务账号池"
                    ) from exc
            try:
                fresh_token = self._token(force_refresh=True)
                return (drive_changes._api_get(FILES_URL, fresh_token, params),
                        fresh_token)
            except PermissionError as exc:
                raise MountAuthError(
                    "Drive 凭据已失效（刷新后仍被拒绝），请检查服务账号池或个人盘授权"
                ) from exc

    def _list_all(self, folder_id: str) -> list[dict]:
        """一个目录的全部条目（分页拉完为止）。

        整个翻页过程钉住同一 SA（共享盘）：开头轮换出一个 SA、后续每页复用；
        Google 的 pageToken 与请求身份绑定，换 SA 会 400。个人盘 OAuth 本来
        就不轮换。401 续跑沿用钉住语义（同 SA 刷新）。
        """
        items: list[dict] = []
        page_token: Optional[str] = None
        token, sa = self._pin_listing_cred()
        while True:
            try:
                body, token = self._list_page(folder_id, page_token,
                                              token=token, sa=sa)
            except MountAuthError:
                raise
            except drive_changes.DriveNotFoundError as exc:
                raise MountError(
                    f"目录不存在或无权访问（folder={folder_id}）："
                    "盘 ID 写错、目录被删、或服务账号不在该盘成员里都会这样") from exc
            except Exception as exc:  # noqa: BLE001 — _api_get 已重试过，剩下的是终局失败
                raise MountError(f"列目录失败（folder={folder_id}）: {exc}") from exc
            items.extend(body.get("files") or [])
            page_token = body.get("nextPageToken")
            if not page_token:
                return items

    def _fid_of(self, rel: str, exclude: FrozenSet[str] = frozenset()) -> str:
        """把挂载内相对路径逐级解析成 folder id（仿 115 的 _cid_of）。

        同名文件夹回溯：Drive 允许同级目录下出现同名文件夹。逐级按名
        解析先走快速路径（每级取首个匹配，与旧实现一致）；若走入死胡同
        （后续某级名在已选文件夹下不存在），改用带回溯的逐级解析，在
        最近的有多个同名候选的层级换下一个候选再试——合法路径一定能
        定位到。无歧义路径的行为、报错与缓存和旧实现完全一致；只有歧义
        路径才付出额外的列举开销。

        ``exclude``：调用方已排除的文件夹 id 集合（_locate 在文件名不
        匹配时逐个排除候选文件夹）；非空时跳过快速路径与 _fid_cache，
        直接走带排除的回溯解析。
        """
        try:
            return self._resolve_folder_id(rel, exclude)
        except _PathDeadEnd as exc:
            raise MountError(f"Google 云盘上找不到目录: {rel}") from exc

    def _resolve_folder_id(self, rel: str,
                           exclude: FrozenSet[str] = frozenset()) -> str:
        """原始解析：死胡同抛 _PathDeadEnd（供 _find_file 回溯/排除用）；
        列举 API 失败抛普通 MountError，直接上抛。"""
        rel = ("/" + (rel or "").lstrip("/")).rstrip("/") or "/"
        if not exclude and rel in self._fid_cache:
            return self._fid_cache[rel]
        parts = [p for p in rel.split("/") if p]
        if not exclude:
            try:
                return self._fid_of_fast(parts, rel)
            except _PathDeadEnd:
                pass  # 死胡同：改用回溯解析
        trail: list[tuple[str, str, bool]] = []
        fid = self._walk_parts(parts, 0, self.root_id, rel, trail, exclude)
        # 只有整条路径各级都无歧义（每级恰一个同名候选）才缓存各级 rel，
        # 与旧实现一致；歧义路径不缓存，避免把某次回溯选中的文件夹钉死
        # 成该 rel 的"标准答案"。
        if not exclude and trail and all(unambiguous for _, _, unambiguous in trail):
            for level_rel, level_fid, _ in trail:
                self._fid_cache[level_rel] = level_fid
        return fid

    def _fid_of_fast(self, parts: list[str], rel: str) -> str:
        """旧实现原样：逐级取首个同名文件夹；死胡同抛 _PathDeadEnd。"""
        fid = self.root_id
        current = ""
        for part in parts:
            parent_rel = current or "/"
            current = f"{current}/{part}"
            if current in self._fid_cache:
                fid = self._fid_cache[current]
                continue
            child = next(
                (e for e in self.list_dir(parent_rel)
                 if e.name == part and e.is_dir),
                None,
            )
            if child is None or not child.entry_id:
                raise _PathDeadEnd(rel)
            fid = child.entry_id
            self._fid_cache[current] = fid
        return fid

    def _walk_parts(self, parts: list[str], idx: int, folder_id: str,
                    full_rel: str,
                    trail: list[tuple[str, str, bool]],
                    exclude: FrozenSet[str] = frozenset()) -> str:
        """带同名回溯的逐级解析；死胡同抛 _PathDeadEnd 由上层换候选。

        列举 API 本身失败抛普通 MountError，直接上抛不回溯（那不是
        "名不存在"，换候选也救不了）。``exclude`` 里的文件夹 id 永不
        入选（_locate 逐个排除已查无文件的候选文件夹时用）。
        """
        if idx >= len(parts):
            return folder_id
        part = parts[idx]
        level_rel = "/" + "/".join(parts[: idx + 1])
        entries = self._list_by_id(folder_id)  # MountError 上抛：列举失败不回溯
        cands = [e for e in entries
                 if e.is_dir and e.name == part and e.entry_id
                 and e.entry_id not in exclude]
        if not cands:
            raise _PathDeadEnd(full_rel)
        last_dead: Optional[_PathDeadEnd] = None
        for cand in cands:
            trail.append((level_rel, cand.entry_id, len(cands) == 1))
            try:
                return self._walk_parts(parts, idx + 1, cand.entry_id,
                                        full_rel, trail, exclude)
            except _PathDeadEnd as exc:
                trail.pop()
                last_dead = exc
                continue
        assert last_dead is not None  # cands 非空，必有一次死胡同
        raise last_dead

    def _list_by_id(self, folder_id: str) -> list[MountEntry]:
        """按 folder id 直接列目录（供回溯解析用：不经过名解析、不写 _fid_cache）。"""
        return self._entries_of(folder_id, "", cache_children=False)

    # ---- 提供者接口 ----

    def test(self) -> dict:
        entries = self.list_dir("/")
        kind = "个人盘" if self.personal else "共享盘"
        return {
            "ok": True,
            "message": f"连接正常（{kind}，盘根下有 {len(entries)} 项）",
            "drive_id": self.drive_id,
        }

    @cached_listing
    def list_dir(self, rel: str = "/") -> list[MountEntry]:
        folder_id = self._fid_of(rel)
        prefix = ("/" + (rel or "").lstrip("/")).rstrip("/")
        return self._entries_of(folder_id, prefix, cache_children=True)

    def _entries_of(self, folder_id: str, prefix: str,
                    cache_children: bool) -> list[MountEntry]:
        """把一个 folder id 的 files.list 结果建成 MountEntry 列表。

        cache_children=True（list_dir 路径）：子目录的 rel→id 按首个匹配
        写入 _fid_cache（与旧行为一致）；回溯解析路径传 False，不污染缓存。
        """
        entries: list[MountEntry] = []
        for item in self._list_all(folder_id):
            name = item.get("name") or ""
            item_id = str(item.get("id") or "")
            if not name or not item_id:
                continue
            is_dir = item.get("mimeType") == FOLDER_MIME
            child_rel = f"{prefix}/{name}"
            if is_dir and cache_children:
                self._fid_cache.setdefault(child_rel, item_id)
            entries.append(MountEntry(
                name=name,
                rel=child_rel,
                is_dir=is_dir,
                size=0 if is_dir else int(item.get("size") or 0),
                entry_id=item_id if is_dir else "",
                # 目录的 file_id 必须为空串（entry_id 已是目录标识）——
                # 与 rclone 挂载同一条约定，扫描器靠它区分文件身份
                file_id="" if is_dir else item_id,
                mod_ts=_parse_mod_ts(item.get("modifiedTime")),
            ))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def walk_workers(self) -> int:
        # Drive 是 per-user 配额（100 秒 / 1 万次量级）：_api_get 已有全局
        # 8 QPS 令牌桶兜底，这里再把本挂载的并发压到 4，避免一轮全量遍历
        # 把令牌桶长期占满、饿着播放探测与追新。
        return 4

    def _locate(self, rel: str) -> MountEntry:
        """按 rel 定位文件条目（resolve 用）。

        同名文件夹的文件级回溯：父目录解析到首个候选文件夹后，若目标
        文件名不在其中，把该文件夹加入排除集、换下一个同名候选再试——
        合法路径一定能定位到；全部候选查完仍无文件才报"找不到文件"。
        """
        parent, _, name = (rel or "").rstrip("/").rpartition("/")
        entry = self._find_file(parent or "/", name)
        if entry is None:
            raise MountError(f"Google 云盘上找不到文件: {rel}")
        return entry

    def _find_file(self, parent: str, name: str) -> Optional[MountEntry]:
        """在 parent 下找文件；同名文件夹歧义时逐个候选文件夹试。

        找到返回条目；全部候选查完无文件返回 None（_locate 转报错、
        exists 转 False）。列举 API 失败抛 MountError 上抛，不吞。
        """
        if not name:
            return None
        excluded: set[str] = set()
        while True:
            try:
                folder_id = self._resolve_folder_id(
                    parent, frozenset(excluded))
            except _PathDeadEnd:
                return None
            for entry in self._list_by_id(folder_id):  # MountError 上抛：列举失败不吞
                if entry.name == name and not entry.is_dir:
                    return entry
            if folder_id in excluded:
                return None  # 防御：候选已穷尽仍无文件
            excluded.add(folder_id)

    def exists(self, rel: str) -> bool:
        """文件/目录是否存在；同名文件夹歧义时逐个候选找，直到命中为止。

        覆盖基类实现：基类只查首个匹配的文件夹，同名文件夹下的文件会被
        误判为不存在（扫描器清理阶段据此标记删除活文件）。
        """
        parent, _, name = (rel or "").rstrip("/").rpartition("/")
        if not name:
            return False  # 与基类一致：空名按不存在
        try:
            excluded: set[str] = set()
            while True:
                try:
                    folder_id = self._resolve_folder_id(
                        parent or "/", frozenset(excluded))
                except _PathDeadEnd:
                    return False
                # 基类按名匹配不分文件/目录，这里保持一致
                if any(e.name == name for e in self._list_by_id(folder_id)):
                    return True
                if folder_id in excluded:
                    return False  # 防御：候选已穷尽
                excluded.add(folder_id)
        except MountError:
            return False  # 与基类一致：列举失败按不存在

    def resolve(self, rel: str) -> PlayTarget:
        """探测/.strm 读取用的解析：usercontent 直链 + Bearer 头（不下发客户端）。

        正常播放不走这里：带 drive_file_id 的条目在 api 层直接 302。
        """
        entry = self._locate(rel)
        if not entry.file_id:
            raise MountError(f"该文件没有 Drive id，无法解析: {rel}")
        url = drive_auth.drive_usercontent_download_url(entry.file_id)
        return PlayTarget(
            "url", url, drive_auth.drive_auth_headers(url, entry.file_id))


PROVIDERS = {MOUNT_DRIVE: DriveMount}


def register() -> None:
    """注册 drive 提供者（幂等）。类型元数据在 mounts.py 里，与另外三种放在一起。"""
    mount_lib.register_providers(PROVIDERS)
    logger.debug("已注册 Google 云盘挂载提供者")


# 模块导入即注册：无论谁先被导入（mounts 或本模块），提供者表都是齐的
register()
