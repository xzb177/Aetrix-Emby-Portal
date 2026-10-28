"""302 Next F4：观测日志脱敏（红线）。

密钥类字段一律丢弃；文件身份类只留 sha256 前 12 位；嵌套值压平；
超长值截断；计数器只增不改。日志原文里出现秘密全文即失败。
"""

import logging

from backend.emby_server import next302_obs as obs


def test_redact_drops_secrets():
    clean = obs.redact({
        "api_key": "sk-live-123456",
        "Authorization": "Bearer abcdef",
        "password": "hunter2",
        "cookie": "session=xyz",
        "event": "search",
        "count": 3,
    })
    assert "api_key" not in clean
    assert "Authorization" not in clean
    assert "password" not in clean
    assert "cookie" not in clean
    assert clean["event"] == "search"
    assert clean["count"] == "3"
    blob = str(clean)
    assert "sk-live-123456" not in blob
    assert "abcdef" not in blob
    assert "hunter2" not in blob


def test_redact_hashes_file_ids():
    clean = obs.redact({
        "file_id": "1AbC2dEfGhIjKlMnOp",
        "variant_file_id": "other-id",
        "pickcode": "pick-123",
    })
    assert clean["file_id"].startswith("sha256:")
    assert len(clean["file_id"]) == len("sha256:") + 12
    assert "1AbC2dEfGhIjKlMnOp" not in str(clean)
    assert "other-id" not in str(clean)
    assert "pick-123" not in str(clean)
    # 同一 id 每次哈希一致（可关联），不同 id 哈希不同。
    again = obs.redact({"file_id": "1AbC2dEfGhIjKlMnOp"})
    assert again["file_id"] == clean["file_id"]
    assert clean["variant_file_id"] != clean["file_id"]


def test_redact_flattens_nested_and_truncates():
    clean = obs.redact({
        "nested": {"token": "should-not-leak"},
        "long": "x" * 2000,
    })
    assert clean["nested"] == "<nested>"
    assert "should-not-leak" not in str(clean)
    assert clean["long"].endswith("…(truncated)")
    assert len(clean["long"]) == 500 + len("…(truncated)")


def test_log_event_counters_and_no_secret_in_output(caplog):
    with caplog.at_level(logging.INFO, logger="next302"):
        out = obs.log_event("next302_search", result_total=5,
                            drive_token="topsecret")
    assert "drive_token" not in out
    assert "topsecret" not in caplog.text
    before = obs.get_counters().get("next302_search", 0)
    obs.log_event("next302_search", result_total=1)
    assert obs.get_counters()["next302_search"] == before + 1
    # 快照是拷贝，调用方改不动内部。
    snap = obs.get_counters()
    snap["next302_search"] = 999999
    assert obs.get_counters()["next302_search"] != 999999
