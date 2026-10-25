"""高级刮削第二批（FMBY 候选 3 / 4）：AI 参谋 + 人物刮削。

钉死：
① 参谋：开关关 / gate 缺失 / 提供方或模型未配置 / provider 未注入
   / provider 抛异常时一律静默返回 None，不报错、不阻塞；
② 参谋复用门：纯 AI 提名（无非 AI 证据）被门拒，状态
   refused_by_policy、无 diff、批不动；有非 AI 证据时 pending，
   权重由门钳制（测试替身门按 #1274 口径：上限两成）；
③ 参谋不写绑定：模块无任何写绑定 / 写库入口，批了也只改建议状态，
   diff 只是“拟改动”描述；
④ 人物：开关关时不问源；只填空缺永不覆盖；锚点从严（双方 tmdb id
   俱在且不同时跳过，哪怕名字相同）；单人失败不拖垮整批。

说明：主线尚未合并 #1274，测试里的 FakeGate 只是按 #1274 实测 API
（evaluate_binding_evidence / classify_confidence）写的替身，
用来证明参谋“调用门、不自算”；真正的门逻辑在 ai_policy.py 里，
不在此复写。门缺失那条用例测的就是主线现状。
"""


class FakeGate:
    """#1274 ai_policy 的测试替身（仅复述其公开口径，不作生产判定）。

    - AI 权重钳制到两成；纯 AI 证据（sources 全是 ai）判不许定；
    - 强弱阈值 0.9 / 0.65 仅作 confidence_band 展示分档。
    """

    MAX_AI_WEIGHT = 0.2
    calls = []

    def evaluate_binding_evidence(self, evidence):
        FakeGate.calls.append(dict(evidence or {}))
        evidence = evidence or {}
        sources = list(evidence.get("sources") or [])
        ai_present = bool(evidence.get("ai_suggested")) or "ai" in sources
        if not ai_present:
            return {"allowed": True, "ai_weight": None, "reason": "ok"}
        non_ai = [s for s in sources if s != "ai"]
        weight = evidence.get("ai_weight")
        capped = None
        if isinstance(weight, (int, float)) and weight == weight:
            capped = max(0.0, min(0.2, float(weight)))
        if not non_ai:
            return {"allowed": False, "ai_weight": capped,
                    "reason": "ai_only_refused"}
        return {"allowed": True, "ai_weight": capped, "reason": "ok_capped"}

    def classify_confidence(self, score):
        try:
            value = float(score)
        except (TypeError, ValueError):
            return "insufficient"
        if value >= 0.9:
            return "strong"
        if value >= 0.65:
            return "weak"
        return "insufficient"


from backend.emby_server.metasources import ai_advisor as advisor
from backend.emby_server.metasources import people_scrape as people


def _advisor_on(monkeypatch, gate):
    monkeypatch.setattr(advisor, "AI_ADVISOR_ENABLED", 1)
    monkeypatch.setattr(advisor, "_gate", gate)
    monkeypatch.setenv("AI_ADVISOR_PROVIDER", "test-provider")
    monkeypatch.setenv("AI_ADVISOR_MODEL", "test-model")


def _people_on(monkeypatch):
    monkeypatch.setattr(people, "PEOPLE_SCRAPE_ENABLED", 1)


# ---------------- AI 参谋：静默关闭 ----------------

def test_advisor_switch_off_returns_none_and_never_calls_provider(monkeypatch):
    monkeypatch.setattr(advisor, "AI_ADVISOR_ENABLED", 0)
    monkeypatch.setattr(advisor, "_gate", FakeGate())
    called = []
    result = advisor.request_suggestion(
        "item-1", {"sources": ["tmdb"]},
        provider=lambda k, e: called.append(k) or {"tmdb_id": "1"})
    assert result is None
    assert called == []


