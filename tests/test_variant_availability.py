"""变体级可用性落库 + 真探活（FMBY 播放阶段 4，清单第 4 项）。

钉死的范围（多一件不做）：
- 状态机：Unknown 不覆盖已知（也不凭空建行）、成功复位、阈值翻转
  （翻转时间只记首次）、失败原因分两类、无记录当可用；
- 探活定性：HTTP 状态 → 结论的映射（含 200 HTML 不是字节的教训）、
  resolve 层已分类异常的透传、未分类异常不冤判凭据错；
- 开关关时零行为：工人单轮不采样、不发请求、不写行；
- 探针采样：只取成功起播事件、每变体每挂载取最新一条、local_file
  不采样、目标缺身份跳过。

全部用隔离的内存 SQLite + 注入的 fetch 函数，不碰网络、不碰生产库。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.emby_server import models as em
from backend.emby_server import variant_availability as va


@pytest.fixture()
def db():
    from backend import models as backend_models

    engine = create_engine("sqlite://")
    backend_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _row(db, key):
    return (db.query(em.VariantAvailability)
            .filter(em.VariantAvailability.variant_key == key)
            .one_or_none())


def _seed(db, key, status="available", failures=0, error=None, since=None):
    row = em.VariantAvailability(
        variant_key=key, availability_status=status,
        consecutive_failures=failures, last_error_kind=error,
        unavailable_since=since,
        last_checked_at=datetime.now() - timedelta(hours=1))
    db.add(row)
    db.commit()
    return row


def _item(db, guid, **kw):
    row = em.MediaItem(guid=guid, library_id=1, item_type="movie",
                       name=f"item-{guid}", **kw)
    db.add(row)
    db.commit()
    return row


def _event(db, variant, item_id, mount_id=None, result="ok", hours_ago=1):
    row = em.PlayStartEvent(
        provider="mount" if variant == "mount_relay" else "drive",
        mount_id=mount_id, variant=variant, result=result,
        created_at=datetime.now() - timedelta(hours=hours_ago))
    row.item_id = item_id
    db.add(row)
    db.commit()
    return row


# --- 状态机 ---

def test_unknown_never_overwrites_known_state(db):
    _seed(db, "drive_relay", failures=2, error="remote_unavailable")
    # 同一 identity map 的对象会被原地改，先把时间戳取值存下来再比。
    before_ts = _row(db, "drive_relay").last_checked_at
    assert va.apply_probe_result(db, "drive_relay",
                                 va.ProbeOutcome(kind=va.OUTCOME_UNKNOWN),
                                 threshold=3) is not None
    after = _row(db, "drive_relay")
    assert after.availability_status == "available"
    assert after.consecutive_failures == 2
    assert after.last_error_kind == "remote_unavailable"
    assert after.last_checked_at > before_ts


def test_unknown_creates_no_row(db):
    assert va.apply_probe_result(db, "drive_relay",
                                 va.ProbeOutcome(kind=va.OUTCOME_UNKNOWN),
                                 threshold=3) is None
    assert _row(db, "drive_relay") is None


def test_success_resets_failures_and_unavailable(db):
    _seed(db, "drive_relay", status="unavailable", failures=5,
          error="credential_error", since=datetime.now() - timedelta(hours=2))
    va.apply_probe_result(db, "drive_relay",
                          va.ProbeOutcome(kind=va.OUTCOME_OK), threshold=3)
    row = _row(db, "drive_relay")
    assert row.availability_status == "available"
    assert row.consecutive_failures == 0
    assert row.last_error_kind is None
    assert row.unavailable_since is None
    assert row.last_success_at is not None


def test_failure_counts_and_flips_only_at_threshold(db):
    for i in (1, 2):
        va.apply_probe_result(db, "drive_relay",
                              va.ProbeOutcome(kind=va.OUTCOME_REMOTE_UNAVAILABLE),
                              threshold=3)
        row = _row(db, "drive_relay")
        assert row.availability_status == "available"
        assert row.consecutive_failures == i
    va.apply_probe_result(db, "drive_relay",
                          va.ProbeOutcome(kind=va.OUTCOME_REMOTE_UNAVAILABLE),
                          threshold=3)
    row = _row(db, "drive_relay")
    assert row.availability_status == "unavailable"
    assert row.consecutive_failures == 3
    flipped_at = row.unavailable_since
    assert flipped_at is not None
    # 继续失败只累计、不刷新首次翻转时间。
    va.apply_probe_result(db, "drive_relay",
                          va.ProbeOutcome(kind=va.OUTCOME_REMOTE_UNAVAILABLE),
                          threshold=3)
    row = _row(db, "drive_relay")
    assert row.consecutive_failures == 4
    assert row.unavailable_since == flipped_at


def test_failure_kinds_recorded_separately(db):
    va.apply_probe_result(db, "drive_relay",
                          va.ProbeOutcome(kind=va.OUTCOME_CREDENTIAL_ERROR),
                          threshold=3)
    assert _row(db, "drive_relay").last_error_kind == "credential_error"
    va.apply_probe_result(db, "drive_direct",
                          va.ProbeOutcome(kind=va.OUTCOME_REMOTE_UNAVAILABLE),
                          threshold=3)
    assert _row(db, "drive_direct").last_error_kind == "remote_unavailable"


def test_is_variant_available_no_row_means_available(db):
    assert va.is_variant_available(db, "drive_relay") is True
    _seed(db, "drive_relay", status="unavailable", failures=3)
    assert va.is_variant_available(db, "drive_relay") is False


def test_variant_key_for():
    assert va.variant_key_for("drive_relay") == "drive_relay"
    assert va.variant_key_for("drive_direct") == "drive_direct"
    assert va.variant_key_for("mount_relay") == "mount_relay"
    assert va.variant_key_for("mount_relay", 3) == "mount_relay:3"


# --- 探活定性 ---

def test_classify_http_status():
    assert va.classify_http_status(206) == va.OUTCOME_OK
    assert va.classify_http_status(200, "video/x-matroska") == va.OUTCOME_OK
    assert va.classify_http_status(200, "text/html; charset=utf-8") == va.OUTCOME_REMOTE_UNAVAILABLE
    assert va.classify_http_status(401) == va.OUTCOME_CREDENTIAL_ERROR
    assert va.classify_http_status(403) == va.OUTCOME_CREDENTIAL_ERROR
    assert va.classify_http_status(404) == va.OUTCOME_UNKNOWN
    assert va.classify_http_status(500) == va.OUTCOME_REMOTE_UNAVAILABLE
    assert va.classify_http_status(429) == va.OUTCOME_REMOTE_UNAVAILABLE


def test_probe_fetch_maps_fetch_results():
    assert va.probe_fetch(lambda: (206, None)).kind == va.OUTCOME_OK
    out = va.probe_fetch(lambda: (403, None))
    assert out.kind == va.OUTCOME_CREDENTIAL_ERROR and out.http_status == 403
    assert va.probe_fetch(lambda: (404, None)).kind == va.OUTCOME_UNKNOWN
    assert va.probe_fetch(lambda: (500, None)).kind == va.OUTCOME_REMOTE_UNAVAILABLE


def test_probe_fetch_propagates_classified_errors():
    def _cred():
        raise va.ProbeError(va.OUTCOME_CREDENTIAL_ERROR, "no token")

    def _unknown():
        raise va.ProbeError(va.OUTCOME_UNKNOWN, "mount gone")

    def _boom():
        raise RuntimeError("weird")

    assert va.probe_fetch(_cred).kind == va.OUTCOME_CREDENTIAL_ERROR
    assert va.probe_fetch(_unknown).kind == va.OUTCOME_UNKNOWN
    # 未分类异常不冤判凭据错，算远端不可用。
    assert va.probe_fetch(_boom).kind == va.OUTCOME_REMOTE_UNAVAILABLE


# --- 开关与工人单轮 ---

def test_probe_disabled_does_nothing(monkeypatch):
    monkeypatch.setenv("VARIANT_AVAILABILITY_PROBE_ENABLED", "0")

    def _must_not_run(_db):
        raise AssertionError("builder must not run when switch is off")

    assert va.probe_variants_once(lambda: None, target_builder=_must_not_run) is None
    assert va.start_probe_worker() is False


def test_probe_enabled_cycle_writes_rows(monkeypatch, db):
    monkeypatch.setenv("VARIANT_AVAILABILITY_PROBE_ENABLED", "1")
    monkeypatch.setenv("VARIANT_AVAILABILITY_FAILURE_THRESHOLD", "3")
    engine = db.get_bind()
    factory = sessionmaker(bind=engine)
    targets = [
        va.ProbeTarget(variant="drive_relay", variant_key="drive_relay", item_id=1),
        va.ProbeTarget(variant="mount_relay", variant_key="mount_relay:3", item_id=2),
    ]
    fetches = {
        "drive_relay": lambda: (206, None),
        "mount_relay:3": lambda: (403, None),
    }
    monkeypatch.setattr(
        va, "build_fetch_fn",
        lambda _db, target: fetches[target.variant_key])
    stats = va.probe_variants_once(factory, target_builder=lambda _db: targets)
    assert stats == {"targets": 2, "ok": 1, "unknown": 0,
                     "credential_error": 1, "remote_unavailable": 0}
    ok_row = _row(db, "drive_relay")
    assert ok_row.availability_status == "available"
    assert ok_row.consecutive_failures == 0
    bad_row = _row(db, "mount_relay:3")
    assert bad_row.availability_status == "available"  # 未达阈值不翻
    assert bad_row.consecutive_failures == 1
    assert bad_row.last_error_kind == "credential_error"


def test_probe_worker_starts_only_when_enabled(monkeypatch):
    monkeypatch.setenv("VARIANT_AVAILABILITY_PROBE_ENABLED", "1")
    stop = __import__("threading").Event()
    stop.set()  # 循环首查即退出，不跑真探针
    assert va.start_probe_worker(stop) is True
    va._probe_thread = None


# --- 探针采样 ---

def test_build_probe_targets_groups_and_filters(db):
    old = _item(db, "g-old", file_path="mount://3/a.mkv")
    new = _item(db, "g-new", file_path="mount://3/b.mkv")
    drv = _item(db, "g-drv", drive_file_id="fid123")
    loc = _item(db, "g-loc", file_path="/data/c.mkv")
    gone = _item(db, "g-gone", file_path="", drive_file_id="")
    _event(db, "mount_relay", old.id, mount_id=3, hours_ago=5)
    _event(db, "mount_relay", new.id, mount_id=3, hours_ago=1)
    _event(db, "drive_relay", drv.id, hours_ago=2)
    _event(db, "local_file", loc.id, hours_ago=1)
    _event(db, "mount_relay", old.id, mount_id=3, result="error", hours_ago=0.5)
    _event(db, "drive_direct", gone.id, hours_ago=1)
    targets = va.build_probe_targets(db)
    by_key = {t.variant_key: t for t in targets}
    # 每变体每挂载只取最新一条成功事件对应的条目；失败事件不采样。
    assert by_key["mount_relay:3"].item_id == new.id
    assert by_key["drive_relay"].item_id == drv.id
    # local_file 不采样；无身份（无路径无 fid）的目标跳过。
    assert "local_file" not in by_key
    assert "drive_direct" not in by_key
