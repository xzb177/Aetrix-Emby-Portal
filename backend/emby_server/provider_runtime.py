"""外置 provider 运行时契约（external-provider-runtime-v1）。

这是 115 sidecar 化与 139 接入共用的**唯一**运行时契约（挂载-3 照抄定稿：
UDS + ``external-provider-runtime-v1`` + manifest
``external-provider-runtime-manifest-v1`` + 8 项基础能力 + 各 provider 扩展）。
139 那一路复用本模块，不另起一套：它只需提供自己的 handler 与扩展名表，
走同一个 :class:`ProviderRuntimeServer` / :class:`ProviderRuntimeClient`。

线格式（刻意极简，JSON over UDS，一连接一请求）：

- 请求：``{"jsonrpc": "2.0", "id": <int|str>, "method": <str>, "params": {...}}``
- 成功：``{"jsonrpc": "2.0", "id": ..., "result": ...}``
- 失败：``{"jsonrpc": "2.0", "id": ...,
  "error": {"code": <int>, "kind": <str>, "message": <str>}}``

二进制载荷（open_range / resolve_display_asset 的文件字节）在 JSON 里走
base64（``data_b64``），调用方按需分段取，不许一次拉整个大文件当默认行为。

方法名与 FMBY 实测 manifest 逐项对应；``manifest`` 是传输内务方法，
不计入 8 项能力（握手专用，任何 sidecar 必须实现）：

- 8 项基础能力：``session_upsert, session_remove, list_page, stat,
  open_range, resolve_play_target, resolve_display_asset, health``
- 115 扩展 4 项：``p115.full_sync, p115.life_poll, p115.diff_refresh,
  p115.share_full_sync``（扩展名必须以 ``<provider>.`` 开头，否则握手拒绝）

换链与搬字节在协议层分离（照抄 FMBY 最干净的一刀）：

- ``resolve_play_target`` 只回直链与非秘密头，**不搬字节、不回 Cookie**；
  凭据由宿主侧自己现取现贴（见 mount_sidecar 的重贴口径，与
  Pan115Mount 的 cacheable/reattach 纪律同源）。
- ``open_range`` 按 ``byte_offset / byte_length`` 搬字节；``resolve_display_asset``
  直接复用它（不另起取图路径）。

错误语义（照抄 115 侧车分级，见照抄定稿 BYOS-4）：上游 401/403 定性为
``cookie_expired``，其他上游失败为 ``remote_unavailable``，超时为 ``timeout``；
凭据错与不可用不许混成同一个“失败”。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import socket
import threading
from dataclasses import dataclass, field

# ---- 协议名（照抄 FMBY，不许改写） ----
PROTOCOL = "external-provider-runtime-v1"
MANIFEST_PROTOCOL = "external-provider-runtime-manifest-v1"

# ---- 8 项基础能力（照抄 FMBY manifest，顺序即 manifest 顺序） ----
BASE_CAPABILITIES: tuple[str, ...] = (
    "session_upsert",
    "session_remove",
    "list_page",
    "stat",
    "open_range",
    "resolve_play_target",
    "resolve_display_asset",
    "health",
)

# ---- 115 扩展 4 项（照抄 FMBY manifest） ----
P115_EXTENSIONS: tuple[str, ...] = (
    "p115.full_sync",
    "p115.life_poll",
    "p115.diff_refresh",
    "p115.share_full_sync",
)

# provider -> 扩展名表（139 那一路在本表登记自己的扩展，不另建契约模块）。
PROVIDER_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "p115": P115_EXTENSIONS,
}


def register_provider_extensions(provider: str, extensions: tuple[str, ...]) -> None:
    """登记某 provider 的扩展名表（139 接入时调用一次）。

    扩展名必须全部以 ``<provider>.`` 开头，否则直接拒绝登记（fail-closed，
    不许把别家扩展挂到自己名下）。
    """
    for name in extensions:
        if not name.startswith(provider + "."):
            raise ValueError(
                f"扩展名 {name!r} 不属于 provider {provider!r}（须以 {provider}. 开头）")
    PROVIDER_EXTENSIONS[provider] = tuple(extensions)


def extensions_for(provider: str) -> tuple[str, ...]:
    """某 provider 的扩展名表；未登记的 provider 返回空元组。"""
    return PROVIDER_EXTENSIONS.get(provider, ())


def default_socket_path(provider: str) -> str:
    """某 provider 的默认 UDS 路径。

    目录走 env ``PROVIDER_RUNTIME_SOCK_DIR``（默认 ``/run/aetrix/providers``）。
    注意：不用 FMBY 学习机的 ``/run/fmby/providers``——同一台机器上可能还跑着
    FMBY 学习实例，撞路径会把两个运行时混在一起；路径是部署细节，不属于线格式，
    139 复用时只改 provider 名即可。
    """
    sock_dir = os.getenv("PROVIDER_RUNTIME_SOCK_DIR", "/run/aetrix/providers")
    return os.path.join(sock_dir, provider, "runtime.sock")


def default_timeout_sec() -> float:
    """默认单次调用超时（秒），env ``PROVIDER_RUNTIME_TIMEOUT_SEC`` 可改。

    默认 30 与 FMBY 115 侧车超时口径同值（照抄定稿 BYOS-4 引用的侧车层超时），
    但这是我方起步默认值、非 FMBY 常量实测，部署者可按环境调。
    """
    try:
        return float(os.getenv("PROVIDER_RUNTIME_TIMEOUT_SEC", "30"))
    except (TypeError, ValueError):
        return 30.0

# ---- 错误模型 ----
ERROR_CODES: dict[str, int] = {
    "invalid_request": -32600,
    "unknown_method": -32601,
    "session_unknown": -32010,
    "not_found": -32011,
    "cookie_expired": -32012,
    "remote_unavailable": -32013,
    "not_implemented": -32014,
    "timeout": -32015,
    "internal": -32603,
}


class ProviderRuntimeError(RuntimeError):
    """运行时契约错误：``kind`` 是分级语义，调用方按 kind 分流，不许只看文本。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.code = ERROR_CODES.get(kind, ERROR_CODES["internal"])

    def as_error_dict(self) -> dict:
        return {"code": self.code, "kind": self.kind, "message": str(self)}


