"""自建 Emby 服务器端到端冒烟测试"""
import json
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

from backend.database import engine, init_db  # noqa: E402
from backend import models  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

Session = sessionmaker(bind=engine)
init_db()

# ---- 造测试数据 ----
from backend.security import hash_password  # noqa: E402

db = Session()
admin = models.AdminUser(username="admin", password_hash="x", role="super_admin")
db.add(admin)
user = models.WebUser(
    username="alice",
    password_hash="x",
    is_active=True,
    is_staff=True, admin_role="super",
    emby_username="emby_alice",
    emby_password=hash_password("alice-play-pw"),
)
db.add(user)
db.commit()

lib_dir = tempfile.mkdtemp(prefix="medialib_")
os.makedirs(os.path.join(lib_dir, "Movie A (2019)"), exist_ok=True)
os.makedirs(os.path.join(lib_dir, "Show B"), exist_ok=True)
os.makedirs(os.path.join(lib_dir, "Show B", "Season 1"), exist_ok=True)

# 用 ffmpeg 生成真实可播放的媒体文件（若可用），否则生成假文件
have_ffmpeg = shutil.which("ffmpeg") is not None
movie_file = os.path.join(lib_dir, "Movie A (2019)", "Movie A (2019).mp4")
ep1_file = os.path.join(lib_dir, "Show B", "Season 1", "Show B S01E01.mp4")
ep2_file = os.path.join(lib_dir, "Show B", "Season 1", "Show B S01E02.mp4")

if have_ffmpeg:
    def gen(path, secs=2):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=%d:size=320x240:rate=10" % secs,
             "-f", "lavfi", "-i", "sine=frequency=440:duration=%d" % secs,
             "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", path],
            capture_output=True, timeout=60, check=True)
    for p in (movie_file, ep1_file, ep2_file):
        gen(p)
else:
    for p in (movie_file, ep1_file, ep2_file):
        with open(p, "wb") as f:
            f.write(b"\x00" * 1024)
    # srt 外挂字幕
    with open(os.path.join(lib_dir, "Show B", "Season 1", "Show B S01E01.chi.srt"), "w") as f:
        f.write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")

# 媒体库
lib = em.Library(guid="a" * 32, name="测试电影库", collection_type="movies", paths=lib_dir)
db.add(lib)
db.commit()
lib_id = lib.id
db.close()

from backend.emby_server.scanner import scan_library_sync  # noqa: E402

db = Session()
lib = db.query(em.Library).get(lib_id)
stats = scan_library_sync(db, lib)
print("SCAN STATS:", stats)
counts = {}
for it in db.query(em.MediaItem).all():
    counts[it.item_type] = counts.get(it.item_type, 0) + 1
print("ITEM COUNTS:", counts)
assert counts.get("movie") == 1, "1 部电影"
assert counts.get("series") == 1, "1 部剧集"
assert counts.get("season") == 1, "1 季"
assert counts.get("episode") == 2, "2 集"
db.close()

# ---- API 测试 ----
from backend.main import app  # noqa: E402

client = TestClient(app)

# 系统信息
r = client.get("/emby/system/info/public")
assert r.status_code == 200, r.text
assert "ServerName" in r.json()
print("OK /emby/system/info/public")

# 认证（正确密码）
r = client.post("/emby/Users/AuthenticateByName",
                json={"Username": "alice", "Pw": "alice-play-pw"},
                headers={"X-Emby-Authorization": 'MediaBrowser Client="Infuse", Device="Test", DeviceId="dev1", Version="7.0"'})
assert r.status_code == 200, r.text
auth = r.json()

# 认证（错误密码必须拒绝）
r = client.post("/emby/Users/AuthenticateByName",
                json={"Username": "alice", "Pw": "nope"},
                headers={"X-Emby-Authorization": 'MediaBrowser Client="Infuse", Device="Test", DeviceId="dev1b", Version="7.0"'})
assert r.status_code == 401, f"错误密码应拒绝, got {r.status_code}"
print("OK wrong password rejected")
token = auth["AccessToken"]
user_id = auth["User"]["Id"]
print("OK AuthenticateByName -> token", token[:8], "user", user_id)
H = {"X-Emby-Token": token}

# 视图（媒体库列表）
r = client.get(f"/emby/Users/{user_id}/Views", headers=H)
assert r.status_code == 200 and len(r.json()["Items"]) == 1, r.text
lib_guid = r.json()["Items"][0]["Id"]
print("OK Views")

# 条目查询
r = client.get(f"/emby/Users/{user_id}/Items?Recursive=true&Limit=50", headers=H)
assert r.status_code == 200, r.text
items = r.json()["Items"]
types = {}
for it in items:
    types.setdefault(it["Type"], []).append(it)
print("OK Items:", {k: len(v) for k, v in types.items()})
assert "Movie" in types and "Series" in types and "Episode" in types

movie = types["Movie"][0]
series = types["Series"][0]

# 详情
r = client.get(f"/emby/Users/{user_id}/Items/{movie['Id']}", headers=H)
assert r.status_code == 200, r.text
detail = r.json()
assert detail["MediaSources"], "详情应包含 MediaSources"
# EMBY_HIDE_MEDIA_PATH 默认开启：客户端响应不再下发服务器路径
ms0 = detail["MediaSources"][0]
print("OK Item detail, path hidden:", "Path" not in ms0)

# 播放信息
r = client.post(f"/emby/Items/{movie['Id']}/PlaybackInfo", headers=H, json={"MaxStreamingBitrate": 8000000})
assert r.status_code == 200, r.text
print("OK PlaybackInfo")

# 直连流（Range）
r = client.get(f"/emby/Videos/{movie['Id']}/stream?api_key={token}",
               headers={"Range": "bytes=0-1023"})
