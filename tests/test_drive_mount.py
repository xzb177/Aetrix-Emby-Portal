"""Google 云盘通用挂载（drive 类型）：注册、列目录、遍历、解析、失败语义

钉住的契约（施工图见 reports/drive-generic-mount-recon-20261017.md）：

- **类型注册**：``drive:`` 前缀被 ``detect_mount_type`` 认成 ``drive``；
  ``build_provider`` 能造出 ``DriveMount``（模块导入即注册）。
- **列目录走统一凭据层**：请求经 ``drive_changes._api_get`` 发出（全局限流 +
  退避都在那一层），凭据经 ``get_token_for_drive`` 按盘取（共享盘带
  ``driveId``/``corpora=drive``，个人盘带 ``spaces=drive`` 且根目录是 ``root``）。
- **file_id 透传**：文件条目带 Drive id、目录条目 ``file_id`` 为空串
  ``entry_id`` 为 folder id——扫描器靠它把 id 写进 ``item.drive_file_id``。
- **失败不吞**：子目录列举失败 → ``walk_media`` 抛 ``MountError``（扫描器据此
  跳过清理，防误删）；目录不存在 → ``MountError``；401 刷新凭据重试一次。

不碰网络、不碰数据库：``drive_changes`` 的两个入口在本文件里被替换成假实现，
目录树是内存里的字典。
"""
import os

# 与 test_drive_changes.py 同款：必须在 import backend 之前钉住 sqlite，
# 否则本文件若先于其他测试被收集，backend.database 会按默认 PG 建引擎，
# 连累后面靠 setdefault 切 sqlite 的测试文件（进程级状态污染）。
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
from types import SimpleNamespace

import pytest

from backend.emby_server import drive_auth, drive_changes
from backend.emby_server import mount_drive  # noqa: F401 — 导入即注册 drive 提供者
from backend.emby_server import mounts as mnt
from backend.emby_server.mount_drive import DriveMount

FOLDER = "application/vnd.google-apps.folder"
DRIVE_ID = "0AERUVu4kfWEfUk9PVA"


def _folder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER}


def _file(fid, name, size="1000", mime="video/x-matroska"):
    return {"id": fid, "name": name, "mimeType": mime, "size": size,
            "modifiedTime": "2026-10-17T00:48:49.000Z"}


# folder id → 该目录下的条目（演唱会 目录故意分两页，验分页拼接）
TREE = {
    DRIVE_ID: [
        _folder("F_MP", "MoviePilot"),
        _folder("F_NT", "nastool"),
        _file("V_ROOT", "根目录电影.mkv"),
    ],
    "F_MP": [_folder("F_CONCERT", "演唱会")],
    "F_CONCERT": [
        _file("V1", "演唱会A.mkv", "1000"),
        _file("V2", "演唱会B.mp4", "2000", "video/mp4"),
        _file("S1", "字幕.srt", "10", "text/plain"),
    ],
    "F_NT": [_folder("F_NT_CONCERT", "演唱会")],
    "F_NT_CONCERT": [_file("V3", "演唱会C.mkv", "3000")],
    "root": [_folder("F_PERSONAL", "我的电影")],
    "F_PERSONAL": [_file("V9", "个人电影.mkv", "9000")],
}

FAIL_FOLDERS: set[str] = set()
API_CALLS: list[dict] = []
TOKEN_CALLS: list[tuple[str, bool]] = []
PIN_SA_CALLS: list[str] = []  # next_shared_sa 返回的 SA email
USED_TOKENS: list[str] = []  # 每次 _api_get 实际收到的 token（验分页钉住）

PINNED_SA = {"client_email": "sa-pin@example.iam.gserviceaccount.com",
             "private_key_id": "pin-key-1"}


def _fake_api_get(url, token, params):
    API_CALLS.append(dict(params))
    USED_TOKENS.append(token)
    match = re.search(r"'([^']+)' in parents", params.get("q", ""))
    folder_id = match.group(1) if match else ""
    if folder_id in FAIL_FOLDERS:
        raise drive_changes.DriveNotFoundError(params.get("driveId", ""))
    items = TREE.get(folder_id, [])
    if folder_id == "F_CONCERT" and not params.get("pageToken"):
        return {"files": items[:1], "nextPageToken": "p2"}
    if folder_id == "F_CONCERT" and params.get("pageToken") == "p2":
        return {"files": items[1:]}
    return {"files": items}


