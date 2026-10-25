"""移动云盘 139 接入（FMBY 挂载-4 照抄）：变体 / 转存 / 账号池 / sidecar。

传输/握手/会话一律复用共享契约（provider_runtime，PR #1251）：
本文件不测任何自家传输层，只测 139 自己的 handler、扩展与宿主 provider，
外加一轮真实 UDS 集成（Server + Client + handler，Fake 上游，无真实网络）。
仓库无 pytest-asyncio：全是同步用例。139 真实上游未实测：上游一律 Fake。
"""
import base64
import os
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mount_sources as ms
from backend.emby_server import mounts as mnt
from backend.emby_server.mount_sidecar import manifest_to_capabilities
from backend.emby_server.mount_yun139 import (
    MOUNT_YUN139,
    YUN139_EXTENSIONS,
    PoolMemberState,
    Yun139AccountPool,
    Yun139Mount,
    Yun139SidecarHandler,
    Yun139TransferRegistry,
    Yun139Upstream,
    Yun139Variant,
    Yun139VariantNotFound,
    apply_pool_failure,
    apply_pool_success,
    credential_profile_for_row,
    load_pool,
    parse_variant_payload,
    personal_space_variants,
    profile_for_mount,
    select_variant,
    variant_id_for,
    yun139_enabled,
)
from backend.emby_server.mounts import (
    MountAuthError,  # noqa: F401  (契约错误分级对照：139 不自造 kind)
    MountError,
    ProviderCapabilities,
)
from backend.emby_server.provider_runtime import (
    BASE_CAPABILITIES,
    ProviderRuntimeClient,
    ProviderRuntimeError,
    ProviderRuntimeServer,
    build_manifest,
    decode_range_result,
    extensions_for,
    register_provider_extensions,
    validate_manifest,
)


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


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv("YUN139_ENABLED", "1")
    yield


def _mount(**kw):
    base = {"mount_type": "yun139", "config": "{}", "path": "139:/",
            "name": "test139", "id": 99}
    base.update(kw)
    return SimpleNamespace(**base)


# ---------- 开关与注册 ----------

def test_yun139_disabled_by_default(monkeypatch):
    monkeypatch.delenv("YUN139_ENABLED", raising=False)
    assert yun139_enabled() is False


def test_yun139_enabled_flag(enabled):
    assert yun139_enabled() is True


def test_detect_mount_type_139():
    assert mnt.detect_mount_type("139:/Movies") == "yun139"


def test_mount_type_registered():
    assert mnt.type_meta("yun139").get("label") == "移动云盘 139"


def test_build_provider_disabled_raises(monkeypatch):
    monkeypatch.delenv("YUN139_ENABLED", raising=False)
    with pytest.raises(MountError, match="未启用"):
        mnt.build_provider(_mount())


def test_build_provider_enabled_but_no_sidecar_raises_runtime_error(enabled, tmp_path):
    # 构造即握手：没有 sidecar 在跑时响亮报 remote_unavailable，不静默空库。
    with pytest.raises(ProviderRuntimeError) as excinfo:
        mnt.build_provider(_mount())
    assert excinfo.value.kind == "remote_unavailable"


def test_capabilities_match_db_derivation(enabled):
    provider = Yun139Mount(_mount(), client=_FakeRuntimeClient())
    assert provider.capabilities() == ProviderCapabilities(
        can_list=True, can_random_read=True, can_read_sidecar=True,
        can_generate_play_target=True, can_refresh_credentials=False,
    )
    assert ms.capabilities_for_mount_type("yun139") == (
        provider.capabilities().as_dict())


# ---------- 共享契约：139 扩展登记与能力映射 ----------

def test_139_extensions_registered_with_yun139_prefix():
    assert extensions_for("yun139") == YUN139_EXTENSIONS
    assert all(name.startswith("yun139.") for name in YUN139_EXTENSIONS)
    assert "yun139.resolve_play_variants" in YUN139_EXTENSIONS
    assert "yun139.create_outer_link_transfer" in YUN139_EXTENSIONS


def test_register_provider_extensions_rejects_foreign_prefix():
    with pytest.raises(ValueError, match="不属于"):
        register_provider_extensions("yun139x", ("yun139.resolve_play_variants",))
    assert extensions_for("yun139x") == ()


