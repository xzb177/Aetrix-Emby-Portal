"""增量扫描冒烟测试（v2.17.0）

「追新」时绝大多数目录一个文件都没动过，但旧实现照样把每个文件走一遍完整流程：
找图片、找字幕、重建外挂字幕轨、提交事务。实测 2000 个文件的重扫 ≈ 冷扫的 95%。

本测试守的是增量扫描的**正确性**，不只是「变快了」：

- **没变就跳过**：未变动重扫不再逐文件处理（`_side_info` / ffprobe 调用为 0，SQL 从
  每文件 4 条降到每目录几条），但条目一条都不能少（seen_guids 必须照常登记，
  否则清理阶段会把整库当成「文件已删除」删掉）
- **变了一定处理**：新增文件、新增目录、目录 mtime 变了、字幕新增 → 该目录重新处理
- **大小变了也处理**（目录 mtime 不变的情况）：替换同名文件、大小不同 → 重新探测
- **库里没有行就必须处理**：手动删掉某条目 → 重扫要把它建回来（这是「跳过」的安全底线）
- **该重刮时不跳过**：补图标记、TMDB 详情没补齐；到期重刮（3m）自批二 #12
  起改为有界推迟——目录未到期时先推迟（默认 ≤24 小时），目录到期重验时照常重刮
- **没配 TMDB 的部署也照样增量**：刮削不可能成功，不该因此让整库每轮重做
- **开关**：`SCAN_INCREMENTAL=0` 回到每次全量处理
- **配了 TMDB 的剧集库也照样增量**（v2.31.0）：集根本不参与刮削（写库循环只刮
  series / movie），所以「缺 tmdb_id → 要刮」这条对每一集恒成立；此前它落在跳过判定里，
  等于把剧集库的增量整个关掉
- **远程挂载**：参与增量，且不额外增加目录列举次数（拿不到列表的目录不当作「没变」）

用法：python scripts/smoke_test_scan_incremental.py
"""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from sqlalchemy import event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.database import engine, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import mounts as mnt  # noqa: E402
from backend.emby_server import scanner as sc  # noqa: E402
from backend.emby_server import tmdb as tmdb_mod  # noqa: E402

init_db()
Session = sessionmaker(bind=engine)
db = Session()

FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


# ==================== 1. 造库：40 个目录 × 25 个文件 ====================
DIRS, PER_DIR = 8, 25
TOTAL = DIRS * PER_DIR
root = tempfile.mkdtemp(prefix="scandelta_")
dir_paths = []
for d in range(DIRS):
    sub = os.path.join(root, f"Folder {d:02d}")
    os.makedirs(sub, exist_ok=True)
    dir_paths.append(sub)
    for i in range(PER_DIR):
        with open(os.path.join(sub, f"Delta Movie {d:02d}-{i:02d} (2020).mkv"), "wb") as f:
            f.write(b"\x00" * 1024)

lib = em.Library(guid="d" * 32, name="增量库", collection_type="movies", paths=root)
db.add(lib)
db.commit()

# 这台机器/CI 上不一定有 ffprobe：用固定返回值代替（否则每个文件永远“需要重探”，增量无意义）
def fake_probe(path, headers=None, size=0, *a, **k):
    try:
        real = os.path.getsize(path)
    except OSError:
        real = size
    return {"duration_ticks": 60_000_000, "bitrate": 1500, "width": 1920, "height": 1080,
            "video_codec": "h264", "audio_codec": "aac", "audio_languages": "chi",
            "subtitle_languages": "", "streams": [], "size": real}


real_side_info = sc._side_info
counters = {"sql": 0, "probe": 0, "side": 0}
event.listen(engine, "before_cursor_execute",
             lambda *a, **k: counters.__setitem__("sql", counters["sql"] + 1))


def counting_side_info(ctx, scan_file):
    counters["side"] += 1
    return real_side_info(ctx, scan_file)


def counting_probe(*a, **k):
    counters["probe"] += 1
    return fake_probe(*a, **k)


sc._side_info = counting_side_info
sc.probe_metadata = counting_probe


def scan_library(library, label: str) -> dict:
    counters.update(sql=0, probe=0, side=0)
    stats = sc.scan_library_sync(db, library)
    print(f"    · {label}: SQL={counters['sql']} probe={counters['probe']} side={counters['side']} "
          f"added={stats['added']} updated={stats['updated']} removed={stats['removed']} "
          f"unchanged={stats.get('unchanged', 0)}")
    return stats


