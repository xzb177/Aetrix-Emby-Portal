"""PA-034：布尔归一化必须按 spec 风格转换 int/字符串输入。

旧行为：int 0/1 直接 str()、字符串 "1"/"yes"/"on" 原样返回；
word 风格键（如 slots_enabled）落库 "1" 后，严格读口径
（economy._get_bool_config 只认 "true"）会把"开"读成"关"，且保存接口不报错。
"""
import pytest

from backend.config_schema import ConfigValueError, normalize


class TestPa034WordStyle:
    @pytest.mark.parametrize(
        "value",
        [True, 1, "1", "yes", "on", "true", "TRUE", " true "],
    )
    def test_word_truthy_normalizes_to_true(self, value):
        assert normalize("slots_enabled", ("bool",), value) == "true"

    @pytest.mark.parametrize(
        "value",
        [False, 0, "0", "no", "off", "false", "FALSE", " false "],
    )
    def test_word_falsy_normalizes_to_false(self, value):
        assert normalize("slots_enabled", ("bool",), value) == "false"

    def test_word_strict_reader_sees_on(self):
        # 模拟严格读口径：落库值 == "true" 才算开
        for value in (1, "1", "yes", "on", True):
            assert normalize("slots_enabled", ("bool",), value) == "true"

    def test_word_registered_key_follows_spec(self):
        from backend import config_schema

        assert config_schema.WELFARE_CONFIG_SCHEMA["slots_enabled"] == ("bool",)
        assert (
            normalize(
                "slots_enabled",
                config_schema.WELFARE_CONFIG_SCHEMA["slots_enabled"],
                1,
            )
            == "true"
        )

    @pytest.mark.parametrize("value", ["maybe", 2, 1.5, "2"])
    def test_word_invalid_rejected(self, value):
        with pytest.raises(ConfigValueError):
            normalize("slots_enabled", ("bool",), value)


class TestPa034DigitStyle:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (True, "1"),
            (False, "0"),
            (1, "1"),
            (0, "0"),
            ("true", "1"),
            ("false", "0"),
            ("yes", "1"),
            ("no", "0"),
            ("on", "1"),
            ("off", "0"),
            ("TRUE", "1"),
        ],
    )
    def test_digit_normalizes_per_style(self, value, expected):
        assert normalize("k", ("bool", "digit"), value) == expected

    def test_digit_lenient_reader_still_sees_on(self):
        # digit 侧读口径宽容（认 "1" 与 "true"）；归一化后仍为开
        for value in (True, "true", "yes", 1):
            assert normalize("k", ("bool", "digit"), value) in ("1", "true")
