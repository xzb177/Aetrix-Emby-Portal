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
文案绝不带 Cookie 原文。

二期（2026-11-01）：在验活面之上补转存/点播所需的全链——
list_dir（分页列目录）、download_info/download_url（取下载直链，
点播靠它＋Cookie/Referer/UA 三件套走服务端中继，绝不裸发 302）、
mkdir、以及上传链 upload_pre → upload_hash（秒传判定）→
逐片 upload_auth + OSS PUT → upload_commit → upload_finish，
由 upload_file 编排。形态来源标注（诚实口径，无真实账号活体
验证）：端点路径、信封与 pre/hash/finish 的请求字段以 OpenList
drivers/quark_uc 公开实现为准（调研报告同上）；upload/auth 的
请求/响应字段名与 auth_meta 构造为按阿里云 OSS V1 签名口径的
推测，代码内逐处标注「未活体验证」，解析一律防御式（多候选
字段名），字段对不上时响亮报错、绝不猜默认值蒙混。
"""
from __future__ import annotations

import logging
import mimetypes
import time
from email.utils import formatdate
from xml.sax.saxutils import escape as _xml_escape

logger = logging.getLogger(__name__)

QUARK_API_BASE = "https://drive-pc.quark.cn/1/clouddrive"
QUARK_REFERER = "https://pan.quark.cn/"
QUARK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_REQUEST_TIMEOUT = 15.0
#: OSS 分片 PUT 的超时：单片可达数十 MB，给足余量（仍有界）。
_UPLOAD_TIMEOUT = 600.0

#: pre 响应缺 part_size 时的缺省分片大小（10 MiB；与 OSS 常规
#: 分片量级一致）。仅是客户端分片粒度，不影响协议正确性。
DEFAULT_PART_SIZE = 10 * 1024 * 1024
#: OSS 分片数上限（协议硬顶）：part_size 过小时按文件大小反推
#: 下限，保证总片数不超此值（防上游回异常小分片把请求数放大）。
_MAX_PARTS = 10000

#: 建目录同名冲突的信封文案标记（调研报告实录：
#: ``file is doloading[同名冲突]``）。命中即「目录已在」，由
#: 调用方按已存在处理（重列目录验 fid），不当失败。
_NAME_CONFLICT_HINTS = ("同名冲突", "doloading")


def is_name_conflict_error(exc) -> bool:
    """异常文案是否为建目录同名冲突（见 _NAME_CONFLICT_HINTS）。"""
    text = str(exc or "")
    return any(hint in text for hint in _NAME_CONFLICT_HINTS)


def _format_type_for(name: str) -> str:
    """按扩展名猜 MIME（upload/pre 的 format_type）；猜不到回通用型。"""
    guessed, _ = mimetypes.guess_type(name or "")
    return guessed or "application/octet-stream"


def _oss_date() -> str:
    """OSS x-oss-date 头值（GMT，RFC 1123）。"""
    return formatdate(usegmt=True)


def _auth_meta(verb: str, bucket: str, obj_key: str, *,
               content_type: str = "", query: str = ""):
    """OSS V1 待签串（auth_meta）构造——**未活体验证（推测）**。

    形状按阿里云 OSS V1 签名口径：VERB\\n（Content-MD5 空）\\n
    Content-Type\\n（Date 空，用 x-oss-date 头替代）\\nx-oss-date
    + x-oss-user-agent 两条规范化头\\n/bucket/obj_key[?query]。
    夸克服务端拿 auth_info + 本串回签 auth_key 的确切字段与串
    格式没有真实账号可验：若活体对不上，失败会响亮落在
    upload_auth/PUT 的错误里，不会静默错传。
    返回 (auth_meta, x_oss_date)：PUT 时须把同一个 date 以
    x-oss-date 头原样带上（签名串里写的是它）。
    """
    date = _oss_date()
    resource = f"/{bucket}/{obj_key}"
    if query:
        resource = f"{resource}?{query}"
    meta = (f"{verb}\n\n{content_type}\n\n"
            f"x-oss-date:{date}\n"
            f"x-oss-user-agent:{QUARK_UA}\n"
            f"{resource}")
    return meta, date


def _finish_fid(data) -> str:
    """upload/finish 响应里提新文件 fid（多候选，取不到回空串）。

    finish 的确切响应形状未经活体验证：fid 可能在 data 顶层
    （fid/file_id/id）或 data.file 里。取不到不算失败——调用方
    以列目录按名核对为最终事实（FMBY discover 口径）。
    """
    if not isinstance(data, dict):
        return ""
    for source in (data, data.get("file")):
        if isinstance(source, dict):
            for key in ("fid", "file_id", "id"):
                value = source.get(key)
                if value:
                    return str(value)
    return ""

#: 信封 message 里出现这些词 → 登录态问题（定性 expired，与 115 的
#: 401/403 定性同口径：凭据没死，是登录态过期，重贴 Cookie 即可）。
_AUTH_HINTS = ("登录", "登陆", "授权", "过期", "token", "unauthorized",
               "login", "auth")

#: 风控/限频信号词：命中即定性 throttled，优先于 _AUTH_HINTS 与
#: HTTP 401/403 的鉴权定性（2026-11-02）。夸克风控文案常与登录
#: 字样混排（如「登录环境存在风险」「操作频繁」配 403），按鉴权
#: 定性会把临时拦截误报成登录过期、让用户白折腾重绑；风控是一
#: 过性的，落 throttled 待用户稍后重验恢复，不落 expired 终态。
_THROTTLE_HINTS = ("频繁", "限流", "风控", "风险", "验证码",
                   "滑块", "安全验证", "too many", "rate limit",
                   "captcha")


def throttle_signal_in(text) -> bool:
    """文本是否带风控/限频信号（客户端定性与绑定层分类共用同一
    口径，避免两处词表漂移）。"""
    lowered = str(text or "").lower()
    return any(hint in lowered for hint in _THROTTLE_HINTS)


class QuarkError(RuntimeError):
    """夸克上游失败（非鉴权类，或未定性）。

    可观测性字段（2026-11-02）：``step`` 为失败步位名（dir /
    pre / hash / part / finish / confirm / download 等，由客户
    端方法在抛错前打上）；``upstream_code`` / ``upstream_message``
    为 401/403 时从响应体读出的上游真实 code/message（已脱敏、
    已截断）——此前 _request 不读响应体，死步不可考，这三项让
    失败落库时能定位死在哪一步、上游到底说了什么。
    """

    def __init__(self, message="", *, step=None, upstream_code=None,
                 upstream_message=None):
        super().__init__(message)
        self.step = step
        self.upstream_code = upstream_code
        self.upstream_message = upstream_message


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


def _sanitize_upstream_text(text, cookie="") -> str:
    """上游错误文案脱敏：截断到 200 字，并抹掉恰好在文案里出现
    的本会话 Cookie 字段值（防御式：上游一般不回显凭据，但错误
    文案要进日志与任务 error_detail，绝不许带凭据原文）。"""
    out = str(text or "")[:200]
    for chunk in (cookie or "").split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        value = chunk.split("=", 1)[1].strip()
        if len(value) >= 6 and value in out:
            out = out.replace(value, "***")
    return out


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
    """夸克 Cookie 型客户端（一期验活面 + 二期目录/下载/上传全链）。"""

    def __init__(self, cookie: str):
        self.cookie = cookie or ""
        #: 当前步位名（各公开方法进入时打上，_request/_oss_send
        #: 抛错时挂到异常 step 字段，供落库定位死步）。
        self._step = None

    def _tag_step(self, exc, step=None):
        """给异常打步位：已有步位（更内层方法打的）优先，不覆盖。"""
        if isinstance(exc, QuarkError) and not exc.step:
            exc.step = step or self._step
        return exc

    def _upstream_error_fields(self, resp):
        """401/403 响应体里的上游 code/message（脱敏后）。

        读不到/形状不对一律回 (None, "")，绝不因读体失败把鉴权
        失败变成别的异常。message 经 _sanitize_upstream_text
        脱敏截断后才许进异常与日志。
        """
        body = None
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — 读体失败不改变定性
            body = None
        if not isinstance(body, dict):
            return None, ""
        code = body.get("code")
        message = _sanitize_upstream_text(body.get("message") or "",
                                          self.cookie)
        return code, message

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
            raise QuarkError(f"夸克请求失败：{exc}",
                             step=self._step) from exc
        self._absorb_rotated_cookies(resp)
        if resp.status_code in (401, 403):
            # 读响应体拿上游真实 code/message（脱敏后进异常与
            # 日志）：此前不读体直接定性，42 条死步不可考。
            up_code, up_message = self._upstream_error_fields(resp)
            if throttle_signal_in(up_message):
                # 风控优先于鉴权定性：403 配「操作频繁/环境风险」
                # 类原话是临时拦截、不是登录过期，抛普通
                # QuarkError 走 throttled 口径（见 _THROTTLE_HINTS）。
                logger.warning(
                    "夸克请求被风控/限频 step=%s http=%s code=%s "
                    "message=%s",
                    self._step, resp.status_code, up_code, up_message)
                raise QuarkError(
                    f"夸克返回 HTTP {resp.status_code}（风控/限频）"
                    f"：{up_message}",
                    step=self._step, upstream_code=up_code,
                    upstream_message=up_message or None)
            text = f"夸克返回 HTTP {resp.status_code}（登录态失效）"
            if up_code is not None:
                text += f"，上游 code={up_code}"
            if up_message:
                text += f"：{up_message}"
            logger.warning(
                "夸克请求鉴权失败 step=%s http=%s code=%s message=%s",
                self._step, resp.status_code, up_code, up_message)
            raise QuarkAuthError(text, step=self._step,
                                 upstream_code=up_code,
                                 upstream_message=up_message or None)
        if resp.status_code == 429:
            raise QuarkError("夸克返回 HTTP 429（请求频繁）",
                             step=self._step)
        if resp.status_code >= 500:
            raise QuarkError(f"夸克返回 HTTP {resp.status_code}",
                             step=self._step)
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise QuarkError("夸克响应不是 JSON",
                             step=self._step) from exc
        if not isinstance(body, dict):
            raise QuarkError("夸克响应形状异常", step=self._step)
        code = body.get("code")
        if code not in (0, None):
            message = str(body.get("message") or "")
            safe_message = _sanitize_upstream_text(message, self.cookie)
            if throttle_signal_in(message):
                # 风控优先于鉴权定性（见 _THROTTLE_HINTS）：文案
                # 混着登录字样也先按临时拦截走 throttled。
                logger.warning(
                    "夸克信封风控/限频 step=%s code=%s message=%s",
                    self._step, code, safe_message)
                raise QuarkError(
                    f"夸克返回 code={code}（风控/限频）：{safe_message}",
                    step=self._step, upstream_code=code,
                    upstream_message=safe_message or None)
            if any(hint in message.lower() for hint in _AUTH_HINTS):
                logger.warning(
                    "夸克信封鉴权失败 step=%s code=%s message=%s",
                    self._step, code, safe_message)
                raise QuarkAuthError(
                    f"夸克登录态失效：{safe_message}", step=self._step,
                    upstream_code=code,
                    upstream_message=safe_message or None)
            raise QuarkError(f"夸克返回 code={code}：{safe_message}",
                             step=self._step, upstream_code=code,
                             upstream_message=safe_message or None)
        return body.get("data")

    def check(self) -> dict:
        """验活第一步：GET /config，code==0 即 Cookie 有效。"""
        self._step = "check"
        self._request("GET", "/config")
        return {"ok": True}

    def list_root(self) -> list:
        """验活第二步：列根目录一页，证明该会话真能读目录。"""
        self._step = "dir"
        data = self._request("GET", "/file/sort", params={
            "pdir_fid": "0", "_page": "1", "_size": "1",
            "_fetch_total": "1"})
        if isinstance(data, dict):
            return data.get("list") or []
        return []

    # ------------------------------------------------------------------
    # 二期：目录 / 下载 / 上传全链（形态来源见模块 docstring；
    # 端点与字段以 OpenList 公开实现为准，auth 段为推测、已标注）
    # ------------------------------------------------------------------

    def list_dir(self, pdir_fid, page_size: int = 100) -> list:
        """列目录全部分页，归一成 [{fid, name, size, is_dir}]。

        条目原始字段（OpenList 口径）：fid / file_name / size /
        file（bool，True=文件、False=目录）。归一时多候选字段
        防御读取（name/dir/is_dir 异名都认），认不出的条目跳过
        不猜。分页直到短页为止，另设 500 页硬顶防上游异常时
        死循环。
        """
        self._step = "dir"
        out = []
        page = 1
        while page <= 500:
            data = self._request("GET", "/file/sort", params={
                "pdir_fid": str(pdir_fid), "_page": str(page),
                "_size": str(page_size), "_fetch_total": "1",
                "fetch_all_file": "1"})
            entries = []
            if isinstance(data, dict):
                entries = data.get("list") or []
            if not isinstance(entries, list):
                break
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                fid = str(entry.get("fid") or "")
                if not fid:
                    continue
                if "file" in entry:
                    is_dir = not bool(entry.get("file"))
                else:
                    is_dir = bool(entry.get("dir")
                                  or entry.get("is_dir"))
                try:
                    size = int(entry.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                out.append({
                    "fid": fid,
                    "name": str(entry.get("file_name")
                                or entry.get("name") or ""),
                    "size": size,
                    "is_dir": is_dir,
                })
            if len(entries) < page_size:
                break
            page += 1
        return out

    def download_info(self, fid) -> dict:
        """文件详情 + 下载信息：POST /file/download {fids:[fid]}。

        返回 data 首条原样 dict（OpenList 口径含 download_url，
        通常也带 file_name/size）。这同时是本客户端的「文件
        详情」口：夸克没有独立的单文件详情端点在本调研范围内
        出现，详情字段以本响应为准（未活体验证）。
        """
        self._step = "download"
        data = self._request("POST", "/file/download",
                             json_body={"fids": [str(fid)]})
        entry = None
        if isinstance(data, list):
            entry = data[0] if data else None
        elif isinstance(data, dict):
            entry = data
        if not isinstance(entry, dict):
            raise QuarkError("夸克下载信息响应形状异常（未活体验证形态）")
        return entry

    def download_url(self, fid) -> str:
        """现取下载直链（点播中继取流用）。

        注意：该 URL 取流时必须带 Cookie + Referer + UA 三件套
        （夸克 CDN 校验），只许服务端中继用，绝不下发客户端、
        绝不 302 裸发。
        """
        entry = self.download_info(fid)
        url = str(entry.get("download_url") or entry.get("url") or "")
        if not url:
            raise QuarkError("夸克下载信息未带 download_url"
                             "（未活体验证形态）")
        return url

    def mkdir(self, pdir_fid, name) -> None:
        """建目录：POST /file。同名冲突抛 QuarkError（文案命中
        is_name_conflict_error），调用方按已存在处理。"""
        self._step = "dir"
        self._request("POST", "/file", json_body={
            "pdir_fid": str(pdir_fid), "file_name": name,
            "dir_path": "", "dir_init_lock": False})

    # ---- 上传链（OSS 分片；auth 段字段为推测，见逐处标注） ----

    def upload_pre(self, name, size, pdir_fid) -> dict:
        """预上传：POST /file/upload/pre，归一出上传会话字段。

        请求字段以 OpenList 公开实现为准（ccp_hash_update /
        file_name / format_type / l_created_at / l_updated_at /
        pdir_fid / size）。响应字段防御解析：task_id / upload_id
        / obj_key / bucket 在 data 顶层取，part_size 顶层或
        data.metadata 里都认；upload_host 在 upload_host / host
        / endpoint / oss_host 里取——全缺失时响亮报错（不许
        猜默认主机把字节打到错地方）。auth_info 原样透传给
        upload_auth。**响应确切嵌套未经活体验证。**
        """
        self._step = "pre"
        now_ms = int(time.time() * 1000)
        data = self._request("POST", "/file/upload/pre", json_body={
            "ccp_hash_update": True,
            "file_name": name,
            "format_type": _format_type_for(name),
            "l_created_at": now_ms,
            "l_updated_at": now_ms,
            "pdir_fid": str(pdir_fid),
            "size": int(size),
        })
        if not isinstance(data, dict):
            raise QuarkError("夸克预上传响应形状异常（未活体验证形态）")
        meta = data.get("metadata")
        if not isinstance(meta, dict):
            meta = {}

        def _pick(*sources_keys):
            for source, key in sources_keys:
                value = source.get(key)
                if value not in (None, ""):
                    return value
            return None

        task_id = _pick((data, "task_id"), (meta, "task_id"))
        obj_key = _pick((data, "obj_key"), (meta, "obj_key"))
        bucket = _pick((data, "bucket"), (meta, "bucket"))
        if not task_id or not obj_key or not bucket:
            raise QuarkError(
                "夸克预上传响应缺 task_id/obj_key/bucket"
                "（未活体验证形态，拒绝继续）")
        part_size_raw = _pick((data, "part_size"), (meta, "part_size"))
        try:
            part_size = int(part_size_raw) if part_size_raw else 0
        except (TypeError, ValueError):
            part_size = 0
        if part_size <= 0:
            part_size = DEFAULT_PART_SIZE
        size_int = int(size)
        if size_int > 0:
            min_part = -(-size_int // _MAX_PARTS)
            part_size = max(part_size, min_part)
        upload_host = _pick((data, "upload_host"), (data, "host"),
                            (data, "endpoint"), (data, "oss_host"),
                            (meta, "upload_host"), (meta, "host"))
        return {
            "task_id": str(task_id),
            "upload_id": str(_pick((data, "upload_id"),
                                   (meta, "upload_id")) or ""),
            "obj_key": str(obj_key),
            "bucket": str(bucket),
            "part_size": part_size,
            "auth_info": _pick((data, "auth_info"),
                               (meta, "auth_info")),
            "upload_host": str(upload_host or ""),
        }

    def upload_hash(self, task_id, md5, sha1) -> bool:
        """秒传判定：POST /file/update/hash。data.finish 为真即
        服务端已有同内容，零字节完成（字段以 OpenList 为准）。"""
        self._step = "hash"
        data = self._request("POST", "/file/update/hash", json_body={
            "task_id": str(task_id), "md5": md5, "sha1": sha1})
        if isinstance(data, dict):
            return bool(data.get("finish"))
        return False

    def upload_auth(self, pre: dict, auth_meta: str) -> str:
        """取一次 OSS 操作的 Authorization：POST /file/upload/auth。

        **未活体验证（推测）**：请求体按 {task_id, auth_info,
        auth_meta} 构造，响应 auth 值在 data 本身（字符串）或
        data.auth_key / authorization / auth 里取，全对不上时
        响亮报错。
        """
        self._step = "part"
        data = self._request("POST", "/file/upload/auth", json_body={
            "task_id": pre["task_id"],
            "auth_info": pre.get("auth_info"),
            "auth_meta": auth_meta,
        })
        if isinstance(data, str) and data:
            return data
        if isinstance(data, dict):
            for key in ("auth_key", "authorization", "auth"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        raise QuarkError("夸克 upload/auth 响应未带可用 auth 值"
                         "（未活体验证形态）")

    def _oss_url(self, pre: dict, query: str = "") -> str:
        host = (pre.get("upload_host") or "").strip()
        if not host:
            raise QuarkError(
                "夸克预上传响应未带上传主机（upload_host/host/"
                "endpoint 均缺）——该形态未经活体验证，拒绝猜测"
                "默认主机")
        host = host.replace("https://", "").replace("http://", "")
        url = f"https://{pre['bucket']}.{host}/{pre['obj_key']}"
        if query:
            url = f"{url}?{query}"
        return url

    def _oss_send(self, method, url, *, auth, date, content=None,
                  content_type=None):
        """OSS 裸请求出口：只带 OSS Authorization（+ 与签名串同
        值的 x-oss-date），绝不带夸克 Cookie（字节面与会话面
        分离）。非 2xx 响亮抛 QuarkError。"""
        headers = {"Authorization": auth, "x-oss-date": date,
                   "x-oss-user-agent": QUARK_UA}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            resp = _send(method, url, headers=headers, content=content,
                         timeout=_UPLOAD_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            raise QuarkError(f"夸克 OSS 请求失败：{exc}",
                             step=self._step) from exc
        self._absorb_rotated_cookies(resp)
        if resp.status_code < 200 or resp.status_code >= 300:
            # OSS 面的 401/403 多半是签名串对不上（auth 段形态
            # 未活体验证），不定性为用户登录失效——绝不许因此
            # 把用户绑定误判 expired。抛普通 QuarkError 走重试。
            raise QuarkError(f"夸克 OSS 返回 HTTP {resp.status_code}",
                             step=self._step)
        return resp

    def upload_part(self, pre: dict, part_number: int, data: bytes,
                    auth: str, date: str, content_type: str = "") -> str:
        """PUT 一片到 OSS，返回 ETag（去引号）。

        content_type 必须与 auth_meta 里签的那个一致（调用方
        upload_file 统一传文件名推得的 format_type）。
        """
        self._step = "part"
        query = (f"partNumber={part_number}"
                 f"&uploadId={pre['upload_id']}")
        resp = self._oss_send(
            "PUT", self._oss_url(pre, query), auth=auth, date=date,
            content=data,
            content_type=content_type or None)
        etag = ""
        try:
            etag = (resp.headers.get("ETag") or "").strip().strip('"')
        except Exception:  # noqa: BLE001 — 无 ETag 由 commit 前兜底
            etag = ""
        if not etag:
            raise QuarkError(
                f"夸克 OSS 分片 {part_number} 响应未带 ETag，"
                "无法组装 CompleteMultipartUpload")
        return etag

    def upload_commit(self, pre: dict, parts: list, auth: str,
                      date: str) -> None:
        """CompleteMultipartUpload：POST OSS（body 为分片清单 XML）。

        parts 为 [(part_number, etag), ...]，按片号排序组装。
        OSS 对本操作可能回 200 + <Error> XML 体，须查体。
        """
        items = "".join(
            "<Part><PartNumber>{}</PartNumber><ETag>\"{}\"</ETag>"
            "</Part>".format(num, _xml_escape(etag))
            for num, etag in sorted(parts))
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                "<CompleteMultipartUpload>" + items
                + "</CompleteMultipartUpload>").encode("utf-8")
        self._step = "finish"
        query = f"uploadId={pre['upload_id']}"
        resp = self._oss_send(
            "POST", self._oss_url(pre, query), auth=auth, date=date,
            content=body, content_type="application/xml")
        text = ""
        try:
            text = resp.text or ""
        except Exception:  # noqa: BLE001
            text = ""
        if "<Error>" in text:
            raise QuarkError("夸克 OSS CompleteMultipartUpload 回 Error"
                             "（详见上游响应，未活体验证形态）")

    def upload_finish(self, task_id, obj_key) -> dict:
        """收尾：POST /file/upload/finish。返回 data 原样（可能
        带新文件 fid，调用方以目录核对为准、不单信回执）。"""
        self._step = "finish"
        data = self._request("POST", "/file/upload/finish", json_body={
            "task_id": str(task_id), "obj_key": str(obj_key)})
        return data if isinstance(data, dict) else {}

    def upload_file(self, reader, name, size, pdir_fid, *,
                    md5: str = "", sha1: str = "") -> dict:
        """上传编排：pre →（有 md5+sha1 时）hash 秒传判定 →
        逐片 auth+PUT → commit → finish。

        reader 须支持 seek/tell/read（transfer 的分段读取器
        即可）。返回 {"outcome": "rapid"|"uploaded", "fid": str}：
        fid 只取自 finish 响应（fid/file_id/id 多候选），取不到
        为空串——调用方必须再列目标目录按名核对（FMBY 口径），
        不许单凭回执登记。秒传命中时不读 reader 一字节。
        """
        size = int(size)
        pre = self.upload_pre(name, size, pdir_fid)
        if md5 and sha1:
            if self.upload_hash(pre["task_id"], md5, sha1):
                data = self.upload_finish(pre["task_id"],
                                          pre["obj_key"])
                return {"outcome": "rapid",
                        "fid": _finish_fid(data)}
        part_size = pre["part_size"]
        parts = []
        reader.seek(0)
        offset = 0
        number = 1
        while offset < size or (size == 0 and number == 1):
            chunk = reader.read(min(part_size, size - offset)
                                if size > 0 else 0)
            if size > 0 and not chunk:
                raise QuarkError(
                    f"夸克上传读取器提前结束：已读 {offset}/{size} 字节")
            meta, date = _auth_meta(
                "PUT", pre["bucket"], pre["obj_key"],
                content_type=_format_type_for(name),
                query=(f"partNumber={number}"
                       f"&uploadId={pre['upload_id']}"))
            auth = self.upload_auth(pre, meta)
            etag = self.upload_part(pre, number, chunk, auth, date,
                                    content_type=_format_type_for(name))
            parts.append((number, etag))
            offset += len(chunk)
            number += 1
            if size == 0:
                break
        meta, date = _auth_meta(
            "POST", pre["bucket"], pre["obj_key"],
            query=f"uploadId={pre['upload_id']}")
        auth = self.upload_auth(pre, meta)
        self.upload_commit(pre, parts, auth, date)
        data = self.upload_finish(pre["task_id"], pre["obj_key"])
        return {"outcome": "uploaded", "fid": _finish_fid(data)}

    def member(self):
        """GET /member：账号会员与容量信息（参数与 OpenList 一致）。

        只返 data 原样，形状判定归 vip_status；请求失败照常抛
        QuarkError（鉴权类抛 QuarkAuthError），由调用方定性。
        """
        self._step = "member"
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