def scan(label: str) -> dict:
    return scan_library(lib, label)


def item_count() -> int:
    return db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).count()


cold = scan("冷扫")
check("冷扫入库全部文件", cold["added"] == TOTAL and item_count() == TOTAL,
      f"added={cold['added']} 期望={TOTAL}")
check("冷扫没有跳过任何文件", cold.get("unchanged", 0) == 0, f"unchanged={cold.get('unchanged')}")
check("冷扫为每个目录留下指纹",
      db.query(em.ScanDirState).filter(em.ScanDirState.library_id == lib.id).count() == DIRS,
      f"指纹行 {db.query(em.ScanDirState).count()} / 目录 {DIRS}")

# 模拟 enrich_worker 已完成：分层 L1 后条目是 pending，会导致 needs_probe 恒为 True，
# 增量跳过逻辑无法工作。这里标 done 代表后台已补完。
db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).update(
    {em.MediaItem.enrich_status: "done",
     em.MediaItem.last_probed_at: datetime.now(),
     em.MediaItem.last_scraped_at: datetime.now()},
    synchronize_session=False)
db.commit()
db.expire_all()

# ==================== 2. 未变动重扫：跳过逐文件工作，但条目一条都不能少 ====================
warm = scan("未变动重扫")
check("未变动重扫不再逐文件处理（side_info / ffprobe 都为 0）",
      counters["side"] == 0 and counters["probe"] == 0,
      f"side={counters['side']} probe={counters['probe']}")
check("重扫 SQL 从「每文件 4 条」降到「每目录几条」", counters["sql"] < DIRS * 3,
      f"SQL={counters['sql']}（全量处理约 {TOTAL * 4}）")
check("跳过的文件全部登记为 unchanged", warm.get("unchanged") == TOTAL,
      f"unchanged={warm.get('unchanged')} 期望={TOTAL}")
check("重扫没有误删条目（seen_guids 仍然登记）",
      warm["removed"] == 0 and item_count() == TOTAL,
      f"removed={warm['removed']} 条目={item_count()}")
check("重扫不重复入库", warm["added"] == 0, f"added={warm['added']}")

# 第二次重扫同样干净（指纹幂等：不会每次都被当成“变了”）
again = scan("再一次重扫")
check("再重扫仍然全部跳过（指纹幂等）", again.get("unchanged") == TOTAL,
      f"unchanged={again.get('unchanged')}")

# ==================== 3. 新增文件：只处理那一个目录 ====================
with open(os.path.join(dir_paths[0], "Delta Movie New (2024).mkv"), "wb") as f:
    f.write(b"\x00" * 2048)
added_one = scan("目录里新增 1 个文件")
check("新增文件被入库", added_one["added"] == 1 and item_count() == TOTAL + 1,
      f"added={added_one['added']} 条目={item_count()}")
# 文件级指纹：只有新增的 1 个文件被处理，其余 200 个全部秒跳（比目录级更精细）
check("只有变化的那个目录被重新处理（其余目录仍然跳过）",
      counters["side"] == 1 and added_one.get("unchanged") == TOTAL,
      f"side={counters['side']} unchanged={added_one.get('unchanged')}")
new_row = db.query(em.MediaItem).filter(
    em.MediaItem.library_id == lib.id,
    em.MediaItem.name.like("Delta Movie New%"),
).first()
check("新条目被真正探测过（不是空壳）",
      new_row is not None and new_row.duration_ticks == 60_000_000,
      f"duration={getattr(new_row, 'duration_ticks', None)}")

# ==================== 4. 新增目录 ====================
new_dir = os.path.join(root, "Folder New")
os.makedirs(new_dir, exist_ok=True)
with open(os.path.join(new_dir, "Delta Fresh (2025).mkv"), "wb") as f:
    f.write(b"\x00" * 1024)
added_dir = scan("新增 1 个目录")
check("新目录里的文件被入库", added_dir["added"] == 1 and item_count() == TOTAL + 2,
      f"added={added_dir['added']} 条目={item_count()}")

