"""compliance.evaluate() 특성화(characterization) 테스트 — SCWEP 기준게이트 리팩터 전 현행 동작 고정.

이 파일이 존재하는 이유:
  app/analyzers/compliance.py 의 evaluate() 는 OK / SUSPECT / NONCOMPLIANT verdict 를
  만들어내는 findings 의 유일한 생산자인데, 지금까지 테스트가 0 건이었다.
  곧 과다청구(billed_ndt_not_in_requirements) 분기를 4 갈래로 쪼개는 작업이 들어간다.
  그 전에 **현재 동작을 그대로 못박아** 변경 범위를 증명 가능하게 만드는 것이 목적이다.

읽는 법:
  - 여기 적힌 것은 "이래야 한다(should)" 가 아니라 "지금 이렇다(is)" 이다.
    명백히 이상해 보이는 동작도 그대로 고정했다 (아래 '알려진 이상 동작' 참조).
  - **이 파일이 깨졌다 = 의도하지 않은 것을 바꿨다.**
    리팩터로 바꾸려던 동작이면 이 파일을 같이 고치면서 무엇을 왜 바꿨는지 적고,
    바꿀 생각이 없었던 동작이면 코드를 되돌려야 한다.
  - findings 의 details 키 이름은 하류 Excel writer 가 그대로 읽는다 (키 불일치 사고 이력 있음).
    그래서 키 이름 자체를 dict 비교로 고정하는 테스트(test_finding_details_key_names)를 둔다.

알려진 이상 동작 (버그로 의심되나 특성화 원칙에 따라 그대로 고정함):
  1) 청구 drawing_no 가 있으면 성적서 쪽 KE 오기는 아예 검사되지 않는다 (or 단락평가).
  2) '.KE.' 검사가 대소문자 구분 — 소문자 '.ke.' 는 잡히지 않는다 (_normalize 미적용).
  3) matched_report 가 {} 이면 no_matching_report 도 result/welder 대조도 전부 건너뛴다 (is None vs 진리값 불일치).
  4) 청구 Joint 가 성적서에 없는데 성적서 Joint 가 딱 1 개면 '단일 Joint 성적서' fallback 이
     엉뚱한 Joint 와 대조해 result_mismatch(하드 위반) 를 만들어낸다.
  5) required_ndt_json 이 None/{}/[] (= 도면 추출 실패 가능성) 인 경우와 진짜 NDT 미요구인 경우가
     구분되지 않고 똑같이 과다청구로 보고된다.
  6) sampling_rate_pct 가 문자열이면 evaluate() 자체가 TypeError 로 죽는다.

hermetic: DB·네트워크·LLM 접근 없음. _scwep_lookup / _code_lookup / scwep_basis.load_docs 를
autouse 픽스처로 봉인한다.

5단계(2026-09-05, SCWEP 근거 게이트)에서 **의도적으로 뒤집은** 항목 — 여기 적힌 것만 바뀌었다:
  · 과다청구 분기: SCWEP 0건이면 billed_ndt_not_in_requirements 대신
    billed_ndt_basis_not_submitted (SUSPECT). 확정은 근거 사슬이 완전할 때만 (test_overbilling_gate.py).
  · 이상 동작 4 (단일 Joint fallback 이 엉뚱한 Joint 와 대조) → **수정됨**. 청구 Joint 가 있으면
    성적서에 없을 때 None. fallback 은 청구에 Joint 가 아예 없을 때만.
  · 도면측 NDT 방법이 청구측과 같은 표(templates.yaml)로 정규화된다 — 'VMC'·'Visual' 은 VT.
이상 동작 1·2·3·5·6 은 2026-09-05 후속 수정에서 뒤집었다 (tests/test_compliance_fixes.py).
감사 ⑧(추적 불가 인용)도 같은 후속 수정에서 뒤집었다 — 검색 스니펫의 (doc,page) 로 되찾지
못한 인용은 더 이상 '신뢰할 만한 인용'으로 취급되지 않고 chunk_source='untraceable',
confidence=None, needs_review=True 가 된다.
"""
from __future__ import annotations

import pytest

from app.analyzers import compliance

# autouse 픽스처가 덮어쓰기 전에 원본을 붙잡아 둔다 (full-path 테스트에서 되살리기 위함).
_REAL_SCWEP = compliance._scwep_lookup
_REAL_CODE = compliance._code_lookup

_KE = "NP.D.N000.1.0UMA.021.KE.0001.E"
_DC = "NP.D.X..052.DC.0001.E"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """권위계층 조회(DB·검색·LLM)를 전부 봉인. 개별 테스트가 필요하면 다시 덮어쓴다.

    _consult_authority_hierarchy 자체는 패치하지 않는다 — scwep→code 연결 순서를
    실제 코드로 계속 검증하기 위해서다.
    """
    monkeypatch.setattr(compliance, "_scwep_lookup", lambda billing_row: [])
    monkeypatch.setattr(compliance, "_code_lookup", lambda billing_row: [])
    # 5단계: 과다청구 분기가 scwep_basis.load_docs(session) 로 DB 를 본다. 기본은 "제출 SCWEP 0건".
    from app.analyzers import scwep_basis
    monkeypatch.setattr(scwep_basis, "load_docs", lambda session: [])
    scwep_basis.reset_cache()


