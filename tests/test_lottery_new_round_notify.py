"""群抽奖创建活动后必须通知群（回归测试）。

背景：管理后台"新建活动"走 POST /welfare/lottery/rounds，
之前创建后没有任何群通知，导致群里没人知道有新抽奖、没人参加，
开奖时 0 参与人数。开奖通知（notify_draw_results）是有的，
但"新活动"通知缺失。

本测试用 AST 静态断言：
1. create_lottery_round 函数体内必须出现 notify_new_round 调用；
2. backend/lottery.py 必须定义 notify_new_round 函数；
3. notify_new_round 必须构造 lottery_join: 回调按钮。
防止将来重构时再次漏掉。
"""
import ast
import sys

sys.path.insert(0, ".")


def _get_func_node(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls_notify_new_round(func_node):
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "notify_new_round":
                return True
            if isinstance(f, ast.Name) and f.id == "notify_new_round":
                return True
    return False


def _has_join_button(func_node):
    """notify_new_round 必须构造 lottery_join: 回调按钮。"""
    src = ast.unparse(func_node)
    return "lottery_join:" in src


def test_create_round_calls_notify_new_round():
    """create_lottery_round 必须调用 notify_new_round。"""
    with open("backend/api/welfare_admin.py", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    func = _get_func_node(tree, "create_lottery_round")
    assert func is not None, "找不到 create_lottery_round 函数"
    assert _calls_notify_new_round(func), (
        "create_lottery_round 没有调用 notify_new_round！"
        "创建抽奖后群里收不到新活动通知（回归）。"
    )


def test_notify_new_round_defined():
    """backend/lottery.py 必须定义 notify_new_round。"""
    with open("backend/lottery.py", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    func = _get_func_node(tree, "notify_new_round")
    assert func is not None, "backend/lottery.py 缺少 notify_new_round 函数"


def test_notify_new_round_has_join_button():
    """公告必须带 lottery_join: 参加按钮。

    2026-10-31 根治后按钮由同源 builder build_announce_markup 构造
    （notify_new_round 与人数编辑共用），断言扩到 builder 本体，
    守卫意图不变：公告卡上必须有 lottery_join 参加按钮。
    """
    with open("backend/lottery.py", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    func = _get_func_node(tree, "notify_new_round")
    assert func is not None, "找不到 notify_new_round 函数"
    builder = _get_func_node(tree, "build_announce_markup")
    assert builder is not None, "找不到 build_announce_markup 函数"
    assert _has_join_button(builder), (
        "build_announce_markup 没有构造 lottery_join: 回调按钮，"
        "群成员无法一键参加。"
    )