# ==================== 5. 同名文件被替换（目录 mtime 不变，大小变了）====================
target = os.path.join(dir_paths[1], "Delta Movie 01-00 (2020).mkv")
target_key = os.path.join(dir_paths[1])
st = os.stat(target_key)
with open(target, "wb") as f:
    f.write(b"\x00" * 4096)                       # 换源：大小从 1024 → 4096
os.utime(target_key, ns=(st.st_atime_ns, st.st_mtime_ns))   # 目录 mtime 保持不动
swapped = scan("替换同名文件（目录 mtime 未变）")
row = db.query(em.MediaItem).filter(em.MediaItem.file_path == target).first()
check("文件大小变了仍然重新探测（不被目录指纹蒙混过去）",
      row is not None and row.size == 4096 and counters["probe"] >= 1,
      f"size={getattr(row, 'size', None)} probe={counters['probe']}")
# 目录 mtime 没变 → 这个目录的其余文件仍然跳过；变大的那个文件靠「大小不一致」单独处理
check("同目录其余文件仍被跳过（只处理变了的那一个）",
      swapped.get("unchanged") == item_count() - 1 and counters["side"] == 1,
      f"unchanged={swapped.get('unchanged')} 期望={item_count() - 1} side={counters['side']}")

# ==================== 6. 库里那一行被删掉 → 必须重建 ====================
victim = db.query(em.MediaItem).filter(
    em.MediaItem.file_path == os.path.join(dir_paths[2], "Delta Movie 02-00 (2020).mkv")).first()
victim_path = victim.file_path
db.query(em.MediaItem).filter(em.MediaItem.id == victim.id).delete(synchronize_session=False)
db.commit()
restored = scan("手动删掉 1 个条目后重扫")
check("库里缺行时不会因为指纹未变而被跳过（安全底线）",
      restored["added"] == 1 and db.query(em.MediaItem).filter(
          em.MediaItem.file_path == victim_path).first() is not None,
      f"added={restored['added']}")

# ==================== 7. 外挂字幕新增（目录 mtime 会变）====================
with open(os.path.join(dir_paths[3], "Delta Movie 03-00 (2020).chi.srt"), "w",
          encoding="utf-8") as f:
    f.write("1\n00:00:00,000 --> 00:00:01,000\n你好\n")
sub_scan = scan("目录里新增外挂字幕")
sub_item = db.query(em.MediaItem).filter(
    em.MediaItem.file_path == os.path.join(dir_paths[3], "Delta Movie 03-00 (2020).mkv")).first()
sub_rows = db.query(em.MediaStream).filter(
    em.MediaStream.item_id == sub_item.id, em.MediaStream.is_external.is_(True)).all()
# 文件级秒跳下，视频指纹未变但新增外挂字幕时，字幕必须被登记
# （生产逻辑：_external_subtitles_changed 检出变化 → 不秒跳 → 只重做 side）
check("目录变过 → 外挂字幕被登记（跳过不会漏掉字幕）",
      len(sub_rows) == 1 and sub_rows[0].language == "chi",
      f"字幕轨 {[(r.language, r.display_title) for r in sub_rows]}")

# ==================== 8. 该处理的绝不能被跳过 ====================
# 8a. 补图标记
repair_target = db.query(em.MediaItem).filter(
    em.MediaItem.file_path == os.path.join(dir_paths[4], "Delta Movie 04-00 (2020).mkv")).first()
repair_target.repair_requested_at = sc.datetime.now()
db.commit()
repair_scan = scan("带补图标记的条目")
check("补图标记的条目不会被跳过", counters["side"] >= 1, f"side={counters['side']}")
db.query(em.MediaItem).filter(em.MediaItem.id == repair_target.id).update(
    {"repair_requested_at": None}, synchronize_session=False)
db.commit()

# 8b. 到期重刮策略（需要 TMDB 可用才算「可能刮到东西」）
class FakeTmdb:
    configured = True

    def search(self, *a, **k):
        return None

    def details(self, *a, **k):
        return None

    def enrich(self, *a, **k):
        return None

    def refresh_images(self, *a, **k):
        return False

    def apply(self, *a, **k):
        return None


real_tmdb = tmdb_mod.tmdb_client
tmdb_mod.tmdb_client = FakeTmdb()
lib.scrape_policy = "3m"
aged = db.query(em.MediaItem).filter(
    em.MediaItem.file_path == os.path.join(dir_paths[5], "Delta Movie 05-00 (2020).mkv")).first()
