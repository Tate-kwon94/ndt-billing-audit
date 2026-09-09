"""3단계 매칭: HCX-007 로 모호한 후보군에서 1개 확정."""
from __future__ import annotations

from typing import Optional

from app.config import matching_rules
from app.hcx_client import call


def should_invoke(candidates: list[dict]) -> bool:
    rules = matching_rules().get("llm_judge", {})
    if not rules.get("enabled", True):
        return False
    if len(candidates) <= 1:
        return False
    gap_threshold = float(rules.get("invoke_if_score_gap_below", 10))
    top = candidates[0].get("_match_score", 0)
    second = candidates[1].get("_match_score", 0)
    return (top - second) < gap_threshold


def judge(billing_row: dict, candidates: list[dict]) -> dict:
    """LLM 의 매칭 판정 결과를 반환. matched_report_no None 이면 매칭 없음."""
    resp = call(
        "matching_judge",
        {
            "billing_row": billing_row,
            "candidates": [_strip(c) for c in candidates],
        },
    )
    return resp.parsed or {
        "matched_report_no": None,
        "match_confidence": 0.0,
        "reasoning": "LLM judge returned no parseable response",
        "discrepancies": [],
    }


def _strip(cand: dict) -> dict:
    keep = {
        "report_no", "joint_no", "ndt_method", "welder_id",
        "inspection_date", "result", "_match_score",
    }
    return {k: v for k, v in cand.items() if k in keep}
