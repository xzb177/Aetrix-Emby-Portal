"""媒体库 / 搜索 / 刮削 冒烟测试（v2.6.4）

覆盖本轮改动：

- 季/集命名识别：S09E01、1x02、第3集、EP05、以及**只有季号**的 S09 / Season 9 / 第九季 / 第9季
- 发行平台识别：Netflix / Disney+ / Apple TV+ / Prime Video / Max / Hulu / Paramount+ / Peacock / Crunchyroll
- 外挂字幕 sidecar：较短字幕名、发行标签差异、多版本、rclone/GD sidecar、同目录别的集不串
- 刮削策略：missing_only / 3m / 6m / 1y / all；探测缓存（文件未变不重复 ffprobe）
- 搜索排序：标题完全匹配 / 前缀优先于别名与模糊；中英文、繁简体、多别名
- 扫描安全网：目录不可用时禁止清理、同一媒体库重复扫描被拒绝、配置快照
- 虚拟媒体库：按平台聚合计数与条目查询

用法: python scripts/smoke_test_media_search.py
"""
import os
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 不写死库文件名：交给 backend.database 解析（新装 aetrix_unified.db，老部署沿用原库）
os.environ.setdefault("DATABASE_TYPE", "sqlite")

from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server.auth import hash_emby_token  # noqa: E402
from backend.emby_server import scanner as sc
from backend.emby_server import search as se
from backend.main import app
from backend.security import hash_password

init_db()
# 冒烟测试不发外网请求（本机可能配了 TMDB_API_KEY）
sc.tmdb_client.session = None

failures: list[str] = []
suffix = str(random.randint(100000, 999999))
tmp_root = tempfile.mkdtemp(prefix="aetrix-media-")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def touch(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * 16)
    return path


# ==================== 1. 季/集命名识别 ====================
print("\n=== 季/集命名识别 ===")
NAME_CASES = [
    # (路径, 库类型, 期望季, 期望集, 期望片名)
    ("/tv/Rick.and.Morty.S09/Rick.and.Morty.S09E01.1080p.WEB-DL.mkv", "tvshows", 9, 1, "Rick and Morty"),
    ("/tv/Show/Show.S01E02.mkv", "tvshows", 1, 2, "Show"),
    ("/tv/Show/Show.1x02.mkv", "tvshows", 1, 2, "Show"),
    ("/tv/剧名/剧名.第3集.mkv", "tvshows", 1, 3, "剧名"),
    ("/tv/Show/Show.EP05.mkv", "tvshows", 1, 5, "Show"),
    # 只有季号（季包 / 中文命名）：旧实现认不出季，整季包会被当成电影
    ("/tv/Rick.and.Morty/Rick.and.Morty.S09.mkv", "tvshows", 9, None, "Rick and Morty"),
    ("/tv/Rick.and.Morty.Season 9/Rick.and.Morty.Season 9.mkv", "tvshows", 9, None, "Rick and Morty"),
    ("/tv/剧名/剧名.第九季.mkv", "tvshows", 9, None, "剧名"),
    ("/tv/剧名/剧名.第12季.mkv", "tvshows", 12, None, "剧名"),
    ("/tv/Show/Show.S09/Show.S09.mkv", "tvshows", 9, None, "Show"),
    # 电影库不认裸 S09：否则 S1m0ne 这类片名会被判成第 1 季
    ("/movies/S1m0ne.2002.1080p.mkv", "movies", None, None, "S1m0ne"),
    # 点分隔的片名要恢复成空格（旧 CLEAN_RE 写错，会原样留下 "Movie.2024"）
    ("/movies/Movie.Name.2024.2160p.mkv", "movies", None, None, "Movie Name"),
]
for path, kind, want_season, want_ep, want_name in NAME_CASES:
    parsed = sc.parse_media_filename(path, kind)
    ok = (
        parsed["season"] == want_season
        and parsed["episode"] == want_ep
        and parsed["name"] == want_name
    )
    check(
        f"命名识别 {os.path.basename(path)}",
        ok,
        f"得到 季={parsed['season']} 集={parsed['episode']} 名={parsed['name']!r}，期望 季={want_season} 集={want_ep} 名={want_name!r}",
    )

