"""图片本地化不得发生在数据库写事务里（v2.42.9，刮削第 3 批）

真实事故（分析报告 7.7 / 处方 9）：``_enrich_apply``（写库阶段）→ ``apply`` → ``_set_image``
→ ``image_store.localize`` 真的发 HTTP，而且 ``scanner.py`` 里明写着「详情是预取的，
apply_details / apply_images 只把已取回的数据落到条目上，不会在这里发请求」——**代码与
自己的声明相反**。一张图超时 15s，等于攥着数据库写锁 15s（PostgreSQL 上就是 15s 的行锁
等待，积压期每条都在重复）。

现在的口径（三层，缺一层就退化）：

1. ``localize(url, allow_download=False)``：写事务里只认「文件已经在本地」，绝不发 HTTP；
2. ``image_store.prewarm(urls)``：IO 阶段先落盘，写事务里那次调用退化成一次 ``isfile``；
3. ``image_store.localize(url)``（默认档）：取图时的按需自愈照旧能下载 —— 预热没赶上的图
   （并发旋钮挡住 / 预热失败）只是「这轮不本地化」，客户端取图时会补一份。

预热与写库必须看**同一份 URL**，所以尺寸拼接收口在 ``tmdb.image_specs``（本文件钉住）。
"""
from types import SimpleNamespace

import pytest

from backend.emby_server import image_store
from backend.emby_server.tmdb import TmdbClient, image_specs, tmdb_client

POSTER_URL = "https://image.tmdb.org/t/p/w500/p.jpg"
BACKDROP_URL = "https://image.tmdb.org/t/p/w1280/b.jpg"
DATA = {"poster_path": "/p.jpg", "backdrop_path": "/b.jpg"}


@pytest.fixture(autouse=True)
def _image_env(tmp_path, monkeypatch):
    """本机图片目录指向临时目录；关掉缩略图预热（避免测试里起后台线程）"""
    monkeypatch.setenv("EMBY_IMAGE_DIR", str(tmp_path / "images"))
    monkeypatch.delenv("EMBY_LOCALIZE_IMAGES", raising=False)
    monkeypatch.setattr(image_store, "enqueue_thumb_warm", lambda *a, **k: None)
    yield


@pytest.fixture
def downloads(monkeypatch):
    """把真下载换成计数替身：返回的内容足够写成一个非空文件

    ``attempts`` 是 v2.53.0 ``_download`` 的重试次数参数（问题一），
    替身收下但不重试——这里数的是「发起下载的 URL」，不是重试次数。
    """
    calls: list = []

    def fake_download(url, attempts=1):
        calls.append(url)
        return b"fake-image-bytes"

    monkeypatch.setattr(image_store, "_download", fake_download)
    return calls


@pytest.fixture
def downloads_forbidden(monkeypatch):
    """写事务里一旦真下载就炸（本文件最关键的断言方式）"""
    def boom(url, attempts=1):  # pragma: no cover — 被调用就是失败
        raise AssertionError(f"写事务里不该下载图片: {url}")

    monkeypatch.setattr(image_store, "_download", boom)


