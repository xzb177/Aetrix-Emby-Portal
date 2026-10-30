# -*- coding: utf-8 -*-
"""多节点健康检查与流量分配测试"""
import os
import sys
from unittest import mock

# 用相对路径定位模块，避免硬编码 /opt 路径
_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, '..', 'backend', 'emby_server', 'node_health.py')
_MOD_PATH = os.path.normpath(_MOD_PATH)

import importlib.util

def _load():
    # 每次加载前确保 sqlalchemy 被 mock，且不污染全局
    with mock.patch.dict(sys.modules, {
        'sqlalchemy': mock.MagicMock(),
        'sqlalchemy.orm': mock.MagicMock(),
    }):
        spec = importlib.util.spec_from_file_location('_nh_test', _MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

_nh = _load()


def test_config_constants():
    assert _nh.CONFIG_KEY == 'stream_nodes'
    assert _nh.HEALTH_CHECK_INTERVAL == 30


def test_is_healthy_defaults_true():
    assert _nh.is_healthy('http://never-checked:9999') is True


def test_health_state_transition():
    url = 'http://test-node:18001'
    _nh._node_health.pop(url, None)
    try:
        assert _nh.is_healthy(url) is True
        _nh._node_health[url] = {'healthy': False, 'last_check': 0, 'fail_count': 2}
        assert _nh.is_healthy(url) is False
        _nh._node_health[url] = {'healthy': True, 'last_check': 0, 'fail_count': 0}
        assert _nh.is_healthy(url) is True
    finally:
        _nh._node_health.pop(url, None)


def test_pick_node_single():
    db = mock.MagicMock()
    single = [{'url': 'http://127.0.0.1:8000', 'weight': 100, 'name': 'local'}]
    with mock.patch.object(_nh, 'healthy_nodes', return_value=single):
        assert _nh.pick_node(db)['url'] == 'http://127.0.0.1:8000'


def test_pick_node_excludes_unhealthy():
    db = mock.MagicMock()
    with mock.patch.object(_nh, 'healthy_nodes',
                           return_value=[{'url': 'http://n1:8000', 'weight': 50}]):
        for _ in range(10):
            assert _nh.pick_node(db)['url'] == 'http://n1:8000'


def test_pick_node_fallback():
    db = mock.MagicMock()
    all_nodes = [{'url': 'http://n1:8000', 'weight': 100}]
    with mock.patch.object(_nh, 'healthy_nodes', return_value=[]):
        with mock.patch.object(_nh, 'get_nodes', return_value=all_nodes):
            assert _nh.pick_node(db)['url'] == 'http://n1:8000'


def test_pick_node_weighted():
    db = mock.MagicMock()
    nodes = [
        {'url': 'http://heavy:8000', 'weight': 90},
        {'url': 'http://light:8000', 'weight': 10},
    ]
    with mock.patch.object(_nh, 'healthy_nodes', return_value=nodes):
        c = {'http://heavy:8000': 0, 'http://light:8000': 0}
        for _ in range(200):
            c[_nh.pick_node(db)['url']] += 1
    assert c['http://heavy:8000'] > c['http://light:8000'] * 3


def test_daemon_idempotent():
    _nh.start_health_daemon(lambda: mock.MagicMock())
    _nh.start_health_daemon(lambda: mock.MagicMock())


class _StopLoop(Exception):
    pass


def test_health_loop_completes_iteration_without_unboundlocal():
    """回归：_health_loop 一轮内不得抛 UnboundLocalError（曾每 30s 刷 warning）"""
    urls = ['http://n1:8000', 'http://n2:8000']
    for u in urls:
        _nh._node_health.pop(u, None)
    warnings = []
    nodes = [{'url': u, 'weight': 100, 'name': u} for u in urls]

    def fake_get_nodes(db):
        return nodes

    def fake_check_one(url):
        return url == 'http://n1:8000'

    def fake_sleep(_):
        raise _StopLoop()

    with mock.patch.object(_nh, 'get_nodes', side_effect=fake_get_nodes), \
         mock.patch.object(_nh, '_check_one', side_effect=fake_check_one), \
         mock.patch.object(_nh.time, 'sleep', side_effect=fake_sleep), \
         mock.patch.object(_nh.logger, 'warning', side_effect=lambda *a, **k: warnings.append(a)):
        try:
            _nh._health_loop(lambda: mock.MagicMock())
        except _StopLoop:
            pass

    try:
        bug_warnings = [w for w in warnings if '节点健康检查异常' in str(w[0])]
        assert not bug_warnings, f"health loop 不应报检查异常，实际: {bug_warnings}"
        assert _nh._node_health['http://n1:8000']['healthy'] is True
        assert _nh._node_health['http://n2:8000']['healthy'] is False
    finally:
        for u in urls:
            _nh._node_health.pop(u, None)


def test_health_loop_skips_nodes_without_url():
    """无 url 字段的节点配置不得炸掉循环"""
    for u in ['http://ok:8000']:
        _nh._node_health.pop(u, None)
    warnings = []
    nodes = [
        {'url': 'http://ok:8000', 'weight': 100},
        {'weight': 100},          # 缺 url
        'not-a-dict',            # 非 dict
    ]

    def fake_sleep(_):
        raise _StopLoop()

    with mock.patch.object(_nh, 'get_nodes', return_value=nodes), \
         mock.patch.object(_nh, '_check_one', return_value=True), \
         mock.patch.object(_nh.time, 'sleep', side_effect=fake_sleep), \
         mock.patch.object(_nh.logger, 'warning', side_effect=lambda *a, **k: warnings.append(a)):
        try:
            _nh._health_loop(lambda: mock.MagicMock())
        except _StopLoop:
            pass

    try:
        bug_warnings = [w for w in warnings if '节点健康检查异常' in str(w[0])]
        assert not bug_warnings, f"health loop 不应报检查异常，实际: {bug_warnings}"
        assert _nh._node_health['http://ok:8000']['healthy'] is True
    finally:
        _nh._node_health.pop('http://ok:8000', None)


def test_start_health_daemon_idempotent_under_concurrency():
    """r30 B1：8 线程并发调用 start_health_daemon 只新增一个 node-health 线程。

    （同文件既有 test_daemon_idempotent 会先起一个同名线程，故用增量断言。）
    计数口径说明：start_health_daemon 等到循环入口就绪事件才返回，故
    返回后新线程必已在 threading.enumerate() 中可见，立即断言无观测竞态。
    worker 异常单独断言，避免 barrier 破裂被误读成计数问题。"""
    import threading
    nh = _load()
    before = sum(1 for t in threading.enumerate() if t.name == "node-health")
    barrier = threading.Barrier(8)
    errors = []

    def _call():
        try:
            barrier.wait(timeout=10)
            nh.start_health_daemon(lambda: None)
        except Exception as e:  # noqa: BLE001 - 透传给断言
            errors.append(e)

    workers = [threading.Thread(target=_call) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)
    assert not any(w.is_alive() for w in workers)
    assert not errors, f"并发调用不应抛错，实际: {errors}"
    after = sum(1 for t in threading.enumerate() if t.name == "node-health")
    assert after == before + 1


def test_start_health_daemon_waits_for_loop_entry():
    """根治回归：start_health_daemon 必须等到循环线程入口才返回。

    伪造一个 start() 后不跑 target 的 Thread：旧实现会立刻返回（红），
    新实现会等到就绪超时才返回（绿）。超时由模块常量控制，测试中改小。"""
    import threading
    import time
    nh = _load()
    nh._DAEMON_START_TIMEOUT = 0.2

    class _NeverRunsThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    started = []
    orig_thread = threading.Thread

    class _RecordingThread(_NeverRunsThread):
        def start(self):
            started.append(True)

    with mock.patch.object(threading, 'Thread', _RecordingThread):
        t0 = time.monotonic()
        nh.start_health_daemon(lambda: None)
        elapsed = time.monotonic() - t0
    try:
        assert started, "应尝试启动线程"
        assert elapsed >= 0.15, f"应等到就绪超时才返回，实际仅 {elapsed:.3f}s"
    finally:
        threading.Thread = orig_thread
