"""人机验证作用域围栏（2026-11-06 用户亲令）：只许网站登录/注册/签到与后台登录。

播放器 / Emby 程序化登录做不了人机验证：谁把 captcha 守卫接进 EA / Emby
协议路径，播放器就会被 400 挡死、直接断播。这个测试把接线点钉死在允许
清单里——新增接线点必须先改这份清单（等于强制人工复核作用域），不许静默扩大。
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

REPO = Path(__file__).resolve().parent.parent

# 允许调用守卫的文件（相对仓库根）：门户登录/注册、网站签到、管理后台登录，
# 以及守卫本体（captcha.py 内部 guard 调 verify_request 不算外部接线）。
ALLOWED_GUARD_FILES = {
    "backend/api/emby_portal.py",
    "backend/api/economy.py",
    "backend/api/admin_auth.py",
    "backend/integrations/captcha.py",
}

_GUARD_CALL = re.compile(r"(?:integrations_)?captcha\.(?:guard|verify_request)\s*\(")
_CAPTCHA_IMPORT = re.compile(r"integrations(?:\.captcha| import captcha)")


def _py_files(*roots: str):
    for root in roots:
        base = REPO / root
        if base.exists():
            yield from base.rglob("*.py")


def test_captcha_guard_wired_only_at_allowed_sites():
    offenders = {}
    for path in _py_files("backend", "emby_api"):
        rel = path.relative_to(REPO).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _GUARD_CALL.search(text) and rel not in ALLOWED_GUARD_FILES:
            offenders[rel] = "调用了 captcha 守卫"
    assert not offenders, (
        "人机验证守卫被接到了允许清单之外的位置（播放器路径加验证会断播）："
        f"{offenders}；确需新增点位先改 tests/test_captcha_scope.py 的清单并复核作用域"
    )
    # 允许清单里的业务接线点必须真的还在（防守卫被整体摘掉后测试假绿）
    for rel in ("backend/api/emby_portal.py", "backend/api/economy.py",
                "backend/api/admin_auth.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert _GUARD_CALL.search(text), f"{rel} 的 captcha 守卫接线丢了"


def test_player_paths_do_not_reference_captcha_at_all():
    """EA / Emby 协议侧（emby_api、backend/emby_server）连引用都不许有。"""
    offenders = []
    for path in _py_files("emby_api", "backend/emby_server"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _CAPTCHA_IMPORT.search(text) or _GUARD_CALL.search(text):
            offenders.append(path.relative_to(REPO).as_posix())
    assert not offenders, f"播放器侧引用了人机验证：{offenders}"