def _item(**kw):
    base = dict(
        poster_path=None, primary_image_url=None,
        backdrop_path=None, backdrop_image_url=None,
        metadata_source=None, overview=None, aliases=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ==================== 1. 写事务档：只认本地已有文件 ====================

def test_localize_refuses_to_download_when_not_allowed(downloads_forbidden):
    assert image_store.localize(POSTER_URL, allow_download=False) == ""


def test_localize_still_downloads_by_default(downloads):
    """取图时的按需自愈是默认档，别把下载整个关掉（否则这张图永远补不上）"""
    path = image_store.localize(POSTER_URL)
    assert path and path.endswith(".jpg")
    assert downloads == [POSTER_URL]


# ==================== 2. 预热把下载挪到 IO 阶段 ====================

def test_prewarm_then_localize_is_offline(downloads):
    assert image_store.prewarm([POSTER_URL]) == 1
    assert downloads == [POSTER_URL]
    # 写事务里的那一次调用：同一个文件、同一把锁，不再发 HTTP
    assert image_store.localize(POSTER_URL, allow_download=False) == \
        image_store.local_path(POSTER_URL)
    assert downloads == [POSTER_URL]          # 没有第二次下载


def test_prewarm_is_idempotent(downloads):
    image_store.prewarm([POSTER_URL, POSTER_URL])
    assert downloads == [POSTER_URL]          # 同一张图不会下两遍
    assert image_store.prewarm([POSTER_URL, BACKDROP_URL]) == 2


def test_prewarm_survives_a_broken_url(monkeypatch):
    def bad(url):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(image_store, "_download", bad)
    assert image_store.prewarm([POSTER_URL, None, ""]) == 0      # 绝不抛异常


# ==================== 3. _set_image：写事务里零 HTTP ====================

def test_set_image_does_not_download_in_the_write_phase(downloads_forbidden):
    item = _item()
    assert tmdb_client.apply_images(item, DATA) is True
    # 远程地址一定落上（唯一事实来源）；本地路径留给预热/按需自愈
    assert item.primary_image_url == POSTER_URL
    assert item.backdrop_image_url == BACKDROP_URL
    assert item.poster_path is None and item.backdrop_path is None


def test_apply_images_uses_the_prewarmed_file(downloads):
    """预热过的图在写库阶段仍然会落本地路径 —— 行为只是「提前下载」，不是「不本地化」"""
    assert image_store.prewarm([POSTER_URL, BACKDROP_URL]) == 2
    item = _item()
    tmdb_client.apply_images(item, DATA)
    assert item.poster_path == image_store.local_path(POSTER_URL)
    assert item.backdrop_path == image_store.local_path(BACKDROP_URL)
    # v2.57.0 起 prewarm 是 4 线程并行下载（as_completed），完成顺序不确定：
    # 这里只断言「下载且仅下载这两张、每张一遍」，不断言顺序。
    assert sorted(downloads) == sorted([POSTER_URL, BACKDROP_URL])  # 全程只下这一遍


# ==================== 4. 预热与写库看同一份 URL ====================

def test_image_specs_is_the_single_source_of_the_urls():
    assert image_specs(DATA) == [("Primary", POSTER_URL), ("Backdrop", BACKDROP_URL)]
    assert image_specs(None, {}) == []

    item = _item()
    tmdb_client.apply_images(item, DATA)
    assert [(k, getattr(item, "primary_image_url" if k == "Primary" else "backdrop_image_url"))
            for k, _url in image_specs(DATA)] == image_specs(DATA)

    hit = {"id": 9, "title": "片子", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg"}
    item2 = _item()
    tmdb_client.apply(item2, hit, "movie")
    assert (item2.primary_image_url, item2.backdrop_image_url) == (POSTER_URL, BACKDROP_URL)


# ==================== 5. 补全 worker：IO 阶段真的预热了 ====================

def test_enrich_fetch_prewarms_the_images_it_will_apply(monkeypatch, downloads):
    """处方 9 点名的那条路：_enrich_fetch（IO）预热 → _enrich_apply（写库）只落字段

    2026-10-21 起刮削期预热默认关（EMBY_IMAGE_PREWARM），本用例测的是显式
    打开后的老行为；默认关时零下载由 test_image_backfill_patrol 覆盖。
    """
    from backend.emby_server import enrich_worker, scanner as sc

    monkeypatch.setenv("EMBY_IMAGE_PREWARM", "1")

    monkeypatch.setattr(TmdbClient, "configured", property(lambda self: True))
    monkeypatch.setattr(
        sc, "_tmdb_work",
        lambda *a, **k: ({"id": 7, "name": "剧名", "poster_path": "/p.jpg",
                          "backdrop_path": "/b.jpg"}, {"backdrop_path": "/b.jpg"}),
    )
    monkeypatch.setattr("backend.emby_server.douban.enabled", lambda db: False)

    item = _item(item_type="series", name="剧名", production_year=2020,
                 file_path="", library_id=1, size=0, tmdb_id=None,
                 repair_requested_at=None, imdb_id=None)
    result = enrich_worker._enrich_fetch(item)

    assert result["images_prewarmed"] == 2
    assert set(downloads) == {POSTER_URL, BACKDROP_URL}
    # 写库阶段（_enrich_apply 那条路）就不会再有下载：文件都已在本地
    assert image_store.localize(POSTER_URL, allow_download=False) == \
        image_store.local_path(POSTER_URL)
    assert set(downloads) == {POSTER_URL, BACKDROP_URL}