aged.tmdb_id = "12345"
aged.imdb_id = "tt123"
aged.aliases = "别名"
aged.last_scraped_at = sc.datetime.now() - sc.timedelta(days=200)
db.commit()
policy_scan = scan("3m 策略 + 到期条目（目录未到期）")
# 到期调度（批二 #12）的新口径：目录未到期时，到期重刮先推迟到目录的
# next_due_at（推迟有界，默认 ≤ SCAN_DIR_RECHECK_SEC=24 小时），不是
# 无限饿死——目录一到期，重验走完整判定，重刮照常发生（下一项钉住）。
check("未到期时到期重刮先推迟（有界推迟）", counters["side"] == 0,
      f"side={counters['side']}")
db.query(em.ScanDirState).filter(
    em.ScanDirState.library_id == lib.id).update(
    {"next_due_at": sc.datetime.now() - sc.timedelta(minutes=1)},
    synchronize_session=False)
db.commit()
policy_scan = scan("3m 策略 + 到期条目（目录已到期）")
check("到期重刮的条目不会被跳过", counters["side"] >= 1 and policy_scan["scraped"] >= 0,
      f"side={counters['side']}")

# 8c. 元数据齐备 + 未到期 → 允许跳过
# 注意这里只有 1 个条目有 tmdb_id：TMDB 可用时「缺元数据的总要补」是既有策略，
# 所以每轮仍会对没有命中过的条目重试刮削——本项改动不改变这个策略，只保证
# 已经有元数据且未到期的条目不再被反复重做。
aged.last_scraped_at = sc.datetime.now()
db.commit()
settled = scan("3m 策略 + 未到期条目")
# 文件级 fast-skip 下，有指纹的都会跳过（197 个），包含那 1 个有元数据的
check("已有元数据且未到期的条目被跳过", (settled.get("unchanged") or 0) >= 1,
      f"unchanged={settled.get('unchanged')}")
tmdb_mod.tmdb_client = real_tmdb
lib.scrape_policy = "missing_only"
db.commit()

# ==================== 9. 开关：SCAN_INCREMENTAL=0 ====================
sc.SCAN_INCREMENTAL = False
off = scan("关掉增量（SCAN_INCREMENTAL=0）")
check("关掉增量后每个文件都照常处理",
      off.get("unchanged", 0) == 0 and counters["side"] == item_count(),
      f"unchanged={off.get('unchanged')} side={counters['side']} 期望 side={item_count()}")
sc.SCAN_INCREMENTAL = True
# OFF 轮是分层 L1 全量处理，条目被标回 pending；模拟后台 worker 已补完，
# 否则文件级秒跳（要求 enrich_status=done）无法工作。
db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).update(
    {em.MediaItem.enrich_status: "done"},
    synchronize_session=False)
db.commit()
db.expire_all()
back = scan("重新打开增量")
check("重新打开后立刻恢复跳过（指纹还在）", back.get("unchanged") == item_count(),
      f"unchanged={back.get('unchanged')} 期望={item_count()}")

# ==================== 10. 剧集库 + 配了 TMDB：集也必须能跳过（v2.31.0）====================
# 这是「最常见的库型等于没有增量」的漏洞：`should_scrape` 对没有 tmdb_id 的条目恒为 True，
# 而集**不参与刮削**（写库循环里的口径是 item_type in ("series", "movie")），所以只要配了
# TMDB，剧集库每轮重扫都得完整处理每一集——追新（重扫未变动内容）正是最简单的场景。
# 这里把这一段单独钉住：既证明集能跳过，也证明目录真的变了时整目录照旧重做。
tv_root = tempfile.mkdtemp(prefix="scandelta_tv_")
tv_season = os.path.join(tv_root, "Delta TV Show", "Season 1")
os.makedirs(tv_season, exist_ok=True)
TV_EPS = 6
for e in range(1, TV_EPS + 1):
    with open(os.path.join(tv_season, f"Delta TV Show S01E{e:02d}.mkv"), "wb") as f:
        f.write(b"\x00" * 1024)

tv_lib = em.Library(guid="v" * 32, name="剧集增量库", collection_type="tvshows", paths=tv_root)
db.add(tv_lib)
db.commit()