# ─────────────────────────── 헬퍼 ───────────────────────────


def _rules(res) -> list[str]:
    """findings 를 rule 이름 목록으로 (순서 유지 — 순서도 결정론적이라 고정 대상)."""
    return [f["rule"] for f in res["findings"]]


def _details(res, rule) -> dict:
    hits = [f["details"] for f in res["findings"] if f["rule"] == rule]
    assert len(hits) == 1, f"{rule} 이 {len(hits)} 건 (1 건 기대)"
    return hits[0]


def _req(*methods, joint_no="FW12", items=None):
    """pipeline._find_joint_requirement 반환 모양 중 evaluate() 가 실제로 읽는 부분만."""
    if items is None:
        items = [{"method": m} for m in methods]
    return {"joint_no": joint_no, "required_ndt_json": {"items": items}}


def _report(joints, **extra):
    """pipeline._to_report_dict 반환 모양 중 evaluate() 가 실제로 읽는 부분만."""
    return {"report_no": "77-005PT", "joints": joints, **extra}


# ─────────────────────────── 호출 규약·반환 봉투 ───────────────────────────


def test_evaluate_is_keyword_only():
    """세 인자 모두 키워드 전용 — 위치 인자 호출은 TypeError."""
    with pytest.raises(TypeError):
        compliance.evaluate({"joint_no": "FW12"}, None, None)


def test_return_envelope_keys_and_identity_passthrough():
    """반환은 정확히 4 키. drawing_requirement / matched_report 는 인자 객체 그대로(동일 객체)."""
    rep = _report([])
    dr = _req("PT")
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT"},
        matched_report=rep,
        drawing_joint_requirement=dr,
    )
    assert list(res.keys()) == ["findings", "authority_refs", "drawing_requirement", "matched_report"]
    assert res["drawing_requirement"] is dr
    assert res["matched_report"] is rep
    assert res["authority_refs"] == []
    assert res["findings"] == []


def test_clean_row_produces_zero_findings():
    """도면 요구=PT, 청구=PT, 성적서 판정·용접사 일치 → findings 완전 무음 (OK 기준선)."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": _DC,
                     "result": "ACC", "welder_id": "DW001"},
        matched_report=_report([{"joint_no": "FW12", "result": "ACC", "welder_id": "DW001"}]),
        drawing_joint_requirement=_req("PT"),
    )
    assert res["findings"] == []
    assert res["authority_refs"] == []


# ─────────────────────────── 0) KE 오기 ───────────────────────────


def test_ke_misentry_from_billing_drawing_no():
    """청구 drawing_no 에 '.KE.' → drawing_no_is_ke_misentry 가 **맨 앞**에 붙는다."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": _KE},
        matched_report=None,
        drawing_joint_requirement=None,
    )
    assert _rules(res) == ["drawing_no_is_ke_misentry", "no_matching_report",
                           "drawing_requirement_missing"]
    d = _details(res, "drawing_no_is_ke_misentry")
    assert set(d) == {"wrong_value", "note", "recommended_action"}
    assert d["wrong_value"] == _KE
    assert d["note"] == (
        "Detailed Drawing 컬럼에 KE 식별자가 잘못 기입됨 — "
        "KE 는 WEP 또는 SCWEP(시공 절차 관련 서류)이며 도면이 아님. "
        "모체 도면이 별도 존재."
    )
    assert d["recommended_action"] == (
        "시공사에 해당 청구 행의 Detailed Drawing 을 모체 도면번호로 수정 요청"
    )


def test_ke_misentry_falls_back_to_report_drawing_no():
    """청구에 drawing_no 가 없으면 성적서 drawing_no 로 KE 검사 — wrong_value 는 성적서 값."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT"},
        matched_report=_report([], drawing_no=_KE),
        drawing_joint_requirement=None,
    )
    assert _rules(res) == ["drawing_no_is_ke_misentry", "drawing_requirement_missing"]
    assert _details(res, "drawing_no_is_ke_misentry")["wrong_value"] == _KE


def test_ke_in_report_is_now_checked_even_with_non_ke_billing_drawing_no():
    """뒤집음: 청구 drawing_no 가 있어도 성적서측 KE 값이 이제는 별도로 검사된다 (이상 동작 1 해소)."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": _DC},
        matched_report=_report([], drawing_no=_KE),
        drawing_joint_requirement=None,
    )
    assert _rules(res) == ["drawing_no_is_ke_misentry", "drawing_requirement_missing"]
    assert _details(res, "drawing_no_is_ke_misentry")["wrong_value"] == _KE


def test_ke_check_is_now_case_insensitive_by_token():
    """뒤집음: '.ke.' 소문자도 점으로 나눈 토큰이 'KE' 면 잡힌다 (이상 동작 2 해소, 대소문자 무시)."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "np.d.x.021.ke.0001.e"},
        matched_report=None,
        drawing_joint_requirement=None,
    )
    assert _rules(res) == ["drawing_no_is_ke_misentry", "no_matching_report",
                           "drawing_requirement_missing"]
    assert _details(res, "drawing_no_is_ke_misentry")["wrong_value"] == "np.d.x.021.ke.0001.e"


# ─────────────────────────── 1) 성적서 미매칭 ───────────────────────────


def test_no_matching_report_details():
    """matched_report is None → no_matching_report. 키는 billing_joint / method 두 개뿐."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT"},
        matched_report=None,
        drawing_joint_requirement=_req("PT"),
    )
    assert _rules(res) == ["no_matching_report"]
    assert _details(res, "no_matching_report") == {"billing_joint": "FW12", "method": "PT"}
    assert res["matched_report"] is None


