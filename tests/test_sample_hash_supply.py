"""采样哈希供给接线：文件大小变了，旧采样哈希清回 pending。

供给口径（按代码实测结论）：
- pending 是派生态：content_hash 为空 + 本地路径 + movie/episode + 非 .strm；
  扫描入库的新本地行天然就是 pending（列默认 NULL），识别未匹配行同理；
  _apply_scan_identity 没给哈希时不动已有值，enrich 链不碰 content_hash。
- 本 PR 只接一环：扫描写库循环里文件大小变了，旧采样哈希（algo 以
  sample- 开头）已是过期证据（采样摘要本身含 size），清回 pending 供
  worker 重算；源给的 md5/sha1 绝不动；worker 计算本体与限速不动。

测试说明：仓库测试无 pytest-asyncio；本文件全部是同步用例，无需 asyncio。
"""

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.emby_server import models as em
from backend.emby_server import scanner as scanner_mod


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
            "name": "x", "file_path": "/movies/X (2020).mkv",
            "enrich_status": "done"}
    base.update(kwargs)
    row = em.MediaItem(**base)
    session.add(row)
    session.commit()
    return row


def _scan_file(size, path="/movies/X (2020).mkv"):
    return SimpleNamespace(size=size, stored_path=path,
                           content_hash="", content_hash_algo="",
                           etag="", revision="")


def test_size_change_resets_stale_sample_hash(db):
    session, lib = db
    row = _row(session, lib, size=100, content_hash="aabbccddeeff00112233",
               content_hash_algo="sample-sha256",
               identity_json=json.dumps({"content_hash": "aabbccddeeff00112233",
                                         "content_hash_algo": "sample-sha256",
                                         "etag": "e1"}, sort_keys=True))
    scanner_mod._apply_enrich_status(row, _scan_file(200), is_new=False,
                                     layered=True, item_type="movie")
    assert row.content_hash is None
    assert row.content_hash_algo is None
    # 采样键摘掉，其他证据键原样保留。
    assert json.loads(row.identity_json) == {"etag": "e1"}


def test_size_change_keeps_source_hash(db):
    session, lib = db
    row = _row(session, lib, size=100, content_hash="deadbeef",
               content_hash_algo="md5",
               identity_json=json.dumps({"content_hash": "deadbeef",
                                         "content_hash_algo": "md5"},
                                        sort_keys=True))
    scanner_mod._apply_enrich_status(row, _scan_file(200), is_new=False,
                                     layered=True, item_type="movie")
    assert row.content_hash == "deadbeef"
    assert row.content_hash_algo == "md5"


def test_same_size_keeps_sample_hash(db):
    session, lib = db
    row = _row(session, lib, size=200, content_hash="aabbccddeeff00112233",
               content_hash_algo="sample-sha256")
    scanner_mod._apply_enrich_status(row, _scan_file(200), is_new=False,
                                     layered=True, item_type="movie")
    assert row.content_hash == "aabbccddeeff00112233"
    assert row.content_hash_algo == "sample-sha256"


def test_first_size_seen_does_not_reset(db):
    # 首次得知大小不算"变化"（_compute_size_change 口径），不重置。
    session, lib = db
    row = _row(session, lib, size=0, content_hash="aabbccddeeff00112233",
               content_hash_algo="sample-sha256")
    scanner_mod._apply_enrich_status(row, _scan_file(200), is_new=False,
                                     layered=True, item_type="movie")
    assert row.content_hash == "aabbccddeeff00112233"


def test_reset_helper_unit_cases(db):
    session, lib = db
    # 只有采样键的快照：清完变 NULL。
    row = _row(session, lib, size=100, content_hash="aabbccddeeff00112233",
               content_hash_algo="sample-sha256",
               identity_json=json.dumps({"content_hash": "aabbccddeeff00112233",
                                         "content_hash_algo": "sample-sha256"},
                                        sort_keys=True))
    assert scanner_mod._reset_stale_sample_hash(row) is True
    assert row.content_hash is None
    assert row.identity_json is None
    # 已是 NULL：幂等空操作。
    assert scanner_mod._reset_stale_sample_hash(row) is False
    # 解析失败的快照：列照清，快照原样不动。
    row2 = _row(session, lib, guid="g2", size=100,
                content_hash="aabbccddeeff00112233",
                content_hash_algo="sample-sha256",
                identity_json="{not json")
    assert scanner_mod._reset_stale_sample_hash(row2) is True
    assert row2.identity_json == "{not json"
    # 源哈希：不动。
    row3 = _row(session, lib, guid="g3", size=100, content_hash="deadbeef",
                content_hash_algo="sha1")
    assert scanner_mod._reset_stale_sample_hash(row3) is False
    assert row3.content_hash == "deadbeef"


def test_scan_identity_empty_hash_leaves_sample_untouched(db):
    # 源没给哈希时 _apply_scan_identity 不动已有值：重扫本身既不破坏
    # 已采样证据，也不把已采样行打回 pending（打回只发生在大小变化时）。
    session, lib = db
    row = _row(session, lib, size=100, content_hash="aabbccddeeff00112233",
               content_hash_algo="sample-sha256")
    scanner_mod._apply_scan_identity(row, _scan_file(100))
    assert row.content_hash == "aabbccddeeff00112233"
    assert row.content_hash_algo == "sample-sha256"


def test_new_local_row_is_pending_by_construction(db):
    # 识别完成处无需另行标记：新本地行入库即 NULL 哈希（已是 pending）。
    session, lib = db
    row = _row(session, lib, size=0, content_hash=None,
               content_hash_algo=None, enrich_status=None)
    scanner_mod._apply_scan_identity(row, _scan_file(200))
    scanner_mod._apply_enrich_status(row, _scan_file(200), is_new=True,
                                     layered=True, item_type="movie")
    assert row.content_hash is None
    assert row.content_hash_algo is None
