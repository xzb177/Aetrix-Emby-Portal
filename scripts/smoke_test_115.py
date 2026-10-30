"""115 账号与直挂冒烟测试（v2.18.0）

v2.18.0 起 115 只保留「Cookie 配置档 + 直挂客户端」：分享链接转存 / 下载任务那一整套
（解析分享、任务状态机、断点续跑、状态目录）已下线，本测试覆盖保留部分，网络层整体被
替换（`transfer115._http_json`），因此不依赖真实 115 账号。

- 客户端：账号校验、目录列举、取下载地址、目录项判定、鉴权类错误可识别
- Cookie 配置档：多账号、默认账号、媒体库绑定、服务器级环境变量兜底与优先级
- 管理端账号 API：创建 / 更新 / 删除、重名与空 Cookie 被拒、Cookie 不回传明文
- 删除账号后媒体库绑定被解除（不留悬空引用）
- 目录浏览接口（账号可用性实测）与鉴权
- 回归护栏：转存相关接口与任务模型已经不存在
"""
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
os.environ.pop("PAN115_COOKIE", None)  # 环境变量兜底单独测，默认先清空

from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import transfer115 as t115
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


# ==================== 假 115 服务 ====================

class FakePan115:
    """替换 transfer115._http_json 的假后端：只实现保留功能用到的端点"""

    def __init__(self) -> None:
        self.dirs = [
            {"fid": "d1", "cid": "100", "n": "电影", "fc": "0"},
            {"fid": "d2", "cid": "200", "n": "剧集", "fc": "0"},
        ]
        self.files = [
            {"fid": "f1", "n": "Alpha.Target.2024.1080p.NF.WEB-DL.mkv", "s": 100, "pc": "pc1", "fc": "1"},
            {"fid": "f2", "n": "Beta.Strike.2023.2160p.mkv", "s": 200, "pc": "pc2", "fc": "1"},
        ]
        self.download_calls: list[str] = []
        self.calls = 0
        self.auth_broken = False          # Cookie 失效（所有端点）

    def __call__(self, method, url, *, params=None, data=None, cookie="", timeout=None, ua=""):
        self.calls += 1
        if not cookie:
            raise t115.Pan115AuthError("未配置 115 Cookie")
        if self.auth_broken:
            raise t115.Pan115AuthError("Cookie 已失效")
        p = params or {}
        if url.endswith("/files/download"):
            self.download_calls.append(p.get("pickcode"))
            return {"state": True, "file_url": f"https://cdn.example.com/{p.get('pickcode')}"}
        if url.endswith("/files"):
            if p.get("limit") == 1:  # check()：只要账号信息
                return {"state": True, "data": {"uid": "u123", "vip": True}}
            cid = str(p.get("cid") or "0")
            return {"state": True, "data": self.dirs if cid == "0" else self.files}
        return {"state": True, "data": {}}


fake = FakePan115()
t115._http_json = fake  # 整体替换网络层


# ==================== 一、直挂客户端 ====================

print("\n--- 直挂客户端 ---")
info = t115.verify_account("UID=ok; CID=ok")
check("校验有效 Cookie（带回 uid / vip）",
      info.get("ok") is True and info.get("uid") == "u123" and info.get("vip") is True, str(info))

api = t115.Pan115Client("UID=ok; CID=ok")
entries = api.list_dir("0")
check("列目录返回目录项", [e["name"] for e in entries] == ["电影", "剧集"], str(entries))
check("目录项带 cid 与 pickcode 字段",
      all(e["cid"] and "pickcode" in e for e in entries), str(entries[:1]))
files = api.list_dir("100")
check("列目录带文件（直挂解析需要 pickcode）",
      [e["pickcode"] for e in files] == ["pc1", "pc2"], str(files))

url = api.download_url("pc1")
check("取下载地址", url.endswith("pc1") and fake.download_calls == ["pc1"], url)

check("目录项判定：显式 is_dir 优先", t115._is_dir({"is_dir": True, "fc": "1"}) is True)
check("目录项判定：fc==0 视为目录",
      t115._is_dir({"fc": "0"}) is True and t115._is_dir({"fc": "1"}) is False)
check("目录项判定：无标记时按「无大小无 pickcode」兜底",
      t115._is_dir({"n": "某个目录"}) is True and t115._is_dir({"s": 10}) is False)

fake.auth_broken = True
info = t115.verify_account("UID=ok; CID=ok")
check("Cookie 失效时给出 auth_error",
      info.get("ok") is False and info.get("auth_error") is True, str(info))
try:
    t115.Pan115Client("UID=ok; CID=ok").list_dir("0")
    check("失效时抛 Pan115AuthError", False)
except t115.Pan115AuthError:
    check("失效时抛 Pan115AuthError", True)
fake.auth_broken = False

try:
    t115.Pan115Client("   ")
    check("空 Cookie 直接失败", False)
except t115.Pan115AuthError:
    check("空 Cookie 直接失败", True)


# ==================== 二、Cookie 配置档与优先级 ====================