def _fake_token(drive_id, force_refresh=False):
    TOKEN_CALLS.append((drive_id, force_refresh))
    return "tok-fresh" if force_refresh else "tok-1"


def _fake_next_sa():
    PIN_SA_CALLS.append(PINNED_SA["client_email"])
    return PINNED_SA


SA_ACCESS_CALLS: list[tuple[str, float]] = []  # (SA email, min_remaining)


def _fake_sa_access_token(sa, min_remaining=60):
    # 按调用次序返回不同 token：钉住取 "tok-pin-1"，401 刷新取 "tok-pin-2"……
    SA_ACCESS_CALLS.append((sa.get("client_email", ""), min_remaining))
    return f"tok-pin-{len(SA_ACCESS_CALLS)}"


@pytest.fixture(autouse=True)
def _fake_drive(monkeypatch):
    FAIL_FOLDERS.clear()
    API_CALLS.clear()
    TOKEN_CALLS.clear()
    PIN_SA_CALLS.clear()
    SA_ACCESS_CALLS.clear()
    USED_TOKENS.clear()
    mnt.invalidate_list_cache()
    monkeypatch.setattr(drive_changes, "_api_get", _fake_api_get)
    monkeypatch.setattr(drive_changes, "get_token_for_drive", _fake_token)
    monkeypatch.setattr(drive_changes, "next_shared_sa", _fake_next_sa)
    monkeypatch.setattr(drive_changes, "_sa_access_token", _fake_sa_access_token)
    # refresh_sa_token 故意走真实实现：弹缓存 + 调 _sa_access_token（已垫），
    # 401 续跑的真实逻辑完整跑一遍。
    yield
    mnt.invalidate_list_cache()


def _mount(path=f"drive:{DRIVE_ID}", config=None):
    return SimpleNamespace(
        id=99, name="gdrive-generic", mount_type="drive",
        path=path, config=config if config is not None else {},
    )


# ---------- 注册与类型 ----------

def test_detect_and_metadata():
    assert mnt.detect_mount_type(f"drive:{DRIVE_ID}") == "drive"
    assert mnt.detect_mount_type("drive:myDrive:paul_emby") == "drive"
    meta = mnt.MOUNT_TYPE_MAP["drive"]
    assert meta["kind"] == "remote" and meta["browse"] is True


def test_build_provider_registered():
    provider = mnt.build_provider(_mount())
    assert isinstance(provider, DriveMount)
    assert provider.drive_id == DRIVE_ID
    assert provider.root_id == DRIVE_ID  # 共享盘根 id == 盘 id


def test_missing_drive_id_rejected():
    with pytest.raises(mnt.MountError):
        DriveMount(_mount(path="drive:"))


# ---------- 列目录 ----------

def test_list_root_entries_and_params():
    provider = DriveMount(_mount())
    entries = provider.list_dir("/")
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"MoviePilot", "nastool", "根目录电影.mkv"}
    # 目录在前（排序约定与其它挂载一致）
    assert entries[0].is_dir
    mp = by_name["MoviePilot"]
    assert mp.is_dir and mp.entry_id == "F_MP" and mp.file_id == ""
    movie = by_name["根目录电影.mkv"]
    assert not movie.is_dir and movie.file_id == "V_ROOT" and movie.size == 1000
    # 共享盘参数：driveId + corpora=drive，没有 spaces
    params = API_CALLS[0]
    assert params["driveId"] == DRIVE_ID and params["corpora"] == "drive"
    assert "spaces" not in params
    # 共享盘凭据：一次列举只轮换出一个 SA，并索取长寿 token（钉住）
    assert PIN_SA_CALLS == ["sa-pin@example.iam.gserviceaccount.com"]
    assert SA_ACCESS_CALLS == [("sa-pin@example.iam.gserviceaccount.com", 1800.0)]
    assert TOKEN_CALLS == []  # 共享盘列举不再走逐页的 get_token_for_drive


def test_list_subpath_by_name_with_pagination():
    provider = DriveMount(_mount())
    entries = provider.list_dir("/MoviePilot/演唱会")
    names = {e.name for e in entries}
    assert names == {"演唱会A.mkv", "演唱会B.mp4", "字幕.srt"}  # 两页拼齐
    a = next(e for e in entries if e.name == "演唱会A.mkv")
    assert a.rel == "/MoviePilot/演唱会/演唱会A.mkv"
    assert a.file_id == "V1" and a.mod_ts > 0
    assert any(c.get("pageToken") == "p2" for c in API_CALLS)


