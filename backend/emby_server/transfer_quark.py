# -*- coding: utf-8 -*-
"""夸克跨 provider 转存执行面（二期，2026-11-01）。

形状照 FMBY + 站内既有跨 provider 执行器（transfer_variant 的
_run_cross_drive_to_115 / _run_cross_115_to_drive）：异步任务由
工人认领后经 transfer_variant._run_cross_job 按 dest 前缀
（quark:）分发到这里；换链与搬字节分离（源侧先解析、再分段取
字节、最后夸克上传 + 列目标目录按名核对）；不另起队列、不另起
错误分类（终态/重试一律走 transfer_variant 的 _fail_job /
_schedule_retry）。

夸克没有服务端 copy：源（Drive 挂载 / 115）在另一家盘里，
只能走夸克上传链——优先秒传（md5+sha1 哈希预检，命中零字节），
未命中分片上传。落点是用户夸克盘的站内专属目录
（transfer_dir_name()，与 drive/115 的 aetrix-transfer 同名口径）。

凭据纪律：只用该用户自己的夸克绑定 Cookie
（user_bindings.binding_quark_cookie 唯一解封口）；用后吸收
上游 Cookie 轮换回写（absorb_quark_cookie_rotation）。源侧凭据
（SA / 服主 115）失效只落任务失败，绝不碰用户绑定。

API 形态诚实标注：夸克上传链的端点/字段以 OpenList 公开实现
为准、auth 段为推测（见 quarkdrive 模块 docstring），无真实
账号活体验证；失败会响亮落在任务 error_detail 里，不静默。
"""
from __future__ import annotations

import hashlib
import logging

from backend.emby_server import quarkdrive
from backend.emby_server import transfer_variant as tv

logger = logging.getLogger(__name__)


def _make_quark_client(cookie):
    """夸克客户端构造口（测试可 monkeypatch 本模块属性替换）。

    生产路径固定用用户自己的 Cookie，绝不用服主/默认账号档。
    """
    return quarkdrive.QuarkClient(cookie)


def ensure_quark_transfer_dir(client) -> str:
    """用户夸克盘转存目录 fid：根目录下找专属目录，没有就建。

    与 _ensure_115_transfer_dir 同款口径：建目录响应形态未活体
    验证，故建完必按名重列根目录验 fid，不依赖 mkdir 响应；
    同名冲突按已存在处理（并发建目录时另一方已建成）。
    """
    name = tv.transfer_dir_name()
    for entry in client.list_dir("0"):
        if entry.get("is_dir") and (entry.get("name") or "") == name:
            fid = entry.get("fid") or ""
            if fid:
                return fid
    try:
        client.mkdir("0", name)
    except quarkdrive.QuarkError as exc:
        if not quarkdrive.is_name_conflict_error(exc):
            raise
    for entry in client.list_dir("0"):
        if entry.get("is_dir") and (entry.get("name") or "") == name:
            fid = entry.get("fid") or ""
            if fid:
                return fid
    raise quarkdrive.QuarkError("夸克建转存目录后仍找不到该目录")


def match_quark_copy_in_dir(client, dir_fid, name, size,
                            expected_fid=None) -> str:
    """列目标目录核对新文件（FMBY discover 口径，与 115 版同规则）。

    返回新文件 fid，核不到返回 ""。只认非目录项：
    - 回执 fid 在同名项中 → 返回它；
    - 否则按名 + 大小命中（大小缺失时跳过大小比对）；
    - 否则恰好一个同名文件时返回它（目录为准）；
    - 多个同名且无一是回执 id 时返回 ""（冲突，不猜）。
    """
    size_int = tv._to_int(size)
    matches = []
    for entry in client.list_dir(dir_fid):
        if entry.get("is_dir"):
            continue
        if (entry.get("name") or "") != name:
            continue
        matches.append(entry)
    if not matches:
        return ""
    if expected_fid:
        for entry in matches:
            if (entry.get("fid") or "") == expected_fid:
                return expected_fid
    if size_int:
        for entry in matches:
            if tv._to_int(entry.get("size")) == size_int:
                return entry.get("fid") or ""
    if len(matches) == 1:
        return matches[0].get("fid") or ""
    return ""


