"""合并容器启动器：API + Worker + EA 三进程同容器。

设计：
- 三个子进程：serve.py（API:8000）、backend.worker（扫描）、emby_api.main（EA:8001）
- SIGTERM/SIGINT 转发给所有子进程，优雅退出
- 任一子进程异常退出则记录日志；API 或 EA 退出视为致命，整个容器退出（让 Docker 重启）
- worker 不是关键进程（S5）：退出后按指数退避重启（5s→10s→…→5min），稳定运行超过
  WORKER_STABLE_SECONDS（默认 10 分钟）后退避计数清零；**worker 永远不会拉停整个容器**
  （旧实现累计重启 3 次就 sys.exit(1)，Redis 启动慢 20 秒就把播放全断了）
"""
import logging
import os
import signal
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
# 日志脱敏：密钥/令牌不得进日志（与 main.py / worker.py 同口径）。
# 本文件以 `python backend/run_all.py` 直跑时仓库根不在 sys.path，
# import 失败则先把仓库根补进去再重试。
try:
    from backend.log_redact import install_secret_redaction
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.log_redact import install_secret_redaction

install_secret_redaction()
logger = logging.getLogger("run_all")

CHILDREN = {
    "api": [sys.executable, "serve.py"],
    "worker": [sys.executable, "-m", "backend.worker"],
    "ea": [sys.executable, "-m", "emby_api.main"],
}

# 致命进程：退出则整个容器退出
CRITICAL = {"api", "ea"}

# 分离架构的流节点角色（AETRIX_ROLE=stream）：只起 EA 出流进程，
# 不起 API / worker / 后台任务。无状态（只读主库 + 本地缓存），可横向扩展。
# 由 docker-compose.stream-node.yml / deploy-streaming-node.sh 使用。
ROLE_CHILDREN = {
    "stream": ["ea"],
}
ROLE_CRITICAL = {
    "stream": {"ea"},
}

# 各子进程的 AETRIX_ROLE（不在表里的子进程沿用容器环境变量）。
#
# api 必须显式声明。容器 env 是 AETRIX_ROLE=all，而 main.py 只把 "api" 认成 API 角色
# （_is_api_role = _role == "api"）：all 会被当成「单体模式」，于是 serve.py 这个子进程
# 也在 lifespan 里把 janitor / enrich_worker / reminders / auto_scan /
# db_backup / chase_new 整套后台任务起一遍 —— 而 backend.worker 子进程同样起一套。
# 结果是同一套后台任务跑两份：补全 worker 变成 2×ENRICH_WORKERS 线程、两个互不相通的
# TMDB 令牌桶互相打架（容器日志里「补全 worker 启动」出现两次），扫描队列也一直停在
# 单体模式的进程内队列上，v2.41 的 api/worker 拆分从未真正生效。
#
# 声明成 api 之后，两层行为都回到拆分的本意：
#   1) main.py 的 `if not _is_api_role` 分支全部跳过，后台任务只由 worker 进程承担
#      （worker.py 的启动清单与 main.py 逐条对应，另加 Redis 扫描队列消费）；
#   2) scan_queue 走 API 分支：enqueue 推 Redis 交给 worker 执行，而不是在 API 进程里
#      就地起扫描线程（进程内队列是单体模式的旧行为）。
#
# worker / ea 不覆盖：只有 "api" 会被特殊对待 —— worker 靠「AETRIX_ROLE != api」走
# 进程内队列，正是它需要的；ea（emby_api）整棵树不读这个变量。
CHILD_ROLE = {"api": "api"}

procs = {}


def log(msg):
    # 走 logging（原 print 前缀 "[run_all]" 由 logger 名 run_all 承担）
    logger.info(str(msg))


def start(name):
    cmd = CHILDREN[name]
    env = os.environ.copy()
    role = CHILD_ROLE.get(name)
    if role:
        env["AETRIX_ROLE"] = role
    # 有效角色打进日志：上一次「三个子进程共用一个 all」正是从日志上看不出来的
    log("start %s: %s (AETRIX_ROLE=%s)" % (
        name, " ".join(cmd), env.get("AETRIX_ROLE") or "<未设置>"))
    procs[name] = subprocess.Popen(cmd, cwd="/app", env=env)


