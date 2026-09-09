"""SCWEP 근거 판정기 테스트 — "과다청구라고 말해도 되는가" 의 경계를 못 박는다.

이 파일이 지키는 한 문장:
    **낮은 신뢰도로 추출된 값은 어떤 경로로도 과다청구 확정을 만들지 못한다.**

전부 합성 dict 다. DB 도 LLM 도 안 쓴다 — 판정기가 순수하기 때문이고,
쉬워야 실제로 테스트되기 때문이다.
"""
from __future__ import annotations

import pytest

from app.analyzers import scwep_basis as sb


# ─────────────────────────── 문서 만들기 ───────────────────────────

def _doc(document_no="SCWEP-001", *, disciplines=("CP-M1",), conditional=None,
         general=None, sampling=None, needs_review=False, confidence=0.9, schema=2):
    """제출된 SCWEP 한 건. 기본값은 '고발 자격을 갖춘' 문서다 —
    각 테스트는 여기서 한 가지씩만 무너뜨려 그 하나의 효과를 본다."""
    extracted = {
        "applicable_scope": {"disciplines": list(disciplines)},
        "conditional_ndt_requirements": list(conditional or []),
        "general_rules": list(general or []),
        "default_sampling_rates": list(sampling or []),
        "needs_review": needs_review,
        "extraction_confidence": confidence,
        "_schema_version": schema,
    }
    return {"document_no": document_no, "file_path": f"/x/{document_no}.pdf", "extracted": extracted}


def _lug_rule(method="PT", confidence=0.9, trigger="임시 러그(가설 부착물) 제거 후", quote="러그 제거부는 PT 를 시행한다."):
    """사용자가 든 실제 도메인 사례: 러그를 떼면 그 자리에 PT."""
    return {"rule_id": "SCWEP-PT-07", "trigger": trigger, "ndt_method": method,
            "applies_to": "임시 부착물 제거부", "page": 12, "quote": quote, "confidence": confidence}


# ─────────────────────────── 면책 ───────────────────────────

def test_러그_제거_후_PT_는_근거_있음():
    """이 파이프라인이 존재하는 이유 그 자체."""
    a = sb.classify([_doc(conditional=[_lug_rule()])], "PT", "CP-M1")
    assert a.state == sb.STATE_COVERED
    assert a.may_claim_overbilling is False
    assert a.refs and a.refs[0]["quote"].startswith("러그 제거부")
    assert a.covering_docs == ["SCWEP-001"]


def test_일반규칙만_있으면_근거_있음이_아니라_확인_필요():
    """general_rules 는 '어떤 사건이 일어나면' 을 표현할 수 없다.
    여기서 걸린 것을 면책으로 인정하면 SCWEP 한 건이 회차 전체를 덮는다."""
    doc = _doc(general=[{"rule_id": "G-1", "ndt_method": "PT", "topic": "procedure",
                         "summary": "PT 절차", "page": 3, "quote": "PT 는 …"}])
    a = sb.classify([doc], "PT", "CP-M1")
    assert a.state == sb.STATE_UNCLEAR
    assert a.may_claim_overbilling is False


def test_조건이나_인용문이_없으면_면책_안_됨():
    """사람이 대조할 수 없는 면책은 면책이 아니다."""
    for broken in (_lug_rule(trigger=""), _lug_rule(quote="")):
        a = sb.classify([_doc(conditional=[broken])], "PT", "CP-M1")
        assert a.state == sb.STATE_UNCLEAR, broken


def test_방법_ALL_은_조건부_면책으로_인정하지_않음():
    """'전부' 를 뜻하는 값으로 특수공정 면책을 만들 수 없다."""
    a = sb.classify([_doc(conditional=[_lug_rule(method="ALL")])], "PT", "CP-M1")
    assert a.state != sb.STATE_COVERED


def test_저신뢰_조건부는_확인_필요로_강등():
    a = sb.classify([_doc(conditional=[_lug_rule(confidence=0.4)])], "PT", "CP-M1")
    assert a.state == sb.STATE_UNCLEAR


