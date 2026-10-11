"""调度层：SA 轮换、限流、抖动、熔断。

横切所有层 (D-CONC-2/3/4)。

反风控铁律（用户 2026-10-11 明确）：
- SA 轮换顺序随机打乱，避免完全规律的轮换模式
- 请求间隔 ±jitter 随机化，不能让 Google 发现是机器在刷
- 配额错误触发熔断 + 指数退避，烧配额时不 hammer

D-CONC-3: SA token 按 SA 独立缓存（禁止 v1 的全局单 token 缓存模式，
那是 PR #575 修掉的 bug——全局缓存会架空轮换，把流量全锤到单个 SA 上）。
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional


def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SAKey:
    """单个 SA 凭证 + 独立 token 缓存 (D-CONC-3)。"""

    TOKEN_TTL = 3300  # 55 分钟

    def __init__(self, path: str):
        self.path = path
        with open(path, encoding="utf-8") as f:
            self._sa = json.load(f)
        self.email: str = self._sa["client_email"]
        self._token: Optional[str] = None
        self._token_at: float = 0.0
        self._lock = threading.Lock()
        # 熔断状态
        self.cooldown_until: float = 0.0
        self.consec_errors: int = 0

    def get_token(self) -> Optional[str]:
        """按 SA 独立缓存 token，过期才刷新。"""
        with self._lock:
            now = time.time()
            if self._token and now - self._token_at < self.TOKEN_TTL:
                return self._token
        # 在锁外刷新（网络 I/O），拿到后再写回
        token = self._mint_token()
        if token:
            with self._lock:
                self._token = token
                self._token_at = time.time()
        return token

    def _mint_token(self) -> Optional[str]:
        """用 SA 私钥签 JWT 换 access_token。"""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".pem", delete=False
            ) as kf:
                kf.write(self._sa["private_key"])
                key_path = kf.name
            try:
                now_i = int(time.time())
                header = _b64url(json.dumps(
                    {"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
                payload = _b64url(json.dumps({
                    "iss": self.email,
                    "scope": "https://www.googleapis.com/auth/drive",
                    "aud": "https://oauth2.googleapis.com/token",
                    "exp": now_i + 3600, "iat": now_i,
                }, separators=(",", ":")).encode())
                sign_input = f"{header}.{payload}"
                proc = subprocess.run(
                    ["openssl", "dgst", "-sha256", "-sign", key_path],
                    input=sign_input.encode(), capture_output=True, timeout=30)
                if proc.returncode != 0:
                    return None
                jwt = f"{sign_input}.{_b64url(proc.stdout)}"
                data = urllib.parse.urlencode({
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": jwt,
                }).encode()
                req = urllib.request.Request(
                    "https://oauth2.googleapis.com/token", data=data)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)["access_token"]
            finally:
                try:
                    os.unlink(key_path)
                except OSError:
                    pass
        except Exception:
            return None

    @property
    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until


class SARotator:
    """SA 轮换器：随机打乱顺序 + 熔断跳过 (D-CONC-3/4)。"""

    def __init__(self, sa_dir: str, cooldown_seconds: int = 600,
                 error_threshold: int = 3):
        sa_files = sorted(
            f for f in os.listdir(sa_dir) if f.endswith(".json"))
        if not sa_files:
            raise RuntimeError(f"SA 凭证目录为空: {sa_dir}")
        self._keys = [SAKey(os.path.join(sa_dir, f)) for f in sa_files]
        self._cooldown = cooldown_seconds
        self._threshold = error_threshold
        self._lock = threading.Lock()
        # D-CONC-3: 轮换顺序随机打乱，避免规律模式被识别
        self._order = list(range(len(self._keys)))
        random.shuffle(self._order)
        self._pos = 0

    def __len__(self) -> int:
        return len(self._keys)

    def next(self) -> Optional[SAKey]:
        """取下一个可用 SA（跳过熔断中的）。全部熔断时返回 None。"""
        with self._lock:
            for _ in range(len(self._keys)):
                idx = self._order[self._pos % len(self._order)]
                self._pos += 1
                key = self._keys[idx]
                if not key.in_cooldown:
                    return key
            return None

    def report_success(self, key: SAKey) -> None:
        with self._lock:
            key.consec_errors = 0

    def report_quota_error(self, key: SAKey) -> None:
        """配额错误：计数，达阈值则熔断该 SA (D-CONC-4)。"""
        with self._lock:
            key.consec_errors += 1
            if key.consec_errors >= self._threshold:
                key.cooldown_until = time.time() + self._cooldown
                key.consec_errors = 0


class TokenBucket:
    """全局 Token Bucket 限流器 + 强制随机抖动 (D-CONC-2)。"""

    def __init__(self, qps: float, jitter_ratio: float = 0.3):
        self._rate = qps
        self._jitter = jitter_ratio
        self._tokens = qps  # 初始满桶
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """阻塞直到拿到 token。等待时间带 ±jitter 随机化。"""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    # D-CONC-2: 间隔强制随机抖动
                    jitter = random.uniform(-self._jitter, self._jitter)
                    delay = max(0.0, (1.0 / self._rate) * jitter)
                    break
                # token 不足，算需要等多久
                wait = (1.0 - self._tokens) / self._rate
            # 锁外 sleep，避免阻塞其他线程
            time.sleep(wait)
        if delay > 0:
            time.sleep(delay)


class Backoff:
    """指数退避 + 抖动（配额错误时用）。"""

    def __init__(self, base: float = 1.0, max_seconds: float = 300.0):
        self._base = base
        self._max = max_seconds
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self._max, self._base * (2 ** self._attempt))
        self._attempt += 1
        # 退避也要抖动，避免惊群
        return delay * random.uniform(0.8, 1.2)

    def reset(self) -> None:
        self._attempt = 0
