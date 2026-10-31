#!/usr/bin/env python3
"""compose 日志轮转上限检查：在用 compose 的每个服务都必须有 logging 上限。

背景（360° 审计 A-083，2026-10-31）：四份在用 compose 此前均无任何
logging 配置，docker 默认 json-file 驱动无上限——日志盘无限增长；
且日志脱敏漏网（A-081/A-082）写入的历史日志无法靠轮转自然淘汰，
与 log_redact.py docstring 的假设相悖。本检查是那次发现的自动化兜底。

口径（逐服务核对，不是查文件里出现过 logging 字样）：
- 目标文件＝部署在用的四份 compose（见 COMPOSE_FILES）；
- 每个 services.* 服务都必须显式带 logging，且
  driver == "json-file"、options.max-size == "10m"、
  options.max-file == "5"（值钉死在本文件常量里：调值须同步改这里，
  防止上限被悄悄放大或摘掉而门禁无声）；
- logging 写成块式、单行 flow 式、或 YAML 锚点引用（logging: *锚名、
  服务级 <<: *锚名 合并）都认，锚点按定义处解析后核对同样的值。

实现说明：仓库未声明 PyYAML 依赖，本检查只用标准库做缩进式解析，
与 scripts/ 下其他 check 脚本同口径（可独立运行，exit 1＝有违规）。
"""

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.stream-node.yml",
    "docker-compose.frontend.yml",
)

REQUIRED_DRIVER = "json-file"
REQUIRED_MAX_SIZE = "10m"
REQUIRED_MAX_FILE = "5"

_TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):")
_SERVICE_KEY_RE = re.compile(r"^  ([A-Za-z0-9._-]+):\s*$")
_LOGGING_KEY_RE = re.compile(r"^(\s+)logging:\s*(.*?)\s*$")
_MERGE_KEY_RE = re.compile(r"^\s+<<:\s*\*([A-Za-z0-9_-]+)\s*$")
_ANCHOR_DEF_RE = re.compile(r":\s*&([A-Za-z0-9_-]+)\s*$")
_DRIVER_RE = re.compile(r"(?:^|[\s{,])driver\s*:\s*\"?([^\"\s,}]+)\"?")
_MAX_SIZE_RE = re.compile(r"max-size\s*:\s*\"?([^\"\s,}]+)\"?")
_MAX_FILE_RE = re.compile(r"max-file\s*:\s*\"?([^\"\s,}]+)\"?")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_after(lines: list[str], idx: int) -> list[str]:
    """返回 lines[idx] 之后、缩进大于该行缩进的连续行（该节点的子树）。"""
    base = _indent(lines[idx])
    out = []
    for line in lines[idx + 1:]:
        if not line.strip():
            continue
        if _indent(line) <= base:
            break
        out.append(line)
    return out


def _extract_caps(text: str) -> dict:
    """从一段文本（块式子行或 flow 映射）里抽 driver/max-size/max-file。"""
    caps = {}
    m = _DRIVER_RE.search(text)
    if m:
        caps["driver"] = m.group(1)
    m = _MAX_SIZE_RE.search(text)
    if m:
        caps["max-size"] = m.group(1)
    m = _MAX_FILE_RE.search(text)
    if m:
        caps["max-file"] = m.group(1)
    return caps


def _anchor_blocks(lines: list[str]) -> dict:
    """收集全文件 &锚名 -> 该锚点所在节点的子树文本。"""
    anchors = {}
    for idx, line in enumerate(lines):
        m = _ANCHOR_DEF_RE.search(line)
        if m:
            anchors[m.group(1)] = "\n".join(_block_after(lines, idx))
    return anchors


def _caps_from_logging_value(lines: list[str], idx: int, rest: str,
                             anchors: dict) -> dict | None:
    """解析一处 logging: 的右值，返回 caps；完全没有 logging 返回 None。"""
    if rest.startswith("*"):
        target = anchors.get(rest[1:].strip())
        return _extract_caps(target) if target is not None else {}
    if rest.startswith("{"):
        return _extract_caps(rest)
    if rest:
        # logging: &锚名（本行定义锚点、正文在子树）等形态：看子树
        return _extract_caps("\n".join(_block_after(lines, idx)))
    return _extract_caps("\n".join(_block_after(lines, idx)))


