"""规则包：命名清洗词表的版本化（FMBY 清单 #18）。

FMBY 形态照搬三点：每个识别任务钉规则包版本 + 内容哈希；服主
覆盖分 custom_terms / disabled_default_terms / protected_terms 三层；
规则升级后可按新版本重放识别（本模块的 ``replay_pack``）。

本包 v1 的边界（说清楚、不夸大）：
- 包管的只是命名清洗里的**质量标签词表**——即扫描器 ``_tidy_name``
  原来内联的那串正则交替项（分辨率 / 编码 / 来源 / 语言等），原样
  搬进 ``DEFAULT_STRIP_TERMS``，顺序一字不动。分隔符归一、括号剥离、
  平台标签、重复标记等其余清洗手法仍是扫描器机制，尚未收进包里，
  后续版本再吸纳，不在本批假装“整套清洗已版本化”。
- 覆盖层来源是 env（与本仓所有开关同一口径：文件配置为准，改了
  重启生效）。三键见 env.example；非法 JSON 当空处理并记 warning，
  绝不因配置写错拖垮扫描。
- 哈希分两层：``content_hash`` 只算包自身内容（名 + 版本 + 默认词表，
  跨服可比）；``effective_hash`` 算实际跑的规则（名 + 版本 + 生效词表
  + 保护词），识别记录钉的是后者——服主改了覆盖层，存量即判定为
  “旧规则产物”，重放才能认出来。两者都是 sha256，十六进制 64 位。

重放语义（保守、不删标题、不碰人工定案）：
- 只认已钉哈希且与现行生效哈希不同的记录；NULL（包上线前的老记录）
  不动——首部署只打戳、不重写存量，机制从新决策开始生效。
- 只对已存 ``scan_title`` 再跑一遍现行词表：新词进了词表才会多剥掉
  词；词表删词不会把已剥的词“长回来”（剥掉的信息已丢，复原只能
  重 parse 文件名，那是另一项）。剥完为空的标题跳过，永不写空标题。
- 人工覆盖受保护：审查队列里 status=bound 的条目（管理员亲手绑定
  过）一律跳过，重放不动它。
- 重放只改标题 + 把条目放回 pending 待识别，不直接改身份列；身份
  重定仍走识别段，证据与政策门照旧。重放默认 dry_run，只报数不写
  库；调用方显式传 dry_run=False 才写，且本函数只 add / 改字段、
  不 commit（与 apply_identity、_upsert_stage_record 同口径）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PACK_NAME = "naming-cleanup"
PACK_VERSION = "v1"

ENV_CUSTOM_TERMS = "RULE_PACK_CUSTOM_TERMS"
ENV_DISABLED_DEFAULT_TERMS = "RULE_PACK_DISABLED_DEFAULT_TERMS"
ENV_PROTECTED_TERMS = "RULE_PACK_PROTECTED_TERMS"

# 默认词表：与 scanner._tidy_name 原内联正则的交替项逐字一致、顺序
# 一致（原正则 ``\\b(... )\\b``）。改这里即改包内容，content_hash
# 随之变，存量重放据此识别旧规则产物。词条是正则片段（与原正则
# 同语法），不是纯字面词——如 ``h\\.?264``、``web[\\s\\-]?dl``。
DEFAULT_STRIP_TERMS = (
    "2160p", "1440p", "1080p", "1080i", "720p", "480p", r"\d{3,4}[pi]",
    "4k", "8k", "uhd", r"hdr10\+?", "hdr", "dolby", "dv", "hevc",
    r"h\.?264", "x264", r"h\.?265", "x265",
    "aac", "ac3", "eac3", "dts", "flac", "truehd", "atmos",
    r"web[\s\-]?dl", "webrip", "web", "remux", r"blu[\s\-]?ray", "bluray",
    "bdrip", "brrip", "hdtv", "dvdrip", "diy", "repack", "proper",
    "multi", "internal", "chs&eng", "chs", "cht", "eng", "gb", "big5",
)

# 保护词占位符：私有区字符（U+E000 起）不是 \\w 词字符，任何
# \\b 界定的词条都不可能在占位符里命中，也不会跨占位符拼出词条。
_PLACEHOLDER_BASE = 0xE000


def _read_term_list(env_key: str) -> tuple:
    """读 env 里的词表：JSON 字符串数组；非法时记 warning 返回空。"""
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("规则包 env %s 不是合法 JSON，已忽略：%r", env_key, raw[:200])
        return ()
    if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
        logger.warning("规则包 env %s 须为字符串数组，已忽略：%r", env_key, raw[:200])
        return ()
    return tuple(t for t in parsed if t)


def _canonical_hash(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RulePack:
    """一条命名清洗规则包：默认词表 + 服主三层覆盖。"""

    name: str = PACK_NAME
    version: str = PACK_VERSION
    default_terms: tuple = DEFAULT_STRIP_TERMS
    custom_terms: tuple = ()
    disabled_default_terms: tuple = ()
    protected_terms: tuple = ()

    @property
    def effective_terms(self) -> tuple:
        """实际跑的词表：默认词减被禁用的，再追加服主自加词。

        自定义词排在默认词之后——重叠时默认词优先；保护词不走词表，
        在 strip 里先行占位，优先级最高。
        """
        disabled = {t.lower() for t in self.disabled_default_terms}
        kept = tuple(t for t in self.default_terms if t.lower() not in disabled)
        return kept + tuple(self.custom_terms)

    @property
    def content_hash(self) -> str:
        """包自身内容哈希：名 + 版本 + 默认词表（跨服可比）。"""
        return _canonical_hash({
            "name": self.name,
            "version": self.version,
            "default_terms": list(self.default_terms),
        })

    @property
    def effective_hash(self) -> str:
        """实际生效规则哈希：名 + 版本 + 生效词表 + 保护词。

        识别记录钉的是它：服主改覆盖层即变，存量随即判定为旧规则产物。
        """
        return _canonical_hash({
            "name": self.name,
            "version": self.version,
            "effective_terms": list(self.effective_terms),
            "protected_terms": list(self.protected_terms),
        })

    def strip(self, name: str) -> str:
        """按生效词表剥掉质量标签词，返回替换后的串（不做空白归一，
        调用方 _tidy_name 后续自会归一；与原内联 re.sub 同语义）。

        保护词先行占位：整词（大小写不敏感）命中即换成私有区占位符，
        剥完再原样还回——保护词哪怕撞上默认词 / 自定义词也不被剥。
        """
        if not name:
            return name
        working = name
        # 保护词占位：每处命中一个独立占位符，还回时保留原始大小写。
        restores: list = []
        for term in self.protected_terms:
            if not term:
                continue
            pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)

            def _protect(match, _restores=restores):
                key = "\x00" + chr(_PLACEHOLDER_BASE + len(_restores)) + "\x00"
                _restores.append((key, match.group(0)))
                return key

            working = pattern.sub(_protect, working)
        if self.effective_terms:
            working = re.sub(
                r"\b(?:" + "|".join(self.effective_terms) + r")\b",
                " ",
                working,
                flags=re.IGNORECASE,
            )
        for key, original in restores:
            working = working.replace(key, original)
        return working


def default_pack() -> RulePack:
    """当前生效包：默认词表 + env 三层覆盖（每次调用重读 env，开销
    只是三次 os.getenv，扫描热路径可接受；改 env 重启即生效）。"""
    return RulePack(
        custom_terms=_read_term_list(ENV_CUSTOM_TERMS),
        disabled_default_terms=_read_term_list(ENV_DISABLED_DEFAULT_TERMS),
        protected_terms=_read_term_list(ENV_PROTECTED_TERMS),
    )


def find_stale_records(db, limit: int = 100):
    """找出存量里“旧规则产物”的识别记录：已钉哈希且不等于现行生效
    哈希。NULL（包上线前的老记录）不在内——首部署不重写存量。

    返回 (pack, records)；records 按 id 升序、至多 limit 条。
    """
    from backend.emby_server import models as _m

    pack = default_pack()
    records = (
        db.query(_m.EnrichStageRecord)
        .filter(
            _m.EnrichStageRecord.rule_pack_hash.isnot(None),
            _m.EnrichStageRecord.rule_pack_hash != pack.effective_hash,
        )
        .order_by(_m.EnrichStageRecord.id)
        .limit(limit)
        .all()
    )
    return pack, records


def replay_pack(db, limit: int = 100, dry_run: bool = True) -> dict:
    """按现行规则包重放存量标题（见模块 docstring 的重放语义）。

    只改标题 + 放回 pending 待识别，不碰身份列；不 commit，调用方
    负责提交。返回报表 dict，dry_run 时 requeued 恒为 0。
    """
    from backend.emby_server import models as _m

    pack, records = find_stale_records(db, limit=limit)
    report = {
        "pack_version": pack.version,
        "pack_hash": pack.effective_hash,
        "checked": len(records),
        "changed": 0,
        "requeued": 0,
        "skipped_protected": 0,
        "skipped_empty": 0,
        "missing": 0,
        "dry_run": dry_run,
    }
    for record in records:
        item = (
            db.query(_m.MediaItem)
            .filter(_m.MediaItem.id == record.item_id)
            .first()
        )
        if item is None:
            report["missing"] += 1
            continue
        # 人工覆盖受保护：审查队列里管理员亲手绑定过的条目不动。
        bound = (
            db.query(_m.IdentifyReviewTask)
            .filter(
                _m.IdentifyReviewTask.item_id == item.id,
                _m.IdentifyReviewTask.status == "bound",
            )
            .first()
        )
        if bound is not None:
            report["skipped_protected"] += 1
            continue
        old_title = item.scan_title or ""
        if not old_title:
            report["skipped_empty"] += 1
            continue
        # 与 _tidy_name 尾段同口径：空白归一 + 剥首尾分隔符。
        new_title = re.sub(r"\s+", " ", pack.strip(old_title)).strip(" .-_")
        if not new_title or new_title == old_title:
            if not new_title:
                report["skipped_empty"] += 1
            continue
        report["changed"] += 1
        if dry_run:
            continue
        item.scan_title = new_title
        item.enrich_status = "pending"
        item.enrich_claimed_at = None
        item.enrich_claim_token = None
        item.enrich_next_retry_at = None
        report["requeued"] += 1
    return report
