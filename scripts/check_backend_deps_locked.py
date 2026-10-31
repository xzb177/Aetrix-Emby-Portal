#!/usr/bin/env python3
"""CI 门禁：后端依赖必须锁定（审计 A-078 的回归兜底）。

断言四件事，任一不满足即 exit 1：
1. backend/requirements.txt 里没有裸下界：每个直接依赖要么钉死
   （==），要么带上界（< / <= / ~=），只有 BARE_ALLOWLIST 里的
   例外可保持裸下界（当前为空——pypinyin 是 == 钉死，不是例外）。
2. backend/requirements.lock 存在，且每个包条目都带至少一个
   --hash=sha256（pip-compile --generate-hashes 的输出形态），
   且 requirements.txt 的每个直接依赖都在锁里。
3. backend/requirements-ci.lock（CI 附加依赖 zhconv/pytest 及其
   传递依赖）同样哈希齐全，且与生产锁共有的包版本必须一致
   （防止 CI 测的版本集合与生产锁脱节）。
4. （审计 A-125）每个直接依赖的生产锁版本必须落在 requirements.txt
   声明的范围内（>= / <= / > / < / == / != / ~= 求值）：范围抬升或
   收紧后忘重编锁时，门禁红灯而非绿灯放行。

只用标准库。用法（仓库根目录下）：
    python scripts/check_backend_deps_locked.py
    python scripts/check_backend_deps_locked.py --repo-root /path/to/repo
"""

import argparse
import re
import sys
from pathlib import Path

# 裸下界例外表：有意保留裸 >= 的直接依赖放这里，并写清理由。
# 当前为空。注意：== 钉死的包（如 pypinyin）天然通过，不需要进表。
BARE_ALLOWLIST = frozenset()

_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)$")
# pip-compile 会把带 extra 的包写成 name[extra]==version 形态（如
# uvicorn[standard]、python-jose[cryptography]），解析时必须吃掉方括号。
_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;\\]+)")
_HASH_RE = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")


