"""A-084 回归：backend/ 生产代码禁 print，输出走 logging 并被脱敏覆盖。

背景：print() 直出 stdout 进 docker logs，旁路 log_redact 的
SecretRedactFilter / SecretRedactFormatter。本文件锁三件事：
1. database.py / run_all.py 里不再有 print() 调用，且有模块 logger；
2. run_all.log() 的子进程日志确实经 logging 发出；
3. database 的迁移日志经 logging 发出时，套上生产同款脱敏
   （filter + formatter，装配手法与 tests/test_log_redact.py 一致）
   后密钥不落明文、正常文案不被误伤。
门禁本体在 scripts/check_no_print_in_backend.py，这里顺带断言它对当前树 exit 0。
"""
import ast
import io
import logging
import os
import pathlib
import subprocess
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend import database  # noqa: E402
from backend import run_all  # noqa: E402
from backend.log_redact import SecretRedactFilter, SecretRedactFormatter  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _print_calls(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]


def test_database_and_run_all_have_no_print():
    assert _print_calls(ROOT / "backend" / "database.py") == []
    assert _print_calls(ROOT / "backend" / "run_all.py") == []
    assert isinstance(database.logger, logging.Logger)
    assert database.logger.name == "backend.database"
    assert isinstance(run_all.logger, logging.Logger)


def test_run_all_log_goes_through_logging(caplog):
    with caplog.at_level(logging.INFO, logger="run_all"):
        run_all.log("worker exited (rc=3)")
    msgs = [r.getMessage() for r in caplog.records if r.name == "run_all"]
    assert msgs == ["worker exited (rc=3)"]


def test_database_log_is_covered_by_redaction():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(SecretRedactFormatter("%(name)s - %(levelname)s - %(message)s"))
    filt = SecretRedactFilter()
    lg = database.logger
    old_level = lg.level
    lg.addHandler(handler)
    lg.addFilter(filt)
    lg.setLevel(logging.INFO)
    try:
        lg.info("  🔧 已迁移: demo_table.demo_col (TEXT)")
        lg.info("upstream https://example.test/x?api_key=FAKESECRET123&y=1")
    finally:
        lg.removeHandler(handler)
        lg.removeFilter(filt)
        lg.setLevel(old_level)
    out = buf.getvalue()
    # 正常迁移文案原样通过（文案等价，不误伤）
    assert "🔧 已迁移: demo_table.demo_col (TEXT)" in out
    # 密钥被脱敏，不落明文
    assert "FAKESECRET123" not in out
    assert "api_key=***" in out


def test_gate_script_passes_on_current_tree():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_no_print_in_backend.py"), str(ROOT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
