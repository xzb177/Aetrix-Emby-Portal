#!/usr/bin/env python3
"""backend/ 禁 print 检查：生产代码的输出必须走 logging，不得直打 print。

背景（360° 审计 A-084）：日志脱敏（backend/log_redact.py 的
SecretRedactFilter / SecretRedactFormatter）只挂在 logging 链路上，
print() 直出 stdout 进 docker logs，全程旁路脱敏——今天 backend/ 里的
print 内容确实没有密钥与 URL，但只要以后有人顺手写一句 print(url)，
凭据就明文落盘。本检查是那条隐患的自动化兜底：backend/ 里只许
白名单内的 print 存在，新增 print 一律拦下，改走模块 logger。

扫描口径：
- 只扫 backend/ 下的生产代码（.py）。tests/ 与 scripts/ 不在范围内：
  它们不是常驻进程的日志面，一次性脚本/测试的 stdout 是本职输出。
- 用 AST 找真正的 print() 调用（函数名恰为 print 的 Call），
  不误伤 fingerprint() 之类的同后缀函数名，也不误伤字符串里的字样。
- 白名单逐条写明文件:行号与理由；白名单条目若已对不上真实的 print
  （行号漂移或 print 已删除），同样报错，逼着同步更新白名单，
  不让白名单烂掉变成后门。

命中违规时打印 file:line 与处置提示并 exit 1；干净时 exit 0。
"""

import ast
import pathlib
import sys

# 允许保留 print 的位置 → 理由。改动这些文件导致行号漂移时，同步改这里。
WHITELIST: dict[tuple[str, int], str] = {
    ("backend/node_auth.py", 177): (
        "运维辅助入口：__main__ 的本职就是把节点共享密钥打印给主动执行它的"
        "运维看（值不是从日志里旁路出去的，是命令的交付物本身）"
    ),
    ("backend/emby_server/p115_sidecar.py", 475): (
        "独立 sidecar 进程 main() 的启动行：该进程以独立 CLI 形态运行，"
        "stdout 是它唯一的运维输出面；内容只有 sock 路径与协议名，无凭据"
    ),
}


def find_prints(root: pathlib.Path) -> dict[tuple[str, int], str]:
    """返回 {(相对路径, 行号): 源码行} —— backend/ 里全部真正的 print() 调用。"""
    backend_dir = root / "backend"
    found: dict[tuple[str, int], str] = {}
    for path in sorted(backend_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = path.relative_to(root).as_posix()
        lines = text.splitlines()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                src = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                found[(rel, node.lineno)] = src
    return found


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    found = find_prints(root)

    violations = {k: v for k, v in found.items() if k not in WHITELIST}
    stale = [k for k in WHITELIST if k not in found]

    ok = True
    if violations:
        ok = False
        print("backend/ 出现未授权的 print() 调用（必须改走 logger，脱敏只覆盖 logging 链路）：")
        for (rel, lineno), src in sorted(violations.items()):
            print(f"  {rel}:{lineno}: {src}")
        print("  处置：改用 logging.getLogger(__name__) 输出；确属运维辅助入口的，")
        print("        在 scripts/check_no_print_in_backend.py 的 WHITELIST 里逐条登记理由。")
    if stale:
        ok = False
        print("白名单条目已对不上真实 print（行号漂移或已删除），请同步更新 WHITELIST：")
        for rel, lineno in sorted(stale):
            print(f"  {rel}:{lineno}")
    if ok:
        print(f"backend/ print 检查通过：仅 {len(WHITELIST)} 处白名单 print，无未授权 print")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