assert r.status_code == 206, (r.status_code, r.text[:200])
assert r.headers.get("content-range", "").startswith("bytes 0-1023/")
print("OK Direct stream with Range (206)")

# 图片（应 404 或 200，不能 500）
r = client.get(f"/emby/Items/{movie['Id']}/Images/Primary")
assert r.status_code in (200, 404), r.status_code
print("OK Images endpoint")

# 会话上报
r = client.post("/emby/Sessions/Playing", headers=H,
                json={"ItemId": movie["Id"], "PositionTicks": 5_000_000, "PlaySessionId": "sess-1"})
assert r.status_code == 200, r.text
r = client.post("/emby/Sessions/Playing/Progress", headers=H,
                json={"ItemId": movie["Id"], "PositionTicks": 15_000_000, "PlaySessionId": "sess-1"})
assert r.status_code == 200, r.text
r = client.get("/emby/Sessions", headers=H)
assert r.status_code == 200 and len(r.json()) >= 1, r.text
print("OK Sessions reporting")

# 续看
r = client.get(f"/emby/Users/{user_id}/Items/Resume", headers=H)
assert r.status_code == 200 and len(r.json()["Items"]) >= 1, r.text
print("OK Resume list")

# 收藏
r = client.post(f"/emby/Users/{user_id}/Items/{movie['Id']}/Rating", headers=H,
                json={"IsFavorite": True})
assert r.status_code == 200 and r.json()["IsFavorite"] is True, r.text
r = client.get(f"/emby/Users/{user_id}/FavoriteItems", headers=H)
assert len(r.json()["Items"]) == 1
print("OK Favorites")

# 剧集
r = client.get(f"/emby/Shows/{series['Id']}/Seasons", headers=H)
assert r.status_code == 200 and len(r.json()["Items"]) == 1, r.text
r = client.get(f"/emby/Shows/{series['Id']}/Episodes", headers=H)
assert r.status_code == 200 and len(r.json()["Items"]) == 2, r.text
print("OK Seasons/Episodes")

# 门户 API（JWT）
from backend.security import create_access_token  # noqa: E402

db2 = Session()
alice_id = db2.query(models.WebUser).filter(models.WebUser.username == "alice").first().id
bob = models.WebUser(username="bob", password_hash="x", is_active=True, is_staff=False)
db2.add(bob)
db2.commit()
bob_id = bob.id
db2.close()

jwt_token = create_access_token(alice_id, {"username": "alice"})
r = client.get("/api/user/emby/server", headers={"Authorization": f"Bearer {jwt_token}"})
assert r.status_code == 200, r.text
server_info = r.json()
# 查看权限 gating：alice 无订阅、默认服为付费服 → 敏感字段必须置空不下发
assert not server_info["emby_username"], server_info
assert not server_info["base_url"], server_info
assert server_info["import_schemes"] == {}, server_info
assert server_info["view_permission"]["granted"] is False, server_info
print("OK portal /server hides account without view permission")

# 给 alice 一个有效订阅 → 查看权限恢复，账号卡完整下发
from datetime import datetime, timedelta  # noqa: E402
from backend import realms as realm_lib  # noqa: E402

db3 = Session()
realm_id = realm_lib.active_realm_id(db3)
plan = models.SubscriptionPlan(name="测试月卡", price=9.99, duration_days=31, realm_id=realm_id)
db3.add(plan)
db3.flush()
db3.add(models.UserSubscription(user_id=alice_id, plan_id=plan.id, realm_id=realm_id,
                                status="active",
                                end_date=datetime.now() + timedelta(days=30)))
db3.commit()
db3.close()

r = client.get("/api/user/emby/server", headers={"Authorization": f"Bearer {jwt_token}"})
assert r.status_code == 200, r.text
server_info = r.json()
assert server_info["emby_username"], server_info
assert server_info["base_url"], server_info
assert server_info["view_permission"]["granted"] is True, server_info
assert not server_info.get("emby_password"), "账号卡不应返回明文密码"
print("OK portal /server:", server_info["server_name"], server_info["base_url"])

r = client.get("/api/user/emby/resume", headers={"Authorization": f"Bearer {jwt_token}"})
assert r.status_code == 200, r.text
print("OK portal /resume")

# 管理端（需 is_staff；无凭证应 401，非 staff 应 403）
r = client.get("/api/admin/emby/overview")
assert r.status_code == 401, f"未认证应 401, got {r.status_code}"
bob_token = create_access_token(bob_id, {"username": "bob"})
r = client.get("/api/admin/emby/overview", headers={"Authorization": f"Bearer {bob_token}"})
assert r.status_code == 403, f"非 staff 应 403, got {r.status_code}"
print("OK admin endpoints reject anonymous and non-staff")

r = client.post("/api/admin/emby/libraries", json={"name": "库2", "collection_type": "tvshows",
                                                   "paths": [lib_dir]},
                headers={"Authorization": f"Bearer {jwt_token}"})
assert r.status_code == 200, r.text
r = client.get("/api/admin/emby/overview", headers={"Authorization": f"Bearer {jwt_token}"})
assert r.status_code == 200, r.text
print("OK admin overview:", r.json())

# HLS 转码已删除（2026-10-17）：master.m3u8 端点不再存在，转码入口返回 404。
r = client.get(f"/emby/videos/{movie['Id']}/master.m3u8?VideoBitrate=2000000", headers=H)
assert r.status_code == 404, f"转码端点应 404, got {r.status_code}"
print("OK HLS transcode endpoint removed (404)")

print("\n✅ 全部冒烟测试通过")
shutil.rmtree(lib_dir, ignore_errors=True)
os.remove(DB)