def test_문서가_재확인_대상이면_강한_히트도_강등():
    a = sb.classify([_doc(conditional=[_lug_rule()], needs_review=True)], "PT", "CP-M1")
    assert a.state == sb.STATE_UNCLEAR


def test_범위_밖_문서는_면책에_쓰이지_않음():
    """다른 공종 절차서로 이 공종 청구를 면책할 수 없다."""
    a = sb.classify([_doc(disciplines=("CP-E1",), conditional=[_lug_rule()])], "PT", "CP-M1")
    assert a.state != sb.STATE_COVERED


def test_범위_미상_문서는_면책은_가능():
    """비대칭이 의도된 지점 — unknown 은 면책은 되고 고발은 안 된다."""
    a = sb.classify([_doc(disciplines=(), conditional=[_lug_rule()])], "PT", "CP-M1")
    assert a.state == sb.STATE_COVERED


# ─────────────────────────── 고발 ───────────────────────────

def test_제출됐고_범위맞고_침묵하면_고발_가능():
    """유일하게 과다청구를 확정할 수 있는 상태."""
    doc = _doc(conditional=[_lug_rule(method="MT")])      # PT 는 어디에도 없음
    a = sb.classify([doc], "PT", "CP-M1")
    assert a.state == sb.STATE_NO_BASIS
    assert a.may_claim_overbilling is True


def test_SCWEP_없으면_고발_불가_제출_요청():
    a = sb.classify([], "PT", "CP-M1")
    assert a.state == sb.STATE_NOT_SUBMITTED
    assert a.detail["reason"] == "scwep_not_submitted"
    assert "제출" in a.reasons[0]


def test_범위_미상이면_고발_불가():
    a = sb.classify([_doc(disciplines=(), conditional=[_lug_rule(method="MT")])], "PT", "CP-M1")
    assert a.state == sb.STATE_NOT_SUBMITTED
    assert a.detail["reason"] == "scwep_scope_unknown"


def test_공종이_비어_있으면_고발_불가():
    """청구 회차에 discipline 이 없는 경우에도 안전측."""
    a = sb.classify([_doc(conditional=[_lug_rule(method="MT")])], "PT", None)
    assert a.may_claim_overbilling is False


def test_구_형식_문서로는_고발_불가():
    """조건부를 인지하지 못하는 프롬프트로 뽑힌 문서는 '침묵했다' 고 말할 자격이 없다."""
    a = sb.classify([_doc(conditional=[_lug_rule(method="MT")], schema=1)], "PT", "CP-M1")
    assert a.state == sb.STATE_NOT_SUBMITTED
    assert a.detail["reason"] == "scwep_legacy_schema"


def test_신뢰도_없으면_임계_미달로_취급():
    """StandardDocument 에 신뢰도 컬럼이 없어 실제로 비는 경우가 생긴다.
    없는 값을 1.0 으로 보면 조용히 고발이 열린다."""
    d = _doc(conditional=[_lug_rule(method="MT")])
    d["extracted"].pop("extraction_confidence")
    a = sb.classify([d], "PT", "CP-M1")
    assert a.may_claim_overbilling is False
    assert a.detail["reason"] == "scwep_low_confidence"


def test_신뢰도가_True_여도_숫자로_보지_않음():
    d = _doc(conditional=[_lug_rule(method="MT")], confidence=True)
    a = sb.classify([d], "PT", "CP-M1")
    assert a.may_claim_overbilling is False


def test_needs_review_키가_없으면_재확인으로_취급():
    d = _doc(conditional=[_lug_rule(method="MT")])
    d["extracted"].pop("needs_review")
    a = sb.classify([d], "PT", "CP-M1")
    assert a.may_claim_overbilling is False


def test_규칙이_하나도_안_뽑힌_문서는_침묵의_증거가_아님():
    """샘플링률 한 줄만 있는 문서가 PT 침묵의 근거가 될 수는 없다."""
    doc = _doc(sampling=[{"applies_to": "Class 2 girth welds", "ndt_method": "RT",
                          "rate_pct": 10, "page": 5, "quote": "…"}])
    a = sb.classify([doc], "PT", "CP-M1")
    assert a.state == sb.STATE_NOT_SUBMITTED
    assert a.detail["reason"] == "scwep_extraction_empty"


