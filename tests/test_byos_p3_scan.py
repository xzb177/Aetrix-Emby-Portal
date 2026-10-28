"""BYOS P3（按绑定选源扫描入库）。

钉死的范围（多一件不做）：
- 按绑定选源：一次扫描只认一个本人 active 绑定，用该绑定自己的
  凭据列举；失败响亮、不回落共享凭据、不回落其他绑定；
- 入库走现行扫描体（定向模式，不清理）与身份锚列（Drive 写
  provider_file_id、115 留空）；
- 不碰：播放、115 sidecar、139、阶段 8、管理后台页；认亲
  （content_hash 查库）是 #13 PR-3 的地盘，本文件只断言透传，
  不断言任何查库去重行为。

网络与上游全部用桩替身，不碰真实 Drive / 115。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import byos_scan
from backend.emby_server import drive_oauth
from backend.emby_server import mounts as mount_lib
from backend.emby_server import scanner
from backend.emby_server import transfer115
from backend.emby_server import user_bindings as ub
from backend.emby_server import models as em
from backend.emby_server.upstream_sources import seal_secret


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


def _make_binding(db, user_id, provider, label, sealed, status="active"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential=sealed, key_id="fernet-v1", status=status)
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label=label,
        credential_id=cred.id, status=status)
    db.add(binding)
    db.flush()
    return binding


def _make_library(db, name, collection_type="movies"):
    lib = em.Library(guid=f"lib-{name}", name=name,
                     collection_type=collection_type, paths="")
    db.add(lib)
    db.flush()
    return lib


class _StubProvider:
    """列举桩：walk_media 产出给定 MountFile，调用次数可数。"""

    def __init__(self, files):
        self.files = list(files)
        self.walk_calls = 0
        self.local_root = None

    def walk_media(self, root="/"):
        self.walk_calls += 1
        yield from self.files

    def list_dir(self, rel="/"):
        return []

    def resolve_final(self, rel):
        return mount_lib.PlayTarget(kind="url", value="https://example.invalid/x.mp4",
                                    headers={})


def _mf(rel, name, file_id="", md5=""):
    return mount_lib.MountFile(rel=rel, name=name, size=123,
                               file_id=file_id, content_hash=md5,
                               content_hash_algo="md5" if md5 else "")


def _enable(monkeypatch):
    monkeypatch.setenv("BYOS_SCAN_ENABLED", "1")


# ---------------------------------------------------------------- 门与归属

def test_gate_disabled_refuses_without_touching_provider(db, monkeypatch):
    monkeypatch.setenv("BYOS_SCAN_ENABLED", "0")
    lib = _make_library(db, "g")
    binding = _make_binding(db, 7, "pan115", "a",
                            seal_secret("cookie-A"))
    stub = _StubProvider([_mf("/Film.mkv", "Film.mkv")])
    with pytest.raises(byos_scan.BindingScanError) as exc:
        byos_scan.scan_binding(db, 7, binding.id, lib.id, provider=stub)
    assert exc.value.kind == "disabled"
    assert stub.walk_calls == 0


def test_other_users_binding_is_not_found(db, monkeypatch):
    _enable(monkeypatch)
    lib = _make_library(db, "g")
    binding = _make_binding(db, 7, "pan115", "a",
                            seal_secret("cookie-A"))
    stub = _StubProvider([])
    with pytest.raises(byos_scan.BindingScanError) as exc:
        byos_scan.scan_binding(db, 8, binding.id, lib.id, provider=stub)
    assert exc.value.kind == "not_found"
    assert stub.walk_calls == 0


def test_inactive_binding_refuses_by_status(db, monkeypatch):
    _enable(monkeypatch)
    lib = _make_library(db, "g")
    binding = _make_binding(db, 7, "pan115", "a",
                            seal_secret("cookie-A"), status="expired")
    stub = _StubProvider([])
    with pytest.raises(byos_scan.BindingScanError) as exc:
        byos_scan.scan_binding(db, 7, binding.id, lib.id, provider=stub)
    assert exc.value.kind == "expired"
    assert stub.walk_calls == 0


# ---------------------------------------------------------------- 形态转换

def test_iter_binding_scan_files_mapping():
    provider = _StubProvider([
        _mf("/Movie 2024.mkv", "Movie 2024.mkv", file_id="fid-1", md5="abc"),
        _mf("/Show/Show S01E01.mkv", "Show S01E01.mkv"),
    ])
    files = list(byos_scan.iter_binding_scan_files(provider, 42))
    assert files[0].stored_path == "byos://42/Movie 2024.mkv"
    assert files[0].file_id == "fid-1"
    assert files[0].content_hash == "abc"
    assert files[0].source_row_id is None
    assert files[0].mount_id is None
    assert files[1].stored_path == "byos://42/Show/Show S01E01.mkv"
    assert files[1].file_id == ""
    assert byos_scan.is_byos_path(files[0].stored_path)
    assert not byos_scan.is_byos_path("mount://3/Show/Show S01E01.mkv")


def test_scanner_prefix_branches_are_binding_scoped():
    a = scanner.ScanFile(stored_path="byos://1/Show/Show S01E01.mkv",
                         name="Show S01E01.mkv", local_dir=None,
                         dir_rel="/Show", mount_id=None)
    b = scanner.ScanFile(stored_path="byos://2/Show/Show S01E01.mkv",
                         name="Show S01E01.mkv", local_dir=None,
                         dir_rel="/Show", mount_id=None)
    assert scanner._dir_key_of(a) != scanner._dir_key_of(b)
    assert scanner._series_guid_of(a) != scanner._series_guid_of(b)
    # 非绑定路径行为逐字不变
    c = scanner.ScanFile(stored_path="mount://3/Show/Show S01E01.mkv",
                         name="Show S01E01.mkv", local_dir=None,
                         dir_rel="/Show", mount_id=3)
    assert scanner._dir_key_of(c) == "mount://3/Show"


def test_iter_scan_sources_override_ignores_library_sources(db):
    lib = _make_library(db, "g")
    snap = scanner.LibrarySnapshot.of(lib)
    stub_files = [_mf("/Film.mkv", "Film.mkv")]
    got = list(scanner.iter_scan_sources(
        snap, lib, db, [], source_override=[("BYOS 绑定 #9（drive）", stub_files)]))
    assert len(got) == 1
    assert got[0][0] == "BYOS 绑定 #9（drive）"
    assert got[0][1] == stub_files


# ---------------------------------------------------------------- Drive 列举器

class _FakeDriveHttp:
    """Drive files.list 桩：root 下一文件夹 + 一电影；文件夹下一集。"""

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, url, token, params):
        assert token == "tok-user-7"
        self.calls.append(params)
        q = params.get("q") or ""
        if self.status != 200:
            raise mount_lib.MountAuthError("Drive 令牌失效")
        if "'root' in parents" in q:
            return {"files": [
                {"id": "dir-1", "name": "Show",
                 "mimeType": "application/vnd.google-apps.folder"},
                {"id": "fid-movie", "name": "Movie 2024.mkv",
                 "mimeType": "video/x-matroska", "size": "456",
                 "md5Checksum": "md5movie",
                 "modifiedTime": "2026-10-24T00:00:00.000Z",
                 "etag": "etag1", "headRevisionId": "rev1"},
            ]}
        if "'dir-1' in parents" in q:
            return {"files": [
                {"id": "fid-ep1", "name": "Show S01E01.mkv",
                 "mimeType": "video/x-matroska", "size": "789",
                 "modifiedTime": "2026-10-24T01:00:00.000Z"},
            ]}
        return {"files": []}


def test_user_drive_enumerator_walk_and_target():
    enum = byos_scan.UserDriveEnumerator("tok-user-7",
                                         http_get=_FakeDriveHttp())
    files = list(enum.walk_media(root="/"))
    rels = sorted(f.rel for f in files)
    assert rels == ["/Movie 2024.mkv", "/Show/Show S01E01.mkv"]
    movie = next(f for f in files if f.rel == "/Movie 2024.mkv")
    assert movie.file_id == "fid-movie"
    assert movie.content_hash == "md5movie"
    assert movie.content_hash_algo == "md5"
    assert movie.revision == "rev1"
    target = enum.resolve_final("/Movie 2024.mkv")
    assert target.value == (
        "https://www.googleapis.com/drive/v3/files/fid-movie?alt=media")
    assert target.headers == {"Authorization": "Bearer tok-user-7"}
    assert "tok-user-7" not in target.value


def test_user_drive_enumerator_auth_failure_is_mount_auth_error():
    enum = byos_scan.UserDriveEnumerator(
        "tok-user-7", http_get=_FakeDriveHttp(status=401))
    with pytest.raises(mount_lib.MountAuthError):
        list(enum.walk_media(root="/"))


# ---------------------------------------------------------------- 凭据分账

def test_drive_token_uses_the_binding_not_the_earliest(db):
    import time
    far = int(time.time()) + 3600
    first = _make_binding(
        db, 7, "drive", "first",
        seal_secret(drive_oauth.encode_drive_token("tok-first", "ref-first", far)))
    second = _make_binding(
        db, 7, "drive", "second",
        seal_secret(drive_oauth.encode_drive_token("tok-second", "ref-second", far)))
    assert first.id < second.id
    assert drive_oauth.get_binding_drive_access_token(db, second) == "tok-second"


# ---------------------------------------------------------------- file-only 绑定禁整盘扫描

def test_file_only_binding_refuses_whole_drive_scan(db):
    # 门在取令牌之前：凭据故意留空（取令牌必失败），仍应先报
    # unsupported，证明没跑列举、没碰令牌、更不会跑定向清理。
    binding = _make_binding(db, 7, "drive", "file-only", seal_secret(""))
    binding.granted_scopes = drive_oauth.DRIVE_FILE_SCOPE
    db.commit()
    with pytest.raises(byos_scan.BindingScanError) as exc:
        byos_scan.binding_provider(db, binding)
    assert exc.value.kind == "unsupported"


def test_readonly_binding_still_scans(db):
    import time
    far = int(time.time()) + 3600
    binding = _make_binding(
        db, 7, "drive", "full",
        seal_secret(drive_oauth.encode_drive_token("tok-full", "ref-full", far)))
    binding.granted_scopes = (
        drive_oauth.DRIVE_READONLY_SCOPE + " " + drive_oauth.DRIVE_FILE_SCOPE)
    db.commit()
    provider = byos_scan.binding_provider(db, binding)
    assert isinstance(provider, byos_scan.UserDriveEnumerator)


def test_legacy_none_scope_binding_still_scans(db):
    # #1348 之前建的老绑定无实授记录（当时只授 readonly），按老口径放行。
    import time
    far = int(time.time()) + 3600
    binding = _make_binding(
        db, 7, "drive", "legacy",
        seal_secret(drive_oauth.encode_drive_token("tok-leg", "ref-leg", far)))
    assert binding.granted_scopes is None
    provider = byos_scan.binding_provider(db, binding)
    assert isinstance(provider, byos_scan.UserDriveEnumerator)


def test_pan115_provider_uses_only_the_binding_cookie(db, monkeypatch):
    seen = {}

    class _Stub115:
        def __init__(self, cookie, ua=""):
            seen["cookie"] = cookie

        def check(self):
            return {"ok": True}

        def list_dir(self, cid="0", limit=200):
            return [{"name": "Film.mkv", "is_dir": False,
                     "pickcode": "PC9", "cid": "c9", "size": 10}]

    monkeypatch.setattr(transfer115, "Pan115Client", _Stub115)
    binding = _make_binding(db, 7, "pan115", "mine",
                            seal_secret("cookie-mine"))
    provider = byos_scan.binding_provider(db, binding)
    assert provider._cookie_override == "cookie-mine"
    assert provider._byos_no_cache is True
    files = list(byos_scan.iter_binding_scan_files(provider, binding.id))
    assert seen["cookie"] == "cookie-mine"
    assert files[0].stored_path == f"byos://{binding.id}/Film.mkv"
    assert files[0].file_id == ""  # 115 无稳定 file_id，留空绝不猜


# ---------------------------------------------------------------- 整轮扫描

def test_scan_binding_two_users_same_file_are_two_items(db, monkeypatch):
    _enable(monkeypatch)
    lib = _make_library(db, "movies")
    b7 = _make_binding(db, 7, "drive", "u7",
                       seal_secret(drive_oauth.encode_drive_token(
                           "tok-7", "ref-7", 9999999999)))
    b8 = _make_binding(db, 8, "drive", "u8",
                       seal_secret(drive_oauth.encode_drive_token(
                           "tok-8", "ref-8", 9999999999)))
    stub7 = _StubProvider([_mf("/Movie 2024.mkv", "Movie 2024.mkv",
                               file_id="shared-fid", md5="m1")])
    stub8 = _StubProvider([_mf("/Movie 2024.mkv", "Movie 2024.mkv",
                               file_id="shared-fid", md5="m1")])
    out7 = byos_scan.scan_binding(db, 7, b7.id, lib.id, provider=stub7)
    out8 = byos_scan.scan_binding(db, 8, b8.id, lib.id, provider=stub8)
    assert out7["stats"]["added"] >= 1
    assert out8["stats"]["added"] >= 1
    items = db.query(em.MediaItem).filter(
        em.MediaItem.file_path.like("byos://%")).all()
    paths = sorted(i.file_path for i in items)
    assert paths == [f"byos://{b7.id}/Movie 2024.mkv",
                     f"byos://{b8.id}/Movie 2024.mkv"]
    guids = {i.guid for i in items}
    assert len(guids) == 2
    drive_items = [i for i in items if i.provider_file_id == "shared-fid"]
    assert len(drive_items) == 2
    # 同一绑定重扫：新增为 0，不重复建条目
    again = byos_scan.scan_binding(db, 7, b7.id, lib.id, provider=stub7)
    assert again["stats"]["added"] == 0
    assert db.query(em.MediaItem).filter(
        em.MediaItem.file_path.like("byos://%")).count() == 2


def test_scan_binding_tvshow_series_guids_are_binding_scoped(db, monkeypatch):
    _enable(monkeypatch)
    lib = _make_library(db, "shows", collection_type="tvshows")
    b7 = _make_binding(db, 7, "pan115", "u7", seal_secret("cookie-7"))
    b8 = _make_binding(db, 8, "pan115", "u8", seal_secret("cookie-8"))
    files = [_mf("/Show/Show S01E01.mkv", "Show S01E01.mkv"),
             _mf("/Show/Show S01E02.mkv", "Show S01E02.mkv")]
    byos_scan.scan_binding(db, 7, b7.id, lib.id,
                           provider=_StubProvider(list(files)))
    byos_scan.scan_binding(db, 8, b8.id, lib.id,
                           provider=_StubProvider(list(files)))
    # series 行本身没有 file_path（父行），按 guid 与归属断言：两绑定
    # 各有一行 series、guid 不同；各绑定的两集挂在各自的 series 下。
    series = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "series").all()
    assert len(series) == 2
    assert len({s.guid for s in series}) == 2
    episodes = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").all()
    episodes = [e for e in episodes if (e.file_path or "").startswith("byos://")]
    assert len(episodes) == 4
    eps7 = {e.series_id for e in episodes
            if e.file_path.startswith(f"byos://{b7.id}/")}
    eps8 = {e.series_id for e in episodes
            if e.file_path.startswith(f"byos://{b8.id}/")}
    assert len(eps7) == 1 and len(eps8) == 1 and eps7 != eps8
