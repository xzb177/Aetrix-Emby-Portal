"""宿主侧 sidecar provider：把 UDS 运行时契约接回 MountProvider 接口。

本模块是 115 sidecar 在宿主进程里的唯一接线点（139 那一路照此再写一个
``Yun139SidecarMount``，复用 :class:`SidecarMountProvider`，不另起传输层）。

关键口径（与 provider_runtime 的契约一一对应）：

- 默认开：``PAN115_SIDECAR_ENABLED`` 不设即开（测试注入 client 的缝除外，
  生产路径永不注入）；显式设 ``0`` 才关，关时构造即 ``MountError``，
  不静默回落进程内 provider——回落是调用方的显式决定，不是本模块的暗度陈仓。
- 分发（生产接线）：``mounts.build_provider`` 对 ``115`` 类型挂载在开闸时
  返回 ``P115SidecarMount``、关闸时返回进程内 ``Pan115Mount``；本模块仍
  **不**往 ``_PROVIDERS`` 注册表里加新类型（``115_sidecar`` 不是挂载类型，
  sidecar 只是 ``115`` 的运行时形态）。直接显式构造 ``P115SidecarMount``
  的调用方同样受总闸管。
- 能力映射（manifest → ProviderCapabilities，139 复用同一张表）：
  ``list_page`` → ``can_list``；``open_range`` → ``can_random_read``；
  ``open_range``（字节取文本）→ ``can_read_sidecar``（sidecar 无独立文本口，
  文本旁挂走 open_range 解码，与 FMBY display_asset 复用 open_range 同口径）；
  ``resolve_play_target`` → ``can_generate_play_target``；
  ``can_refresh_credentials`` 恒 False（115 Cookie 静态，会话重建不是刷新）。
- 凭据：Cookie/UA/根 cid 的解析**复用**进程内 ``Pan115Mount``（同一份优先级，
  不另写第二套解析）；会话建立时 Cookie 经 ``session_upsert`` 交给 sidecar
  进程内存；``resolve_play_target`` 回包按契约不含 Cookie，宿主在这里用自己
  现取的 Cookie 重贴（与 Pan115Mount 的 cacheable/reattach 纪律同源），
  凭据绝不以 sidecar 回包形态落缓存。
- 不碰红线：本模块不做播放变体选路、不做 BYOS 绑定、不做 #13 认亲、
  不做阶段 8 文件；它只把 sidecar 能力翻译成 MountProvider 的四口。
"""
from __future__ import annotations

import os
import uuid
from typing import Iterator, Optional

from backend.emby_server import disc_filter
from backend.emby_server.mounts import (
    MountEntry,
    MountError,
    MountFile,
    require_capability,
    MountProvider,
    Pan115Mount,
    PlayTarget,
    ProviderCapabilities,
    REMOTE_MEDIA_EXTS,
    _is_strm_name,
)
from backend.emby_server.provider_runtime import (
    BASE_CAPABILITIES,
    P115_EXTENSIONS,
    ProviderRuntimeClient,
    ProviderRuntimeError,
    decode_range_result,
)

PROVIDER_P115 = "p115"


def sidecar_enabled() -> bool:
    """115 sidecar 总闸（默认开：未设即开；显式设 0 关回进程内，是回滚手段；
    其它脏值按关处理，不静默开新路径）。"""
    return os.getenv("PAN115_SIDECAR_ENABLED", "1") == "1"


def manifest_to_capabilities(manifest: dict) -> ProviderCapabilities:
    """manifest 能力表 → ProviderCapabilities（139 复用同一张映射表）。"""
    caps = set((manifest or {}).get("capabilities") or [])
    return ProviderCapabilities(
        can_list="list_page" in caps,
        can_random_read="open_range" in caps,
        can_read_sidecar="open_range" in caps,
        can_generate_play_target="resolve_play_target" in caps,
        can_refresh_credentials=False,
    )