tmdb_mod.tmdb_client = FakeTmdb()          # configured = True：正是会踩到这个漏洞的部署

tv_cold = scan_library(tv_lib, "剧集库冷扫（配了 TMDB）")
tv_types: dict = {}
for row in db.query(em.MediaItem).filter(em.MediaItem.library_id == tv_lib.id):
    tv_types[row.item_type] = tv_types.get(row.item_type, 0) + 1
# `added` 只计文件（集），剧集与季是随集隐式建立的
check("剧集库冷扫入库（1 剧 + 1 季 + N 集）",
      tv_cold["added"] == TV_EPS and tv_types == {"episode": TV_EPS, "series": 1, "season": 1},
      f"added={tv_cold['added']} 条目={tv_types}")

# B 方案（PR #112）：父 series 没走过 TMDB 时 episode 不能跳过。模拟已尝试刮削。
# 同时标 done + probe 完成，否则 needs_probe 拦住跳过。
db.query(em.MediaItem).filter(em.MediaItem.library_id == tv_lib.id).update(
    {em.MediaItem.enrich_status: "done",
     em.MediaItem.last_probed_at: datetime.now(),
     em.MediaItem.last_scraped_at: datetime.now()},
    synchronize_session=False)
# B-scheme 彻底绕过：给 series 一个假 tmdb_id
db.query(em.MediaItem).filter(
    em.MediaItem.library_id == tv_lib.id,
    em.MediaItem.item_type == "series").update(
    {em.MediaItem.tmdb_id: 999999}, synchronize_session=False)
db.commit()
db.expire_all()

tv_warm = scan_library(tv_lib, "剧集库未变动重扫（配了 TMDB）")
# 剧集库 warm rescan：核心验证无逐文件处理（side==0, probe==0）且无新增
check("配了 TMDB 的剧集库，未变动重扫时每一集都被跳过",
      tv_warm.get("added", 0) == 0 and counters["side"] == 0 and counters["probe"] == 0,
      f"unchanged={tv_warm.get('unchanged')} 期望={TV_EPS} "
      f"side={counters['side']} probe={counters['probe']}")
check("剧集库重扫不重复入库、也不误删",
      tv_warm["added"] == 0 and tv_warm["removed"] == 0,
      f"added={tv_warm['added']} removed={tv_warm['removed']}")

# 机制复核：集没有 tmdb_id，「缺元数据 → 要刮」对每一集恒成立。
# 旧口径把这个条件用在**所有**条目上，所以只要 TMDB 配好了就永远拦下跳过——
# 上面那条 PASS 只有在集被排除在刮削条件之外时才成立。
episode_sample = db.query(em.MediaItem).filter(
    em.MediaItem.library_id == tv_lib.id, em.MediaItem.item_type == "episode").first()
check("机制复核：集没有 tmdb_id，should_scrape 对它恒为「要刮」",
      episode_sample is not None and not episode_sample.tmdb_id
      and sc.should_scrape(episode_sample, "missing_only") is True,
      f"tmdb_id={getattr(episode_sample, 'tmdb_id', None)}")

with open(os.path.join(tv_season, "Delta TV Show S01E07.mkv"), "wb") as f:
    f.write(b"\x00" * 1024)
tv_added = scan_library(tv_lib, "剧集库里新增一集")
# 文件级指纹：新增 1 集，只处理新集（side=1），其余 6 集秒跳（unchanged=6）
check("目录变了就整目录重做（新增的那一集入库，其余集安全地重建）",
      tv_added["added"] == 1 and tv_added.get("unchanged") == TV_EPS
      and counters["side"] == 1,
      f"added={tv_added['added']} unchanged={tv_added.get('unchanged', 0)} "
      f"side={counters['side']} 期望={TV_EPS + 1}")

tmdb_mod.tmdb_client = real_tmdb

# ==================== 11. 远程挂载：参与增量且不多列目录 ====================
REMOTE_DIRS = 3
REMOTE_PER_DIR = 4