def test_advisor_gate_missing_closes_silently(monkeypatch):
    # 主线现状：#1274 未合并，ai_policy 不存在，参谋链路必须关闭。
    monkeypatch.setattr(advisor, "AI_ADVISOR_ENABLED", 1)
    monkeypatch.setattr(advisor, "_gate", None)
    monkeypatch.setenv("AI_ADVISOR_PROVIDER", "test-provider")
    monkeypatch.setenv("AI_ADVISOR_MODEL", "test-model")
    called = []
    result = advisor.request_suggestion(
        "item-1", {"sources": ["tmdb"]},
        provider=lambda k, e: called.append(k) or {"tmdb_id": "1"})
    assert result is None
    assert called == []


def test_advisor_unconfigured_provider_or_model_closes_silently(monkeypatch):
    monkeypatch.setattr(advisor, "AI_ADVISOR_ENABLED", 1)
    monkeypatch.setattr(advisor, "_gate", FakeGate())
    monkeypatch.delenv("AI_ADVISOR_PROVIDER", raising=False)
    monkeypatch.delenv("AI_ADVISOR_MODEL", raising=False)
    result = advisor.request_suggestion(
        "item-1", {"sources": ["tmdb"]},
        provider=lambda k, e: {"tmdb_id": "1"})
    assert result is None


def test_advisor_provider_failure_or_empty_never_raises(monkeypatch):
    _advisor_on(monkeypatch, FakeGate())

    def _boom(k, e):
        raise RuntimeError("provider down")

    assert advisor.request_suggestion("item-1", {}, provider=_boom) is None
    assert advisor.request_suggestion(
        "item-1", {}, provider=lambda k, e: None) is None
    assert advisor.request_suggestion(
        "item-1", {}, provider=lambda k, e: {}) is None
    assert advisor.request_suggestion("item-1", {}) is None  # 未注入 provider


# ---------------- AI 参谋：门裁决与拍板 ----------------

def test_advisor_pure_ai_refused_by_gate_no_diff_cannot_approve(monkeypatch):
    _advisor_on(monkeypatch, FakeGate())
    suggestion = advisor.request_suggestion(
        "item-9", None,
        provider=lambda k, e: {"tmdb_id": "42", "ai_weight": 0.9})
    assert suggestion is not None
    assert suggestion.status == "refused_by_policy"
    assert suggestion.reason == "ai_only_refused"
    assert suggestion.ai_weight == 0.2  # 门钳制，不是参谋自算
    assert suggestion.raw["tmdb_id"] == "42"  # 建议内容原样记账
    assert advisor.build_pending_diff(suggestion) is None
    still_refused = advisor.approve_suggestion(suggestion)
    assert still_refused.status == "refused_by_policy"
    assert any(e["event"] == "approve_blocked"
               for e in still_refused.audit)


def test_advisor_with_non_ai_evidence_pending_and_approvable(monkeypatch):
    gate = FakeGate()
    FakeGate.calls = []
    _advisor_on(monkeypatch, gate)
    suggestion = advisor.request_suggestion(
        "item-2", {"sources": ["tmdb"]},
        provider=lambda k, e: {"tmdb_id": "7", "ai_weight": 0.9})
    assert suggestion.status == "pending"
    assert suggestion.ai_weight == 0.2
    assert suggestion.provider == "test-provider"
    assert suggestion.model == "test-model"
    assert FakeGate.calls and FakeGate.calls[0]["ai_suggested"] is True
    assert "ai" in FakeGate.calls[0]["sources"]
    assert "tmdb" in FakeGate.calls[0]["sources"]
    diff = advisor.build_pending_diff(suggestion)
    assert diff["status"] == "awaiting_human_approval"
    assert diff["proposed_tmdb_id"] == "7"
    approved = advisor.approve_suggestion(suggestion)
    assert approved.status == "approved"
    assert advisor.build_pending_diff(approved)["status"] == \
        "approved_awaiting_application"
    with_outcome = advisor.record_outcome(approved, "not_applied",
                                          "等现行写库路径")
    assert any(e["event"] == "outcome:not_applied"
               for e in with_outcome.audit)


def test_advisor_reject_blocks_diff_and_has_no_binding_writer():
    assert not hasattr(advisor, "apply_binding")
    assert not hasattr(advisor, "write_binding")
    assert not hasattr(advisor, "apply_identity")
    assert not hasattr(advisor, "MAX_AI_WEIGHT")  # 阈值只活在门里


