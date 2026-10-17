"""完美审查第 17 轮（P2 15 项）的行为探针。

A-P2-2 未修（fail-open 是有意设计：fail-closed 曾导致 DNS 抖动时公开 CDN
域名被误拦，见 items_skipped），故无对应探针。

运行：cd 仓库根 && python3 -m pytest tests/test_perfection_r17.py -q
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import subprocess
import sys
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, ".")

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend import models


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _make_user(db, **kw):
    u = models.WebUser(username=kw.pop("username", "u1"), password_hash="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ==================== A-P2-1：吊销查询 DB 异常时 fail-closed ==============

def test_a_p2_1_is_jti_revoked_db_error_raises_401():
    from backend.security import is_jti_revoked

    class _BrokenQuery:
        def filter(self, *a, **k):
            raise RuntimeError("statement timeout")

    class _BrokenDB:
        def query(self, *a, **k):
            raise RuntimeError("statement timeout")

    with pytest.raises(HTTPException) as ei:
        is_jti_revoked(_BrokenDB(), "some-jti-not-in-cache")
    assert ei.value.status_code == 401


def test_a_p2_1_is_jti_revoked_empty_jti_still_false():
    from backend.security import is_jti_revoked

    class _BrokenDB:
        def query(self, *a, **k):
            raise AssertionError("empty jti 不该查库")

    assert is_jti_revoked(_BrokenDB(), None) is False
    assert is_jti_revoked(_BrokenDB(), "") is False


# ==================== A-P2-3：X-Real-IP 非法值不采信 =======================

def _fake_request(direct_host, headers):
    return SimpleNamespace(
        client=SimpleNamespace(host=direct_host),
        headers=headers,
    )


def test_a_p2_3_x_real_ip_must_be_valid_ip():
    from backend.ratelimit import get_client_ip

    # 回环直连（可信代理），X-Real-IP 是非法字符串 → 不许采信
    req = _fake_request("127.0.0.1", {"x-real-ip": "not-an-ip"})
    assert get_client_ip(req) != "not-an-ip"
    # 之前会原样返回、写入 LoginLog.ip 污染审计；现在回落到直连 IP
    assert get_client_ip(req) == "127.0.0.1"


def test_a_p2_3_x_real_ip_valid_still_trusted():
    from backend.ratelimit import get_client_ip

    req = _fake_request("127.0.0.1", {"x-real-ip": "1.2.3.4"})
    assert get_client_ip(req) == "1.2.3.4"
    # IPv6 也合法
    req6 = _fake_request("127.0.0.1", {"x-real-ip": "::1"})
    assert get_client_ip(req6) == "::1"


# ==================== B-P2-1：公益负天数 ==================================

def test_b_p2_1_grant_request_rejects_negative_days():
    from backend.api.welfare_admin import BulkExtendRequest, GrantRequest

    with pytest.raises(ValidationError):
        GrantRequest(user_id=1, days=-1)
    with pytest.raises(ValidationError):
        BulkExtendRequest(add_days=-30)
    # 0（永不过期）与正数仍合法
    assert GrantRequest(user_id=1, days=0).days == 0
    assert BulkExtendRequest(add_days=7).add_days == 7


def test_b_p2_1_grant_welfare_rejects_negative_days_depth():
    from backend.emby_server import portal

    with pytest.raises(ValueError):
        portal.grant_welfare(None, SimpleNamespace(id=1), "t", -5)


# ==================== B-P2-2：取消抽奖原子性 ===============================

def _lottery_db(db):
    # lottery_rounds 已在 models.Base 里（create_all 已建表），这里只清空
    db.execute(text("DELETE FROM lottery_rounds"))
    db.commit()
    return db


def test_b_p2_2_cancel_open_round_ok(db):
    from backend.api.welfare_admin import cancel_lottery_round

    _lottery_db(db)
    db.add(models.LotteryRound(id=1, title="t", chat_id=100, status="open"))
    db.commit()
    out = cancel_lottery_round(1, SimpleNamespace(id=9), db)
    assert out == {"success": True}
    assert db.execute(text("SELECT status FROM lottery_rounds WHERE id=1")).scalar() == "cancelled"


def test_b_p2_2_cancel_drawing_round_conflicts_not_clobbers(db):
    from backend.api.welfare_admin import cancel_lottery_round

    _lottery_db(db)
    # 调度器已原子认领（drawing）：取消必须 409，且不能把状态打成 cancelled
    db.add(models.LotteryRound(id=2, title="t", chat_id=100, status="drawing"))
    db.commit()
    with pytest.raises(HTTPException) as ei:
        cancel_lottery_round(2, SimpleNamespace(id=9), db)
    assert ei.value.status_code == 409
    assert db.execute(text("SELECT status FROM lottery_rounds WHERE id=2")).scalar() == "drawing"


def test_b_p2_2_cancel_missing_round_404(db):
    from backend.api.welfare_admin import cancel_lottery_round

    _lottery_db(db)
    with pytest.raises(HTTPException) as ei:
        cancel_lottery_round(999, SimpleNamespace(id=9), db)
    assert ei.value.status_code == 404


# ==================== B-P2-3：批量延期键集分页 =============================

def test_b_p2_3_bulk_extend_keyset_pagination(db):
    from backend.api.welfare_admin import BulkExtendRequest, welfare_bulk_extend

    # 520 个过期用户（> 单批 500），验证键集分页不丢人
    expired = datetime.now() - timedelta(days=5)
    for i in range(520):
        db.add(models.WebUser(username=f"w{i}", password_hash="x",
                               is_welfare=True, welfare_expires_at=expired))
    db.commit()
    req = BulkExtendRequest(min_expired_days=0, max_expired_days=30, add_days=7)
    out = welfare_bulk_extend(req, SimpleNamespace(id=9), db)
    assert out["affected"] == 520
    # 抽查：确实延期了 7 天
    u = db.query(models.WebUser).filter(models.WebUser.username == "w0").one()
    assert u.welfare_expires_at > datetime.now()


# ==================== B-P2-4/B-P2-5：发言积分 ===============================

def _chat_user(db):
    return _make_user(db, username="chatter", points=0)


def test_b_p2_4_cap_branch_rolls_back_and_releases_lock(db):
    from backend import points

    u = _chat_user(db)
    _set_config(db, "points_chat_daily_cap", 5)
    _set_config(db, "points_chat_per_msg", 1)
    # 今日已拿满 5 分 → 走上限分支
    db.add(models.PointsLog(user_id=u.id, amount=5, balance_after=5,
                            type="chat", description="t",
                            created_at=datetime.now()))
    db.commit()
    assert points.award_chat_points(db, u.id) == 0
    # 上限分支已结束事务（之前行锁持有到请求结束）
    assert not db.in_transaction()


def test_b_p2_5_negative_per_msg_falls_back_no_deduction(db):
    from backend import points

    u = _chat_user(db)
    _set_config(db, "points_chat_daily_cap", 100)
    _set_config(db, "points_chat_per_msg", -3)
    got = points.award_chat_points(db, u.id)
    # 非法负配置回落默认 1：奖励 1 分，绝不扣分
    assert got == 1
    db.refresh(u)
    assert u.points == 1


def test_b_p2_5_zero_per_msg_disables_reward(db):
    from backend import points

    u = _chat_user(db)
    _set_config(db, "points_chat_daily_cap", 100)
    _set_config(db, "points_chat_per_msg", 0)
    assert points.award_chat_points(db, u.id) == 0
    db.refresh(u)
    assert u.points == 0


# ==================== B-P2-6：券预订清理时限 ================================

def test_b_p2_6_reserve_hours_negative_falls_back_default(db):
    from backend import coupons

    _set_config(db, "coupon_reserve_hours", "-5")
    assert coupons.reserve_hours(db) == coupons.DEFAULT_RESERVE_HOURS
    _set_config(db, "coupon_reserve_hours", "0")
    assert coupons.reserve_hours(db) == 0  # 显式 0 = 禁用清理
    _set_config(db, "coupon_reserve_hours", "48")
    assert coupons.reserve_hours(db) == 48
    _set_config(db, "coupon_reserve_hours", "not-a-number")
    assert coupons.reserve_hours(db) == coupons.DEFAULT_RESERVE_HOURS


# ==================== C-P2-1：合并条目 enrich_status =======================

def test_c_p2_1_merge_group_marks_done():
    from backend.emby_server import merge_versions_worker

    def _item(i):
        return SimpleNamespace(id=i, merged_into_id=None, name=f"m{i}",
                               enrich_status="pending", poster_path=None,
                               primary_image_url=None, tmdb_id="",
                               overview="")

    group = [_item(1), _item(2)]
    n = merge_versions_worker.merge_group(None, group)
    assert n == 1
    merged = [x for x in group if x.merged_into_id is not None]
    assert len(merged) == 1
    # 被合并条目 enrich 记 done：_claim_batch 永久排除它们，backlog 不再膨胀
    assert merged[0].enrich_status == "done"


# ==================== C-P2-2：消费异常时 ACK ================================

def test_c_p2_2_consume_loop_acks_on_enqueue_failure(monkeypatch):
    from backend.emby_server import scan_queue_redis as sqr

    acked = []

    def _pop(timeout=5):
        # 第一次调用后停循环
        sqr._consumer_stop.set()
        return {"library_id": 1, "trigger": "manual", "_raw": "raw-1"}

    class _BrokenSession:
        def __init__(self, *a, **k):
            raise RuntimeError("db down")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(sqr, "pop_scan_request", _pop)
    # SessionLocal 是 _consume_loop 内延迟导入的，打补丁要打 backend.database 上
    import backend.database as _dbmod
    monkeypatch.setattr(_dbmod, "SessionLocal", _BrokenSession)
    monkeypatch.setattr(sqr, "ack_scan_request", lambda raw: acked.append(raw))
    try:
        sqr._consume_loop()
    finally:
        sqr._consumer_stop.clear()
    assert acked == ["raw-1"]


# ==================== C-P2-3：指纹混入远端 ModTime ==========================

def _scan_file(**kw):
    from backend.emby_server.scanner import ScanFile
    base = dict(stored_path="mount://9/a.mkv", name="a.mkv", size=100,
                local_dir=None)
    base.update(kw)
    return ScanFile(**base)


def test_c_p2_3_file_fingerprint_detects_same_size_replace():
    from backend.emby_server import scanner as sc

    before = _scan_file(remote_mod_ts=1700000000.0)
    after = _scan_file(remote_mod_ts=1700003600.0)  # 同尺寸内容替换
    assert sc._file_fingerprint(before) != sc._file_fingerprint(after)


def test_c_p2_3_file_fingerprint_stable_without_modtime():
    from backend.emby_server import scanner as sc

    a = _scan_file(remote_mod_ts=0.0)
    b = _scan_file(remote_mod_ts=0.0)
    assert sc._file_fingerprint(a) == sc._file_fingerprint(b)
    # 无 ModTime 的提供方保持老口径 path|size（不抖动）
    import hashlib
    old = hashlib.md5("mount://9/a.mkv|100".encode()).hexdigest()
    assert sc._file_fingerprint(a) == old


def test_c_p2_3_dir_fingerprint_detects_same_size_replace(monkeypatch):
    from backend.emby_server import scanner as sc
    from backend.emby_server.mounts import MountEntry

    entries = [MountEntry(name="a.mkv", rel="/a.mkv", is_dir=False,
                          size=100, mod_ts=1000.0),
               MountEntry(name="b.mkv", rel="/b.mkv", is_dir=False,
                          size=200, mod_ts=2000.0)]
    monkeypatch.setattr(sc, "_mount_names", lambda ctx, sf: entries)

    class _Ctx:
        def __init__(self):
            self.dir_fingerprints = {}
            self.dir_list_failed = set()

    sf = _scan_file(local_dir=None, mount_id=9, dir_rel="/")
    fp1 = sc._dir_fingerprint(_Ctx(), sf)
    # 同尺寸替换 a.mkv（只改 ModTime）
    entries[0].mod_ts = 9999.0
    fp2 = sc._dir_fingerprint(_Ctx(), sf)
    assert fp1 != fp2


# ==================== C-P2-4：等父级上限 + 指数退避 =========================

def _wait_item(iid=7):
    return SimpleNamespace(id=iid, series_id=3, enrich_status="enriching",
                           enrich_next_retry_at=None, enrich_claimed_at="x",
                           enrich_claim_token="t", enrich_priority=5)


def _wait_db(parent_status):
    parent = SimpleNamespace(enrich_status=parent_status)

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return parent

    return SimpleNamespace(query=lambda *a, **k: _Q())


def test_c_p2_4_wait_caps_at_20_then_gives_up(monkeypatch):
    from backend.emby_server import enrich_worker as ew

    ew._parent_wait_counts.clear()
    db = _wait_db("pending")
    item = _wait_item()
    backoffs = []
    for _ in range(21):
        if ew._parent_should_wait(db, item):
            delta = (item.enrich_next_retry_at - datetime.now()).total_seconds()
            backoffs.append(delta)
        else:
            backoffs.append(None)
    # 前 20 次等待，第 21 次超限按父级终态失败处理（返回 False → 走 none）
    assert len([b for b in backoffs[:20] if b is not None]) == 20
    assert backoffs[20] is None
    # 指数退避：300s 起，每次翻倍（上限 2h）
    assert backoffs[0] == pytest.approx(300, abs=5)
    assert backoffs[1] == pytest.approx(600, abs=5)
    assert backoffs[2] == pytest.approx(1200, abs=5)
    # 超限后计数清掉，不会泄漏
    assert item.id not in ew._parent_wait_counts


def test_c_p2_4_parent_done_returns_false_and_clears():
    from backend.emby_server import enrich_worker as ew

    ew._parent_wait_counts.clear()
    ew._parent_wait_counts[7] = 5
    item = _wait_item()
    assert ew._parent_should_wait(_wait_db("done"), item) is False
    assert 7 not in ew._parent_wait_counts


# ==================== C-P2-5：douban/altmeta 配置 60s 缓存 ==================

def test_c_p2_5_douban_cfg_cached_60s(monkeypatch):
    from backend.emby_server import enrich_worker as ew
    from backend.emby_server import douban as dbn

    calls = {"n": 0}

    def _enabled(db):
        calls["n"] += 1
        return True

    monkeypatch.setattr(dbn, "enabled", _enabled)
    monkeypatch.setattr(dbn, "min_interval", lambda db: 1.5)
    monkeypatch.setattr(ew, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    ew._douban_cfg_cache.update({"at": 0.0, "cfg": None})

    assert ew._cached_douban_cfg() == (True, 1.5)
    assert ew._cached_douban_cfg() == (True, 1.5)
    assert calls["n"] == 1  # 第二次走缓存，不再开会话
    # TTL 过期后重新读
    ew._douban_cfg_cache["at"] = 0.0
    assert ew._cached_douban_cfg() == (True, 1.5)
    assert calls["n"] == 2


def test_c_p2_5_altmeta_cfg_cached_60s(monkeypatch):
    from backend.emby_server import enrich_worker as ew
    from backend.emby_server import altmeta

    calls = {"n": 0}

    def _enabled(db):
        calls["n"] += 1
        return True

    monkeypatch.setattr(altmeta, "enabled", _enabled)
    monkeypatch.setattr(altmeta, "min_interval", lambda db: 2.0)
    monkeypatch.setattr(altmeta, "min_interval_bangumi", lambda db: 3.0)
    monkeypatch.setattr(ew, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    ew._altmeta_cfg_cache.update({"at": 0.0, "cfg": None})

    assert ew._cached_altmeta_cfg() == (True, 2.0, 3.0)
    assert ew._cached_altmeta_cfg() == (True, 2.0, 3.0)
    assert calls["n"] == 1


# ==================== C-P2-6/7：模块顶层 env 解析 fail-safe ================

def test_c_p2_6_env_int_invalid_falls_back_with_warning(monkeypatch, caplog):
    from backend.emby_server.env_util import env_float, env_int

    monkeypatch.setenv("T17_INT", "not-a-number")
    with caplog.at_level("WARNING", logger="backend.emby_server.env_util"):
        assert env_int("T17_INT", 400, 20, 100000) == 400
    assert any("T17_INT" in r.message for r in caplog.records)

    monkeypatch.setenv("T17_INT", "10")  # 越下限 → 钳制
    assert env_int("T17_INT", 400, 20, 100000) == 20
    monkeypatch.delenv("T17_INT")
    assert env_int("T17_INT", 400, 20, 100000) == 400

    monkeypatch.setenv("T17_FLOAT", "abc")
    assert env_float("T17_FLOAT", 0.5, 0.0) == 0.5


def test_c_p2_7_scanner_module_loads_with_garbage_env():
    # 模块顶层数值 env 全非法时，import 不许炸（之前 int("abc") 直接阻止模块加载）
    env = dict(os.environ, SCAN_BATCH="abc", SCAN_CLEANUP_BATCH="",
               SCAN_REMOVAL_MAX_RATIO="x", SCAN_REMOVAL_MAX_ABSOLUTE="y",
               SCAN_IO_NICE="z", PROBE_FILE_TIMEOUT_SEC="w",
               FFPROBE_PROBESIZE="v", EMBY_SCAN_HISTORY="u")
    code = ("import backend.emby_server.scanner as sc;"
            "print(sc.SCAN_BATCH, sc.SCAN_IO_NICE, sc.SCAN_RUN_KEEP)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "400 10 20"