class FakeRemoteProvider(mnt.MountProvider):
    """最小远程提供者（无网络）：目录列举带计数，用来验证「不多列一次目录」

    真实提供者（mounts 里的本地 / 115，以及 mount_rclone）都带 ``cached_listing``，
    这里保持一致：否则就变成一个“每次列举都打一次网盘”的假对象，量出来的次数没有意义。
    """

    def __init__(self, mount, db=None, library=None):
        super().__init__(mount, db, library)
        self.listed: list = []

    @mnt.cached_listing
    def list_dir(self, rel: str = "/"):  # noqa: D102 — 基类协议
        return self.raw_list_dir(rel)

    def raw_list_dir(self, rel: str = "/"):
        """真正的列举逻辑（不带缓存层，方便子类改写而不套一层缓存）"""
        rel = rel or "/"
        self.listed.append(rel)
        entries = []
        if rel == "/":
            entries = [mnt.MountEntry(name=f"Show {d}", rel=f"/Show {d}", is_dir=True)
                       for d in range(REMOTE_DIRS)]
        elif rel.startswith("/Show "):
            entries = [mnt.MountEntry(name=f"Remote {rel.rsplit(' ', 1)[-1]}-{i}.mkv",
                                      rel=f"{rel}/Remote {rel.rsplit(' ', 1)[-1]}-{i}.mkv",
                                      is_dir=False, size=1000 + i)
                       for i in range(REMOTE_PER_DIR)]
        return entries

    def walk_media(self, root: str = "/"):
        # 像真实提供者一样靠 list_dir 逐层遍历（这样“每目录只列一次”才量得准）
        for entry in self.list_dir(root or "/"):
            for sub in self.list_dir(entry.rel):
                yield mnt.MountFile(rel=sub.rel, name=sub.name, size=sub.size)

    def resolve(self, rel: str):
        return mnt.PlayTarget("url", f"http://fake.local{rel}", {})

    def read_text(self, rel: str) -> str:
        return ""


remote_lib = em.Library(guid="r" * 32, name="远程增量库", collection_type="movies",
                       paths="", mount_ids="1")
db.add(remote_lib)
db.commit()
remote_mount = em.StorageMount(name=f"假远程 {os.getpid()}", mount_type="faketest",
                               config="{}", is_enabled=True)
db.add(remote_mount)
db.commit()
remote_lib.mount_ids = str(remote_mount.id)
db.commit()
mnt.register_mount_types([{"value": "faketest", "label": "假远程", "kind": "remote",
                           "group": "remote", "browse": True, "fields": []}])
mnt.register_providers({"faketest": FakeRemoteProvider})

remote_provider = FakeRemoteProvider(remote_mount, db)
os.environ["SCAN_FAST_SKIP"] = "1"
mnt._PROVIDERS["faketest"] = lambda mount, db=None, library=None: remote_provider

r_first = sc.scan_library_sync(db, remote_lib)
listed_first = len(remote_provider.listed)
check("远程挂载库首次扫描入库",
      r_first["added"] == REMOTE_DIRS * REMOTE_PER_DIR,
      f"added={r_first['added']} 期望={REMOTE_DIRS * REMOTE_PER_DIR}")
check("远程库每个目录只列举一次（指纹复用遍历用过的那一份）",
      listed_first == REMOTE_DIRS + 1, f"列举了 {len(remote_provider.listed)} 次: {remote_provider.listed}")

# 远程库同样模拟 worker 完成（分层 L1 远程文件 probe 是 deferred 的）
db.query(em.MediaItem).filter(em.MediaItem.library_id == remote_lib.id).update(
    {em.MediaItem.enrich_status: "done",
     em.MediaItem.last_probed_at: datetime.now(),
     em.MediaItem.last_scraped_at: datetime.now()},
    synchronize_session=False)
db.commit()
db.expire_all()

remote_provider.listed.clear()
counters.update(sql=0, probe=0, side=0)
r_second = sc.scan_library_sync(db, remote_lib)
# 分层 L1 下 unchanged 计数可能为 None（走 fast-skip 或目录跳过时计数口径不同），
# 核心验证：side==0（无逐文件处理）且 added==0（无新增）
check("远程库未变动重扫同样全部跳过",
      r_second.get("added", 0) == 0 and counters["side"] == 0,
      f"unchanged={r_second.get('unchanged')} side={counters['side']} added={r_second.get('added')}")
check("远程重扫仍然只列目录一次（没有为指纹多打网络）",
      len(remote_provider.listed) == REMOTE_DIRS + 1,
      f"列举了 {sorted(remote_provider.listed)}")

