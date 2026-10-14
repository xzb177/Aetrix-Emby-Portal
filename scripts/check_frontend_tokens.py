#!/usr/bin/env python3
"""前端设计令牌契约检查：禁止引用「没人定义」的 CSS 变量，禁止令牌自引用。

起因：用户端的 `mobile-tokens.css` 写了整套间距 / 排版令牌，但**从未被 main.ts 引入**，
而 ListItem / SectionHeader / StatCard 三个组件一直在用 `var(--space-md)`、`var(--card-bg)`：
变量解析失败 → `padding: var(--space-md) 0` 整条声明作废，卡片直接没有背景。
类型检查看不出来（CSS 字符串就是字符串），构建也不会失败——只能在构建期用契约检查兜住。

另外 index.css 里曾有一批「自引用」的兼容映射（`--text-primary: var(--text-primary)`）：
CSS 规范里这是循环引用，变量会变成 invalid at computed-value time，
所有 `color: var(--text-primary)` 静默退化成「继承父级颜色」，标题层级全乱且不报错。

检查四条规则：
  1. 任何 `var(--x)` 的 `--x` 必须在**被引入**的样式表里定义过；
  2. 不允许 `--x: var(--x)` 自引用；
  3. 不允许纯别名构成的循环（`--a: var(--b)` + `--b: var(--a)`）；
  4. src 下不允许存在「谁都没引入」的样式表（上面那次事故的根源）。

样式表集合从入口（main.ts / index.html 的 <style>）出发，沿 @import 与 import 语句递归收集，
所以「文件存在但没接进应用」不会被误判成已定义。

用法：python3 scripts/check_frontend_tokens.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTENDS = ("user_frontend", "admin_frontend")

DEFINE_RE = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")
# 一条完整声明：`--x: 值;`（值里带分号的情况在自定义属性里不合法，不必考虑）
DECL_RE = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;]*);")
COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
USE_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")
# 只有「整条值就是一个 var(--x)，没有兜底值」才参与循环判定：
# 带 fallback 的写法（var(--x, 8px)）本身不会构成循环。
PURE_ALIAS_RE = re.compile(r"^var\(\s*(--[A-Za-z0-9_-]+)\s*\)$")
CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\()?['"]([^'"]+)['"]""")
TS_IMPORT_RE = re.compile(r"""import\s+['"]([^'"]*\.css)['"]""")
STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S)

failures: list[str] = []
checks = 0


def resolve(base: Path, spec: str) -> Path | None:
    """把相对引用解析成文件路径；包名（node_modules）与远程地址一律忽略"""
    if not spec or spec.startswith(("http://", "https://", "//", "~", "@")):
        return None
    candidate = (base.parent / spec).resolve()
    if candidate.suffix == "":
        candidate = candidate.with_suffix(".css")
    return candidate if candidate.exists() else None


def collect_stylesheets(frontend: str) -> tuple[dict[Path, str], set[Path]]:
    """返回（被引入的样式表 → 内容）与（src 下所有样式表）

    dict 的顺序按 BFS 收集顺序，也就是**级联顺序**（入口先写的在后面生效）：
    同名令牌的最后一个声明胜出，判定别名就得按这个顺序取最后一次声明。
    """
    src = ROOT / frontend / "src"
    all_sheets = {p.resolve() for p in src.rglob("*.css")}
    loaded: dict[Path, str] = {}
    queue: list[Path] = []

    main_ts = src / "main.ts"
    if main_ts.exists():
        for m in TS_IMPORT_RE.finditer(main_ts.read_text(encoding="utf-8")):
            found = resolve(main_ts, m.group(1))
            if found:
                queue.append(found)

    # 样式表也可能被组件直接 import，或被 <style> 里的 @import 引入
    for path in list(src.rglob("*.ts")) + list(src.rglob("*.vue")):
        text = path.read_text(encoding="utf-8")
        for m in TS_IMPORT_RE.finditer(text):
            found = resolve(path, m.group(1))
            if found:
                queue.append(found)
        for block in STYLE_BLOCK_RE.findall(text):
            for m in CSS_IMPORT_RE.finditer(block):
                found = resolve(path, m.group(1))
                if found:
                    queue.append(found)

    index = 0
    while index < len(queue):
        sheet = queue[index]
        index += 1
        if sheet in loaded or sheet.suffix != ".css":
            continue
        text = sheet.read_text(encoding="utf-8")
        loaded[sheet] = text
        for m in CSS_IMPORT_RE.finditer(text):
            found = resolve(sheet, m.group(1))
            if found:
                queue.append(found)

    return loaded, all_sheets


def find_alias_cycles(aliases: dict[str, str]) -> list[list[str]]:
    """aliases: 变量 → 它唯一的别名目标；返回所有环形链"""
    cycles: list[list[str]] = []
    for start in aliases:
        seen: list[str] = []
        node = start
        while node in aliases:
            if node in seen:
                cycles.append(seen[seen.index(node):] + [node])
                break
            seen.append(node)
            node = aliases[node]
    # 同一个环会从任意一个成员出发被再发现一次，去重（按环上最小成员）
    unique: dict[str, list[str]] = {}
    for cycle in cycles:
        unique.setdefault(min(cycle), cycle)
    return list(unique.values())


