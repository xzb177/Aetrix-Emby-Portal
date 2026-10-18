"""streaming 模块注解名称的回归测试（仅依赖标准库 ast）。

背景
----
``backend/emby_server/streaming.py`` 顶部有 ``from __future__ import annotations``，
模块内对 httpx 采用运行时延迟导入（各函数内按需 ``import httpx``，保持模块导入轻量），
但模块级注解里引用了 ``httpx.AsyncClient`` / ``httpx.Timeout``：

- ``_shared_client: Optional["httpx.AsyncClient"] = None``
- ``def relay_timeout() -> "httpx.Timeout":``
- ``def get_relay_client() -> "httpx.AsyncClient":``

顶层从未导入 httpx，pyflakes 会报 3 处 F821 ``undefined name 'httpx'``。
修复方式是在 ``if TYPE_CHECKING:`` 分支内 ``import httpx``：类型检查器可见，
运行时不会执行，延迟导入行为不变。

（2026-10-17 中继删除：relay_timeout / get_relay_client 已随中继删除，
streaming.py 当前已无 httpx 引用；下面的 TYPE_CHECKING 断言改为条件式，
httpx 注解若回来仍必须走 TYPE_CHECKING 分支。）

本测试静态解析源码，收集模块顶层已定义的名称与所有注解引用的名称根，
断言注解引用的名称根均有定义，从而防止 F821 回归。
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "backend"
    / "emby_server"
    / "streaming.py"
)


def _load_module_ast() -> ast.Module:
    """解析被测模块源码，返回模块级 AST。"""
    source = MODULE_PATH.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(MODULE_PATH))


def _collect_top_level_names(tree: ast.Module) -> set[str]:
    """收集模块顶层定义的名称。

    包括顶层 ``import x`` / ``from y import z`` 的引入名、顶层赋值 / 函数 / 类定义，
    以及 ``if TYPE_CHECKING:`` 分支内的 import（该分支静态可见、运行时不执行）。
    """

    def collect(statements: list[ast.stmt]) -> set[str]:
        names: set[str] = set()
        for stmt in statements:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for alias in stmt.names:
                    # `import a.b` 运行时绑定的是 a
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(stmt.name)
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    for node in ast.walk(target):
                        if isinstance(node, ast.Name):
                            names.add(node.id)
            elif isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name):
                    names.add(stmt.target.id)
            elif isinstance(stmt, ast.If):
                # if TYPE_CHECKING: ... / else: ...
                names |= collect(stmt.body) | collect(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                names |= collect(stmt.body)
                names |= collect(stmt.orelse)
                names |= collect(stmt.finalbody)
                for handler in stmt.handlers:
                    names |= collect(handler.body)
            elif isinstance(
                stmt, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)
            ):
                names |= collect(stmt.body)
                names |= collect(getattr(stmt, "orelse", []))
        return names

    return collect(tree.body)


def _name_roots(annotation: ast.AST) -> set[str]:
    """提取一个注解表达式引用的名称根。

    - ``Name`` 节点取 ``id``；
    - ``Attribute`` 节点（如 ``httpx.AsyncClient``）剥到最内层的 ``Name`` 取 ``id``；
    - 字符串形式的向前引用（``-> "httpx.Timeout"``）按表达式再解析一次，
      与 pyflakes 的行为保持一致，避免漏检。
    """
    roots: set[str] = set()
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            roots.add(node.id)
        elif isinstance(node, ast.Attribute):
            inner: ast.AST = node
            while isinstance(inner, ast.Attribute):
                inner = inner.value
            if isinstance(inner, ast.Name):
                roots.add(inner.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                parsed = ast.parse(node.value, mode="eval")
            except SyntaxError:
                continue
            roots |= _name_roots(parsed)
    return roots


def _collect_annotation_roots(tree: ast.Module) -> set[str]:
    """收集模块内所有注解（变量注解、参数注解、返回注解）引用的名称根。"""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            roots |= _name_roots(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            if args.vararg is not None:
                all_args.append(args.vararg)
            if args.kwarg is not None:
                all_args.append(args.kwarg)
            for arg in all_args:
                if arg.annotation is not None:
                    roots |= _name_roots(arg.annotation)
            if node.returns is not None:
                roots |= _name_roots(node.returns)
    return roots


def _runtime_top_level_import_names(tree: ast.Module) -> set[str]:
    """收集模块顶层会真正执行的 import 名称（不含 if TYPE_CHECKING 分支）。"""
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _type_checking_import_names(tree: ast.Module) -> set[str]:
    """收集顶层 ``if TYPE_CHECKING:`` 分支内的 import 名称。"""
    names: set[str] = set()
    for stmt in tree.body:
        if not isinstance(stmt, ast.If):
            continue
        test = stmt.test
        is_type_checking = (
            isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
        ) or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if not is_type_checking:
            continue
        for sub in stmt.body:
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for alias in sub.names:
                    names.add(alias.asname or alias.name.split(".")[0])
    return names


def test_annotation_names_are_defined() -> None:
    """注解里引用的每个名称根都必须能在模块顶层找到定义（防 F821 回归）。"""
    tree = _load_module_ast()
    defined = _collect_top_level_names(tree) | set(dir(builtins))
    referenced = _collect_annotation_roots(tree)

    missing = sorted(referenced - defined)
    assert not missing, (
        f"{MODULE_PATH} 的注解引用了未定义的名称：{missing}"
        "（pyflakes 会报 F821 undefined name）"
    )


def test_httpx_only_declared_under_type_checking() -> None:
    """httpx 只能在 ``if TYPE_CHECKING:`` 内声明，不能被提升为顶层运行时导入。

    2026-10-17 中继删除：httpx 延迟客户端（relay_timeout/get_relay_client）
    已随中继删除，streaming.py 当前已无 httpx 引用；若将来重新引入
    httpx 注解，仍必须走 TYPE_CHECKING 分支（运行时保持延迟导入），
    否则 pyflakes 报 F821。
    """
    tree = _load_module_ast()

    runtime_imports = _runtime_top_level_import_names(tree)
    assert "httpx" not in runtime_imports, (
        "httpx 应保持运行时延迟导入，不应出现在模块顶层的运行时 import 中"
    )

    referenced = _collect_annotation_roots(tree)
    if "httpx" in referenced:
        type_checking_imports = _type_checking_import_names(tree)
        assert "httpx" in type_checking_imports, (
            "缺少 `if TYPE_CHECKING: import httpx` 声明，注解中的 "
            "httpx.AsyncClient / httpx.Timeout 会让 pyflakes 报 F821"
        )
