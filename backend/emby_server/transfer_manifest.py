# -*- coding: utf-8 -*-
"""换代线地基：交付状态落盘 + 块哈希清单记账（2026-11-01）。

蓝图 v2 附二换代线「分块清单同步」的第一刀地基（可行性报告
chunk-sync-engine-feasibility-20261031.md §七顺序 1–2）：

1. **交付状态落盘**：Drive→115 分片会话此前只活在 worker 本地
   sidecar（mp-<job>.json，stage 目录默认 /tmp、容器重建即失）。
   本模块把同一会话旁路同步进 ``transfer_deliveries`` 表（part
   ↔块映射一并入册），sidecar 缺失时执行面凭表行恢复会话——
   在飞单不因部署/容器重建丢续传状态。旧 sidecar 的读口径
   （content_key＋size＋目标目录三项校验）原样保留、仍是第一
   落点；表恢复是 sidecar 读不到时的第二落点，恢复出的会话
   会被执行面回写 sidecar，之后与旧路径无差别。
2. **清单＋块哈希透传**：搬运程内（哈希遍／分片上传读程）字节
   流经 ``TrackingReader`` 时顺手算源侧固定 8 MiB 块的 SHA-256
   与整文件 MD5，写入 ``transfer_content_manifests``（一内容
   一行、块哈希定长打包成一个 blob，见模型注释）。整文件
   sha1 不在本模块算——它由现行交付链的 _HashingReader 透传
   与回调交叉核对后经 ``flush(whole_sha1=...)`` 入册，两套
   哈希分工不混用（报告 §四.1）。

**红线（点火门，报告 §一.6）**：本模块只记账，不接管任何真实
执行路径——不做调度合并、不做会话语义裁决；115 续传认不认
会话仍由 transfer115 的 ListParts 对账说了算，落盘行只是给
sidecar 丢失时多一个找回会话的来源。

**fail-open 总则**：本模块全部 DB 函数失败只记日志返回失败
标记，绝不抛进转存主流程；``ChunkTracker.feed`` 与
``TrackingReader`` 的记账动作同样吞异常——清单写失败、tracker
出错，最坏结果是这一单没长出清单，转存成败/重试/核对语义与
没有本模块时逐字一致。总开关 ``transfer_manifest_accounting_
enabled``（SystemConfig，默认开）关时执行面根本不接线。
"""

import hashlib
import json
import logging
from datetime import datetime

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

#: 源侧固定块大小（可行性报告 §六定版）：与 _CROSS_SEGMENT、
#: 暂存顺序读同口径 8 MiB；是 Drive resumable 256 KiB 粒度的
#: 整数倍（32×），合规。
CHUNK_SIZE = 8 * 1024 * 1024

_HEX64 = frozenset("0123456789abcdef")
_HEX40 = frozenset("0123456789abcdef")
_HEX32 = frozenset("0123456789abcdef")


def _is_hex(value, chars, length):
    return (isinstance(value, str) and len(value) == length
            and all(c in chars for c in value))


