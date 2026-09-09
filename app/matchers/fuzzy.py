"""2단계 매칭: rapidfuzz 기반 후보 좁히기."""
from __future__ import annotations

from typing import Iterable

from app.config import matching_rules
from app.matchers.deterministic import _cand_values


def find_candidates(billing_row: dict, candidate_reports: Iterable[dict]) -> list[dict]:
    """field_thresholds 충족 후보 목록 반환. 점수 내림차순."""
    try:
        from rapidfuzz import fuzz
    except ImportError:  # pragma: no cover - test environments may lack rapidfuzz
        return []

    rules = matching_rules().get("fuzzy", {})
    if not rules.get("enabled", True):
        return []
    thresholds = rules.get("field_thresholds", {})
    max_n = int(rules.get("max_candidates_per_billing", 5))

    scored: list[tuple[float, dict]] = []
    for cand in candidate_reports:
        field_scores = {}
        best_val: dict = {}
        ok = True
        for field, thr in thresholds.items():
            b = str(billing_row.get(field) or "").strip()
            cvals = [str(x).strip() for x in _cand_values(cand, field) if str(x).strip()]
            if not b or not cvals:
                ok = False
                break
            best_c = max(cvals, key=lambda c: fuzz.token_set_ratio(b.upper(), c.upper()))
            score = fuzz.token_set_ratio(b.upper(), best_c.upper())
            field_scores[field] = score
            best_val[field] = best_c
            if score < thr:
                ok = False
                break
        if not ok:
            continue
        composite = sum(field_scores.values()) / len(field_scores)
        out = dict(cand)
        out["_match_rule"] = "fuzzy"
        out["_match_score"] = composite
        out["_field_scores"] = field_scores
        if "joint_no" in best_val and not cand.get("joint_no"):
            out["joint_no"] = best_val["joint_no"]
        scored.append((composite, out))

    scored.sort(reverse=True, key=lambda t: t[0])
    return [s[1] for s in scored[:max_n]]
