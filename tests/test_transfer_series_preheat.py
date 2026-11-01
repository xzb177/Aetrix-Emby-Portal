# -*- coding: utf-8 -*-
"""转存等待压缩 期四：剧集预热（2026-11-01 方案
reports/transfer-wait-compression-design-20261101.md §3）。

口径（任务书拍板版）：
- 用户播完一集（Stopped finished）后，给同季后续 K 集（默认 3）
  建预热：先只探针——复用期二 probe_only 通道与可探针判据，
  命中即落 ready 变体；未命中不自动搬字节，探针单不留墓碑行
  （删行，否则 24h 节流桶会挡住用户随后对该集的正常点播建单）。
- 强信号（同用户 7 天内已播同剧 ≥2 集，或 SeriesFollow 有效行，
  或 14 天内播过同剧另一集）才建正式搬字节单；搬字节预热
  独立日帽 2（新 SystemConfig 键 transfer_series_preheat_daily_max），
  与详情页 prefetch 每人每天帽 5 分开计数、互不干扰。
- 探针单（trigger=series_preheat）工人两道认领一律跳过：
  搬字节道不认领（否则未命中也会被搬走）、探针清扫不认领
  （预热探针在触发后台线程里 inline 跑完，不走清扫）。
- 无绑定/绑定失效用户不建单并响亮记日志；默认开关开。
- 不新增 env 键。

全部离线：SQLite :memory:，115/SA 一律桩替身。
先红后绿：本文件每个用例在实现前均为红（函数/键不存在）。
"""
import hashlib
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import transfer115
from backend.emby_server import user_bindings as ub
from backend.integrations import store

_TV_LOGGER = "backend.emby_server.transfer_variant"

_PAYLOAD = bytes(range(256)) * 4  # 1024 字节
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()


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
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_state():
    store.invalidate()
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()
    yield
    store.invalidate()
    tv._paused_until.clear()
    tv._WAKE_EVENT.clear()


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()
    store.invalidate()


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


def _mount(db, mount_id=3):
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type="drive", path="drive:shared1")
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, provider="pan115", status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider=provider,
                                account_label="test",
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


def _series(db):
    row = em.MediaItem(guid="series-preheat", library_id=1,
                       item_type="series", name="预热测试剧")
    db.add(row)
    db.commit()
    return row


def _episode(db, series_id, ep, sha1=_PAYLOAD_SHA1, season=1):
    row = em.MediaItem(
        guid=f"preheat-s{series_id}-e{ep}", library_id=1,
        item_type="episode", name=f"第{ep}集", series_id=series_id,
        season_number=season, episode_number=ep,
        drive_file_id=f"fid-ep{ep}",
        file_path=f"mount://3/剧/ep{ep}.mkv", size=len(_PAYLOAD))
    if sha1:
        row.content_hash = sha1
        row.content_hash_algo = "sha1"
    db.add(row)
    db.commit()
    return row


def _umd(db, user_id, item_id, days_ago=0):
    row = em.UserMediaData(user_id=user_id, item_id=item_id,
                           last_played_at=datetime.now()
                           - timedelta(days=days_ago))
    db.add(row)
    db.commit()
    return row


def _follow(db, user_id, series_id):
    row = em.SeriesFollow(user_id=user_id, series_item_id=series_id,
                          status="active")
    db.add(row)
    db.commit()
    return row


class _FakeSA:
    def __init__(self, payload=_PAYLOAD):
        self.payload = payload
        self.download_calls = 0

    def get_file(self, drive_id, fid):
        return {"id": fid, "size": len(self.payload)}

    def download_range(self, drive_id, fid, start, end):
        self.download_calls += 1
        return self.payload[start:end + 1]


