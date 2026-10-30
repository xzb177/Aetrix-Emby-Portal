"""多节点健康检查与流量分配（预留）

- 节点配置：SystemConfig['stream_nodes']，JSON 列表
  [{"url": "http://127.0.0.1:8000", "weight": 100, "name": "本机"}]
- 健康检查：每 30 秒 GET 各节点 /api/health，不健康自动摘除、恢复自动加回
- pick_node()：按权重在健康节点中选择；单节点时直接返回本机
- 预留设计：以后加节点只改配置，不用改代码
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

CONFIG_KEY = "stream_nodes"
HEALTH_CHECK_INTERVAL = 30
HEALTH_TIMEOUT = 5

_node_health: dict = {}
_lock = threading.Lock()
_daemon_started = False
_daemon_lock = threading.Lock()
# 循环线程入口置位；start_health_daemon 等到它才返回，保证“已启动”
# 在 threading.enumerate() 中立即可见（新线程向 _active 的注册发生在
# target 运行之前，start() 返回时未必已完成——直接数线程是观测竞态，
# 曾致并发幂等测试在 CI 全量负载下抖动）。
_daemon_ready = threading.Event()
_DAEMON_START_TIMEOUT = 10


def get_nodes(db: Session) -> list:
    """读取节点列表；无配置时返回默认本机单节点"""
    from backend.models import SystemConfig
    row = db.query(SystemConfig).filter(SystemConfig.key == CONFIG_KEY).first()
    if row and row.value:
        try:
            nodes = json.loads(row.value)
            if isinstance(nodes, list) and nodes:
                return nodes
        except (json.JSONDecodeError, TypeError):
            logger.warning("stream_nodes 配置解析失败，使用默认节点")
    return [{"url": "http://127.0.0.1:8000", "weight": 100, "name": "本机"}]


def set_nodes(db: Session, nodes: list) -> None:
    """写入节点配置"""
    from backend.models import SystemConfig
    row = db.query(SystemConfig).filter(SystemConfig.key == CONFIG_KEY).first()
    value = json.dumps(nodes, ensure_ascii=False)
    if row:
        row.value = value
    else:
        db.add(SystemConfig(key=CONFIG_KEY, value=value, description="流媒体节点列表（多节点负载均衡预留）"))
    db.commit()


def _check_one(url: str) -> bool:
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


def _health_loop(get_db):
    _daemon_ready.set()
    while True:
        try:
            db = get_db()
            try:
                nodes = get_nodes(db)
            finally:
                db.close()
            urls = [n["url"] for n in nodes if isinstance(n, dict) and n.get("url")]
            _probe_results = {}
            if urls:
                with ThreadPoolExecutor(max_workers=min(len(urls), 10), thread_name_prefix="health-probe") as ex:
                    _fut2url = {ex.submit(_check_one, u): u for u in urls}
                    for _fut in as_completed(_fut2url):
                        _u = _fut2url[_fut]
                        try:
                            _probe_results[_u] = _fut.result()
                        except Exception:
                            _probe_results[_u] = False
            for url, healthy in _probe_results.items():
                with _lock:
                    prev = _node_health.get(url, {})
                    was = prev.get("healthy", True)
                    _node_health[url] = {
                        "healthy": healthy,
                        "last_check": time.time(),
                        "fail_count": 0 if healthy else prev.get("fail_count", 0) + 1,
                    }
                    if was and not healthy:
                        logger.warning("节点不健康，已摘除: %s", url)
                    elif not was and healthy:
                        logger.info("节点恢复，已加回: %s", url)
        except Exception as e:
            logger.warning("节点健康检查异常: %s", e)
        time.sleep(HEALTH_CHECK_INTERVAL)


def start_health_daemon(get_db) -> None:
    """启动健康检查后台线程（幂等）"""
    global _daemon_started
    with _daemon_lock:
        if _daemon_started:
            return
        _daemon_started = True
    threading.Thread(target=_health_loop, args=(get_db,), daemon=True, name="node-health").start()
    if not _daemon_ready.wait(timeout=_DAEMON_START_TIMEOUT):
        # 只告警不抛错：守护线程缺席不得拖垮服务启动
        logger.warning("节点健康检查线程未在 %ss 内就绪", _DAEMON_START_TIMEOUT)
    logger.info("节点健康检查已启动")


def is_healthy(url: str) -> bool:
    """节点是否健康；从未检查过时默认健康，避免启动初期误摘"""
    with _lock:
        s = _node_health.get(url)
    return True if s is None else s.get("healthy", True)


def healthy_nodes(db: Session) -> list:
    """返回健康节点列表"""
    return [n for n in get_nodes(db) if is_healthy(n.get("url", ""))]


def pick_node(db: Session):
    """按权重从健康节点选一个；单节点直接返回；全不健康时降级返回全部"""
    nodes = healthy_nodes(db)
    if not nodes:
        nodes = get_nodes(db)
        if not nodes:
            return None
    if len(nodes) == 1:
        return nodes[0]
    total = sum(n.get("weight", 100) for n in nodes)
    r = random.uniform(0, total)
    upto = 0
    for n in nodes:
        upto += n.get("weight", 100)
        if r <= upto:
            return n
    return nodes[-1]
