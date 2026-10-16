"""存储挂载冒烟测试（v2.6.6）

「挂载」是把内容接进媒体库的方式，本测试逐层覆盖：

- **类型注册表**：local / 115 / rclone 三种类型的元数据、
  必填字段、脱敏集合与提供者注册
- **虚拟路径**：`mount://<id>/<rel>` 的构造与解析
- **本机挂载**（local / strm）：目录可读、只枚举媒体、STRM 读文件内容取直链
- **远程挂载**（115 / rclone）：目录枚举、鉴权头、凭据失效可识别
- **扫描集成**：媒体库绑定挂载后条目入库（远程为 `mount://` 路径）、来源不可用时禁止清理
- **播放 / 文件 / 字幕**：解析成直链、302/410（中继已删）、字幕解析钩子
- **管理端 API**：CRUD、必填校验、脱敏、测试连接、目录浏览、媒体库绑定与解绑

网络层整体被替换（`httpx.Client` → 假服务），因此本测试不依赖真实网盘 / rclone 账号。
"""
import asyncio
import inspect
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 不写死库文件名：交给 backend.database 解析（新装 aetrix_unified.db，老部署沿用原库）
os.environ.setdefault("DATABASE_TYPE", "sqlite")
# 空值也算没设（配置自愈会往 .env 物化出空的 SECRET_KEY，setdefault 只认「不存在」）
os.environ["SECRET_KEY"] = (os.environ.get("SECRET_KEY") or "").strip() or \
    "smoke-test-only-secret-key-not-for-production"
os.environ.pop("PAN115_COOKIE", None)

import httpx
from fastapi.testclient import TestClient
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import mount_rclone
from backend.emby_server import mounts as mnt
from backend.emby_server import playback_security as pb_sec
from backend.emby_server import scanner as sc
from backend.emby_server import streaming as st
from backend.emby_server import subtitles as subs
from backend.main import app
from backend.security import hash_password

init_db()
client = TestClient(app)
suf = str(random.randint(100000, 999999))
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ==================== 假的远端服务（替换 httpx.Client）====================

MEDIA_BYTES = bytes(range(256)) * 8          # 2048 字节，Range 断言用
PAN_COOKIE = "UID=smoke; CID=smoke"


class FakeResponse:
    def __init__(self, status: int = 200, content: bytes = b"", body=None, headers=None):
        self.status_code = status
        self.content = content
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "ignore")

    def iter_bytes(self, chunk_size: int = 1024 * 256):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self) -> None:
        pass


