"""L65 回归守卫：共享 db 夹具的 teardown 必须清理关键表。

背景（#1287，2026-10-25）：全量套件 5831 过中唯一红项。泄漏链是
- test_media_library_cover_strategy.py 的 db 夹具只在 setup 清表、不在
  teardown 清；
- test_media_library_endpoints.py 的 db 夹具当时 7 张表里没有 SystemConfig；
- 全套件同进程共享最后一次导入绑定的临时库引擎；
于是 strategy 测试留下的 system_configs 行（COVER_STRATEGY_KEY 顺序
["render","svg"]）漏进 endpoints 测试，effective_strategy_order 认保存值
-> render 无图 -> 落到 svg。

本文件只断言“已知泄漏向量”的 teardown 契约，不做全套件扫描：
- 直接驱动两媒体文件 db 夹具的生成器协议（setup -> 写 canary 行 -> teardown），
  断言 teardown 后 canary 行已消失、各夹具清理清单内的表计数归零；
- 不重绑引擎、不新建库、不调用 init_db：夹具会话与复核会话都走当前绑定的
  SessionLocal，红/绿只取决于夹具 teardown 有没有删表；
- 安全护栏：仅当当前绑定是临时 SQLite 文件（mkstemp 产物）且关键表齐全时
  才跑断言，否则跳过——绝不触碰真实库；
- 文件名取 zz：字母序最后，保证套件内最后执行；但本测试不依赖“最后执行”
  才有效（它自己驱动夹具协议），排最后只是让它看到最完整的套件状态。
- 明确边界：本守卫覆盖两媒体夹具；其余 40+ 文件的 db 夹具若将来出现同类
  泄漏，全量红时按“在同分支补 teardown 清理”的口径处理，不弱化本断言。
"""

from __future__ import annotations

import ast
import pathlib
import tempfile

import pytest
from sqlalchemy import inspect

import test_media_library_cover_strategy as strategy_mod
import test_media_library_endpoints as endpoints_mod
from backend import database as dbmod
from backend import models as base_models
from backend.emby_server import models as em

# 两夹具清理清单的并集（已文档化；与夹具 setup/teardown 的删除列表逐字一致）。
KEY_TABLES_STRATEGY = (
    base_models.SystemConfig,
    em.MediaItem,
    em.Library,
)
KEY_TABLES_ENDPOINTS = (
    em.HomeSection,
    em.MediaItemSortProjection,
    em.MediaItemArtworkOverride,
    em.UserMediaData,
    em.MediaItem,
    em.Library,
    base_models.WebUser,
    base_models.SystemConfig,
)

CANARY_KEY = "__fixture_hygiene_canary__"


def _check_temp_test_db(tables):
    """护栏：当前绑定必须是临时 SQLite 测试库且关键表齐全，否则跳过。"""
    engine = dbmod.engine
    url = str(engine.url)
    if not url.startswith("sqlite:///"):
        pytest.skip(f"当前绑定不是 SQLite 测试库：{url}")
    path = pathlib.Path(url[len("sqlite:///"):])
    if path.parent != pathlib.Path(tempfile.gettempdir()):
        pytest.skip(f"当前绑定不是临时目录下的测试库：{path}")
    if not path.name.startswith("tmp"):
        pytest.skip(f"当前绑定不是 mkstemp 产物：{path}")
    inspector = inspect(engine)
    missing = [m.__tablename__ for m in tables if not inspector.has_table(m.__tablename__)]
    if missing:
        pytest.skip(f"关键表未建出（非共享测试库状态）：{missing}")


def _build_strategy_canaries(s):
    s.add(base_models.SystemConfig(key=CANARY_KEY, value="1", description="hygiene"))
    lib = em.Library(guid="__hygiene_lib__", name="__hygiene__")
    s.add(lib)
    s.flush()
    s.add(
        em.MediaItem(
            guid="__hygiene_item__",
            library_id=lib.id,
            item_type="movie",
            name="__hygiene__",
        )
    )
    s.flush()