check("中文数字转换 第九季=9", sc.cn_to_int("九") == 9)
check("中文数字转换 十二=12", sc.cn_to_int("十二") == 12)
check("中文数字转换 二十=20", sc.cn_to_int("二十") == 20)

# ==================== 2. 发行平台识别 ====================
print("\n=== 发行平台识别 ===")
PLATFORM_CASES = [
    ("/m/Show.2024.1080p.NF.WEB-DL.mkv", ["netflix"]),
    ("/m/Show.2024.2160p.DSNP.WEB-DL.mkv", ["disney"]),
    ("/m/Show.2024.1080p.ATVP.WEB-DL.mkv", ["appletv"]),
    ("/m/Show.2024.1080p.AMZN.WEB-DL.mkv", ["prime"]),
    ("/m/Show.2024.1080p.HMAX.WEB-DL.mkv", ["max"]),
    ("/m/Show.2024.1080p.HULU.WEB-DL.mkv", ["hulu"]),
    ("/m/Show.2024.1080p.PMTP.WEB-DL.mkv", ["paramount"]),
    ("/m/Show.2024.1080p.PCOK.WEB-DL.mkv", ["peacock"]),
    ("/m/Show.2024.1080p.CR.WEB-DL.mkv", ["crunchyroll"]),
    # 没有发布标识时不认短标签，避免把片名当平台
    ("/m/Max.2015.mkv", []),
    ("/m/Movie.2024.1080p.mkv", []),
]
for path, want in PLATFORM_CASES:
    got = sc.detect_platforms(path)
    check(f"平台识别 {os.path.basename(path)}", got == want, f"得到 {got}，期望 {want}")

# ==================== 3. 外挂字幕 sidecar ====================
print("\n=== 外挂字幕 sidecar ===")
sub_dir = os.path.join(tmp_root, "subs")
video = touch(os.path.join(sub_dir, "Show.S01E01.1080p.WEB-DL.x265-GROUP.mkv"))
touch(os.path.join(sub_dir, "Show.S01E01.chi.srt"))            # 标准同名 + 语言
touch(os.path.join(sub_dir, "Show.S01E01.ass"))                # 较短字幕名
touch(os.path.join(sub_dir, "Show.S01E01.简体.srt"))            # 中文标注
touch(os.path.join(sub_dir, "Show.S01E01.mkv.zh.srt"))         # rclone/GD sidecar
touch(os.path.join(sub_dir, "Show.S01E02.chi.srt"))            # 别的集：不能串进来
touch(os.path.join(sub_dir, "Other.Movie.2024.chs.ass"))       # 别的影片：不能串进来
found = sc.find_external_subtitles(video)
found_names = sorted(os.path.basename(p) for _lang, p in found)
check(
    "字幕识别：合并 4 条、不串其他集/其他片",
    len(found) == 4 and "Show.S01E02.chi.srt" not in found_names and "Other.Movie.2024.chs.ass" not in found_names,
    f"识别到 {found_names}",
)
langs = {os.path.basename(p): lang for lang, p in found}
check("字幕语言 简体 → chi", langs.get("Show.S01E01.简体.srt") == "chi", str(langs))
check("字幕语言 sidecar .zh → chi", langs.get("Show.S01E01.mkv.zh.srt") == "chi", str(langs))

# 多版本：同一目录下 UHD 版与普通版各带自己的字幕
multi_dir = os.path.join(tmp_root, "multi")
touch(os.path.join(multi_dir, "Movie.2024.1080p.mkv"))
touch(os.path.join(multi_dir, "Movie.2024.1080p.chs.ass"))
touch(os.path.join(multi_dir, "Movie.2024.2160p.mkv"))
touch(os.path.join(multi_dir, "Movie.2024.2160p.eng.srt"))
hd = {os.path.basename(p) for _l, p in sc.find_external_subtitles(os.path.join(multi_dir, "Movie.2024.1080p.mkv"))}
uhd = {os.path.basename(p) for _l, p in sc.find_external_subtitles(os.path.join(multi_dir, "Movie.2024.2160p.mkv"))}
check("多版本媒体：1080p 只配自己的字幕", hd == {"Movie.2024.1080p.chs.ass"}, str(hd))
check("多版本媒体：2160p 只配自己的字幕", uhd == {"Movie.2024.2160p.eng.srt"}, str(uhd))

