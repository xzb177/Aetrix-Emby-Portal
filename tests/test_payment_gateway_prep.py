"""支付网关可插拔接口（准备线）测试。

只测纯接口层：假配置、假订单号、假回调，不发真实 HTTP、不碰真实
商户、不读 DB。本地 Python 3.12 + pyshim 口径，全量以 GitHub CI 为准。
"""

import hashlib
import hmac
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.payment_gateway import (
    EpayGateway,
    PaymentGatewayNotConfigured,
    StripeGateway,
    available_methods,
    gateway_from_env,
    resolve_gateway,
    verify_stripe_webhook,
    _epay_sign,
)


class FakeGateway:
    """测试用假适配器：记录调用，不发请求。"""

    provider = "fake"

    def __init__(self):
        self.calls = []

    def create_order(self, **kwargs):
        self.calls.append(("create_order", kwargs))
        return {"provider": "fake", "order_no": kwargs["order_no"],
                "pay_url": "https://fake.example/pay"}

    def verify_callback(self, params):
        self.calls.append(("verify_callback", params))
        return params.get("sign") == "ok"

    def refund(self, **kwargs):
        self.calls.append(("refund", kwargs))
        return {"provider": "fake", "status": "refunded"}

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return {"provider": "fake", "status": "paid"}


def test_resolve_empty_returns_none():
    assert resolve_gateway("") is None
    assert resolve_gateway("   ") is None
    assert resolve_gateway(None) is None


def test_resolve_unknown_raises():
    with pytest.raises(ValueError):
        resolve_gateway("unknown-pay")


def test_resolve_epay_alias():
    assert resolve_gateway("epay").provider == "epay"
    assert resolve_gateway("yipay").provider == "epay"
    assert resolve_gateway("stripe").provider == "stripe"


def test_gateway_from_env_empty_is_none(monkeypatch):
    monkeypatch.setenv("PAYMENT_GATEWAY_PROVIDER", "")
    assert gateway_from_env() is None


def test_gateway_from_env_epay(monkeypatch):
    monkeypatch.setenv("PAYMENT_GATEWAY_PROVIDER", "epay")
    monkeypatch.setenv("PAYMENT_EPAY_GATEWAY_URL", "https://pay.example.com")
    monkeypatch.setenv("PAYMENT_EPAY_PARTNER_ID", "10001")
    monkeypatch.setenv("PAYMENT_EPAY_PARTNER_KEY", "fake-key")
    gw = gateway_from_env()
    assert isinstance(gw, EpayGateway)
    assert gw.partner_id == "10001"


def test_epay_create_order_signs_and_builds_url():
    gw = EpayGateway("https://pay.example.com", "10001", "fake-key")
    out = gw.create_order(order_no="ORD-1", amount=10.5, method="alipay",
                          return_url="https://site.example/return",
                          notify_url="https://site.example/notify")
    assert out["provider"] == "epay"
    assert out["pay_url"].startswith("https://pay.example.com/submit.php?")
    assert "out_trade_no=ORD-1" in out["pay_url"]
    assert "sign=" in out["pay_url"]


def test_epay_verify_callback_roundtrip_and_tamper():
    gw = EpayGateway("https://pay.example.com", "10001", "fake-key")
    params = {"pid": "10001", "out_trade_no": "ORD-1", "money": "10.50",
              "trade_status": "TRADE_SUCCESS"}
    params["sign"] = _epay_sign(params, "fake-key")
    assert gw.verify_callback(params) is True
    tampered = dict(params, money="999.00")
    assert gw.verify_callback(tampered) is False
    assert gw.verify_callback({}) is False


def test_epay_unconfigured_create_raises_verify_false():
    gw = EpayGateway("", "", "")
    with pytest.raises(PaymentGatewayNotConfigured):
        gw.create_order(order_no="ORD-1", amount=1.0, method="alipay")
    assert gw.verify_callback({"sign": "x"}) is False


def test_epay_refund_query_are_not_supported_not_fake_success():
    gw = EpayGateway("https://pay.example.com", "10001", "fake-key")
    assert gw.refund(order_no="ORD-1", amount=1.0)["status"] == "not_supported"
    assert gw.query(order_no="ORD-1")["status"] == "not_supported"


def test_stripe_verify_webhook_roundtrip_and_tamper():
    secret = "whsec-fake"
    payload = b'{"type":"checkout.session.completed"}'
    timestamp = "1700000000"
    signed = f"{timestamp}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={timestamp},v1={sig}"
    assert verify_stripe_webhook(payload, header, secret) is True
    assert verify_stripe_webhook(b"tampered", header, secret) is False
    assert verify_stripe_webhook(payload, "garbage", secret) is False
    assert verify_stripe_webhook(payload, header, "") is False


def test_stripe_create_is_dry_run_no_network():
    gw = StripeGateway("pk_fake", "sk_fake", "whsec-fake")
    out = gw.create_order(order_no="ORD-2", amount=99.0)
    assert out["status"] == "dry_run_no_network"
    assert out["provider"] == "stripe"
    assert gw.verify_callback({"payload": b"{}", "sig_header": "x"}) is False
    assert gw.refund(order_no="ORD-2", amount=99.0)["status"] == "dry_run_no_network"
    assert gw.query(order_no="ORD-2")["status"] == "dry_run_no_network"


def test_stripe_unconfigured_raises():
    gw = StripeGateway("", "", "")
    with pytest.raises(PaymentGatewayNotConfigured):
        gw.create_order(order_no="ORD-2", amount=1.0)


def test_fake_gateway_records_calls():
    gw = FakeGateway()
    gw.create_order(order_no="ORD-9", amount=5.0, method="alipay")
    assert gw.verify_callback({"sign": "ok"}) is True
    assert gw.refund(order_no="ORD-9", amount=5.0)["status"] == "refunded"
    assert gw.query(order_no="ORD-9")["status"] == "paid"
    assert len(gw.calls) == 4


def test_available_methods_defaults_to_current():
    assert available_methods("") == ["alipay", "wxpay"]
    assert available_methods("epay") == ["alipay", "wxpay"]
    assert available_methods("stripe") == ["card"]