def test_missing_dir_raises():
    provider = DriveMount(_mount())
    with pytest.raises(mnt.MountError):
        provider.list_dir("/不存在的目录")


def test_root_id_override():
    provider = DriveMount(_mount(config={"root_id": "F_CONCERT"}))
    names = {e.name for e in provider.list_dir("/")}
    assert names == {"演唱会A.mkv", "演唱会B.mp4", "字幕.srt"}


def test_test_ok():
    result = DriveMount(_mount()).test()
    assert result["ok"] is True and "3 项" in result["message"]


# ---------- 遍历与失败语义 ----------

def test_walk_media_yields_file_ids():
    provider = DriveMount(_mount())
    files = {f.rel: f for f in provider.walk_media(root="/")}
    assert files["/MoviePilot/演唱会/演唱会A.mkv"].file_id == "V1"
    assert files["/nastool/演唱会/演唱会C.mkv"].file_id == "V3"
    assert files["/根目录电影.mkv"].file_id == "V_ROOT"
    # 非媒体扩展名不进遍历（与其它远程挂载同一张表）
    assert "/MoviePilot/演唱会/字幕.srt" not in files
    assert provider.walk_workers() == 4


def test_walk_subdir_failure_raises_not_silent():
    FAIL_FOLDERS.add("F_NT")
    provider = DriveMount(_mount())
    got = []
    with pytest.raises(mnt.MountError):
        for f in provider.walk_media(root="/"):
            got.append(f)
    # 失败目录之外的文件照常产出，但收尾必须抛错（扫描器据此跳过清理）
    assert any(f.file_id == "V1" for f in got)


# ---------- 解析与个人盘 ----------

def test_resolve_usercontent_url(monkeypatch):
    monkeypatch.setattr(
        drive_auth, "drive_auth_headers",
        lambda url, key=None: {"Authorization": "Bearer T"})
    provider = DriveMount(_mount())
    target = provider.resolve("/MoviePilot/演唱会/演唱会A.mkv")
    assert target.kind == "url"
    assert "id=V1" in target.value
    assert target.headers["Authorization"] == "Bearer T"


def test_personal_drive_uses_spaces_and_root():
    provider = DriveMount(_mount(path="drive:myDrive:paul_emby"))
    assert provider.personal is True and provider.root_id == "root"
    entries = provider.list_dir("/")
    assert {e.name for e in entries} == {"我的电影"}
    params = API_CALLS[0]
    assert params["spaces"] == "drive" and "driveId" not in params
    assert TOKEN_CALLS[0][0] == "myDrive:paul_emby"
    # 个人盘固定 OAuth，不碰 SA 池
    assert PIN_SA_CALLS == [] and SA_ACCESS_CALLS == []


# ---------- 401 刷新续跑 ----------

def test_401_refreshes_same_sa_once(monkeypatch):
    """共享盘 401：钉住模式下对同一 SA 刷新续跑（不推进轮换、不换 SA）"""
    state = {"calls": 0}

    def flaky_api_get(url, token, params):
        state["calls"] += 1
        if state["calls"] == 1:
            raise PermissionError("Drive token 无效或过期")
        return _fake_api_get(url, token, params)

    monkeypatch.setattr(drive_changes, "_api_get", flaky_api_get)
    provider = DriveMount(_mount())
    entries = provider.list_dir("/")
    assert len(entries) == 3
    # 只轮换出一个 SA：钉住取一次 + 401 同 SA 刷新一次
    assert PIN_SA_CALLS == ["sa-pin@example.iam.gserviceaccount.com"]
    assert SA_ACCESS_CALLS == [
        ("sa-pin@example.iam.gserviceaccount.com", 1800.0),
        ("sa-pin@example.iam.gserviceaccount.com", 1800.0),
    ]
    assert USED_TOKENS == ["tok-pin-2"]  # 重试页用了刷新后的新 token
    assert TOKEN_CALLS == []  # 共享盘不走 get_token_for_drive(force_refresh)