def test_manifest_validation_for_139():
    manifest = build_manifest("yun139")
    assert validate_manifest(manifest, "yun139", YUN139_EXTENSIONS) == manifest
    with pytest.raises(ProviderRuntimeError):
        validate_manifest(manifest, "p115")
    with pytest.raises(ProviderRuntimeError):
        validate_manifest(manifest, "yun139", ("yun139.nope",))
    bad = dict(manifest, capabilities=["list_page"])
    with pytest.raises(ProviderRuntimeError):
        validate_manifest(bad, "yun139")


def test_manifest_to_capabilities_mapping():
    full = {"capabilities": list(BASE_CAPABILITIES)}
    assert manifest_to_capabilities(full) == ProviderCapabilities(
        can_list=True, can_random_read=True, can_read_sidecar=True,
        can_generate_play_target=True, can_refresh_credentials=False)
    no_range = {"capabilities": ["list_page", "resolve_play_target"]}
    caps = manifest_to_capabilities(no_range)
    assert caps.can_list is True
    assert caps.can_random_read is False
    assert caps.can_read_sidecar is False
    assert caps.can_generate_play_target is True
    assert caps.can_refresh_credentials is False


# ---------- 变体 ----------

def _variants():
    return [
        Yun139Variant(id="original-h1080", label="原画", kind="original",
                      is_default=True, height=1080),
        Yun139Variant(id="transcoded-h720", label="流畅", kind="transcoded",
                      height=720, requires_transfer=False),
    ]


def test_variant_id_with_height_and_conflict():
    existing = {"original-h1080"}
    assert variant_id_for("original", "原画", 1080, existing) == "original-h1080-2"
    assert variant_id_for("original", "原画", 1080, set()) == "original-h1080"


def test_variant_id_slug_without_height():
    assert variant_id_for("transcoded", "超清 1080P", 0, set()) == "1080p"
    assert variant_id_for("transcoded", "!!!", 0, set()) == "variant"


def test_select_variant_exact_default_first():
    variants = _variants()
    assert select_variant(variants, "transcoded-h720").id == "transcoded-h720"
    assert select_variant(variants, None).id == "original-h1080"
    plain = [Yun139Variant(id="a", label="A", kind="original"),
             Yun139Variant(id="b", label="B", kind="transcoded")]
    assert select_variant(plain, None).id == "a"


def test_select_variant_mismatch_is_404_not_fallback():
    with pytest.raises(Yun139VariantNotFound):
        select_variant(_variants(), "no-such-variant")
    with pytest.raises(Yun139VariantNotFound):
        select_variant([], None)


def test_public_payload_excludes_url():
    variant = Yun139Variant(id="original", label="原画", kind="original",
                            url="https://upstream/secret", is_default=True)
    payload = variant.public_payload()
    assert "url" not in payload
    assert payload["id"] == "original"


def test_parse_variant_payload_drops_url_and_unknown_keys():
    variant = parse_variant_payload({
        "id": "original-h1080", "label": "原画", "kind": "original",
        "url": "https://upstream/secret", "is_default": True,
        "bogus_key": "x", "height": 1080})
    assert variant.url == ""
    assert variant.height == 1080


def test_personal_space_single_original():
    variants = personal_space_variants()
    assert len(variants) == 1
    assert variants[0].id == "original"
    assert variants[0].kind == "original"
    assert variants[0].is_default is True
    assert variants[0].requires_transfer is False


# ---------- 转存 ----------

def test_transfer_registry_lifecycle():
    registry = Yun139TransferRegistry()
    task = registry.create(share_ref="share-1")
    assert task.state == "preparing"
    assert registry.get(task.id).id == task.id
    registry.mark_ready(task.id, provider_file_id="fid-9")
    assert registry.get(task.id).state == "ready"
    assert registry.get(task.id).provider_file_id == "fid-9"
    task2 = registry.create()
    registry.mark_failed(task2.id, "upstream 403")
    assert registry.get(task2.id).state == "failed"
    assert registry.get(task2.id).error == "upstream 403"
    with pytest.raises(MountError):
        registry.get("no-such-task")


# ---------- 账号池 ----------

