"""검토 엑셀 컬럼 — 근거_상태 신설과 "항상 빈칸" 이던 3개 컬럼 복구 (6단계).

감사(2026-09-04)에서 도면_요구NDT·누락_검사·과다_검사가 **키 이름 불일치로 항상 비어 있던** 것이
확인됐다. writer 는 'requested' / 'missing_methods' 를 읽었고 compliance 는 'requested_ndt' /
'still_required' 를 냈다. 이 파일은 그 키 이름을 실제 finding 모양으로 고정한다.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

from app.report import excel_writer as ew


def _finding(findings, *, drawing_requirement=None, authority_refs=None, explanation=None,
             verdict="SUSPECT", risk=45, action="조치", summary="요약"):
    return NS(citations_json={"findings": findings, "authority_refs": authority_refs or [],
                              "drawing_requirement": drawing_requirement},
              explanation_json=explanation, verdict=verdict, risk_score=risk,
              recommended_action=action, summary=summary, needs_review=True, review_reasons_json=None)


def _col(name):
    return ew.REVIEW_COLUMNS.index(name)


def test_컬럼_순서_근거_상태는_판정_바로_뒤_메모는_맨_끝():
    assert ew.REVIEW_COLUMNS[_col("적합성_판정") + 1] == "근거_상태"
    assert ew.REVIEW_COLUMNS[-1] == "검토자_메모"
    assert "누락_검사" not in ew.REVIEW_COLUMNS and "타행_요구NDT" in ew.REVIEW_COLUMNS


def test_행_값_길이는_컬럼_수와_같다():
    vals = ew._row_values(NS(), None, None, None)
    assert len(vals) == len(ew.REVIEW_COLUMNS)


def test_과다_검사_컬럼이_실제로_채워진다():
    """예전엔 'requested' 를 읽어 항상 빈칸. 실제 키는 'requested_ndt'."""
    f = _finding([{"rule": "billed_ndt_not_in_requirements",
                   "details": {"requested_ndt": "UT", "basis_state": "no_basis_found"}}])
    vals = ew._row_values(NS(), None, f, None)
    assert vals[_col("과다_검사")] == "UT"
    assert vals[_col("근거_상태")] == "근거 밖 — 과다청구"


def test_근거_미제출_행도_과다_검사는_채우고_상태로_뜻을_가른다():
    f = _finding([{"rule": "billed_ndt_basis_not_submitted",
                   "details": {"requested_ndt": "PT", "basis_state": "not_submitted"}}])
    vals = ew._row_values(NS(), None, f, None)
    assert vals[_col("과다_검사")] == "PT"
    assert vals[_col("근거_상태")] == "근거 미제출 — 제출 요청"


def test_타행_요구NDT_는_still_required_를_읽는다():
    """예전 키 'missing_methods' 는 compliance 가 만든 적이 없다."""
    f = _finding([{"rule": "required_ndt_missing", "details": {"still_required": ["RT", "VT"]}}])
    vals = ew._row_values(NS(), None, f, None)
    assert vals[_col("타행_요구NDT")] == "RT, VT"


def test_도면_요구NDT_는_citations_의_drawing_requirement_를_읽는다():
    f = _finding([], drawing_requirement={"required_ndt_json": {"items": [{"method": "vt"}, {"method": "RT"}]}})
    vals = ew._row_values(NS(), None, f, None)
    assert vals[_col("도면_요구NDT")] == "RT, VT"


def test_도면_요구NDT_fallback_은_과다청구_finding_의_목록():
    f = _finding([{"rule": "billed_ndt_basis_not_submitted",
                   "details": {"requested_ndt": "PT", "required_ndt_by_drawing": ["VT"], "basis_state": "not_submitted"}}])
    assert ew._row_values(NS(), None, f, None)[_col("도면_요구NDT")] == "VT"


def test_근거_문서는_LLM_인용이_없으면_결정론_refs_로():
    f = _finding([{"rule": "billed_ndt_covered_by_scwep", "details": {"basis_state": "covered"}}],
                 authority_refs=[{"authority_level": 2, "doc": "SCWEP-7", "page": 12, "section": "러그 제거"}])
    vals = ew._row_values(NS(), None, f, None)
    assert vals[_col("근거_문서")] == "SCWEP-7"
    assert vals[_col("근거_조항/페이지")] == "러그 제거/p.12"


def test_과다청구_확정_행에는_SCWEP_인용을_찍지_않는다():
    """고발 옆에 SCWEP 이 붙으면 그 SCWEP 이 청구를 인정한 것처럼 읽힌다."""
    f = _finding([{"rule": "billed_ndt_not_in_requirements", "details": {"basis_state": "no_basis_found"}}],
                 authority_refs=[{"authority_level": 2, "doc": "SCWEP-7", "page": 12},
                                 {"authority_level": 3, "doc": "GOST 10922", "page": 5}])
    vals = ew._row_values(NS(), None, f, None)
    assert vals[_col("근거_문서")] == "GOST 10922"


def test_finding_없으면_전부_None():
    vals = ew._row_values(NS(), None, None, None)
    for name in ("도면_요구NDT", "타행_요구NDT", "과다_검사", "근거_상태", "근거_문서"):
        assert vals[_col(name)] is None
