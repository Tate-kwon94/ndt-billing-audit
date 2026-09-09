"""조건부 근거를 성적서가 적은 검사 사유와 대조한다 (2026-09-08, 1항차 실물 증거).

증거 사슬:
  · 도면 MJZ0001 : 배관 용접부에 PT 요구 **없음** (VT 100 · UT 100 뿐)
  · SCWEP p.18/21: "임시 공정 장치를 제거한 자리는 VT 100% 와 침투탐상(LPT) 을 한다"
  · 성적서 표1    : 각 행의 '용접부 특성' 칸에 **"Removing temporary plate fit-up"** 이라고 적혀 있다
  · 청구 PT 399행 : 그 성적서를 참조한다

즉 PT 는 정당하다. 다만 **성적서가 그렇게 적은 행에 한해서** 정당하다.
지금 스키마는 그 칸을 안 받아서 정당한 PT 와 부당한 PT 를 구분할 수 없다 — 받아서 대조한다.
"""
from app.analyzers import scwep_basis as sb


_RULE = {"rule_id": "SCWEP-PT-01", "ndt_method": "PT",
         "trigger": "임시 공정 장치 제거 후",
         "trigger_keywords": ["temporary", "임시", "lug", "러그"],
         "quote": "the places from which temporary process devices have been removed shall be "
                  "subjected to 100 % visual and measurement inspection and liquid penetrant testing",
         "page": 18, "confidence": 0.9, "quote_verified": True}


def test_report_context_matching_trigger_keyword():
    assert sb.context_matches(_RULE, "Removing temporary plate fit-up") is True
    assert sb.context_matches(_RULE, "임시 러그 제거부") is True


def test_report_context_not_matching():
    assert sb.context_matches(_RULE, "Butt weld of pipeline spool") is False
    assert sb.context_matches(_RULE, "") is False
    assert sb.context_matches(_RULE, None) is False


def test_rule_without_keywords_falls_back_to_trigger_words():
    rule = dict(_RULE); rule.pop("trigger_keywords")
    assert sb.context_matches(rule, "임시 공정 장치를 제거한 자리") is True
    assert sb.context_matches(rule, "일반 맞대기 용접") is False


def _docs():
    return [{"document_no": "SCWEP-1", "extracted": {
        "_schema_version": 2, "document_no": "SCWEP-1",
        "applicable_scope": {"disciplines": ["CP-P1"]},
        "conditional_ndt_requirements": [_RULE],
        "needs_review": False, "extraction_confidence": 0.9}}]


def _kinds(a):
    return [r.get("kind") for r in a.refs]


def test_matching_report_context_keeps_the_rule_as_a_strong_basis():
    a = sb.classify(_docs(), "PT", "CP-P1", report_context="Removing temporary plate fit-up")
    assert "conditional" in _kinds(a)


def test_mismatching_report_context_demotes_the_rule_and_says_why():
    a = sb.classify(_docs(), "PT", "CP-P1", report_context="Butt weld of pipeline spool")
    assert "conditional" not in _kinds(a)
    weak = [r for r in a.refs if r.get("kind") == "conditional_weak"]
    assert weak and "검사 사유" in weak[0]["mismatch"]


def test_absent_report_context_keeps_previous_behaviour():
    """성적서가 사유를 안 적었다고 해서 있는 근거를 없애지 않는다."""
    assert "conditional" in _kinds(sb.classify(_docs(), "PT", "CP-P1"))
