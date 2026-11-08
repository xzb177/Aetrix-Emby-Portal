"""导入期环境变量泄漏守卫：测试文件导入时不许写 os.environ（引导键除外）。

背景（同一毛病两次，2026-11-08）：
- #1888：tests/test_pool_playbackinfo.py 在导入期写
  os.environ.setdefault("TRANSFER_VARIANT_ENABLED", "1")，pytest 收集期
  就把转存总闸钉死为开，污染同进程其他用例，CI 红 6 条；
- #1891：tests/test_pool_serve.py 又写了同一行导入期 setdefault，污染
  两条 video_stream 用例，CI 红 2 条。
两处现都已改为用例期 monkeypatch.setenv 按用例设置（泄漏源已删），
本守卫负责让这类写法以后在 CI 里当场变红、而不是靠人肉复盘才发现。

为什么是静态 AST 扫描而不是运行时导入比对：
- pytest 先收集（导入）全部测试模块、之后才执行任何用例，本守卫运行时
  泄漏早已发生，进程内拿不到「导入前」的环境基线；
- 子进程逐个重导入 900+ 个测试模块等于重跑一遍收集，且每个模块的导入
  副作用（重绑引擎、建临时库）会在子进程里互相污染，结果不可靠；
- 同文件族的 #1668 守卫（test_zz_fixture_hygiene.py 里查导入期重绑引擎）
  用的就是纯源码静态检查，本守卫与它同形状：只算导入时真正执行的顶层
  语句（递归进 If/Try/With，不进函数/类体），逐文件点名是哪个文件、
  哪个键、哪一行。

判定口径：
- 引导键（全套件既定约定，数值全仓一致或语义即「测试库引导」）放行：
  DATABASE_TYPE 只许值 "sqlite"、REDIS_ENABLED 只许值 "false"
  （setdefault 与直接赋值均可，但值必须是这两个字面量）；
  DATABASE_URL 放行（各文件指向自己的临时 sqlite 库，引擎侧另有
  #1668 守卫管重绑建表）；SECRET_KEY 只许 setdefault（12 个文件皆
  如此，应用导入期需要一个占位密钥，先到者生效、不许直接赋值覆盖）。
- 其余任何键在导入期被 setdefault / 赋值 / putenv / pop / 删除 /
  environ.update 触碰，一律违规；键名是动态表达式同样违规（无法核）。
- 存量单点写键的只进 EXEMPTIONS 豁免表（文件+键双锚），逐条写理由；
  不许整表放行、不许按目录放行。新文件或新键默认不放行。
- 用例运行期 monkeypatch.setenv 是合法用法，本守卫只看导入期顶层语句，
  函数/类体内的写法不在扫描面内。
"""

from __future__ import annotations

import ast
import pathlib

# 引导键：值被钉死的两个（键 -> 唯一允许的字面量值）。
BOOTSTRAP_EXACT_VALUES = {
    "DATABASE_TYPE": "sqlite",
    "REDIS_ENABLED": "false",
}
# 引导键：值动态放行（每文件指向自己的临时 sqlite 库）。
BOOTSTRAP_FREE_KEYS = {"DATABASE_URL"}
# 引导键：只许 setdefault、不许直接赋值覆盖。
SETDEFAULT_ONLY_KEYS = {"SECRET_KEY"}

# 存量豁免（文件, 键）-> 理由。本单只登记、不治理存量；治理方式是把
# 导入期写键改成用例期 monkeypatch，治理完一条就删一条豁免。
# 三个功能开关类（OBSERVABILITY_ENABLED / PANEL_V4_ENABLED /
# TG_PROACTIVE_FLUSHER）与两次事故同类、属已知风险存量，登记是为了让
# 守卫先落地；其余四个是该文件所测功能读的目录类键，值均为 /tmp 下路径。
EXEMPTIONS = {
    ("test_governance_report_step2.py", "WELCOME_FILE_DIR"): (
        "存量登记：该文件所测治理报告在导入期读欢迎文件目录，setdefault "
        "'/tmp'；治理另单处理。"
    ),
    ("test_image_season_poster_ws5.py", "EMBY_TMDB_CACHE_DIR"): (
        "存量登记：该文件所测海报缓存在导入期读缓存目录，赋值为其临时"
        "路径变量；治理另单处理。"
    ),
    ("test_mediainfo_persist.py", "MEDIAINFO_JSON_DIR"): (
        "存量登记：该文件所测 mediainfo 落盘在导入期读输出目录，"
        "setdefault '/tmp/test_mediainfo_json'；治理另单处理。"
    ),
    ("test_probe_streams_mediainfo.py", "MEDIAINFO_JSON_DIR"): (
        "存量登记：该文件所测 mediainfo 探测在导入期读输出目录，"
        "setdefault '/tmp/test_mediainfo_json_streams'；治理另单处理。"
    ),
    ("test_observability.py", "OBSERVABILITY_ENABLED"): (
        "存量登记（与事故同类的功能开关）：该文件测可观测性本身，导入期"
        "赋值 '1' 打开开关；治理另单处理。"
    ),
    ("test_tg_proactive_notify.py", "TG_PROACTIVE_FLUSHER"): (
        "存量登记（与事故同类的功能开关）：该文件测 TG 主动通知，导入期"
        "赋值 '0' 关掉刷盘器；治理另单处理。"
    ),
    ("test_worn_badge.py", "PANEL_V4_ENABLED"): (
        "存量登记（与事故同类的功能开关）：该文件测 v4 面板徽章，导入期"
        "setdefault 'true'；治理另单处理。"
    ),
}


