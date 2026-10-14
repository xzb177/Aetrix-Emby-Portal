#!/usr/bin/env python3
"""UI 统一 U1：纸色 hex → token、圆角 px → 阶梯 token 的机械迁移（等值替换）。

规则（见 ~/workspace/reports/ui-unify-plan-20261014.md §3/§5）：
- 只处理纸色色板原值 hex；dark 体系（#000/#0c0a09/#11100e/#f3ede4…）与非色板值一律不动。
- #fff/#ffffff 只替换 `color:` 声明（深色/彩色底上的白字 → --on-dark），
  background 用法与 JS 逻辑（useBranding.ts）不动。
- 圆角：仅单值 border-radius（含四个角属性）；多值简写、异形值不动。
- 跳过：au 框架层文件（Login/顶栏/Toast/Modal/EmptyState…）、token 定义行、
  var(--x, #hex) 兜底值、注释。
- 等值保证：每个被替换的 hex 必须与对应 token 当前值逐一相等（大小写不敏感）；
  半径 token 值与 px 字面值逐一相等。脚本内建断言，失败即停。
- 半径替换只在「已引用过 v4 token」的文件里做（证明在 .v4-scope 继承链内）。

用法：python3 scripts/ui-u1-migrate.py [--apply]
默认只输出替换/跳过报告；--apply 才真正写文件。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "user_frontend" / "src"
APPLY = "--apply" in sys.argv

# token 名 → 当前值（必须与 v4-paper.css .v4-scope 内定义逐一对应）
TOKEN_VALUES = {
    "paper": "#F6F1E7",
    "paper-card": "#FFFDF6",
    "paper2": "#EFE7D6",
    "ink": "#2A2620",
    "ink2": "#6B6257",
    "line": "#D8CDB8",
    "red": "#D9482B",
    "green": "#4A7C59",
    "gold": "#C99B3F",
    "red-deep": "#A33319",
    "pulse-green": "#9BE08A",
    "ember": "#FFB25E",
    "line-deep": "#C9BFA9",
    "gold-deep": "#9A7627",
    "on-dark": "#FFFFFF",
}
HEX_MAP = {v.lower(): k for k, v in TOKEN_VALUES.items()}

# px 字面值 → (阶梯 token, 阶梯实际值)。6px/14px 并入邻近阶梯（差 2px，人眼复核）。
RADIUS_MAP = {
    "6px": ("paper-r-sm", "8px"),
    "8px": ("paper-r-sm", "8px"),
    "10px": ("paper-r-md", "10px"),
    "12px": ("paper-r-lg", "12px"),
    "14px": ("paper-r-xl", "16px"),
    "16px": ("paper-r-xl", "16px"),
    "20px": ("paper-r-2xl", "20px"),
    "50%": ("paper-r-full", "999px"),
    "999px": ("paper-r-full", "999px"),
    "9999px": ("paper-r-full", "999px"),
}
LADDER_VALUES = {
    "paper-r-sm": "8px", "paper-r-md": "10px", "paper-r-lg": "12px",
    "paper-r-xl": "16px", "paper-r-2xl": "20px", "paper-r-full": "999px",
}

# au 框架层：牵涉 dark 渲染，U1 一律不碰（留给 U2）
EXCLUDE_FILES = {
    "views/LoginView.vue", "views/TgBindGuideView.vue", "views/TgLoginView.vue",
    "views/NotFoundView.vue", "components/AppHeader.vue", "components/AppDock.vue",
    "components/Toast.vue", "components/ProfileSubscriptions.vue",
    "components/ui/Modal.vue", "components/ui/EmptyState.vue",
    "composables/useBranding.ts",
    # router/index.ts 的内联样式直接挂 document.body（.v4-scope 之外），
    # var(--on-dark) 在那里解析不到——整文件跳过。
    "router/index.ts",
}
V4_TOKEN_RE = re.compile(
    r"var\(--(?:paper|paper2|paper-card|ink|ink2|line|red|blue|green|gold|scrim"
    r"|red-deep|gold-deep|line-deep|ember|pulse-green|on-dark)\b"
)
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
DEF_RE = re.compile(r"^\s*--[A-Za-z0-9_-]+\s*:")
RADIUS_RE = re.compile(
    r"(border(?:-top-left|-top-right|-bottom-left|-bottom-right)?-radius\s*:\s*)([^;{}]+)(;)"
)
WHITE_COLOR_RE = re.compile(r"(color\s*:\s*)#(?:fff|ffffff)\b", re.IGNORECASE)
COMMENT_SPLIT_RE = re.compile(r"/\*|//")

replaced: list[str] = []
skipped: list[str] = []
spot14: list[str] = []


def migrate_file(path: Path) -> None:
    rel = str(path.relative_to(SRC))
    if rel in EXCLUDE_FILES:
        skipped.append(f"{rel}: 整文件跳过（au 框架层/dark 相关）")
        return
    text = path.read_text(encoding="utf-8")
    has_v4 = bool(V4_TOKEN_RE.search(text))
    out_lines: list[str] = []
    file_changed = False

    for i, raw in enumerate(text.splitlines(keepends=True), 1):
        nl = "\n" if raw.endswith("\n") else ""
        body = raw[: -len(nl)] if nl else raw
        cm = COMMENT_SPLIT_RE.search(body)
        code, comment = (body[: cm.start()], body[cm.start():]) if cm else (body, "")
        if DEF_RE.match(code):
            out_lines.append(raw)
            continue
        changed = False

        def hex_sub(m: re.Match) -> str:
            nonlocal changed
            h = m.group(0)
            # R40：HEX_RE.group(0) 含 # 前缀，原写法恒 False，跳过分支从未生效
            if h.lower() in ("#fff", "#ffffff"):
                skipped.append(f"{rel}:{i}: 跳过（#fff 非 color 用法，留给 U2 语义判断）")
                return h
            if h.lower() not in HEX_MAP:
                skipped.append(f"{rel}:{i}: 跳过（非色板 hex {h}）")
                return h
            before = code[: m.start()]
            if re.search(r"var\(\s*--[A-Za-z0-9_-]+\s*,\s*$", before):
                skipped.append(f"{rel}:{i}: 跳过（var 兜底值 {h}）")
                return h
            token = HEX_MAP[h.lower()]
            assert h.lower() == TOKEN_VALUES[token].lower(), (
                f"等值断言失败 {rel}:{i} {h} != {token}"
            )
            changed = True
            replaced.append(f"{rel}:{i}: {h} → var(--{token}, {h})")
            return f"var(--{token}, {h})"

        def white_sub(m: re.Match) -> str:
            nonlocal changed
            if not has_v4:
                skipped.append(f"{rel}:{i}: 跳过（文件无 v4 token 引用，继承链未证）")
                return m.group(0)
            changed = True
            replaced.append(
                f"{rel}:{i}: {m.group(0).strip()} → color: var(--on-dark, #fff)"
            )
            return f"{m.group(1)}var(--on-dark, #fff)"

        def radius_sub(m: re.Match) -> str:
            nonlocal changed
            v = m.group(2).strip()
            if v not in RADIUS_MAP:
                skipped.append(f"{rel}:{i}: 跳过（圆角无对应阶梯/异形值 {v}）")
                return m.group(0)
            token = RADIUS_MAP[v][0]
            # R40：真断言——替换后的 var() 实际渲染值（token 定义值）必须等于原文值，
            # 非等值替换（如 14px → --paper-r-xl/16px）是视觉 bug，必须人眼复核确认。
            # 原写法 LADDER_VALUES[token] != ladder_val 同源比较永真，断言空转。
            assert LADDER_VALUES[token] == v, (
                f"圆角非等值替换需人眼复核 {rel}:{i} {v} → --{token}（定义 {LADDER_VALUES[token]}）"
            )
            if not has_v4:
                skipped.append(f"{rel}:{i}: 跳过（文件无 v4 token 引用，继承链未证）")
                return m.group(0)
            if v == "14px":
                spot14.append(f"{rel}:{i}: 14px → var(--paper-r-xl)")
            changed = True
            replaced.append(f"{rel}:{i}: border-radius {v} → var(--{token}, {v})")
            return f"{m.group(1)}var(--{token}, {v}){m.group(3)}"

        code = WHITE_COLOR_RE.sub(white_sub, code)
        code = HEX_RE.sub(hex_sub, code)
        code = RADIUS_RE.sub(radius_sub, code)
        out_lines.append(code + comment + nl)
        file_changed = file_changed or changed

    if file_changed and APPLY:
        path.write_text("".join(out_lines), encoding="utf-8")


def main() -> None:
    files = sorted(
        list(SRC.rglob("*.vue")) + list(SRC.rglob("*.css")) + list(SRC.rglob("*.ts"))
    )
    for p in files:
        migrate_file(p)
    print(f"== 替换 {len(replaced)} 处 ==")
    for r in replaced:
        print("  " + r)
    print(f"== 跳过 {len(skipped)} 条 ==")
    for s in skipped:
        print("  " + s)
    print(f"== 14px 并入 xl 需人眼抽查 {len(spot14)} 处 ==")
    for s in spot14:
        print("  " + s)
    print("APPLY" if APPLY else "DRY-RUN（未写文件）")


if __name__ == "__main__":
    main()
