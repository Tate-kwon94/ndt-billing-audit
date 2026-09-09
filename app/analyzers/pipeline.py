"""검토 파이프라인 오케스트레이터 — 청구회차 1건 처리.

흐름:
1. billing_round 의 billing_items 와 inspection_reports 로딩
2. 각 billing_item 에 대해:
   a. drawing_no 로 effective drawing_set 조회 → joint 요구사항
   b. 후보 성적서(같은 청구회차) 중 deterministic → fuzzy → llm_judge 순 매칭
   c. compliance.evaluate → findings + authority_refs
   d. risk_scorer.compute → 점수
   e. explainer.explain → verdict + 자연어 설명 + 근거 인용
   f. matches, findings 저장
"""
from __future__ import annotations

from pathlib import Path

import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analyzers import compliance, explainer, risk_scorer
from app.database.models import (
    BillingItem,
    BillingRound,
    InspectionReport,
    Requirement,
    StandardDocument,
    get_session,
)
from app.database.repository import save_finding, save_match
from app.extractors.drawing.revision_manager import find_effective
from app.matchers import deterministic, fuzzy, llm_judge

logger = logging.getLogger(__name__)


def run(billing_round_id: int) -> dict:
    with get_session() as s:
        br = s.get(BillingRound, billing_round_id)
        if br is None:
            raise ValueError(f"BillingRound {billing_round_id} 미존재")
        from app.analyzers import scwep_basis
        scwep_basis.reset_cache()

        items = s.scalars(
            select(BillingItem).where(BillingItem.billing_round_id == billing_round_id)
        ).all()
        reports = s.scalars(
            select(InspectionReport).where(InspectionReport.billing_round_id == billing_round_id)
        ).all()

        # 같은 청구회차의 성적서들을 후보 풀로 사용
        report_candidates = [_to_report_dict(r) for r in reports]
        report_by_no = {(r.report_no or f"id-{r.id}"): r for r in reports}

        contract_clauses = _all_contract_clauses(s)
        n_processed = 0
        n_failed = 0
        from app.progress_fmt import progress_line
        every = max(1, len(items) // 50)            # 2% 마다 '진행 [행] k/n' — 런처가 읽어 남은 시간을 보여준다
        for idx, item in enumerate(items, start=1):
            try:
                with s.begin_nested():          # SAVEPOINT — 실패 행만 되감는다
                    _process_one(s, br, item, report_candidates, report_by_no, contract_clauses)
                n_processed += 1
            except Exception as e:      # noqa: BLE001 - 한 행의 실패가 회차를 멈추면 안 되지만, 사라져도 안 된다
                n_failed += 1
                logger.exception("Item %s failed: %s", item.id, e)
                _record_failed_row(s, item, e)
            if idx % every == 0 or idx == len(items):
                logger.info(progress_line("행", idx, len(items)))

        s.commit()
        rollup = _basis_rollup(s, billing_round_id)
    stats = {"processed": n_processed, "failed": n_failed, "items_total": len(items),
             "reports": len(reports), **rollup}
    if n_failed:
        logger.warning("청구 %d행이 처리 중 실패해 SUSPECT(재확인) 로 기록됐습니다 — 검토 엑셀에서 "
                       "'처리 실패' 사유를 찾아 개발자에게 보내세요.", n_failed)
    _warn_if_gate_silent(rollup)
    return stats


def _record_failed_row(s: Session, item: BillingItem, exc: Exception) -> None:
    """처리 중 예외가 난 청구 행을 결과에 남긴다. 예전엔 로그에만 남고 화면은 '완료' 였다 (감사 ④).
    판정은 SUSPECT — 위반도 적합도 아니고 '아직 못 봤다' 이다."""
    reason = f"처리 실패: {type(exc).__name__}: {exc}"
    save_finding(
        s,
        billing_item_id=item.id,
        verdict="SUSPECT",
        risk_score=risk_scorer.compute([{"rule": "pipeline_row_failed"}]),
        summary="이 청구 행은 자동 처리 중 오류로 판정되지 않았다. 검토자가 직접 확인해야 한다.",
        explanation_json={"verdict": "SUSPECT", "needs_review": True, "error": reason},
        citations_json={"findings": [{"rule": "pipeline_row_failed", "details": {"error": reason}}],
                        "authority_refs": [], "drawing_requirement": None},
        recommended_action="data\\progress.log 와 이 행 번호를 개발자에게 보낸다. 판정 전까지 기성 인정 보류.",
        needs_review=True,
        review_reasons_json={"reasons": [reason]},
    )


def _basis_rollup(s: Session, billing_round_id: int) -> dict:
    """회차 단위 근거 상태 집계. 이 설계의 가장 큰 잔여 위험 둘 —
    '아무도 고발하지 않게 되는 것' 과 'SCWEP 조항 하나가 회차 전체를 면책하는 것' —
    은 산출물이 안 생기는 실패라 눈에 안 띈다. 그래서 회차마다 소리내어 센다."""
    from collections import Counter
    from app.database.models import Finding
    states: Counter = Counter()
    reasons: Counter = Counter()
    covering: Counter = Counter()
    rows = s.scalars(select(Finding).join(BillingItem, Finding.billing_item_id == BillingItem.id)
                     .where(BillingItem.billing_round_id == billing_round_id))
    rows_with_findings = 0
    unevaluated = 0          # 도면 요구사항이 없어 근거 게이트가 평가조차 안 된 행 (2026-09-07 스모크: 회차 전체가 이랬다)
    for f in rows:
        rows_with_findings += 1
        fes = (f.citations_json or {}).get("findings") or []
        if any(fe.get("rule") == "drawing_requirement_missing" for fe in fes):
            unevaluated += 1
        for fe in fes:
            d = fe.get("details") or {}
            st = d.get("basis_state")
            if not st:
                continue
            states[st] += 1
            if st == "not_submitted":
                reasons[d.get("basis_reason") or "?"] += 1
            if st == "covered":
                for doc in d.get("basis_docs") or []:
                    covering[doc] += 1
    top = covering.most_common(1)
    return {
        "basis_states": {k: states.get(k, 0) for k in ("covered", "unclear", "not_submitted", "no_basis_found")},
        "suppression_reasons": dict(reasons),
        "top_covering_doc": {"doc": top[0][0], "rows": top[0][1]} if top else None,
        "unevaluated_rows": unevaluated,
        "rows_with_findings": rows_with_findings,
    }


def _warn_if_gate_silent(rollup: dict) -> None:
    st = rollup.get("basis_states") or {}
    total_overbill_family = sum(st.values())
    # 게이트가 한 번도 평가되지 않은 회차 — 과다청구 계열이 0건이라 아래 경고도 안 울리는 구멍 (2026-09-07)
    if total_overbill_family == 0 and rollup.get("unevaluated_rows"):
        logger.warning("근거 게이트 미평가: 도면 요구사항이 없는 행 %d행 (판정 행 %d행 중). 청구의 도면번호·용접도가 "
                       "적재된 도면 세트와 이어지지 않으면 요구사항이 안 붙고 과다청구 판정 경로 전체가 침묵합니다. "
                       "성적서 drawing_no 와 도면 세트 번호를 대조하세요.",
                       rollup["unevaluated_rows"], rollup.get("rows_with_findings", 0))
    if total_overbill_family and st.get("no_basis_found", 0) == 0:
        why = ", ".join(f"{k} {v}" for k, v in (rollup.get("suppression_reasons") or {}).items()) or "사유 없음"
        logger.warning("과다청구 확정 0건 — 근거 게이트가 전부 억제했습니다 (사유: %s). "
                       "고발 경로가 현재 비활성 상태입니다. SCWEP 제출·적재 여부를 확인하세요.", why)
    top = rollup.get("top_covering_doc")
    covered = st.get("covered", 0)
    if top and covered and top["rows"] / covered >= 0.3 and top["rows"] >= 3:
        logger.warning("경고: %s 하나로 %d행이 인정됨 (covered %d행 중) — 그 조항 원문을 직접 대조하세요.",
                       top["doc"], top["rows"], covered)


# ─────────────────────────── Internal ───────────────────────────


def _process_one(
    s: Session,
    br: BillingRound,
    item: BillingItem,
    report_candidates: list[dict],
    report_by_no: dict[str, InspectionReport],
    contract_clauses: list[dict],
):
    billing_row = _to_billing_row(item, br)

    # 1) 매칭
    match_obj = deterministic.find_match(billing_row, report_candidates)
    match_method = "deterministic"
    discrepancies = []
    reasoning = None

    if match_obj is None:
        candidates = fuzzy.find_candidates(billing_row, report_candidates)
        if candidates:
            if llm_judge.should_invoke(candidates):
                judged = llm_judge.judge(billing_row, candidates)
                match_method = "llm"
                reasoning = judged.get("reasoning")
                discrepancies = judged.get("discrepancies", [])
                if judged.get("matched_report_no"):
                    # 후보에서 해당 성적서 찾기
                    for c in candidates:
                        if c.get("report_no") == judged["matched_report_no"]:
                            match_obj = c
                            break
                # LLM 이 명시적으로 needs_review 표시했거나 매칭 못 한 경우 보존
                if judged.get("needs_review"):
                    discrepancies = list(discrepancies) + [
                        {"field": "_llm_judge", "billing_value": None, "report_value": None,
                         "severity": "high",
                         "_review_reasons": judged.get("review_reasons") or []}
                    ]
            elif len(candidates) >= 2 and (candidates[0].get("_match_score", 0) - candidates[1].get("_match_score", 0)) < 5:
                # 1·2위 점수 격차가 극단적으로 작으면 LLM 임계는 통과 못 했어도 자동 선택 위험 → 보류
                match_method = "fuzzy"
                match_obj = None
                reasoning = (
                    f"fuzzy 후보 {len(candidates)}개의 점수 격차 < 5 — "
                    "임의 선택 보류, 검토자 수동 확인 필요"
                )
            else:
                match_obj = candidates[0]
                match_method = "fuzzy"

    matched_report_dict = None
    matched_report_db_id = None
    rep_obj = None
    if match_obj:
        matched_report_dict = match_obj
        rep_obj = report_by_no.get(match_obj.get("report_no"))
        if rep_obj is None and report_candidates:
            # fallback: candidate 의 _db_id 사용
            db_id = match_obj.get("_db_id")
            matched_report_db_id = db_id
        elif rep_obj is not None:
            matched_report_db_id = rep_obj.id

    match_review_reasons = _match_review_reasons(
        match_obj, match_method, candidates_count=None, discrepancies=discrepancies
    )
    save_match(
        s,
        billing_item_id=item.id,
        inspection_report_id=matched_report_db_id,
        matched_joint_no=(match_obj or {}).get("joint_no"),
        match_method=match_method if match_obj else "none",
        match_score=(match_obj or {}).get("_match_score"),
        reasoning=reasoning,
        discrepancies_json={"items": discrepancies} if discrepancies else None,
        needs_review=bool(match_review_reasons),
        review_reasons_json={"reasons": match_review_reasons} if match_review_reasons else None,
    )

    # 2) 도면 요구사항 조회 — 청구.drawing_no 없으면 매칭 성적서의 'Drawing:' 사용
    joint_requirement = _find_joint_requirement(
        s, item, matched_report=matched_report_dict, as_of=br.billing_date
    )

    # 3a) Rule Engine 검증 (deterministic, false positive 0)
    from app.analyzers import rule_engine
    from app.extractors import ocr_corrector
    rule_violations: list[dict] = []
    correction_logs: list[dict] = []

    billing_errs = rule_engine.validate_billing_item(billing_row)
    if billing_errs:
        # HCX-007 으로 보정 시도 (high-severity 만 대상, false positive 0 유지)
        high_errs = [e for e in billing_errs if e.severity == "high"]
        if high_errs:
            billing_row, log = ocr_corrector.correct_violations(
                billing_row, high_errs,
                neighboring_context=str(billing_row.get("raw_json", "")),
            )
            correction_logs.extend([{"scope": "billing_row", **l} for l in log])
            # 보정 후 재검증 — 살아남은 errs 만 violation
            billing_errs = rule_engine.validate_billing_item(billing_row)
        if billing_errs:
            rule_violations.append({
                "scope": "billing_row",
                "summary": rule_engine.summarize(billing_errs),
            })

    if matched_report_dict:
        rep_errs = rule_engine.validate_inspection_report(matched_report_dict)
        if rep_errs:
            high_errs = [e for e in rep_errs if e.severity == "high"]
            if high_errs:
                matched_report_dict, log = ocr_corrector.correct_violations(
                    matched_report_dict, high_errs,
                    neighboring_context="",
                    # 2026-09-07 감사 3번: 이걸 안 넘겨서 vision 검증이 켜져도 한 번도 안 돌았다.
                    vision_fallback=_vision_fallback_for(rep_obj),
                )
                correction_logs.extend([{"scope": "matched_report", **l} for l in log])
                rep_errs = rule_engine.validate_inspection_report(matched_report_dict)
            if rep_errs:
                rule_violations.append({
                    "scope": "matched_report",
                    "summary": rule_engine.summarize(rep_errs),
                })

    # 3b) compliance
    eval_result = compliance.evaluate(
        billing_row=billing_row,
        matched_report=matched_report_dict,
        drawing_joint_requirement=joint_requirement,
    )
    findings = eval_result["findings"]
    authority_refs = eval_result["authority_refs"]

    if rule_violations:
        findings.append({
            "rule": "rule_engine_violation",
            "details": {
                "violations": rule_violations,
                "correction_attempts": correction_logs,
                "note": "추출 결과의 형식·enum·범위 위반 — HCX-007 보정 시도 후에도 남은 위반",
            },
        })
    elif correction_logs:
        # 보정으로 모두 해결됨 — 정보성 finding
        findings.append({
            "rule": "ocr_auto_corrected",
            "details": {
                "corrections": correction_logs,
                "note": f"HCX-007 context correction 으로 {len(correction_logs)}건 자동 보정 — 검토자가 확인 권장",
            },
        })

    # 4) risk score
    risk = risk_scorer.compute(findings)

    # 5) 설명 생성 (LLM — 2차 독립 의견)
    explanation = explainer.explain(
        billing_row=billing_row,
        matched_report=matched_report_dict,
        drawing_requirements=joint_requirement,
        scwep_rules_relevant=[r for r in authority_refs if r.get("authority_level") == 2],
        code_lookup_results=[r for r in authority_refs if r.get("authority_level") == 3],
        contract_clauses_relevant=contract_clauses,
        compliance_findings=findings,
        risk_score=risk,
    )

    # 5b) 결정론 판정 (권위) ↔ LLM 판정 교차검증.
    #     정확도 최우선 정책 + "HCX 과대평가 금지": LLM 이 결정론 위반을 OK 로 못 덮음.
    #     최종 = 더 보수적(엄격) 쪽. 불일치 시 항상 needs_review.
    det_verdict, det_reasons = _deterministic_verdict(findings, match_obj, match_method)
    llm_verdict = explanation.get("verdict", "SUSPECT")
    llm_ceiling = _llm_ceiling_for(findings)
    final_verdict, verdicts_disagree = _reconcile_verdicts(det_verdict, llm_verdict, llm_ceiling=llm_ceiling)
    llm_was_capped = (llm_ceiling is not None
                      and _VERDICT_SEVERITY.get(llm_verdict, 1) > _VERDICT_SEVERITY.get(llm_ceiling, 1))
    explanation["verdict_deterministic"] = det_verdict
    explanation["verdict_llm"] = llm_verdict
    explanation["verdict"] = final_verdict      # 저장·표시에 쓰일 최종 판정 (보수적)

    finding_review_reasons = list(explanation.get("review_reasons") or [])
    finding_review_reasons.extend(det_reasons)
    if llm_was_capped:
        # "최종 보수 채택" 이라고 쓰면 거짓말이다 — 보수 쪽(LLM)을 일부러 누른 것이다.
        finding_review_reasons.append(
            f"LLM 판정={llm_verdict} 이었으나 근거 미제출·미확정 사유로 {llm_ceiling} 상한 적용 "
            f"→ 최종={final_verdict}. 시공사 근거 제출 후 재평가."
        )
    elif verdicts_disagree:
        finding_review_reasons.append(
            f"⚠ 교차검증 불일치: 결정론 판정={det_verdict} ≠ LLM 판정={llm_verdict} "
            f"→ 최종 보수 채택={final_verdict}. 검토자 확인 필수."
        )
    if final_verdict == "SUSPECT" and not finding_review_reasons:
        finding_review_reasons.append("verdict=SUSPECT — 검토자 수동 확인 필요")
    if not match_obj:
        finding_review_reasons.append("매칭된 성적서 없음")
    # Pre-LLM 룰 기반 findings 를 review_reasons 에 흘려보내기 (LLM mock 한계에 의존하지 않음)
    seen_dwg_missing = False
    for fnd in findings:
        rule = fnd.get("rule")
        if rule == "drawing_requirement_missing" and not seen_dwg_missing:
            finding_review_reasons.append(
                "도면 요구사항 DB 누락 — 자동 적합성 판정 불가, 검토자가 도면 확인 필요"
            )
            seen_dwg_missing = True
        elif rule == "drawing_no_is_ke_misentry":
            details = fnd.get("details", {})
            finding_review_reasons.append(
                f"Detailed Drawing 컬럼 오기(KE=WEP/SCWEP): {details.get('wrong_value')} "
                f"→ 시공사에 모체 도면번호 정정 요청 필요"
            )
        elif rule == "billed_ndt_covered_by_scwep":
            d = fnd.get("details", {})
            finding_review_reasons.append(
                f"도면 미요구 {d.get('requested_ndt')} 이지만 SCWEP 조건부 근거 있음 "
                f"({', '.join(d.get('basis_docs') or [])}) — 조건 '{d.get('trigger') or '?'}' 발생 여부 확인"
            )
        elif rule == "billed_ndt_basis_unclear":
            d = fnd.get("details", {})
            finding_review_reasons.append(
                f"도면 미요구 {d.get('requested_ndt')} — SCWEP 에 언급은 있으나 적용 조건 불명, 원문 확인 필요"
            )
        elif rule == "billed_ndt_basis_not_submitted":
            d = fnd.get("details", {})
            finding_review_reasons.append(
                f"도면 미요구 {d.get('requested_ndt')} — 근거 절차서(SCWEP) 미제출·미확인 "
                f"({d.get('basis_reason') or '?'}). 과다청구 단정 아님, 시공사에 제출 요청"
            )
        elif rule == "rule_engine_violation":
            details = fnd.get("details", {})
            for v in details.get("violations", []):
                summ = v.get("summary", {})
                if summ.get("count_high", 0) > 0:
                    finding_review_reasons.append(
                        f"[Rule Engine] {v.get('scope')} 검증 실패 {summ['count_high']}건(high) — OCR·LLM 오인식 의심"
                    )
    save_finding(
        s,
        billing_item_id=item.id,
        verdict=explanation.get("verdict", "SUSPECT"),
        risk_score=risk,
        summary=explanation.get("summary_korean"),
        explanation_json=explanation,
        citations_json={"findings": findings, "authority_refs": authority_refs,
                        "drawing_requirement": joint_requirement},
        recommended_action=_deterministic_action(findings) or explanation.get("recommended_action_korean"),
        needs_review=bool(explanation.get("needs_review")) or bool(finding_review_reasons),
        review_reasons_json={"reasons": finding_review_reasons} if finding_review_reasons else None,
    )


# ─────────────────────────── 결정론 판정 + 교차검증 ───────────────────────────

def _vision_fallback_for(rep_obj) -> Optional[dict]:
    """매칭된 성적서 DB 행 → ocr_corrector 의 vision_fallback (원본 PDF 쪽을 HCX-005 가 다시 본다). 행이 없으면 None."""
    if rep_obj is None or not getattr(rep_obj, "source_pdf", None):
        return None
    return {"pdf_path": Path(rep_obj.source_pdf), "page_index": int(getattr(rep_obj, "start_page", 0) or 0),
            "context_hint": f"성적서 {getattr(rep_obj, 'report_no', '') or ''} 첫 쪽 (CRST 양식)"}


# 하드 위반 (재정·계약 영향 — 과다/판정 불일치). 결정론적으로 NONCOMPLIANT.
_HARD_RULES = {
    "billed_ndt_not_in_requirements",   # 도면 미요구 NDT 청구 = 과다 청구
    "result_mismatch",                  # 청구 판정 ≠ 성적서 판정
}
# 의심 (사람 확인 필요, 위반 단정 불가). 결정론적으로 SUSPECT.
_SUSPECT_RULES = {
    "no_matching_report",               # 청구됐으나 성적서 미발견 (과다 의심 or 매칭실패)
    "drawing_no_is_ke_misentry",        # 시공사 오기 — 정정 요청 대상
    "rule_engine_violation",            # OCR·형식 위반
    "drawing_requirement_missing",      # 도면 기준 없음 — 자동 판정 불가
    "welder_mismatch",                  # 용접사 불일치
    # ── SCWEP 근거 게이트 (2026-09-05, app/analyzers/scwep_basis.py) ──
    # 도면에 없는 검사를 청구했을 때 곧장 과다청구로 확정하지 않고 근거 상태로 갈라 놓는다.
    # 셋 다 "사람이 봐야 한다" 이지 위반 확정이 아니다. 과다청구 확정은 여전히
    # billed_ndt_not_in_requirements (하드) 하나뿐이고, 그것은 이제 근거 사슬이 완전할 때만 나온다.
    "billed_ndt_basis_not_submitted",   # SCWEP 미제출·범위불명·구형식 — 시공사에 제출 요청
    "billed_ndt_basis_unclear",         # SCWEP 에 방법 언급은 있으나 조건 불명 — 조항 확인
    "billed_ndt_covered_by_scwep",      # SCWEP 조건부 요구가 지목 — 조건 발생 사실 대조 후 종결
    "pipeline_row_failed",              # 처리 중 예외 — 판정 안 됨, 사람이 봐야 함 (감사 ④)
    "low_confidence_code_ref",          # 추적 불가·저신뢰 인용 (감사 ⑧) — 근거로 쓰지 말고 원문 확인
}

# LLM 판정 상한. 이 rule 이 붙은 행에서 LLM 은 SUSPECT 위로 올라갈 수 없다.
# 왜: LLM 은 "도면에 없음" 을 보면 부적합에 표를 던지고, _reconcile_verdicts 는 엄한 쪽을 택한다.
# 상한이 없으면 근거 게이트가 SUSPECT 로 내린 것을 LLM 이 조용히 다시 NONCOMPLIANT 로 올린다 —
# 고친 것이 되돌려지는데 아무 로그도 남지 않는다. 상한은 LLM 의 **상향만** 막는다.
# 같은 행에 result_mismatch 같은 다른 하드 위반이 있으면 결정론 NONCOMPLIANT 는 그대로 산다.
_LLM_CEILING_RULES = {
    "billed_ndt_basis_not_submitted": "SUSPECT",
    "billed_ndt_basis_unclear": "SUSPECT",
    "billed_ndt_covered_by_scwep": "SUSPECT",
}
# 정보성 (verdict 에 영향 없음): required_ndt_missing(교차행), ocr_auto_corrected(보정성공)

_VERDICT_SEVERITY = {"OK": 0, "SUSPECT": 1, "NONCOMPLIANT": 2}


def _deterministic_verdict(
    findings: list[dict], match_obj, match_method: str
) -> tuple[str, list[str]]:
    """규칙 기반 결정론 판정 — LLM 무관, false positive 최소. (verdict, 사유목록)."""
    reasons: list[str] = []
    has_hard = False
    has_suspect = False
    for f in findings:
        rule = f.get("rule")
        if rule in _HARD_RULES:
            has_hard = True
            reasons.append(f"[결정론] 하드 위반: {rule}")
        elif rule in _SUSPECT_RULES:
            has_suspect = True
            reasons.append(f"[결정론] 의심: {rule}")
    # 매칭 품질
    if match_obj is None:
        has_suspect = True   # no_matching_report finding 과 중복 가능하나 안전하게
    elif match_method == "fuzzy" and (match_obj or {}).get("_match_score", 100) < 90:
        has_suspect = True
        reasons.append("[결정론] fuzzy 매칭 점수 낮음 (<90)")
    if has_hard:
        return "NONCOMPLIANT", reasons
    if has_suspect:
        return "SUSPECT", reasons
    return "OK", reasons


def _reconcile_verdicts(det: str, llm: str, *, llm_ceiling: Optional[str] = None) -> tuple[str, bool]:
    """결정론·LLM 판정 조정 → (최종 보수 판정, 불일치 여부).

    최종 = 더 엄격한 쪽 (NONCOMPLIANT > SUSPECT > OK).
    → LLM 이 결정론 위반을 OK 로 덮지 못함 (과다청구 누락 방지),
      LLM 이 추가로 잡은 것도 반영 (recall ↑).

    llm_ceiling (키워드 전용, 기본 None = 예전과 동일):
      LLM 표를 이 값 위로 올리지 않는다. 근거 게이트가 SUSPECT 로 내린 행에서 LLM 이
      NONCOMPLIANT 를 외쳐도 SUSPECT 로 눌린다. 결정론 쪽은 건드리지 않으므로
      다른 하드 위반이 있으면 여전히 NONCOMPLIANT 다. 불일치 여부는 **원본** LLM 표로 센다 —
      상한에 눌린 사실은 호출부가 따로 사유에 적는다.
    """
    det = det if det in _VERDICT_SEVERITY else "SUSPECT"
    llm = llm if llm in _VERDICT_SEVERITY else "SUSPECT"
    capped = llm
    if llm_ceiling in _VERDICT_SEVERITY and _VERDICT_SEVERITY[llm] > _VERDICT_SEVERITY[llm_ceiling]:
        capped = llm_ceiling
    final = det if _VERDICT_SEVERITY[det] >= _VERDICT_SEVERITY[capped] else capped
    return final, (det != llm)


def _llm_ceiling_for(findings: list[dict]) -> Optional[str]:
    """findings 에 상한 rule 이 하나라도 있으면 가장 낮은 상한을 돌려준다."""
    ceilings = [_LLM_CEILING_RULES[f.get("rule")] for f in findings if f.get("rule") in _LLM_CEILING_RULES]
    if not ceilings:
        return None
    return min(ceilings, key=lambda v: _VERDICT_SEVERITY.get(v, 1))


def _deterministic_action(findings: list[dict]) -> Optional[str]:
    """근거 게이트 rule 이 있으면 권고 조치를 **결정론으로** 정한다.

    지금까지는 LLM 문구를 그대로 썼는데, 판정은 부적합인데 문구는 "확인 요청" 인 식으로
    서로 다른 말을 했다. 근거 상태가 있는 행은 조치 문구도 그 상태에서 나와야 한다.
    """
    for f in findings:
        rule = f.get("rule"); d = f.get("details") or {}
        if rule == "billed_ndt_covered_by_scwep":
            docs = ", ".join(d.get("basis_docs") or []) or "제출 SCWEP"
            return (f"{docs} 조건부 요구 인용 — 조건('{d.get('trigger') or '해당 공정'}') 발생 사실을 "
                    f"1회 대조 후 종결. 과다청구 아님.")
        if rule == "billed_ndt_basis_unclear":
            return "제출 SCWEP 해당 조항 원문 확인 — 적용 조건 확정 전까지 과다청구로 단정하지 않음"
        if rule == "billed_ndt_basis_not_submitted":
            return ("시공사에 근거 절차서(SCWEP) 제출 요청. "
                    "근거 제출 전까지 과다청구로 단정하지 않음")
        if rule == "billed_ndt_not_in_requirements":
            # 확정 행: finding 이 이미 도면 요구·SCWEP 침묵을 적은 문구를 갖고 있다. LLM 의 일반 문구보다 그것이 맞다.
            return d.get("recommended_action") or None
    return None


def _match_review_reasons(
    match_obj, match_method: str, candidates_count, discrepancies: list[dict]
) -> list[str]:
    reasons: list[str] = []
    if match_obj is None:
        reasons.append("자동 매칭 실패 — 검토자가 직접 성적서 확인 필요")
        return reasons
    score = match_obj.get("_match_score")
    if match_method == "fuzzy" and score is not None and score < 90:
        reasons.append(f"fuzzy 매칭 점수 낮음 ({score:.1f})")
    if match_method == "llm":
        reasons.append("LLM 판정 매칭 — 검토자 1회 확인 권장")
    for d in discrepancies or []:
        if d.get("severity") == "high":
            reasons.append(f"고심각도 불일치: {d.get('field')} ({d.get('billing_value')} ≠ {d.get('report_value')})")
    return reasons


def _to_billing_row(item: BillingItem, br: Optional[BillingRound] = None) -> dict:
    # discipline 은 근거 게이트가 SCWEP 적용 범위를 대조하는 키다 (scwep_basis.scope_state).
    # rule_engine.validate_billing_item 은 이름이 정해진 필드만 읽으므로 추가 키는 무해.
    # raw_json 은 넣지 않는다 — ocr_corrector 의 neighboring_context 로 흘러가 보정 동작이 바뀐다.
    return {
        "discipline": br.discipline if br is not None else None,
        "billing_no": item.billing_no,
        "report_no": item.report_no,
        "joint_no": item.joint_no,
        "line_no": item.line_no,
        "welder_id": item.welder_id,
        "ndt_method": item.ndt_method,
        "drawing_no": item.drawing_no,
        "inspection_date": item.inspection_date.isoformat() if item.inspection_date else None,
        "result": item.result,
        "unit": item.unit,
        "bldg": item.bldg,
    }


def _to_report_dict(r: InspectionReport) -> dict:
    # DB 컬럼이 segment-level 결정 값 (정규식 추출). LLM extracted_json 보다 우선.
    # 매칭 키(report_no, ndt_method, inspection_date) 는 DB 컬럼으로 강제 덮어쓰기.
    d = dict(r.extracted_json or {})
    if r.report_no:
        d["report_no"] = r.report_no
    if r.ndt_method:
        d["ndt_method"] = r.ndt_method
    if r.inspection_date:
        d["inspection_date"] = r.inspection_date.isoformat()
    if r.drawing_no:
        d["drawing_no"] = r.drawing_no
    d["_db_id"] = r.id
    return d


def _find_joint_requirement(
    s: Session, item: BillingItem, *, matched_report: Optional[dict] = None, as_of=None
) -> Optional[dict]:
    # 청구 엑셀에 drawing_no 컬럼이 없는 경우(예: the plant CP-P1)
    # 매칭된 성적서의 'Drawing:' 필드(예: NP.D.X..052.DC.0001.E)를 fallback 으로 사용
    drawing_no = item.drawing_no
    if not drawing_no and matched_report:
        drawing_no = matched_report.get("drawing_no")
    if not drawing_no:
        return None

    ds = _find_drawing_set_by_prefix(s, drawing_no, as_of=as_of)
    if ds is None:
        return None
    # 성적서가 인용한 개정이 있으면 **그 개정**의 요구사항을 쓴다. 시공사는 그 개정을 근거로 검사했고,
    # 나중 개정으로 지난 작업을 판정하면 검사율이 바뀐 만큼 전 행이 조용히 틀린다 (2026-09-08 사용자 지적).
    cited = _norm_revision((matched_report or {}).get("drawing_revision"))
    rev_reasons: list[str] = []
    if cited:
        exact = _set_for_revision(s, ds.drawing_no, cited)
        if exact is not None:
            ds = exact
        else:
            rev_reasons.append(
                f"성적서가 인용한 도면 개정 {cited} 이 적재돼 있지 않다 — 적재된 것은 "
                f"{ds.set_revision or '(개정 미상)'} 다. 그 개정으로 판정하지 않았으니 "
                f"{cited} 도면을 넣고 다시 돌리거나 검토자가 직접 대조할 것")
    req = s.scalar(
        select(Requirement).where(
            Requirement.drawing_set_id == ds.id, Requirement.joint_no == item.joint_no
        )
    )
    if req is None:
        return None
    return _requirement_dict(ds, req, rev_reasons)


def _requirement_dict(ds, req, rev_reasons: list[str]) -> dict:
    return {
        # 도면 세트 자체가 재확인 대상이면 그 요구사항으로 과다청구를 확정하지 않는다 (5단계 게이트)
        "_drawing_set_needs_review": bool(getattr(ds, "needs_review", False)),
        "joint_no": req.joint_no,
        "required_ndt_json": req.required_ndt_json,
        "applicable_codes_json": req.applicable_codes_json,
        "citations_json": req.citations_json,
        "weld_type": req.weld_type,
        "thickness_mm": req.thickness_mm,
        "wps_no": req.wps_no,
        "line_no": req.line_no,
        "safety_class": req.safety_class,
        "drawing_set_id": ds.id,
        "drawing_revision": ds.set_revision,
        "needs_review": bool(rev_reasons) or bool(getattr(ds, "needs_review", False)),
        "review_reasons": list(rev_reasons),
    }


# 키릴 동형문자(С=C, О=O) 를 라틴으로 바꾼 뒤 본다 — 성적서 OCR 이 'СО2' 로 내놓는다.
_REV_HOMOGLYPH = str.maketrans("СОсо", "COco")
_REV_RE = re.compile(r"C\s*[O0Q]?\s*(\d{1,2})", re.I)


def _norm_revision(raw) -> Optional[str]:
    """'Revision C02' · 'rev. c2' · 'СО2'(키릴) · 'DC=C03' → 'C02'. 못 읽으면 None."""
    if raw is None:
        return None
    m = _REV_RE.search(str(raw).translate(_REV_HOMOGLYPH))
    return f"C{int(m.group(1)):02d}" if m else None


def _set_for_revision(s: Session, drawing_no: str, revision: str):
    """그 도면번호의 **지정 개정** 세트. 없으면 None (다른 개정으로 대신하지 않는다)."""
    from app.database.models import DrawingSet
    for ds in s.scalars(select(DrawingSet).where(DrawingSet.drawing_no == drawing_no)):
        for token in (ds.set_revision or "").split(","):
            if _norm_revision(token) == revision:
                return ds
    return None


def _find_drawing_set_by_prefix(s: Session, drawing_no_full: str, *, as_of=None):
    """청구·성적서의 도면번호 (예: 'NP.D.X..052.DC.0001.E') 와
    도면 DB 의 drawing_no (예: 'NP.D.X..052', type 직전까지) 매칭.

    1) 정확 일치
    2) 청구·성적서값 startswith 도면 DB 값 (prefix)
    3) 도면 DB 값 startswith 청구·성적서값 (역방향 — 드물지만 가능)
    """
    from app.database.models import DrawingSet
    # 1) 정확
    ds = find_effective(s, drawing_no_full, as_of=as_of)
    if ds:
        return ds
    # 2) prefix — DB 의 가장 긴 prefix 가 일치하는 세트 채택
    candidates = s.scalars(select(DrawingSet)).all()
    best = None
    best_len = 0
    for ds in candidates:
        if drawing_no_full.startswith(ds.drawing_no) and len(ds.drawing_no) > best_len:
            best, best_len = ds, len(ds.drawing_no)
    if best:
        return best
    # 3) 역방향 (드문 케이스)
    for ds in candidates:
        if ds.drawing_no.startswith(drawing_no_full):
            return ds
    return None


def _all_contract_clauses(s: Session) -> list[dict]:
    out: list[dict] = []
    for d in s.scalars(select(StandardDocument).where(StandardDocument.doc_type == "contract")):
        for c in (d.extracted_json or {}).get("clauses", []) or []:
            out.append({**c, "doc": d.document_no or d.file_path, "authority_level": 4})
    return out