# ==================== 4. 刮削策略与探测缓存 ====================
print("\n=== 刮削策略与探测缓存 ===")


class _Item:
    def __init__(self, tmdb_id=None, last_scraped_at=None, date_added=None):
        self.tmdb_id = tmdb_id
        self.last_scraped_at = last_scraped_at
        self.date_added = date_added or datetime.now()


now = datetime.now()
check("策略 missing_only：缺元数据要刮", sc.should_scrape(_Item(), "missing_only", now))
check("策略 missing_only：已有元数据不刮", not sc.should_scrape(_Item("123"), "missing_only", now))
check("策略 3m：昨天刮过不重刮", not sc.should_scrape(_Item("123", now - timedelta(days=1)), "3m", now))
check("策略 3m：100 天前刮过要重刮", sc.should_scrape(_Item("123", now - timedelta(days=100)), "3m", now))
check("策略 6m：100 天前刮过不重刮", not sc.should_scrape(_Item("123", now - timedelta(days=100)), "6m", now))
check("策略 1y：200 天前刮过不重刮", not sc.should_scrape(_Item("123", now - timedelta(days=200)), "1y", now))
check("策略 all：永远重刮", sc.should_scrape(_Item("123", now), "all", now))
check("未知策略回退 missing_only", sc.normalize_scrape_policy("bogus") == "missing_only")
check("策略表包含 3m/6m/1y/all", set(sc.SCAN_POLICIES) == {"missing_only", "3m", "6m", "1y", "all"})


class _ProbeItem:
    def __init__(self, duration_ticks=0, last_probed_at=None, size=0):
        self.duration_ticks = duration_ticks
        self.last_probed_at = last_probed_at
        self.size = size


probe_path = touch(os.path.join(tmp_root, "probe.mkv"))
size = os.path.getsize(probe_path)
check("探测缓存：从未探测 → 要探", sc.needs_probe(_ProbeItem(), probe_path))
check(
    "探测缓存：已探测且大小未变 → 不重探",
    not sc.needs_probe(_ProbeItem(10_000_000, now, size), probe_path),
)
check(
    "探测缓存：文件大小变了 → 重探",
    sc.needs_probe(_ProbeItem(10_000_000, now, size + 1), probe_path),
)

# ==================== 5. 搜索排序 ====================
print("\n=== 搜索排序 ===")


class _SearchItem:
    def __init__(self, name, original_title=None, aliases="", sort_name=None, genres="",
                 studios="", guid="g", season_number=None, episode_number=None):
        self.name = name
        self.original_title = original_title
        self.aliases = aliases
        self.sort_name = sort_name
        self.genres = genres
        self.studios = studios
        self.guid = guid
        self.season_number = season_number
        self.episode_number = episode_number


exact = _SearchItem("瑞克和莫蒂", guid="exact")
prefix = _SearchItem("rick and morty", guid="prefix")
word = _SearchItem("The Rick and Morty Story", guid="word")
alias = _SearchItem("Rick & Morty", aliases="瑞克和莫蒂,外星也难民", guid="alias")
meta = _SearchItem("别的片", genres="科幻", guid="meta")
near = _SearchItem("瑞克与莫蒂", guid="near")

ranked = se.rank_items([near, alias, exact], "瑞克和莫蒂")
check(
    "标题完全匹配第一、别名其次、模糊最后",
    [i.guid for i in ranked] == ["exact", "alias", "near"],
    " -> ".join(i.guid for i in ranked),
)
check(
    "「瑞克与莫蒂」被归为模糊命中（不是完全匹配）",
    se.score_item(near, "瑞克和莫蒂")[1] == "fuzzy",
    str(se.score_item(near, "瑞克和莫蒂")),
)