def error_from_dict(data: dict) -> ProviderRuntimeError:
    """把对端 error 对象还原为异常（kind 不可信时按 internal 计）。"""
    kind = str((data or {}).get("kind") or "internal")
    if kind not in ERROR_CODES:
        kind = "internal"
    return ProviderRuntimeError(kind, str((data or {}).get("message") or kind))


def build_manifest(provider: str, socket_path: str | None = None) -> dict:
    """构造某 provider 的 manifest（握手文档，宿主侧用它验 sidecar 身份）。"""
    return {
        "protocol": MANIFEST_PROTOCOL,
        "provider": provider,
        "runtime": PROTOCOL,
        "capabilities": list(BASE_CAPABILITIES),
        "extensions": list(extensions_for(provider)),
        "socket": socket_path or default_socket_path(provider),
        "version": "1",
    }


def validate_manifest(manifest: dict, provider: str,
                      required_extensions: tuple[str, ...] | None = None) -> dict:
    """校验 manifest，任一不符即 raise（fail-closed，不许带病接入）。

    校验项：协议名、provider 名、8 项基础能力齐全、扩展名归属、
    required_extensions（如传）全部在列。返回原 manifest 供调用方取用。
    """
    if not isinstance(manifest, dict):
        raise ProviderRuntimeError("invalid_request", "manifest 不是对象")
    if manifest.get("protocol") != MANIFEST_PROTOCOL:
        raise ProviderRuntimeError(
            "invalid_request",
            f"manifest 协议不符：{manifest.get('protocol')!r}（期望 {MANIFEST_PROTOCOL}）")
    if manifest.get("runtime") != PROTOCOL:
        raise ProviderRuntimeError(
            "invalid_request",
            f"运行时协议不符：{manifest.get('runtime')!r}（期望 {PROTOCOL}）")
    if manifest.get("provider") != provider:
        raise ProviderRuntimeError(
            "invalid_request",
            f"manifest provider 不符：{manifest.get('provider')!r}（期望 {provider!r}）")
    caps = manifest.get("capabilities") or []
    missing = [c for c in BASE_CAPABILITIES if c not in caps]
    if missing:
        raise ProviderRuntimeError(
            "invalid_request", f"manifest 缺基础能力：{missing}")
    for name in manifest.get("extensions") or []:
        if not str(name).startswith(provider + "."):
            raise ProviderRuntimeError(
                "invalid_request", f"manifest 扩展名 {name!r} 不属于 {provider!r}")
    if required_extensions:
        exts = manifest.get("extensions") or []
        missing_ext = [e for e in required_extensions if e not in exts]
        if missing_ext:
            raise ProviderRuntimeError(
                "invalid_request", f"manifest 缺扩展：{missing_ext}")
    return manifest