def normalize(name):
    """PEP 503 名称归一化：大小写不敏感，- _ . 统一成 -。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _strip_inline_comment(line):
    if "://" in line:
        return line
    return line.split("#", 1)[0].strip()


def _join_continuations(text):
    out = []
    buf = ""
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


def parse_requirements(path):
    """解析 requirements.txt，返回 [(归一化名, 原始行)]。"""
    reqs = []
    for line in _join_continuations(Path(path).read_text(encoding="utf-8")):
        line = _strip_inline_comment(line.strip())
        if not line or line.startswith(("-r ", "--", "-c ", "-e ", "-f ")):
            continue
        m = _NAME_RE.match(line)
        if not m:
            continue
        spec = m.group(3).split(";", 1)[0].strip()
        reqs.append((normalize(m.group(1)), spec, line))
    return reqs


def _spec_is_bounded(spec):
    """已钉死（== / ===）或带上界（< / <= / ~=）即视为有界。"""
    if not spec:
        return False
    ops = re.findall(r"(===|==|~=|>=|<=|>|<|!=)", spec)
    if any(op in ("==", "===", "<", "<=", "~=") for op in ops):
        return True
    return False


def _parse_version(version):
    """PEP 440 近似解析：剥离 epoch（N!）与 local 后缀（+...），
    每段取前导数字（如 0.115.0 → (0, 115, 0)）。只用于范围求值，
    不做严格合法性校验。"""
    version = version.strip()
    if "!" in version:
        version = version.split("!", 1)[1]
    version = version.split("+", 1)[0]
    parts = []
    for seg in version.split("."):
        m = re.match(r"(\d+)", seg)
        parts.append(int(m.group(1)) if m else 0)
    return tuple(parts)


def _pad(a, b):
    """两版本元组补齐到等长（缺段按 0 计，如 2.0 == 2.0.0）。"""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)), b + (0,) * (n - len(b))


def _version_satisfies(version, spec):
    """锁版本是否满足声明范围（逗号分隔的多条件取交集）。

    支持 >= / <= / > / < / == / != / ~=；== / != 带 .* 后缀时按
    前缀匹配；~= 按 PEP 440 展开为 >=x 且 ==x 前缀（去掉最后一段，
    如 ~=0.114.0 即 >=0.114.0 且 ==0.114.*）。空 spec 视为无约束
    （裸依赖另由有界检查判违规）；无法解析的约束按不满足处理
    （fail-closed，不让可疑范围蒙混过关）。
    """
    if not spec:
        return True
    ver = _parse_version(version)
    for clause in spec.split(","):
        clause = clause.strip()
        if not clause:
            continue
        m = re.match(r"(===|==|~=|>=|<=|>|<|!=)\s*(\S+)$", clause)
        if not m:
            return False
        op, req = m.group(1), m.group(2)
        if op == "===":
            if version.strip() != req:
                return False
            continue
        if op == "~=":
            base = _parse_version(req)
            if len(base) < 2:
                # 单段 ~= 无上界可展，退化为 >=。
                lo, vv = _pad(base, ver)
                if vv < lo:
                    return False
                continue
            lo, vv = _pad(base, ver)
            if vv < lo or vv[:len(base) - 1] != base[:-1]:
                return False
            continue
        if req.endswith(".*"):
            prefix = _parse_version(req[:-2])
            _, vv = _pad(prefix, ver)
            matched = vv[:len(prefix)] == prefix
            if op == "==" and not matched:
                return False
            if op == "!=" and matched:
                return False
            continue
        req_v = _parse_version(req)
        rv, vv = _pad(req_v, ver)
        if op == "==" and vv != rv:
            return False
        if op == "!=" and vv == rv:
            return False
        if op == ">=" and vv < rv:
            return False
        if op == "<=" and vv > rv:
            return False
        if op == ">" and vv <= rv:
            return False
        if op == "<" and vv >= rv:
            return False
    return True


def parse_lock(path):
    """解析 pip-compile --generate-hashes 锁文件。

    返回 {归一化名: {"version": str, "hashes": int, "marker": str}}。
    无哈希的条目 hashes=0（由调用方判违规）。
    """
    entries = {}
    current = None
    for raw in _join_continuations(Path(path).read_text(encoding="utf-8")):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_RE.match(line)
        if m and not raw.startswith((" ", "\t")):
            current = normalize(m.group(1))
            marker = ""
            rest = line[m.end():]
            if ";" in rest:
                marker = rest.split(";", 1)[1].strip()
            entries[current] = {"version": m.group(2), "hashes": 0,
                                "marker": marker}
            if "--hash=" in rest:
                entries[current]["hashes"] += len(
                    re.findall(r"--hash=sha256:[0-9a-f]{64}", rest))
            continue
        if current is not None and line.startswith("--hash="):
            if _HASH_RE.match(line):
                entries[current]["hashes"] += 1
    return entries


def check(repo_root):
    """返回 (violations: list[str], summary: str)。"""
    violations = []
    root = Path(repo_root)
    req_path = root / "backend" / "requirements.txt"
    lock_path = root / "backend" / "requirements.lock"
    ci_lock_path = root / "backend" / "requirements-ci.lock"

    reqs = parse_requirements(req_path)
    for name, spec, line in reqs:
        if name in BARE_ALLOWLIST:
            continue
        if not _spec_is_bounded(spec):
            violations.append(
                f"裸下界：{line}（直接依赖 {name} 只有下界、无上界、未钉死）")

    if not lock_path.exists():
        violations.append(f"缺生产锁文件：{lock_path}（请用 pip-compile 生成）")
        lock = {}
    else:
        lock = parse_lock(lock_path)
        for name, entry in sorted(lock.items()):
            if entry["hashes"] == 0:
                violations.append(
                    f"锁缺哈希：{name}=={entry['version']} 无 --hash")
        for name, _spec, line in reqs:
            if name not in lock:
                violations.append(f"锁缺直接依赖：{line} 未出现在生产锁中")
            elif not _version_satisfies(lock[name]["version"], _spec):
                violations.append(
                    f"范围漂移：{name} 锁版本 {lock[name]['version']} 不在 "
                    f"{_spec} 内，请重编锁")

    if not ci_lock_path.exists():
        violations.append(
            f"缺 CI 附加锁文件：{ci_lock_path}（请用 pip-compile 生成）")
        ci_lock = {}
    else:
        ci_lock = parse_lock(ci_lock_path)
        for name, entry in sorted(ci_lock.items()):
            if entry["hashes"] == 0:
                violations.append(
                    f"CI 锁缺哈希：{name}=={entry['version']} 无 --hash")
        for name, entry in sorted(ci_lock.items()):
            if name in lock and lock[name]["version"] != entry["version"]:
                violations.append(
                    f"CI/生产锁版本脱节：{name} 生产锁 "
                    f"{lock[name]['version']} vs CI 锁 {entry['version']}")

    summary = (
        f"直接依赖 {len(reqs)} 个，生产锁 {len(lock)} 个包，"
        f"CI 锁 {len(ci_lock)} 个包，违规 {len(violations)} 项")
    return violations, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=None,
                        help="仓库根目录（默认：脚本所在目录的上级）")
    args = parser.parse_args(argv)
    root = Path(args.repo_root) if args.repo_root else \
        Path(__file__).resolve().parent.parent
    violations, summary = check(root)
    if violations:
        print("❌ 后端依赖锁定门禁失败：")
        for v in violations:
            print(f"  - {v}")
        print(summary)
        return 1
    print(f"✅ 后端依赖锁定门禁通过：{summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
