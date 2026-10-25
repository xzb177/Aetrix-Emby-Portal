#!/usr/bin/env python3
"""冲突标记检查：rebase/amend 解决冲突后残留的标记行必须为零。

背景（2026-10-15 r11）：rebase 解决 api.py 冲突时只替换了分隔线之后的部分，
起始标记首行残留，git add + rebase --continue 照单提交，靠 pytest 报
SyntaxError 才发现。本检查是那次教训的自动化兜底，CI 与 pre-commit 共用同一实现。

匹配口径（只认 git 冲突标记的形态，不误报）：
- 行首 7 个小于号（如起始标记行）或行首 7 个大于号（如结束标记行）；
- 单独一行的 7 个等号（即分隔线本身）。等号必须整行匹配，因为代码里
  有合法的等号分隔线（如 image_store.py 的注释横幅），前缀匹配会误报。
扫描整棵检出树，跳过 .git 与 node_modules；无法按 UTF-8 解码的文件跳过。
命中时打印 file:line:content 并 exit 1；干净时 exit 0。
"""

import pathlib
import re
import sys

PATTERNS = (
    re.compile(r"^<{7}"),
    re.compile(r"^>{7}"),
    re.compile(r"^={7}\s*$"),
)
SKIP_DIRS = {".git", "node_modules"}


def find_markers(root: pathlib.Path) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in PATTERNS):
                hits.append(f"{path}:{lineno}:{line.rstrip()}")
    return hits


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    hits = find_markers(root)
    if hits:
        print("冲突标记残留（必须清零）：")
        for hit in hits:
            print("  " + hit)
        return 1
    print("冲突标记检查通过：无残留")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