@dataclass
class RuntimeSession:
    """sidecar 侧会话记录：凭据只活在 sidecar 进程内存里，不落盘、不进日志。"""

    session_id: str
    data: dict = field(default_factory=dict)

class ProviderRuntimeClient:
    """宿主侧 UDS 客户端：一连接一请求，JSON 行收发，超时即 ``timeout``。

    ``socket_path`` 显式传优先；不传时按 ``PROVIDER_RUNTIME_SOCK_DIR`` +
    provider 推导。139 复用：换 provider 名 + 扩展名表即可，传输层不动。
    """

    def __init__(self, provider: str, socket_path: str | None = None,
                 timeout_sec: float | None = None):
        self.provider = provider
        self.socket_path = socket_path or default_socket_path(provider)
        self.timeout_sec = timeout_sec if timeout_sec is not None else default_timeout_sec()
        self._next_id = 0

    # ---- 传输 ----
    def call(self, method: str, params: dict | None = None) -> object:
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": self._next_id, "method": method,
               "params": params or {}}
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout_sec)
            sock.connect(self.socket_path)
            try:
                sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
                sock.shutdown(socket.SHUT_WR)
                chunks: list[bytes] = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            finally:
                sock.close()
        except socket.timeout as exc:
            raise ProviderRuntimeError("timeout", f"sidecar 调用超时：{method}") from exc
        except OSError as exc:
            raise ProviderRuntimeError(
                "remote_unavailable", f"sidecar 不可达（{self.socket_path}）：{exc}") from exc
        try:
            resp = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderRuntimeError("internal", f"sidecar 响应不是合法 JSON：{exc}") from exc
        if not isinstance(resp, dict) or resp.get("id") != req["id"]:
            raise ProviderRuntimeError("internal", "sidecar 响应 id 不匹配")
        if "error" in resp:
            raise error_from_dict(resp["error"])
        return resp.get("result")

    # ---- 握手 ----
    def manifest(self) -> dict:
        """取对端 manifest（内务方法，不计入 8 项能力）。"""
        result = self.call("manifest", {})
        if not isinstance(result, dict):
            raise ProviderRuntimeError("invalid_request", "manifest 响应不是对象")
        return result

    def handshake(self, required_extensions: tuple[str, ...] | None = None) -> dict:
        """握手：取 manifest 并校验，不符即拒绝接入（fail-closed）。"""
        return validate_manifest(self.manifest(), self.provider, required_extensions)

    # ---- 8 项基础能力（签名即契约，139 复用时一字不改） ----
    def session_upsert(self, session_id: str, data: dict) -> dict:
        return self.call("session_upsert", {"session_id": session_id, "data": data})  # type: ignore[return-value]

    def session_remove(self, session_id: str) -> dict:
        return self.call("session_remove", {"session_id": session_id})  # type: ignore[return-value]

    def list_page(self, session_id: str, path: str, page_token: str | None = None,
                  page_size: int | None = None) -> dict:
        params: dict = {"session_id": session_id, "path": path}
        if page_token is not None:
            params["page_token"] = page_token
        if page_size is not None:
            params["page_size"] = page_size
        return self.call("list_page", params)  # type: ignore[return-value]

    def stat(self, session_id: str, path: str) -> dict:
        return self.call("stat", {"session_id": session_id, "path": path})  # type: ignore[return-value]

    def open_range(self, session_id: str, path: str, byte_offset: int,
                   byte_length: int | None = None) -> dict:
        params: dict = {"session_id": session_id, "path": path, "byte_offset": byte_offset}
        if byte_length is not None:
            params["byte_length"] = byte_length
        return self.call("open_range", params)  # type: ignore[return-value]

    def resolve_play_target(self, session_id: str, path: str) -> dict:
        return self.call("resolve_play_target", {"session_id": session_id, "path": path})  # type: ignore[return-value]

    def resolve_display_asset(self, session_id: str, path: str,
                              byte_offset: int = 0,
                              byte_length: int | None = None) -> dict:
        params: dict = {"session_id": session_id, "path": path, "byte_offset": byte_offset}
        if byte_length is not None:
            params["byte_length"] = byte_length
        return self.call("resolve_display_asset", params)  # type: ignore[return-value]

    def health(self, session_id: str | None = None) -> dict:
        params: dict = {}
        if session_id is not None:
            params["session_id"] = session_id
        return self.call("health", params)  # type: ignore[return-value]

    def call_extension(self, name: str, params: dict | None = None) -> object:
        """调扩展方法：名必须属于本 provider（``<provider>.`` 前缀），否则本地拒绝，
        不把越界调用发往 sidecar。"""
        if not name.startswith(self.provider + "."):
            raise ProviderRuntimeError(
                "invalid_request", f"扩展名 {name!r} 不属于 {self.provider!r}")
        return self.call(name, params or {})


