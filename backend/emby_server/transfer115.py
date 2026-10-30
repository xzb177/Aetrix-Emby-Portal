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

import hashlib
import hmac
import json
import logging
import os
import re
import time
from base64 import b64encode
from email.utils import formatdate
import xml.etree.ElementTree as ET
from binascii import b2a_hex, crc32
from typing import Optional
from urllib.parse import urlencode

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

# ==================== 秒传（initupload）协议 ====================
# 协议对照 p115cipher 0.0.6、p115client 的 upload_init/upload_file_init
# 与 py115 的 UploadInitApi（三方实现公式互洽，2026-10-30 对照确认）：
# - 先 GET proapi /app/uploadinfo 取 user_id + userkey；
# - POST uplb /4.0/initupload.php：query 带 k_ec（ECDH token），body
#   为 AES-128-CBC（固定 ECDH 派生 key/iv，PKCS7 填充）加密的
#   urlencode 表单（空值字段不进表单）；
# - sig = SHA1(userkey + hex(SHA1("{userid}{fileid}{target}0"))
#   + "000000")，大写；token = MD5(MD5_SALT + fileid + filesize
#   + sign_key + sign_val + userid + t + hex(MD5(userid))
#   + appversion)，小写；
# - 响应同为 AES 加密，解密后是分段 lz4 帧（2 字节小端块长 + lz4
#   block）包着的 JSON；status：2 = 秒传命中（reuse，文件已落用户
#   盘）、7 = 需 sign_check 二轮校验（按 sign_check 区间算 sha1
#   回填 sign_val 重发）、1 = 未命中（需走 OSS 上传）。
# 秒传只做到「命中判定」为止；未命中（status=1）的 OSS 落盘由
# 本模块的 multipart_upload 接完（见下「分片上传」节），调用方
# 按大小在 simple_upload（≤5 GiB）与 multipart_upload（>5 GiB）
# 之间路由。**真实端到端未实测**（不许用生产用户 Cookie 做
# 开发测试），协议正确性靠上述开源实现对照 + mock。

PAN115_PROAPI_BASE = os.getenv(
    "PAN115_PROAPI_BASE", "https://proapi.115.com").rstrip("/")
PAN115_APP_VERSION = "36.2.28"  # p115client 当前 appversion

_P115_MD5_SALT = b"Qclm8MGWUv59TnrR0XPg"
_P115_CRC_SALT = b"^j>WD3Kr?J2gLFjD4W2y@"
_P115_AES_PUBKEY = (
    b"\x1d\x03\x0e\x80\xa1x\xdc\xee\xce\xcd\xa3w\xde\x12"
    b"\x8d\x8e\xd9\xdd\xcfU\xaea\xedF\xea\x12\x1a\x1c\xfc\x81")
_P115_AES_KEY = b"\xfb\x1a\x19\xd6R\xf5\xaa\xf7\xbce\x1d\x0fi\xbfB/"
_P115_AES_IV = b"i\xbfB/I\x96\x05P\xa0\xadD\xec4F\xcbL"



# ==================== 分片上传（initupload + 阿里云 OSS 多段） ====================
# 协议对照 p115oss 0.1.2（upload.py 的 upload_file_init /
# upload_file、oss.py 的 oss_sign / oss_multipart_*）与 py115
# 0.1.1（UploadInitApi / UploadTokenApi / Cloud.request_upload_stream），
# 2026-10-30 对照确认，步骤：
# 1. 与秒传同一 initupload 初始化（含 sign_check 二轮）：status=2
#    存量命中；status=1 返回 bucket / object / callback{callback,
#    callback_var} / pickcode（#1526 秒传在此止步，本节接第 2 步）；
# 2. GET uplb /3.0/gettoken.php（Cookie 鉴权）取 OSS STS 临时凭据
#    {AccessKeyId, AccessKeySecret, SecurityToken, Expiration}；
# 3. OSS InitiateMultipartUpload：POST {bucket}.
#    oss-cn-shenzhen.aliyuncs.com/{object}?sequential=1&uploads=1，
#    HMAC-SHA1 头签名（见 _oss_auth_headers），XML 响应取 UploadId；
# 4. 逐片 UploadPart：PUT ?partNumber=N&uploadId=…，串行；每片
#    校验响应 ETag == 本片 MD5（大写 hex），单片有限重试；
# 5. CompleteMultipartUpload：POST ?uploadId=…，body 为 parts 的
#    XML 清单，带 x-oss-callback / x-oss-callback-var（init 回的
#    callback 两段 base64；callbackBody 里的 ${sha1} 占位先按
#    py115 口径替换为整文件 sha1）触发 115 回调落盘，响应体即
#    回调 JSON（与 simple_upload 回调同形态）；
# 6. 任一步失败 best-effort AbortMultipartUpload（DELETE
#    ?uploadId=…）再抛错——未 complete 前不声称成功、不登记。
# **真实端到端未实测**（与秒传同状态：不许用生产用户 Cookie 做
# 开发测试），协议正确性靠上述开源实现对照 + mock，上线后靠
# 真实大文件 job 观察。

