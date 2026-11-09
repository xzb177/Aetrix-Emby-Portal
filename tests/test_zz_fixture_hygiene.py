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
import contextlib
import importlib
import os
import pathlib
import tempfile
import threading

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

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


@contextlib.contextmanager
def _provisioned_temp_db():
    """给守卫自备一个临时库并把全局绑定切过去，退出原样还原。

    测试卫生根治（导入期换绑改模块夹具）后，全套件不再共享「最后一个
    导入文件绑定的临时库」，本守卫不能再蹭环境绑定——在 CI 里那时当前
    绑定是套件库 ci-smoke.db，_check_temp_test_db 会把断言整段跳过。
    自备临时库让 teardown 契约断言在任何跑法下都真正执行。
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    saved_engine = dbmod.engine
    saved_factory = dbmod.SessionLocal._factory
    dbmod.engine = engine
    dbmod.configure_session_local(sessionmaker(bind=engine))
    try:
        dbmod.init_db()
        yield engine
    finally:
        dbmod.engine = saved_engine
        dbmod.configure_session_local(saved_factory)
        engine.dispose()
        try:
            os.unlink(path)
        except OSError:
            pass


def _drive_fixture_teardown(fixture_fn, tables, build_canaries):
    """驱动夹具生成器：setup -> 写 canary -> teardown，断言表计数归零。"""
    with _provisioned_temp_db():
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


# ---------------------------------------------------------------------------
# 测试卫生根治守卫（2026-11-09）：导入期全局状态污染四类一并钉死。
#
# 背景：全量/组合跑时红、单跑全绿的老毛病有四个同族源头——
# 1) 约 77 个测试文件在导入期把全局 backend.database.engine 与
#    SessionLocal 代理目标换绑到自己的临时库且不还原，collection 后
#    全局绑定停在字母序最后的重绑者身上；后来文件的夹具/用例用全局
#    SessionLocal 时读错库、撞 no such table（#1668 只堵了「重绑不
#    建表」一种形态）。正统写法：换绑只许在模块夹具内做、结束还原。
# 2) 假 Thread 类缺 is_alive()：被打补丁窗口内若有产品代码把假线程
#    存进常驻槽位（scan_queue._SCHEDULER 等），后续 .is_alive() 复查
#    撞 AttributeError，时红时不红。
# 3) 真 Timer/后台线程在导入期悬挂，或假 Timer 类缺 start/cancel
#    接口面（ephemeral 的顶替/取消路径会调 cancel）。
# 4) 常驻后台线程槽位里存着假线程对象（同 2 的落点面）。
# 本节断言全部确定性：类别 1/2/4 是运行时同一性检查，类别 2/3 另配
# 纯 AST 静态检查（不执行被测模块）。
# ---------------------------------------------------------------------------


def _import_all_test_modules():
    """把 tests/ 下全部测试模块导入本进程（全量跑时 collection 已导入，
    这里是 no-op；单跑本文件时靠它复现 collection 后的全局状态）。"""
    tests_dir = pathlib.Path(__file__).parent
    failures = []
    for p in sorted(tests_dir.glob("test_*.py")):
        if p.stem == pathlib.Path(__file__).stem:
            continue
        try:
            importlib.import_module(p.stem)
        except Exception as e:  # noqa: BLE001 - 汇总后统一断言
            failures.append(f"{p.name}: {type(e).__name__}: {e}")
    assert not failures, "测试模块导入失败：" + "; ".join(failures)


def test_global_db_binding_identity_intact():
    """类别 1：导入全部测试模块后，全局引擎与 SessionLocal 代理目标
    必须仍是 database 模块的默认绑定（身份同一），不许停在某个测试
    文件的临时库上。"""
    _import_all_test_modules()
    default_factory = dbmod._real_session_local
    default_engine = default_factory.kw.get("bind")
    assert dbmod.engine is default_engine, (
        "全局 backend.database.engine 已被测试文件导入期换绑且未还原"
        f"（当前 {dbmod.engine!r}，应为默认 {default_engine!r}）——"
        "换绑只许在模块夹具内做并在 teardown 还原"
    )
    assert dbmod.SessionLocal._factory is default_factory, (
        "全局 SessionLocal 代理目标已被测试文件导入期换绑且未还原"
        f"（当前 {dbmod.SessionLocal._factory!r}）——"
        "换绑只许在模块夹具内做并在 teardown 还原"
    )


def test_threading_thread_class_not_replaced():
    """类别 2（运行时）：threading.Thread 必须仍是解释器原装类，
    任何打补丁未还原都会现形。锚的取法：主线程的类是 threading
    模块定义的 _MainThread 子类，它的基类在模块定义时就绑死为
    原装 Thread——事后给 threading.Thread 属性换绑动不了这条链，
    所以拿它当锚；直接 type(main_thread()) 当锚则恒不等（子类）。"""
    _import_all_test_modules()
    real_thread = type(threading.main_thread()).__bases__[0]
    assert threading.Thread is real_thread, (
        f"threading.Thread 已被全局替换且未还原：{threading.Thread!r}"
    )



def _classes_by_name(path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }


def _methods_with_inheritance(classes, node, _seen=None):
    """类自身方法 ∪ 同文件内基类的方法（递归一层文件内继承）。"""
    methods = {
        n.name
        for n in node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    _seen = _seen or set()
    for base in node.bases:
        base_name = None
        if isinstance(base, ast.Name):
            base_name = base.id
        elif isinstance(base, ast.Attribute):
            base_name = base.attr
        if base_name in classes and base_name not in _seen:
            _seen.add(base_name)
            methods |= _methods_with_inheritance(
                classes, classes[base_name], _seen
            )
    return methods


def _subclasses_real_thread(node):
    for base in node.bases:
        if isinstance(base, ast.Attribute) and base.attr == "Thread":
            return True
        if isinstance(base, ast.Name) and base.id == "Thread":
            return True
    return False


def test_fake_thread_classes_have_is_alive():
    """类别 2（静态）：测试里自造的 Thread 替身类（不继承真 Thread、
    非测试用例类）必须有 is_alive()。假线程一旦被产品代码存进常驻
    槽位，后续 .is_alive() 复查撞 AttributeError 的老坑靠这条钉死。"""
    tests_dir = pathlib.Path(__file__).parent
    bad = []
    for p in sorted(tests_dir.glob("test_*.py")):
        classes = _classes_by_name(p)
        for name, node in classes.items():
            if "Thread" not in name:
                continue
            if name.startswith("Test") or name.endswith("Test"):
                continue
            if _subclasses_real_thread(node):
                continue
            if "is_alive" not in _methods_with_inheritance(classes, node):
                bad.append(f"{p.name}::{name}")
    assert not bad, (
        "假 Thread 类缺 is_alive()（被存进常驻线程槽位后会让后续用例"
        "撞 AttributeError）：" + ", ".join(bad)
    )


def test_fake_timer_classes_have_start_cancel():
    """类别 3（静态）：自造的假 Timer 类必须同时有 start 与 cancel——
    ephemeral.schedule_delete 的顶替路径与 cancel_delete 都会调
    .cancel()，缺了就是下一次同键重调度时的 AttributeError。"""
    tests_dir = pathlib.Path(__file__).parent
    bad = []
    for p in sorted(tests_dir.glob("test_*.py")):
        classes = _classes_by_name(p)
        for name, node in classes.items():
            if "Timer" not in name or "Fake" not in name:
                continue
            methods = _methods_with_inheritance(classes, node)
            if not {"start", "cancel"} <= methods:
                bad.append(f"{p.name}::{name}（有 {sorted(methods)}）")
    assert not bad, "假 Timer 类缺 start/cancel 接口面：" + ", ".join(bad)


def test_background_thread_slots_hold_real_threads():
    """类别 4：常驻后台线程槽位里只许是真 threading.Thread 或 None；
    混进假线程对象，后续 .is_alive()/.join() 复查就是定时炸弹。"""
    from backend.emby_server import scan_queue, scan_queue_redis

    # 锚同类别 2 运行时：_MainThread 的基类才是原装 Thread 本身，
    # isinstance 拿子类当锚会把真线程误判成假线程。
    real_thread = type(threading.main_thread()).__bases__[0]
    slots = (
        ("scan_queue._SCHEDULER", scan_queue, "_SCHEDULER"),
        ("scan_queue._FLUSHER", scan_queue, "_FLUSHER"),
        ("scan_queue_redis._consumer_thread", scan_queue_redis,
         "_consumer_thread"),
    )
    bad = []
    for label, module, attr in slots:
        if not hasattr(module, attr):
            bad.append(f"{label}（槽位不存在）")
            continue
        obj = getattr(module, attr)
        if obj is not None and not isinstance(obj, real_thread):
            bad.append(f"{label}（{type(obj).__name__}）")
    assert not bad, "常驻线程槽位里存了假线程：" + ", ".join(bad)



# 导入全部测试模块后允许新出现的线程名白名单（类别 3）。现行主线实测
# 为 0 个：任何模块在导入期新起常驻线程都必须先过这条名单——把名字加
# 进来时要在 PR 里说明这个线程为什么必须在导入期存在。
_IMPORT_THREAD_WHITELIST = frozenset()


def test_no_new_threads_after_import_all():
    """类别 3（运行时）：导入全部测试模块不得新起常驻线程/Timer。
    导入期挂上的真 Timer 会在后续用例的窗口里到期乱入（2026-11-02
    slots 偶红同族），所以这条在导入动作前后直接对照线程表。"""
    before = {t.ident for t in threading.enumerate()}
    _import_all_test_modules()
    offenders = [
        t.name
        for t in threading.enumerate()
        if t.ident not in before
        and t.is_alive()
        and t.name not in _IMPORT_THREAD_WHITELIST
    ]
    assert not offenders, (
        "导入测试模块期间新起了未登记的常驻线程：" + ", ".join(offenders)
    )