def test_advisor_reject_pending(monkeypatch):
    _advisor_on(monkeypatch, FakeGate())
    suggestion = advisor.request_suggestion(
        "item-3", {"sources": ["tmdb"]},
        provider=lambda k, e: {"imdb_id": "tt123", "ai_weight": 0.1})
    rejected = advisor.reject_suggestion(suggestion)
    assert rejected.status == "rejected"
    assert advisor.build_pending_diff(rejected) is None


# ---------------- 人物刮削 ----------------

def test_people_switch_off_never_calls_fetcher(monkeypatch):
    monkeypatch.setattr(people, "PEOPLE_SCRAPE_ENABLED", 0)
    called = []
    result = people.scrape_people(
        [{"name": "甲"}],
        fetcher=lambda r: called.append(r) or {"name": "甲", "role": "主演"})
    assert result == {"processed": 0, "filled": [], "skipped": [],
                      "failed": []}
    assert called == []


def test_people_fill_only_never_overwrites(monkeypatch):
    _people_on(monkeypatch)
    record = {"name": "甲", "role": "已有角色", "image": "",
              "person_tmdb_id": "11"}
    result = people.scrape_people(
        [record],
        fetcher=lambda r: {"name": "甲", "role": "新角色",
                           "image": "https://img/x.jpg",
                           "person_tmdb_id": "11",
                           "sources": ["tmdb"]})
    assert len(result["filled"]) == 1
    filled = result["filled"][0]
    assert filled["filled"] == ["image"]
    assert filled["record"]["role"] == "已有角色"  # 未覆盖
    assert filled["record"]["person_tmdb_id"] == "11"  # 未覆盖
    assert filled["record"]["image"] == "https://img/x.jpg"
    assert filled["evidence"] == {"sources": ["tmdb"]}


def test_people_anchor_strict_id_mismatch_skipped_even_if_name_matches(monkeypatch):
    _people_on(monkeypatch)
    result = people.scrape_people(
        [{"name": "甲", "person_tmdb_id": "11"}],
        fetcher=lambda r: {"name": "甲", "person_tmdb_id": "22",
                           "role": "主演"})
    assert result["filled"] == []
    assert result["skipped"][0]["reason"] == "anchor_mismatch"


def test_people_name_match_acquires_tmdb_id(monkeypatch):
    _people_on(monkeypatch)
    result = people.scrape_people(
        [{"name": "甲", "role": None, "image": None,
          "person_tmdb_id": None}],
        fetcher=lambda r: {"name": "甲", "person_tmdb_id": "33",
                           "role": "主演"})
    assert result["filled"][0]["filled"] == ["role", "person_tmdb_id"]


def test_people_single_failure_does_not_break_batch(monkeypatch):
    _people_on(monkeypatch)

    def _fetcher(record):
        if record["name"] == "坏人":
            raise RuntimeError("源挂了")
        if record["name"] == "空人":
            return None
        return {"name": "好人", "role": "主演"}

    result = people.scrape_people(
        [{"name": "坏人"}, {"name": "空人"}, {"name": "好人"}],
        fetcher=_fetcher)
    assert result["processed"] == 3
    assert len(result["failed"]) == 1
    assert result["failed"][0]["record"]["name"] == "坏人"
    assert result["skipped"][0]["reason"] == "empty_fetch"
    assert len(result["filled"]) == 1


def test_people_whitespace_counts_as_empty_and_object_adapter(monkeypatch):
    _people_on(monkeypatch)
    outcome = people.fill_person(
        {"name": "甲", "role": "   ", "image": None},
        {"name": "甲", "role": "主演"})
    assert outcome["filled"] == ["role"]

    class _Person:
        role = "已有"
        image = None
        person_tmdb_id = ""

    person = _Person()
    changed = people.apply_filled_to_person(
        person, {"role": "新角色", "image": "https://img/y.jpg",
                 "person_tmdb_id": "55"})
    assert changed == ["image", "person_tmdb_id"]
    assert person.role == "已有"
    assert person.image == "https://img/y.jpg"