#: OSS 端点（p115oss DEFAULT_ENDPOINT）；可用环境变量覆盖（同
#: PAN115_UPLB_BASE 口径，便于测试指向本地假服务）。
PAN115_OSS_ENDPOINT = os.getenv(
    "PAN115_OSS_ENDPOINT", "http://oss-cn-shenzhen.aliyuncs.com").rstrip("/")
#: init 响应缺 bucket 时的缺省桶（p115oss DEFAULT_BUCKET）。
PAN115_OSS_DEFAULT_BUCKET = "fhnfile"
#: 分片大小下限：p115oss determine_partsize 的算法下限是 100 KiB，
#: 但 p115client 明确分片至少 10 MB，且分片过小徒增请求数——
#: 取 10 MiB 为下限，倍增算法与 determine_partsize 同源。
_MULTIPART_MIN_PART_SIZE = 10 * 1024 * 1024
#: 分片数上限（OSS 协议上限，determine_partsize 同值）。
_MULTIPART_MAX_PARTS = 10000
#: 单片 PUT 有限重试次数（与搬运层有界重试同口径；整单最终成败
#: 仍由 job 层有界重试兜底）。
_MULTIPART_PART_ATTEMPTS = 3

#: 进 OSS 签名 CanonicalizedResource 的子资源键（p115oss
#: SUBRESOURCE_KEYS 中与分片上传相关者）。
_OSS_SUBRESOURCE_KEYS = frozenset({
    "uploads", "uploadId", "partNumber", "sequential",
})


def _determine_partsize(length, *, min_partsize=None, max_parts=None):
    """分片大小（p115oss determine_partsize 移植）：自下限倍增至
    分片数不超过上限。length 不大于下限时回下限。"""
    if min_partsize is None:
        min_partsize = _MULTIPART_MIN_PART_SIZE
    if max_parts is None:
        max_parts = _MULTIPART_MAX_PARTS
    if length <= min_partsize:
        return min_partsize
    n = -(-length // max_parts)
    partsize = min_partsize
    while partsize < n:
        partsize <<= 1
    return partsize


def _oss_object_url(bucket, object_key, query=None):
    """虚拟主机式 OSS URL：{scheme}://{bucket}.{host}/{object}。"""
    scheme, _, host = PAN115_OSS_ENDPOINT.partition("://")
    url = f"{scheme}://{bucket}.{host}/{object_key}"
    if query:
        url += "?" + urlencode(query)
    return url


def _oss_auth_headers(token, method, bucket, object_key, query=None,
                      extra_headers=None, content_type=""):
    """阿里云 OSS 头签名（p115oss oss_sign 移植，STS 凭据）。

    待签名串 = METHOD + "\n" + Content-MD5（空）+ "\n" +
    Content-Type + "\n" + Date + "\n" + 排序后的 x-oss-* 头
    （每行 "k:v\n"）+ CanonicalizedResource（/{bucket}/{object}
    + 命中的子资源 query 按键排序 urlencode）。签名 =
    base64(HMAC-SHA1(AccessKeySecret, 待签名串))。
    """
    headers = dict(extra_headers or {})
    headers["date"] = formatdate(usegmt=True)
    if token.get("SecurityToken"):
        headers["x-oss-security-token"] = token["SecurityToken"]
    if content_type:
        headers["content-type"] = content_type
    canon = "".join(
        f"{key}:{headers[key]}\n"
        for key in sorted(headers) if key.startswith("x-oss-"))
    resource = f"/{bucket}/{object_key}"
    if query:
        subs = sorted((k, v) for k, v in query.items()
                      if k in _OSS_SUBRESOURCE_KEYS)
        if subs:
            resource += "?" + urlencode(subs)
    string_to_sign = (
        f"{method}\n\n{content_type}\n{headers['date']}\n"
        f"{canon}{resource}")
    signature = b64encode(hmac.new(
        str(token.get("AccessKeySecret") or "").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1).digest()).decode("ascii")
    headers["Authorization"] = f"OSS {token.get('AccessKeyId')}:{signature}"
    return headers


def _xml_find_text(content, tag):
    """OSS XML 响应里按本地名找第一个元素的文本（命名空间容错）。"""
    try:
        root = ET.fromstring(content)
    except Exception:  # noqa: BLE001 — XML 不可解析即提不到
        return ""
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == tag:
            return (el.text or "").strip()
    return ""


def _http_full(method: str, url: str, *, headers: Optional[dict] = None,
               data=None, client=None):
    """OSS 专用底层请求：返回 (status, 小写键响应头 dict, content)。

    与 _http_bytes 同分工（测试可替换本函数）。非 2xx 统一抛
    Pan115Error 并摘录响应体——OSS 的错误是 XML，且 STS 令牌失效
    与 115 Cookie 失效是两回事，不套 _http_bytes 的 401/403 →
    AuthError 换算。超时按大文件口径放大（单片可达数十 MiB）。
    """
    import httpx

    own = client is None
    c = client or httpx.Client(
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=600.0,
                              pool=30.0),
        follow_redirects=True)
    try:
        resp = c.request(method, url, headers=headers or {}, data=data)
    except httpx.HTTPError as exc:
        raise Pan115Error(f"115 OSS 请求失败：{exc}") from exc
    finally:
        if own:
            c.close()
    if resp.status_code >= 400:
        raise Pan115Error(
            f"115 OSS 返回 HTTP {resp.status_code}："
            f"{resp.content[:200]!r}")
    return (resp.status_code,
            {k.lower(): v for k, v in resp.headers.items()},
            resp.content)