def test_pool_weighted_pick_favors_heavy_member():
    pool = Yun139AccountPool([
        PoolMemberState(id=1, weight=3),
        PoolMemberState(id=2, weight=1),
    ], strategy="weighted", cooldown_seconds=60, max_concurrent=0)
    picks = [pool.pick(1000.0).id for _ in range(4)]
    assert sorted(picks) == [1, 1, 1, 2]


def test_pool_round_robin_ignores_weight():
    pool = Yun139AccountPool([
        PoolMemberState(id=1, weight=9),
        PoolMemberState(id=2, weight=1),
    ], strategy="round_robin", cooldown_seconds=60, max_concurrent=0)
    assert [pool.pick(1000.0).id for _ in range(4)] == [1, 2, 1, 2]


def test_pool_cooldown_and_recovery():
    pool = Yun139AccountPool([PoolMemberState(id=1, weight=1)],
                             strategy="weighted", cooldown_seconds=60,
                             max_concurrent=0)
    pool.record_failure(1, 1000.0)
    assert pool.pick(1010.0) is None
    assert pool.pick(1060.0) is not None
    pool.record_success(1)
    member = pool.members[0]
    assert member.fail_count == 0
    assert member.cooldown_until is None


def test_pool_max_concurrent_blocks_pick():
    pool = Yun139AccountPool([PoolMemberState(id=1, weight=1)],
                             strategy="weighted", cooldown_seconds=60,
                             max_concurrent=1)
    assert pool.pick(1000.0) is not None
    pool.acquire(1)
    assert pool.pick(1000.0) is None
    pool.release(1)
    assert pool.pick(1000.0) is not None


def test_pool_db_roundtrip(db):
    pool_row = em.Yun139AccountPool(name="p1", strategy="weighted",
                                    cooldown_seconds=60, max_concurrent=0)
    db.add(pool_row)
    db.commit()
    db.add(em.Yun139AccountPoolMember(pool_id=pool_row.id, label="a",
                                      weight=2, fail_count=0))
    db.add(em.Yun139AccountPoolMember(pool_id=pool_row.id, label="b",
                                      weight=1, fail_count=0))
    db.commit()
    pool = load_pool(db, pool_row.id)
    assert pool is not None
    assert len(pool.members) == 2
    assert pool.cooldown_seconds == 60
    member_id = pool.members[0].id
    assert apply_pool_failure(db, pool, member_id, now=1000.0) is True
    row = db.query(em.Yun139AccountPoolMember).filter(
        em.Yun139AccountPoolMember.id == member_id).first()
    assert row.fail_count == 1
    assert row.cooldown_until is not None
    assert apply_pool_success(db, pool, member_id) is True
    row = db.query(em.Yun139AccountPoolMember).filter(
        em.Yun139AccountPoolMember.id == member_id).first()
    assert row.fail_count == 0
    assert row.cooldown_until is None
    assert load_pool(db, 999999) is None


# ---------- 凭据档案视图 ----------

def test_credential_profile_view_is_conservative(db):
    row = em.MountCredential(mount_id=7, scope="source",
                             sealed_credential="sealed-x", key_id="fernet-v1",
                             status="active", last_error_kind="expired")
    db.add(row)
    db.commit()
    profile = profile_for_mount(db, 7)
    assert profile is not None
    assert profile["provider"] == "yun139"
    assert profile["key_id"] == "fernet-v1"
    assert profile["last_error_kind"] == "expired"
    # 无列可读就不编值：can_refresh 保守 False，授权过期时间 None。
    assert profile["can_refresh"] is False
    assert profile["authorization_expires_at"] is None
    assert profile_for_mount(db, 12345) is None
    assert profile_for_mount(None, 7) is None


def test_credential_profile_for_row_direct():
    profile = credential_profile_for_row(SimpleNamespace(
        id=3, key_id="k1", status="invalid", last_error_kind="auth_error",
        last_verified_at=None))
    assert profile["credential_id"] == 3
    assert profile["status"] == "invalid"


# ---------- Fake 上游与 Fake 运行时客户端 ----------