class SidecarMountProvider(MountProvider):
    """通用 sidecar provider 基类：UDS 客户端 + MountProvider 四口翻译。

    子类只定三样：``provider`` 名、``required_extensions``、会话数据
    （``_session_data()``）。传输、握手、分页、能力映射全部共用。
    """

    provider = ""
    required_extensions: tuple[str, ...] = ()
    mount_type = ""

    def __init__(self, mount, db=None, library=None, client=None,
                 socket_path: str | None = None, session_id: str | None = None):
        super().__init__(mount, db, library)
        if client is None and not sidecar_enabled():
            raise MountError(
                "115 sidecar 未启用（PAN115_SIDECAR_ENABLED=0）："
                "当前走进程内 Pan115Mount；要走 sidecar 请去掉该 0 值并重启服务"
                "（启动时会自动拉起 sidecar 进程，见 p115_sidecar_supervisor）。")
        self.client = client or ProviderRuntimeClient(self.provider, socket_path)
        # 握手即 fail-closed：协议/provider/8 项能力/扩展任一不符都拒绝接入。
        self.manifest = self.client.handshake(self.required_extensions)
        self._capabilities = manifest_to_capabilities(self.manifest)
        mount_id = getattr(mount, "id", None)
        self.session_id = session_id or (
            f"{self.provider}-mount-{mount_id}" if mount_id else
            f"{self.provider}-{uuid.uuid4().hex}")
        try:
            self.client.session_upsert(self.session_id, self._session_data())
        except ProviderRuntimeError as exc:
            raise MountError(f"sidecar 会话建立失败：{exc}") from exc

    # ---- 子类定 ----
    def _session_data(self) -> dict:
        raise NotImplementedError

    def _cookie(self) -> str:
        raise NotImplementedError

    # ---- 能力 ----
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    # ---- 会话 ----
    def close(self) -> None:
        """显式拆会话（best-effort；进程退出时 sidecar 侧会话随进程走，不强求）。"""
        try:
            self.client.session_remove(self.session_id)
        except Exception:  # noqa: BLE001 — 拆会话失败不影响调用方
            pass

    # ---- 目录 ----
    def list_dir(self, rel: str = "/") -> list[MountEntry]:
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        entries: list[MountEntry] = []
        token = None
        while True:
            try:
                page = self.client.list_page(self.session_id, rel, token)
            except ProviderRuntimeError as exc:
                raise MountError(f"sidecar 列目录失败：{exc}") from exc
            for item in page.get("entries") or []:
                entries.append(MountEntry(
                    name=item.get("name") or "",
                    rel=item.get("path") or "",
                    is_dir=bool(item.get("is_dir")),
                    size=int(item.get("size") or 0),
                    entry_id=item.get("entry_id") or "",
                    file_id=item.get("file_id") or "",
                ))
            token = page.get("next_page_token")
            if not token:
                break
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def walk_media(self, root: str = "/") -> Iterator[MountFile]:
        stack: list[str] = [root]
        seen: set[str] = set()
        while stack:
            rel = stack.pop()
            if rel in seen:
                continue
            seen.add(rel)
            for entry in self.list_dir(rel):
                if entry.is_dir:
                    if disc_filter.is_disc_subtree_dir(entry.name):
                        continue
                    stack.append(entry.rel)
                    continue
                if os.path.splitext(entry.name)[1].lower() not in REMOTE_MEDIA_EXTS:
                    continue
                yield MountFile(rel=entry.rel, name=entry.name, size=entry.size,
                                is_strm=_is_strm_name(entry.name),
                                file_id=entry.file_id or "")

    def stat(self, rel: str = "/") -> dict:
        """sidecar stat 直通（MountProvider 基类没有 stat，本口是 sidecar 专属加法）。"""
        try:
            return self.client.stat(self.session_id, rel)
        except ProviderRuntimeError as exc:
            raise MountError(f"sidecar stat 失败：{exc}") from exc

    # ---- 换链 / 搬字节 / 旁挂（MountProvider 四口的 sidecar 翻译） ----
    def resolve_play_target(self, rel: str) -> PlayTarget:
        try:
            result = self.client.resolve_play_target(self.session_id, rel)
        except ProviderRuntimeError as exc:
            raise MountError(f"sidecar 换链失败：{exc}") from exc
        url = result.get("url") or ""
        if not url:
            raise MountError(f"sidecar 未返回播放地址：{rel}")
        headers = dict(result.get("headers") or {})
        # 契约规定回包不含 Cookie：宿主用自己现取的 Cookie 重贴（不信任回包凭据）。
        headers["Cookie"] = self._cookie()
        return PlayTarget("url", url, headers)

    def open_range(self, rel: str, start: int, end: Optional[int] = None) -> bytes:
        # FMBY #23 门控：manifest 未含 open_range 时本提供者的
        # can_random_read 为 False（见 manifest_to_capabilities），
        # 此时不向 sidecar 发请求；文本旁挂走本口解码，同样被挡。
        require_capability(self, "can_random_read")
        if start < 0 or (end is not None and end <= start):
            raise MountError(f"无效的取段范围：start={start} end={end}")
        try:
            result = self.client.open_range(
                self.session_id, rel, start,
                None if end is None else end - start)
        except ProviderRuntimeError as exc:
            raise MountError(f"sidecar 按段读取失败：{exc}") from exc
        return decode_range_result(result)

    def read_text(self, rel: str) -> str:
        """文本旁挂经 open_range 取字节后解码（sidecar 无独立文本口）。"""
        return self.open_range(rel, 0, None).decode("utf-8", errors="ignore")

    def refresh_credentials(self) -> bool:
        # 115 Cookie 静态：会话重建不是凭据刷新，与进程内 Pan115Mount 的 False 声明一致。
        return False

    def test(self) -> dict:
        try:
            health = self.client.health(self.session_id)
        except ProviderRuntimeError as exc:
            raise MountError(f"sidecar 健康检查失败：{exc}") from exc
        return {"ok": bool(health.get("ok")),
                "message": f"sidecar 正常（provider={health.get('provider')}）",
                "provider": health.get("provider")}


class P115SidecarMount(SidecarMountProvider):
    """115 sidecar 的宿主侧 provider（显式构造，不进 build_provider 分发）。"""

    provider = PROVIDER_P115
    required_extensions = P115_EXTENSIONS
    mount_type = "115_sidecar"

    def __init__(self, mount, db=None, library=None, client=None,
                 socket_path: str | None = None, session_id: str | None = None,
                 cookie_override: str | None = None):
        # 凭据解析复用进程内 Pan115Mount（同一份优先级，不另写第二套）：
        # 绑定凭据（cookie_override）> 账号档/挂载配置 > 服务器级 PAN115_COOKIE。
        self._inproc = Pan115Mount(mount, db, library, cookie_override=cookie_override)
        self._cookie_override = cookie_override
        super().__init__(mount, db, library, client=client,
                         socket_path=socket_path, session_id=session_id)

    def _cookie(self) -> str:
        try:
            return self._inproc._cookie()
        except Exception as exc:  # noqa: BLE001 — MountAuthError 原样转 MountError 文案
            raise MountError(str(exc)) from exc

    def _session_data(self) -> dict:
        # 会话只交三样：Cookie（sidecar 进程内存态）、UA、根 cid；不交库信息。
        return {
            "cookie": self._cookie(),
            "ua": self._inproc._ua(),
            "root_cid": self._inproc.root_cid,
        }


__all__ = [
    "P115SidecarMount",
    "SidecarMountProvider",
    "manifest_to_capabilities",
    "sidecar_enabled",
]
