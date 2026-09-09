"""과다청구 근거 게이트 — evaluate() 수준에서 네 갈래 판정과 두 결함 수정을 고정한다.

test_scwep_basis.py 가 판정기(순수 함수)를, 이 파일이 **compliance.evaluate 에 실제로 배선된
결과**를 본다. 둘이 어긋나면 게이트가 공회전하는 것이다.

한 문장: **근거 사슬이 완전할 때만 billed_ndt_not_in_requirements(하드) 가 나온다.**
"""
from __future__ import annotations

import pytest

from app.analyzers import compliance, scwep_basis


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(compliance, "_scwep_lookup", lambda br: [])
    monkeypatch.setattr(compliance, "_code_lookup", lambda br: [])
    scwep_basis.reset_cache()


def _rules(res): return [f["rule"] for f in res["findings"]]
def _d(res, rule):
    hits = [f["details"] for f in res["findings"] if f["rule"] == rule]
    assert len(hits) == 1, (rule, _rules(res)); return hits[0]
def _req(*methods, flagged=False):
    d = {"joint_no": "FW12", "required_ndt_json": {"items": [{"method": m} for m in methods]}}
    if flagged: d["_drawing_set_needs_review"] = True
    return d
def _scwep(monkeypatch, *, conditional=(), general=(), disciplines=("CP-M1",), schema=2, conf=0.9, nr=False):
    doc = {"document_no": "SCWEP-7", "extracted": {
        "applicable_scope": {"disciplines": list(disciplines)}, "_schema_version": schema,
        "needs_review": nr, "extraction_confidence": conf,
        "conditional_ndt_requirements": list(conditional), "general_rules": list(general)}}
    monkeypatch.setattr(scwep_basis, "load_docs", lambda s: [doc])
LUG_PT = {"rule_id": "S-7", "trigger": "임시 러그 제거 후", "ndt_method": "PT",
          "quote": "러그 제거부는 PT 를 시행한다.", "page": 12, "confidence": 0.9}
ROW = {"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1", "discipline": "CP-M1"}


# ─────────────────────────── 네 갈래 ───────────────────────────

def test_근거_있음_러그_제거_후_PT(monkeypatch):
    """도메인 사례 그 자체. 도면엔 VT 만, 청구는 PT, 제출 SCWEP 가 러그 제거 후 PT 를 요구."""
    _scwep(monkeypatch, conditional=[LUG_PT])
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=_req("VT"))
    assert "billed_ndt_covered_by_scwep" in _rules(res)
    assert "billed_ndt_not_in_requirements" not in _rules(res)
    d = _d(res, "billed_ndt_covered_by_scwep")
    assert d["basis_state"] == "covered" and d["basis_docs"] == ["SCWEP-7"]
    assert d["trigger"] == "임시 러그 제거 후"
    assert d["scwep_refs"][0]["quote"].startswith("러그 제거부")
    assert "과다청구 아님" in d["recommended_action"]


def test_불명확_일반규칙만(monkeypatch):
    _scwep(monkeypatch, general=[{"ndt_method": "PT", "topic": "procedure", "quote": "…"}])
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=_req("VT"))
    assert "billed_ndt_basis_unclear" in _rules(res)
    assert "billed_ndt_not_in_requirements" not in _rules(res)


def test_미제출_SCWEP_0건(monkeypatch):
    monkeypatch.setattr(scwep_basis, "load_docs", lambda s: [])
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=_req("VT"))
    d = _d(res, "billed_ndt_basis_not_submitted")
    assert d["basis_reason"] == "scwep_not_submitted"
    assert "제출 요청" in d["recommended_action"]


def test_근거_밖_확정은_사슬이_완전할_때만(monkeypatch):
    """제출됐고·범위 맞고·v2 이고·조항이 뽑혔는데 PT 에 침묵 + 도면 요구가 비어 있지 않음 → 확정."""
    _scwep(monkeypatch, conditional=[{**LUG_PT, "ndt_method": "MT"}])
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=_req("VT"))
    d = _d(res, "billed_ndt_not_in_requirements")
    assert d["basis_state"] == "no_basis_found" and d["basis_docs"] == ["SCWEP-7"]
    assert d["note"] == "도면 미요구 NDT 'PT' 청구 — 과다 청구 의심"        # 예전 문구 바이트 동일
    assert "제출 SCWEP SCWEP-7 에도 근거 없음" in d["recommended_action"]


# ─────────────────────────── 도면측 게이트 ───────────────────────────

def test_도면_요구가_공집합이면_확정_안_함(monkeypatch):
    """SCWEP 가 침묵해도 도면 추출이 비어 있으면(추출 실패 가능) 확정하지 않는다."""
    _scwep(monkeypatch, conditional=[{**LUG_PT, "ndt_method": "MT"}])
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=_req())
    d = _d(res, "billed_ndt_basis_not_submitted")
    assert d["basis_reason"] == "drawing_requirement_empty"
    assert d["needs_confirm_extraction"] is True
    assert "도면측 확인 먼저" in d["recommended_action"]


