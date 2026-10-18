"""115 网盘账号与直挂客户端

面板侧只保留「用 115 直挂媒体库」所需的两件事：

- **账号配置档**：可以配置多个命名账号（`pan115_accounts`），其中一个为默认账号；
  媒体库可单独绑定账号（`Library.account_115_id`），未绑定时回退「默认账号 → 服务器级
  `PAN115_COOKIE`」，因此历史部署里只在 .env 放一个 Cookie 的写法继续可用。
- **Cookie 型客户端**：校验 Cookie、列目录、取下载地址（直挂播放由 EA 用它换直链）。

v2.18.0 起不再包含「分享链接转存 / 下载任务」那一套（解析分享、任务状态机、断点续跑、
`PAN115_STATE_DIR` 落盘全部移除）：把分享搬进自己账号这件事在 115 官方 App / 网页里
一次即可完成，面板长期维护一套抓第三方页面的任务引擎不值得。115 直挂本身不走那套代码。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server.env_util import env_float

logger = logging.getLogger(__name__)

# ==================== 配置 ====================

PAN115_COOKIE_ENV = "PAN115_COOKIE"
# 端点可用环境变量覆盖：115 的 Web API 域名偶有调整，也便于测试指向本地假服务
PAN115_WEBAPI_BASE = os.getenv("PAN115_WEBAPI_BASE", "https://webapi.115.com").rstrip("/")
PAN115_TIMEOUT = env_float("PAN115_TIMEOUT", 20.0)
PAN115_UA = os.getenv(
    "PAN115_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
)

# ==================== 异常 ====================


class Pan115Error(RuntimeError):
    """115 调用失败（业务错误）"""


class Pan115AuthError(Pan115Error):
    """115 登录态失效（Cookie 过期 / 被踢）——调用方应提示重新配置 Cookie"""


# ==================== Cookie 解析 ====================


def normalize_cookie(raw: Optional[str]) -> str:
    """规范化 Cookie：去掉换行与首尾空白，便于整体粘贴"""
    return re.sub(r"\s+", " ", (raw or "").strip())


def resolve_cookie(
    db: Session,
    *,
    library=None,
    account_id: Optional[int] = None,
    explicit_cookie: Optional[str] = None,
) -> tuple[str, str]:
    """解析要使用的 115 Cookie，返回 ``(cookie, 来源说明)``

    优先级：显式账号 > 显式 Cookie > 媒体库绑定账号 > 默认账号 > 服务器级环境变量。
    每次解析前都会刷新一次，因此**配置档更新后无需重启**。

    媒体库绑定账号被删除或停用时，回退到默认账号并给出可读来源，避免调用方直接卡死。
    """
    if account_id is not None:
        account = db.query(em.Pan115Account).filter(em.Pan115Account.id == account_id).first()
        if not account:
            raise Pan115Error(f"115 账号配置档 #{account_id} 不存在")
        return normalize_cookie(account.cookie), f"账号配置档「{account.name}」"

    if explicit_cookie and explicit_cookie.strip():
        return normalize_cookie(explicit_cookie), "表单 Cookie"

    bound_id = getattr(library, "account_115_id", None)
    if bound_id:
        account = db.query(em.Pan115Account).filter(em.Pan115Account.id == bound_id).first()
        if account and account.is_enabled:
            return normalize_cookie(account.cookie), f"媒体库绑定账号「{account.name}」"
        logger.warning(
            "媒体库 #%s 绑定的 115 账号 #%s 不可用，回退到默认账号",
            getattr(library, "id", None), bound_id,
        )

    default = (
        db.query(em.Pan115Account)
        .filter(em.Pan115Account.is_default == True, em.Pan115Account.is_enabled == True)  # noqa: E712
        .first()
    )
    if default:
        return normalize_cookie(default.cookie), f"默认账号「{default.name}」"

    env_cookie = os.getenv(PAN115_COOKIE_ENV, "").strip()
    if env_cookie:
        return normalize_cookie(env_cookie), f"服务器级 {PAN115_COOKIE_ENV}"

    return "", "未配置"


def resolve_ua(
    db: Session,
    *,
    library=None,
    account_id: Optional[int] = None,
    explicit_ua: str = "",
) -> str:
    """解析该用哪个 UA，**优先级与 :func:`resolve_cookie` 完全一致**

    刻意与 Cookie 走同一套优先级而不是各选各的：UA 是「这个账号像哪台设备」的
    一部分，两边选到不同账号（例如 Cookie 用了默认档、UA 用了停用档）就串味了。
    返回空串 = 用服务器级 ``PAN115_UA``。
    """
    if account_id is not None:
        account = db.query(em.Pan115Account).filter(em.Pan115Account.id == account_id).first()
        return (account.ua or "") if account else ""

    if (explicit_ua or "").strip():
        return explicit_ua.strip()

    bound_id = getattr(library, "account_115_id", None)
    if bound_id:
        account = db.query(em.Pan115Account).filter(em.Pan115Account.id == bound_id).first()
        if account and account.is_enabled:
            return account.ua or ""

    default = (
        db.query(em.Pan115Account)
        .filter(em.Pan115Account.is_default == True, em.Pan115Account.is_enabled == True)  # noqa: E712
        .first()
    )
    return (default.ua or "") if default else ""


def ensure_single_default(db: Session, keep_id: int) -> None:
    """保证同一时间只有一个默认账号（否则配置档更新后行为不确定）"""
    for other in db.query(em.Pan115Account).filter(em.Pan115Account.id != keep_id).all():
        if other.is_default:
            other.is_default = False


# ==================== HTTP 客户端 ====================

# 115 用 state/errno 表达失败；这几个 errno 说明是登录态问题（而不是业务错误）
_AUTH_ERRNOS = {"401", "403", "40101017", "40101002", "990002", "990009"}


def _http_json(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    cookie: str = "",
    timeout: Optional[float] = None,
    ua: str = "",
) -> dict:
    """薄 HTTP 封装（测试可替换本函数，其余逻辑全部与网络解耦）"""
    import httpx

    headers = {
        "User-Agent": (ua or "").strip() or PAN115_UA,
        "Referer": "https://115.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie
    with httpx.Client(timeout=timeout or PAN115_TIMEOUT, follow_redirects=True) as client:
        resp = client.request(method, url, params=params, data=data, headers=headers)
    if resp.status_code in (401, 403):
        raise Pan115AuthError(f"115 返回 HTTP {resp.status_code}，Cookie 可能已失效")
    if resp.status_code >= 400:
        raise Pan115Error(f"115 返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — 115 偶发返回 HTML 登录页
        raise Pan115AuthError("115 返回了非 JSON 响应，通常是登录态失效")
    if not isinstance(body, dict):
        return {"state": True, "data": body}
    return body


def _raise_for_state(body: dict) -> dict:
    """把 115 的 state/errno 表达统一成异常或成功返回"""
    if body.get("state") in (True, 1, "1"):
        return body
    errno = str(body.get("errno") or body.get("errNo") or "")
    message = str(body.get("error") or body.get("message") or "115 接口返回失败")
    if errno in _AUTH_ERRNOS or "登录" in message or "cookie" in message.lower():
        raise Pan115AuthError(message)
    raise Pan115Error(message)


class Pan115Client:
    """Cookie 型 115 客户端（只覆盖直挂与后台浏览用到的几个端点）"""

    def __init__(self, cookie: str, ua: str = ""):
        self.cookie = normalize_cookie(cookie)
        # 空 = 用服务器级 PAN115_UA；不同配置档可以指向不同设备（115 直链接口挑 UA）
        self.ua = (ua or "").strip() or PAN115_UA
        if not self.cookie:
            raise Pan115AuthError("未配置 115 Cookie")

    # ---- 账号 ----

    def check(self) -> dict:
        """校验 Cookie 是否有效，返回 ``{ok, uid, vip}``"""
        body = self._call(
            "GET", f"{PAN115_WEBAPI_BASE}/files",
            params={"aid": 1, "cid": 0, "limit": 1, "offset": 0, "show_dir": 1},
        )
        _raise_for_state(body)
        info = body.get("data") or {}
        if isinstance(info, list):
            info = {}
        return {"ok": True, "uid": str(info.get("uid") or "") or None,
                "vip": bool(info.get("vip") or info.get("is_vip"))}

    # ---- 目录 ----

    def list_dir(self, cid: str = "0", limit: int = 200) -> list[dict]:
        """列目录（用于目标路径浏览）

        P1 修复（审查）：此前写死 limit=200/offset=0，超 200 个文件的目录
        被静默截断。现自动翻页取全量（单页 limit 可配，上限 1000）。
        """
        limit = max(1, min(int(limit or 200), 1000))
        out: list[dict] = []
        offset = 0
        while True:
            body = self._call(
                "GET", f"{PAN115_WEBAPI_BASE}/files",
                params={
                    "aid": 1, "cid": cid or "0", "o": "user_ptime", "asc": 1,
                    "show_dir": 1, "limit": limit, "offset": offset,
                },
            )
            _raise_for_state(body)
            data = body.get("data") or []
            if not isinstance(data, list):
                break
            out.extend(
                {
                    "fid": str(entry.get("fid") or entry.get("cid") or ""),
                    "cid": str(entry.get("cid") or entry.get("fid") or ""),
                    "name": entry.get("n") or entry.get("name") or "",
                    "is_dir": _is_dir(entry),
                    "size": int(entry.get("s") or 0),
                    "pickcode": entry.get("pc") or "",
                }
                for entry in data
            )
            if len(data) < limit:
                break
            offset += len(data)
            # 安全阀：单目录 10 万条封顶，防异常 API 无限翻页
            if offset >= 100000:
                break
        return out

    # ---- 下载 ----

    def download_url(self, pickcode: str) -> str:
        """取单个文件下载地址（供外部下载器 / 直链使用）"""
        body = self._call(
            "GET", f"{PAN115_WEBAPI_BASE}/files/download", params={"pickcode": pickcode},
        )
        _raise_for_state(body)
        url = body.get("file_url") or body.get("url")
        if not url:
            raise Pan115Error("115 未返回下载地址")
        return str(url)

    # ---- 内部 ----

    def _call(self, method: str, url: str, *, params: Optional[dict] = None,
              data: Optional[dict] = None) -> dict:
        return _http_json(method, url, params=params, data=data, cookie=self.cookie,
                          ua=self.ua)


def _is_dir(entry: dict) -> bool:
    """115 目录项判定：优先显式字段，其次 fc == "0"（115 的目录分类码）"""
    if "is_dir" in entry:
        return bool(entry["is_dir"])
    for key in ("isdir", "is_dir"):
        if key in entry:
            return bool(entry[key])
    fc = entry.get("fc")
    if fc is not None:
        return str(fc) == "0"
    # 目录没有大小、没有 pickcode 是常见特征
    return not entry.get("s") and not entry.get("pc")


def verify_account(cookie: str) -> dict:
    """校验一个 Cookie（供后台「测试账号」按钮）"""
    client = Pan115Client(cookie)
    try:
        return client.check()
    except Pan115AuthError as exc:
        return {"ok": False, "message": str(exc), "auth_error": True}
    except Pan115Error as exc:
        return {"ok": False, "message": str(exc), "auth_error": False}
