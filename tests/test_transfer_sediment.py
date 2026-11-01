# -*- coding: utf-8 -*-
"""指纹沉淀 worker（转存等待压缩期三，2026-11-01 用户拍板全套推进）。

定位：把「全站第一次整遍读源算 SHA1」从用户点播关键路径挪到闲时：
后台按信号选片、流式读源算整文件 SHA1、登记进既有可信哈希登记
（MediaItem.content_hash，写入走 transfer_variant._persist_item_sha1，
与 #1538/#1559 秒传接力同一套登记/查询路径）。

本测试锁的口径：
- 需求档：pending/failed 的跨 Drive→115 单触及、指纹未登记的内容；
- 追剧档：近 14 天播放历史推导的在追剧同季后续集（SeriesFollow
  生产 0 行，信号只用播放历史）；
- 同一内容二次沉淀被登记命中跳过（零重读源）；
- 闲置门：队列有 due 单或有单在跑时整轮不沉淀；
- 日预算（读源字节）耗尽即停；
- 转化率熔断：老批次沉淀长期无人消费超阈值 → 自动停并落状态键；
- 热门/新入库档本期未实现：开关打开也不产生候选。

全部离线：Drive SA 客户端用桩替身（按 fid 供字节、记账读量）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_sediment as ts
from backend.emby_server import transfer_variant as tv
from backend.integrations import store


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    store.invalidate()
    # 源挂载：mount://3 → drive:src-drive-1（_source_drive_id 解析面）
    session.add(em.StorageMount(id=3, name="sediment-src",
                                path="drive:src-drive-1"))
    session.commit()
    yield session
    session.close()
    store.invalidate()


_counter = [9000]


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()
    store.invalidate()


def _item(db, payload_len=1024, **kw):
    _counter[0] += 1
    n = _counter[0]
    row = em.MediaItem(guid=f"sed-{n}", library_id=1, item_type="movie",
                       name=f"沉淀测试片{n}",
                       drive_file_id=f"fid-{n}",
                       file_path=f"mount://3/电影/sed{n}.mkv",
                       size=payload_len)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _cross_job(db, item, status="pending", trigger="playback",
               created_at=None):
    job = em.TransferJob(
        item_id=item.id, trigger=trigger, provider="drive",
        owner_scope="user", owner_ref="42",
        dest_drive_id="pan115:user-42", drive_id="src-drive-1",
        status=status, attempts=0)
    if created_at is not None:
        job.created_at = created_at
    db.add(job)
    db.commit()
    return job


class FakeDriveApi:
    """SA 池 Drive 客户端桩：按 fid 供字节并记账（调用次数＋字节数）。

    payload 可以是 bytes（小文件）或 (size, fill_byte) 元组（大文件
    按段现生成，不整份驻留内存）。
    """

    def __init__(self, payloads):
        self.payloads = payloads
        self.range_calls = 0
        self.bytes_served = 0

    def _size(self, fid):
        spec = self.payloads[fid]
        return spec[0] if isinstance(spec, tuple) else len(spec)

    def get_file(self, drive_id, fid):
        return {"size": self._size(fid)}

    def download_range(self, drive_id, fid, start, end):
        self.range_calls += 1
        spec = self.payloads[fid]
        if isinstance(spec, tuple):
            data = bytes([spec[1]]) * (end - start + 1)
        else:
            data = spec[start:end + 1]
        assert len(data) == end - start + 1
        self.bytes_served += len(data)
        return data


def _payload_sha1(data):
    return hashlib.sha1(data).hexdigest()


# ---------- 需求档：选片＋登记＋接力查询命中 ----------

def test_demand_candidate_sedimented_and_relay_query_hits(db):
    payload = bytes(range(256)) * 16  # 4096 字节
    item = _item(db, payload_len=len(payload))
    _cross_job(db, item, status="pending")
    api = FakeDriveApi({item.drive_file_id: payload})

    stats = ts.run_once(db, drive_api=api)

    assert stats["skipped_reason"] is None
    assert stats["registered"] == 1
    assert stats["bytes_read"] == len(payload)
    assert api.bytes_served == len(payload)
    db.refresh(item)
    # 登记进既有登记面，且 #1538 接力预检的查询口径直接命中。
    assert item.content_hash == _payload_sha1(payload)
    assert item.content_hash_algo == "sha1"
    assert tv._trusted_item_sha1(item, len(payload)) == _payload_sha1(payload)
    # 记账行落库：档位、字节、指纹可查。
    rec = db.query(em.TransferSedimentRecord).one()
    assert rec.item_id == item.id
    assert rec.tier == "demand"
    assert rec.bytes_read == len(payload)
    assert rec.sha1 == _payload_sha1(payload)


def test_failed_cross_job_also_demand_but_same_provider_not(db):
    payload = b"x" * 2048
    item = _item(db, payload_len=len(payload))
    _cross_job(db, item, status="failed")
    # 同 provider 单（dest 是 userdrive）不算跨 provider 需求。
    other = _item(db, payload_len=len(payload))
    job = em.TransferJob(item_id=other.id, trigger="playback",
                         provider="drive", owner_scope="user",
                         owner_ref="42", dest_drive_id="userdrive:42",
                         drive_id="src-drive-1", status="pending",
                         attempts=0)
    db.add(job)
    db.commit()
    api = FakeDriveApi({item.drive_file_id: payload,
                        other.drive_file_id: payload})

    candidates = ts.demand_candidates(db)
    assert [c.id for c in candidates] == [item.id]


def test_sediment_skips_item_that_got_registered_meanwhile(db):
    # 登记命中即跳过：零读源（转存主路已沉淀时沉淀不许重读）。
    payload = b"y" * 1024
    item = _item(db, payload_len=len(payload),
                 content_hash=_payload_sha1(payload),
                 content_hash_algo="sha1")
    _cross_job(db, item, status="pending")
    api = FakeDriveApi({item.drive_file_id: payload})

    stats = ts.run_once(db, drive_api=api)
    assert stats["registered"] == 0
    assert api.range_calls == 0
    assert api.bytes_served == 0


def test_second_round_does_not_reread_registered_content(db):
    payload = b"z" * 3000
    item = _item(db, payload_len=len(payload))
    _cross_job(db, item, status="pending")
    api = FakeDriveApi({item.drive_file_id: payload})

    first = ts.run_once(db, drive_api=api)
    assert first["registered"] == 1
    calls_after_first = api.range_calls
    second = ts.run_once(db, drive_api=api)
    assert second["registered"] == 0
    assert api.range_calls == calls_after_first  # 第二轮零重读
    assert db.query(em.TransferSedimentRecord).count() == 1


# ---------- 闲置门 ----------
# 口径（方案 §2.4）：追剧档是投机预热，只吃闲置产能——队列有 due
# 单或有单在跑时整档跳过；需求档本身就是在救队列里的单（沉淀完
# 直接喂那张单的探针/省哈希遍），豁免闲置门、只受预算/熔断约束。

def _watching_setup(db, payload=b"q" * 1024):
    """一组追剧候选：一部剧播过 E2、E3 未登记。返回 (目标集, api)。"""
    series = _series(db)
    eps = {n: _episode(db, series, 1, n, payload_len=len(payload))
           for n in range(1, 4)}
    db.add(em.PlayStartEvent(user_id=7, item_id=eps[2].id,
                             provider="mount", result="ok",
                             created_at=datetime.now()
                             - timedelta(hours=2)))
    db.commit()
    api = FakeDriveApi({eps[3].drive_file_id: payload})
    return eps[3], api


def test_idle_gate_blocks_watching_when_due_job_pending(db):
    target, api = _watching_setup(db)
    # 一张已到期的同 provider 单在排队 → 队列非空，追剧档跳过。
    other = _item(db, payload_len=1024)
    job = em.TransferJob(item_id=other.id, trigger="playback",
                         provider="drive", owner_scope="user",
                         owner_ref="7", dest_drive_id="userdrive:7",
                         drive_id="src-drive-1", status="pending",
                         attempts=0)
    db.add(job)
    db.commit()

    stats = ts.run_once(db, drive_api=api)
    assert stats["skipped_reason"] == "queue_busy"
    assert stats["registered"] == 0
    assert api.range_calls == 0


def test_idle_gate_blocks_watching_when_job_running(db):
    target, api = _watching_setup(db)
    running_item = _item(db, payload_len=1024)
    job = em.TransferJob(item_id=running_item.id, trigger="playback",
                         provider="drive", owner_scope="user",
                         owner_ref="7", dest_drive_id="userdrive:7",
                         drive_id="src-drive-1", status="running",
                         attempts=1)
    db.add(job)
    db.commit()

    stats = ts.run_once(db, drive_api=api)
    assert stats["skipped_reason"] == "queue_busy"
    assert stats["registered"] == 0
    assert api.range_calls == 0


def test_demand_tier_exempt_from_idle_gate(db):
    # 需求档豁免：队列里正排着单（含它自己的需求单）时照样沉淀，
    # 沉淀完这张单被认领时即可命中接力/跳过哈希遍。
    payload = b"q" * 1024
    item = _item(db, payload_len=len(payload))
    _cross_job(db, item, status="pending")
    other = _item(db, payload_len=len(payload))
    job = em.TransferJob(item_id=other.id, trigger="playback",
                         provider="drive", owner_scope="user",
                         owner_ref="7", dest_drive_id="userdrive:7",
                         drive_id="src-drive-1", status="pending",
                         attempts=0)
    db.add(job)
    db.commit()
    api = FakeDriveApi({item.drive_file_id: payload,
                        other.drive_file_id: payload})

    stats = ts.run_once(db, drive_api=api)
    assert stats["registered"] == 1
    assert stats["by_tier"].get("demand", 0) == 1


# ---------- 日预算 ----------

def test_daily_budget_seed_blocks_round(db):
    # 今日已沉淀字节逼近预算 → 本轮一只都不读。
    _set_config(db, "transfer_sediment_daily_budget_gib", 1)
    gib = 1024 ** 3
    db.add(em.TransferSedimentRecord(
        item_id=1, drive_file_id="fid-old", sha1="a" * 40,
        tier="demand", bytes_read=gib - 100,
        created_at=datetime.now()))
    db.commit()
    payload = b"b" * 4096
    item = _item(db, payload_len=len(payload))
    _cross_job(db, item, status="pending")
    api = FakeDriveApi({item.drive_file_id: payload})

    stats = ts.run_once(db, drive_api=api)
    assert stats["registered"] == 0
    assert stats["budget_exhausted"] is True
    assert api.range_calls == 0


def test_daily_budget_stops_mid_round(db):
    # 预算 1 GiB：第一只 600 MiB 落账后，第二只会超预算 → 停。
    _set_config(db, "transfer_sediment_daily_budget_gib", 1)
    size = 600 * 1024 * 1024
    first = _item(db, payload_len=size)
    second = _item(db, payload_len=size)
    _cross_job(db, first, status="pending")
    _cross_job(db, second, status="pending")
    api = FakeDriveApi({first.drive_file_id: (size, 0x41),
                        second.drive_file_id: (size, 0x42)})

    stats = ts.run_once(db, drive_api=api)
    assert stats["registered"] == 1
    assert stats["budget_exhausted"] is True
    assert api.bytes_served == size  # 只读了第一只


# ---------- 转化率熔断 ----------

def _seed_old_records(db, count, days_ago=31, consumed=0):
    now = datetime.now()
    for i in range(count):
        rec = em.TransferSedimentRecord(
            item_id=5000 + i, drive_file_id=f"fid-old-{i}",
            sha1="c" * 40, tier="demand", bytes_read=1000,
            created_at=now - timedelta(days=days_ago))
        if i < consumed:
            rec.consumed_at = now - timedelta(days=days_ago - 1)
        db.add(rec)
    db.commit()


def test_fuse_trips_on_unconsumed_old_cohort(db):
    _seed_old_records(db, 20, consumed=0)
    assert ts.evaluate_fuse(db) is True
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key
                   == "transfer_sediment_fused").first())
    assert row is not None and row.value == "true"
    # 熔断后整轮不沉淀。
    payload = b"f" * 1024
    item = _item(db, payload_len=len(payload))
    _cross_job(db, item, status="pending")
    api = FakeDriveApi({item.drive_file_id: payload})
    stats = ts.run_once(db, drive_api=api)
    assert stats["skipped_reason"] == "fused"
    assert api.range_calls == 0


def test_fuse_stays_off_when_cohort_mostly_consumed(db):
    _seed_old_records(db, 20, consumed=19)
    assert ts.evaluate_fuse(db) is False


def test_consumption_marked_by_later_job(db):
    payload = b"c" * 1024
    item = _item(db, payload_len=len(payload))
    _cross_job(db, item, status="pending")
    api = FakeDriveApi({item.drive_file_id: payload})
    ts.run_once(db, drive_api=api)
    rec = db.query(em.TransferSedimentRecord).one()
    assert rec.consumed_at is None
    # 沉淀之后同条目又来一张转存单（别的用户）→ 视为被消费。
    job = em.TransferJob(item_id=item.id, trigger="playback",
                         provider="drive", owner_scope="user",
                         owner_ref="99", dest_drive_id="pan115:user-99",
                         drive_id="src-drive-1", status="pending",
                         attempts=0,
                         created_at=datetime.now() + timedelta(minutes=5))
    db.add(job)
    db.commit()
    marked = ts.refresh_consumption(db)
    assert marked == 1
    db.refresh(rec)
    assert rec.consumed_at is not None


# ---------- 追剧档 ----------

def _episode(db, series, season, number, payload_len=1024, **kw):
    _counter[0] += 1
    n = _counter[0]
    row = em.MediaItem(guid=f"sed-ep-{n}", library_id=1,
                       item_type="episode", name=f"剧集 S{season}E{number}",
                       series_id=series.id, season_number=season,
                       episode_number=number,
                       drive_file_id=f"fid-ep-{n}",
                       file_path=f"mount://3/剧集/ep{n}.mkv",
                       size=payload_len)
    for key, value in kw.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


def _series(db):
    _counter[0] += 1
    row = em.MediaItem(guid=f"sed-series-{_counter[0]}", library_id=1,
                       item_type="series", name="在追的剧", size=0)
    db.add(row)
    db.commit()
    return row


def test_watching_candidates_are_next_episodes_after_latest_played(db):
    series = _series(db)
    eps = {n: _episode(db, series, 1, n) for n in range(1, 10)}
    # 近 14 天播过 E5 → 后续 E6/E7/E8 进候选，E9 不进（每剧取后 3 集）。
    db.add(em.PlayStartEvent(user_id=7, item_id=eps[5].id,
                             provider="mount", result="ok",
                             created_at=datetime.now() - timedelta(days=1)))
    db.commit()

    candidates = ts.watching_candidates(db)
    assert [c.id for c in candidates] == [eps[6].id, eps[7].id, eps[8].id]


def test_watching_ignores_stale_history_and_registered(db):
    series = _series(db)
    eps = {n: _episode(db, series, 1, n) for n in range(1, 8)}
    payload = b"w" * 1024
    # E6 已登记 → 不进候选；播出记录是 20 天前 → 整剧不进候选。
    eps[6].content_hash = _payload_sha1(payload)
    eps[6].content_hash_algo = "sha1"
    db.commit()
    db.add(em.PlayStartEvent(user_id=7, item_id=eps[5].id,
                             provider="mount", result="ok",
                             created_at=datetime.now() - timedelta(days=20)))
    db.commit()
    assert ts.watching_candidates(db) == []


def test_watching_tier_runs_when_demand_tier_off(db):
    _set_config(db, "transfer_sediment_tier_demand_enabled", "false")
    series = _series(db)
    eps = {n: _episode(db, series, 1, n) for n in range(1, 5)}
    payload = b"v" * 1024
    db.add(em.PlayStartEvent(user_id=7, item_id=eps[2].id,
                             provider="mount", result="ok",
                             created_at=datetime.now() - timedelta(hours=3)))
    db.commit()
    api = FakeDriveApi({eps[3].drive_file_id: payload,
                        eps[4].drive_file_id: payload})

    stats = ts.run_once(db, drive_api=api)
    assert stats["by_tier"].get("watching", 0) == 2
    assert stats["registered"] == 2


# ---------- 档位与总开关 ----------

def test_hot_and_new_tiers_produce_no_candidates_this_phase(db):
    # 热门/新入库档本期只有开关位：打开也不选片（无任何信号也
    # 不许凭空读源）。
    _set_config(db, "transfer_sediment_tier_hot_enabled", "true")
    _set_config(db, "transfer_sediment_tier_new_enabled", "true")
    payload = b"h" * 1024
    _item(db, payload_len=len(payload))  # 无需求、无播放历史
    api = FakeDriveApi({})
    stats = ts.run_once(db, drive_api=api)
    assert stats["registered"] == 0
    assert api.range_calls == 0


def test_master_switch_off_is_noop(db):
    _set_config(db, "transfer_sediment_enabled", "false")
    payload = b"m" * 1024
    item = _item(db, payload_len=len(payload))
    _cross_job(db, item, status="pending")
    api = FakeDriveApi({item.drive_file_id: payload})
    stats = ts.run_once(db, drive_api=api)
    assert stats["skipped_reason"] == "disabled"
    assert api.range_calls == 0


def test_config_defaults(db):
    assert ts.sediment_enabled(db) is True
    assert ts.tier_enabled(db, "demand") is True
    assert ts.tier_enabled(db, "watching") is True
    assert ts.tier_enabled(db, "hot") is False
    assert ts.tier_enabled(db, "new") is False
    assert ts.daily_budget_bytes(db) == 200 * 1024 ** 3