def _iter_import_time_nodes(node):
    """产出导入时执行的节点：顶层语句，递归进 If/Try/With，不进函数/类体。"""
    if isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    ):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _iter_import_time_nodes(child)


def _is_environ(node):
    """是不是 os.environ（或 from os import environ 后的 environ）。"""
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _const_str(node):
    """节点是字符串字面量就返回其值，否则返回 None（动态/不可核）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _import_time_env_writes(path):
    """扫一个文件导入期的环境变量写操作：[(键, 形式, 值字面量或None, 行号)]。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    writes = []
    for stmt in tree.body:
        for node in _iter_import_time_nodes(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and _is_environ(func.value):
                    if func.attr == "setdefault" and node.args:
                        value = (
                            _const_str(node.args[1]) if len(node.args) > 1 else None
                        )
                        writes.append(
                            (_const_str(node.args[0]), "setdefault", value, node.lineno)
                        )
                    elif func.attr in ("pop", "update"):
                        # pop 改环境、update 批量改环境：导入期一律不许。
                        key = _const_str(node.args[0]) if node.args else None
                        writes.append((key, func.attr, None, node.lineno))
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr == "putenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    if node.args:
                        value = (
                            _const_str(node.args[1]) if len(node.args) > 1 else None
                        )
                        writes.append(
                            (_const_str(node.args[0]), "putenv", value, node.lineno)
                        )
            targets = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and _is_environ(target.value):
                    value = _const_str(node.value) if hasattr(node, "value") else None
                    writes.append(
                        (_const_str(target.slice), "assign", value, node.lineno)
                    )
            if isinstance(node, ast.Delete):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and _is_environ(target.value):
                        writes.append(
                            (_const_str(target.slice), "del", None, node.lineno)
                        )
    return writes


def _write_violation(fname, key, form, value):
    """一条导入期写操作是否违规；违规返回人话原因，放行返回 None。"""
    if key is None:
        return "键名是动态表达式，无法核对"
    if (fname, key) in EXEMPTIONS:
        return None
    if form in ("pop", "del", "update"):
        return f"导入期不许用 {form} 改环境变量"
    if key in BOOTSTRAP_EXACT_VALUES:
        expected = BOOTSTRAP_EXACT_VALUES[key]
        if value == expected:
            return None
        return f"引导键 {key} 只许值 {expected!r}，这里写的是 {value!r}"
    if key in BOOTSTRAP_FREE_KEYS:
        return None
    if key in SETDEFAULT_ONLY_KEYS:
        if form == "setdefault":
            return None
        return f"引导键 {key} 只许 setdefault，不许 {form} 覆盖"
    return "非引导键不许在导入期写环境变量（收集期就会污染同进程其他用例）"


def test_no_import_time_env_writes_outside_bootstrap():
    tests_dir = pathlib.Path(__file__).parent
    violations = []
    for path in sorted(tests_dir.glob("test_*.py")):
        for key, form, value, lineno in _import_time_env_writes(path):
            reason = _write_violation(path.name, key, form, value)
            if reason is not None:
                shown_key = key if key is not None else "<动态键>"
                violations.append(
                    f"{path.name}:{lineno} {form} {shown_key}（{reason}）"
                )
    assert not violations, (
        "测试文件在导入期写 os.environ：pytest 收集期就生效、且全进程不"
        "回收，其他文件的用例会被静默污染（#1888/#1891 同形态回归）——"
        "请改成用例期 monkeypatch.setenv 按用例设置；确属引导键或存量"
        "单点的，先在本文件 BOOTSTRAP_*/EXEMPTIONS 里按规则登记并写明"
        "理由。违规清单：\n" + "\n".join(violations)
    )