class _FakeUser115:
    def __init__(self, rapid="hit"):
        self.dirs = {
            "0": [{"name": "aetrix-transfer", "is_dir": True, "cid": "900"}],
            "900": [],
        }
        self.rapid_behavior = rapid
        self.rapid_calls = []
        self.simple_calls = []

    def list_dir(self, cid="0", limit=200):
        return list(self.dirs.get(str(cid), []))

    def mkdir(self, parent_cid, name):
        self.dirs.setdefault("0", []).append(
            {"name": name, "is_dir": True, "cid": "900"})
        return {}

    def rapid_upload(self, filename, filesize, file_sha1, pid,
                     range_reader=None):
        self.rapid_calls.append((filename, filesize, file_sha1, pid))
        if self.rapid_behavior == "error":
            raise transfer115.Pan115Error("协议炸了")
        if self.rapid_behavior == "auth_error":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        if self.rapid_behavior == "miss":
            return {"outcome": "miss", "fid": "", "pickcode": "", "raw": {}}
        self.dirs["900"] = [{"name": filename, "is_dir": False,
                             "fid": "rapidfid", "pickcode": "pcrapid",
                             "size": filesize, "sha1": file_sha1}]
        return {"outcome": "hit", "fid": "rapidfid", "pickcode": "pcrapid",
                "raw": {"status": 2}}

    def simple_upload(self, fileobj, filename, pid):
        data = fileobj.read()
        self.simple_calls.append(data)
        sha1 = hashlib.sha1(data).hexdigest()
        self.dirs["900"] = [{"name": filename, "is_dir": False,
                             "fid": "upfid", "pickcode": "pcup",
                             "size": len(data), "sha1": sha1}]
        return {"fid": "upfid", "pickcode": "pcup", "sha1": sha1,
                "raw": {"data": {"file_id": "upfid", "sha1": sha1}}}


def _patch_user115(monkeypatch, fake):
    monkeypatch.setattr(
        ub, "binding_115_cookie",
        lambda db_arg, user_id, binding_id: "CK-1")
    monkeypatch.setattr(tv, "_make_pan115_client", lambda cookie: fake)


def _jobs(db, user_id=7):
    return (db.query(em.TransferJob)
            .filter(em.TransferJob.owner_scope == "user",
                    em.TransferJob.owner_ref == str(user_id))
            .order_by(em.TransferJob.id).all())


def _preheat(db, monkeypatch, anchor, fake, user_id=7):
    _enable(monkeypatch)
    _patch_user115(monkeypatch, fake)
    return tv.maybe_series_preheat_transfer(
        db, anchor, SimpleNamespace(id=user_id),
        drive_api_factory=lambda: _FakeSA())


# ---------- 配置键：默认/钳制/schema/payload 往返（四齐） ----------

def test_series_preheat_keys_default_and_clamp(db):
    assert tv.series_preheat_enabled(db) is True
    assert tv.series_preheat_daily_max(db) == 2
    assert tv.series_preheat_next_count(db) == 3
    _set_config(db, "transfer_series_preheat_daily_max", 5)
    assert tv.series_preheat_daily_max(db) == 5
    _set_config(db, "transfer_series_preheat_daily_max", 99)
    assert tv.series_preheat_daily_max(db) == 16
    _set_config(db, "transfer_series_preheat_daily_max", "junk")
    assert tv.series_preheat_daily_max(db) == 2
    _set_config(db, "transfer_series_preheat_next_count", 1)
    assert tv.series_preheat_next_count(db) == 1
    _set_config(db, "transfer_series_preheat_next_count", 99)
    assert tv.series_preheat_next_count(db) == 10
    _set_config(db, "transfer_series_preheat_enabled", "false")
    assert tv.series_preheat_enabled(db) is False


def test_series_preheat_keys_schema_and_payload(db):
    from backend.config_schema import TRANSFER_CONFIG_SCHEMA
    assert TRANSFER_CONFIG_SCHEMA["transfer_series_preheat_enabled"] == (
        "bool",)
    assert TRANSFER_CONFIG_SCHEMA["transfer_series_preheat_daily_max"] == (
        "int", 1, 16)
    assert TRANSFER_CONFIG_SCHEMA["transfer_series_preheat_next_count"] == (
        "int", 1, 10)
    payload = tv.transfer_config_payload(db)
    assert payload["transfer_series_preheat_enabled"] is True
    assert payload["transfer_series_preheat_daily_max"] == 2
    assert payload["transfer_series_preheat_next_count"] == 3
    out = tv.write_transfer_config(
        db, transfer_series_preheat_enabled=False,
        transfer_series_preheat_daily_max=1,
        transfer_series_preheat_next_count=5)
    assert out["transfer_series_preheat_enabled"] is False
    assert out["transfer_series_preheat_daily_max"] == 1
    assert out["transfer_series_preheat_next_count"] == 5


def test_series_preheat_adds_no_env_key():
    import pathlib
    text = pathlib.Path("env.example").read_text(encoding="utf-8")
    assert "transfer_series_preheat" not in text


# ---------- 播完触发：探针命中落 ready ----------

