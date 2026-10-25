"""#13 PR-3＋PR-4：认亲层升级与采样哈希后台队列。

PR-3 覆盖：
- content_hash 层：同 hash+algo+size 认领；开关关时 inert；algo 不成对不认；
  size 不等不认；同 hash 多行歧义不认；episode 按集号配对；file_id 换了
  但内容没变（重传/转存）仍认领；已认领行不被后层再抢；采样哈希须叠加
  归一化标题一致。
- 指纹层：开关开时行侧读 scan_title/scan_year 列（列值与旧路径解析不一致
  时以列为准）；归一化收敛 dedup.normalize_name（旧 _norm 会漏的标点差
  现在能认）；movie 补版本签名（2160p/1080p 不互认）；开关关时行为与基线
  逐字一致（上述三项全不生效）。
- 115 fid 泛化：walk_media 仅开关开时透传 fid；关时 file_id 为空；
  sha1 仍缺位（PR-2 结论不变）。
- transfer115：fid 绝不拿父目录 cid 回落冒充（无 fid 时为空串）。

PR-4 覆盖：
- 采样哈希：同内容同采样、不同内容不同采样；只填空（已有 md5 不覆盖）；
  mount:// 行与 .strm 行不碰；缺失文件跳过；每轮条数上限；identity_json
  合并式追加（旧键保留、解析失败原样不动）。
- 管理后台展示页不在本 PR：按分工归管理后台专线（本文件不测管理页）。

测试说明：仓库测试无 pytest-asyncio；本文件全部是同步用例，无需 asyncio。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.emby_server import models as em
from backend.emby_server import mounts as mnt
from backend.emby_server import scanner as scanner_mod
from backend.emby_server import transfer115
from backend.emby_server.mounts import Pan115Mount
from backend.emby_server.sample_hash_worker import (
    SAMPLE_ALGO,
    compute_sample_hash,
    run_once,
)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    em.Base.metadata.create_all(engine, tables=[
        em.Library.__table__, em.MediaItem.__table__])
    session = sessionmaker(bind=engine)()
    lib = em.Library(guid="lib-1", name="lib", collection_type="movies")
    session.add(lib)
    session.commit()
    yield session, lib
    session.close()


def _row(session, lib, **kwargs):
    base = {"guid": "g", "library_id": lib.id, "item_type": "movie",
            "name": "x", "file_path": "/movies/X (2020).mkv"}
    base.update(kwargs)
    row = em.MediaItem(**base)
    session.add(row)
    session.commit()
    return row


def _pending(path, item_type="movie", size=1000, file_id="",
             content_hash="", content_hash_algo="", parsed=None,
             series_name=None):
    if parsed is None:
        lib_type = "movies" if item_type == "movie" else "tvshows"
        parsed = scanner_mod.parse_media_filename(path, lib_type)
    return SimpleNamespace(
        scan_file=SimpleNamespace(
            stored_path=path, size=size, file_id=file_id,
            content_hash=content_hash, content_hash_algo=content_hash_algo),
        guid=scanner_mod.item_guid(path),
        parsed=parsed, item_type=item_type, series_name=series_name,
        item=None, renamed=False)


def _ctx(lib):
    return SimpleNamespace(seen_guids=set(), lib_id=lib.id, stats={})


# ---------------- PR-3：content_hash 层 ----------------

def test_hash_layer_claims_same_hash_algo_size(db):
    session, lib = db
    row = _row(session, lib, guid=scanner_mod.item_guid("/movies/Old (2020).mkv"),
               file_path="/movies/Old (2020).mkv", size=1000,
               content_hash="md5aaa", content_hash_algo="md5")
    p = _pending("/movies/New (2020).mkv", size=1000,
                 content_hash="md5aaa", content_hash_algo="md5")
    ctx = _ctx(lib)
    scanner_mod._match_renames_by_content_hash(session, [p], {}, ctx, enabled=True)
    assert p.item is not None and p.item.id == row.id and p.renamed is True
    assert ctx.stats.get("renamed_by_content_hash") == 1


def test_hash_layer_disabled_is_inert(db):
    session, lib = db
    row = _row(session, lib, guid=scanner_mod.item_guid("/movies/Old (2020).mkv"),
               file_path="/movies/Old (2020).mkv", size=1000,
               content_hash="md5aaa", content_hash_algo="md5")
    p = _pending("/movies/New (2020).mkv", size=1000,
                 content_hash="md5aaa", content_hash_algo="md5")
    scanner_mod._match_renames_by_content_hash(
        session, [p], {}, _ctx(lib), enabled=False)
    assert p.item is None and p.renamed is False


def test_hash_layer_requires_algo_pair_and_size(db):
    session, lib = db
    _row(session, lib, guid=scanner_mod.item_guid("/movies/Old (2020).mkv"),
         file_path="/movies/Old (2020).mkv", size=1000,
         content_hash="samevalue", content_hash_algo="md5")
    # algo 不成对：pending 是 sha1、同串值也不认
    p1 = _pending("/movies/New1 (2020).mkv", size=1000,
                  content_hash="samevalue", content_hash_algo="sha1")
    scanner_mod._match_renames_by_content_hash(
        session, [p1], {}, _ctx(lib), enabled=True)
    assert p1.item is None
    # size 不等不认
    p2 = _pending("/movies/New2 (2020).mkv", size=1001,
                  content_hash="samevalue", content_hash_algo="md5")
    scanner_mod._match_renames_by_content_hash(
        session, [p2], {}, _ctx(lib), enabled=True)
    assert p2.item is None


def test_hash_layer_ambiguity_refuses_to_claim(db):
    session, lib = db
    _row(session, lib, guid=scanner_mod.item_guid("/movies/A (2020).mkv"),
         file_path="/movies/A (2020).mkv", size=1000,
         content_hash="md5dup", content_hash_algo="md5")
    _row(session, lib, guid=scanner_mod.item_guid("/movies/B (2020).mkv"),
         file_path="/movies/B (2020).mkv", size=1000,
         content_hash="md5dup", content_hash_algo="md5")
    p = _pending("/movies/C (2020).mkv", size=1000,
                 content_hash="md5dup", content_hash_algo="md5")
    scanner_mod._match_renames_by_content_hash(
        session, [p], {}, _ctx(lib), enabled=True)
    assert p.item is None, "同 hash 两行是歧义，宁可错分不可错认"


def test_hash_layer_episode_pairs_by_number(db):
    session, lib = db
    lib.collection_type = "tvshows"
    session.commit()
    r5 = _row(session, lib, guid=scanner_mod.item_guid("/tv/Show/Show S01E05.mkv"),
              item_type="episode", file_path="/tv/Show/Show S01E05.mkv",
              size=1000, content_hash="md5ep", content_hash_algo="md5",
              season_number=1, episode_number=5)
    r6 = _row(session, lib, guid=scanner_mod.item_guid("/tv/Show/Show S01E06.mkv"),
              item_type="episode", file_path="/tv/Show/Show S01E06.mkv",
              size=1000, content_hash="md5ep", content_hash_algo="md5",
              season_number=1, episode_number=6)
    p5 = _pending("/tv/Show/Show S01E05.mkv", item_type="episode", size=1000,
                  content_hash="md5ep", content_hash_algo="md5",
                  series_name="Show")
    p6 = _pending("/tv/Show/Show S01E06.mkv", item_type="episode", size=1000,
                  content_hash="md5ep", content_hash_algo="md5",
                  series_name="Show")
    # pending 路径与行路径同 guid 时 guid 层已认领，本测试只验 hash 层配对：
    # 把 pending 路径改到新目录，使 guid 不命中、走 hash 层。
    p5.scan_file.stored_path = "/tv/ShowNew/Show S01E05.mkv"
    p5.guid = scanner_mod.item_guid(p5.scan_file.stored_path)
    p6.scan_file.stored_path = "/tv/ShowNew/Show S01E06.mkv"
    p6.guid = scanner_mod.item_guid(p6.scan_file.stored_path)
    scanner_mod._match_renames_by_content_hash(
        session, [p5, p6], {}, _ctx(lib), enabled=True)
    assert p5.item is not None and p5.item.id == r5.id
    assert p6.item is not None and p6.item.id == r6.id


def test_hash_layer_claims_when_file_id_changed_but_content_same(db):
    session, lib = db
    row = _row(session, lib, guid=scanner_mod.item_guid("/movies/Old (2020).mkv"),
               file_path="/movies/Old (2020).mkv", size=1000,
               drive_file_id="fid-old",
               content_hash="md5re", content_hash_algo="md5")
    # file_id 层认不上（fid 已换），hash 层把旧行接回来
    p = _pending("/movies/New (2020).mkv", size=1000, file_id="fid-new",
                 content_hash="md5re", content_hash_algo="md5")
    scanner_mod._match_renames_by_file_id(session, [p], {}, _ctx(lib))
    assert p.item is None
    scanner_mod._match_renames_by_content_hash(
        session, [p], {}, _ctx(lib), enabled=True)
    assert p.item is not None and p.item.id == row.id


def test_hash_layer_respects_claimed_rows(db):
    session, lib = db
    row = _row(session, lib, guid=scanner_mod.item_guid("/movies/Old (2020).mkv"),
               file_path="/movies/Old (2020).mkv", size=1000,
               drive_file_id="fid-1",
               content_hash="md5c", content_hash_algo="md5")
    p_a = _pending("/movies/A (2020).mkv", size=1000, file_id="fid-1",
                   content_hash="md5c", content_hash_algo="md5")
    scanner_mod._match_renames_by_file_id(session, [p_a], {}, _ctx(lib))
    assert p_a.item is not None and p_a.item.id == row.id
    # 另一文件同 hash：行已被 file_id 层认走，hash 层不得再抢
    p_b = _pending("/movies/B (2020).mkv", size=1000,
                   content_hash="md5c", content_hash_algo="md5")
    scanner_mod._match_renames_by_content_hash(
        session, [p_b], {}, _ctx(lib), claimed_row_ids={row.id}, enabled=True)
    assert p_b.item is None


def test_sample_hash_requires_matching_normalized_title(db):
    session, lib = db
    row = _row(session, lib, guid=scanner_mod.item_guid("/movies/Old (2020).mkv"),
               file_path="/movies/Old (2020).mkv", size=1000,
               content_hash="samplehash1", content_hash_algo=SAMPLE_ALGO,
               scan_title="Movie", scan_year=2020)
    # 标题一致 → 认领
    p_ok = _pending("/movies/Movie (2020).mkv", size=1000,
                    content_hash="samplehash1", content_hash_algo=SAMPLE_ALGO)
    scanner_mod._match_renames_by_content_hash(
        session, [p_ok], {}, _ctx(lib), enabled=True)
    assert p_ok.item is not None and p_ok.item.id == row.id
    # 标题不一致 → 采样是弱证据，不认领
    p_bad = _pending("/movies/Other (2020).mkv", size=1000,
                     content_hash="samplehash1", content_hash_algo=SAMPLE_ALGO)
    scanner_mod._match_renames_by_content_hash(
        session, [p_bad], {}, _ctx(lib), enabled=True)
    assert p_bad.item is None


# ---------------- PR-3：指纹层 ----------------

def test_fingerprint_reads_scan_columns_when_enabled(db):
    session, lib = db
    # 旧路径解析出 AAA，但入库快照 scan_title 是 BBB；pending 解析出 BBB。
    row = _row(session, lib, guid=scanner_mod.item_guid("/movies/AAA (2020).mkv"),
               file_path="/movies/AAA (2020).mkv", size=1000,
               scan_title="BBB", scan_year=2020)
    p = _pending("/movies/BBB (2020).mkv", size=1000)
    scanner_mod._match_renames_by_fingerprint(
        session, [p], {}, _ctx(lib), enabled=True)
    assert p.item is not None and p.item.id == row.id


def test_fingerprint_ignores_scan_columns_when_disabled(db):
    session, lib = db
    _row(session, lib, guid=scanner_mod.item_guid("/movies/AAA (2020).mkv"),
         file_path="/movies/AAA (2020).mkv", size=1000,
         scan_title="BBB", scan_year=2020)
    p = _pending("/movies/BBB (2020).mkv", size=1000)
    scanner_mod._match_renames_by_fingerprint(
        session, [p], {}, _ctx(lib), enabled=False)
    assert p.item is None, "开关关时行侧仍重 parse 旧路径（AAA≠BBB），不认领"


def test_fingerprint_normalization_converges_to_dedup(db):
    session, lib = db
    # 旧路径含全角冒号：旧局部 _norm 保留冒号（a：b），dedup 归一去标点（ab）。
    _row(session, lib, guid=scanner_mod.item_guid("/movies/A：B (2020).mkv"),
         file_path="/movies/A：B (2020).mkv", size=1000)
    p = _pending("/movies/A B (2020).mkv", size=1000)
    scanner_mod._match_renames_by_fingerprint(
        session, [p], {}, _ctx(lib), enabled=True)
    assert p.item is not None, "开时归一化收敛到 dedup，两侧都算 ab"
    p2 = _pending("/movies/A B (2020).mkv", size=1000)
    scanner_mod._match_renames_by_fingerprint(
        session, [p2], {}, _ctx(lib), enabled=False)
    assert p2.item is None, "关时仍是旧局部 _norm（a：b≠ab），不认领"


def test_fingerprint_movie_version_signature(db):
    session, lib = db
    _row(session, lib,
         guid=scanner_mod.item_guid("/movies/Movie (2020) 1080p WEB-DL.mkv"),
         file_path="/movies/Movie (2020) 1080p WEB-DL.mkv", size=1000)
    # 开时：2160p pending 不认 1080p 的行（同片多版本不互认）
    p_new = _pending("/movies/Movie (2020) 2160p WEB-DL.mkv", size=1000)
    scanner_mod._match_renames_by_fingerprint(
        session, [p_new], {}, _ctx(lib), enabled=True)
    assert p_new.item is None
    # 开时：同版本（1080p）改名认领
    p_same = _pending("/movies/Movie (2020) 1080p WEB-DL.mkv", size=1000)
    p_same.scan_file.stored_path = "/movies/Other/Movie (2020) 1080p WEB-DL.mkv"
    p_same.guid = scanner_mod.item_guid(p_same.scan_file.stored_path)
    scanner_mod._match_renames_by_fingerprint(
        session, [p_same], {}, _ctx(lib), enabled=True)
    assert p_same.item is not None
    # 关时：movie 无版本签分量，2160p pending 会认 1080p 的行（基线行为）
    p_old = _pending("/movies/Movie (2020) 2160p WEB-DL.mkv", size=1000)
    scanner_mod._match_renames_by_fingerprint(
        session, [p_old], {}, _ctx(lib), enabled=False)
    assert p_old.item is not None


# ---------------- PR-3：115 fid 泛化 ----------------

def _pan115_provider(monkeypatch, raw_items):
    provider = Pan115Mount(SimpleNamespace(path="115:/", config={}),
                           cookie_override="COOKIE-TEST")
    monkeypatch.setattr(provider, "_cid_of", lambda rel: "0")
    monkeypatch.setattr(provider, "_raw_list",
                        lambda cid: list(raw_items) if cid == "0" else [])
    return provider


def test_115_walk_media_fid_gated_by_switch(monkeypatch):
    raw = [{"name": "a.mkv", "is_dir": False, "size": 10, "fid": "fid-115-1",
            "cid": "c1", "pickcode": "pc1"}]
    provider = _pan115_provider(monkeypatch, raw)
    monkeypatch.setattr(mnt, "PAN115_FID_ENABLED", True)
    files = list(provider.walk_media(root="/"))
    assert len(files) == 1
    assert files[0].file_id == "fid-115-1"
    # sha1 仍缺位：fid 泛化不改变 PR-2 的缺位结论
    assert files[0].content_hash == "" and files[0].content_hash_algo == ""
    monkeypatch.setattr(mnt, "PAN115_FID_ENABLED", False)
    files_off = list(provider.walk_media(root="/"))
    assert files_off[0].file_id == ""


def test_transfer115_fid_never_falls_back_to_cid(monkeypatch):
    client = transfer115.Pan115Client("COOKIE-TEST")

    def fake_call(method, url, params=None, data=None):
        return {"state": True, "data": [
            {"n": "a.mkv", "s": 10, "pc": "pc1", "cid": "parent-cid"},
            {"n": "b.mkv", "s": 20, "pc": "pc2", "fid": "fid-b",
             "cid": "parent-cid"},
        ]}

    monkeypatch.setattr(client, "_call", fake_call)
    entries = client.list_dir("0")
    by_name = {e["name"]: e for e in entries}
    assert by_name["a.mkv"]["fid"] == "", "无 fid 时为空串，绝不拿父 cid 冒充"
    assert by_name["b.mkv"]["fid"] == "fid-b"


# ---------------- PR-4：采样哈希 ----------------

def test_sample_hash_same_content_same_digest_different_differs(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    data = os.urandom(3 * 1024 * 1024 + 123)
    a.write_bytes(data)
    b.write_bytes(data)
    c.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
    ha, hb, hc = (compute_sample_hash(str(p)) for p in (a, b, c))
    assert ha is not None and len(ha) == 32
    assert ha == hb
    assert ha != hc
    assert compute_sample_hash(str(tmp_path / "missing.bin")) is None


def test_sample_worker_fills_only_empty_local_rows(db, tmp_path):
    session, lib = db
    local = tmp_path / "movie.mkv"
    local.write_bytes(b"x" * 100000)
    row_empty = _row(session, lib,
                     guid=scanner_mod.item_guid(str(local)),
                     file_path=str(local), size=100000)
    row_md5 = _row(session, lib,
                   guid=scanner_mod.item_guid("/movies/HasMd5 (2020).mkv"),
                   file_path=str(tmp_path / "hasmd5.mkv"), size=100000,
                   content_hash="md5keep", content_hash_algo="md5")
    (tmp_path / "hasmd5.mkv").write_bytes(b"y" * 100000)
    row_mount = _row(session, lib,
                     guid=scanner_mod.item_guid("mount://1/a.mkv"),
                     file_path="mount://1/a.mkv", size=100000)
    row_strm = _row(session, lib,
                    guid=scanner_mod.item_guid(str(tmp_path / "s.strm")),
                    file_path=str(tmp_path / "s.strm"), size=10,
                    container="strm")
    row_missing = _row(session, lib,
                       guid=scanner_mod.item_guid(str(tmp_path / "gone.mkv")),
                       file_path=str(tmp_path / "gone.mkv"), size=100000)
    stats = run_once(session, max_files=50, delay_sec=0)
    assert stats["filled"] == 1
    session.refresh(row_empty)
    assert row_empty.content_hash is not None
    assert row_empty.content_hash_algo == SAMPLE_ALGO
    assert len(row_empty.content_hash) == 32
    assert "sample-sha256" in (row_empty.identity_json or "")
    session.refresh(row_md5)
    assert row_md5.content_hash == "md5keep", "已有 md5 绝不被采样覆盖"
    assert row_md5.content_hash_algo == "md5"
    session.refresh(row_mount)
    assert row_mount.content_hash is None, "mount:// 行不采样"
    session.refresh(row_strm)
    assert row_strm.content_hash is None, ".strm 行不采样"
    session.refresh(row_missing)
    assert row_missing.content_hash is None


def test_sample_worker_respects_max_files_and_merges_json(db, tmp_path):
    session, lib = db
    rows = []
    for i in range(3):
        p = tmp_path / f"m{i}.mkv"
        p.write_bytes(bytes([i + 1]) * 50000)
        rows.append(_row(session, lib,
                         guid=scanner_mod.item_guid(str(p)),
                         file_path=str(p), size=50000,
                         identity_json='{"etag": "e1"}'))
    stats = run_once(session, max_files=1, delay_sec=0)
    assert stats["filled"] == 1
    session.refresh(rows[0])
    assert rows[0].content_hash_algo == SAMPLE_ALGO
    assert '"etag": "e1"' in (rows[0].identity_json or ""), "旧证据键保留"
    session.refresh(rows[1])
    assert rows[1].content_hash is None, "上限 1 时只填一行"