def test_도면_세트가_재확인_대상이면_확정_안_함(monkeypatch):
    _scwep(monkeypatch, conditional=[{**LUG_PT, "ndt_method": "MT"}])
    res = compliance.evaluate(billing_row=ROW, matched_report=None,
                              drawing_joint_requirement=_req("VT", flagged=True))
    assert _d(res, "billed_ndt_basis_not_submitted")["basis_reason"] == "drawing_set_flagged"


# ─────────────────────────── 스위치 ───────────────────────────

def test_스위치_끄면_예전_동작(monkeypatch):
    """require_submitted_basis=false → SCWEP 0건이어도 즉시 확정 (코드 패치 없는 되돌리기)."""
    monkeypatch.setattr(scwep_basis, "load_docs", lambda s: [])
    monkeypatch.setattr(compliance, "matching_rules", lambda: {"compliance": {
        "overbilling_claim": {"require_submitted_basis": False,
                              "never_claim_on_empty_drawing_requirement": False}}})
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=_req("VT"))
    assert "billed_ndt_not_in_requirements" in _rules(res)
    assert _d(res, "billed_ndt_not_in_requirements")["basis_reason"] == "gate_disabled"


def test_구형식_인정_스위치(monkeypatch):
    """require_conditional_schema=false → 구 형식(v1) SCWEP 도 침묵의 증거로 인정."""
    _scwep(monkeypatch, general=[{"ndt_method": "MT", "topic": "procedure", "quote": "…"}], schema=1)
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=_req("VT"))
    assert "billed_ndt_basis_not_submitted" in _rules(res)           # 기본: 구형식은 고발 불가
    monkeypatch.setattr(compliance, "matching_rules", lambda: {"compliance": {
        "overbilling_claim": {"require_conditional_schema": False}}})
    res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=_req("VT"))
    assert "billed_ndt_not_in_requirements" in _rules(res)


# ─────────────────────────── 결함 수정 2건 ───────────────────────────

@pytest.mark.parametrize("drawing_method", ["VT", "VMC", "Visual", "visual test", "Visual and Measuring"])
def test_도면측_방법_정규화_VMC_는_VT(monkeypatch, drawing_method):
    """실측(2026-09-05): 도면이 'VMC'·'Visual' 이라고 적히면 정상 VT 청구가 과다청구로 확정됐다."""
    monkeypatch.setattr(scwep_basis, "load_docs", lambda s: [])
    res = compliance.evaluate(billing_row={**ROW, "ndt_method": "VT"}, matched_report=None,
                              drawing_joint_requirement=_req(drawing_method))
    assert not any(r.startswith("billed_ndt_") for r in _rules(res)), (drawing_method, _rules(res))


def test_도면측_정규화가_미청구_정보성에도_적용(monkeypatch):
    """도면 'VMC' + 청구 VT 면 '아직 요구가 남았다(VT)' 고 하지 않는다."""
    monkeypatch.setattr(scwep_basis, "load_docs", lambda s: [])
    res = compliance.evaluate(billing_row={**ROW, "ndt_method": "VT"}, matched_report=None,
                              drawing_joint_requirement=_req("VMC"))
    assert "required_ndt_missing" not in _rules(res)


def test_단일_Joint_성적서로_엉뚱한_Joint_대조_안_함(monkeypatch):
    monkeypatch.setattr(scwep_basis, "load_docs", lambda s: [])
    res = compliance.evaluate(
        billing_row={"joint_no": "FW99", "ndt_method": "PT", "result": "ACC", "welder_id": "DW001"},
        matched_report={"report_no": "R", "joints": [{"joint_no": "FW12", "result": "REJ", "welder_id": "DW777"}]},
        drawing_joint_requirement=_req("PT"))
    assert "result_mismatch" not in _rules(res) and "welder_mismatch" not in _rules(res)


def test_같은_Joint_면_여전히_대조한다(monkeypatch):
    """수정이 정상 경로를 죽이지 않았는지 — Joint 가 맞으면 불일치를 그대로 잡는다."""
    monkeypatch.setattr(scwep_basis, "load_docs", lambda s: [])
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "result": "ACC", "welder_id": "DW001"},
        matched_report={"report_no": "R", "joints": [{"joint_no": "FW12", "result": "REJ", "welder_id": "DW777"}]},
        drawing_joint_requirement=_req("PT"))
    assert "result_mismatch" in _rules(res) and "welder_mismatch" in _rules(res)


# ─────────────────────────── 원칙 자체 ───────────────────────────

def test_저신뢰_SCWEP_로는_절대_확정_안_됨(monkeypatch):
    """모듈 전체를 관통하는 한 문장의 evaluate 수준 증명."""
    for kw in ({"conf": 0.3}, {"nr": True}, {"schema": 1}, {"disciplines": ()}):
        _scwep(monkeypatch, conditional=[{**LUG_PT, "ndt_method": "MT"}], **kw)
        res = compliance.evaluate(billing_row=ROW, matched_report=None, drawing_joint_requirement=_req("VT"))
        assert "billed_ndt_not_in_requirements" not in _rules(res), kw