class DualHashReader:
    """透传双哈希包装：委托内层读取器，顺序读到的字节同时喂
    md5 与 sha1（夸克秒传预检要 md5+sha1 双哈希）。

    链规则与 transfer_variant._HashingReader 逐字同款：回 seek
    重读已计入区间不重复喂；跳读未计入区间则哈希链断裂、作废；
    读满全长前 hexdigest 返回 None（半截哈希绝不冒充整文件）。
    """

    def __init__(self, inner, size):
        self._inner = inner
        self._size = int(size)
        self._md5 = hashlib.md5()
        self._sha1 = hashlib.sha1()
        self._hashed_until = 0
        self._broken = False

    def tell(self):
        return self._inner.tell()

    def seek(self, offset, whence=0):
        return self._inner.seek(offset, whence)

    def read(self, n=-1):
        pos = self._inner.tell()
        data = self._inner.read(n)
        if data:
            if pos == self._hashed_until:
                self._md5.update(data)
                self._sha1.update(data)
                self._hashed_until += len(data)
            elif pos > self._hashed_until:
                self._broken = True  # 跳过了未计入区间，哈希链断裂
            # pos < _hashed_until：重读已计入区间，同内容不重复喂
        return data

    def md5_hexdigest(self):
        """整文件 md5（hex 小写）；未读满/链断裂返回 None。"""
        if self._broken or self._hashed_until != self._size:
            return None
        return self._md5.hexdigest()

    def sha1_hexdigest(self):
        """整文件 sha1（hex 小写）；未读满/链断裂返回 None。"""
        if self._broken or self._hashed_until != self._size:
            return None
        return self._sha1.hexdigest()


def _http_get(url, headers, timeout=120):
    """115 源 Range 取字节的 HTTP 出口（测试打桩点）。"""
    import httpx

    return httpx.request("GET", url, headers=headers, timeout=timeout)


def _fetch_115_range(client, url, start, end):
    """115 源分段取字节（与 _run_cross_115_to_drive 的 _raw_fetch
    同口径：UA/Referer/Cookie 三件套 + Range，状态码严格校验）。"""
    headers = {"User-Agent": client.ua,
               "Referer": "https://115.com/",
               "Cookie": client.cookie,
               "Range": f"bytes={start}-{end}"}
    try:
        resp = _http_get(url, headers)
    except Exception as exc:  # noqa: BLE001 — 网络类收敛成远端错误
        raise tv._CrossRemoteError(
            f"115 源下载网络异常：{type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise tv._CrossAuthError(
            f"115 源下载被拒：{resp.status_code}（服主侧凭据失效）")
    if resp.status_code != 206:
        raise tv._CrossRemoteError(
            f"115 源下载失败：{resp.status_code}（非 206）")
    data = resp.content or b""
    if len(data) != end - start + 1:
        raise tv._CrossRemoteError(
            f"115 源下载短读：要 {end - start + 1} 字节，"
            f"实得 {len(data)} 字节")
    return data


_HASH_PASS_CHUNK = 8 * 1024 * 1024


