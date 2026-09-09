"""SCWEP 근거 게이트 — 4단계 배선 테스트.

이 단계는 **껍데기만** 만든다. compliance 가 아직 새 rule 을 내지 않으므로 실제 판정은
한 줄도 안 바뀐다. 그래서 여기서 못 박는 것은 배선 자체다:
  · 새 rule 이 어느 집합에 들어가는가 (SUSPECT, 하드 아님)
  · LLM 상한이 상향만 막고 결정론 NONCOMPLIANT 는 못 내리는가
  · 기존 위치인자 호출이 그대로 되는가 (키워드 전용 인자)
  · 위험도 가중치가 yaml 에 없어도 0 으로 가라앉지 않는가

순서가 반대면(compliance 먼저) 새 rule 이 어느 집합에도 없어 weight 0 → 대시보드 기본
필터(risk_min 30) 아래로 사라지는 공백 구간이 생긴다. 그래서 배선이 먼저다.
"""
from __future__ import annotations

from app.analyzers import pipeline as pl
from app.analyzers import risk_scorer


# ─────────────────────────── 규칙 집합 ───────────────────────────

def test_새_rule_셋은_SUSPECT_집합에_있고_하드에는_없다():
    for r in ("billed_ndt_basis_not_submitted", "billed_ndt_basis_unclear", "billed_ndt_covered_by_scwep"):
        assert r in pl._SUSPECT_RULES, r
        assert r not in pl._HARD_RULES, r


def test_과다청구_확정_rule_은_여전히_하드():
    """근거 게이트는 확정 rule 을 없애는 게 아니라 나오는 조건을 좁히는 것이다."""
    assert "billed_ndt_not_in_requirements" in pl._HARD_RULES


def test_새_rule_은_결정론_판정에서_SUSPECT():
    for r in ("billed_ndt_basis_not_submitted", "billed_ndt_basis_unclear", "billed_ndt_covered_by_scwep"):
        v, reasons = pl._deterministic_verdict([{"rule": r}], {"_match_score": 100}, "deterministic")
        assert v == "SUSPECT", r
        assert any(r in s for s in reasons)


# ─────────────────────────── LLM 상한 ───────────────────────────

def test_기존_위치인자_호출은_그대로():
    """기존 테스트 5건이 위치인자 2개로 부른다. 새 인자는 키워드 전용이어야 한다."""
    assert pl._reconcile_verdicts("OK", "NONCOMPLIANT") == ("NONCOMPLIANT", True)
    assert pl._reconcile_verdicts("SUSPECT", "OK") == ("SUSPECT", True)
    assert pl._reconcile_verdicts("OK", "OK") == ("OK", False)


def test_상한은_LLM_상향을_막는다():
    """근거 미제출 행: 결정론 SUSPECT, LLM 이 NONCOMPLIANT 를 외쳐도 SUSPECT."""
    final, disagree = pl._reconcile_verdicts("SUSPECT", "NONCOMPLIANT", llm_ceiling="SUSPECT")
    assert final == "SUSPECT"
    assert disagree is True            # 불일치는 원본 LLM 표로 센다 — 눌린 사실을 숨기지 않는다


def test_상한은_결정론_NONCOMPLIANT_를_못_내린다():
    """같은 행에 result_mismatch 가 있으면 결정론이 NONCOMPLIANT 고, 상한은 그걸 건드리지 않는다."""
    final, _ = pl._reconcile_verdicts("NONCOMPLIANT", "OK", llm_ceiling="SUSPECT")
    assert final == "NONCOMPLIANT"


def test_상한_없으면_예전과_동일():
    assert pl._reconcile_verdicts("SUSPECT", "NONCOMPLIANT", llm_ceiling=None) == ("NONCOMPLIANT", True)


def test_상한_계산은_가장_낮은_것():
    assert pl._llm_ceiling_for([{"rule": "billed_ndt_basis_not_submitted"}]) == "SUSPECT"
    assert pl._llm_ceiling_for([{"rule": "result_mismatch"}]) is None
    assert pl._llm_ceiling_for([]) is None