order = [i.guid for i in se.rank_items([word, alias, prefix, meta], "rick")]
check("前缀匹配优先于词边界前缀与包含匹配",
      order.index("prefix") < order.index("word"), str(order))
check("标题命中优先于分类元数据命中",
      "meta" not in order, str(order))

# 分类元数据：能搜到，但排在标题命中之后
meta_rank = se.rank_items([meta, _SearchItem("科幻片", guid="title_hit")], "科幻")
check(
    "标题命中 > 分类元数据命中（且元数据仍可搜到）",
    [i.guid for i in meta_rank] == ["title_hit", "meta"],
    str([i.guid for i in meta_rank]),
)

# 中英文 / 繁简体 / 多别名
tc = _SearchItem("瑞克與莫蒂", guid="tc")
check("繁体输入命中简体标题", any(i.guid == "tc" for i in se.rank_items([tc], "瑞克与莫蒂")))
check("简体输入命中繁体标题", any(i.guid == "tc" for i in se.rank_items([tc], "瑞克與莫蒂")))
cn = _SearchItem("瑞克和莫蒂", aliases="Rick and Morty,Rick & Morty", guid="cn")
en = _SearchItem("Rick and Morty", aliases="瑞克和莫蒂,瑞克与莫蒂", guid="en")
check("英文输入命中中文条目的英文别名",
      [i.guid for i in se.rank_items([cn], "Rick and Morty")] == ["cn"])
check("中文输入命中英文条目的中文别名",
      [i.guid for i in se.rank_items([en], "瑞克和莫蒂")] == ["en"])
# 这一条真正走的是繁简转换（别名匹配比标题匹配严，没有 zhconv 时模糊兜底不够）：
# zhconv 是**可选依赖**（未安装时 search 会自动降级为不转换，见 backend/emby_server/search.py），
# 所以没装时如实 SKIP，而不是把「没装可选依赖」当成回归。CI 里会显式装上它，
# 所以这条在 CI 上是真跑的。
if se._get_zhconv() is not None:
    check("繁体别名可命中", any(i.guid == "en" for i in se.rank_items([en], "瑞克與莫蒂")))
else:
    print("SKIP  繁体别名可命中 —— 未安装可选依赖 zhconv（`pip install zhconv` 后可覆盖）")
check("完全不相关的词不命中", se.rank_items([meta], "量子纠缠") == [])
variants = se.search_variants("Show.2024.1080p.WEB-DL")
check("搜索扩展包含去掉发布标签的核心词", any("Show.2024" in v for v in variants), str(variants))
check("无演职员数据时演员加权为 0（不影响排序语义）", se.score_item(_SearchItem("X"), "y")[0] == 0)