def test_401_personal_drive_keeps_legacy_semantics(monkeypatch):
    """个人盘 401：沿用旧语义——get_token_for_drive(force_refresh=True)"""
    state = {"calls": 0}

    def flaky_api_get(url, token, params):
        state["calls"] += 1
        if state["calls"] == 1:
            raise PermissionError("Drive token 无效或过期")
        return _fake_api_get(url, token, params)

    monkeypatch.setattr(drive_changes, "_api_get", flaky_api_get)
    provider = DriveMount(_mount(path="drive:myDrive:paul_emby"))
    entries = provider.list_dir("/")
    assert {e.name for e in entries} == {"我的电影"}
    assert ("myDrive:paul_emby", True) in TOKEN_CALLS  # OAuth 刷新过一次
    assert PIN_SA_CALLS == []  # 不碰 SA 池


# ---------- 单次列举钉住同一 SA（分页 400 回归） ----------

PIN_EMAIL = "sa-pin@example.iam.gserviceaccount.com"


def _big_items(n=1001):
    return [
        {"id": f"W{i:05d}", "name": f"大目录视频{i:05d}.mkv",
         "mimeType": "video/x-matroska", "size": "1000",
         "modifiedTime": "2026-10-17T00:48:49.000Z"}
        for i in range(n)
    ]


def _big_api_get(items):
    """两页列举假实现：第 1 页 1000 项 + nextPageToken，第 2 页收尾"""
    def api_get(url, token, params):
        USED_TOKENS.append(token)
        match = re.search(r"'([^']+)' in parents", params.get("q", ""))
        folder_id = match.group(1) if match else ""
        if folder_id == "F_BIG" and not params.get("pageToken"):
            return {"files": items[:1000], "nextPageToken": "BIG-p2"}
        if folder_id == "F_BIG" and params.get("pageToken") == "BIG-p2":
            return {"files": items[1000:]}
        return {"files": []}
    return api_get


def test_multi_page_listing_pins_single_sa(monkeypatch):
    """>1000 项的两页列举：两页用同一 SA 的同一 token 完整翻完"""
    items = _big_items()
    monkeypatch.setattr(drive_changes, "_api_get", _big_api_get(items))
    provider = DriveMount(_mount(config={"root_id": "F_BIG"}))
    entries = provider.list_dir("/")
    assert len(entries) == 1001
    # 一次列举只轮换出一个 SA、索取一次长寿 token
    assert PIN_SA_CALLS == [PIN_EMAIL]
    assert SA_ACCESS_CALLS == [(PIN_EMAIL, 1800.0)]
    # 两页请求都带同一个 token（换 SA 翻第 2 页会被 Google 回 400）
    assert USED_TOKENS == ["tok-pin-1", "tok-pin-1"]


def test_401_mid_pagination_refreshes_same_sa_and_continues(monkeypatch):
    """钉住 SA 后中途 401：第 2 页首攻被拒 → 同 SA 刷新 → 续跑成功，条目齐全"""
    items = _big_items()
    state = {"big_page2_hits": 0}

    def api_get(url, token, params):
        match = re.search(r"'([^']+)' in parents", params.get("q", ""))
        folder_id = match.group(1) if match else ""
        USED_TOKENS.append(token)
        if folder_id == "F_BIG" and not params.get("pageToken"):
            return {"files": items[:1000], "nextPageToken": "BIG-p2"}
        if folder_id == "F_BIG" and params.get("pageToken") == "BIG-p2":
            state["big_page2_hits"] += 1
            if state["big_page2_hits"] == 1:
                raise PermissionError("Drive token 无效或过期")
            return {"files": items[1000:]}
        return {"files": []}

    monkeypatch.setattr(drive_changes, "_api_get", api_get)
    provider = DriveMount(_mount(config={"root_id": "F_BIG"}))
    entries = provider.list_dir("/")
    assert len(entries) == 1001  # 完整翻完，一条不丢
    assert PIN_SA_CALLS == [PIN_EMAIL]  # 整个枚举只轮换一次
    # 第 1 页 + 第 2 页首攻用钉住 token；重试用同 SA 刷新后的新 token
    assert USED_TOKENS == ["tok-pin-1", "tok-pin-1", "tok-pin-2"]
    assert SA_ACCESS_CALLS == [(PIN_EMAIL, 1800.0), (PIN_EMAIL, 1800.0)]


# ---------- 同名文件夹回溯（_fid_of/_locate 缺陷修复） ----------

