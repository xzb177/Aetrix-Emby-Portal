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
# 上传域名（与 Web API 分离，见 115cli 0.3.4 的 Endpoint.UPLB）
PAN115_UPLB_BASE = os.getenv("PAN115_UPLB_BASE", "https://uplb.115.com").rstrip("/")
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
                    # #13 PR-3：fid 必须是文件自身的真实 id，绝不拿父目录 cid
                    # 回落冒充——同目录所有文件会共享一个"身份"，file_id 认亲层
                    # 会大规模错认。API 没给 fid 时 fid 为空串（无身份）。
                    # cid 侧保留回落（目录浏览靠 cid，目录项本无 fid）。
                    "fid": str(entry.get("fid") or ""),
                    "cid": str(entry.get("cid") or entry.get("fid") or ""),
                    "name": entry.get("n") or entry.get("name") or "",
                    "is_dir": _is_dir(entry),
                    "size": int(entry.get("s") or 0),
                    "pickcode": entry.get("pc") or "",
                    # sha1 仅透传（响应里有才取，缺时为空）：本 PR 不建
                    # hash 上传秒传路径——服务端内 copy 不需要 sha1，
                    # 绝不拿 pickcode 或其它字段冒充哈希。
                    "sha1": entry.get("sha1") or "",
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

    # ---- 转存（同 provider 内 copy，不搬字节） ----

    def copy_file(self, source_fid: str, dest_cid: str) -> dict:
        """服务端内 copy（转存到用户盘用）。

        请求形态：POST /files/copy，fid=源 fid、pid=目标目录 cid。
        响应形态待 115 测试账号实测：返回原始 body，调用方用多字段
        容错提新 fid/pickcode，提不到再列目标目录匹配。115 copy 是
        服务端内操作、通常瞬时，但大文件不假设严格瞬时——状态机与
        Drive 线同一套（准备中 → 就绪/失败），不因“秒传”省状态。
        """
        body = self._call(
            "POST", f"{PAN115_WEBAPI_BASE}/files/copy",
            data={"fid": source_fid, "pid": dest_cid},
        )
        return _raise_for_state(body)

    def mkdir(self, parent_cid: str, name: str) -> dict:
        """在指定目录下建子目录（转存目录用）。

        请求形态：POST /files/add，pid=父目录 cid、cname=目录名。
        响应形态待实测：返回原始 body，调用方建完按名重列验 cid，
        不依赖本响应字段。
        """
        body = self._call(
            "POST", f"{PAN115_WEBAPI_BASE}/files/add",
            data={"pid": parent_cid, "cname": name},
        )
        return _raise_for_state(body)

    # ---- 上传（跨 provider 转存 Drive→115 用） ----

    def simple_upload(self, fileobj, filename: str, pid: str) -> dict:
        """简单上传（115cli 0.3.4 的 simple_upload 口径，Cookie 鉴权）。

        两步，不走 initupload 加密协议（见模块外说明）：
        1. POST ``uplb.115.com/3.0/sampleinitupload.php``（filename +
           target=U_1_<pid>）取 OSS 表单参数（host/object/policy/
           accessid/callback/signature）；
        2. 向 host 做 OSS 表单 POST（字段 name/key/policy/
           OSSAccessKeyId/success_action_status=200/callback/signature
           + 文件体），success_action_status=200 时响应体即 115 回调
           JSON（含 file_id/file_name/pick_code/sha1/file_size）。

        上限：115cli 的 MAX_SIMPLE_UPLOAD_SIZE = 5 GiB，超限文件不
        走本方法（调用方先拦，记 unsupported，不静默拆段）。
        本方法入口亦自带同一上限守卫：能用 seek/tell 测出大小时，
        超限直接报 Pan115Error，绝不在初始化 OSS 会话后再失败；
        测不出大小时才放行（调用方已拦，守卫是第二道、不是唯一一道）。
        fileobj 须支持 read/seek/tell（httpx 按 seek/tell 算
        Content-Length；跨 provider 搬字节用分段读取器直供，不落盘）。
        fileobj.read 中抛出的搬运层异常（如源盘凭据失效）原样上抛，
        本方法只把 httpx 层失败转成 Pan115Error/AuthError。
        返回 ``{fid, pickcode, raw}``；回调里提不到 fid/pickcode 时
        对应字段为空串，调用方走列目录匹配回落（不依赖本响应字段）。
        """
        import httpx

        # 入口上限守卫（第二道）：测得出大小且超 5 GiB 即拒，绝不
        # 先初始化 OSS 会话；测不出大小时放行，调用方已先拦过。
        _max = 5 * 1024 * 1024 * 1024  # 115cli MAX_SIMPLE_UPLOAD_SIZE
        try:
            _pos = fileobj.tell()
            _end = fileobj.seek(0, 2)
            fileobj.seek(_pos)
        except Exception:
            try:
                fileobj.seek(0)
            except Exception:
                pass
            _end = None
        if _end is not None and _end > _max:
            raise Pan115Error(
                f"文件 {_end} 字节超过 115 简单上传 5 GiB 上限；"
                "多段上传需 initupload 加密协议（未实测），暂不支持")

        init = self._call(
            "POST", f"{PAN115_UPLB_BASE}/3.0/sampleinitupload.php",
            data={"filename": filename, "target": f"U_1_{pid}"},
        )
        if init.get("state") is False:
            raise Pan115Error(
                f"115 简单上传初始化失败：{init.get('error') or init.get('message') or init}")
        host = init.get("host") or ""
        fields = {
            "name": filename,
            "key": init.get("object") or "",
            "policy": init.get("policy") or "",
            "OSSAccessKeyId": init.get("accessid") or "",
            "success_action_status": "200",
            "callback": init.get("callback") or "",
            "signature": init.get("signature") or "",
        }
        if not host or not all((fields["key"], fields["policy"],
                                fields["OSSAccessKeyId"], fields["callback"],
                                fields["signature"])):
            raise Pan115Error("115 简单上传初始化未返回完整 OSS 参数")
        timeout = httpx.Timeout(connect=30.0, read=300.0, write=300.0,
                                pool=30.0)
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    host, data=fields,
                    files={"file": (filename, fileobj)},
                    headers={"User-Agent": self.ua,
                             "Referer": "https://115.com/"},
                )
        except httpx.HTTPError as exc:
            raise Pan115Error(f"115 简单上传请求失败：{exc}") from exc
        if resp.status_code in (401, 403):
            raise Pan115AuthError(
                f"115 简单上传返回 HTTP {resp.status_code}，Cookie 可能已失效")
        if resp.status_code >= 400:
            raise Pan115Error(f"115 简单上传返回 HTTP {resp.status_code}")
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 — OSS 回调非 JSON 即失败
            raise Pan115Error("115 简单上传回调非 JSON") from exc
        if not isinstance(body, dict):
            raise Pan115Error("115 简单上传回调形态异常")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        fid = str(data.get("file_id") or data.get("fid")
                  or data.get("id") or "")
        pickcode = str(data.get("pick_code") or data.get("pickcode")
                       or data.get("pc") or "")
        return {"fid": fid, "pickcode": pickcode, "raw": body}

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