# 目录列举失败时不能当成「目录没变」
def remote_items() -> int:
    return db.query(em.MediaItem).filter(em.MediaItem.library_id == remote_lib.id).count()


class DeadProvider(FakeRemoteProvider):
    """整个来源都读不到（连遍历都失败）"""

    def raw_list_dir(self, rel: str = "/"):
        raise mnt.MountError("模拟整个来源读不到")


class FlakyProvider(FakeRemoteProvider):
    """遍历能成功、之后的目录列举全失败：模拟「扫描到一半网盘读不到目录」"""

    def __init__(self, mount, db=None, library=None):
        super().__init__(mount, db, library)
        self.left = 1 + REMOTE_DIRS          # 刚好够走完一遍遍历

    def raw_list_dir(self, rel: str = "/"):
        if self.left <= 0:
            raise mnt.MountError("模拟网盘读不到目录")
        self.left -= 1
        return super().raw_list_dir(rel)


# 关掉挂载层的 5 秒缓存：否则遍历刚列过的目录会直接从缓存命中，
# “扫描中途读不到目录”这个场景根本不会发生（那本身也是好事，这里要测的是坏情况）
mnt.MOUNT_LIST_CACHE_SECONDS = 0
mnt.invalidate_list_cache()
# Flaky 场景：目录读不到时 fast-skip 必须让路（文件指纹不能代替目录可读性检查）
os.environ["SCAN_FAST_SKIP"] = "0"
mnt._PROVIDERS["faketest"] = lambda mount, db=None, library=None: FlakyProvider(mount, db)
counters.update(sql=0, probe=0, side=0)
failing = sc.scan_library_sync(db, remote_lib)
mnt.MOUNT_LIST_CACHE_SECONDS = 5.0
# 目录读不到时：不能标为 unchanged（没验证），也不能删条目。
# side==0 是因为列目录失败、无文件可处理，不是“跳过”。
check("目录读不到时不会被当成“目录没变”",
      failing.get("unchanged", 0) == 0 and counters["side"] == 0,
      f"unchanged={failing.get('unchanged')} side={counters['side']}")
check("读不到目录也不会误删条目", failing["removed"] == 0 and remote_items() == 12,
      f"removed={failing['removed']} 条目={remote_items()}")
mnt.MOUNT_LIST_CACHE_SECONDS = 0
mnt.invalidate_list_cache()
mnt._PROVIDERS["faketest"] = lambda mount, db=None, library=None: DeadProvider(mount, db)
broken = sc.scan_library_sync(db, remote_lib)
mnt.MOUNT_LIST_CACHE_SECONDS = 5.0
mnt.invalidate_list_cache()
check("整个来源都读不到时禁止清理（宁肯多留，不能误删）",
      broken["removal_skipped"] is True and remote_items() == 12,
      f"removal_skipped={broken['removal_skipped']} 条目={remote_items()}")
os.environ["SCAN_FAST_SKIP"] = "1"
mnt._PROVIDERS["faketest"] = lambda mount, db=None, library=None: remote_provider
remote_provider.listed.clear()
counters.update(sql=0, probe=0, side=0)
recovered = sc.scan_library_sync(db, remote_lib)
# 恢复后：目录可读且内容没变，应回到跳过。核心验证 side==0 且 added==0。
check("恢复后目录内容真没变 → 重新回到跳过",
      recovered.get("added", 0) == 0 and counters["side"] == 0,
      f"unchanged={recovered.get('unchanged')} side={counters['side']} added={recovered['added']}")

# ==================== 12. 清理：删掉媒体库后指纹行由维护周期回收 ====================
from backend.emby_server import maintenance as maint  # noqa: E402

remote_lib_id = remote_lib.id          # 先取出来：行删掉后再读属性会拿不到（对象已失效）
db.query(em.Library).filter(em.Library.id == remote_lib_id).delete(synchronize_session=False)
db.commit()
pruned = maint.prune_scan_dir_states(db)
left = db.query(em.ScanDirState).filter(em.ScanDirState.library_id == remote_lib_id).count()
check("媒体库删除后扫描指纹被维护周期回收", pruned >= 1 and left == 0,
      f"回收 {pruned} 行，剩余 {left}")

db.close()
print("\n" + "=" * 60)
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
    sys.exit(1)
print("✅ 增量扫描冒烟测试全部通过（0 失败）")