print("\n--- Cookie 配置档与优先级 ---")
db = SessionLocal()

# 清掉历史冒烟测试遗留的配置档（命名带纯数字后缀，含 v2.18.0 前转存任务的 转存号/下载号），
# 否则它们里的 is_default 会盖过本轮断言（优先级依赖全局默认账号）
import re as _re  # noqa: E402
for _row in db.query(em.Pan115Account).all():
    if _re.match(r"^(主号|副号|影库号|停用号|转存号|下载号|空号)\d+$", _row.name or ""):
        db.delete(_row)
db.commit()

acc_a = em.Pan115Account(name=f"主号{suf}", cookie="UID=aaa; CID=aaa", is_default=True)
acc_b = em.Pan115Account(name=f"影库号{suf}", cookie="UID=bbb; CID=bbb")
acc_off = em.Pan115Account(name=f"停用号{suf}", cookie="UID=ccc; CID=ccc", is_enabled=False)
db.add_all([acc_a, acc_b, acc_off])
db.commit()
for row in (acc_a, acc_b, acc_off):
    db.refresh(row)
acc_a_id, acc_b_id, acc_off_id = acc_a.id, acc_b.id, acc_off.id

cookie, source = t115.resolve_cookie(db)
check("无绑定时用默认账号", cookie == "UID=aaa; CID=aaa" and "默认账号" in source, source)

cookie, source = t115.resolve_cookie(db, account_id=acc_b_id)
check("显式账号优先", cookie == "UID=bbb; CID=bbb", source)

cookie, source = t115.resolve_cookie(db, explicit_cookie="UID=form; CID=form")
check("表单 Cookie 优先于默认账号", cookie == "UID=form; CID=form", source)

# 媒体库绑定（直挂按库选账号）
library = em.Library(guid=f"lib{suf}", name=f"媒体库{suf}", collection_type="movies",
                     paths=tempfile.mkdtemp(prefix="pan115_lib_"), account_115_id=acc_b_id)
db.add(library)
db.commit()
db.refresh(library)

cookie, source = t115.resolve_cookie(db, library=library)
check("媒体库绑定账号生效", cookie == "UID=bbb; CID=bbb" and "绑定账号" in source, source)

library.account_115_id = acc_off_id  # 绑到停用账号
db.commit()
try:
    t115.resolve_cookie(db, library=library)
    check("绑定账号停用时响亮报错", False)
except t115.Pan115Error as exc:
    check("绑定账号停用时响亮报错", "已停用" in str(exc), str(exc))
library.account_115_id = acc_b_id
db.commit()

# 显式账号不存在时给出可读错误（而不是静默用别的账号）
try:
    t115.resolve_cookie(db, account_id=99999999)
    check("不存在的配置档报错", False)
except t115.Pan115Error as exc:
    check("不存在的配置档报错", "不存在" in str(exc), str(exc))

# 删除全部配置档后回落到服务器级环境变量
db.query(em.Pan115Account).filter(em.Pan115Account.id.in_([acc_a_id, acc_b_id, acc_off_id])).delete(
    synchronize_session=False
)
db.commit()
cookie, source = t115.resolve_cookie(db)
check("无配置档且无环境变量时返回空", cookie == "" and source == "未配置", source)

os.environ["PAN115_COOKIE"] = "UID=env; CID=env"
cookie, source = t115.resolve_cookie(db)
check("服务器级 PAN115_COOKIE 兜底", cookie == "UID=env; CID=env" and "PAN115_COOKIE" in source, source)
os.environ.pop("PAN115_COOKIE", None)
library_id = library.id  # 先取出主键：db 关闭后再读已过期属性会 DetachedInstanceError
db.close()


# ==================== 三、管理端账号 API ====================

print("\n--- 管理端账号 API ---")
db = SessionLocal()
staff = models.WebUser(username=f"pan115_stf{suf}", password_hash=hash_password("pass12345"), is_staff=True, admin_role="super")
db.add(staff)
db.commit()
db.refresh(staff)
staff_name = staff.username
db.close()

r = client.post("/api/user/auth/login", json={"username": staff_name, "password": "pass12345"})
assert r.status_code == 200, r.text
sh = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = client.post("/api/admin/emby/115/accounts",
                json={"name": f"主号{suf}", "cookie": "UID=xxx; CID=xxx; SEID=yyy",
                      "is_default": True, "remark": "冒烟"}, headers=sh)
check("创建账号配置档", r.status_code == 200, r.text[:120])
acc_id = r.json()["account"]["id"]
check("Cookie 不回传明文",
      "cookie" not in r.json()["account"] and "UID=xxx" not in r.text, r.text[:120])

r = client.post("/api/admin/emby/115/accounts",
                json={"name": f"副号{suf}", "cookie": "UID=yyy; CID=yyy", "is_default": True}, headers=sh)
check("创建第二个账号", r.status_code == 200, r.text[:120])
r2 = client.get("/api/admin/emby/115/accounts", headers=sh)
defaults = [a for a in r2.json()["accounts"] if a["is_default"]]
check("默认账号唯一", len(defaults) == 1 and defaults[0]["id"] == r.json()["account"]["id"],
      str([a["id"] for a in defaults]))

