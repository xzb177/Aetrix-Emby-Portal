"""部署自检：把真实服务跑起来，然后用真实 HTTP 走一遍关键链路

冒烟测试用的是 FastAPI TestClient（进程内、网络层被假服务替换），本脚本是它的补充：
**真的起 uvicorn、真的发 HTTP、真的读写数据库**，用来回答「这套东西部署起来到底能不能用」。

覆盖：

1. 预检：依赖可导入、数据库可初始化、前后端静态产物是否存在（缺了只警告）
2. 单进程模式：`python serve.py`（`PORT` 可覆盖）在超时内就绪，否则打印服务日志
3. 静态面：用户端 `/`、管理端 `/admin/` 拿到构建产物
4. 协议面：`/emby/System/Info/Public` 可识别；受保护端点未带 token 时被拒
5. 管理面：错误密码被拒、登录拿 token、挂载类型表（含 rclone）、rclone remote 接口的失败提示
6. 真实读写：创建本机挂载 → 浏览目录 → 测试连接 → 建媒体库并绑定 → 触发扫描 →
   条目真的入库（`/api/admin/emby/items` 查得到）
7. 分离部署：再起一套 `ENABLE_EMBY_GATEWAY=false` 的 EM + 独立 `serve_emby.py`，
   验证 EM 交出协议面并给出「去连 EA」指引、EA 上报配对状态、**客户端在 EA 上
   用 EM 库里的账号完成认证**（跨服务配对）
8. 收尾：删掉自检创建的账号 / 挂载 / 媒体库与条目，并停掉全部服务

用法::

    python3 scripts/deploy_check.py                 # 默认端口 8010（分离模式用 8011/8012）
    python3 scripts/deploy_check.py --port 9000
    python3 scripts/deploy_check.py --keep          # 自检完不杀进程（调试用）

退出码 0 = 全部通过。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import secrets
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 不写死库文件名：交给 backend.database 解析，升级上来的老部署继续用原库文件
os.environ.setdefault("DATABASE_TYPE", "sqlite")

MEDIA_NAME = "Deploy.Check.2024.1080p.mkv"
failures: list[str] = []
warnings: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def warn(name: str, detail: str = "") -> None:
    print(f"WARN  {name}{(' — ' + detail) if detail else ''}")
    warnings.append(name)


# ==================== HTTP 小工具 ====================

def request(method: str, url: str, *, token: str = "", body=None, timeout: float = 20,
            emby_token: str = "", extra_headers: dict | None = None):
    """返回 (状态码, 文本, 响应头)；非 2xx 不抛异常，由调用方断言

    `token` 走门户的 Bearer；`emby_token` 走 Emby 客户端的两套头（客户端两种都用）；
    `extra_headers` 给内部调用用（例如 EM → EA 的挂载体检要带 X-Panel-Key）。
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if emby_token:
        req.add_header("X-Emby-Token", emby_token)
        req.add_header("X-MediaBrowser-Token", emby_token)
    for name, value in (extra_headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore"), dict(exc.headers or {})
    except Exception as exc:  # noqa: BLE001 — 连接失败统一成 0 状态码
        return 0, str(exc), {}


def as_json(text: str):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


# ==================== 一、预检 ====================

def preflight(media_dir: str) -> tuple[int, str, str]:
    print("=== 一、部署预检 ===")
    try:
        import fastapi  # noqa: F401
        import sqlalchemy  # noqa: F401
        import uvicorn  # noqa: F401

        check("后端依赖可导入（fastapi / uvicorn / sqlalchemy）", True)
    except Exception as exc:  # noqa: BLE001
        check("后端依赖可导入（fastapi / uvicorn / sqlalchemy）", False, str(exc))
        raise SystemExit(1)

    from backend.database import DATABASE_TYPE, SessionLocal, init_db

    try:
        init_db()
        SessionLocal().close()
        check("数据库可连接并初始化", True, f"type={DATABASE_TYPE}")
    except Exception as exc:  # noqa: BLE001
        check("数据库可连接并初始化", False, str(exc))
        raise SystemExit(1)

    # 必须看 EM **真正托管**的那两个目录（可被 FRONTEND_DIST / ADMIN_DIST 覆盖）——
    # 不能拿别的目录当依据，否则会出现“构建产物其实是旧的”这种假绿。
    for label, dist_env, default in (
        ("用户端", "FRONTEND_DIST", os.path.join(ROOT, "user_frontend", "dist")),
        ("管理端", "ADMIN_DIST", os.path.join(ROOT, "admin_frontend", "dist")),
    ):
        dist = Path(os.getenv(dist_env) or default)
        rel = os.path.relpath(dist, ROOT)
        if not (dist / "index.html").is_file():
            warn(f"{label}构建产物缺失", f"{rel}（构建后页面才渲染）")
            continue
        check(f"{label}构建产物存在（EM 实际托管的目录）", True, rel)
        # 产物比源码旧 = 部署上去的是旧界面，这种问题从代码里看不出来
        src = dist.parent / "src"
        stale = []
        if src.is_dir():
            built = (dist / "index.html").stat().st_mtime
            stale = [p for p in src.rglob("*") if p.is_file() and p.stat().st_mtime > built]
        if stale:
            warn(f"{label}构建产物落后于源码",
                 f"{rel} 比源码旧（{len(stale)} 个文件更新，如 {stale[0].name}）；重新构建才不会部署旧界面")
        else:
            check(f"{label}构建产物不落后于源码", True, rel)

    if not shutil.which("ffprobe"):
        warn("本机没有 ffprobe", "媒体探测会退化为按文件大小建档（部署环境建议装 ffmpeg）")

    from backend import models
    from backend.emby_server.auth import ensure_emby_credentials
    from backend.security import hash_password

    suffix = random.randint(100000, 999999)
    username = f"deploy_check_{suffix}"
    password = f"DeployCheck#{suffix}"
    db = SessionLocal()
    user = models.WebUser(username=username, password_hash=hash_password(password),
                          is_staff=True, admin_role="super", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    # 写入播放密码哈希，不自动生成 emby_username。没有接线用户名时，
    # AuthenticateByName 仍按门户用户名命中（emby_username OR username）。
    # 只有门户密码、没有 emby_password 时 EA 会正确地回 401。
    ensure_emby_credentials(db, user, password=password)
    user_id = user.id
    db.close()
    check("自检账号已创建（含 Emby 客户端凭据）", True, username)

    with open(os.path.join(media_dir, MEDIA_NAME), "wb") as fh:
        fh.write(b"\x00" * 4096)
    return user_id, username, password


# ==================== 二、冷启动 ====================

def start_service(script: str, port: int, timeout: float, *, label: str,
                  extra_env: dict | None = None) -> tuple[subprocess.Popen, str, str]:
    """启动一个服务进程，等它 /api/health 就绪；起不来就打印日志尾部并退出"""
    log_path = os.path.join(tempfile.gettempdir(), f"deploy_check_{port}.log")
    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — 子进程持有
    # PORT 给 EM（serve.py），EMBY_API_PORT 给 EA（serve_emby.py），两个都设省得区分
    env = {**os.environ, "PORT": str(port), "EMBY_API_PORT": str(port), "HOST": "0.0.0.0",
           **(extra_env or {})}
    proc = subprocess.Popen([sys.executable, script], cwd=ROOT, env=env,
                            stdout=log_file, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    started = time.time()
    while time.time() - started < timeout:
        if proc.poll() is not None:
            break
        status, text, _ = request("GET", f"{base}/api/health", timeout=3)
        if status == 200 and as_json(text):
            check(f"{label}在超时内就绪", True, f"{time.time() - started:.1f}s（{base}）")
            return proc, base, log_path
        time.sleep(1)

    tail = ""
    try:
        with open(log_path, encoding="utf-8") as fh:
            tail = "".join(fh.readlines()[-15:]).strip()
    except OSError:
        pass
    check(f"{label}在超时内就绪", False, f"{timeout}s 未就绪（日志 {log_path}）")
    if tail:
        print("---- 服务日志尾部 ----")
        print(tail)
        print("----------------------")
    proc.terminate()
    raise SystemExit(1)


# ==================== 三、真实 HTTP 检查 ====================

def run_checks(base: str, username: str, password: str, media_dir: str,
               token: str) -> None:
    print("\n=== 三、静态面与协议面 ===")
    status, text, _ = request("GET", f"{base}/api/health")
    payload = as_json(text) or {}
    check("健康检查可用", status == 200 and payload.get("status") == "healthy",
          f"HTTP {status} {json.dumps(payload, ensure_ascii=False)[:110]}")

    status, text, headers = request("GET", f"{base}/")
    check("用户端首页可访问", status == 200 and 'id="app"' in text,
          f"HTTP {status} {headers.get('Content-Type', '')}")

    status, text, _ = request("GET", f"{base}/admin/")
    check("管理端首页可访问", status == 200 and 'id="app"' in text, f"HTTP {status}")

    for path in ("/emby/System/Info/Public", "/emby/system/info/public", "/System/Info/Public"):
        status, text, _ = request("GET", f"{base}{path}")
        info = as_json(text) or {}
        check(f"Emby 协议：{path} 可识别",
              status == 200 and bool(info.get("ServerName") or info.get("Version")),
              f"HTTP {status} {json.dumps(info, ensure_ascii=False)[:90]}")

    status, _, _ = request("GET", f"{base}/emby/Users/Me")
    check("Emby 协议：受保护端点未带 token 时被拒", status in (401, 403), f"HTTP {status}")

    print("\n=== 四、管理面 ===")
    status, _, _ = request("POST", f"{base}/api/admin/auth/login",
                           body={"username": username, "password": "definitely-wrong"})
    check("错误密码被拒绝", status in (400, 401, 403), f"HTTP {status}")

    status, _, _ = request("GET", f"{base}/api/admin/emby/mounts")
    check("未登录访问管理接口被拒", status in (401, 403), f"HTTP {status}")

    status, text, _ = request("GET", f"{base}/api/admin/emby/mounts", token=token)
    mounts_payload = as_json(text) or {}
    types = [t.get("value") for t in mounts_payload.get("mount_types") or []]
    # v2.42.12 起精简后四种、#1255 加 yun139 成五种（类型由路径前缀决定；
    # drive 是后加的 API 直读类型，不是恢复当年下线的 gdrive 原生类型）。
    # 这里断言**集合相等**而不是「至少 N 种」：以前写的是 >= 10，
    # 往回加一种删不掉一种都照样绿。
    check("挂载类型表可下发且为五种（local/115/rclone/drive/yun139）",
          status == 200 and sorted(types) == ["115", "drive", "local", "rclone", "yun139"],
          f"HTTP {status} {len(types)} 种：{','.join(types)}")
    check("rclone 类型已注册且声明 remote 选择器",
          any(t.get("value") == "rclone" and t.get("remotes")
              for t in mounts_payload.get("mount_types") or []))

    status, text, _ = request("GET", f"{base}/api/admin/emby/mounts/rclone/conf", token=token)
    conf = as_json(text) or {}
    check("rclone.conf 接口可读，且只回 remote 名不回明文",
          status == 200 and "remotes" in conf and "conf" not in conf,
          f"HTTP {status} {str(conf)[:70]}")

    print("\n=== 五、存储挂载与媒体库（真实读写）===")
    suffix = random.randint(100000, 999999)
    mount_name = f"部署自检挂载{suffix}"
    status, text, _ = request("POST", f"{base}/api/admin/emby/mounts", token=token,
                              body={"name": mount_name, "mount_type": "local", "path": media_dir})
    mount_id = ((as_json(text) or {}).get("mount") or {}).get("id")
    check("创建本机挂载", status == 200 and bool(mount_id), f"HTTP {status} {text[:80]}")
    if not mount_id:
        return

    status, text, _ = request("GET", f"{base}/api/admin/emby/mounts/{mount_id}/browse",
                              token=token)
    names = [e.get("name") for e in (as_json(text) or {}).get("entries") or []]
    check("浏览挂载目录", status == 200 and MEDIA_NAME in names, f"HTTP {status} {names}")

    status, text, _ = request("POST", f"{base}/api/admin/emby/mounts/{mount_id}/test", token=token)
    check("测试已保存的挂载", status == 200 and (as_json(text) or {}).get("success") is True,
          f"HTTP {status}")

    status, text, _ = request("POST", f"{base}/api/admin/emby/libraries", token=token,
                              body={"name": f"部署自检库{suffix}", "collection_type": "movies",
                                    "paths": [], "mount_ids": [mount_id]})
    lib_id = (as_json(text) or {}).get("id")
    check("媒体库绑定挂载", status == 200 and bool(lib_id), f"HTTP {status} {text[:80]}")
    if not lib_id:
        return

    status, _, _ = request("POST", f"{base}/api/admin/emby/libraries/{lib_id}/scan", token=token)
    check("触发扫描任务", status in (200, 202), f"HTTP {status}")

    ingested, item_path = 0, ""
    for _ in range(25):
        time.sleep(1)
        status, text, _ = request("GET", f"{base}/api/admin/emby/items?"
                                   + urllib.parse.urlencode({"limit": 200}), token=token)
        rows = [i for i in (as_json(text) or {}).get("items") or []
                if i.get("library_id") == lib_id]
        if rows:
            ingested, item_path = len(rows), str(rows[0].get("file_path") or "")
            break
    check("扫描把挂载里的媒体入库", ingested > 0, f"入库 {ingested} 条 {item_path}")

    # R41：item_count 是扫描结束时才回写的计数 —— 必须等扫描跑完再断言，
    # 否则慢 runner 上扫描还在 processing 阶段就查列表，item_count 仍为 0 误判失败。
    # （2026-10-14 PR #866 实证：items 接口已入库 1 条，但 is_scanning=True 时该断言翻红；
    #  同名检查在 1 小时前的主干 63a84fd5 上是绿的，纯时序 flake）
    scan_done = False
    for _ in range(60):
        time.sleep(1)
        status, text, _ = request("GET", f"{base}/api/admin/emby/libraries", token=token)
        libs = [l for l in (as_json(text) or {}).get("libraries") or [] if l.get("id") == lib_id]
        if libs and not libs[0].get("is_scanning"):
            scan_done = True
            break
    check("扫描任务结束后再查计数", scan_done, "" if scan_done else "60s 内扫描未结束")

    status, text, _ = request("GET", f"{base}/api/admin/emby/libraries", token=token)
    rows = [l for l in (as_json(text) or {}).get("libraries") or [] if l.get("id") == lib_id]
    check("媒体库列表回传条目数与挂载绑定",
          bool(rows) and rows[0].get("mount_ids") == [mount_id]
          and int(rows[0].get("item_count") or 0) >= 1,
          f"HTTP {status} {rows[0] if rows else '未找到'}")

    status, text, _ = request("GET", f"{base}/api/admin/emby/items", token=token)
    check("条目列表接口可用", status == 200 and "items" in (as_json(text) or {}), f"HTTP {status}")


def expect_startup_refused(script: str, port: int, *, label: str, extra_env: dict | None,
                          expect: str, timeout: float = 30) -> None:
    """起一个**应该失败**的服务，验证它真的拒绝启动并给出可操作提示

    EA 的配对闸门就是靠这个语义生效的：宁可拒启，也不要跑一个「能连上但谁都播不了」的假服务。

    必须显式清掉 ``ALLOW_EPHEMERAL_SECRET``：它是**合法**的本地开发开关
    （见 ``backend.security.validate_secret_key``），开着时缺 ``SECRET_KEY`` 也能启动——
    继承环境变量会让本项断言变成「取决于本机的开发配置」，在开发机上误报失败，
    而它要验的恰恰是「没有这个开关时会不会拒启」。
    """
    log_path = os.path.join(tempfile.gettempdir(), f"deploy_check_{port}.log")
    inherited = {k: v for k, v in os.environ.items() if k != "ALLOW_EPHEMERAL_SECRET"}
    with open(log_path, "w", encoding="utf-8") as log_file:
        env = {**inherited, "PORT": str(port), "EMBY_API_PORT": str(port),
               "HOST": "0.0.0.0", **(extra_env or {})}
        proc = subprocess.Popen([sys.executable, script], cwd=ROOT, env=env,
                                stdout=log_file, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    with open(log_path, encoding="utf-8") as fh:
        tail = fh.read()
    check(f"{label}", expect in tail, f"exit={proc.returncode} 包含「{expect}」"
          if expect in tail else f"exit={proc.returncode} 未见提示，日志：{tail[-200:]}")


# ==================== 六、分离部署（EM + EA）====================

def run_split_checks(em_base: str, ea_base: str, username: str, password: str,
                     panel_key: str = "") -> None:
    """分离部署（文档推荐的产线形态）：EM 只跑面板，EA 独占协议面

    这一步关掉 EM 的网关再起一个 EA，验证的是**跨服务配对**：客户端在 EA 上认证，
    用户与凭据都来自 EM 写进共享库的那份数据。
    """
    print("\n=== 六、分离部署（EM 面板 + EA 协议网关）===")

    status, _, _ = request("GET", f"{em_base}/api/health")
    check("EM（分离模式）健康检查可用", status == 200, f"HTTP {status}")

    status, text, _ = request("GET", f"{em_base}/")
    check("EM（分离模式）仍托管门户页面", status == 200 and 'id="app"' in text,
          f"HTTP {status}")

    status, text, _ = request("GET", f"{em_base}/emby/System/Info/Public")
    detail = str((as_json(text) or {}).get("detail") or "")
    check("EM 关闭网关后不再提供协议面，并给出「去连 EA」指引",
          status == 404 and "EA" in detail, f"HTTP {status} {detail[:70]}")

    status, text, _ = request("GET", f"{ea_base}/api/health")
    payload = as_json(text) or {}
    check("EA 健康检查上报节点身份与配对状态",
          status == 200 and payload.get("service") == "ea"
          and payload.get("paired_with_em") is True,
          f"HTTP {status} {json.dumps(payload, ensure_ascii=False)[:100]}")

    status, text, _ = request("GET", f"{ea_base}/")
    check("EA 根路径不托管页面（只给运维一个识别点）",
          status == 200 and "service" in (as_json(text) or {}), f"HTTP {status}")

    # ---- 挂载体检：分离部署下「EM 能碰到的存储」不等于「EA 能碰到的存储」 ----
    status, text, _ = request("GET", f"{ea_base}/api/admin/mounts/health")
    check("EA 挂载体检端点：无面板密钥时被拒", status == 401, f"HTTP {status}")

    # S3：节点密钥（NODE_SHARED_SECRET 或由 SECRET_KEY 派生），不是 SECRET_KEY 原文
    status, text, _ = request("GET", f"{ea_base}/api/admin/mounts/health",
                              extra_headers={"X-Panel-Key": panel_key})
    ea_health = as_json(text) or {}
    check("EA 挂载体检端点：带共享密钥时可用（真跑 resolve + 路径检查）",
          status == 200 and ea_health.get("service") == "ea"
          and isinstance(ea_health.get("mounts"), list),
          f"HTTP {status} mounts={len(ea_health.get('mounts') or [])}")

    status, text, _ = request("POST", f"{em_base}/api/admin/auth/login",
                              body={"username": username, "password": password})
    split_token = str((as_json(text) or {}).get("access_token") or "")
    check("分离部署的 EM 管理员登录成功", status == 200 and bool(split_token), f"HTTP {status}")

    status, text, _ = request("PUT", f"{em_base}/api/admin/emby/servers", token=split_token,
                              body={"mode": "managed_ea", "url": ea_base, "enabled": True})
    body = as_json(text) or {}
    pulled = body.get("mounts_health") or {}
    check("EM 保存 EA 服务入口时拉到了 EA 视角的挂载体检",
          status == 200 and (body.get("probe") or {}).get("ok") is True and pulled.get("ok") is True,
          f"HTTP {status} {json.dumps(pulled, ensure_ascii=False)[:90]}")

    status, text, _ = request("GET", f"{em_base}/api/admin/emby/mounts", token=split_token)
    listing = as_json(text) or {}
    rows = listing.get("mounts") or []
    check("挂载列表同时给出 EM / EA 两个视角的可达性",
          status == 200 and listing.get("playback_node") == "ea" and bool(rows)
          and all("em_reachable" in row and "ea_reachable" in row for row in rows),
          f"HTTP {status} node={listing.get('playback_node')} n={len(rows)}")
    check("EA 的逐条结论真的落到了列表上",
          any(row.get("ea_reachable") is not None for row in rows),
          str([(row.get("name"), row.get("ea_reachable")) for row in rows][:3]))

    for path in ("/emby/System/Info/Public", "/System/Info/Public", "/emby/system/info/public"):
        status, text, _ = request("GET", f"{ea_base}{path}")
        info = as_json(text) or {}
        check(f"EA 协议面：{path} 可识别",
              status == 200 and bool(info.get("ServerName") or info.get("Version")),
              f"HTTP {status}")

    status, _, _ = request("GET", f"{ea_base}/emby/Users/Me")
    check("EA 协议面：受保护端点未带 token 时被拒", status in (401, 403), f"HTTP {status}")

    # 真正跨服务的一步：客户端在 EA 上认证，账号来自 EM 写的共享库
    status, text, _ = request("POST", f"{ea_base}/Users/AuthenticateByName",
                              body={"Username": username, "Pw": password})
    auth = as_json(text) or {}
    ea_token = str(auth.get("AccessToken") or auth.get("accessToken") or "")
    ea_user = (auth.get("User") or {})
    check("EA 用 EM 库里的账号完成客户端认证（跨服务配对）",
          status == 200 and bool(ea_token), f"HTTP {status} {text[:70]}")
    if not ea_token:
        return

    # Name：设了接线用户名就用它，否则回落门户用户名。自检账号不自动生成接线用户名。
    status, text, _ = request("GET", f"{ea_base}/emby/Users/Me", emby_token=ea_token)
    me = as_json(text) or {}
    check("EA 接受该 token 并读出同一用户的身份",
          status == 200 and me.get("Name") == ea_user.get("Name") and bool(me.get("Id")),
          f"HTTP {status} Name={me.get('Name')} 期望={ea_user.get('Name')}")

    status, text, _ = request("GET", f"{ea_base}/emby/Users/Me", token=ea_token)
    check("EA 同时接受 Bearer 形式的同一 token（网页播放器路径）",
          status == 200, f"HTTP {status}")


# ==================== 七、收尾 ====================

def teardown(procs, base: str, user_id: int, keep: bool) -> None:
    print("\n=== 七、收尾 ===")
    from backend.database import SessionLocal
    from backend.emby_server import models as em
    from backend import models

    db = SessionLocal()
    libs = [l for l in db.query(em.Library).all() if (l.name or "").startswith("部署自检库")]
    lib_ids = [l.id for l in libs]
    items = db.query(em.MediaItem).filter(em.MediaItem.library_id.in_(lib_ids)).all() if lib_ids else []
    for row in items:
        db.query(em.MediaStream).filter(em.MediaStream.item_id == row.id).delete(synchronize_session=False)
        db.query(em.UserMediaData).filter(em.UserMediaData.item_id == row.id).delete(synchronize_session=False)
        db.delete(row)
    for lib in libs:
        db.delete(lib)
    mounts = [m for m in db.query(em.StorageMount).all()
              if (m.name or "").startswith("部署自检挂载")]
    for mount in mounts:
        db.delete(mount)
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if user is not None:
        db.delete(user)
    db.commit()
    db.close()
    check("清掉自检创建的媒体库 / 挂载 / 账号", True,
          f"库 {len(libs)}（条目 {len(items)}）/ 挂载 {len(mounts)} / 账号 1")

    if keep:
        warn("按 --keep 保留了服务进程", f"{base}（PID {'、'.join(str(p.pid) for p in procs)}）")
        return
    for proc in procs:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    for proc in procs:
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    check("全部服务已退出（SIGTERM）", all(p.poll() is not None for p in procs),
          f"exit={[p.returncode for p in procs]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="部署自检（真实起服务 + 真实 HTTP）")
    parser.add_argument("--port", type=int, default=8010,
                        help="单进程模式的端口；分离模式用 port+1 与 port+2")
    parser.add_argument("--timeout", type=float, default=60, help="等待就绪的秒数")
    parser.add_argument("--keep", action="store_true", help="自检完不杀进程（调试用）")
    args = parser.parse_args()

    media_dir = tempfile.mkdtemp(prefix="deploy_check_media_")
    user_id, username, password = preflight(media_dir)
    procs: list[subprocess.Popen] = []
    base = f"http://127.0.0.1:{args.port}"
    # 生产部署必须显式给 SECRET_KEY：不设时 EM 用临时随机密钥，EA 也就无法与它配对
    ambient_secret = os.getenv("SECRET_KEY", "").strip()
    secret = ambient_secret or secrets.token_urlsafe(48)
    if not ambient_secret:
        warn("当前环境没有设置 SECRET_KEY",
             "本次自检自造了一个（生产必须显式设置，否则 EA 会拒绝启动、重启后登录态全失效）")
    try:
        # 阶段一：单进程（EM 自带协议面）
        print("\n=== 二、冷启动（单进程）===")
        proc, base, _log = start_service("serve.py", args.port, args.timeout,
                                         label="EM（单进程模式）",
                                         extra_env={"SECRET_KEY": secret})
        procs.append(proc)

        status, text, _ = request("POST", f"{base}/api/admin/auth/login",
                                  body={"username": username, "password": password})
        auth = as_json(text) or {}
        token = str(auth.get("access_token") or "")
        check("管理员登录成功", status == 200 and bool(token), f"HTTP {status}")
        if not token:
            failures.append("管理员登录成功")
        else:
            run_checks(base, username, password, media_dir, token)

        # 阶段二：分离部署（EM 关网关 + 独立 EA），验证跨服务配对
        print("\n=== 五、分离部署冷启动 ===")
        ea_port = args.port + 2
        expect_startup_refused(
            "serve_emby.py", ea_port + 1, label="EA 在没有 SECRET_KEY 时拒绝启动（配对闸门）",
            extra_env={"SECRET_KEY": ""}, expect="SECRET_KEY")
        em_split_proc, em_split_base, _ = start_service(
            "serve.py", args.port + 1, args.timeout,
            label="EM（分离模式，网关关闭）",
            extra_env={"ENABLE_EMBY_GATEWAY": "false", "SECRET_KEY": secret,
                       "EMBY_API_PUBLIC_URL": f"http://127.0.0.1:{ea_port}"})
        procs.append(em_split_proc)
        ea_proc, ea_base, _ = start_service(
            "serve_emby.py", ea_port, args.timeout, label="EA（Emby 协议网关）",
            extra_env={"SECRET_KEY": secret, "EM_PANEL_URL": em_split_base})
        procs.append(ea_proc)
        import hashlib as _hl
        import hmac as _hm
        node_key = _hm.new(secret.encode("utf-8"), b"aetrix-node-auth", _hl.sha256).hexdigest()
        run_split_checks(em_split_base, ea_base, username, password, panel_key=node_key)
    finally:
        teardown(procs, base, user_id, args.keep)
        shutil.rmtree(media_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    if warnings:
        print(f"⚠️  警告 {len(warnings)} 项: {warnings}")
    if failures:
        print(f"❌ 部署自检失败 {len(failures)} 项: {failures}")
        return 1
    print("✅ 部署自检全部通过（真实起服务 + 真实 HTTP）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
