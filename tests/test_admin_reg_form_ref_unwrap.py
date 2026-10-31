"""管理端注册策略保存：el-form 在 v-for 里时模板 ref 是数组，validate 必须先解包。

亲验背景（2026-10-31 诊断，vue 3.5.26 + element-plus 2.13.0 + jsdom
同版本复现闭环）：Settings.vue 的注册策略 ``<el-form ref="regFormRef">``
位于 SECTIONS 的 ``<template v-for>`` 内部，模板编译后带 ref_for，
``regFormRef.value`` 实际是长度为 1 的**数组**而非表单实例。旧
``saveRegistrationCore`` 直接 ``await regFormRef.value?.validate()``——
对数组调用不存在的方法抛 TypeError，又被无参 catch 静默吞掉
``return false``：PUT 永不发出、后端零日志，页面只剩 saveAll 的汇总
横幅（「「注册策略」没存上」），每次必现。

修复口径：调 validate 前先解包 ref（数组取 [0]），且 catch 区分两类
失败——校验不通过（Element Plus 拒绝值是 invalidFields 普通对象）保持
静默返回；程序错误（Error 实例）必须提示＋console.error 留痕，不许再
无声无息。

本文件只读源码文本，不 import 后端任何模块（避免抢先初始化 engine
污染同批测试，同 tests/test_admin_api_paths.py 的口径）。
"""
import re
from pathlib import Path

SETTINGS_VUE = (
    Path(__file__).resolve().parents[1]
    / "admin_frontend" / "src" / "views" / "Settings.vue"
)


def _src() -> str:
    return SETTINGS_VUE.read_text(encoding="utf-8")


def _function_chunk(src: str, name: str) -> str:
    """截取顶层函数 name 的整段源码（定义行起到行首 `}` 收尾）。

    本文件由 prettier 格式化，顶层函数的收尾 `}` 恰在行首，
    函数内部的收尾都带缩进，所以非贪婪匹配到第一个 ``\\n}`` 即函数尾。
    """
    m = re.search(rf"function\s+{re.escape(name)}\b[\s\S]*?\n\}}", src)
    assert m, f"没在 Settings.vue 里找到函数 {name}（源码结构变了？）"
    return m.group(0)


def _validate_receiver(body: str) -> str:
    m = re.search(r"([A-Za-z_$][\w$]*(?:\(\))?)\?\.validate\(\)", body)
    assert m, "saveRegistrationCore 里没找到 ?.validate() 调用（校验被删了？）"
    return m.group(1)


def test_save_registration_does_not_validate_the_raw_template_ref():
    """不许再对 regFormRef.value 直接调 validate（它在 v-for 里是数组）。"""
    body = _function_chunk(_src(), "saveRegistrationCore")
    assert "regFormRef.value?.validate" not in body
    assert "regFormRef.value.validate" not in body


def test_validate_receiver_comes_from_unwrapping_the_ref():
    """validate 的接收者必须来自对 regFormRef 的 Array.isArray 解包。

    接收者可以是解包 helper 的调用，也可以是函数内先解包出的本地变量；
    两种形态都要求其定义里出现 Array.isArray(...) 且引用 regFormRef。
    """
    src = _src()
    body = _function_chunk(src, "saveRegistrationCore")
    receiver = _validate_receiver(body)
    assert receiver != "regFormRef.value"
    name = receiver[:-2] if receiver.endswith("()") else receiver

    region = ""
    helper = re.search(rf"function\s+{re.escape(name)}\b[\s\S]*?\n\}}", src)
    if helper:
        region = helper.group(0)
    else:
        local = re.search(
            rf"(?:const|let)\s+{re.escape(name)}\s*=[\s\S]{{0,300}}?(?:;|\n)", body
        )
        if local:
            region = local.group(0)
    assert region, f"找不到 validate 接收者 {name} 的定义（解包 helper 去哪了？）"
    assert "Array.isArray(" in region and "regFormRef" in region, (
        f"validate 接收者 {name} 的定义里没有对 regFormRef 的 Array.isArray 解包：\n{region}"
    )


def test_validate_catch_surfaces_program_errors():
    """validate 的 catch 不许再把程序错误和校验不通过一起静默吞掉。

    程序错误（Error 实例，如 ref 形状异常抛的 TypeError）必须留痕并给
    用户一句提示；校验不通过的拒绝值是普通对象，不走这条分支。
    """
    body = _function_chunk(_src(), "saveRegistrationCore")
    assert "instanceof Error" in body, "catch 没有用 instanceof Error 区分程序错误"
    assert "console.error" in body, "程序错误没有 console.error 留痕"