class FakeWeb:
    """假的 115 / rclone / CDN：只实现本功能用到的端点"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.auth_fail = False       # 所有带鉴权的端点都失败（凭据失效）
        # 115：cid → 子项（115 风格字段）
        self.pan115 = {
            "0": [
                {"fid": "d1", "cid": "100", "n": "电影", "fc": "0"},
                {"fid": "f9", "n": "README.txt", "s": 5, "pc": "pc-txt", "fc": "1"},
            ],
            "100": [
                {"fid": "f1", "n": "Alpha.Target.2024.1080p.NF.WEB-DL.mkv", "s": 1024, "pc": "pc-alpha", "fc": "1"},
                {"fid": "f2", "n": "Beta.S01E01.1080p.mkv", "s": 2048, "pc": "pc-beta", "fc": "1"},
                {"fid": "s1", "n": "Beta.S01E01.1080p.zh.srt", "s": 20, "pc": "pc-sub", "fc": "1"},
            ],
        }
        # rclone rc：fs → 子项（rclone /operations/list 风格字段）
        self.rclone = {
            "gdrive:Movies": [
                {"Path": "A.Movie.2024.mkv", "Name": "A.Movie.2024.mkv", "Size": 4096, "IsDir": False},
                {"Path": "Link.strm", "Name": "Link.strm", "Size": 64, "IsDir": False},
                {"Path": "Sub", "Name": "Sub", "Size": 0, "IsDir": True},
            ],
            "gdrive:Movies/Sub": [
                {"Path": "Sub/B.mkv", "Name": "B.mkv", "Size": 2048, "IsDir": False},
            ],
        }
        self.rclone_remotes = ["gdrive:", "onedrive:", "s3:"]
        self.rclone_strm_body = "https://cdn.example.com/strm/from-rclone.mkv"
        self.rc_serve = True       # False = 没开 --rc-serve（列目录正常，播放 404）
        self.rc_auth = ""           # "user:pass" = 需要 Basic 认证
        self.rc_calls: list[str] = []

    # ---- httpx.Client 接口 ----

    def build_request(self, method, url, headers=None, **kwargs):
        return {"method": method, "url": str(url), "headers": headers or {}}

    def send(self, request, stream: bool = False):
        return self.handle(request["method"], request["url"], headers=request["headers"])

    @staticmethod
    def _with_basic_auth(headers: dict, auth) -> dict:
        """httpx 传 auth=(user, pass) 时会加 Basic 头；假服务照做，便于断言"""
        import base64

        headers = dict(headers or {})
        if auth:
            user, password = (auth if isinstance(auth, (tuple, list)) else (auth, ""))
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            headers.setdefault("Authorization", f"Basic {token}")
        return headers

    def request(self, method, url, headers=None, params=None, data=None, json=None, auth=None):
        return self.handle(method, str(url), headers=self._with_basic_auth(headers, auth),
                           params=params, data=data, body=json)

    def get(self, url, headers=None, **kwargs):
        return self.handle("GET", str(url), headers=headers or {})

    def post(self, url, headers=None, json=None, auth=None, **kwargs):
        return self.handle("POST", str(url), headers=self._with_basic_auth(headers, auth), body=json)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # ---- 路由 ----

    def handle(self, method: str, url: str, headers=None, params=None, data=None, body=None):
        headers = headers or {}
        params = params or {}
        self.calls.append((method, url))
        if url.startswith("https://cdn.example.com"):
            return self._media(headers)
        if "webapi.115.com" in url or "115share" in url:
            return self._pan115(url, headers, params)
        if url.startswith("http://127.0.0.1:5572"):
            return self._rclone_rc(method, url, headers, body)
        return FakeResponse(404, b"not found")

    def _media(self, headers) -> FakeResponse:
        rng = headers.get("Range") or headers.get("range") or ""
        if rng.startswith("bytes="):
            start_s, _, end_s = rng[len("bytes="):].partition("-")
            start = int(start_s or 0)
            end = min(int(end_s) if end_s else len(MEDIA_BYTES) - 1, len(MEDIA_BYTES) - 1)
            return FakeResponse(206, MEDIA_BYTES[start:end + 1], headers={
                "content-range": f"bytes {start}-{end}/{len(MEDIA_BYTES)}",
                "accept-ranges": "bytes",
                "content-type": "video/x-matroska",
            })
        return FakeResponse(200, MEDIA_BYTES, headers={
            "accept-ranges": "bytes", "content-type": "video/x-matroska",
        })

    def _pan115(self, url, headers, params) -> FakeResponse:
        if not headers.get("Cookie"):
            return FakeResponse(401, b"")
        if self.auth_fail:
            return FakeResponse(200, body={"state": False, "errno": 40101017,
                                           "error": "登录已失效"})
        if url.endswith("/files/download"):
            return FakeResponse(200, body={
                "state": True, "file_url": f"https://cdn.example.com/115/{params.get('pickcode')}",
            })
        if url.endswith("/files"):
            if str(params.get("limit")) == "1":
                return FakeResponse(200, body={"state": True, "data": {"uid": "u-115", "vip": True}})
            cid = str(params.get("cid") or "0")
            return FakeResponse(200, body={"state": True, "data": self.pan115.get(cid, [])})
        return FakeResponse(404, b"")

    def _rclone_rc(self, method, url, headers, body) -> FakeResponse:
        import base64

        self.rc_calls.append(url)
        if self.rc_auth:
            expected = "Basic " + base64.b64encode(self.rc_auth.encode()).decode()
            if (headers.get("Authorization") or "") != expected:
                return FakeResponse(401, b"")
        path = url.split("127.0.0.1:5572", 1)[-1]
        if path.startswith("/operations/list"):
            payload = body or {}
            fs = str(payload.get("fs") or "")
            remote = str(payload.get("remote") or "").strip("/")
            key = f"{fs.rstrip('/')}/{remote}" if remote else fs
            items = self.rclone.get(key)
            if items is None:
                return FakeResponse(200, body={"error": f"directory not found: {key}"})
            return FakeResponse(200, body={"list": items})
        if path.startswith("/config/listremotes"):
            return FakeResponse(200, body={"remotes": self.rclone_remotes})
        if not self.rc_serve:
            return FakeResponse(404, b"")
        if path.endswith(".strm"):
            return FakeResponse(200, self.rclone_strm_body.encode())
        return self._media(headers)


FAKE = FakeWeb()
_real_httpx_client = httpx.Client
httpx.Client = lambda *a, **kw: FAKE  # type: ignore[assignment]


class FakeCompleted:
    """假的 subprocess 结果"""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRcloneCLI:
    """假的 rclone 可执行文件：只实现本功能用到的子命令"""

    RCLONE_PATH = "/opt/rclone/rclone"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.mode = "ok"          # ok / auth_fail

    def __call__(self, command, **kwargs) -> FakeCompleted:
        import json

        args = [str(a) for a in command]
        self.calls.append(args)
        sub = args[1] if len(args) > 1 else ""
        target = args[2] if len(args) > 2 else ""
        if self.mode == "auth_fail":
            return FakeCompleted(1, b"", b"Failed to create file system: Unauthorized: 401 invalid_grant")
        if sub == "listremotes":
            return FakeCompleted(0, ("\n".join(FAKE.rclone_remotes) + "\n").encode())
        if sub == "lsjson":
            items = FAKE.rclone.get(target)
            if items is None:
                return FakeCompleted(3, b"", b"directory not found")
            return FakeCompleted(0, json.dumps(items).encode())
        if sub == "link":
            if "nolink" in target:
                return FakeCompleted(1, b"", b"FS link: not supported by this backend")
            return FakeCompleted(0, b"https://cdn.example.com/rclone/linked\n")
        if sub == "cat" and target.endswith(".strm"):
            return FakeCompleted(0, FAKE.rclone_strm_body.encode())
        return FakeCompleted(1, b"", b"unknown command")


import subprocess  # noqa: E402

RCLONE_CLI = FakeRcloneCLI()
_real_subprocess_run = subprocess.run
subprocess.run = RCLONE_CLI  # type: ignore[assignment]


def _mount(name: str, mount_type: str, *, path: str = "", config: dict | None = None,
           is_enabled: bool = True, remark: str = "") -> em.StorageMount:
    return em.StorageMount(
        name=name, mount_type=mount_type, path=path,
        config=mnt.dump_config(config or {}), is_enabled=is_enabled, remark=remark,
    )


# ==================== 一、类型注册表与虚拟路径 ====================

print("=== 类型注册表与虚拟路径 ===")
registered = {t["value"] for t in mnt.MOUNT_TYPES}
check("只剩三种挂载类型（v2.42.12 精简）",
      registered == {"local", "115", "rclone"}, str(sorted(registered)))
check("删掉的类型一个都不许复活",
      not ({"strm", "webdav", "alist", "s3", "aliyun", "quark", "onedrive", "gdrive"}
           & (registered | set(mnt._PROVIDERS))), str(sorted(registered)))
check("每种类型都有标签 / 说明 / 字段定义",
      all(t.get("label") and t.get("hint") and "fields" in t for t in mnt.MOUNT_TYPES))
check("本机类型需要路径，远程类型不需要",
      all(t["needs_path"] for t in mnt.MOUNT_TYPES if t["kind"] == "local")
      and all(not t["needs_path"] for t in mnt.MOUNT_TYPES if t["kind"] == "remote"))
check("类型标签可查", mnt.MOUNT_TYPE_LABELS.get("115") == "115 网盘",
      str(mnt.MOUNT_TYPE_LABELS.get("115")))
check("每种类型都有提供者", set(mnt.MOUNT_TYPE_MAP) <= set(mnt._PROVIDERS), str(sorted(mnt._PROVIDERS)))
check("类型带分组 / 浏览 / 根字段元数据",
      all(t.get("group") and "browse" in t and "root_key" in t for t in mnt.MOUNT_TYPES)
      and mnt.supports_browse("115") and mnt.root_key("115") == "cid",
      str([(t["value"], t.get("group"), t.get("root_key")) for t in mnt.MOUNT_TYPES]))

# ---- 路径前缀即类型 ----
check("路径前缀决定类型", [mnt.detect_mount_type(p) for p in
                           ("115:/0", "rclone:gdrive/M", "/media/movies")]
      == ["115", "rclone", "local"],
      str([mnt.detect_mount_type(p) for p in ("115:/0", "rclone:gdrive/M", "/media/movies")]))
check("认不出来的前缀返回空串（让调用方明确报错，不猜）",
      mnt.detect_mount_type("s3://x") == "" and mnt.detect_mount_type("") == "",
      repr(mnt.detect_mount_type("s3://x")))
check("密钥字段自动进入脱敏集合",
      {"rc_pass", "cookie", "access_token"} <= mnt.secret_config_keys(),
      str(sorted(mnt.secret_config_keys())))

mount_path = mnt.mount_path(7, "/Movies/A.mkv")
check("构造挂载路径", mount_path == "mount://7/Movies/A.mkv", mount_path)
check("解析挂载路径", mnt.parse_mount_path(mount_path) == (7, "/Movies/A.mkv"),
      str(mnt.parse_mount_path(mount_path)))
check("解析挂载根路径", mnt.parse_mount_path("mount://12") == (12, "/"))
check("普通路径不是挂载路径",
      not mnt.is_mount_path("/media/movies") and mnt.parse_mount_path("/x") is None)
check("非法挂载 id 不解析", mnt.parse_mount_path("mount://abc/x") is None)

try:
    mnt.build_provider(_mount("未知", "ftp"))
    check("未知类型构造提供者会报错", False)
except mnt.MountError as exc:
    check("未知类型构造提供者会报错", "不支持的挂载类型" in str(exc), str(exc))


# ==================== 二、本机挂载（本地硬盘；.strm 也归这一类）====================

print("\n=== 本机挂载（本地硬盘，含 .strm）===")
local_root = tempfile.mkdtemp(prefix="mount_local_")
with open(os.path.join(local_root, "Local.Movie.2024.1080p.mkv"), "wb") as f:
    f.write(b"x" * 1024)
with open(os.path.join(local_root, "notes.txt"), "w", encoding="utf-8") as f:
    f.write("忽略")
os.makedirs(os.path.join(local_root, "Sub"), exist_ok=True)
with open(os.path.join(local_root, "Sub", "Local.EP01.mkv"), "wb") as f:
    f.write(b"y" * 2048)

local_mount = _mount("本地盘", "local", path=local_root)
local_provider = mnt.build_provider(local_mount)
check("本机挂载测试连接", mnt.test_mount(local_mount)["ok"])
check("本机挂载暴露本地根目录", local_provider.local_root == local_root)
walked = sorted(f.rel for f in local_provider.walk_media())
check("只枚举媒体文件", walked == ["/Local.Movie.2024.1080p.mkv", "/Sub/Local.EP01.mkv"], str(walked))
check("目录浏览列出子项",
      {e.name for e in local_provider.list_dir("/")} == {"Local.Movie.2024.1080p.mkv", "Sub", "notes.txt"},
      str([e.name for e in local_provider.list_dir("/")]))
target = local_provider.resolve("/Local.Movie.2024.1080p.mkv")
check("本机挂载解析成本机文件",
      target.kind == "local" and target.value.endswith("Local.Movie.2024.1080p.mkv"))
check("本机挂载存在性判断",
      local_provider.exists("/Sub/Local.EP01.mkv") and not local_provider.exists("/nope.mkv"))

# 路径越界（v2.30.0）：``exists()`` / ``size()`` 与 ``list_dir`` / ``resolve`` / ``read_text``
# 统一口径。以前这里直接 ``os.path.join(root, rel)``，于是 ``../`` 能探到挂载根之外——
# 一个目录里放个 ``../../etc/passwd``，面板就能把「这个文件存不存在 / 多大」问出来。
# 越界一律当作「不存在 / 拿不到大小」，不是 500。
SECRET_OUTSIDE = os.path.join(os.path.dirname(local_root), "outside_secret.mkv")
with open(SECRET_OUTSIDE, "wb") as f:
    f.write(b"z" * 4096)
rel_escape = os.path.relpath(SECRET_OUTSIDE, local_root).replace(os.sep, "/")
check("挂载根之外的文件：exists() 报不存在（不被 ../ 探到）",
      local_provider.exists("/../" + os.path.basename(SECRET_OUTSIDE)) is False
      and local_provider.exists(rel_escape) is False,
      f"rel={rel_escape}")
check("挂载根之外的文件：size() 报 0（不是真实大小）",
      local_provider.size("/../" + os.path.basename(SECRET_OUTSIDE)) == 0
      and local_provider.size(rel_escape) == 0)
check("越界不会把挂载内的正常文件一起挡掉",
      local_provider.exists("/Local.Movie.2024.1080p.mkv") and local_provider.size("/Sub/Local.EP01.mkv") == 2048)

# 符号链接指向挂载根之外：与越界同一口径（安全边界看的是**真实路径**）
link_outside = os.path.join(local_root, "escape_link")
try:
    os.symlink(os.path.dirname(local_root), link_outside)
except (OSError, NotImplementedError):  # 平台不支持符号链接时跳过，不算失败
    link_supported = False
else:
    link_supported = True
if link_supported:
    check("符号链接指向挂载根之外：exists() 报不存在",
          local_provider.exists("/escape_link/" + os.path.basename(SECRET_OUTSIDE)) is False)
    check("符号链接指向挂载根之外：size() 报 0",
          local_provider.size("/escape_link/" + os.path.basename(SECRET_OUTSIDE)) == 0)

missing = mnt.build_provider(_mount("坏盘", "local", path="/does/not/exist"))
try:
    missing.test()
    check("目录不存在时测试报错", False)
except mnt.MountError as exc:
    check("目录不存在时测试报错", "目录不可用" in str(exc), str(exc))

strm_root = tempfile.mkdtemp(prefix="mount_strm_")
with open(os.path.join(strm_root, "Strm.Movie.2022.1080p.strm"), "w", encoding="utf-8") as f:
    f.write("\ufeff# 注释行\n\nhttps://cdn.example.com/strm/movie.mkv\n")
with open(os.path.join(strm_root, "Broken.strm"), "w", encoding="utf-8") as f:
    f.write("这里没有直链")
strm_mount = _mount("STRM 目录", "local", path=strm_root)
check("STRM 内容解析（BOM / 注释 / 空行）",
      mnt.strm_url("\ufeff#c\n\nhttps://a.example.com/x.mkv\n") == "https://a.example.com/x.mkv")
check("STRM 容器取自直链", mnt.strm_container("https://a.example.com/x.mkv") == "mkv")
check("STRM 认不出扩展名时退回 strm", mnt.strm_container("https://a.example.com/play?id=1") == "strm")
strm_target = mnt.build_provider(strm_mount).resolve("/Strm.Movie.2022.1080p.strm")
check("STRM 挂载解析成直链",
      strm_target.kind == "url" and strm_target.value.endswith("movie.mkv"), strm_target.value)
try:
    mnt.build_provider(strm_mount).resolve("/Broken.strm")
    check("没有直链的 STRM 报错", False)
except mnt.MountError as exc:
    check("没有直链的 STRM 报错", "没有可用的直链" in str(exc), str(exc))
check("本机目录里的 .strm 也认（无需挂载）",
      mnt.local_play_target(os.path.join(strm_root, "Strm.Movie.2022.1080p.strm")).kind == "url")


# ---- 直链安全口径（v2.23.1）----
# 配置来源的直链默认允许指向内网：局域网 NAS、自建 WebDAV / AList / MinIO、rclone 的本地
# HTTP 端点都是这个项目的一等场景（能配媒体来源的人本来就有管理权限）。
# 真正要挡的是「服务器自己追出去的重定向目标」——那是可以被第三方直接利用的 SSRF 面。
lan_root = tempfile.mkdtemp(prefix="mount_lan_")
with open(os.path.join(lan_root, "Lan.strm"), "w", encoding="utf-8") as f:
    f.write("http://192.168.1.10:8096/media/movie.mkv\n")
lan_target = mnt.local_play_target(os.path.join(lan_root, "Lan.strm"))
check("本机 .strm 指向内网地址仍可播放（局域网 NAS / 自建网盘是正常用法）",
      lan_target.kind == "url" and lan_target.value.startswith("http://192.168.1.10"),
      str(lan_target.value))
check("解析不了的主机名不再被当成内网地址（离线 / 内网 DNS 不该弄坏公开直链）",
      pb_sec.validate_remote_url("https://no-such-host.invalid/x.mkv").endswith("x.mkv"),
      pb_sec.validate_remote_url("https://no-such-host.invalid/x.mkv"))
for bad, why in (("file:///etc/passwd", "非 http(s)"),
                 ("http://u:p@93.184.216.34/x.mkv", "内嵌凭据")):
    try:
        pb_sec.validate_remote_url(bad)
        check(f"直链校验拦下{why}", False, bad)
    except mnt.MountError as exc:
        check(f"直链校验拦下{why}", True, str(exc))

os.environ["EMBY_BLOCK_PRIVATE_MEDIA_URLS"] = "1"
try:
    try:
        mnt.local_play_target(os.path.join(lan_root, "Lan.strm"))
        check("EMBY_BLOCK_PRIVATE_MEDIA_URLS=1 时内网直链被拒", False, "没有拦")
    except mnt.MountError as exc:
        check("EMBY_BLOCK_PRIVATE_MEDIA_URLS=1 时内网直链被拒", "内部地址" in str(exc), str(exc))
    os.environ["EMBY_MEDIA_URL_ALLOWLIST"] = "192.168.1.10"
    allowed = mnt.local_play_target(os.path.join(lan_root, "Lan.strm"))
    check("EMBY_MEDIA_URL_ALLOWLIST 能放行自己的 NAS 地址",
          allowed.kind == "url" and allowed.value.startswith("http://192.168.1.10"),
          str(allowed.value))
finally:
    os.environ.pop("EMBY_BLOCK_PRIVATE_MEDIA_URLS", None)
    os.environ.pop("EMBY_MEDIA_URL_ALLOWLIST", None)

# 重定向目标：服务器自己会跟过去取，所以**一律**拒绝内网 / 回环（不受上面开关影响）
for target, why in (("http://10.0.0.5/secret", "内网"),
                    ("http://169.254.169.254/latest/meta-data/", "云元数据链路本地"),
                    ("http://localhost:8096/System/Info", "回环主机名")):
    try:
        st._next_redirect("https://93.184.216.34/movie.mkv", target)
        check(f"重定向目标指向{why}被拒（SSRF）", False, target)
    except mnt.MountError as exc:
        check(f"重定向目标指向{why}被拒（SSRF）", "内部地址" in str(exc), str(exc))
_redir_target, _redir_pin = st._next_redirect("https://93.184.216.34/movie.mkv", "/other.mkv")
check("重定向到公开地址照常放行（相对 Location 按当前主机解析）",
      _redir_target == "https://93.184.216.34/other.mkv",
      _redir_target)
cross = st._redirect_headers({"Authorization": "Basic x", "Cookie": "c", "User-Agent": "u"},
                             "https://a.example.com/1.mkv", "https://b.example.com/2.mkv")
check("跨主机重定向丢掉凭据头（不把 Basic / Cookie 交给重定向目标）",
      cross == {"User-Agent": "u"}, str(cross))
same = st._redirect_headers({"Authorization": "Basic x", "User-Agent": "u"},
                            "https://a.example.com/1.mkv", "https://a.example.com/2.mkv")
check("同主机重定向保留凭据头（WebDAV 的鉴权要跟着走）",
      same == {"Authorization": "Basic x", "User-Agent": "u"}, str(same))


# ==================== 三、远程挂载：115 ====================

print("\n=== 远程挂载：115 ===")
os.environ["PAN115_COOKIE"] = PAN_COOKIE
pan_mount = _mount("115 影库", "115", config={"cid": "0"})
pan = mnt.build_provider(pan_mount)
check("115 测试连接", mnt.test_mount(pan_mount)["ok"])
pan_files = sorted(f.rel for f in pan.walk_media())
check("115 递归枚举媒体（跳过目录与文本文件）",
      pan_files == ["/电影/Alpha.Target.2024.1080p.NF.WEB-DL.mkv", "/电影/Beta.S01E01.1080p.mkv"],
      str(pan_files))
pan_target = pan.resolve("/电影/Alpha.Target.2024.1080p.NF.WEB-DL.mkv")
check("115 解析成下载直链",
      pan_target.kind == "url" and pan_target.value.endswith("pc-alpha"), pan_target.value)
check("115 直链带 UA / Referer / Cookie（仅本机使用）",
      pan_target.headers.get("Cookie") == PAN_COOKIE
      and "User-Agent" in pan_target.headers and "Referer" in pan_target.headers,
      str(sorted(pan_target.headers)))
nested = mnt.build_provider(_mount("115 电影目录", "115", config={"cid": "100"}))
check("115 可把子目录设为挂载根",
      sorted(f.name for f in nested.walk_media()) == ["Alpha.Target.2024.1080p.NF.WEB-DL.mkv",
                                                      "Beta.S01E01.1080p.mkv"])

FAKE.auth_fail = True
pan_fail = mnt.test_mount(pan_mount)
check("115 Cookie 失效识别为凭据问题",
      pan_fail["ok"] is False and pan_fail.get("auth_error") is True, str(pan_fail))
FAKE.auth_fail = False

os.environ.pop("PAN115_COOKIE", None)
try:
    mnt.build_provider(_mount("无 Cookie", "115")).resolve("/电影/x.mkv")
    check("未配置 Cookie 时报凭据错误", False)
except mnt.MountAuthError as exc:
    check("未配置 Cookie 时报凭据错误", "Cookie" in str(exc), str(exc))
os.environ["PAN115_COOKIE"] = PAN_COOKIE


# ==================== 四、扫描集成 ====================

print("\n=== 扫描集成 ===")
db = SessionLocal()


def _purge_previous_runs() -> int:
    """清掉上次崩溃时留下的同名残留（本测试失败退出时不会走到收尾）

    开发库是共用的：残留的媒体库/条目会干扰 media_search 那类全局计数断言，
    所以每次开跑先把上一轮的残留收干净。
    """
    import re as _re

    pattern = _re.compile(
        r"^(本机挂载库|115 挂载库|STRM 挂载库|路径加挂载库|API 挂载库|云盘挂载库|"
        r"rclone 挂载库|空来源库|绑定停用库)\d+$"
    )
    stale = [l for l in db.query(em.Library).all() if pattern.match(l.name or "")]
    ids = [l.id for l in stale]
    rows = db.query(em.MediaItem).filter(em.MediaItem.library_id.in_(ids)).all() if ids else []
    rows += db.query(em.MediaItem).filter(em.MediaItem.file_path.like("mount://%")).all()
    seen: set[int] = set()
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        db.query(em.MediaStream).filter(em.MediaStream.item_id == row.id).delete(
            synchronize_session=False)
        db.query(em.UserMediaData).filter(em.UserMediaData.item_id == row.id).delete(
            synchronize_session=False)
        db.delete(row)
    if ids:
        db.query(em.Library).filter(em.Library.id.in_(ids)).delete(synchronize_session=False)
    db.query(em.StorageMount).delete()
    # 孤儿条目：库已经没了、条目还指向那个 id。SQLite 复用 id 时会把这些行算到新库头上
    alive = {l.id for l in db.query(em.Library).all()}
    for row in db.query(em.MediaItem).all():
        if row.library_id not in alive:
            db.query(em.MediaStream).filter(
                em.MediaStream.item_id == row.id).delete(synchronize_session=False)
            db.query(em.UserMediaData).filter(
                em.UserMediaData.item_id == row.id).delete(synchronize_session=False)
            db.delete(row)
            seen.add(-1)
    for lib in db.query(em.Library).all():
        if lib.mount_ids:
            lib.mount_ids = ""
    db.commit()
    return len(ids) + len(seen)


purged = _purge_previous_runs()
check("开跑前清理上一轮残留", True, f"媒体库 {purged} 项（正常情况下为 0）")

staff = models.WebUser(username=f"mount_stf{suf}", password_hash=hash_password("pass12345"),
                       is_staff=True, admin_role="super")
db.add(staff)

# 挂载必须先落库：扫描按挂载 id 从数据库取行（与后台保存后的行为一致）
local_row = _mount(f"本机源{suf}", "local", path=local_root)
strm_row = _mount(f"STRM 目录{suf}", "local", path=strm_root)
pan_row = _mount(f"115 源{suf}", "115", config={"cid": "100"})
db.add_all([local_row, strm_row, pan_row])
db.commit()
for row in (local_row, strm_row, pan_row):
    db.refresh(row)
db.refresh(staff)
staff_name = staff.username

lib_local = em.Library(guid=f"mountlocal{suf}", name=f"本机挂载库{suf}", collection_type="movies",
                       paths="", mount_ids=str(local_row.id), scrape_policy="missing_only")
lib_remote = em.Library(guid=f"mountremote{suf}", name=f"115 挂载库{suf}", collection_type="movies",
                        paths="", mount_ids=str(pan_row.id), scrape_policy="missing_only")
lib_strm = em.Library(guid=f"mountstrm{suf}", name=f"STRM 挂载库{suf}", collection_type="movies",
                      paths="", mount_ids=str(strm_row.id), scrape_policy="missing_only")
lib_dual = em.Library(guid=f"mountdual{suf}", name=f"路径加挂载库{suf}", collection_type="movies",
                      paths=local_root, mount_ids=str(local_row.id), scrape_policy="missing_only")
db.add_all([lib_local, lib_remote, lib_strm, lib_dual])
db.commit()
for row in (lib_local, lib_remote, lib_strm, lib_dual):
    db.refresh(row)
# 提前取出 id（避免后续 session 关闭后访问 ORM 属性触发 DetachedInstanceError）
lib_local_id, lib_remote_id = lib_local.id, lib_remote.id
lib_strm_id, lib_dual_id = lib_strm.id, lib_dual.id

snap = sc.LibrarySnapshot.of(lib_local)
check("配置快照记录挂载 id", snap.mount_ids == (local_row.id,), str(snap.mount_ids))
sources, failed = mnt.library_sources(lib_local, db)
check("来源解析：挂载可读且无失败来源",
      len(sources) == 1 and sources[0].kind == "mount" and failed == [],
      f"sources={len(sources)} failed={failed}")

# 假探测：远程源是 URL，无法按本机文件处理；本机源保持同样的返回值
_real_probe = sc.probe_metadata
sc.probe_metadata = lambda p, headers=None, size=0: {
    "duration_ticks": 8_000_000, "bitrate": 1_200_000, "width": 1920, "height": 1080,
    "video_codec": "H264", "audio_codec": "AAC", "audio_languages": "chi",
    "subtitle_languages": "", "streams": [], "size": size or 1024,
}

stats_local = sc.scan_library_sync(db, lib_local, sc.LibrarySnapshot.of(lib_local))
check("本机挂载库扫描入库", stats_local["added"] == 2 and stats_local["removal_skipped"] is False,
      str(stats_local))
local_items = db.query(em.MediaItem).filter(em.MediaItem.library_id == lib_local_id).all()
# 只关心带文件的条目（series / season 是层级节点，本身没有文件）
local_files = [(i.guid, i.file_path) for i in local_items if i.file_path]
check("本机挂载条目存真实路径",
      len(local_files) == 2
      and all(not i[1].startswith(mnt.MOUNT_PATH_PREFIX) for i in local_files),
      str([i[1] for i in local_files])[:120])

stats_remote = sc.scan_library_sync(db, lib_remote, sc.LibrarySnapshot.of(lib_remote))
check("远程挂载库扫描入库", stats_remote["added"] == 2, str(stats_remote))
remote_items = [
    i for i in db.query(em.MediaItem).filter(em.MediaItem.library_id == lib_remote_id).all()
    if i.file_path
]
check("远程挂载条目存 mount:// 路径",
      len(remote_items) == 2 and all(i.file_path.startswith("mount://") for i in remote_items),
      str([i.file_path for i in remote_items])[:140])
alpha = next((i for i in remote_items if "Alpha" in i.name), None)
check("远程挂载条目容器取真实扩展名", alpha is not None and alpha.container == "mkv",
      alpha.container if alpha else "无条目")
resolved_remote = mnt.resolve_play_target(alpha.file_path, db)
check("远程条目可解析成直链",
      resolved_remote.kind == "url" and resolved_remote.value.endswith("pc-alpha"),
      resolved_remote.value)
check("远程条目存在性判断走挂载", mnt.media_exists(alpha.file_path, db) is True)

# 停用挂载：来源失败 → 跳过清理，已入库条目不被误删
pan_row.is_enabled = False
db.commit()
sources_disabled, failed_disabled = mnt.library_sources(lib_remote, db)
check("停用挂载报为不可用来源",
      sources_disabled == [] and len(failed_disabled) == 1 and "停用" in failed_disabled[0]["reason"],
      str(failed_disabled))
stats_disabled = sc.scan_library_sync(db, lib_remote, sc.LibrarySnapshot.of(lib_remote))
still_there = db.query(em.MediaItem).filter(
    em.MediaItem.library_id == lib_remote_id, em.MediaItem.file_path.isnot(None),
).count()
check("来源不可用时跳过清理（不误删条目）",
      stats_disabled["removal_skipped"] is True and stats_disabled["removed"] == 0
      and still_there == 2, str(stats_disabled))
check("停用挂载的条目视为不可用", mnt.media_exists(alpha.file_path, db) is False)
pan_row.is_enabled = True
db.commit()

# 条目 guid 由路径决定：同一目录被路径与挂载同时引用时，不会再产生第二份条目
# 按本测试的目录前缀计数：全局计数与「按库 id」都会被其它测试的残留行干扰
# （开发库共用一个 SQLite，删除后 id 会被复用），路径是本测试独有的
def _dual_scope_count() -> int:
    return db.query(em.MediaItem).filter(
        em.MediaItem.file_path.like(f"{local_root}%")
    ).count()


before_rows = _dual_scope_count()
stats_dual = sc.scan_library_sync(db, lib_dual, sc.LibrarySnapshot.of(lib_dual))
check("路径与挂载指向同一目录时不重复入库", stats_dual["added"] == 0, str(stats_dual))
check("同一目录不会产生重复条目", _dual_scope_count() == before_rows,
      f"{before_rows} → {db.query(em.MediaItem).count()}")

stats_strm = sc.scan_library_sync(db, lib_strm, sc.LibrarySnapshot.of(lib_strm))
strm_items = db.query(em.MediaItem).filter(em.MediaItem.library_id == lib_strm_id).all()
strm_ok = next((i for i in strm_items if "Strm" in i.name), None)
check("STRM 挂载入库且容器取自直链", strm_ok is not None and strm_ok.container == "mkv",
      f"added={stats_strm['added']} container={strm_ok.container if strm_ok else '无'}")
check("STRM 条目播放解析成直链",
      mnt.resolve_play_target(strm_ok.file_path, db).kind == "url")
check("没有直链的 STRM 不会被入库",
      all("Broken" not in (i.name or "") for i in strm_items))
sc.probe_metadata = _real_probe


# ==================== 五、播放 / 文件 / 字幕 ====================

print("\n=== 播放 / 文件 / 字幕 ===")
from backend.emby_server import mount_routes  # noqa: E402
from backend.emby_server.api import emby_router  # noqa: E402

file_endpoints = [
    r.endpoint.__name__ for r in emby_router.routes
    if getattr(r, "path", "") in mount_routes.SUPERSEDED_FILE_PATHS
]
check("协议面里 /Items/{id}/File 已被挂载感知实现替换",
      file_endpoints == ["mounted_item_file", "mounted_item_file"], str(file_endpoints))

req = Request({"type": "http", "method": "GET", "path": "/Items/x/File", "headers": []})


def call_endpoint(value):
    """端点可能是同步 def（由线程池执行，v2.13.0 起大部分如此），也可能仍是 async def：
    两种都要能直接测，这里统一处理（避免端点改签名后测试静默失效）。"""
    return asyncio.run(value) if inspect.isawaitable(value) else value


# 中继已删（SEAM-3）：挂载远端 URL 条目不再走服务器代理转发。
# 无 drive_file_id 的挂载 URL 条目 -> 410；有 drive_file_id 的 Drive 条目 -> 302 usercontent 直链。
try:
    call_endpoint(mount_routes.mounted_item_file(alpha.guid, req, staff, db))
    check("中继已删：挂载远端 URL 不再走代理（410）", False)
except HTTPException as exc:
    check("中继已删：挂载远端 URL 不再走代理（410）", exc.status_code == 410, str(exc.status_code))

alpha.drive_file_id = "smoke-fid-302"
# 注意：mounted_item_file 的 @release_db_before_response 装饰器每次调用后会关闭 session、
# 把 alpha 逐出；直接赋值 + flush 发不出 UPDATE，必须先 db.add 重新挂回。
db.add(alpha)
db.flush()
fid_resp = call_endpoint(mount_routes.mounted_item_file(alpha.guid, req, staff, db))
fid_loc = fid_resp.headers.get("location", "") if isinstance(fid_resp, RedirectResponse) else ""
check("Drive 条目 302 到 usercontent 直链",
      isinstance(fid_resp, RedirectResponse) and fid_resp.status_code == 302
      and "smoke-fid-302" in fid_loc, fid_loc or type(fid_resp).__name__)
alpha.drive_file_id = None
db.add(alpha)
db.flush()

local_only = next(i for i in local_files if (i[1] or "").endswith("Local.Movie.2024.1080p.mkv"))
local_resp = call_endpoint(mount_routes.mounted_item_file(local_only[0], req, staff, db))
check("本机条目仍直接返回文件", isinstance(local_resp, FileResponse), type(local_resp).__name__)

sub_source = subs.resolve_mount_source(alpha.file_path)
check("字幕模块通过钩子解析挂载路径",
      sub_source is not None and str(sub_source[0]).endswith("pc-alpha"), str(sub_source))
check("字幕钩子对普通路径返回 None", subs.resolve_mount_source("/media/x.mkv") is None)

# pan_row 的挂载根就是 cid 100（电影目录），所以 rel 不带目录前缀
srt_mount_path = mnt.mount_path(pan_row.id, "/Beta.S01E01.1080p.zh.srt")
check("挂载里的外挂字幕也能解析成直链",
      (subs.resolve_mount_source(srt_mount_path) or ("", {}))[0].endswith("pc-sub"),
      str(subs.resolve_mount_source(srt_mount_path)))


class _SubStream:
    is_external = True
    external_path = srt_mount_path
    codec = "subrip"
    stream_index = 5


sub_resp = subs.render_subtitle(item_guid="subtest", media_path=None, stream=_SubStream(),
                                fmt="vtt", subtitle_ordinal=0)
check("挂载外挂字幕取回并转成 WebVTT",
      sub_resp.status_code == 200 and sub_resp.body.decode(errors="ignore").startswith("WEBVTT"),
      sub_resp.body[:16].decode(errors="ignore"))


# ==================== 七、rclone 挂载（rc / cli）====================

print("\n=== rclone 挂载（rc / cli）===")

RC_CONFIG = {"mode": "rc", "fs": "gdrive:Movies", "rc_url": "http://127.0.0.1:5572"}
rc_mount = _mount("rclone rc", "rclone", config=RC_CONFIG)
rc = mnt.build_provider(rc_mount)
check("rclone 类型可浏览 / 支持 remote 列表",
      mnt.supports_browse("rclone") and mnt.type_meta("rclone").get("remotes") is True,
      str(mnt.type_meta("rclone")))
check("rclone RC 测试连接", mnt.test_mount(rc_mount)["ok"], str(mnt.test_mount(rc_mount)))
check("rclone RC 列目录走 RC API", any("/operations/list" in call for call in FAKE.rc_calls))
check("rclone RC 递归枚举媒体",
      sorted(f.rel for f in rc.walk_media()) == ["/A.Movie.2024.mkv", "/Link.strm", "/Sub/B.mkv"],
      str(sorted(f.rel for f in rc.walk_media())))
# rc-serve 用方括号把 remote 名与本机目录区分开（见 mount_rclone._rc_play_url）：
# 写成 /gdrive:Movies/x 会被当成叫「gdrive:Movies」的本机目录而 404。
check("rclone RC 播放地址走 rc-serve（remote 名要套方括号）",
      rc.resolve("/A.Movie.2024.mkv").value
      == "http://127.0.0.1:5572/[gdrive:Movies]/A.Movie.2024.mkv",
      rc.resolve("/A.Movie.2024.mkv").value)
check(".strm 在 rclone 挂载里也按内容解析",
      rc.resolve_final("/Link.strm").value == FAKE.rclone_strm_body,
      rc.resolve_final("/Link.strm").value)
check("rclone 可列出已配置的 remote",
      mount_rclone.list_remotes("http://127.0.0.1:5572") == ["gdrive:", "onedrive:", "s3:"],
      str(mount_rclone.list_remotes("http://127.0.0.1:5572")))
FAKE.rc_serve = False
msg = mnt.test_mount(rc_mount)["message"]
check("rclone RC 未开 rc-serve 时给出提示", "rc-serve" in msg, msg)
FAKE.rc_serve = True

FAKE.rc_auth = "u:secret-rc"
auth_mount = _mount("rclone rc 认证", "rclone",
                    config={**RC_CONFIG, "rc_user": "u", "rc_pass": "secret-rc"})
check("rclone RC 带认证可访问", mnt.test_mount(auth_mount)["ok"], str(mnt.test_mount(auth_mount)))
check("rclone RC 播放地址带 Basic 认证头",
      mnt.build_provider(auth_mount).resolve("/A.Movie.2024.mkv")
      .headers.get("Authorization", "").startswith("Basic "))
check("rclone RC 认证失败识别为凭据问题",
      mnt.test_mount(rc_mount).get("auth_error") is True)
FAKE.rc_auth = ""

CLI_CONFIG = {"mode": "cli", "fs": "gdrive:Movies",
              "rclone_bin": FakeRcloneCLI.RCLONE_PATH}
cli_mount = _mount("rclone cli", "rclone", config=CLI_CONFIG)
cli = mnt.build_provider(cli_mount)
check("rclone CLI 测试连接", mnt.test_mount(cli_mount)["ok"], str(mnt.test_mount(cli_mount)))
check("rclone CLI 递归枚举媒体",
      sorted(f.rel for f in cli.walk_media()) == ["/A.Movie.2024.mkv", "/Link.strm", "/Sub/B.mkv"],
      str(sorted(f.rel for f in cli.walk_media())))
check("rclone CLI 用 link 取直链",
      cli.resolve("/A.Movie.2024.mkv").value == "https://cdn.example.com/rclone/linked",
      cli.resolve("/A.Movie.2024.mkv").value)
check("rclone CLI 读 .strm 用 cat",
      cli.resolve_final("/Link.strm").value == FAKE.rclone_strm_body,
      cli.resolve_final("/Link.strm").value)
check("rclone CLI 也能列出 remote",
      mount_rclone.list_remotes(mode="cli", bin_path=FakeRcloneCLI.RCLONE_PATH)
      == ["gdrive:", "onedrive:", "s3:"])
try:
    cli.resolve("/nolink.mkv")
    check("rclone CLI 后端不支持公开链接时提示改用 rc 模式", False)
except mnt.MountError as exc:
    check("rclone CLI 后端不支持公开链接时提示改用 rc 模式", "rc 模式" in str(exc), str(exc))
try:
    cli.list_dir("/Nope")
    check("rclone CLI 路径不存在时报错", False)
except mnt.MountError as exc:
    check("rclone CLI 路径不存在时报错", "路径不存在" in str(exc), str(exc))
RCLONE_CLI.mode = "auth_fail"
check("rclone CLI 凭据失效识别为凭据问题", mnt.test_mount(cli_mount).get("auth_error") is True)
RCLONE_CLI.mode = "ok"
try:
    mnt.build_provider(_mount("rclone 未安装", "rclone",
                              config={"mode": "cli", "fs": "gdrive:",
                                      "rclone_bin": "rclone-not-installed"})).test()
    check("rclone 未安装时报可操作错误", False)
except mnt.MountError as exc:
    check("rclone 未安装时报可操作错误", "找不到 rclone" in str(exc), str(exc))
try:
    mnt.build_provider(_mount("rclone 无 remote", "rclone", config={"mode": "rc"})).test()
    check("rclone 缺 remote 时报配置错误", False)
except mnt.MountError as exc:
    check("rclone 缺 remote 时报配置错误", "remote" in str(exc), str(exc))


# ==================== 八、管理端 API ====================

print("\n=== 管理端 API ===")
r = client.post("/api/user/auth/login", json={"username": staff_name, "password": "pass12345"})
assert r.status_code == 200, r.text
sh = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = client.get("/api/admin/emby/mounts")
check("未登录访问挂载接口被拒绝", r.status_code in (401, 403), str(r.status_code))

r = client.get("/api/admin/emby/mounts", headers=sh)
check("挂载列表返回类型元数据",
      r.status_code == 200 and any(t["value"] == "115" for t in r.json()["mount_types"]),
      r.text[:100])

r = client.post("/api/admin/emby/mounts",
                json={"name": f"API 本机{suf}", "mount_type": "local", "path": local_root},
                headers=sh)
check("创建本机挂载", r.status_code == 200, r.text[:120])
api_local_id = r.json()["mount"]["id"]

r = client.post("/api/admin/emby/mounts",
                json={"name": f"API 本机{suf}", "mount_type": "local", "path": local_root},
                headers=sh)
check("挂载重名被拒绝", r.status_code == 400, r.text[:120])

r = client.post("/api/admin/emby/mounts",
                json={"name": f"坏类型{suf}", "mount_type": "ftp",
                      "path": f"/media/{suf}"}, headers=sh)
check("未知类型被拒绝", r.status_code == 400, r.text[:120])

r = client.post("/api/admin/emby/mounts",
                json={"name": f"无前缀{suf}", "mount_type": "rclone",
                      "path": f"gdrive:Movies{suf}"}, headers=sh)
check("路径没带来源前缀被拒绝（不静默按调用方给的类型存）",
      r.status_code == 400 and "前缀" in r.json().get("detail", ""), r.text[:160])

r = client.post("/api/admin/emby/mounts",
                json={"name": f"坏路径{suf}", "mount_type": "local", "path": "/nope/nope"}, headers=sh)
check("路径不存在被拒绝", r.status_code == 400, r.text[:120])

r = client.post("/api/admin/emby/mounts",
                json={"name": f"API rclone 脱敏{suf}", "mount_type": "rclone",
                      "path": "rclone:gdrive:Movies",
                      "config": {"mode": "rc", "rc_user": "u", "rc_pass": "super-secret-pw"}},
                headers=sh)
check("创建 rclone 挂载", r.status_code == 200, r.text[:120])
rclone_api_id = r.json()["mount"]["id"]
check("配置回传脱敏（密码只报已配置）",
      "super-secret-pw" not in r.text and "rc_pass" in r.json()["mount"]["secret_keys"],
      str(r.json()["mount"]["secret_keys"]))

r = client.put(f"/api/admin/emby/mounts/{rclone_api_id}", json={"remark": "改备注"}, headers=sh)
check("更新挂载返回「需重新扫描」",
      r.status_code == 200 and r.json()["rescan_required"] is True, r.text[:120])
mounts_now = client.get("/api/admin/emby/mounts", headers=sh).json()["mounts"]
rclone_row = next(m for m in mounts_now if m["id"] == rclone_api_id)
check("留空密钥不会把已保存的密钥抹掉", "rc_pass" in rclone_row["secret_keys"],
      str(rclone_row["secret_keys"]))

# 换源：类型是路径前缀的函数，改前缀就等于换来源。
# 这件事必须让前端知道——已入库的条目是**旧**来源解释出来的，不会自己改口径。
r = client.post("/api/admin/emby/mounts",
                json={"name": f"API 换源{suf}", "mount_type": "local", "path": local_root},
                headers=sh)
switch_id = r.json()["mount"]["id"]

r = client.put(f"/api/admin/emby/mounts/{switch_id}", json={"remark": "只改备注"}, headers=sh)
check("只改备注不报换源", r.json().get("mount_type_changed") is False, r.text[:120])

r = client.put(f"/api/admin/emby/mounts/{switch_id}",
               json={"path": "rclone:gdrive:Movies", "config": {"mode": "rc"}}, headers=sh)
check("改路径前缀 = 换来源，并回传这个事实让前端提醒重扫",
      r.status_code == 200 and r.json().get("mount_type_changed") is True
      and r.json()["mount"]["mount_type"] == "rclone"
      and r.json()["mount"]["path"] == "rclone:gdrive:Movies", r.text[:160])
check("换源后类型不再接受调用方给的旧值（类型只由路径决定）",
      r.json()["mount"]["mount_type"] != "local", r.json()["mount"]["mount_type"])
client.delete(f"/api/admin/emby/mounts/{switch_id}", headers=sh)

r = client.post(f"/api/admin/emby/mounts/{rclone_api_id}/test", headers=sh)
check("测试已保存的挂载并记录结果",
      r.status_code == 200 and r.json()["success"] is True
      and r.json()["mount"]["last_check_ok"] is True, r.text[:140])

r = client.post("/api/admin/emby/mounts/test",
                json={"mount_type": "rclone", "path": "rclone:gdrive:Movies",
                      "config": {"mode": "rc", "rc_url": "http://127.0.0.1:5572"}},
                headers=sh)
check("测试未保存的配置（先测再存）", r.status_code == 200 and r.json()["success"] is True,
      r.text[:120])

r = client.post("/api/admin/emby/mounts/test",
                json={"mount_type": "115", "path": "115:/0", "config": {"cid": "0"}},
                headers=sh)
check("未保存的 115 配置也能测试",
      r.status_code == 200 and r.json()["success"] is True, r.text[:120])

r = client.post("/api/admin/emby/mounts",
                json={"name": f"API 115{suf}", "mount_type": "115", "path": "115:/0",
                      "config": {"cid": "0"}},
                headers=sh)
check("创建 115 直挂", r.status_code == 200, r.text[:120])
pan_api_id = r.json()["mount"]["id"]

r = client.get(f"/api/admin/emby/mounts/{pan_api_id}/browse", params={"rel": "/"}, headers=sh)
check("浏览 115 目录并带目录 ID",
      r.status_code == 200
      and any(e["is_dir"] and e.get("entry_id") == "100" for e in r.json()["entries"]),
      r.text[:140])

r = client.get(f"/api/admin/emby/mounts/{api_local_id}/browse", params={"rel": "/"}, headers=sh)
check("浏览本机挂载目录", r.status_code == 200 and r.json()["total"] >= 3, r.text[:120])

r = client.post("/api/admin/emby/libraries",
                json={"name": f"API 挂载库{suf}", "collection_type": "movies", "paths": [],
                      "mount_ids": [api_local_id, pan_api_id]}, headers=sh)
check("媒体库可绑定多个挂载", r.status_code == 200, r.text[:140])
api_lib_id = r.json()["id"]

r = client.post("/api/admin/emby/libraries",
                json={"name": f"空来源库{suf}", "collection_type": "movies", "paths": []},
                headers=sh)
check("既没有路径也没有挂载时被拒绝", r.status_code == 400, r.text[:120])

r = client.get("/api/admin/emby/libraries", headers=sh)
lib_row = next(l for l in r.json()["libraries"] if l["id"] == api_lib_id)
check("媒体库列表返回挂载绑定",
      sorted(lib_row["mount_ids"]) == sorted([api_local_id, pan_api_id]), str(lib_row["mount_ids"]))

r = client.post("/api/admin/emby/mounts",
                json={"name": f"停用挂载{suf}", "mount_type": "local", "path": local_root,
                      "is_enabled": False}, headers=sh)
check("创建时可直接停用", r.status_code == 200, r.text[:120])
disabled_id = r.json()["mount"]["id"]
r = client.post("/api/admin/emby/libraries",
                json={"name": f"绑定停用库{suf}", "collection_type": "movies", "paths": [],
                      "mount_ids": [disabled_id]}, headers=sh)
check("不能绑定已停用的挂载", r.status_code == 400, r.text[:120])

r = client.delete(f"/api/admin/emby/mounts/{pan_api_id}", headers=sh)
check("删除挂载并解绑媒体库",
      r.status_code == 200 and r.json()["unbound_libraries"] == 1, r.text[:120])
mounts_after = client.get("/api/admin/emby/mounts", headers=sh).json()["mounts"]
check("删除后挂载列表不再包含它",
      all(m["id"] != pan_api_id for m in mounts_after), str([m["id"] for m in mounts_after]))
lib_after = next(l for l in client.get("/api/admin/emby/libraries", headers=sh).json()["libraries"]
                 if l["id"] == api_lib_id)
check("删除后媒体库不再引用它", pan_api_id not in lib_after["mount_ids"], str(lib_after["mount_ids"]))
check("删除挂载不会删掉已入库条目",
      db.query(em.MediaItem).filter(
          em.MediaItem.file_path.like(f"mount://{pan_row.id}/%"),
      ).count() == 2)

# rclone.conf 由用户粘贴：落盘后只回 remote 名（不回原文）
CONF_BODY = ("[gdrive]\ntype = drive\nclient_id = cid\n"
             "\n[onedrive]\ntype = onedrive\n\n[s3]\ntype = s3\n")
r = client.post("/api/admin/emby/mounts/rclone/conf", json={"conf": CONF_BODY}, headers=sh)
check("粘贴 rclone.conf 能落盘并解析出 remote",
      r.status_code == 200 and r.json()["remotes"] == ["gdrive:", "onedrive:", "s3:"],
      r.text[:160])

r = client.get("/api/admin/emby/mounts/rclone/conf", headers=sh)
check("读 rclone.conf 只回 remote 名，不回原文（里面全是 token）",
      r.status_code == 200 and r.json()["remotes"] == ["gdrive:", "onedrive:", "s3:"]
      and "client_id" not in r.text, r.text[:160])

r = client.post("/api/admin/emby/mounts/rclone/conf",
                json={"conf": "[断掉的\ntype = drive\n"}, headers=sh)
check("非法 INI 当场拒绝（不等到扫描才发现一个 remote 都没有）",
      r.status_code == 400 and "INI" in r.json().get("detail", ""), r.text[:160])

r = client.post("/api/admin/emby/mounts/rclone/conf",
                json={"conf": "[gdrive]\nclient_id = cid\n"}, headers=sh)
check("缺 type 的 remote 段被拒绝", r.status_code == 400, r.text[:160])

r = client.post("/api/admin/emby/mounts",
                json={"name": f"API rclone{suf}", "mount_type": "rclone",
                      "path": "rclone:gdrive:Movies",
                      "config": {**RC_CONFIG, "rc_pass": "secret-rc-pass"}}, headers=sh)
check("创建 rclone 挂载", r.status_code == 200, r.text[:160])
rclone_api = r.json()["mount"]
check("rclone 的 RC 密码同样脱敏",
      "secret-rc-pass" not in r.text and "rc_pass" in rclone_api["secret_keys"],
      str(rclone_api["secret_keys"]))
r = client.post(f"/api/admin/emby/mounts/{rclone_api['id']}/test", headers=sh)
check("测试已保存的 rclone 挂载",
      r.status_code == 200 and r.json()["success"] is True, r.text[:160])

r = client.post("/api/admin/emby/libraries",
                json={"name": f"rclone 挂载库{suf}", "collection_type": "movies", "paths": [],
                      "mount_ids": [rclone_api["id"]]}, headers=sh)
check("媒体库可绑定 rclone 挂载", r.status_code == 200, r.text[:160])
rclone_lib_id = r.json()["id"]

# 远程挂载条目的 file_path 是 mount://，播放/存在性都靠提供者解析
cloud_item = db.query(em.MediaItem).filter(
    em.MediaItem.library_id == lib_remote_id,
).first()
check("播放入口统一走 resolve_final（远程条目）",
      mnt.resolve_play_target(cloud_item.file_path, db).kind == "url",
      str(cloud_item.file_path))

# ==================== 收尾：清理本次测试写入的数据 ====================
# 冒烟测试共用同一个数据库：远程挂载条目按 mount://<id>/<rel> 生成 guid，
# 每次运行的挂载 id 都不同，不清理会持续累积（并影响其它测试的全局统计断言）。
cleanup_lib_ids = [x for x in (lib_local_id, lib_remote_id, lib_strm_id, lib_dual_id,
                              api_lib_id, rclone_lib_id) if x]
cleanup_rows = db.query(em.MediaItem).filter(em.MediaItem.library_id.in_(cleanup_lib_ids)).all()
for row in cleanup_rows:
    db.query(em.MediaStream).filter(em.MediaStream.item_id == row.id).delete(synchronize_session=False)
    db.query(em.UserMediaData).filter(em.UserMediaData.item_id == row.id).delete(synchronize_session=False)
    db.delete(row)
db.query(em.Library).filter(em.Library.id.in_(cleanup_lib_ids)).delete(synchronize_session=False)
db.query(em.StorageMount).filter(em.StorageMount.name.like(f"%{suf}")).delete(synchronize_session=False)
db.query(models.WebUser).filter(models.WebUser.username == staff_name).delete(synchronize_session=False)
db.commit()
removed_items = len(cleanup_rows)
check("收尾：本次测试的媒体库与挂载已清理",
      db.query(em.Library).filter(em.Library.id.in_(cleanup_lib_ids)).count() == 0,
      f"条目 {removed_items} / 媒体库 {len(cleanup_lib_ids)}")

httpx.Client = _real_httpx_client  # type: ignore[assignment]
subprocess.run = _real_subprocess_run  # type: ignore[assignment]
db.close()

print("\n" + "=" * 60)
if failures:
    print(f"❌ 失败 {len(failures)} 项: {failures}")
    sys.exit(1)
print(f"✅ 存储挂载冒烟测试全部通过（{len(failures)} 失败）")
