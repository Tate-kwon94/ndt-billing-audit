"""2026-09-05 특성화 테스트가 남겨둔 이상 동작 1·2·3·5·6 과 감사 ⑧ 을 닫는다.
test_compliance_baseline.py 의 해당 항목은 이 파일과 함께 뒤집는다."""
from __future__ import annotations

import pytest

from app.analyzers import compliance, scwep_basis

# autouse 픽스처가 _code_lookup 을 봉인하기 전에 원본을 붙잡아 둔다.
_REAL_CODE = compliance._code_lookup


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(compliance, "_scwep_lookup", lambda br: [])
    monkeypatch.setattr(compliance, "_code_lookup", lambda br: [])
    monkeypatch.setattr(scwep_basis, "load_docs", lambda s: [])
    scwep_basis.reset_cache()


def _rules(res): return [f["rule"] for f in res["findings"]]
ROW = {"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1", "discipline": "CP-M1"}


def test_empty_dict_report_is_no_match():                           # 이상동작 3
    res = compliance.evaluate(billing_row=ROW, matched_report={}, drawing_joint_requirement=None)
    assert "no_matching_report" in _rules(res)


def test_string_sampling_rate_does_not_crash():                    # 이상동작 6
    req = {"joint_no": "FW12", "required_ndt_json": {"items": [{"method": "PT", "sampling_rate_pct": "10%"}]}}
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=req)
    assert "billed_ndt_not_in_requirements" not in _rules(res)


def test_missing_extraction_is_not_overbilling():                  # 이상동작 5
    req = {"joint_no": "FW12", "required_ndt_json": None}
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=req)
    assert "drawing_requirement_missing" in _rules(res)
    assert not any(r.startswith("billed_ndt") for r in _rules(res))
    d = [f["details"] for f in res["findings"] if f["rule"] == "drawing_requirement_missing"][0]
    assert "추출값 없음" in d["note"] and "미발견" not in d["note"]   # 자기모순 문구 금지


def test_truly_no_ndt_required_still_goes_to_gate():
    req = {"joint_no": "FW12", "required_ndt_json": {"items": []}}
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=req)
    assert "billed_ndt_basis_not_submitted" in _rules(res)      # 도면 공집합 게이트 → 미제출


@pytest.mark.parametrize("bill_dwg,rep_dwg,expect", [
    ("NP.D.N000.1.0UMA.021.ke.0001.E", None, True),             # 이상동작 2: 소문자
    ("NP.D.X..052.DC.0001.E", "NP.D.N000.1.0UMA.021.KE.0001.E", True),   # 이상동작 1: 성적서측
    ("NP.D.X..052.DC.0001.E", None, False),
    ("NP.D.X..052.MKE.0001.E", None, False),                     # 토큰 부분일치는 아님
])
def test_ke_misentry_detection(bill_dwg, rep_dwg, expect):
    row = {**ROW, "drawing_no": bill_dwg}
    rep = {"drawing_no": rep_dwg, "joints": []} if rep_dwg else None
    res = compliance.evaluate(billing_row=row, matched_report=rep, drawing_joint_requirement=None)
    assert ("drawing_no_is_ke_misentry" in _rules(res)) is expect


def test_untraceable_citation_is_needs_review(monkeypatch):        # 감사 ⑧
    from app.extractors import code_indexer
    monkeypatch.setattr(code_indexer, "search",
                        lambda q, top_k=3: [{"doc": "GOST-1", "page": 3, "confidence": 0.95,
                                             "text": "VT of welded joints shall be 100 % for all types."}])

    class R: parsed = {"found_in_context": True,
                       "citations": [{"doc": "GOST-1", "page": 3,
                                      "quote": "VT of welded joints shall be 100 % for all types"},
                                     {"doc": "GOST-1", "page": 99, "quote": "환각?"}]}
    monkeypatch.setattr(compliance, "call", lambda stage, payload: R())
    refs = _REAL_CODE(ROW)
    assert [r["needs_review"] for r in refs] == [False, True]
    assert refs[1]["chunk_source"] == "untraceable"
    # 인용문이 원문에 실재하면 통과, 검색에 없는 쪽을 댄 인용은 확인 실패 (2026-09-07 파생 검토)
    assert [r["quote_verified"] for r in refs] == [True, False]


_KE = "NP.D.P002.1.0UMA&&LAH&&&.015.KE.0001.E"


def test_ke_misentry_number_is_not_used_as_kks_scope_key(monkeypatch):
    """C2 — KE 오기 도면번호로 SCWEP 범위를 열어 같은 행을 과다청구로 확정하면 안 된다.

    KE 는 도면이 아니라 절차서 번호다. 그 번호를 KKS 범위 키로 쓰면 우연히 같은 계통의
    SCWEP 가 '이 공종 문서' 로 인정되어 근거 게이트가 열리고, 도구가 방금 '도면번호가
    틀렸다' 고 지적한 바로 그 행이 하드 위반(NONCOMPLIANT)으로 확정된다.
    """
    monkeypatch.setattr(scwep_basis, "load_docs", lambda s: [{
        "document_no": _KE, "file_path": "scwep.pdf",
        "extracted": {
            "_schema_version": 2, "document_no": _KE, "needs_review": False,
            "extraction_confidence": 0.9,
            "conditional_ndt_requirements": [
                {"ndt_method": "RT", "trigger": "온도 상승 시", "quote": "…RT…", "confidence": 0.9}],
            "general_rules": [],
        }}])
    scwep_basis.reset_cache()
    row = {**ROW, "drawing_no": _KE}
    req = {"joint_no": "FW12", "required_ndt_json": {"items": [{"method": "VT"}]}}
    res = compliance.evaluate(billing_row=row, matched_report=None, drawing_joint_requirement=req)
    rules = _rules(res)
    assert "drawing_no_is_ke_misentry" in rules
    assert "billed_ndt_not_in_requirements" not in rules
    assert "billed_ndt_basis_not_submitted" in rules