class _FakeUpstream(Yun139Upstream):
    def list_page(self, path, page_token=None):
        return {"entries": [
            {"name": "a.mkv", "is_dir": False, "size": 16,
             "file_id": "fid-a", "entry_id": "eid-a"},
            {"name": "sub", "is_dir": True, "size": 0,
             "file_id": "", "entry_id": "eid-sub"},
        ], "next_page_token": None}

    def stat(self, path):
        if path == "/a.mkv":
            return {"name": "a.mkv", "file_id": "fid-a", "size": 16,
                    "is_dir": False}
        if path == "/":
            return {"name": "", "file_id": "", "size": 0, "is_dir": True}
        raise ProviderRuntimeError("not_found", f"139 上找不到：{path}")

    def open_range(self, path, byte_offset, byte_length=None):
        blob = b"0123456789abcdef"
        if byte_length is None:
            return blob[byte_offset:]
        return blob[byte_offset:byte_offset + byte_length]

    def resolve_play_target(self, path, **kw):
        return {"url": "https://139.example/down/fid-a",
                "headers": {"User-Agent": "yun139-test",
                            "Cookie": "must-be-stripped",
                            "Authorization": "Bearer must-be-stripped"}}

    def resolve_play_variants(self, path, **kw):
        return [Yun139Variant(id="original", label="原画", kind="original",
                              url="https://139.example/secret",
                              is_default=True, requires_transfer=False),
                Yun139Variant(id="transcoded-h720", label="流畅",
                              kind="transcoded", height=720)]


class _FakeRuntimeClient:
    """ProviderRuntimeClient 的 duck-type 替身：方法签名与契约一致。"""

    def __init__(self):
        self.calls = []
        self.sessions = {}
        self.removed = []

    def handshake(self, required_extensions=None):
        manifest = build_manifest("yun139")
        validate_manifest(manifest, "yun139", required_extensions)
        return manifest

    def session_upsert(self, session_id, data):
        self.calls.append(("session_upsert", session_id, data))
        self.sessions[session_id] = dict(data)
        return {"ok": True, "session_id": session_id}

    def session_remove(self, session_id):
        self.calls.append(("session_remove", session_id))
        self.removed.append(session_id)
        self.sessions.pop(session_id, None)
        return {"ok": True, "session_id": session_id}

    def list_page(self, session_id, path, page_token=None, page_size=None):
        self.calls.append(("list_page", session_id, path))
        return {"path": path,
                "entries": [
                    {"name": "b.mkv", "path": "/b.mkv", "is_dir": False,
                     "size": 20, "file_id": "fid-b", "entry_id": "eid-b"},
                    {"name": "a.mkv", "path": "/a.mkv", "is_dir": False,
                     "size": 16, "file_id": "fid-a", "entry_id": "eid-a"},
                ],
                "next_page_token": None}

    def stat(self, session_id, path):
        self.calls.append(("stat", session_id, path))
        if path == "/a.mkv":
            return {"name": "a.mkv", "path": "/a.mkv", "file_id": "fid-a",
                    "size": 16, "is_dir": False, "entry_id": "eid-a"}
        return {"name": "", "path": path, "file_id": "", "size": 0,
                "is_dir": True, "entry_id": ""}

    def open_range(self, session_id, path, byte_offset, byte_length=None):
        self.calls.append(("open_range", session_id, path, byte_offset,
                           byte_length))
        blob = b"0123456789abcdef"
        if byte_length is None:
            data = blob[byte_offset:]
        else:
            data = blob[byte_offset:byte_offset + byte_length]
        return {"data_b64": base64.b64encode(data).decode("ascii"),
                "byte_offset": byte_offset, "byte_length": len(data)}

    def resolve_play_target(self, session_id, path):
        self.calls.append(("resolve_play_target", session_id, path))
        return {"url": "https://139.example/down/fid-a",
                "headers": {"User-Agent": "yun139-test"}}

    def resolve_display_asset(self, session_id, path, byte_offset=0,
                              byte_length=None):
        return self.open_range(session_id, path, byte_offset, byte_length)

    def health(self, session_id=None):
        return {"ok": True, "provider": "yun139"}

    def call_extension(self, name, params):
        self.calls.append(("extension", name, params))
        if name == "yun139.resolve_play_variants":
            return {"variants": [
                {"id": "original", "label": "原画", "kind": "original",
                 "url": "https://139.example/secret", "is_default": True}]}
        if name == "yun139.resolve_play_target":
            if params.get("variant_id") == "nope":
                raise ProviderRuntimeError("not_found", "分享播放版本不存在: nope")
            return {"url": "https://139.example/down/fid-a",
                    "headers": {"User-Agent": "yun139-test"}}
        if name == "yun139.create_outer_link_transfer":
            return {"transfer_id": "yun139-transfer-1", "state": "preparing"}
        if name == "yun139.transfer_status":
            return {"transfer_id": params["transfer_id"], "state": "preparing"}
        raise ProviderRuntimeError("not_implemented", f"未知扩展：{name}")