# 生产实证：/nastool/剧集/综艺/ 下有两个同名 "脱口秀大会 (2017)" 文件夹，
# 文件真身在第二个里；旧实现逐级只认第一个，合法路径报"找不到"。
DUP_TREE = {
    DRIVE_ID: [_folder("F_DUP", "综艺")],
    "F_DUP": [
        _folder("F_SHOW_A", "脱口秀大会 (2017)"),  # 第一个：只有 Season 6
        _folder("F_SHOW_B", "脱口秀大会 (2017)"),  # 第二个：有 Season 5（文件真身）
    ],
    "F_SHOW_A": [_folder("F_S6", "Season 6")],
    "F_S6": [_file("V_S6E1", "S06E01.mkv")],
    "F_S5": [_file("V_S5E9", "脱口秀大会-Part2 - S05E09.mkv")],
    "F_SHOW_B": [_folder("F_S5", "Season 5")],
}
DUP_FAIL_FOLDERS: set[str] = set()


def _dup_api_get(url, token, params):
    match = re.search(r"'([^']+)' in parents", params.get("q", ""))
    folder_id = match.group(1) if match else ""
    if folder_id in DUP_FAIL_FOLDERS:
        raise drive_changes.DriveNotFoundError(folder_id)
    return {"files": DUP_TREE.get(folder_id, [])}


@pytest.fixture()
def _dup_drive(monkeypatch):
    DUP_FAIL_FOLDERS.clear()
    monkeypatch.setattr(drive_changes, "_api_get", _dup_api_get)
    yield
    DUP_FAIL_FOLDERS.clear()


DUP_FILE_REL = "/综艺/脱口秀大会 (2017)/Season 5/脱口秀大会-Part2 - S05E09.mkv"


def test_locate_backtracks_into_second_duplicate_folder(_dup_drive):
    """文件在第二个同名文件夹里：_locate 回溯找到它，不再报"找不到文件\""""
    provider = DriveMount(_mount())
    entry = provider._locate(DUP_FILE_REL)
    assert entry.file_id == "V_S5E9" and not entry.is_dir


def test_resolve_duplicate_folder_file(_dup_drive, monkeypatch):
    """resolve 走 _locate：同名文件夹下的文件能拿到 usercontent 直链"""
    monkeypatch.setattr(
        drive_auth, "drive_auth_headers",
        lambda url, key=None: {"Authorization": "Bearer T"})
    provider = DriveMount(_mount())
    target = provider.resolve(DUP_FILE_REL)
    assert target.kind == "url" and "id=V_S5E9" in target.value


def test_locate_first_duplicate_unchanged(_dup_drive):
    """第一个同名文件夹里的文件：行为与旧实现一致（首个匹配）"""
    provider = DriveMount(_mount())
    entry = provider._locate("/综艺/脱口秀大会 (2017)/Season 6/S06E01.mkv")
    assert entry.file_id == "V_S6E1"


def test_locate_genuinely_missing_still_raises(_dup_drive):
    """真不存在的路径：仍然明确报找不到，不静默"""
    provider = DriveMount(_mount())
    with pytest.raises(mnt.MountError, match="找不到"):
        provider._locate("/综艺/脱口秀大会 (2017)/Season 9/不存在.mkv")
    with pytest.raises(mnt.MountError, match="找不到"):
        provider._locate("/综艺/没有这个综艺/xxx.mkv")


def test_backtrack_does_not_pin_ambiguous_rel(_dup_drive):
    """回溯命中的歧义路径不写入 _fid_cache：不把某次选中的文件夹钉死"""
    provider = DriveMount(_mount())
    provider._locate(DUP_FILE_REL)
    assert "/综艺/脱口秀大会 (2017)/Season 5" not in provider._fid_cache
    # 无歧义路径仍正常缓存（旧行为）
    provider._locate("/综艺/脱口秀大会 (2017)/Season 6/S06E01.mkv")
    assert provider._fid_cache.get("/综艺/脱口秀大会 (2017)") == "F_SHOW_A"


def test_backtrack_does_not_swallow_listing_failure(_dup_drive):
    """回溯时列举 API 本身失败：原样上抛，不被翻译成"找不到\""""
    DUP_FAIL_FOLDERS.add("F_SHOW_B")
    provider = DriveMount(_mount())
    with pytest.raises(mnt.MountError) as excinfo:
        provider._locate(DUP_FILE_REL)
    assert "目录不存在或无权访问" in str(excinfo.value)
    assert "找不到" not in str(excinfo.value)


