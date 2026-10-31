"""夸克网盘 PC 端 API 客户端（一期：绑定验活最小面）。

只做绑定验活需要的几件事：GET /config 验 Cookie、GET /file/sort
列根目录证明可读、GET /member 判会员三态（照 115 验 VIP 对齐：
免费账号下载限速、容量有限，绑了也用不了）。接口形状以 OpenList
drivers/quark_uc 的公开实现
为准（调研见 ~/workspace/reports/quark-binding-research-20261031.md）：

- 基址 https://drive-pc.quark.cn/1/clouddrive，固定查询参数
  pr=ucpro&fr=pc；
- 请求头必须带 Cookie + Referer(https://pan.quark.cn/) + 浏览器 UA；
- 响应信封 {status, code, message, data}，code != 0 即失败；
- 响应 Set-Cookie 会轮换 __puus（及 __pus）：必须回写，否则 Cookie
  慢性失效——client.cookie 在每次请求后同步为最新值，调用方
  （user_bindings）负责把变化后的值重新密封落库。

凭据纪律：Cookie 只在本模块与 user_bindings 内经手；日志与异常
文案绝不带 Cookie 原文。二期（转存/点播）所需的下载、上传全链
接口调研结论已在调研报告里，本模块一期不实现。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

QUARK_API_BASE = "https://drive-pc.quark.cn/1/clouddrive"
QUARK_REFERER = "https://pan.quark.cn/"
QUARK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_REQUEST_TIMEOUT = 15.0

#: 信封 message 里出现这些词 → 登录态问题（定性 expired，与 115 的
#: 401/403 定性同口径：凭据没死，是登录态过期，重贴 Cookie 即可）。
_AUTH_HINTS = ("登录", "登陆", "授权", "过期", "token", "unauthorized",
               "login", "auth")


class QuarkError(RuntimeError):
    """夸克上游失败（非鉴权类，或未定性）。"""


class QuarkAuthError(QuarkError):
    """夸克登录态失效（HTTP 401/403 或信封鉴权类错误）。"""


def _send(method, url, **kwargs):
    """HTTP 出口（测试打桩点，与 transfer115 的桩法同款）。"""
    import httpx

    return httpx.request(method, url, **kwargs)


def _splice_cookie(cookie: str, name: str, value: str) -> str:
    """把 name=value 拼回 Cookie 串：已有则替换，没有则追加。"""
    parts = []
    replaced = False
    for chunk in (cookie or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        key = chunk.split("=", 1)[0].strip()
        if key == name:
            parts.append(f"{name}={value}")
            replaced = True
        else:
            parts.append(chunk)
    if not replaced:
        parts.append(f"{name}={value}")
    return "; ".join(parts)


#: /member 的 member_status 档位字段名（OpenList MemberResp 结构：
#: 均为字符串，持有该档时非空）与字符串「假值」字面集。
#: 计入会员的正经档只有 SUPER_VIP / VIP / Z_VIP 三档：MINI_VIP 是
#: 电视端小会员、PC 下载权益不完整，用户 2026-10-31 亲定不算——
#: 只有 MINI_VIP、无正经档 → 非会员（MINI_VIP 字段本身仍在响应里，
#: 只是不进判定集）。
_MEMBER_STATUS_KEYS = ("SUPER_VIP", "VIP", "Z_VIP")
_MEMBER_TYPE_VIP_VALUES = ("SUPER_VIP", "VIP", "Z_VIP")
_MEMBER_FALSEY_STRINGS = ("", "0", "false", "no", "off", "none", "null")


def _member_flag_on(value) -> bool:
    """member_status 某档是否持有：布尔/数字按值，字符串看假值字面。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _MEMBER_FALSEY_STRINGS
    return False


def _member_vip_signal(data: dict) -> bool:
    """响应里是否有确凿的正经会员信号：member_status 的
    SUPER_VIP/VIP/Z_VIP 任一档持有，或 member_type 本身就是这
    三档之一。MINI_VIP 单独出现不算：member_type 字面 "MINI_VIP"
    含 "VIP" 子串，故 member_type 只许整值比对、不许子串匹配。"""
    status = data.get("member_status")
    if isinstance(status, dict):
        if any(_member_flag_on(status.get(key))
               for key in _MEMBER_STATUS_KEYS):
            return True
    member_type = data.get("member_type")
    return (isinstance(member_type, str)
            and member_type.strip().upper() in _MEMBER_TYPE_VIP_VALUES)