def _build_endpoints_canaries(s):
    lib = em.Library(guid="__hygiene_lib__", name="__hygiene__")
    s.add(lib)
    s.flush()
    item = em.MediaItem(
        guid="__hygiene_item__",
        library_id=lib.id,
        item_type="movie",
        name="__hygiene__",
    )
    s.add(item)
    user = base_models.WebUser(username="__hygiene_user__", password_hash="test")
    s.add(user)
    s.flush()
    s.add(em.HomeSection(key="__hygiene__", title="__hygiene__"))
    s.add(em.MediaItemSortProjection(item_id=item.id, library_id=lib.id))
    s.add(
        em.MediaItemArtworkOverride(
            item_id=item.id,
            kind="Primary",
            image_url="https://example.invalid/__hygiene__.jpg",
        )
    )
    s.add(em.UserMediaData(user_id=user.id, item_id=item.id))
    s.add(base_models.SystemConfig(key=CANARY_KEY, value="1", description="hygiene"))
    s.flush()


def _drive_fixture_teardown(fixture_fn, tables, build_canaries):
    """驱动夹具生成器：setup -> 写 canary -> teardown，断言表计数归零。"""
    _check_temp_test_db(tables)
    gen = fixture_fn.__wrapped__()
    session = next(gen)
    try:
        build_canaries(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    try:
        next(gen)
    except StopIteration:
        pass
    verify = dbmod.SessionLocal()
    try:
        counts = {m.__name__: verify.query(m).count() for m in tables}
    finally:
        verify.close()
    leftover = {k: v for k, v in counts.items() if v != 0}
    assert not leftover, (
        "db 夹具 teardown 未清理关键表（canary 行残留，跨文件泄漏会重现）："
        + ", ".join(f"{k}={v}" for k, v in leftover.items())
    )


def test_strategy_fixture_teardown_cleans():
    _drive_fixture_teardown(
        strategy_mod.db, KEY_TABLES_STRATEGY, _build_strategy_canaries
    )


def test_endpoints_fixture_teardown_cleans():
    _drive_fixture_teardown(
        endpoints_mod.db, KEY_TABLES_ENDPOINTS, _build_endpoints_canaries
    )


# ---------------------------------------------------------------------------
# #1668 回归守卫：测试文件导入时不许把全局引擎重绑到未建表的空库。
#
# 背景（2026-11-01）：tests/test_whitelist_code_permanent.py 在导入时把全局
# backend.database.engine/SessionLocal 重绑到自己的临时库且不调 init_db，又是
# 套件中字母序最后的重绑者，导致之后所有依赖全局 SessionLocal 的测试在 setup
# 集体报 no such table: web_users，CI 冒烟两轮同形态红。
# 正统写法（其余 70+ 重绑文件皆如此）：导入时重绑后必须紧跟顶层 init_db()。
# 本守卫是纯源码静态检查，不执行被测模块、不动全局引擎、不触碰真实库：
# 对 tests/test_*.py 逐文件做 AST 扫描，只算导入时真正执行的顶层语句
# （递归进 If/Try/With，但不进函数/类体）；凡顶层出现 .engine/.SessionLocal
# 赋值或 configure_session_local 调用、却没有顶层 init_db()/create_all() 调用，
# 即判违规。test_honeypot_code.py 曾是全套件唯一违规者，已随本守卫同单修复。
# ---------------------------------------------------------------------------


def _iter_import_time_nodes(node):
    """产出导入时执行的节点：顶层语句，递归进 If/Try/With，不进函数/类体。"""
    if isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    ):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _iter_import_time_nodes(child)


def _import_time_rebind_without_init(path):
    """该文件是否在导入时重绑全局引擎却未在导入时建表。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    rebind = False
    init = False
    for stmt in tree.body:
        for node in _iter_import_time_nodes(stmt):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr in (
                        "engine",
                        "SessionLocal",
                    ):
                        rebind = True
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    if func.attr == "configure_session_local":
                        rebind = True
                    if func.attr in ("init_db", "create_all"):
                        init = True
                elif isinstance(func, ast.Name) and func.id == "init_db":
                    init = True
    return rebind and not init


def test_no_import_time_engine_rebind_without_init():
    tests_dir = pathlib.Path(__file__).parent
    violators = sorted(
        p.name
        for p in tests_dir.glob("test_*.py")
        if _import_time_rebind_without_init(p)
    )
    assert not violators, (
        "测试文件在导入时重绑全局引擎却未在导入时建表：重绑者若成套件最后"
        "重绑者，后续依赖全局 SessionLocal 的测试会在 setup 集体报缺表"
        "（#1668 同形态回归）——请删导入时副作用，或在导入时补 init_db()："
        + ", ".join(violators)
    )
