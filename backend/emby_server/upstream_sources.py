"""上游源管理（FMBY 学习 #25 25-1：只做源管理）。

学的是 FMBY 第六阶段实测的上游源形态，只学机制、不抄闭源代码：
FMBY 的 ``upstream_sources`` 表实测字段是 source_type、base_url、
auth_method、sealed_secret、extra_headers_json、user_agent、referer；
自连实测建的是 Emby 型源、指向实例自己的 /emby，账号密码密封存储、
响应只给 has_secret，建源后发现通。目录同步与上游播放第六阶段未跑完，
所以本模块到「源管得住」为止：聚合是 25-2、探活是 25-3、播放接入是
25-4，本模块不许越界去拉目录、探活、拼播放地址。

证据与自创的分界（2026-10-24 追加口径：凡第六阶段有实测证据的按
实测来，证据不足的不悄悄按我方旧口径定死）：
- 按实测做的：源字段全集（上列七项）；凭据密封存储、响应只出
  has_secret；类型上 Emby 型实测在先。
- 接近实测做的（证据不足、报告里标待复验/待裁定）：
  ① 凭据形态：实测只见「账号密码密封」，按用户映射在第六阶段无
  任何证据——本模块做源级一把凭据（一个源一份管理员录入的凭据），
  auth_method 取 password（实测）与 api_key（Emby 协议标准形态、
  未在第六阶段上游建源时单独实测）两项，凭据模型最终是源级还是
  按用户映射，留给主代理裁定，代码里不留第二套路径；
  ② jellyfin 类型：第六阶段单独实测的只有 Emby 型，jellyfin 列入是
  用户 2026-10-24 亲定的首批口径（Emby/Jellyfin 同 API 族）；
  ③ enabled / health_status / last_checked_at：FMBY 实测列里没有，
  是我方治理面补的字段（默认关、状态未知占位），不是 FMBY 实测值；
  ④ name：FMBY 实测列里没有，是我方管理端辨识源的必需字段。
- 苹果 CMS 型：用户 2026-10-25 亲定与 Emby/Jellyfin 并列（绝不做兜底
  定位）；本单只做类型与持久化（none 鉴权、分类绑定），同步与播放
  是后续步骤，不在本模块越界；FMBY 的许可证档位 gating（那是它的商业模式，
  用户已定不卖软件，本模块不设任何档位门）。

凭据密封：Fernet，密钥由 SECRET_KEY 经 SHA-256 派生（SECRET_KEY 是
面板与 EA 已同步的同一密钥，见 play_sign）。SECRET_KEY 一旦更换，
旧密封值解不开——此时 unseal_secret 响亮抛 SealedSecretError，
调用方须按「凭据失效、请管理员重录」处理，绝不静默回落明文、
空凭据或旧凭据。SECRET_KEY 未配置时 security 会生成随机值：密封在
本进程内可用，重启后解不开——生产必须配固定 SECRET_KEY（play_sign
本就依赖它同步，属既有要求，不是本模块新增的前提）。

SSRF 守卫：管理员填的上游地址是典型 SSRF 面，照流节点（stream_nodes）
的公网校验口径——只许 http/https、拒绝回环/内网/保留地址与 localhost。
这会挡住局域网上游（不少自建服的上游就在局域网），属已知取舍，已
记进报告的待裁定项：是否给管理员留一个显式放行局域网的开关，本步
不擅自加（加开关就是改安全口径，须主代理点头）。

状态集中维护（魂 6）：源行的全部写操作只经本模块函数；25-3 的探活
工人只许调 set_health_status 写状态，不许自己拼 UPDATE；25-2 的聚合
工人只许读 enabled 的源。默认关：新建源 enabled 恒 False，启用是
单独动作；关着的源对全站行为逐字无影响。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

#: 允许值元组只读 models 的唯一定义点（约束与校验同源）。
SOURCE_TYPES = em.UPSTREAM_SOURCE_TYPES
AUTH_METHODS = em.UPSTREAM_AUTH_METHODS
CREDENTIAL_SCOPES = em.UPSTREAM_CREDENTIAL_SCOPES
HEALTH_STATUSES = em.UPSTREAM_HEALTH_STATUSES

#: 密封值前缀：格式变了就换前缀，旧值解不开时响亮报错不静默兼容。
_SEAL_PREFIX = "fernet-v1:"


class UpstreamSourceError(ValueError):
    """上游源校验/业务错误（管理端映射为 400；编程错误同样响亮）。"""


class DuplicateSourceName(UpstreamSourceError):
    """源名称已存在（管理端映射为 409）。"""


class SealedSecretError(RuntimeError):
    """密封凭据解不开（密钥已换或值损坏）：凭据失效，须管理员重录。"""


# ---------------------------------------------------------------------------
# 地址校验与归一化
# ---------------------------------------------------------------------------

def _is_public_host(host: str) -> bool:
    """主机是否公网可达：拒绝回环/内网/保留地址与 localhost（SSRF 守卫，\