def test_unambiguous_path_caching_unchanged(_dup_drive):
    """无歧义路径：一次 _fid_of 后各级 rel 都进缓存（与旧实现一致）"""
    provider = DriveMount(_mount())
    fid = provider._fid_of("/综艺/脱口秀大会 (2017)/Season 6")
    assert fid == "F_S6"
    assert provider._fid_cache["/综艺"] == "F_DUP"
    assert provider._fid_cache["/综艺/脱口秀大会 (2017)"] == "F_SHOW_A"
    assert provider._fid_cache["/综艺/脱口秀大会 (2017)/Season 6"] == "F_S6"


# ---------- 同名文件夹的文件级回溯（第二形态） ----------
# 生产实证（黑白密码案）：两个同名 "黑白密码 (2023)" 文件夹**都有** Season 1
# 子目录，但目标文件只在第二个里。旧实现（及只有目录级回溯）按首个匹配
# 走通目录、却在文件名处报"找不到文件"。
DUP2_TREE = {
    DRIVE_ID: [_folder("F_G", "剧集")],
    "F_G": [
        _folder("F_DRA_A", "黑白密码 (2023)"),  # 第一个：有 Season 1，但无目标文件
        _folder("F_DRA_B", "黑白密码 (2023)"),  # 第二个：目标文件真身
    ],
    "F_DRA_A": [_folder("F_S1A", "Season 1")],
    "F_S1A": [_file("V_OTHER", "别的剧集 - S01E01.mkv")],
    "F_DRA_B": [_folder("F_S1B", "Season 1")],
    "F_S1B": [_file("V_TGT", "黑白密码 - S01E16.mkv")],
}


def _dup2_api_get(url, token, params):
    match = re.search(r"'([^']+)' in parents", params.get("q", ""))
    folder_id = match.group(1) if match else ""
    return {"files": DUP2_TREE.get(folder_id, [])}


@pytest.fixture()
def _dup2_drive(monkeypatch):
    monkeypatch.setattr(drive_changes, "_api_get", _dup2_api_get)
    yield


DUP2_FILE_REL = "/剧集/黑白密码 (2023)/Season 1/黑白密码 - S01E16.mkv"


def test_locate_backtracks_when_file_missing_in_first_duplicate(_dup2_drive):
    """首个同名文件夹有子目录但无目标文件：_locate 换候选找到真身"""
    provider = DriveMount(_mount())
    entry = provider._locate(DUP2_FILE_REL)
    assert entry.file_id == "V_TGT" and not entry.is_dir


def test_locate_all_duplicates_exhausted_raises(_dup2_drive):
    """所有同名候选都没有该文件：明确报"找不到文件\""""
    provider = DriveMount(_mount())
    with pytest.raises(mnt.MountError, match="找不到文件"):
        provider._locate("/剧集/黑白密码 (2023)/Season 1/根本不存在.mkv")


def test_exists_file_in_second_duplicate(_dup2_drive):
    """exists：同名文件夹下的文件判存在（清理阶段不再误删活文件）"""
    provider = DriveMount(_mount())
    assert provider.exists(DUP2_FILE_REL) is True
    assert provider.exists("/剧集/黑白密码 (2023)/Season 1/根本不存在.mkv") is False


def test_exists_dir_name_still_works(_dup2_drive):
    """exists：目录名仍按名匹配（与基类一致，不分文件/目录）"""
    provider = DriveMount(_mount())
    assert provider.exists("/剧集/黑白密码 (2023)/Season 1") is True
    assert provider.exists("") is False


def test_resolve_second_duplicate_file(_dup2_drive, monkeypatch):
    """resolve：第二个同名文件夹里的文件能拿到直链"""
    monkeypatch.setattr(
        drive_auth, "drive_auth_headers",
        lambda url, key=None: {"Authorization": "Bearer T"})
    provider = DriveMount(_mount())
    target = provider.resolve(DUP2_FILE_REL)
    assert target.kind == "url" and "id=V_TGT" in target.value


# ---------- 同名文件夹命名空间合并（walk_media 覆盖） ----------

def test_walk_media_yields_second_duplicate_branch_files(_dup2_drive):
    """遍历合并同名文件夹命名空间：第二个分支的文件也要产出（自然路径）"""
    provider = DriveMount(_mount())
    files = {f.rel: f for f in provider.walk_media(root="/剧集")}
    # 第一分支的文件
    assert files["/剧集/黑白密码 (2023)/Season 1/别的剧集 - S01E01.mkv"].file_id == "V_OTHER"
    # 第二分支（旧实现里被遮蔽）的文件：自然路径产出，不改名
    assert files["/剧集/黑白密码 (2023)/Season 1/黑白密码 - S01E16.mkv"].file_id == "V_TGT"