# ==================== 6. 扫描安全网与虚拟媒体库 ====================
print("\n=== 扫描安全网与虚拟媒体库 ===")
db = SessionLocal()
lib = None
virtual = None
try:
    media_dir = os.path.join(tmp_root, "library")
    touch(os.path.join(media_dir, "Show.2024.1080p.NF.WEB-DL.mkv"))
    touch(os.path.join(media_dir, "Show.2024.1080p.NF.WEB-DL.chs.srt"))
    touch(os.path.join(media_dir, "Other.2023.1080p.DSNP.WEB-DL.mkv"))
    open(os.path.join(media_dir, "Show.2024.1080p.NF.WEB-DL.idx"), "w").close()

    lib = em.Library(
        guid=f"testlib{suffix}", name=f"冒烟库{suffix}", collection_type="movies",
        paths=media_dir, is_enabled=True, scrape_policy="missing_only",
    )
    db.add(lib)
    db.commit()

    snap = sc.LibrarySnapshot.of(lib)
    check("配置快照固定路径与策略", snap.paths == (media_dir,) and snap.scrape_policy == "missing_only")

    # 合成文件 ffprobe 探不出时长，这里用可控的探测结果，才能验证「探测缓存」
    # （真实视频的行为由上面的 needs_probe 单测覆盖）
    _real_probe = sc.probe_metadata
    def _fake_probe(p, headers=None, size=0):
        try:
            real_size = os.path.getsize(p)
        except OSError:
            real_size = size
        return {
            "duration_ticks": 10_000_000, "bitrate": 1_000_000, "width": 1920, "height": 1080,
            "video_codec": "H264", "audio_codec": "AAC", "audio_languages": "chi",
            "subtitle_languages": "", "streams": [], "size": real_size,
        }

    sc.probe_metadata = _fake_probe
    stats = sc.scan_library_sync(db, lib, snap)
    check("扫描入库 2 个视频", stats["added"] == 2, str(stats))
    check("扫描确实做了探测", stats["probed"] == 2, str(stats))
    check("扫描未跳过清理（目录完整）", stats["removal_skipped"] is False, str(stats))

    items = db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).all()
    nf_item = next((i for i in items if "NF" in (i.file_path or "")), None)
    check("发行平台落库（NF → netflix）", nf_item is not None and nf_item.platforms == "netflix",
          nf_item.platforms if nf_item else "无条目")
    check("探测时间已记录", nf_item is not None and nf_item.last_probed_at is not None)

    # 探测缓存：文件没变，重扫不应再探测
    db.refresh(lib)
    # #12 到期调度（默认开）：首扫后目录的 next_due_at 在未来，未到期重扫会
    # 整段跳过、只记 unchanged，不走完整核对——本段验的是「到期后重扫」的
    # 探测缓存与更新计数，先把本库各目录的到期时刻拨到过去，让这次重扫走
    # due 完整核对路径（与生产里目录到期后的行为一致）。
    db.query(em.ScanDirState).filter(
        em.ScanDirState.library_id == lib.id
    ).update({em.ScanDirState.next_due_at: datetime.now() - timedelta(minutes=1)})
    db.commit()
    stats2 = sc.scan_library_sync(db, lib, sc.LibrarySnapshot.of(lib))
    sc.probe_metadata = _real_probe
    check("重扫不重复探测（文件未变）", stats2["probed"] == 0, str(stats2))
    check("重扫更新而非新增", stats2["added"] == 0 and stats2["updated"] == 2, str(stats2))

    # 外挂字幕：stream_index 必须非空，否则客户端拼出 Subtitles/None
    subs = db.query(em.MediaStream).filter(
        em.MediaStream.item_id == nf_item.id, em.MediaStream.is_external.is_(True)
    ).all()
    check("外挂字幕入库", len(subs) == 1, f"{len(subs)} 条")
    check("外挂字幕 stream_index 非空", all(s.stream_index is not None for s in subs))
    check("外挂字幕语言为 chi", all(s.language == "chi" for s in subs))

    # 重复扫描被拒绝
    sc._acquire_scan(lib.id)
    try:
        blocked = False
        try:
            sc.scan_library_sync(db, lib, sc.LibrarySnapshot.of(lib))
        except sc.ScanInProgress:
            blocked = True
        check("同一媒体库重复扫描被拒绝", blocked)
        check("is_scan_active 反映运行中状态", sc.is_scan_active(lib.id))
    finally:
        sc._release_scan(lib.id)
    check("释放后可再次扫描", not sc.is_scan_active(lib.id))

    # 目录不可用：禁止清理
    lib.paths = os.path.join(tmp_root, "does-not-exist")
    db.commit()
    stats3 = sc.scan_library_sync(db, lib, sc.LibrarySnapshot.of(lib))
    check("根目录不可用时跳过清理（不误删）", stats3["removal_skipped"] is True, str(stats3))
    check("根目录不可用时未删除条目", stats3["removed"] == 0, str(stats3))
    remain = db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).count()
    check("条目仍在库中", remain == 2, f"{remain} 条")

    # 虚拟媒体库：按平台聚合
    lib.paths = media_dir
    db.commit()
    virtual = em.Library(
        guid=f"testvirt{suffix}", name="Netflix", collection_type="movies",
        paths="", is_enabled=True, is_virtual=True, platform="netflix",
    )
    db.add(virtual)
    db.commit()
    db.refresh(virtual)
    sc.scan_library_sync(db, virtual, sc.LibrarySnapshot.of(virtual))
    db.refresh(virtual)

    # 虚拟库是**跳库**的发行平台视图，计数天然是全库口径：这里用同一谓词独立算一遍再比对。
    # （开发机的库文件里可能留着别的用例的条目，把期望值写死成 1 会误报。）
    def _platform_ids(platform: str) -> set:
        return {
            row[0]
            for row in db.query(em.MediaItem.id).filter(
                em.MediaItem.is_hidden == False,  # noqa: E712
                em.MediaItem.item_type.in_(["movie", "series"]),
                em.MediaItem.platforms.ilike(f"%{platform}%"),
            ).all()
        }

    netflix_ids = _platform_ids("netflix")
    expected_netflix = len(netflix_ids)
    check("虚拟媒体库按平台计数与全库同口径一致（且至少含本用例条目）",
          virtual.item_count == expected_netflix and virtual.item_count >= 1,
          f"item_count={virtual.item_count} 同口径={expected_netflix}")

    disney = em.Library(
        guid=f"testvirt2{suffix}", name="Disney+", collection_type="movies",
        paths="", is_enabled=True, is_virtual=True, platform="disney",
    )
    db.add(disney)
    db.commit()
    db.refresh(disney)
    sc.scan_library_sync(db, disney, sc.LibrarySnapshot.of(disney))
    db.refresh(disney)
    disney_ids = _platform_ids("disney")
    check("不同平台的虚拟库互不混淆（Disney+ 视图按自己的平台口径计数）",
          disney.item_count == len(disney_ids) and disney.item_count >= 1,
          f"disney={disney.item_count} 同口径={len(disney_ids)}")
    check("两个平台视图命中不同的条目（不是同一套结果的别名）",
          bool(netflix_ids) and bool(disney_ids) and netflix_ids != disney_ids,
          f"netflix={sorted(netflix_ids)} disney={sorted(disney_ids)}")

    # 季/集图片回退（集无图时用剧集海报）
    tv_dir = os.path.join(tmp_root, "tvlib", "Show")
    touch(os.path.join(tv_dir, "Show.S01E01.1080p.WEB-DL.mkv"))
    tvlib = em.Library(
        guid=f"testtv{suffix}", name=f"剧集库{suffix}", collection_type="tvshows",
        paths=os.path.join(tmp_root, "tvlib"), is_enabled=True,
    )
    db.add(tvlib)
    db.commit()
    sc.scan_library_sync(db, tvlib, sc.LibrarySnapshot.of(tvlib))
    series = db.query(em.MediaItem).filter(
        em.MediaItem.library_id == tvlib.id, em.MediaItem.item_type == "series"
    ).first()
    episode = db.query(em.MediaItem).filter(
        em.MediaItem.library_id == tvlib.id, em.MediaItem.item_type == "episode"
    ).first()
    check("剧集层级建立（series + episode）", series is not None and episode is not None)
    check("集数编号正确", episode is not None and episode.season_number == 1 and episode.episode_number == 1)
    check("剧集条目被创建为 series 而非 movie", series is not None and series.item_type == "series")