def _service_caps(lines: list[str], start: int, end: int,
                  anchors: dict) -> dict | None:
    """在服务块 lines[start:end] 里找 logging 并解析；没有返回 None。"""
    for idx in range(start, end):
        m = _LOGGING_KEY_RE.match(lines[idx])
        if m and _indent(lines[idx]) == 4:
            return _caps_from_logging_value(lines, idx, m.group(2), anchors)
        mm = _MERGE_KEY_RE.match(lines[idx])
        if mm:
            # 服务级合并锚点：到被合并块里找 logging 再解析其子树
            target_lines = (anchors.get(mm.group(1)) or "").splitlines()
            for j, tline in enumerate(target_lines):
                tm = _LOGGING_KEY_RE.match(tline)
                if tm:
                    return _caps_from_logging_value(
                        target_lines, j, tm.group(2), anchors)
    return None


def _services(lines: list[str]) -> list[tuple[str, int, int]]:
    """定位顶层 services: 下的服务块，返回 (服务名, 起始行, 结束行)。"""
    out = []
    in_services = False
    cur = None
    for idx, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent(line) == 0:
            m = _TOP_KEY_RE.match(line)
            in_services = bool(m and m.group(1) == "services")
            if cur is not None:
                out.append((cur[0], cur[1], idx))
                cur = None
            continue
        if in_services:
            m = _SERVICE_KEY_RE.match(line)
            if m:
                if cur is not None:
                    out.append((cur[0], cur[1], idx))
                cur = (m.group(1), idx)
    if cur is not None:
        out.append((cur[0], cur[1], len(lines)))
    return out


def check_text(filename: str, text: str) -> list[str]:
    """对一份 compose 文本逐服务核对，返回违规清单（空＝合规）。"""
    lines = text.splitlines()
    anchors = _anchor_blocks(lines)
    services = _services(lines)
    if not services:
        return [f"{filename}: 未解析到任何 services.* 服务"]
    violations = []
    for name, start, end in services:
        caps = _service_caps(lines, start, end, anchors)
        where = f"{filename}: 服务 {name}"
        if caps is None:
            violations.append(f"{where} 缺少 logging 配置（无日志轮转上限）")
            continue
        if caps.get("driver") != REQUIRED_DRIVER:
            violations.append(
                f"{where} logging.driver 应为 {REQUIRED_DRIVER}，"
                f"实际 {caps.get('driver')!r}")
        if caps.get("max-size") != REQUIRED_MAX_SIZE:
            violations.append(
                f"{where} logging.options.max-size 应为 {REQUIRED_MAX_SIZE}，"
                f"实际 {caps.get('max-size')!r}")
        if caps.get("max-file") != REQUIRED_MAX_FILE:
            violations.append(
                f"{where} logging.options.max-file 应为 {REQUIRED_MAX_FILE}，"
                f"实际 {caps.get('max-file')!r}")
    return violations


def find_violations(root: pathlib.Path) -> list[str]:
    violations = []
    for name in COMPOSE_FILES:
        path = root / name
        if not path.is_file():
            violations.append(f"{name}: 文件不存在")
            continue
        violations.extend(check_text(name, path.read_text(encoding="utf-8")))
    return violations


def main() -> int:
    violations = find_violations(REPO)
    if violations:
        print(f"compose 日志轮转上限检查未通过：{len(violations)} 条违规")
        for v in violations:
            print(f"  - {v}")
        return 1
    total = 0
    for name in COMPOSE_FILES:
        total += len(_services((REPO / name).read_text(
            encoding="utf-8").splitlines()))
    print(f"compose 日志轮转上限检查通过：{len(COMPOSE_FILES)} 份文件、"
          f"{total} 个服务均带 logging 上限"
          f"（{REQUIRED_DRIVER}，max-size={REQUIRED_MAX_SIZE}，"
          f"max-file={REQUIRED_MAX_FILE}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