def test_walk_media_is_deterministic_across_runs(_dup2_drive):
    """多次遍历结果确定一致（分支键+file_id 排序，不依赖 API 返回顺序）"""
    provider = DriveMount(_mount())
    run1 = [(f.rel, f.file_id) for f in provider.walk_media(root="/剧集")]
    run2 = [(f.rel, f.file_id) for f in provider.walk_media(root="/剧集")]
    assert run1 == run2 and len(run1) == 2


def test_walk_media_subdir_failure_still_raises(_dup2_drive, monkeypatch):
    """合并遍历下子目录列举失败：其余文件照常产出，收尾仍抛错（防误删语义）"""
    failing = {"F_S1B"}

    def _api_get_fail(url, token, params):
        import re as _re
        match = _re.search(r"'([^']+)' in parents", params.get("q", ""))
        folder_id = match.group(1) if match else ""
        if folder_id in failing:
            raise drive_changes.DriveNotFoundError(folder_id)
        return {"files": DUP2_TREE.get(folder_id, [])}

    monkeypatch.setattr(drive_changes, "_api_get", _api_get_fail)
    provider = DriveMount(_mount())
    got = []
    import pytest as _pytest
    with _pytest.raises(mnt.MountError):
        for f in provider.walk_media(root="/剧集"):
            got.append(f)
    assert any(f.file_id == "V_OTHER" for f in got)


# 真撞树：两个同名文件夹下有完全相同的文本路径文件（不同 file id）
SHADOW_COLLIDE_TREE = {
    DRIVE_ID: [_folder("F_C_ROOT", "综艺")],
    "F_C_ROOT": [
        _folder("F_C_A", "同名节目"),  # 第一个分支
        _folder("F_C_B", "同名节目"),  # 第二个分支（被遮蔽）
    ],
    "F_C_A": [_file("V_CA_E1", "S01E01.mkv")],
    "F_C_B": [_file("V_CB_E1", "S01E01.mkv")],  # 真撞：同文本路径，不同 fid
}


def _collide_api_get(url, token, params):
    match = re.search(r"'([^']+)' in parents", params.get("q", ""))
    folder_id = match.group(1) if match else ""
    return {"files": SHADOW_COLLIDE_TREE.get(folder_id, [])}


@pytest.fixture()
def _collide_drive(monkeypatch):
    monkeypatch.setattr(drive_changes, "_api_get", _collide_api_get)
    yield


def test_walk_media_true_collision_gets_indexed_suffix(_collide_drive):
    """真撞（同文本路径、不同 file_id）：首个保留原名，其余加 ' [n]' 后缀"""
    provider = DriveMount(_mount())
    files = {f.rel: f for f in provider.walk_media(root="/综艺")}
    assert files["/综艺/同名节目/S01E01.mkv"].file_id == "V_CA_E1"
    assert files["/综艺/同名节目/S01E01 [2].mkv"].file_id == "V_CB_E1"
    # 两个文件都不丢，且 guid 必然不同（路径派生）
    from backend.emby_server.scanner import item_guid, parse_media_filename
    guids = {item_guid(rel) for rel in files}
    assert len(guids) == 2
    # 后缀不影响季/集解析（复用多版本合并语义的前提）
    parsed = parse_media_filename("/综艺/同名节目/S01E01 [2].mkv", "tvshows")
    assert parsed["season"] == 1 and parsed["episode"] == 1


def test_walk_media_collision_suffix_is_deterministic(_collide_drive):
    """真撞改名多次遍历一致：分支键 (…F_C_A) < (…F_C_B)，归属不漂移"""
    provider = DriveMount(_mount())
    run1 = sorted(((f.file_id, f.rel) for f in provider.walk_media(root="/综艺")))
    run2 = sorted(((f.file_id, f.rel) for f in provider.walk_media(root="/综艺")))
    assert run1 == run2
    assert run1[0] == ("V_CA_E1", "/综艺/同名节目/S01E01.mkv")
    assert run1[1] == ("V_CB_E1", "/综艺/同名节目/S01E01 [2].mkv")