def run_cross_to_quark(db, job, item, drive_api):
    """跨 provider →用户夸克盘：源解析 → 哈希遍（秒传预检）→
    夸克上传（秒传优先、未命中分片）→ 目录核对 → 登记 ready。

    失败分类照既有跨执行器口径：
    - 用户夸克 Cookie 失效 → 先验活复核（recheck_quark_binding），
      复核也鉴权失败才绑定落 expired + failed/auth_error；复核
      未判死按 retriable 重排、不落绑定死（2026-11-02 去单点化）；
    - 源侧凭据（SA / 服主 115）失效 → failed/auth_error，绝不
      碰用户绑定；
    - 源已死 → failed/dead；目录核不到 → failed/copy_unresolved
      （不重试）；其余 → _schedule_retry 有界重试。
    """
    from backend.emby_server import user_bindings as ub
    from backend.emby_server.transfer115 import Pan115AuthError

    uid = (getattr(job, "owner_ref", "") or "").strip()
    try:
        user_id = int(uid)
    except (TypeError, ValueError):
        user_id = None
    binding = tv._active_binding(db, user_id, "quark") if user_id else None
    if binding is None:
        return tv._fail_job(db, job, "no_binding",
                            "用户夸克绑定已失效或不存在（不重试）")
    name = tv._copy_name_for_item(item)
    provider = (getattr(job, "provider", "") or "").strip()

    # ---- 源解析（换链步骤；与搬字节分离） ----
    src_sha1_hint = ""
    try:
        if provider == "drive":
            orig_fid = (getattr(item, "drive_file_id", "") or "").strip()
            source_drive = (getattr(job, "drive_id", "") or "").strip()
            if not source_drive:
                return tv._fail_job(db, job, "no_drive",
                                    "跨任务无源 Drive 盘标识")
            info = drive_api.get_file(source_drive, orig_fid)
            size = tv._to_int((info or {}).get("size"))

            def _raw_fetch(start, end, _d=source_drive, _f=orig_fid):
                return drive_api.download_range(_d, _f, start, end)

            stage = tv._acquire_source_stage("drive", source_drive,
                                             orig_fid, size)
        elif provider == "pan115":
            server_client, pickcode, src_size, src_sha1 = \
                tv._resolve_115_source(db, item)
            src_sha1_hint = (src_sha1 or "").strip().lower()
            try:
                url = server_client.download_url(pickcode)
            except Pan115AuthError as exc:
                return tv._fail_job(db, job, "auth_error",
                                    f"服主侧 115 凭据失效：{exc}")
            size = src_size or tv._to_int(getattr(item, "size", None))

            def _raw_fetch(start, end, _c=server_client, _u=url):
                return _fetch_115_range(_c, _u, start, end)

            stage = tv._acquire_source_stage(
                "pan115", "", getattr(item, "drive_file_id", "") or "",
                size)
        else:
            return tv._fail_job(
                db, job, "unsupported",
                f"夸克跨转存不支持的源 provider：{provider}（不重试）")
    except (tv._NotFound, tv._Forbidden) as exc:
        return tv._fail_job(db, job, "dead", f"源已死/无权：{exc}")
    except tv._CrossAuthError as exc:
        return tv._fail_job(db, job, "auth_error", str(exc))
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": tv._schedule_retry(db, job, exc)}
    if not size:
        size = tv._to_int(getattr(item, "size", None))
    if size <= 0:
        return tv._fail_job(
            db, job, "unsupported",
            "源文件大小未知/为零，无法分段搬运（不猜大小）")
    fetch = tv._staged_fetch(_raw_fetch, stage)

    # ---- 哈希遍：秒传预检要 md5+sha1 双哈希 ----
    # 夸克分片上传本身不需要哈希（与 115 simple 的回调 sha1
    # 不同，夸克 finish 不回整文件哈希），哈希遍是拿秒传资格与
    # sha1 沉淀的唯一路：字节走暂存包装，上传时同内容直接读
    # 暂存、不再重读源（暂存未命中时多读一遍源，与秒传 sign
    # 遍的代价同级）。透传哈希是实际字节的事实，优先于源列表值。
    dual = DualHashReader(tv._SegmentReader(size, fetch), size)
    try:
        while dual.tell() < size:
            if not dual.read(min(_HASH_PASS_CHUNK, size - dual.tell())):
                break
    except tv._CrossAuthError as exc:
        return tv._fail_job(db, job, "auth_error", str(exc))
    except (tv._NotFound, tv._Forbidden) as exc:
        return tv._fail_job(db, job, "dead", f"源已死/无权：{exc}")
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": tv._schedule_retry(db, job, exc)}
    stream_sha1 = dual.sha1_hexdigest() or ""
    if (stream_sha1 and src_sha1_hint
            and tv._is_sha1_hex(src_sha1_hint)
            and src_sha1_hint != stream_sha1):
        logger.warning(
            "→夸克搬运 sha1 不一致 item=%s list=%s stream=%s"
            "（以透传为准）",
            getattr(item, "id", None), src_sha1_hint[:12],
            stream_sha1[:12])
    md5_hex = dual.md5_hexdigest() or ""
    sha1_hex = stream_sha1 or src_sha1_hint

    # ---- 夸克侧：取凭据 → 建目录 → 上传 → 核对 ----
    # 凭据临界区（解封→用→回写）全程持 per-binding 锁，与点播
    # 取链/手动重验串行（user_bindings.quark_binding_lock）；
    # 锁只罩夸克侧——源解析/哈希遍不经手夸克凭据，不持锁。
    with ub.quark_binding_lock(binding.id):
        return _run_quark_side(
            db, job, item, binding, user_id, name, size, fetch,
            md5_hex=md5_hex, sha1_hex=sha1_hex,
            stream_sha1=stream_sha1, src_sha1_hint=src_sha1_hint)


def _auth_detail(exc, step: str) -> str:
    """鉴权失败落库文案：[步位名] + 异常原文 + 上游 code。

    步位名 dir/pre/hash/part/finish/confirm 让死步可考（异常自
    带 step 时由 _auth_fail 传入）；上游 code 在原文里没有时从
    异常的 upstream_code 字段补上（quarkdrive 在 401/403 时读
    响应体记录，见其 QuarkError 字段）。文案不含凭据原文。
    """
    text = str(exc)
    code = getattr(exc, "upstream_code", None)
    if code is not None and f"code={code}" not in text:
        text = f"{text}（上游 code={code}）"
    return f"[{step}] {text}"


