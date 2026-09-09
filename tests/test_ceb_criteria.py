"""CEB 검사기준 파서 — 실측 25종 텍스트 기반 분류 테스트."""
from __future__ import annotations

from app.analyzers.ceb_criteria import parse_criteria, judge_billing_against_criteria


def test_a1_sp_clause():
    p = parse_criteria("According to SP 70.13330.2012 clause 10.4 table 10.6, each weld is checked")
    assert p.grade == "A1"
    assert p.standard == "SP 70.13330.2012"
    assert p.scope_pct == 100.0
    assert not p.needs_review


def test_a1_with_concealed_suffix():
    """#12 — §10.4 인용 + 은폐작업 병기 → 검사범위 조항(A1) 우선."""
    p = parse_criteria(
        "According to SP 70.13330.2012 clause 10.4 table 10.6, each weld is checked ; "
        "Concealed work accpetance certificates shall be drawn up in accordance with SP 70.13330.2012."
    )
    assert p.grade == "A1"


def test_a2_gost_2012():
    p = parse_criteria("GOST 23118-2012 6.18. TVisual and measuring inspection : 100%")
    assert p.grade == "A2"
    assert p.standard == "GOST 23118-2012"
    assert p.needs_review  # 적용판 확인 중
    assert p.scope_pct == 100.0


def test_a3_concealed():
    p = parse_criteria(
        "Concealed works acceptance certificates shall be duly drawn up for construction "
        "of the walls as per SP 70.13330.2012")
    assert p.grade == "A3"


def test_a4_critical():
    p = parse_criteria(
        "Examination certificates of critical sturctures shall be executed for the "
        "following activities as required by 70.13330.2012")
    assert p.grade == "A4"


def test_a5_embedded():
    p = parse_criteria(
        'Fabricate embedded parts according to SP 70.13330-2012 "Load-Bearing Structure"')
    assert p.grade == "A5"


def test_a6_general():
    p = parse_criteria(
        "The works shall be implemented and accepted in compliance with the following "
        'regulations : SP 70.13330.2012 "Load-Bearing Structures and Building Enclosures"')
    assert p.grade == "A6"
    assert p.needs_review


def test_b1_note_ref():
    p = parse_criteria(
        "100% visual and measurement inspection shall be performed for all welded joints. (E/5.2)")
    assert p.grade == "B1"
    assert p.note_ref == "E/5.2"
    assert p.needs_review


def test_b2_uncited_variants():
    for t in (
        "Visual inpspection of welded joints: 100%",      # 오타 포함 최다 변형
        "the 100% scope of visual inspection and measurement for all the joints",
        "visual inspection and measuement is 100% of all joints",  # 오타
        "Visual inspection and measurement is 100% of all points",
    ):
        p = parse_criteria(t)
        assert p.grade == "B2", f"{t!r} → {p.grade}"
        assert p.scope_pct == 100.0
        assert p.needs_review


def test_b2_entire_length():
    """#22 — 'entire length' 도 100% 범위 표현."""
    p = parse_criteria(
        "Perform quality control by visual measurement control along the entire length of the seams.")
    assert p.grade == "B2"


def test_b3_no_scope():
    p = parse_criteria("Visual Inspection of welded joints")
    assert p.grade == "B3"
    assert p.scope_pct is None


def test_empty():
    assert parse_criteria(None).grade == "EMPTY"
    assert parse_criteria("").grade == "EMPTY"


# ─────────────────────────── 판정 (과다 중심) ───────────────────────────


def test_judge_a1_vt_ok_and_ut_watch():
    p = parse_criteria("According to SP 70.13330.2012 clause 10.4 table 10.6, each weld is checked")
    findings = judge_billing_against_criteria(p, {"VT": 224})
    rules = [f["rule"] for f in findings]
    assert "billed_method_not_in_cited_criteria" not in rules   # VT 는 근거 성립
    assert "required_method_unbilled_info" in rules             # UT 미청구 = 정보성 감시
    info = [f for f in findings if f["rule"] == "required_method_unbilled_info"][0]
    assert info["severity"] == "info"                           # 문제 아님 (비용절감)


def test_judge_a1_pt_billed_needs_basis():
    """기준은 VT 인데 PT 청구 → 근거 조항 확인 (과다 방향 검토)."""
    p = parse_criteria("According to SP 70.13330.2012 clause 10.4 table 10.6, each weld is checked")
    findings = judge_billing_against_criteria(p, {"VT": 100, "PT": 4})
    rules = [f["rule"] for f in findings]
    assert "billed_method_not_in_cited_criteria" in rules


def test_judge_b2_billing_without_basis():
    p = parse_criteria("Visual inpspection of welded joints: 100%")
    findings = judge_billing_against_criteria(p, {"VT": 56})
    assert any(f["rule"] == "billing_without_cited_criteria" for f in findings)


def test_judge_unbilled_never_flags_as_problem():
    """미청구는 어떤 경우에도 review/과다 아님 — info 만 (비용절감 정책)."""
    p = parse_criteria("According to SP 70.13330.2012 clause 10.4 table 10.6, each weld is checked")
    findings = judge_billing_against_criteria(p, {})
    assert all(f["severity"] == "info" for f in findings)
