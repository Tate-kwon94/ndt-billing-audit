"""결정론·LLM 교차검증 verdict 로직 — 정확도 최우선 정책 테스트.

핵심 보장:
- LLM 이 결정론 하드 위반(과다청구·판정불일치)을 OK 로 덮지 못함 (false negative 방지)
- 결정론·LLM 불일치 시 항상 보수적(엄격) 채택 + 불일치 신호
"""
from __future__ import annotations

from app.analyzers.pipeline import _deterministic_verdict, _reconcile_verdicts


# ─────────────────────────── 결정론 판정 ───────────────────────────


def test_det_verdict_hard_violation_noncompliant():
    """도면 미요구 NDT 청구(과다) → NONCOMPLIANT."""
    findings = [{"rule": "billed_ndt_not_in_requirements", "details": {}}]
    v, reasons = _deterministic_verdict(findings, {"_match_score": 100}, "deterministic")
    assert v == "NONCOMPLIANT"
    assert any("하드 위반" in r for r in reasons)


def test_det_verdict_result_mismatch_noncompliant():
    findings = [{"rule": "result_mismatch", "details": {}}]
    v, _ = _deterministic_verdict(findings, {"_match_score": 100}, "deterministic")
    assert v == "NONCOMPLIANT"


def test_det_verdict_suspect_rules():
    for rule in ("no_matching_report", "drawing_no_is_ke_misentry",
                 "rule_engine_violation", "drawing_requirement_missing", "welder_mismatch"):
        findings = [{"rule": rule, "details": {}}]
        v, _ = _deterministic_verdict(findings, {"_match_score": 100}, "deterministic")
        assert v == "SUSPECT", f"{rule} should be SUSPECT, got {v}"


def test_det_verdict_informational_stays_ok():
    """required_ndt_missing(교차행)·ocr_auto_corrected 만 있으면 OK (verdict 영향 없음)."""
    findings = [
        {"rule": "required_ndt_missing", "details": {}},
        {"rule": "ocr_auto_corrected", "details": {}},
    ]
    v, _ = _deterministic_verdict(findings, {"_match_score": 100}, "deterministic")
    assert v == "OK"


def test_det_verdict_clean_ok():
    v, _ = _deterministic_verdict([], {"_match_score": 100}, "deterministic")
    assert v == "OK"


def test_det_verdict_no_match_suspect():
    v, _ = _deterministic_verdict([], None, "none")
    assert v == "SUSPECT"


def test_det_verdict_low_fuzzy_suspect():
    v, reasons = _deterministic_verdict([], {"_match_score": 80}, "fuzzy")
    assert v == "SUSPECT"
    assert any("fuzzy" in r for r in reasons)


# ─────────────────────────── 교차검증 조정 ───────────────────────────


def test_reconcile_llm_cannot_downgrade_hard():
    """결정론=NONCOMPLIANT, LLM=OK → 최종 NONCOMPLIANT (LLM 과신 차단), 불일치."""
    final, disagree = _reconcile_verdicts("NONCOMPLIANT", "OK")
    assert final == "NONCOMPLIANT"
    assert disagree is True


def test_reconcile_llm_can_escalate():
    """결정론=OK, LLM=SUSPECT → 최종 SUSPECT (recall ↑), 불일치."""
    final, disagree = _reconcile_verdicts("OK", "SUSPECT")
    assert final == "SUSPECT"
    assert disagree is True


def test_reconcile_agreement():
    final, disagree = _reconcile_verdicts("OK", "OK")
    assert final == "OK"
    assert disagree is False


def test_reconcile_unknown_llm_verdict_safe():
    """LLM 이 이상한 verdict 반환(파싱실패 등) → SUSPECT 로 안전 처리."""
    final, disagree = _reconcile_verdicts("OK", "GARBAGE")
    assert final == "SUSPECT"
    assert disagree is True


def test_reconcile_both_noncompliant():
    final, disagree = _reconcile_verdicts("NONCOMPLIANT", "NONCOMPLIANT")
    assert final == "NONCOMPLIANT"
    assert disagree is False
