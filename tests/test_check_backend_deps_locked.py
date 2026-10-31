"""scripts/check_backend_deps_locked.py 的回归测试（审计 A-078 兜底）。

用临时目录搭“假仓库根”跑脚本真进程，不依赖后端模块（conftest 的
autouse 缓存夹具在 CI 全量里照常跑；本地单跑可用 --noconftest）。
"""

import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / \
    "check_backend_deps_locked.py"


def _run(root):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(root)],
        capture_output=True, text=True, timeout=60)


def _write(root, requirements, lock=None, ci_lock=None):
    backend = Path(root) / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "requirements.txt").write_text(
        textwrap.dedent(requirements), encoding="utf-8")
    if lock is not None:
        (backend / "requirements.lock").write_text(
            textwrap.dedent(lock), encoding="utf-8")
    if ci_lock is not None:
        (backend / "requirements-ci.lock").write_text(
            textwrap.dedent(ci_lock), encoding="utf-8")


_GOOD_LOCK = """\
    # pip-compile --generate-hashes 产物（节选）
    fastapi==0.115.0 \\
        --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    pypinyin==0.55.0 \\
        --hash=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    """

_GOOD_CI_LOCK = """\
    pytest==8.3.0 \\
        --hash=sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
    """


def test_bare_lower_bound_fails(tmp_path):
    """旧形态：裸 >= 且无锁文件 → 门禁必须红。"""
    _write(tmp_path, """\
        fastapi>=0.104.0
        pypinyin==0.55.0
        """)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "裸下界" in proc.stdout
    assert "缺生产锁文件" in proc.stdout


def test_pinned_and_bounded_passes(tmp_path):
    """修后形态：钉死/带上界＋两锁哈希齐全 → 绿。"""
    _write(tmp_path, """\
        fastapi>=0.104.0,<1
        pypinyin==0.55.0
        """, lock=_GOOD_LOCK, ci_lock=_GOOD_CI_LOCK)
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout
    assert "门禁通过" in proc.stdout


def test_lock_missing_hash_fails(tmp_path):
    """锁条目无 --hash → 红。"""
    _write(tmp_path, """\
        fastapi>=0.104.0,<1
        """, lock="fastapi==0.115.0\n", ci_lock=_GOOD_CI_LOCK)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "锁缺哈希" in proc.stdout


def test_ci_lock_version_drift_fails(tmp_path):
    """CI 锁与生产锁共有包版本不一致 → 红。"""
    _write(tmp_path, """\
        fastapi>=0.104.0,<1
        """, lock=_GOOD_LOCK, ci_lock="""\
        fastapi==0.116.0 \\
            --hash=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
        """)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "版本脱节" in proc.stdout


def test_direct_dep_missing_from_lock_fails(tmp_path):
    """直接依赖未进生产锁 → 红。"""
    _write(tmp_path, """\
        fastapi>=0.104.0,<1
        orjson>=3.9.0,<4
        """, lock=_GOOD_LOCK, ci_lock=_GOOD_CI_LOCK)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "锁缺直接依赖" in proc.stdout


def test_lock_version_below_lower_bound_fails(tmp_path):
    """审计 A-125：范围下界抬过锁版本（忘重编锁）→ 红。"""
    _write(tmp_path, """\
        fastapi>=0.200.0,<1
        pypinyin==0.55.0
        """, lock=_GOOD_LOCK, ci_lock=_GOOD_CI_LOCK)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "范围漂移" in proc.stdout


def test_lock_version_above_upper_bound_fails(tmp_path):
    """审计 A-125：范围上界收到锁版本之下（忘重编锁）→ 红。"""
    _write(tmp_path, """\
        fastapi>=0.104.0,<0.115.0
        pypinyin==0.55.0
        """, lock=_GOOD_LOCK, ci_lock=_GOOD_CI_LOCK)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "范围漂移" in proc.stdout


def test_lock_version_within_range_passes(tmp_path):
    """审计 A-125：锁版本在声明范围内 → 绿。"""
    _write(tmp_path, """\
        fastapi>=0.104.0,<1
        pypinyin==0.55.0
        """, lock=_GOOD_LOCK, ci_lock=_GOOD_CI_LOCK)
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout
    assert "门禁通过" in proc.stdout


def test_tilde_equal_range_fails(tmp_path):
    """审计 A-125：~= 按 PEP 440 展开（~=0.114.0 即 >=0.114.0 且 ==0.114.*），
    锁 0.115.0 不在其中 → 红。"""
    _write(tmp_path, """\
        fastapi~=0.114.0
        pypinyin==0.55.0
        """, lock=_GOOD_LOCK, ci_lock=_GOOD_CI_LOCK)
    proc = _run(tmp_path)
    assert proc.returncode != 0
    assert "范围漂移" in proc.stdout