finally:
    if lib is not None:
        for it in db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).all():
            db.query(em.MediaStream).filter(em.MediaStream.item_id == it.id).delete()
            db.delete(it)
        db.flush()
        db.query(em.Library).filter(em.Library.id == lib.id).delete()
    for extra in (virtual,):
        if extra is not None:
            db.query(em.Library).filter(em.Library.id == extra.id).delete()
    db.query(em.Library).filter(em.Library.guid.in_([f"testvirt2{suffix}", f"testtv{suffix}"])).delete(
        synchronize_session=False
    )
    for leftover in db.query(em.Library).filter(em.Library.name.like(f"%{suffix}%")).all():
        for it in db.query(em.MediaItem).filter(em.MediaItem.library_id == leftover.id).all():
            db.query(em.MediaStream).filter(em.MediaStream.item_id == it.id).delete()
            db.delete(it)
        db.flush()
        db.query(em.Library).filter(em.Library.id == leftover.id).delete()
    db.commit()
    db.close()
    shutil.rmtree(tmp_root, ignore_errors=True)

# ==================== 7. 协议接口（搜索排序 / 筛选 / 虚拟库 / 图片）====================
print("\n=== 协议接口 ===")
client = TestClient(app)
api_db = SessionLocal()
probe_lib = api_probe_items = None
try:
    probe_dir = os.path.join(tmp_root, "api-lib")
    touch(os.path.join(probe_dir, "Alpha.Target.2024.1080p.NF.WEB-DL.mkv"))
    touch(os.path.join(probe_dir, "Alpha.Target.Extra.Story.2023.1080p.DSNP.WEB-DL.mkv"))
    touch(os.path.join(probe_dir, "Something.Else.2022.1080p.mkv"))
    touch(os.path.join(probe_dir, "海报.jpg"))

    probe_lib = em.Library(
        guid=f"apilib{suffix}", name=f"接口库{suffix}", collection_type="movies",
        paths=probe_dir, is_enabled=True, scrape_policy="missing_only",
    )
    api_db.add(probe_lib)
    api_db.commit()
    _real_probe2 = sc.probe_metadata
    sc.probe_metadata = lambda p, headers=None, size=0: {
        "duration_ticks": 9_000_000, "bitrate": 1, "width": 1920, "height": 1080,
        "video_codec": "H264", "audio_codec": "AAC", "audio_languages": "chi",
        "subtitle_languages": "", "streams": [],
        "size": size or (os.path.getsize(p) if os.path.exists(p) else 0),
    }
    sc.scan_library_sync(api_db, probe_lib, sc.LibrarySnapshot.of(probe_lib))
    sc.probe_metadata = _real_probe2

    alpha = (
        api_db.query(em.MediaItem)
        .filter(em.MediaItem.library_id == probe_lib.id, em.MediaItem.name == "Alpha Target")
        .first()
    )
    check("接口测试条目已入库", alpha is not None,
          str([i.name for i in api_db.query(em.MediaItem)
               .filter(em.MediaItem.library_id == probe_lib.id).all()]))

    # 认证：直接造一个 Emby token（等价于客户端登录后的 X-Emby-Token）
    api_user = models.WebUser(
        username=f"media_smoke{suffix}", password_hash=hash_password("pass12345")
    )
    api_db.add(api_user)
    api_db.commit()
    api_db.refresh(api_user)
    token = f"media-smoke-{suffix}-" + "a" * 12
    api_db.add(em.EmbyApiToken(token=hash_emby_token(token), user_id=api_user.id, device_id="smoke-device"))
    api_db.commit()
    headers = {"X-Emby-Token": token}

    check("未认证访问 /Items 被拒",
          client.get("/emby/Users/me/Items", params={"Recursive": "true"}).status_code in (401, 403))

    # 搜索相关度：精确标题要排在其他包含相同字的条目之前
    resp = client.get("/emby/Users/me/Items", params={"SearchTerm": "Alpha Target",
                                               "Recursive": "true", "Limit": "10"}, headers=headers)
    check("搜索接口可用", resp.status_code == 200, str(resp.status_code))
    names = [i["Name"] for i in resp.json().get("Items", [])]
    check("搜索：完全匹配排在包含匹配之前",
          names[:1] == ["Alpha Target"] and "Alpha Target Extra Story" in names, str(names))

    # 搜索建议（/Search/Hints）走同一套排序
    resp = client.get("/emby/Search/Hints", params={"SearchTerm": "Alpha Target", "Limit": "10"},
                      headers=headers)
    check("搜索建议接口可用", resp.status_code == 200, str(resp.status_code))
    hint_names = [h["Name"] for h in resp.json().get("SearchHints", [])]
    check("搜索建议：完全匹配排第一", hint_names[:1] == ["Alpha Target"], str(hint_names))

    # 类型筛选：合成 Id 点进去要得到真实筛选结果
    resp = client.get("/emby/Genres", headers=headers)
    genre_items = resp.json().get("Items", [])
    check("类型列表可枚举", resp.status_code == 200, str(resp.status_code))
    if genre_items:
        gid = genre_items[0]["Id"]
        gname = genre_items[0]["Name"]
        resp = client.get("/emby/Users/me/Items", params={"ParentId": gid, "Recursive": "true",
                                                   "Limit": "50"}, headers=headers)
        got = resp.json().get("Items", [])
        check(f"按类型筛选（{gname}）不返回空集", len(got) > 0, f"{len(got)} 条")
        check(f"按类型筛选（{gname}）只返回命中类型",
              all(gname in (i.get("Genres") or []) for i in got))

    # 虚拟媒体库：按发行平台聚合；未开启时直达也 404
    virt = em.Library(guid=f"apivirt{suffix}", name=f"Netflix{suffix}", collection_type="mixed",
                      paths="", is_enabled=True, is_virtual=True, platform="netflix")
    api_db.add(virt)
    api_db.commit()
    api_db.refresh(virt)
    resp = client.get("/emby/Users/me/Items", params={"ParentId": virt.guid,
                                               "Recursive": "true", "Limit": "50"}, headers=headers)
    virt_names = [i["Name"] for i in resp.json().get("Items", [])]
    # 同样不写死条目数：开发机的库文件里可能已有同名条目。断言的是「聚合范围」——
    # 本用例的 NF 条目在、DSNP 条目与无平台条目不在。
    check("虚拟媒体库只聚合对应平台的条目（本用例 NF 条目在，DSNP 与无平台条目不在）",
          set(virt_names) == {"Alpha Target"}
          and "Alpha Target Extra Story" not in virt_names
          and "Something Else" not in virt_names,
          str(virt_names))

    virt.is_enabled = False
    api_db.commit()
    resp = client.get("/emby/Users/me/Items", params={"ParentId": virt.guid}, headers=headers)
    check("虚拟媒体库关闭时对客户端不可见", resp.status_code == 404, str(resp.status_code))
    api_db.delete(virt)
    api_db.commit()

    # 图片：数据库有记录但本地文件丢失 → 404 + 排队修复（不是 5xx）
    alpha.poster_path = os.path.join(tmp_root, "long-gone-poster.jpg")
    api_db.commit()
    resp = client.get(f"/emby/Items/{alpha.guid}/Images/Primary", headers=headers)
    check("图片文件丢失时返回 404（不是 5xx）", resp.status_code == 404, str(resp.status_code))
    api_db.refresh(alpha)
    check("图片丢失时已排队修复", alpha.repair_requested_at is not None)
    check("失效的本地图片路径已清理", alpha.poster_path is None, str(alpha.poster_path))

    # 图片：远程图取不到 → 404（不是 500）
    alpha.primary_image_url = "http://127.0.0.1:9/nope.jpg"
    alpha.repair_requested_at = None
    api_db.commit()
    resp = client.get(f"/emby/Items/{alpha.guid}/Images/Primary", headers=headers)
    check("远程图片失效时返回 404（不是 500）", resp.status_code == 404, str(resp.status_code))
    api_db.refresh(alpha)
    check("远程图片失效时也排队修复", alpha.repair_requested_at is not None)
    alpha.primary_image_url = None
    api_db.commit()

    # 系统信息（无认证）——证明应用本身能正常启动响应
    check("公开系统信息可用（裸根路径）", client.get("/System/Info/Public").status_code == 200)

    api_db.query(em.EmbyApiToken).filter(em.EmbyApiToken.token == hash_emby_token(token)).delete()
    api_db.query(models.WebUser).filter(models.WebUser.id == api_user.id).delete()
    api_db.commit()
finally:
    if probe_lib is not None:
        for it in api_db.query(em.MediaItem).filter(em.MediaItem.library_id == probe_lib.id).all():
            api_db.query(em.MediaStream).filter(em.MediaStream.item_id == it.id).delete()
            api_db.delete(it)
        api_db.flush()
        api_db.query(em.Library).filter(em.Library.id == probe_lib.id).delete()
    api_db.commit()
    api_db.close()

print(f"\n{'=' * 60}")
if failures:
    print(f"❌ {len(failures)} 项失败：")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print("✅ 全部通过")