def test_finished_anchor_probes_next_episodes_hit_ready(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    ep2 = _episode(db, series.id, 2)
    ep3 = _episode(db, series.id, 3)
    ep4 = _episode(db, series.id, 4)
    fake = _FakeUser115(rapid="hit")
    result = _preheat(db, monkeypatch, anchor, fake)
    assert result["triggered"] is True
    assert len(fake.rapid_calls) == 3  # 默认 K=3：后续 3 集各探一次
    jobs = _jobs(db)
    assert all(j.status == "done" for j in jobs)
    assert {j.item_id for j in jobs} == {ep2.id, ep3.id, ep4.id}
    variants = (db.query(em.TransferVariant)
                .filter(em.TransferVariant.owner_ref == "7",
                        em.TransferVariant.status == "ready").all())
    assert {v.item_id for v in variants} == {ep2.id, ep3.id, ep4.id}
    assert fake.simple_calls == []  # 探针绝不搬字节


def test_next_count_default_three(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    eps = [_episode(db, series.id, n) for n in (2, 3, 4, 5)]
    fake = _FakeUser115(rapid="hit")
    result = _preheat(db, monkeypatch, anchor, fake)
    assert result["triggered"] is True
    jobs = _jobs(db)
    # 默认 K=3：只取后续 3 集（ep2/3/4），ep5 不动
    assert {j.item_id for j in jobs} == {e.id for e in eps[:3]}


# ---------- 探针未命中：删单、无墓碑、可重建 ----------

def test_probe_miss_deletes_probe_job_no_residue(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    ep2 = _episode(db, series.id, 2)
    fake = _FakeUser115(rapid="miss")
    result = _preheat(db, monkeypatch, anchor, fake)
    assert result["triggered"] is True
    assert _jobs(db) == []  # 未命中不留 pending/failed 墓碑行
    assert fake.simple_calls == []
    # 无墓碑的证明：随后对同一集建正式单不被 24h 节流挡住
    job = tv.create_cross_provider_job(db, ep2, 7, "drive",
                                       trigger="playback",
                                       dest_provider="pan115")
    assert job.id is not None


def test_probe_error_deletes_probe_job_no_residue(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    _episode(db, series.id, 2)
    fake = _FakeUser115(rapid="error")
    _preheat(db, monkeypatch, anchor, fake)
    assert _jobs(db) == []


# ---------- 强信号才升搬字节：日帽 2 ----------

def _strong_setup(db, anchor, series):
    # 14 天内播过同剧 2 集（锚 + 另一集）= 继续追剧的强信号
    _umd(db, 7, anchor.id, days_ago=0)
    other = _episode(db, series.id, 9, season=2)
    _umd(db, 7, other.id, days_ago=1)
    return other


def test_strong_signal_creates_byte_jobs_capped_at_two(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    _strong_setup(db, anchor, series)
    eps = [_episode(db, series.id, n) for n in (2, 3, 4)]
    fake = _FakeUser115(rapid="miss")
    result = _preheat(db, monkeypatch, anchor, fake)
    jobs = _jobs(db)
    byte_jobs = [j for j in jobs
                 if j.trigger == "series_preheat_byte"]
    assert len(byte_jobs) == 2  # 日帽 2 拦住第 3 单
    assert {j.item_id for j in byte_jobs} == {e.id for e in eps[:2]}
    assert all(j.status == "pending" for j in byte_jobs)
    # 第 3 集没拿到搬字节帽时仍可走探针（可探针）：探针未命中删单
    assert result["triggered"] is True
    assert fake.simple_calls == []


def test_byte_cap_separate_from_prefetch_cap(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    _strong_setup(db, anchor, series)
    # 先把详情页 prefetch 帽 5 打满（与预热分开计数）
    for n in range(5):
        item = _episode(db, series.id, 20 + n, season=3)
        db.add(em.TransferJob(item_id=item.id, trigger="prefetch",
                              drive_id="shared1", provider="drive",
                              owner_scope="user", owner_ref="7",
                              dest_drive_id="pan115:user-7",
                              status="pending"))
    db.commit()
    eps = [_episode(db, series.id, n) for n in (2, 3)]
    fake = _FakeUser115(rapid="miss")
    _preheat(db, monkeypatch, anchor, fake)
    byte_jobs = [j for j in _jobs(db)
                 if j.trigger == "series_preheat_byte"]
    assert len(byte_jobs) == 2  # prefetch 帽满了也不影响预热帽
    assert {j.item_id for j in byte_jobs} == {e.id for e in eps}


def test_weak_signal_never_creates_byte_job(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    _umd(db, 7, anchor.id, days_ago=0)  # 只播过锚这一集 = 弱信号
    eps = [_episode(db, series.id, n) for n in (2, 3)]
    fake = _FakeUser115(rapid="miss")
    _preheat(db, monkeypatch, anchor, fake)
    assert [j for j in _jobs(db)
            if j.trigger == "series_preheat_byte"] == []
    # 弱信号 + 可探针：只探针（未命中删单），不建搬字节单
    assert _jobs(db) == []
    assert len(fake.rapid_calls) == 2


def test_follow_row_is_strong_signal(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    _follow(db, 7, series.id)  # 显式追剧订阅 = 强信号（无播放史也算）
    eps = [_episode(db, series.id, n) for n in (2, 3)]
    fake = _FakeUser115(rapid="miss")
    _preheat(db, monkeypatch, anchor, fake)
    byte_jobs = [j for j in _jobs(db)
                 if j.trigger == "series_preheat_byte"]
    assert len(byte_jobs) == 2
    assert {j.item_id for j in byte_jobs} == {e.id for e in eps}


def test_unprobeable_candidate_gets_no_job_on_weak_signal(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    _episode(db, series.id, 2, sha1=None)  # 无可信 sha1 = 不可探针
    fake = _FakeUser115(rapid="hit")
    result = _preheat(db, monkeypatch, anchor, fake)
    assert _jobs(db) == []
    assert fake.rapid_calls == []
    assert result["triggered"] is False


# ---------- 无绑定不建单、响亮日志 ----------

def test_no_binding_creates_nothing_and_logs_loud(db, monkeypatch, caplog):
    _mount(db)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    _episode(db, series.id, 2)
    fake = _FakeUser115(rapid="hit")
    with caplog.at_level("WARNING", logger=_TV_LOGGER):
        result = _preheat(db, monkeypatch, anchor, fake)
    assert result["triggered"] is False
    assert result["reason"] == "no_binding"
    assert _jobs(db) == []
    assert fake.rapid_calls == []
    assert any("no_binding" in (r.message or "") or "无绑定" in (r.message or "")
               for r in caplog.records)


def test_preheat_off_creates_nothing(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    _episode(db, series.id, 2)
    _set_config(db, "transfer_series_preheat_enabled", "false")
    fake = _FakeUser115(rapid="hit")
    result = _preheat(db, monkeypatch, anchor, fake)
    assert result["triggered"] is False
    assert _jobs(db) == []


# ---------- 工人两道认领跳过探针单 ----------

def test_worker_lanes_skip_probe_only_jobs(db, monkeypatch, db_factory):
    _enable(monkeypatch)
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    _episode(db, series.id, 1)
    ep2 = _episode(db, series.id, 2)
    db.add(em.TransferJob(item_id=ep2.id, trigger="series_preheat",
                          drive_id="shared1", provider="drive",
                          owner_scope="user", owner_ref="7",
                          dest_drive_id="pan115:user-7",
                          status="pending"))
    db.commit()
    assert tv.claim_due_jobs(db) == []
    assert tv.claim_probe_jobs(db) == []
    row = (db.query(em.TransferJob)
           .filter(em.TransferJob.trigger == "series_preheat").first())
    assert row.status == "pending"  # 两道都不碰它


# ---------- 同内容去重：在途/已就绪不重复建单 ----------

def test_inflight_or_ready_candidate_skipped(db, monkeypatch):
    _mount(db)
    _binding(db, 7)
    series = _series(db)
    anchor = _episode(db, series.id, 1)
    ep2 = _episode(db, series.id, 2)
    ep3 = _episode(db, series.id, 3)
    # ep2 已有在途 playback 单（同用户同落点）→ 预热不重复建
    db.add(em.TransferJob(item_id=ep2.id, trigger="playback",
                          drive_id="shared1", provider="drive",
                          owner_scope="user", owner_ref="7",
                          dest_drive_id="pan115:user-7",
                          status="pending"))
    # ep3 已有 ready 变体 → 不建
    db.add(em.TransferVariant(item_id=ep3.id, owner_scope="user",
                              owner_ref="7", status="ready",
                              source_file_id="fid-ep3",
                              variant_drive_id="pan115:user-7/xxx",
                              variant_file_id="rapidfid", pickcode="pc"))
    db.commit()
    fake = _FakeUser115(rapid="hit")
    result = _preheat(db, monkeypatch, anchor, fake)
    assert result["triggered"] is False
    jobs = _jobs(db)
    assert len(jobs) == 1 and jobs[0].trigger == "playback"
    assert fake.rapid_calls == []
