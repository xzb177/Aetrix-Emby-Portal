"""用户端信鸽/工单页：对话列表在 v-for 里时模板 ref 是数组，滚动前必须先解包。

亲验背景（2026-10-31 #1630 注册修复单的全仓扫查发现，机理与管理端
regFormRef 完全同款）：SignalView.vue 的对话列表
``<div v-else ref="messagesEl">`` 位于工单卡片 ``v-for="t in tickets"``
的详情区内，模板编译后带 ref_for，``messagesEl.value`` 实际是**数组**
（同时至多展开一张卡，长度 ≤1）。旧 ``scrollMessagesToBottom`` 按单
元素取用——``const el = messagesEl.value; if (el) el.scrollTop = ...``：
给数组赋 scrollTop 不抛错、静默无效，症状只有「新消息自动滚到底部
从不生效」，其他功能不受影响。

修复口径：消费前先解包（数组取 [0]，空数组/未展开时按现状不滚），
取用走一个与 #1630 Settings.vue 同风格的小解包 helper，声明类型同步
放宽为 ``HTMLElement | HTMLElement[] | null``。

本文件只读源码文本，不 import 任何应用模块（避免抢先初始化 engine
污染同批测试，同 tests/test_admin_reg_form_ref_unwrap.py 的口径）。
"""
import re
from pathlib import Path

SIGNAL_VUE = (
    Path(__file__).resolve().parents[1]
    / "user_frontend" / "src" / "views" / "game" / "SignalView.vue"
)


def _src() -> str:
    return SIGNAL_VUE.read_text(encoding="utf-8")


def _function_chunk(src: str, name: str) -> str:
    """截取顶层函数 name 的整段源码（定义行起到行首 `}` 收尾）。

    本文件由 prettier 格式化，顶层函数的收尾 `}` 恰在行首，
    函数内部的收尾都带缩进，所以非贪婪匹配到第一个 ``\\n}`` 即函数尾。
    """
    m = re.search(
        rf"(?:async\s+)?function\s+{re.escape(name)}\b[\s\S]*?\n\}}", src
    )
    assert m, f"没在 SignalView.vue 里找到函数 {name}（源码结构变了？）"
    return m.group(0)


def test_scroll_does_not_touch_the_raw_template_ref():
    """scrollMessagesToBottom 不许再直接碰 messagesEl.value（它在 v-for 里是数组）。

    元素必须经解包 helper 取得，消费函数本体里不应出现裸 ref 取值。
    """
    body = _function_chunk(_src(), "scrollMessagesToBottom")
    assert "messagesEl.value" not in body, (
        "scrollMessagesToBottom 仍在直接取 messagesEl.value——"
        "v-for 内的模板 ref 是数组，必须先解包再滚动"
    )
    assert "scrollTop" in body, "滚动赋值不见了（把功能改没了？）"


def test_scroll_element_comes_from_an_unwrap_helper():
    """滚动所用的元素必须来自一个对 messagesEl 做 Array.isArray 解包的 helper。

    helper 形态与 #1630 的 regFormInstance 同风格：顶层函数，内部
    ``Array.isArray(value) ? value[0] ...`` 且引用 messagesEl。
    """
    src = _src()
    body = _function_chunk(src, "scrollMessagesToBottom")
    m = re.search(r"const\s+el\s*=\s*([A-Za-z_$][\w$]*)\s*\(\s*\)", body)
    assert m, (
        "scrollMessagesToBottom 里没找到 `const el = <helper>()` 形态——"
        "元素不是经解包 helper 取得的"
    )
    helper_name = m.group(1)
    helper = _function_chunk(src, helper_name)
    assert "messagesEl" in helper, f"helper {helper_name} 没有引用 messagesEl"
    assert "Array.isArray(" in helper and "[0]" in helper, (
        f"helper {helper_name} 里没有对 messagesEl 的 Array.isArray 解包取 [0]：\n{helper}"
    )


def test_messages_el_declaration_allows_array_shape():
    """messagesEl 的类型标注必须容纳 v-for 收集出的数组形态。"""
    src = _src()
    m = re.search(r"const\s+messagesEl\s*=\s*ref<([^>]*)>", src)
    assert m, "没找到 messagesEl 的 ref 声明（声明被改名了？）"
    assert "HTMLElement[]" in m.group(1), (
        f"messagesEl 类型仍是 {m.group(1)!r}，没有容纳 HTMLElement[] 数组形态"
    )
