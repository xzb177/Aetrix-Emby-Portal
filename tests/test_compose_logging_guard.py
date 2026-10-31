"""A-083 回归：在用 compose 的每个服务都必须带日志轮转上限。

检查实现在 scripts/check_compose_logging.py（可独立运行、CI 同口径），
本测试把「仓库现状合规」与解析器口径（块式/flow/锚点/缺项）钉住，
防止以后新增服务漏配 logging、或上限被悄悄放大而无人察觉。
"""
from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_compose_logging", REPO / "scripts" / "check_compose_logging.py")
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)

GOOD_BLOCK = """\
services:
  api:
    image: example/api:latest
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    restart: unless-stopped
"""


def test_repo_compose_files_all_capped():
    assert checker.find_violations(REPO) == []


def test_missing_logging_is_flagged():
    text = """\
services:
  api:
    image: example/api:latest
    restart: unless-stopped
"""
    violations = checker.check_text("docker-compose.yml", text)
    assert len(violations) == 1
    assert "api" in violations[0]


def test_block_form_passes():
    assert checker.check_text("docker-compose.yml", GOOD_BLOCK) == []


def test_flow_form_passes():
    text = """\
services:
  api:
    image: example/api:latest
    logging: {driver: json-file, options: {max-size: "10m", max-file: "5"}}
    restart: unless-stopped
"""
    assert checker.check_text("docker-compose.yml", text) == []


def test_anchor_reference_passes():
    text = """\
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "5"

services:
  api:
    image: example/api:latest
    logging: *default-logging
    restart: unless-stopped
"""
    assert checker.check_text("docker-compose.yml", text) == []


def test_missing_max_file_is_flagged():
    text = """\
services:
  api:
    image: example/api:latest
    logging:
      driver: json-file
      options:
        max-size: "10m"
    restart: unless-stopped
"""
    violations = checker.check_text("docker-compose.yml", text)
    assert len(violations) == 1
    assert "max-file" in violations[0]


def test_wrong_driver_is_flagged():
    text = GOOD_BLOCK.replace("driver: json-file", "driver: syslog")
    violations = checker.check_text("docker-compose.yml", text)
    assert len(violations) == 1
    assert "driver" in violations[0]


def test_oversized_cap_is_flagged():
    text = GOOD_BLOCK.replace('max-size: "10m"', 'max-size: "10g"')
    violations = checker.check_text("docker-compose.yml", text)
    assert len(violations) == 1
    assert "max-size" in violations[0]


def test_only_the_uncapped_service_is_flagged():
    text = GOOD_BLOCK + """\
  worker:
    image: example/worker:latest
    restart: unless-stopped
"""
    violations = checker.check_text("docker-compose.yml", text)
    assert len(violations) == 1
    assert "worker" in violations[0]