def test_no_matching_report_method_is_normalized_empty_string():
    """ndt_method 가 없으면 method 는 None 이 아니라 빈 문자열 (_normalize 결과)."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12"},
        matched_report=None,
        drawing_joint_requirement=_req("PT"),
    )
    assert _details(res, "no_matching_report")["method"] == ""


def test_empty_dict_report_is_now_treated_as_no_match():
    """뒤집음: matched_report={} 도 이제 '매칭 없음' 이다 (이상 동작 3 해소, falsy 취급)."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "result": "ACC", "welder_id": "DW001"},
        matched_report={},
        drawing_joint_requirement=_req("PT"),
    )
    assert _rules(res) == ["no_matching_report"]
    assert res["matched_report"] is None


# ─────────────────────────── 2) 도면 기준 없음 + 권위계층 ───────────────────────────


def test_drawing_requirement_missing_consults_authority_hierarchy(monkeypatch):
    """도면 기준 None → drawing_requirement_missing + 권위계층을 scwep → code 순으로 1 회씩 조회."""
    seen = []
    monkeypatch.setattr(compliance, "_scwep_lookup", lambda br: seen.append(("scwep", br)) or [])
    monkeypatch.setattr(compliance, "_code_lookup", lambda br: seen.append(("code", br)) or [])
    row = {"joint_no": "FW12", "ndt_method": "PT"}
    res = compliance.evaluate(billing_row=row, matched_report=None, drawing_joint_requirement=None)

    assert [s[0] for s in seen] == ["scwep", "code"]
    assert all(s[1] is row for s in seen)   # billing_row 를 그대로 넘긴다
    d = _details(res, "drawing_requirement_missing")
    assert set(d) == {"billing_joint", "drawing_no_in_billing", "drawing_no_in_report", "note"}
    assert d["billing_joint"] == "FW12"
    assert d["drawing_no_in_billing"] is None
    assert d["drawing_no_in_report"] is None
    assert d["note"] == "청구·성적서 모두 drawing_no 없음 · 적합성 자동 판정 불가, 검토자 수동 확인 필요"


def test_drawing_requirement_missing_note_when_billing_drawing_no_present():
    """drawing_no 는 있는데 DB 조회 실패 → note 가 '미발견 (조회시도 ...)' 문구로 바뀐다."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": _DC},
        matched_report=_report([]),
        drawing_joint_requirement=None,
    )
    d = _details(res, "drawing_requirement_missing")
    assert d["drawing_no_in_billing"] == _DC
    assert d["drawing_no_in_report"] is None
    assert d["note"] == (
        f"도면 DB 에서 미발견 (조회시도 drawing_no='{_DC}')"
        " · 적합성 자동 판정 불가, 검토자 수동 확인 필요"
    )


def test_drawing_requirement_missing_note_uses_report_drawing_no():
    """청구에 drawing_no 가 없으면 note 의 '조회시도' 값은 성적서 drawing_no."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT"},
        matched_report=_report([], drawing_no=_DC),
        drawing_joint_requirement=None,
    )
    d = _details(res, "drawing_requirement_missing")
    assert d["drawing_no_in_billing"] is None
    assert d["drawing_no_in_report"] == _DC
    assert f"조회시도 drawing_no='{_DC}'" in d["note"]


def test_authority_hierarchy_not_consulted_when_requirement_exists(monkeypatch):
    """도면 기준이 있으면 권위계층은 아예 호출되지 않고 authority_refs 는 [] 그대로."""
    def _boom(br):
        raise AssertionError("도면 기준이 있으면 권위계층을 조회하면 안 된다")

    monkeypatch.setattr(compliance, "_scwep_lookup", _boom)
    monkeypatch.setattr(compliance, "_code_lookup", _boom)
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT"},
        matched_report=None,
        drawing_joint_requirement=_req("PT"),
    )
    assert res["authority_refs"] == []
    assert "low_confidence_code_ref" not in _rules(res)


def test_authority_refs_concatenated_scwep_then_code(monkeypatch):
    """authority_refs = scwep 결과 + code 결과 (가공 없이 그대로 이어붙임)."""
    s_ref = {"authority_level": 2, "doc": "SCWEP-001", "page": 5}
    c_ref = {"authority_level": 3, "doc": "ASME-V", "page": 12, "needs_review": False}
    monkeypatch.setattr(compliance, "_scwep_lookup", lambda br: [s_ref])
    monkeypatch.setattr(compliance, "_code_lookup", lambda br: [c_ref])
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT"},
        matched_report=None,
        drawing_joint_requirement=None,
    )
    assert res["authority_refs"] == [s_ref, c_ref]
    assert "low_confidence_code_ref" not in _rules(res)


# ─────────────────── 2') 과다청구 — 이번 리팩터가 쪼갤 바로 그 분기 ───────────────────