# ─────────────────────────── 결정론 조치 문구 ───────────────────────────

def test_근거_상태별_조치문구가_결정론으로_나온다():
    """판정과 문구가 서로 다른 말을 하던 문제를 여기서 없앤다."""
    a = pl._deterministic_action([{"rule": "billed_ndt_basis_not_submitted", "details": {}}])
    assert "제출 요청" in a and "단정하지 않음" in a
    a = pl._deterministic_action([{"rule": "billed_ndt_covered_by_scwep",
                                   "details": {"basis_docs": ["SCWEP-7"], "trigger": "러그 제거 후"}}])
    assert "SCWEP-7" in a and "러그 제거 후" in a and "과다청구 아님" in a
    assert pl._deterministic_action([{"rule": "result_mismatch"}]) is None   # 게이트 rule 아니면 LLM 문구 유지


# ─────────────────────────── 위험도 ───────────────────────────

def test_위험도_yaml_에_있으면_그_값():
    assert risk_scorer.compute([{"rule": "billed_ndt_basis_not_submitted"}]) == 45
    assert risk_scorer.compute([{"rule": "billed_ndt_basis_unclear"}]) == 50
    assert risk_scorer.compute([{"rule": "billed_ndt_covered_by_scwep"}]) == 35


def test_위험도_yaml_에_없어도_0으로_가라앉지_않음(monkeypatch):
    """운영자 yaml 이 구버전이어도 근거 게이트 rule 이 화면에서 사라지지 않는다."""
    monkeypatch.setattr(risk_scorer, "matching_rules", lambda: {"risk_score": {"weights": {}}})
    assert risk_scorer.compute([{"rule": "billed_ndt_basis_not_submitted"}]) == 45
    assert risk_scorer.compute([{"rule": "totally_unknown_rule"}]) == 0


def test_새_rule_셋은_대시보드_기본_필터_안에_있다():
    """기본 필터 risk_min=30, 빨강 임계 60. 셋 다 그 사이여야 '보이되 확정처럼 안 보인다'."""
    from app.config import matching_rules
    w = matching_rules()["risk_score"]["weights"]
    thr = matching_rules()["review_priority"]["highlight_threshold"]
    for r in ("billed_ndt_basis_not_submitted", "billed_ndt_basis_unclear", "billed_ndt_covered_by_scwep"):
        assert 30 <= w[r] < thr, (r, w[r])


# ─────────────────────────── 설정 스위치 ───────────────────────────

def test_과다청구_확정_스위치가_yaml_에_있고_전부_켜져_있다():
    from app.config import matching_rules
    sw = matching_rules()["compliance"]["overbilling_claim"]
    for k in ("require_submitted_basis", "require_conditional_schema",
              "never_claim_on_empty_drawing_requirement", "never_claim_on_flagged_drawing_set"):
        assert sw[k] is True, k


# ─────────────────────────── 아직 동작이 안 바뀌었다 ───────────────────────────

def test_compliance_가_내는_새_rule_은_전부_배선돼_있다():
    """5단계 이후: compliance 가 낼 수 있는 rule 이름은 전부 판정 집합·위험도 표에 있어야 한다.
    어느 하나라도 빠지면 그 rule 은 weight 0 → 대시보드 필터 밖으로 조용히 사라진다."""
    import inspect
    from app.analyzers import compliance
    from app.config import matching_rules
    src = inspect.getsource(compliance)
    w = matching_rules()["risk_score"]["weights"]
    for r in ("billed_ndt_basis_not_submitted", "billed_ndt_basis_unclear", "billed_ndt_covered_by_scwep"):
        assert r in src, f"{r} 가 compliance 에 없음"
        assert r in pl._SUSPECT_RULES or r in pl._HARD_RULES, f"{r} 판정 집합 누락"
        assert r in w, f"{r} 위험도 가중치 누락"