def check_frontend(frontend: str) -> None:
    global checks
    src = ROOT / frontend / "src"
    loaded, all_sheets = collect_stylesheets(frontend)

    defined: dict[str, str] = {}
    # 同名令牌以「最后一次声明」为准：值 → (声明序号, 别名目标 or None)
    latest: dict[str, tuple[int, str | None]] = {}
    selfref: list[str] = []
    seq = 0
    for sheet, text in loaded.items():
        rel = sheet.relative_to(ROOT)
        body = COMMENT_RE.sub("", text)
        for lineno, line in enumerate(body.splitlines(), 1):
            for m in DEFINE_RE.finditer(line):
                defined.setdefault(m.group(1), f"{rel}:{lineno}")
        for m in DECL_RE.finditer(body):
            seq += 1
            name, value = m.group(1), m.group(2).strip()
            alias = PURE_ALIAS_RE.match(value)
            target = alias.group(1) if alias else None
            latest[name] = (seq, target)
            if target == name:
                selfref.append(f"{rel}:{body[:m.start()].count(chr(10)) + 1}  {name}: var({name})")
    aliases = {name: target for name, (_seq, target) in latest.items() if target and target != name}

    used: dict[str, set[str]] = {}
    for path in list(src.rglob("*.vue")) + list(src.rglob("*.css")) + list(src.rglob("*.ts")):
        text = path.read_text(encoding="utf-8")
        for m in USE_RE.finditer(text):
            used.setdefault(m.group(1), set()).add(str(path.relative_to(ROOT)))

    checks += 1
    missing = sorted(name for name in used if name not in defined)
    if missing:
        failures.append(f"{frontend}: {len(missing)} 个令牌被引用但没有任何被引入的样式表定义它们")
        for name in missing:
            where = sorted(used[name])
            failures.append(f"    {name} ← {where[0]}" + (f" 等 {len(where)} 处" if len(where) > 1 else ""))

    checks += 1
    cycles = find_alias_cycles(aliases)
    if selfref or cycles:
        total = len(selfref) + len(cycles)
        failures.append(f"{frontend}: {total} 处令牌循环引用（CSS 里会整条失效并静默降级）")
        for item in selfref:
            failures.append("    " + item)
        for cycle in cycles:
            failures.append("    " + " → ".join(cycle))

    checks += 1
    orphans = sorted(p.relative_to(ROOT) for p in all_sheets - set(loaded))
    if orphans:
        failures.append(f"{frontend}: {len(orphans)} 个样式表没有任何入口引入（写了也不会生效）")
        for path in orphans:
            failures.append(f"    {path}")


# U0 冻结（UI 统一 LC-09/LC-10）：main.ts 全量引入的主题 CSS 不许新增；
# --neo-* 体系只许退场（U3/A5），不许在新文件里出现。
# neo-noir-tokens.css 被删除（A5）时，记得同步从 NEO_FREEZE 里删掉它这一行。
MAIN_CSS_ALLOWLIST = {
    "user_frontend": {"aurora.css", "neo-noir-tokens.css", "index.css", "mobile.css", "calendar.css", "v4-paper.css"},
    "admin_frontend": {"aurora.css", "tokens.css", "base.css", "index.css", "element-plus-theme.css", "responsive.css"},
}
NEO_FREEZE = {
    "user_frontend": {
        "components/AppDock.vue", "components/ui/Modal.vue",
        "styles/aurora.css", "styles/index.css", "styles/mobile.css",
        "styles/neo-noir-tokens.css",
    },
    "admin_frontend": set(),
}
NEO_TOKEN_RE = re.compile(r"--neo-[A-Za-z0-9_-]+")


def check_freeze(frontend: str) -> None:
    global checks
    src = ROOT / frontend / "src"
    main_ts = src / "main.ts"

    checks += 1
    imported = set()
    for m in TS_IMPORT_RE.finditer(main_ts.read_text(encoding="utf-8")):
        found = resolve(main_ts, m.group(1))
        if found:
            imported.add(found.name)
    extra = sorted(imported - MAIN_CSS_ALLOWLIST[frontend])
    if extra:
        failures.append(
            f"{frontend}: main.ts 新增了未登记的主题 CSS 全量引入（UI 统一 U0 冻结）："
            + ", ".join(extra)
        )

    checks += 1
    neo_files = set()
    for path in list(src.rglob("*.vue")) + list(src.rglob("*.css")) + list(src.rglob("*.ts")):
        if NEO_TOKEN_RE.search(path.read_text(encoding="utf-8")):
            neo_files.add(str(path.relative_to(src)))
    new_neo = sorted(neo_files - NEO_FREEZE[frontend])
    if new_neo:
        failures.append(
            f"{frontend}: {len(new_neo)} 个文件新增了 --neo-* 引用"
            "（UI 统一 U0 冻结：neo 体系只许退场、不许扩张）"
        )
        for f in new_neo:
            failures.append(f"    {f}")


for frontend in FRONTENDS:
    check_frontend(frontend)
    check_freeze(frontend)

print(f"设计令牌契约检查：{checks} 项")
if failures:
    print("\n".join(failures))
    print("❌ 设计令牌契约检查失败")
    sys.exit(1)
print("✅ 设计令牌契约检查通过（引用的令牌都有定义 · 无循环引用 · 没有孤立样式表 · 主题引入冻结 · neo 冻结）")
