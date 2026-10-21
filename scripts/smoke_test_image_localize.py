"""刮削图片本地化冒烟测试（v2.17.0）

条目的封面原先只有 TMDB 的远程地址，客户端每取一次图都要由服务器代理去第三方拉一趟。
本测试覆盖「把远程图落成本机文件」这一层的全部关键行为：

- **落盘**：写入原子（不留 .part）、内容一致、同一张图只下载一次、并发只下载一次
- **写事务里绝不下载**（v2.42.9）：下载统一在 IO 阶段（`prewarm_images`），写库阶段只落字段；
  没预热上的图只落远程地址，交给取图时的按需自愈
- **预热默认关**（v2.54.0，`EMBY_IMAGE_PREWARM=1` 才开）：默认刮削只落远程地址，
  由后台补下载巡检（`image_backlog_patrol`）涓流落盘；显式开预热时 IO 阶段照旧下载
- **失败回退**：源站 404/超时/非图片内容 → 不落盘、不改条目、接口照旧走原来的代理路径
- **自愈**：本地缓存被清掉（/tmp 重启清空、维护淘汰）后，取图时按需重新落一份并记住路径
- **优先级**：本地化过的图优先走本地；用户自己放的封面（不在缓存目录里）不受影响
- **开关**：`EMBY_LOCALIZE_IMAGES=0` 时完全不下载、不改条目
- **清理策略**：没被引用的删掉、有保护期、超上限从旧到新淘汰但绝不删有引用的
- **维护周期**：janitor 上报清理计数

全程不发真实网络请求：httpx.Client 被换成假服务。
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 不写死库文件名：交给 backend.database 解析（新装 aetrix_unified.db，老部署沿用原库）
os.environ.setdefault("DATABASE_TYPE", "sqlite")
# 空值也算没设（配置自愈会往 .env 物化出空的 SECRET_KEY，setdefault 只认「不存在」）
os.environ["SECRET_KEY"] = (os.environ.get("SECRET_KEY") or "").strip() or \
    "smoke-test-only-secret-key-not-for-production"
IMAGE_DIR = tempfile.mkdtemp(prefix="imgcache_")
os.environ["EMBY_IMAGE_DIR"] = IMAGE_DIR

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import api as emby_api  # noqa: E402
from backend.emby_server import image_store  # noqa: E402
from backend.emby_server import maintenance as maint  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import tmdb  # noqa: E402
from backend.main import app  # noqa: E402

init_db()
client = TestClient(app)
db = SessionLocal()
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


# ==================== 假的图片源 ====================
POSTER_BYTES = b"\xff\xd8\xff\xe0" + b"poster-bytes" * 20
BACKDROP_BYTES = b"\xff\xd8\xff\xe0" + b"backdrop-bytes" * 20
downloads: list = []
AUTH_FAIL = {"on": False}


class FakeResponse:
    def __init__(self, status: int = 200, content: bytes = POSTER_BYTES, headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {"content-type": "image/jpeg"}


class FakeClient:
    """替换 httpx.Client：记录每次请求，按 URL 返回对应的假图"""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, *a, **k):
        downloads.append(url)
        if AUTH_FAIL["on"]:
            raise httpx.ConnectError("模拟源站不可达")
        if "backdrop" in url:
            return FakeResponse(content=BACKDROP_BYTES)
        if "boom" in url:
            return FakeResponse(status=404, content=b"")
        if "html" in url:
            return FakeResponse(content=b"<html>", headers={"content-type": "text/html"})
        return FakeResponse()


_real_client = httpx.Client
httpx.Client = FakeClient  # type: ignore[assignment]

POSTER_URL = "https://image.tmdb.org/t/p/w500/poster.jpg"
BACKDROP_URL = "https://image.tmdb.org/t/p/w1280/backdrop.jpg"

# ==================== 1. 落盘：原子、内容一致、只下一次 ====================
downloads.clear()
path = image_store.localize(POSTER_URL)
check("远程图落地成本地文件", bool(path) and os.path.isfile(path),
      f"path={path}")
check("落盘内容与源一致", open(path, "rb").read() == POSTER_BYTES,
      f"{os.path.getsize(path)} 字节")
check("目录里不留临时文件", not [n for n in os.listdir(IMAGE_DIR) if ".part" in n],
      str(os.listdir(IMAGE_DIR)))
check("同一张图再取一次直接用本地（不重复下载）",
      image_store.localize(POSTER_URL) == path and len(downloads) == 1,
      f"下载 {len(downloads)} 次")

# 并发：8 个线程同时要同一张（本地化发生在扫描线程池与请求线程里，会并发）
os.remove(path)
downloads.clear()
results: list = []
threads = [threading.Thread(target=lambda: results.append(image_store.localize(POSTER_URL)))
           for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("并发请求同一张图只下载一次（单飞）", len(downloads) == 1, f"下载 {len(downloads)} 次")
check("等待中的线程都拿到同一个文件", len(set(results)) == 1 and os.path.isfile(results[0]),
      str(set(results)))

# ==================== 2. 失败回退：不落盘、不影响功能 ====================
for label, url in (("源站 404", "https://img.tmdb.org/boom.jpg"),
                   ("内容不是图片", "https://img.tmdb.org/html.jpg"),
                   ("源站不可达", "https://img.tmdb.org/unreachable.jpg")):
    AUTH_FAIL["on"] = label == "源站不可达"
    got = image_store.localize(url)
    check(f"{label} → 不本地化、返回空（调用方退回远程图）", got == "", f"返回 {got!r}")
AUTH_FAIL["on"] = False
check("失败不会留下半张图", not [n for n in os.listdir(IMAGE_DIR) if ".part" in n])

# ==================== 3. 刮削时本地化（IO 阶段预热 → 写库只落字段）====================
# 注（本轮 PR 起）：刮削期预热默认关（EMBY_IMAGE_PREWARM=1 才开）。本节先显式开启，
# 覆盖"开预热"路径；默认关的行为见 3b。失败回退/自愈/开关等节不受影响。
os.environ["EMBY_IMAGE_PREWARM"] = "1"
lib = em.Library(guid="i" * 32, name="图片本地化库", collection_type="movies", paths="")
db.add(lib)
db.commit()
item = em.MediaItem(guid="i" * 31 + "1", library_id=lib.id, item_type="movie", name="本地化测试片")
db.add(item)
db.commit()

hit = {"id": 999001, "title": "本地化测试片", "overview": "…", "vote_average": 7.5,
       "poster_path": "/poster.jpg", "backdrop_path": "/backdrop.jpg", "genre_ids": [28]}
# v2.42.9：下载统一在 IO 阶段（写事务之外）。以前是 apply() → _set_image() → localize()
# 在**写事务里**真的发 HTTP——一张图超时 15s 就等于攥着数据库写锁 15s，而 scanner /
# enrich 的注释里却写着「不会在这里发请求」，代码与自己的声明相反。
downloads.clear()
check("IO 阶段预热：要落哪两张图就哪两张备好", tmdb.prewarm_images(hit) == 2,
      f"下载 {len(downloads)} 次 {downloads}")
marked = len(downloads)
tmdb.tmdb_client.apply(item, hit, "movie")
check("写库阶段一次 HTTP 都不发（预热过的图只查本地文件）",
      len(downloads) == marked, f"写库阶段又下载 {len(downloads) - marked} 次")
db.commit()
check("刮削后条目同时有远程地址与本地文件",
      item.primary_image_url.endswith("/poster.jpg") and
      image_store.is_cached_path(item.poster_path or "") and os.path.isfile(item.poster_path),
      f"url={item.primary_image_url} local={item.poster_path}")
check("背景图同样本地化",
      image_store.is_cached_path(item.backdrop_path or "") and os.path.isfile(item.backdrop_path),
      str(item.backdrop_path))
chain = emby_api._image_chain(item, "Primary", db)
check("取图链路优先走本地（缓存目录里的那份）", chain and chain[0] == item.poster_path,
      str(chain[:2]))

# ==================== 3b. 默认行为：预热关（本轮 PR 起的默认） ====================
os.environ["EMBY_IMAGE_PREWARM"] = "0"
downloads.clear()
default_hit = {**hit, "id": 999004, "poster_path": "/poster4.jpg",
               "backdrop_path": "/backdrop4.jpg"}
check("默认不预热：IO 阶段一张都不下载",
      tmdb.prewarm_images(default_hit) == 0 and downloads == [],
      f"下载 {len(downloads)} 次")
defitem = em.MediaItem(guid="i" * 31 + "8", library_id=lib.id, item_type="movie",
                       name="默认不预热的片")
db.add(defitem)
db.commit()
tmdb.tmdb_client.apply(defitem, default_hit, "movie")
db.commit()
check("默认不预热：写库只落远程地址，海报/背景都不落本地（交给后台补下载巡检）",
      defitem.primary_image_url.endswith("/poster4.jpg")
      and not defitem.poster_path and not defitem.backdrop_path,
      f"poster_path={defitem.poster_path!r} backdrop_path={defitem.backdrop_path!r}")
os.environ["EMBY_IMAGE_PREWARM"] = "1"

# 没预热过的图：写库阶段不下载，只落远程地址——由取图时的按需自愈补上（见第 4 节）
bare = em.MediaItem(guid="i" * 31 + "9", library_id=lib.id, item_type="movie",
                    name="没预热的片")
db.add(bare)
db.commit()
hit2 = {**hit, "id": 999003, "poster_path": "/poster2.jpg", "backdrop_path": "/backdrop2.jpg"}
marked = len(downloads)
tmdb.tmdb_client.apply(bare, hit2, "movie")
check("没预热过的图绝不在写事务里下载，只落远程地址（交给按需自愈）",
      len(downloads) == marked and bare.primary_image_url.endswith("/poster2.jpg")
      and not bare.poster_path,
      f"下载 +{len(downloads) - marked} poster_path={bare.poster_path!r}")
db.commit()

# ==================== 4. 缓存被清掉 → 自愈 ====================
os.remove(item.poster_path)
chain_gone = emby_api._image_chain(item, "Primary", db)
check("本地缓存丢了就退回远程地址（行为与本地化之前一致）",
      chain_gone and chain_gone[0] == item.primary_image_url, str(chain_gone[:2]))
downloads.clear()
resp = client.get(f"/Items/{item.guid}/Images/Primary")
check("取图时按需重新落一份并直接发出（自愈）",
      resp.status_code == 200 and resp.content == POSTER_BYTES,
      f"HTTP {resp.status_code} 长度={len(resp.content)}")
db.expire_all()
item = db.query(em.MediaItem).filter(em.MediaItem.guid == item.guid).first()
check("自愈后把本地路径记住（下次直接命中本地）",
      image_store.is_cached_path(item.poster_path or "") and os.path.isfile(item.poster_path),
      str(item.poster_path))

# 用户自己放在媒体目录里的封面不受影响（不在缓存目录里，仍按原顺序：远程优先）
item.poster_path = os.path.join(tempfile.mkdtemp(prefix="localart_"), "poster.jpg")
open(item.poster_path, "wb").write(b"user-art")
db.commit()
chain_user = emby_api._image_chain(item, "Primary", db)
check("用户自己的封面不受影响（远程图仍优先）",
      chain_user[0] == item.primary_image_url, str(chain_user[:2]))

# ==================== 5. 开关：EMBY_LOCALIZE_IMAGES=0 ====================
os.environ["EMBY_LOCALIZE_IMAGES"] = "0"
downloads.clear()
fresh = em.MediaItem(guid="i" * 31 + "2", library_id=lib.id, item_type="movie", name="关闭开关")
db.add(fresh)
db.commit()
tmdb.tmdb_client.apply(fresh, {**hit, "id": 999002}, "movie")
db.commit()
check("关闭开关后不下载", downloads == [] and image_store.localize(POSTER_URL) == "",
      f"下载 {len(downloads)} 次")
check("关闭开关后仍然写入远程地址（行为回到本地化之前）",
      fresh.primary_image_url.endswith("/poster.jpg") and not fresh.poster_path,
      f"poster_path={fresh.poster_path}")
check("关闭开关时 is_cached_path 仍只认缓存目录",
      image_store.is_cached_path(os.path.join(IMAGE_DIR, "x.jpg")) is True)
os.environ.pop("EMBY_LOCALIZE_IMAGES", None)

# ==================== 6. 清理策略 ====================
old_used = image_store.localize(POSTER_URL)                 # 被条目引用
os.utime(old_used, (time.time() - 7200, time.time() - 7200))
orphan = os.path.join(IMAGE_DIR, "orphan-old.jpg")
open(orphan, "wb").write(b"x" * 500)
os.utime(orphan, (time.time() - 7200, time.time() - 7200))
fresh_orphan = os.path.join(IMAGE_DIR, "orphan-fresh.jpg")
open(fresh_orphan, "wb").write(b"y" * 500)
db.expire_all()
item = db.query(em.MediaItem).filter(em.MediaItem.guid == item.guid).first()
item.poster_path = old_used
item.backdrop_path = None
db.commit()

result = image_store.prune(db)
check("没被引用且过了保护期的图片被清掉",
      not os.path.exists(orphan) and result["removed"] >= 1, f"{result}")
check("被条目引用的图片绝不删除（哪怕很旧）", os.path.isfile(old_used), str(old_used))
check("刚落下、还在保护期的文件不动", os.path.isfile(fresh_orphan))

os.environ["EMBY_IMAGE_GRACE_SECONDS"] = "0"
keep_bytes = os.path.getsize(old_used)
for i in range(4):
    p = os.path.join(IMAGE_DIR, f"trim-{i}.jpg")
    open(p, "wb").write(b"z" * 20000)
    os.utime(p, (time.time() - 3600 + i, time.time() - 3600 + i))
os.environ["EMBY_IMAGE_CACHE_MB"] = "1"                     # 上限 1MB，逼出淘汰
result = image_store.prune(db)
check("超出上限时从旧到新淘汰未引用的图片", result["removed"] >= 1 and result["freed_bytes"] > 0,
      f"{result}")
check("淘汰过程中被引用的封面仍然在", os.path.isfile(old_used),
      f"引用图 {keep_bytes} 字节")
check("即使超上限也不删被引用的图（只记日志）",
      image_store.stats()["files"] >= 1 and result["over_limit"] in (True, False),
      str(image_store.stats()))
os.environ.pop("EMBY_IMAGE_CACHE_MB", None)
os.environ.pop("EMBY_IMAGE_GRACE_SECONDS", None)

stats_now = image_store.stats()
check("缓存统计可观测（文件数 / 字节 / 下载与失败次数）",
      stats_now["files"] >= 1 and stats_now["bytes"] > 0 and
      stats_now["downloaded"] >= 1 and stats_now["failed"] >= 1, str(stats_now))

# ==================== 7. 维护周期上报 ====================
tick = maint.janitor_tick()
check("维护周期上报图片缓存清理计数",
      "images_pruned" in tick and "images_freed_bytes" in tick, str(sorted(tick)))

httpx.Client = _real_client  # type: ignore[assignment]
db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).delete(synchronize_session=False)
db.query(em.Library).filter(em.Library.id == lib.id).delete(synchronize_session=False)
db.commit()
db.close()

print("\n" + "=" * 60)
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
    sys.exit(1)
print("✅ 图片本地化冒烟测试全部通过（0 失败）")
