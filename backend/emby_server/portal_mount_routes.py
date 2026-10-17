"""管理后台 · 存储挂载端点（从 portal.py 拆出）

挂载 = 媒体库的内容来源，只有四种：**本地硬盘**（/media 开头）、**115 网盘**
（115:/ 开头）、**rclone**（rclone: 开头）、**Google 云盘**（drive: 开头，API 直读）。
远程来源的条目入库为 mount://<id>/<rel>，播放时由 EA 按 Range 代理转发，凭据不下发
（带 drive_file_id 的条目例外，api 层直接 302）。

类型由**路径前缀**决定（``mounts.detect_mount_type``），表单不另给一个类型下拉——
这几种来源的配置本来就只有一条路径加少量可选项，多一个下拉只会让人选出不匹配的组合。

类型元数据与脱敏规则都来自 ``backend/emby_server/mounts.py`` 的 ``MOUNT_TYPES``。

拆分约定：路由仍挂在 portal.py 定义的 ``admin_emby_router`` 上（导入即注册），
调用方（backend/main.py）在 ``include_router`` 之前导入本模块；
``require_staff`` 等共享依赖从 portal 导入，本模块不被 portal 反向引用（无循环导入）。
"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import models, realms
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import mount_health
from backend.emby_server import mount_rclone
from backend.emby_server import mounts as mount_lib
from backend.emby_server.portal import admin_emby_router, require_staff


def _mask_mount_config(config: dict) -> tuple[dict, list[str]]:
    """脱敏：密钥类字段只报是否已配置，不回明文

    密钥集合由挂载类型元数据算出（见 ``mounts.secret_config_keys``），所以新增
    挂载类型时把字段标成 ``secret`` 就够了，不用动这里。
    """
    secret_keys = mount_lib.secret_config_keys()
    public: dict = {}
    secrets: list[str] = []
    for key, value in (config or {}).items():
        if key in secret_keys and value:
            secrets.append(key)
            continue
        public[key] = value
    return public, secrets


def _merge_mount_config(old: dict, new: dict) -> dict:
    """合并配置：密钥类字段留空 = 不修改（避免前端拿不到明文就被抹掉）"""
    secret_keys = mount_lib.secret_config_keys()
    merged = dict(old or {})
    for key, value in (new or {}).items():
        if key in secret_keys and value in ("", None):
            continue
        merged[key] = value
    return merged


def _validate_mount_fields(mount_type: str, path: str, config: dict) -> None:
    """按类型元数据校验：本机路径 + 必填字段

    必填字段来自 ``MOUNT_TYPES[*].fields[*].required``。
    """
    meta = mount_lib.MOUNT_TYPE_MAP.get(mount_type)
    if meta is None:
        raise HTTPException(status_code=400, detail=f"不支持的挂载类型: {mount_type or '(空)'}")
    if meta["needs_path"]:
        if not (path or "").strip():
            raise HTTPException(status_code=400, detail="请填写目录路径")
        if not os.path.isdir(path):
            raise HTTPException(status_code=400, detail=f"目录不存在: {path}")
    for field in mount_lib.required_fields(mount_type):
        if not str(config.get(field["key"]) or "").strip():
            raise HTTPException(status_code=400, detail=f"请填写{field.get('label') or field['key']}")


def _resolve_mount_type(path: str) -> str:
    """类型以**路径前缀**为准；认不出来就报错

    这里**故意不接受调用方给的 ``mount_type`` 兜底**：路径没带前缀时，按调用方说的类型
    存进去，就等于允许存下「一条路径与它的类型对不上」的挂载——扫描时才会以
    「不支持的挂载类型」的形式炸出来，那时候没人想得起来该改哪一栏。
    历史数据不受影响：它只在读取时被解析，那条路走 ``mount.mount_type``。
    """
    detected = mount_lib.detect_mount_type(path)
    if detected:
        return detected
    raise HTTPException(
        status_code=400,
        detail=("路径要带上来源前缀：本机目录（约定 /media）填绝对路径，"
                "115 以 115:/ 开头，rclone 以 rclone: 开头，Google 云盘以 drive: 开头"),
    )


def _fix_rclone_root(mount_type: str, path: str, config: dict) -> str:
    """rclone 的 remote 名必须带冒号（漏了会被 rclone 当成本机目录）

    只在**首段没有冒号**时介入，正常写法（``rclone:gdrive/Movies``）不受影响：

    - 首段就是已配置的 remote → 直接补上冒号（``rclone:paul_emby/电影`` →
      ``rclone:paul_emby:电影``）；
    - 查得到 remote 列表却没有这个名字 → 400，给出正确写法与可用的 remote；
    - 查不到（还没粘 rclone.conf / 这台机器没 rclone）→ 按文本规则兜底。

    不拦明确的本地写法（``/media``）：rclone 本来就支持本机路径，只是本机目录
    应该用「本地硬盘」类型，提示里也这么写。
    """
    if mount_type != mount_rclone.MOUNT_RCLONE:
        return path
    target = path[len(mount_rclone.MOUNT_RCLONE) + 1:].strip()
    if not target or mount_rclone.fs_remote_name(target) or mount_rclone.looks_like_local_path(target):
        return path
    try:
        remotes = mount_rclone.conf_remote_names(
            mount_rclone.read_rclone_conf(str(config.get("rclone_config") or "")))
    except mount_lib.MountError:
        remotes = []
    fixed = mount_rclone.normalize_fs(target, remotes)
    if fixed != target:
        return f"{mount_rclone.MOUNT_RCLONE}:{fixed}"
    hint = mount_rclone.fs_missing_colon_hint(target, remotes)
    if hint:
        raise HTTPException(status_code=400, detail=hint)
    return path


def _load_mount(db: Session, mount_id: int) -> em.StorageMount | None:
    """取一条挂载（同步；只许从工作线程调用——路由都是 async，不能占事件循环）"""
    return db.query(em.StorageMount).filter(em.StorageMount.id == mount_id).first()


def _ea_server_names(db: Session) -> dict:
    """``{server_id: name}``（只列 EA）——列表里把 server_id 显示成人看得懂的名字"""
    rows = db.query(models.RemoteServer).filter(models.RemoteServer.kind == "ea").all()
    return {row.id: row.name for row in rows}


def _legacy_note(mount_type: str) -> str:
    """v2.42.12 删掉的类型在库里可能还有残留行：**不自动删**，只说清怎么改

    留着它们是有意的：自动删 = 删别人机器上的数据且无法撤销。所以走「明确报错 +
    告诉管理员手动改」这条路，而不是让扫描一碰到就报「不支持的挂载类型」然后让人
    猜到底要改哪一栏。
    """
    if mount_type in mount_lib.MOUNT_TYPE_MAP:
        return ""
    return (f"「{mount_type or '(空)'}」是 v2.42.12 已下线的挂载类型，数据仍然保留，"
            "但不再可用。请在服务器管理里把它改成下面几种之一："
            "本地硬盘（绝对路径）/ 115 网盘（115:/ 开头）/ rclone（rclone: 开头）/ "
            "Google 云盘（drive: 开头，API 直读）。"
            "除 Google 云盘外，这些后端 rclone 都支持，粘一份 rclone.conf 即可接上。")


def _serialize_mount(db: Session, mount: em.StorageMount, ea_map: dict | None = None,
                     server_names: dict | None = None) -> dict:
    config, secrets = _mask_mount_config(mount_lib.parse_config(mount))
    meta = mount_lib.MOUNT_TYPE_MAP.get(mount.mount_type, {})
    kind = meta.get("kind", "local")
    path = (mount.path or "").strip()
    # EM 视角 = 后台「测试连接」的结果（跑在 EM 进程里）
    em_result = {
        "ok": mount.last_check_ok,
        "message": mount.last_check_message or "",
        "checked_at": mount.last_checked_at.isoformat() if mount.last_checked_at else None,
    }
    # EA 视角 = 保存 EA 服务入口 / 手动刷新时拉到的那份快照
    if ea_map is None:
        ea_map = mount_health.ea_mount_map(db)
    ea_item = ea_map.get(mount.id) or {}
    return {
        "id": mount.id,
        "name": mount.name,
        "realm_id": mount.realm_id,
        # 由哪台 EA 去读（每台 EA 一份 rclone.conf 就靠它）；null = 老数据，回退到本服 EA
        "server_id": mount.server_id,
        "server_name": server_names.get(mount.server_id) if server_names else None,
        "mount_type": mount.mount_type,
        "mount_type_label": mount_lib.MOUNT_TYPE_LABELS.get(mount.mount_type, mount.mount_type),
        # 已下线类型的残留行：前端直接报红并显示怎么改（数据本身不删）
        "legacy_note": _legacy_note(mount.mount_type),
        "kind": kind,
        "path": path,
        "config": config,
        "secret_keys": secrets,
        "is_enabled": bool(mount.is_enabled),
        "remark": mount.remark or "",
        "last_checked_at": mount.last_checked_at.isoformat() if mount.last_checked_at else None,
        "last_check_ok": mount.last_check_ok,
        "last_check_message": mount.last_check_message,
        # local / strm 的路径是本机相对资源（远程类型为 None）
        "path_exists": os.path.isdir(path) if kind == "local" and path else None,
        # 两个播放节点各自能不能用它（EA 缺失快照时为 None，表示「未体检」）
        "em_reachable": em_result["ok"],
        "em_message": em_result["message"],
        "em_checked_at": em_result["checked_at"],
        "ea_reachable": ea_item.get("ok"),
        "ea_message": ea_item.get("message") or "",
        "library_ids": [
            lib.id for lib in db.query(em.Library).all()
            if mount.id in mount_lib.parse_mount_ids(lib)
        ],
    }


class MountCreate(BaseModel):
    name: str
    mount_type: str
    path: str = ""
    config: dict = {}
    is_enabled: bool = True
    remark: str = ""
    # 归属哪个服（留空 = 当前服）：存储是主机相对资源，跟着服走
    realm_id: int | None = None
    # 由哪台 EA 去读（留空 = 本服已激活的 EA，再不济用共享的那份 rclone.conf）
    server_id: int | None = None


class MountUpdate(BaseModel):
    name: str | None = None
    path: str | None = None
    config: dict | None = None
    is_enabled: bool | None = None
    remark: str | None = None
    realm_id: int | None = None
    server_id: int | None = None


class MountTestRequest(BaseModel):
    """测试尚未保存的配置（先测再存）"""
    mount_type: str
    path: str = ""
    config: dict = {}


class RcloneConfSave(BaseModel):
    """用户粘贴的 rclone.conf（标准 INI 文本，整份替换）"""
    conf: str = ""


@admin_emby_router.get("/mounts")
def list_mounts(staff: models.WebUser = Depends(require_staff), db: Session = Depends(get_db),
                     realm_id: int | None = None):
    """存储挂载清单（按服；realm_id=0 表示全部服）"""
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    query = realms.scope_inclusive(db.query(em.StorageMount), em.StorageMount.realm_id, scope_id)
    mounts = query.order_by(em.StorageMount.id).all()
    ea_map = mount_health.ea_mount_map(db, scope_id)
    snapshot = mount_health.read_ea_health(db, scope_id)
    realm_names = {r.id: r.name for r in realms.list_realms(db)}
    server_names = _ea_server_names(db)
    return {
        "mounts": [_serialize_mount(db, m, ea_map, server_names) for m in mounts],
        # 类型元数据（标签 / 说明 / 需要哪些字段）由后端下发，前端不再自己维护一份
        "mount_types": [dict(t) for t in mount_lib.MOUNT_TYPES],
        # 当前谁在出流：EA 分离部署 / 外部 Emby / 面板自己。
        # 「被媒体库引用却 EA 不可达」只有 EA 才是阻断性问题，前端据此决定要不要报红。
        "playback_node": mount_health.playback_node(db, scope_id),
        "realm_id": scope_id,
        "realm_names": realm_names,
        "ea_health": {
            "ok": bool(snapshot.get("ok")),
            "checked_at": snapshot.get("checked_at"),
            "error": snapshot.get("error") or "",
        },
    }


@admin_emby_router.post("/mounts/health")
async def check_all_mounts(staff: models.WebUser = Depends(require_staff),
                          db: Session = Depends(get_db)):
    """一键体检（EM 视角）：逐条跑与「测试连接」相同的探测并落库

    只解决「这台面板自己能不能碰到存储」；EA 那一侧要看 ``/mounts`` 响应里的
    ``ea_reachable``（由 EA 服务入口拉取）。"""
    def probe_all() -> dict:
        """逐条探测（同步；下放线程池——每条都要发一次网络请求）"""
        return mount_health.mounts_health(db, "panel", realms.active_realm_id(db))

    health = await run_in_threadpool(probe_all)

    def record() -> None:
        """把每条的最近结果写回挂载行（同步；下放线程池）"""
        checked_at = datetime.now()
        for item in health.get("mounts", []):
            if item.get("ok") is None:
                continue  # 停用的挂载不写测试结果
            mount = _load_mount(db, item["id"])
            if not mount:
                continue
            mount.last_checked_at = checked_at
            mount.last_check_ok = bool(item.get("ok"))
            mount.last_check_message = str(item.get("message") or "")[:300]
        db.commit()

    await run_in_threadpool(record)
    return health


@admin_emby_router.get("/mounts/rclone/conf")
async def get_rclone_conf(staff: models.WebUser = Depends(require_staff)):
    """读取 rclone.conf（只报 remote 名，不回明文）

    rclone.conf 里全是 token / secret，回明文就等于把它写进浏览器历史与前端日志。
    用户要改内容直接重新粘贴一次覆盖（**本来就是整份替换**，没有增量编辑）。
    """
    text = await run_in_threadpool(mount_rclone.read_rclone_conf, "")
    try:
        remotes = mount_rclone.conf_remote_names(text)
    except mount_lib.MountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "path": mount_rclone.CONF_PATH,
        "configured": bool(remotes),
        "remotes": remotes,
        "total": len(remotes),
    }


@admin_emby_router.post("/mounts/rclone/conf")
async def save_rclone_conf(req: RcloneConfSave,
                           staff: models.WebUser = Depends(require_staff)):
    """保存用户粘贴的 rclone.conf（整份替换，写到 data/rclone/rclone.conf）"""
    try:
        path = await run_in_threadpool(mount_rclone.write_rclone_conf, req.conf)
    except mount_lib.MountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    remotes = mount_rclone.conf_remote_names(req.conf)
    return {"success": True, "path": path, "remotes": remotes, "total": len(remotes)}


def _resolve_mount_server(db: Session, server_id: int | None, realm_id: int | None) -> int | None:
    """挂载归哪台 EA 读：显式指定 > 本服已激活的 EA > None（用共享的那份 rclone.conf）

    指定了就必须是**存在且是 EA** 的服务器：挂到一台不存在的机器上，表现是
    「配都配对了，就是扫不出东西」——这种错不该等到扫描时才暴露。
    """
    if server_id:
        server = db.query(models.RemoteServer).filter(
            models.RemoteServer.id == server_id).first()
        if not server:
            raise HTTPException(status_code=400, detail=f"服务器不存在: #{server_id}")
        if server.kind != "ea":
            raise HTTPException(
                status_code=400,
                detail=f"「{server.name}」不是 EA（{server.kind}），只有 EA 能读存储挂载")
        return server.id
    from backend import servers as server_registry

    active = server_registry.active_server(db, "ea", realm_id)
    return active.id if active else None


@admin_emby_router.post("/mounts")
def create_mount(req: MountCreate, staff: models.WebUser = Depends(require_staff),
                       db: Session = Depends(get_db)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写挂载名称")
    if db.query(em.StorageMount).filter(em.StorageMount.name == name).first():
        raise HTTPException(status_code=400, detail=f"挂载名称已存在: {name}")
    config = _merge_mount_config({}, req.config)
    path = (req.path or "").strip()
    mount_type = _resolve_mount_type(path)
    path = _fix_rclone_root(mount_type, path, config)
    _validate_mount_fields(mount_type, path, config)
    realm_id = req.realm_id or realms.active_realm_id(db)
    if not realms.get_realm(db, realm_id):
        raise HTTPException(status_code=400, detail=f"服不存在: #{realm_id}")
    mount = em.StorageMount(
        name=name, mount_type=mount_type, path=path,
        config=mount_lib.dump_config(config), is_enabled=req.is_enabled,
        remark=(req.remark or "")[:300], realm_id=realm_id,
        server_id=_resolve_mount_server(db, req.server_id, realm_id),
    )
    db.add(mount)
    db.commit()
    db.refresh(mount)
    return {"success": True, "mount": _serialize_mount(db, mount, None, _ea_server_names(db))}


@admin_emby_router.put("/mounts/{mount_id}")
def update_mount(mount_id: int, req: MountUpdate,
                       staff: models.WebUser = Depends(require_staff),
                       db: Session = Depends(get_db)):
    mount = db.query(em.StorageMount).filter(em.StorageMount.id == mount_id).first()
    if not mount:
        raise HTTPException(status_code=404, detail="挂载不存在")
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="请填写挂载名称")
        exists = db.query(em.StorageMount).filter(
            em.StorageMount.name == name, em.StorageMount.id != mount_id,
        ).first()
        if exists:
            raise HTTPException(status_code=400, detail=f"挂载名称已存在: {name}")
        mount.name = name
    config = _merge_mount_config(mount_lib.parse_config(mount), req.config or {})
    path = (req.path if req.path is not None else mount.path or "").strip()
    # 类型跟着路径走：改路径就等于换来源（115:/ ↔ rclone: ↔ /media），这是允许的。
    # 但已入库条目的路径是 mount://<id>/<rel>，换来源后它们会被**新**提供者重新解释——
    # 所以下面把这个事实回给前端，让它提醒「重新扫描」，而不是悄悄换掉一堆条目的含义。
    mount_type = _resolve_mount_type(path)
    type_changed = mount_type != mount.mount_type
    path = _fix_rclone_root(mount_type, path, config)
    _validate_mount_fields(mount_type, path, config)
    mount.mount_type = mount_type
    mount.path = path
    mount.config = mount_lib.dump_config(config)
    if req.is_enabled is not None:
        mount.is_enabled = req.is_enabled
    if req.remark is not None:
        mount.remark = req.remark[:300]
    if "server_id" in req.model_fields_set and req.server_id is not None:
        mount.server_id = _resolve_mount_server(db, req.server_id, mount.realm_id)
    if "realm_id" in req.model_fields_set and req.realm_id is not None:
        if not realms.get_realm(db, req.realm_id):
            raise HTTPException(status_code=400, detail=f"服不存在: #{req.realm_id}")
        mount.realm_id = req.realm_id
        # 引用本挂载的媒体库跟着走，否则库会跨服引用存储
        for lib in db.query(em.Library).filter(
                em.Library.mount_ids.ilike(f"%{mount.id}%")).all():
            if str(mount.id) in [x.strip() for x in (lib.mount_ids or "").split(",") if x.strip()]:
                lib.realm_id = req.realm_id
    db.commit()
    db.refresh(mount)
    # 路径/配置变更后需要重新扫描才生效（扫描任务使用固定配置快照）
    return {"success": True,
            "mount": _serialize_mount(db, mount, None, _ea_server_names(db)),
            "rescan_required": True, "mount_type_changed": type_changed}


@admin_emby_router.delete("/mounts/{mount_id}")
def delete_mount(mount_id: int, staff: models.WebUser = Depends(require_staff),
                       db: Session = Depends(get_db)):
    mount = db.query(em.StorageMount).filter(em.StorageMount.id == mount_id).first()
    if not mount:
        raise HTTPException(status_code=404, detail="挂载不存在")
    # 解除媒体库绑定，不留悬空引用（条目会在下一次扫描时清理）
    unbound = 0
    for lib in db.query(em.Library).all():
        ids = mount_lib.parse_mount_ids(lib)
        if mount_id in ids:
            lib.mount_ids = ",".join(str(i) for i in ids if i != mount_id)
            unbound += 1
    db.delete(mount)
    db.commit()
    return {"success": True, "unbound_libraries": unbound}


@admin_emby_router.post("/mounts/{mount_id}/test")
async def test_saved_mount(mount_id: int, staff: models.WebUser = Depends(require_staff),
                           db: Session = Depends(get_db)):
    mount = await run_in_threadpool(_load_mount, db, mount_id)
    if not mount:
        raise HTTPException(status_code=404, detail="挂载不存在")
    result = await run_in_threadpool(mount_lib.test_mount, mount, db)

    def record() -> dict:
        """写回测试结果并序列化（同步；下放线程池）

        测试结果只作展示，不影响扫描（扫描自己会报错）。
        """
        mount.last_checked_at = datetime.now()
        mount.last_check_ok = bool(result.get("ok"))
        mount.last_check_message = str(result.get("message") or "")[:300]
        db.commit()
        db.refresh(mount)
        return _serialize_mount(db, mount)

    return {"success": bool(result.get("ok")), "result": result,
            "mount": await run_in_threadpool(record)}


@admin_emby_router.post("/mounts/test")
async def test_unsaved_mount(req: MountTestRequest,
                             staff: models.WebUser = Depends(require_staff)):
    """测试表单里还没保存的配置（Cookie / 令牌不用先存再测）"""
    probe = em.StorageMount(
        name="(未保存)", mount_type=req.mount_type, path=(req.path or "").strip(),
        config=mount_lib.dump_config(_merge_mount_config({}, req.config)), is_enabled=True,
    )
    result = await run_in_threadpool(mount_lib.test_mount, probe, None)
    return {"success": bool(result.get("ok")), "result": result}


@admin_emby_router.get("/mounts/{mount_id}/browse")
async def browse_mount(mount_id: int, rel: str = "/",
                       staff: models.WebUser = Depends(require_staff),
                       db: Session = Depends(get_db)):
    """浏览挂载目录（给「目标目录 / 目录 ID」选择器用）"""
    mount = await run_in_threadpool(_load_mount, db, mount_id)
    if not mount:
        raise HTTPException(status_code=404, detail="挂载不存在")
    try:
        # 建 provider 要读挂载配置 / 账号档（同步 DB）——和列目录一起放在工作线程
        provider = await run_in_threadpool(mount_lib.build_provider, mount, db)
        entries = await run_in_threadpool(provider.list_dir, rel)
    except mount_lib.MountAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except mount_lib.MountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "rel": rel or "/",
        "entries": [
            {"name": e.name, "rel": e.rel, "is_dir": e.is_dir, "size": e.size,
             "entry_id": e.entry_id}
            for e in entries
        ],
        "total": len(entries),
    }


# ==================== 挂载路径选择器（媒体库表单用） ====================

#: 本机目录浏览单层最多回多少个子目录（再多前端也渲染不动）
_LOCAL_BROWSE_MAX = 1000

mount_picker_router = APIRouter(
    prefix="/api/admin/mounts",
    tags=["挂载路径选择"],
    dependencies=[Depends(require_staff)],
)


def _normalize_browse_path(raw: str | None) -> str:
    """规范化浏览路径，防 `..` 跳出挂载根。

    返回以 `/` 开头的干净路径；非法时抛 ValueError。
    """
    text = (raw or "").strip().replace("\\", "/")
    # 逐段处理，遇到 `..` 直接拒绝（不静默消化，避免语义混淆）
    parts: list[str] = []
    for seg in text.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("路径不允许包含 ..")
        parts.append(seg)
    return "/" + "/".join(parts)


@mount_picker_router.get("/local-dirs")
async def browse_local_dirs(
    path: str = Query(default="/", description="服务器上的目录（绝对路径）"),
    staff: models.WebUser = Depends(require_staff),
):
    """浏览**服务器本机**目录（媒体库「本地文件」来源选路径用）

    与 :func:`browse_mount_dirs` 返回**完全一样的结构**，所以前端一个浏览组件
    同时能吃本地目录与挂载目录（新增路径弹窗因此不用写两套列表 UI）。

    - 只列目录，不列文件；按名称排序；
    - 单层最多回 1000 个目录（再多页面也渲染不动，截断时带 truncated 标记）；
    - 只读，不写库、不触发扫描。
    """
    clean = (path or "/").strip() or "/"
    if not clean.startswith("/"):
        raise HTTPException(status_code=400, detail="本地目录必须是绝对路径（以 / 开头）")
    listing = await run_in_threadpool(_list_local_dirs, clean)
    if isinstance(listing, tuple):
        dirs, truncated = listing
    else:
        # None = 不存在；字符串 = 存在但不是目录。两种都直接告诉管理员改哪里
        raise HTTPException(
            status_code=404 if listing is None else 400,
            detail=(f"目录不存在或不可读：{clean}" if listing is None
                    else f"不是目录：{clean}"),
        )
    dirs, truncated = listing
    segments = [s for s in clean.split("/") if s]
    acc: list[str] = []
    crumbs: list[dict] = []
    for seg in segments:
        acc.append(seg)
        crumbs.append({"name": seg, "path": "/" + "/".join(acc)})
    return {
        "path": clean,
        # 根目录没有上一级（返回 None 而不是 "/"：弹窗靠它决定要不要画“返回上一级”）
        "parent": "/" + "/".join(segments[:-1]) if len(segments) > 1 else None,
        "crumbs": crumbs,
        "dirs": dirs,
        "total": len(dirs),
        "truncated": truncated,
    }


def _list_local_dirs(path: str):
    """列一层子目录（阻塞 IO，在线程池里跑）

    返回 ``([{name, path}], truncated)``；路径不存在返回 ``None``，存在但不是目录
    返回字符串（说明原因）。用 ``os.scandir`` 而不是 ``os.listdir``：能不能进得去
    靠 ``is_dir(follow_symlinks=False)`` 一次拿到，不对每个条目都触发一次 stat。
    """
    if not os.path.isdir(path):
        if os.path.exists(path):
            return "不是目录"
        return None
    names: list[str] = []
    truncated = False
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue  # 断链 / 无权限的条目直接略过，不让整个列表挂掉
                names.append(entry.name)
                if len(names) >= _LOCAL_BROWSE_MAX:
                    truncated = True
                    break
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"没有权限读取该目录：{path}") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"读取目录失败：{exc}") from exc
    dirs = sorted(
        ({"name": name, "path": (path.rstrip("/") + "/" + name) if path != "/" else "/" + name}
         for name in names),
        key=lambda d: d["name"].lower(),
    )
    return dirs, truncated


@mount_picker_router.get("/{mount_id}/browse")
async def browse_mount_dirs(
    mount_id: int,
    path: str | None = Query(default=None, description="挂载内的子路径，如 /MoviePilot/剧集"),
    staff: models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """浏览挂载下的子目录（媒体库「路径」字段的选择器用）。

    - 只返回目录，按名称排序；不返回文件。
    - 只读：不触发扫描、不写库。
    - path 必须位于挂载根内，`..` 直接 400。
    """
    try:
        clean_path = _normalize_browse_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    mount = await run_in_threadpool(_load_mount, db, mount_id)
    if not mount:
        raise HTTPException(status_code=404, detail="挂载不存在")
    try:
        provider = await run_in_threadpool(mount_lib.build_provider, mount, db)
        entries = await run_in_threadpool(provider.list_dir, clean_path)
    except mount_lib.MountAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except mount_lib.MountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    dirs = sorted(
        ({"name": e.name, "path": e.rel} for e in entries if e.is_dir),
        key=lambda d: d["name"].lower(),
    )
    # 面包屑：/a/b → [{name: a, path: /a}, {name: b, path: /a/b}]
    crumbs: list[dict] = []
    acc: list[str] = []
    for seg in clean_path.strip("/").split("/"):
        if not seg:
            continue
        acc.append(seg)
        crumbs.append({"name": seg, "path": "/" + "/".join(acc)})
    return {
        "mount_id": mount_id,
        "path": clean_path,
        "parent": "/" + "/".join(clean_path.strip("/").split("/")[:-1]) if clean_path != "/" else None,
        "crumbs": crumbs,
        "dirs": dirs,
        "total": len(dirs),
    }