def stop_all():
    log("stopping all children")
    for name, p in procs.items():
        if p.poll() is None:
            log("  -> %s (pid=%s) SIGTERM" % (name, p.pid))
            try:
                p.terminate()
            except Exception as e:
                log("  -> %s terminate failed: %s" % (name, e))
    deadline = time.time() + 15
    for name, p in procs.items():
        try:
            remaining = max(0, deadline - time.time())
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            log("  -> %s timeout, SIGKILL" % name)
            p.kill()


def _env_seconds(name, default):
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        value = float(default)
    return value if value > 0 else float(default)


#: 非关键进程重启退避：首次等待、上限、稳定运行多久后计数清零（秒）
RESTART_BASE = _env_seconds("WORKER_RESTART_BASE", 5)
RESTART_MAX = _env_seconds("WORKER_RESTART_MAX", 300)
STABLE_SECONDS = _env_seconds("WORKER_STABLE_SECONDS", 600)


def restart_delay(attempt):
    """第 attempt 次（从 1 起）连续重启前要等多久：BASE × 2^(attempt-1)，封顶 MAX"""
    return min(RESTART_MAX, RESTART_BASE * (2 ** max(0, attempt - 1)))


class Supervisor:
    """子进程看护：关键进程退出 → 整容器退出；非关键进程退出 → 退避重启

    状态机抽出来是为了能脱离真实进程单测（见 tests/test_run_all_supervisor.py）。
    """

    def __init__(self, critical, starter=None, clock=time.monotonic):
        self.critical = set(critical)
        self.starter = starter or start
        self.clock = clock
        self.started_at = {}
        self.attempts = {}
        self.pending = {}  # name -> 计划重启的时间点

    def started(self, name):
        self.started_at[name] = self.clock()

    def tick(self, children):
        """检查一轮；返回 "exit" 表示应整容器退出，否则 None"""
        now = self.clock()
        for name, p in list(children.items()):
            if name in self.pending:
                continue
            rc = p.poll()
            if rc is None:
                continue
            log("%s exited (rc=%s)" % (name, rc))
            if name in self.critical:
                log("%s is critical, exiting container" % name)
                return "exit"
            uptime = now - self.started_at.get(name, now)
            if uptime >= STABLE_SECONDS:
                self.attempts[name] = 0  # 跑稳过了：这次算新的一轮
            self.attempts[name] = self.attempts.get(name, 0) + 1
            delay = restart_delay(self.attempts[name])
            self.pending[name] = now + delay
            log("%s ran %.0fs; restarting in %.0fs (attempt %d, api/ea unaffected)"
                % (name, uptime, delay, self.attempts[name]))
        for name, due in list(self.pending.items()):
            if now >= due:
                del self.pending[name]
                log("restarting %s..." % name)
                try:
                    self.starter(name)
                except Exception as e:  # noqa: BLE001 - 起不来就下一轮再退避
                    log("restart %s failed: %s" % (name, e))
                    self.attempts[name] = self.attempts.get(name, 0) + 1
                    self.pending[name] = now + restart_delay(self.attempts[name])
                    continue
                self.started(name)
        return None


def main():
    def _handler(signum, frame):
        stop_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    role = os.environ.get("AETRIX_ROLE", "")
    wanted = ROLE_CHILDREN.get(role, list(CHILDREN))
    critical = ROLE_CRITICAL.get(role, CRITICAL)
    if wanted != list(CHILDREN):
        log("role %r: only starting %s" % (role, ",".join(wanted)))
    sup = Supervisor(critical)
    for name in wanted:
        start(name)
        sup.started(name)

    while True:
        time.sleep(5)
        if sup.tick(procs) == "exit":
            stop_all()
            sys.exit(1)


if __name__ == "__main__":
    main()