def _read_exact(fileobj, n):
    """从 fileobj 顺序读满 n 字节（读短返回实得字节，调用方判失败）。"""
    buf = bytearray()
    while len(buf) < n:
        piece = fileobj.read(n - len(buf))
        if not piece:
            break
        buf += piece
    return bytes(buf)


def _pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise Pan115Error("115 秒传响应为空")
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
        raise Pan115Error("115 秒传响应填充非法")
    return data[:-pad]


def _ecdh_aes_encrypt(data: bytes) -> bytes:
    """AES-128-CBC 加密（ECDH 派生的固定 key/iv，p115cipher 口径）。"""
    from cryptography.hazmat.primitives.ciphers import (
        Cipher, algorithms, modes)
    enc = Cipher(algorithms.AES(_P115_AES_KEY),
                 modes.CBC(_P115_AES_IV)).encryptor()
    return enc.update(_pkcs7_pad(data)) + enc.finalize()


def _ecdh_aes_decrypt(cipher_data: bytes) -> bytes:
    """AES-128-CBC 解密 + 分段 lz4 帧解压（p115cipher 口径）。"""
    from cryptography.hazmat.primitives.ciphers import (
        Cipher, algorithms, modes)
    dec = Cipher(algorithms.AES(_P115_AES_KEY),
                 modes.CBC(_P115_AES_IV)).decryptor()
    plain = _pkcs7_unpad(dec.update(cipher_data) + dec.finalize())
    return _framed_lz4_decompress(plain)


