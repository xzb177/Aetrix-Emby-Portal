"""审查修复回归（第二批）：lottery seed 门控、签名头。

（2026-10-17 转码删除：原转码缓存 LRU 用例已随 transcode 模块删除。）"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-for-review")

import time

from backend.database import SessionLocal, init_db
from backend import models

init_db()


def test_lottery_seed_gated_before_draw():
    """开奖前 verify_round 不得返回 seed 明文，只给 seed_hash。"""
    from backend import lottery
    db = SessionLocal()
    try:
        r = models.LotteryRound(title="review-seed-test", status="open", chat_id=-100123,
                                seed="SECRET_SEED_123", seed_hash="hash_abc")
        db.add(r)
        db.commit()
        out = lottery.verify_round(db, r.id)
        assert out["seed"] is None, "开奖前 seed 明文泄露！"
        assert out["seed_hash"] == "hash_abc"
    finally:
        db.query(models.LotteryRound).filter(
            models.LotteryRound.title == "review-seed-test").delete()
        db.commit()
        db.close()


def test_lottery_seed_visible_after_draw():
    """开奖后 seed 明文可公开（供外部复算验证）。"""
    from backend import lottery
    db = SessionLocal()
    try:
        r = models.LotteryRound(title="review-seed-test2", status="done", chat_id=-100124,
                                seed="SECRET_SEED_456", seed_hash="hash_def")
        db.add(r)
        db.commit()
        out = lottery.verify_round(db, r.id)
        assert out["seed"] == "SECRET_SEED_456"
    finally:
        db.query(models.LotteryRound).filter(
            models.LotteryRound.title == "review-seed-test2").delete()
        db.commit()
        db.close()


def test_verify_signed_headers_accepts_valid(monkeypatch):
    """合法 HMAC 签名头通过。"""
    import secrets as pysecrets
    from backend import node_auth
    monkeypatch.setenv("NODE_SHARED_SECRET", "ci-test-shared-secret")
    key = node_auth.node_shared_secret()
    assert key, "测试需要 SECRET_KEY"
    ts = str(int(time.time()))
    nonce = pysecrets.token_hex(16)
    sig = node_auth._sign(key, ts, nonce, "GET", "/api/admin/stream-nodes/bundle")
    headers = {"X-Panel-Ts": ts, "X-Panel-Nonce": nonce, "X-Panel-Sign": sig}
    assert node_auth.verify_signed_headers(
        headers, "GET", "/api/admin/stream-nodes/bundle") is True


def test_verify_signed_headers_rejects_static_key(monkeypatch):
    """静态 X-Panel-Key 必须被拒绝（bundle 不再接受）。"""
    from backend import node_auth
    monkeypatch.setenv("NODE_SHARED_SECRET", "ci-test-shared-secret")
    key = node_auth.node_shared_secret()
    assert node_auth.verify_signed_headers(
        {"X-Panel-Key": key}, "GET", "/api/admin/stream-nodes/bundle") is False


def test_verify_signed_headers_rejects_stale_ts(monkeypatch):
    """过期时间戳被拒绝。"""
    import secrets as pysecrets
    from backend import node_auth
    monkeypatch.setenv("NODE_SHARED_SECRET", "ci-test-shared-secret")
    key = node_auth.node_shared_secret()
    ts = str(int(time.time()) - 3600)  # 1 小时前
    nonce = pysecrets.token_hex(16)
    sig = node_auth._sign(key, ts, nonce, "GET", "/api/admin/stream-nodes/bundle")
    headers = {"X-Panel-Ts": ts, "X-Panel-Nonce": nonce, "X-Panel-Sign": sig}
    assert node_auth.verify_signed_headers(
        headers, "GET", "/api/admin/stream-nodes/bundle") is False