def _run_quark_side(db, job, item, binding, user_id, name, size,
                    fetch, *, md5_hex, sha1_hex, stream_sha1,
                    src_sha1_hint):
    """夸克侧执行（锁内）：取凭据 → 建目录 → 上传 → 目录核对
    → 登记 ready。失败分类见 run_cross_to_quark docstring。"""
    from backend.emby_server import user_bindings as ub

    try:
        cookie = ub.binding_quark_cookie(db, user_id, binding.id)
    except ub.BindingError as exc:
        if exc.kind == "auth_error":
            return tv._fail_job(db, job, "auth_error", str(exc))
        if exc.kind == "not_found":
            return tv._fail_job(db, job, "no_binding", str(exc))
        return {"job_id": job.id,
                "outcome": tv._schedule_retry(db, job, exc)}
    client = _make_quark_client(cookie)

    def _absorb():
        ub.absorb_quark_cookie_rotation(db, binding, cookie,
                                        client.cookie)

    def _auth_fail(exc, step=None):
        _absorb()
        step = step or getattr(exc, "step", None) or "unknown"
        detail = _auth_detail(exc, step)
        # 判死去单点化（2026-11-02）：单次鉴权失败先用库内最新
        # Cookie 验活复核，复核也鉴权失败才落 expired +
        # failed/auth_error；复核未判死（alive/unknown）说明这
        # 次 401 多半是并发轮换互踩的一过性失败——绑定不落死、
        # 本单按 retriable 重排。此前是任一步抛 QuarkAuthError
        # 即打死绑定，同用户其余 pending 单随即被认领前复核
        # 秒判 no_binding（24h 内级联 71 条）。
        verdict = ub.recheck_quark_binding(
            db, binding, client_factory=_make_quark_client)
        if verdict == "dead":
            try:
                ub._mark_failed(db, binding, "expired")
                db.commit()
            except Exception:  # noqa: BLE001 — 落态失败不掩盖原错误
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
            return tv._fail_job(db, job, "auth_error", detail)
        logger.warning(
            "夸克转存单次鉴权失败但复核未判死 job=%s step=%s "
            "verdict=%s，按可重试重排（不落绑定死）",
            job.id, step, verdict)
        return {"job_id": job.id,
                "outcome": tv._schedule_retry(
                    db, job, RuntimeError(detail))}

    try:
        dest_dir = ensure_quark_transfer_dir(client)
    except quarkdrive.QuarkAuthError as exc:
        return _auth_fail(exc, "dir")
    except Exception as exc:
        _absorb()
        return {"job_id": job.id,
                "outcome": tv._schedule_retry(db, job, exc)}
    reader = tv._SegmentReader(size, fetch)
    try:
        result = client.upload_file(reader, name, size, dest_dir,
                                    md5=md5_hex, sha1=sha1_hex)
    except quarkdrive.QuarkAuthError as exc:
        return _auth_fail(exc)
    except tv._CrossAuthError as exc:
        _absorb()
        return tv._fail_job(db, job, "auth_error", str(exc))
    except (tv._NotFound, tv._Forbidden) as exc:
        _absorb()
        return tv._fail_job(db, job, "dead", f"源已死/无权：{exc}")
    except Exception as exc:
        _absorb()
        return {"job_id": job.id,
                "outcome": tv._schedule_retry(db, job, exc)}
    try:
        new_fid = match_quark_copy_in_dir(
            client, dest_dir, name, size,
            expected_fid=(result or {}).get("fid") or None)
    except quarkdrive.QuarkAuthError as exc:
        return _auth_fail(exc, "confirm")
    except Exception as exc:
        _absorb()
        return {"job_id": job.id,
                "outcome": tv._schedule_retry(db, job, exc)}
    _absorb()
    if not new_fid:
        return tv._fail_job(
            db, job, "copy_unresolved",
            f"夸克转存上传后在用户转存目录中核不到新文件"
            f"（文件名={name}，回执 fid="
            f"{(result or {}).get('fid') or '无'}），不登记、不回落")
    if stream_sha1:
        tv._persist_item_sha1(db, item, stream_sha1,
                              origin="quark_cross_stream")
    elif src_sha1_hint:
        tv._persist_item_sha1(db, item, src_sha1_hint,
                              origin="pan115_source_list")
    row = tv._register_ready_variant(db, job, item, new_fid,
                                     f"quark:user-{user_id}", size=size)
    logger.info("→夸克转存完成 job=%s item=%s outcome=%s fid=%s",
                job.id, getattr(item, "id", None),
                (result or {}).get("outcome"), new_fid)
    return {"job_id": job.id, "outcome": "done", "variant_id": row.id}
