# -*- coding: utf-8 -*-
"""形态 B 云下载 lane（2026-10-30 立项，用户亲批「按草案做、
新开关默认关、失败回落」）：跨 provider Drive→115 在秒传接力
预检之后、搬字节老路之前，先试让 115 自己拉——为 job 现铸转存
专用 TTL 的签名取流地址（走唯一构造点 _signed_local_stream_url，
api_key 不带任何用户令牌），115 抓取器从我方取流落进点播用户
本人盘的转存目录；成功后按「大小＋后缀」认领新文件（落盘名取
自 URL 尾段如 stream.m2ts、不是片名，不许按名匹配），登记本人
变体 ready 后立即删 115 侧任务记录（记录里存有签名地址）。

失败口径：建单失败/任务终态失败/超时/认领不到 → 同一 job 明确
回落搬字节老路，落独立 error_kind=cloudpull_failed 与
transfer_cloudpull outcome=fallback 日志；绝不直接判 job failed
（老路也失败才走既有失败语义）。Pan115AuthError 是绑定问题、
不回落，与其它 lane 同口径判 auth_error。开关默认关：关时
lane 完全不触发、行为与现行一字不变。

全部离线：115 客户端、SA 下载、签名构造点、公开基址一律桩
替身，不用生产 Cookie、不连真实 115。GB 级/302 跟随/冷态
直抓未实测（可行性报告第七节），靠开关默认关 + 失败回落兜底。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
import logging

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
from backend.emby_server import stream_limits as sl


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
    sl.clear_table_policy_cache()
    store.invalidate()
    yield session
    session.close()
    sl.clear_table_policy_cache()
    store.invalidate()


_counter = [2000]
_PAYLOAD = bytes(range(256)) * 4  # 1024 字节
_PAYLOAD_SHA1 = hashlib.sha1(_PAYLOAD).hexdigest()
_TV_LOGGER = "backend.emby_server.transfer_variant"


def _item(db, **kw):
    _counter[0] += 1
    row = em.MediaItem(guid=f"cloudpull-{_counter[0]}", library_id=1,
                       item_type="movie", name="云下载测试片",
                       drive_file_id="origfid",
                       file_path="mount://3/电影/relay.mkv",
                       size=len(_PAYLOAD))
    for key, value in kw.items():
        setattr(row, key, value)
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


def _cross_job(db, item_id, user_id):
    row = em.TransferJob(item_id=item_id, trigger="playback",
                         drive_id="shared1", provider="drive",
                         owner_scope="user", owner_ref=str(user_id),
                         dest_drive_id=f"pan115:user-{user_id}",
                         status="pending")
    db.add(row)
    db.commit()
    return row


def _enable(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


def _set_config(db, key, value):
    row = db.query(base_models.SystemConfig).filter_by(key=key).first()
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()
    store.invalidate()


def _enable_cloudpull(db):
    _set_config(db, "transfer_cross_cloudpull_enabled", "true")


class _FakeSA:
    """SA 桩：get_file 报大小，download_range 按段切字节并计数。"""

    def __init__(self, payload=_PAYLOAD):
        self.payload = payload
        self.download_calls = 0

    def get_file(self, drive_id, fid):
        return {"id": fid, "size": len(self.payload)}

    def download_range(self, drive_id, fid, start, end):
        self.download_calls += 1
        return self.payload[start:end + 1]


class _FakeCloud115:
    """用户侧 115 桩：转存目录预建；rapid 可配命中/未命中；
    cloudpull 行为可配：ok（落盘＋干扰项）/ambiguous（两个同
    大小同后缀候选、靠任务 file_id 定夺）/claim_missing（任务
    报成功但目录无可认领新文件）/add_error/add_auth_error/
    task_fail/never（永远进行中，靠超时回落）。"""

    def __init__(self, cloudpull="ok", rapid="miss"):
        self.dirs = {
            "0": [{"name": "aetrix-transfer", "is_dir": True, "cid": "900"}],
            "900": [],
        }
        self.cloudpull_behavior = cloudpull
        self.rapid_behavior = rapid
        self.rapid_calls = []
        self.simple_calls = []
        self.add_task_calls = []
        self.del_task_calls = []
        self.rename_calls = []
        self.tasks_calls = 0
        self._added = False
        self._task = None

    # ---- 现行口（与接力桩同形态） ----

    def list_dir(self, cid="0", limit=200):
        return list(self.dirs.get(str(cid), []))

    def mkdir(self, parent_cid, name):
        self.dirs.setdefault("0", []).append(
            {"name": name, "is_dir": True, "cid": "900"})
        return {}

    def rapid_upload(self, filename, filesize, file_sha1, pid,
                     range_reader=None):
        self.rapid_calls.append((filename, filesize, file_sha1, pid))
        if self.rapid_behavior == "hit":
            self.dirs["900"] = [{"name": filename, "is_dir": False,
                                 "fid": "rapidfid", "pickcode": "pcrapid",
                                 "size": filesize, "sha1": file_sha1}]
            return {"outcome": "hit", "fid": "rapidfid",
                    "pickcode": "pcrapid", "raw": {"status": 2}}
        return {"outcome": "miss", "fid": "", "pickcode": "", "raw": {}}

    def simple_upload(self, fileobj, filename, pid):
        data = fileobj.read()
        self.simple_calls.append(data)
        sha1 = hashlib.sha1(data).hexdigest()
        self.dirs["900"] = [e for e in self.dirs["900"]
                            if e.get("fid") != "upfid"]
        self.dirs["900"].append({"name": filename, "is_dir": False,
                                 "fid": "upfid", "pickcode": "pcup",
                                 "size": len(data), "sha1": sha1})
        return {"fid": "upfid", "pickcode": "pcup", "sha1": sha1,
                "raw": {"data": {"file_id": "upfid", "sha1": sha1}}}

    # ---- 云下载口 ----

    def cloudpull_tasks(self):
        self.tasks_calls += 1
        if not self._added or self._task is None:
            return []
        return [dict(self._task)]

    def cloudpull_add_task(self, url, wp_path_id):
        self.add_task_calls.append((url, wp_path_id))
        if self.cloudpull_behavior == "add_error":
            raise transfer115.Pan115Error("建单被拒")
        if self.cloudpull_behavior == "add_auth_error":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        self._added = True
        base = {"info_hash": "cphash", "url": url, "name": "stream.m2ts",
                "size": len(_PAYLOAD), "file_id": ""}
        if self.cloudpull_behavior == "task_fail":
            self._task = {**base, "status": -1, "percentDone": 0}
        elif self.cloudpull_behavior == "never":
            self._task = {**base, "status": 1, "percentDone": 50}
        else:
            self._task = {**base, "status": 2, "percentDone": 100,
                          "file_id": "cpfid"}
            if self.cloudpull_behavior == "ok":
                self._land_ok()
            elif self.cloudpull_behavior == "ambiguous":
                self._land_ambiguous()
            # claim_missing：任务报成功但不落任何可认领文件
        return {"state": True}

    def _land_ok(self):
        self.dirs["900"].extend([
            {"name": "zzz.mp4", "is_dir": False, "fid": "wrongsize",
             "pickcode": "pcw1", "size": 999, "sha1": ""},
            {"name": "other.mkv", "is_dir": False, "fid": "wrongsuffix",
             "pickcode": "pcw2", "size": len(_PAYLOAD), "sha1": ""},
            {"name": "stream.m2ts", "is_dir": False, "fid": "cpfid",
             "pickcode": "pccp", "size": len(_PAYLOAD),
             "sha1": _PAYLOAD_SHA1},
        ])

    def _land_ambiguous(self):
        self.dirs["900"].extend([
            {"name": "stream.m2ts", "is_dir": False, "fid": "cpother",
             "pickcode": "pco", "size": len(_PAYLOAD), "sha1": ""},
            {"name": "stream.m2ts", "is_dir": False, "fid": "cpfid",
             "pickcode": "pccp", "size": len(_PAYLOAD),
             "sha1": _PAYLOAD_SHA1},
        ])

    def cloudpull_del_task(self, info_hash):
        self.del_task_calls.append(info_hash)
        return {"state": True}

    def rename_file(self, fid, new_name):
        self.rename_calls.append((fid, new_name))
        for e in self.dirs["900"]:
            if e.get("fid") == fid:
                e["name"] = new_name
        return {"state": True}


class _Mint:
    """签名构造点桩：记录 base/ttl/api_key，返回实测同形地址。"""

    def __init__(self):
        self.calls = []

    def __call__(self, item, base, user_id, db, api_key="", with_sign=True,
                 ttl_seconds=None):
        self.calls.append({"base": base, "user_id": user_id,
                           "api_key": api_key, "ttl_seconds": ttl_seconds})
        url = (f"{base}/emby/Videos/{item.guid}/stream.m2ts"
               f"?static=true&MediaSourceId={item.guid}"
               f"&uid={user_id}&exp=9999999999&sign={'s' * 32}")
        return url, 9999999999, "s" * 32


def _patch_mint(monkeypatch, base="https://cdn.example.test"):
    from backend.emby_server import api as ea_api
    from backend.emby_server import public_url
    mint = _Mint()
    monkeypatch.setattr(ea_api, "_signed_local_stream_url", mint)
    monkeypatch.setattr(public_url, "client_base_url", lambda req: base)
    return mint


def _patch_users(monkeypatch, fakes):
    monkeypatch.setattr(
        ub, "binding_115_cookie",
        lambda db_arg, user_id, binding_id: f"CK-{user_id}")
    monkeypatch.setattr(
        tv, "_make_pan115_client",
        lambda cookie: fakes[int(str(cookie).split("-")[1])])


def _variants(db, item_id):
    return (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item_id)
            .order_by(em.TransferVariant.id)
            .all())


def _setup(db, monkeypatch, fake, user_id=7, **item_kw):
    _enable(monkeypatch)
    item = _item(db, **item_kw)
    _binding(db, user_id)
    job = _cross_job(db, item.id, user_id)
    _patch_users(monkeypatch, {user_id: fake})
    return item, job


# ---------- 开关关：lane 完全不触发（默认口径锁定） ----------

def test_cloudpull_switch_off_never_triggers(db, monkeypatch):
    fake = _FakeCloud115(cloudpull="ok")
    item, job = _setup(db, monkeypatch, fake)  # 不开 cloudpull 开关
    mint = _patch_mint(monkeypatch)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.add_task_calls == []
    assert fake.tasks_calls == 0
    assert mint.calls == []
    assert fake.simple_calls == [_PAYLOAD]  # 老路照常搬字节
    rows = _variants(db, item.id)
    assert len(rows) == 1 and rows[0].variant_file_id == "upfid"


# ---------- 成功路：按大小＋后缀认领、登记 ready、删任务记录 ----------

def test_cloudpull_success_claims_by_size_suffix(db, monkeypatch, caplog):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="ok")
    item, job = _setup(db, monkeypatch, fake)
    # 转存目录里预先躺着一个同大小同后缀的旧文件：认领必须靠
    # 建单前快照差集排除它，绝不认错。
    fake.dirs["900"] = [{"name": "stream.m2ts", "is_dir": False,
                         "fid": "oldfid", "pickcode": "pcold",
                         "size": len(_PAYLOAD), "sha1": ""}]
    mint = _patch_mint(monkeypatch)
    sa = _FakeSA()
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, sa)
    assert res["outcome"] == "done"
    assert fake.simple_calls == []          # 没走搬字节
    assert sa.download_calls == 0          # 也没经 SA 取字节
    assert len(fake.add_task_calls) == 1
    assert fake.add_task_calls[0][1] == "900"  # 落点=本人转存目录
    assert mint.calls[0]["api_key"] == ""   # 不带任何用户令牌
    assert mint.calls[0]["user_id"] == 7
    rows = _variants(db, item.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.owner_ref == "7"
    assert row.variant_drive_id == "pan115:user-7"
    assert row.variant_file_id == "cpfid"   # 不是 oldfid/wrongsize/wrongsuffix
    assert row.pickcode == "pccp"
    assert row.status == "ready"
    # 认领后改名到副本名（file_path 尾段）
    assert fake.rename_calls == [("cpfid", "relay.mkv")]
    # 任务记录里存有签名地址：ready 后立即删除
    assert fake.del_task_calls == ["cphash"]
    # 目录项 sha1 沉淀进条目（与搬字节路同口径，给接力留弹药）
    db.refresh(item)
    assert item.content_hash_algo == "sha1"
    assert item.content_hash == _PAYLOAD_SHA1
    assert "transfer_cloudpull outcome=hit" in caplog.text


def test_cloudpull_claim_prefers_task_file_id_when_ambiguous(
        db, monkeypatch):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="ambiguous")
    item, job = _setup(db, monkeypatch, fake)
    _patch_mint(monkeypatch)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    rows = _variants(db, item.id)
    assert len(rows) == 1
    # 两个同大小同后缀候选：认任务回执 file_id 指的那个
    assert rows[0].variant_file_id == "cpfid"


# ---------- lane 顺序：接力命中在前、未命中才试 cloudpull ----------

def test_cloudpull_not_tried_when_relay_hits(db, monkeypatch):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="ok", rapid="hit")
    item, job = _setup(db, monkeypatch, fake,
                       content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _patch_mint(monkeypatch)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1
    assert fake.add_task_calls == []  # 接力已命中，cloudpull 不出场
    rows = _variants(db, item.id)
    assert rows[0].variant_file_id == "rapidfid"


def test_cloudpull_tried_before_simple_after_relay_miss(
        db, monkeypatch):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="ok", rapid="miss")
    item, job = _setup(db, monkeypatch, fake,
                       content_hash=_PAYLOAD_SHA1, content_hash_algo="sha1")
    _patch_mint(monkeypatch)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert len(fake.rapid_calls) == 1
    assert len(fake.add_task_calls) == 1
    assert fake.simple_calls == []
    rows = _variants(db, item.id)
    assert rows[0].variant_file_id == "cpfid"


# ---------- 失败回落：建单失败 ----------

def test_cloudpull_add_failure_falls_back(db, monkeypatch, caplog):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="add_error")
    item, job = _setup(db, monkeypatch, fake)
    _patch_mint(monkeypatch)
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"          # 回落后老路搬成
    assert fake.simple_calls == [_PAYLOAD]
    rows = _variants(db, item.id)
    assert len(rows) == 1 and rows[0].variant_file_id == "upfid"
    assert "transfer_cloudpull outcome=fallback" in caplog.text
    assert "error_kind=cloudpull_failed" in caplog.text
    assert "step=add_task" in caplog.text


# ---------- 失败回落：任务终态失败 ----------

def test_cloudpull_task_failure_falls_back(db, monkeypatch, caplog):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="task_fail")
    item, job = _setup(db, monkeypatch, fake)
    _patch_mint(monkeypatch)
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.simple_calls == [_PAYLOAD]
    assert "transfer_cloudpull outcome=fallback" in caplog.text
    assert "step=task_failed" in caplog.text
    # 失败任务记录里同样存有签名地址：顺手删掉，不留痕
    assert fake.del_task_calls == ["cphash"]


# ---------- 失败回落：超时 ----------

class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += sec


def test_cloudpull_timeout_falls_back(db, monkeypatch, caplog):
    _enable_cloudpull(db)
    _set_config(db, "transfer_cross_cloudpull_timeout_sec", "60")
    fake = _FakeCloud115(cloudpull="never")
    item, job = _setup(db, monkeypatch, fake)
    _patch_mint(monkeypatch)
    clock = _FakeClock()
    monkeypatch.setattr(tv, "_CLOUDPULL_NOW", clock.now)
    monkeypatch.setattr(tv, "_CLOUDPULL_SLEEP", clock.sleep)
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.simple_calls == [_PAYLOAD]
    assert "transfer_cloudpull outcome=fallback" in caplog.text
    assert "step=timeout" in caplog.text


# ---------- 失败回落：认领不到 ----------

def test_cloudpull_claim_missing_falls_back(db, monkeypatch, caplog):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="claim_missing")
    item, job = _setup(db, monkeypatch, fake)
    _patch_mint(monkeypatch)
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.simple_calls == [_PAYLOAD]
    rows = _variants(db, item.id)
    assert len(rows) == 1 and rows[0].variant_file_id == "upfid"
    assert "transfer_cloudpull outcome=fallback" in caplog.text
    assert "step=claim" in caplog.text


# ---------- 失败回落：无公开基址现铸不出地址 ----------

def test_cloudpull_no_public_base_falls_back(db, monkeypatch, caplog):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="ok")
    item, job = _setup(db, monkeypatch, fake)
    _patch_mint(monkeypatch, base="")
    with caplog.at_level(logging.INFO, logger=_TV_LOGGER):
        res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert fake.add_task_calls == []
    assert fake.simple_calls == [_PAYLOAD]
    assert "transfer_cloudpull outcome=fallback" in caplog.text
    assert "step=mint" in caplog.text


# ---------- Cookie 失效不回落：与其它 lane 同口径判 auth_error ----------

def test_cloudpull_auth_error_not_fallback(db, monkeypatch):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="add_auth_error")
    item, job = _setup(db, monkeypatch, fake)
    _patch_mint(monkeypatch)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "failed"
    assert res["error_kind"] == "auth_error"
    assert fake.simple_calls == []  # 死 Cookie 不拿去走搬字节再失败一遍


# ---------- TTL：走转存专用口径，不碰播放按时长口径 ----------

def test_cloudpull_ttl_uses_transfer_knob(db, monkeypatch):
    _enable_cloudpull(db)
    _set_config(db, "transfer_cross_cloudpull_ttl_sec", "20000")
    fake = _FakeCloud115(cloudpull="ok")
    item, job = _setup(db, monkeypatch, fake)
    mint = _patch_mint(monkeypatch)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    assert len(mint.calls) == 1
    assert mint.calls[0]["ttl_seconds"] == 20000


def test_cloudpull_ttl_default_is_transfer_default(db, monkeypatch):
    _enable_cloudpull(db)
    fake = _FakeCloud115(cloudpull="ok")
    item, job = _setup(db, monkeypatch, fake)
    mint = _patch_mint(monkeypatch)
    res = tv.run_transfer_job_once(db, job, _FakeSA())
    assert res["outcome"] == "done"
    # 默认 43200（12 小时）：覆盖 115 排队调度 + GB 级慢拉 + 其
    # 内置重试；与播放按时长口径（未知时长约 3600）明确分开。
    assert mint.calls[0]["ttl_seconds"] == 43200
