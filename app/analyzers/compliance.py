"""적합성 점검 — 도면 요구사항 ↔ 청구·성적서 비교.

도면이 누락·모호한 사안에 대해 권위계층(SCWEP→Code→Contract) 으로 거슬러 올라간다.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.analyzers import scwep_basis
from app.config import matching_rules
from app.database.models import StandardDocument, get_session
from app.extractors import code_indexer
from app.hcx_client import call

logger = logging.getLogger(__name__)


def _normalize(s) -> str:
    return str(s or "").strip().upper()


def _canon_method(s) -> str:
    """NDT 방법을 청구 엑셀과 **같은 어휘**로.

    청구측은 excel_parser 가 templates.yaml(method_normalization) 으로 VMC→VT 처럼 정규화해
    들어오는데, 도면 추출값(LLM 자유 추출)은 지금까지 strip/upper 만 했다. 실측(2026-09-05):
    도면이 'VMC'·'Visual'·'visual test' 라고 적히면 정상 VT 청구가 전부 과다청구로 확정됐다.
    양쪽을 같은 표로 통과시킨다. 표에 없는 값은 예전처럼 strip/upper.
    """
    raw = _normalize(s)
    if not raw:
        return ""
    try:
        from app.extractors.excel_parser import _build_method_map, _normalize_token
        mapped = _build_method_map().get(_normalize_token(s))
    except Exception:       # noqa: BLE001 - 정규화 표가 없어도 판정은 계속된다
        mapped = None
    return _normalize(mapped) if mapped else raw


def evaluate(
    *,
    billing_row: dict,
    matched_report: Optional[dict],     # InspectionReport.extracted_json
    drawing_joint_requirement: Optional[dict],   # Requirement → joint dict
) -> dict:
    """compliance_findings 목록 + 권위계층 참조 결과 반환."""
    rules = matching_rules().get("compliance", {})
    findings: list[dict] = []
    if not matched_report:          # {} 도 '매칭 없음' 이다 (특성화 이상동작 3)
        matched_report = None
    requested_method = _canon_method(billing_row.get("ndt_method"))
    # KKS 범위 대조용 도면번호 (Task 12, 2026-09-05) — 청구 행에 drawing_no 가 없고
    # 성적서에만 있을 때 SCWEP scope_state 가 그 값을 쓸 수 있게 보강한다. billing_row
    # 자체는 바꾸지 않는다 — drawing_requirement_missing 의 청구/성적서 구분 보고(Task 7,
    # 두 값을 모두 검사하는 KE 오기 판정과 동일한 이유)가 이 값에 기대고 있기 때문이다.
    drawing_no_for_scope = billing_row.get("drawing_no") or (matched_report or {}).get("drawing_no")

    # 0) drawing_no 가 KE 식별자면 시공사 오기 — 모체 도면번호 정정 요청 대상.
    #    청구·성적서 둘 다 본다. 대소문자 무시, 점으로 나눈 토큰이 정확히 KE 일 때만.
    ke_hits = [v for v in (billing_row.get("drawing_no"),
                           matched_report.get("drawing_no") if matched_report else None)
               if v and "KE" in str(v).upper().split(".")]
    if ke_hits:
        # KE 번호는 **도면번호가 아니다**. 이것을 KKS 범위 키로 쓰면 우연히 같은 계통의
        # SCWEP 가 '이 공종 문서' 로 인정되어 근거 게이트가 열리고, 방금 '도면번호가 틀렸다' 고
        # 지적한 바로 그 행이 하드 위반으로 확정된다. 범위는 '모름' 이 맞다 (2026-09-05 리뷰).
        drawing_no_for_scope = None
        findings.append({
            "rule": "drawing_no_is_ke_misentry",
            "details": {
                "wrong_value": ke_hits[0],
                "note": (
                    "Detailed Drawing 컬럼에 KE 식별자가 잘못 기입됨 — "
                    "KE 는 WEP 또는 SCWEP(시공 절차 관련 서류)이며 도면이 아님. "
                    "모체 도면이 별도 존재."
                ),
                "recommended_action": (
                    "시공사에 해당 청구 행의 Detailed Drawing 을 모체 도면번호로 수정 요청"
                ),
            },
        })

    # 1) 매칭된 성적서가 없는 경우
    if matched_report is None:
        findings.append({
            "rule": "no_matching_report",
            "details": {"billing_joint": billing_row.get("joint_no"), "method": requested_method},
        })

    # 도면 기준이 없으면 권위계층 상위 문서로 보강
    auth_refs = []
    extraction_missing = (drawing_joint_requirement is not None
                          and drawing_joint_requirement.get("required_ndt_json") is None)
    if drawing_joint_requirement is None or extraction_missing:
        # 사유 더 명확하게: drawing_no 자체가 없는지, DB 조회 실패인지, Joint 미발견인지
        if matched_report:
            mr_drawing = matched_report.get("drawing_no")
        else:
            mr_drawing = None
        note_parts = []
        if extraction_missing:
            # Joint 행은 찾았다 — "미발견" 문구를 붙이면 자기모순이 된다 (2026-09-05 리뷰)
            note_parts.append(
                f"도면 DB 에 Joint 는 있으나 NDT 요구 추출값 없음 (추출 실패 가능, "
                f"drawing_no='{billing_row.get('drawing_no') or mr_drawing}')"
            )
        elif not billing_row.get("drawing_no") and not mr_drawing:
            note_parts.append("청구·성적서 모두 drawing_no 없음")
        else:
            note_parts.append(
                f"도면 DB 에서 미발견 (조회시도 drawing_no='{billing_row.get('drawing_no') or mr_drawing}')"
            )
        note_parts.append("적합성 자동 판정 불가, 검토자 수동 확인 필요")

        findings.append({
            "rule": "drawing_requirement_missing",
            "details": {
                "billing_joint": billing_row.get("joint_no"),
                "drawing_no_in_billing": billing_row.get("drawing_no"),
                "drawing_no_in_report": mr_drawing,
                "note": " · ".join(note_parts),
            },
        })
        auth_refs = _consult_authority_hierarchy(billing_row)
    else:
        # 2) 청구 NDT 가 도면 요구사항에 있는가 — **과다 청구 검증 (발주처 핵심)**
        required_ndt = (drawing_joint_requirement.get("required_ndt_json") or {}).get("items", [])
        required_methods = {_canon_method(n.get("method")) for n in required_ndt if n.get("method")}
        required_methods.discard("")
        if rules.get("check_billed_ndt_in_requirements", True):
            # 과다청구 검출: 청구 NDT 가 도면 요구에 없을 때.
            # ⚠ required_methods 가 공집합(도면이 이 Joint 에 NDT 미요구)인 경우도 후보다.
            #    이전엔 `required_methods and ...` 단락평가로 공집합 케이스를 놓쳤음 (false negative).
            if requested_method and (
                not required_methods or requested_method not in required_methods
            ):
                claim = _classify_overbilling_claim(
                    rules, billing_row, requested_method, required_methods, drawing_joint_requirement,
                    drawing_no=drawing_no_for_scope,
                    report_context=_report_context(matched_report),
                )
                findings.append(claim)
                # 근거로 쓴 SCWEP 조항은 authority_refs 에도 싣는다 — 검토 엑셀 '근거_문서' 와
                # 설명 프롬프트의 scwep_rules_relevant 가 이 목록을 읽는다. 확정(no_basis_found)
                # 행은 refs 가 비어 있으므로 고발 옆에 SCWEP 이 붙지 않는다.
                auth_refs = list(auth_refs) + list((claim.get("details") or {}).get("scwep_refs") or [])

        # 3) 도면이 요구한 NDT 중 청구되지 않은 것 — **정보성** (누락 = 비용절감)
        # 사용자 정책 (2026-07-06): 기성검토 위험은 과다청구(돈 나감) 단방향.
        # 미청구는 발주처 입장에서 비용절감이므로 문제 아님. 다만 향후 시공사이
        # 소급 청구할 경우의 검증 포인트로 기록만 남긴다 (verdict·위험도 영향 최소).
        if rules.get("check_required_ndt_present", True):
            missing_at_row = [m for m in required_methods if m and m != requested_method]
            if missing_at_row and requested_method:
                findings.append({
                    "rule": "required_ndt_missing",
                    "details": {
                        "billing_joint": billing_row.get("joint_no"),
                        "drawing_no": billing_row.get("drawing_no"),
                        "this_row_method": requested_method,
                        "still_required": sorted(missing_at_row),
                        "note": (
                            f"도면 요구 중 이 행에 미청구 NDT 있음 ({sorted(missing_at_row)}) — "
                            f"미청구는 비용절감으로 기성검토상 문제 아님 (정보성). "
                            f"향후 해당 방법 소급 청구 시 이 기록으로 기준·수행증빙 대조."
                        ),
                    },
                })

        # 4) 샘플링률 충족 (별도 집계 단계에서 처리. 여기서는 메모만)
        if rules.get("check_sampling_rate", True):
            for n in required_ndt:
                try:
                    pct = float(str(n.get("sampling_rate_pct")).rstrip("%"))
                except (TypeError, ValueError):
                    continue            # 비수치는 집계 단계에서 재확인 대상으로 다룬다
                if pct < 100:
                    pass

    # 5) 성적서 ↔ 청구 판정 불일치
    if matched_report:
        m_result = _result_from_report(matched_report, billing_row.get("joint_no"))
        if billing_row.get("result") and m_result and _normalize(billing_row["result"]) != _normalize(m_result):
            findings.append({
                "rule": "result_mismatch",
                "details": {"billing_result": billing_row["result"], "report_result": m_result},
            })

    # 6) 용접사 불일치
    if matched_report:
        m_welder = _welder_from_report(matched_report, billing_row.get("joint_no"))
        if billing_row.get("welder_id") and m_welder and _normalize(billing_row["welder_id"]) != _normalize(m_welder):
            findings.append({
                "rule": "welder_mismatch",
                "details": {"billing_welder": billing_row["welder_id"], "report_welder": m_welder},
            })

    # 7) 근거 청크 신뢰도 — 표 전사에서 OCR 숫자 대조에 실패한 인용은 재확인 대상
    low = [r for r in auth_refs if r.get("needs_review")]
    if low:
        findings.append({
            "rule": "low_confidence_code_ref",
            "details": {"refs": [{"doc": r.get("doc"), "page": r.get("page"),
                                  "confidence": r.get("confidence")} for r in low]},
        })

    return {
        "findings": findings,
        "authority_refs": auth_refs,
        "drawing_requirement": drawing_joint_requirement,
        "matched_report": matched_report,
    }


def _joint_field_from_report(report: dict, joint_no: Optional[str], field: str):
    """성적서에서 이 Joint 의 값을. 단일 Joint fallback 은 **청구에 Joint 가 없을 때만**.

    2026-09-05 특성화 테스트가 잡은 결함: 예전엔 joint_no 조회가 **명시적으로 실패한 뒤에도**
    성적서 Joint 가 하나면 그 값을 돌려줬다. FW99 청구가 FW12 성적서와 대조되어
    result_mismatch(하드 위반 → NONCOMPLIANT) 를 **날조**했다. 같은 입력에 Joint 가 둘이면
    아무 지적도 안 나오는 비대칭이었다. Joint 를 적어 냈는데 성적서에 없으면 "모름"(None) 이 맞다.
    """
    if not report:
        return None
    joints = report.get("joints") or []
    if joint_no:
        for j in joints:
            if _normalize(j.get("joint_no")) == _normalize(joint_no):
                return j.get(field)
        return None                      # 청구 Joint 가 성적서에 없음 — 엉뚱한 Joint 로 대체하지 않는다
    if len(joints) == 1:                 # 청구에 Joint 가 아예 없을 때만 단일 Joint 성적서로 본다
        return joints[0].get(field)
    return None


def _result_from_report(report: dict, joint_no: Optional[str]):
    return _joint_field_from_report(report, joint_no, "result")


def _welder_from_report(report: dict, joint_no: Optional[str]):
    return _joint_field_from_report(report, joint_no, "welder_id")


# ─────────────────────────── 과다청구 근거 게이트 ───────────────────────────

def _classify_overbilling_claim(rules: dict, billing_row: dict, requested_method: str,
                                required_methods: set, drawing_joint_requirement: dict, *,
                                drawing_no: Optional[str] = None,
                                report_context: Optional[str] = None) -> dict:
    """도면에 없는 검사를 청구했다. 이것을 무엇이라고 부를 것인가.

    원칙: **근거 사슬이 완전할 때까지 "과다청구" 라고 말하지 않는다.**
    사슬 = (도면 요구가 실제로 뽑혔고 비어 있지 않음) AND (제출된 SCWEP 가 이 공종 범위이면서
    이 방법에 침묵). 하나라도 빠지면 확정이 아니라 제출 요구다. 판정은 scwep_basis 가 하고,
    여기서는 도면측 게이트를 겹친 뒤 rule 이름을 고른다. 기존 details 키는 전부 유지한다 —
    하류(엑셀·가이드)가 그 이름을 읽는다.
    """
    sw = rules.get("overbilling_claim") or {}
    empty_req = not required_methods
    flagged_set = bool(drawing_joint_requirement.get("_drawing_set_needs_review"))

    # 1) SCWEP 근거 상태
    if sw.get("require_submitted_basis", True):
        with get_session() as s:
            docs = scwep_basis.load_docs(s)
        # 성적서 표1 의 '용접부 특성' 칸이 그 검사를 왜 했는지 말한다 ("Removing temporary plate fit-up").
        # SCWEP 조건부 요구와 대조해 근거 있는 검사인지 가린다 (2026-09-08 1항차 실물 증거).
        basis = scwep_basis.classify(docs, requested_method, billing_row.get("discipline"),
                                      drawing_no=drawing_no,
                                      report_context=report_context)
        if not sw.get("require_conditional_schema", True) and basis.state == scwep_basis.STATE_NOT_SUBMITTED \
                and basis.detail.get("reason") == "scwep_legacy_schema":
            # 스위치를 끄면 구 형식 문서도 침묵의 증거로 인정한다 (중간 단계 되돌리기용)
            basis = scwep_basis.BasisAssessment(state=scwep_basis.STATE_NO_BASIS,
                                                reasons=basis.reasons, detail=basis.detail)
    else:
        basis = scwep_basis.BasisAssessment(state=scwep_basis.STATE_NO_BASIS,
                                            detail={"reason": "gate_disabled"})

    # 2) 도면측 게이트 — 추출 실패·재확인 도면으로는 확정하지 않는다
    drawing_ok = True
    drawing_block = None
    if empty_req and sw.get("never_claim_on_empty_drawing_requirement", True):
        drawing_ok, drawing_block = False, "drawing_requirement_empty"
    elif flagged_set and sw.get("never_claim_on_flagged_drawing_set", True):
        drawing_ok, drawing_block = False, "drawing_set_flagged"

    base = {
        "requested_ndt": requested_method,
        "required_ndt_by_drawing": sorted(required_methods),
        "billing_joint": billing_row.get("joint_no"),
        "drawing_no": billing_row.get("drawing_no"),
        "needs_confirm_extraction": empty_req,   # 공집합은 도면 추출 누락 가능성도 → 검토자 확인
        "basis_state": basis.state,
        "basis_docs": list(basis.covering_docs or basis.detail.get("docs") or []),
        "basis_reason": basis.detail.get("reason"),
        "trigger": basis.detail.get("trigger"),
    }
    joint = billing_row.get("joint_no")

    # 3) 이름 고르기 — 면책이 먼저, 확정은 맨 마지막
    if basis.state == scwep_basis.STATE_COVERED:
        return {"rule": "billed_ndt_covered_by_scwep", "details": {**base,
            "note": (f"도면 미요구 '{requested_method}' 청구이나 SCWEP 조건부 요구에 근거 있음 "
                     f"({', '.join(base['basis_docs'])}) — 조건 '{base['trigger']}' 발생 사실 확인 대상"),
            "recommended_action": (f"{', '.join(base['basis_docs'])} 조건부 요구 인용 — 조건 발생 사실을 "
                                   f"1회 대조 후 종결. 과다청구 아님."),
            "scwep_refs": basis.refs}}
    if basis.state == scwep_basis.STATE_UNCLEAR:
        return {"rule": "billed_ndt_basis_unclear", "details": {**base,
            "note": (f"도면 미요구 '{requested_method}' 청구 — SCWEP 에 언급은 있으나 적용 조건 미확정 "
                     f"({', '.join(base['basis_docs'])})"),
            "recommended_action": "제출 SCWEP 해당 조항 원문 확인 — 적용 조건 확정 전까지 과다청구로 단정하지 않음",
            "scwep_refs": basis.refs}}
    if basis.state == scwep_basis.STATE_NO_BASIS and drawing_ok:
        # 유일한 확정 경로. 이름·키는 예전과 바이트 동일 — 하류 무변경.
        return {"rule": "billed_ndt_not_in_requirements", "details": {**base,
            "note": (
                f"도면이 이 Joint 에 NDT 미요구인데 '{requested_method}' 청구 — 과다 청구 의심"
                if empty_req else
                f"도면 미요구 NDT '{requested_method}' 청구 — 과다 청구 의심"
            ),
            "recommended_action": (
                f"시공사에 '{joint}' Joint 의 {requested_method} 청구 사유 확인 요청. "
                + ("도면에 해당 Joint NDT 요구가 없음 (도면 추출 누락 여부도 함께 확인) — 과다분 환불 검토."
                   if empty_req else
                   f"도면 요구는 {sorted(required_methods)} 뿐 — 과다분 환불 검토.")
                + (f" 제출 SCWEP {', '.join(base['basis_docs'])} 에도 근거 없음." if base["basis_docs"] else "")
            )}}
    # 그 외 전부 — 근거 미제출 (SCWEP 쪽이든 도면 쪽이든 사슬이 끊겼다)
    reason = drawing_block if (basis.state == scwep_basis.STATE_NO_BASIS and not drawing_ok) else base["basis_reason"]
    return {"rule": "billed_ndt_basis_not_submitted", "details": {**base, "basis_reason": reason,
        "note": (f"도면 미요구 '{requested_method}' 청구 — 근거 사슬 불완전({reason}). "
                 f"과다청구로 단정하지 않음"),
        "recommended_action": ("시공사에 근거 절차서(SCWEP) 제출 요청. 근거 제출 전까지 과다청구로 단정하지 않음"
                               if not drawing_block else
                               f"도면측 확인 먼저({'도면 요구 추출 결과가 비어 있음' if drawing_block == 'drawing_requirement_empty' else '도면 세트 재확인 표시'}) — "
                               f"확인 후 시공사에 근거 절차서 제출 요청")}}


# ─────────────────────────── Authority hierarchy ───────────────────────────


def _consult_authority_hierarchy(billing_row: dict) -> list[dict]:
    """도면이 모호·없음일 때 SCWEP → Code → Contract 순으로 조회."""
    refs: list[dict] = []
    refs += _scwep_lookup(billing_row)
    refs += _code_lookup(billing_row)
    # Contract 는 통상 일반 검사 책임/3% 조항이므로 단건 평가 단계에선 잘 호출되지 않음 (생략 가능)
    return refs


def _scwep_lookup(billing_row: dict) -> list[dict]:
    """SCWEP 검색 — 사용자 정책 (2026-05-22): SCWEP 는 중요 배관·기계에만 존재.
    적재된 SCWEP 가 없거나 매칭 안 되면 빈 결과 반환 (finding 발생 안 함, 정상).
    """
    out: list[dict] = []
    with get_session() as s:
        for d in s.query(StandardDocument).filter(StandardDocument.doc_type == "scwep").all():
            extracted = d.extracted_json or {}
            for rule in extracted.get("default_sampling_rates", []) or []:
                if _normalize(rule.get("ndt_method")) == _normalize(billing_row.get("ndt_method")):
                    out.append({
                        "authority_level": 2,
                        "doc": d.document_no or d.file_path,
                        "page": rule.get("page"),
                        "section": rule.get("applies_to"),
                        "quote": rule.get("quote"),
                    })
    return out   # 빈 결과 = SCWEP 미적용 항목, 정상


def _report_context(matched_report) -> Optional[str]:
    """매칭된 성적서가 적은 검사 사유들을 한 줄로. 표1 각 행의 characteristics 를 모은다."""
    if not isinstance(matched_report, dict):
        return None
    vals = []
    for j in matched_report.get("joints") or []:
        if isinstance(j, dict) and j.get("characteristics"):
            vals.append(str(j["characteristics"]))
    if matched_report.get("characteristics"):
        vals.append(str(matched_report["characteristics"]))
    seen, out = set(), []
    for v in vals:
        k = v.strip().lower()
        if k and k not in seen:
            seen.add(k); out.append(v.strip())
    return " / ".join(out) if out else None


def _code_lookup(billing_row: dict) -> list[dict]:
    method = billing_row.get("ndt_method") or ""
    joint = billing_row.get("joint_no") or ""
    query = f"{method} acceptance criteria sampling rate {joint}"
    snippets = code_indexer.search(query, top_k=3)
    if not snippets:
        return []
    resp = call("code_lookup", {"question": query, "context_snippets": snippets})
    if not resp.parsed or not resp.parsed.get("found_in_context"):
        return []
    # 인용이 어느 청크에서 왔는지 (doc, page) 로 되찾아 청크 신뢰도를 붙인다.
    # 표 전사 청크(vlm_table)는 OCR 숫자 대조 비율이 confidence 이고, 대조에 실패한
    # 표(needs_review)에서 나온 인용은 판정 근거로 쓰지 말고 재확인 대상으로 표시한다.
    by_key = {(s.get("doc"), s.get("page")): s for s in snippets}
    refs = []
    for c in resp.parsed.get("citations", []):
        src = by_key.get((c.get("doc"), c.get("page")))
        traceable = src is not None
        src = src or {}
        refs.append({
            "authority_level": 3,
            "doc": c.get("doc"),
            "page": c.get("page"),
            "section": c.get("section"),
            "quote": c.get("quote"),
            "chunk_source": src.get("chunk_source") if traceable else "untraceable",
            "confidence": src.get("confidence") if traceable else None,
            # 되찾지 못한 인용 = 검색 결과에 없는 (doc,page) 를 LLM 이 말한 것. 환각 가능성이 가장 높은
            # 인용을 깨끗한 근거로 적던 것을 뒤집는다 (감사 ⑧).
            "needs_review": (not traceable) or bool(src.get("needs_review", False)),
        })
    # (문서, 쪽) 이 맞아도 **인용문 자체**가 그 원문에 없을 수 있다 — 모양만 맞는 지어낸 문장이다.
    # 2026-09-07 파생 검토: 도면 검사표가 근거 없이 채워진 것과 같은 결함이고, 이쪽은 면책을 만들어 낸다.
    from app.analyzers.extraction_guard import verify_citations
    refs = verify_citations(refs, snippets)
    for r in refs:
        r["needs_review"] = bool(r.get("needs_review"))
    return refs
