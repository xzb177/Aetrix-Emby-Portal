"""Emby 网关（/emby/*）兼容面冒烟测试

覆盖本轮补齐的客户端兼容端点与「路由优先级」回归（这是最容易悄悄坏掉的部分：
新增的 {id} 通配会把 /Users/Public、/Items/Counts 这类固定路径吞掉）：

- 路由优先级：/Users/Public、/Users/Me、/Items/Counts 不被 {id} 通配吞掉
- 播放流优先级：/Videos/{id}/stream 不被新增的 HLS / stream.{container} 抢走
- 图片：/Items/{id}/Images/{Type}/{Index}（Backdrop/0 此前直接 404）
- 字幕投递：GBK 外挂字幕 → VTT（中文不乱码）、SRT 原格式
- 收藏规范路由、搜索、筛选、相似、探针端点、策略、登出、释放转码、库刷新
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["EMBY_PUBLIC_URL"] = "http://media.example.com:8000"

DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend import models  # noqa: E402
from backend.database import engine, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.security import hash_password  # noqa: E402

Session = sessionmaker(bind=engine)
init_db()

PASSED = 0


def ok(msg: str) -> None:
    global PASSED
    PASSED += 1
    print("OK", msg)


# ---- 造数据 ----
db = Session()
user = models.WebUser(
    username="alice", password_hash="x", is_active=True, is_staff=True, admin_role="super",
    emby_username="emby_alice", emby_password=hash_password("alice-play-pw"),
)
db.add(user)
db.commit()

lib_dir = tempfile.mkdtemp(prefix="gwlib_")
movie_dir = os.path.join(lib_dir, "Movie A (2019)")
season_dir = os.path.join(lib_dir, "Show B", "Season 1")
os.makedirs(movie_dir, exist_ok=True)
os.makedirs(season_dir, exist_ok=True)

movie_file = os.path.join(movie_dir, "Movie A (2019).mp4")
ep_file = os.path.join(season_dir, "Show B S01E01.mp4")
_ffmpeg_bin = shutil.which("ffmpeg")
for path in (movie_file, ep_file):
    if _ffmpeg_bin:
        try:
            subprocess.run(
                [
                    _ffmpeg_bin, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=15",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", "-movflags", "+faststart",
                    path,
                ],
                check=True,
                timeout=60,
            )
        except Exception:
            # ffmpeg 生成失败时回退到假文件
            with open(path, "wb") as f:
                f.write(b"\x00" * 8192)
    else:
        with open(path, "wb") as f:
            f.write(b"\x00" * 8192)

# 本地海报：验证 /Images/Primary/{index}
with open(os.path.join(movie_dir, "poster.jpg"), "wb") as f:
    f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 64)

# GBK 编码中文外挂字幕（国内资源常见），用于验证编码嗅探与 srt→vtt
srt_file = os.path.join(season_dir, "Show B S01E01.chi.srt")
with open(srt_file, "wb") as f:
    f.write("1\n00:00:01,000 --> 00:00:03,000\n你好，世界\n".encode("gb18030"))

lib = em.Library(guid="b" * 32, name="测试库", collection_type="movies", paths=lib_dir)
db.add(lib)
db.commit()
lib_id = lib.id
db.close()

from backend.emby_server.scanner import scan_library_sync  # noqa: E402

db = Session()
scan_library_sync(db, db.query(em.Library).filter(em.Library.id == lib_id).first())
movie = db.query(em.MediaItem).filter(em.MediaItem.item_type == "movie").first()
episode = db.query(em.MediaItem).filter(em.MediaItem.item_type == "episode").first()
assert movie is not None and episode is not None, "扫描未产出条目"
assert movie.poster_path, "本地海报未被识别"

sub = (
    db.query(em.MediaStream)
    .filter(em.MediaStream.item_id == episode.id, em.MediaStream.stream_type == "Subtitle")
    .first()
)
assert sub is not None, "外挂字幕未被扫描为轨道"
assert sub.stream_index is not None, "外挂字幕缺少 stream_index（客户端会拼出 Subtitles/None）"
movie_guid, episode_guid, sub_index = movie.guid, episode.guid, sub.stream_index
db.close()

# ---- 客户端会话 ----
from backend.main import app  # noqa: E402

client = TestClient(app)

r = client.post(
    "/emby/Users/AuthenticateByName",
    json={"Username": "alice", "Pw": "alice-play-pw"},
    headers={"X-Emby-Authorization": 'MediaBrowser Client="Infuse", Device="Test", DeviceId="gw1", Version="7.0"'},
)
assert r.status_code == 200, r.text
auth = r.json()
token = auth["AccessToken"]
uid = auth["User"]["Id"]
H = {"X-Emby-Token": token}
ok("AuthenticateByName")


def get(path, **kw):
    return client.get(path, headers=H, **kw)


# ---- 路由优先级：固定路径不能被 {id} 通配吞掉 ----
r = client.get("/emby/Users/Public")
assert r.status_code == 200 and r.json() == [], (r.status_code, r.text[:200])
ok("/Users/Public 返回空列表（未被 /Users/{id} 吞掉，且不泄露账号列表）")

r = client.get("/emby/Users/Me")
assert r.status_code == 401, f"未认证的 /Users/Me 应为 401，got {r.status_code}"
r = get("/emby/Users/Me")
assert r.status_code == 200 and r.json()["Id"] == uid, r.text
ok("/Users/Me 路由优先且返回当前用户")

r = get("/emby/Items/Counts")
assert r.status_code == 200, (r.status_code, r.text[:200])
counts = r.json()
assert counts["MovieCount"] >= 1 and counts["EpisodeCount"] >= 1, counts
ok(f"/Items/Counts 路由优先 {counts['MovieCount']} 影 / {counts['EpisodeCount']} 集")

r = get("/emby/Items/Filters")
assert r.status_code == 200 and "Years" in r.json(), r.text
ok("/Items/Filters 返回类型/年代筛选元数据")

r = get("/emby/Items/Intros")
assert r.status_code == 200, r.text
ok("/Items/Intros 路由优先")

# ---- 播放流优先级回归 ----
r = client.get(f"/emby/Videos/{movie_guid}/stream?api_key={token}",
               headers={"Range": "bytes=0-1023"})
assert r.status_code == 206, (r.status_code, r.text[:200])
assert r.headers.get("content-range", "").startswith("bytes 0-1023/")
ok("/Videos/{id}/stream 仍走直连流（未被 HLS/stream.{container} 抢走）")

r = client.get(f"/emby/Videos/{movie_guid}/stream.mp4?api_key={token}",
               headers={"Range": "bytes=0-1023"})
assert r.status_code == 206, (r.status_code, r.text[:200])
ok("/Videos/{id}/stream.{container} 可用")

# ---- 图片 ----
r = get(f"/emby/Items/{movie_guid}/Images/Primary/0")
assert r.status_code == 200 and r.headers["content-type"].startswith("image/"), (r.status_code, r.text[:120])
ok("/Items/{id}/Images/Primary/0 返回图片")

r = get(f"/emby/Items/{movie_guid}/Images/Backdrop/0")
assert r.status_code in (200, 404), f"Backdrop/0 不应 500，got {r.status_code}"
assert r.status_code != 500
ok("/Items/{id}/Images/Backdrop/0 路由存在（无背景图时 404 而非 500）")

# ---- 条目详情：MediaSource / 字幕 DeliveryUrl / RunTimeTicks 字段 ----
r = get(f"/emby/Items/{movie_guid}")
assert r.status_code == 200, r.text
detail = r.json()
assert "RunTimeTicks" in detail and "Container" in detail, detail
assert detail["MediaSources"][0]["Id"] == movie_guid
ok("条目详情含 RunTimeTicks/Container/MediaSources")

r = get(f"/emby/Items/{episode_guid}")
assert r.status_code == 200, r.text
streams = r.json()["MediaSources"][0]["MediaStreams"]
sub_streams = [s for s in streams if s.get("Type") == "Subtitle"]
assert sub_streams, streams
assert sub_streams[0].get("DeliveryUrl"), "字幕轨缺少 DeliveryUrl，客户端会显示“无字幕”"
assert sub_streams[0]["IsTextSubtitleStream"] is True
ok("字幕轨带 DeliveryUrl 与 IsTextSubtitleStream")

# ---- 字幕投递 ----
r = get(f"/emby/Videos/{episode_guid}/subtitles/{sub_index}/Stream.vtt")
assert r.status_code == 200, (r.status_code, r.text[:200])
assert r.text.lstrip().startswith("WEBVTT"), r.text[:80]
assert "你好，世界" in r.text, "中文乱码：GBK 字幕未正确解码"
assert "00:00:01.000 --> 00:00:03.000" in r.text
ok("外挂 GBK 字幕 → VTT（中文无乱码）")

r = get(f"/emby/Videos/{episode_guid}/{episode_guid}/Subtitles/{sub_index}/Stream.vtt")
assert r.status_code == 200 and "你好，世界" in r.text, (r.status_code, r.text[:200])
ok("带 MediaSourceId 的字幕路径可用（客户端标准路径）")

r = get(f"/emby/Videos/{episode_guid}/subtitles/{sub_index}/0/Stream.srt")
assert r.status_code == 200 and "00:00:01,000 --> 00:00:03,000" in r.text, (r.status_code, r.text[:200])
ok("字幕 SRT 原格式投递")

r = get(f"/emby/Videos/{episode_guid}/subtitles/999/Stream.vtt")
assert r.status_code == 404, f"不存在的字幕轨应 404，got {r.status_code}"
ok("不存在的字幕轨 404")

# ---- 收藏规范路由 + 筛选 ----
movie_id = movie_guid
r = client.post(f"/emby/Users/{uid}/FavoriteItems/{movie_id}", headers=H)
assert r.status_code == 200 and r.json()["IsFavorite"] is True, r.text
r = get(f"/emby/Users/{uid}/FavoriteItems")
assert r.json()["TotalRecordCount"] == 1, r.text
ok("POST /Users/{uid}/FavoriteItems/{id} 生效")

r = get(f"/emby/Users/{uid}/Items?Recursive=true&IncludeItemTypes=Movie&Filters=IsFavorite")
assert r.status_code == 200 and r.json()["TotalRecordCount"] == 1, r.text
assert r.json()["Items"][0]["Id"] == movie_id
ok("Filters=IsFavorite 只返回收藏项（旧实现忽略该参数返回全量）")

r = get(f"/emby/Users/{uid}/Items?Recursive=true&IncludeItemTypes=Movie&Filters=IsUnplayed")
assert r.status_code == 200, r.text
ok("Filters=IsUnplayed 可用")

r = get(f"/emby/Users/{uid}/Items?Recursive=true&IncludeItemTypes=Movie&SortBy=Random&Limit=1")
assert r.status_code == 200, r.text
ok("SortBy=Random 可用")

r = get(f"/emby/Users/{uid}/Items?Ids={movie_id}")
assert r.status_code == 200 and r.json()["TotalRecordCount"] == 1, r.text
ok("Ids 批量查询可用")

r = client.delete(f"/emby/Users/{uid}/FavoriteItems/{movie_id}", headers=H)
assert r.status_code == 200 and r.json()["IsFavorite"] is False, r.text
ok("DELETE /Users/{uid}/FavoriteItems/{id} 生效")

# ---- 搜索 ----
r = get("/emby/Search/Hints?SearchTerm=Movie&Limit=10")
assert r.status_code == 200, r.text
hints = r.json()
assert hints["TotalRecordCount"] >= 1 and hints["SearchHints"][0]["ItemId"], hints
ok("Search/Hints 全局搜索可用")

r = get("/emby/Search/Hints?SearchTerm=不存在的片名xyz")
assert r.json()["TotalRecordCount"] == 0
ok("Search/Hints 无结果时返回空集合")

# ---- 相似 / 祖先 / 探针端点 ----
r = get(f"/emby/Items/{movie_id}/Similar")
assert r.status_code == 200, r.text
r = get(f"/emby/Shows/{episode_guid}/Similar")
assert r.status_code == 200, r.text
ok("Similar 端点可用")

r = get(f"/emby/Items/{episode_guid}/Ancestors")
assert r.status_code == 200, r.text
ancestors = r.json()["Items"]
assert any(a["Type"] == "Series" for a in ancestors), ancestors
ok("Ancestors 返回季/剧链路")

probes = {
    "LocalTrailers": f"/emby/Items/{movie_id}/LocalTrailers",
    "SpecialFeatures": f"/emby/Items/{movie_id}/SpecialFeatures",
    "ThemeMedia": f"/emby/Items/{movie_id}/ThemeMedia",
    "ThemeVideos": f"/emby/Items/{movie_id}/ThemeVideos",
    "ItemIntros": f"/emby/Items/{movie_id}/Intros",
    "UserItemIntros": f"/emby/Users/{uid}/Items/{movie_id}/Intros",
    "CriticReviews": f"/emby/Items/{movie_id}/CriticReviews",
    "Persons": "/emby/Persons",
    "Plugins": "/emby/Plugins",
    "ScheduledTasks": "/emby/ScheduledTasks",
    "ParentalRatings": "/emby/Localization/ParentalRatings",
    "ActivityLog": "/emby/Activity/Log/Entries",
    "SystemEndpoint": "/emby/System/Endpoint",
    "Genres": "/emby/Genres",
    "Studios": "/emby/Studios",
    "MediaFolders": "/emby/Library/MediaFolders",
    "UserViews": "/emby/UserViews",
}
for label, path in probes.items():
    resp = get(path)
    assert resp.status_code == 200, f"{label} -> {resp.status_code} {resp.text[:160]}"
ok(f"{len(probes)} 个探针/元数据端点全部返回 200（无 404 刷屏）")

r = get("/emby/Genres")
genres = r.json()
if genres["TotalRecordCount"]:
    name = genres["Items"][0]["Name"]
    assert get(f"/emby/Genres/{name}").status_code == 200
ok("Genres/{name} 可用")

# ---- 用户策略 ----
r = get(f"/emby/Users/{uid}/Policy")
assert r.status_code == 200 and "EnableMediaPlayback" in r.json(), r.text
r = client.post(f"/emby/Users/{uid}/Policy", headers=H, json={"EnableMediaPlayback": False})
assert r.status_code == 200 and r.json()["EnableMediaPlayback"] is True, "客户端写入策略应被忽略"
ok("Users/{id}/Policy 读可用、写入被忽略（策略由门户管理）")

# ---- PlaybackInfo 变体 ----
r = get(f"/emby/Items/{movie_id}/PlaybackInfo")
assert r.status_code == 200, r.text
info = r.json()
assert info["MediaSources"][0]["Id"] == movie_id and info["PlaySessionId"], info
ok("GET PlaybackInfo 可用（此前只有 POST）")

r = get(f"/emby/Users/{uid}/Items/{movie_id}/PlaybackInfo")
assert r.status_code == 200, r.text
ok("用户维度 PlaybackInfo 路径可用")

r = client.post(f"/emby/Items/{movie_id}/PlaybackInfo", headers=H, json={})
assert r.status_code == 200, r.text
ok("POST PlaybackInfo 仍可用")

# ---- 原始文件 ----
r = get(f"/emby/Items/{movie_id}/File")
assert r.status_code == 200, r.text
ok("/Items/{id}/File 可用")

# ---- 释放转码 / 库刷新 ----
r = client.delete("/emby/Videos/ActiveEncodings", headers=H)
assert r.status_code == 204, (r.status_code, r.text[:200])
r = client.post("/emby/Videos/ActiveEncodings/Delete", headers=H)
assert r.status_code == 204, r.status_code
ok("释放活跃转码会话可用")

r = client.post("/emby/Library/Refresh", headers=H)
assert r.status_code == 200 and r.json()["success"], r.text
ok("Library/Refresh 触发扫描（staff）")

bob_token = None
r = client.post("/emby/Library/Refresh")
assert r.status_code == 401, f"未认证刷新应 401，got {r.status_code}"
ok("Library/Refresh 未认证 401")

# ---- 付费墙同样覆盖新端点（字幕不能成为绕过播放收费的入口）----
db2 = Session()
bob = models.WebUser(
    username="bob", password_hash="x", is_active=True, is_staff=False,
    emby_username="emby_bob", emby_password=hash_password("bob-play-pw"),
)
db2.add(bob)
db2.commit()
db2.close()

r = client.post(
    "/emby/Users/AuthenticateByName",
    json={"Username": "bob", "Pw": "bob-play-pw"},
    headers={"X-Emby-Authorization": 'MediaBrowser Client="Infuse", Device="Test", DeviceId="gw2", Version="7.0"'},
)
assert r.status_code == 200, r.text
bob_token = r.json()["AccessToken"]

r = client.get(f"/emby/Videos/{episode_guid}/subtitles/{sub_index}/Stream.vtt?api_key={bob_token}")
assert r.status_code == 403, f"非会员取字幕应 403（与播放同门槛），got {r.status_code}"
r = client.get(f"/emby/Videos/{movie_guid}/stream?api_key={bob_token}")
assert r.status_code == 403, f"非会员直连拉流应 403，got {r.status_code}"
r = client.get(f"/emby/Items/{movie_guid}/File?api_key={bob_token}")
assert r.status_code == 403, f"非会员取原始文件应 403，got {r.status_code}"
ok("非会员字幕/拉流/原始文件同为 403（付费墙覆盖新端点）")

# ---- HLS 变体地址必须自带 api_key（hls.js 子请求不带认证头）----
if shutil.which("ffmpeg"):
    r = client.get(f"/emby/videos/{movie_guid}/master.m3u8?VideoBitrate=2000000&api_key={token}")
    if r.status_code == 503:
        print("SKIP HLS 变体（转码服务暂不可用，503）")
    else:
        assert r.status_code == 200 and "m3u8" in r.text, r.text[:200]
        assert "api_key=" in r.text, "HLS 变体地址缺少 api_key，子请求会 401"
        variant = [ln for ln in r.text.splitlines() if ln.startswith("http")][0]
        r2 = client.get(variant)  # 故意不带任何认证头
        if r2.status_code == 503:
            print("SKIP HLS 变体请求（转码进程未就绪，503）")
        else:
            assert r2.status_code == 200, f"HLS 变体请求应免额外认证头，got {r2.status_code}"
            ok("HLS 变体地址自带 api_key 且免额外认证头")
else:
    print("SKIP HLS 变体（沙箱无 ffmpeg）")

# ---- 播放列表重写（纯函数，不依赖 ffmpeg；转码删除分支已移除该函数）----
try:
    from backend.emby_server.api import _rewrite_playlist  # noqa: E402
except ImportError:  # 转码已删除的分支：该函数不存在，跳过本段
    _rewrite_playlist = None

if _rewrite_playlist is None:
    print("SKIP 播放列表重写（服务端转码已删除）")
else:
    pl_dir = tempfile.mkdtemp(prefix="gwpl_")
    with open(os.path.join(pl_dir, "master.m3u8"), "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg00001.ts\n")
    rewritten = _rewrite_playlist(pl_dir, "http://h", "GUID", "SESS", "TOK")
    assert "seg00001.ts?session=SESS&api_key=TOK" in rewritten, rewritten
    rewritten_no_key = _rewrite_playlist(pl_dir, "http://h", "GUID", "SESS")
    assert "api_key" not in rewritten_no_key, rewritten_no_key
    ok("播放列表重写带上 session 与 api_key（旧实现只有 session，切片 401）")
    shutil.rmtree(pl_dir, ignore_errors=True)

# ---- 登出：token 失效 ----
r = client.post("/emby/Sessions/Logout", headers=H)
assert r.status_code == 200, r.text
r = get(f"/emby/Users/{uid}/Items")
assert r.status_code == 401, f"登出后 token 应失效，got {r.status_code}"
ok("Sessions/Logout 撤销当前 token")

# 会员 token 不应受影响（只撤销了当前设备的那个 token）
r = client.get(f"/emby/Users/{uid}/Items?api_key={token}")
assert r.status_code == 401, f"同一 token 应保持失效，got {r.status_code}"
r = client.get(f"/emby/Users/1/Items?api_key={bob_token}")
assert r.status_code == 200, f"其他用户的 token 不应被误撤销，got {r.status_code}"
ok("登出只撤销当前 token，不影响其他会话")

print(f"\n✅ Emby 网关冒烟测试全部通过（{PASSED} 项）")
shutil.rmtree(lib_dir, ignore_errors=True)
os.remove(DB)