# ---------- handler（直接调，不走 socket） ----------

def test_handler_health_and_sessions():
    handler = Yun139SidecarHandler(upstream=_FakeUpstream())
    assert handler.provider == "yun139"
    assert handler.extensions == YUN139_EXTENSIONS
    health = handler.health(None)
    assert health["ok"] is True
    assert health["provider"] == "yun139"
    handler.session_upsert("s1", {"space": "personal", "pool_id": None})
    assert handler._session_data["s1"]["space"] == "personal"
    handler.session_remove("s1")
    assert "s1" not in handler._session_data


def test_handler_without_upstream_is_not_implemented():
    handler = Yun139SidecarHandler(upstream=None)
    with pytest.raises(ProviderRuntimeError) as excinfo:
        handler.list_page(SimpleNamespace(session_id="s1"), "/", None, None)
    assert excinfo.value.kind == "not_implemented"


def test_handler_list_stat_range_display():
    handler = Yun139SidecarHandler(upstream=_FakeUpstream())
    session = SimpleNamespace(session_id="s1")
    page = handler.list_page(session, "/", None, None)
    assert [e["name"] for e in page["entries"]] == ["a.mkv", "sub"]
    assert page["entries"][0]["path"] == "/a.mkv"
    assert page["entries"][0]["file_id"] == "fid-a"
    stat = handler.stat(session, "/a.mkv")
    assert stat["file_id"] == "fid-a"
    assert stat["path"] == "/a.mkv"
    ranged = handler.open_range(session, "/a.mkv", 2, 4)
    assert base64.b64decode(ranged["data_b64"]) == b"2345"
    assert handler.resolve_display_asset(session, "/a.mkv", 2, 4) == ranged


def test_handler_play_target_strips_secrets():
    handler = Yun139SidecarHandler(upstream=_FakeUpstream())
    session = SimpleNamespace(session_id="s1")
    target = handler.resolve_play_target(session, "/a.mkv")
    assert target["url"] == "https://139.example/down/fid-a"
    assert "Cookie" not in target["headers"]
    assert "Authorization" not in target["headers"]
    assert target["headers"]["User-Agent"] == "yun139-test"


def test_handler_extensions_variants_transfer_mismatch():
    handler = Yun139SidecarHandler(upstream=_FakeUpstream())
    session = SimpleNamespace(session_id="s1")
    out = handler.call_extension(session, "yun139.resolve_play_variants",
                                 {"path": "/a.mkv"})
    assert len(out["variants"]) == 2
    assert all("url" not in v for v in out["variants"])
    target = handler.call_extension(
        session, "yun139.resolve_play_target",
        {"path": "/a.mkv", "variant_id": "original"})
    assert target["url"].startswith("https://139.example/")
    with pytest.raises(ProviderRuntimeError) as excinfo:
        handler.call_extension(session, "yun139.resolve_play_target",
                               {"path": "/a.mkv", "variant_id": "nope"})
    assert excinfo.value.kind == "not_found"
    created = handler.call_extension(
        session, "yun139.create_outer_link_transfer", {"share_ref": "share-1"})
    assert created["state"] == "preparing"
    status = handler.call_extension(
        session, "yun139.transfer_status",
        {"transfer_id": created["transfer_id"]})
    assert status["transfer_id"] == created["transfer_id"]
    with pytest.raises(ProviderRuntimeError) as excinfo:
        handler.call_extension(session, "yun139.transfer_status",
                               {"transfer_id": "missing"})
    assert excinfo.value.kind == "not_found"


# ---------- 宿主 provider（Fake 运行时客户端） ----------

def _provider(**mount_kw):
    client = _FakeRuntimeClient()
    provider = Yun139Mount(_mount(**mount_kw), client=client)
    return provider, client


def test_provider_construction_handshake_and_session(enabled):
    provider, client = _provider()
    assert provider.session_id == "yun139-mount-99"
    assert client.sessions["yun139-mount-99"]["space"] == "personal"
    assert provider.test()["ok"] is True
    provider.close()
    assert "yun139-mount-99" in client.removed