照 stream_nodes._is_public_node_url 的口径）。"""
    host = (host or "").strip().lower()
    if not host or host == "localhost":
        return False
    try:
        addr = ipaddress.ip_address(host)
        return not (addr.is_loopback or addr.is_private or addr.is_reserved
                    or addr.is_link_local or addr.is_multicast
                    or addr.is_unspecified)
    except ValueError:
        # 非 IP 即域名：域名本身无法在不解析的情况下断定内外网——
        # 拒绝 localhost 后缀类本机名，其余放行（解析时 DNS 仍可能指
        # 向内网，25-2/25-3 发请求时须复用本校验的解析版，见报告待办）。
        return not (host.endswith(".localhost") or host.endswith(".local"))


def normalize_base_url(base_url: str) -> str:
    """校验并归一化上游基址：只许 http/https、必须有主机、去尾斜杠。

    非法一律 UpstreamSourceError（编程错误响亮，不静默修地址）。
    """
    raw = (base_url or "").strip()
    if not raw:
        raise UpstreamSourceError("上游地址不能为空")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise UpstreamSourceError(f"上游地址无法解析：{raw!r}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UpstreamSourceError(
            f"上游地址只许 http/https，当前 scheme={scheme!r}")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise UpstreamSourceError(f"上游地址缺少主机名：{raw!r}")
    if parts.username or parts.password:
        raise UpstreamSourceError(
            "上游地址不许带 userinfo（凭据只走密封字段，不许嵌进地址）")
    if not _is_public_host(host):
        raise UpstreamSourceError(
            f"上游地址主机 {host!r} 不是公网地址（回环/内网/保留地址不许作"
            "上游源，局域网上游放行与否属待裁定项，见报告）")
    try:
        port = parts.port
    except ValueError as exc:
        raise UpstreamSourceError(f"上游地址端口非法：{raw!r}") from exc
    # 主机名小写归一（DNS 大小写不敏感）；端口原样保留；不用原 netloc，
    # 否则大小写与 userinfo 会原样进库（归一化必须逐字可验证）。
    normalized = f"{scheme}://{host}"
    if port:
        normalized += f":{port}"
    path = parts.path.rstrip("/")
    if path:
        normalized += path
    return normalized


# ---------------------------------------------------------------------------
# 凭据密封（Fernet，密钥派生自 SECRET_KEY）
# ---------------------------------------------------------------------------

def _sealing_key() -> bytes:
    """由 SECRET_KEY 派生 Fernet 密钥（32 字节摘要 → urlsafe base64）。"""
    from backend.security import SECRET_KEY

    digest = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def seal_secret(plaintext: str) -> str:
    """密封凭据原文，返回带前缀的密封值（存库只存这个，不存原文）。"""
    from cryptography.fernet import Fernet

    token = Fernet(_sealing_key()).encrypt(plaintext.encode("utf-8"))
    return _SEAL_PREFIX + token.decode("ascii")


def unseal_secret(sealed: str) -> str:
    """解开密封值。值损坏/前缀不对/密钥已换 → SealedSecretError（响亮）。"""
    from cryptography.fernet import Fernet, InvalidToken

    if not sealed or not sealed.startswith(_SEAL_PREFIX):
        raise SealedSecretError("密封凭据格式不正确（非本模块密封值）")
    try:
        raw = Fernet(_sealing_key()).decrypt(
            sealed[len(_SEAL_PREFIX):].encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise SealedSecretError(
            "密封凭据解不开：SECRET_KEY 已更换或值已损坏，须管理员重录") from exc
    return raw.decode("utf-8")


def has_secret(source) -> bool:
    """该源是否配了凭据（只看密封值在不在，不碰原文）。"""
    return bool(getattr(source, "sealed_secret", None))


def load_credential(source) -> Optional[dict]:
    """25-2/25-3 向该上游发请求时取凭据的唯一入口。

    返回 ``{"auth_method": ..., "username": ..., "secret": ...}``；
    未配凭据返回 None（匿名访问）；密封解不开抛 SealedSecretError，
    调用方须按凭据失效处理，绝不降级成匿名（匿名会悄悄拿到 401
    后的错误数据或把无权内容当成空目录）。
    """
    if not has_secret(source):
        return None
    return {
        "auth_method": source.auth_method,
        "username": source.username,
        "secret": unseal_secret(source.sealed_secret),
    }


# ---------------------------------------------------------------------------
# 附加请求头校验
# ---------------------------------------------------------------------------

def validate_extra_headers(extra_headers_json: Optional[str]) -> Optional[str]:
    """校验附加请求头 JSON：空 = 无；非空必须是「键值均为字符串」的对象。

    返回归一化后的 JSON 文本（ensure_ascii=False、键排序），调用方
    存这个返回值，不要存用户原串。凭据不许走这里——凭据只走 sealed_secret。
    """
    if extra_headers_json is None:
        return None
    raw = extra_headers_json.strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise UpstreamSourceError("附加请求头必须是 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise UpstreamSourceError("附加请求头必须是 JSON 对象，不是数组/标量")
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise UpstreamSourceError(
                "附加请求头的键与值都必须是字符串")
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


#: 苹果 CMS 总闸 env 键（与 upstream_playback / applecms_sync 同名同义）。
#: 读法与播放分支一致：0/false/no/off/空 = 关，其余 = 开；默认关。
APPLECMS_ENABLED_ENV = "APPLECMS_ENABLED"


def applecms_enabled() -> bool:
    """苹果 CMS 总闸是否开启（创建口的唯一入口；测试 patch 本函数常量无效时用 env）。"""
    return (str(os.getenv(APPLECMS_ENABLED_ENV, "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off", ""})


def _validate_common(name: str, source_type: str, base_url: str,
                     auth_method: str, username: Optional[str]) -> str:
    """新建/编辑共用的校验：名称、类型、认证形态、地址。返回归一化地址。"""
    if not (name or "").strip():
        raise UpstreamSourceError("上游源名称不能为空")
    if len(name.strip()) > 80:
        raise UpstreamSourceError("上游源名称过长（最多 80 字）")
    if source_type not in SOURCE_TYPES:
        raise UpstreamSourceError(
            f"source_type={source_type!r} 不在允许集 {SOURCE_TYPES}")
    if auth_method not in AUTH_METHODS:
        raise UpstreamSourceError(
            f"auth_method={auth_method!r} 不在允许集 {AUTH_METHODS}")
    if auth_method == "password" and not (username or "").strip():
        raise UpstreamSourceError("password 形态必须填用户名")
    if auth_method == "api_key" and (username or "").strip():
        raise UpstreamSourceError("api_key 形态不填用户名（凭据只走密封值）")
    # none 形态：无凭据，username 可空、密封值可空（create_source 里
    # secret 为空即不存密封值）；password / api_key 的规则原样不动。
    return normalize_base_url(base_url)


def _check_name_unique(db, name: str, exclude_id: Optional[int] = None) -> None:
    """名称唯一预检（DB 唯一索引是兜底；预检先给 409，不靠异常猜）。"""
    query = db.query(em.UpstreamSource).filter(
        em.UpstreamSource.name == name.strip())
    if exclude_id is not None:
        query = query.filter(em.UpstreamSource.id != exclude_id)
    if query.first() is not None:
        raise DuplicateSourceName(f"上游源名称已存在：{name.strip()!r}")


# ---------------------------------------------------------------------------
# 写操作（状态集中维护：源行的全部写操作只经这里）
# ---------------------------------------------------------------------------

def create_source(db, *, name: str, source_type: str, base_url: str,
                  auth_method: str = "password",
                  username: Optional[str] = None,
                  secret: Optional[str] = None,
                  extra_headers_json: Optional[str] = None,
                  user_agent: Optional[str] = None,
                  referer: Optional[str] = None,
                  note: Optional[str] = None):
    """新建上游源。新建恒为关闭（enabled=False，不接受 enabled 入参），
    启用是 enable_source 的单独动作。

    苹果 CMS 总闸（APPLECMS_ENABLED，默认关）：关时 applecms 型源
    不可创建（UpstreamSourceError → 管理端 400）；已建源的停用语义
    不由本函数管——播放分支与同步工人各自按总闸视已建源为停用
    （见 upstream_playback._resolve_applecms_stream 与
    applecms_sync.gates_enabled），本函数不因此拦 enable_source。

    secret=None = 不配凭据（匿名访问）；secret="" 视同未配（不存空串）。
    本函数只 flush 不 commit，调用方事务内执行。
    """
    if source_type == "applecms" and not applecms_enabled():
        raise UpstreamSourceError(
            "苹果 CMS 总闸未开启（APPLECMS_ENABLED=0），applecms 型源不可创建")
    normalized = _validate_common(name, source_type, base_url, auth_method,
                                  username)
    headers = validate_extra_headers(extra_headers_json)
    _check_name_unique(db, name)
    sealed = seal_secret(secret) if secret else None
    row = em.UpstreamSource(
        name=name.strip(),
        source_type=source_type,
        base_url=normalized,
        auth_method=auth_method,
        username=(username.strip() if username else None),
        sealed_secret=sealed,
        extra_headers_json=headers,
        user_agent=(user_agent.strip() if user_agent else None),
        referer=(referer.strip() if referer else None),
        enabled=False,
        credential_scope="source",
        health_status="unknown",
        note=(note.strip() if note else None),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise DuplicateSourceName(
            f"上游源名称已存在：{name.strip()!r}") from exc
    logger.info("上游源新建 id=%s name=%r type=%s enabled=False",
                row.id, row.name, row.source_type)
    return row


def update_source(db, source, *, name: Optional[str] = None,
                  source_type: Optional[str] = None,
                  base_url: Optional[str] = None,
                  auth_method: Optional[str] = None,
                  username: Optional[str] = None,
                  secret: Optional[str] = None, secret_set: bool = False,
                  extra_headers_json: Optional[str] = None,
                  extra_headers_set: bool = False,
                  user_agent: Optional[str] = None,
                  user_agent_set: bool = False,
                  referer: Optional[str] = None,
                  referer_set: bool = False,
                  note: Optional[str] = None, note_set: bool = False,
                  import_enabled: Optional[bool] = None):
    """编辑上游源（只改传了的字段；没传的字段逐字不动）。

    凭据三态（secret_set 区分「没传」与「传了」）：
    - secret_set=False：凭据不动；
    - secret_set=True 且 secret 非空：重密封替换；
    - secret_set=True 且 secret 为空/None：清掉凭据（改匿名访问）。
    enabled 不在这里改：启用/停用只走 enable_source / disable_source。
    import_enabled（独有资源进库开关）在这里改：None = 不动；翻转时
    联动该源托管行的可见性（关→隐藏、开→恢复，只改 is_hidden 不删
    行，见 _set_import_visibility），与字段变更同一事务。
    """
    new_name = name if name is not None else source.name
    new_type = source_type if source_type is not None else source.source_type
    new_auth = auth_method if auth_method is not None else source.auth_method
    new_username = username if username is not None else source.username
    if auth_method is not None and new_auth == "api_key" and username is None:
        # 认证形态显式切到 api_key 时，旧用户名随之清掉（api_key 形态
        # 没有用户名字段）；显式传了非空用户名又切 api_key 时，下面的
        # 校验会响亮拒绝——矛盾输入不静默丢。
        new_username = None
    new_url = base_url if base_url is not None else source.base_url
    normalized = _validate_common(new_name, new_type, new_url, new_auth,
                                  new_username)
    if name is not None and name.strip() != source.name:
        _check_name_unique(db, name, exclude_id=source.id)
    source.name = new_name.strip()
    source.source_type = new_type
    source.base_url = normalized
    source.auth_method = new_auth
    source.username = new_username.strip() if new_username else None
    if extra_headers_set:
        source.extra_headers_json = validate_extra_headers(extra_headers_json)
    if user_agent_set:
        source.user_agent = user_agent.strip() if user_agent else None
    if referer_set:
        source.referer = referer.strip() if referer else None
    if note_set:
        source.note = note.strip() if note else None
    if secret_set:
        source.sealed_secret = seal_secret(secret) if secret else None
    if import_enabled is not None:
        new_flag = bool(import_enabled)
        if new_flag != bool(getattr(source, "import_enabled", True)):
            source.import_enabled = new_flag
            _set_import_visibility(db, source.id, new_flag)
    source.updated_at = datetime.now()
    try:
        db.flush()
    except IntegrityError as exc:
        raise DuplicateSourceName(
            f"上游源名称已存在：{new_name.strip()!r}") from exc
    return source


def _set_import_visibility(db, source_id, import_enabled: bool) -> int:
    """进库开关翻转的可见性联动：关 → 该源全部托管行隐藏；开 → 恢复。

    只动 is_hidden（可逆），绝不删行、不动映射（映射读契约本就只认
    活行，隐藏期间线路自然不可见）。恢复时把该源托管行一律置可见：
    管理员对单条托管行的手工隐藏会被开关恢复覆盖，以开关为准
    （与 enabled 停用/启用的整体口径一致）。返回受影响行数。
    """
    updated = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.upstream_source_id == source_id)
        .update({em.MediaItem.is_hidden: not import_enabled},
                synchronize_session=False))
    db.flush()
    logger.info("上游源进库开关联动可见性 source_id=%s import_enabled=%s "
                "rows=%s", source_id, import_enabled, updated)
    return int(updated or 0)


def enable_source(db, source):
    """启用上游源（单独动作；启用本身不探活，健康状态仍是占位值）。"""
    source.enabled = True
    source.updated_at = datetime.now()
    db.flush()
    logger.info("上游源启用 id=%s name=%r", source.id, source.name)
    return source


def disable_source(db, source):
    """停用上游源（25-2/25-4 只许读 enabled 的源，停用即断聚合与播放）。"""
    source.enabled = False
    source.updated_at = datetime.now()
    db.flush()
    logger.info("上游源停用 id=%s name=%r", source.id, source.name)
    return source


def delete_source(db, source) -> dict:
    """删除上游源并联动清理（PA-038；同一事务内，调用方提交）。

    旧口径“只删本行”已作废：无外键约束下，源行一删，托管行
    仍带 tmdb_id 上架、可点不可播（身份门以 tmdb 非空为准入，
    货架面全仓无 upstream_source_id 过滤），2026-11-01 唯一源
    删除时就得靠 parent 手工软删 6,074 部托管行＋硬删 9,554 条
    映射——产品删口必须自己做这件事。联动清单：
    - 托管行：全部软删（复用 upstream_import._delete_rows 口径：
      软删开时 mark_soft_deleted，否则硬删；剧→季→集子树行
      同样带 upstream_source_id，按源过滤即全覆盖）；
    - 硬删残留行：映射、自动建库关系、托管生命周期、分类绑定、
      非 running 聚合任务（running 行保留：在跑线程删行也拦
      不住，留待其自然失败／存量过期清理；其写映射读口本就
      验源存在，源删后线路自然不可见）；
    - 本地自动建库本身不动：库可能还挂其它内容，关系行已删，
      库不再与源关联，空库由管理员手动处理。
    返回各段计数供调用方核对。残留窗口：删源恰逢聚合任务在跑，
    该任务此后仍可能写出少量带已删 source_id 的托管行（执行前
    才验源存在），属已知竞态，不在本单加锁，管理员删源前停
    聚合可避开。
    """
    from backend.emby_server.upstream_import import _delete_rows

    source_id = source.id
    hosted = (db.query(em.MediaItem)
              .filter(em.MediaItem.upstream_source_id == source_id)
              .all())
    hosted_deleted = _delete_rows(db, hosted)
    residual = (db.query(em.MediaItem)
                .filter(em.MediaItem.upstream_source_id == source_id)
                .count())
    if residual:
        logger.warning("上游源删除后托管行仍有残留 source_id=%s residual=%s "
                       "（软删守卫 fail-closed 剔除，须人工复核）",
                       source_id, residual)

    def _purge(model, extra=None):
        q = db.query(model).filter(model.source_id == source_id)
        if extra is not None:
            q = q.filter(extra)
        n = q.delete(synchronize_session=False)
        db.flush()
        return int(n or 0)

    stats = {
        "hosted_rows_deleted": int(hosted_deleted),
        "hosted_rows_residual": int(residual),
        "mappings_deleted": _purge(em.UpstreamItemMapping),
        "library_links_deleted": _purge(em.UpstreamLibraryLink),
        "lifecycle_deleted": _purge(em.UpstreamHostedLifecycle),
        "category_bindings_deleted": _purge(em.SourceCategoryBinding),
        "tasks_deleted": _purge(
            em.UpstreamAggregateTask,
            em.UpstreamAggregateTask.status != "running"),
    }
    logger.info("上游源删除 id=%s name=%r stats=%r",
                source_id, source.name, stats)
    db.delete(source)
    db.flush()
    return stats


def set_health_status(db, source, status: str):
    """写健康状态占位（25-3 探活工人的唯一写入口；本模块不探活）。

    status 只许出自 HEALTH_STATUSES；写状态同时刷新 last_checked_at。
    """
    if status not in HEALTH_STATUSES:
        raise UpstreamSourceError(
            f"health_status={status!r} 不在允许集 {HEALTH_STATUSES}")
    source.health_status = status
    source.last_checked_at = datetime.now()
    source.updated_at = datetime.now()
    db.flush()
    return source


# ---------------------------------------------------------------------------
# 读操作
# ---------------------------------------------------------------------------

def get_source(db, source_id: int):
    """按 id 取源行（没有返回 None，管理端映射 404）。"""
    return (db.query(em.UpstreamSource)
            .filter(em.UpstreamSource.id == source_id).first())


def list_sources(db):
    """全部上游源（id 升序；管理端列表用，不分页——源是管理员手工建的，
    数量级是「台」不是「条」，分页是多余复杂度）。"""
    return (db.query(em.UpstreamSource)
            .order_by(em.UpstreamSource.id).all())


def enabled_sources(db):
    """已启用的上游源（25-2/25-4 只许读这些；关着的源不许拉目录、不许播）。"""
    return (db.query(em.UpstreamSource)
            .filter(em.UpstreamSource.enabled.is_(True))
            .order_by(em.UpstreamSource.id).all())


# ---------------------------------------------------------------------------
# 分类绑定（苹果 CMS W1：只做持久化，不拉目录、不做同步）
# ---------------------------------------------------------------------------

def list_category_bindings(db, source_id: int):
    """某源的全部类别绑定（id 升序；含 sync_enabled=False 的行）。"""
    return (db.query(em.SourceCategoryBinding)
            .filter(em.SourceCategoryBinding.source_id == source_id)
            .order_by(em.SourceCategoryBinding.id).all())


def set_category_binding(db, source_id: int, category_id: str,
                         category_name: Optional[str] = None,
                         sync_enabled: bool = True):
    """绑定上游分类（幂等 upsert）：同一（源，分类 id）重复写只更新
    分类名 / 同步开关 / 更新时间，不建新行。只 flush 不 commit，
    调用方事务内执行（照 create_source 口径）。"""
    cid = (category_id or "").strip()
    if not cid:
        raise UpstreamSourceError("上游分类 id 不能为空")
    row = (db.query(em.SourceCategoryBinding)
           .filter(em.SourceCategoryBinding.source_id == source_id,
                   em.SourceCategoryBinding.upstream_category_id == cid)
           .first())
    now = datetime.now()
    if row is None:
        row = em.SourceCategoryBinding(
            source_id=source_id,
            upstream_category_id=cid,
            upstream_category_name=(category_name.strip()
                                    if category_name else None),
            sync_enabled=bool(sync_enabled),
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.upstream_category_name = (category_name.strip()
                                      if category_name else None)
        row.sync_enabled = bool(sync_enabled)
        row.updated_at = now
    db.flush()
    return row


def enabled_category_bindings(db, source_id: int):
    """某源已启用同步的分类绑定（只返回 sync_enabled=True 的行）。"""
    return (db.query(em.SourceCategoryBinding)
            .filter(em.SourceCategoryBinding.source_id == source_id,
                    em.SourceCategoryBinding.sync_enabled.is_(True))
            .order_by(em.SourceCategoryBinding.id).all())


def source_payload(source) -> dict:
    """源行的对外形态：永远不出凭据原文、不出密封值，只出 has_secret。

    has_secret 的语义与 FMBY 自连实测的响应一致：配没配凭据只给布尔值。
    """
    return {
        "id": source.id,
        "name": source.name,
        "source_type": source.source_type,
        "base_url": source.base_url,
        "auth_method": source.auth_method,
        "username": source.username,
        "has_secret": has_secret(source),
        "extra_headers": _extra_headers_dict(source.extra_headers_json),
        "user_agent": source.user_agent,
        "referer": source.referer,
        "enabled": bool(source.enabled),
        "import_enabled": bool(getattr(source, "import_enabled", True)),
        "credential_scope": source.credential_scope,
        "health_status": source.health_status,
        "last_checked_at": (source.last_checked_at.isoformat()
                            if source.last_checked_at else None),
        "note": source.note,
        "created_at": source.created_at.isoformat() if source.created_at else None,
        "updated_at": source.updated_at.isoformat() if source.updated_at else None,
    }


def _extra_headers_dict(extra_headers_json: Optional[str]) -> dict:
    """附加请求头 JSON 文本 → dict（脏数据返回空对象，不炸列表）。"""
    if not extra_headers_json:
        return {}
    try:
        parsed = json.loads(extra_headers_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
