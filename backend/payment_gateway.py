# -*- coding: utf-8 -*-
"""支付网关可插拔接口（准备线，不接通、不收费）。

定位：
- 本模块只定义抽象支付接口（创建订单、回调验签、退款、查询四方法）
  与两个适配器（易支付、Stripe），全部走配置选择；**不调用任何真实
  支付接口、不发任何真实 HTTP 请求、不依赖任何支付 SDK**。
- 现有下单/回调路径（``backend/api/economy.py`` 的易支付直连、积分抵扣
  分支）本 PR 一字不动；本模块尚未接线到 ``POST /payment/order``，
  接线是下一步（须另行评审钱路），本 PR 只交付可测的接口层。
- 网关未配时（``PAYMENT_GATEWAY_PROVIDER`` 为空，默认）：``gateway_from_env()``
  返回 ``None``，``GET /payment/methods`` 行为保持现状（alipay / wxpay，
  另加 DB 开关控制的 qqpay），PR 说明里写明。

配置键（全部进 env.example，中文注释，默认空 = 不启用）：
- ``PAYMENT_GATEWAY_PROVIDER``：``""``（默认，不启用）/ ``epay`` / ``stripe``；
  ``yipay`` 视为 ``epay`` 的别名。
- 易支付侧：``PAYMENT_EPAY_GATEWAY_URL``、``PAYMENT_EPAY_PARTNER_ID``、
  ``PAYMENT_EPAY_PARTNER_KEY``（真实值由服主在部署配置里填，本仓库
  一律不填、不编造；见 PR 说明“还缺的商户信息清单”）。
- Stripe 侧：``PAYMENT_STRIPE_PUBLISHABLE_KEY``、``PAYMENT_STRIPE_SECRET_KEY``、
  ``PAYMENT_STRIPE_WEBHOOK_SECRET``（同上，缺项见 PR 说明）。

适配器诚实边界（写进 PR 说明，不许对外宣称已接通）：
- 易支付：创建订单只拼跳转 URL（签名算法与 ``economy._yipay_sign`` 同款
  MD5：参数 key 升序拼接 ``a=b&...&<key>``，此处独立实现以免顶层 import
  FastAPI 路由模块）；回调验签为纯函数。退款/查询：易支付各服务商的
  退款/查询接口形态不统一，本 PR 不编造调用，返回 ``not_supported``
  明示未接线，待商户文档确认后再接。
- Stripe：创建订单/退款/查询均不发 HTTP（无 SDK、无真实调用），只做
  配置校验与 dry-run 载荷回显；回调验签是纯函数（HMAC-SHA256，
  Stripe 签名头 ``t=...,v1=...`` 形态）。真实 Checkout Session 创建、
  退款/查询 API 调用是下一步，须等商户密钥与 SDK 接线评审。
- 凭据处理：本模块只读 env，不打印密钥、不把密钥写进日志与返回值；
  测试用假密钥/假回调，不碰真实商户。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from abc import ABC, abstractmethod
from typing import Dict, Optional
from urllib.parse import urlencode


class PaymentGatewayError(Exception):
    """网关操作失败（配置缺失、参数非法、适配器未支持某方法）。"""


class PaymentGatewayNotConfigured(PaymentGatewayError):
    """网关未配置（provider 为空或必需凭据缺失）。"""


def _epay_sign(params: dict, key: str) -> str:
    """易支付 MD5 签名：与 economy._yipay_sign 同款算法（独立实现）。

    过滤空值与 sign / sign_type 字段，按 key ASCII 升序拼接
    ``a=b&...&<key>`` 后 MD5。比较一律用 hmac.compare_digest。
    """
    filtered = {k: v for k, v in params.items()
                if v not in (None, "") and k != "sign" and k != "sign_type"}
    qs = urlencode(sorted(filtered.items()))
    return hashlib.md5(f"{qs}{key}".encode("utf-8")).hexdigest()


class PaymentGateway(ABC):
    """抽象支付接口：创建订单、回调验签、退款、查询四方法。"""

    provider: str = ""

    @abstractmethod
    def create_order(self, *, order_no: str, amount: float, method: str,
                     return_url: str = "", notify_url: str = "",
                     subject: str = "") -> dict:
        """创建订单，返回至少含 provider / order_no 的 dict。

        易支付返回 pay_url（跳转用）；Stripe 本 PR 返回 dry-run 载荷
        （status=dry_run_no_network，不代表已在 Stripe 建单）。
        """

    @abstractmethod
    def verify_callback(self, params: dict) -> bool:
        """回调验签：验签失败返回 False，不抛错（调用方据此回失败响应）。"""

    @abstractmethod
    def refund(self, *, order_no: str, amount: float) -> dict:
        """退款：未接线的适配器返回 status=not_supported，不静默成功。"""

    @abstractmethod
    def query(self, *, order_no: str) -> dict:
        """查询：未接线的适配器返回 status=not_supported，不编造状态。"""


class EpayGateway(PaymentGateway):
    """易支付适配器（纯 URL 拼接 + 纯验签，不发 HTTP）。"""

    provider = "epay"

    def __init__(self, gateway_url: str = "", partner_id: str = "",
                 partner_key: str = ""):
        self.gateway_url = (gateway_url or "").strip().rstrip("/")
        self.partner_id = (partner_id or "").strip()
        self.partner_key = (partner_key or "").strip()

    def _require_config(self) -> None:
        if not self.gateway_url or not self.partner_id or not self.partner_key:
            raise PaymentGatewayNotConfigured(
                "易支付未配置：PAYMENT_EPAY_GATEWAY_URL / "
                "PAYMENT_EPAY_PARTNER_ID / PAYMENT_EPAY_PARTNER_KEY 缺项")

    def create_order(self, *, order_no: str, amount: float, method: str,
                     return_url: str = "", notify_url: str = "",
                     subject: str = "") -> dict:
        self._require_config()
        if not order_no or amount is None or float(amount) <= 0:
            raise PaymentGatewayError("订单号为空或金额非法")
        params = {
            "pid": self.partner_id,
            "type": method,
            "out_trade_no": order_no,
            "notify_url": notify_url,
            "return_url": return_url,
            "name": subject or order_no,
            "money": f"{float(amount):.2f}",
        }
        params["sign"] = _epay_sign(params, self.partner_key)
        params["sign_type"] = "MD5"
        return {"provider": "epay", "order_no": order_no,
                "pay_url": f"{self.gateway_url}/submit.php?{urlencode(params)}"}

    def verify_callback(self, params: dict) -> bool:
        if not self.partner_key:
            return False
        sign = str((params or {}).get("sign") or "")
        if not sign:
            return False
        expected = _epay_sign(dict(params or {}), self.partner_key)
        return hmac.compare_digest(sign.lower(), expected.lower())

    def refund(self, *, order_no: str, amount: float) -> dict:
        return {"provider": "epay", "order_no": order_no,
                "status": "not_supported",
                "reason": "易支付退款接口形态因服务商而异，本准备线未接线，"
                          "待商户文档确认后再接，不静默成功"}

    def query(self, *, order_no: str) -> dict:
        return {"provider": "epay", "order_no": order_no,
                "status": "not_supported",
                "reason": "易支付查询接口形态因服务商而异，本准备线未接线，"
                          "待商户文档确认后再接，不编造订单状态"}


def verify_stripe_webhook(payload: bytes, sig_header: str,
                          webhook_secret: str) -> bool:
    """Stripe 回调验签（纯函数，不发 HTTP）。

    签名头形态 ``t=<unix时间戳>,v1=<hex>``；待验串为
    ``f"{t}.{payload}"``，HMAC-SHA256，密钥为 webhook_secret。
    任何形态异常一律返回 False，不抛错。
    """
    if not webhook_secret or not sig_header:
        return False
    try:
        parts = dict(item.split("=", 1) for item in sig_header.split(",")
                     if "=" in item)
        timestamp = parts.get("t", "")
        signature = parts.get("v1", "")
        if not timestamp or not signature:
            return False
        signed = f"{timestamp}.".encode("utf-8") + (payload or b"")
        expected = hmac.new(webhook_secret.encode("utf-8"), signed,
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.lower(), expected.lower())
    except (ValueError, TypeError):
        return False


class StripeGateway(PaymentGateway):
    """Stripe 适配器（本 PR 不发 HTTP：只校验配置 + dry-run + 纯验签）。

    真实 Checkout Session / 退款 / 查询 API 调用须等下一步：
    商户密钥到位 + Stripe SDK 接线 + 钱路评审，本 PR 不冒充已接通。
    """

    provider = "stripe"

    def __init__(self, publishable_key: str = "", secret_key: str = "",
                 webhook_secret: str = ""):
        self.publishable_key = (publishable_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.webhook_secret = (webhook_secret or "").strip()

    def _require_config(self) -> None:
        if not self.secret_key:
            raise PaymentGatewayNotConfigured(
                "Stripe 未配置：PAYMENT_STRIPE_SECRET_KEY 缺项")

    def create_order(self, *, order_no: str, amount: float, method: str = "card",
                     return_url: str = "", notify_url: str = "",
                     subject: str = "") -> dict:
        self._require_config()
        if not order_no or amount is None or float(amount) <= 0:
            raise PaymentGatewayError("订单号为空或金额非法")
        # 不发 HTTP：只回显下一步建 Checkout Session 要用的载荷。
        return {"provider": "stripe", "order_no": order_no,
                "status": "dry_run_no_network",
                "amount": float(amount), "currency": "cny",
                "method": method, "subject": subject or order_no,
                "note": "本准备线未调用 Stripe API，未建真实 Checkout Session"}

    def verify_callback(self, params: dict) -> bool:
        # params 约定：{"payload": bytes/str, "sig_header": str}
        payload = (params or {}).get("payload", b"")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        sig_header = str((params or {}).get("sig_header") or "")
        return verify_stripe_webhook(payload, sig_header, self.webhook_secret)

    def refund(self, *, order_no: str, amount: float) -> dict:
        self._require_config()
        return {"provider": "stripe", "order_no": order_no,
                "status": "dry_run_no_network", "amount": float(amount),
                "note": "本准备线未调用 Stripe 退款 API，未产生真实退款"}

    def query(self, *, order_no: str) -> dict:
        self._require_config()
        return {"provider": "stripe", "order_no": order_no,
                "status": "dry_run_no_network",
                "note": "本准备线未调用 Stripe 查询 API，未返回真实订单状态"}


def resolve_gateway(provider: str, *, epay_url: str = "",
                    epay_pid: str = "", epay_key: str = "",
                    stripe_publishable: str = "", stripe_secret: str = "",
                    stripe_webhook: str = "") -> Optional[PaymentGateway]:
    """按 provider 选择适配器。

    provider 为空 / 仅空白 → None（不启用，调用方保持现状行为）；
    ``epay`` / ``yipay`` → EpayGateway；``stripe`` → StripeGateway；
    其他值 → ValueError（未知 provider 响亮失败，不静默回落）。
    """
    name = (provider or "").strip().lower()
    if not name:
        return None
    if name in ("epay", "yipay"):
        return EpayGateway(epay_url, epay_pid, epay_key)
    if name == "stripe":
        return StripeGateway(stripe_publishable, stripe_secret, stripe_webhook)
    raise ValueError(f"未知支付网关 provider：{provider!r}（只支持 epay / stripe）")


def gateway_from_env() -> Optional[PaymentGateway]:
    """从环境变量读配置并选择网关（默认空 = 不启用，返回 None）。"""
    return resolve_gateway(
        os.getenv("PAYMENT_GATEWAY_PROVIDER", ""),
        epay_url=os.getenv("PAYMENT_EPAY_GATEWAY_URL", ""),
        epay_pid=os.getenv("PAYMENT_EPAY_PARTNER_ID", ""),
        epay_key=os.getenv("PAYMENT_EPAY_PARTNER_KEY", ""),
        stripe_publishable=os.getenv("PAYMENT_STRIPE_PUBLISHABLE_KEY", ""),
        stripe_secret=os.getenv("PAYMENT_STRIPE_SECRET_KEY", ""),
        stripe_webhook=os.getenv("PAYMENT_STRIPE_WEBHOOK_SECRET", ""))


def available_methods(provider: str) -> list:
    """适配器声明的支付方式（仅作接口层参考，未接线到 /payment/methods）。

    未配 / 易支付 → 与现状一致的 alipay / wxpay（qqpay 仍由 DB 开关
    ``payment_qqpay_enabled`` 在端点层另加）；Stripe → card。
    本 PR 不改 ``GET /payment/methods`` 的行为。
    """
    name = (provider or "").strip().lower()
    if name == "stripe":
        return ["card"]
    return ["alipay", "wxpay"]
