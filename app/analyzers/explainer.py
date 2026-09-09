"""HCX-007 로 검토자에게 보여줄 한국어 적합성 설명 + 근거 인용 생성."""
from __future__ import annotations

from typing import Optional

from app.hcx_client import call


def explain(
    *,
    billing_row: dict,
    matched_report: Optional[dict],
    drawing_requirements: Optional[dict],
    scwep_rules_relevant: list[dict],
    code_lookup_results: list[dict],
    contract_clauses_relevant: list[dict],
    compliance_findings: list[dict],
    risk_score: int,
) -> dict:
    payload = {
        "billing_row": billing_row,
        "matched_report": matched_report,
        "drawing_requirements": drawing_requirements,
        "scwep_rules_relevant": scwep_rules_relevant,
        "code_lookup_results": code_lookup_results,
        "contract_clauses_relevant": contract_clauses_relevant,
        "compliance_findings": compliance_findings,
        "risk_score": risk_score,
    }
    resp = call("compliance_explain", payload)
    return resp.parsed or {
        "verdict": "SUSPECT",
        "summary_korean": "LLM 응답 파싱 실패 — 검토자 수동 확인 필요.",
        "findings_explained": [],
        "recommended_action_korean": "검토자가 직접 확인하세요.",
        "confidence": 0.0,
    }
