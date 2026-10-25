"""求片额度收拢（2026-10-23 运营参数去重 #5）回归测试。

此前三个求片额度散在两处：日限 media_seek_daily_limit 在运营参数，
公益月限 welfare_request_monthly 在公益页，付费月限 paid_request_monthly
没有任何后台入口（只能改库）。本轮把三者收进运营参数同一张卡：
两个月限登记进 ECONOMY 注册表与 schema（保存路径），公益页摘除月限字段
（前端）。后端 media_seek 的消费口径不变，这里只钉「登记 + 可保存」。
"""
from backend import config_schema
from backend.api import admin_economy


def test_monthly_limits_registered_for_ops_settings():
    assert admin_economy.ECONOMY_CONFIG_KEYS["welfare_request_monthly"] == "int"
    assert admin_economy.ECONOMY_CONFIG_KEYS["paid_request_monthly"] == "int"
    assert config_schema.ECONOMY_CONFIG_SCHEMA["welfare_request_monthly"] == ("int", 0, 10000)
    assert config_schema.ECONOMY_CONFIG_SCHEMA["paid_request_monthly"] == ("int", 0, 10000)


def test_monthly_limits_normalize_like_other_ints():
    spec = config_schema.ECONOMY_CONFIG_SCHEMA["welfare_request_monthly"]
    assert config_schema.normalize("welfare_request_monthly", spec, " 3 ") == "3"
    spec = config_schema.ECONOMY_CONFIG_SCHEMA["paid_request_monthly"]
    assert config_schema.normalize("paid_request_monthly", spec, "010") == "10"


def test_monthly_limits_reject_out_of_range():
    import pytest

    schema = config_schema.ECONOMY_CONFIG_SCHEMA
    with pytest.raises(config_schema.ConfigValueError):
        config_schema.normalize("paid_request_monthly",
                                schema["paid_request_monthly"], "10001")
    with pytest.raises(config_schema.ConfigValueError):
        config_schema.normalize("welfare_request_monthly",
                                schema["welfare_request_monthly"], "-1")


def test_monthly_limits_still_valid_for_welfare_endpoint():
    # welfare_request_monthly 在 WELFARE_CONFIG_SCHEMA 的历史登记保留，
    # 公益配置端点的校验口径不因本轮收拢而变化
    assert config_schema.WELFARE_CONFIG_SCHEMA["welfare_request_monthly"] == ("int", 0, 10000)
