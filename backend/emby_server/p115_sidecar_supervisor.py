"""115 sidecar 显式拉起：随服务启动拉起 sidecar 子进程（生产接线）。

背景（#1251 只给了进程与契约）：宿主不自动拉起，部署者须手动起
``python -m backend.emby_server.p115_sidecar``；本模块把“手动起”改成
服务启动时显式拉起——开闸（``PAN115_SIDECAR_ENABLED`` 默认开，见
``mount_sidecar.sidecar_enabled``）的服务在启动时就把 sidecar 子进程
拉起来并握手验活，不再等第一次用到 115 挂载时才发现进程不在。

拉起语义（与 #1251 的失败语义一致，不静默改行为）：

- 关闸时什么都不做（回落到进程内 ``Pan115Mount`` 是分发层的决定，
  见 ``mounts.build_provider``；本模块不替调用方选路）。
- 开闸时：先探活——socket 已有健康 sidecar 在服役就复用它，不重复拉起
  （同一台机器上 api/worker 共用 socket 目录时，两进程不会为抢 socket
  互踢；容器部署下各容器各有自己的 socket 目录，本分支基本走不到）。
- 拉起失败（子进程早退、握手超时、socket 目录建不出）记 ERROR 日志、
  ``status()`` 里留 ``last_error``，并返回 False——**不阻断服务启动**：
  115 之外的挂载与服务本身仍可用；115 挂载在首次使用时按既有语义
  响亮报错（``MountError``，不静默回落进程内）。回滚 = 设
  ``PAN115_SIDECAR_ENABLED=0`` 后重启，见 env.example。
- 只停自己拉起的子进程：复用的外部 sidecar 不碰（那是部署者的进程）；
  服务退出时 ``stop()`` 做 best-effort 终止。

诊断：服务日志里搜 ``p115 sidecar``——拉起成功有 pid + socket 行，
失败有命令 + socket + returncode/超时 + 排查提示；sidecar 自己的
``listening on ...`` 行也进同一份服务日志（子进程输出直接继承）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

PROVIDER = "p115"
DEFAULT_STARTUP_TIMEOUT_SEC = 10.0
_PROBE_TIMEOUT_SEC = 2.0
_POLL_INTERVAL_SEC = 0.2


def startup_timeout_sec() -> float:
    """拉起等待上限（秒；env ``PAN115_SIDECAR_STARTUP_TIMEOUT_SEC``，默认 10）。"""
    try:
        return max(1.0, float(os.getenv(
            "PAN115_SIDECAR_STARTUP_TIMEOUT_SEC",
            str(DEFAULT_STARTUP_TIMEOUT_SEC))))
    except (TypeError, ValueError):
        return DEFAULT_STARTUP_TIMEOUT_SEC


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.spawned = False
        self.reused = False
        self.socket_path: str | None = None
        self.last_error: str | None = None


_STATE = _State()


def _sidecar_enabled() -> bool:
    # 懒导入：mount_sidecar 依赖 mounts，顶层导入会把重链拉进来；
    # 本模块被 main/worker 启动口调用，懒导入也让单测可 monkeypatch。
    from backend.emby_server.mount_sidecar import sidecar_enabled
    return sidecar_enabled()


def _default_socket_path() -> str:
    from backend.emby_server.provider_runtime import default_socket_path
    return default_socket_path(PROVIDER)


def _handshake_ok(socket_path: str) -> bool:
    """对 socket 做一次 manifest 握手：协议/provider/能力/扩展任一不符即 False。"""
    try:
        from backend.emby_server.provider_runtime import (
            P115_EXTENSIONS,
            ProviderRuntimeClient,
        )
        client = ProviderRuntimeClient(
            PROVIDER, socket_path, timeout_sec=_PROBE_TIMEOUT_SEC)
        client.handshake(P115_EXTENSIONS)
        return True
    except Exception:  # noqa: BLE001 — 探活失败就是“不在”，细节由调用方日志交代
        return False


def _repo_root() -> str:
    # 本文件在 <root>/backend/emby_server/ 下；子进程须以仓库根为 cwd，
    # 否则 `python -m backend.emby_server.p115_sidecar` 找不到包。
    # 三级：文件 → emby_server → backend → 仓库根。
    here = os.path.abspath(__file__)
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def _default_argv(socket_path: str) -> list[str]:
    return [sys.executable, "-m", "backend.emby_server.p115_sidecar",
            "--sock", socket_path]


def ensure_started(argv: list[str] | None = None,
                   socket_path: str | None = None,
                   timeout_sec: float | None = None) -> bool:
    """显式拉起 115 sidecar（幂等；多进程/多线程重复调是安全的）。

    返回 True = “无需担心”（关闸，或 sidecar 已在服役——自拉或复用）；
    返回 False = “已开闸但拉起失败”（ERROR 已记日志，``status()`` 有
    ``last_error``；服务继续启动，115 挂载首次使用时响亮报错）。
    ``argv`` 只供测试注入假命令，生产路径永远用默认命令。
    """
    if not _sidecar_enabled():
        logger.info("115 sidecar 未启用（PAN115_SIDECAR_ENABLED=0），不拉起 sidecar 进程")
        return True
    sock = socket_path or _default_socket_path()
    timeout = timeout_sec if timeout_sec is not None else startup_timeout_sec()
    with _STATE.lock:
        # 已拉起且仍活着 + 握手仍通：幂等直接返回。
        if _STATE.spawned and _STATE.proc is not None \
                and _STATE.proc.poll() is None and _handshake_ok(sock):
            return True
        if _STATE.reused and _handshake_ok(sock):
            return True
        # 先探活：已有健康 sidecar 在服役就复用，不重复拉起。
        if _handshake_ok(sock):
            _STATE.proc = None
            _STATE.spawned = False
            _STATE.reused = True
            _STATE.socket_path = sock
            _STATE.last_error = None
            logger.info("115 sidecar 已在运行，复用现有进程（socket=%s）", sock)
            return True
        # 建 socket 目录：建不出就是可诊断的部署错误（权限/只读挂载）。
        sock_dir = os.path.dirname(sock)
        try:
            os.makedirs(sock_dir, exist_ok=True)
        except OSError as exc:
            _STATE.last_error = (
                f"建 sidecar socket 目录失败：{sock_dir}（{exc}）——"
                "检查该目录的写权限或 PROVIDER_RUNTIME_SOCK_DIR 是否指向可写路径")
            logger.error("115 sidecar 拉起失败：%s", _STATE.last_error)
            return False
        cmd = list(argv) if argv is not None else _default_argv(sock)
        try:
            proc = subprocess.Popen(cmd, cwd=_repo_root(), start_new_session=True)
        except OSError as exc:
            _STATE.last_error = (
                f"sidecar 子进程起不来：命令={cmd}（{exc}）——"
                "检查 python 可执行文件与 backend 包是否完整")
            logger.error("115 sidecar 拉起失败：%s", _STATE.last_error)
            return False
        _STATE.proc = proc
        _STATE.spawned = True
        _STATE.reused = False
        _STATE.socket_path = sock
        _STATE.last_error = None
        logger.info("115 sidecar 拉起中：pid=%s socket=%s 命令=%s",
                    proc.pid, sock, " ".join(cmd))
        deadline = time.monotonic() + timeout
        while True:
            rc = proc.poll()
            if rc is not None:
                # 子进程早退：先看是不是 sibling 赢了抢 socket 的竞态。
                if _handshake_ok(sock):
                    _STATE.proc = None
                    _STATE.spawned = False
                    _STATE.reused = True
                    _STATE.last_error = None
                    logger.info(
                        "115 sidecar 已由另一进程拉起，复用现有进程（socket=%s）", sock)
                    return True
                _STATE.last_error = (
                    f"sidecar 子进程启动后退出：returncode={rc}，命令={' '.join(cmd)}，"
                    f"socket={sock}——看服务日志里 sidecar 自身的 traceback（子进程输出"
                    "直接进服务日志），或手动跑该命令复现")
                logger.error("115 sidecar 拉起失败：%s", _STATE.last_error)
                return False
            if _handshake_ok(sock):
                logger.info("115 sidecar 已就绪：pid=%s socket=%s", proc.pid, sock)
                return True
            if time.monotonic() >= deadline:
                _STATE.last_error = (
                    f"sidecar 拉起超时（{timeout:g}s）：pid={proc.pid}，命令={' '.join(cmd)}，"
                    f"socket={sock}——进程还在但握手未通：看服务日志里 sidecar 是否已打印 "
                    "listening 行；若 socket 目录被旧 socket 占住，先确认无其他 sidecar "
                    "在跑再清掉旧 socket 重启")
                logger.error("115 sidecar 拉起失败：%s", _STATE.last_error)
                return False
            time.sleep(_POLL_INTERVAL_SEC)


def stop(timeout_sec: float = 5.0) -> None:
    """停自己拉起的子进程（best-effort；复用的外部 sidecar 不碰）。"""
    with _STATE.lock:
        proc = _STATE.proc if _STATE.spawned else None
        _STATE.proc = None
        _STATE.spawned = False
        _STATE.reused = False
        _STATE.last_error = None
        _STATE.socket_path = None
    if proc is None or proc.poll() is not None:
        return
    logger.info("正在停止 115 sidecar 子进程：pid=%s", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=timeout_sec)
    except Exception:  # noqa: BLE001 — 退出收尾失败不影响服务关闭
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def status() -> dict:
    """拉起状态（诊断用，不抛异常）。"""
    with _STATE.lock:
        proc = _STATE.proc
        spawned = _STATE.spawned
        reused = _STATE.reused
        last_error = _STATE.last_error
        sock = _STATE.socket_path
    if not _sidecar_enabled():
        mode = "disabled"
    elif spawned and proc is not None and proc.poll() is None:
        mode = "spawned"
    elif reused:
        mode = "reused"
    else:
        mode = "not_running"
    return {
        "enabled": _sidecar_enabled(),
        "mode": mode,
        "pid": proc.pid if (spawned and proc is not None) else None,
        "socket_path": sock,
        "alive": bool(spawned and proc is not None and proc.poll() is None),
        "last_error": last_error,
    }


def _reset_state_for_tests() -> None:
    """单测专用：清掉模块级拉起状态（不停外部进程，测试自己负责收尾）。"""
    with _STATE.lock:
        _STATE.proc = None
        _STATE.spawned = False
        _STATE.reused = False
        _STATE.socket_path = None
        _STATE.last_error = None


__all__ = ["ensure_started", "stop", "status", "startup_timeout_sec"]