def _lz4_block_decompress(block: bytes) -> bytes:
    """纯 Python LZ4 block 解压（运行环境无 lz4 库，不新增依赖）。"""
    out = bytearray()
    i = 0
    n = len(block)
    while i < n:
        token = block[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if i >= n:
                    raise Pan115Error("lz4 块截断（字面量长度）")
                b = block[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        out += block[i:i + lit_len]
        i += lit_len
        if i >= n:
            break
        if i + 2 > n:
            raise Pan115Error("lz4 块截断（偏移）")
        offset = block[i] | (block[i + 1] << 8)
        i += 2
        if offset <= 0 or offset > len(out):
            raise Pan115Error("lz4 偏移非法")
        match_len = token & 0x0F
        if match_len == 15:
            while True:
                if i >= n:
                    raise Pan115Error("lz4 块截断（匹配长度）")
                b = block[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        match_len += 4
        start = len(out) - offset
        for j in range(match_len):
            out.append(out[start + j])
    return bytes(out)


def _framed_lz4_decompress(data: bytes) -> bytes:
    """p115cipher 响应帧：重复（2 字节小端块长 + lz4 block）。

    帧解析不成立且明文像 JSON 时原样返回（个别响应只加密不压缩
    的兜底）；两者都不成立才报协议错误（调用方回落全量上传）。
    """
    out = bytearray()
    src = data
    ok = True
    while src and len(src) > 2:
        ln = src[0] | (src[1] << 8)
        if ln == 0 or ln > len(src) - 2:
            ok = False
            break
        try:
            out += _lz4_block_decompress(src[2:2 + ln])
        except Pan115Error:
            ok = False
            break
        src = src[2 + ln:]
    if ok and not src:
        return bytes(out)
    if data.lstrip()[:1] == b"{":
        return data
    raise Pan115Error("115 秒传响应解压失败")


def _ecdh_encode_token(timestamp: int) -> str:
    """k_ec 参数：公钥 + 时间戳 + CRC 的 base64（p115cipher 口径）。"""
    token = bytearray()
    token += _P115_AES_PUBKEY[:15]
    token += b"\x00s\x00\x00\x00"
    token += int(timestamp).to_bytes(4, "little")
    token += _P115_AES_PUBKEY[15:]
    token += b"\x00\x01\x00\x00\x00"
    token += crc32(_P115_CRC_SALT + bytes(token)).to_bytes(4, "little")
    return b64encode(bytes(token)).decode("ascii")


def _make_upload_payload(payload: dict):
    """构造 initupload 请求：返回 (params, AES 加密 body)。

    payload 须含 userid/userkey/fileid/target/filesize/appversion，
    sign_key/sign_val 可空（空值不进加密表单，与开源实现同口径）；
    本函数补 t/sig/token（每次调用都刷新，二轮校验重发同理）。
    """
    payload = dict(payload)
    t = payload["t"] = int(time.time())
    sig = hashlib.sha1(payload["userkey"].encode("ascii"))
    sig.update(b2a_hex(hashlib.sha1(
        "{userid}{fileid}{target}0".format_map(payload)
        .encode("ascii")).digest()))
    sig.update(b"000000")
    payload["sig"] = sig.hexdigest().upper()
    token = hashlib.md5(_P115_MD5_SALT)
    token.update(
        "{fileid}{filesize}{sign_key}{sign_val}{userid}{t}"
        .format_map(payload).encode("ascii"))
    token.update(b2a_hex(hashlib.md5(
        b"%d" % int(payload["userid"])).digest()))
    token.update(payload["appversion"].encode("ascii"))
    payload["token"] = token.hexdigest()
    items = sorted((k, v) for k, v in payload.items() if v)
    body = _ecdh_aes_encrypt(urlencode(items).encode("latin-1"))
    return {"k_ec": _ecdh_encode_token(t)}, body


def _parse_sign_check_range(sign_check):
    """sign_check 是 Range 区间串 "start-end"（闭区间）；失败回 None。"""
    try:
        start_s, end_s = str(sign_check).strip().split("-", 1)
        start, end = int(start_s), int(end_s)
    except (TypeError, ValueError):
        return None
    if start < 0 or end < start:
        return None
    return start, end


def _extract_init_result(resp):
    """秒传命中响应里提 fid + pickcode（多字段容错）。

    提不到返回空串，调用方走列目录匹配回落（不依赖本响应字段）。
    """
    if not isinstance(resp, dict):
        return "", ""
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    fid = str(data.get("file_id") or data.get("fid") or data.get("id")
              or resp.get("file_id") or "")
    pickcode = str(data.get("pickcode") or data.get("pick_code")
                   or data.get("pc") or resp.get("pickcode") or "")
    return fid, pickcode


def _http_bytes(method: str, url: str, *, params: Optional[dict] = None,
                data: Optional[bytes] = None, auth_ck: Optional[str] = None,
                timeout: Optional[float] = None, ua: str = "",
                content_type: str = "") -> bytes:
    """initupload 专用薄封装：响应是加密字节、不是 JSON。

    与 _http_json 同分工（测试可替换本函数，其余逻辑与网络解耦）。
    """
    import httpx

    headers = {
        "User-Agent": (ua or "").strip() or PAN115_UA,
        "Referer": "https://115.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if auth_ck:
        headers["Cookie"] = auth_ck
    if content_type:
        headers["Content-Type"] = content_type
    with httpx.Client(timeout=timeout or PAN115_TIMEOUT,
                      follow_redirects=True) as client:
        resp = client.request(method, url, params=params, data=data,
                              headers=headers)
    if resp.status_code in (401, 403):
        raise Pan115AuthError(
            f"115 返回 HTTP {resp.status_code}，Cookie 可能已失效")
    if resp.status_code >= 400:
        raise Pan115Error(f"115 返回 HTTP {resp.status_code}")
    return resp.content

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

    def vip_status(self) -> str:
        """账号 VIP 三态：``"vip"`` / ``"not_vip"`` / ``"unknown"``。

        与 check() 同一个账号信息口（/files 的 data 同时带 uid 与
        vip/is_vip，check() 已在读这两个字段，本方法只是不把「字段
        不在场」压成 False）：vip/is_vip 在场且为真 → vip；在场且为
        假 → not_vip（确凿非 VIP）；两个字段都不在场、或 data 不是
        字典 → unknown（响应形状对不上，不下结论）。网络/上游异常
        照常抛，由调用方按「查不清」处理——本方法只解析、不吞异常。
        """
        body = self._call(
            "GET", f"{PAN115_WEBAPI_BASE}/files",
            params={"aid": 1, "cid": 0, "limit": 1, "offset": 0, "show_dir": 1},
        )
        _raise_for_state(body)
        info = body.get("data") or {}
        if not isinstance(info, dict):
            return "unknown"
        if info.get("vip") or info.get("is_vip"):
            return "vip"
        if "vip" in info or "is_vip" in info:
            return "not_vip"
        return "unknown"

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

    def rename_file(self, fid: str, new_name: str) -> dict:
        """改名（云下载落盘名是 URL 尾段，认领后改回副本名用）。

        请求形态：POST /files/edit，fid + file_name（p115client
        fs_edit 口径）。返回原始 body 经 state 校验。
        """
        body = self._call(
            "POST", f"{PAN115_WEBAPI_BASE}/files/edit",
            data={"fid": fid, "file_name": new_name},
        )
        return _raise_for_state(body)

    # ---- 云下载（形态 B：让 115 自己拉，2026-10-30 立项） ----
    # 协议形态以 2026-10-30 生产探针实证为准（报告
    # pan115-cloudpull-feasibility-20261030.md 第六节）：web 形态
    # lixian 接口，签名先行；抓取器是单次普通 GET、无 HEAD/Range。
    # 任务记录里存有提交的 URL（我方场景下是带签名的取流地址），
    # 调用方用完必须 cloudpull_del_task 删掉，不许留痕。

    def cloudpull_uid(self) -> str:
        """云下载接口要的 uid：Cookie 里 UID 段下划线前段。"""
        for part in (self.cookie or "").split(";"):
            part = part.strip()
            if part.startswith("UID="):
                return part[4:].split("_", 1)[0].strip()
        return ""

    def cloudpull_sign(self) -> tuple:
        """取云下载签名 (sign, time)：GET 115.com/ ct=clouddownload
        &ac=space。签名与时间戳成对使用，建单时现取现用。"""
        body = self._call(
            "GET", "https://115.com/",
            params={"ct": "clouddownload", "ac": "space"},
        )
        _raise_for_state(body)
        sign = str(body.get("sign") or "")
        ts = str(body.get("time") or "")
        if not sign or not ts:
            raise Pan115Error("115 云下载签名响应缺 sign/time")
        return sign, ts

    def cloudpull_add_task(self, url: str, wp_path_id: str) -> dict:
        """建云下载任务：POST 115.com/web/lixian/ ac=add_task_url。

        url 原样提交（调用方传我方现铸签名取流地址）；wp_path_id
        是落点目录 cid。返回原始 body 经 state 校验。
        """
        uid = self.cloudpull_uid()
        if not uid:
            raise Pan115Error("115 Cookie 里取不到 UID，无法建云下载单")
        sign, ts = self.cloudpull_sign()
        body = self._call(
            "POST", "https://115.com/web/lixian/",
            params={"ct": "lixian", "ac": "add_task_url"},
            data={"url": url, "uid": uid, "sign": sign, "time": ts,
                  "wp_path_id": str(wp_path_id)},
        )
        return _raise_for_state(body)

    def cloudpull_tasks(self) -> list:
        """云下载任务列表（归一化）：info_hash/url/name/status/
        percentDone/file_id/size。status<0 为终态失败，percentDone
        >=100 或 status==2 为完成（实测口径）。"""
        uid = self.cloudpull_uid()
        body = self._call(
            "POST", "https://115.com/web/lixian/",
            params={"ct": "lixian", "ac": "task_lists"},
            data={"uid": uid, "page": 1, "time": int(time.time())},
        )
        _raise_for_state(body)
        raw = body.get("tasks")
        if not isinstance(raw, list):
            raw = []
            data = body.get("data")
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                raw = data["tasks"]
        out = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                status = int(entry.get("status"))
            except (TypeError, ValueError):
                status = 0
            try:
                percent = float(entry.get("percentDone"))
            except (TypeError, ValueError):
                percent = 0.0
            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            out.append({
                "info_hash": str(entry.get("info_hash") or ""),
                "url": str(entry.get("url") or ""),
                "name": str(entry.get("name") or ""),
                "status": status,
                "percentDone": percent,
                "file_id": str(entry.get("file_id") or ""),
                "size": size,
            })
        return out

    def cloudpull_del_task(self, info_hash: str) -> dict:
        """删云下载任务记录：POST ac=task_del，hash[0]=info_hash。

        任务记录里存着提交的 URL（带签名地址），转存收尾必须删。
        """
        body = self._call(
            "POST", "https://115.com/web/lixian/",
            params={"ct": "lixian", "ac": "task_del"},
            data={"hash[0]": info_hash},
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
        走本方法（调用方先拦，超限改走 multipart_upload 分片）。
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
                "超限文件走 multipart_upload（initupload 分片），"
                "本方法不接")

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
        # sha1 一并透出：回调里的 sha1 是 115 对实际落盘字节算的
        # 整文件哈希，调用方据此沉淀条目 content_hash（秒传资格）。
        sha1 = str(data.get("sha1") or "")
        return {"fid": fid, "pickcode": pickcode, "sha1": sha1,
                "raw": body}

    # ---- 秒传（initupload，跨 provider 转存 Drive→115 用） ----

    def upload_info(self) -> dict:
        """取上传身份 ``{user_id, userkey}``（GET proapi /app/uploadinfo）。

        响应形态多容错：平铺或 data 包裹、userkey/user_key 两名。
        缺任一字段报 Pan115Error（协议层失败，调用方回落全量上传）。
        """
        body = self._call("GET", f"{PAN115_PROAPI_BASE}/app/uploadinfo")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        if not isinstance(data, dict):
            raise Pan115Error("115 uploadinfo 响应形态异常")
        user_id = str(data.get("user_id") or data.get("userid")
                      or data.get("uid") or "")
        userkey = str(data.get("userkey") or data.get("user_key") or "")
        if not user_id or not userkey:
            raise Pan115Error("115 uploadinfo 未返回完整 user_id/userkey")
        return {"user_id": user_id, "userkey": userkey}

    def rapid_upload(self, filename: str, filesize: int, file_sha1: str,
                     pid: str, range_reader=None) -> dict:
        """秒传尝试（initupload，可能秒传）：命中直接落用户盘，不搬字节。

        :param range_reader: ``range_reader(start, end) -> bytes``
            （闭区间），供 sign_check 二轮校验取区间字节；返回 str
            时视为已算好的 sha1。文件 ≥ 1MB 时服务端通常要求二轮
            校验，缺 range_reader 且被要求二轮 → Pan115Error。

        返回 ``{"outcome": "hit", "fid", "pickcode", "raw"}`` 或
        ``{"outcome": "miss", "fid": "", "pickcode": "", "raw"}``。
        协议层失败抛 Pan115Error、Cookie 失效抛 Pan115AuthError；
        回落全量上传与绑定落态一律由调用方决定，本方法不吞异常。
        命中回执的 fid/pickcode 只作期望值，调用方仍须列目录核对。
        """
        resp = self._upload_init(filename, filesize, file_sha1, pid,
                                 range_reader=range_reader)
        if resp.get("status") == 2:
            fid, pickcode = _extract_init_result(resp)
            return {"outcome": "hit", "fid": fid, "pickcode": pickcode,
                    "raw": resp}
        return {"outcome": "miss", "fid": "", "pickcode": "",
                "raw": resp}

    def _upload_init(self, filename: str, filesize: int, file_sha1: str,
                     pid: str, range_reader=None) -> dict:
        """initupload 初始化（秒传与分片共用）：收敛到 status 1
        （未命中、需 OSS 上传）或 2（存量命中）后返回最终响应。

        status 7 时按 sign_check 区间算 sign_val 二轮重发（缺
        range_reader 或区间信息不全抛 Pan115Error，与旧口径同）；
        两轮后仍非 1/2 抛 Pan115Error（未知状态）。
        """
        info = self.upload_info()
        payload = {
            "appid": 0,
            "appversion": PAN115_APP_VERSION,
            "userid": info["user_id"],
            "userkey": info["userkey"],
            "target": f"U_1_{pid}",
            "fileid": str(file_sha1).strip().upper(),
            "filename": filename,
            "filesize": int(filesize),
            "sign_key": "",
            "sign_val": "",
            "topupload": "true",
        }
        resp = self._upload_init_once(payload)
        if (resp or {}).get("status") == 7:
            sign_key = str(resp.get("sign_key") or "")
            rng = _parse_sign_check_range(resp.get("sign_check"))
            if not sign_key or rng is None or range_reader is None:
                raise Pan115Error(
                    "115 秒传要求二轮校验但区间信息不全"
                    f"（sign_check={resp.get('sign_check')!r}）")
            content = range_reader(rng[0], rng[1])
            if isinstance(content, str):
                sign_val = content.strip().upper()
            else:
                sign_val = hashlib.sha1(bytes(content)).hexdigest().upper()
            payload["sign_key"] = sign_key
            payload["sign_val"] = sign_val
            resp = self._upload_init_once(payload)
        status = (resp or {}).get("status")
        if status not in (1, 2):
            raise Pan115Error(
                f"115 initupload 返回未知状态 status={status} "
                f"statuscode={(resp or {}).get('statuscode')}")
        return resp

    # ---- 分片上传（initupload + OSS 多段，>5 GiB 全量路） ----

    def upload_token(self) -> dict:
        """gettoken.php → OSS STS 临时凭据（Cookie 鉴权）。

        返回 {AccessKeyId, AccessKeySecret, SecurityToken}；缺任一
        抛 Pan115Error。Expiration 不透出（单次上传内用完即弃）。
        """
        body = self._call("GET", f"{PAN115_UPLB_BASE}/3.0/gettoken.php")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        if not isinstance(data, dict):
            raise Pan115Error("115 gettoken 响应形态异常")
        token = {
            "AccessKeyId": str(data.get("AccessKeyId") or ""),
            "AccessKeySecret": str(data.get("AccessKeySecret") or ""),
            "SecurityToken": str(data.get("SecurityToken") or ""),
        }
        if not all(token.values()):
            raise Pan115Error("115 gettoken 未返回完整 STS 凭据")
        return token

    def multipart_upload(self, fileobj, filename: str, filesize: int,
                         file_sha1: str, pid: str, range_reader=None) -> dict:
        """分片（多段）上传：initupload + OSS multipart（>5 GiB 全量路）。

        协议步骤见模块「分片上传」节。与 rapid_upload 共用
        _upload_init：初始化直接判 status=2 时回
        ``{"outcome": "hit", ...}``（调用方按秒传命中收尾）；
        status=1 才走 OSS 三步（init → 逐片 PUT → complete）。
        fileobj 须支持 read/seek（从头串行读，内存 O(分片大小)，
        不整文件进内存）。返回 ``{"outcome": "uploaded", "fid",
        "pickcode", "sha1", "raw"}``，sha1 为 complete 回调值
        （115 对实际落盘字节算的整文件哈希，可空——调用方与源端
        透传哈希交叉核对）。任一步失败：best-effort abort 后抛
        Pan115Error / Pan115AuthError，绝不半成品冒充完成。
        """
        filesize = int(filesize)
        if filesize <= 0:
            raise Pan115Error("分片上传文件大小非法")
        resp = self._upload_init(filename, filesize, file_sha1, pid,
                                 range_reader=range_reader)
        if resp.get("status") == 2:
            fid, pickcode = _extract_init_result(resp)
            return {"outcome": "hit", "fid": fid, "pickcode": pickcode,
                    "sha1": str(file_sha1).strip().lower(), "raw": resp}
        bucket = str(resp.get("bucket") or PAN115_OSS_DEFAULT_BUCKET)
        object_key = str(resp.get("object") or "")
        callback = resp.get("callback")
        if not object_key:
            raise Pan115Error(
                "115 initupload 未返回 OSS object（分片上传无从落盘）")
        if not isinstance(callback, dict) or not callback.get("callback"):
            raise Pan115Error(
                "115 initupload 未返回 callback"
                "（分片上传 complete 后无法落盘）")
        token = self.upload_token()
        upload_id = self._oss_multipart_init(token, bucket, object_key)
        try:
            parts = self._oss_upload_parts(
                token, bucket, object_key, upload_id, fileobj, filesize)
            body = self._oss_multipart_complete(
                token, bucket, object_key, upload_id, parts, callback,
                str(file_sha1).strip().upper())
        except Exception:
            self._oss_multipart_abort(token, bucket, object_key, upload_id)
            raise
        if not isinstance(body, dict):
            raise Pan115Error("115 分片上传回调形态异常")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        fid = str(data.get("file_id") or data.get("fid")
                  or data.get("id") or "")
        pickcode = str(data.get("pick_code") or data.get("pickcode")
                       or data.get("pc") or "")
        sha1 = str(data.get("sha1") or "")
        return {"outcome": "uploaded", "fid": fid, "pickcode": pickcode,
                "sha1": sha1, "raw": body}

    def _oss_multipart_init(self, token, bucket, object_key):
        query = {"sequential": "1", "uploads": "1"}
        headers = _oss_auth_headers(token, "POST", bucket, object_key,
                                    query)
        _status, _h, content = _http_full(
            "POST", _oss_object_url(bucket, object_key, query),
            headers=headers)
        upload_id = _xml_find_text(content, "UploadId")
        if not upload_id:
            raise Pan115Error("OSS 分片初始化未返回 UploadId")
        return upload_id

    def _oss_upload_parts(self, token, bucket, object_key, upload_id,
                          fileobj, filesize):
        """串行逐片 PUT：自 fileobj 头部顺序读满整文件。

        每片读满 partsize（尾片除外），读短即协议失败；每片校验
        ETag，失败按 _MULTIPART_PART_ATTEMPTS 有限重试（同片字节
        已在内存，重试不重读源）。返回 [{PartNumber, ETag}]。
        """
        partsize = _determine_partsize(filesize)
        try:
            fileobj.seek(0)
        except Exception:  # noqa: BLE001 — 不可 seek 的流按当前位读
            pass
        parts = []
        offset = 0
        number = 1
        while offset < filesize:
            want = min(partsize, filesize - offset)
            chunk = _read_exact(fileobj, want)
            if len(chunk) != want:
                raise Pan115Error(
                    f"分片上传源读取短读：偏移 {offset} 需 {want} "
                    f"实得 {len(chunk)}")
            etag = self._oss_upload_part_retry(
                token, bucket, object_key, upload_id, number, chunk)
            parts.append({"PartNumber": number, "ETag": etag})
            offset += want
            number += 1
        return parts

    def _oss_upload_part_retry(self, token, bucket, object_key,
                               upload_id, number, chunk):
        last_exc = None
        for attempt in range(1, _MULTIPART_PART_ATTEMPTS + 1):
            try:
                return self._oss_upload_part(
                    token, bucket, object_key, upload_id, number, chunk)
            except Pan115Error as exc:
                last_exc = exc
                logger.info(
                    "pan115 multipart part=%s attempt=%s failed: %s",
                    number, attempt, exc)
        raise Pan115Error(
            f"OSS 第 {number} 片重试 {_MULTIPART_PART_ATTEMPTS} "
            f"次仍失败：{last_exc}")

    def _oss_upload_part(self, token, bucket, object_key, upload_id,
                         number, chunk):
        query = {"partNumber": str(number), "uploadId": upload_id}
        headers = _oss_auth_headers(token, "PUT", bucket, object_key,
                                    query)
        _status, resp_headers, _content = _http_full(
            "PUT", _oss_object_url(bucket, object_key, query),
            headers=headers, data=chunk)
        etag = (resp_headers.get("etag") or "").strip()
        if not etag:
            raise Pan115Error(f"OSS 第 {number} 片未返回 ETag")
        expect = hashlib.md5(chunk).hexdigest().upper()
        if etag.strip('"').upper() != expect:
            raise Pan115Error(
                f"OSS 第 {number} 片 ETag 不匹配：响应 {etag} "
                f"本地 MD5 {expect}")
        return etag

    def _oss_multipart_complete(self, token, bucket, object_key,
                                upload_id, parts, callback,
                                file_sha1_upper):
        cb = str(callback.get("callback") or "")
        if "${sha1}" in cb:
            # py115 口径：callbackBody 里的 ${sha1} 占位须先替换为
            # 整文件 sha1 再 base64 进头，否则 115 回调落盘缺哈希。
            cb = cb.replace("${sha1}", file_sha1_upper)
        extra = {}
        if cb:
            extra["x-oss-callback"] = b64encode(
                cb.encode("utf-8")).decode("ascii")
        cbv = str(callback.get("callback_var") or "")
        if cbv:
            extra["x-oss-callback-var"] = b64encode(
                cbv.encode("utf-8")).decode("ascii")
        body = "<CompleteMultipartUpload>" + "".join(
            f"<Part><PartNumber>{p['PartNumber']}</PartNumber>"
            f"<ETag>{p['ETag']}</ETag></Part>" for p in parts
        ) + "</CompleteMultipartUpload>"
        query = {"uploadId": upload_id}
        headers = _oss_auth_headers(
            token, "POST", bucket, object_key, query,
            extra_headers=extra, content_type="text/xml")
        _status, _h, content = _http_full(
            "POST", _oss_object_url(bucket, object_key, query),
            headers=headers, data=body.encode("utf-8"))
        try:
            return json.loads(content.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 — 回调非 JSON 即失败
            raise Pan115Error(f"115 分片上传回调非 JSON：{exc}") from exc

    def _oss_multipart_abort(self, token, bucket, object_key, upload_id):
        """best-effort 取消分片任务（失败不冒泡：整单已判失败）。"""
        try:
            query = {"uploadId": upload_id}
            headers = _oss_auth_headers(token, "DELETE", bucket,
                                        object_key, query)
            _http_full("DELETE",
                       _oss_object_url(bucket, object_key, query),
                       headers=headers)
        except Exception:  # noqa: BLE001
            logger.info("pan115 multipart abort 未成功 upload=%s（忽略）",
                        upload_id)

    def _upload_init_once(self, payload: dict) -> dict:
        """一轮 initupload：加密请求 → 解密响应 JSON。"""
        params, body = _make_upload_payload(payload)
        raw = _http_bytes(
            "POST", f"{PAN115_UPLB_BASE}/4.0/initupload.php",
            params=params, data=body, auth_ck=self.cookie, ua=self.ua,
            content_type="application/x-www-form-urlencoded")
        try:
            resp = json.loads(_ecdh_aes_decrypt(raw))
        except Pan115Error:
            raise
        except Exception as exc:  # noqa: BLE001 — 解密/解析失败即协议失败
            raise Pan115Error(f"115 initupload 响应解析失败：{exc}") from exc
        if not isinstance(resp, dict):
            raise Pan115Error("115 initupload 响应形态异常")
        return resp

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