def decode_range_result(result: dict) -> bytes:
    """把 open_range / resolve_display_asset 的结果还原为字节（base64 解码）。"""
    raw = (result or {}).get("data_b64") or ""
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ProviderRuntimeError("internal", f"sidecar 字节载荷 base64 非法：{exc}") from exc


def encode_bytes(data: bytes) -> str:
    """字节 → base64（sidecar 侧打包 open_range 结果用）。"""
    return base64.b64encode(data).decode("ascii")

class ProviderRuntimeServer:
    """sidecar 侧 UDS 服务：把 handler 的 8 项能力 + 扩展暴露为线格式。

    handler 约定（duck-type，139 那一路照此实现自己的 handler，不必继承）：

    - ``provider`` 属性（str，如 ``"p115"``）
    - ``session_upsert(session_id, data)`` / ``session_remove(session_id)``
    - ``list_page(session, path, page_token, page_size)``
    - ``stat(session, path)``
    - ``open_range(session, path, byte_offset, byte_length)``
    - ``resolve_play_target(session, path)``
    - ``resolve_display_asset(session, path, byte_offset, byte_length)``
    - ``health(session_or_none)``
    - ``extensions`` 属性（tuple，声明的扩展名）与 ``call_extension(session, name, params)``

    ``session`` 是 :class:`RuntimeSession`（会话数据只活在进程内存）。
    未知方法 / 未知会话 / handler 抛出的 :class:`ProviderRuntimeError` 一律
    转 error 信封，不让连接无响应挂死；handler 抛其他异常按 ``internal`` 计。
    每连接一线程（daemon），避免一个慢调用堵住整条 UDS。
    """

    def __init__(self, handler, socket_path: str):
        self.handler = handler
        self.provider = getattr(handler, "provider", "")
        self.socket_path = socket_path
        self._sessions: dict[str, RuntimeSession] = {}
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    # ---- 会话 ----
    def _require_session(self, params: dict) -> RuntimeSession:
        session_id = params.get("session_id") or ""
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ProviderRuntimeError("session_unknown", f"未知会话：{session_id!r}")
        return session

    def _dispatch(self, method: str, params: dict) -> object:
        if not isinstance(params, dict):
            raise ProviderRuntimeError("invalid_request", "params 不是对象")
        if method == "manifest":
            return build_manifest(self.provider, self.socket_path)
        handler = self.handler
        if method == "session_upsert":
            session_id = str(params.get("session_id") or "")
            if not session_id:
                raise ProviderRuntimeError("invalid_request", "session_id 为空")
            data = params.get("data") or {}
            if not isinstance(data, dict):
                raise ProviderRuntimeError("invalid_request", "session data 不是对象")
            with self._lock:
                self._sessions[session_id] = RuntimeSession(session_id, dict(data))
            try:
                if hasattr(handler, "session_upsert"):
                    handler.session_upsert(session_id, data)
            except Exception:
                # handler 建会话失败时服务端会话一并回滚：不留“服务端有、handler 无”
                # 的半会话，否则后续调用在两处状态不一致里静默失败。
                with self._lock:
                    self._sessions.pop(session_id, None)
                raise
            return {"ok": True, "session_id": session_id}
        if method == "session_remove":
            session_id = str(params.get("session_id") or "")
            with self._lock:
                self._sessions.pop(session_id, None)
            if hasattr(handler, "session_remove"):
                handler.session_remove(session_id)
            return {"ok": True, "session_id": session_id}
        if method in BASE_CAPABILITIES:
            # health 是存活探针：无会话也可调（session_id 有则带上，无则传 None），
            # 其余 7 项必须先有会话（fail-closed，不许无凭据调取数据）。
            if method == "health":
                session = None
                session_id = params.get("session_id") or ""
                if session_id:
                    with self._lock:
                        session = self._sessions.get(session_id)
            else:
                session = self._require_session(params)
            fn = getattr(handler, method, None)
            if not callable(fn):
                raise ProviderRuntimeError("not_implemented", f"handler 未实现 {method}")
            if method == "list_page":
                return fn(session, params.get("path") or "/",
                          params.get("page_token"), params.get("page_size"))
            if method == "stat":
                return fn(session, params.get("path") or "/")
            if method in ("open_range", "resolve_display_asset"):
                return fn(session, params.get("path") or "/",
                          params.get("byte_offset", 0), params.get("byte_length"))
            if method in ("resolve_play_target",):
                return fn(session, params.get("path") or "/")
            if method == "health":
                return fn(session)
            raise ProviderRuntimeError("unknown_method", f"未分发的方法：{method}")
        # 扩展方法：必须在 handler.extensions 声明内，否则本地拒绝（不许调未声明能力）
        declared = tuple(getattr(handler, "extensions", ()) or ())
        if method in declared:
            session = self._require_session(params)
            fn = getattr(handler, "call_extension", None)
            if not callable(fn):
                raise ProviderRuntimeError("not_implemented", f"handler 未实现扩展分发：{method}")
            return fn(session, method, params)
        raise ProviderRuntimeError("unknown_method", f"未知方法：{method}")

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            chunks: list[bytes] = []
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                chunks.append(data)
            try:
                req = json.loads(b"".join(chunks).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                resp = {"jsonrpc": "2.0", "id": None,
                        "error": ProviderRuntimeError("invalid_request", f"请求不是合法 JSON：{exc}").as_error_dict()}
                conn.sendall(json.dumps(resp).encode("utf-8"))
                return
            req_id = req.get("id") if isinstance(req, dict) else None
            try:
                if not isinstance(req, dict):
                    raise ProviderRuntimeError("invalid_request", "请求不是对象")
                result = self._dispatch(str(req.get("method") or ""), req.get("params") or {})
                resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
            except ProviderRuntimeError as exc:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": exc.as_error_dict()}
            except Exception as exc:  # noqa: BLE001 — 兜底：任何异常都回 error，不挂连接
                resp = {"jsonrpc": "2.0", "id": req_id,
                        "error": ProviderRuntimeError("internal", str(exc)).as_error_dict()}
            conn.sendall(json.dumps(resp).encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve_forever(self) -> None:
        """绑 UDS 并接受连接（阻塞；调用方在线程里跑它，停服调 :meth:`stop`）。"""
        import os as _os

        parent = _os.path.dirname(self.socket_path)
        if parent:
            _os.makedirs(parent, exist_ok=True)
        try:
            _os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(64)
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
            thread.start()

    def stop(self) -> None:
        """停服并清理 socket 文件（幂等）。"""
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