def test_현재_mock_픽스처_모양의_문서는_고발을_열지_않는다():
    """tests/fixtures/hcx_mock.json 의 scwep_extract 와 같은 모양.
    구 형식 + general_rules 비어 있음 → 고발 불가여야 한다."""
    doc = {"document_no": "SCWEP-MOCK-001", "extracted": {
        "applicable_scope": {"disciplines": ["CP-M1", "CP-P1"]},
        "general_rules": [],
        "default_sampling_rates": [{"applies_to": "Class 2 girth welds", "ndt_method": "RT",
                                    "rate_pct": 10, "page": 5, "quote": "…"}],
        "extraction_confidence": 0.85, "needs_review": False}}
    a = sb.classify([doc], "PT", "CP-M1")
    assert a.may_claim_overbilling is False


# ─────────────────────────── 잡음 내성 ───────────────────────────

def test_청구_방법이_비면_판정_불가():
    a = sb.classify([_doc(conditional=[_lug_rule()])], "", "CP-M1")
    assert a.state == sb.STATE_NOT_SUBMITTED
    assert a.detail["reason"] == "billed_method_empty"


def test_망가진_항목이_섞여도_죽지_않음():
    doc = _doc(conditional=[None, "문자열", 42, _lug_rule()])
    a = sb.classify([doc], "PT", "CP-M1")
    assert a.state == sb.STATE_COVERED


def test_방법_대소문자_공백_정규화():
    a = sb.classify([_doc(conditional=[_lug_rule(method=" pt ")])], "Pt", "CP-M1")
    assert a.state == sb.STATE_COVERED


@pytest.mark.parametrize("disciplines,discipline,expected", [
    (("CP-M1",), "CP-M1", "yes"),
    (("cp-m1",), "CP-M1", "yes"),
    (("CP-E1",), "CP-M1", "no"),
    ((), "CP-M1", "unknown"),
    (("CP-M1",), None, "unknown"),
])
def test_범위_판정(disciplines, discipline, expected):
    assert sb.scope_state({"applicable_scope": {"disciplines": list(disciplines)}}, discipline) == expected


def test_여러_문서_중_하나만_면책해도_면책():
    docs = [_doc("SCWEP-A", conditional=[_lug_rule(method="MT")]),
            _doc("SCWEP-B", conditional=[_lug_rule()])]
    a = sb.classify(docs, "PT", "CP-M1")
    assert a.state == sb.STATE_COVERED
    assert a.covering_docs == ["SCWEP-B"]


def test_scope_state_prefers_kks(monkeypatch):
    ext = {"document_no": "NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E", "applicable_scope": {"disciplines": []}}
    assert sb.scope_state(ext, None, drawing_no="NP.D.P000.1.0UMA&&LAH10&.052.DC.0001.E") == "yes"
    assert sb.scope_state(ext, None, drawing_no="NP.D.P000.1.0UGB95GML90&.052.DC.0001.E") == "no"
    assert sb.scope_state(ext, None, drawing_no=None) == "unknown"
    assert sb.scope_state(ext, "CP-M1", drawing_no=None) == "unknown"        # discipline 만으론 못 정함 (문서에 없음)


def test_scope_state_tolerates_string_kks_field():
    """LLM 이 applicable_scope.kks 를 dict 가 아니라 문자열로 내도 죽지 않고 그 값을 raw 로 쓴다."""
    ext = {"document_no": "SCWEP-7", "applicable_scope": {"kks": "0UMA&&LAH&&&", "disciplines": []}}
    assert sb.scope_state(ext, None, drawing_no="NP.D.P000.1.0UMA&&LAH10&.052.DC.0001.E") == "yes"
    ext2 = {"document_no": "SCWEP-7", "applicable_scope": {"kks": 12345, "disciplines": []}}
    assert sb.scope_state(ext2, None, drawing_no="NP.D.P000.1.0UMA&&LAH10&.052.DC.0001.E") == "unknown"