r = client.post("/api/admin/emby/115/accounts",
                json={"name": f"副号{suf}", "cookie": "UID=zzz"}, headers=sh)
check("账号重名被拒绝", r.status_code == 400, r.text[:120])

r = client.post("/api/admin/emby/115/accounts",
                json={"name": f"空号{suf}", "cookie": "   "}, headers=sh)
check("空 Cookie 被拒绝", r.status_code == 400, r.text[:120])

r = client.put(f"/api/admin/emby/115/accounts/{acc_id}", json={"is_enabled": False}, headers=sh)
check("停用账号", r.status_code == 200 and r.json()["account"]["is_enabled"] is False, r.text[:120])

# 校验接口（假后端：有效 Cookie）
r = client.post("/api/admin/emby/115/accounts/{}/verify".format(acc_id), headers=sh)
check("校验已保存账号", r.status_code == 200 and r.json()["result"]["ok"] is True, r.text[:120])
r = client.post("/api/admin/emby/115/verify", json={"cookie": "UID=form; CID=form"}, headers=sh)
check("校验未保存 Cookie", r.status_code == 200 and r.json()["result"]["ok"] is True, r.text[:120])

# 绑定媒体库 → 通过媒体库接口
r = client.put(f"/api/admin/emby/libraries/{library_id}", json={"account_115_id": acc_id}, headers=sh)
check("媒体库绑定 115 账号", r.status_code == 200, r.text[:120])
r = client.get("/api/admin/emby/libraries", headers=sh)
bound = [x for x in r.json()["libraries"] if x["id"] == library_id]
check("媒体库列表返回 account_115_id", bool(bound) and bound[0]["account_115_id"] == acc_id,
      str(bound[:1]))

# 通过 API 删除后，媒体库绑定被解除（不留悬空引用）
r = client.delete(f"/api/admin/emby/115/accounts/{acc_id}", headers=sh)
check("删除账号配置档", r.status_code == 200, r.text[:120])
r = client.get("/api/admin/emby/libraries", headers=sh)
bound = [x for x in r.json()["libraries"] if x["id"] == library_id]
check("删除账号后媒体库绑定被解除", bound and bound[0]["account_115_id"] is None, str(bound[:1]))

# 目录浏览（账号可用性实测；假服务在根目录返回两个目录）
r = client.get("/api/admin/emby/115/browse", params={"cid": "0"}, headers=sh)
body = r.json()
check("路径浏览只返回目录",
      r.status_code == 200 and [e["name"] for e in body["entries"]] == ["电影", "剧集"],
      r.text[:160])
check("路径浏览带 Cookie 来源", bool(body.get("cookie_source")), str(body.get("cookie_source")))

# 鉴权来自 admin_emby_router 的 require_staff：未登录不该碰到任何 115 接口
for path in ("/api/admin/emby/115/accounts", "/api/admin/emby/115/browse"):
    r = client.get(path)
    check(f"未登录被拒绝（{path}）", r.status_code in (401, 403), f"HTTP {r.status_code}")


# ==================== 四、转存相关已下线（回归护栏） ====================

print("\n--- 转存相关已下线 ---")
# 注：面板的 SPA 兜底路由认领所有 GET 路径，因此已删掉的 POST 端点会回 405 而不是 404，
# 两种都说明「这个接口不存在」。
def gone(r) -> bool:
    return r.status_code in (404, 405)


r = client.post("/api/admin/emby/115/parse",
                json={"share_url": "https://115.com/s/abcdefghij#1234"}, headers=sh)
check("分享解析接口已下线", gone(r), f"HTTP {r.status_code}")
r = client.get("/api/admin/emby/115/tasks", headers=sh)
check("任务列表接口已下线", gone(r), f"HTTP {r.status_code}")
r = client.post("/api/admin/emby/115/tasks", json={"share_url": "x"}, headers=sh)
check("建任务接口已下线", gone(r), f"HTTP {r.status_code}")
r = client.post("/api/admin/emby/115/tasks/1/retry", headers=sh)
check("任务重试接口已下线", gone(r), f"HTTP {r.status_code}")

check("任务模型已移除", not hasattr(em, "Pan115Task"))
check("transfer115 里已无任务引擎",
      not any(hasattr(t115, n) for n in
              ("create_task", "run_task", "resume_pending_tasks", "serialize_task",
               "parse_share_link", "PAN115_STATE_DIR", "ACTIVE_STATUSES")))
check("直挂客户端仍在", hasattr(t115, "Pan115Client") and hasattr(t115, "resolve_cookie"))

# 清理 api 建的账号，避免影响下次运行
db = SessionLocal()
db.query(em.Pan115Account).filter(em.Pan115Account.name.like(f"%{suf}")).delete(synchronize_session=False)
db.commit()
db.close()


# ==================== 结果 ====================

print()
if failures:
    print(f"FAILED {len(failures)}:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("ALL PASS")
