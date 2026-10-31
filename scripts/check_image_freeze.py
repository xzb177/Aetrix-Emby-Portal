#!/usr/bin/env python3
"""部署自检：镜像内 pip freeze 必须与 CI 冻结点（生产锁）一致。

用法（publish-images.yml 的出厂自检之后、推送之前）：
    docker run --rm --entrypoint pip ghcr.io/xzb177/aetrix-api:sha-<sha> freeze > /tmp/image-freeze.txt
    python scripts/check_image_freeze.py backend/requirements.lock /tmp/image-freeze.txt --python-version 3.12

规则：
- 锁里每个包必须在 freeze 里出现且版本逐字相等，否则阻断；
- freeze 里多出来的包（如基础镜像自带的 pip/setuptools）只作提示，
  不阻断——它们不在锁的管辖面；
- 锁条目带环境标记（如 python_version < "3.11"）时，只有标记在
  --python-version / 当前平台下成立才要求 freeze 里有它；不传
  --python-version 时全部条目都要求（严格模式）。

只用标准库。标记求值只覆盖 pip-compile 实际输出的常见形态：
单个比较，或 and 连接的多个比较（python_version、sys_platform）；
无法识别的标记按“要求”处理（fail-closed）。
"""

import argparse
import re
import sys
from pathlib import Path

# 锁里带 extra 的包写作 name[extra]==version（如 uvicorn[standard]），
# 解析时吃掉方括号；freeze 输出本身没有方括号。
_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;\\]+)")


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _join_continuations(text):
    out, buf = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def parse_lock(path):
    entries = {}
    for raw in _join_continuations(Path(path).read_text(encoding="utf-8")):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _NAME_RE.match(line)
        if m and not raw.startswith((" ", "\t")):
            marker = ""
            rest = line[m.end():]
            if ";" in rest:
                marker = rest.split(";", 1)[1].strip()
            entries[normalize(m.group(1))] = (m.group(2), marker)
    return entries


def parse_freeze(path):
    pkgs = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        m = _NAME_RE.match(line)
        if m:
            pkgs[normalize(m.group(1))] = m.group(2)
    return pkgs


_CMP_RE = re.compile(
    r"""^(python_version|sys_platform)\s*(==|!=|<=|>=|<|>)\s*["']([^"']+)["']$""")


def _version_tuple(v):
    parts = []
    for p in re.split(r"[.-]", v):
        parts.append(int(p) if p.isdigit() else p)
    return tuple(parts)


def _eval_marker(marker, python_version, sys_platform):
    """求值标记；返回 True=该条目在目标环境应安装。"""
    if not marker:
        return True
    if python_version is None:
        return True
    for clause in marker.split(" and "):
        clause = clause.strip().strip("()").strip()
        m = _CMP_RE.match(clause)
        if not m:
            return True  # 无法识别的标记形态：按要求处理
        var, op, want = m.group(1), m.group(2), m.group(3)
        actual = python_version if var == "python_version" else sys_platform
        if var == "python_version":
            a, b = _version_tuple(actual), _version_tuple(want)
            ok = {"==": a == b, "!=": a != b, "<": a < b,
                  "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        else:
            ok = {"==": actual == want, "!=": actual != want}.get(op, True)
        if not ok:
            return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", help="生产锁文件（如 backend/requirements.lock）")
    parser.add_argument("freeze", help="镜像内 pip freeze 输出文件")
    parser.add_argument("--python-version", default=None,
                        help="镜像内 Python 大版本（如 3.12），用于标记求值")
    parser.add_argument("--sys-platform", default="linux",
                        help="镜像平台（默认 linux）")
    args = parser.parse_args(argv)

    lock = parse_lock(args.lock)
    freeze = parse_freeze(args.freeze)
    violations, skipped, extras = [], [], []
    for name, (version, marker) in sorted(lock.items()):
        if not _eval_marker(marker, args.python_version, args.sys_platform):
            skipped.append(name)
            continue
        if name not in freeze:
            violations.append(f"镜像缺包：{name}=={version}（锁要求，镜像未装）")
        elif freeze[name] != version:
            violations.append(
                f"镜像版本漂移：{name} 锁 {version} vs 镜像 {freeze[name]}")
    for name in sorted(freeze):
        if name not in lock:
            extras.append(name)
    if violations:
        print("❌ 镜像依赖冻结点比对失败：")
        for v in violations:
            print(f"  - {v}")
        print(f"锁 {len(lock)} 个包，镜像 {len(freeze)} 个包，"
              f"标记跳过 {len(skipped)} 个，违规 {len(violations)} 项")
        return 1
    print(f"✅ 镜像依赖冻结点比对通过：锁 {len(lock)} 个包全对齐"
          f"（标记跳过 {len(skipped)} 个，镜像多余 {len(extras)} 个仅提示："
          f"{', '.join(extras[:10])}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