def test_billed_method_in_requirements_no_overbilling_finding():
    """청구 NDT 가 도면 요구에 포함 → 과다청구 finding 없음."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req(items=[{"method": "PT", "sampling_rate_pct": 100}]),
    )
    assert _rules(res) == ["no_matching_report"]


def test_billed_method_matched_case_insensitively():
    """양쪽 모두 _normalize (strip+upper) 후 비교 — ' pt ' 와 'pt' 는 일치."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": " pt "},
        matched_report=None,
        drawing_joint_requirement=_req("pt"),
    )
    assert _rules(res) == ["no_matching_report"]


def test_billed_ndt_not_in_requirements_nonempty_required():
    """[리팩터 대상] 요구집합이 비어있지 않은데 청구 방법이 그 안에 없음 → empty_req=False 문구.

    같은 행에서 required_ndt_missing 도 함께 발생한다 (risk_scorer 는 60 + 5 를 더한다).
    """
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "UT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req("PT", "VT"),
    )
    # 5단계: SCWEP 0건 → 확정이 아니라 "근거 미제출". 확정 경로는 test_overbilling_gate.py 가 고정한다.
    assert _rules(res) == ["no_matching_report", "billed_ndt_basis_not_submitted",
                           "required_ndt_missing"]
    d = _details(res, "billed_ndt_basis_not_submitted")
    assert set(d) == {"requested_ndt", "required_ndt_by_drawing", "billing_joint",
                      "drawing_no", "note", "needs_confirm_extraction", "recommended_action",
                      "basis_state", "basis_docs", "basis_reason", "trigger"}
    assert d["requested_ndt"] == "UT"
    assert d["required_ndt_by_drawing"] == ["PT", "VT"]     # set 이 아니라 정렬된 list
    assert d["billing_joint"] == "FW12"
    assert d["drawing_no"] == "D1"
    assert d["needs_confirm_extraction"] is False
    assert d["basis_state"] == "not_submitted"
    assert d["basis_reason"] == "scwep_not_submitted"
    assert "과다청구로 단정하지 않음" in d["note"]
    assert d["recommended_action"].startswith("시공사에 근거 절차서(SCWEP) 제출 요청")


def test_billed_ndt_not_in_requirements_empty_required():
    """[리팩터 대상] 요구집합이 공집합 → 같은 rule 이름, 문구·플래그만 다름 (empty_req=True).

    이 행에서는 required_ndt_missing 이 발생하지 않는다 (요구가 아예 없으므로).
    """
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "RT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req(),
    )
    # 5단계: 공집합 + SCWEP 0건. SCWEP 게이트(not_submitted)가 도면 게이트보다 먼저 판정된다.
    assert _rules(res) == ["no_matching_report", "billed_ndt_basis_not_submitted"]
    d = _details(res, "billed_ndt_basis_not_submitted")
    assert d["required_ndt_by_drawing"] == []
    assert d["needs_confirm_extraction"] is True
    assert d["basis_reason"] == "scwep_not_submitted"


@pytest.mark.parametrize("dr", [
    {"joint_no": "FW12", "required_ndt_json": {}},
    {"joint_no": "FW12", "required_ndt_json": {"items": []}},
    {"joint_no": "FW12", "required_ndt_json": []},
])
def test_empty_req_variants_without_none_still_take_the_empty_req_branch(dr):
    """[이상 동작 5 잔존분] required_ndt_json 이 {}/[]/{"items":[]} (진짜 NDT 미요구로 읽을 수 있는 값)
    이면 여전히 과다청구 분기로 간다 — required_ndt_json 이 정확히 None(추출 실패)인 경우만
    뒤집혔다 (아래 test_required_ndt_json_none_is_now_extraction_missing 참조).
    """
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT"},
        matched_report=None,
        drawing_joint_requirement=dr,
    )
    d = _details(res, "billed_ndt_basis_not_submitted")     # 5단계: 확정 대신 미제출
    assert d["required_ndt_by_drawing"] == []
    assert d["needs_confirm_extraction"] is True
    assert "drawing_requirement_missing" not in _rules(res)


@pytest.mark.parametrize("dr", [
    {"joint_no": "FW12", "required_ndt_json": None},
    {},   # falsy 이고 required_ndt_json 키 자체가 없음 → dr.get(...) 도 None
])
def test_required_ndt_json_none_is_now_extraction_missing(dr):
    """뒤집음(이상 동작 5): required_ndt_json 이 None(추출 실패 가능성)이면 이제
    과다청구 분기가 아니라 drawing_requirement_missing 으로 간다 — 진짜 NDT 미요구
    ({}/[]/{"items":[]}) 와 구분된다."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT"},
        matched_report=None,
        drawing_joint_requirement=dr,
    )
    assert "drawing_requirement_missing" in _rules(res)
    assert not any(r.startswith("billed_ndt") for r in _rules(res))
    d = _details(res, "drawing_requirement_missing")
    assert "추출값 없음" in d["note"]


def test_required_items_without_method_are_dropped():
    """method 가 falsy 인 요구 항목은 전부 버려져 요구집합이 공집합이 된다."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req(items=[{"method": None}, {"method": ""},
                                              {"sampling_rate_pct": 50}]),
    )
    d = _details(res, "billed_ndt_basis_not_submitted")     # 5단계: 확정 대신 미제출
    assert d["required_ndt_by_drawing"] == []
    assert d["needs_confirm_extraction"] is True


