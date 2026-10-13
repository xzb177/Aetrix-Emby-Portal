# -*- coding: utf-8 -*-
"""cpu_budget：后台并发按 CPU 自适应，永远给前台留一核"""
import os


def _mock_cpus(monkeypatch, n):
    from backend.emby_server import cpu_budget
    # lru_cache 要清掉，否则 mock 不生效
    cpu_budget.cpu_count.cache_clear()
    monkeypatch.setattr(cpu_budget, "cpu_count", lambda: n)
    for k in ("SCAN_WORKERS", "PROBE_WORKERS", "THUMBNAIL_WORKERS", "ENRICH_WORKERS"):
        monkeypatch.delenv(k, raising=False)


def test_default_is_half_cpus(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 4)
    assert cpu_budget.background_workers("SCAN_WORKERS") == 2  # 4 // 2


def test_manual_capped_at_cpus_minus_one(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 4)
    monkeypatch.setenv("SCAN_WORKERS", "8")
    assert cpu_budget.background_workers("SCAN_WORKERS") == 3  # min(8, 4-1)


def test_manual_small_value_kept(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 4)
    monkeypatch.setenv("SCAN_WORKERS", "2")
    assert cpu_budget.background_workers("SCAN_WORKERS") == 2


def test_invalid_value_falls_back_to_default(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 4)
    monkeypatch.setenv("SCAN_WORKERS", "abc")
    assert cpu_budget.background_workers("SCAN_WORKERS") == 2


def test_single_core_still_gets_one(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 1)
    assert cpu_budget.background_workers("SCAN_WORKERS") == 1


def test_max_cap_respected(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 64)
    assert cpu_budget.background_workers("SCAN_WORKERS") == 16  # 默认上限 16
    monkeypatch.setenv("SCAN_WORKERS", "100")
    assert cpu_budget.background_workers("SCAN_WORKERS") == 16


def test_capped_workers_keeps_custom_default(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 4)
    # 未配置时用自定义默认
    assert cpu_budget.capped_workers("THUMBNAIL_WORKERS", 1, max_cap=3) == 1
    # 手动配置时截断
    monkeypatch.setenv("THUMBNAIL_WORKERS", "8")
    assert cpu_budget.capped_workers("THUMBNAIL_WORKERS", 1, max_cap=3) == 3


# ==================== io_bound 模式（v2.57.0 提速包 item1） ====================
# IO 密集型后台任务（线程 90% 时间等网络）不按 CPU 截断：未配置默认 8，
# 手动配置只按 max_cap 截断。旧调用（不传 io_bound）语义不变，见上文用例。


def test_io_bound_default_is_8_even_on_2_cores(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 2)
    # 2 核机：旧模式默认 1、手动 8 也被截成 1；io_bound 下默认 8
    assert cpu_budget.background_workers("ENRICH_WORKERS", io_bound=True) == 8
    assert cpu_budget.background_workers("ENRICH_WORKERS") == 1


def test_io_bound_manual_value_not_truncated_by_cpu(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 2)
    monkeypatch.setenv("ENRICH_WORKERS", "8")
    assert cpu_budget.background_workers("ENRICH_WORKERS", io_bound=True) == 8
    # 旧模式仍按 CPU 截断
    assert cpu_budget.background_workers("ENRICH_WORKERS") == 1


def test_io_bound_respects_max_cap(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 2)
    monkeypatch.setenv("ENRICH_WORKERS", "100")
    assert cpu_budget.background_workers("ENRICH_WORKERS", io_bound=True) == 16
    assert cpu_budget.background_workers(
        "ENRICH_WORKERS", io_bound=True, max_cap=4) == 4


def test_io_bound_invalid_value_falls_back_to_default(monkeypatch):
    from backend.emby_server import cpu_budget
    _mock_cpus(monkeypatch, 2)
    monkeypatch.setenv("ENRICH_WORKERS", "abc")
    assert cpu_budget.background_workers("ENRICH_WORKERS", io_bound=True) == 8
