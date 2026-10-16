"""run_all.py 子进程角色：合并容器里同一套后台任务只能起一份。

真实事故（生产）：单容器部署（``backend/run_all.py`` 起 api/worker/ea 三个子进程）时，
容器 env 是 ``AETRIX_ROLE=all``，而 ``main.py`` 只把 ``"api"`` 认成 API 角色
（``_is_api_role = _role == "api"``）—— ``all`` 被当成「单体模式」，于是：

* ``serve.py``（api 子进程）在 lifespan 里把 janitor / probe_worker / enrich_worker /
  reminders / auto_scan / db_backup / chase_new 整套后台任务起了一遍；
* ``backend.worker`` 子进程**同样**起了一套。

结果是同一套后台任务跑两份：补全 worker 变成 ``2×ENRICH_WORKERS`` 线程、两个互不相通
的 TMDB 令牌桶互相打架（容器日志里「补全 worker 启动」出现两次），并且 ``scan_queue``
一直停在单体模式的进程内队列上 —— v2.41 的 api/worker 拆分从未真正生效。

为什么 CI 没拦住：``start()`` 的幂等只靠模块级 ``_worker_threads``（**进程内**），
跨进程无效；而三进程共用一个 ``all`` 从日志上看不出来。

这里钉住三件事：

1. ``run_all.start()`` 给 api 子进程显式注入 ``AETRIX_ROLE=api``；
2. worker / ea 沿用容器 env 不动（只有 ``"api"`` 会被特殊对待）；
3. ``main.py`` 角色门后面的每个后台任务，``worker.py`` 都必须也启动它 ——
   这是「api 子进程跳过这些启动」之所以安全的前提，少一个就是静默丢功能。
"""
import os
import re
from pathlib import Path

import pytest

from backend import run_all

_ROOT = Path(__file__).resolve().parent.parent
_SRC_MAIN = _ROOT / "backend" / "main.py"
_SRC_WORKER = _ROOT / "backend" / "worker.py"

# main.py 里被 `if not _is_api_role:` 门住的后台任务模块（worker 必须逐条接过）
_ROLE_GATED_TASKS = (
    "maintenance", "enrich_worker", "probe_worker",
    "reminders", "auto_scan", "db_backup", "change_watcher",
)


class _FakePopen:
    """记录 (cmd, cwd, env)，不真的起进程"""

    def __init__(self, cmd, cwd=None, env=None):
        self.cmd = cmd
        self.cwd = cwd
        self.env = dict(os.environ if env is None else env)

    def poll(self):
        return None


@pytest.fixture()
def children(monkeypatch):
    """把 Popen 换成记录器；返回 run_all.procs（start() 之后按名字取 env）"""
    monkeypatch.setattr(run_all, "procs", {})
    monkeypatch.setattr(
        run_all.subprocess, "Popen",
        lambda cmd, cwd=None, env=None: _FakePopen(cmd, cwd=cwd, env=env),
    )
    return run_all.procs


# ---------- 1. 角色注入 ----------

def test_api_child_declares_api_role(monkeypatch, children):
    """合并部署（容器 env = all）下，api 子进程必须显式声明成 api"""
    monkeypatch.setenv("AETRIX_ROLE", "all")
    run_all.start("api")
    assert children["api"].env["AETRIX_ROLE"] == "api"
    assert children["api"].cwd == "/app"


@pytest.mark.parametrize("name", ["worker", "ea"])
def test_worker_and_ea_keep_container_env(monkeypatch, children, name):
    """worker / ea 沿用容器 env 不动：worker 靠「AETRIX_ROLE != api」走进程内队列，
    ea（emby_api）整棵树不读这个变量"""
    monkeypatch.setenv("AETRIX_ROLE", "all")
    run_all.start(name)
    assert children[name].env["AETRIX_ROLE"] == "all"


def test_api_child_env_is_otherwise_inherited(monkeypatch, children):
    """只覆盖 AETRIX_ROLE，其余环境变量照旧继承（子进程与今天的行为一致）"""
    monkeypatch.setenv("AETRIX_ROLE", "all")
    monkeypatch.setenv("AETRIX_TEST_MARKER", "kept")
    run_all.start("api")
    assert children["api"].env["AETRIX_TEST_MARKER"] == "kept"
    assert children["api"].cmd == run_all.CHILDREN["api"]


def test_role_table_only_overrides_api(monkeypatch, children):
    """契约：三个子进程都在册，**只有** api 被覆盖角色"""
    assert set(run_all.CHILDREN) == {"api", "worker", "ea"}
    assert run_all.CHILD_ROLE == {"api": "api"}
    # 全部起一遍，确认没有任何子进程被意外改角色
    monkeypatch.setenv("AETRIX_ROLE", "all")
    for name in run_all.CHILDREN:
        run_all.start(name)
    assert children["api"].env["AETRIX_ROLE"] == "api"
    assert children["worker"].env["AETRIX_ROLE"] == "all"
    assert children["ea"].env["AETRIX_ROLE"] == "all"


# ---------- 2. main.py 的角色判定口径（run_all 依赖它） ----------

def test_main_treats_only_api_role_as_api_process():
    """main.py 只有 ``"api"`` 才跳过后台任务 —— run_all 的注入依赖这个口径"""
    src = _SRC_MAIN.read_text(encoding="utf-8")
    assert '_role = os.getenv("AETRIX_ROLE", "").strip().lower()' in src
    assert '_is_api_role = (_role == "api")' in src
    assert "if not _is_api_role:" in src


# ---------- 3. 安全前提：worker 必须接过角色门后的每一个后台任务 ----------

@pytest.mark.parametrize("module", _ROLE_GATED_TASKS)
def test_worker_starts_every_role_gated_task(module):
    """main.py 角色门后面的每个后台任务，worker.py 都必须也启动它

    少一个就是静默丢功能（例如把 enrich 改成 api 专属、worker 却没起 probe），
    而这一改动正是靠「worker 全接过」才安全的。
    """
    main_src = _SRC_MAIN.read_text(encoding="utf-8")
    worker_src = _SRC_WORKER.read_text(encoding="utf-8")

    assert f"import {module}" in main_src, f"main.py 不再导入 {module}，请同步本测试"
    assert f"import {module}" in worker_src, f"worker.py 没有接过 {module}"
    assert re.search(rf"\b{module}\.\w+\(", worker_src), (
        f"worker.py 导入了 {module} 但没有调用它的启动函数"
    )
