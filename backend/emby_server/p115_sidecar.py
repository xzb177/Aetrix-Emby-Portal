"""115 sidecar：独立进程形态的 115 provider（挂载-3 照抄定稿）。

部署形态（照抄 FMBY）：独立 Python 进程，UDS 通信，协议
``external-provider-runtime-v1``，manifest
``external-provider-runtime-manifest-v1``，8 项基础能力 + 115 扩展 4 项。
启动方式（生产接线）：服务启动时由 ``p115_sidecar_supervisor`` 显式拉起
本进程（开闸时；关闸 ``PAN115_SIDECAR_ENABLED=0`` 时不拉起，分发回
进程内 ``Pan115Mount``）。手动起进程仍可用（部署者按需、或单测）：

    python -m backend.emby_server.p115_sidecar --sock /run/aetrix/providers/p115/runtime.sock

收益与代价沿用挂载稿 §3.6：115 逻辑崩了不拖垮主进程、115 可独立部署升级；
代价是进程管理、UDS、排障复杂度——本 PR 只给进程与契约；分发切换在
``mounts.build_provider``（115 类型开闸走 sidecar），管理后台 UI 由管理后台
专线另做，本 PR 不含管理页。

实现复用原则：115 上游协议只实现一份——本 handler 复用
``transfer115.Pan115Client``（list_dir / download_url），不重写 115 API；
凭据（Cookie）只由宿主经 ``session_upsert`` 交到本进程内存，sidecar 不读库、
不读盘上凭据、不记日志；``resolve_play_target`` 回包不含 Cookie（宿主重贴，
见 provider_runtime 的换链/搬字节分离口径）。

扩展 4 项的诚实分期（不编能力）：

- ``p115.full_sync``：真实全量 walk（list_dir 递归 + 目录环 guard + 10 万条封顶，
  与 transfer115 单目录封顶同口径）；``generation`` 是本进程单调计数，
  不是 FMBY 的代际 SQLite——持久代际库归扫描线另议，本 PR 不伪造。
- ``p115.diff_refresh``：真实重列指定目录（stateless，无脏目录库）。
- ``p115.life_poll``：轮询差分（不是 115 原生推送）——对监视目录做
  ``diff_refresh``，与本 sidecar 内保存的上一次快照比对，输出新增/
  删除/变更事件（带 ``generation``）。事件字段只含路径、条目与指纹，
  不含 115 未提供的推送字段；首次轮询只建基线、不报事件。
- ``p115.share_full_sync``：分享根全量同步——transfer115 没有分享 API，
  本扩展不编造分享接口；对给定分享根（``root_cid`` 或分享目录 ``path``）
  做与 ``full_sync`` 同款全量 walk，返回 ``generation``/``entries``/
  ``truncated``，分享链接与权限语义不在此口径内，待 115 真实账号实测。
- 两个扩展默认不自动跑，须显式 ``call_extension`` 才工作；不改
  list/stat/play 等已有行为。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from dataclasses import dataclass, field

from backend.emby_server import transfer115
from backend.emby_server.provider_runtime import (
    BASE_CAPABILITIES,
    P115_EXTENSIONS,
    PROTOCOL,
    ProviderRuntimeError,
    ProviderRuntimeServer,
    default_socket_path,
    default_timeout_sec,
    encode_bytes,
)

PROVIDER = "p115"
DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 1000
MAX_SYNC_ENTRIES = 100000


def _map_115_error(exc: Exception) -> ProviderRuntimeError:
    """115 客户端异常 → 契约错误分级（401/403 定性 cookie_expired，见 BYOS-4）。"""
    if isinstance(exc, transfer115.Pan115AuthError):
        return ProviderRuntimeError("cookie_expired", str(exc))
    if isinstance(exc, transfer115.Pan115Error):
        return ProviderRuntimeError("remote_unavailable", str(exc))
    return ProviderRuntimeError("internal", str(exc))


@dataclass
class _SessionState:
    cookie: str
    ua: str
    root_cid: str
    client: object = None
    cid_cache: dict = field(default_factory=dict)
    pick_cache: dict = field(default_factory=dict)
    # life_poll 快照：key = 监视目录元组，value = {path: 指纹}；
    # 只存本 sidecar 进程内存，不落盘、不跨进程（与 full_sync 的 generation 同口径）。
    life_snapshots: dict = field(default_factory=dict)


class P115SidecarHandler:
    """115 sidecar handler：会话内存态 + Pan115Client 直调。"""

    provider = PROVIDER
    extensions = P115_EXTENSIONS

    def __init__(self, client_factory=None, http_client_factory=None):
        # client_factory(cookie, ua) -> 有 list_dir/download_url 的客户端；
        # 默认即 transfer115.Pan115Client。测试注入假客户端用，不改生产路径。
        self._client_factory = client_factory or (
            lambda cookie, ua: transfer115.Pan115Client(cookie, ua=ua))
        self._http_client_factory = http_client_factory or _default_http_client
        self._states: dict[str, _SessionState] = {}
        self._lock = threading.Lock()
        self._generation = 0

    # ---- 会话 ----
    def session_upsert(self, session_id: str, data: dict) -> None:
        cookie = transfer115.normalize_cookie((data or {}).get("cookie") or "")
        if not cookie:
            raise ProviderRuntimeError("invalid_request", "session data 缺 cookie")
        ua = str((data or {}).get("ua") or "")
        root_cid = str((data or {}).get("root_cid") or "0")
        try:
            client = self._client_factory(cookie, ua)
        except transfer115.Pan115AuthError as exc:
            raise ProviderRuntimeError("invalid_request", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — 工厂异常按上游失败计
            raise _map_115_error(exc) from exc
        state = _SessionState(cookie=cookie, ua=ua, root_cid=root_cid, client=client)
        state.cid_cache = {"": root_cid, "/": root_cid}
        with self._lock:
            self._states[session_id] = state

    def session_remove(self, session_id: str) -> None:
        with self._lock:
            self._states.pop(session_id, None)

    def _state(self, session) -> _SessionState:
        state = self._states.get(session.session_id)
        if state is None:
            raise ProviderRuntimeError("session_unknown", f"未知会话：{session.session_id!r}")
        return state

    # ---- 目录解析（与 Pan115Mount._cid_of/_locate 同语义，sidecar 内单份） ----
    def _raw_list(self, state: _SessionState, cid: str) -> list[dict]:
        try:
            return state.client.list_dir(cid or "0")
        except Exception as exc:  # noqa: BLE001 — 115 异常统一分级
            raise _map_115_error(exc) from exc

    def _cid_of(self, state: _SessionState, rel: str) -> str:
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        if rel in state.cid_cache and state.cid_cache[rel]:
            return state.cid_cache[rel]
        cid = state.root_cid
        for part in [p for p in rel.split("/") if p]:
            child = next(
                (e for e in self._raw_list(state, cid)
                 if e.get("name") == part and e.get("is_dir")),
                None,
            )
            if child is None:
                raise ProviderRuntimeError("not_found", f"115 上找不到目录：{rel}")
            cid = str(child.get("cid") or "")
        state.cid_cache[rel] = cid
        return cid

    def _locate(self, state: _SessionState, rel: str) -> dict:
        rel = "/" + (rel or "").lstrip("/")
        if rel in state.pick_cache:
            return {"pickcode": state.pick_cache[rel], "name": os.path.basename(rel)}
        parent, _, name = rel.rpartition("/")
        for item in self._raw_list(state, self._cid_of(state, parent or "/")):
            if item.get("name") == name and not item.get("is_dir"):
                return {"pickcode": str(item.get("pickcode") or ""), "name": name,
                        "size": int(item.get("size") or 0)}
        raise ProviderRuntimeError("not_found", f"115 上找不到文件：{rel}")

    @staticmethod
    def _entry(item: dict, parent_rel: str) -> dict:
        name = item.get("name") or ""
        rel = (parent_rel.rstrip("/") + "/" + name) if parent_rel != "/" else ("/" + name)
        is_dir = bool(item.get("is_dir"))
        return {
            "name": name,
            "path": rel,
            "is_dir": is_dir,
            "size": int(item.get("size") or 0),
            # 文件稳定 id 即 pickcode；目录没有 pickcode，file_id 为空、entry_id 为 cid。
            "file_id": "" if is_dir else str(item.get("pickcode") or ""),
            "entry_id": str(item.get("cid") or ""),
        }

    # ---- 8 项基础能力 ----
    def list_page(self, session, path: str, page_token, page_size) -> dict:
        state = self._state(session)
        rel = "/" + (path or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        cid = self._cid_of(state, rel)
        try:
            offset = int(page_token) if page_token is not None else 0
        except (TypeError, ValueError):
            raise ProviderRuntimeError("invalid_request", f"page_token 非法：{page_token!r}")
        if offset < 0:
            raise ProviderRuntimeError("invalid_request", "page_token 为负")
        size = DEFAULT_PAGE_SIZE if page_size is None else int(page_size)
        size = max(1, min(size, MAX_PAGE_SIZE))
        items = self._raw_list(state, cid)
        page = items[offset:offset + size]
        entries = [self._entry(item, rel) for item in page]
        nxt = offset + size
        return {
            "path": rel,
            "entries": entries,
            "next_page_token": str(nxt) if nxt < len(items) else None,
            "total": len(items),
        }

    def stat(self, session, path: str) -> dict:
        state = self._state(session)
        rel = "/" + (path or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        if rel == "/":
            return {"path": "/", "name": "", "is_dir": True, "size": 0,
                    "file_id": "", "entry_id": state.root_cid}
        parent, _, name = rel.rpartition("/")
        for item in self._raw_list(state, self._cid_of(state, parent or "/")):
            if item.get("name") == name:
                return self._entry(item, parent or "/")
        raise ProviderRuntimeError("not_found", f"115 上找不到：{rel}")

    def _fetch_range(self, state: _SessionState, rel: str,
                     byte_offset: int, byte_length) -> bytes:
        if byte_offset < 0 or (byte_length is not None and byte_length <= 0):
            raise ProviderRuntimeError(
                "invalid_request", f"无效的取段范围：offset={byte_offset} length={byte_length}")
        entry = self._locate(state, rel)
        pickcode = entry.get("pickcode") or ""
        if not pickcode:
            raise ProviderRuntimeError("not_found", f"115 未返回该文件的 pickcode：{rel}")
        try:
            url = state.client.download_url(pickcode)
        except Exception as exc:  # noqa: BLE001 — 115 异常统一分级
            raise _map_115_error(exc) from exc
        end = None if byte_length is None else byte_offset + byte_length - 1
        range_header = f"bytes={byte_offset}-" if end is None else f"bytes={byte_offset}-{end}"
        headers = {
            "User-Agent": state.ua or transfer115.PAN115_UA,
            "Referer": "https://115.com/",
            "Range": range_header,
        }
        client = self._http_client_factory()
        try:
            resp = client.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 — 传输异常按上游不可用计
            raise ProviderRuntimeError("remote_unavailable", f"115 取段传输失败：{exc}") from exc
        status = resp.status_code
        if status in (401, 403):
            raise ProviderRuntimeError("cookie_expired", f"115 取段被拒：HTTP {status}")
        if status >= 400:
            raise ProviderRuntimeError("remote_unavailable", f"115 取段失败：HTTP {status}")
        body = resp.content or b""
        if status == 206:
            return body
        # 上游无视 Range 回 200：本地切片兜底（与 Pan115Mount.open_range 同口径，
        # 正确性有保证，只是多拉了字节）。
        return body[byte_offset:] if byte_length is None else body[byte_offset:byte_offset + byte_length]

    def open_range(self, session, path: str, byte_offset: int, byte_length) -> dict:
        state = self._state(session)
        body = self._fetch_range(state, path, int(byte_offset or 0), byte_length)
        return {"data_b64": encode_bytes(body), "byte_offset": int(byte_offset or 0),
                "byte_length": len(body)}

    def resolve_display_asset(self, session, path: str, byte_offset: int, byte_length) -> dict:
        """展示资产直接复用 open_range（FMBY 口径：display_asset 不另起取图路径）。"""
        return self.open_range(session, path, byte_offset, byte_length)

    def resolve_play_target(self, session, path: str) -> dict:
        """换链：只回直链与非秘密头，不搬字节、不回 Cookie（宿主重贴）。"""
        state = self._state(session)
        entry = self._locate(state, path)
        pickcode = entry.get("pickcode") or ""
        if not pickcode:
            raise ProviderRuntimeError("not_found", f"115 未返回该文件的 pickcode：{path}")
        try:
            url = state.client.download_url(pickcode)
        except Exception as exc:  # noqa: BLE001 — 115 异常统一分级
            raise _map_115_error(exc) from exc
        return {
            "url": url,
            "headers": {
                "User-Agent": state.ua or transfer115.PAN115_UA,
                "Referer": "https://115.com/",
                "X-Pickcode": pickcode,
            },
        }

    def health(self, session) -> dict:
        return {"ok": True, "provider": PROVIDER, "protocol": PROTOCOL,
                "capabilities": list(BASE_CAPABILITIES), "extensions": list(P115_EXTENSIONS)}

    # ---- 115 扩展 4 项 ----
    def call_extension(self, session, name: str, params: dict) -> object:
        state = self._state(session)
        if name == "p115.full_sync":
            return self._full_sync(state, params or {})
        if name == "p115.diff_refresh":
            return self._diff_refresh(state, params or {})
        if name == "p115.life_poll":
            return self._life_poll(state, params or {})
        if name == "p115.share_full_sync":
            return self._share_full_sync(state, params or {})
        raise ProviderRuntimeError("unknown_method", f"未知扩展：{name}")

    def _full_sync(self, state: _SessionState, params: dict) -> dict:
        root_cid = str(params.get("root_cid") or state.root_cid)
        root_rel = "/" + str(params.get("root_rel") or "/").strip("/")
        root_rel = root_rel.rstrip("/") or "/"
        entries: list[dict] = []
        seen: set[str] = set()
        stack: list[tuple[str, str]] = [(root_rel, root_cid)]
        while stack:
            rel, cid = stack.pop()
            if cid in seen and cid != root_cid:
                continue  # 目录环 guard（115 正常不会出现，但同步不能因此死循环）
            seen.add(cid)
            for item in self._raw_list(state, cid):
                entry = self._entry(item, rel)
                entry["parent_id"] = cid
                entries.append(entry)
                if len(entries) >= MAX_SYNC_ENTRIES:
                    break
                if item.get("is_dir"):
                    stack.append((entry["path"], str(item.get("cid") or "")))
            if len(entries) >= MAX_SYNC_ENTRIES:
                break
        with self._lock:
            self._generation += 1
            generation = self._generation
        return {"generation": generation, "entries": entries, "truncated": len(entries) >= MAX_SYNC_ENTRIES}

    def _diff_refresh(self, state: _SessionState, params: dict) -> dict:
        directories = params.get("directories") or []
        entries: list[dict] = []
        missing: list[str] = []
        for rel in directories:
            rel = "/" + str(rel or "").lstrip("/")
            rel = rel.rstrip("/") or "/"
            try:
                cid = self._cid_of(state, rel)
            except ProviderRuntimeError:
                missing.append(rel)
                continue
            for item in self._raw_list(state, cid):
                entry = self._entry(item, rel)
                entry["parent_id"] = cid
                entries.append(entry)
        return {"entries": entries, "missing": missing}

    @staticmethod
    def _snapshot_fingerprint(entry: dict) -> dict:
        """快照指纹：只用条目自身已有的字段，不编造上游字段。"""
        return {
            "entry_id": entry.get("entry_id") or "",
            "file_id": entry.get("file_id") or "",
            "is_dir": bool(entry.get("is_dir")),
            "size": int(entry.get("size") or 0),
        }

    def _life_poll(self, state: _SessionState, params: dict) -> dict:
        """轮询差分 life_poll：115 无原生推送，此处明确不是推送。

        对监视目录做 _diff_refresh，与上一次快照比对：新增 / 删除 /
        变更（指纹变化）。首次轮询只建基线、不报事件，避免把存量
        全量误报成新增。快照只存本进程内存，sidecar 重启后重新建基线。
        """
        directories = params.get("directories") or []
        if not directories and params.get("path"):
            directories = [params.get("path")]
        if not directories:
            directories = ["/"]
        normalized: list[str] = []
        for rel in directories:
            rel = "/" + str(rel or "").lstrip("/")
            rel = rel.rstrip("/") or "/"
            if rel not in normalized:
                normalized.append(rel)
        refreshed = self._diff_refresh(state, {"directories": normalized})
        new_snapshot = {
            entry["path"]: self._snapshot_fingerprint(entry)
            for entry in refreshed["entries"]
        }
        by_path = {entry["path"]: entry for entry in refreshed["entries"]}
        key = tuple(normalized)
        old_snapshot = state.life_snapshots.get(key)
        baseline = old_snapshot is None
        events: list[dict] = []
        if not baseline:
            for path in sorted(set(new_snapshot) | set(old_snapshot)):
                if path not in old_snapshot:
                    events.append({"type": "added", "path": path,
                                   "entry": by_path[path], "previous": None})
                elif path not in new_snapshot:
                    events.append({"type": "deleted", "path": path,
                                   "entry": None, "previous": old_snapshot[path]})
                elif new_snapshot[path] != old_snapshot[path]:
                    events.append({"type": "changed", "path": path,
                                   "entry": by_path[path],
                                   "previous": old_snapshot[path]})
        with self._lock:
            self._generation += 1
            generation = self._generation
            state.life_snapshots[key] = new_snapshot
        return {
            "generation": generation,
            "events": events,
            "directories": normalized,
            "missing": refreshed["missing"],
            "source": "poll_diff",
            "is_poll_diff": True,
            "baseline": baseline,
            "snapshot_size": len(new_snapshot),
            "note": "轮询差分：115 无原生 life 推送，本扩展以重列结果与上一次快照"
                    "比对生成事件；事件字段只含路径、条目与指纹，不含 115 未"
                    "提供的推送字段。",
        }

    def _share_full_sync(self, state: _SessionState, params: dict) -> dict:
        """分享根全量同步：transfer115 无分享 API，不编造分享接口。

        给定分享根（root_cid / share_root_cid / share_cid，或分享目录
        path）做与 full_sync 同款全量 walk。返回 generation / entries /
        truncated，与 full_sync 同款语义；分享链接、权限、过期等分享
        语义不在此口径内，待 115 真实账号实测。
        """
        root_cid = (
            params.get("root_cid") or params.get("share_root_cid")
            or params.get("share_cid") or ""
        )
        path = params.get("path") or params.get("share_path") or params.get("root_path")
        root_rel = "/"
        if path:
            root_rel = "/" + str(path).lstrip("/")
            root_rel = root_rel.rstrip("/") or "/"
            if not root_cid:
                root_cid = self._cid_of(state, root_rel)
        if not root_cid:
            root_cid = state.root_cid
        result = self._full_sync(
            state, {"root_cid": str(root_cid), "root_rel": root_rel})
        result.update({
            "share_root_cid": str(root_cid),
            "share_mode": "full_sync_via_root",
            "note": "分享口待 115 真实账号实测：transfer115 无分享 API，本扩展"
                    "未调用任何 115 分享接口，仅对给定分享根做全量同步；"
                    "分享链接、权限、过期等分享语义不在此口径内。",
        })
        return result


def _default_http_client():
    import httpx

    return httpx.Client(timeout=default_timeout_sec())


def main(argv=None) -> int:
    """sidecar 进程入口：绑 UDS，阻塞服务；SIGINT/SIGTERM 优雅停服清 socket。"""
    parser = argparse.ArgumentParser(description="115 provider sidecar (external-provider-runtime-v1)")
    parser.add_argument("--sock", default=None,
                        help="UDS 路径（默认 PROVIDER_RUNTIME_SOCK_DIR/p115/runtime.sock）")
    args = parser.parse_args(argv)
    sock_path = args.sock or default_socket_path(PROVIDER)
    handler = P115SidecarHandler()
    server = ProviderRuntimeServer(handler, sock_path)
    stop = threading.Event()

    def _on_signal(signum, _frame):
        stop.set()
        server.stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    print(f"p115 sidecar listening on {sock_path} protocol={PROTOCOL}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