def chunk_count_for(size, chunk_size=CHUNK_SIZE):
    size = int(size)
    if size <= 0:
        return 0
    return -(-size // int(chunk_size))


def _chunk_len(index, size, chunk_size):
    start = index * chunk_size
    return max(0, min(chunk_size, size - start))


# ---------------------------------------------------------------------------
# 块哈希 blob 打包（一内容一 blob：定长 32B/块 + bitmap 标记入册位）
# ---------------------------------------------------------------------------

def pack_chunk_hashes(hashes, chunk_count):
    """{块序号: sha256 hex} → (blob, bitmap)。

    blob 为 chunk_count × 32B 定长打包，未入册块零填充；bitmap
    每块 1 bit 标记已入册——零填充与真实哈希靠 bitmap 区分。
    非法序号/哈希值直接忽略（记账层不为坏输入抛错）。
    """
    chunk_count = int(chunk_count)
    blob = bytearray(chunk_count * 32)
    bitmap = bytearray(-(-chunk_count // 8) if chunk_count > 0 else 0)
    for index, value in (hashes or {}).items():
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue
        value = str(value or "").strip().lower()
        if not (0 <= index < chunk_count) or not _is_hex(value, _HEX64, 64):
            continue
        blob[index * 32:(index + 1) * 32] = bytes.fromhex(value)
        bitmap[index // 8] |= (1 << (index % 8))
    return bytes(blob), bytes(bitmap)


def unpack_chunk_hashes(blob, bitmap, chunk_count):
    """pack_chunk_hashes 的逆：blob/bitmap → {块序号: sha256 hex}。

    形态不对（长度不符/为空）一律回空 dict——清单行损坏时当
    没记过处理，靠后续搬运重新长，绝不把坏数据当事实。
    """
    chunk_count = int(chunk_count or 0)
    if chunk_count <= 0 or not blob or not bitmap:
        return {}
    if len(blob) < chunk_count * 32:
        return {}
    out = {}
    for index in range(chunk_count):
        byte_i = index // 8
        if byte_i >= len(bitmap) or not (bitmap[byte_i] >> (index % 8)) & 1:
            continue
        out[index] = bytes(blob[index * 32:(index + 1) * 32]).hex()
    return out


def part_chunk_span(number, partsize, size, chunk_size=CHUNK_SIZE):
    """第 number 片（1 起）覆盖的清单块序号闭区间 (from, to)。

    part 是目标侧交付单位（大小由 _determine_partsize 定），块
    是源侧清单单位（钉 8 MiB），两层分离：本函数把 part 的字节
    区间 [start, end) 折算成它完整/部分覆盖的块序号范围，映射
    入册后交付状态不再靠"偏移＝片数×片大小"乘法反推。
    """
    number = int(number)
    partsize = int(partsize)
    size = int(size)
    start = (number - 1) * partsize
    end = min(number * partsize, size)
    if number < 1 or partsize <= 0 or start >= end:
        return (0, -1)
    return (start // chunk_size, (end - 1) // chunk_size)


# ---------------------------------------------------------------------------
# 搬运程内块哈希累加器（纯计算，不碰 DB、不抛错）
# ---------------------------------------------------------------------------

class ChunkTracker:
    """顺手算块 SHA-256 + 整文件 MD5 的流式累加器。

    与 _HashingReader 同纪律：只认从读位置连续流过的字节。
    - 块哈希：同一时刻只攒一个"当前块"的增量哈希——字节读程
      在本管道里恒顺序（分段读取器 + seek 到已成片偏移续读），
      读位置落进哪个块就往哪个块攒；块头字节没在本程见过（续
      传从块中间 seek 起）时该块本程攒不齐，保持未入册，等某
      一程从块头顺序读过再补——绝不拿半块字节拼哈希。
    - 重读已入册/已攒过的区间不重复喂（seek 回读同内容由内层
      分段读取器保证）；跳过未攒区间只废当前块的部分进度。
    - 整文件 MD5：从 0 起的连续前沿（同 _HashingReader 的
      sha1 口径）；前沿断裂即作废，读满全长才给值。
    - ``existing``：此前已入册的块哈希（从清单行解包），作为
      种子直接进已入册集合，本程只补缺块。
    """

    def __init__(self, size, chunk_size=CHUNK_SIZE, existing=None):
        self.size = int(size)
        self.chunk_size = int(chunk_size)
        self.chunk_count = chunk_count_for(self.size, self.chunk_size)
        self._final = {}
        for index, value in (existing or {}).items():
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
            value = str(value or "").strip().lower()
            if 0 <= index < self.chunk_count and _is_hex(value, _HEX64, 64):
                self._final[index] = value
        self._pending_index = None
        self._pending_hash = None
        self._pending_len = 0
        self._md5 = hashlib.md5()
        self._md5_frontier = 0
        self._md5_broken = False

    def _drop_pending(self):
        self._pending_index = None
        self._pending_hash = None
        self._pending_len = 0

    def feed(self, pos, data):
        """喂入在读位置 pos 处读到的字节（失败静默：记账不拖主流程）。"""
        try:
            self._feed(pos, data)
        except Exception:  # noqa: BLE001 — 累加器坏了也不能打挂搬运
            pass

    def _feed(self, pos, data):
        if not data or pos is None or pos < 0:
            return
        pos = int(pos)
        # 整文件 MD5 前沿（与 _HashingReader 的 sha1 同口径）。
        if not self._md5_broken:
            if pos == self._md5_frontier:
                self._md5.update(data)
                self._md5_frontier += len(data)
            elif pos > self._md5_frontier:
                self._md5_broken = True
            # pos < frontier：重读已计入区间，不重复喂。
        # 块哈希：按读位置逐段归块。
        end = pos + len(data)
        cursor = pos
        while cursor < end:
            index = cursor // self.chunk_size
            offset_in_chunk = cursor % self.chunk_size
            take = min(self.chunk_size - offset_in_chunk, end - cursor)
            segment = data[cursor - pos:cursor - pos + take]
            if index not in self._final:
                if offset_in_chunk == 0:
                    # 块头到达：以本段为新起点重新起攒——此前攒
                    # 的无论是哪块（含本块半截进度、如失败尝试留
                    # 下的）都被块头重读 supersede，内容同源不
                    # 会算错，只会补齐。
                    self._pending_index = index
                    self._pending_hash = hashlib.sha256()
                    self._pending_len = 0
                if (self._pending_index == index
                        and offset_in_chunk == self._pending_len):
                    self._pending_hash.update(segment)
                    self._pending_len += take
                    if self._pending_len == _chunk_len(
                            index, self.size, self.chunk_size):
                        self._final[index] = self._pending_hash.hexdigest()
                        self._drop_pending()
                elif self._pending_index == index:
                    # 块内不连续（块头没见过/中间跳段）：本块本程
                    # 攒不齐，丢弃部分进度，等整块顺序读再补。
                    self._drop_pending()
            cursor += take

    def chunk_hashes(self):
        """已入册块哈希 {序号: hex}（含种子与本程攒满的块）。"""
        return dict(self._final)

    def is_complete(self):
        return (self.chunk_count > 0
                and len(self._final) >= self.chunk_count)

    def whole_md5(self):
        """整文件 MD5（hex 小写）；未从 0 连续读满全长返回 None。"""
        if self._md5_broken or self._md5_frontier != self.size:
            return None
        return self._md5.hexdigest()


class TrackingReader:
    """透传记账包装：委托内层读取器，read 到的字节顺手喂 tracker。

    与 _HashingReader 同形态（tell/seek/read 委托）；记账动作
    全程吞异常——tracker 坏了返回的仍是原字节原语义。
    """

    def __init__(self, inner, tracker):
        self._inner = inner
        self._tracker = tracker

    def __getattr__(self, name):
        # 内层读取器的其余接口（如 _HashingReader.sha1_hexdigest）
        # 原样透传——包装层只加记账，不许遮蔽被包对象的能力。
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def tell(self):
        return self._inner.tell()

    def seek(self, offset, whence=0):
        return self._inner.seek(offset, whence)

    def read(self, n=-1):
        try:
            pos = self._inner.tell()
        except Exception:  # noqa: BLE001 — 记账取位失败不影响读
            pos = None
        data = self._inner.read(n)
        if data and pos is not None and self._tracker is not None:
            try:
                self._tracker.feed(pos, data)
            except Exception:  # noqa: BLE001 — fail-open
                pass
        return data


# ---------------------------------------------------------------------------
# 清单入册（fail-open：失败只记日志，返回 False）
# ---------------------------------------------------------------------------

class ManifestRecorder:
    """一张内容在本程搬运里的清单记账句柄（由 begin_recorder 建）。"""

    def __init__(self, content_key, size, tracker):
        self.content_key = content_key
        self.size = int(size)
        self.tracker = tracker

    def flush(self, db, whole_sha1=None, whole_md5=None):
        """把 tracker 当前进度并入清单行（可多次调，增量入册）。

        whole_sha1 只接受调用方已经交叉核对过的值（现行交付链
        口径），本函数只做形态校验与入册；whole_md5 缺省取
        tracker 程内算出的整文件 MD5。已入册块哈希先写者胜：
        同一 content_key 即同一份源字节，后写者若算出不同值只
        记警告不覆盖（防坏字节污染已入册事实，与暂存"文件是
        事实来源"同精神）。
        """
        if db is None or self.tracker is None:
            return False
        try:
            row = (db.query(em.TransferContentManifest)
                   .filter_by(content_key=self.content_key).one_or_none())
            if row is not None and int(row.chunk_size) != self.tracker.chunk_size:
                logger.warning(
                    "transfer_manifest flush skipped: chunk_size drift "
                    "content_key=%s row=%s tracker=%s",
                    self.content_key, row.chunk_size,
                    self.tracker.chunk_size)
                return False
            merged = {}
            if row is not None:
                merged = unpack_chunk_hashes(
                    row.chunk_hashes, row.chunk_bitmap, row.chunk_count)
            for index, value in self.tracker.chunk_hashes().items():
                if index in merged and merged[index] != value:
                    logger.warning(
                        "transfer_manifest chunk hash conflict "
                        "content_key=%s chunk=%s（保留已入册值）",
                        self.content_key, index)
                    continue
                merged[index] = value
            chunk_count = self.tracker.chunk_count
            blob, bitmap = pack_chunk_hashes(merged, chunk_count)
            sha1 = (str(whole_sha1 or "").strip().lower() or None)
            if sha1 is not None and not _is_hex(sha1, _HEX40, 40):
                sha1 = None
            if sha1 is None and row is not None:
                sha1 = row.whole_sha1
            md5 = (str(whole_md5 or "").strip().lower() or None)
            if md5 is not None and not _is_hex(md5, _HEX32, 32):
                md5 = None
            if md5 is None:
                md5 = self.tracker.whole_md5()
            if md5 is None and row is not None:
                md5 = row.whole_md5
            state = ("complete" if (chunk_count > 0
                                    and len(merged) >= chunk_count
                                    and sha1)
                     else "partial")
            if row is None:
                row = em.TransferContentManifest(
                    content_key=self.content_key, size=self.size,
                    chunk_size=self.tracker.chunk_size,
                    chunk_count=chunk_count)
                db.add(row)
            row.whole_sha1 = sha1
            row.whole_md5 = md5
            row.size = self.size
            row.chunk_count = chunk_count
            row.chunk_hashes = blob
            row.chunk_bitmap = bitmap
            row.manifest_state = state
            row.updated_at = datetime.now()
            db.commit()
            return True
        except Exception:  # noqa: BLE001 — 记账失败绝不打挂转存
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(
                "transfer_manifest flush failed content_key=%s",
                self.content_key, exc_info=True)
            return False


def begin_recorder(db, content_key, size):
    """为一次搬运建清单记账句柄：先读已入册块哈希作种子。

    读失败按空种子继续（本程攒的块 flush 时再并入）；size 非法
    或 content_key 为空回 None（调用方据此跳过全部记账接线）。
    """
    if not content_key:
        return None
    try:
        size = int(size)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    existing = {}
    if db is not None:
        try:
            row = (db.query(em.TransferContentManifest)
                   .filter_by(content_key=content_key).one_or_none())
            if row is not None and int(row.chunk_size) == CHUNK_SIZE:
                existing = unpack_chunk_hashes(
                    row.chunk_hashes, row.chunk_bitmap, row.chunk_count)
        except Exception:  # noqa: BLE001 — 读不到按没记过处理
            try:
                db.rollback()
            except Exception:
                pass
            existing = {}
    tracker = ChunkTracker(size, CHUNK_SIZE, existing)
    return ManifestRecorder(content_key, size, tracker)


# ---------------------------------------------------------------------------
# 交付状态落盘（fail-open：失败只记日志，返回 False）
# ---------------------------------------------------------------------------

def persist_delivery(db, *, job_id, content_key, dest_provider,
                     dest_binding_id, dest_ref, session, size,
                     state="active"):
    """把分片会话同步进 transfer_deliveries（init/part/failed 事件点调）。

    session 与 sidecar 同构；落盘时给每片补 chunk_from/chunk_to
    （part↔块映射入册）。按 job_id 一行（同一单的会话演进原地
    更新）。committed_* 按已成片前缀（片号 1..N 连续）折算。
    """
    if db is None or not job_id or not isinstance(session, dict):
        return False
    try:
        from backend.emby_server.transfer115 import _determine_partsize
        size = int(size)
        partsize = _determine_partsize(size) if size > 0 else 0
        enriched = []
        max_number = 0
        for part in session.get("parts") or []:
            try:
                number = int(part["PartNumber"])
                etag = str(part["ETag"])
            except Exception:
                continue
            chunk_from, chunk_to = part_chunk_span(
                number, partsize, size) if partsize else (0, -1)
            enriched.append({"PartNumber": number, "ETag": etag,
                             "chunk_from": chunk_from,
                             "chunk_to": chunk_to})
            max_number = max(max_number, number)
        payload = dict(session)
        payload["parts"] = enriched
        uploaded = min(max_number * partsize, size) if partsize else 0
        row = (db.query(em.TransferDelivery)
               .filter_by(job_id=int(job_id)).one_or_none())
        if row is None:
            row = em.TransferDelivery(job_id=int(job_id),
                                      content_key=content_key or "")
            db.add(row)
        row.content_key = content_key or ""
        row.dest_provider = dest_provider or ""
        row.dest_binding_id = dest_binding_id
        row.dest_ref = dest_ref
        row.size = size
        row.session = json.dumps(payload, ensure_ascii=False)
        row.committed_chunks = (uploaded // CHUNK_SIZE) if uploaded else 0
        row.committed_offset = uploaded
        row.state = state or "active"
        row.updated_at = datetime.now()
        db.commit()
        return True
    except Exception:  # noqa: BLE001 — 落盘失败绝不打挂转存
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("transfer_manifest persist_delivery failed "
                       "job=%s", job_id, exc_info=True)
        return False


def finish_delivery(db, job_id, state):
    """交付终态标记（completed / aborted）；无行/失败都静默。"""
    if db is None or not job_id:
        return False
    try:
        row = (db.query(em.TransferDelivery)
               .filter_by(job_id=int(job_id)).one_or_none())
        if row is None:
            return False
        row.state = state
        row.updated_at = datetime.now()
        db.commit()
        return True
    except Exception:  # noqa: BLE001 — fail-open
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("transfer_manifest finish_delivery failed "
                       "job=%s", job_id, exc_info=True)
        return False


def restore_delivery_session(db, job_id, content_key, size, dest_cid):
    """sidecar 缺失时凭 transfer_deliveries 行恢复分片会话。

    校验与 _load_multipart_session 同口径：job 行存在且 state
    为 active、content_key＋size＋目标目录（dest_ref）三项全
    对、会话关键字段齐全；parts 归一回 sidecar 形态
    （{PartNumber, ETag}，剥掉入册时补的块映射）。任一不符
    回 None（调用方按无会话走全新上传，与 sidecar 丢失的
    现行语义一致）。
    """
    if db is None or not job_id:
        return None
    try:
        row = (db.query(em.TransferDelivery)
               .filter_by(job_id=int(job_id)).one_or_none())
        if row is None or row.state != "active":
            return None
        if (row.content_key != content_key
                or int(row.size or 0) != int(size)
                or row.dest_ref != dest_cid):
            return None
        data = json.loads(row.session or "")
        session = data if isinstance(data, dict) else {}
        if not all(session.get(k) for k in
                   ("bucket", "object_key", "upload_id", "callback")):
            return None
        parts = []
        for part in session.get("parts") or []:
            try:
                parts.append({"PartNumber": int(part["PartNumber"]),
                              "ETag": str(part["ETag"])})
            except Exception:
                return None
        session["parts"] = parts
        return session
    except Exception:  # noqa: BLE001 — 恢复失败按无会话处理
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("transfer_manifest restore_delivery failed "
                       "job=%s", job_id, exc_info=True)
        return None