def test_no_billed_method_suppresses_overbilling():
    """ndt_method 가 없으면 요구집합이 공집합이어도 과다청구 finding 이 나오지 않는다."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req(),
    )
    assert _rules(res) == ["no_matching_report"]


def test_no_billed_method_suppresses_required_ndt_missing():
    """ndt_method 가 없으면 미청구 정보성 finding 도 억제된다 (requested_method 가드)."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req("PT", "VT"),
    )
    assert _rules(res) == ["no_matching_report"]


# ─────────────────────────── 3) 미청구 NDT (정보성) ───────────────────────────


def test_required_ndt_missing_details():
    """도면 요구 중 이 행이 청구하지 않은 방법 → required_ndt_missing (정보성).

    키 이름이 과다청구 finding 과 다르다: this_row_method / still_required.
    """
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req("PT", "VT", "RT"),
    )
    assert _rules(res) == ["no_matching_report", "required_ndt_missing"]
    d = _details(res, "required_ndt_missing")
    assert set(d) == {"billing_joint", "drawing_no", "this_row_method", "still_required", "note"}
    assert d["billing_joint"] == "FW12"
    assert d["drawing_no"] == "D1"
    assert d["this_row_method"] == "PT"
    assert d["still_required"] == ["RT", "VT"]      # 정렬된 list, 자기 행 방법은 제외
    assert d["note"] == (
        "도면 요구 중 이 행에 미청구 NDT 있음 (['RT', 'VT']) — "
        "미청구는 비용절감으로 기성검토상 문제 아님 (정보성). "
        "향후 해당 방법 소급 청구 시 이 기록으로 기준·수행증빙 대조."
    )


# ─────────────────────────── 4) 샘플링률 (현재 no-op) ───────────────────────────


def test_sampling_rate_below_100_emits_nothing():
    """샘플링률 50% 요구여도 finding 이 하나도 나오지 않는다 (집계 모듈에 위임하는 빈 루프)."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req(items=[{"method": "PT", "sampling_rate_pct": 50}]),
    )
    assert _rules(res) == ["no_matching_report"]


def test_sampling_rate_as_string_no_longer_raises():
    """뒤집음(이상 동작 6): sampling_rate_pct 가 문자열이어도 evaluate() 는 더 이상 TypeError 로
    죽지 않는다 — 비수치는 float() 변환 실패로 건너뛰고(continue), 집계 단계에서 재확인 대상으로
    다룬다. 예전엔 pipeline.run() 의 광범위 except 가 TypeError 를 삼켜 해당 청구 행이 조용히
    사라졌었다."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req(items=[{"method": "PT", "sampling_rate_pct": "50"}]),
    )
    assert "billed_ndt_not_in_requirements" not in _rules(res)
    assert "billed_ndt_basis_not_submitted" not in _rules(res)


# ─────────────────────────── 5·6) 성적서 대조 ───────────────────────────


def test_result_mismatch():
    """청구 판정 ≠ 성적서 판정 → result_mismatch (_HARD_RULES → NONCOMPLIANT)."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "result": "ACC"},
        matched_report=_report([{"joint_no": "FW12", "result": "REJ", "welder_id": "DW001"}]),
        drawing_joint_requirement=_req("PT"),
    )
    assert _rules(res) == ["result_mismatch"]
    assert _details(res, "result_mismatch") == {"billing_result": "ACC", "report_result": "REJ"}


def test_result_compared_normalized_but_details_carry_raw_values():
    """비교는 정규화 후, details 에는 원본 문자열 그대로 들어간다."""
    same = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "result": " acc "},
        matched_report=_report([{"joint_no": "FW12", "result": "ACC"}]),
        drawing_joint_requirement=_req("PT"),
    )
    assert _rules(same) == []      # 정규화 후 같으므로 finding 없음

    diff = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "result": " acc "},
        matched_report=_report([{"joint_no": "FW12", "result": " rej "}]),
        drawing_joint_requirement=_req("PT"),
    )
    assert _details(diff, "result_mismatch") == {"billing_result": " acc ", "report_result": " rej "}


def test_welder_mismatch():
    """용접사 불일치 → welder_mismatch (_SUSPECT_RULES). 키는 billing_welder / report_welder."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "result": "ACC", "welder_id": "DW001"},
        matched_report=_report([{"joint_no": "FW12", "result": "ACC", "welder_id": "DW999"}]),
        drawing_joint_requirement=_req("PT"),
    )
    assert _rules(res) == ["welder_mismatch"]
    assert _details(res, "welder_mismatch") == {"billing_welder": "DW001", "report_welder": "DW999"}