def test_provider_list_and_file_id(enabled):
    provider, _ = _provider()
    entries = provider.list_dir("/")
    assert [e.name for e in entries] == ["a.mkv", "b.mkv"]
    assert entries[0].file_id == "fid-a"
    assert provider._file_id_of("/a.mkv") == "fid-a"


def test_provider_play_target_no_cookie_reattach(enabled):
    provider, client = _provider()
    target = provider.resolve_play_target("/a.mkv")
    assert target.kind == "url"
    assert target.value == "https://139.example/down/fid-a"
    assert "Cookie" not in target.headers
    with pytest.raises(MountError):
        provider._cookie()


def test_provider_variant_target_and_variants(enabled):
    provider, client = _provider()
    target = provider.resolve_play_target("/a.mkv", variant_id="original")
    assert target.value == "https://139.example/down/fid-a"
    ext_calls = [c for c in client.calls if c[0] == "extension"]
    assert ext_calls[0][1] == "yun139.resolve_play_target"
    assert ext_calls[0][2]["variant_id"] == "original"
    assert ext_calls[0][2]["session_id"] == provider.session_id
    variants = provider.resolve_play_variants("/a.mkv")
    assert [v.id for v in variants] == ["original"]
    assert variants[0].url == ""
    with pytest.raises(MountError, match="分享播放版本不存在"):
        provider.resolve_play_target("/a.mkv", variant_id="nope")


def test_provider_range_text_transfer(enabled, monkeypatch):
    provider, client = _provider()
    assert provider.open_range("/a.mkv", 2, 6) == b"2345"
    assert provider.read_text("/a.mkv") == "0123456789abcdef"
    assert provider.request_transfer("share-1")["transfer_id"] == "yun139-transfer-1"
    assert provider.transfer_status("yun139-transfer-1")["state"] == "preparing"
    assert provider.refresh_credentials() is False
    # 限长下限钳制在 1024（env 设 4 会被钳回 1024），故直接 mock 限长函数测守卫。
    monkeypatch.setattr(
        "backend.emby_server.mount_yun139.sidecar_text_max_bytes",
        lambda: 4)
    with pytest.raises(MountError, match="旁挂文本过大"):
        provider.read_text("/a.mkv")


# ---------- 真实 UDS 集成（共享 Server + Client + 139 handler） ----------

def test_real_uds_roundtrip(tmp_path):
    sock = str(tmp_path / "runtime.sock")
    handler = Yun139SidecarHandler(upstream=_FakeUpstream())
    server = ProviderRuntimeServer(handler, sock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 服务线程绑定是异步的：等 socket 文件出现再握手，避免首调用竞态。
        import time as _time
        deadline = _time.time() + 5.0
        while not os.path.exists(sock):
            if _time.time() > deadline:
                raise AssertionError("sidecar UDS 未在 5 秒内绑好")
            _time.sleep(0.05)
        client = ProviderRuntimeClient("yun139", sock)
        manifest = client.handshake(YUN139_EXTENSIONS)
        assert manifest["provider"] == "yun139"
        # health 无会话也可探活；其余能力须先建会话。
        assert client.health()["ok"] is True
        with pytest.raises(ProviderRuntimeError) as excinfo:
            client.list_page("no-session", "/")
        assert excinfo.value.kind == "session_unknown"
        client.session_upsert("s1", {"space": "personal", "pool_id": None})
        page = client.list_page("s1", "/")
        assert [e["name"] for e in page["entries"]] == ["a.mkv", "sub"]
        assert decode_range_result(client.open_range("s1", "/a.mkv", 2, 4)) == b"2345"
        variants = client.call_extension(
            "yun139.resolve_play_variants", {"session_id": "s1", "path": "/a.mkv"})
        assert len(variants["variants"]) == 2
        assert all("url" not in v for v in variants["variants"])
        created = client.call_extension(
            "yun139.create_outer_link_transfer",
            {"session_id": "s1", "share_ref": "share-1"})
        assert created["state"] == "preparing"
        # 错 provider 握手即拒绝（manifest provider 不符）。
        wrong = ProviderRuntimeClient("p115", sock)
        with pytest.raises(ProviderRuntimeError):
            wrong.handshake()
    finally:
        server.stop()
        thread.join(timeout=5)
