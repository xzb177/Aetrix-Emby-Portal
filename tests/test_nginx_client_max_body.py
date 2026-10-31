"""A-124 回归：docker/nginx.conf 的 location /api/ 必须放行库封面上传体。

后端契约：backend/emby_server/portal.py 的 _LIBRARY_COVER_MAX_BYTES = 8MB，
POST /api/admin/emby/libraries/{lib_id}/cover（全仓唯一 UploadFile 端点）。
nginx 默认 client_max_body_size 为 1MB；若 /api/ 内无覆盖指令，
1–8MB 的封面会在 nginx 层先被默认 413 页截断，后端自己的 8MB
中文校验永远够不着。修法取 10m（multipart 体比文件本身大，
10m 才能让后端 8MB 校验去产生业务报错），本测试把该值钉住。
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
NGINX_CONF = REPO / "docker" / "nginx.conf"

_SUFFIX = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def _location_block(text: str, location: str) -> str | None:
    """取 location 块全文（花括号计数；块内注释不含花括号）。"""
    start = text.find(f"location {location}")
    if start < 0:
        return None
    brace = text.find("{", start)
    if brace < 0:
        return None
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1:i]
    return None


def _body_limit_bytes(text: str) -> int | None:
    """解析 /api/ 块内的 client_max_body_size，缺失返回 None。"""
    block = _location_block(text, "/api/")
    if block is None:
        return None
    match = re.search(r"^\s*client_max_body_size\s+(\d+)([kmg]?)\s*;",
                      block, re.MULTILINE)
    if not match:
        return None
    return int(match.group(1)) * _SUFFIX[match.group(2)]


def test_repo_api_location_has_10m_body_limit():
    limit = _body_limit_bytes(NGINX_CONF.read_text(encoding="utf-8"))
    assert limit == 10 * 1024 * 1024


def test_missing_directive_is_rejected():
    text = "server {\n    location /api/ {\n        proxy_pass http://aetrix_backend;\n    }\n}\n"
    assert _body_limit_bytes(text) is None


def test_1m_limit_is_below_backend_8m_cap():
    text = ("server {\n    location /api/ {\n"
            "        client_max_body_size 1m;\n    }\n}\n")
    limit = _body_limit_bytes(text)
    assert limit is not None and limit < 8 * 1024 * 1024


def test_10m_limit_covers_backend_8m_cap():
    text = ("server {\n    location /api/ {\n"
            "        client_max_body_size 10m;\n    }\n}\n")
    assert _body_limit_bytes(text) == 10 * 1024 * 1024