def test_welder_compared_normalized():
    """용접사도 정규화 후 비교 — 'dw001' 과 'DW001' 은 불일치가 아니다."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "welder_id": "dw001"},
        matched_report=_report([{"joint_no": "FW12", "welder_id": "DW001"}]),
        drawing_joint_requirement=_req("PT"),
    )
    assert _rules(res) == []


def test_billed_joint_absent_from_multi_joint_report_is_silent():
    """청구 Joint 가 성적서(2 Joint 이상)에 없으면 finding 이 **하나도** 없다.

    '성적서에 그 Joint 가 없다' 는 별도 rule 이 존재하지 않는다 — 조용히 OK 로 넘어간다.
    """
    res = compliance.evaluate(
        billing_row={"joint_no": "FW99", "ndt_method": "PT", "result": "ACC", "welder_id": "DW001"},
        matched_report=_report([{"joint_no": "FW12", "result": "REJ", "welder_id": "DW777"},
                                {"joint_no": "FW13", "result": "REJ", "welder_id": "DW777"}]),
        drawing_joint_requirement=_req("PT"),
    )
    assert res["findings"] == []


def test_billed_joint_absent_from_single_joint_report_no_longer_fabricates_mismatch():
    """[이상 동작 4 — 5단계에서 수정] 성적서 Joint 가 1 개뿐이어도 엉뚱한 Joint 와 대조하지 않는다.

    바로 위 테스트와 입력이 같고 성적서 Joint 개수만 2 → 1. 예전엔 결과가 뒤집혀
    result_mismatch(하드 위반)를 날조했다. 이제 두 경우가 같다 — 둘 다 무음.
    (청구 Joint 가 성적서에 없다는 별도 rule 은 여전히 없다 — 그건 이 패치 범위 밖.)
    """
    res = compliance.evaluate(
        billing_row={"joint_no": "FW99", "ndt_method": "PT", "result": "ACC", "welder_id": "DW001"},
        matched_report=_report([{"joint_no": "FW12", "result": "REJ", "welder_id": "DW777"}]),
        drawing_joint_requirement=_req("PT"),
    )
    assert res["findings"] == []


def test_single_joint_fallback_still_applies_when_billing_has_no_joint():
    """fallback 은 **청구에 Joint 가 아예 없을 때만** — 그 경우는 예전처럼 단일 Joint 성적서로 본다."""
    res = compliance.evaluate(
        billing_row={"ndt_method": "PT", "result": "ACC", "welder_id": "DW001"},
        matched_report=_report([{"joint_no": "FW12", "result": "REJ", "welder_id": "DW777"}]),
        drawing_joint_requirement=None,
    )
    assert "result_mismatch" in _rules(res) and "welder_mismatch" in _rules(res)


@pytest.mark.parametrize("report", [
    {"report_no": "77-005PT", "joints": []},
    {"report_no": "77-005PT", "joints": None},
    {"report_no": "77-005PT"},                 # joints 키 자체가 없음
])
def test_report_without_joints_yields_no_mismatch(report):
    """성적서에 joints 가 없으면 (빈 list·None·키 없음) 대조 finding 이 없다."""
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "result": "ACC", "welder_id": "DW001"},
        matched_report=report,
        drawing_joint_requirement=_req("PT"),
    )
    assert res["findings"] == []


# ─────────────────────────── 7) 근거 청크 신뢰도 ───────────────────────────


def test_low_confidence_code_ref(monkeypatch):
    """needs_review 인 권위 참조가 있으면 low_confidence_code_ref 가 **맨 끝**에 붙는다.

    finding 안의 ref 는 {doc, page, confidence} 3 키로만 투영된다 —
    section/quote/chunk_source/authority_level/needs_review 는 authority_refs 에만 남는다.
    """
    monkeypatch.setattr(compliance, "_code_lookup", lambda br: [
        {"authority_level": 3, "doc": "ASME-V", "page": 12, "section": "T-274",
         "quote": "q", "chunk_source": "vlm_table", "confidence": 0.42, "needs_review": True},
        {"authority_level": 3, "doc": "ASME-V", "page": 13, "section": "T-275",
         "quote": "q2", "chunk_source": "text", "confidence": 0.99, "needs_review": False},
    ])
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=None,
    )
    assert _rules(res) == ["no_matching_report", "drawing_requirement_missing",
                           "low_confidence_code_ref"]
    d = _details(res, "low_confidence_code_ref")
    assert set(d) == {"refs"}
    assert d["refs"] == [{"doc": "ASME-V", "page": 12, "confidence": 0.42}]
    assert len(res["authority_refs"]) == 2      # authority_refs 는 필터링하지 않는다


# ─────────────────── 권위계층 실제 본문 (DB·검색·LLM 만 대체) ───────────────────


class _FakeQuery:
    def filter(self, *a, **k):
        return self

    def all(self):
        return []          # 적재된 SCWEP 없음 = 정상 (finding 발생 안 함)


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, *a, **k):
        return _FakeQuery()


def test_code_lookup_full_path_builds_refs_from_snippets(monkeypatch):
    """_scwep_lookup / _code_lookup 실제 본문 — 검색 질의 문자열·top_k·(doc,page) 결합을 고정.

    질의는 정규화되지 않은 원본 값을 그대로 f-string 에 넣는다.
    뒤집음(감사 ⑧): citation 의 (doc, page) 가 어떤 청크와도 매칭되지 않으면 (= 검색 결과에
    없는 근거를 LLM 이 인용, 환각 가능성) chunk_source 는 'untraceable', confidence 는 None,
    needs_review 는 True 가 된다 — 예전엔 needs_review=False 로 '신뢰할 만한 인용'처럼
    취급되던 것을 뒤집었다.
    """
    monkeypatch.setattr(compliance, "_scwep_lookup", _REAL_SCWEP)
    monkeypatch.setattr(compliance, "_code_lookup", _REAL_CODE)
    monkeypatch.setattr(compliance, "get_session", lambda *a, **k: _FakeSession())

    seen = {}

    def _fake_search(query, *, top_k=None, hybrid=None):
        seen["call"] = (query, top_k)
        return [{"doc": "ASME-V", "page": 12, "chunk_source": "vlm_table",
                 "confidence": 0.42, "needs_review": True}]

    monkeypatch.setattr(compliance.code_indexer, "search", _fake_search)

    class _Resp:
        parsed = {
            "found_in_context": True,
            "citations": [
                {"doc": "ASME-V", "page": 12, "section": "T-274", "quote": "q"},
                {"doc": "NOPE", "page": 9, "section": "S", "quote": "q2"},
            ],
        }

    # `call` 은 compliance 네임스페이스로 import 되어 있으므로 compliance 에 패치해야 한다.
    monkeypatch.setattr(compliance, "call", lambda stage, payload: _Resp())

    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=None,
    )
    assert seen["call"] == ("PT acceptance criteria sampling rate FW12", 3)
    # 모양이 아니라 규칙을 확인한다 — refs 에 필드가 추가돼도(예: quote_verified) 깨지지 않도록.
    refs = res["authority_refs"]
    assert [(r["doc"], r["page"], r["section"], r["quote"]) for r in refs] == [
        ("ASME-V", 12, "T-274", "q"), ("NOPE", 9, "S", "q2")]
    assert all(r["authority_level"] == 3 for r in refs)
    assert refs[0]["chunk_source"] == "vlm_table" and refs[0]["confidence"] == 0.42
    assert refs[1]["chunk_source"] == "untraceable" and refs[1]["confidence"] is None
    assert [r["needs_review"] for r in refs] == [True, True]
    # 스니펫에 원문(text)이 없으므로 인용문은 확인 불가 — 확인 못 한 것을 통과시키면 안 된다.
    assert [r["quote_verified"] for r in refs] == [False, False]
    assert _details(res, "low_confidence_code_ref")["refs"] == [
        {"doc": "ASME-V", "page": 12, "confidence": 0.42},
        {"doc": "NOPE", "page": 9, "confidence": None},
    ]


def test_code_lookup_skips_llm_when_no_snippets(monkeypatch):
    """검색 결과가 비면 LLM 을 아예 호출하지 않고 [] 를 반환한다."""
    monkeypatch.setattr(compliance, "_scwep_lookup", _REAL_SCWEP)
    monkeypatch.setattr(compliance, "_code_lookup", _REAL_CODE)
    monkeypatch.setattr(compliance, "get_session", lambda *a, **k: _FakeSession())
    monkeypatch.setattr(compliance.code_indexer, "search",
                        lambda query, *, top_k=None, hybrid=None: [])

    def _boom(*a, **k):
        raise AssertionError("검색 결과가 없으면 LLM 을 호출하면 안 된다")

    monkeypatch.setattr(compliance, "call", _boom)

    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=None,
    )
    assert res["authority_refs"] == []
    assert _rules(res) == ["no_matching_report", "drawing_requirement_missing"]


def test_code_lookup_returns_empty_when_llm_not_found_in_context(monkeypatch):
    """LLM 이 found_in_context=False 를 주면 인용을 만들지 않는다."""
    monkeypatch.setattr(compliance, "_scwep_lookup", _REAL_SCWEP)
    monkeypatch.setattr(compliance, "_code_lookup", _REAL_CODE)
    monkeypatch.setattr(compliance, "get_session", lambda *a, **k: _FakeSession())
    monkeypatch.setattr(compliance.code_indexer, "search",
                        lambda query, *, top_k=None, hybrid=None: [{"doc": "D", "page": 1}])

    class _Resp:
        parsed = {"found_in_context": False, "citations": [{"doc": "D", "page": 1}]}

    monkeypatch.setattr(compliance, "call", lambda stage, payload: _Resp())
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=None,
    )
    assert res["authority_refs"] == []


# ─────────────────── config 토글 ───────────────────


def test_overbilling_check_toggle_off(monkeypatch):
    """check_billed_ndt_in_requirements=False → 과다청구만 억제, 정보성 검사는 그대로."""
    monkeypatch.setattr(compliance, "matching_rules",
                        lambda: {"compliance": {"check_billed_ndt_in_requirements": False}})
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "UT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req("PT"),
    )
    assert _rules(res) == ["no_matching_report", "required_ndt_missing"]


def test_required_ndt_check_toggle_off(monkeypatch):
    """check_required_ndt_present=False → 정보성 finding 억제."""
    monkeypatch.setattr(compliance, "matching_rules",
                        lambda: {"compliance": {"check_required_ndt_present": False}})
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req("PT", "VT"),
    )
    assert _rules(res) == ["no_matching_report"]


def test_missing_compliance_section_defaults_all_checks_on(monkeypatch):
    """matching_rules 에 compliance 섹션이 없어도 세 검사 모두 기본 True 로 동작."""
    monkeypatch.setattr(compliance, "matching_rules", lambda: {})
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "UT", "drawing_no": "D1"},
        matched_report=None,
        drawing_joint_requirement=_req("PT"),
    )
    # 5단계: 근거 게이트 스위치도 기본 True → 설정이 통째로 비어도 SCWEP 0건이면 확정이 아니라 미제출.
    # 검사 셋이 전부 켜져 있다는 이 테스트의 본래 뜻은 그대로다 (세 finding 모두 발생).
    assert _rules(res) == ["no_matching_report", "billed_ndt_basis_not_submitted",
                           "required_ndt_missing"]


def test_real_config_yaml_enables_all_three_compliance_checks():
    """현행 config/matching_rules.yaml 기준값 — 세 토글 모두 true (패치 없이 확인)."""
    cfg = compliance.matching_rules().get("compliance", {})
    assert cfg["check_billed_ndt_in_requirements"] is True
    assert cfg["check_required_ndt_present"] is True
    assert cfg["check_sampling_rate"] is True


# ─────────────────── details 키 이름 고정 (하류 Excel writer 가 읽음) ───────────────────


def test_finding_details_key_names(monkeypatch):
    """rule 별 details 키 이름 전체를 한 번에 dict 로 고정.

    하류 Excel writer 가 이 키 이름을 그대로 읽는다. 과거에 키 이름 불일치 사고가 있었으므로
    리팩터가 조용히 키를 바꾸면 여기서 먼저 깨져야 한다.
    """
    monkeypatch.setattr(compliance, "_code_lookup", lambda br: [
        {"doc": "ASME-V", "page": 12, "confidence": 0.42, "needs_review": True}])

    observed: dict[str, list[str]] = {}

    def _collect(**kw):
        for f in compliance.evaluate(**kw)["findings"]:
            observed.setdefault(f["rule"], sorted(f["details"].keys()))

    # KE 오기 + 성적서 미매칭 + 도면 기준 없음 + 저신뢰 근거
    _collect(billing_row={"joint_no": "FW12", "ndt_method": "PT", "drawing_no": _KE},
             matched_report=None, drawing_joint_requirement=None)
    # 과다청구(SCWEP 0건 → 근거 미제출) + 미청구 정보성
    _collect(billing_row={"joint_no": "FW12", "ndt_method": "UT", "drawing_no": "D1"},
             matched_report=None, drawing_joint_requirement=_req("PT", "VT"))
    # 과다청구 **확정** 경로 — 범위 맞는 v2 SCWEP 가 UT 에 침묵하는 문서를 넣어 강제
    from app.analyzers import scwep_basis
    monkeypatch.setattr(scwep_basis, "load_docs", lambda session: [{
        "document_no": "SCWEP-X", "extracted": {
            "applicable_scope": {"disciplines": ["CP-M1"]}, "_schema_version": 2,
            "needs_review": False, "extraction_confidence": 0.9,
            "conditional_ndt_requirements": [{"trigger": "러그 제거 후", "ndt_method": "MT",
                                              "quote": "…", "confidence": 0.9}],
            "general_rules": []}}])
    _collect(billing_row={"joint_no": "FW12", "ndt_method": "UT", "drawing_no": "D1", "discipline": "CP-M1"},
             matched_report=None, drawing_joint_requirement=_req("PT", "VT"))
    # 판정·용접사 불일치
    _collect(billing_row={"joint_no": "FW12", "ndt_method": "PT",
                          "result": "ACC", "welder_id": "DW001"},
             matched_report=_report([{"joint_no": "FW12", "result": "REJ", "welder_id": "DW999"}]),
             drawing_joint_requirement=_req("PT"))

    assert observed == {
        "drawing_no_is_ke_misentry": ["note", "recommended_action", "wrong_value"],
        "no_matching_report": ["billing_joint", "method"],
        "drawing_requirement_missing": ["billing_joint", "drawing_no_in_billing",
                                        "drawing_no_in_report", "note"],
        "low_confidence_code_ref": ["refs"],
        # 5단계: 기존 7키 + 근거 4키. 하류가 읽는 기존 키는 그대로.
        "billed_ndt_not_in_requirements": ["basis_docs", "basis_reason", "basis_state",
                                           "billing_joint", "drawing_no",
                                           "needs_confirm_extraction", "note",
                                           "recommended_action", "requested_ndt",
                                           "required_ndt_by_drawing", "trigger"],
        "billed_ndt_basis_not_submitted": ["basis_docs", "basis_reason", "basis_state",
                                           "billing_joint", "drawing_no",
                                           "needs_confirm_extraction", "note",
                                           "recommended_action", "requested_ndt",
                                           "required_ndt_by_drawing", "trigger"],
        "required_ndt_missing": ["billing_joint", "drawing_no", "note",
                                 "still_required", "this_row_method"],
        "result_mismatch": ["billing_result", "report_result"],
        "welder_mismatch": ["billing_welder", "report_welder"],
    }


def test_low_confidence_ref_projection_key_names(monkeypatch):
    """low_confidence_code_ref 의 refs 원소 키 이름도 고정 (doc/page/confidence 3 개)."""
    monkeypatch.setattr(compliance, "_code_lookup", lambda br: [
        {"authority_level": 3, "doc": "ASME-V", "page": 12, "section": "T-274",
         "quote": "q", "chunk_source": "vlm_table", "confidence": 0.42, "needs_review": True}])
    res = compliance.evaluate(
        billing_row={"joint_no": "FW12", "ndt_method": "PT"},
        matched_report=None, drawing_joint_requirement=None,
    )
    refs = _details(res, "low_confidence_code_ref")["refs"]
    assert [sorted(r.keys()) for r in refs] == [["confidence", "doc", "page"]]