class QuarkClient:
    """夸克 Cookie 型客户端（一期仅验活用：check + list_root + 会员三态）。"""

    def __init__(self, cookie: str):
        self.cookie = cookie or ""

    def _absorb_rotated_cookies(self, resp) -> None:
        """回写上游轮换的 __puus/__pus（见模块 docstring）。

        响应头读不到（桩/异常响应）时静默跳过：轮换回写是保活
        优化，读不到不该把一次成功的验活变成失败。
        """
        try:
            raw = resp.headers.get_list("set-cookie")
        except Exception:  # noqa: BLE001 — 读不到轮换值不拦验活
            raw = []
        for header in raw or []:
            first = (header or "").split(";", 1)[0].strip()
            if "=" not in first:
                continue
            name, value = first.split("=", 1)
            name = name.strip()
            if name in ("__puus", "__pus") and value:
                self.cookie = _splice_cookie(self.cookie, name, value)

    def _request(self, method, path, *, params=None, json_body=None):
        query = {"pr": "ucpro", "fr": "pc"}
        if params:
            query.update(params)
        headers = {
            "Cookie": self.cookie,
            "Referer": QUARK_REFERER,
            "User-Agent": QUARK_UA,
            "Accept": "application/json, text/plain, */*",
        }
        try:
            resp = _send(method, f"{QUARK_API_BASE}{path}", params=query,
                         headers=headers, json=json_body,
                         timeout=_REQUEST_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — 网络类收敛成一过性
            raise QuarkError(f"夸克请求失败：{exc}") from exc
        self._absorb_rotated_cookies(resp)
        if resp.status_code in (401, 403):
            raise QuarkAuthError(
                f"夸克返回 HTTP {resp.status_code}（登录态失效）")
        if resp.status_code == 429:
            raise QuarkError("夸克返回 HTTP 429（请求频繁）")
        if resp.status_code >= 500:
            raise QuarkError(f"夸克返回 HTTP {resp.status_code}")
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise QuarkError("夸克响应不是 JSON") from exc
        if not isinstance(body, dict):
            raise QuarkError("夸克响应形状异常")
        code = body.get("code")
        if code not in (0, None):
            message = str(body.get("message") or "")
            if any(hint in message.lower() for hint in _AUTH_HINTS):
                raise QuarkAuthError(f"夸克登录态失效：{message}")
            raise QuarkError(f"夸克返回 code={code}：{message}")
        return body.get("data")

    def check(self) -> dict:
        """验活第一步：GET /config，code==0 即 Cookie 有效。"""
        self._request("GET", "/config")
        return {"ok": True}

    def list_root(self) -> list:
        """验活第二步：列根目录一页，证明该会话真能读目录。"""
        data = self._request("GET", "/file/sort", params={
            "pdir_fid": "0", "_page": "1", "_size": "1",
            "_fetch_total": "1"})
        if isinstance(data, dict):
            return data.get("list") or []
        return []

    def member(self):
        """GET /member：账号会员与容量信息（参数与 OpenList 一致）。

        只返 data 原样，形状判定归 vip_status；请求失败照常抛
        QuarkError（鉴权类抛 QuarkAuthError），由调用方定性。
        """
        return self._request("GET", "/member", params={
            "fetch_subscribe": "false", "_ch": "home",
            "fetch_identity": "false"})

    def vip_status(self) -> str:
        """账号会员三态："vip" / "not_vip" / "unknown"。

        判定字段以 OpenList drivers/quark_uc 的 MemberResp 为准：
        data.member_type（字符串）与 data.member_status{VIP, Z_VIP,
        MINI_VIP, SUPER_VIP}（字符串，持有该档时非空）。其中
        MINI_VIP 不计入会员（电视端小会员，用户 2026-10-31 亲定）：
        只有 MINI_VIP 信号、无正经档 → 按非会员处理。

        - 正经会员信号在场 → vip（SUPER_VIP/VIP/Z_VIP 任一档持有，
          或 member_type 为这三档之一）；
        - 响应结构完整（member_type 与 member_status 两键都在场）
          且无任何会员信号 → not_vip（确凿免费账号）；
        - data 不是字典、或两键缺失 → unknown（响应形状对不上，
          不下结论）。网络/上游异常照常抛，由调用方按「查不清」
          处理——本方法只解析、不吞异常（与 transfer115 的
          vip_status 同款分工）。
        """
        data = self.member()
        if not isinstance(data, dict):
            return "unknown"
        if _member_vip_signal(data):
            return "vip"
        if "member_type" in data and "member_status" in data:
            return "not_vip"
        return "unknown"
